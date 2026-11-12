"""Taking a cross-chain swap order from the website (docs/MARKETPLACE-PLAN.md, 1 and 6): the visitor's browser makes
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

Coins for coins on chains this site keeps a wallet on (V1.0's lock times are clock times there); tokens, inscriptions
and the ARCADE chain come next. The operator turns it on in /admin (market_taker).
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
from arcade import htlc as htlclib
from arcade.trade_watch import WatchError, put_scripts

from .v1 import V1Error

#: What a spend out of a lock pays the network on every chain (V1.0 htlc.rs SPEND_FEE): a lock holds its amount and
#: this, as V1.0's maker checks (swaps.rs `seen`).
SPEND_FEE = 1_000_000
COIN = 100_000_000
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
        """The site's chain for V1.0's chain key ("peptest", "doge", ...), only where this site keeps wallets."""
        return next((c for c in chains.account_chains() if c.key == key), None)

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
        if give.get("kind") != "coin" or get.get("kind") != "coin":
            return fail("on this website, swaps across chains are coins for coins for now; tokens and inscriptions "
                        "come next. The Arcade app on your own computer takes them all.")
        gc, wc = chain_of(give.get("chain", "")), chain_of(get.get("chain", ""))
        if gc is None or wc is None:
            return fail("this website keeps no wallet on one of those chains")
        my_give, my_get = coin_pub(account.pubkey, gc), coin_pub(account.pubkey, wc)
        if not my_give or not my_get:
            return fail("your account has no key on one of those chains yet: open your wallet once on each")
        maker_keys = q.get("maker_keys") or {}
        secs = int(q.get("maker_secs") or 4 * 3600)
        need = int(get["amount"]) + SPEND_FEE
        s = {"account": account.pubkey, "question": qid, "node": args["node"], "order": o, "sized": bool(q.get("sized")),
             "maker_keys": maker_keys, "maker_secs": secs, "taker_time": int(time.time()) + TAKER_FACTOR * secs,
             "give_net": gc.network, "get_net": wc.network, "my_give": my_give, "my_get": my_get, "step": "quoted"}
        save(qid, account.pubkey, s)
        row["network"] = gc.network                         # the claim this question ends with is on that chain
        return JSONResponse({"give": {"chain": gc.label, "network": gc.network, "coin": gc.params.coin,
                                      "amount": _fmt(int(give["amount"]))},
                             "get": {"chain": wc.label, "network": wc.network, "coin": wc.params.coin,
                                     "amount": _fmt(int(get["amount"]))},
                             "lock": _fmt(need), "lock_network": wc.network, "taker_time": s["taker_time"],
                             "maker_key": maker_keys.get(get["chain"], ""), "my_key": my_get,
                             "minutes": secs // 60})

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
        return JSONResponse({"address": htlclib.address_of(redeem, wc.params),
                             "amount": _fmt(int(get["amount"]) + SPEND_FEE), "network": wc.network})

    @app.post("/account/swap/locked")
    def swap_locked(request: Request, payload: Any = Body(None)):
        said = payload if isinstance(payload, dict) else {}
        account, s = mine(request, str(said.get("id") or ""))
        txid = str(said.get("txid") or "").lower()
        if s is None or s["step"] != "locking" or not TXID.fullmatch(txid):
            return fail("no such swap locking", 404)
        wc = chains.by_network(s["get_net"])
        script = htlclib.p2sh_script(bytes.fromhex(s["redeem"])).hex()
        try:
            got = v1.core(wc.network, "getrawtransaction", txid, True)
        except V1Error as e:
            return fail(str(e))
        need = int(s["order"]["get"]["amount"]) + SPEND_FEE
        vout = next((n for n, o in enumerate((got or {}).get("vout") or [])
                     if o["scriptPubKey"]["hex"] == script and int(round(float(o["value"]) * COIN)) >= need), None)
        if vout is None:
            return fail("that transaction does not pay the lock")
        s.update(lock_txid=txid, lock_vout=vout, step="locked")
        save(s["question"], account.pubkey, s)
        return JSONResponse({"vout": vout, "redeem": s["redeem"], "network": wc.network, "locktime": s["taker_time"]})

    @app.post("/account/swap/step")
    def swap_step(request: Request, payload: Any = Body(None)):
        """One step on: the maker told once the visitor's lock is mined; then the maker's lock read and checked."""
        said = payload if isinstance(payload, dict) else {}
        account, s = mine(request, str(said.get("id") or ""))
        if s is None:
            return fail("no such swap", 404)
        o, wait = s["order"], ""
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
                if out["script"] != htlclib.p2sh_script(redeem).hex() or out["value"] < int(o["give"]["amount"]) + SPEND_FEE:
                    s["step"] = "wrong"
                    save(s["question"], account.pubkey, s)
                    return JSONResponse({"step": "failed", "say": "The other side's lock is not what was agreed: it is "
                                         "not claimed. Your refund comes back after your lock's time."})
                s.update(step="claimable", maker_redeem=redeem.hex(), maker_txid=txid, maker_vout=vout)
                save(s["question"], account.pubkey, s)
            if s["step"] == "claimable":
                return JSONResponse({"step": "claimable", "network": s["give_net"], "redeem": s["maker_redeem"],
                                     "txid": s["maker_txid"], "vout": s["maker_vout"],
                                     "leg": {"kind": "coin", "amount": o["give"]["amount"]}})
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
            if str((said.get("leg") or {}).get("kind") or "coin") != "coin":
                raise ValueError("coins only on this website for now")
            txid, vout = str(said.get("txid") or "").lower(), int(said.get("vout") or 0)
            out = lock_coin(chain, txid, vout)
            if out is None:
                raise ValueError("that lock coin is spent, or not there")
            if out["scriptPubKey"]["hex"] != htlclib.p2sh_script(redeem).hex():
                raise ValueError("that coin is not under this lock")
            coin = {"txid": txid, "vout": vout, "value": int(round(float(out["value"]) * COIN))}
            inputs, outputs = htlclib.pay_out(coin, owner, fee=SPEND_FEE, params=chain.params)
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
        if len(vin) != 1 or len(sigs) != 1:
            raise ValueError("a lock spend here spends one coin, with one signature")
        under = lock_coin(chain, vin[0]["txid"], int(vin[0]["vout"]))
        if under is None or under["scriptPubKey"]["hex"] != htlclib.p2sh_script(redeem).hex():
            raise ValueError("that spend takes a coin that is not under this lock")
        pays = {o["scriptPubKey"]["hex"] for o in decoded["vout"]}
        from arcade.txbuild import p2pkh_script
        if pays != {p2pkh_script(owner).hex()}:
            raise ValueError("that spend pays somebody else")
        kind = "refund" if said.get("kind") == "refund" else "claim"
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
