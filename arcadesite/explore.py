"""Explore: every chain's inscriptions, tokens and arcade assets, read from Arcade V1.0's explorer (its own indexes,
or a mesh arcade's for a chain it borrows), drawn by the original's explore.html with the answers its script reads.

    GET /explore                        the page: a tab per chain V1.0 can answer for
    GET /explore/api/inscription        one inscription, by number or id (+ the newest number)
    GET /explore/api/lookup             the search box: a number, an id, a coin, a tick, an address
    GET /explore/api/typed              inscriptions of one content type, newest first
    GET /explore/api/ticks              ticks starting with what is typed
    GET /explore/api/holders            one tick: its deploy and its biggest holders
    GET /explore/api/assets, asset      arcade ledger tokens (Omni-style assets)
    GET /explore/api/collections, collection
                                        "Sub 1K" (a chain's first thousand inscriptions) now; the collection
                                        registry and known collections come from V1.0 once it keeps them
"""
from __future__ import annotations

import json
import re
import time

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse

from arcade import ord_chains as ordchainslib

from .v1 import V1Error, chain_key

INSCRIPTION_ID = re.compile(r"^[0-9a-f]{64}i[0-9]+$")
SUB1K, SUB1K_PAGE, SUB1K_SLUG = 1000, 48, "sub1k"
COLLECTIONS_PAGE = 30


def _from(got: dict) -> str:
    m = (got or {}).get("_mesh") or {}
    return f"@{m['tag']}" if m.get("tag") else (m.get("node") or "")


def _piece(x: dict) -> dict:
    """V1.0's inscription row -> the fields explore.html reads."""
    at = str(x.get("at") or "")
    parts = at.split(":")
    out = {"id": x.get("id"), "number": x.get("n"), "content_type": x.get("ct") or (x.get("content") or {}).get("type"),
           "size": x.get("size"), "pieces": x.get("pieces") or 1, "height": x.get("h"), "owner": x.get("owner"),
           "output": ":".join(parts[:2]) if len(parts) >= 2 else None,
           "offset": int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else 0,
           "moved_at": x.get("moved_at"), "delegate": x.get("delegate"), "parent": x.get("parent"),
           "protocol": x.get("protocol"), "charms": x.get("charms") or [], "burned": bool(x.get("burned")),
           "reinscription": bool(x.get("reinscription"))}
    if x.get("delegate"):
        out["delegate_content_type"] = x.get("delegate_ct")
    return out


