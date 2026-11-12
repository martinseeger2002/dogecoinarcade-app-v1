"""The Exchange: the arcade ledger's token book on the home chain (Pepecoin testnet), its markets, trades and charts,
read from Arcade V1.0's market API, and the account's order-book actions built as offers for the browser to sign --
the same payloads the original writes (arcade/payload.py MetaDEx*, arcade/inscriptions.py Offer/Swap), funded from
the account's own coins (wallet.CoinDB) and never signed here.

    GET  /exchange                         the page (tabs; Tokens drawn from V1.0's markets)
    GET  /exchange/pair/{property_id}      one token against the coin: book, chart, form
    GET  /exchange/pairs/{base}/{quote}    one token against another
    GET  /exchange/history                 the reader's own trades
    GET  /exchange/trade/{txid}            one trade: who sold, who bought
    GET  /exchange/token20/search          DRC-20 / PRC-20 ticks as typed (V1.0's explorer.tokens)
    GET  /exchange/pairs                   the "open a pair" box -> the pair's page
    GET  /exchange/sell/{key}              the price form of one piece
    GET  /listings                         the legs this site holds signatures for (site-local book)
    POST /account/order, /account/order/cancel, /account/order/take, /account/order/list
    POST /account/pair-order, /account/pair-order/cancel
    POST /account/take                     which order a take would fill, and whose key to ask (a read)
    POST /account/offer                    an offer on somebody's piece (one OP_RETURN)
    POST /account/offer/decline, /account/offer/withdraw, /account/offer/bid, /account/offer/bid-build
    POST /account/standing, /standing/lots, /standing/sign, /standing/cancel; GET /account/standing/fillable
    GET  /account/listed; POST /account/list, /account/list/sign, /account/list/cancel, /account/buy, /account/buy/sign

Site-local state (st.home, the original's files and modules): standing.sqlite (arcade/standing.py), bids.sqlite
(arcade/bids.py), listings.sqlite (arcade/listings.py), all filled by this site's own requests only.

NEEDS-V1:
- /r/market/markets took over 150 s on a small node machine (0.3 s on a bigger one, 2026-11-11): the Tokens tab is drawn from a copy
  kept in the background (markets and each market's trades, two minutes) and is empty until the first read finishes.
- `reserved` of a book order: /r/book gives the remainder (`tokens`), which the engine keeps equal to the reserve.
- One trade by txid (the original's take_trade / pair_trade rows): read here out of the cached markets' trades; a
  coin-market take's taker is null in /r/market/trades, so it is matched to the reader's own tx.activity.
- One-transaction swaps (inscriptions.Swap, index.trades) and offers on pieces (index.offers_on/pending_offers):
  no V1.0 read, so the NFT market, the Offers tab and /account/offer/decline have nothing to answer with.
- The listing book shared over the mesh (book_relay): listings here are this site's own.
- Lock-script swaps (/exchange/swap/*, /account/trade/*), the custodial cross-chain book (/exchange/x/*) and
  delegated orders are not here (the first: remaining; the others hold a user's key or coins server-side).
- Node-facilitated trades (the `node_trades` setting, off by default as in the original): listings and away buy
  orders follow it; pre-signed offers (/account/offer/bid*) and away fills (the filler thread) are not ported, so
  those answer the original's refusal whatever the setting says.
"""
from __future__ import annotations

import re
import threading
import time
import urllib.parse
from fractions import Fraction
from typing import Any

from fastapi import Body, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from arcade import bids as bidslib
from arcade import charts as chartlib
from arcade import fees
from arcade import funding as fundinglib
from arcade import inscriptions as inscriptionlib
from arcade import listings as listingslib
from arcade import mintpad as mintpadlib
from arcade import payload as P
from arcade import standing as standinglib
from arcade import swap as swaplib
from arcade import tokens as tokenlib
from arcade import txbuild
from arcade.ledger import AmountError, format_amount, parse_amount
from arcade.script import b58check_decode, hash160

from . import legacy as L
from .actions import class_c_or_b
from .v1 import V1Error
from .wallet import CoinDB

COIN = 100_000_000
TXID = re.compile(r"[0-9a-f]{64}")
#: The original's exchange tabs (app.py EXCHANGE_TABS less the ord tabs this site does not draw).
EXCHANGE_TABS = ("offers", "mintpads", "tokens", "market", "llm")
PAIR_SHOWS = ("mix", "testnet", "mainnet", "dogecoin", "litecoin", "cross")
OUTPUTS_FOR_A_SWAP = 2
LOT_FEE = fees.DUST_LIMIT
LOT_CHANGE = fees.DUST_LIMIT
SMALLEST_TX = 60
NODE_TRADES_OFF = ("This arcade trades only while both people are here, each signing in their own tab: it does not "
                   "hold a signature to finish a trade for somebody who has gone. The operator can turn "
                   "node-facilitated trades on in the admin panel.")


def _plain(sats: float) -> str:
    return f"{sats / COIN:,.8f}".rstrip("0").rstrip(".")


def _coins_each(price: Any, divisible: bool) -> float:
    """The book's ratio (coins per token unit) as coins per whole token (the original's _coins_each)."""
    return float(price) if divisible else float(price) / COIN


def _said_int(said: dict, field: str, label: str, default: int = 0) -> int:
    value = said.get(field)
    if value is None or value == "":
        return default
    if isinstance(value, (list, dict)):
        raise ValueError(f"{label} has to be a number")
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{label} has to be a whole number") from None


def _said_list(said: dict, field: str, label: str) -> list:
    value = said.get(field)
    if not value:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{label} has to be a list")
    return value


def _said_hex(said: dict, field: str, label: str, sizes: tuple = (), least: int = 0) -> str:
    value = said.get(field) or ""
    if not isinstance(value, str):
        raise ValueError(f"{label} has to be hex")
    try:
        got = len(bytes.fromhex(value))
    except ValueError:
        raise ValueError(f"{label} has to be hex") from None
    if got and sizes and got not in sizes:
        raise ValueError(f"{label} has to be {' or '.join(str(n) for n in sizes)} bytes of hex")
    if got < least:
        raise ValueError(f"{label} is too short to be one" if got else f"name {label}")
    return value


def _key(value: str):
    """content._key: a txid by its length, else a number, else as typed (an inscription id <txid>i<n> -> txid)."""
    text = str(value).strip()
    if len(text) == 64:
        return text.lower()
    m = re.fullmatch(r"([0-9a-fA-F]{64})i0", text)
    if m:
        return m[1].lower()
    if text.isdigit():
        return int(text)
    return text.lower()


class _Rpc:
    """What arcade/listings.py asks of a chain's daemon, answered by V1.0's Core-style RPC."""

    def __init__(self, v1, network: str):
        self.v1, self.network = v1, network

    def call(self, method: str, *params):
        try:
            return self.v1.core(self.network, method, *params)
        except V1Error as exc:
            raise RuntimeError(str(exc)) from None


class Index:
    """The few questions the original's TokenIndex answered for these routes, asked of V1.0 (home chain)."""

    def __init__(self, v1, chain):
        self.v1, self.chain = v1, chain
        self._props: dict[int, dict | None] = {}
        self._books: dict[int, dict] = {}
        self._pieces: dict = {}
        self._bal: dict[str, list] = {}

    # ---- tokens
    def property(self, pid) -> dict | None:
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            return None
        if pid not in self._props:
            got = None
            try:
                code, p = self.v1.page(f"token/{pid}")
                if code == 200 and p:
                    got = {"property_id": pid, "propertyid": pid, "name": p.get("name") or f"Token {pid}",
                           "divisible": bool(p.get("divisible")), "issuer": p.get("issuer"),
                           "managed": 1 if p.get("managed") else 0, "data": p.get("data") or "",
                           "url": p.get("url") or "", "category": p.get("category") or "",
                           "subcategory": p.get("subcategory") or ""}
            except V1Error:
                got = None
            self._props[pid] = got
        return self._props[pid]

    def balances_of(self, address: str) -> list:
        if address not in self._bal:
            try:
                code, rows = self.v1.page(f"balances/{address}", {"pending": 1})
                self._bal[address] = rows if code == 200 and isinstance(rows, list) else []
            except V1Error:
                self._bal[address] = []
        return self._bal[address]

    def balance(self, address: str, pid: int) -> int:
        row = next((r for r in self.balances_of(address) if r.get("propertyid") == int(pid)), {})
        return int(row.get("units") or 0)

    def pending_out(self, address: str, pid: int) -> int:
        row = next((r for r in self.balances_of(address) if r.get("propertyid") == int(pid)), {})
        return int(row.get("leaving") or 0)

    # ---- inscriptions
    def inscription(self, key) -> dict | None:
        if key in self._pieces:
            return self._pieces[key]
        row = None
        try:
            txid = key
            if isinstance(key, int):
                got = self.v1.cmd("explorer.ledger_inscription", chain=self.chain.key, id=str(key)) or {}
                txid = str(got.get("txid") or got.get("id") or "").split("i")[0]
            if txid and TXID.fullmatch(str(txid)):
                code, x = self.v1.page(f"inscription/{txid}")
                if code == 200 and x:
                    row = {"txid": str(x.get("id") or txid), "number": x.get("number"), "owner": x.get("owner"),
                           "creator": x.get("creator"), "json": x.get("rawjson") or "",
                           "collection": x.get("collection"), "edition": x.get("edition"),
                           "content_type": x.get("contenttype") or "", "held": bool(x.get("held")),
                           "block_height": x.get("block")}
        except V1Error:
            row = None
        self._pieces[key] = row
        return row

    # ---- the book (V1.0's /r/market/book, coin market)
    def book(self, pid: int) -> dict:
        pid = int(pid)
        if pid not in self._books:
            asks, bids = [], []
            try:
                code, got = self.v1.page("market/book", {"base": pid, "quote": 0, "depth": 500})
                res = (got or {}).get("result") or {} if code == 200 else {}
            except V1Error:
                res = {}
            for side, out in (("asks", asks), ("bids", bids)):
                for r in res.get(side) or []:
                    tokens, coins = int(r.get("base") or 0), int(r.get("quote") or 0)
                    ask = side == "asks"
                    out.append({"txid": r.get("order"), "address": r.get("maker"), "tokens": tokens, "coins": coins,
                                "price": Fraction(coins, tokens) if tokens else Fraction(0),
                                "pending": bool(r.get("pending")), "block_height": int(r.get("height") or 0),
                                "position": 0, "taking": int(r.get("taking") or 0),
                                "sale_property": pid if ask else 0, "want_property": 0 if ask else pid,
                                "sale_amount": tokens if ask else coins, "want_amount": coins if ask else tokens,
                                "reserved": tokens if ask and not r.get("pending") else 0})
            self._books[pid] = {"asks": asks, "bids": bids}
        return {"asks": [dict(r) for r in self._books[pid]["asks"]],
                "bids": [dict(r) for r in self._books[pid]["bids"]]}

    def order(self, txid: str) -> dict | None:
        """One order on any coin book, by txid (the original's index.order)."""
        txid = str(txid or "").strip().lower()
        if not TXID.fullmatch(txid):
            return None
        for pid in list(self._books):
            for r in self._books[pid]["asks"] + self._books[pid]["bids"]:
                if r["txid"] == txid:
                    return dict(r)
        return None

    def pending_takes(self, pid: int) -> dict[str, int]:
        b = self.book(pid)
        return {r["txid"]: r["taking"] for r in b["asks"] + b["bids"] if r["taking"]}

    def pair_book(self, base: int, quote: int) -> dict:
        try:
            code, got = self.v1.page("market/book", {"base": base, "quote": quote, "depth": 500})
            res = (got or {}).get("result") or {} if code == 200 else {}
        except V1Error:
            res = {}
        out = {"asks": [], "bids": []}
        for side in ("asks", "bids"):
            for r in res.get(side) or []:
                b, q = int(r.get("base") or 0), int(r.get("quote") or 0)
                out[side].append({"txid": r.get("order"), "address": r.get("maker"), "base": b, "quote": q,
                                  "price": Fraction(q, b) if b else Fraction(0), "pending": bool(r.get("pending")),
                                  "crosses": bool(r.get("crosses")), "block_height": int(r.get("height") or 0)})
        return out

    def trades(self, base: int, quote: int, limit: int = 1000) -> list[dict]:
        try:
            code, got = self.v1.page("market/trades", {"base": base, "quote": quote, "limit": limit})
            return list((got or {}).get("result") or []) if code == 200 else []
        except V1Error:
            return []


