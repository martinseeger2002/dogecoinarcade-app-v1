"""An account's wallet: what it holds and how it pays, with every coin read from Arcade V1.0's address index.

The original built transactions from its own coin table (arcade/utxos.py) through arcade/funding.py. The builders are
used here unchanged: `CoinDB` answers the coin table's two queries from V1.0's tx.coins, so every funding.build*
picks coins the same way it always has. Coins carrying an inscription are kept out by V1.0 (tx.coins `held_back`).

    GET  /account                   what the page draws (the original's fields)
    GET  /account/coins             one chain's coins of the account
    GET  /account/activity          what changed (tx.activity)
    POST /account/send              build a payment for the browser to sign (nothing broadcast)
    POST /account/sign              the browser's signatures -> checked against the offer -> tx.broadcast
"""
from __future__ import annotations

import threading
import time
import unicodedata
from typing import Any

from fastapi import Body, Request
from fastapi.responses import JSONResponse

from arcade import accounts as accountslib
from arcade import fees
from arcade import funding as fundinglib
from arcade import tags as taglib
from arcade import txbuild
from arcade.ledger import AmountError, format_amount, parse_amount
from arcade.script import hash160, segwit_decode
from arcade.web import account as accountlib

from . import imported as importedlib
from .chains import Chain
from .v1 import V1, V1Error

COIN_SQL = "SELECT txid, vout, address, value, height FROM utxo WHERE address = ? ORDER BY value DESC, txid"
BALANCE_SQL = "SELECT COALESCE(SUM(value), 0) FROM utxo WHERE address = ?"
#: The offers that put the account's announcement out (actions.py).
ANNOUNCING = ("publish your messaging key", "publish your profile")
#: How long an announcement sent here counts as on its way without a block.
ANNOUNCING_FOR = 3600


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def __iter__(self):
        return iter(self.rows)


class _Conn:
    def __init__(self, db: "CoinDB"):
        self.db = db

    def execute(self, sql: str, params=()):
        sql = " ".join(sql.split())
        if sql == COIN_SQL:
            return _Rows(self.db.unspent(params[0]))
        if sql == BALANCE_SQL:
            return _Rows([(sum(c["value"] for c in self.db.unspent(params[0])),)])
        raise RuntimeError(f"the site's coin table answers only the coin queries, not: {sql[:80]}")


class CoinDB:
    """What funding.build reads as `db`: an address's spendable coins, from V1.0 (confirmed, and waiting outputs
    nothing spends yet), never one carrying an inscription. One V1.0 call per address per CoinDB."""

    def __init__(self, v1: V1, chain: Chain):
        self.v1, self.chain = v1, chain
        self.conn = _Conn(self)
        self._kept: dict[str, list[dict]] = {}

    def coins_of(self, addresses: list[str]) -> dict:
        got = self.v1.cmd("tx.coins", addresses=list(addresses), chain=self.chain.key)
        return (got or {}).get("addresses") or {}

    def unspent(self, address: str) -> list[dict]:
        if address not in self._kept:
            a = self.coins_of([address]).get(address) or {}
            rows = [{"txid": c["txid"], "vout": int(c["vout"]), "address": address, "value": int(c["value"]),
                     "height": int(c.get("height") or 0)}
                    for c in (a.get("confirmed") or []) + (a.get("unconfirmed") or [])]
            rows.sort(key=lambda r: (-r["value"], r["txid"]))
            self._kept[address] = rows
        return [dict(r) for r in self._kept[address]]


