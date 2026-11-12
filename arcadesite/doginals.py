"""The Wallet's Doginals (ord inscriptions) and DRC-20 / PRC-20 balances, for the account's own address or one of its
imported wallets, from Arcade V1.0's ord and token indexes (explorer.address, explorer.inscription,
explorer.collection_of) on that chain.

    GET /account/doginals/page   one page of what the address holds, in inscription number order (doginal_pages.view)
    GET /account/doginal-card    one inscription's card
    GET /account/token20         DRC-20 (Dogecoin) / PRC-20 (Pepecoin) balances

Every inscription an address holds comes from explorer.held (V1.0 0.1.37, 500 a page), collections from
explorer.collection_of with `ids` (500 at once), tokens from explorer.address without history/inscriptions. On an older
V1.0 each falls back to what it has: explorer.address's first 60 inscriptions, one collection call per inscription.
"""
from __future__ import annotations

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from fastapi import Request
from fastapi.responses import JSONResponse

from arcade import doginal_pages as pageslib

from . import imported as importedlib
from .v1 import V1Error

#: Chains whose ord inscriptions the Wallet shows (V1.0 keys them by network here).
ORD_CHAINS = ("doge", "main", "dgb", "flop", "wjk", "kot", "ltc")
TOKEN20 = {"doge": "drc-20", "main": "prc-20"}
FRESH_FOR = 60.0
STALE_FOR = 3600.0
NOT_ART = pageslib.NOT_ART
INSCRIPTION_ID = re.compile(r"[0-9a-f]{64}i[0-9]+")