def register(app, st, chains):
    v1 = st.v1
    offers, quota = app.state.offers, app.state.quota
    standing = standinglib.Standing(st.home / "standing.sqlite")
    bids = bidslib.Bids(st.home / "bids.sqlite")
    listings = listingslib.Listings(st.home / "listings.sqlite")
    kept: dict = {}
    lanes: set[str] = set()
    lanes_lock = threading.Lock()

    def render(request, template, **ctx):
        return app.state.render(request, template, **ctx)

    def node_trades() -> bool:
        return bool(st.setting("node_trades", False))

    def node_trades_refused():
        return JSONResponse({"detail": NODE_TRADES_OFF, "node_trades": False}, status_code=403)

    def home():
        return chains.account_chain()

    def chain_asked(said: dict):
        net = str(said.get("chain") or "test").strip() or "test"
        one = chains.by_network(net)
        if one is None:
            raise ValueError(f"this arcade keeps no {net} wallet")
        if one.network != home().network:
            # V1.0 keeps the arcade ledger's book on its home chain only (marketsvc.rs).
            raise ValueError(f"the token book is on {home().label}; {one.label} has none on this arcade yet")
        return one

    def coin_pubkey(pubkey: str, chain) -> bytes:
        try:
            return bytes.fromhex(str(st.setting(chains.coinkey_key(pubkey, chain), "") or ""))
        except ValueError:
            return b""

    def no_address(chain):
        return JSONResponse({"detail": f"this account has no {chain.label} address yet"}, status_code=400)

    def build(chain, address: str, outputs: list, what: str):
        return fundinglib.build(CoinDB(v1, chain), chain.params, address, outputs, rate=fees.MIN_FEE_PER_KB,
                                what=what)

    def viewer_of(request) -> tuple[str, Any]:
        account = app.state.signed_in(request)
        return ("account", account) if account is not None else ("nobody", None)

    def tags_for(addresses) -> dict[str, str]:
        out = {}
        for a in {x for x in addresses if x}:
            hit = kept.get(("tag", a))
            if hit and time.time() - hit[0] < 300:
                out[a] = hit[1]
                continue
            try:
                tag = str((v1.page(f"address/{a}")[1] or {}).get("tag") or "")
            except V1Error:
                tag = ""
            kept[("tag", a)] = (time.time(), tag)
            if tag:
                out[a] = tag
        return {a: t for a, t in out.items() if t}

    def key_at(address: str) -> bytes:
        """The messaging key the wallet at this address has announced (the original's _key_at; V1.0's msg.who)."""
        try:
            key = str((v1.cmd("msg.who", name=address) or {}).get("key") or "")
        except V1Error:
            key = ""
        if len(key) != 64:
            raise swaplib.SwapError(f"{address} has not published a messaging key, so there is nobody to send an "
                                    "offer to. Ask them to publish one from their address book.")
        return bytes.fromhex(key)

    def faces_for(index: Index, props) -> dict[int, dict]:
        out = {}
        for prop in props:
            face = tokenlib.details(prop)
            if face.get("icon"):
                row = index.inscription(_key(face["icon"]))
                if not (row and row["held"] and str(row["content_type"] or "").startswith("image/")):
                    face["icon"] = ""
            out[prop["property_id"]] = face
        return out

    # ------------------------------------------------------------------ the markets, kept in the background
    def market_snapshot() -> dict:
        """{markets: [...], trades: {(base, quote): [...]}, at}: V1.0's markets and each one's trades."""
        code, got = v1.page("market/markets", timeout=600)
        markets = list((got or {}).get("result") or []) if code == 200 else []
        trades = {}
        for m in markets:
            b, q = m.get("base") or {}, m.get("quote") or {}
            if b.get("network") != home().key or q.get("network") != home().key:
                continue
            bp, qp = int(b.get("property_id") or 0), int(q.get("property_id") or 0)
            try:
                code, t = v1.page("market/trades", {"base": bp, "quote": qp, "limit": 1000}, timeout=120)
                trades[(bp, qp)] = list((t or {}).get("result") or []) if code == 200 else []
            except V1Error:
                trades[(bp, qp)] = []
        return {"markets": markets, "trades": trades, "at": time.time()}

    snap_lock = threading.Lock()
    snap: dict = {"value": None, "running": False, "error": ""}

    def markets_now(wait: float = 3.0) -> dict:
        """The newest snapshot; a fresh read started when it is over two minutes old (the caller waits `wait` s)."""
        with snap_lock:
            value = snap["value"]
            stale = value is None or time.time() - value["at"] > 120
            start = stale and not snap["running"]
            if start:
                snap["running"] = True
        if start:
            def run():
                try:
                    got = market_snapshot()
                    with snap_lock:
                        snap["value"], snap["error"] = got, ""
                except Exception as exc:                    # noqa: BLE001 -- said on the page, asked again later
                    with snap_lock:
                        snap["error"] = str(exc)
                finally:
                    with snap_lock:
                        snap["running"] = False
            t = threading.Thread(target=run, name="exchange-markets", daemon=True)
            t.start()
            if value is None:
                t.join(wait)
        with snap_lock:
            return snap["value"] or {"markets": [], "trades": {}, "at": 0}

    app.state.exchange_markets = markets_now

    def points_of(trades: list[dict], bdiv: bool, qdiv: bool) -> list[dict]:
        return chartlib.pair_prices([{"when": t.get("at") or 0, "height": t.get("height") or 0, "base": int(t["base"]),
                                      "quote": int(t["quote"]), "txid": t.get("txid")} for t in trades
                                     if t.get("base") and t.get("quote") is not None], bdiv, qdiv)

    def spark(points: list) -> list[float]:
        rows = sorted(points, key=lambda p: (p.get("when") or 0, p.get("height") or 0))[-24:]
        prices = [float(p["price"]) for p in rows if p.get("price")]
        return prices if len(prices) >= 2 else []

    def pair_rows(show: str) -> list[dict]:
        """The Tokens tab's rows (the original's _pairs over every chain), from V1.0's markets."""
        if show not in ("mix", "testnet"):
            return []
        chain = home()
        snapv = markets_now()
        index = Index(v1, chain)
        rows = []
        for m in snapv["markets"]:
            b, q = m.get("base") or {}, m.get("quote") or {}
            if b.get("network") != chain.key or q.get("network") != chain.key or b.get("kind") not in ("token", "coin"):
                continue
            bp, qp = int(b.get("property_id") or 0), int(q.get("property_id") or 0)
            if bp == 0:                                     # a coin side is always the quote here
                continue
            bprop = index.property(bp) or {"property_id": bp, "name": b.get("name") or f"Token {bp}",
                                            "divisible": bool(b.get("divisible"))}
            qdiv = True if qp == 0 else bool(q.get("divisible"))
            points = points_of(snapv["trades"].get((bp, qp), []), bool(bprop["divisible"]), qdiv)
            stats = chartlib.day(points)
            change, new = chartlib.table_move(points, stats["change"])
            face = faces_for(index, [bprop])[bp]
            orders = int(m.get("asks") or 0) + int(m.get("bids") or 0)
            row = {"property_id": bp, "name": bprop["name"], "divisible": bprop["divisible"],
                   "managed": bprop.get("managed", 0), "all_trades": len(points), "orders": orders,
                   "icon": face.get("icon", ""), "about": face.get("about", ""), "last": stats["last"],
                   "change": change, "new": new, "high": stats["high"], "low": stats["low"],
                   "trades": stats["trades"], "volume": stats["volume"], "coins": stats["coins"],
                   "spark": spark(points), "ask": None, "bid": None, "net": "testnet", "mainnet": False,
                   "chain_net": chain.network, "switch": ""}
            if qp == 0:
                row.update(href=f"/exchange/pair/{bp}", quote_label=None, face_quote={"coin": chain.network})
            else:
                row.update(href=f"/exchange/pairs/{bp}/{qp}", quote_label=q.get("name") or f"Token {qp}",
                           face_quote={"net": chain.network, "icon": "", "letter": str(q.get("name") or "?")[:1].upper()})
            row["face_base"] = {"net": chain.network, "icon": face.get("icon", ""),
                                "letter": (str(bprop["name"]) or "?")[:1].upper()}
            rows.append(row)
        rows.sort(key=lambda p: (-(p["trades"] or 0), -p["all_trades"], -p["orders"], str(p["name"]).lower()))
        return rows

    # ------------------------------------------------------------------ pages
    @app.get("/exchange", response_class=HTMLResponse)
    def exchange(request: Request, tab: str = "llm", sort: str = "popular", show: str = "mix", count: int = 0):
        tab = tab if tab in EXCHANGE_TABS else "tokens"
        chain = home()
        kind, account = viewer_of(request)
        here = chains.account_address(account.pubkey, chain) if account else ""
        data: dict[str, Any] = {
            "tab": tab, "chain": chain, "node": {}, "other_chains": [], "shops": [], "node_error": None,
            "owned": {here} if here else set(), "offers_in": [], "offers_out": [], "tags": {}, "offer_history": [],
            "token_bids": [], "viewer": "account", "mintpads": [], "account_pads": [], "token_pads": [],
            "sort": "new" if sort == "new" else "popular", "pads": [], "pad_frames": {}, "market": [], "tokens": [],
            "pairs": [], "collections": [], "popular": [], "sales": [], "listings": [], "doginals": [],
            "doge_ready": False, "doge_version": 30, "x_in": [], "x_out": [], "ord_rows": [],
            "show": show if show in PAIR_SHOWS else "mix", "trade_orders": [], "my_trade_orders": [],
            "my_trades": [], "can_trade_orders": False, "can_list_orders": False, "coin_pairs": [],
            "held_tokens": [], "pair_viewer": "account" if account else "nobody", "held_nfts": [],
            "xchain_open": False, "held_net": chain.network, "pair_choices": [],
        }
        if tab == "tokens":
            data["pairs"] = pair_rows(data["show"])
            with snap_lock:
                if snap["value"] is None:
                    data["node_error"] = ("the markets are still being read from this arcade's ledger; "
                                          "this list fills in on a reload" + (f" ({snap['error']})" if snap["error"] else ""))
            index = Index(v1, chain)
            if here:
                held = []
                for r in index.balances_of(here):
                    prop = index.property(r.get("propertyid")) or {}
                    if int(r.get("units") or 0) > 0 and prop:
                        held.append({"property_id": prop["property_id"], "name": prop["name"],
                                     "managed": prop.get("managed", 0), "divisible": prop["divisible"]})
                data["held_tokens"] = sorted(held, key=lambda t: str(t["name"]).lower())
                try:
                    code, rows = v1.page(f"inscriptions/{here}", {"limit": 50})
                    data["held_nfts"] = [{"txid": r.get("id"), "number": r.get("number")}
                                         for r in (rows or [])] if code == 200 else []
                except V1Error:
                    pass
            data["pair_choices"] = pair_choices(chain, here, data["held_tokens"])
        if tab == "offers" and account is not None and here:
            # Buy orders on the tokens this account issued (the chain's bids and this site's standing ones).
            index = Index(v1, chain)
            declined = set(st.setting(f"declined_bids:{account.pubkey}", []) or [])
            try:
                for prop_row in token_list():
                    if prop_row.get("issuer") != here:
                        continue
                    pid = int(prop_row["id"])
                    prop = index.property(pid)
                    if prop is None:
                        continue
                    wants = [(b["price"], b["tokens"], b["address"], "", b["txid"]) for b in index.book(pid)["bids"]
                             if b.get("tokens")]
                    wants += [(Fraction(s["coins"], s["units"]), s["left_units"], s["buyer"],
                               "away" if s["away"] else "back", f"standing-{s['id']}")
                              for s in standing.open_on(chain.network, pid)]
                    for price, units, buyer, mode, key in sorted(wants, key=lambda w: -w[0]):
                        if buyer == here or key in declined:
                            continue
                        data["token_bids"].append({
                            "key": key, "property_id": pid, "name": prop["name"],
                            "price": f"{_coins_each(price, prop['divisible']):.8f}".rstrip("0").rstrip("."),
                            "amount": format_amount(units, prop["divisible"]), "buyer": buyer, "mode": mode})
                data["tags"].update(tags_for([b["buyer"] for b in data["token_bids"]]))
            except V1Error as exc:
                data["node_error"] = str(exc)
        if count:
            return JSONResponse({"waiting": len(data["offers_in"])}, headers={"Cache-Control": "no-store"})
        mine_tag = tags_for([here]).get(here, "") if here else ""
        return render(request, "exchange.html", when=lambda ts=None: L._local_time(time.time() if ts is None else ts,
                                                                                  "full"),
                      mine_tag=mine_tag, **data)

    def token_list() -> list[dict]:
        hit = kept.get("tokens")
        if hit and time.time() - hit[0] < 300:
            return hit[1]
        out, offset = [], 0
        while True:
            got = v1.cmd("explorer.ledger_tokens", chain=home().key, offset=offset, limit=500) or {}
            items = got.get("items") or []
            out += items
            if len(items) < 500:
                break
            offset += 500
        kept["tokens"] = (time.time(), out)
        return out

    def pair_choices(chain, here: str, held: list[dict]) -> list[dict]:
        """What the "Start a market" box offers (the original's _pair_choices, home chain only)."""
        coins = ""
        if here:
            try:
                sats = sum(c["value"] for c in CoinDB(v1, chain).unspent(here))
                coins = _plain(sats) if sats else ""
            except V1Error:
                pass
        out = [{"key": f"{chain.network}-coin", "kind": "coin", "net": chain.network, "name": chain.params.coin,
                "divisible": True, "label": getattr(chain.params, "title", "") or f"{chain.params.coin} · {chain.label}",
                "mine": bool(coins), "held": coins}]
        index = Index(v1, chain)
        for t in held:
            out.append({"key": f"{chain.network}-token-{t['property_id']}", "kind": "token", "net": chain.network,
                        "name": t["name"], "divisible": t["divisible"],
                        "label": f"{t['name']} (#{t['property_id']}) · {chain.params.coin}", "mine": True,
                        "held": format_amount(index.balance(here, t["property_id"]), t["divisible"])})
        try:
            shown = {t["property_id"] for t in held}
            for p in token_list()[:2000]:
                if int(p["id"]) in shown:
                    continue
                out.append({"key": f"{chain.network}-token-{p['id']}", "kind": "token", "net": chain.network,
                            "name": p.get("name") or f"Token {p['id']}", "divisible": p.get("type") == 2,
                            "label": f"{p.get('name') or 'Token'} (#{p['id']}) · {chain.params.coin}",
                            "mine": False, "held": ""})
        except V1Error:
            pass
        out.append({"key": f"other:{chain.network}", "kind": "other", "net": chain.network, "name": "",
                    "divisible": True, "label": f"Another token on {chain.params.coin} (by number)…",
                    "mine": False, "held": ""})
        return out

    def orders_of(index: Index, address: str, only: int = 0) -> list[dict]:
        """This address's standing coin-book orders (the original's index.orders_of), pool first."""
        out = []
        pids = [only] if only else [int(p["id"]) for p in token_list()]
        for pid in pids:
            try:
                code, got = v1.page(f"book/{pid}", {"address": address})
            except V1Error:
                continue
            if code != 200 or not got or not (got.get("asks") or got.get("bids")):
                continue
            b = index.book(pid)
            for r in b["asks"] + b["bids"]:
                if r["address"] == address:
                    out.append(r)
        out.sort(key=lambda r: (not r["pending"], -r["block_height"]))
        return out

    @app.get("/exchange/pair/{property_id}", response_class=HTMLResponse)
    def exchange_pair(request: Request, property_id: int):
        """One pair: its chart, its book, and the form that adds to the book."""
        chain = home()
        index = Index(v1, chain)
        prop = index.property(property_id)
        if prop is None:
            raise HTTPException(status_code=404, detail=f"there is no token {property_id} on this node. Every token "
                                                        "it knows is on the Exchange's Tokens tab.")
        points = points_of(index.trades(property_id, 0), prop["divisible"], True)
        asked = request.query_params.get("tf", "")
        frame = chartlib.timeframe(asked if asked in {t[0] for t in chartlib.TIMEFRAMES}
                                   else chartlib.pick_timeframe(points))
        book = index.book(property_id)
        for s in standing.open_on(chain.network, property_id):
            book["bids"].append({"txid": f"standing-{s['id']}", "standing": s["id"], "away": bool(s["away"]),
                                 "address": s["buyer"], "tokens": s["left_units"],
                                 "coins": -(-s["left_units"] * s["coins"] // s["units"]),
                                 "price": Fraction(s["coins"], s["units"]), "pending": False, "block_height": 0,
                                 "position": 0, "sale_property": 0, "want_property": property_id, "taking": 0})
        book["bids"].sort(key=lambda r: -r["price"])
        kind, account = viewer_of(request)
        owned, held, coins, account_address = set(), 0, 0.0, ""
        if account is not None:
            account_address = chains.account_address(account.pubkey, chain)
            if account_address:
                owned = {account_address}
                held = index.balance(account_address, property_id)
                try:
                    coins = sum(c["value"] for c in CoinDB(v1, chain).unspent(account_address)) / COIN
                except V1Error:
                    coins = 0.0

        def each(order):
            return _coins_each(order["price"], prop["divisible"])

        for side in ("asks", "bids"):
            for o in book[side]:
                o["free"] = max(0, o["tokens"] - o.get("taking", 0))
                o["free_shown"] = format_amount(o["free"], prop["divisible"])
                o["taking_shown"] = format_amount(o.get("taking", 0), prop["divisible"])
                o["mine"] = o["address"] in owned
                o["price_shown"] = f"{each(o):.8f}".rstrip("0").rstrip(".")
                o["tokens_shown"] = format_amount(o["tokens"], prop["divisible"])
                o["coins_shown"] = format_amount(o["coins"], True)
            biggest = max((o["tokens"] for o in book[side]), default=0)
            for o in book[side]:
                o["depth"] = round(100 * o["tokens"] / biggest, 1) if biggest else 0
        spread, crossed = None, False
        if book["asks"] and book["bids"]:
            spread = each(book["asks"][0]) - each(book["bids"][0])
            crossed = spread <= 0
        best_ask = each(book["asks"][0]) if crossed else None
        for o in book["bids"]:
            o["clears_now"] = crossed and each(o) >= best_ask
        face = faces_for(index, [prop])[property_id]
        mine = orders_of(index, account_address) if account_address else []
        for o in mine:
            o["askable"] = True
            if o["sale_property"]:
                try:
                    key_at(o["address"])
                except swaplib.SwapError:
                    o["askable"] = False
        day = chartlib.day(points)
        day.update(dict(zip(("change", "new"), chartlib.table_move(points, day["change"]))))
        height = None
        try:
            height = int((v1.cmd("status") or {}).get("height") or 0) or None
        except V1Error:
            pass
        return render(request, "pair.html", chain=chain, prop=prop, book=book, token_pads=[], face=face,
                      spread=spread, crossed=crossed, day=day, stats=chartlib.last_and_change(points),
                      slots=chartlib.candles(points, buckets=frame[2], span=frame[1]), tf=frame[0],
                      timeframes=[t[0] for t in chartlib.TIMEFRAMES],
                      recent=sorted(points, key=lambda p: -p["when"])[:12],
                      held=format_amount(held, prop["divisible"]), held_units=held, coins=coins, mine=mine,
                      viewer=kind, account_address=account_address, fills=[], fills_from=chain.params.fills_from,
                      fills_ready=chain.params.fills_from is not None, height=height,
                      tags=tags_for([o["address"] for o in book["asks"] + book["bids"]]))

    @app.get("/exchange/pairs/{base}/{quote}", response_class=HTMLResponse)
    def exchange_token_pair(request: Request, base: int, quote: int):
        """BASE/QUOTE: its book, what traded, the chart, and the order form."""
        chain = home()
        index = Index(v1, chain)
        base_prop, quote_prop = index.property(base), index.property(quote)
        if base_prop is None or quote_prop is None or base == quote:
            raise HTTPException(status_code=404, detail="a pair is two different tokens this node knows")
        book = index.pair_book(base, quote)
        done = index.trades(base, quote)
        points = points_of(done, base_prop["divisible"], quote_prop["divisible"])
        asked = request.query_params.get("tf", "")
        frame = chartlib.timeframe(asked if asked in {t[0] for t in chartlib.TIMEFRAMES}
                                   else chartlib.pick_timeframe(points))
        scale = (COIN if base_prop["divisible"] else 1) / (COIN if quote_prop["divisible"] else 1)
        for row in book["asks"] + book["bids"]:
            row["each"] = float(row["price"]) * scale
            row["base_shown"] = format_amount(row["base"], base_prop["divisible"])
            row["quote_shown"] = format_amount(row["quote"], quote_prop["divisible"])
        kind, account = viewer_of(request)
        account_address = chains.account_address(account.pubkey, chain) if account else ""
        for row in book["asks"] + book["bids"]:
            row["mine"] = bool(account_address) and row["address"] == account_address
        held_base = index.balance(account_address, base) if account_address else 0
        held_quote = index.balance(account_address, quote) if account_address else 0
        face = faces_for(index, [base_prop]).get(base, {})
        most = max([r["base"] for r in book["asks"] + book["bids"]] or [1]) or 1
        for row in book["asks"] + book["bids"]:
            row["depth"] = round(100 * row["base"] / most)
        since = getattr(chain.params, "token_pairs_from", None)
        trades = [dict(t, when=t.get("at"), base_shown=format_amount(int(t["base"]), base_prop["divisible"]),
                       quote_shown=format_amount(int(t["quote"]), quote_prop["divisible"]),
                       each=(int(t["quote"]) / int(t["base"])) * scale if t.get("base") else None)
                  for t in done[:20]]
        return render(request, "pair_tokens.html", chain=chain, base=base_prop, quote=quote_prop, face=face,
                      day=chartlib.day(points), book=book, trades=trades, stats=chartlib.last_and_change(points),
                      slots=chartlib.candles(points, buckets=frame[2], span=frame[1]), tf=frame[0],
                      timeframes=[t[0] for t in chartlib.TIMEFRAMES], viewer=kind, account_address=account_address,
                      held_base=format_amount(held_base, base_prop["divisible"]),
                      held_quote=format_amount(held_quote, quote_prop["divisible"]),
                      live=since is not None, since=since, height=None)

    def my_txids(address: str) -> set[str]:
        try:
            got = v1.cmd("tx.activity", addresses=[address], since=0, chain=home().key) or {}
        except V1Error:
            return set()
        mine = (got.get("addresses") or {}).get(address) or {}
        return {str(r.get("txid")) for r in (mine.get("confirmed") or []) + (mine.get("waiting") or []) if r.get("txid")}

    @app.get("/exchange/history", response_class=HTMLResponse)
    def exchange_history(request: Request):
        """The reader's own trades: takes and token-for-token matches it was a side of, each opening its own page."""
        kind, account = viewer_of(request)
        chain = home()
        rows: list[dict] = []
        me = chains.account_address(account.pubkey, chain) if account else ""
        if me:
            index = Index(v1, chain)
            sent = my_txids(me)
            coin = chain.params.coin

            def token(pid: int, amount: int) -> str:
                prop = index.property(pid) or {}
                return f"{format_amount(amount, bool(prop.get('divisible', True)))} {prop.get('name') or f'token #{pid}'}"
            for (bp, qp), trades in markets_now(wait=20.0)["trades"].items():
                for t in trades:
                    took = t.get("taker") == me or (t.get("taker") is None and t.get("txid") in sent)
                    made = t.get("maker") == me
                    if not (took or made):
                        continue
                    b, q = int(t.get("base") or 0), int(t.get("quote") or 0)
                    if qp == 0:
                        sold = (made and t.get("side") == "buy") or (took and t.get("side") == "sell")
                        rows.append({"txid": t["txid"], "net": chain.network, "chain": chain, "when": t.get("at"),
                                     "keys": {f"{chain.network}-token-{bp}", f"{chain.network}-coin"},
                                     "side": "sold" if sold else "bought", "what": token(bp, b),
                                     "for": f"{_plain(q)} {coin}", "who": t.get("taker") or "" if made else t.get("maker")})
                    else:
                        gave_base = (t.get("side") == "sell") == bool(took)
                        rows.append({"txid": t["txid"], "net": chain.network, "chain": chain, "when": t.get("at"),
                                     "keys": {f"{chain.network}-token-{bp}", f"{chain.network}-token-{qp}"},
                                     "side": "traded", "what": token(bp, b) if gave_base else token(qp, q),
                                     "for": token(qp, q) if gave_base else token(bp, b),
                                     "who": t.get("maker") if took else t.get("taker") or ""})
        pair, pair_label, back = None, "", "/exchange?tab=tokens"
        asked = (request.query_params.get("base", ""), request.query_params.get("quote", ""))
        if all(asked):
            pair = set(asked)
            rows = [r for r in rows if r.get("keys") == pair]
            m = [re.fullmatch(r"[a-z]+-(coin|token)(?:-(\d+))?", a) for a in asked]
            if all(m):
                index = Index(v1, chain)
                name = lambda x: chain.params.coin if x[1] == "coin" else (index.property(int(x[2])) or {}).get("name", "?")  # noqa: E731
                pair_label = f"{name(m[0])}/{name(m[1])}"
                if m[0][1] == "token" and m[1][1] == "coin":
                    back = f"/exchange/pair/{m[0][2]}"
                elif m[0][1] == m[1][1] == "token":
                    back = f"/exchange/pairs/{m[0][2]}/{m[1][2]}"
        rows.sort(key=lambda r: -(r["when"] or 0))
        return render(request, "trade_history.html", rows=rows[:300], viewer=kind,
                      pair=list(asked) if pair else None, pair_label=pair_label, back=back)

    @app.get("/exchange/trade/{txid}", response_class=HTMLResponse)
    def exchange_trade(request: Request, txid: str):
        """One trade, opened from a market's Recent trades: who sold, who bought."""
        txid = txid.strip().lower()
        if not TXID.fullmatch(txid):
            raise HTTPException(404, "a trade is named by its txid")
        chain = home()
        index = Index(v1, chain)
        coin = chain.params.coin
        fills: list[dict] = []
        height = None
        for (bp, qp), trades in markets_now(wait=20.0)["trades"].items():
            for t in trades:
                if t.get("txid") != txid:
                    continue
                height = t.get("height")
                b, q = int(t.get("base") or 0), int(t.get("quote") or 0)
                bprop = index.property(bp) or {}
                bt = f"{format_amount(b, bool(bprop.get('divisible', True)))} {bprop.get('name') or f'token #{bp}'}"
                if qp == 0:
                    fills.append({"seller": t.get("maker"), "buyer": t.get("taker") or "", "sold": bt,
                                  "paid": f"{_plain(q)} {coin}", "how": "Taken from the book"})
                else:
                    qprop = index.property(qp) or {}
                    qt = f"{format_amount(q, bool(qprop.get('divisible', True)))} {qprop.get('name') or f'token #{qp}'}"
                    buy = t.get("side") == "buy"
                    fills.append({"seller": t.get("maker") if buy else t.get("taker"),
                                  "buyer": t.get("taker") if buy else t.get("maker"), "sold": bt, "paid": qt,
                                  "how": "Matched by the chain: a crossing order met one standing on the book"})
        tx, inputs, confirmations, when = None, [], 0, 0
        try:
            tx = v1.core(chain.network, "getrawtransaction", txid, 1)
            for vin in tx.get("vin", [])[:20]:
                if not vin.get("txid"):
                    continue
                prev = v1.core(chain.network, "getrawtransaction", vin["txid"], 1)
                out = (prev.get("vout") or [])[int(vin["vout"])]
                spk = out.get("scriptPubKey") or {}
                inputs.append(spk.get("address") or (spk.get("addresses") or [""])[0])
            confirmations, when = int(tx.get("confirmations") or 0), int(tx.get("blocktime") or tx.get("time") or 0)
        except (V1Error, IndexError, ValueError, TypeError, AttributeError):
            pass
        for f in fills:
            if not f["buyer"]:
                f["buyer"] = next((a for a in inputs if a and a != f["seller"]), "")
        if not fills and tx is None:
            raise HTTPException(404, "this node does not know that trade")
        said = urllib.parse.urlsplit(request.headers.get("referer") or "")
        back = (said.path + (f"?{said.query}" if said.query else "")) if said.path.startswith("/exchange") \
            else "/exchange?tab=tokens"
        return render(request, "trade.html", txid=txid, chain=chain, fills=fills, inputs=inputs,
                      confirmations=confirmations, when=when, height=height, back=back)

    @app.get("/exchange/token20/search")
    def exchange_token20_search(q: str = ""):
        """DRC-20 / PRC-20 ticks starting with what was typed: a dozen at most (V1.0's explorer ticks)."""
        typed = str(q or "").strip().lower()[:16]
        out: list[dict] = []
        if typed:
            for net, proto in (("main", "PRC-20"), ("doge", "DRC-20")):
                if chains.by_network(net) is None:
                    continue
                try:
                    got = v1.cmd("explorer.tokens", chain=chains.by_network(net).key, prefix=typed, limit=12) or {}
                except V1Error:
                    continue
                for row in got.get("items") or got.get("tokens") or []:
                    tick = str(row.get("tick") or "")
                    if tick.lower().startswith(typed) and re.fullmatch(r"[a-z0-9]{1,16}", tick.lower()):
                        out.append({"value": f"{net}-token20-{tick.lower()}", "label": f"{tick} · {proto}",
                                    "net": net, "kind": "token20", "name": tick, "div": 0})
        return JSONResponse({"results": out[:12]}, headers={"Cache-Control": "public, max-age=60"})

    @app.get("/exchange/pairs")
    def exchange_pairs_open(request: Request, base: str = "", quote: str = "", token: str = ""):
        """The "Start a market" box: two sides on the home chain open its own market page; anything else is a
        lock-script swap market, which this site does not have yet."""
        chain = home()
        back = RedirectResponse("/exchange?tab=tokens", status_code=303)
        if str(base).startswith("other:"):
            number = str(token or "").strip().lstrip("#")
            base = f"{base[6:]}-token-{int(number)}" if number.isdigit() else "nothing"

        def leg(value: str):
            value = str(value or "").strip()
            if value == "coin":
                return (chain.network, "coin", 0)
            if value.isdigit():
                return (chain.network, "token", int(value))
            m = re.fullmatch(r"([a-z]+)-(coin|token)(?:-(\d+))?", value)
            if not m:
                return None
            return (m[1], m[2], int(m[3] or 0))
        b, q = leg(base), leg(quote)
        if b is None or q is None or b == q or (b[1] == q[1] == "coin"):
            return back
        if b[0] != chain.network or q[0] != chain.network:
            return back                                     # a cross-chain market: lock scripts, not here yet
        if b[1] == "coin":
            b, q = q, b
        if q[1] == "coin":
            return RedirectResponse(f"/exchange/pair/{b[2]}", status_code=303)
        return RedirectResponse(f"/exchange/pairs/{b[2]}/{q[2]}", status_code=303)

    @app.get("/exchange/sell/{key}", response_class=HTMLResponse)
    def sell_form(request: Request, key: str):
        """The form that puts a price on one piece."""
        chain = home()
        index = Index(v1, chain)
        row = index.inscription(_key(key))
        if row is None:
            return RedirectResponse("/me/wallet", status_code=303)
        data = L._fromjson(row.get("json")) or {}
        name = data.get("name") if isinstance(data, dict) else None
        tokens = [{"property_id": int(p["id"]), "name": p.get("name"), "divisible": p.get("type") == 2}
                  for p in token_list()]
        return render(request, "sell.html", chain=chain, node={}, row=row,
                      name=str(name) if name else f"Inscription #{row.get('number')}", ask=None, shop=None,
                      tokens=tokens, amount="", kind="coins", property_id="",
                      asks_ready=chain.params.asks_from is not None, asks_from=chain.params.asks_from, height=None)

    # ------------------------------------------------------------------ the token book, for an account
    def account_chain_address(request, said):
        account = app.state.signed_in_account(request)
        chain = chain_asked(said)
        return account, chain, chains.account_address(account.pubkey, chain)

    @app.post("/account/order")
    def account_order(request: Request, payload: Any = Body(None)):
        """Put this account's order on the token book (a MetaDExTrade, unwrapped). Nothing is broadcast here."""
        account = app.state.signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        try:
            chain = chain_asked(said)
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        address = chains.account_address(account.pubkey, chain)
        if not address:
            return no_address(chain)
        try:
            index = Index(v1, chain)
            side = str(said.get("side") or "")
            if side not in ("ask", "bid"):
                raise ValueError("an order is an ask or a bid, and says which")
            prop = index.property(_said_int(said, "property_id", "the token number"))
            if prop is None:
                raise tokenlib.TokenError(f"there is no token {said.get('property_id')}.")
            units = parse_amount(str(said.get("amount", "")), prop["divisible"])
            each = parse_amount(str(said.get("price", "")), True)
            if units <= 0 or each <= 0:
                raise tokenlib.TokenError("an amount and a price, both above zero.")
            raw = units * each
            if not prop["divisible"]:
                raw *= COIN
            if raw < COIN:
                raise tokenlib.TokenError("that comes to less than a satoshi in coins; raise the price or the amount.")
            if raw % COIN:
                raise tokenlib.TokenError(
                    "that price does not come out in whole satoshis. The chain takes two whole numbers and a price is "
                    "only their ratio, so rounded here it is a price on the book that was not typed; raise the price "
                    "or the amount until it divides even.")
            coins = raw // COIN
            shown = format_amount(units, prop["divisible"])
            if side == "ask":
                held = index.balance(address, prop["property_id"])
                resting = sum(o["tokens"] for o in index.book(prop["property_id"])["asks"]
                              if o["address"] == address and o.get("pending"))
                resting += index.pending_out(address, prop["property_id"])
                if held - resting < units:
                    raise tokenlib.TokenError(
                        f"this account holds {format_amount(held, prop['divisible'])} of {prop['name']}"
                        + (f", of which {format_amount(resting, prop['divisible'])} is already on this book waiting "
                           "for its block" if resting > 0 else "")
                        + f", not {shown}. An ask is held back by the engine when its block lands, and an order it "
                        "cannot hold back costs a fee to fail in its own block.")
                message = P.MetaDExTrade(property_id_for_sale=prop["property_id"], amount_for_sale=units,
                                         property_id_desired=0, amount_desired=coins)
            else:
                message = P.MetaDExTrade(property_id_for_sale=0, amount_for_sale=coins,
                                         property_id_desired=prop["property_id"], amount_desired=units)
            outputs = class_c_or_b(chain, address, message.encode(), coin_pubkey(account.pubkey, chain), wrap=False)
            what = f"{'sell' if side == 'ask' else 'buy'} {shown} {prop['name']} for {format_amount(coins, True)} coins"
            unsigned = build(chain, address, outputs, what)
            quota(account, "trade")
        except (tokenlib.TokenError, fundinglib.FundingError, AmountError, ValueError, V1Error) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        offer = offers.add(account.pubkey, chain.network, unsigned, what)
        return JSONResponse({"offer": offer.id, "what": what, "side": side, "property_id": prop["property_id"],
                             "name": prop["name"], "chain": chain.network, "amount": shown,
                             "price": format_amount(each, True), "total": format_amount(coins, True),
                             **unsigned.as_json()})

    def cancel_one_or_pair(index: Index, address: str, prop: dict, side: str, price: str, who: str) -> tuple:
        """The cancel to file for one side of one pair: one price (MetaDExCancelPrice), or all (MetaDExCancelPair)."""
        pid = int(prop["property_id"])
        if not str(price or "").strip():
            sale, want = (pid, 0) if side == "ask" else (0, pid)
            return (P.MetaDExCancelPair(property_id_for_sale=sale, property_id_desired=want),
                    f"cancel {who}'s {side}s for {prop['name']}", True)
        at = parse_amount(str(price), True)
        if at <= 0:
            raise tokenlib.TokenError(f"a price above zero -- or leave it empty to cancel every {side} of yours for "
                                      f"{prop['name']}.")
        mine = {}
        for row in orders_of(index, address, pid):
            ask = row["want_property"] == 0
            if ask != (side == "ask"):
                continue
            tokens = row["sale_amount"] if ask else row["want_amount"]
            if not tokens:
                continue
            at_row = Fraction(row["want_amount"] if ask else row["sale_amount"], tokens)
            mine.setdefault(at_row * COIN if prop["divisible"] else at_row, row)
        row = mine.get(Fraction(int(at)))
        if row is None:
            raise tokenlib.TokenError(
                f"nothing of yours stands at {price} for {prop['name']}, so there is no price to withdraw. Standing: "
                + (", ".join(f"{float(o / COIN):.8f}" for o in sorted(mine)) if mine else "nothing"))
        return (P.MetaDExCancelPrice(property_id_for_sale=row["sale_property"], amount_for_sale=row["sale_amount"],
                                     property_id_desired=row["want_property"], amount_desired=row["want_amount"]),
                f"cancel {who}'s {side}s for {prop['name']} at {float(Fraction(int(at)) / COIN):.8f} "
                "(one price, not the pair)", False)

    @app.post("/account/order/cancel")
    def account_order_cancel(request: Request, payload: Any = Body(None)):
        """Take this account's orders off one side of one pair, or one price."""
        account = app.state.signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        try:
            chain = chain_asked(said)
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        address = chains.account_address(account.pubkey, chain)
        if not address:
            return no_address(chain)
        try:
            index = Index(v1, chain)
            pid = _said_int(said, "property_id", "the token number")
            prop = index.property(pid)
            if prop is None:
                raise tokenlib.TokenError(f"there is no token {pid}.")
            side = str(said.get("side") or "")
            if side not in ("ask", "bid"):
                raise ValueError("an order is an ask or a bid, and says which")
            message, what, every = cancel_one_or_pair(index, address, prop, side, str(said.get("price") or ""),
                                                      "this account")
            outputs = class_c_or_b(chain, address, message.encode(), coin_pubkey(account.pubkey, chain), wrap=False)
            unsigned = build(chain, address, outputs, what)
            quota(account, "trade")
        except (tokenlib.TokenError, fundinglib.FundingError, AmountError, ValueError, V1Error) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        offer = offers.add(account.pubkey, chain.network, unsigned, what)
        return JSONResponse({"offer": offer.id, "what": what, "side": side, "property_id": pid, "name": prop["name"],
                             "chain": chain.network, "every": every, **unsigned.as_json()})

    def find_order(index: Index, txid: str, pid_hint: int = 0) -> dict | None:
        """An order on the coin book by txid: the hinted token's book first, else every token's (V1.0 has no
        order-by-txid read)."""
        if pid_hint:
            index.book(pid_hint)
            got = index.order(txid)
            if got:
                return got
        for p in token_list():
            try:
                code, b = v1.page(f"book/{int(p['id'])}")
            except V1Error:
                continue
            if code == 200 and any(r.get("order") == txid for r in (b or {}).get("asks", []) + (b or {}).get("bids", [])):
                index.book(int(p["id"]))
                return index.order(txid)
        return None

    @app.post("/account/order/take")
    def account_order_take(request: Request, payload: Any = Body(None)):
        """Buy from somebody's resting ask with their tab closed (type 29): the payment to the order's address
        and the take naming it, from this account's own coins. Nothing is broadcast here; the tab signs."""
        account = app.state.signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        try:
            chain = chain_asked(said)
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        address = chains.account_address(account.pubkey, chain)
        if not address:
            return no_address(chain)
        try:
            if chain.params.take_from is None:
                raise tokenlib.TokenError("this chain does not settle takes yet")
            index = Index(v1, chain)
            txid = str(said.get("order") or "").strip().lower()
            if not TXID.fullmatch(txid):
                raise ValueError("name the order by its transaction id")
            row = find_order(index, txid, _said_int(said, "property_id", "the token number"))
            if row is None or row.get("pending"):
                raise tokenlib.TokenError("that order is not on the book any more: filled, cancelled, or still "
                                          "waiting for its block. Refresh the page.")
            if row["want_property"] != 0 or not row["sale_property"]:
                raise tokenlib.TokenError("only an ask, tokens for coins, can be taken")
            if row["address"] == address:
                raise tokenlib.TokenError("that is your own order; cancel it instead of taking it")
            prop = index.property(row["sale_property"])
            units = (int(said["units"]) if str(said.get("units") or "").isdigit()
                     else parse_amount(str(said.get("amount", "")), prop["divisible"]))
            if units <= 0:
                raise tokenlib.TokenError("take an amount above zero")
            taking = row["taking"]
            free = row["reserved"] - taking
            if free <= 0:
                raise tokenlib.TokenError("somebody is buying all of this order right now. Wait for its block (a "
                                          "minute or two), so you never pay for tokens that are already gone.")
            if units > free:
                raise tokenlib.TokenError(
                    f"that order has {format_amount(free, prop['divisible'])} {prop['name']} left to buy"
                    + (f" ({format_amount(taking, prop['divisible'])} is being bought right now)" if taking else "")
                    + "; take that much or less")
            need = row["want_amount"] * units // row["sale_amount"]
            if need < fees.DUST_LIMIT:
                raise tokenlib.TokenError(f"that comes to {format_amount(need, True)} coins, under the "
                                          f"{format_amount(fees.DUST_LIMIT, True)} the network will carry to one "
                                          "address; take more of it")
            message = P.MetaDExTake(property_id=row["sale_property"], amount=units, order=bytes.fromhex(txid))
            filling = standing.get(str(said.get("standing") or ""))
            if filling and (filling["account"] != account.pubkey or filling["property_id"] != row["sale_property"]):
                filling = None
            outputs = [(need, txbuild.p2pkh_script(row["address"]))] + class_c_or_b(
                chain, address, message.encode(), coin_pubkey(account.pubkey, chain), wrap=False)
            shown = format_amount(units, prop["divisible"])
            what = f"buy {shown} {prop['name']} for {format_amount(need, True)} coins"
            unsigned = build(chain, address, outputs, what)
            quota(account, "trade")
        except (tokenlib.TokenError, fundinglib.FundingError, AmountError, ValueError, V1Error) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)

        def still_free(order=txid, want=units, token=prop, pid=row["sale_property"]):
            """Asked again at the broadcast: another buyer's take may have reached the pool since."""
            fresh = Index(v1, chain)
            now = next((r for r in fresh.book(pid)["asks"] if r["txid"] == order), None)
            left = (now["reserved"] - now["taking"]) if now else 0
            if want > left:
                raise ValueError(
                    "somebody else bought from this order while you were signing, and it has "
                    f"{format_amount(max(0, left), token['divisible'])} {token['name']} left. Nothing was sent and "
                    "nothing was spent; press Buy again for what is left.")

        def took(_txid: str, which=filling, n=units) -> None:
            if which:
                standing.took(which["id"], n)

        offer = offers.add(account.pubkey, chain.network, unsigned, what, check=still_free, done=took)
        return JSONResponse({"offer": offer.id, "what": what, "chain": chain.network, "name": prop["name"],
                             "amount": shown, "coins": format_amount(need, True), "maker": row["address"],
                             "maker_tag": tags_for([row["address"]]).get(row["address"], ""),
                             "left": format_amount(row["reserved"] - units, prop["divisible"]),
                             **unsigned.as_json()})

    @app.post("/account/market/build")
    def account_market_build(request: Request, payload: Any = Body(None)):
        """The transaction for one market question a page asked this account (apps.py, kind "market"), for the card
        to show and the browser to sign: place {give, get} is the on-chain order (a MetaDExTrade: give for get, a
        token for the coin, the coin for a token, or one token for another), cancel {order} withdraws one of this
        account's orders at its price, take {order, amount} buys from an ask (/account/order/take). Nothing is
        broadcast here."""
        account = app.state.signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        chain = home()
        address = chains.account_address(account.pubkey, chain)
        if not address:
            return no_address(chain)
        row = app.state.page_send(str(said.get("id") or "")) if hasattr(app.state, "page_send") else None
        if row is None or row.get("kind") != "market" or row["address"] != address or row["status"] != "pending":
            return JSONResponse({"detail": "no such question waiting"}, status_code=404)
        op, args = row["op"], row["args"]
        if op == "take":
            return account_order_take(request, {"order": args["order"], "units": str(args["amount"])})
        try:
            index = Index(v1, chain)

            def named(pid: int, units: int) -> str:
                if not pid:
                    return f"{format_amount(units, True)} {chain.params.coin}"
                prop = index.property(pid)
                if prop is None:
                    raise tokenlib.TokenError(f"there is no token {pid}.")
                return f"{format_amount(units, bool(prop['divisible']))} {prop['name']}"

            if op == "place":
                give, get = args["give"], args["get"]
                if give["asset"] and get["asset"] and getattr(chain.params, "token_pairs_from", None) is None:
                    raise tokenlib.TokenError(f"token-for-token pairs are not switched on on {chain.label} yet.")
                said_give, said_get = named(give["asset"], give["amount"]), named(get["asset"], get["amount"])
                if give["asset"]:
                    # What it sells is held back when its block lands: it must be free now (account_order's check).
                    held = index.balance(address, give["asset"]) - index.pending_out(address, give["asset"])
                    if not get["asset"]:
                        held -= sum(o["tokens"] for o in index.book(give["asset"])["asks"]
                                    if o["address"] == address and o.get("pending"))
                    if held < give["amount"]:
                        raise tokenlib.TokenError(f"this account has {named(give['asset'], max(0, held))} free, not "
                                                  f"{said_give}. An order holds back what it sells when its block "
                                                  "lands, and one it cannot hold back costs a fee to fail.")
                message = P.MetaDExTrade(property_id_for_sale=give["asset"], amount_for_sale=give["amount"],
                                         property_id_desired=get["asset"], amount_desired=get["amount"])
                what = f"put an order on the book: {said_give} for {said_get}"
            elif op == "cancel":
                # The order on one of the home chain's books (each market's book, as the page's own "mine" reads).
                mine = None
                for m in markets_now().get("markets") or []:
                    if mine is not None:
                        break
                    if (m.get("base") or {}).get("network") != chain.key or (m.get("venue") or "ledger") != "ledger":
                        continue
                    b, q = int((m.get("base") or {}).get("property_id") or 0), int((m.get("quote") or {}).get("property_id") or 0)
                    pb = index.pair_book(b, q)
                    for side in ("asks", "bids"):
                        for r in pb[side]:
                            if r["txid"] == args["order"] and r["address"] == address:
                                mine = (b, r["base"], q, r["quote"]) if side == "asks" else (q, r["quote"], b, r["base"])
                if mine is None:
                    raise tokenlib.TokenError("that order is not yours on the book any more: filled, cancelled, or "
                                              "still waiting for its block. Refresh the page.")
                message = P.MetaDExCancelPrice(property_id_for_sale=mine[0], amount_for_sale=mine[1],
                                               property_id_desired=mine[2], amount_desired=mine[3])
                what = (f"cancel your order of {named(mine[0], mine[1])} for {named(mine[2], mine[3])} "
                        "(every order of yours at that price on that book)")
            else:
                raise ValueError(f"{op} is not a market request")
            outputs = class_c_or_b(chain, address, message.encode(), coin_pubkey(account.pubkey, chain), wrap=False)
            unsigned = build(chain, address, outputs, what)
            quota(account, "trade")
        except (tokenlib.TokenError, fundinglib.FundingError, AmountError, ValueError, V1Error) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        offer = offers.add(account.pubkey, chain.network, unsigned, what)
        return JSONResponse({"offer": offer.id, "what": what, "chain": chain.network, **unsigned.as_json()})

    @app.post("/account/order/list")
    def account_order_list(request: Request, payload: Any = Body(None)):
        """What this account has standing on the book, and where it stands in line. A read."""
        account = app.state.signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        try:
            chain = chain_asked(said)
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        address = chains.account_address(account.pubkey, chain)
        if not address:
            return no_address(chain)
        index = Index(v1, chain)
        rows: list[dict] = []
        try:
            only = _said_int(said, "property_id", "the token number")
            coins_held = None
            for row in orders_of(index, address, only):
                ask = row["want_property"] == 0
                token = row["sale_property"] if ask else row["want_property"]
                prop = index.property(token) or {"name": f"token {token}", "divisible": True}
                tokens = row["sale_amount"] if ask else row["want_amount"]
                coins = row["want_amount"] if ask else row["sale_amount"]
                at = Fraction(coins, tokens) if tokens else Fraction(0)
                same = [r["txid"] for r in index.book(token)["asks" if ask else "bids"] if r["price"] == at]
                entry = {"order": row["txid"], "side": "ask" if ask else "bid", "property_id": token,
                         "name": prop["name"], "tokens": tokens,
                         "amount": format_amount(tokens, bool(prop["divisible"])), "coins": coins,
                         "total": format_amount(coins, True),
                         "price": f"{(float(at) if prop['divisible'] else float(at) / COIN):.8f}",
                         "pending": bool(row.get("pending")), "block": row["block_height"],
                         "ahead": same.index(row["txid"]) if row["txid"] in same else 0}
                if not ask:
                    if coins_held is None:
                        coins_held = sum(c["value"] for c in CoinDB(v1, chain).unspent(address))
                    entry.update(held=coins_held, held_amount=format_amount(coins_held, True),
                                 short=coins_held < coins)
                rows.append(entry)
        except (tokenlib.TokenError, AmountError, ValueError, V1Error) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        return JSONResponse({"chain": chain.network, "address": address, "count": len(rows), "orders": rows})

    @app.post("/account/take")
    def account_take(request: Request, payload: Any = Body(None)):
        """What taking a price off the book would ask for, and of whom: which order the queue chose (price, then
        time), what it prices to, and whose messaging key the question is sealed to. A read."""
        account = app.state.signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        try:
            chain = chain_asked(said)
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        address = chains.account_address(account.pubkey, chain)
        if not address:
            return no_address(chain)
        try:
            index = Index(v1, chain)
            clicked = find_order(index, str(said.get("order") or "").strip().lower(),
                                 _said_int(said, "property_id", "the token number"))
            if clicked is None:
                raise swaplib.SwapError("that order is not on this node's book -- it may have been cancelled or "
                                        "filled, or its block may not have arrived here")
            if clicked["want_property"] != 0:
                raise swaplib.SwapError("this fills an order that sells a token for coins. To fill a bid, the wallet "
                                        "holding the tokens has to offer them")
            prop = index.property(clicked["sale_property"])
            tokens = parse_amount(str(said.get("amount", "")), bool(prop["divisible"]))
            if not 0 < tokens <= clicked["sale_amount"]:
                raise swaplib.SwapError(
                    f"that order has {format_amount(clicked['sale_amount'], bool(prop['divisible']))} left")
            row = None
            want = Fraction(clicked["want_amount"], clicked["sale_amount"])
            for r in index.book(clicked["sale_property"])["asks"]:
                if r.get("pending"):
                    continue
                if Fraction(r["want_amount"], r["sale_amount"]) > want:
                    break
                if r["sale_amount"] < tokens:
                    continue
                try:
                    key_at(r["address"])
                except swaplib.SwapError:
                    continue
                row = r
                break
            row = row or clicked
            coins = -(-row["want_amount"] * tokens // row["sale_amount"])
            if row["address"] == address:
                raise swaplib.SwapError("that order is your own")
            held = CoinDB(v1, chain).unspent(address)
            if len(held) < OUTPUTS_FOR_A_SWAP:
                raise swaplib.SwapError(
                    f"this account has its coins in {len(held)} confirmed output{'' if len(held) == 1 else 's'}, and "
                    "taking a price needs two: one goes into the trade and one pays for the message that asks about "
                    "it. Split it first -- Wallet, Send, 1 coin to your own @name -- and take it again.")
            to = key_at(row["address"])
        except (swaplib.SwapError, tokenlib.TokenError, AmountError, ValueError, V1Error) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        return JSONResponse({"chain": chain.network, "order": row["txid"], "clicked": clicked["txid"],
                             "moved": row["txid"] != clicked["txid"], "property_id": row["sale_property"],
                             "name": prop["name"], "tokens": tokens, "coins": coins,
                             "amount": format_amount(tokens, bool(prop["divisible"])), "maker": row["address"],
                             "maker_tag": tags_for([row["address"]]).get(row["address"], ""), "to": to.hex(),
                             "price": f"{coins / COIN:.8f}", "swapv": swaplib.PROTOCOL, "buyer": address})

    # ------------------------------------------------------------------ token/token pairs, for an account
    def pair_message(index: Index, base_prop, quote_prop, side, amount, price, address):
        if side not in ("buy", "sell"):
            raise ValueError("an order buys or sells, and says which")
        units = parse_amount(str(amount or ""), base_prop["divisible"])
        each = parse_amount(str(price or ""), quote_prop["divisible"])
        if units <= 0 or each <= 0:
            raise tokenlib.TokenError("an amount and a price, both above zero.")
        raw = units * each
        scale = COIN if base_prop["divisible"] else 1
        if raw % scale:
            raise tokenlib.TokenError(f"that price does not come out in whole units of {quote_prop['name']}; change "
                                      "the price or the amount until it divides even.")
        total = raw // scale
        if total <= 0:
            raise tokenlib.TokenError("that comes to less than one unit of the price token.")
        sold, sold_amount = (base_prop, units) if side == "sell" else (quote_prop, total)
        pending_pair = sum(r["base"] if side == "sell" else r["quote"]
                           for r in index.pair_book(base_prop["property_id"], quote_prop["property_id"])
                           ["asks" if side == "sell" else "bids"] if r["address"] == address and r["pending"])
        held = (index.balance(address, sold["property_id"]) - index.pending_out(address, sold["property_id"])
                - pending_pair)
        if held < sold_amount:
            raise tokenlib.TokenError(
                f"{address} holds {format_amount(max(0, held), sold['divisible'])} {sold['name']}, not "
                f"{format_amount(sold_amount, sold['divisible'])}. An order holds what it sells back from the block "
                "it lands in.")
        if side == "sell":
            message = P.MetaDExTrade(property_id_for_sale=base_prop["property_id"], amount_for_sale=units,
                                     property_id_desired=quote_prop["property_id"], amount_desired=total)
        else:
            message = P.MetaDExTrade(property_id_for_sale=quote_prop["property_id"], amount_for_sale=total,
                                     property_id_desired=base_prop["property_id"], amount_desired=units)
        what = (f"{side} {format_amount(units, base_prop['divisible'])} {base_prop['name']} for "
                f"{format_amount(total, quote_prop['divisible'])} {quote_prop['name']}")
        return message, what

    def pair_props(index: Index, said: dict):
        if getattr(home().params, "token_pairs_from", None) is None:
            raise tokenlib.TokenError(f"token-for-token pairs are not switched on on {home().label} yet.")
        b = index.property(_said_int(said, "base", "the base token"))
        q = index.property(_said_int(said, "quote", "the quote token"))
        if b is None or q is None:
            raise tokenlib.TokenError("a pair is two tokens this chain has.")
        if b["property_id"] == q["property_id"]:
            raise tokenlib.TokenError("a pair is two different tokens.")
        return b, q

    @app.post("/account/pair-order")
    def account_pair_order(request: Request, payload: Any = Body(None)):
        """An order on a token/token pair: one message and a fee from the account's own address."""
        account = app.state.signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        try:
            chain = chain_asked(said)
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        address = chains.account_address(account.pubkey, chain)
        if not address:
            return no_address(chain)
        try:
            index = Index(v1, chain)
            b, q = pair_props(index, said)
            message, what = pair_message(index, b, q, str(said.get("side") or ""), said.get("amount"),
                                         said.get("price"), address)
            outputs = class_c_or_b(chain, address, message.encode(), coin_pubkey(account.pubkey, chain), wrap=False)
            unsigned = build(chain, address, outputs, what)
            quota(account, "trade")
        except (tokenlib.TokenError, fundinglib.FundingError, AmountError, ValueError, V1Error) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        offer = offers.add(account.pubkey, chain.network, unsigned, what)
        return JSONResponse({"offer": offer.id, "what": what, "chain": chain.network, **unsigned.as_json()})

    @app.post("/account/pair-order/cancel")
    def account_pair_order_cancel(request: Request, payload: Any = Body(None)):
        """Take this account's buys or sells off a token/token pair (MetaDExCancelPair)."""
        account = app.state.signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        try:
            chain = chain_asked(said)
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        address = chains.account_address(account.pubkey, chain)
        if not address:
            return no_address(chain)
        try:
            index = Index(v1, chain)
            b, q = pair_props(index, said)
            side = str(said.get("side") or "sell")
            if side not in ("buy", "sell"):
                raise ValueError("a cancel names buys or sells")
            sale, want = (b, q) if side == "sell" else (q, b)
            message = P.MetaDExCancelPair(property_id_for_sale=sale["property_id"],
                                          property_id_desired=want["property_id"])
            what = f"cancel this account's {side}s of {b['name']} for {q['name']}"
            outputs = class_c_or_b(chain, address, message.encode(), coin_pubkey(account.pubkey, chain), wrap=False)
            unsigned = build(chain, address, outputs, what)
            quota(account, "trade")
        except (tokenlib.TokenError, fundinglib.FundingError, AmountError, ValueError, V1Error) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        offer = offers.add(account.pubkey, chain.network, unsigned, what)
        return JSONResponse({"offer": offer.id, "what": what, "chain": chain.network, **unsigned.as_json()})

    # ------------------------------------------------------------------ offers on pieces
    @app.post("/account/offer")
    def account_offer(request: Request, payload: Any = Body(None)):
        """Say on the chain what this account would pay for somebody's piece: an Offer in an AnyData in one
        OP_RETURN, funded from the account's address and signed in its own tab. Not pre-signed (see the docstring)."""
        account = app.state.signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        try:
            chain = chain_asked(said)
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        address = chains.account_address(account.pubkey, chain)
        if not address:
            return no_address(chain)
        try:
            index = Index(v1, chain)
            row = index.inscription(_key(str(said.get("piece", ""))))
            if row is None:
                raise ValueError("no such inscription on this node")
            if row["owner"] == address:
                raise ValueError("that one is already yours")
            take = swaplib.leg_of(mintpadlib.take_of(str(said.get("kind", "coins")), str(said.get("amount", "")),
                                                     int(said["property_id"]) if said.get("property_id") else None),
                                  index)
            price = swaplib.describe_leg(swaplib.leg_json(take, index))
            try:
                key_at(address)
            except swaplib.SwapError:
                raise ValueError("publish your key first, from your own page, or whoever holds this cannot answer "
                                 "you. It costs a small fee and is done once.") from None
            if take.kind == inscriptionlib.LEG_TOKEN and index.balance(address, take.property_id) < take.amount:
                raise ValueError(f"this account does not hold {price}. An offer is not a promise a node can keep -- "
                                 "it is the terms of a trade, and offering in a token you do not have spends your "
                                 "fee to be told no by whoever holds the piece.")
            if take.kind == inscriptionlib.LEG_COINS:
                enough = sum(c["value"] for c in CoinDB(v1, chain).unspent(address))
                if enough < take.amount:
                    raise ValueError(f"this account holds {format_amount(enough, True)} on {chain.label}, which is "
                                     f"less than the {price} offered. Anything on its way back to it is counted once "
                                     "its block lands.")
            body = inscriptionlib.Offer(txid=bytes.fromhex(row["txid"]), take=take).encode()
            outputs = class_c_or_b(chain, address, body, coin_pubkey(account.pubkey, chain))
            what = f"offer {price} for inscription #{row['number']}"
            unsigned = build(chain, address, outputs, what)
            quota(account, "trade")
        except (tokenlib.TokenError, fundinglib.FundingError, AmountError, swaplib.SwapError,
                mintpadlib.MintpadError, inscriptionlib.InscriptionError, ValueError, V1Error) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        offer = offers.add(account.pubkey, chain.network, unsigned, what)
        return JSONResponse({"offer": offer.id, "what": what, "number": row["number"], "chain": chain.network,
                             "presigned": False, **unsigned.as_json()})

    @app.post("/account/offer/bid-build")
    def account_offer_bid_build(request: Request, payload: Any = Body(None)):
        app.state.signed_in_account(request)
        return node_trades_refused()

    @app.post("/account/offer/bid")
    def account_offer_bid(request: Request, payload: Any = Body(None)):
        app.state.signed_in_account(request)
        return node_trades_refused()

    @app.post("/account/offer/decline")
    def account_offer_decline(request: Request, payload: Any = Body(None)):
        """Decline an offer on a piece of yours. The offer has to stand on the chain (NEEDS-V1: offers on pieces)."""
        account = app.state.signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        try:
            chain = chain_asked(said)
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        address = chains.account_address(account.pubkey, chain)
        index = Index(v1, chain)
        row = index.inscription(_key(str(said.get("piece", ""))))
        if row is None:
            return JSONResponse({"detail": "no such inscription on this node"}, status_code=400)
        if row["owner"] != address:
            return JSONResponse({"detail": "only whoever holds a piece can answer an offer on it, and this one is "
                                           f"held by {row['owner']}"}, status_code=400)
        return JSONResponse({"detail": "no such offer on this piece -- an offer is a transaction, and it stands only "
                                       "while the chain says it does. It may have been cancelled, or made over for a "
                                       "piece that has since moved"}, status_code=400)

    @app.post("/account/offer/withdraw")
    def account_offer_withdraw(request: Request, payload: Any = Body(None)):
        """Take a pre-signed offer back: spend the coins it was signed over, back to this account."""
        account = app.state.signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        row = bids.get(str(said.get("offer") or "").lower())
        if row is None or row["account"] != account.pubkey or row["status"] != bidslib.OPEN:
            return JSONResponse({"detail": "no standing offer of yours by that id"}, status_code=404)
        chain = chains.by_network(row["network"])
        if chain is None:
            return JSONResponse({"detail": "that chain is not here"}, status_code=400)
        try:
            unsigned = fundinglib.build_sweep(CoinDB(v1, chain), chain.params, row["buyer"], row["inputs"],
                                              rate=fees.MIN_FEE_PER_KB, what="withdraw your offer")
        except (fundinglib.FundingError, V1Error) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        offer = offers.add(account.pubkey, chain.network, unsigned, unsigned.what,
                           done=lambda txid, rid=row["id"]: bids.close(rid, "withdrawn", txid))
        return JSONResponse({"offer": offer.id, "chain": chain.network, **unsigned.as_json()})

    # ------------------------------------------------------------------ buy orders this site holds (standing.py)
    def standing_shape(s: dict, prop: dict) -> dict:
        return {"id": s["id"], "property_id": s["property_id"], "name": prop["name"],
                "left": format_amount(s["left_units"], prop["divisible"]),
                "amount": format_amount(s["units"], prop["divisible"]),
                "price": f"{_coins_each(Fraction(s['coins'], s['units']), prop['divisible']):.8f}".rstrip("0").rstrip("."),
                "away": bool(s["away"]), "status": s["status"]}

    @app.post("/account/standing")
    def account_standing(request: Request, payload: Any = Body(None)):
        """Place a buy order this site holds. `away` (fill while the buyer is gone) is node-facilitated and not
        here; a fill-when-back order stands now and /account/standing/fillable says when a sell order meets it."""
        account = app.state.signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        try:
            chain = chain_asked(said)
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        address = chains.account_address(account.pubkey, chain)
        if not address:
            return JSONResponse({"detail": "this account has no address on that chain"}, status_code=400)
        if said.get("away"):
            return node_trades_refused()
        try:
            index = Index(v1, chain)
            prop = index.property(_said_int(said, "property_id", "the token number"))
            if prop is None:
                raise ValueError("there is no such token")
            units = parse_amount(str(said.get("amount", "")), prop["divisible"])
            each = parse_amount(str(said.get("price", "")), True)
            if units <= 0 or each <= 0:
                raise ValueError("an amount and a price above zero, please")
            whole = COIN if prop["divisible"] else 1
            coins = -(-units * each // whole)
            if coins < fees.DUST_LIMIT:
                raise ValueError("that comes to less than the smallest payment the network carries; buy more, or at "
                                 "a higher price")
            quota(account, "trade")
            row = standing.add(network=chain.network, account=account.pubkey, buyer=address,
                               buyer_key=coin_pubkey(account.pubkey, chain).hex(), property_id=prop["property_id"],
                               units=units, coins=coins, away=False)
        except (tokenlib.TokenError, fundinglib.FundingError, AmountError, ValueError, V1Error) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        return JSONResponse({"standing": standing_shape(row, prop)})

    @app.post("/account/standing/lots")
    def account_standing_lots(request: Request, payload: Any = Body(None)):
        """What the buyer signs for each lot (SINGLE|ANYONECANPAY over the lot's coin and its own change)."""
        account = app.state.signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        s = standing.get(str(said.get("standing") or ""))
        if not s or s["account"] != account.pubkey:
            return JSONResponse({"detail": "no buy order of yours by that id"}, status_code=404)
        out = []
        change_script = txbuild.p2pkh_script(s["buyer"])
        for lot in standing.lots(s["id"], standinglib.LOT_UNSIGNED):
            coin = [{"txid": lot["txid"], "vout": lot["vout"], "value": lot["value"], "address": s["buyer"]}]
            outs = [(lot["change"], change_script)]
            out.append({"txid": lot["txid"], "vout": lot["vout"], "value": lot["value"], "units": lot["units"],
                        "change": lot["change"], "raw": fundinglib.build_raw_tx([(lot["txid"], lot["vout"])], outs),
                        "digest": fundinglib.sighash(coin, outs, 0, change_script,
                                                     sighash_type=fundinglib.SINGLE_ANYONECANPAY).hex()})
        return JSONResponse({"standing": s["id"], "lots": out})

    @app.post("/account/standing/sign")
    def account_standing_sign(request: Request, payload: Any = Body(None)):
        """Keep the lots' signatures. Nothing is broadcast."""
        from arcade import doginal_market as doginal_marketlib
        account = app.state.signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        s = standing.get(str(said.get("standing") or ""))
        if not s or s["account"] != account.pubkey:
            return JSONResponse({"detail": "no buy order of yours by that id"}, status_code=404)
        try:
            signatures = [str(x) for x in _said_list(said, "signatures", "the signatures")]
            chain = chains.by_network(s["network"])
            if chain is None:
                raise ValueError("that chain is not here")
            pubkey = coin_pubkey(account.pubkey, chain)
            if hash160(pubkey) != b58check_decode(s["buyer"])[1]:
                raise standinglib.StandingError("this account's key is not the buy order's address")
            change_script = txbuild.p2pkh_script(s["buyer"])
            for lot, sig_hex in zip(standing.lots(s["id"], standinglib.LOT_UNSIGNED), signatures):
                sig = bytes.fromhex(sig_hex)
                digest = fundinglib.sighash(
                    [{"txid": lot["txid"], "vout": lot["vout"], "value": lot["value"], "address": s["buyer"]}],
                    [(lot["change"], change_script)], 0, change_script, sighash_type=fundinglib.SINGLE_ANYONECANPAY)
                if not sig or sig[-1] != fundinglib.SINGLE_ANYONECANPAY \
                        or not doginal_marketlib.verify_signature(digest, sig[:-1], pubkey):
                    raise standinglib.StandingError("a lot's signature is not this account's SINGLE|ANYONECANPAY "
                                                    "signature over that lot: nothing was kept")
            standing.sign_lots(s["id"], signatures)
        except (standinglib.StandingError, ValueError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        return JSONResponse({"standing": s["id"], "status": "open"})

    @app.post("/account/standing/cancel")
    def account_standing_cancel(request: Request, payload: Any = Body(None)):
        """Stop a buy order, forget its signatures, and offer the transaction that moves its signed lots back."""
        account = app.state.signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        s = standing.get(str(said.get("standing") or ""))
        if not s or s["account"] != account.pubkey:
            return JSONResponse({"detail": "no buy order of yours by that id"}, status_code=404)
        signed = [{"txid": lot["txid"], "vout": int(lot["vout"]), "value": int(lot["value"])}
                  for lot in standing.lots(s["id"], standinglib.LOT_OPEN)]
        standing.cancel(s["id"])
        done = {"standing": s["id"], "status": "cancelled"}
        if not signed:
            return JSONResponse(done)
        try:
            chain = chains.by_network(s["network"])
            unspent = [c for c in signed if v1.core(chain.network, "gettxout", c["txid"], c["vout"], True)]
            if not unspent:
                return JSONResponse(done)
            unsigned = fundinglib.build_sweep(CoinDB(v1, chain), chain.params, s["buyer"], unspent,
                                              rate=fees.MIN_FEE_PER_KB, what="move your buy order's signed coins back")
        except (fundinglib.FundingError, V1Error, AttributeError) as exc:
            return JSONResponse({**done, "sweep_error": str(exc)})
        offer = offers.add(account.pubkey, chain.network, unsigned, unsigned.what)
        return JSONResponse({**done, "offer": offer.id, "chain": chain.network, **unsigned.as_json()})

    @app.get("/account/standing/fillable")
    def account_standing_fillable(request: Request):
        """This account's fill-when-back buy orders that a sell order now crosses."""
        account = app.state.signed_in_account(request)
        out = []
        for chain in [home()]:
            mine = [s for s in standing.mine(account.pubkey, chain.network)
                    if not s["away"] and s["status"] == standinglib.OPEN]
            if not mine:
                continue
            index = Index(v1, chain)
            for s in mine:
                left = s["left_units"]
                limit = Fraction(s["coins"], s["units"])
                for ask in index.book(s["property_id"])["asks"]:
                    if ask["pending"] or ask["address"] == s["buyer"]:
                        continue
                    if Fraction(ask["want_amount"], ask["sale_amount"]) > limit:
                        break
                    free = ask["reserved"] - ask["taking"]
                    units = min(left, free)
                    if units <= 0:
                        continue
                    if ask["want_amount"] * units // ask["sale_amount"] < fees.DUST_LIMIT:
                        continue
                    prop = index.property(s["property_id"])
                    out.append({"standing": s["id"], "chain": chain.network, "order": ask["txid"], "units": units,
                                "name": prop["name"] if prop else "", "version": chain.params.pubkeyhash_version})
                    left -= units
                    if left <= 0:
                        break
        return JSONResponse({"fills": out})

    # ------------------------------------------------------------------ listings: legs this site holds signatures for
    def listing_swap(naming: bytes):
        return listingslib.named_swap(naming)

    def sold_piece(row: dict) -> str:
        try:
            swap = listing_swap(bytes.fromhex(row["payload"] or ""))
        except ValueError:
            return ""
        if swap is None or swap.give.kind != inscriptionlib.LEG_INSCRIPTION:
            return ""
        return swap.give.txid.hex()

    def price_text(chain, listing: dict) -> str:
        swap = listing_swap(bytes.fromhex(str(listing.get("payload") or "")))
        if swap is not None and swap.take.kind == inscriptionlib.LEG_TOKEN:
            return swaplib.describe_leg(swaplib.leg_json(swap.take, Index(v1, chain)))
        coins = int(listing.get("price") or 0) / COIN
        return f"{coins:.8f}".rstrip("0").rstrip(".") + (" coin" if coins == 1 else " coins")

    def piece_called(chain, listing: dict) -> str:
        row = Index(v1, chain).inscription(sold_piece(listing)) if sold_piece(listing) else None
        if not row:
            return ""
        data = L._fromjson(row.get("json")) or {}
        name = data.get("name") if isinstance(data, dict) else None
        return f"{name} (#{row['number']})" if name else f"#{row['number']}"

    swept: dict[str, float] = {}

    def sweep_book(chain, force: bool = False) -> None:
        """Close what the book can no longer honestly advertise: expired, moved, or a signed coin spent."""
        listings.expire_due(chain.network)
        now = time.time()
        if not force and now - swept.get(chain.network, 0) < 20:
            return
        swept[chain.network] = now
        rows = listings.open_listings(chain.network, limit=1000, claims=True)
        if not rows:
            return
        try:
            index = Index(v1, chain)
            for row in rows:
                sold = sold_piece(row)
                got = index.inscription(sold) if sold else None
                if sold and got is not None and got["owner"] != row["owner"]:
                    listings.close(row["id"], "moved")
                    row["status"] = "moved"
            for row in rows:
                if row["status"] != "open":
                    continue
                for coin in (row["input"], row["coin"]):
                    if coin and not v1.core(chain.network, "gettxout", coin["txid"], int(coin["vout"]), True):
                        listings.close(row["id"], "spent")
                        break
        except (V1Error, ValueError, KeyError):
            return

    @app.get("/listings", response_class=HTMLResponse)
    def listings_page(request: Request):
        """The legs this site is holding a signature for, and nothing else (its own book; not the mesh's)."""
        groups, book_error = [], ""
        if node_trades():
            chain = home()
            try:
                sweep_book(chain)
                rows = listings.open_listings(chain.network)
            except Exception as exc:                        # noqa: BLE001 -- said on the page
                book_error, rows = f"the listing book could not be read: {exc}", []
            if rows:
                asked, why, held = True, "", {}
                try:
                    rpc = _Rpc(v1, chain.network)
                    for row in rows:
                        held[row["id"]] = listingslib.piece_held(rpc, row)
                except Exception as exc:                    # noqa: BLE001
                    asked, why = False, str(exc)
                named = tags_for([row["owner"] for row in rows])
                from arcade.messaging.sender import describe_duration
                groups.append({"chain": chain, "asked": asked, "why": why, "rows": [{
                    "id": row["id"], "what": row["what"], "seller": row["owner"], "tag": named.get(row["owner"], ""),
                    "price": f"{int(row['price']) / COIN:.8f}".rstrip("0").rstrip("."),
                    "piece": f"{row['input']['txid'][:16]}…:{row['input']['vout']}", "sells": sold_piece(row),
                    "held": held.get(row["id"]), "listed": row["created"],
                    "left": describe_duration(max(0, int(row["expires"] - time.time())))} for row in rows]})
        return render(request, "listings.html", groups=groups, book_error=book_error)

    @app.get("/account/listed")
    def account_listed(request: Request, chain: str = ""):
        """What this account's open listings hold: the pieces they sell and the coins they stand on."""
        account = app.state.signed_in_account(request)
        one = chains.by_network(chain or "test") or home()
        address = chains.account_address(account.pubkey, one)
        pieces, coins = [], []
        if address:
            for r in listings.open_listings(one.network, limit=1000, claims=True):
                if r["owner"] != address:
                    continue
                if sold_piece(r):
                    pieces.append(sold_piece(r))
                for c in (r["input"], r["coin"]):
                    if c:
                        coins.append({"txid": c["txid"], "vout": int(c["vout"])})
        return JSONResponse({"chain": one.network, "pieces": pieces, "coins": coins},
                            headers={"Cache-Control": "no-store"})

    def ask_payload(row: dict, take) -> bytes:
        from arcade.encoding import EncodingError, encode_class_c
        body = inscriptionlib.Swap(give=inscriptionlib.Leg(inscriptionlib.LEG_INSCRIPTION,
                                                           txid=bytes.fromhex(row["txid"])), take=take).encode()
        try:
            return encode_class_c(P.AnyData(data=body).encode())
        except EncodingError as exc:
            raise ValueError(f"{exc} A listing is written in one OP_RETURN because a leg has two outputs and both "
                             "are signed: what it sells, and what it costs.") from None

    def no_dearer_relist(chain, address: str, piece: dict, price: int) -> None:
        def mine():
            return [r for r in listings.open_listings(chain.network, limit=1000, claims=True)
                    if r["owner"] == address and sold_piece(r) == str(piece["txid"]).lower()]
        older = mine()
        if older:
            sweep_book(chain, force=True)
            older = mine()
        if not older:
            return
        cheapest = min(int(r.get("price") or 0) for r in older)
        if price > 0 and cheapest > 0 and price < cheapest:
            return
        raise ValueError(f"#{piece['number']} is listed already, and that signed listing stays good until its coins "
                         "move: cancel it first (it moves them back to you), then list it again"
                         + (" for more" if price > 0 and cheapest > 0 else ""))

    @app.post("/account/list")
    def account_list(request: Request, payload: Any = Body(None)):
        """Show an account the leg it is about to sign, and stop there (node-facilitated: off by default)."""
        account = app.state.signed_in_account(request)
        if not node_trades():
            return node_trades_refused()
        said = payload if isinstance(payload, dict) else {}
        try:
            chain = chain_asked(said)
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        address = chains.account_address(account.pubkey, chain)
        if not address:
            return no_address(chain)
        try:
            index = Index(v1, chain)
            token = _said_int(said, "token", "the token number")
            if token:
                prop = index.property(token)
                if prop is None:
                    raise ValueError(f"there is no token {token}")
                units = parse_amount(str(said.get("amount", "")), prop["divisible"])
                if units <= 0:
                    raise ValueError("a listing names a price, and that is nothing")
                take = inscriptionlib.Leg(inscriptionlib.LEG_TOKEN, property_id=token, amount=units)
                price = 0
            else:
                price = parse_amount(str(said.get("amount", "")), True)
                if price <= 0:
                    raise ValueError("a listing names a price, and that is nothing")
                if price < fees.dust_limit(None):
                    raise ValueError(f"the smallest price the network will carry is "
                                     f"{fees.dust_limit(None) / COIN:g} -- anything less is dust, and nobody would "
                                     "relay it")
                take = inscriptionlib.Leg(inscriptionlib.LEG_COINS, amount=price)
            row = index.inscription(_key(str(said.get("piece", ""))))
            if row is None:
                raise ValueError("no such inscription on this node")
            if row["owner"] != address:
                raise ValueError(f"only whoever holds a piece can price it, and this one is held by {row['owner']}")
            no_dearer_relist(chain, address, row, price)
            naming = ask_payload(row, take)
            piece = swaplib.describe_leg(swaplib.leg_json(swaplib.leg_of({"inscription": row["txid"]}, index), index))
            cost = swaplib.describe_leg(swaplib.leg_json(take, index))
            what = f"list {piece} for {cost}"
            coins = CoinDB(v1, chain).unspent(address)
            usable = sorted((c for c in coins if int(c["value"]) >= fees.DUST_LIMIT), key=lambda c: int(c["value"]))
            held = usable + [c for c in coins if c not in usable]
            if len(held) < 2:
                raise ValueError(f"this address has {len(held)} coin to spend and a listing that says what it sells "
                                 "needs two of them: one input signs the bytes naming the piece, the other signs the "
                                 "price. Split it first: Wallet, Send, 1 coin to your own @name, then list it again.")
            leg = fundinglib.build_leg(chain.params, address, held[0], coins=price, rate=fees.MIN_FEE_PER_KB,
                                       what=what, payload=naming, coin=held[1])
        except (fundinglib.FundingError, inscriptionlib.InscriptionError, swaplib.SwapError, AmountError,
                ValueError, V1Error) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        return JSONResponse({"chain": chain.network, "price": price, "number": row["number"],
                             **({"take": swaplib.leg_json(take, index)}
                                if take.kind == inscriptionlib.LEG_TOKEN else {}), **leg.as_json()})

    @app.post("/account/list/sign")
    def account_list_sign(request: Request, payload: Any = Body(None)):
        """File the leg this browser signed, from its bytes and the chain alone (listings.Listings.register).
        Claims and prize pools are not on this site: a leg is filed as the account's own listing of a piece."""
        account = app.state.signed_in_account(request)
        if not node_trades():
            return node_trades_refused()
        said = payload if isinstance(payload, dict) else {}
        try:
            chain = chain_asked(said)
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        address = chains.account_address(account.pubkey, chain)
        if not address:
            return no_address(chain)
        try:
            raw = _said_hex(said, "raw", "the signed leg", least=SMALLEST_TX)
            signatures = [str(s) for s in _said_list(said, "signatures", "the leg's signatures")]
            pubkey = bytes.fromhex(_said_hex(said, "pubkey", "your public key", sizes=(33, 65)))
            if said.get("claim_hash") or str(said.get("owner") or "") not in ("", address) or said.get("bound"):
                raise ValueError("claims and prize pools are not on this arcade's site yet")
            quota(account, "list", nbytes=len(bytes.fromhex(raw)))
            said_amount = str(said.get("amount", "")).strip()
            try:
                token_priced = float(said_amount or "x") == 0
            except ValueError:
                token_priced = False
            price = 0 if token_priced else parse_amount(said_amount, True)
            days = max(0, min(90, _said_int(said, "days", "the days")))
            listing = listings.register(_Rpc(v1, chain.network), raw=raw, signatures=signatures, pubkey=pubkey,
                                        network=chain.network, owner=address, price=price,
                                        seconds=days * 86400 or listingslib.LISTED_FOR)
        except (listingslib.ListingError, fundinglib.FundingError, swaplib.SwapError, AmountError, ValueError,
                RuntimeError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        if token_priced:
            swap = listing_swap(bytes.fromhex(str(listing.get("payload") or "")))
            if swap is None or swap.take.kind != inscriptionlib.LEG_TOKEN:
                listings.close(listing["id"], "withdrawn")
                return JSONResponse({"detail": "a listing names a price, and that is nothing"}, status_code=400)
        swap = listing_swap(bytes.fromhex(listing["payload"]))
        words = ""
        if swap is not None:
            index = Index(v1, chain)
            try:
                words = (f"gives {swaplib.describe_leg(swaplib.leg_json(swap.give, index))} and takes "
                         f"{swaplib.describe_leg(swaplib.leg_json(swap.take, index))}")
            except swaplib.SwapError:
                words = ""
        return JSONResponse({"listed": listing["id"], "claim": False, "what": words, "chain": chain.network,
                             "expires": listing["expires"],
                             "piece": f"{listing['input']['txid'][:16]}…:{listing['input']['vout']}"})

    @app.post("/account/list/cancel")
    def account_list_cancel(request: Request, payload: Any = Body(None)):
        """Offer the transaction that takes this account's listings of a piece back: it spends the coins their
        signatures were made over, back to this account. Nothing is broadcast here."""
        account = app.state.signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        try:
            chain = chain_asked(said)
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        address = chains.account_address(account.pubkey, chain)
        piece = str(said.get("piece") or "").strip().lower()
        rows = [r for r in listings.open_listings(chain.network, limit=1000, claims=True)
                if r["owner"] == address and sold_piece(r) == piece]
        if not address or not rows:
            return JSONResponse({"detail": "this account has no open listing of that piece"}, status_code=404)
        coins = []
        for r in rows:
            for c in (r["input"], r["coin"]):
                if c and (c["txid"], c["vout"]) not in {(x["txid"], x["vout"]) for x in coins}:
                    coins.append(c)
        try:
            number = (Index(v1, chain).inscription(piece) or {}).get("number", "?")
            unsigned = fundinglib.build_sweep(CoinDB(v1, chain), chain.params, address, coins,
                                              rate=fees.MIN_FEE_PER_KB, what=f"cancel the listing of #{number}")
        except (fundinglib.FundingError, ValueError, V1Error) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        offer = offers.add(account.pubkey, chain.network, unsigned, unsigned.what)
        return JSONResponse({"offer": offer.id, "chain": chain.network, "listings": [r["id"] for r in rows],
                             **unsigned.as_json()})

    def fill_terms(chain, address: str, listing: dict):
        """A listing's trade, and the transaction this account's signature completes (the original's _fill_terms,
        less claim lots and arrivals)."""
        if listing["owner"] == address:
            raise swaplib.SwapError("a wallet cannot fill its own order")
        index = Index(v1, chain)
        sold = sold_piece(listing)
        got = index.inscription(sold) if sold else None
        if sold and (got is None or got["owner"] != listing["owner"]):
            if listing.get("id") and listings.get(str(listing["id"])):
                listings.close(listing["id"], "moved")
            raise swaplib.SwapError("the seller no longer holds that piece, so this listing is over. Nothing was "
                                    "spent.")
        foreign, outputs = listingslib.leg_terms(listing)
        named = listing_swap(bytes.fromhex(str(listing["payload"] or "")))
        piece = (f"{listing['input']['txid'][:16]}…:{listing['input']['vout']}" if named is None
                 else swaplib.describe_leg(swaplib.leg_json(named.give, index)))
        take = named.take if named is not None else None
        if take is not None and take.kind == inscriptionlib.LEG_TOKEN:
            left = index.balance(address, take.property_id) - index.pending_out(address, take.property_id)
            if left < take.amount:
                raise swaplib.SwapError(f"this costs {swaplib.describe_leg(swaplib.leg_json(take, index))}, and this "
                                        "account does not hold that much that is not already on its way somewhere. "
                                        "Nothing was spent.")
            cost = swaplib.describe_leg(swaplib.leg_json(take, index))
        else:
            cost = swaplib.describe_leg(swaplib.leg_json(
                inscriptionlib.Leg(inscriptionlib.LEG_COINS, amount=int(listing["price"])), index))
        what = f"buy {piece} for {cost}"
        unsigned = fundinglib.build_partial(CoinDB(v1, chain), chain.params, address, foreign, outputs,
                                            rate=fees.MIN_FEE_PER_KB, what=what)
        return listing, unsigned, what

    def listing_to_fill(chain, listing_id: str):
        listing = listings.get(str(listing_id))
        if listing is None or listing["network"] != chain.network or listing.get("status", "open") != "open" \
                or listing.get("claim_hash"):
            raise ValueError("no such listing on this chain -- it may have expired, been filled, or been made over "
                             "on the other one")
        return listing

    @app.post("/account/buy")
    def account_buy(request: Request, payload: Any = Body(None)):
        """Show an account the transaction that fills somebody else's listing. Nothing is spent here."""
        account = app.state.signed_in_account(request)
        if not node_trades():
            return node_trades_refused()
        said = payload if isinstance(payload, dict) else {}
        try:
            chain = chain_asked(said)
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        address = chains.account_address(account.pubkey, chain)
        if not address:
            return no_address(chain)
        try:
            listing, unsigned, _ = fill_terms(chain, address, listing_to_fill(chain, str(said.get("listing", ""))))
        except (fundinglib.FundingError, listingslib.ListingError, swaplib.SwapError, ValueError, V1Error) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        return JSONResponse({"chain": chain.network, "listing": listing["id"], "seller": listing["owner"],
                             "seller_tag": tags_for([listing["owner"]]).get(listing["owner"], ""),
                             "price": int(listing["price"]), "price_text": price_text(chain, listing), "claim": False,
                             "piece": piece_called(chain, listing), "lot": False, **unsigned.as_json()})

    @app.post("/account/buy/sign")
    def account_buy_sign(request: Request, payload: Any = Body(None)):
        """Paste the buyer's signatures onto the seller's leg (listings.paste_leg), and broadcast it."""
        account = app.state.signed_in_account(request)
        if not node_trades():
            return node_trades_refused()
        said = payload if isinstance(payload, dict) else {}
        try:
            chain = chain_asked(said)
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        address = chains.account_address(account.pubkey, chain)
        if not address:
            return no_address(chain)
        lane = f"account {account.pubkey}"
        with lanes_lock:
            if lane in lanes:
                return JSONResponse({"detail": "one of your transactions is still going. Wait for it to be broadcast "
                                               "and try again -- signing two at once from one wallet spends the same "
                                               "coin twice."}, status_code=409)
            lanes.add(lane)
        try:
            try:
                listing, unsigned, what = fill_terms(chain, address,
                                                     listing_to_fill(chain, str(said.get("listing", ""))))
            except (fundinglib.FundingError, listingslib.ListingError, swaplib.SwapError, ValueError,
                    V1Error) as exc:
                return JSONResponse({"detail": str(exc)}, status_code=400)
            if str(said.get("raw") or "") != unsigned.raw:
                return JSONResponse({"detail": "this node would build you a different transaction now than the one you "
                                               "signed -- a block landed, or one of your other transactions went out. "
                                               "Ask for another one and sign that; a signature over the old bytes "
                                               "would buy something you never agreed to."}, status_code=409)
            try:
                signatures = [str(x) for x in _said_list(said, "signatures", "the signatures")]
                pubkey = bytes.fromhex(str(said.get("pubkey") or ""))
                if not pubkey:
                    raise ValueError("this request names no public key, and a transaction nobody can trace to a key "
                                     "is not a transaction this node will finish")
                if hash160(pubkey) != b58check_decode(address)[1]:
                    raise ValueError(f"that public key is not the key behind this account's {chain.label} address, "
                                     "so pasting it would write a sale one wallet paid and another is named on")
                raw = listingslib.paste_leg(_Rpc(v1, chain.network), listing, unsigned, signatures, pubkey)
                quota(account, "send")
                txid = str((v1.cmd("tx.broadcast", hex=raw, chain=chain.key) or {}).get("txid") or "")
            except (listingslib.ListingError, fundinglib.FundingError, swaplib.SwapError, AmountError,
                    ValueError) as exc:
                return JSONResponse({"detail": str(exc)}, status_code=400)
            except (V1Error, RuntimeError) as exc:
                return JSONResponse({"detail": f"the node refused it: {exc}"}, status_code=409)
            listings.close(listing["id"], "filled", spent_by=txid)
            return JSONResponse({"txid": txid, "what": what, "chain": chain.network, "seller": listing["owner"],
                                 "price": int(listing["price"])})
        finally:
            with lanes_lock:
                lanes.discard(lane)
