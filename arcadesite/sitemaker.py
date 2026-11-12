"""Placing an order for a website visitor: the maker side, so that both makers and takers
work.

The visitor's browser makes a key for the order and moves what it gives to that key's address (coins; or an
inscription and the coins its lock needs). Once that is in a block, this site places the order in its Arcade V1.0 with
the key and the visitor's own addresses as payouts (arcade-v1 swap.place key/payout, PR #117): V1.0 runs the maker's
side with the key, and what it gets, or a refund, goes to the visitor. The operator's maker fee rides on the order
(maker_fee): a share of what the maker gives, paid with its lock; for an inscription given, of the coins it gets.
Cancelling (`swap.release`) sends what the key holds back to the visitor. The key is held by this site's V1.0 while the
order is open, so an order is capped (market_maker_cap, whole coins per chain) and the card says so.

    POST /account/swap/make/quote    {id}               a swap_place question: its terms, fee and what to fund
    POST /account/swap/make/key      {id, key}          the order's key (before anything moves to it): its addresses
    POST /account/swap/make/funded   {id}               the browser has sent what the order gives to that address
    POST /account/swap/make/step     {id}               funded and in a block: placed in V1.0
    POST /account/swap/make/cancel   {id}               a swap_cancel question: the order released, its funds back
    GET  /account/swap/made                              this account's orders placed here

Giving coins or an inscription, for coins, on chains this site keeps a wallet on; tokens come next. The operator turns
it on in /admin (market_maker).
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from typing import Any

from fastapi import Body, Request
from fastapi.responses import JSONResponse

from arcade import htlc as htlclib
from arcade.script import b58check_encode, hash160

from .v1 import V1Error

SPEND_FEE = 1_000_000
COIN = 100_000_000
#: Coins on top of what an order gives, for the network fees of its lock (and the lock of an inscription's reserve).
FUND_MARGIN = 2 * SPEND_FEE
TXID = re.compile(r"[0-9a-f]{64}")


def _fmt(sats: int) -> str:
    return f"{sats // COIN}.{sats % COIN:08d}".rstrip("0").rstrip(".")


def register(app, st, chains):
    v1 = st.v1
    db_path = st.home / "site-made.sqlite"
    lock = threading.Lock()

    def db():
        c = sqlite3.connect(db_path, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("CREATE TABLE IF NOT EXISTS made (id TEXT PRIMARY KEY, account TEXT, data TEXT, step TEXT, at INTEGER)")
        return c

    def load(qid: str) -> dict | None:
        with db() as c:
            r = c.execute("SELECT data FROM made WHERE id=?", (qid,)).fetchone()
        return json.loads(r["data"]) if r else None

    def save(qid: str, account: str, data: dict) -> None:
        with lock, db() as c:
            c.execute("INSERT OR REPLACE INTO made (id, account, data, step, at) VALUES (?,?,?,?,?)",
                      (qid, account, json.dumps(data), data.get("step", ""), int(time.time())))

    def fail(detail: str, status: int = 400):
        return JSONResponse({"detail": detail}, status_code=status)

    def chain_of(key: str):
        return next((c for c in chains.account_chains() if c.key == key or c.network == key), None)

    def question(request: Request, qid: str, op: str):
        account = app.state.signed_in_account(request)
        row = app.state.page_send(qid) if hasattr(app.state, "page_send") else None
        if row is None or row.get("kind") != "market" or row.get("op") != op or row["status"] != "pending":
            return account, None
        if row["address"] != chains.account_address(account.pubkey, chains.account_chain()):
            return account, None
        return account, row

    def answered(qid: str, order: str) -> None:
        """The page's question answered here: its order placed (or released), named by the order's id."""
        row = app.state.page_send(qid) if hasattr(app.state, "page_send") else None
        if row is not None and row.get("status") == "pending":
            row.update(status="sent", txid=order)

    def coin_pub_of(pubkey: str, chain) -> str:
        """An account's coin key on a chain (hex, compressed), as the wallet published it (swaptake's coin_pub)."""
        return str(st.setting(chains.coinkey_key(pubkey, chain), "") or "")

    def mine(request: Request, qid: str):
        account = app.state.signed_in_account(request)
        s = load(qid)
        return account, (s if s and s["account"] == account.pubkey else None)

    # V1.0's coin under a token's lock (swaps.rs LEDGER_COIN: its spend's fee and two dust outputs).
    LEDGER_COIN = SPEND_FEE + 2 * htlclib.DUST

    @app.post("/account/swap/make/quote")
    def make_quote(request: Request, payload: Any = Body(None)):
        said = payload if isinstance(payload, dict) else {}
        qid = str(said.get("id") or "")
        account, row = question(request, qid, "swap_place")
        if row is None:
            return fail("no such question waiting", 404)
        if not st.setting("market_maker", False):
            return fail("this website does not place orders for visitors (its operator can turn that on)", 403)
        give, get = row["args"]["give"], row["args"]["get"]
        gc, wc = chain_of(give.get("chain", "")), chain_of(get.get("chain", ""))
        if gc is None or wc is None:
            return fail("this website keeps no wallet on one of those chains")
        if get.get("kind") not in ("coin", "dd") or give.get("kind") not in ("coin", "ord", "token", "token20", "dd"):
            return fail("on this website an order gives coins, an inscription, a token or DigiDollars, for coins or "
                        "DigiDollars. The Arcade app on your own computer places them all.")
        if give["kind"] == "dd" and get["kind"] == "dd":
            return fail("DigiDollars for DigiDollars is no trade")
        for leg in (give, get):
            if leg["kind"] == "dd" and (not str(leg.get("amount") or "").isdigit() or int(leg["amount"]) % 100 or int(leg["amount"]) <= 0):
                return fail("DigiDollars move in whole dollars")
        t20_give = give["kind"] == "token20"
        dd_give = give["kind"] == "dd"
        ord_give = give["kind"] == "ord"
        token_give = give["kind"] == "token"
        amount, price = (0 if ord_give or token_give or t20_give or dd_give else int(give["amount"])), int(get["amount"])
        at = {}
        if ord_give:
            # The visitor's own inscription, at the start of its coin (it moves to the order's address whole).
            try:
                ins = v1.cmd("explorer.inscription", chain=gc.key, id=give.get("asset", "")) or {}
            except V1Error as e:
                return fail(str(e))
            loc = str(ins.get("at") or "").split(":")
            if ins.get("owner") != chains.account_address(account.pubkey, gc) or len(loc) != 3:
                return fail("that inscription is not in your wallet on " + gc.label)
            at = {"txid": loc[0], "vout": int(loc[1])}
        cap = (st.setting("market_maker_cap", {}) or {}).get(gc.network)
        if not ord_give and cap not in (None, "") and amount > float(cap) * COIN:
            return fail(f"this website places orders of at most {cap} {gc.params.coin}")
        # The maker fee: a share of what the visitor gives, paid with the lock; for an inscription, of the coins it gets.
        bps = int(round(float(st.setting("trade_cut", 0) or 0) * 100))
        fee_chain = wc if ord_give else gc
        fee_to = app.state.market_payout(fee_chain.network) if hasattr(app.state, "market_payout") else ""
        token_units, token_fee = 0, 0
        if dd_give:
            # Whole dollars; the fee whole dollars rounded up (V1.0's whole_fee), sent by V1.0 from the order's key to the
            # operator's DigiDollar address.
            from arcade.ddlib import ddtx
            token_units = int(give["amount"])
            op = str(st.setting("operator") or "").lower()
            okey = coin_pub_of(op, gc) if op else ""
            fee_to = ddtx.dd_address(gc.network, bytes.fromhex(okey)[1:]) if okey else ""
            token_fee = (-(-(token_units // 100) * bps // 10000)) * 100 if (bps and fee_to) else 0
            fee = 0
        elif t20_give:
            # A DRC-20 / PRC-20 token: whole tokens; the fee whole tokens rounded up (V1.0's whole_fee), sent from the
            # order's key by V1.0 itself (a token send of its own).
            token_units = int(give["amount"]) if str(give.get("amount") or "").isdigit() else 0
            if token_units <= 0:
                return fail("a DRC-20 / PRC-20 order gives whole tokens")
            token_fee = -(-token_units * bps // 10000) if (bps and fee_to) else 0
            fee = 0
        elif token_give:
            # An arcade token: the fee in the token itself (V1.0's token_fee: units * bps / 10000), sent with the lock.
            token_units = int(give["amount"])
            token_fee = token_units * bps // 10000 if fee_to else 0
            fee = 0
        else:
            fee = (price if ord_give else amount) * bps // 10000
            if fee < SPEND_FEE:
                fee = 0
        if not fee_to or not (fee or token_fee):
            fee, token_fee, bps, fee_to = 0, 0, 0, ""
        if ord_give:
            fund = htlclib.DOGINAL_RESERVE + FUND_MARGIN
        elif dd_give:
            # The lock's 0.5 DGB reserve and the fees of the lock and the fee's DD send (DigiByte's DigiDollar fees).
            fund = 50_000_000 + 30_000_000
        elif t20_give:
            # V1.0's note steps from the key (note 1, the funding, note 2 with its reserve) and the fee's token send.
            fund = htlclib.DOGINAL_RESERVE + 10 * SPEND_FEE
        elif token_give:
            fund = LEDGER_COIN * 2 + 2 * FUND_MARGIN            # the lock's coin, and the fee's send, and their fees
        else:
            fund = amount + SPEND_FEE + fee + FUND_MARGIN
        s = {"account": account.pubkey, "question": qid, "give": give, "get": get, "give_net": gc.network,
             "get_net": wc.network, "ord": ord_give, "fee": fee, "bps": bps, "fee_to": fee_to,
             "fee_net": fee_chain.network, "fund": fund, "step": "quoted", "token": token_give, "t20": t20_give, "dd": dd_give,
             "dd_get": get["kind"] == "dd",
             "token_units": token_units + token_fee, "token_fee": token_fee}
        save(qid, account.pubkey, s)
        row["network"] = gc.network
        return JSONResponse({"give": {"chain": gc.label, "network": gc.network, "coin": gc.params.coin,
                                      "amount": "" if ord_give else _fmt(amount),
                                      "inscription": str(give.get("asset") or "") if ord_give else ""},
                             "get": {"chain": wc.label, "network": wc.network, "coin": wc.params.coin, "amount": _fmt(price)},
                             "fee": _fmt(fee) if fee else "", "fee_pct": bps / 100 if fee else 0,
                             "fee_coin": fee_chain.params.coin, "fund": _fmt(fund), "fund_network": gc.network,
                             "cap": cap or "", "ord": ord_give, "at": at, "token": token_give,
                             "token_units": token_units + token_fee, "token_fee": token_fee,
                             "property_id": int(give["asset"]) if token_give else None,
                             "t20": t20_give, "tick": str(give.get("asset") or "").lower() if t20_give else "",
                             "dd": dd_give, "dd_get": get["kind"] == "dd"})

    @app.post("/account/swap/make/key")
    def make_key(request: Request, payload: Any = Body(None)):
        """The order's key, taken before anything moves to it (a tab closed after funding leaves nothing stranded: this
        site's arcade holds the key and the order goes on, or is released back to the visitor)."""
        said = payload if isinstance(payload, dict) else {}
        account, s = mine(request, str(said.get("id") or ""))
        if s is None or s["step"] != "quoted":
            return fail("no such order being made", 404)
        key = str(said.get("key") or "").lower()
        if not re.fullmatch(r"[0-9a-f]{64}", key):
            return fail("that is not a key")
        try:
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric import ec
            pub = ec.derive_private_key(int(key, 16), ec.SECP256K1()).public_key().public_bytes(
                serialization.Encoding.X962, serialization.PublicFormat.CompressedPoint)
        except ValueError:
            return fail("that is not a key")
        gc, wc = chains.by_network(s["give_net"]), chains.by_network(s["get_net"])
        s.update(key=key, pubkey=pub.hex(), give_address=b58check_encode(gc.params.pubkeyhash_version, hash160(pub)),
                 get_address=b58check_encode(wc.params.pubkeyhash_version, hash160(pub)), step="funding")
        if s.get("dd"):
            from arcade.ddlib import ddtx
            s["give_dd_address"] = ddtx.dd_address(gc.network, pub[1:])
        save(s["question"], account.pubkey, s)
        return JSONResponse({"give_address": s["give_address"], "fund": _fmt(s["fund"]), "network": gc.network,
                             "dd_address": s.get("give_dd_address", ""), "dollars": f"{int(s['token_units']) / 100:.2f}" if s.get("dd") else ""})

    @app.post("/account/swap/make/funded")
    def make_funded(request: Request, payload: Any = Body(None)):
        said = payload if isinstance(payload, dict) else {}
        account, s = mine(request, str(said.get("id") or ""))
        if s is None or s["step"] != "funding":
            return fail("no such order being funded", 404)
        s.update(step="funded", funded_at=int(time.time()))
        save(s["question"], account.pubkey, s)
        return JSONResponse({"ok": True})

    def funded_enough(s: dict) -> str:
        """"" when the order key holds what the order needs, confirmed; else what it waits for."""
        gc = chains.by_network(s["give_net"])
        got = (v1.cmd("tx.coins", addresses=[s["give_address"]], chain=gc.key) or {}).get("addresses") or {}
        a = got.get(s["give_address"]) or {}
        coins = sum(int(c["value"]) for c in a.get("confirmed") or [])
        if coins < s["fund"]:
            return f"waiting for {_fmt(s['fund'])} {gc.params.coin} at the order's address to be in a block"
        if s.get("dd"):
            held = v1.cmd("dd.coins", chain=gc.key, pubkey=s["pubkey"]) or {}
            if int(held.get("cents") or 0) < int(s["token_units"]):
                return "waiting for the DigiDollars to reach the order's address"
        if s.get("t20"):
            rows = v1.cmd("tokens", owner=s["give_address"], chain=gc.key) or []
            rows = rows if isinstance(rows, list) else rows.get("tokens") or []
            row = next((r for r in rows if str(r.get("tick")).lower() == str(s["give"]["asset"]).lower()), {})
            dec = int((v1.cmd("explorer.token", chain=gc.key, tick=str(s["give"]["asset"])) or {}).get("dec") or 0)
            if int(row.get("overall") or 0) - int(row.get("transferable") or 0) < int(s["token_units"]) * 10 ** dec:
                return "waiting for the tokens to reach the order's address"
        if s.get("token"):
            held = v1.cmd("explorer.address", chain=gc.key, address=s["give_address"]) or {}
            row = next((r for r in held.get("ledger_tokens") or [] if str(r.get("propertyid")) == str(s["give"]["asset"])), {})
            if int(row.get("units") or 0) < int(s["token_units"]):
                return "waiting for the tokens to reach the order's address"
        if s["ord"]:
            ins = v1.cmd("explorer.inscription", chain=gc.key, id=s["give"]["asset"]) or {}
            if ins.get("owner") != s["give_address"]:
                return "waiting for the inscription to reach the order's address"
            at = str(ins.get("at") or "").split(":")
            if len(at) != 3 or at[2] != "0":
                return "the inscription is not at the start of its coin there"
        return ""

    @app.post("/account/swap/make/step")
    def make_step(request: Request, payload: Any = Body(None)):
        said = payload if isinstance(payload, dict) else {}
        account, s = mine(request, str(said.get("id") or ""))
        if s is None:
            return fail("no such order", 404)
        if s["step"] == "placed":
            return JSONResponse({"step": "placed", "order": s["order"]})
        if s["step"] != "funded":
            return JSONResponse({"step": s["step"], "say": ""})
        try:
            wait = funded_enough(s)
            if wait:
                return JSONResponse({"step": "funded", "say": wait})
            gc, wc = chains.by_network(s["give_net"]), chains.by_network(s["get_net"])
            payout = {gc.key: chains.account_address(account.pubkey, gc), wc.key: chains.account_address(account.pubkey, wc)}
            args = {"give": s["give"], "get": s["get"], "key": {gc.key: s["key"], wc.key: s["key"]}, "payout": payout}
            if s.get("dd") or s.get("dd_get"):
                from arcade.ddlib import ddtx
                ch = gc if s.get("dd") else wc
                my_key = coin_pub_of(account.pubkey, ch)
                args["payout_dd"] = {ch.key: ddtx.dd_address(ch.network, bytes.fromhex(my_key)[1:])}
            if s["fee"] or s.get("token_fee"):
                fc = chains.by_network(s["fee_net"])
                args["maker_fee"] = {"bps": s["bps"], "to": {fc.key: s["fee_to"]}}
            o = v1.cmd("swap.place", **args) or {}
        except V1Error as e:
            return JSONResponse({"step": "funded", "say": str(e)})
        s.update(order=o.get("id"), step="placed", placed_at=int(time.time()))
        save(s["question"], account.pubkey, s)
        answered(s["question"], s["order"])
        return JSONResponse({"step": "placed", "order": s["order"]})

    @app.post("/account/swap/make/cancel")
    def make_cancel(request: Request, payload: Any = Body(None)):
        said = payload if isinstance(payload, dict) else {}
        account, row = question(request, str(said.get("id") or ""), "swap_cancel")
        if row is None:
            return fail("no such question waiting", 404)
        order = str(row["args"].get("order") or "")
        with db() as c:
            rows = [json.loads(r["data"]) for r in c.execute("SELECT data FROM made WHERE account=?", (account.pubkey,))]
        s = next((x for x in rows if x.get("order") == order and x.get("step") == "placed"), None)
        if s is None:
            return fail("that is not an open order of yours placed by this website")
        try:
            r = v1.cmd("swap.release", order=order) or {}
        except V1Error as e:
            return fail(str(e))
        s.update(step="released", returned=r.get("returned"))
        save(s["question"], account.pubkey, s)
        answered(row["id"], order)
        return JSONResponse({"ok": True, "returned": r.get("returned") or []})

    @app.get("/account/swap/made")
    def made(request: Request):
        account = app.state.signed_in_account(request)
        with db() as c:
            rows = [json.loads(r["data"]) for r in c.execute("SELECT data FROM made WHERE account=? ORDER BY at DESC", (account.pubkey,))]
        return JSONResponse({"orders": [{k: x.get(k) for k in ("order", "give", "get", "step", "fee", "give_address", "returned")}
                                        for x in rows if x.get("order") or x.get("step") in ("funding", "funded")]})

    def placed_by(address_account: str) -> set:
        """The V1.0 order ids this website placed for an account (for the page API's `mine`)."""
        with db() as c:
            rows = [json.loads(r["data"]) for r in c.execute("SELECT data FROM made WHERE account=?", (address_account,))]
        return {x["order"] for x in rows if x.get("order") and x.get("step") == "placed"}

    app.state.made_orders = placed_by