def register(app, st, chains):
    v1 = st.v1
    kept: dict = {}

    def keep(key, ttl, fn):
        hit = kept.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        val = fn()
        kept[key] = (time.time(), val)
        return val

    def ask(cmd: str, net: str, **params):
        return v1.cmd(cmd, chain=chain_key(net), **params) or {}

    def explore_chains() -> list[dict]:
        def build():
            listed = {c.get("key"): c for c in (v1.cmd("chains.list") or [])}
            out = []
            for net in ordchainslib.EXPLORE_ORDER:
                c = listed.get(chain_key(net))
                if not c:
                    continue
                keeps = bool(c.get("installed")) and ("ord" in (c.get("indexes") or []) or bool(c.get("ord")))
                if not keeps and not c.get("borrowed"):
                    continue
                protocols = ordchainslib.explore_protocols(net)
                if c.get("home") or (c.get("installed") and "ledger" in (c.get("indexes") or [])):
                    protocols = protocols + [{"key": "assets", "label": "Arcade assets (Omni)", "kind": "assets"}]
                out.append({"net": net, "chain": ordchainslib.chain_name(net), "coin": ordchainslib.coin(net),
                            "word": ordchainslib.word(net), "mode": "walk" if keeps else "borrowed", "answers": True,
                            "protocols": protocols})
            return out
        return keep("chains", 300, build)

    def newest(net: str) -> int:
        def build():
            got = ask("explorer.inscriptions", net, limit=1)
            return int(got.get("total") or 0) - 1
        return keep(("newest", net), 30, build)

    def one(net: str, which: str) -> dict:
        try:
            got = ask("explorer.inscription", net, id=which)
        except V1Error as e:
            if "no such" in str(e).lower() or "not" in str(e).lower():
                return {"number": {"inscription": None, "last": newest(net)}}
            raise
        return {"number": {"inscription": _piece(got), "last": newest(net)}, "from": _from(got),
                "collection": in_collection(net, str(got.get("id") or which))}

    def bad(detail: str, code: int = 400) -> JSONResponse:
        return JSONResponse({"detail": detail}, status_code=code)

    def known_net(net: str) -> bool:
        return any(c["net"] == net for c in explore_chains())

    @app.get("/explore", response_class=HTMLResponse)
    def explore_page(request: Request):
        return app.state.render(request, "explore.html", chains=explore_chains())

    @app.get("/explore/api/inscription")
    def api_inscription(net: str = "doge", number: str = "", id: str = ""):
        if not known_net(net):
            return bad("this arcade does not read that chain", 404)
        iid = (id or "").strip().lower()
        if iid:
            if not INSCRIPTION_ID.match(iid):
                return bad("an inscription id: <txid>i<n>")
            return JSONResponse(one(net, iid))
        said = (number or "0").strip().lstrip("#")
        if not re.match(r"^-?[0-9]{1,12}$", said):
            return bad("an inscription number: 0, 1, 2 ... (or -1, -2 ... for a cursed one)")
        return JSONResponse(one(net, said))

    @app.get("/explore/api/lookup")
    def api_lookup(net: str = "doge", q: str = ""):
        if not known_net(net):
            return bad("this arcade does not read that chain", 404)
        q = (q or "").strip()
        if not q or len(q) > 120:
            return bad("ask for an inscription number or id, a coin (txid:vout), a tick, a name or an address")
        low = q.lower().lstrip("#")
        if re.fullmatch(r"-?[0-9]{1,12}", low) or INSCRIPTION_ID.fullmatch(low):
            return JSONResponse(one(net, low))
        m = re.fullmatch(r"([0-9a-f]{64}):([0-9]+)", low)
        if m:
            tx = ask("explorer.tx", net, txid=m.group(1))
            out = next((o for o in tx.get("outputs") or [] if int(o.get("vout", -1)) == int(m.group(2))), {}) or {}
            on = [{"id": i if isinstance(i, str) else i.get("id")} for i in out.get("inscriptions") or []]
            return JSONResponse({"on": on, "from": _from(tx)})
        if re.fullmatch(r"[A-Za-z0-9]{25,62}", q):
            got = ask("explorer.address", net, address=q)
            bal = [{"tick": t.get("tick") or t.get("shown"), "units": t.get("overall") or t.get("balance"),
                    "decimals": t.get("dec") or 0} for t in got.get("tokens") or []]
            return JSONResponse({"balances": bal, "from": _from(got)})
        if "." in q:
            try:
                got = ask("explorer.name", net, name=q)
            except V1Error:
                got = {}
            return JSONResponse({"name": {"name": q, "readings": got.get("readings") or {}}, "from": _from(got)})
        try:
            tok = ask("explorer.token", net, tick=q)
        except V1Error:
            tok = {}
        return JSONResponse({"token": {"tick": tok.get("tick")} if tok.get("tick") else None, "from": _from(tok)})

    GROUP_OF = {"image/": "image", "image/svg+xml": "svg", "text/html": "html", "application/json": "json",
                "audio/": "audio", "video/": "video", "model/": "model", "text/plain": "text",
                "text/javascript": "text"}

    def group_for(t: str) -> str | None:
        if t in GROUP_OF:
            return GROUP_OF[t]
        if t.startswith("image/") and "svg" not in t:
            return "image"
        return None

    @app.get("/explore/api/typed")
    def api_typed(net: str = "doge", type: str = "", after: str = ""):
        """Inscriptions of one content type, newest first. A type in one of V1.0's groups is paged from the group
        (the cursor is an offset in it, filtered to the exact type); any other is V1.0's slower scan by number."""
        if not known_net(net):
            return bad("this arcade does not read that chain", 404)
        t = (type or "").strip().lower()
        if not re.fullmatch(r"[a-z0-9.+\-/]{1,80}", t):
            return bad("a content type, like image/png")
        group = group_for(t)
        if group is not None:
            offset = int(after) + 1 if str(after).strip().isdigit() else 0
            rows, scanned = [], 0
            while len(rows) < 48 and scanned < 480:
                got = ask("explorer.inscriptions", net, type=group, offset=offset, limit=100)
                items = got.get("items") or []
                for x in items:
                    ct = str(x.get("ct") or "").lower()
                    if t.endswith("/") or group in ("image", "text") and t in ("image/", "text/") or ct == t \
                            or (t == "text/plain" and ct.startswith("text/plain")):
                        rows.append(_piece(x))
                offset += len(items)
                scanned += len(items)
                if not items or offset >= int(got.get("type_total") or 0):
                    offset = None                           # V1.0 pages 24 at a time whatever is asked: the group's
                    break                                   # total says when it is done
            nxt = offset - 1 if offset is not None else None
            return JSONResponse({"typed": {"rows": rows, "next": nxt}, "from": _from(got)})
        args = {"type": t, "limit": 48}
        if str(after).strip().lstrip("-").isdigit():
            args["before"] = int(after) + 1
        got = ask("explorer.inscriptions", net, **args)
        nxt = got.get("next_before")
        return JSONResponse({"typed": {"rows": [_piece(x) for x in got.get("items") or []],
                                       "next": (int(nxt) - 1) if nxt not in (None, 0) else None}, "from": _from(got)})

    def tick_at(net: str, i: int) -> str:
        """The tick at position i of V1.0's alphabetical tick list (kept a minute)."""
        def build():
            items = ask("explorer.tokens", net, offset=i, limit=1).get("items") or []
            return str(items[0].get("tick") or "").lower() if items else "\uffff"
        return keep(("tick", net, i), 60, build)

    def tick_total(net: str) -> int:
        return keep(("ticks", net), 60, lambda: int(ask("explorer.tokens", net, offset=0, limit=1).get("total") or 0))

    def first_at_or_after(net: str, key: str) -> int:
        lo, hi = 0, tick_total(net)
        while lo < hi:
            mid = (lo + hi) // 2
            if tick_at(net, mid) < key:
                lo = mid + 1
            else:
                hi = mid
        return lo

    def pct(minted, supply) -> float | None:
        try:
            return 100.0 * int(minted) / int(supply) if int(supply) else None
        except (TypeError, ValueError):
            return None

    @app.get("/explore/api/tick")
    def api_tick(net: str = "doge", tick: str = ""):
        """One DRC-20 / PRC-20 tick as a minter needs it (2026-11-12): its supply, how much is minted, the most one mint
        may take, its decimals (all in base units, as strings), and who deployed it."""
        def open_bad(detail: str, code: int = 400) -> JSONResponse:
            return JSONResponse({"detail": detail}, status_code=code, headers={"Access-Control-Allow-Origin": "*"})
        if not known_net(net):
            return open_bad("this arcade does not read that chain", 404)
        t = (tick or "").strip()
        if not t or len(t) > 32:
            return open_bad("ask for a tick")
        try:
            tok = ask("explorer.token", net, tick=t)
        except V1Error as e:
            return open_bad(str(e), 404)
        if not tok.get("tick"):
            return open_bad(f"no tick {t} on that chain", 404)
        # Read by apps too, from their sandboxed frames (an opaque origin): public, so any origin may read it.
        return JSONResponse({"tick": tok.get("tick"), "shown": tok.get("shown") or tok.get("tick"), "supply": str(tok.get("supply") or "0"),
                             "minted": str(tok.get("minted") or "0"), "lim": str(tok.get("lim") or tok.get("supply") or "0"),
                             "dec": int(tok.get("dec") if tok.get("dec") is not None else 18), "deployer": tok.get("deployer"),
                             "height": tok.get("h"), "holders": tok.get("holders"), "from": _from(tok)},
                            headers={"Access-Control-Allow-Origin": "*"})

    @app.get("/explore/api/ticks")
    def api_ticks(net: str = "doge", q: str = ""):
        """Ticks starting with what is typed: V1.0 lists ticks in alphabetical order, so where they start and end are
        two binary searches, and the first 30 one page."""
        if not known_net(net):
            return bad("this arcade does not read that chain", 404)
        low = (q or "").strip().lower()
        try:
            start = first_at_or_after(net, low) if low else 0
            end = first_at_or_after(net, low + "\uffff") if low else tick_total(net)
            rows = ask("explorer.tokens", net, offset=start, limit=30).get("items") or [] if end > start else []
        except V1Error as e:
            return bad(str(e), 503)
        rows = [t for t in rows if str(t.get("tick") or "").lower().startswith(low)]
        first = [{"tick": t.get("tick"), "number": t.get("n"), "minted_pct": pct(t.get("minted"), t.get("supply"))}
                 for t in rows]
        return JSONResponse({"ticks": {"first": first, "count": max(0, end - start)}})

    @app.get("/explore/api/holders")
    def api_holders(net: str = "doge", tick: str = "", page: int = 0):
        if not known_net(net):
            return bad("this arcade does not read that chain", 404)
        page = max(0, int(page or 0))
        try:
            t = ask("explorer.token", net, tick=tick, offset=page * 25, n=25)
        except V1Error:
            return JSONResponse({"holders": None})
        paged = "total" in t                                # V1.0 with holder pages; older ones answer the top 25
        dec = int(t.get("dec") or 0)

        def shown(v):
            try:
                b = int(v)
            except (TypeError, ValueError):
                return v
            w, f = divmod(b, 10 ** dec) if dec else (b, 0)
            return f"{w}.{str(f).rjust(dec, '0').rstrip('0')}" if f else str(w)
        rows = []
        first = int(t.get("offset") or 0) if paged else 0
        for i, h in enumerate(t.get("top") or []):
            rows.append({"rank": first + i + 1, "owner": h.get("tag") and f"@{h['tag']}" or h.get("address"),
                         "balance": shown(h.get("overall")), "transferable": shown(h.get("transferable")),
                         "pct": pct(h.get("overall"), t.get("supply"))})
        holders = str(t.get("holders") or "").rstrip("+")
        out = {"tick": t.get("tick"), "deploy": t.get("id"), "number": t.get("n"), "height": t.get("h"),
               "deployer": t.get("deployer"), "supply": shown(t.get("supply")), "limit": shown(t.get("lim")),
               "decimals": dec, "minted": shown(t.get("minted")), "minted_pct": pct(t.get("minted"), t.get("supply")),
               "holders": int(holders) if holders.isdigit() else None, "rows": rows,
               "page": page if paged else 0,
               "pages": max(1, -(-int(t.get("total") or 0) // 25)) if paged else 1}
        if paged:
            out["holders"] = int(t.get("total") or 0)
        older = None
        if not paged and (str(t.get("holders") or "").endswith("+") or (out["holders"] or 0) > len(rows)):
            older = f"The {len(rows)} biggest holders are shown; this arcade's V1.0 lists no more than those yet."
        return JSONResponse({"holders": out, "older": older, "from": _from(t)})

    def ledger_tokens(net: str) -> list[dict]:
        def build():
            out, offset = [], 0
            while offset < 50_000:
                got = ask("explorer.ledger_tokens", net, offset=offset, limit=500)
                items = got.get("items") or []
                out += items
                if not items:                               # V1.0 pages 50 at a time whatever is asked
                    break
                offset += len(items)
            return out
        return keep(("assets", net), 120, build)

    def about(t: dict) -> dict:
        try:
            d = json.loads(t.get("data") or "{}")
        except ValueError:
            d = {}
        return d if isinstance(d, dict) else {}

    @app.get("/explore/api/assets")
    def api_assets(net: str = "test", q: str = "", category: str = ""):
        if not known_net(net):
            return bad("this arcade does not read that chain", 404)
        every = ledger_tokens(net)
        cats: dict[str, int] = {}
        for t in every:
            cats[t.get("category") or ""] = cats.get(t.get("category") or "", 0) + 1
        low = (q or "").strip().lower()
        hit = [t for t in every if (not category or (t.get("category") or "") == category)
               and (not low or low in str(t.get("name") or "").lower() or low == str(t.get("id")))]
        assets = [{"id": t.get("id"), "name": t.get("name"), "category": t.get("category"),
                   "icon": about(t).get("icon") or "", "about": about(t).get("about") or ""} for t in hit[:120]]
        return JSONResponse({"assets": assets, "count": len(every),
                             "categories": sorted(cats.items(), key=lambda kv: -kv[1])})

    @app.get("/explore/api/asset")
    def api_asset(net: str = "test", id: int = 0):
        if not known_net(net):
            return bad("this arcade does not read that chain", 404)
        try:
            t = ask("explorer.ledger_token", net, id=int(id))
        except V1Error as e:
            return bad(str(e), 404)
        d = about(t)
        holders = [{"address": h.get("tag") and f"@{h['tag']}" or h.get("address"),
                    "held": h.get("shown") or h.get("balance") or h.get("units")}
                   for h in t.get("top") or []]
        a = {"id": t.get("id"), "name": t.get("name"), "icon": d.get("icon") or "", "about": d.get("about") or "",
             "category": t.get("category"), "subcategory": t.get("subcategory"),
             "supply": t.get("total_shown") or t.get("total"), "holders": t.get("holders"),
             "divisible": t.get("type") == 2, "managed": bool(t.get("managed")),
             "issuer": t.get("issuer_tag") and f"@{t['issuer_tag']}" or t.get("issuer"),
             "created_at": t.get("creation_block"), "created_in": t.get("creation_txid"), "url": t.get("url"),
             "page": f"/tokens/{t.get('id')}"}
        return JSONResponse({"asset": a, "holders": holders})

    # ---------------------------------------------------------------- collections
    # From V1.0's collections index (explorer.collections / collection / collection_of, built by the ArcadeCoin
    # session): Sub 1K and the published collection registry now, known collections later as kind "known". A V1.0
    # without those commands still answers Sub 1K here, from explorer.inscriptions.
    def sub1k_row(net: str) -> dict:
        return {"kind": "builtin", "slug": SUB1K_SLUG, "name": "Sub 1K", "chain": net, "supply": SUB1K,
                "count": SUB1K, "imported": True, "source": "built in", "agreed": None, "owners": None,
                "description": f"The first thousand {ordchainslib.word(net)}s, #0 to #999.", "cover": "",
                "first": 0, "last": SUB1K - 1}

    def no_command(e: V1Error) -> bool:
        return "unknown method" in str(e)

    def row_of(net: str, r: dict) -> dict:
        """V1.0's collection row -> the original Explore's."""
        if r.get("kind") == "sub1k" or r.get("id") == SUB1K_SLUG:
            return sub1k_row(net)
        if r.get("kind") == "known":
            return {"kind": "known", "slug": r.get("slug") or r.get("id"), "name": r.get("name") or r.get("slug"),
                    "collection": r.get("name") or r.get("slug"), "chain": r.get("chain") or net,
                    "supply": r.get("supply"), "count": r.get("count"), "cover": r.get("cover") or "",
                    "description": r.get("description") or "", "owners": r.get("owners"), "fp": r.get("fp"),
                    "state": r.get("state"), "imported": r.get("state") in (None, "held", "complete"),
                    "source": "Nintondo", "agreed": None, "height": r.get("height"), "created": r.get("height"),
                    "first": r.get("first"), "last": r.get("last")}
        rid = str(r.get("id") or "")
        return {"kind": "registry", "txid": rid.split("i")[0], "id": rid, "name": r.get("name") or "",
                "chain": r.get("chain") or net, "supply": r.get("supply"), "count": r.get("count"),
                "missing": r.get("missing"), "cover": r.get("cover") or "", "description": r.get("description") or "",
                "traits": r.get("traits") or [], "publisher": r.get("publisher"), "height": r.get("height"),
                "number": r.get("number"), "status": r.get("status"), "reason": r.get("reason") or "",
                "created": r.get("height"), "imported": True, "source": "registry", "agreed": None, "owners": None}

    SORTS = {"size": "size", "name": "name", "newest": "newest", "oldest": "oldest", "holders": "holders",
             "agreed": "newest"}

    @app.get("/explore/api/collections")
    def api_collections(net: str = "doge", q: str = "", sort: str = "size", source: str = "all", offset: str = "0",
                        limit: str = ""):
        if not known_net(net):
            return bad("this arcade does not read that chain", 404)
        at = int(offset) if str(offset).isdigit() else 0
        size = min(60, int(limit)) if str(limit).isdigit() and int(limit) > 0 else COLLECTIONS_PAGE
        try:
            got = ask("explorer.collections", net, q=(q or "").strip(), sort=SORTS.get(sort, "newest"), offset=at,
                      n=size, source=source if source in ("all", "registry", "known") else "all")
        except V1Error:                                     # no collections index here or on the mesh: Sub 1K
            low = (q or "").strip().lower()
            found = [sub1k_row(net)] if (not low or low in "sub 1k" or low in SUB1K_SLUG) else []
            return JSONResponse({"collections": found[at:at + size], "total": len(found), "offset": at, "limit": size,
                                 "note": "Published collections come here once this arcade's V1.0 keeps them."},
                                headers={"Cache-Control": "no-store"})
        rows = [row_of(net, r) for r in got.get("items") or []]
        return JSONResponse({"collections": rows, "total": int(got.get("total") or 0), "offset": at, "limit": size,
                             "note": got.get("note") or "", "reading": bool(got.get("reading")),
                             "from": _from(got)}, headers={"Cache-Control": "no-store"})

    def sub1k_page(net: str, start: int) -> dict:
        items = []
        if start < SUB1K:
            got = ask("explorer.inscriptions", net, before=min(start + SUB1K_PAGE, SUB1K), limit=SUB1K_PAGE)
            items = [_piece(x) for x in reversed(got.get("items") or []) if int(x.get("n") or -1) >= start]
        said = {"collection": sub1k_row(net), "loading": False, "items": items, "next": min(start + SUB1K_PAGE, SUB1K)}
        if start < SUB1K and not items:
            said["unanswered"] = f"Inscriptions #{start} to #{min(start + SUB1K_PAGE, SUB1K) - 1} are not indexed yet."
        return said

    @app.get("/explore/api/collection")
    def api_collection(net: str = "", slug: str = "", root: str = "", offset: str = "0"):
        if not re.match(r"^[0-9]{1,7}$", (offset or "0").strip()):
            return bad("a collection and an offset")
        start = int(offset)
        if slug == SUB1K_SLUG:
            if not known_net(net):
                return bad("this arcade does not read that chain", 404)
            return JSONResponse(sub1k_page(net, start), headers={"Cache-Control": "no-store"})
        if slug:
            if not known_net(net) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", slug):
                return bad("a chain, a collection's slug and an offset")
            try:
                got = ask("explorer.collection", net, slug=slug, offset=start, n=SUB1K_PAGE)
            except V1Error as e:
                return bad(str(e), 404)
            row = row_of(net, {**got, "kind": "known"})
            loading = got.get("loading")
            if isinstance(loading, dict):
                if loading.get("state") == "skipped":
                    return JSONResponse({"collection": row, "items": [], "loading": False,
                                         "skipped": loading.get("why") or "not fetched"},
                                        headers={"Cache-Control": "no-store"})
                return JSONResponse({"collection": row, "items": [], "loading": True, "have": loading.get("have") or 0,
                                     "of": loading.get("of"), "importing": bool(loading.get("asks_nintondo"))},
                                    headers={"Cache-Control": "no-store"})
            items = [{"id": it.get("id") or it.get("inscription"), "number": it.get("number"),
                      "name": it.get("name") or (f"{row['name']} #{it.get('number')}" if it.get("number") is not None
                                                 else ""),
                      "inscription_number": it.get("n"), "content_type": it.get("ct")} for it in got.get("items") or []]
            total = int(got.get("total") or 0)
            nxt = start + len(items)
            return JSONResponse({"collection": row, "loading": False, "items": items,
                                 "next": nxt if nxt < total else None, "from": _from(got)},
                                headers={"Cache-Control": "no-store"})
        root = (root or "").strip().lower()
        if not re.match(r"^[0-9a-f]{64}$", root):
            return bad("a collection (its root txid) and an offset")
        # The root may be inscribed on any chain this arcade reads: asked on the page's chain, else the home chain.
        got, last_err = None, None
        for where in [n for n in (net, "test") if n]:
            try:
                got = ask("explorer.collection", where, id=root + "i0", offset=start, n=SUB1K_PAGE)
                break
            except V1Error as e:
                last_err = e
        if got is None:
            return bad(str(last_err) if last_err else "no such collection", 404)
        name = got.get("name") or ""
        items = [{"id": it.get("id") or it.get("inscription"), "number": it.get("number"),
                  "name": f"{name} #{it.get('number')}" if it.get("number") is not None else "",
                  "inscription_number": it.get("n"), "content_type": it.get("ct"), "traits": it.get("traits") or {}}
                 for it in got.get("items") or []]
        total = int(got.get("total") or 0)
        nxt = start + len(items)
        return JSONResponse({"collection": row_of(net or "test", got), "loading": False, "items": items,
                             "next": nxt if nxt < total else None, "from": _from(got)},
                            headers={"Cache-Control": "no-store"})

    def in_collection(net: str, which: str) -> dict | None:
        """The viewer's "In collection": a published collection the inscription is in (Sub 1K is not shown there)."""
        try:
            got = ask("explorer.collection_of", net, id=which)
        except V1Error:
            return None
        for c in got.get("collections") or []:
            if c.get("kind") == "known":
                return {"kind": "known", "slug": c.get("id"), "collection": c.get("name") or "",
                        "position": c.get("number"), "name": f"{c.get('name') or ''} #{c.get('number')}".strip(),
                        "source": "Nintondo"}
            if c.get("kind") == "registry":
                rid = str(c.get("id") or "")
                return {"kind": "registry", "txid": rid.split("i")[0], "collection": c.get("name") or "",
                        "position": c.get("number"),
                        "name": f"{c.get('name') or ''} #{c.get('number')}".strip(), "source": "chain"}
        return None

    app.state.in_collection = in_collection
