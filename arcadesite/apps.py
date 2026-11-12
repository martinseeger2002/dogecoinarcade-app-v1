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
    /r/follows/<who>, /r/profile/<who> (the original's answer plus follows and linked), /r/feed/following,
    POST /r/follow, /r/profile/publish   the Profiles app (2026-11-12): asked in the reader's card, signed there
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

    def one_label(net: str) -> str:
        one = chains.by_network(net)
        return one.label if one is not None else net

    def profile_address(who: str, net: str) -> str:
        """The address a name's published profile gives on `net` (Arcade V1.0 `profile` linked; a hash kept as
        "h160:<hex>" is written with that chain's own version byte). "" when it gives none."""
        from arcade.script import b58check_encode
        one = chains.by_network(net)
        if one is None:
            return ""
        try:
            prof = v1.cmd("profile", who="@" + str(who).lstrip("@")) or {}
        except V1Error:
            return ""
        prof = prof.get("profile") or prof
        for row in prof.get("linked") or []:
            if str(row.get("chain") or "") != chain_key(net) and str(row.get("chain") or "") != net:
                continue
            a = str(row.get("address") or "")
            if a.startswith("h160:"):
                try:
                    a = b58check_encode(one.params.pubkeyhash_version, bytes.fromhex(a[5:]))
                except ValueError:
                    return ""
            return a if not chains.check_address(a, one) else ""
        return ""

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
        to_address = to if not to.startswith("@") else (v1.tag_owner(to.lstrip("@")) if net == "test"
                                                         else profile_address(to, net))
        if to.startswith("@") and kind == "coins" and not to_address:
            return _json({"error": f"{to} has published no {one_label(net)} address in their profile yet"}, 400)
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
                "issuer", "inscription", "made_by", "silent", "coin", "op", "args")
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
            if row.get("op") == "swap_take" and hasattr(app.state, "swap_claim_of") and txid \
                    and app.state.swap_claim_of(row["id"]) == txid:
                seen = True                                 # the claim this site sent for that take (swaptake.py)
            elif re.fullmatch(r"[0-9a-f]{64}", txid):
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

    # ---------------------------------------------------------------- the market page API (Swap and other market apps)
    # Arcade V1.0's arcade.market, for the reader: the market's own reads come from V1.0 (the markets from the
    # exchange's background copy: V1.0 can take minutes to answer them), the reader's holdings and orders are the
    # reader's, and an order, a cancel or a take is a question on the reader's card (kind "market"), built by
    # /account/market/build (exchange.py) and signed in the reader's browser. Only the home chain's on-chain book
    # (venue "ledger") is traded here: DigiDollar trades and swaps across chains need an arcade that holds its own
    # wallet and stays on (docs/MARKET.md, "Trust in V1.0"), so those answer that plainly.
    market_js = (Path(__file__).parent / "shims" / "market.js").read_text()
    MARKET_OPS = ("place", "cancel", "take")
    ACROSS = "Trading across chains and DigiDollar trades need the Arcade app on your own computer: it holds your " \
             "wallet and stays on to finish a swap. Here you can trade on the Pepecoin testnet book."

    # A page runs sandboxed (an opaque origin), so its JSON POSTs (a market order, a send) are preflighted: answered
    # here, for the page API only. Every /r/ write is a question its reader approves on this site's own card.
    @app.options("/r/{rest:path}")
    def r_preflight(rest: str):
        return Response(status_code=204, headers={**contentlib.CORS, "Access-Control-Allow-Methods": "GET, POST",
                                                  "Access-Control-Allow-Headers": "Content-Type",
                                                  "Access-Control-Max-Age": "600"})

    @app.get("/r/market.js")
    def r_market_js():
        return Response(market_js, media_type="application/javascript",
                        headers={**contentlib.CORS, "Cache-Control": "public, max-age=3600"})

    def market_ok(v, status: int = 200) -> Response:
        return _json({"ok": True, "result": v}, status)

    def market_no(e: str, status: int = 400) -> Response:
        return _json({"ok": False, "error": e}, status)

    def home_markets() -> list[dict]:
        """The home chain's on-chain book markets, from the exchange's copy."""
        snap = app.state.exchange_markets() if hasattr(app.state, "exchange_markets") else {"markets": []}
        here = chains.account_chain().key
        return [m for m in snap.get("markets") or []
                if (m.get("venue") or "ledger") == "ledger" and (m.get("base") or {}).get("network") == here
                and (m.get("quote") or {}).get("network") == here]

    def market_holdings(address: str) -> list[dict]:
        """What the reader can pay with on the home chain: its coin (satoshis) and its tokens (base units)."""
        home = chains.account_chain()
        coins = (v1.coins([address]).get("addresses") or {}).get(address) or {}
        amount = sum(int(c["value"]) for c in (coins.get("confirmed") or []) + (coins.get("unconfirmed") or []))
        out = [{"network": home.key, "kind": "coin", "asset": "coin", "amount": amount, "name": home.params.coin}]
        code, bal = v1.page(f"balances/{address}", {"pending": 1})
        for b in (bal or []) if code == 200 and isinstance(bal, list) else []:
            free = int(b.get("units") or 0) - int(b.get("leaving") or 0)
            if free > 0:
                pid = int(b.get("propertyid") or 0)
                out.append({"network": home.key, "kind": "token", "asset": str(pid), "property_id": pid,
                            "name": b.get("name") or f"Token #{pid}", "divisible": bool(b.get("divisible")),
                            "amount": free})
        return out

    def market_mine(address: str) -> dict:
        """The reader's orders resting on the home chain's books (V1.0's mine, for this address)."""
        orders = []
        for m in home_markets():                            # every book: the copy's counts can be minutes old
            b, q = int((m.get("base") or {}).get("property_id") or 0), int((m.get("quote") or {}).get("property_id") or 0)
            try:
                code, got = v1.page("market/book", {"base": b, "quote": q, "depth": 500})
            except V1Error:
                continue
            res = (got or {}).get("result") or {} if code == 200 else {}
            for side in ("asks", "bids"):
                for r in res.get(side) or []:
                    if r.get("maker") != address:
                        continue
                    base, quote = {"asset": b, "amount": int(r.get("base") or 0)}, {"asset": q, "amount": int(r.get("quote") or 0)}
                    give, get = (base, quote) if side == "asks" else (quote, base)
                    orders.append({"order": r.get("order"), "address": address, "give": give, "get": get,
                                   "pending": bool(r.get("pending")), "taking": int(r.get("taking") or 0),
                                   "status": "open"})
        return {"orders": orders, "swaps": [], "listings": [], "offers": []}

    @app.get("/r/market/{name}")
    def r_market(name: str, request: Request):
        if name in ("holdings", "mine"):
            address, _ = viewer_of(request)
            if not address:
                return market_no("sign in and open this page there to see your wallet", 403)
            try:
                return market_ok(market_holdings(address) if name == "holdings" else market_mine(address))
            except V1Error as e:
                return market_no(str(e), 503)
        if name in ("markets", "pairs"):
            return market_ok(home_markets())
        if name == "swaps":
            if not st.setting("market_taker", False):
                return market_ok({"book": [], "mine": [], "running": [], "me": "", "note": ACROSS})
            try:
                book = v1.cmd("swap.book") or []
            except V1Error:
                book = []
            # The orders a visitor can take here: coins for coins, on chains this site keeps wallets on (swaptake.py).
            keys = {c.key for c in chains.account_chains()}
            book = [o for o in book if (o.get("give") or {}).get("kind") == "coin" and (o.get("get") or {}).get("kind") == "coin"
                    and (o.get("give") or {}).get("chain") in keys and (o.get("get") or {}).get("chain") in keys]
            return market_ok({"book": book, "mine": [], "running": [], "me": ""})
        if name == "trades":
            snap = app.state.exchange_markets() if hasattr(app.state, "exchange_markets") else {}
            def one(v: str) -> int:
                return 0 if v in ("", "0", "coin") else int(v)
            try:
                key = (one(request.query_params.get("base") or ""), one(request.query_params.get("quote") or ""))
            except ValueError:
                return market_no("base and quote are token numbers, or coin")
            if key in (snap.get("trades") or {}):
                limit = max(1, min(5000, int(request.query_params.get("limit") or 100)))
                return market_ok(snap["trades"][key][:limit])
        if name not in ("book", "trades", "candles", "assets", "terms"):
            return market_no(f"{name} is not a market read", 404)
        if (request.query_params.get("network") or chains.account_chain().key) != chains.account_chain().key:
            return market_no(ACROSS)
        try:
            r = v1.raw("GET", f"/r/market/{name}", params=dict(request.query_params))
        except V1Error as e:
            return market_no(str(e), 503)
        return Response(r.content, status_code=r.status_code, media_type="application/json",
                        headers={**contentlib.CORS, "Cache-Control": "no-store"})

    def market_side(v) -> dict:
        if not isinstance(v, dict):
            raise ValueError("each side is {asset, amount}")
        asset = str(v.get("asset") if v.get("asset") is not None else "").strip()
        amount = str(v.get("amount") if v.get("amount") is not None else "").strip()
        if asset != "coin" and not asset.isdigit():
            raise ValueError(ACROSS if asset else "each side names its asset: coin or a token number")
        if not amount.isdigit() or int(amount) <= 0:
            raise ValueError("an amount in whole base units, above zero")
        return {"asset": 0 if asset == "coin" else int(asset), "amount": int(amount)}

    @app.post("/r/market/{op}")
    def r_market_write(op: str, request: Request, body: dict = Body(default={})):
        """A market question for the reader (never an action): place {give, get}, cancel {order}, take {order, amount}."""
        if op == "swap_take" and st.setting("market_taker", False):
            pass                                            # a take across chains (swaptake.py), when the operator allows
        elif op.startswith("swap_"):
            return market_no(ACROSS)
        if op not in MARKET_OPS + ("swap_take",):
            return market_no(f"{op} is not a market request", 404)
        if not isinstance(body, dict):
            return market_no("send a JSON object")
        if body.get("network") and str(body["network"]) != chains.account_chain().key:
            return market_no(ACROSS)
        address, page = viewer_of(request)
        if not address:
            return market_no("sign in and open this page there to be asked", 403)
        try:
            if op == "swap_take":
                node, order = str(body.get("node") or ""), str(body.get("order") or "")
                if not re.fullmatch(r"[0-9a-f]{8,64}", node) or not re.fullmatch(r"[0-9a-f]{4,64}", order):
                    raise ValueError("name the order and its arcade")
                args = {"node": node, "order": order}
            elif op == "place":
                args = {"give": market_side(body.get("give")), "get": market_side(body.get("get"))}
                if args["give"]["asset"] == args["get"]["asset"]:
                    raise ValueError("a trade is two different assets")
            else:
                order = str(body.get("order") or "").strip().lower()
                if not re.fullmatch(r"[0-9a-f]{64}", order):
                    raise ValueError("name the order by its transaction id")
                args = {"order": order}
                if op == "take":
                    amount = str(body.get("amount") or "").strip()
                    if not amount.isdigit() or int(amount) <= 0:
                        raise ValueError("take an amount in whole base units, above zero")
                    args["amount"] = int(amount)
        except ValueError as e:
            return market_no(str(e))
        with lock:
            now = time.time()
            sweep(now)
            waiting = [r for r in sends.values() if r["address"] == address and r["status"] == "pending"]
            if len(waiting) >= 20 and not unlimited(address):
                return market_no("twenty requests are already waiting", 429)
        page_row = None
        if page:
            code, page_row = v1.page(f"inscription/{page}")
            page_row = page_row if code == 200 else None
        rid = secrets.token_urlsafe(12)
        home = chains.account_chain()
        row = {"id": rid, "address": address, "network": home.network, "kind": "market", "coin": home.params.coin,
               "op": op, "args": args, "to": "", "amount": "", "propertyid": None, "name": "", "label": "", "note": "",
               "page": page, "creator": (page_row or {}).get("creator") or "", "to_address": "", "issuer": "",
               "inscription": "", "made_by": "", "silent": False, "status": "pending", "txid": "", "error": "",
               "created": time.time()}
        with lock:
            sends[rid] = row
        return _json(told(row), 202)

    app.state.page_send = lambda rid: sends.get(rid)

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

    # ---------------------------------------------------------------- one DRC-20 / PRC-20 tick (2026-11-12)
    # For the Token Minter and any page: on the pages host only /r/ answers, so the tick read is here as well as at
    # /explore/api/tick. Public: supply, minted, the most one mint may take, decimals (base units, as strings).
    @app.get("/r/ord-token/{net}/{tick}")
    def r_ord_token(net: str, tick: str):
        if not re.fullmatch(r"[a-z]{2,10}", net) or not tick.strip() or len(tick) > 32:
            return _json({"error": "ask /r/ord-token/<doge|main|test>/<tick>"}, 400)
        try:
            tok = v1.cmd("explorer.token", chain=chain_key(net), tick=tick.strip()) or {}
        except V1Error as e:
            return _json({"error": str(e)}, 404)
        if not tok.get("tick"):
            return _json({"error": f"no tick {tick} on that chain"}, 404)
        return Response(json.dumps({"tick": tok.get("tick"), "shown": tok.get("shown") or tok.get("tick"),
                                    "supply": str(tok.get("supply") or "0"), "minted": str(tok.get("minted") or "0"),
                                    "lim": str(tok.get("lim") or tok.get("supply") or "0"),
                                    "dec": int(tok.get("dec") if tok.get("dec") is not None else 18),
                                    "deployer": tok.get("deployer"), "holders": tok.get("holders")}),
                        media_type="application/json", headers={**contentlib.CORS, "Cache-Control": "no-store"})

    # ---------------------------------------------------------------- the Profiles app (2026-11-12): follows on the chain
    @app.get("/r/follows/{who}")
    def r_follows(who: str):
        """An account's followers and the accounts it follows (Arcade V1.0 `follows`), with @tags and counts."""
        want = urllib.parse.unquote(who).strip()
        try:
            return _json(v1.cmd("follows", who=want if want.startswith("@") or len(want) > 24 else "@" + want) or {})
        except V1Error as e:
            return _json({"error": str(e)}, 404 if "no such" in str(e).lower() or "unknown" in str(e).lower() else 503)

    @app.get("/r/profile/{who}")
    def r_profile(who: str):
        """The original's /r/profile answer, unchanged, plus `follows` {followers, following} and `linked` (the address
        it gives on each coin) from Arcade V1.0's `profile`."""
        want = urllib.parse.unquote(who).strip()
        try:
            code, body = v1.page(f"profile/{urllib.parse.quote(want)}")
        except V1Error as e:
            return _json({"error": str(e)}, 503)
        if code != 200 or not isinstance(body, dict):
            return _json(body if body is not None else {"error": "no such profile"}, code)
        try:
            prof = v1.cmd("profile", who=want if want.startswith("@") or len(want) > 24 else "@" + want) or {}
        except V1Error:
            prof = {}
        inner = prof.get("profile") or prof
        return _json({**body, "follows": prof.get("follows") or {"followers": 0, "following": 0},
                      "linked": inner.get("linked") or []})

    @app.get("/r/feed/following")
    def r_feed_following(request: Request, offset: int = 0, limit: int = 20):
        """Posts by the accounts the signed-in viewer follows, newest first (the Feed page's Following tab)."""
        address, _ = viewer_of(request)
        posts, total, note = app.state.following_posts(address, max(0, int(offset)), max(1, min(int(limit), 50)))
        return _json({"posts": posts, "total": total, "note": note})

    def page_request(request: Request, kind: str, args: dict, what: str) -> Response:
        """File a write the page asks for (follow, unfollow, publish a profile): the reader's arcade card asks, and their
        browser builds and signs it (base.html pageSends)."""
        address, page = viewer_of(request)
        if not address:
            return _json({"error": "sign in and open this page there to be asked"}, 403)
        with lock:
            now = time.time()
            sweep(now)
            waiting = [r for r in sends.values() if r["address"] == address and r["status"] == "pending"]
            if len(waiting) >= 20 and not unlimited(address):
                return _json({"error": "twenty requests are already waiting"}, 429)
        page_row = None
        if page:
            code, page_row = v1.page(f"inscription/{page}")
            page_row = page_row if code == 200 else None
        rid = secrets.token_urlsafe(12)
        home = chains.account_chain()
        row = {"id": rid, "address": address, "network": home.network, "kind": kind, "coin": home.params.coin,
               "op": what, "args": args, "to": "", "amount": "", "propertyid": None, "name": "", "label": "", "note": "",
               "page": page, "creator": (page_row or {}).get("creator") or "", "to_address": "", "issuer": "",
               "inscription": "", "made_by": "", "silent": False, "status": "pending", "txid": "", "error": "",
               "created": time.time()}
        with lock:
            sends[rid] = row
        return _json(told(row), 202)

    @app.post("/r/follow")
    def r_follow(request: Request, body: dict = Body(default={})):
        """Ask the reader to follow {who} (on: false unfollows)."""
        if not isinstance(body, dict):
            return _json({"error": "send a JSON object"}, 400)
        who = str(body.get("who") or "").strip()
        if not who:
            return _json({"error": "follow whom? Give an @name or an address"}, 400)
        on = body.get("on", True) not in (False, "false", 0, "0")
        return page_request(request, "follow", {"who": who[:120], "on": on}, "follow" if on else "unfollow")

    @app.post("/r/profile/publish")
    def r_profile_publish(request: Request, body: dict = Body(default={})):
        """Ask the reader to publish their profile: any of pfp, bio, url (a field left out stays as it is)."""
        if not isinstance(body, dict):
            return _json({"error": "send a JSON object"}, 400)
        fields = {k: str(body[k] or "")[:300] for k in ("pfp", "bio", "url") if k in body}
        return page_request(request, "profile", fields, "publish your profile")

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
