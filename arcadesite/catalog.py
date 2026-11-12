"""The public catalog: tokens, NFTs (inscriptions), collections, a launch's thread and a mintpad, read from Arcade
V1.0 (the arcade ledger of the tag chain, Pepecoin testnet) and drawn by the original's templates with the context
they read.

    GET /tokens                          every ledger token, newest first, 50 a page (+ the account's create form)
    GET /tokens/{property_id}            one token: supply, holders, its mintpads (?chain= another ledger chain)
    GET /nfts                            every inscription, newest first, 24 a page (+ the account's inscribe panels)
    GET /inscriptions                    the old name of /nfts: a redirect
    GET /inscriptions/{key}              a piece's bare address: a redirect to /view
    GET /inscriptions/{key}/view         one piece; an HTML piece in its sandboxed, ticketed frame (frame_src)
    GET /collections                     every ledger collection, newest first
    GET /collections/{creator}/{name}    one collection, a page of its pieces in edition order, its traits
    GET /launches                        folded into Exchange > Mintpads (as the original): a redirect
    GET /launches/{txid}                 one launch (a token's creation, a collection's #1, an app card) and its thread
    GET /mintpad/{creator}/{name}        a collection's mintpad, read only (Mint is the market module's)
    GET /r/mintpad/{creator}/{name}      what that pad's script polls: listings left (see NEEDS-V1)

The operator's collection wizard (/inscriptions/collection, /inscriptions/collection/{job_id}) is not public in the
original's door (door.py PUBLIC_PAGES), so it is not served here.

NEEDS-V1: a token's history (index.history: grants, sends, revokes per token) -- shown as "Nothing yet".
NEEDS-V1: every holder of a token with its balance -- explorer.ledger_token's `top` is what is shown.
NEEDS-V1: prices on pieces (asks, shop listings, the pre-signed listing book: _prices_for, _open_listing_of), offers on
          a piece (offers_on / pending_offers / bids), a pending swap of it and cross-chain bids -- shown as none.
NEEDS-V1: mintpad listings (the listing book behind /r/mintpad) -- a pad shows nothing left; token mintpad facts
          (pad supply from the order book) -- not drawn above the frame.
NEEDS-V1: tokens still in the mempool (pending_properties) and pieces still being inscribed (unfinished).
NEEDS-V1: comments and likes on a launch (feed acts aimed at a token's creation or a collection's #1: V1.0's `feed`
          answers posts only) -- a launch's thread shows "No comments yet".
"""
from __future__ import annotations

import concurrent.futures
import json
import re
import time
import urllib.parse
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from arcade import inscriptions as inscriptionlib
from arcade import media
from arcade import tags as taglib
from arcade import tokens as tokenlib
from arcade.config import NETWORKS
from arcade.ledger import format_amount
from arcade.web import content as contentlib
from arcade.web.state import ChainContext

from . import legacy as L
from .v1 import V1Error, chain_key

PAGE_INSCRIPTIONS = 24          # the original's PAGE_INSCRIPTIONS
PAGE_TOKENS = 50                # the original's PAGE_TOKENS (and explorer.ledger_tokens' most per call)
KEY = re.compile(r"^(?:[0-9a-fA-F]{64}(?:i[0-9]+)?|[0-9]{1,12})$")
TXID = re.compile(r"^[0-9a-f]{64}$")
#: How an account's mintpad page looks (the original's MINTPAD_TEMPLATES keys; the look is kept per site).
MINTPAD_TEMPLATES = ("spotlight", "wall", "gallery", "arcade", "lottery", "neon", "polaroid", "terminal", "minimal")
EMPTY_PAD = {"left": 0, "next": None, "prices": [], "price_texts": [], "listed": []}