def register(app, st, chains):
    v1: V1 = st.v1
    offers = accountlib.Offers()
    app.state.offers = offers

    def unlimited(address: str) -> bool:
        listed = st.setting("unlimited_accounts", []) or []
        return bool(address) and isinstance(listed, list) and address in [str(x).strip() for x in listed]

    def quota(account, kind: str, nbytes: int = 0, count: bool = True) -> None:
        """The original's _quota: the pile of unsigned offers, then the allowance for the action (accounts.py)."""
        if unlimited(chains.account_address(account.pubkey, chains.account_chain())):
            return
        if len(offers.waiting(account.pubkey)) >= accountslib.OFFERS_WAITING:
            raise ValueError("this node is still waiting to hear about the other offers it made you. Sign one or let "
                             "a few minutes go by and ask again -- each one names specific coins, and two of them "
                             "spending the same coin cannot both go.")
        if count:
            try:
                st.accounts().charge(account.pubkey, kind, nbytes, caps=accountslib.limits(st.settings()))
            except accountslib.AccountError as exc:         # an allowance used up: a refusal, said as one (400)
                raise ValueError(str(exc)) from None

    app.state.quota = quota
    lanes: set[str] = set()
    lanes_lock = threading.Lock()

    def signed_in(request):
        return app.state.signed_in(request)

    def signed_in_account(request):
        return app.state.signed_in_account(request)

    def chain_asked(said: dict) -> Chain:
        net = str(said.get("chain") or "test").strip() or "test"
        one = chains.by_network(net)
        if one is None:
            raise ValueError(f"this arcade keeps no {net} wallet")
        return one

    def chain_row(one: Chain) -> dict:
        return {"network": one.network, "label": one.label, "title": getattr(one.params, "title", one.label),
                "version": one.params.pubkeyhash_version, "p2sh": one.params.scripthash_version,
                "mainnet": bool(one.is_mainnet), "tags": one.network == "test"}

    def balances(one: Chain, address: str) -> dict:
        """The original's balance fields for one address, from tx.coins."""
        a = CoinDB(v1, one).coins_of([address]).get(address) or {}
        confirmed = sum(int(c["value"]) for c in a.get("confirmed") or [])
        waiting = sum(int(c["value"]) for c in a.get("unconfirmed") or [])
        held = sum(int(c["value"]) for c in a.get("held_back") or [])
        return {"balance": confirmed + held, "incoming": waiting, "arriving": 0, "leaving": 0,
                "held": 0, "inscribed": held, "spendable": confirmed + waiting, "watching": 0}

    # ---------------------------------------------------------------- what the page draws
    @app.get("/account")
    def account_state(request: Request):
        account = signed_in(request)
        listed = chains.account_chains()
        rows = [chain_row(one) for one in listed]
        if account is None:
            return JSONResponse({"pubkey": None, "chains": rows})
        if request.query_params.get("only") == "addresses":
            return JSONResponse({"pubkey": account.pubkey,
                                 "chains": [{**row, "address": chains.account_address(account.pubkey, one)}
                                            for row, one in zip(rows, listed)]})
        home = chains.account_chain()
        mine = st.vault().by_pubkey(account.pubkey) or {}
        address = chains.account_address(account.pubkey, home)
        said: dict[str, Any] = {
            "mesh_node": "", "swap_big": {}, "delegate_cap": {},
            "pubkey": account.pubkey, "network": home.network, "version": home.params.pubkeyhash_version,
            "address": address, "balance": 0, "watching": None, "tag": "", "announced": False, "announcing": False,
            "profile": {"pfp": "", "bio": "", "url": ""}, "name": mine.get("tag", ""),
            "mail_from": int(st.setting(f"mail_from:{account.pubkey}", 0) or 0), "claiming": mine.get("claimed", ""),
        }
        if address:
            def ask(fn, default=None):
                try:
                    return fn()
                except V1Error:
                    return default                          # an arcade still catching up says nothing
            got = ask(lambda: balances(home, address))
            if got:
                said.update(got)
            said["tag"] = ask(lambda: str((v1.page(f"address/{address}")[1] or {}).get("tag") or ""), "")
            who = ask(lambda: v1.cmd("msg.who", name=address) or {}, {})
            said["announced"] = bool(who.get("key"))
            sent = int(st.setting(f"announcing:{account.pubkey}", 0) or 0)
            said["announcing"] = not said["announced"] and time.time() - sent < ANNOUNCING_FOR
            prof = ask(lambda: (v1.cmd("profile", who=address) or {}).get("profile") or {}, {})
            said["profile"] = {f: prof.get(f) or "" for f in ("pfp", "bio", "url")}
        asked = {n.strip() for n in str(request.query_params.get("chains", "")).split(",") if n.strip()}
        everyone = "all" in asked
        asked |= {home.network}
        said["chains"] = []
        for row, one in zip(rows, listed):
            here = chains.account_address(account.pubkey, one)
            row = {**row, "coin": getattr(one.params, "coin", ""), "address": here, "balance": 0, "incoming": 0,
                   "watching": None, "backed_up": st.setting(f"mainnet:{account.pubkey}") == "yes", "lock_fee": {}}
            if not everyone and one.network not in asked:
                row.update({"lazy": True, "balance": None, "incoming": None, "spendable": None})
            elif here:
                try:
                    row.update(balances(one, here))
                except V1Error:
                    pass
            said["chains"].append(row)
        said["hidden"] = st.setting(f"wallets_hidden:{account.pubkey}", []) or []
        said["quota"] = st.accounts().room(account.pubkey, caps=accountslib.limits(st.settings()))
        said["boost"] = None
        return JSONResponse(said)

    @app.get("/account/coins")
    def account_coins(request: Request, chain: str = "", address: str = "", spendable: int = 0):
        account = signed_in_account(request)
        try:
            one = chain_asked({"chain": chain})
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        own = chains.account_address(account.pubkey, one)
        address = address or own
        if address != own and importedlib.entry(st, one.network, account.pubkey, address) is None:
            return JSONResponse({"detail": "only this account's own addresses"}, status_code=403)
        a = CoinDB(v1, one).coins_of([address]).get(address) or {}
        rows = [{"txid": c["txid"], "vout": int(c["vout"]), "value": int(c["value"]),
                 "height": int(c.get("height") or 0), "address": address}
                for c in (a.get("confirmed") or []) + (a.get("unconfirmed") or [])]
        if not spendable:
            rows += [{"txid": c["txid"], "vout": int(c["vout"]), "value": int(c["value"]),
                      "height": int(c.get("height") or 0), "address": address, "held": True}
                     for c in a.get("held_back") or []]
        return JSONResponse({"chain": one.network, "address": address, "doge": one.network == "doge",
                             "ltc": one.network == "ltc", "ltctest": one.network == "ltctest", "coins": rows})

    @app.get("/account/activity")
    def account_activity(request: Request, chain: str = "", since: int = 0, address: str = "", before: int = 0):
        account = signed_in_account(request)
        try:
            one = chain_asked({"chain": chain})
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        own = chains.account_address(account.pubkey, one)
        address = address.strip()
        if address and address != own and importedlib.entry(st, one.network, account.pubkey, address) is None:
            return JSONResponse({"detail": "only this account's own addresses"}, status_code=403)
        address = address or own
        if not address:
            return JSONResponse({"chain": one.network, "rows": [], "waiting": [], "height": 0})
        got = v1.cmd("tx.activity", addresses=[address], since=int(since), chain=one.key) or {}
        mine = (got.get("addresses") or {}).get(address) or {}
        tip = int(got.get("height") or 0)
        rows, more = activity_page(mine.get("confirmed") or [], tip, before, one, address)
        return JSONResponse({"chain": one.network, "address": address, "rows": rows, "more": more,
                             "tip": tip, "waiting": mine.get("waiting") or [], "height": tip})

    ACTIVITY_PAGE = 50
    block_times: dict[tuple[str, int], int] = {}

    def block_time(one: Chain, height: int) -> int:
        """A block's time, from V1's explorer header row (cheap: no block read). Kept, blocks don't change time."""
        key = (one.key, height)
        if key not in block_times:
            try:
                rows = v1.cmd("explorer.blocks", chain=one.key, before=height + 1, n=1) or []
                block_times[key] = int(rows[0].get("time") or 0) if rows and int(rows[0].get("height", -1)) == height else 0
            except V1Error:
                return 0
            if len(block_times) > 20000:
                block_times.clear()
        return block_times[key]

    def activity_page(raw: list, tip: int, before: int, one: Chain, address: str) -> tuple[list, bool]:
        """V1's tx.activity rows are {height, txid, received, spent}, oldest first; the wallet page lists
        {net, time, confirmations, other, other_tag, fee}, newest first, a page at a time (before = a height)."""
        rows = sorted(raw, key=lambda r: int(r.get("height") or 0), reverse=True)
        if before:
            rows = [r for r in rows if int(r.get("height") or 0) < before]
        page, more = rows[:ACTIVITY_PAGE], len(rows) > ACTIVITY_PAGE
        out = []
        for r in page:
            h = int(r.get("height") or 0)
            net = int(r.get("received") or 0) - int(r.get("spent") or 0)
            row = {"txid": r.get("txid"), "height": h, "net": net, "spent": int(r.get("spent") or 0),
                   "time": block_time(one, h), "confirmations": max(0, tip - h + 1) if tip and h else 0}
            if net < 0 and r.get("txid"):
                # Who it went to: the first output that is not this address (none: only data or our own change).
                try:
                    tx = v1.cmd("explorer.tx", chain=one.key, txid=r["txid"]) or {}
                except V1Error:
                    tx = {}
                row["fee"] = tx.get("fee")
                for o in tx.get("outputs") or []:
                    if o.get("address") and o["address"] != address:
                        row["other"], row["other_tag"] = o["address"], o.get("tag")
                        break
            out.append(row)
        return out, more

    # ---------------------------------------------------------------- paying
    def where_to_pay(tag: str, one: Chain) -> str:
        """A @tag's address on the chain being paid: on Pepecoin testnet the claim itself; elsewhere the address the
        holder published for that chain (their profile's linked addresses, D-032)."""
        if one.network == "test":
            owner = v1.tag_owner(tag)
            if not owner:
                raise ValueError(f"@{tag} is not claimed")
            return owner
        prof = (v1.cmd("profile", who=f"@{tag}") or {}).get("profile") or {}
        for link in prof.get("linked") or []:
            if str(link.get("chain") or "") in (one.key, one.network):
                return str(link.get("address") or "")
        raise ValueError(f"@{tag} has not published a {one.label} address")

    def payee(raw: Any, one: Chain) -> tuple[str, str]:
        text = unicodedata.normalize("NFKC", str(raw or ""))
        text = "".join(ch for ch in text if unicodedata.category(ch) not in ("Cf", "Zs") or ch == " ").strip()
        if not text:
            raise ValueError("who is it to? Type their @name or an address.")
        if text.startswith("@") or not (25 <= len(text) <= 90 and text.isalnum()):
            tag = taglib.validate(text.lstrip("@"))
            return where_to_pay(tag, one), tag
        return text, ""

    app.state.payee = payee

    def pay_script(to: str, one: Chain) -> bytes:
        hrp = getattr(one.params, "bech32_hrp", "")
        if hrp and to.lower().startswith(hrp + "1"):
            version, program = segwit_decode(hrp, to.lower())
            if version != 0:
                raise ValueError("that is not a native SegWit address for this chain")
            return bytes([0x00, len(program)]) + program
        return txbuild.p2pkh_script(to, one.params)

    @app.post("/account/address")
    def account_address(request: Request, payload: Any = Body(None)):
        """File this account's address on one chain (the original's route; wallet_js tellTheNode asks it for every
        listed chain the account has no address on yet, so a coin added later gets the account's address the next time
        the wallet opens). Checked, never derived here: the chain's own version byte, never another account's, and a
        first claim proved by that address's own key (wallet_js claimProof). V1.0 indexes every address, so nothing is
        watched here."""
        from arcade import wallet_imports
        account = signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        address = str(said.get("address", "")).strip()
        try:
            one = chain_asked(said)
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        complaint = chains.check_address(address, one)
        if complaint:
            return JSONResponse({"detail": complaint}, status_code=400)
        if chains.address_held_by_another(account.pubkey, one, address):
            return JSONResponse({"detail": "that address is another account's on this arcade"}, status_code=409)
        coin = str(said.get("coin_pubkey", "")).strip().lower()
        if address != chains.account_address(account.pubkey, one):
            try:
                wallet_imports.check_proof(one.params, account.pubkey, one.network, address, coin, "p2pkh",
                                           str(said.get("address_signature") or ""))
            except wallet_imports.ImportRefused as exc:
                return JSONResponse({"detail": f"that address was not proved to be this account's: {exc}"},
                                    status_code=400)
        st.set_setting(chains.address_key(account.pubkey, one), address)
        if coin:
            st.set_setting(chains.coinkey_key(account.pubkey, one), coin)
        return JSONResponse({"address": address, "chain": one.network})

    @app.post("/account/send")
    def account_send(request: Request, payload: Any = Body(None)):
        account = signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        try:
            one = chain_asked(said)
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        address = chains.account_address(account.pubkey, one)
        if not address:
            return JSONResponse({"detail": f"this account has no {one.label} address yet"}, status_code=400)
        keyhash = None
        want_from = str(said.get("from") or "").strip()
        if want_from not in ("", address):
            # An imported wallet of this account: only an address it registered with its key's signature. Its coins
            # only, its change back to it; the browser signs with that wallet's key.
            imported = importedlib.entry(st, one.network, account.pubkey, want_from)
            if imported is None:
                return JSONResponse({"detail": "that address is not one of this account's wallets"}, status_code=400)
            if imported["k"] == "p2sh-p2wpkh":
                try:
                    keyhash = hash160(bytes.fromhex(imported["p"]))
                except ValueError:
                    return JSONResponse({"detail": "import this wallet again: its key was not kept here"},
                                        status_code=400)
            address = want_from
        try:
            to, to_tag = payee(said.get("to", ""), one)
            complaint = chains.check_address(to, one, segwit_ok=True)
            if complaint:
                raise ValueError(complaint)
            amount = parse_amount(str(said.get("amount", "")), True)
            if amount < fees.dust_limit(one.params):
                raise ValueError(f"a payment this small is dust: at least "
                                 f"{format_amount(fees.dust_limit(one.params), True)}")
            unsigned = fundinglib.build(CoinDB(v1, one), one.params, address, [(amount, pay_script(to, one))],
                                        rate=fees.MIN_FEE_PER_KB,
                                        what=f"send {format_amount(amount, True)} {one.label} to {to}",
                                        keyhash=keyhash)
            quota(account, "send")
        except (taglib.TagError, fundinglib.FundingError, AmountError, accountslib.AccountError, ValueError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        offer = offers.add(account.pubkey, one.network, unsigned, unsigned.what)
        return JSONResponse({"offer": offer.id, "to": to, "to_tag": to_tag, "amount": amount, "chain": one.network,
                             "label": one.label, **unsigned.as_json()})

    def same_as_offered(decoded: dict, unsigned) -> None:
        spent = {(vin.get("txid"), int(vin.get("vout", -1))) for vin in decoded.get("vin", [])}
        if spent != {(coin["txid"], coin["vout"]) for coin in unsigned.inputs}:
            raise ValueError("that transaction does not spend what was offered")
        outs = [(int(round(float(o.get("value", 0)) * 100_000_000)), (o.get("scriptPubKey") or {}).get("hex", ""))
                for o in decoded.get("vout", [])]
        if outs != [(value, script.hex()) for value, script in unsigned.outputs]:
            raise ValueError("that transaction does not pay what was offered")

    @app.post("/account/sign")
    def account_sign(request: Request, payload: Any = Body(None)):
        account = signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        lane = f"account {account.pubkey}"
        with lanes_lock:
            if lane in lanes:
                return JSONResponse({"detail": "one of your transactions is still going. Wait for it to be broadcast "
                                               "and try again -- signing two at once from one wallet spends the same "
                                               "coin twice."}, status_code=409)
            lanes.add(lane)
        try:
            try:
                offer = offers.take(str(said.get("offer", "")), account.pubkey)
                sigs = said.get("signatures")
                if not isinstance(sigs, list):
                    raise ValueError("the signatures are not a list")
                signed = fundinglib.assemble(offer.unsigned, [str(x) for x in sigs],
                                             bytes.fromhex(str(said.get("pubkey", ""))))
            except (accountlib.OfferError, fundinglib.FundingError, ValueError) as exc:
                return JSONResponse({"detail": str(exc)}, status_code=400)
            one = chains.by_network(offer.network)
            try:
                same_as_offered(v1.core(one.network, "decoderawtransaction", signed), offer.unsigned)
                if offer.check:
                    offer.check()
                got = v1.cmd("tx.broadcast", hex=signed, chain=one.key) or {}
                txid = str(got.get("txid") or "")
            except ValueError as exc:
                return JSONResponse({"detail": str(exc)}, status_code=400)
            except V1Error as exc:
                return JSONResponse({"detail": f"the node refused it: {exc}"}, status_code=409)
            if offer.done is not None:
                try:
                    offer.done(txid)
                except Exception as exc:                    # noqa: BLE001
                    return JSONResponse({"detail": f"it went out ({txid}) but this site could not write it down: "
                                                   f"{exc}."}, status_code=500)
            if offer.what in ANNOUNCING:
                # Out, not in a block: /account says "announcing" so the page does not publish again (V1.0's msg.who
                # reads mined announcements only; the original read its own pool).
                st.set_setting(f"announcing:{account.pubkey}", int(time.time()))
            if offer.what.startswith("claim @"):
                try:
                    st.vault().note_claim(offer.what[len("claim @"):], txid)
                except Exception:                           # noqa: BLE001
                    pass
            return JSONResponse({"txid": txid, "what": offer.what})
        finally:
            with lanes_lock:
                lanes.discard(lane)
