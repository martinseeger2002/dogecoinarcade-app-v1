"""Digi-ID: sign in by scanning a QR code with a DigiByte wallet (DigiByte Go, Digi-ID apps), on a node that has
installed the "Digi-ID" node module from the App Store.

Digi-ID proves who is signing in. It cannot open the account's sealed wallet: only the password does that, in the
browser. So turning Digi-ID on (signed in with the password, wallet open) keeps a second sealed copy of the words IN
THAT BROWSER, under a key made of two halves: one the browser keeps, one this site keeps and hands over only after a
Digi-ID scan from the linked wallet. Neither side alone opens anything; the site never sees the words or a key that
opens them. A browser where Digi-ID was never turned on still signs in with the password.

    GET  /digiid/status             {on, linked?, devices?}       on = the node module is installed (or the setting)
    POST /digiid/start              {purpose: link|login, device} -> {uri, nonce, poll}
    POST /digiid/callback           the wallet's {address, uri, signature}   (Digi-ID protocol)
    POST /digiid/poll               {nonce, poll}                 -> waiting | done (+ half) | unknown | expired
    POST /digiid/off                {device?, all?}               signed in: forget this device, or every device

The Digi-ID URI is digiid://<host>/digiid/callback?x=<nonce> (&u=1 when the site is served over plain http, for a
test address). The wallet signs that URI with the key it derives for this host (a DigiByte P2PKH address), as a
Core-style message signature with DigiByte's message prefix.
"""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any

from fastapi import Body, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from arcade import llm as siglib

from .hash160 import hash160          # RIPEMD-160 even where OpenSSL leaves it out

SESSION_COOKIE = "arcade_session"
#: The App Store card that turns Digi-ID on for a node: a node module of this name, installed on the node's arcade.
MODULE_NAME = "Digi-ID"
MODULE_CATEGORY = "node-modules"
DB_FILE = "digiid.sqlite"
NONCE_SECONDS = 300                 # a QR code is good for five minutes
MODULE_CHECK_SECONDS = 30
STATIC = Path(__file__).with_name("static")
MAGICS = ("DigiByte Signed Message:\n", "Bitcoin Signed Message:\n")   # Digi-ID wallets use DigiByte's; BitID's too


siglib.hash160 = hash160            # recover_hash160 looks it up at call time: the same answer, on every Python build


def signed(uri: str, address: str, signature: str) -> bool:
    """Whether `signature` (base64, compact, recoverable) is `address`'s signature of `uri`, under a Digi-ID prefix."""
    want = siglib.address_hash(address)
    if want is None:
        return False
    return any(siglib.recover_hash160(signature, uri, magic) == want for magic in MAGICS)