def _int(v, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _count(got) -> int:
    """/r/.../count answers {"count": n} in the original and a bare n from V1.0."""
    if isinstance(got, dict):
        return _int(got.get("count"))
    return _int(got)


def _row(d: dict) -> dict:
    """The page API's description of a piece (content.describe) -> the original index row the templates read."""
    ct = str(d.get("contenttype") or d.get("content_type") or "")
    raw = d.get("rawjson")
    if raw is None:
        j = d.get("json")
        raw = json.dumps(j) if isinstance(j, (dict, list)) else str(j or "")
    return {"txid": str(d.get("id") or d.get("txid") or "").split("i")[0], "number": _int(d.get("number")),
            "creator": str(d.get("creator") or ""), "owner": str(d.get("owner") or ""),
            "creatortag": d.get("creatortag"), "ownertag": d.get("ownertag"),
            "block_height": _int(d.get("block", d.get("block_height"))), "position": None, "content_type": ct,
            "content_len": _int(d.get("length", d.get("content_len"))), "sha256": str(d.get("sha256") or ""),
            "json": raw or "", "chunks": _int(d.get("transactions", d.get("chunks")), 1) or 1,
            "held": bool(d.get("held")), "collection": d.get("collection"), "edition": d.get("edition")}


def _collection(d: dict) -> dict:
    """The page API's description of a collection (content.describe_collection) -> the original's summary row."""
    return {"creator": str(d.get("creator") or ""), "collection": str(d.get("name") or ""),
            "count": _int(d.get("count")), "first_number": _int(d.get("firstnumber")),
            "last_number": _int(d.get("lastnumber")), "first_edition": d.get("firstedition"),
            "last_edition": d.get("lastedition"), "cover_txid": d.get("cover"), "cover_type": d.get("covertype"),
            "supply": d.get("supply")}


def _prop(t: dict) -> dict:
    """explorer.ledger_token(s)' row -> the original's property row (ledger._property_row)."""
    divisible = _int(t.get("type")) == 2
    total = _int(t.get("total"))
    return {"property_id": _int(t.get("id")), "ecosystem": _int(t.get("ecosystem"), 1),
            "property_type": _int(t.get("type"), 1), "issuer": str(t.get("issuer") or ""),
            "category": str(t.get("category") or ""), "subcategory": str(t.get("subcategory") or ""),
            "name": str(t.get("name") or ""), "url": str(t.get("url") or ""), "data": str(t.get("data") or ""),
            "managed": bool(_int(t.get("managed"))), "total_tokens": total,
            "creation_txid": str(t.get("creation_txid") or ""), "creation_block": _int(t.get("creation_block")),
            "divisible": divisible, "test_ecosystem": _int(t.get("ecosystem"), 1) == 2,
            "total_display": format_amount(total, divisible),
            "holder_count": _int(t.get("holders")) if t.get("holders") is not None else 0}


def _shown_units(text: Any, divisible: bool) -> tuple[int, str]:
    """A balance as V1.0 shows it ("40000", "1,000.5") -> (units, the original's display)."""
    try:
        amount = Decimal(str(text or "0").replace(",", ""))
    except InvalidOperation:
        return 0, str(text or "")
    units = int(amount * 100_000_000) if divisible else int(amount)
    return units, format_amount(units, divisible)


def _about_of(data: Any) -> str:
    """A token's description as a person wrote it (the original's _about_of)."""
    text = str(data or "")
    try:
        said = json.loads(text)
    except ValueError:
        return text
    if isinstance(said, dict):
        return str(said.get("about") or "")
    return text


def _pages(total: int, per: int) -> int:
    return max(1, -(-total // per))


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

    def render(request: Request, template: str, **ctx):
        # wall_text(text, label): these templates pass the label too (pages.py's global takes one argument).
        ctx.setdefault("wall_text", lambda text, label="": str(text or ""))
        return app.state.render(request, template, **ctx)

    # ---------------------------------------------------------------- the chain, as the templates read it
    def ctx_chain(network: str = "test") -> ChainContext:
        params = NETWORKS.get(network) or NETWORKS["test"]
        return ChainContext(network if network in NETWORKS else "test", "ledger", params.label)

    def node_status(network: str = "test") -> dict:
        """chain.status(), from V1.0's chains.list: what _chains.html's card reads."""
        def build():
            try:
                listed = {c.get("key"): c for c in (v1.cmd("chains.list") or [])}
            except V1Error as e:
                return {"online": False, "error": str(e)}
            c = listed.get(chain_key(network))
            if not c:
                return {"online": False, "error": "this arcade does not keep that chain"}
            tip = _int(c.get("tip"))
            try:
                peers = _int(((v1.cmd("status") or {}).get("mesh") or {}).get("links"))
            except V1Error:
                peers = None
            synced = str(c.get("state") or "").startswith("synced")
            return {"online": True, "chain": c.get("name"), "blocks": tip, "headers": tip, "synced": synced,
                    "peers": peers, "wallet": None, "borrowed": bool(c.get("borrowed"))}
        return keep(("node", network), 15, build)

    def index_status(network: str = "test") -> dict:
        """index.status(): V1.0 keeps its ledger current with its node, so behind is 0 when synced."""
        node = node_status(network)
        params = NETWORKS.get(network) or NETWORKS["test"]
        tip = node.get("blocks") if node.get("online") else None
        return {"enabled": True, "activation_height": params.activation_height, "indexed_height": tip,
                "node_tip": tip, "behind": 0 if tip is not None else None, "current": tip is not None,
                "stopped": None, "last_sync": None, "stats": {}}

    # ---------------------------------------------------------------- who is looking
    def account_here(request: Request, network: str = "test") -> tuple[Any, str]:
        """(the signed-in account or None, its address on that chain or "")."""
        account = app.state.signed_in(request)
        if account is None:
            return None, ""
        chain = chains.by_network(network) or chains.account_chain()
        return account, chains.account_address(account.pubkey, chain)

    def me_of(request: Request) -> dict:
        """The original's _tag_of_whoever_is_asking: the account's tag and address (the feed's me_of)."""
        account, home = account_here(request)
        tag = None
        if account is not None:
            try:
                tag = (str((v1.page(f"address/{home}")[1] or {}).get("tag") or "") if home else "") or None
            except V1Error:
                tag = None
            tag = tag or (st.vault().by_pubkey(account.pubkey) or {}).get("tag") or None
        return {"tag": tag, "address": home, "chain": chains.account_chain(), "min": taglib.MIN_LENGTH,
                "max": taglib.MAX_LENGTH}

    def purses(address: str) -> list[dict]:
        """What one address holds, one row per token (the original's _purses over index.balances)."""
        if not address:
            return []
        try:
            code, rows = v1.page(f"balances/{address}")
        except V1Error:
            return []
        out = []
        for r in (rows if code == 200 and isinstance(rows, list) else []):
            units = _int(r.get("units"))
            if units <= 0:
                continue
            divisible = bool(r.get("divisible"))
            display = format_amount(units, divisible)
            piece = {"address": address, "balance": units, "display": display}
            out.append({"property_id": _int(r.get("propertyid")), "name": str(r.get("name") or ""),
                        "divisible": divisible, "test_ecosystem": False, "balance": units, "pieces": [piece],
                        "display": display, "largest": piece})
        return sorted(out, key=lambda p: p["property_id"])

    def coins_of(address: str, network: str = "test") -> float:
        if not address:
            return 0.0
        try:
            got = v1.coins([address], network) or {}
            return _int(((got.get("addresses") or {}).get(address) or {}).get("total")) / 100_000_000
        except V1Error:
            return 0.0

    # ---------------------------------------------------------------- reads
    def piece(key: str) -> dict | None:
        try:
            code, got = v1.page(f"inscription/{key}")
        except V1Error:
            return None
        return _row(got) if code == 200 and isinstance(got, dict) and got.get("id") else None

    def drawable_icon(icon: str) -> bool:
        """A token's icon is shown only when its bytes are kept and a browser draws them (_faces_for)."""
        def build():
            row = piece(icon)
            return bool(row and row["held"] and row["content_type"].startswith("image/"))
        return keep(("icon", icon), 300, build)

    def face_of(prop: dict) -> dict:
        face = tokenlib.details(prop)
        if face.get("icon") and not drawable_icon(face["icon"]):
            face["icon"] = ""
        return face

    def ledger_token(pid: int, network: str = "test") -> dict | None:
        try:
            return v1.cmd("explorer.ledger_token", chain=chain_key(network), id=int(pid)) or None
        except V1Error as e:
            said = str(e).lower()
            if "no arcade token" in said or "no such" in said or "not found" in said:
                return None
            raise

    def all_tokens(network: str = "test") -> list[dict]:
        """Every ledger token's row, newest first (for finding a launch by its creating transaction)."""
        def build():
            out, offset = [], 0
            while True:
                got = v1.cmd("explorer.ledger_tokens", chain=chain_key(network), offset=offset,
                             limit=PAGE_TOKENS) or {}
                items = got.get("items") or []
                out += items
                offset += len(items)
                if not items or offset >= _int(got.get("total")):
                    return out
        return keep(("tokens", network), 60, build)

    def all_collections() -> list[dict]:
        def build():
            out, offset = [], 0
            while True:
                code, rows = v1.page("collections", {"limit": 500, "offset": offset})
                rows = rows if code == 200 and isinstance(rows, list) else []
                out += rows
                offset += len(rows)
                if len(rows) < 500:
                    return out
        return keep("collections", 60, build)

    def address_of_tag(tag: str) -> str:
        return v1.tag_owner(tag)

    # ================================================================ tokens
    @app.get("/tokens", response_class=HTMLResponse)
    def tokens_page(request: Request, page: int = 1):
        """Every token on the chain, and the way to make one (the account's form, signed in its tab)."""
        chain = ctx_chain()
        account, address = account_here(request)
        data: dict[str, Any] = {
            "chain": chain, "node": node_status(), "other_chains": [], "index": index_status(),
            "tokens": [], "pending_tokens": [], "holdings": [], "funded": [], "owned": set(), "pending": [],
            "node_error": None, "faces": {}, "my_pictures": [], "page": 1, "pages": 1, "total": 0, "tags": {},
            "purses": [], "prepared": None, "account_address": address, "account_signed": account is not None}
        try:
            first = v1.cmd("explorer.ledger_tokens", chain=chain_key("test"), limit=1) or {}
            data["total"] = _int(first.get("total"))
            data["pages"] = _pages(data["total"], PAGE_TOKENS)
            data["page"] = max(1, min(int(page), data["pages"]))
            got = v1.cmd("explorer.ledger_tokens", chain=chain_key("test"),
                         offset=(data["page"] - 1) * PAGE_TOKENS, limit=PAGE_TOKENS) or {}
            items = got.get("items") or []
            # Holders per token: explorer.ledger_tokens leaves them out, explorer.ledger_token says (a few ms each).
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                full = list(pool.map(lambda t: keep(("token", _int(t.get("id"))), 30,
                                                    lambda: ledger_token(_int(t.get("id"))) or t), items))
            data["tokens"] = [_prop(t) for t in full]
        except V1Error as e:
            data["node_error"] = f"the token index could not be read: {e}"
            return render(request, "tokens.html", **data)
        data["owned"] = {address} if address else set()
        data["purses"] = purses(address)
        data["holdings"] = [dict(p["largest"], property_id=p["property_id"], name=p["name"]) for p in data["purses"]]
        data["faces"] = {t["property_id"]: face_of(t) for t in data["tokens"]}
        if address:
            try:
                code, rows = v1.page("inscriptions", {"owner": address, "limit": 200})
                rows = [_row(r) for r in rows] if code == 200 and isinstance(rows, list) else []
            except V1Error:
                rows = []
            data["my_pictures"] = [
                {"txid": r["txid"], "label": " · ".join(x for x in (
                    (L._fromjson(r["json"]) or {}).get("name") if isinstance(L._fromjson(r["json"]), dict) else None,
                    f"#{r['number']:,}") if x)}
                for r in rows if r["held"] and r["content_type"].startswith("image/")]
        return render(request, "tokens.html", **data)

    def token_pads(pid: int) -> list[dict]:
        """Token mintpads on the chain (the original's _token_pads): an HTML inscription whose JSON says
        {tokenpad: {creator, property_id, lot, look}}, inscribed by that seller. How many mints are left needs the
        order book (NEEDS-V1), so `mints` is None: a tile draws no number rather than a wrong one."""
        def build():
            out, offset = [], 0
            # The newest few hundred HTML pieces: what the original's query read (LIMIT 500).
            while offset < 500:
                code, rows = v1.page("inscriptions", {"limit": 100, "offset": offset})
                rows = rows if code == 200 and isinstance(rows, list) else []
                for d in rows:
                    j = d.get("json") if isinstance(d.get("json"), dict) else None
                    if j and isinstance(j.get("tokenpad"), dict) and \
                            str(d.get("contenttype") or "").startswith("text/html"):
                        out.append(d)
                if len(rows) < 100:
                    break
                offset += 100
            return out
        out = []
        for d in keep("tokenpads", 60, build):
            pad = d["json"]["tokenpad"]
            if pad.get("creator") != d.get("creator") or _int(pad.get("property_id")) != pid:
                continue
            t = ledger_token(pid)
            if not t:
                continue
            prop = _prop(t)
            out.append({"txid": str(d.get("id") or "").split("i")[0], "number": d.get("number"),
                        "seller": d.get("creator"), "property_id": pid, "name": prop["name"],
                        "lot": format_amount(_int(pad.get("lot")), prop["divisible"]), "mints": None,
                        "look": str(pad.get("look") or "counter")})
        return out[:50]

    @app.get("/tokens/{property_id}", response_class=HTMLResponse)
    def token_page(request: Request, property_id: int, chain: str = ""):
        network = chain if chain and chains.by_network(chain) is not None else "test"
        t = ledger_token(property_id, network)
        if not t:
            raise HTTPException(status_code=404, detail=f"no token {property_id}")
        prop = _prop(t)
        holders = []
        for h in t.get("top") or []:
            units, display = _shown_units(h.get("balance"), prop["divisible"])
            holders.append({"address": h.get("address"), "balance": units, "reserved": 0, "held": units,
                            "display": display, "reserved_display": ""})
        _, mine = account_here(request, network)
        return render(request, "token.html", prepared=None, chain=ctx_chain(network), node=node_status(network),
                      prop=prop, holders=holders, history=[], index=index_status(network), owned=set(),
                      is_issuer=False, face=face_of(prop), tags={}, about=_about_of(prop.get("data")), pad=None,
                      node_error=None, account_issuer=bool(mine) and mine == prop["issuer"], mine_address=mine,
                      token_pads=token_pads(property_id) if network == "test" else [])

    # ================================================================ NFTs
    @app.get("/nfts", response_class=HTMLResponse)
    def nfts_page(request: Request, page: int = 1):
        """Everything inscribed on this chain, drawn for the reader (the account's address, never a node's)."""
        account, address = account_here(request)
        data: dict[str, Any] = {
            "chain": ctx_chain(), "node": node_status(), "other_chains": [], "index": index_status(),
            "inscriptions": [], "mine": [], "unfinished": [], "owned": {address} if address else set(),
            "funded": [], "error": None, "plan": None, "node_error": None, "my_tag": None, "tags": {},
            "my_total": 0, "listed": {}, "page": page, "pages": 1, "per_page": PAGE_INSCRIPTIONS, "total": 0,
            "account_address": address, "account_signed": account is not None}
        try:
            data["total"] = _count(v1.page("inscriptions/count")[1])
            data["pages"] = _pages(data["total"], PAGE_INSCRIPTIONS)
            data["page"] = max(1, min(page, data["pages"]))
            code, rows = v1.page("inscriptions", {"limit": PAGE_INSCRIPTIONS,
                                                  "offset": (data["page"] - 1) * PAGE_INSCRIPTIONS})
            data["inscriptions"] = [_row(r) for r in rows] if code == 200 and isinstance(rows, list) else []
        except V1Error as e:
            data["node_error"] = f"the index could not be read: {e}"
        return render(request, "inscriptions.html", **data)

    @app.get("/inscriptions", response_class=HTMLResponse)
    def inscriptions_page(page: int = 1):
        """The old name for /nfts, kept: links to it are on the chain."""
        return RedirectResponse("/nfts" + (f"?page={page}" if page != 1 else ""), status_code=303)

    @app.get("/inscriptions/{key}")
    def inscription_bare(key: str):
        if not re.fullmatch(r"[0-9a-fA-F]{64}|[0-9]{1,12}", key or ""):
            raise HTTPException(status_code=404, detail="no such piece")
        return RedirectResponse(f"/inscriptions/{key}/view", status_code=307)

    @app.get("/inscriptions/{key}/view", response_class=HTMLResponse)
    def inscription_view(request: Request, key: str):
        """One piece. Its content is never put into this page: an HTML piece runs in a sandboxed frame from the
        pages host, with the reader's ticket (frame_src), so inscribed code has an opaque origin."""
        row = piece(key) if KEY.fullmatch(key or "") else None
        if row is None:
            return RedirectResponse("/inscriptions", status_code=303)
        account, here = account_here(request)
        viewer = "account" if account is not None else "nobody"
        held, coins, mine_account = [], 0.0, False
        if here:
            held, coins = purses(here), coins_of(here)
            mine_account = row["owner"] == here
        # The frame's address is the site's frame_src (pages origin + /content/<id> + ?v=<ticket>); the template
        # writes it as {{ pages }}/content/{{ txid }}?v={{ ticket }}, so it is handed over in those two parts.
        pages, ticket = "", ""
        renders = media.standard_type(row["content_type"]).startswith(contentlib.RENDERABLE)
        if renders:
            src = app.state.frame_src(request, row["txid"])
            at = src.find("/content/")
            pages = src[:at] if at > 0 else ""
            ticket = (urllib.parse.parse_qs(urllib.parse.urlsplit(src).query).get("v") or [""])[0]
        return render(request, "inscription_view.html", row=row, chain=ctx_chain(), xchain_open=False, x_bids=[],
                      my_offers=[], swap_waiting="", ticket=ticket, listed=None, tag=row.get("ownertag"),
                      sale=None, creator_tag=row.get("creatortag"), pages=pages, mine=False, viewer=viewer,
                      mine_account=mine_account, asks=[], tokens=held, coins=coins, advice="", pad_facts=None,
                      renders=renders)

    # ================================================================ collections
    @app.get("/collections", response_class=HTMLResponse)
    def collections_page(request: Request, page: int = 1):
        data: dict[str, Any] = {"chain": ctx_chain(), "node": node_status(), "other_chains": [],
                                "collections": [], "node_error": None, "tags": {}, "page": page, "pages": 1,
                                "total": 0}
        try:
            data["total"] = _count(v1.page("collections/count")[1])
            data["pages"] = _pages(data["total"], PAGE_INSCRIPTIONS)
            page = max(1, min(page, data["pages"]))
            data["page"] = page
            code, rows = v1.page("collections", {"limit": PAGE_INSCRIPTIONS,
                                                 "offset": (page - 1) * PAGE_INSCRIPTIONS})
            data["collections"] = [_collection(r) for r in rows] if code == 200 and isinstance(rows, list) else []
        except V1Error as e:
            data["node_error"] = f"the index could not be read: {e}"
        return render(request, "collections.html", **data)

    def collection_summary(creator: str, name: str, limit: int = 1, offset: int = 0,
                           traits: bool = False) -> tuple[dict | None, list[dict], dict]:
        params = {"limit": limit, "offset": offset}
        if traits:
            params["traits"] = 1
        code, got = v1.page(f"collection/{urllib.parse.quote(creator, safe='')}/{urllib.parse.quote(name, safe='')}",
                            params)
        if code != 200 or not isinstance(got, dict) or not got.get("name"):
            return None, [], {}
        summary = _collection(got)
        # What #1 says about the set (collection_details over the cover's JSON): its description, artist, link.
        details: dict = {}
        if summary["cover_txid"]:
            cover = next((r for r in got.get("items") or [] if str(r.get("id") or "").split("i")[0]
                          == summary["cover_txid"]), None)
            raw = (cover or {}).get("rawjson")
            if cover is None:
                c = piece(summary["cover_txid"])
                raw = c["json"] if c else ""
            try:
                details = inscriptionlib.collection_details(raw or "") or {}
            except Exception:                               # noqa: BLE001
                details = {}
        summary["details"] = details
        if summary["supply"] is None and details.get("supply"):
            summary["supply"] = details.get("supply")
        return summary, [_row(r) for r in got.get("items") or []], got.get("traits") or {}

    @app.get("/collections/{creator}/{name}", response_class=HTMLResponse)
    def collection_page(request: Request, creator: str, name: str, page: int = 1):
        summary, _, _ = collection_summary(creator, name)
        if summary is None:
            return RedirectResponse("/collections", status_code=303)
        pages = _pages(summary["count"], PAGE_INSCRIPTIONS)
        page = max(1, min(page, pages))
        _, rows, traits = collection_summary(creator, name, limit=PAGE_INSCRIPTIONS,
                                             offset=(page - 1) * PAGE_INSCRIPTIONS, traits=True)
        for r in rows:
            r["listing"] = None                             # NEEDS-V1: prices on pieces
        _, address = account_here(request)
        return render(request, "collection.html", chain=ctx_chain(), node=node_status(), summary=summary,
                      inscriptions=rows, owned={address} if address else set(), for_sale=[], account_view=True,
                      tags={}, traits=traits, page=page, pages=pages, per_page=PAGE_INSCRIPTIONS)

    # ================================================================ launches
    @app.get("/launches", response_class=HTMLResponse)
    def launches_page(sort: str = "popular"):
        """Folded into Exchange > Mintpads (2026-09-28); each launch's own thread still answers."""
        return RedirectResponse("/exchange?tab=mintpads" + ("&sort=new" if sort == "new" else ""), status_code=303)

    def launch_of(txid: str) -> dict | None:
        """A token by its creating transaction, a collection by its #1, else an app card (index.launches)."""
        for t in all_tokens():
            if str(t.get("creation_txid") or "") == txid:
                return {"kind": "token", "id": _int(t.get("id")), "name": str(t.get("name") or ""),
                        "creator": str(t.get("issuer") or ""), "txid": txid, "time": 0}
        for c in all_collections():
            if str(c.get("cover") or "").split("i")[0] == txid:
                return {"kind": "collection", "name": str(c.get("name") or ""), "creator": str(c.get("creator") or ""),
                        "count": _int(c.get("count")), "txid": txid, "cover_type": c.get("covertype"), "time": 0}
        try:
            got = v1.cmd("apps.card", chain=chain_key("test"), txid=txid) or {}
        except V1Error:
            got = {}
        card = got.get("card") or {}
        if card:
            return {"kind": "game", "name": str(card.get("name") or ""), "creator": str(got.get("creator") or ""),
                    "id": str(got.get("opens") or card.get("opens") or txid)}
        return None

    @app.get("/launches/{txid}", response_class=HTMLResponse)
    def launch_thread(request: Request, txid: str):
        """One launch's whole discussion. Anyone can read it; an account signs its comments in its tab."""
        txid = (txid or "").lower()
        launch = launch_of(txid) if TXID.fullmatch(txid) else None
        if launch is None:
            raise HTTPException(404, "no such launch")
        mine = me_of(request)
        account = app.state.signed_in(request)
        # NEEDS-V1: the replies and likes aimed at the launch's transaction.
        return render(request, "launch_thread.html", chain=ctx_chain(), launch=launch, tree={}, root=txid, count=0,
                      mine=mine, public=True, when=L._local_time, acts=account is not None and bool(mine.get("tag")))

    # ================================================================ mintpads
    def mintpad_look(seller: str, name: str) -> dict:
        said = st.setting(f"mintpad:{seller}:{name}", {}) or {}
        look = said.get("template") if isinstance(said, dict) else None
        return {"template": look if look in MINTPAD_TEMPLATES else "spotlight",
                "blurb": str((said or {}).get("blurb") or "")[:300] if isinstance(said, dict) else ""}

    def mintpad_inscription(creator: str, name: str) -> str:
        """The newest HTML inscription by the seller whose JSON says it is this collection's mintpad."""
        try:
            code, rows = v1.page("inscriptions", {"creator": creator, "limit": 200})
        except V1Error:
            return ""
        for d in rows if code == 200 and isinstance(rows, list) else []:
            j = d.get("json") if isinstance(d.get("json"), dict) else {}
            pad = j.get("mintpad") if isinstance(j.get("mintpad"), dict) else {}
            if str(d.get("contenttype") or "").startswith("text/html") and pad.get("creator") == creator \
                    and pad.get("collection") == name:
                return str(d.get("id") or "").split("i")[0]
        return ""

    @app.get("/mintpad/{creator}/{name}", response_class=HTMLResponse)
    def mintpad_page(request: Request, creator: str, name: str):
        """A collection's mintpad: how many are left, the price, and Mint (read only here)."""
        tag = creator[1:] if creator.startswith("@") else creator
        if len(tag) < 26 or creator.startswith("@"):
            found = address_of_tag(tag)
            if found:
                creator = found
        summary, _, _ = collection_summary(creator, name)
        if summary is None:
            return RedirectResponse("/exchange?tab=mintpads", status_code=303)
        _, art = collection_summary(creator, name, limit=60)[:2]
        art = [r for r in art if r["content_type"].startswith("image/")]
        account, here = account_here(request)
        is_seller = bool(here) and here == creator
        unlisted = 0
        if is_seller:
            # Every piece the seller still holds is unlisted: no listing book here (NEEDS-V1).
            _, items, _ = collection_summary(creator, name, limit=500)
            unlisted = sum(1 for r in items if r["owner"] == creator)
        return render(request, "mintpad_view.html", chain=ctx_chain(), summary=summary, look=mintpad_look(creator, name),
                      art=art, unlisted=unlisted, is_seller=is_seller, pad_txid=mintpad_inscription(creator, name),
                      unannounced=0, seller=creator, left=0, prices=[], signed_in=account is not None)

    @app.get("/r/mintpad/{creator}/{name}")
    def r_mintpad(creator: str, name: str):
        """What a collection's mintpad has left. NEEDS-V1: the listing book, so nothing is left here."""
        return contentlib._json(dict(EMPTY_PAD))
