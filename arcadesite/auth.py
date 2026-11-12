"""Signing in, signing up and the sealed wallets (vaults): the original arcade's routes, with the same answers, so
its browser code (signin.js, wallet.js) works unchanged and accounts move between arcades as they do now.

    GET  /auth/challenge            a nonce to sign (Ed25519, the seat key)
    POST /auth/login                a signed nonce -> the session cookie
    POST /auth/logout, /auth/logout-everywhere
    GET  /auth/who
    GET  /signup/{tag}              is the name free: here, on the chain (V1.0), on another arcade
    POST /signup                    name + seat key + address (proved) + sealed wallet; welcome coins
    GET  /signin/{tag}[?here=1]     the sealed wallet for a name, from here or (not ?here=1) another arcade
    POST /account/vault             the sealed wallet again, under a new password
    GET  /signin-import             another arcade's /signin answer, for a browser that can't fetch cross-origin

The password never arrives: the browser opens and seals the wallet. What the chain says (who holds a @tag) comes
from Arcade V1.0; the arcades that may hold a vault are V1.0's wss:// peers and the well-known ones.
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, TimeoutError as Late, as_completed
from typing import Any

import requests
from fastapi import Body, HTTPException, Request
from fastapi.responses import JSONResponse

from arcade import accounts as accountslib
from arcade import apikeys as apikeylib
from arcade import tags as taglib
from arcade.web import door as doorlib

from .v1 import V1Error

SESSION_COOKIE = "arcade_session"
#: Arcades with a public web address that hold accounts (asked even when the mesh names none), as V1.0's list.
KNOWN_HOSTS = ("pepecoinarcade.com", "app.dogecoinarcade.com")
NAME_CHECK_WAIT = 4.0
SIGNUP_ELSEWHERE_WAIT = 6.0


class ArcadeBusy(Exception):
    pass


def register(app, st, chains):
    """Add the routes. `st` = SiteState; `chains` = the account chains (chains.py)."""
    elsewhere_kept: dict[str, tuple[float, dict | None]] = {}

    # ---------------------------------------------------------------- who is asking
    def origin(request: Request) -> str:
        proto = request.headers.get("x-forwarded-proto") or request.url.scheme
        return f"{proto}://{request.headers.get('host', '')}"

    def over_https(request: Request) -> bool:
        return (request.headers.get("x-forwarded-proto") or request.url.scheme) == "https"

    def signs_in_here(request: Request, signed: str) -> bool:
        """A sign-in signed for one of this site's own names (the browser signs location.origin)."""
        try:
            parts = urllib.parse.urlsplit(str(signed or "").strip())
        except ValueError:
            return False
        if parts.scheme not in ("http", "https") or parts.path or parts.query or parts.fragment or "@" in parts.netloc:
            return False
        name = doorlib.host_name(parts.netloc)
        if not name or name == st.pages_hostname:
            return False
        if name in st.public_hosts:
            return True
        # This machine's own names, only for a request that came straight to it (no edge in between).
        edge = any(h in request.headers for h in doorlib.EDGE_HEADERS)
        peer = request.client.host if request.client else ""
        if not edge and peer in ("127.0.0.1", "::1"):
            return doorlib.loopback_host(name, st.local_hosts)
        return False

    def signed_in(request: Request):
        try:
            return st.accounts().session(request.cookies.get(SESSION_COOKIE, ""))
        except Exception:                                   # noqa: BLE001
            return None

    def signed_in_account(request: Request):
        account = signed_in(request)
        if account is None:
            raise HTTPException(403, "sign in first")
        return account

    def set_session(answer, token: str, request: Request) -> None:
        answer.set_cookie(SESSION_COOKIE, token, max_age=accountslib.SESSION_DAYS * 86400,
                          httponly=True, samesite="lax", secure=over_https(request))

    def wait_answer(seconds: int, why: str) -> JSONResponse:
        seconds = max(1, int(seconds))
        said = f"{seconds} seconds" if seconds < 90 else f"{(seconds + 59) // 60} minutes"
        return JSONResponse({"detail": f"{why}. Wait {said}.", "wait": seconds}, status_code=429,
                            headers={"Retry-After": str(seconds)})

    app.state.arcade_hosts = lambda: arcade_hosts()
    app.state.signed_in = signed_in
    app.state.signed_in_account = signed_in_account
    app.state.set_session = set_session

    # ---------------------------------------------------------------- vaults on other arcades
    def arcade_hosts() -> list[str]:
        mine = st.public_hosts
        seen, out = set(), []
        for h in list(KNOWN_HOSTS) + st.v1.hosts():
            h = h.strip().lower()
            if h and h not in seen and h not in mine and re.fullmatch(r"[a-z0-9.\-]+\.[a-z]{2,}", h):
                seen.add(h)
                out.append(h)
        return out[:16]

    def ask_vault(host: str, name: str, owner: str = "", timeout: float = 8) -> dict | None:
        got = requests.get(f"https://{host}/signin/{urllib.parse.quote(name)}?here=1", timeout=timeout,
                           headers={"User-Agent": "arcade-node"}, allow_redirects=False)
        if got.status_code == 429:
            raise ArcadeBusy(host)
        said = got.json() if got.status_code == 200 else None
        blob = said.get("blob") if isinstance(said, dict) else None
        if not isinstance(blob, dict) or not blob.get("sealed"):
            return None
        if owner and str(said.get("address") or "") != owner:
            return None
        return {"tag": str(said.get("tag") or name), "pubkey": str(said.get("pubkey") or "").lower(),
                "address": str(said.get("address") or ""), "blob": blob, "from": f"https://{host}",
                "elsewhere": True}

    def vault_elsewhere(tag: str, wait: float | None = None, ask: bool = True) -> dict | None:
        """A sealed wallet from whichever known arcade holds it (the chain's owner of the @name only, when it names
        one); kept a few minutes. `wait`: at most this long; a later answer is kept for the next ask."""
        name = str(tag or "").strip().lstrip("@").lower()
        if not re.fullmatch(r"[a-z0-9_.\-]{1,40}", name):
            return None
        kept = elsewhere_kept.get(name)
        if kept and time.time() - kept[0] < (600 if kept[1] else 60):
            return kept[1]
        if not ask:
            return None
        owner = st.v1.tag_owner(name)

        def keep_late(job) -> None:
            try:
                got = job.result()
            except Exception:                               # noqa: BLE001
                return
            if got:
                elsewhere_kept[name] = (time.time(), got)

        found = None
        hosts = arcade_hosts()
        if hosts:
            pool = ThreadPoolExecutor(max_workers=min(8, len(hosts)))
            jobs = [pool.submit(ask_vault, h, name, owner) for h in hosts]
            try:
                for job in as_completed(jobs, timeout=wait):
                    try:
                        found = job.result()
                    except Exception:                       # noqa: BLE001
                        found = None
                    if found:
                        break
            except Late:
                elsewhere_kept[name] = (time.time(), None)
                for job in jobs:
                    job.add_done_callback(keep_late)
                return None
            finally:
                pool.shutdown(wait=wait is None)
        elsewhere_kept[name] = (time.time(), found)
        return found

    # ---------------------------------------------------------------- sessions
    @app.get("/auth/challenge")
    def auth_challenge(request: Request):
        return JSONResponse(st.accounts().challenge(origin(request)))

    @app.post("/auth/login")
    def auth_login(request: Request, payload: Any = Body(None)):
        said = payload if isinstance(payload, dict) else {}
        register_ = st.accounts()
        try:
            token = register_.login(str(said.get("pubkey", "")), str(said.get("nonce", "")),
                                    str(said.get("signature", "")), origin=origin(request),
                                    ip=apikeylib.client_ip(request), join=bool(said.get("join")),
                                    signed=str(said.get("origin") or "")[:300],
                                    accept=lambda signed: signs_in_here(request, signed))
        except accountslib.SeatsFull as full:
            return JSONResponse({"detail": str(full), "seats": register_.seats, "free": 0}, status_code=409)
        except accountslib.AccountError as refused:
            return JSONResponse({"detail": str(refused)}, status_code=403)
        account = register_.account(str(said.get("pubkey", "")))
        answer = JSONResponse({"pubkey": account.pubkey, "tag": "", "created": account.created,
                               "free": register_.free()})
        set_session(answer, token, request)
        return answer

    @app.post("/auth/logout")
    def auth_logout(request: Request):
        st.accounts().logout(request.cookies.get(SESSION_COOKIE, ""))
        answer = JSONResponse({"pubkey": None})
        answer.delete_cookie(SESSION_COOKIE)
        return answer

    @app.post("/auth/logout-everywhere")
    def auth_logout_everywhere(request: Request):
        account = signed_in_account(request)
        ended = st.accounts().end_sessions(account.pubkey)
        answer = JSONResponse({"pubkey": None, "ended": ended})
        answer.delete_cookie(SESSION_COOKIE)
        return answer

    @app.get("/auth/who")
    def auth_who(request: Request):
        account = signed_in(request)
        register_ = st.accounts()
        if account is None:
            return JSONResponse({"pubkey": None, "free": register_.free(), "seats": register_.seats})
        return JSONResponse({"pubkey": account.pubkey, "tag": "", "created": account.created, "seen": account.seen,
                             "free": register_.free(), "seats": register_.seats, "operator": False,
                             "claimable": False, "handover": False})

    # ---------------------------------------------------------------- signing up
    @app.get("/signup/{tag}")
    def signup_free(tag: str, request: Request):
        try:
            wanted = taglib.validate((tag or "").strip().lstrip("@"))
        except taglib.TagError as bad:
            return JSONResponse({"free": False, "detail": str(bad)})
        here = st.vault().taken(wanted)
        on_chain = st.v1.tag_owner(wanted)
        elsewhere = None
        if not here and not on_chain:
            register_ = st.accounts()
            who = f"namecheck:{apikeylib.client_ip(request)}"
            now = int(time.time())
            limited = register_._rate_limited(who, now)
            if not limited:
                register_._attempt(who, now)
            elsewhere = vault_elsewhere(wanted, wait=NAME_CHECK_WAIT, ask=not limited)
        there = (elsewhere or {}).get("from", "").removeprefix("https://")
        return JSONResponse({
            "tag": wanted, "free": not here and not on_chain and not there,
            "here": here, "on_chain": bool(on_chain), "elsewhere": there,
            "detail": (f"@{wanted} is already signed up on this node" if here else
                       f"@{wanted} is claimed on the chain" if on_chain else
                       f"@{wanted} already has an account on {there}: sign in instead, with the same name and "
                       f"password, and this arcade brings it here" if there else ""),
        })

    @app.post("/signup")
    def signup(request: Request, payload: Any = Body(None)):
        from arcade import wallet_imports
        said = payload if isinstance(payload, dict) else {}
        home = chains.account_chain()
        try:
            wanted = taglib.validate(str(said.get("tag", "")).strip().lstrip("@"))
            pubkey = str(said.get("pubkey", "")).strip().lower()
            address = str(said.get("address", "")).strip()
            blob = said.get("blob")
            if not isinstance(blob, dict) or not blob.get("sealed"):
                raise ValueError("that is not an encrypted wallet")
            accountslib.check_sealed(blob)
            complaint = chains.check_address(address, home)
            if complaint:
                raise ValueError(complaint)
            owner = st.v1.tag_owner(wanted)
            if owner and owner != address:
                raise ValueError(f"@{wanted} is claimed on the chain by another address")
            if not owner and not st.vault().taken(wanted):
                held = vault_elsewhere(wanted, wait=SIGNUP_ELSEWHERE_WAIT)
                if held and held.get("pubkey") and held["pubkey"] != pubkey:
                    there = str(held.get("from") or "").removeprefix("https://")
                    raise ValueError(f"@{wanted} already has an account on {there}: sign in instead, with the "
                                     f"same name and password, and this arcade brings it here")
            register_ = st.accounts()
            if st.vault().by_pubkey(pubkey) is not None or register_.account(pubkey) is not None:
                raise ValueError("this key already has an account on this arcade: sign in instead")
            try:
                wallet_imports.check_proof(home.params, pubkey, home.network, address,
                                           str(said.get("coin_pubkey", "")), "p2pkh",
                                           str(said.get("address_signature", "")))
            except wallet_imports.ImportRefused:
                raise ValueError("this sign-up did not prove the address is yours: reload the page and try again") \
                    from None
            token = register_.login(pubkey, str(said.get("nonce", "")), str(said.get("signature", "")),
                                    origin=origin(request), ip=apikeylib.client_ip(request), join=True,
                                    signed=str(said.get("origin") or "")[:300],
                                    accept=lambda signed: signs_in_here(request, signed))
            st.vault().put(wanted, pubkey, address, json.dumps(blob))
            came = str(said.get("from") or "").strip().rstrip("/").lower()
            if came:
                st.set_setting(f"came_from:{pubkey}", came if came.startswith("https://") else "https://" + came)
        except accountslib.SeatsFull as full:
            return JSONResponse({"detail": str(full)}, status_code=409)
        except (taglib.TagError, accountslib.AccountError, ValueError) as bad:
            return JSONResponse({"detail": str(bad)}, status_code=400)
        st.set_setting(f"address:{pubkey}", address)
        coin = str(said.get("coin_pubkey", "")).strip().lower()
        if coin:
            st.set_setting(f"coinkey:{pubkey}", coin)
        # The same words' addresses on the other chains, each proved by its own key (as the original).
        for other in chains.account_chains()[1:]:
            there_addr = str(said.get(f"address_{other.network}", "")).strip()
            if not there_addr or chains.check_address(there_addr, other):
                continue
            their_key = str(said.get(f"coin_pubkey_{other.network}", "")).strip().lower()
            try:
                wallet_imports.check_proof(other.params, pubkey, other.network, there_addr, their_key, "p2pkh",
                                           str(said.get(f"address_signature_{other.network}") or ""))
            except wallet_imports.ImportRefused:
                continue
            if chains.address_held_by_another(pubkey, other, there_addr):
                continue
            st.set_setting(chains.address_key(pubkey, other), there_addr)
            if their_key:
                st.set_setting(chains.coinkey_key(pubkey, other), their_key)
        given, why_not = 0, ""
        try:
            given = chains.welcome(pubkey, address, ip=apikeylib.client_ip(request))
        except Exception as exc:                            # noqa: BLE001 -- never fatal: the account exists
            why_not = str(exc)
        answer = JSONResponse({"tag": wanted, "pubkey": pubkey, "address": address, "fruit": False,
                               "given": given, "no_coins": why_not})
        set_session(answer, token, request)
        return answer

    # ---------------------------------------------------------------- the sealed wallets
    @app.get("/signin/{tag}")
    def signin_blob(request: Request, tag: str):
        register_ = st.accounts()
        who = apikeylib.client_ip(request)
        now = int(time.time())
        wait = register_.wait_for(f"vault:{who}", now)
        if wait:
            return wait_answer(wait, "too many sign-ins from here in a row")
        register_._attempt(f"vault:{who}", now)
        wait = register_.vault_lookup_wait(tag, who, now)
        if wait:
            return wait_answer(wait, "this name's wallet was asked for from too many places this hour")
        found = st.vault().get(tag)
        if found is None:
            if request.query_params.get("here") != "1":
                elsewhere = vault_elsewhere(tag)
                if elsewhere is not None:
                    return JSONResponse(elsewhere)
            return JSONResponse({"detail": "no wallet here by that name"}, status_code=404)
        return JSONResponse({"tag": found["tag"], "pubkey": found["pubkey"], "address": found["address"],
                             "blob": json.loads(found["blob"])})

    @app.post("/account/vault")
    def account_vault(request: Request, payload: Any = Body(None)):
        account = signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        blob = said.get("blob")
        try:
            try:
                sealed = blob if isinstance(blob, dict) else json.loads(str(blob or "{}"))
            except ValueError:
                sealed = {}
            accountslib.check_sealed(sealed)
            text = json.dumps(blob) if isinstance(blob, dict) else str(blob or "")
            if not st.vault().replace_blob(account.pubkey, text):
                return JSONResponse({"detail": ("this node keeps no wallet for these words. If you signed up on "
                                                "another node, restore there, or open a backup file here.")},
                                    status_code=404)
        except accountslib.AccountError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        ended = st.accounts().end_sessions(account.pubkey, keep=request.cookies.get(SESSION_COOKIE, ""))
        return JSONResponse({"ok": True, "ended": ended})

    @app.get("/signin-import")
    def signin_import(request: Request, node: str = "", tag: str = ""):
        """Only from the arcades the operator lists (`import_from`), over https: a public site must not fetch
        wherever a stranger names."""
        node = str(node or "").strip().rstrip("/")
        if node and "://" not in node:
            node = "https://" + node
        if not re.fullmatch(r"https://[A-Za-z0-9.\-]+(:\d+)?", node):
            return JSONResponse({"detail": "that is not an arcade's address, like https://app.dogecoinarcade.com"},
                                status_code=400)
        allowed = [str(h).lower().rstrip("/") for h in (st.setting("import_from") or [])]
        if node.lower() not in allowed:
            listed = ", ".join(allowed) if allowed else "none"
            return JSONResponse({"detail": "this arcade brings accounts only from the arcades its operator lists "
                                           f"({listed}). From another one, sign in with the file its Backup page "
                                           "saves.", "allowed": allowed}, status_code=403)
        register_ = st.accounts()
        who = apikeylib.client_ip(request)
        now = int(time.time())
        if register_._rate_limited(f"import:{who}", now):
            return JSONResponse({"detail": "too many in a row. Wait a few minutes."}, status_code=429)
        register_._attempt(f"import:{who}", now)
        name = str(tag or "").strip().lstrip("@")
        if not re.fullmatch(r"[A-Za-z0-9_.\-]{1,40}", name):
            return JSONResponse({"detail": "that is not a name"}, status_code=400)
        try:
            answer = requests.get(f"{node}/signin/{urllib.parse.quote(name)}", timeout=15, allow_redirects=False)
            said = answer.json()
        except Exception:                                   # noqa: BLE001
            return JSONResponse({"detail": f"{node} did not answer"}, status_code=502)
        if answer.status_code != 200:
            return JSONResponse({"detail": f"{node} says: " + str(said.get("detail") or "no wallet by that name")},
                                status_code=404)
        blob = said.get("blob") if isinstance(said, dict) else None
        if not isinstance(blob, dict) or not blob.get("sealed"):
            return JSONResponse({"detail": f"{node} did not send a wallet"}, status_code=502)
        return JSONResponse({"tag": str(said.get("tag") or name), "pubkey": str(said.get("pubkey") or ""),
                             "address": str(said.get("address") or ""), "blob": blob, "from": node})

    # ---------------------------------------------------------------- sealed side records (opaque to the site)
    @app.get("/account/mailbox")
    def mailbox_get(request: Request):
        account = signed_in_account(request)
        have = st.mailbox().get(account.pubkey)
        return JSONResponse(have or {"blob": "", "updated": 0})

    @app.post("/account/mailbox")
    def mailbox_put(request: Request, payload: Any = Body(None)):
        account = signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        try:
            base = int(said.get("base") or 0)
        except (TypeError, ValueError):
            return JSONResponse({"detail": "the base is not a number"}, status_code=400)
        try:
            out = st.mailbox().put(account.pubkey, str(said.get("blob") or ""), base)
        except accountslib.MailboxMoved as exc:
            return JSONResponse({"detail": str(exc), "updated": exc.updated}, status_code=409)
        except (accountslib.AccountError, ValueError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        return JSONResponse(out)

    return signed_in, signed_in_account
