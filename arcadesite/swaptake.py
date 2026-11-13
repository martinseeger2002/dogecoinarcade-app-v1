"""Taking a cross-chain swap order from the website: the visitor's browser makes
the secret and signs every step; this site carries the questions to the maker's arcade through its own Arcade V1.0
(swap.quote / swap.told / swap.status), builds each transaction for the browser to check and sign, and holds the
refund the browser signs first, which it sends after the lock's time if the swap never finished.

    POST /account/swap/quote     {id}            the order held for this visitor, its terms and the lock to make
    POST /account/swap/lock      {id, hash, redeem}  the visitor's lock checked; where to send, and how much
    POST /account/swap/locked    {id, txid}      the visitor's lock on the chain (its output found)
    POST /account/swap/step      {id}            tell the maker once the lock is mined; then the maker's lock, checked
    POST /account/swap/done      {id, txid}      the claim the browser sent
    POST /account/trade/spend    the unsigned claim or refund of one lock coin and its digests (the original's route)
    POST /account/trade/spend/sign   the browser's signatures put in place, checked and broadcast
    POST /account/trade/watch    a pre-signed refund held, sent after the lock's time if the coin is still there

Coins for coins on chains this site keeps a wallet on (V1.0's lock times are clock times there), and one inscription
for coins (2026-11-12, the Ord Market app's listings): the visitor locks the price, claims the inscription's lock (its
coin first and whole, the reserve beside it spent as the fee: htlc.doginal_out), and this site's taker fee is paid as
an output of the visitor's lock. Tokens and the ARCADE chain come next. The operator turns it on in /admin (market_taker).
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

from arcade import escrow as escrowlib
from arcade import fees
from arcade import funding as fundinglib
from arcade import htlc as htlclib
from arcade.txbuild import op_return_script, p2pkh_script
from arcade.trade_watch import WatchError, put_scripts

from .v1 import V1Error

#: What a spend out of a lock pays the network on every chain (V1.0 htlc.rs SPEND_FEE): a lock holds its amount and
#: this, as V1.0's maker checks (swaps.rs `seen`).
SPEND_FEE = 1_000_000
COIN = 100_000_000
#: The coin under a token's lock (V1.0 swaps.rs LEDGER_COIN): its spend's fee and two dust outputs.
LEDGER_COIN = SPEND_FEE + 2 * 1_000_000
#: The taker's lock lasts this many times the maker's (swaps.rs taker_factor, off the ARCADE chain).
TAKER_FACTOR = 2
#: A claim is not built when the maker's lock ends sooner than this, or a third of its time if that is less (swaps.rs
#: never claims within 2 minutes of it).
CLAIM_MARGIN = 600
TXID = re.compile(r"[0-9a-f]{64}")


def _fmt(sats: int) -> str:
    return f"{sats // COIN}.{sats % COIN:08d}".rstrip("0").rstrip(".")


def register(app, st, chains):
    v1 = st.v1
    db_path = st.home / "site-swaps.sqlite"
    lock = threading.Lock()

    def db():
        c = sqlite3.connect(db_path, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("CREATE TABLE IF NOT EXISTS swap (id TEXT PRIMARY KEY, account TEXT, data TEXT, step TEXT, at INTEGER)")
        c.execute("CREATE TABLE IF NOT EXISTS held (n INTEGER PRIMARY KEY AUTOINCREMENT, account TEXT, network TEXT, "
                  "txid TEXT, vout INTEGER, kind TEXT, redeem TEXT, raw TEXT, sigs TEXT, locktime INTEGER, "
                  "status TEXT, sent TEXT, note TEXT, at INTEGER)")
        return c

    def load(qid: str) -> dict | None:
        with db() as c:
            row = c.execute("SELECT data FROM swap WHERE id=?", (qid,)).fetchone()
        return json.loads(row["data"]) if row else None

    def save(qid: str, account: str, data: dict) -> None:
        with lock, db() as c:
            c.execute("INSERT OR REPLACE INTO swap VALUES (?,?,?,?,?)",
                      (qid, account, json.dumps(data), str(data.get("step") or ""), int(time.time())))

    def chain_of(key: str):
        """The site's chain for V1.0's chain key ("peptest", "doge", ...) or network name ("test"), only where this
        site keeps wallets: an order's leg may name its chain either way."""
        return next((c for c in chains.account_chains() if key in (c.key, c.network)), None)

    def coin_pub(pubkey: str, chain) -> str:
        return str(st.setting(chains.coinkey_key(pubkey, chain), "") or "").lower()

    def fail(detail: str, status: int = 400):
        return JSONResponse({"detail": detail}, status_code=status)

    def mine(request: Request, qid: str):
        """(account, its swap) for a question this account was asked, or (account, None)."""
        account = app.state.signed_in_account(request)
        s = load(qid)
        return account, (s if s and s.get("account") == account.pubkey else None)

    def tx_out(chain, txid: str, vout: int) -> dict | None:
        """One output as the chain has it (Core's verbose transaction), with its confirmations."""
        got = v1.core(chain.network, "getrawtransaction", txid, True)
        outs = (got or {}).get("vout") or []
        if vout >= len(outs):
            return None
        o = outs[vout]
        return {"value": int(round(float(o["value"]) * COIN)), "script": o["scriptPubKey"]["hex"],
                "confirmations": int(got.get("confirmations") or 0)}

    def lock_coin(chain, txid: str, vout: int) -> dict | None:
        """A lock coin as gettxout answers it, or, while its transaction still waits for a block (V1.0's gettxout
        answers confirmed coins only), as that waiting transaction has it. None when it is spent or not there."""
        out = v1.core(chain.network, "gettxout", txid, vout, True)
        if out is not None:
            return out
        try:
            got = v1.core(chain.network, "getrawtransaction", txid, True)
        except V1Error:
            return None
        outs = (got or {}).get("vout") or []
        if int((got or {}).get("confirmations") or 0) > 0 or vout >= len(outs):
            return None                                      # mined and not unspent: spent
        return {"value": outs[vout]["value"], "scriptPubKey": outs[vout]["scriptPubKey"], "confirmations": 0}

    # ---------------------------------------------------------------- the visitor's take, step by step
    @app.post("/account/swap/quote")
    def swap_quote(request: Request, payload: Any = Body(None)):
        said = payload if isinstance(payload, dict) else {}
        qid = str(said.get("id") or "")
        account = app.state.signed_in_account(request)
        row = app.state.page_send(qid) if hasattr(app.state, "page_send") else None
        if row is None or row.get("kind") != "market" or row.get("op") != "swap_take" or row["status"] != "pending":
            return fail("no such question waiting", 404)
        if row["address"] != chains.account_address(account.pubkey, chains.account_chain()):
            return fail("no such question waiting", 404)
        if not st.setting("market_taker", False):
            return fail("this website does not take trades across chains (its operator can turn that on)", 403)
        args = row["args"]
        try:
            q = v1.cmd("swap.quote", node=args["node"], order=args["order"], taker=qid)
        except V1Error as e:
            return fail(str(e))
        o = q.get("order") or {}
        give, get = o.get("give") or {}, o.get("get") or {}
        ord_buy = give.get("kind") == "ord" and get.get("kind") == "coin"
        # Tokens bought for coins (2026-11-12): an arcade-ledger token, or a DRC-20 / PRC-20 transfer note in the lock.
        token_buy = give.get("kind") in ("token", "token20") and get.get("kind") == "coin"
        # An arcade token given (2026-11-12): the visitor's lock is the ledger's send of it to the lock's address.
        token_give = get.get("kind") == "token" and give.get("kind") == "coin"
        # An inscription sold to an offer (2026-11-12): the visitor locks it (its coin first and whole, a reserve beside
        # it); being an inscription, its fee is a share of the coins it gets, paid at its claim.
        ord_sell = get.get("kind") == "ord" and give.get("kind") == "coin"
        # A DRC-20 / PRC-20 token given (2026-11-12): locked as V1.0 locks one (two notes, token20_swap), its fee whole
        # tokens rounded up, a token send of its own to the operator.
        t20_give = get.get("kind") == "token20" and give.get("kind") == "coin"
        # DigiDollars (2026-11-12, ddswap.py: V1.0's Taproot lock in Python), bought or given for coins; whole dollars.
        dd_buy = give.get("kind") == "dd" and get.get("kind") == "coin"
        dd_give = get.get("kind") == "dd" and give.get("kind") == "coin"
        for leg in ([give] if dd_buy else [get] if dd_give else []):
            if not str(leg.get("amount") or "").isdigit() or int(leg["amount"]) % 100 or int(leg["amount"]) <= 0:
                return fail("DigiDollars move in whole dollars")
        if not (ord_buy or token_buy or token_give or ord_sell or t20_give or dd_buy or dd_give) and (give.get("kind") != "coin" or get.get("kind") != "coin"):
            return fail("on this website a take gives coins (for coins, an inscription or a token); giving tokens or an "
                        "inscription comes next. The Arcade app on your own computer takes them all.")
        gc, wc = chain_of(give.get("chain", "")), chain_of(get.get("chain", ""))
        if gc is None or wc is None:
            return fail("this website keeps no wallet on one of those chains")
        my_give, my_get = coin_pub(account.pubkey, gc), coin_pub(account.pubkey, wc)
        if not my_give or not my_get:
            return fail("your account has no key on one of those chains yet: open your wallet once on each")
        maker_keys = q.get("maker_keys") or {}
        secs = int(q.get("maker_secs") or 4 * 3600)
        need = int(get["amount"]) + SPEND_FEE
        # This site's fee (the taker's fee goes to the taker's website's operator): a share of what the
        # visitor receives, paid as its own output by their claim; none under the dust line or without a payout address.
        bps = int(round(float(st.setting("trade_cut", 0) or 0) * 100))
        # Paid in what the visitor gives, with their lock:
        # a share of the amount locked, an output of the lock transaction on that chain.
        fee_chain = wc
        fee = int(get["amount"]) * bps // 10000
        token_fee = 0
        sell_at = None
        if ord_sell:
            from . import ordsend
            inscription = str(get.get("asset") or "")
            try:
                ins = v1.cmd("explorer.inscription", chain=wc.key, id=inscription) or {}
                at = str(ins.get("at") or "").split(":")
                tx = v1.cmd("explorer.tx", chain=wc.key, txid=at[0]) if len(at) == 3 else {}
            except V1Error as e:
                return fail(str(e))
            mine_addr = chains.account_address(account.pubkey, wc)
            bad = ordsend.exactly_this(ins, (tx or {}).get("outputs") or [], at[0] if at else "", int(at[1]) if len(at) == 3 else -1,
                                       inscription, mine_addr) if len(at) == 3 else "that inscription is not on the chain here"
            if bad:
                return fail(bad)
            sell_at = {"txid": at[0], "vout": int(at[1])}
            fee = int(give["amount"]) * bps // 10000           # of the coins it gets, at the claim
            fee_chain = gc
        if token_give:
            # The taker fee in the token itself, a ledger send to the operator beside the lock.
            token_fee, fee = fee, 0
        if t20_give:
            whole = int(get["amount"]) if str(get.get("amount") or "").isdigit() else 0
            token_fee, fee = (-(-whole * bps // 10000) if bps else 0), 0
        dd_fee_to = ""
        if dd_give:
            # The fee in DigiDollars, whole dollars rounded up, its own DD send to
            # the operator's DigiDollar address (the operator account's key on that chain).
            dollars = int(get["amount"]) // 100
            token_fee, fee = ((-(-dollars * bps // 10000)) * 100 if bps else 0), 0
            from arcade.ddlib import ddtx
            op = str(st.setting("operator") or "").lower()
            okey = coin_pub(op, wc) if op else ""
            dd_fee_to = ddtx.dd_address(wc.network, bytes.fromhex(okey)[1:]) if okey else ""
        fee_to = ""
        fee_to = app.state.market_payout(fee_chain.network) if hasattr(app.state, "market_payout") else ""
        if dd_give:
            fee_to = dd_fee_to
        if (fee < SPEND_FEE and not token_fee) or not fee_to:
            fee, token_fee, fee_to = 0, 0, ""
        s = {"account": account.pubkey, "question": qid, "node": args["node"], "order": o, "sized": bool(q.get("sized")),
             "maker_keys": maker_keys, "maker_secs": secs, "taker_time": int(time.time()) + TAKER_FACTOR * secs,
             "give_net": gc.network, "get_net": wc.network, "my_give": my_give, "my_get": my_get, "step": "quoted",
             "sell_at": sell_at, "ord_sell": bool(ord_sell),
             "fee": fee, "fee_to": fee_to, "kind": "ord" if ord_buy else give.get("kind") if token_buy else "coin",
             "token": {"asset": str(give.get("asset") or ""), "amount": str(give.get("amount") or "")} if token_buy else None,
             "inscription": str(give.get("asset") or "") if ord_buy else str(get.get("asset") or "") if ord_sell else "",
             "fee_with_lock": bool(fee) and not ord_sell,
             "token_give": {"asset": str(get.get("asset") or ""), "amount": str(get.get("amount") or ""), "fee": token_fee} if token_give else None,
             "t20_give": {"tick": str(get.get("asset") or "").lower(), "amount": str(get.get("amount") or ""), "fee": token_fee} if t20_give else None,
             "dd_buy": int(give["amount"]) if dd_buy else 0,
             "dd_give": {"cents": int(get["amount"]), "fee": token_fee} if dd_give else None}
        save(qid, account.pubkey, s)
        row["network"] = gc.network                         # the claim this question ends with is on that chain
        token_name = (f"token #{give.get('asset')}" if give.get("kind") == "token" else str(give.get("asset") or "").upper())
        return JSONResponse({"give": {"chain": gc.label, "network": gc.network,
                                      "coin": token_name if token_buy else gc.params.coin,
                                      "amount": str(give["amount"]) if token_buy else _fmt(int(give["amount"]))},
                             "get": {"chain": wc.label, "network": wc.network, "coin": wc.params.coin,
                                     "amount": _fmt(int(get["amount"]))},
                             "lock": _fmt(need), "lock_network": wc.network, "taker_time": s["taker_time"],
                             "maker_key": maker_keys.get(get["chain"], ""), "my_key": my_get,
                             "minutes": secs // 60, "fee": _fmt(fee) if fee else "", "fee_pct": bps / 100 if fee else 0,
                             "receive": str(give["amount"]) if token_buy else _fmt(int(give["amount"])),
                             "kind": s["kind"], "inscription": s["inscription"], "fee_with_lock": s["fee_with_lock"],
                             "sell": bool(ord_sell),
                             "t20": {**s["t20_give"], "to": s["fee_to"]} if s.get("t20_give") else None,
                             "dd": ({"give": int(get["amount"]) // 100, "fee": token_fee // 100} if dd_give
                                    else {"get": int(give["amount"]) // 100} if dd_buy else None)})

    @app.post("/account/swap/lock")
    def swap_lock(request: Request, payload: Any = Body(None)):
        said = payload if isinstance(payload, dict) else {}
        account, s = mine(request, str(said.get("id") or ""))
        if s is None or s["step"] != "quoted":
            return fail("no such swap waiting", 404)
        try:
            redeem = bytes.fromhex(str(said.get("redeem") or ""))
            want_hash = bytes.fromhex(str(said.get("hash") or ""))
        except ValueError:
            return fail("hex, please")
        lk = htlclib.parse(redeem)
        get = s["order"]["get"]
        if (lk is None or lk.get("sized") or lk["secret_hash"] != want_hash or len(want_hash) != 32
                or lk["claimer"].hex() != s["maker_keys"].get(get["chain"], "").lower()
                or lk["refunder"].hex() != s["my_get"] or lk["locktime"] != s["taker_time"]):
            return fail("that lock is not the one agreed: nothing to send")
        wc = chains.by_network(s["get_net"])
        s.update(hash=want_hash.hex(), redeem=redeem.hex(), step="locking")
        save(s["question"], account.pubkey, s)
        out = {"address": htlclib.address_of(redeem, wc.params), "amount": _fmt(int(get["amount"]) + SPEND_FEE),
               "network": wc.network}
        o_give = s["order"]["give"]
        if s.get("dd_give"):
            try:
                out.update(dd_lock_offers(account, s, wc, lk))
            except (fundinglib.FundingError, ValueError, V1Error) as e:
                return fail(str(e))
            return JSONResponse(out)
        if s.get("t20_give"):
            try:
                out["lock20"] = t20_start(account, s, wc, redeem)
            except (fundinglib.FundingError, ValueError, V1Error) as e:
                return fail(str(e))
            return JSONResponse(out)
        if s.get("ord_sell"):
            from . import ordsend
            payer = chains.account_address(account.pubkey, wc)
            got_coins = (v1.cmd("tx.coins", addresses=[payer], chain=wc.key) or {}).get("addresses", {}).get(payer, {})
            at = s["sell_at"]
            row = next((c for c in (got_coins.get("held_back") or []) + (got_coins.get("confirmed") or [])
                        if c["txid"] == at["txid"] and int(c["vout"]) == at["vout"]), None)
            if row is None:
                return fail("that inscription's coin is not yours to spend now")
            coin = {"txid": at["txid"], "vout": at["vout"], "value": int(row["value"]), "address": payer}
            try:
                u = ordsend.build_first(v1, wc, payer, coin, htlclib.doginal_lock_outputs(coin["value"], redeem),
                                        f"lock inscription {s['inscription']} for the swap")
            except Exception as e:  # noqa: BLE001
                return fail(str(e))
            o_ = app.state.offers.add(account.pubkey, wc.network, u, u.what)
            s.update(sell_value=coin["value"])
            save(s["question"], account.pubkey, s)
            out["offer"] = {"offer": o_.id, "chain": wc.network, **u.as_json()}
            return JSONResponse(out)
        if s.get("token_give"):
            # The ledger's send of the token to the lock's address (its coin LEDGER_COIN, as V1.0's own token locks), and
            # this site's fee in the token by a second send: both built here for the browser to check and sign.
            from .actions import class_c_or_b
            from .wallet import CoinDB
            from arcade import tokens as tokenlib
            payer = chains.account_address(account.pubkey, wc)
            key = bytes.fromhex(s["my_get"])
            tg = s["token_give"]
            def offer_for(units: int, to_script: bytes, value: int, what: str):
                outs = class_c_or_b(wc, payer, tokenlib.send_payload(int(tg["asset"]), units), key, wrap=False)
                outs.append((value, to_script))
                u = fundinglib.build(CoinDB(v1, wc), wc.params, payer, outs, rate=fees.MIN_FEE_PER_KB, what=what)
                o_ = app.state.offers.add(account.pubkey, wc.network, u, u.what)
                return {"offer": o_.id, "chain": wc.network, **u.as_json()}
            try:
                out["offer"] = offer_for(int(tg["amount"]), htlclib.p2sh_script(redeem), LEDGER_COIN,
                                         f"lock {tg['amount']} of token #{tg['asset']} for the swap")
                if int(tg.get("fee") or 0):
                    out["fee_offer"] = offer_for(int(tg["fee"]), p2pkh_script(s["fee_to"]), 1_000_000,
                                                 f"this site's fee, {tg['fee']} of token #{tg['asset']}")
            except (fundinglib.FundingError, ValueError) as e:
                return fail(str(e))
            return JSONResponse(out)
        if s.get("fee_with_lock"):
            # The lock and this site's fee in one transaction, built here from the visitor's coins for their browser
            # to check and sign (as every send here is).
            from .wallet import CoinDB
            payer = chains.account_address(account.pubkey, wc)
            outputs = [(int(get["amount"]) + SPEND_FEE, htlclib.p2sh_script(redeem)), (int(s["fee"]), p2pkh_script(s["fee_to"]))]
            what = "an inscription" if s.get("kind") == "ord" else f"{_fmt(int(o_give['amount']))} {chains.by_network(s['give_net']).params.coin}"
            try:
                unsigned = fundinglib.build(CoinDB(v1, wc), wc.params, payer, outputs, rate=fees.MIN_FEE_PER_KB,
                                            what=f"lock {_fmt(int(get['amount']))} {wc.params.coin} for {what}, "
                                                 f"and this site's fee {_fmt(int(s['fee']))}")
            except fundinglib.FundingError as e:
                return fail(str(e))
            offer = app.state.offers.add(account.pubkey, wc.network, unsigned, unsigned.what)
            out["offer"] = {"offer": offer.id, "chain": wc.network, **unsigned.as_json()}
        return JSONResponse(out)

    @app.post("/account/swap/locked")
    def swap_locked(request: Request, payload: Any = Body(None)):
        said = payload if isinstance(payload, dict) else {}
        account, s = mine(request, str(said.get("id") or ""))
        txid = str(said.get("txid") or "").lower()
        if s is None or s["step"] != "locking" or not TXID.fullmatch(txid):
            return fail("no such swap locking", 404)
        wc = chains.by_network(s["get_net"])
        script = htlclib.p2sh_script(bytes.fromhex(s["redeem"])).hex()
        if s.get("dd_give"):
            script = taker_dd_lock(s).script().hex()
        try:
            got = v1.core(wc.network, "getrawtransaction", txid, True)
        except V1Error as e:
            return fail(str(e))
        need = (0 if s.get("dd_give") else LEDGER_COIN if s.get("token_give") else int(s.get("sell_value") or 0) if s.get("ord_sell")
                else int(s["order"]["get"]["amount"]) + SPEND_FEE)
        vout = next((n for n, o in enumerate((got or {}).get("vout") or [])
                     if o["scriptPubKey"]["hex"] == script and int(round(float(o["value"]) * COIN)) >= need), None)
        if vout is None:
            return fail("that transaction does not pay the lock")
        s.update(lock_txid=txid, lock_vout=vout, step="locked")
        save(s["question"], account.pubkey, s)
        if s.get("ord_sell") and vout != 0:
            return fail("an inscription's lock is output 0 of its transaction")
        if s.get("dd_give"):
            s.update(lock_txid=txid, lock_vout=vout, step="locked")
            save(s["question"], account.pubkey, s)
            return JSONResponse({"vout": vout, "network": wc.network, "locktime": s["taker_time"], "dd": True})
        leg = ({"kind": "token", "asset": s["token_give"]["asset"], "amount": s["token_give"]["amount"]} if s.get("token_give")
               else {"kind": "doginal", "asset": s["inscription"], "amount": "1"} if s.get("ord_sell")
               else {"kind": "coin", "amount": _fmt(need)})
        return JSONResponse({"vout": vout, "redeem": s["redeem"], "network": wc.network, "locktime": s["taker_time"], "leg": leg})

    def ord_lock_wrong(chain, txid: str, vout: int, redeem: bytes, inscription: str) -> str:
        """Why the maker's lock of an inscription is not the one agreed ("" when it is): the inscription at the start of
        output 0, the reserve as output 1, both under the lock, as V1.0's own ord index reads it."""
        script = htlclib.p2sh_script(redeem).hex()
        if vout != 0:
            return "the inscription's lock is output 0 of its transaction"
        reserve = tx_out(chain, txid, 1)
        if not reserve or reserve["script"] != script or reserve["value"] < SPEND_FEE:
            return "no reserve beside the inscription"
        try:
            ins = v1.cmd("explorer.inscription", chain=chain.key, id=inscription) or {}
        except V1Error as e:
            return str(e)
        if str(ins.get("at") or "") != f"{txid}:0:0":
            return "the inscription is not at the lock's first coin"
        return ""

    def token_lock_wrong(chain, txid: str, vout: int, redeem: bytes, kind: str, token: dict) -> str:
        """Why the maker's token lock is not the one agreed ("" when it is): an arcade-ledger token held by the lock's
        address (V1.0's ledger), or a DRC-20 / PRC-20 transfer note of exactly that tick and amount revealed onto the
        lock's first coin with the reserve beside it."""
        script = htlclib.p2sh_script(redeem).hex()
        try:
            if kind == "token":
                out = tx_out(chain, txid, vout)
                if not out or out["script"] != script:
                    return "no lock coin"
                addr = htlclib.address_of(redeem, chain.params)
                held = v1.cmd("explorer.address", chain=chain.key, address=addr) or {}
                row = next((r for r in held.get("ledger_tokens") or [] if str(r.get("propertyid")) == str(token["asset"])), {})
                return "" if int(row.get("units") or 0) >= int(token["amount"]) else "the lock does not hold the tokens (yet)"
            if vout != 0:
                return "a note's lock is output 0 of its transaction"
            reserve = tx_out(chain, txid, 1)
            if not reserve or reserve["script"] != script:
                return "no reserve beside the note"
            ins = v1.cmd("explorer.inscription", chain=chain.key, id=f"{txid}i0") or {}
            note = json.loads(str((ins.get("content") or {}).get("text") or "{}"))
            if (note.get("op") != "transfer" or str(note.get("tick", "")).lower() != str(token["asset"]).lower()
                    or str(note.get("amt")) != str(token["amount"]) or str(ins.get("at") or "") != f"{txid}:0:0"):
                return "the note in the lock is not the one agreed"
            return ""
        except (V1Error, ValueError) as e:
            return str(e)

    # ---------------------------------------------------------------- DigiDollars (ddswap.py)
    def maker_dd_lock(s: dict, time_: int):
        """The maker's DigiDollar lock as V1.0 makes it (swaps.rs Spec.dd): the visitor claims, the maker refunds."""
        from .ddswap import Lock
        o = s["order"]
        return Lock(bytes.fromhex(s["hash"]), bytes.fromhex(s["my_give"]), bytes.fromhex(s["maker_keys"].get(o["give"]["chain"], "")),
                    int(time_), sized=bool(s.get("sized")))

    def taker_dd_lock(s: dict):
        """The visitor's DigiDollar lock: the maker claims, the visitor refunds after its time (a taker's lock is not sized)."""
        from .ddswap import Lock
        o = s["order"]
        return Lock(bytes.fromhex(s["hash"]), bytes.fromhex(s["maker_keys"].get(o["get"]["chain"], "")), bytes.fromhex(s["my_get"]),
                    int(s["taker_time"]), sized=False)

    def dd_lock_wrong(chain, txid: str, vout: int, lock, cents: int) -> str:
        """Why the maker's DigiDollar lock is not the one agreed ("" when it is): `cents` dollars at the lock's script as
        its record says, and a reserve at the same script."""
        from arcade.ddlib import ddtx
        from .ddswap import RESERVE
        got = v1.core(chain.network, "getrawtransaction", txid, True) or {}
        script = lock.script().hex()
        outs = got.get("vout") or []
        if vout >= len(outs) or outs[vout]["scriptPubKey"]["hex"] != script:
            return "the dollars are not under the lock agreed"
        held = {n: c for n, c, _k in ddtx.dollars_out(got)}
        if held.get(vout) != cents:
            return f"the lock holds {held.get(vout, 0)} cents, not {cents}"
        if not any(o["scriptPubKey"]["hex"] == script and round(float(o["value"]) * COIN) >= RESERVE for o in outs):
            return "no reserve beside the dollars"
        return ""

    def dd_lock_offers(account, s: dict, wc, lk: dict) -> dict:
        """The visitor's DigiDollar lock (a DD send into V1.0's lock shape) and this site's fee in DigiDollars (a DD send
        of its own), as offers for the browser to check and sign."""
        from arcade import digidollar as digidollib
        from .ddswap import lock_send
        from .wallet import CoinDB
        address = chains.account_address(account.pubkey, wc)
        pub = bytes.fromhex(s["my_get"])
        dd = v1.cmd("dd.coins", chain=wc.key, pubkey=pub.hex()) or {}
        held = [{"txid": c["txid"], "vout": int(c["vout"]), "cents": int(c["cents"])} for c in dd.get("coins") or []]
        dgb = [{"txid": c["txid"], "vout": c["vout"], "value": c["value"]} for c in CoinDB(v1, wc).unspent(address)]
        change = fundinglib.spend_for(address, wc.params)["change_script"]
        call = lambda method, params: v1.core(wc.network, method, *params)
        prepared = lock_send(wc.network, pub[1:], held, dgb, change, taker_dd_lock(s), int(s["dd_give"]["cents"]))
        u = digidollib.offer(prepared, wc.params, call, address, pub)
        o1 = app.state.offers.add(account.pubkey, wc.network, u, u.what)
        out = {"offer": {"offer": o1.id, "chain": wc.network, **u.as_json()}}
        fee = int(s["dd_give"].get("fee") or 0)
        if fee and s.get("fee_to"):
            spent = {(i["txid"], int(i["vout"])) for i in u.inputs}
            fresh = [c for c in held if (c["txid"], c["vout"]) not in spent]
            fresh_dgb = [c for c in dgb if (c["txid"], int(c["vout"])) not in spent]
            pf = digidollib.prepare_send(wc.network, pub, fresh, fresh_dgb, change, s["fee_to"], fee)
            uf = digidollib.offer(pf, wc.params, call, address, pub)
            o2 = app.state.offers.add(account.pubkey, wc.network, uf, uf.what)
            out["fee_offer"] = {"offer": o2.id, "chain": wc.network, **uf.as_json()}
        return out

    @app.post("/account/swap/ddrefund")
    def swap_ddrefund(request: Request, payload: Any = Body(None)):
        """The visitor's DigiDollar lock's refund, unsigned (`{id}`), then held (`{id, signatures}`): assembled here and
        sent by the watcher after the lock's time if the swap never finished."""
        from .ddswap import RESERVE, spend
        from arcade.ddlib import taproot as tr
        said = payload if isinstance(payload, dict) else {}
        account, s = mine(request, str(said.get("id") or ""))
        if s is None or not s.get("dd_give") or not s.get("lock_txid"):
            return fail("no DigiDollar lock of yours here", 404)
        wc = chains.by_network(s["get_net"])
        ddl = taker_dd_lock(s)
        got = v1.core(wc.network, "getrawtransaction", s["lock_txid"], True) or {}
        rv = next((n for n, o in enumerate(got.get("vout") or []) if o["scriptPubKey"]["hex"] == ddl.script().hex()
                   and round(float(o["value"]) * COIN) >= RESERVE), None)
        if rv is None:
            return fail("no reserve beside your dollars")
        mine_key = bytes.fromhex(s["my_get"])[1:]
        to = tr.p2tr(tr.tweak(mine_key))
        dgb_to = p2pkh_script(chains.account_address(account.pubkey, wc))
        u = spend(ddl, {"txid": s["lock_txid"], "vout": s["lock_vout"]}, int(s["dd_give"]["cents"]),
                  {"txid": s["lock_txid"], "vout": rv, "value": round(float(got["vout"][rv]["value"]) * COIN)}, to, dgb_to, None)
        if not said.get("signatures"):
            s["dd_refund"] = {"raw": u.raw, "rv": rv}
            save(s["question"], account.pubkey, s)
            return JSONResponse({"chain": wc.network, "offer": "", **u.as_json()})
        sigs = [str(x) for x in said.get("signatures") or []]
        try:
            raw = fundinglib.assemble(u, sigs, bytes.fromhex(s["my_get"]))
        except fundinglib.FundingError as e:
            return fail(str(e))
        from arcade import taproot as tap
        rows = [{**c, "spent": bytes.fromhex(c["spent"])} for c in u.inputs]
        for n, sig in enumerate(sigs):
            if not tap.verify(ddl.refunder, bytes.fromhex(u.sighashes[n]), bytes.fromhex(sig)):
                return fail("that refund's signature is not your key's")
        with lock, db() as c:
            c.execute("INSERT INTO held (account, network, txid, vout, kind, redeem, raw, sigs, locktime, status, note, sent) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (account.pubkey, wc.network, s["lock_txid"], int(s["lock_vout"]),
                                                          "ddrefund", "", raw, "", int(ddl.time), "held", "", ""))
        return JSONResponse({"ok": True})

    @app.post("/account/swap/ddclaim")
    def swap_ddclaim(request: Request, payload: Any = Body(None)):
        """The claim of the maker's DigiDollar lock, an offer (the secret in it) for the browser to sign; the visitor's
        dollars to their own DigiDollar address, the reserve's rest to their DGB address."""
        from .ddswap import RESERVE, spend
        from arcade.ddlib import taproot as tr
        said = payload if isinstance(payload, dict) else {}
        account, s = mine(request, str(said.get("id") or ""))
        if s is None or not s.get("dd_buy") or s.get("step") != "claimable":
            return fail("no DigiDollar lock to claim", 404)
        gc = chains.by_network(s["give_net"])
        ddl = maker_dd_lock(s, int(s["maker_time"]))
        try:
            secret = bytes.fromhex(str(said.get("secret") or ""))
        except ValueError:
            return fail("hex, please")
        got = v1.core(gc.network, "getrawtransaction", s["maker_txid"], True) or {}
        rv = next((n for n, o in enumerate(got.get("vout") or []) if o["scriptPubKey"]["hex"] == ddl.script().hex()
                   and round(float(o["value"]) * COIN) >= RESERVE), None)
        if rv is None:
            return fail("no reserve beside the dollars")
        to = tr.p2tr(tr.tweak(bytes.fromhex(s["my_give"])[1:]))
        dgb_to = p2pkh_script(chains.account_address(account.pubkey, gc))
        try:
            u = spend(ddl, {"txid": s["maker_txid"], "vout": s["maker_vout"]}, int(s["dd_buy"]),
                      {"txid": s["maker_txid"], "vout": rv, "value": round(float(got["vout"][rv]["value"]) * COIN)}, to, dgb_to, secret)
        except ValueError as e:
            return fail(str(e))
        o_ = app.state.offers.add(account.pubkey, gc.network, u, u.what)
        return JSONResponse({"offer": o_.id, "chain": gc.network, **u.as_json()})

    # ---------------------------------------------------------------- a DRC-20 / PRC-20 token into the visitor's lock
    # The original's /account/trade/lock20 (token20_swap) over V1.0: note 1 on the visitor's address (commit, reveal), the
    # funding (note 1 to the lock: its first move gives the lock's address the tokens; note 2's commit), then note 2
    # revealed onto the lock with the reserve beside it. Four offers, each signed in the browser before the next.
    T20_PROTO = {"doge": "drc-20", "main": "prc-20", "test": "prc-20"}
    t20_runs: dict[str, dict] = {}

    def t20_start(account, s: dict, wc, redeem: bytes) -> dict:
        from arcade import token20_send as t20
        from .wallet import CoinDB
        owner = chains.account_address(account.pubkey, wc)
        tg = s["t20_give"]
        held = v1.cmd("tokens", owner=owner, chain=wc.key) or []
        held = held if isinstance(held, list) else held.get("tokens") or []
        row = next((b for b in held if str(b.get("tick")).lower() == tg["tick"]), None)
        if row is None:
            raise ValueError(f"this wallet holds no {tg['tick']}")
        dec = int((v1.cmd("explorer.token", chain=wc.key, tick=str(row["tick"])) or {}).get("dec") or 0)
        need = (int(tg["amount"]) + int(tg.get("fee") or 0)) * 10 ** dec
        if need > int(row.get("overall") or 0) - int(row.get("transferable") or 0):
            raise ValueError(f"that is more {row['tick']} than this wallet has to trade (with this site's fee)")
        key = bytes.fromhex(s["my_get"])
        content = t20.note(T20_PROTO[wc.network], str(row["tick"]), str(tg["amount"]))
        pushes, note_redeem = t20.envelope(content, key)
        lock_script = htlclib.p2sh_script(redeem)
        outs = [(t20.POSTAGE, lock_script), (htlclib.DOGINAL_RESERVE, lock_script)]
        commit2 = sum(v for v, _ in outs) + t20.reveal_fee(wc.params, pushes, note_redeem, fees.MIN_FEE_PER_KB, outs)
        commit1 = t20.POSTAGE + t20.reveal_fee(wc.params, pushes, note_redeem, fees.MIN_FEE_PER_KB)
        unsigned = fundinglib.build(CoinDB(v1, wc), wc.params, owner, [(commit1, t20.commit_script(note_redeem))],
                                    rate=fees.MIN_FEE_PER_KB, what=f"lock {tg['amount']} {row['tick']} for the swap (1 of 4: the commit)")
        run = {"q": s["question"], "chain": wc.network, "address": owner, "tick": str(row["tick"]), "amount": tg["amount"],
               "pushes": [x.hex() for x in pushes], "redeem": note_redeem.hex(), "outs": [[v, sc.hex()] for v, sc in outs],
               "commit1": commit1, "commit2": commit2, "step": "commit", "txids": {}}
        t20_runs[s["question"]] = run
        return t20_offer(account, wc, unsigned, run, "commit")

    def t20_offer(account, chain, unsigned, run: dict, slot: str) -> dict:
        o_ = app.state.offers.add(account.pubkey, chain.network, unsigned, unsigned.what,
                                  done=lambda t, r=run, k=slot: r["txids"].__setitem__(k, t))
        return {"step": slot, "of": 4, "offer": o_.id, "chain": chain.network, **unsigned.as_json()}

    @app.post("/account/swap/lock20")
    def swap_lock20(request: Request, payload: Any = Body(None)):
        """The next of the four offers of a token lock, once the one before it went out; then where the lock is."""
        from arcade import token20_send as t20
        from . import ordsend
        said = payload if isinstance(payload, dict) else {}
        account, s = mine(request, str(said.get("id") or ""))
        run = t20_runs.get(str(said.get("id") or ""))
        if s is None or run is None:
            return fail("no such token lock being made (it may have expired): start again", 404)
        wc = chains.by_network(run["chain"])
        pushes = [bytes.fromhex(x) for x in run["pushes"]]
        note_redeem = bytes.fromhex(run["redeem"])
        tx_ = run["txids"]
        what = f"lock {run['amount']} {run['tick']} for the swap"
        try:
            if run["step"] == "commit":
                if not tx_.get("commit"):
                    raise ValueError("the commit has not gone out yet: sign it first")
                got = v1.core(wc.network, "getrawtransaction", tx_["commit"], True) or {}
                vout = next(int(o["n"]) for o in got.get("vout") or [] if o["scriptPubKey"]["hex"] == t20.commit_script(note_redeem).hex())
                u = t20.build_reveal(wc.params, {"txid": tx_["commit"], "vout": vout, "value": run["commit1"]}, pushes, note_redeem,
                                     run["address"], what=what + " (2 of 4: the note)")
                run["step"] = "reveal"
                return JSONResponse(t20_offer(account, wc, u, run, "reveal"))
            if run["step"] == "reveal":
                if not tx_.get("reveal"):
                    raise ValueError("the note has not gone out yet: sign it first")
                lock_script = bytes.fromhex(run["outs"][0][1])
                coin = {"txid": tx_["reveal"], "vout": 0, "value": t20.POSTAGE, "address": run["address"]}
                u = ordsend.build_first(v1, wc, run["address"], coin, [(t20.POSTAGE, lock_script),
                                                                       (run["commit2"], t20.commit_script(note_redeem))],
                                        what + " (3 of 4: the tokens to the lock)")
                run["step"] = "fund"
                return JSONResponse(t20_offer(account, wc, u, run, "fund"))
            if run["step"] == "fund":
                if not tx_.get("fund"):
                    raise ValueError("the funding has not gone out yet: sign it first")
                outs = [(int(v), bytes.fromhex(sc)) for v, sc in run["outs"]]
                u = t20.build_reveal(wc.params, {"txid": tx_["fund"], "vout": 1, "value": run["commit2"]}, pushes, note_redeem,
                                     what=what + " (4 of 4: the lock)", outputs=outs)
                run["step"] = "lock"
                return JSONResponse(t20_offer(account, wc, u, run, "lock"))
            if run["step"] == "lock":
                if not tx_.get("lock"):
                    raise ValueError("the lock has not gone out yet: sign it first")
                s.update(lock_txid=tx_["lock"], lock_vout=0, step="locked")
                save(s["question"], account.pubkey, s)
                run["step"] = "done"
            return JSONResponse({"step": "done", "lock_txid": tx_["lock"], "vout": 0, "redeem": s["redeem"], "network": wc.network,
                                 "locktime": s["taker_time"], "leg": {"kind": "token20", "asset": run["tick"].lower(), "amount": run["amount"]}})
        except (fundinglib.FundingError, ValueError, V1Error, StopIteration) as e:
            return fail(str(e) or "that commit is not the one this lock made")

    @app.post("/account/swap/step")
    def swap_step(request: Request, payload: Any = Body(None)):
        """One step on: the maker told once the visitor's lock is mined; then the maker's lock read and checked."""
        said = payload if isinstance(payload, dict) else {}
        account, s = mine(request, str(said.get("id") or ""))
        if s is None:
            return fail("no such swap", 404)
        o, wait = s["order"], ""
        if s["step"] == "wrong":
            return JSONResponse({"step": "failed", "say": "The other side's lock is not what was agreed: it is not claimed. "
                                 "Your refund comes back after your lock's time."})
        try:
            if s["step"] == "locked":
                wc = chains.by_network(s["get_net"])
                out = tx_out(wc, s["lock_txid"], s["lock_vout"])
                if not out or out["confirmations"] < 1:
                    return JSONResponse({"step": "locked", "say": f"Your lock is waiting for its block on {wc.label}."})
                keys = {o["get"]["chain"]: s["my_get"], o["give"]["chain"]: s["my_give"]}
                v1.cmd("swap.told", node=s["node"], order=o["id"], hash=s["hash"], taker=s["question"], taker_keys=keys,
                       taker_time=s["taker_time"], taker_lock={"txid": s["lock_txid"], "vout": s["lock_vout"], "step": "locked"})
                s["step"] = "told"
                save(s["question"], account.pubkey, s)
            if s["step"] == "told":
                a = v1.cmd("swap.status", node=s["node"], hash=s["hash"], taker=s["question"])
                ml = a.get("maker_lock") or {}
                if a.get("step") in ("failed", "refunded"):
                    return JSONResponse({"step": "failed", "say": "The other side did not lock. Your coins come back "
                                         "to you after your lock's time; this site sends the refund you signed."})
                if ml.get("step") != "locked":
                    return JSONResponse({"step": "told", "say": "The other side is locking its coins."})
                gc = chains.by_network(s["give_net"])
                t = int(ml.get("time") or 0)
                if t < time.time() + min(CLAIM_MARGIN, s["maker_secs"] // 3):
                    return JSONResponse({"step": "told", "say": "The other side's lock ends too soon to claim safely; "
                                         "your refund comes back after your lock's time."})
                redeem = htlclib.redeem_script(bytes.fromhex(s["hash"]), bytes.fromhex(s["my_give"]),
                                               bytes.fromhex(s["maker_keys"].get(o["give"]["chain"], "")), t,
                                               sized=s["sized"])
                txid, vout = str(ml.get("txid") or ""), int(ml.get("vout") or 0)
                out = tx_out(gc, txid, vout) if TXID.fullmatch(txid) else None
                if not out or out["confirmations"] < 1:
                    return JSONResponse({"step": "told", "say": f"The other side's lock is waiting for its block on "
                                                                f"{gc.label}."})
                if s.get("kind") == "ord":
                    bad = ord_lock_wrong(gc, txid, vout, redeem, s["inscription"])
                elif s.get("kind") in ("token", "token20"):
                    bad = token_lock_wrong(gc, txid, vout, redeem, s["kind"], s["token"])
                elif s.get("dd_buy"):
                    bad = dd_lock_wrong(gc, txid, vout, maker_dd_lock(s, t), s["dd_buy"])
                    if not bad:
                        s["maker_time"] = t
                elif out["script"] != htlclib.p2sh_script(redeem).hex() or out["value"] < int(o["give"]["amount"]) + SPEND_FEE:
                    bad = "short"
                else:
                    bad = ""
                if bad:
                    s["step"] = "wrong"
                    save(s["question"], account.pubkey, s)
                    return JSONResponse({"step": "failed", "say": "The other side's lock is not what was agreed: it is "
                                         "not claimed. Your refund comes back after your lock's time."})
                s.update(step="claimable", maker_redeem=redeem.hex(), maker_txid=txid, maker_vout=vout)
                save(s["question"], account.pubkey, s)
            if s["step"] == "claimable" and s.get("dd_buy"):
                return JSONResponse({"step": "claimable", "network": s["give_net"], "dd": True,
                                     "leg": {"kind": "dd", "network": s["give_net"], "amount": s["dd_buy"]}})
            if s["step"] == "claimable" and s.get("kind") in ("token", "token20"):
                return JSONResponse({"step": "claimable", "network": s["give_net"], "redeem": s["maker_redeem"],
                                     "txid": s["maker_txid"], "vout": 0 if s["kind"] == "token20" else s["maker_vout"],
                                     "leg": {"kind": s["kind"], "network": s["give_net"], **s["token"]}})
            if s["step"] == "claimable" and s.get("kind") == "ord":
                return JSONResponse({"step": "claimable", "network": s["give_net"], "redeem": s["maker_redeem"],
                                     "txid": s["maker_txid"], "vout": 0,
                                     "leg": {"kind": "doginal", "network": s["give_net"], "asset": s["inscription"], "amount": "1"}})
            if s["step"] == "claimable":
                return JSONResponse({"step": "claimable", "network": s["give_net"], "redeem": s["maker_redeem"],
                                     "txid": s["maker_txid"], "vout": s["maker_vout"],
                                     "leg": {"kind": "coin", "network": s["give_net"],
                                             **({"amount": str(int(o["give"]["amount"]) - int(s["fee"])), "fee_value": int(s["fee"]),
                                                 "fee_to": s["fee_to"]} if s.get("fee") and not s.get("fee_with_lock")
                                                else {"amount": str(int(o["give"]["amount"]))})}})
        except V1Error as e:
            wait = str(e)
        return JSONResponse({"step": s["step"], "say": wait or ""})

    @app.post("/account/swap/done")
    def swap_done(request: Request, payload: Any = Body(None)):
        said = payload if isinstance(payload, dict) else {}
        account, s = mine(request, str(said.get("id") or ""))
        if s is None:
            return fail("no such swap", 404)
        s.update(step="claimed", claim_txid=str(said.get("txid") or "")[:64])
        save(s["question"], account.pubkey, s)
        return JSONResponse({"ok": True})

    # ---------------------------------------------------------------- lock spends (the original's routes, over V1.0)
    def take_fee(account: str, txid: str, vout: int):
        """(fee, address) a claim of this account's take of the maker's lock `txid:vout` pays this site, or None."""
        with db() as c:
            rows = [json.loads(r["data"]) for r in c.execute("SELECT data FROM swap WHERE account=?", (account,))]
        s = next((x for x in rows if x.get("maker_txid") == txid and int(x.get("maker_vout") or 0) == vout), None)
        if s and s.get("fee_with_lock"):
            return None                                     # paid with the visitor's lock
        return (int(s["fee"]), s["fee_to"]) if s and int(s.get("fee") or 0) > 0 and s.get("fee_to") else None

    def lock_for(account, said: dict):
        chain = chains.by_network(str(said.get("chain") or ""))
        if chain is None:
            raise ValueError("this website keeps no wallet on that chain")
        redeem = bytes.fromhex(str(said.get("redeem") or ""))
        lk = htlclib.parse(redeem)
        if lk is None:
            raise ValueError("that is not a lock")
        owner = chains.account_address(account.pubkey, chain)
        if not owner:
            raise ValueError(f"this account has no {chain.label} address")
        return chain, owner, redeem, lk

    def ntime(chain):
        return int(time.time()) if getattr(chain.params, "tx_time", False) else None

    @app.post("/account/trade/spend")
    def trade_spend(request: Request, payload: Any = Body(None)):
        """The unsigned claim or refund of one lock coin, paying this account's own address, and its digests."""
        account = app.state.signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        try:
            chain, owner, redeem, lk = lock_for(account, said)
            kind = str(said.get("kind") or "")
            me = bytes.fromhex(coin_pub(account.pubkey, chain) or "00")
            if kind not in ("claim", "refund"):
                raise ValueError("a lock is claimed or refunded")
            if (kind == "claim" and lk["claimer"] != me) or (kind == "refund" and lk["refunder"] != me):
                raise ValueError("that lock is not this account's to " + kind)
            leg = said.get("leg") or {}
            leg_kind = str(leg.get("kind") or "coin")
            if leg_kind not in ("coin", "doginal", "token", "token20"):
                raise ValueError("coins, inscriptions and tokens only on this website for now")
            txid, vout = str(said.get("txid") or "").lower(), int(said.get("vout") or 0)
            if leg_kind in ("doginal", "token20"):
                vout = 0
            out = lock_coin(chain, txid, vout)
            if out is None:
                raise ValueError("that lock coin is spent, or not there")
            if out["scriptPubKey"]["hex"] != htlclib.p2sh_script(redeem).hex():
                raise ValueError("that coin is not under this lock")
            coin = {"txid": txid, "vout": vout, "value": int(round(float(out["value"]) * COIN))}
            operator = take_fee(account.pubkey, txid, vout) if kind == "claim" else None
            if leg_kind == "token":
                # An arcade-ledger token out of the lock (V1.0's swaps.rs spend): the ledger's send in one data output,
                # the receiver the last output; the lock's coin pays the fee.
                pid, units = int(leg.get("asset") or 0), int(leg.get("amount") or 0)
                if pid <= 0 or units <= 0:
                    raise ValueError("a token leg names its token and amount")
                data = b"arcd" + bytes(4) + pid.to_bytes(4, "big") + units.to_bytes(8, "big")
                if coin["value"] < htlclib.DUST + SPEND_FEE // 2:
                    raise ValueError("the lock's coin is too small to pay for its spend")
                inputs = [{"txid": txid, "vout": vout, "value": coin["value"]}]
                outputs = [(0, op_return_script(data)), (htlclib.DUST, p2pkh_script(owner))]
            elif leg_kind in ("doginal", "token20"):
                # An inscription (or a DRC-20 / PRC-20 note) comes out first and whole; its reserve (output 1, under the same lock) is the fee.
                res = lock_coin(chain, txid, 1)
                if res is None or res["scriptPubKey"]["hex"] != htlclib.p2sh_script(redeem).hex():
                    raise ValueError("no reserve under this lock beside the inscription")
                reserve = {"txid": txid, "vout": 1, "value": int(round(float(res["value"]) * COIN))}
                inputs, outputs = htlclib.doginal_out(coin, reserve, owner, operator=operator, refund=kind == "refund")
                inputs = [{**inputs[0], "value": coin["value"]}, {**inputs[1], "value": reserve["value"]}]
            else:
                inputs, outputs = htlclib.pay_out(coin, owner, fee=SPEND_FEE, params=chain.params, operator=operator)
            locktime = 0
            if kind == "refund":
                inputs = [{**c, "sequence": htlclib.NOT_FINAL} for c in inputs]
                locktime = lk["locktime"]
            nt = ntime(chain)
            raw = escrowlib.serialize(inputs, outputs, locktime, ntime=nt).hex()
            digests = [escrowlib.sighash(inputs, outputs, n, redeem, locktime, ntime=nt).hex() for n in range(len(inputs))]
        except (ValueError, V1Error) as exc:
            return fail(str(exc))
        return JSONResponse({"chain": chain.network, "kind": kind, "raw": raw, "sighash": digests[0], "sighashes": digests,
                             "value": coin["value"], "to": owner, "locktime": locktime})

    def checked_spend(account, said: dict):
        """(chain, redeem, lock, raw, signatures, kind): a spend of one lock coin that pays this account only."""
        chain, owner, redeem, lk = lock_for(account, said)
        raw = bytes.fromhex(str(said.get("raw") or ""))
        sigs = [bytes.fromhex(str(x)) for x in (said.get("signatures") or [said.get("signature") or ""])]
        decoded = v1.core(chain.network, "decoderawtransaction", raw.hex())
        vin = decoded["vin"]
        two = len(vin) == 2 and vin[0]["txid"] == vin[1]["txid"] and (int(vin[0]["vout"]), int(vin[1]["vout"])) == (0, 1)
        if not (len(vin) == 1 or two) or len(sigs) != len(vin):
            raise ValueError("a lock spend here spends one coin, or an inscription and its reserve, one signature each")
        for x in vin:
            under = lock_coin(chain, x["txid"], int(x["vout"]))
            if under is None or under["scriptPubKey"]["hex"] != htlclib.p2sh_script(redeem).hex():
                raise ValueError("that spend takes a coin that is not under this lock")
        if two and decoded["vout"][0]["scriptPubKey"]["hex"] != p2pkh_script(owner).hex():
            raise ValueError("an inscription comes out first, to you")
        kind = "refund" if said.get("kind") == "refund" else "claim"
        operator = take_fee(account.pubkey, vin[0]["txid"], int(vin[0]["vout"])) if kind == "claim" else None
        allowed = {p2pkh_script(owner).hex()} | ({p2pkh_script(operator[1]).hex()} if operator else set())
        pays = [(o["scriptPubKey"]["hex"], int(round(float(o["value"]) * COIN))) for o in decoded["vout"]]
        data = [(sc, v) for sc, v in pays if sc.startswith("6a")]
        if len(data) > 1 or any(v for _, v in data):
            raise ValueError("a lock spend carries at most one data output, of no value")
        allowed |= {sc for sc, _ in data}
        if {sc for sc, _ in pays} - allowed or (operator and (p2pkh_script(operator[1]).hex(), operator[0]) not in pays):
            raise ValueError("that spend pays somebody else")
        if kind == "refund" and int(decoded.get("locktime", 0)) != lk["locktime"]:
            raise ValueError("a refund carries the lock's own time")
        return chain, redeem, lk, raw, sigs, kind, decoded

    @app.post("/account/trade/spend/sign")
    def trade_spend_sign(request: Request, payload: Any = Body(None)):
        """The browser's signature (and a claim's secret) put in place, checked, and broadcast."""
        account = app.state.signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        try:
            chain, redeem, lk, raw, sigs, kind, _ = checked_spend(account, said)
            if kind == "refund":
                scripts = [htlclib.refund_script_sig(sig, redeem) for sig in sigs]
            else:
                secret = bytes.fromhex(str(said.get("secret") or ""))
                if htlclib.hash_of(secret) != lk["secret_hash"] or (lk.get("sized") and len(secret) != 32):
                    raise ValueError("that secret does not open this lock")
                scripts = [htlclib.claim_script_sig(sig, secret, redeem) for sig in sigs]
            signed = put_scripts(raw, scripts)
            txid = v1.core(chain.network, "sendrawtransaction", signed.hex())
        except (ValueError, WatchError, V1Error) as exc:
            return fail(str(exc))
        return JSONResponse({"ok": True, "txid": txid})

    @app.post("/account/trade/watch")
    def trade_watch(request: Request, payload: Any = Body(None)):
        """A pre-signed REFUND of this account's own lock, held and sent after the lock's time if the coin is still
        under the lock then (the swap never finished). Claims are not held here: the browser claims while it is open."""
        account = app.state.signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        try:
            if said.get("kind") != "refund":
                raise ValueError("this website holds refunds only")
            chain, redeem, lk, raw, sigs, kind, decoded = checked_spend(account, said)
            if lk["refunder"].hex() != coin_pub(account.pubkey, chain):
                raise ValueError("that lock is not this account's to refund")
            vin = decoded["vin"][0]
            inputs = [{"txid": vin["txid"], "vout": int(vin["vout"]), "sequence": int(vin.get("sequence", 0xFFFFFFFF))}]
            outputs = [(round(float(o["value"]) * COIN), bytes.fromhex(o["scriptPubKey"]["hex"])) for o in decoded["vout"]]
            digest = escrowlib.sighash(inputs, outputs, 0, redeem, lk["locktime"],
                                       ntime=int(decoded["time"]) if "time" in decoded and ntime(chain) else None)
            if not app.state.signature_good(lk["refunder"], digest, sigs[0]):
                raise ValueError("that signature is not this account's over that refund")
            with lock, db() as c:
                c.execute("INSERT INTO held (account, network, txid, vout, kind, redeem, raw, sigs, locktime, status, "
                          "sent, note, at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (account.pubkey, chain.network, vin["txid"], int(vin["vout"]), "refund", redeem.hex(), raw.hex(),
                           sigs[0].hex(), lk["locktime"], "held", "", "", int(time.time())))
        except (ValueError, V1Error) as exc:
            return fail(str(exc))
        return JSONResponse({"ok": True})

    def signature_good(pubkey: bytes, digest: bytes, signature: bytes) -> bool:
        """DER + SIGHASH_ALL byte, by `pubkey` (compressed), over `digest`."""
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec, utils
        if not signature or signature[-1] != 1:
            return False
        try:
            key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256K1(), bytes(pubkey))
            key.verify(signature[:-1], bytes(digest), ec.ECDSA(utils.Prehashed(hashes.SHA256())))
            return True
        except (InvalidSignature, ValueError):
            return False

    app.state.signature_good = signature_good
    # The claim a take ended with, as this site recorded it (/account/swap/done): the card's answer is checked against
    # it, since a just-sent transaction on another chain may not be read back yet.
    app.state.swap_claim_of = lambda qid: str((load(qid) or {}).get("claim_txid") or "")

    # ---------------------------------------------------------------- the refunds held
    def watch_once() -> None:
        with db() as c:
            rows = [dict(r) for r in c.execute("SELECT * FROM held WHERE status='held'")]
        for r in rows:
            try:
                one = chains.by_network(r["network"])
                out = lock_coin(one, r["txid"], r["vout"]) if one is not None else None
                if out is None:
                    status, note, sent = "spent", "the lock coin was spent (claimed or refunded)", ""
                else:
                    # The chain's median time when it gives one (V1.0 doesn't), else this clock: a refund sent early is
                    # only refused by the chain ("non-final"), noted, and tried again on the next pass.
                    info = v1.core(r["network"], "getblockchaininfo") or {}
                    if int(info.get("mediantime") or time.time()) < int(r["locktime"]):
                        continue
                    if r.get("kind") == "ddrefund":
                        sent = v1.core(r["network"], "sendrawtransaction", r["raw"])   # assembled when it was held
                    else:
                        script = htlclib.refund_script_sig(bytes.fromhex(r["sigs"]), bytes.fromhex(r["redeem"]))
                        sent = v1.core(r["network"], "sendrawtransaction", put_scripts(bytes.fromhex(r["raw"]), [script]).hex())
                    status, note = "sent", ""
            except (V1Error, WatchError, ValueError) as e:
                with lock, db() as c:
                    c.execute("UPDATE held SET note=? WHERE n=?", (str(e)[:200], r["n"]))
                continue
            with lock, db() as c:
                c.execute("UPDATE held SET status=?, sent=?, note=? WHERE n=?", (status, sent, note, r["n"]))

    def watcher():
        while True:
            try:
                watch_once()
            except Exception:                               # noqa: BLE001 -- kept running; each row notes its own error
                pass
            time.sleep(60)

    app.state.swap_watch_once = watch_once
    threading.Thread(target=watcher, name="swap-refunds", daemon=True).start()