def register(app, st, chains):
    v1 = st.v1
    kept: dict[tuple[str, str], tuple[float, dict]] = {}
    colls: dict[tuple[str, str], dict | None] = {}
    lock = threading.Lock()
    pool = ThreadPoolExecutor(max_workers=8)

    def account_of(request: Request):
        return app.state.signed_in_account(request)

    def own_address(account, one, address: str) -> str:
        """The account's main address on `one`, or one of its imported wallets there; anything else is refused."""
        main = chains.account_address(account.pubkey, one)
        address = (address or "").strip() or main
        if address != main and importedlib.entry(st, one.network, account.pubkey, address) is None:
            raise PermissionError("only this account's own addresses")
        return address

    def chain_of(net: str):
        one = chains.by_network(net or "doge")
        if one is None or one.network not in {c.network for c in chains.account_chains()}:
            raise LookupError("no such chain here")
        return one

    refreshing: set = set()

    def store(key, got) -> None:
        with lock:
            if len(kept) > 200:
                kept.clear()
            kept[key] = (time.time(), got)

    def cached(key, fresh: bool, read):
        """What V1.0 last said, at once: younger than FRESH_FOR as it is; up to STALE_FOR old as it is while it is
        read again behind the page (a big wallet's list takes seconds to read); older, or `fresh`, read now."""
        with lock:
            hit = kept.get(key)
        age = time.time() - hit[0] if hit else None
        if hit and not fresh and age < FRESH_FOR:
            return hit[1]
        if hit and not fresh and age < STALE_FOR:
            with lock:
                start = key not in refreshing
                refreshing.add(key)
            if start:
                def again():
                    try:
                        store(key, read())
                    except Exception:                       # noqa: BLE001 -- the old answer stands
                        pass
                    finally:
                        with lock:
                            refreshing.discard(key)
                threading.Thread(target=again, daemon=True).start()
            return hit[1]
        got = read()
        store(key, got)
        return got

    def address_view(one, address: str, fresh: bool = False, tokens_only: bool = False) -> dict:
        """explorer.address; with `tokens_only` the light form (V1.0 0.1.37: history/inscriptions left out), the full
        one on an older V1.0."""
        def read():
            if tokens_only:
                try:
                    return v1.cmd("explorer.address", chain=one.key, address=address, history=False,
                                  inscriptions=False) or {}
                except V1Error:
                    pass
            return v1.cmd("explorer.address", chain=one.key, address=address) or {}
        return cached(("address", tokens_only, one.network, address), fresh, read)

    def held_list(one, address: str, fresh: bool = False) -> tuple[list[dict], int]:
        """Every inscription the address holds, by number: explorer.held 500 at a time (V1.0 0.1.37). On an older
        V1.0, explorer.address's first 60 (NEEDS-V1 until it is installed)."""
        def read():
            def page(offset: int) -> tuple[list[dict], int]:
                got = v1.cmd("explorer.held", _timeout=90, chain=one.key, address=address, offset=offset,
                             limit=500, order="old") or {}
                return ([r for r in got.get("items") or [] if isinstance(r, dict) and r.get("id")],
                        int(got.get("total") or 0))
            try:
                rows, total = page(0)                       # the first page says how many there are
                for more, _ in pool.map(page, range(500, total, 500)):   # the rest side by side
                    rows += more
                return rows, total, True
            except V1Error:
                got = v1.cmd("explorer.address", chain=one.key, address=address) or {}
                rows = [r for r in got.get("inscriptions") or [] if isinstance(r, dict) and r.get("id")]
                return rows, int(got.get("inscription_count") or len(rows)), False
        rows, total, _whole = cached(("held", one.network, address), fresh, read)
        return rows, total

    def one_collection(rows) -> dict | None:
        first = rows[0] if isinstance(rows, list) and rows else None
        if not isinstance(first, dict):
            return None
        name = str(first.get("name") or first.get("slug") or "")
        num = first.get("number")
        return {"key": str(first.get("id") or first.get("slug") or name), "name": name,
                "item": f"{name} #{num}" if num is not None and name else None}

    def keep_coll(key, value) -> None:
        if len(colls) > 200000:
            colls.clear()
        colls[key] = value

    def collection_of(one, iid: str) -> dict | None:
        key = (one.network, iid)
        if key in colls:
            return colls[key]
        try:
            got = v1.cmd("explorer.collection_of", chain=one.key, id=iid) or {}
        except V1Error:
            return None                                     # asked again next time
        out = one_collection(got.get("collections") if isinstance(got, dict) else got)
        keep_coll(key, out)
        return out

    def collections_for(one, ids: list[str]) -> dict[str, dict | None]:
        """Each id's collection: kept ones from memory, the rest asked 500 at a time (V1.0 0.1.37 `ids`), or one at a
        time on an older V1.0."""
        out = {i: colls[(one.network, i)] for i in ids if (one.network, i) in colls}
        left = [i for i in ids if i not in out]
        for k in range(0, len(left), 500):
            part = left[k:k + 500]
            try:
                got = v1.cmd("explorer.collection_of", _timeout=60, chain=one.key, ids=part) or {}
                rows = got.get("items") if isinstance(got, dict) else None
                if not isinstance(rows, list) or len(rows) != len(part):
                    raise V1Error("no batch answer")
            except V1Error as exc:
                if "collections index is not on" in str(exc):
                    # This chain's collections are not indexed here: none to show, and nothing to ask one by one
                    # (7,038 single calls took half a minute). Not kept, so they appear once the index is on.
                    out.update({i: None for i in part})
                    continue
                for i, c in zip(part, pool.map(lambda i: collection_of(one, i), part)):   # an older V1.0: one by one
                    out[i] = c
                continue
            for i, row in zip(part, rows):
                if isinstance(row, dict) and row.get("error"):
                    continue                                # asked again next time
                c = one_collection((row or {}).get("collections") if isinstance(row, dict) else None)
                keep_coll((one.network, i), c)
                out[i] = c
        return out

    def token_of(one, ins: dict) -> bool:
        proto = TOKEN20.get(one.network)
        if not proto:
            return False
        if "p" in ins and not isinstance(ins.get("content"), dict):
            return str(ins.get("p") or "").lower() == proto  # explorer.held says it
        data = ((ins.get("content") or {}).get("data") or "") if isinstance(ins.get("content"), dict) else ""
        if not isinstance(data, str) or not NOT_ART.match(str(ins.get("ct") or "")):
            return False
        text = data.split(",", 1)[1] if data.startswith("data:") and "," in data else data
        try:
            return str((json.loads(text) or {}).get("p") or "").lower() == proto
        except (ValueError, AttributeError):
            return False

    def item(one, ins: dict) -> dict:
        at = str(ins.get("at") or "").split(":")
        ctype = str(ins.get("ct") or "") or None
        return {"id": ins.get("id"), "txid": at[0] if at else "", "vout": int(at[1]) if len(at) > 1 and at[1].isdigit() else 0,
                "value": 0, "offset": int(at[2]) if len(at) > 2 and at[2].isdigit() else 0,
                "number": ins.get("n"), "height": ins.get("h"), "content_type": ctype, "size": ins.get("size"),
                "text": bool(ctype and NOT_ART.match(ctype)), "token": token_of(one, ins), "collection": None}

    @app.get("/account/doginals/page")
    def doginals_page(request: Request, chain: str = "doge", address: str = "", page: int = 0, size: int = 30,
                      kind: str = "all", coll: str = "", order: str = "old", go: str = "", fresh: int = 0):
        account = account_of(request)
        try:
            one = chain_of(chain)
            address = own_address(account, one, address)
        except LookupError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=404)
        except PermissionError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=403)
        if one.network not in ORD_CHAINS:
            return JSONResponse({"detail": "this node does not check inscriptions on that chain"}, status_code=404)
        try:
            rows, total = held_list(one, address, fresh=bool(fresh))
        except V1Error as exc:
            return JSONResponse({"detail": f"this node cannot read that chain's inscriptions yet: {exc}"}, status_code=503)
        items = [item(one, r) for r in rows]
        found = collections_for(one, [g["id"] for g in items])
        for g in items:
            g["collection"] = found.get(g["id"])
        items.sort(key=lambda g: (g["number"] is None, g["number"] if g["number"] is not None else 0, g["id"]))
        doc = {"items": items, "done": True, "checked": len(items), "of": total, "unread": 0, "error": ""}
        return JSONResponse({"chain": one.network, "address": address,
                             **pageslib.view(doc, kind=kind, coll=coll, order=order, page=page, size=size, go=go)},
                            headers={"Cache-Control": "no-store"})

    @app.get("/account/doginal-card")
    def doginal_card(request: Request, chain: str = "doge", id: str = ""):
        account_of(request)
        iid = (id or "").strip().lower()
        if not INSCRIPTION_ID.fullmatch(iid):
            return JSONResponse({"detail": "an inscription id: <txid>i<n>"}, status_code=400)
        try:
            one = chain_of(chain)
            ins = v1.cmd("explorer.inscription", chain=one.key, id=iid) or {}
        except LookupError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=404)
        except V1Error as exc:
            return JSONResponse({"detail": f"the {chain} index could not say just now: {exc}"}, status_code=503)
        if not ins.get("id") and not ins.get("n"):
            return JSONResponse({"detail": "no copy of the index holds that inscription yet"}, status_code=404)
        at = str(ins.get("at") or "").split(":")
        card = {"id": iid, "number": ins.get("n"), "height": ins.get("h"), "content_type": ins.get("ct"),
                "size": ins.get("size"), "owner": ins.get("owner"),
                "output": ":".join(at[:2]) if len(at) >= 2 else None,
                "offset": int(at[2]) if len(at) > 2 and at[2].isdigit() else 0}
        made = None
        try:
            if ins.get("h") is not None:
                made = int(v1.core(one.network, "getblockheader", v1.core(one.network, "getblockhash", int(ins["h"])))["time"])
        except (V1Error, KeyError, TypeError, ValueError):
            made = None
        return JSONResponse({"chain": one.network, "inscription": card, "made": made,
                             "collection": collection_of(one, iid),
                             "content": f"/content/{iid}?chain={one.network}", "listing": None},
                            headers={"Cache-Control": "no-store"})

    @app.get("/account/token20")
    def token20(request: Request, chain: str = "doge", address: str = ""):
        account = account_of(request)
        if chain not in TOKEN20:
            return JSONResponse({"detail": "DRC-20 and PRC-20 are on Dogecoin and Pepecoin main"}, status_code=404)
        try:
            one = chain_of(chain)
            address = own_address(account, one, address)
        except LookupError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=404)
        except PermissionError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=403)
        try:
            got = address_view(one, address, tokens_only=True)
        except V1Error as exc:
            return JSONResponse({"chain": one.network, "address": address, "protocol": TOKEN20[chain], "balances": None,
                                 "detail": str(exc)[:200]}, headers={"Cache-Control": "no-store"})
        balances = [{"tick": t.get("tick"), "decimals": int(t.get("dec") or 18), "units": str(t.get("overall") or "0"),
                     "transferable_units": str(t.get("transferable") or "0"), "pending_units": "0", "listed_units": "0"}
                    for t in got.get("tokens") or [] if isinstance(t, dict) and t.get("tick")]
        return JSONResponse({"chain": one.network, "address": address, "protocol": TOKEN20[chain],
                             "balances": balances, "walked_to": None, "tip": None},
                            headers={"Cache-Control": "no-store"})
