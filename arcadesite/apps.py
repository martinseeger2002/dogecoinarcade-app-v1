"""Inscribed apps on the site: content in its sandbox, the page API, and a page asking the reader to send.

An inscribed page runs in a sandboxed frame (opaque origin, no cookies), loaded from the pages host
(`pages_host`, e.g. pages.dogecoinarcade.com) as /content/<id>?v=<ticket>. The ticket names the signed-in reader
looking at it (arcade/web/viewer_tickets.py); the page's own /r/ calls carry it (the viewer shim added to the page,
and its Referer). So:

    /content/<id>         the bytes, from V1.0, with the original's sandbox headers (+ the viewer shim with ?v=)
    /r/wallet             the READER's wallet (their address, coins, tokens, inscriptions), never anybody else's
    /r/send, /r/send/<id> a question for the reader ("pay 5 GOLD to @x?"), answered in the reader's own page
    /account/pagesends    ... which that page polls, and /account/pagesends/answer, which it answers
    /r/<name>.js          the shims (storage, realtime, groups, contacts, access, app, node, swap, owner, state, ...)
    /r/<anything else>    read from V1.0's page API, as it is

On the pages host only /content/, /r/ and /site/ are served (the original's PAGES_DOOR).
"""
from __future__ import annotations

import json
import re
import secrets
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any

from fastapi import Body, Request
from fastapi.responses import JSONResponse, Response

from arcade.web import content as contentlib
from arcade.web import door as doorlib
from arcade.web.viewer_tickets import Tickets

from .v1 import V1Error, chain_key

SHIMS = {"groups": "groups.js", "contacts": "contacts.js", "llm": "llm.js", "access": "access.js", "app": "app_js.js",
         "storage": "storage.js", "node": "node.js", "swap": "swap.js", "owner": "owner.js", "state": "state.js",
         "escrow": "escrow.js", "realtime": "realtime.js"}
#: How long a page's question waits for its reader (the original's PAGE_SEND_FOR).
PAGE_SEND_FOR = 600
#: Page API paths answered here, not relayed: they speak for the reader.
OWN = ("wallet", "send")
VIEWER_SHIM = (
    "<script>/* DogecoinArcade viewer shim: added by the node that served this "
    "page, not part of the inscription. It adds the viewer's ticket to this "
    "page's own /r/ calls. */(function(){var m=/[?&]v=([A-Za-z0-9_-]+)/"
    ".exec(location.search);if(!m)return;var v=m[1];function add(u){try{var "
    "x=new URL(String(u),location.href);if(x.host===location.host&&x.pathname"
    ".indexOf('/r/')===0&&!x.searchParams.has('v')){x.searchParams.set('v',v);"
    "return x.href}}catch(e){}return u}var f=window.fetch;if(f)window.fetch="
    "function(i,o){if(typeof i==='string'||(window.URL&&i instanceof URL))i=add(i);"
    "else if(i&&i.url){var n=add(i.url);if(n!==i.url)i=new Request(n,i)}return "
    "f.call(this,i,o)};var X=window.XMLHttpRequest&&XMLHttpRequest.prototype.open;"
    "if(X)XMLHttpRequest.prototype.open=function(){if(arguments.length>1)arguments[1]"
    "=add(arguments[1]);return X.apply(this,arguments)}})();</script>")


def _json(v, status: int = 200) -> Response:
    return Response(json.dumps(v), status_code=status, media_type="application/json",
                    headers={**contentlib.CORS, "Cache-Control": "no-store"})


CONTENT_CHAINS = ("main", "doge", "dgb", "flop", "wjk", "kot", "ltc")   # tried in turn for an id not on the home chain