# ------------------------------------------------------------------------------------------------ the site's records
class Store:
    """Links (a Digi-ID address -> an account) and devices (a browser that keeps a sealed copy -> its unlock half)."""

    def __init__(self, path):
        self.path = str(path)
        self.lock = threading.Lock()
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS links (address TEXT PRIMARY KEY, pubkey TEXT NOT NULL, tag TEXT NOT NULL,
                                                  created INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS devices (device TEXT PRIMARY KEY, pubkey TEXT NOT NULL, address TEXT NOT NULL,
                                                    half TEXT NOT NULL, created INTEGER NOT NULL, used INTEGER NOT NULL);
                CREATE INDEX IF NOT EXISTS devices_pubkey ON devices(pubkey);""")
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def link(self, address: str, pubkey: str, tag: str, device: str) -> str:
        """Link `address` to the account and register this device; its new unlock half (hex)."""
        half = secrets.token_hex(32)
        now = int(time.time())
        with self.lock, self._db() as db:
            held = db.execute("SELECT pubkey FROM links WHERE address=?", (address,)).fetchone()
            if held and held["pubkey"] != pubkey:
                raise PermissionError("this Digi-ID is already turned on for another account here")
            db.execute("INSERT OR REPLACE INTO links VALUES (?,?,?,?)", (address, pubkey, tag, now))
            db.execute("INSERT OR REPLACE INTO devices VALUES (?,?,?,?,?,?)", (device, pubkey, address, half, now, now))
        return half

    def unlock(self, address: str, device: str) -> dict | None:
        """The account and this device's half, when `address` is linked and this device was turned on with it."""
        with self.lock, self._db() as db:
            row = db.execute("SELECT l.pubkey, l.tag, d.half FROM links l JOIN devices d ON d.address = l.address "
                             "AND d.pubkey = l.pubkey WHERE l.address=? AND d.device=?", (address, device)).fetchone()
            if row is None:
                return None
            db.execute("UPDATE devices SET used=? WHERE device=?", (int(time.time()), device))
            return dict(row)

    def mine(self, pubkey: str) -> dict:
        with self._db() as db:
            links = [r["address"] for r in db.execute("SELECT address FROM links WHERE pubkey=?", (pubkey,))]
            devices = db.execute("SELECT COUNT(*) FROM devices WHERE pubkey=?", (pubkey,)).fetchone()[0]
        return {"linked": links, "devices": devices}

    def has_device(self, pubkey: str, device: str) -> bool:
        with self._db() as db:
            return db.execute("SELECT 1 FROM devices WHERE pubkey=? AND device=?", (pubkey, device)).fetchone() is not None

    def off(self, pubkey: str, device: str = "", everywhere: bool = False) -> int:
        with self.lock, self._db() as db:
            if everywhere:
                gone = db.execute("DELETE FROM devices WHERE pubkey=?", (pubkey,)).rowcount
                db.execute("DELETE FROM links WHERE pubkey=?", (pubkey,))
                return gone
            gone = db.execute("DELETE FROM devices WHERE pubkey=? AND device=?", (pubkey, device)).rowcount
            # a Digi-ID with no device left opens nothing: forget the link too
            db.execute("DELETE FROM links WHERE pubkey=? AND address NOT IN (SELECT address FROM devices WHERE pubkey=?)",
                       (pubkey, pubkey))
            return gone


# ------------------------------------------------------------------------------------------------ routes
def register(app, st, chains):
    store = Store(st.home / DB_FILE)
    pending: dict[str, dict] = {}        # nonce -> a QR code being scanned
    plock = threading.Lock()
    module_seen = {"at": 0.0, "on": False}

    def module_on() -> bool:
        """The site setting "digiid" ("on"/"off") wins; otherwise Digi-ID is on when the node has installed the
        Digi-ID node module (arcaded apps.installed), asked at most every 30 seconds."""
        set_to = str(st.setting("digiid") or "").lower()
        if set_to in ("on", "off"):
            return set_to == "on"
        now = time.time()
        if now - module_seen["at"] < MODULE_CHECK_SECONDS:
            return module_seen["on"]
        on = False
        try:
            installed = st.v1.cmd("apps.installed", _timeout=8)
            rows = installed.get("apps", installed) if isinstance(installed, dict) else installed
            for a in rows or []:
                card = (a.get("card") or a.get("app") or a) if isinstance(a, dict) else {}
                name = str(card.get("name") or a.get("name") or "").strip()
                category = str(card.get("category") or a.get("category") or "").strip().lower().replace(" ", "-")
                if name.lower() == MODULE_NAME.lower() and category == MODULE_CATEGORY:
                    on = True
                    break
        except Exception:                                   # noqa: BLE001  (the arcade busy: keep the last answer)
            module_seen["at"] = now - MODULE_CHECK_SECONDS + 5
            return module_seen["on"]
        module_seen.update(at=now, on=on)
        return on

    def session(request: Request):
        try:
            return st.accounts().session(request.cookies.get(SESSION_COOKIE, ""))
        except Exception:                                   # noqa: BLE001
            return None

    def host_of(request: Request) -> tuple[str, bool]:
        host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
        https = (request.headers.get("x-forwarded-proto") or request.url.scheme) == "https"
        return host, https

    def sweep(now: float) -> None:
        for n in [n for n, p in pending.items() if now - p["at"] > NONCE_SECONDS]:
            pending.pop(n, None)

    def device_id(said: dict) -> str:
        d = str(said.get("device", ""))
        return d if 16 <= len(d) <= 64 and all(c in "0123456789abcdef" for c in d) else ""

    def no(detail: str, status: int = 400) -> JSONResponse:
        return JSONResponse({"detail": detail}, status_code=status)

    @app.get("/digiid/status")
    def digiid_status(request: Request):
        on = module_on()
        account = session(request)
        said: dict[str, Any] = {"on": on}
        if account is not None:
            said.update(store.mine(account.pubkey))
            device = request.query_params.get("device", "")
            said["this_device"] = bool(device) and store.has_device(account.pubkey, device)
        return JSONResponse(said)

    @app.post("/digiid/start")
    def digiid_start(request: Request, payload: Any = Body(None)):
        if not module_on():
            return no("Digi-ID isn't turned on for this arcade.", 404)
        said = payload if isinstance(payload, dict) else {}
        purpose = str(said.get("purpose", ""))
        device = device_id(said)
        if purpose not in ("link", "login") or not device:
            return no("ask for purpose link or login, with this browser's device id")
        account = session(request)
        if purpose == "link" and account is None:
            return no("sign in with your password first, then turn on Digi-ID", 403)
        host, https = host_of(request)
        if not host:
            return no("no host")
        nonce, poll = secrets.token_hex(16), secrets.token_hex(16)
        uri = f"digiid://{host}/digiid/callback?x={nonce}" + ("" if https else "&u=1")
        now = time.time()
        with plock:
            sweep(now)
            if len(pending) > 5000:
                return no("too many Digi-ID codes waiting; try again in a minute", 429)
            pending[nonce] = {"at": now, "purpose": purpose, "device": device, "poll": poll, "uri": uri,
                              "pubkey": account.pubkey if account is not None else "", "address": ""}
        return JSONResponse({"uri": uri, "nonce": nonce, "poll": poll, "expires": NONCE_SECONDS})

    @app.post("/digiid/callback")
    async def digiid_callback(request: Request):
        """What the wallet sends after the scan: {address, uri, signature} as JSON or a form."""
        try:
            raw = await request.body()
            kind = request.headers.get("content-type", "")
            said = json.loads(raw or b"{}") if "json" in kind or raw[:1] == b"{" else \
                {k: v[0] for k, v in urllib.parse.parse_qs(raw.decode()).items()}
        except Exception:                                   # noqa: BLE001
            return no("send address, uri and signature")
        address, uri, signature = (str(said.get(k, "")).strip() for k in ("address", "uri", "signature"))
        if not (address and uri and signature) or len(uri) > 400:
            return no("send address, uri and signature")
        nonce = urllib.parse.parse_qs(urllib.parse.urlsplit(uri).query).get("x", [""])[0]
        with plock:
            sweep(time.time())
            wanted = pending.get(nonce)
        if wanted is None:
            return no("this code has expired; ask for a new one")
        if uri != wanted["uri"]:
            return no("this code was made for another address")
        if not signed(uri, address, signature):
            return no("the signature doesn't match this Digi-ID")
        with plock:
            if nonce in pending and not pending[nonce]["address"]:
                pending[nonce]["address"] = address
        return JSONResponse({"ok": True})

    @app.post("/digiid/poll")
    def digiid_poll(request: Request, payload: Any = Body(None)):
        said = payload if isinstance(payload, dict) else {}
        nonce = str(said.get("nonce", ""))
        with plock:
            sweep(time.time())
            p = pending.get(nonce)
            if p is None or not secrets.compare_digest(str(said.get("poll", "")), p["poll"]):
                return JSONResponse({"state": "expired"})
            if not p["address"]:
                return JSONResponse({"state": "waiting"})
            pending.pop(nonce, None)           # used once
        if p["purpose"] == "link":
            account = session(request)
            if account is None or account.pubkey != p["pubkey"]:
                return no("this browser is no longer signed in as the account that asked", 403)
            try:
                tag = (st.vault().by_pubkey(account.pubkey) or {}).get("tag", "")
            except Exception:                               # noqa: BLE001
                tag = ""
            try:
                half = store.link(p["address"], account.pubkey, tag, p["device"])
            except PermissionError as refused:
                return no(str(refused), 409)
            return JSONResponse({"state": "done", "address": p["address"], "tag": tag, "half": half})
        found = store.unlock(p["address"], p["device"])
        if found is None:
            return JSONResponse({"state": "unknown", "detail": (
                "That Digi-ID isn't turned on for an account in this browser. Sign in with your password, then turn "
                "on Digi-ID in Settings › Access on this device.")})
        return JSONResponse({"state": "done", "tag": found["tag"], "pubkey": found["pubkey"], "half": found["half"]})

    @app.post("/digiid/off")
    def digiid_off(request: Request, payload: Any = Body(None)):
        account = session(request)
        if account is None:
            return no("sign in first", 403)
        said = payload if isinstance(payload, dict) else {}
        gone = store.off(account.pubkey, device_id(said), everywhere=bool(said.get("all")))
        return JSONResponse({"ok": True, "forgotten": gone})

    @app.get("/digiid.js", include_in_schema=False)
    def digiid_js():
        js = (STATIC / "digiid.js").read_text()
        return Response(js, media_type="application/javascript", headers={"Cache-Control": "no-cache"})

    @app.get("/me/digiid", response_class=HTMLResponse)
    def my_digiid(request: Request):
        """Turn Digi-ID on or off for this browser (signed in)."""
        if session(request) is None:
            return RedirectResponse("/join?next=/me/digiid", status_code=303)
        return app.state.render(request, "my_digiid.html", digiid_on=module_on(), chain=chains.account_chain())

    app.state.digiid_on = module_on
