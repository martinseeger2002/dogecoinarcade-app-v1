"""Sending one ord inscription (a Doginal, a Pepinal, ...) from the website's wallet (2026-11-12, Martin: "start on ORD
transfers now. Do the website first").

    POST /account/doginal/send   {chain, txid, vout, inscription, to, from?, pubkey?}  -> an offer to sign

The original arcade's send (arcade/web/app.py account_doginal_send, funding.build_doginal_send), read from Arcade V1.0
instead of a local ord index: the inscribed coin is input 0 and goes WHOLE to the recipient as output 0, so the
inscription's first koinu is the recipient's first; clean coins (V1.0's tx.coins, which never lists one carrying an
inscription) pay the fee; change last, back to the sender. Nothing is broadcast here: the browser checks the offer is
the send it asked for (my_wallet.html mismatch), signs, and /account/sign broadcasts.

Refused unless V1.0's ord index says, now:
  - the inscription sits on exactly that coin, at offset 0, owned by the sending address;
  - that coin carries no other inscription (sending it would hand those over too; splitting one off comes later);
  - the coin is the sender's (V1.0 lists it among the address's held-back coins), and is not standing in a listing.
From the account's own address, or one of its imported wallets' ordinary (legacy) addresses, signed by that wallet's
key in the browser (`from`, `pubkey`, as /account/token20/send).
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import Body, Request
from fastapi.responses import JSONResponse

from arcade import fees
from arcade import tags as taglib
from arcade import funding as fundinglib
from arcade import txbuild
from arcade.script import hash160

from .doginals import ORD_CHAINS
from .v1 import V1Error
from .wallet import CoinDB

WORDS = {"doge": "Doginal", "main": "Pepinal", "dgb": "DigiByte ord", "flop": "Flopinal", "wjk": "Wojakinal",
         "kot": "Kotia ord", "ltc": "Litecoin ord", "bel": "Bellinal", "lky": "Luckinal", "test": "inscription"}


def exactly_this(ins: dict, outputs: list, txid: str, vout: int, inscription: str, address: str) -> str:
    """Why this coin may not carry `inscription` away on its own ("" when it may)."""
    at = str(ins.get("at") or "").split(":")
    if len(at) != 3 or at[0] != txid or at[1] != str(vout):
        return "that inscription is not on that coin any more (it has moved): read your wallet again"
    if at[2] != "0":
        return (f"that inscription sits {at[2]} koinu into its coin, not at its start: sending it plainly could hand "
                "the wrong piece away, so it is not sent")
    if ins.get("owner") and ins["owner"] != address:
        return "that inscription is not this wallet's"
    out = next((o for o in outputs if int(o.get("vout", -1)) == vout), None)
    if out is None:
        return "that coin is not in its transaction"
    on = [str(i) for i in (out.get("inscriptions") or [])]
    if inscription not in on:
        return "the index has not read that inscription on that coin yet: try again in a minute"
    others = [i for i in on if i != inscription]
    if others:
        return (f"that coin carries {len(on)} inscriptions: sending it would hand all of them over. Sending one of "
                "them on its own is not ready yet")
    return ""


def build_send(v1, chain, address: str, coin: dict, to: str, what: str) -> fundinglib.Unsigned:
    """The inscribed coin first, whole to `to`; clean coins pay the fee; change last (creator.note_send's shape)."""
    params = chain.params
    db = CoinDB(v1, chain)
    paid = [(int(coin["value"]), txbuild.p2pkh_script(to))]
    keep_out = {(coin["txid"], int(coin["vout"]))}
    rate = fees.rate_for(params, fees.MIN_FEE_PER_KB)
    net = getattr(params, "name", None)
    fee = fundinglib.price(2, paid, rate, change=True, params=params)
    fees_in = fundinglib.choose(db, address, fee, exclude=keep_out, network=net)
    fee = fundinglib.price(1 + len(fees_in), paid, rate, change=True, params=params)
    if sum(c["value"] for c in fees_in) < fee:
        fees_in = fundinglib.choose(db, address, fee, exclude=keep_out, network=net)
        fee = fundinglib.price(1 + len(fees_in), paid, rate, change=True, params=params)
    if any((c["txid"], int(c["vout"])) == (coin["txid"], int(coin["vout"])) for c in fees_in):
        raise fundinglib.FundingError("the inscribed coin cannot pay its own fee")
    chosen = [dict(coin, vout=int(coin["vout"]), address=address)] + fees_in
    change = sum(c["value"] for c in fees_in) - fee
    outputs = list(paid)
    if change >= fees.dust_limit(params):
        outputs.append((change, txbuild.p2pkh_script(address)))
    else:
        fee += max(0, change)
        change = 0
    raw = fundinglib.build_raw_tx([(c["txid"], c["vout"]) for c in chosen], outputs)
    script = txbuild.p2pkh_script(address)
    hashes = [fundinglib.sighash(chosen, outputs, n, script).hex() for n in range(len(chosen))]
    return fundinglib.Unsigned(raw=raw, inputs=chosen, outputs=outputs, sighashes=hashes, fee=fee,
                               change=change, what=what)


def register(app, st, chains):
    v1 = st.v1
    offers = app.state.offers

    def held_coin(chain, address: str, txid: str, vout: int) -> dict | None:
        """The coin, from the address's held-back coins (the ones carrying inscriptions) as V1.0 lists them."""
        got = (v1.cmd("tx.coins", addresses=[address], chain=chain.key) or {}).get("addresses") or {}
        a = got.get(address) or {}
        for c in (a.get("held_back") or []) + (a.get("confirmed") or []):
            if str(c.get("txid")) == txid and int(c.get("vout", -1)) == vout:
                return {"txid": txid, "vout": vout, "value": int(c["value"]), "address": address}
        return None

    @app.post("/account/doginal/send")
    def account_doginal_send(request: Request, payload: Any = Body(None)):
        account = app.state.signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        chain = None
        try:
            network = str(said.get("chain") or "")
            chain = next((c for c in chains.account_chains() if c.network == network), None)
            if chain is None or network not in ORD_CHAINS:
                raise ValueError("inscriptions are sent on the chains whose ord index this arcade keeps")
            word = WORDS.get(network, "inscription")
            address = chains.account_address(account.pubkey, chain)
            frm = str(said.get("from") or "").strip()
            if frm and frm != address:
                from .imported import entry as imported_entry
                e = imported_entry(st, chain.network, account.pubkey, frm)
                if e is None:
                    raise ValueError("that address is not one of this account's imported wallets")
                if str(e.get("k") or "p2pkh") != "p2pkh":
                    raise ValueError(f"a {word} is sent from an imported wallet's ordinary (legacy) address only")
                try:
                    key = bytes.fromhex(str(said.get("pubkey") or ""))
                except ValueError:
                    key = b""
                if len(key) != 33 or txbuild.p2pkh_script(frm) != b"\x76\xa9\x14" + hash160(key) + b"\x88\xac":
                    raise ValueError("that is not the key of that wallet's address")
                address = frm
            if not address:
                raise ValueError(f"this account has no {chain.label} address yet")
            txid = str(said.get("txid") or "").lower()
            if not re.fullmatch(r"[0-9a-f]{64}", txid):
                raise ValueError("that is not a transaction id")
            try:
                vout = int(said.get("vout"))
            except (TypeError, ValueError):
                raise ValueError("which output of that transaction?") from None
            inscription = str(said.get("inscription") or "").strip().lower()
            if not re.fullmatch(r"[0-9a-f]{64}i\d+", inscription):
                raise ValueError(f"that is not a {word}'s id")
            listed = getattr(app.state, "listed_coins", None)
            if listed and (txid, vout) in listed(chain.network, address):
                raise ValueError(f"that {word} is listed for sale: unlist it before sending it")
            coin = held_coin(chain, address, txid, vout)
            if coin is None:
                raise ValueError("that coin is not this wallet's to spend")
            ins = v1.cmd("explorer.inscription", chain=chain.key, id=inscription) or {}
            tx = v1.cmd("explorer.tx", chain=chain.key, txid=txid) or {}
            bad = exactly_this(ins, tx.get("outputs") or [], txid, vout, inscription, address)
            if bad:
                raise ValueError(bad)
            to, to_tag = app.state.payee(said.get("to", ""), chain)
            complaint = chains.check_address(to, chain)
            if complaint:
                raise ValueError(complaint)
            if to == address:
                raise ValueError("that is this wallet's own address")
            unsigned = build_send(v1, chain, address, coin, to, f"send {word} {inscription} to {to}")
            app.state.quota(account, "send")
        except V1Error as exc:
            where = f"{chain.label} " if chain else ""
            return JSONResponse({"detail": f"the {where}ord index can't be read right now: {exc}"}, status_code=503)
        except (fundinglib.FundingError, taglib.TagError, ValueError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        offer = offers.add(account.pubkey, chain.network, unsigned, unsigned.what)
        return JSONResponse({"offer": offer.id, "to": to, "to_tag": to_tag, "inscription": inscription,
                             "txid": txid, "vout": vout, "amount": coin["value"], "chain": chain.network,
                             **unsigned.as_json()})