def register(app, st, chains, template_dir: Path):
    v1 = st.v1
    tickets = Tickets(st.home / "viewer-tickets.sqlite")
    sends: dict[str, dict] = {}
    refs: dict[tuple, str] = {}
    lock = threading.Lock()
    pages_host = st.pages_hostname
    pages_origin = str(st.setting("pages_origin") or (f"https://{pages_host}" if pages_host else "")).rstrip("/")

    # ---------------------------------------------------------------- the pages host serves pages and nothing else
    @app.middleware("http")
    async def pages_door(request: Request, call_next):
        host = doorlib.host_name(request.headers.get("host", ""))
        if pages_host and host == pages_host and not doorlib.pages_path(request.url.path):
            return Response("not on this host\n", status_code=404, media_type="text/plain")
        return await call_next(request)

    # ---------------------------------------------------------------- who is looking: the frame's ticket
    def home_address(account) -> str:
        return chains.account_address(account.pubkey, chains.account_chain())

    def ticket_for(request: Request, piece: str) -> str:
        """A ticket for the signed-in reader to look at one page (empty when nobody is signed in)."""
        account = app.state.signed_in(request)
        here = home_address(account) if account is not None else ""
        return tickets.mint(here, "test", piece.split("i")[0]) if here else ""

    def frame_src(request: Request, piece: str, chain: str = "") -> str:
        t = ticket_for(request, piece)
        q = [f"v={t}"] if t else []
        if chain and chain != "test":
            q.append(f"chain={chain}")
        return f"{pages_origin}/content/{piece}" + ("?" + "&".join(q) if q else "")

    app.state.frame_src = frame_src

    def viewer_of(request: Request) -> tuple[str, str]:
        """(the reader's address, the page) a /r/ call speaks for, from its ticket (?v= or the Referer's)."""
        ticket = request.query_params.get("v", "")
        page = ""
        refer = request.headers.get("referer", "")
        if refer:
            said = urllib.parse.urlparse(refer)
            ticket = ticket or (urllib.parse.parse_qs(said.query).get("v") or [""])[0]
            if said.path.startswith("/content/"):
                page = said.path[len("/content/"):].split("/")[0].split("i")[0]
        held = tickets.get(ticket or "")
        if not held:
            return "", ""
        if page and page != held[2]:
            return "", ""                                   # one page's ticket, not every page's
        return held[0], held[2]

    # ---------------------------------------------------------------- the bytes, sandboxed
    found_on: dict[str, str] = {}                           # inscription -> the chain it was found on

    @app.get("/content/{piece}")
    def content(piece: str, request: Request, v: str = "", download: int = 0, chain: str = ""):
        if not re.fullmatch(r"[0-9a-fA-F]{64}(i[0-9]+)?", piece):
            return Response("no such inscription\n", status_code=404, media_type="text/plain")
        # Another chain's inscription: V1.0 reads the chain an app page on it names (its Referer, /app/<chain>/<id>).
        refer = f"{v1.base}/app/{chain_key(chain)}/{piece.split('i')[0].lower()}" \
            if re.fullmatch(r"[a-z]{2,10}", chain or "") and chain != "test" else ""
        txid = piece.split('i')[0].lower()
        try:
            if not refer and txid in found_on:
                refer = f"{v1.base}/app/{found_on[txid]}/{txid}"
            r = v1.raw("GET", f"/content/{piece}", headers={"referer": refer})
            if r.status_code == 404 and not refer:
                # Not on the home chain: the old site served every chain's inscriptions at /content/ (feed, apps).
                for one in CONTENT_CHAINS:
                    key = chain_key(one)
                    got = v1.raw("GET", f"/content/{piece}", headers={"referer": f"{v1.base}/app/{key}/{txid}"})
                    if got.status_code == 200:
                        r = got
                        if len(found_on) > 4096:
                            found_on.clear()
                        found_on[txid] = key
                        break
        except V1Error:
            return Response("the arcade behind this site did not answer\n", status_code=503, media_type="text/plain")
        if r.status_code != 200:
            return Response(r.content, status_code=r.status_code, media_type=r.headers.get("content-type"))
        kind = r.headers.get("content-type", "application/octet-stream")
        headers = dict(contentlib.CONTENT_HEADERS)
        proto = request.headers.get("x-forwarded-proto") or request.url.scheme
        headers["Content-Security-Policy"] = contentlib.own_origin(headers["Content-Security-Policy"], proto,
                                                                   request.headers.get("host", ""))
        body = r.content
        if download:
            headers["Content-Disposition"] = f'attachment; filename="{piece}"'
        elif v and kind.startswith("text/html"):
            found = re.match(rb"\s*<!doctype[^>]*>", body, re.I)
            at = found.end() if found else 0
            body = body[:at] + VIEWER_SHIM.encode() + body[at:]
            headers["Cache-Control"] = "no-store"           # one viewer's ticket, not for a cache
        return Response(body, media_type=kind, headers=headers)

    # ---------------------------------------------------------------- the shims
    def shim(name: str):
        def handler():
            return Response((template_dir / SHIMS[name]).read_text(), media_type="application/javascript",
                            headers={**contentlib.CORS, "Cache-Control": "public, max-age=3600"})
        return handler

    for name in SHIMS:
        app.add_api_route(f"/r/{name}.js", shim(name), methods=["GET"], include_in_schema=False)

    # ---------------------------------------------------------------- the reader's wallet
    @app.get("/r/wallet")
    def r_wallet(request: Request):
        address, _ = viewer_of(request)
        if not address:
            return _json({"wallet": "off", "error": "nobody signed in is looking at this page"}, 403)
        coins = (v1.coins([address]).get("addresses") or {}).get(address) or {}
        spendable = sum(int(c["value"]) for c in (coins.get("confirmed") or []) + (coins.get("unconfirmed") or []))
        code, bal = v1.page(f"balances/{address}")
        tokens = [contentlib.holding({"propertyid": b.get("propertyid"), "name": b.get("name"),
                                      "divisible": b.get("divisible"), "units": int(b.get("units") or 0)})
                  for b in (bal or []) if code == 200]
        code, ins = v1.page(f"inscriptions/{address}", {"limit": 1})
        tag = str((v1.page(f"address/{address}")[1] or {}).get("tag") or "") or None
        try:
            owned = int(v1.page("inscriptions/count", {"owner": address})[1] or 0)
        except (V1Error, ValueError, TypeError):
            owned = len(ins or []) if code == 200 else 0
        return _json({"network": "test", "mainnet": False, "addresses": [address], "tag": tag,
                      "coin": {"spendable": spendable / 1e8, "ticker": ""}, "tokens": tokens, "inscriptions": owned,
                      "wallet": "account"})

    # ---------------------------------------------------------------- a page asks the reader to send
    def told(row: dict) -> dict:
        out = {k: row[k] for k in ("id", "kind", "to", "amount", "propertyid", "status", "txid", "error")}
        if row["status"] == "sent" and row["txid"]:
            try:
                code, s = v1.page(f"tx/{row['txid']}")
                if code == 200 and s:
                    out["confirmations"], out["confirmed"] = s.get("confirmations"), s.get("confirmed")
            except V1Error:
                pass
        return out

    def sweep(now: float) -> None:
        for rid, row in list(sends.items()):
            if row["status"] == "pending" and now - row["created"] > PAGE_SEND_FOR:
                row["status"] = "expired"
            if now - row["created"] > 6 * PAGE_SEND_FOR:
                sends.pop(rid, None)

    def unlimited(address: str) -> bool:
        listed = st.setting("unlimited_accounts", []) or []
        return isinstance(listed, list) and address in listed

    @app.post("/r/send")
    def r_send(request: Request, body: dict = Body(default={})):
        """A question for the reader (never an action): kind coins | token | inscription, to, amount; an optional
        `ref` makes asking twice the same question."""
        if not isinstance(body, dict):
            return _json({"error": "send a JSON object"}, 400)
        address, page = viewer_of(request)
        if not address:
            return _json({"error": "sign in and open this page there to be asked"}, 403)
        kind = str(body.get("kind") or "")
        if kind not in ("token", "coins", "inscription"):
            return _json({"error": "a page may ask for coins, a token or an inscription"}, 400)
        net = str(body.get("chain") or "test").strip() or "test"
        if net != "test" and kind != "coins":
            return _json({"error": "only coins can be asked for on another chain"}, 400)
        if chains.by_network(net) is None:
            return _json({"error": f"this arcade keeps no {net} wallet"}, 400)
        to = str(body.get("to") or "").strip()
        amount = "1" if kind == "inscription" else str(body.get("amount") or "").strip()
        if not to or not amount:
            return _json({"error": "say who to pay and how much"}, 400)
        ref = str(body.get("ref") or "")[:80]
        key = (address, net, ref)
        with lock:
            if ref and key in refs and refs[key] in sends:
                return _json({**told(sends[refs[key]]), "repeat": True}, 200)
            now = time.time()
            sweep(now)
            waiting = [r for r in sends.values() if r["address"] == address and r["status"] == "pending"]
            if len(waiting) >= 20 and not unlimited(address):
                return _json({"error": "twenty requests are already waiting"}, 429)
        page_row, piece, name, issuer = None, None, "", ""
        if page:
            code, page_row = v1.page(f"inscription/{page}")
            page_row = page_row if code == 200 else None
        if kind == "inscription":
            code, piece = v1.page(f"inscription/{str(body.get('inscription') or '').strip()}")
            if code != 200 or not piece:
                return _json({"error": "there is no such inscription"}, 400)
            if piece.get("owner") != address:
                return _json({"error": "that inscription is not yours to send"}, 400)
            pj = piece.get("json") if isinstance(piece.get("json"), dict) else {}
            name = f"#{piece.get('number')}" + (f" ({pj.get('name')})" if pj.get("name") else "")
        if kind == "token":
            try:
                code, prop = v1.page(f"token/{int(body.get('propertyid') or 0)}")
            except (TypeError, ValueError):
                code, prop = 404, None
            if code != 200 or not prop:
                return _json({"error": "there is no such token"}, 400)
            name, issuer = prop.get("name") or "", str(prop.get("issuer") or "")
        to_address = to if not to.startswith("@") else (v1.tag_owner(to.lstrip("@")) if net == "test" else "")
        rid = secrets.token_urlsafe(12)
        one = chains.by_network(net)
        row = {"id": rid, "address": address, "network": net, "kind": kind, "coin": one.params.coin, "to": to[:120],
               "amount": amount[:40], "propertyid": int(body.get("propertyid") or 0) if kind == "token" else None,
               "name": name, "label": str(body.get("label") or "")[:60], "note": str(body.get("note") or "")[:140],
               "page": page, "creator": (page_row or {}).get("creator") or "", "to_address": to_address,
               "issuer": issuer, "inscription": (piece or {}).get("id") or "",
               "made_by": (piece or {}).get("creator") or "", "silent": body.get("silent") is True,
               "status": "pending", "txid": "", "error": "", "created": time.time()}
        with lock:
            sends[rid] = row
            if ref:
                refs[key] = rid
        return _json(told(row), 202)

    @app.get("/r/send/{request_id}")
    def r_send_status(request_id: str):
        row = sends.get(request_id)
        return _json(told(row)) if row else _json({"error": "no such request"}, 404)

    @app.get("/account/pagesends")
    def account_pagesends(request: Request):
        account = app.state.signed_in_account(request)
        address = home_address(account)
        now = time.time()
        rows = [r for r in sends.values() if r["address"] == address and r["status"] == "pending"
                and now - r["created"] <= PAGE_SEND_FOR]
        keys = ("id", "kind", "to", "amount", "propertyid", "name", "label", "note", "page", "creator", "to_address",
                "issuer", "inscription", "made_by", "silent", "coin")
        return JSONResponse({"chain": "test", "requests": [{**{k: r.get(k, "") for k in keys},
                                                            "chain": r.get("network", "")} for r in rows]},
                            headers={"Cache-Control": "no-store"})

    @app.post("/account/pagesends/answer")
    def account_pagesends_answer(request: Request, payload: Any = Body(None)):
        account = app.state.signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        row = sends.get(str(said.get("id") or ""))
        if row is None or row["address"] != home_address(account) or row["status"] != "pending":
            return JSONResponse({"detail": "no such question waiting"}, status_code=404)
        status = str(said.get("status") or "")
        if status == "sent":
            txid = str(said.get("txid") or "").strip().lower()
            seen = False
            if re.fullmatch(r"[0-9a-f]{64}", txid):
                try:
                    seen = v1.page(f"tx/{txid}")[0] == 200 if row["network"] == "test" else \
                        bool(v1.core(row["network"], "getrawtransaction", txid, True))
                except V1Error:
                    seen = False
            if not seen:
                return JSONResponse({"detail": "this node has not seen that transaction"}, status_code=400)
            row.update(status="sent", txid=txid)
        elif status in ("denied", "failed"):
            row.update(status=status, error=str(said.get("error") or "")[:200])
        else:
            return JSONResponse({"detail": "sent, denied or failed"}, status_code=400)
        return JSONResponse(told(row))

    # ---------------------------------------------------------------- game state and escrow: not used on this arcade
    # (Strawberry's stores held no game_state row and no escrow, 2026-11-11); answered plainly, never pretended.
    @app.post("/r/state/update")
    def r_state_update():
        return _json({"ok": False, "error": "no game on this arcade publishes state yet"}, 400)

    @app.post("/r/escrow/release")
    def r_escrow_release():
        return _json({"ok": False, "error": "this arcade holds no escrows"}, 400)

    @app.get("/site/{rest:path}")
    def site_page(rest: str, request: Request):
        """A name's page (sites on names): V1.0's own /site/, sandboxed as an inscription is."""
        try:
            r = v1.raw("GET", f"/site/{rest}", params=dict(request.query_params), headers={"referer": ""})
        except V1Error:
            return Response("the arcade behind this site did not answer\n", status_code=503, media_type="text/plain")
        keep = {k: val for k, val in r.headers.items()
                if k.lower() in ("content-type", "content-security-policy", "cache-control", "location",
                                 "access-control-allow-origin")}
        return Response(r.content, status_code=r.status_code, headers=keep)

    # ---------------------------------------------------------------- every other page API read: V1.0's own
    @app.api_route("/r/{rest:path}", methods=["GET", "HEAD"])
    def r_read(rest: str, request: Request):
        first = rest.split("/", 1)[0]
        if first in OWN or rest.endswith(".js"):
            return _json({"error": "no such page API route"}, 404)
        # An app on another chain (its frame's address says chain=): its reads answer on that chain (V1.0 #79).
        headers = {}
        refer = request.headers.get("referer", "")
        if refer:
            net = (urllib.parse.parse_qs(urllib.parse.urlparse(refer).query).get("chain") or [""])[0]
            if re.fullmatch(r"[a-z]{2,10}", net) and net != "test":
                headers["X-Arcade-Chain"] = chain_key(net)
        try:
            r = v1.raw("GET", f"/r/{rest}", params=dict(request.query_params), headers=headers)
        except V1Error as e:
            return _json({"error": str(e)}, 503)
        return Response(r.content, status_code=r.status_code,
                        media_type=r.headers.get("content-type", "application/json"),
                        headers={**contentlib.CORS, "Cache-Control": "no-store"})


