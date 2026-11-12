"""Arcade ledger tokens and inscriptions an account sends, built as the original builds them (a genuine Omni Simple
Send, unwrapped; an inscription Transfer; each with a dust reference output to the recipient), with balances, what is
already leaving, token properties and owners read from Arcade V1.0.

    POST /account/token/send   {chain?, property_id, amount | units, to | tip_of}
    POST /account/nft/send     {chain?, piece, to}
"""
from __future__ import annotations

import re
from typing import Any

from fastapi import Body, Request
from fastapi.responses import JSONResponse

from arcade import fees
from arcade import funding as fundinglib
from arcade import inscriptions as inscriptionlib
from arcade import tags as taglib
from arcade import tokens as tokenlib
from arcade import txbuild
from arcade.ledger import AmountError, format_amount, parse_amount
from arcade.messaging import sender as sendermod

from .actions import class_c_or_b
from .v1 import V1Error
from .wallet import CoinDB


def register(app, st, chains):
    v1 = st.v1
    offers, quota = app.state.offers, app.state.quota

    def coin_pubkey(pubkey: str, chain) -> bytes:
        try:
            return bytes.fromhex(str(st.setting(chains.coinkey_key(pubkey, chain), "") or ""))
        except ValueError:
            return b""

    def prop_of(chain, pid: int) -> dict | None:
        try:
            if chain.network == "test":
                code, p = v1.page(f"token/{pid}")
                return p if code == 200 and p else None
            p = v1.cmd("explorer.ledger_token", chain=chain.key, id=pid) or {}
            return {"propertyid": pid, "name": p.get("name"), "divisible": p.get("type") == 2,
                    "issuer": p.get("issuer")} if p else None
        except V1Error:
            return None

    def free_units(chain, address: str, pid: int) -> tuple[int, int]:
        """(units held, units already leaving in the mempool) of one token at an address."""
        if chain.network == "test":
            code, rows = v1.page(f"balances/{address}", {"pending": 1})
            row = next((r for r in (rows or []) if r.get("propertyid") == pid), {}) if code == 200 else {}
            return int(row.get("units") or 0), int(row.get("leaving") or 0)
        got = v1.cmd("tokens", owner=address, chain=chain.key) or {}
        rows = got if isinstance(got, list) else got.get("tokens") or got.get("items") or []
        row = next((r for r in rows if int(r.get("propertyid") or r.get("id") or -1) == pid), {})
        return int(row.get("units") or row.get("balance_units") or 0), 0

    def chain_asked(said: dict):
        one = chains.by_network(str(said.get("chain") or "test").strip() or "test")
        if one is None:
            raise ValueError(f"this arcade keeps no {said.get('chain')} wallet")
        return one

    @app.post("/account/token/send")
    def token_send(request: Request, payload: Any = Body(None)):
        account = app.state.signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        try:
            chain = chain_asked(said)
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        address = chains.account_address(account.pubkey, chain)
        if not address:
            return JSONResponse({"detail": f"this account has no {chain.label} address yet"}, status_code=400)
        try:
            try:
                property_id = int(said.get("property_id"))
            except (TypeError, ValueError):
                raise ValueError("the token number is not a number") from None
            prop = prop_of(chain, property_id)
            if prop is None:
                raise tokenlib.TokenError(f"there is no token {property_id}.")
            divisible = bool(prop.get("divisible"))
            tip_of = str(said.get("tip_of", "") or "").strip().lower()
            if tip_of:
                if not re.fullmatch(r"[0-9a-f]{64}", tip_of):
                    raise ValueError("that is not a transaction id")
                to, to_tag = app.state.pays_at(app.state.post_author(tip_of), chain), ""
                if not to:
                    raise ValueError(f"there is nowhere to send it: whoever wrote that has not published a "
                                     f"{chain.label} address")
            else:
                to, to_tag = app.state.payee(said.get("to", ""), chain)
            complaint = chains.check_address(to, chain)
            if complaint:
                raise ValueError(complaint)
            if to == address:
                raise ValueError("that is this account's own address")
            if said.get("units") is not None:
                amount = int(said["units"])
                if amount <= 0:
                    raise ValueError("an amount above zero")
            else:
                amount = parse_amount(str(said.get("amount", "")), divisible)
            held, going = free_units(chain, address, property_id)
            if amount > held - going:
                raise tokenlib.TokenError(
                    f"only {format_amount(max(0, held - going), divisible)} of {prop['name']} is here to send"
                    + (f" ({format_amount(going, divisible)} more is already on its way out, waiting for its block)"
                       if going else "") + ".")
            body = tokenlib.send_payload(property_id, amount)
            outputs = class_c_or_b(chain, address, body, coin_pubkey(account.pubkey, chain), wrap=False)
            outputs.append((sendermod.OUTPUT_VALUE, txbuild.p2pkh_script(to)))
            unsigned = fundinglib.build(CoinDB(v1, chain), chain.params, address, outputs, rate=fees.MIN_FEE_PER_KB,
                                        what=f"send {format_amount(amount, divisible)} {prop['name']} to {to}")
            quota(account, "send")
        except (taglib.TagError, tokenlib.TokenError, fundinglib.FundingError, AmountError, ValueError, V1Error) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)

        def still_here(want=amount):
            """Asked again at broadcast: two sends offered before either went out must not both go."""
            h, g = free_units(chain, address, property_id)
            if want > h - g:
                raise ValueError(f"only {format_amount(max(0, h - g), divisible)} of {prop['name']} is left to send: "
                                 "another send of it is already on its way out, waiting for its block. Nothing was sent.")
        offer = offers.add(account.pubkey, chain.network, unsigned, unsigned.what, check=still_here)
        return JSONResponse({"offer": offer.id, "to": to, "to_tag": to_tag, "amount": amount, "divisible": divisible,
                             "property_id": property_id, "name": prop["name"], "chain": chain.network,
                             **unsigned.as_json()})

    @app.post("/account/nft/send")
    def nft_send(request: Request, payload: Any = Body(None)):
        account = app.state.signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        try:
            chain = chain_asked(said)
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        address = chains.account_address(account.pubkey, chain)
        if not address:
            return JSONResponse({"detail": f"this account has no {chain.label} address yet"}, status_code=400)
        try:
            piece = str(said.get("piece", "")).strip().lower()
            if chain.network == "test":
                code, row = v1.page(f"inscription/{piece}")
                row = row if code == 200 else None
            else:
                got = v1.cmd("explorer.ledger_inscription", chain=chain.key, id=piece) or {}
                row = {"id": got.get("txid") or got.get("id"), "owner": got.get("owner"),
                       "number": got.get("number")} if got else None
            if not row:
                raise ValueError("no such inscription on this node")
            if row.get("owner") != address:
                raise ValueError("that piece is not this account's to send")
            to, to_tag = app.state.payee(said.get("to", ""), chain)
            complaint = chains.check_address(to, chain)
            if complaint:
                raise ValueError(complaint)
            if to == address:
                raise ValueError("that is this account's own address")
            txid = str(row.get("id") or piece).split("i")[0]
            body = inscriptionlib.Transfer(txid=bytes.fromhex(txid)).encode()
            outputs = class_c_or_b(chain, address, body, coin_pubkey(account.pubkey, chain))
            outputs.append((sendermod.OUTPUT_VALUE, txbuild.p2pkh_script(to)))
            unsigned = fundinglib.build(CoinDB(v1, chain), chain.params, address, outputs, rate=fees.MIN_FEE_PER_KB,
                                        what=f"send inscription #{row.get('number')} to {to}")
            quota(account, "send")
        except (taglib.TagError, fundinglib.FundingError, ValueError, V1Error) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        offer = offers.add(account.pubkey, chain.network, unsigned, unsigned.what)
        return JSONResponse({"offer": offer.id, "to": to, "to_tag": to_tag, "number": row.get("number"),
                             "chain": chain.network, **unsigned.as_json()})
