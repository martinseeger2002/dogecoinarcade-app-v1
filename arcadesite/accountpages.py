"""An account's own pages, somebody's profile, and the arcade's information pages, read from Arcade V1.0.

    /me/contacts, /me/access, /me/backup      kept in the browser (contacts, access) or a notice (backup)
    /me/wallet/tokens  + /account/tokens      what the account holds in tokens on each chain (and issues)
    /me/nfts  + /account/nfts[/collections|/pieces]   what the account holds in inscriptions
    /me/notifications  + /account/badges      replies on the account's posts, mentions, messages that paid it
    /u/{tag}, /u/{tag}/wallet, /u/{tag}/pieces       somebody's posts, holdings and collections
    /feed/{txid}/tip                          who wrote it and where they take payment
    /docs, /docs/{name}, /guide, /affiliates(.json), /terms, /terms/left, /instances, /clone
    /home, /desktop  + /account/homeapps, /account/deskbg, /account/ring/apps   the PWA home screen, the Desktop View
    /globe, /globe.json                       the nodes globe, from V1.0's mesh.overview
    /account/mainnet (POST)                   the Backup page's "the words match" answer
    /account/claimpools                       (empty: see NEEDS-V1)

/me/accounts is not served: the original shows it only on the operator's own machine, never on a public site.

NEEDS-V1: who liked, disliked, shared or tipped a post (V1.0's feed gives counts only), so Notifications has replies,
mentions and messages but no likes/shares/tips rows; sales, swaps and listings of an account's pieces (no listing book
or swap history in V1.0), so /account/nfts carries no `sale`/`listing`/`ask` and Notifications no sales; prize pools
(/account/claimpools answers none); the instance directory (/instances lists none: V1.0 keeps no announcements of
arcades); /clone has no packed source or bootstrap copies (V1.0 shares chain parts itself: share.*); the globe's own
row and city choice (the operator's tag and picks are not V1.0's to say; known tags still get their places);
inscriptions of a non-home chain in a collection (explorer.address names no collection, so they are all "loose", at
most 60); the Doginal market link on a profile's wallet (no /exchange on this site yet).
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any

from fastapi import Body, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from arcade import notify
from arcade.ledger import format_amount
from arcade.messaging import feed as feedlib
from arcade import tags as taglib

from . import legacy as L
from .feed import _shown
from .v1 import V1Error, chain_key

#: Machine pieces (a game's saves, an app's records) are no picture to show (ledger.MACHINE_TYPES).
MACHINE = "application/vnd.arcade."
#: How many of an address's pieces are read for its collections, at most.
PIECES_MOST = 5000
PER_PAGE = 20
NOTIFS_PER_PAGE = 30
#: Mentions are looked for in this many of the newest posts.
MENTION_SCAN = 100
#: Comment times looked up per page (two calls each, kept after).
TIMES_PER_PAGE = 40
HOME_KEY = re.compile(r"tab:/[a-z/-]{0,40}|store|helper|app:[A-Za-z0-9]{20,90}:[a-z0-9-]{1,80}")
HOME_DOCK, HOME_MOST, HOME_PAGES, HOME_FOLDER = 6, 120, 15, 60
HOME_BG = re.compile(r"[0-9a-f]{64}i\d{1,6}")
DESK_FITS = ("fill", "stretch", "center", "tile")
COIN_FILES = {"main": "pep", "test": "pep-test"}
LOCKED_PAGE = """<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>%(title)s · %(brand)s</title>
<style>body{font:16px/1.5 system-ui,sans-serif;margin:0;display:grid;
place-items:center;min-height:100vh;background:#12131a;color:#e8e8ea}
div{max-width:26rem;padding:2rem;text-align:center}
h1{font-size:1.2rem}p{color:#a0a0ab}a{color:%(accent)s}</style>
<div><h1>%(title)s</h1><p>%(detail)s</p>
<p><a href="/">Home</a> &middot; <a href="/feed">Feed</a> &middot; <a href="/me">Your arcade</a></p></div>
"""
_BARE_SCRIPT = re.compile(r"<script(?=[\s>])(?![^>]*\bnonce=)")


class _ChainView:
    """What the templates read of a chain (profile_wallet.html: network, label, is_doge, ...)."""

    def __init__(self, chain):
        self.network, self.label, self.params = chain.network, chain.label, chain.params
        self.is_mainnet = chain.is_mainnet
        self.is_doge = chain.network == "doge"
        self.key = chain.key


def _locked(title: str, detail: str, status: int = 403) -> HTMLResponse:
    from arcade.web import brand as brandlib
    return HTMLResponse(LOCKED_PAGE % {"title": title, "detail": detail, "brand": brandlib.current().name,
                                       "accent": brandlib.current().dark_accent}, status_code=status)


def _hue(text: str) -> int:
    return int(hashlib.sha256(str(text or "").encode()).hexdigest()[:4], 16) % 360


def _piece_row(r: dict) -> dict:
    """A V1.0 /r/inscriptions row as the original index's row (txid, number, content_type, ...)."""
    data = r.get("json") if isinstance(r.get("json"), dict) else {}
    return {"txid": str(r.get("id") or "").split("i")[0], "number": r.get("number"),
            "content_type": str(r.get("contenttype") or ""), "content_len": int(r.get("length") or 0),
            "block_height": int(r.get("block") or 0), "collection": str(r.get("collection") or ""),
            "edition": r.get("edition"), "creator": str(r.get("creator") or ""),
            "name": str(data.get("name") or "") if isinstance(data.get("name"), (str, int)) else ""}


def _held_collections(rows: list[dict], cover: int = 4) -> list[dict]:
    """ledger.held_collections: {collection, count, latest, cover: [{txid, content_type}]}, machine pieces left out."""
    groups: dict[str, dict] = {}
    for r in rows:
        if r["content_type"].startswith(MACHINE):
            continue
        g = groups.setdefault(r["collection"], {"collection": r["collection"], "count": 0, "latest": 0, "_r": []})
        g["count"] += 1
        g["latest"] = max(g["latest"], r["block_height"])
        g["_r"].append(r)
    out = []
    for g in groups.values():
        picks = sorted(g.pop("_r"), key=lambda r: (not r["content_type"].startswith("image/"), -(r["number"] or 0)))
        g["cover"] = [{"txid": r["txid"], "content_type": r["content_type"]} for r in picks[:cover]]
        out.append(g)
    return out


def _collections_held(rows: list[dict]) -> list[dict]:
    """ledger.collections_held (a profile's Collections tab): {collection, count, newest, covers: [txid]}."""
    groups: dict[str, dict] = {}
    for r in rows:
        g = groups.setdefault(r["collection"], {"collection": r["collection"], "count": 0, "newest": 0, "_r": []})
        g["count"] += 1
        g["newest"] = max(g["newest"], r["block_height"])
        g["_r"].append(r)
    out = []
    for g in groups.values():
        pics = sorted((r for r in g.pop("_r") if r["content_type"].startswith("image/")),
                      key=lambda r: -(r["number"] or 0))
        g["covers"] = [r["txid"] for r in pics[:4]]
        out.append(g)
    out.sort(key=lambda c: (c["collection"] == "", -c["count"], c["collection"].lower()))
    return out


def _held_pieces(rows: list[dict], collection: str, query: str = "", sort: str = "number", offset: int = 0,
                 limit: int = 60) -> tuple[list[dict], int]:
    """ledger.held_pieces: one page of one collection, searched by #number/edition or by name."""
    picked = [r for r in rows if r["collection"] == collection and not r["content_type"].startswith(MACHINE)]
    text = (query or "").strip().lstrip("#")
    if text.isdigit():
        picked = [r for r in picked if str(r["edition"] if r["edition"] is not None else "").startswith(text)
                  or str(r["number"]).startswith(text)]
    elif text:
        picked = [r for r in picked if text.lower() in r["name"].lower()]
    if sort == "recent":
        picked.sort(key=lambda r: (-r["block_height"], -(r["number"] or 0)))
    elif sort == "name":
        picked.sort(key=lambda r: (r["name"].lower(), r["edition"] if r["edition"] is not None else 0,
                                   r["number"] or 0))
    else:
        picked.sort(key=lambda r: (r["edition"] if r["edition"] is not None else (r["number"] or 0), r["number"] or 0))
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    page = [{k: r[k] for k in ("txid", "number", "content_type", "block_height", "edition", "name")}
            for r in picked[offset:offset + limit]]
    return page, len(picked)


def _home_pages(said: Any) -> list | None:
    """A home screen as the page sent it, cleaned, or None when it is not one (the original's _home_pages)."""
    if isinstance(said, list) and all(isinstance(k, str) for k in said):
        said = [said]
    if not isinstance(said, list) or not said or len(said) > HOME_PAGES:
        return None
    out, count, seen = [], 0, set()
    for page in said:
        if not isinstance(page, list):
            return None
        kept = []
        for it in page:
            if isinstance(it, str):
                if not HOME_KEY.fullmatch(it) or it in seen:
                    return None
                seen.add(it)
                kept.append(it)
                count += 1
            elif isinstance(it, dict):
                apps, fid = it.get("apps"), str(it.get("id", ""))
                if (not isinstance(apps, list) or not apps or len(apps) > HOME_FOLDER
                        or not re.fullmatch(r"[A-Za-z0-9_-]{1,20}", fid)):
                    return None
                for k in apps:
                    if not isinstance(k, str) or not HOME_KEY.fullmatch(k) or k in seen:
                        return None
                    seen.add(k)
                    count += 1
                kept.append({"id": fid, "folder": " ".join(str(it.get("folder") or "Folder").split())[:30] or "Folder",
                             "apps": list(apps)})
            else:
                return None
        out.append(kept)
    return out if count <= HOME_MOST else None


def _home_dock(said: Any) -> list | None:
    if not isinstance(said, list) or len(said) > HOME_DOCK:
        return None
    out: list[str] = []
    for k in said:
        if not isinstance(k, str) or not HOME_KEY.fullmatch(k):
            return None
        if k not in out:
            out.append(k)
    return out


def register(app, st, chains):
    v1 = st.v1
    kept: dict[Any, tuple[float, Any]] = {}
    kept_lock = threading.Lock()
    forever: dict[str, Any] = {}                            # confirmed facts: a tx's block time, a block's time
    import arcade
    template_dir = Path(arcade.__file__).parent / "web" / "templates"

    def keep(key, ttl: float, fn):
        with kept_lock:
            hit = kept.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        value = fn()
        with kept_lock:
            if len(kept) > 2000:
                kept.clear()
            kept[key] = (time.time(), value)
        return value

    def render(request: Request, template: str, **ctx):
        return app.state.render(request, template, **ctx)

    def signed_in(request: Request):
        return app.state.signed_in(request)

    def to_join(request: Request):
        return RedirectResponse("/join?next=" + urllib.parse.quote(request.url.path), status_code=303)

    def home_address(account) -> str:
        return chains.account_address(account.pubkey, chains.account_chain())

    def coin_icon(net: str) -> str:
        name = COIN_FILES.get(net, net)
        return f"/coin-{name}.png" if (template_dir / f"coin-{name}.png").is_file() else ""

    def chain_indexes() -> dict[str, list]:
        def build():
            try:
                return {str(c.get("key")): list(c.get("indexes") or []) for c in v1.cmd("chains.list") or []
                        if c.get("installed")}
            except V1Error:
                return {}
        return keep("indexes", 300, build)

    # ---------------------------------------------------------------- reading the chain, kept a little while
    def tag_of(address: str) -> str:
        if not address:
            return ""

        def build():
            try:
                return str((v1.page(f"address/{address}")[1] or {}).get("tag") or "")
            except V1Error:
                return ""
        return keep(("tag", address), 60, build)

    def profile_of(address: str) -> dict:
        """What somebody published about themselves (the original's _profile_of): bio, url, pfp, tag, name, mainnet,
        doge, linked {network: address}, height."""
        out = {"bio": "", "url": "", "pfp": "", "tag": "", "mainnet": "", "name": "", "doge": "", "linked": {},
               "height": 0}
        if not address:
            return out

        def build():
            try:
                return v1.cmd("profile", who=address) or {}
            except V1Error:
                return {}
        got = keep(("profile", address), 30, build)
        prof = got.get("profile") or {}
        out["tag"] = str(got.get("tag") or prof.get("tag") or "")
        for f in ("bio", "url", "pfp", "name"):
            out[f] = str(prof.get(f) or "")
        for link in prof.get("linked") or []:
            net = str(link.get("chain") or "")
            out["linked"].setdefault({"pep": "main", "peptest": "test"}.get(net, net), str(link.get("address") or ""))
        out["mainnet"] = out["linked"].get("main", "")
        out["doge"] = out["linked"].get("doge", "")
        out["height"] = int(prof.get("height") or 0)
        return out

    def address_of_tag(tag: str) -> str:
        if not tag:
            return ""
        return keep(("owner", tag), 30, lambda: v1.tag_owner(tag))

    def block_time(height: int) -> int:
        if not height:
            return 0
        if ("h", height) in forever:
            return forever[("h", height)]
        try:
            t = int((v1.cmd("explorer.block", chain="peptest", id=str(height)) or {}).get("time") or 0)
        except V1Error:
            return 0
        if t:
            forever[("h", height)] = t
        return t

    def tx_time(txid: str) -> int:
        if txid in forever:
            return forever[txid]
        try:
            height = int((v1.cmd("explorer.tx", chain="peptest", txid=txid) or {}).get("height") or 0)
        except V1Error:
            return 0
        t = block_time(height) if height else 0
        if t:
            forever[txid] = t
        return t

    def pieces_of(address: str) -> list[dict]:
        """Every piece an address holds on the home chain (V1.0's page API), as the original index's rows."""
        def build():
            out: list[dict] = []
            while len(out) < PIECES_MOST:
                try:
                    code, rows = v1.page(f"inscriptions/{address}", {"limit": 500, "offset": len(out)})
                except V1Error:
                    break
                if code != 200 or not isinstance(rows, list) or not rows:
                    break
                out += [_piece_row(r) for r in rows if r.get("held", True)]
                if len(rows) < 500:
                    break
            return out
        return keep(("pieces", address), 30, build) if address else []

    def explored(chain, address: str) -> dict | None:
        """explorer.address on one chain: balance, ledger tokens and inscriptions, ord inscriptions."""
        if not address:
            return None

        def build():
            try:
                return v1.cmd("explorer.address", chain=chain.key, address=address) or {}
            except V1Error:
                return None
        return keep(("explored", chain.key, address), 30, build)

    def ledger_props(chain) -> dict[int, dict]:
        """Every token of a chain's arcade ledger, by id (explorer.ledger_tokens), or {} without one."""
        def build():
            out: dict[int, dict] = {}
            offset = 0
            while offset < 20000:
                try:
                    got = v1.cmd("explorer.ledger_tokens", chain=chain.key, offset=offset, limit=500) or {}
                except V1Error:
                    return None
                items = got.get("items") or []
                for t in items:
                    out[int(t.get("id") or 0)] = t
                offset += len(items)
                if not items or offset >= int(got.get("total") or 0):
                    break
            return out
        return keep(("props", chain.key), 120, build)

    def other_pieces(chain, address: str) -> list[dict]:
        """A non-home chain's ledger inscriptions of an address: no collection known (NEEDS-V1), at most 60."""
        got = explored(chain, address) or {}
        return [{"txid": str(r.get("txid") or ""), "number": r.get("number"),
                 "content_type": str(r.get("content_type") or ""), "content_len": 0, "block_height": 0,
                 "collection": "", "edition": None, "creator": "", "name": ""}
                for r in got.get("ledger_inscriptions") or []]

    def held_on(chain, address: str) -> list[dict]:
        return pieces_of(address) if chain.network == chains.account_chain().network else other_pieces(chain, address)

    # ---------------------------------------------------------------- pages kept in the browser
    @app.get("/me/contacts", response_class=HTMLResponse)
    def my_contacts(request: Request):
        """An account's own address book, kept in its own browser."""
        if signed_in(request) is None:
            return to_join(request)
        return render(request, "my_contacts.html", chain=chains.account_chain())

    @app.get("/me/access", response_class=HTMLResponse)
    def my_access(request: Request):
        """The games this browser has let use the wallet, camera and microphone, or the address book: kept in this
        browser, so the page reads it there."""
        if signed_in(request) is None:
            return to_join(request)
        return render(request, "my_access.html", chain=chains.account_chain())

    @app.get("/account/ring/apps")
    def ring_apps(request: Request):
        """The apps this account switched Notifications on for, and whether this site can push at all."""
        account = app.state.signed_in_account(request)
        said = st.setting(f"ring:{account.pubkey}") or {}
        said = said if isinstance(said, dict) else {}
        apps = [a for a in (said.get("apps") or []) if isinstance(a, str) and re.fullmatch(r"[0-9a-f]{64}", a)]
        allow = [a for a in (said.get("allow") or []) if isinstance(a, str)]
        push = pusher()
        return JSONResponse({"apps": apps, "allowed": len(allow), "push": push is not None,
                             "devices": len(push.devices(account.pubkey)) if push is not None else 0},
                            headers={"Cache-Control": "no-store"})

    @app.get("/me/backup", response_class=HTMLResponse)
    def my_backup(request: Request):
        """Backup, for an account: the twelve words are the whole account, and what lives only in this browser."""
        account = signed_in(request)
        if account is None:
            return to_join(request)
        from arcade import restore_kit
        mine = st.vault().by_pubkey(account.pubkey) or {}
        known = min([t for t in (getattr(account, "created", 0), mine.get("made")) if t] or [0]) or None
        return render(request, "my_backup.html", chain=chains.account_chain(), my_name=mine.get("tag", ""),
                      real_chains=[{"network": one.network, "label": one.label,
                                    "version": one.params.pubkeyhash_version,
                                    "address": chains.account_address(account.pubkey, one)}
                                   for one in chains.account_chains() if one.is_mainnet],
                      real_on=str(st.setting(f"mainnet:{account.pubkey}", "") or "") == "yes",
                      restore_chains=restore_kit.chains(known))

    @app.post("/account/mainnet")
    def account_mainnet(request: Request, payload: Any = Body(None)):
        """Mark this account's twelve words as backed up: the browser checked them against the account's own key and
        sends only the answer. The words never come here."""
        account = app.state.signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        if not said.get("words_match"):
            return JSONResponse({"detail": "those are not your words, so they are not marked as backed up. Nothing "
                                           "was spent and nothing was changed -- check the order and the spelling "
                                           "and try again."}, status_code=400)
        st.set_setting(f"mainnet:{account.pubkey}", "yes")
        return JSONResponse({"mainnet": True, "backed_up": True,
                             "chains": [one.network for one in chains.account_chains() if one.is_mainnet]})

    # ---------------------------------------------------------------- tokens
    @app.get("/me/wallet/tokens", response_class=HTMLResponse)
    def my_wallet_tokens(request: Request):
        if signed_in(request) is None:
            return to_join(request)
        return render(request, "my_wallet_tokens.html", chain=chains.account_chain())

    def token_rows(chain, address: str) -> list[dict] | None:
        """What `address` holds in tokens on `chain`, and the ones it issues holding none; None: no ledger there."""
        props = ledger_props(chain)
        if props is None:
            return None
        units: dict[int, int] = {}
        if chain.network == chains.account_chain().network:
            code, rows = v1.page(f"balances/{address}")
            for r in rows if code == 200 and isinstance(rows, list) else []:
                units[int(r.get("propertyid") or 0)] = int(r.get("units") or 0)
        else:
            for r in (explored(chain, address) or {}).get("ledger_tokens") or []:
                units[int(r.get("propertyid") or 0)] = int(r.get("units") or 0)
        out = []
        for pid, n in units.items():
            p = props.get(pid) or {}
            divisible = int(p.get("type") or 1) == 2
            out.append({"property_id": pid, "name": str(p.get("name") or ""), "sensitive": False,
                        "issuer": str(p.get("issuer") or ""), "divisible": divisible, "balance": n,
                        "display": format_amount(n, divisible), "issuer_is_me": p.get("issuer") == address,
                        "managed": bool(p.get("managed"))})
        for pid, p in sorted(props.items()):
            if p.get("issuer") == address and pid not in units:
                out.append({"property_id": pid, "name": str(p.get("name") or ""), "issuer": address,
                            "divisible": int(p.get("type") or 1) == 2, "balance": 0, "display": "0",
                            "issuer_is_me": True, "sensitive": False, "managed": bool(p.get("managed"))})
        return out

    @app.get("/account/tokens")
    def account_tokens(request: Request):
        """What this account holds in tokens, on every chain it has an address on."""
        account = app.state.signed_in_account(request)
        out = []
        for chain in chains.account_chains():
            address = chains.account_address(account.pubkey, chain)
            if not address:
                continue
            try:
                rows = token_rows(chain, address)
            except V1Error:
                rows = None
            if rows is None:
                continue
            out.append({"network": chain.network, "label": chain.label, "mainnet": bool(chain.is_mainnet),
                        "address": address, "tokens": rows})
        return JSONResponse({"chains": out})

    @app.get("/account/claimpools")
    def account_claimpools(request: Request, chain: str = ""):
        """This account's prize pools. NEEDS-V1: none are known here."""
        app.state.signed_in_account(request)
        one = chains.by_network(chain or "test")
        if one is None:
            return JSONResponse({"detail": f"this arcade keeps no {chain} wallet"}, status_code=400)
        return JSONResponse({"chain": one.network, "pools": [], "setup": [], "next_index": 1},
                            headers={"Cache-Control": "no-store"})

    # ---------------------------------------------------------------- NFTs
    @app.get("/me/nfts", response_class=HTMLResponse)
    def my_nfts(request: Request):
        if signed_in(request) is None:
            return to_join(request)
        return render(request, "my_nfts.html", chain=chains.account_chain())

    @app.get("/account/nfts")
    def account_nfts(request: Request):
        """What this account holds, on every chain it has an address on (NEEDS-V1: no sale/listing/ask)."""
        account = app.state.signed_in_account(request)
        out = []
        for chain in chains.account_chains():
            address = chains.account_address(account.pubkey, chain)
            if not address:
                continue
            if chain.network != chains.account_chain().network and "ledger" not in chain_indexes().get(chain.key, []):
                continue
            rows = held_on(chain, address)[:200]
            out.append({"network": chain.network, "label": chain.label, "mainnet": bool(chain.is_mainnet),
                        "address": address,
                        "pieces": [{**{k: r[k] for k in ("txid", "number", "content_type", "content_len",
                                                          "block_height", "collection", "edition", "creator")},
                                    "sale": None, "listing": None, "ask": None} for r in rows]})
        return JSONResponse({"chains": out})

    def nft_chain(account, chain: str):
        picked = chains.by_network(chain) if chain else chains.account_chain()
        return picked, (chains.account_address(account.pubkey, picked) if picked else "")

    @app.get("/account/nfts/collections")
    def account_nft_collections(request: Request, chain: str = ""):
        account = app.state.signed_in_account(request)
        picked, address = nft_chain(account, chain)
        if picked is None:
            return JSONResponse({"detail": f"no chain called {chain} here"}, status_code=404)
        groups = _held_collections(held_on(picked, address)) if address else []
        return JSONResponse({"chain": picked.network, "label": picked.label, "address": address,
                             "collections": groups})

    @app.get("/account/nfts/pieces")
    def account_nft_pieces(request: Request, chain: str = "", collection: str = "", q: str = "",
                           sort: str = "number", offset: int = 0, limit: int = 60):
        account = app.state.signed_in_account(request)
        picked, address = nft_chain(account, chain)
        if picked is None:
            return JSONResponse({"detail": f"no chain called {chain} here"}, status_code=404)
        rows, total = _held_pieces(held_on(picked, address), collection, q, sort, offset, limit) if address \
            else ([], 0)
        return JSONResponse({"chain": picked.network, "collection": collection, "total": total, "offset": offset,
                             "pieces": rows})

    # ---------------------------------------------------------------- notifications
    from arcade import push as pushlib
    _push = []

    def pusher():
        if not _push:
            _push.append(pushlib.Push(st.home) if pushlib.available() else None)
        return _push[0]

    def notif_seen(pubkey: str) -> dict:
        got = st.setting(f"notif_seen:{pubkey}")
        return dict(got) if isinstance(got, dict) else {}

    def notif_events(account) -> list:
        """Replies on the account's posts, posts and comments naming its @tag, and messages that paid it."""
        me = home_address(account)
        events: list = []
        budget = [TIMES_PER_PAGE]

        def when(p: dict) -> int:
            if p.get("time") or p.get("block_time"):
                return int(p.get("time") or p.get("block_time"))
            if p.get("age_hours") is not None and p.get("height"):
                return int(time.time() - float(p["age_hours"]) * 3600)
            txid = str(p.get("txid") or "")
            if txid in forever:
                return forever[txid]
            if budget[0] <= 0:
                return 0
            budget[0] -= 1
            return tx_time(txid)

        def seq(at: int) -> int:
            return at or int(time.time())

        if me:
            try:
                mine = (v1.cmd("feed", author=me, order="new", limit=100) or {}).get("posts") or []
            except V1Error:
                mine = []
            for p in mine:
                for r in p.get("replies") or []:
                    who = str((r.get("author") or {}).get("address") or "")
                    if not who or who == me:
                        continue
                    at = when(r)
                    events.append(notify.Event(source="feed", seq=seq(at), kind="replied to", actor=who,
                                               target=str(p.get("txid") or ""), text=notify._snippet(r.get("text")),
                                               at=at, about_mine=True, extra={"txid": str(r.get("txid") or "")}))
            tag = tag_of(me)
            if tag:
                word = re.compile(rf"(?<![\w@/.])@{re.escape(tag)}(?![\w@])", re.I)
                told = {e.extra.get("txid") for e in events}
                try:
                    newest = (v1.cmd("feed", order="new", limit=MENTION_SCAN) or {}).get("posts") or []
                except V1Error:
                    newest = []
                for p in newest:
                    who = str((p.get("author") or {}).get("address") or "")
                    if who and who != me and word.search(p.get("text") or ""):
                        at = when(p)
                        events.append(notify.Event(source="mention", seq=seq(at), kind="mentioned you in a post",
                                                   actor=who, target=str(p.get("txid") or ""),
                                                   text=notify._snippet(p.get("text")), at=at,
                                                   extra={"txid": str(p.get("txid") or "")}))
                    for r in p.get("replies") or []:
                        rwho = str((r.get("author") or {}).get("address") or "")
                        if rwho and rwho != me and r.get("txid") not in told and word.search(r.get("text") or ""):
                            at = when(r)
                            events.append(notify.Event(source="mention_reply", seq=seq(at),
                                                       kind="mentioned you in a comment", actor=rwho,
                                                       target=str(p.get("txid") or ""),
                                                       text=notify._snippet(r.get("text")), at=at,
                                                       extra={"txid": str(r.get("txid") or "")}))
        push = pusher()
        if push is not None:
            def token_name(pid: str) -> str:
                try:
                    code, rows = v1.page("tokens", {"ids": str(int(pid))})
                    return str((rows or [{}])[0].get("name") or "") if code == 200 and rows else ""
                except (V1Error, ValueError):
                    return ""
            events += notify.message_events(push.arrivals(account.pubkey), token_name)
        return notify.merge(events, notif_seen(account.pubkey))

    @app.get("/me/notifications", response_class=HTMLResponse)
    def my_notifications(request: Request, page: int = 1):
        account = signed_in(request)
        if account is None:
            return to_join(request)
        events = notif_events(account)
        names: dict[str, str] = {}
        for ev in events:
            if ev.actor and ev.actor not in names:
                names[ev.actor] = tag_of(ev.actor)
        seen = notify.seen_now(events, notif_seen(account.pubkey))
        seen["message_tab"] = max(int(seen.get("message_tab", 0)), int(seen.get("message", 0)))
        st.set_setting(f"notif_seen:{account.pubkey}", seen)
        every = notify.grouped(events)
        page = max(1, int(page or 1))
        at = (page - 1) * NOTIFS_PER_PAGE
        rows = every[at:at + NOTIFS_PER_PAGE]
        more = at + NOTIFS_PER_PAGE < len(every)
        faces: dict[str, str] = {}
        for row in rows:
            for who in row.actors[:1]:
                if who and who not in faces:
                    faces[who] = profile_of(who).get("pfp") or ""
        hues = {who: _hue(who) for row in rows for who in row.actors[:1] if who}
        return render(request, "notifications.html", rows=rows, names=names, faces=faces, hues=hues, ago=notify.ago,
                      now=int(time.time()), chain=chains.account_chain(), page=page, more=more)

    @app.get("/account/badges")
    def account_badges(request: Request):
        """The red counts of the tabs, for the home screen to keep fresh without reloading."""
        account = signed_in(request)
        notif = mail = 0
        if account is not None:
            def build():
                events = notif_events(account)
                seen = notif_seen(account.pubkey)
                m = sum(1 for e in events if e.source == "message" and e.seq > int(seen.get("message_tab", 0)))
                return sum(1 for e in events if e.unread and e.source != "message") + m, m
            try:
                notif, mail = keep(("badges", account.pubkey), 20, build)
            except V1Error:
                pass
        out = {"/approvals": 0, "/messages": 0, "/notifications": notif, "/me/notifications": notif,
               "/me/messages": mail, "/feed": 0, "/exchange": 0}
        return JSONResponse(out, headers={"Cache-Control": "no-store"})

    # ---------------------------------------------------------------- somebody's profile
    def me_of(request: Request) -> dict:
        account = signed_in(request)
        home, tag = "", None
        if account is not None:
            home = home_address(account)
            tag = tag_of(home) or (st.vault().by_pubkey(account.pubkey) or {}).get("tag") or None
        return {"tag": tag, "address": home, "chain": chains.account_chain(), "min": taglib.MIN_LENGTH,
                "max": taglib.MAX_LENGTH}

    def bylines(shown: list) -> dict:
        out, stack = {}, list(shown)
        while stack:
            p = stack.pop()
            stack.extend(p.replies)
            if p.author and p.author not in out:
                prof = profile_of(p.author)
                out[p.author] = {"tag": prof["tag"] or tag_of(p.author), "face": prof["pfp"]}
        return out

    def drawable(shown: list) -> dict:
        out, stack = {}, list(shown)
        while stack:
            p = stack.pop()
            stack.extend(p.replies)
            for piece in re.findall(r"/content/([0-9a-f]{64})", p.text or ""):
                if piece not in out:
                    try:
                        code, row = v1.page(f"inscription/{piece}")
                        out[piece] = str((row or {}).get("contenttype") or "") if code == 200 else ""
                    except V1Error:
                        out[piece] = ""
        return out

    def pay_coins(request: Request, tag: str) -> list[dict]:
        """The coins a profile's Pay offers, real coins first, each opening the Wallet's Send sheet on that coin."""
        out = []
        for chain in sorted(chains.account_chains(), key=lambda c: not c.is_mainnet):
            try:
                where, _ = app.state.payee(f"@{tag}", chain)
                if not where or chains.check_address(where, chain, segwit_ok=True):
                    continue
            except Exception:                               # noqa: BLE001 -- nowhere to pay them there
                continue
            ask = urllib.parse.urlencode({"to": f"@{tag}", "coin": chain.network})
            out.append({"network": chain.network, "label": chain.label, "address": where,
                        "href": f"/me/wallet?{ask}"})
        return out

    def games_of(address: str) -> list[dict]:
        def build():
            try:
                return (v1.cmd("apps.store", sort="new") or {}).get("apps") or []
            except V1Error:
                return []
        out = []
        for a in keep("store:new", 60, build):
            if a.get("creator") != address or a.get("removed") or a.get("flag"):
                continue
            g = a.get("card") or {}
            out.append({"txid": a.get("txid"), "name": g.get("name") or "", "cover": g.get("cover") or "",
                        "opens": a.get("opens") or a.get("txid"), "version": g.get("version") or ""})
        return out

    def profile_facts(address: str) -> dict:
        out: dict[str, Any] = {"joined": 0, "joined_text": "", "posts": 0, "games": [], "collections": [],
                               "pieces": 0, "hue": 0}
        if not address:
            return out
        out["hue"] = sum(address.encode()) % 360
        try:
            out["joined"] = block_time(profile_of(address)["height"])
            if out["joined"]:
                out["joined_text"] = time.strftime("%b %Y", time.gmtime(out["joined"]))
            out["posts"] = int((v1.cmd("feed", author=address, limit=1) or {}).get("total") or 0)
        except V1Error:
            pass
        out["games"] = games_of(address)
        out["collections"] = _collections_held(pieces_of(address))
        out["pieces"] = sum(c["count"] for c in out["collections"])
        return out

    @app.get("/u/{tag}", response_class=HTMLResponse)
    def profile_page(request: Request, tag: str, before: int | None = None):
        """One person's feed. Every @tag on every page links here."""
        mine = me_of(request)
        wanted = (tag or "").strip().lstrip("@").lower()
        address = address_of_tag(wanted)
        offset = max(0, int(before or 0))
        posts, total = [], 0
        if address:
            got = v1.cmd("feed", author=address, order="new", limit=PER_PAGE, offset=offset) or {}
            posts, total = got.get("posts") or [], int(got.get("total") or 0)
        shown = [_shown(p, mine["address"]) for p in posts]
        cursor = str(offset + PER_PAGE) if offset + PER_PAGE < total else None
        drawn = drawable(shown)
        prof = profile_of(address)
        return render(request, "feed.html", chain=chains.account_chain(), posts=shown, bylines=bylines(shown),
                      drawable=drawn,
                      frames={k: app.state.frame_src(request, k) for k, t in drawn.items() if t == "text/html"},
                      cursor=cursor,
                      whose={"tag": wanted, "address": address, "claiming": False, "face": prof["pfp"],
                             **{k: prof[k] for k in ("bio", "url", "name", "mainnet")}, **profile_facts(address),
                             "pay": [] if not address or address == mine["address"] else pay_coins(request, wanted),
                             "pay_signin": signed_in(request) is None},
                      missing="", not_an_id=False, focus="", here=f"/u/{wanted}", sort="new", mine=mine,
                      kinds=feedlib.BY_NAME, friends=[], when=L._local_time, node={}, loading=None)

    @app.get("/u/{tag}/pieces")
    def profile_pieces(tag: str, collection: str = "", offset: int = 0, chain: str = ""):
        """One collection of what somebody holds, sixty at a time. Public, as the chain is."""
        wanted = (tag or "").strip().lstrip("@").lower()
        address = address_of_tag(wanted)
        if not address:
            return JSONResponse({"detail": f"nobody holds @{wanted} on this chain"}, status_code=404)
        home = chains.account_chain()
        if chain and chain != home.network:
            other = chains.by_network(chain)
            where = profile_of(address)["linked"].get(chain, "") if other is not None else ""
            if not where:
                return JSONResponse({"pieces": [], "more": False})
            rows = [r for r in other_pieces(other, where) if r["collection"] == collection]
        else:
            rows = [r for r in pieces_of(address) if r["collection"] == collection]
        offset = max(0, int(offset))
        page = rows[offset:offset + 61]
        return JSONResponse({"pieces": [{"txid": r["txid"], "number": r["number"], "content_type": r["content_type"],
                                         "edition": r["edition"]} for r in page[:60]], "more": len(page) > 60})

    @app.get("/u/{tag}/wallet", response_class=HTMLResponse)
    def profile_wallet(request: Request, tag: str):
        """What somebody holds, read from the chain for the addresses their tag names. Nothing here can be spent."""
        wanted = (tag or "").strip().lstrip("@").lower()
        address = address_of_tag(wanted)
        profile = profile_of(address)
        home = chains.account_chain()

        def theirs(chain) -> str:
            return address if chain.network == home.network else profile["linked"].get(chain.network, "")

        coins, holdings = [], []
        for chain in chains.account_chains() if address else []:
            where = theirs(chain)
            got = explored(chain, where) if where else None
            view = _ChainView(chain)
            balance = int(got["balance"]) / 100_000_000 if got and got.get("balance") is not None else None
            coins.append({"chain": view, "coin": chain.params.coin, "address": where, "balance": balance})
            if not where or got is None:
                continue
            indexes = chain_indexes().get(chain.key, [])
            if chain.network != home.network and not ({"ledger", "ord"} & set(indexes)):
                continue
            tokens = []
            if "ledger" in indexes or chain.network == home.network:
                try:
                    tokens = [t for t in token_rows(chain, where) or [] if t["balance"]]
                except V1Error:
                    tokens = []
            pieces = held_on(chain, where)
            ords = []
            if chain.network != home.network:
                ords = [{"id": str(i.get("id") or ""), "txid": str(i.get("at") or "").split(":")[0],
                         "vout": int((str(i.get("at") or "::").split(":") + ["0"])[1] or 0),
                         "network": chain.network, "kind": "doginal" if chain.network == "doge" else "ord",
                         "value": 0, "market": ""} for i in got.get("inscriptions") or [] if i.get("id")][:60]
            holdings.append({"chain": view, "address": where, "coins": balance, "tokens": tokens,
                             "pieces": pieces[:24],
                             "groups": sorted(_held_collections(pieces),
                                              key=lambda g: (g["collection"] == "", str(g["collection"]).lower())),
                             "ords": ords})
        return render(request, "profile_wallet.html", tag=wanted, address=address, profile=profile,
                      holdings=holdings, coins=coins)

    # ---------------------------------------------------------------- a tip's page
    def feed_thing(txid: str) -> dict | None:
        """The post or comment with that txid: {txid, author, text, block_time, what}."""
        try:
            posts = (v1.cmd("feed", txid=txid) or {}).get("posts") or []
        except V1Error:
            posts = []
        stack = [(p, False) for p in posts]
        while stack:
            p, reply = stack.pop()
            if p.get("txid") == txid:
                author = str((p.get("author") or {}).get("address") or "")
                when = int(p.get("time") or 0) or (int(time.time() - float(p["age_hours"]) * 3600)
                                                   if p.get("age_hours") is not None and p.get("height") else 0)
                if not when and p.get("height"):
                    when = block_time(int(p["height"]))
                if not when and reply:
                    when = tx_time(txid)
                return {"txid": txid, "author": author, "text": str(p.get("text") or ""), "block_time": when,
                        "what": "comment" if reply else "post"}
            stack.extend((r, True) for r in p.get("replies") or [])
        return None

    @app.get("/feed/{txid}/tip", response_class=HTMLResponse)
    def tip_page(request: Request, txid: str):
        """Who wrote this, where they take payment, and the form to send one."""
        txid = (txid or "").strip().lower()
        row = feed_thing(txid) if re.fullmatch(r"[0-9a-f]{64}", txid) else None
        if row is None:
            return RedirectResponse("/feed", status_code=303)
        author = row["author"]
        wheres = []
        for chain in chains.account_chains():
            where = app.state.pays_at(author, chain)
            if where:
                wheres.append({"network": chain.network, "label": chain.label, "address": where})
        return render(request, "tip.html", chain=chains.account_chain(), post=row, tag=tag_of(author),
                      author=author, wheres=wheres, when=L._local_time)

    # ---------------------------------------------------------------- the arcade's own pages
    from arcade.web import guide as guidelib
    from arcade.web import terms as termslib

    @app.get("/guide", response_class=HTMLResponse)
    def guide():
        return RedirectResponse("/docs/features.md", status_code=307)

    @app.get("/docs", response_class=HTMLResponse)
    def docs_index(request: Request):
        """Every document that ships, less the plans (never for a public site)."""
        return render(request, "docs.html", pages=guidelib.pages(private=False), bugs_url=guidelib.BUGS_URL)

    @app.get("/docs/{name:path}", response_class=HTMLResponse)
    def docs_page(request: Request, name: str):
        text = None if name in guidelib.PRIVATE else guidelib.page(name)
        if text is None:
            return RedirectResponse("/docs", status_code=303)
        return render(request, "doc.html", name=name, title=guidelib.title_of(name),
                      sections=guidelib.document(text))

    @app.get("/affiliates", response_class=HTMLResponse)
    def affiliates(request: Request):
        from arcade import affiliates as affiliateslib
        tab = affiliateslib.for_tab(st)
        return render(request, "affiliates.html", affiliates=tab["own"], shared=tab["groups"])

    @app.get("/affiliates.json")
    def affiliates_json():
        from arcade import affiliates as affiliateslib
        tab = affiliateslib.for_tab(st)
        sites = [{"url": s["url"], "name": s["name"], "by": "this arcade"} for s in tab["own"]]
        sites += [{"url": s["url"], "name": s["name"], "by": g["by"]} for g in tab["groups"] for s in g["sites"]]
        return JSONResponse({"ok": True, "sites": sites}, headers={"Cache-Control": "no-store"})

    def terms_page_of(request: Request, **context):
        return render(request, "terms.html", theme=request.cookies.get("arcade_theme", ""), **context)

    @app.get("/terms", response_class=HTMLResponse)
    def terms_page(request: Request, back: str = "/"):
        """The terms, for a visitor to accept or leave and for anybody to read again; none with the switch off."""
        if not termslib.on(st):
            return _locked("Not here", "This arcade has no terms of use to show.", status=404)
        words = termslib.text(st)
        version = termslib.version(words)
        return terms_page_of(request, back=termslib.safe_back(back), blocks=termslib.blocks(words), version=version,
                             accepted=request.cookies.get(termslib.COOKIE) == version)

    @app.get("/terms/left", response_class=HTMLResponse)
    def terms_left(request: Request):
        """Leave: a page that says so. An earlier yes goes too."""
        answer = terms_page_of(request, left=True)
        answer.delete_cookie(termslib.COOKIE)
        return answer

    @app.get("/instances", response_class=HTMLResponse)
    def instances_page(request: Request):
        """Every arcade that has said on the chain who runs it. NEEDS-V1: none are known here."""
        return render(request, "instances.html", rows=[], fee_address="", revision="",
                      hosts=sorted(st.public_hosts), when=L._local_time)

    @app.get("/clone", response_class=HTMLResponse)
    def clone_page(request: Request):
        """Run your own. NEEDS-V1: no packed source or bootstrap copies are served by this site."""
        proto = request.headers.get("x-forwarded-proto") or request.url.scheme
        return render(request, "clone.html", source=None, packing=False, copies=[],
                      here=f"{proto}://{request.headers.get('host', '')}", first_peer="",
                      seats=st.accounts().seats)

    # ---------------------------------------------------------------- the PWA home screen and the Desktop View
    #: The apps every home screen has (2026-11-12, the operator: "make the explorer app a default app"), by app key
    #: (maker and name, every version): the Arcade Explorer, published by @strawberry. The site setting `default_apps`
    #: (a list of keys) replaces it.
    DEFAULT_APPS = ["app:nWKJgSeAZSmfRCxZnwVgtYyPNowQT5X1Gk:arcade-explorer"]

    def default_apps() -> list[str]:
        got = st.setting("default_apps", None)
        return [k for k in got if isinstance(k, str)] if isinstance(got, list) else list(DEFAULT_APPS)

    #: Each card's own icon (its JSON's "icon": an inscription id, square), read once per card: a card never changes.
    #: Arcade V1.0's card list keeps the cover but not the icon, so it is read from the card inscription itself.
    card_icons: dict[str, str] = {}

    def icon_of(card_txid: str) -> str:
        if not card_txid:
            return ""
        if card_txid not in card_icons:
            got = ""
            try:
                code, row = v1.page(f"inscription/{card_txid}")
                raw = (row or {}).get("rawjson") if code == 200 else None
                said = json.loads(raw) if isinstance(raw, str) and raw else {}
                inner = said.get("game") if isinstance(said.get("game"), dict) else said.get("app")
                icon = str((inner or {}).get("icon") or "").strip().lower()
                got = icon if re.fullmatch(r"[0-9a-f]{64}", icon) else ""
            except (V1Error, ValueError, AttributeError):
                return ""                                   # asked again next time
            if len(card_icons) > 2048:
                card_icons.clear()
            card_icons[card_txid] = got
        return card_icons[card_txid]

    def shelf_apps() -> list[dict]:
        from arcade import games as gameslib

        def build():
            try:
                return (v1.cmd("apps.store", sort="popular") or {}).get("apps") or []
            except V1Error:
                return []
        out = []
        for it in keep("store:popular", 60, build):
            if it.get("removed") or it.get("flag"):
                continue
            g = it.get("card") or {}
            page = it.get("opens") or it.get("txid")
            who = str(it.get("creator_tag") or "")
            soc = it.get("social") or {}
            creator = str(it.get("creator") or "")
            out.append({
                "key": gameslib.app_key(creator, g.get("name") or ""), "name": str(g.get("name") or "")[:60],
                "page": page, "card": it.get("txid"), "open": f"/inscriptions/{page}/full",
                "cover": f"/content/{g['cover']}" if g.get("cover") else None,
                "icon": f"/content/{icon_of(str(it.get('txid') or ''))}" if icon_of(str(it.get("txid") or "")) else None,
                "genre": str(g.get("genre") or "")[:24].lower(), "mp": bool(g.get("multiplayer")), "tdeck": None,
                "desc": str(g.get("description") or "")[:400],
                "by": ("@" + who) if who else creator[:12] + "…", "address": creator,
                "version": str(g.get("version") or "")[:24], "players": str(g.get("players") or "")[:40],
                "likes": int(soc.get("likes") or 0), "comments": int(soc.get("comments") or 0),
                "perms": [p for p in ("wallet", "contacts", "groups", "media", "pictures", "notify") if g.get(p)],
                "timeout": g.get("minimized_timeout"),
            })
        return out

    @app.get("/home", response_class=HTMLResponse)
    def home_screen(request: Request):
        """The installed app's one page: the tabs and the apps as icons. Nobody signed in: the sign-in page first
        (a phone's browser tab, ?tab=1, shows it to a visitor too)."""
        if signed_in(request) is None and request.query_params.get("tab") != "1":
            return RedirectResponse("/join?next=/home", status_code=303)
        return render(request, "home.html", fullscreen=True, apps=shelf_apps(), operator_view=False,
                      default_apps=default_apps())

    @app.get("/desktop", response_class=HTMLResponse)
    def desktop_view(request: Request):
        """The Desktop View: the tabs and the apps as desktop icons, each opened in a window."""
        indexes = chain_indexes()
        listed = chains.account_chains()
        nets = [{"net": c.network, "label": c.label} for c in listed if "ord" in indexes.get(c.key, [])]
        coins = [{"net": c.network, "label": c.label, "icon": coin_icon(c.network)} for c in listed]
        try:
            node = str((v1.cmd("status") or {}).get("node") or "")[:16]
        except V1Error:
            node = ""
        system = {"version": "", "node": node,
                  "chains": [{"net": c.network, "label": c.label, "icon": coin_icon(c.network),
                              "real": bool(c.is_mainnet), "ords": "ord" in indexes.get(c.key, [])} for c in listed]}
        return render(request, "desktop.html", fullscreen=True, apps=shelf_apps(), ord_nets=nets, coins=coins,
                      system=system, operator_view=False)

    def owner_of(request: Request) -> str | None:
        account = signed_in(request)
        return account.pubkey if account is not None else None

    def home_apps(owner: str) -> tuple[list | None, list | None]:
        got = st.setting(f"homeapps:{owner}")
        if isinstance(got, dict):
            return _home_pages(got.get("pages")), _home_dock(got.get("dock"))
        return _home_pages(got), None

    @app.get("/account/homeapps")
    def homeapps_get(request: Request):
        owner = owner_of(request)
        if owner is None:
            return JSONResponse({"pages": None, "starred": [], "saved": False}, headers={"Cache-Control": "no-store"})
        pages, dock = home_apps(owner)
        starred = [k for k in (st.setting(f"favorites:{owner}") or []) if isinstance(k, str) and k.startswith("app:")]
        return JSONResponse({"pages": pages, "dock": dock, "starred": starred, "saved": True},
                            headers={"Cache-Control": "no-store"})

    @app.post("/account/homeapps")
    def homeapps_set(request: Request, payload: Any = Body(None)):
        owner = owner_of(request)
        if owner is None:
            raise HTTPException(403, "sign in first")
        said = payload if isinstance(payload, dict) else {}
        pages = _home_pages(said.get("pages", said.get("apps")))
        if pages is None:
            return JSONResponse({"detail": f"pages of icons and folders, at most {HOME_MOST} icons on "
                                           f"{HOME_PAGES} pages"}, status_code=400)
        dock = _home_dock(said["dock"]) if "dock" in said else None
        if "dock" in said and dock is None:
            return JSONResponse({"detail": f"a dock of at most {HOME_DOCK} icons"}, status_code=400)
        if dock is None:
            dock = home_apps(owner)[1]
        st.set_setting(f"homeapps:{owner}", {"pages": pages, "dock": dock})
        return JSONResponse({"pages": pages, "dock": dock, "saved": True})

    @app.get("/account/deskbg")
    def deskbg_get(request: Request):
        """The Desktop View's wallpaper: an inscription id ("" for the plain one) and how it is laid."""
        owner = owner_of(request)
        got = st.setting(f"deskbg:{owner}") if owner is not None else None
        got = got if isinstance(got, dict) else {}
        said, fit = str(got.get("id") or ""), str(got.get("fit") or "")
        return JSONResponse({"id": said if HOME_BG.fullmatch(said) else "", "fit": fit if fit in DESK_FITS else "fill"},
                            headers={"Cache-Control": "no-store"})

    @app.post("/account/deskbg")
    def deskbg_set(request: Request, payload: Any = Body(None)):
        owner = owner_of(request)
        if owner is None:
            raise HTTPException(403, "sign in first")
        said = payload if isinstance(payload, dict) else {}
        pick = str(said.get("id") or "").strip().lower()
        fit = str(said.get("fit") or "fill").strip().lower()
        if pick and not HOME_BG.fullmatch(pick):
            return JSONResponse({"detail": "an inscription id: 64 hex characters, i, a number"}, status_code=400)
        if fit not in DESK_FITS:
            return JSONResponse({"detail": "fit: fill, stretch, center or tile"}, status_code=400)
        st.set_setting(f"deskbg:{owner}", {"id": pick, "fit": fit})
        return JSONResponse({"id": pick, "fit": fit})

    # ---------------------------------------------------------------- the nodes globe
    @app.get("/globe", response_class=HTMLResponse)
    def globe_page():
        """The nodes globe: the same file GitHub Pages serves."""
        from arcade import globe as globelib
        path = globelib.page_path()
        if path is None:
            return HTMLResponse("the nodes globe is not in this copy", status_code=404)
        # Its inline scripts carry a nonce of their own (the original's `nonced`); this site's page policy names
        # no script source, so the nonce only keeps the file as the original sends it.
        nonce = secrets.token_urlsafe(16)
        text = _BARE_SCRIPT.sub(f'<script nonce="{nonce}"', path.read_text(encoding="utf-8"))
        return HTMLResponse(text, headers={"Cache-Control": "no-store"})

    @app.get("/globe.json")
    def globe_json():
        """The list the globe polls: tags and chain codes from V1.0's mesh.overview, never a host or a mesh id."""
        from arcade import globe as globelib

        def build():
            try:
                mesh = v1.cmd("mesh.overview") or {}
            except V1Error:
                return []
            rows = []
            for a in mesh.get("arcades") or []:
                rows.append({"tag": str(a.get("tag") or ""), "linked": bool(a.get("linked")), "self": False,
                             "chains": [str(c.get("key") or "") for c in a.get("chains") or []]})
            return rows
        choice = globelib.parse_choice(st.setting(globelib.SETTING))
        return JSONResponse(globelib.feed_from(keep("globe", 60, build), choice), headers={"Cache-Control": "no-store"})