def register_pages(app, st, chains):
    """The Apps page, an app full screen, the game gate, a game's front door: app cards from V1.0's App Store
    (apps.store: every chain with the arcade ledger)."""
    from fastapi import HTTPException
    from fastapi.responses import HTMLResponse, RedirectResponse
    from arcade import games as gameslib
    from arcade.messaging import feed as feedlib
    from . import legacy as L
    v1 = st.v1
    kept: dict = {}

    def store(sort: str = "popular") -> list[dict]:
        hit = kept.get(sort)
        if hit and time.time() - hit[0] < 60:
            return hit[1]
        try:
            items = (v1.cmd("apps.store", sort=sort) or {}).get("apps") or []
        except V1Error:
            items = hit[1] if hit else []
        kept[sort] = (time.time(), items)
        return items

    def net_of(key: str) -> str:
        return {"pep": "main", "peptest": "test"}.get(key, key)

    def as_item(a: dict) -> dict:
        card = dict(a.get("card") or {})
        card["opens"] = a.get("opens") if a.get("opens") and a.get("opens") != a.get("txid") else ""
        soc = a.get("social") or {}
        return {"txid": a.get("txid"), "number": a.get("number"), "creator": a.get("creator"),
                "name": card.get("name") or "", "game": card, "time": int(a.get("time") or 0),
                "height": a.get("height"), "net": net_of(str(a.get("chain") or "")), "netlabel": a.get("chain_name"),
                "likes": int(soc.get("likes") or 0), "dislikes": int(soc.get("dislikes") or 0),
                "tips": int(soc.get("tips") or 0), "comments": int(soc.get("comments") or 0),
                "liked": False, "disliked": False, "mine": False, "tdeck": None,
                "comment_rows": [{"author": c.get("author") or "", "text": c.get("text") or ""}
                                 for c in soc.get("last_comments") or []],
                "star_key": gameslib.app_key(a.get("creator") or "", card.get("name") or ""), "starred": False}

    def find(page: str) -> dict | None:
        """The app whose card is `page`, or whose card opens `page`."""
        for a in store("new"):
            if a.get("txid") == page or a.get("opens") == page:
                return a
        return None

    @app.get("/apps", response_class=HTMLResponse)
    def apps_page(request: Request, sort: str = "popular"):
        sort = "new" if sort == "new" else "popular"
        games = [as_item(a) for a in store(sort) if not a.get("removed") and not a.get("flag")]
        account = app.state.signed_in(request)
        starred = set(st.setting(f"favorites:{account.pubkey}", []) or []) if account is not None else set()
        for g in games:
            g["starred"] = g["star_key"] in starred
        games = [g for g in games if g["starred"]] + [g for g in games if not g["starred"]]
        mine = str((st.vault().by_pubkey(account.pubkey) or {}).get("tag") or "") if account is not None else ""
        return app.state.render(request, "games.html", chain=chains.account_chain(), games=games, sort=sort,
                                kinds=feedlib.BY_NAME, viewer="account", mine_tag=mine, when=L._local_time)

    @app.get("/games")
    def games_old():
        return RedirectResponse("/apps", status_code=303)

    @app.get("/games/{txid}/access")
    def game_access(txid: str):
        txid = (txid or "").lower()
        if not re.fullmatch(r"[0-9a-f]{64}", txid):
            return JSONResponse({"game": False})
        a = find(txid)
        if a is None:
            return JSONResponse({"game": False})
        card = a.get("card") or {}
        tag = a.get("creator_tag") or ""
        return JSONResponse({"game": True, "card": a.get("txid"), "name": card.get("name") or "",
                             "wallet": bool(card.get("wallet")), "media": bool(card.get("media")),
                             "pictures": bool(card.get("pictures")), "contacts": bool(card.get("contacts")),
                             "groups": bool(card.get("groups")), "opens": a.get("opens") or a.get("txid"),
                             "publisher": f"@{tag}" if tag else "", "address": a.get("creator")})

    @app.get("/inscriptions/{key}/full", response_class=HTMLResponse)
    def inscription_full(request: Request, key: str):
        """One inscription filling the screen inside the site: a page in the sandboxed frame with the reader's
        ticket (so the wallet carries in), a picture whole."""
        key = (key or "").strip().lower()
        code, row = v1.page(f"inscription/{key}")
        net = "test"
        if code != 200 or not row:
            row = None
            for one in ("main", "doge", "dgb", "flop", "wjk", "kot", "ltc"):   # another chain's arcade ledger
                try:                                         # (kept here or borrowed: V1.0 asks the mesh)
                    got = v1.cmd("explorer.ledger_inscription", chain=chain_key(one), id=key) or {}
                except V1Error:
                    continue
                if got:
                    row, net = {**got, "id": got.get("txid") or got.get("id") or key}, one
                    break
        if not row:
            raise HTTPException(status_code=404, detail="no such piece")
        txid = str(row.get("id") or key).split("i")[0]
        ctype = str(row.get("contenttype") or row.get("content_type") or "")
        data = row.get("json") if isinstance(row.get("json"), dict) else {}
        title = data.get("name") if data.get("name") else f"#{row.get('number')}"
        if ctype.startswith("text/html"):
            kind, src = "page", app.state.frame_src(request, txid, net)
        elif ctype.startswith("image/"):
            kind, src = "picture", ""
        else:
            return RedirectResponse(f"/content/{txid}", status_code=303)
        account = app.state.signed_in(request)
        row = {**row, "txid": txid, "content_type": ctype}   # the original's row names (V1.0 says id/contenttype)
        return app.state.render(request, "inscription_full.html", fullscreen=True, kind=kind, src=src, txid=txid,
                                title=str(title)[:80], row=row, chain=chains.account_chain(),
                                viewer="account" if account is not None else "nobody",
                                back=f"/inscriptions/{txid}/full")

    @app.get("/play/{which}", response_class=HTMLResponse)
    def game_door(request: Request, which: str):
        wanted = (which or "").strip().lower()
        found = None
        if re.fullmatch(r"[0-9a-f]{64}", wanted):
            found = find(wanted)
        elif re.fullmatch(r"[a-z0-9._-]{1,64}", wanted):
            for a in store("new"):
                g = a.get("card") or {}
                if wanted in ((g.get("family") or "").lower(), gameslib.slug(g.get("name") or "")):
                    found = a
                    break
        if found is None:
            raise HTTPException(404, "no such game")
        game = dict(found.get("card") or {})
        opens = f"/inscriptions/{found.get('opens') or found['txid']}/full"
        if app.state.signed_in(request) is not None:
            if request.query_params.get("hop") == "1":
                return RedirectResponse(opens, status_code=303)
            return app.state.render(request, "game_hop.html", game=game, card=found["txid"], opens=opens,
                                    skin=gameslib.door_skin(game), which=wanted)
        register_ = st.accounts()
        return app.state.render(request, "game_door.html", chain=chains.account_chain(), game=game, which=wanted,
                                card=found["txid"], opens=opens, skin=gameslib.door_skin(game),
                                notice=gameslib.door_notice(game), seats=register_.seats, free=register_.free(),
                                secure_enough=L.secure_context(request), why_not=L.WHY_NOT_SECURE)
