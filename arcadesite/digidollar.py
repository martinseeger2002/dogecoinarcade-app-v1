"""DigiDollar in the Wallet, on Arcade V1.0 (Martin 2026-11-12: "I'm missing ... digi dollar and test DD").

The original's routes, with V1.0 as the index:

    GET  /account/digidollar?chain=&pubkey=   this account's DigiDollar address and coins on a DigiByte chain
                                              (V1.0's dd.coins, for any key: the key stays in the browser)
    POST /account/digidollar/send             an unsigned offer of a DigiDollar send, built as the original built it
                                              (arcade/digidollar.py prepare_send and offer: DigiByte Core's way), every
                                              input read back from the chain and checked to be this account's; the
                                              browser signs it and /account/sign broadcasts it, as any send

DigiByte and its test chain show it whenever V1.0 keeps or borrows them (dd_nets)."""
from __future__ import annotations

import decimal
from typing import Any

from fastapi import Body, Request
from fastapi.responses import JSONResponse

from arcade import digidollar as digidollib
from arcade import funding as fundinglib
from arcade.modhost import ModuleFailed
from arcade.script import b58check_decode, hash160

from .chains import Chain
from .v1 import V1Error
from .wallet import CoinDB


def nets(chains) -> list[str]:
    """The DigiByte chains whose Wallet shows DigiDollar as a coin of its own."""
    return [one.network for one in chains.account_chains() if one.network in digidollib.NETS]


def register(app, st, chains):
    v1 = st.v1

    def no(detail: str, status: int = 400, **extra) -> JSONResponse:
        return JSONResponse({"detail": detail, **extra}, status_code=status, headers={"Cache-Control": "no-store"})

    def dd_chain(network: str) -> Chain | None:
        return next((c for c in chains.account_chains() if c.network == network and c.network in digidollib.NETS), None)

    def owner(account, chain: Chain, pubkey_hex: str) -> tuple[str, bytes] | str:
        """(the account's DigiByte address, its key): the key the browser gave, checked to be that address's; or why not."""
        address = chains.account_address(account.pubkey, chain)
        if not address:
            return f"this account has no {chain.label} address yet: open the Wallet once"
        try:
            pub = bytes.fromhex(str(pubkey_hex or ""))
            ok = len(pub) == 33 and hash160(pub) == b58check_decode(address)[1]
        except Exception:                                   # noqa: BLE001
            ok = False
        if not ok:
            return f"that is not this account's {chain.label} key"
        return address, pub

    def floor_of(chain: Chain) -> int:
        """A borrowed chain's floor: the first block its coins are seen from."""
        try:
            row = next((c for c in v1.cmd("chains.list") or [] if c.get("key") == chain.key), {})
            return int(row.get("floor") or 0)
        except (V1Error, TypeError, ValueError):
            return 0

    @app.get("/account/digidollar")
    def account_digidollar(request: Request, chain: str = "", pubkey: str = ""):
        account = app.state.signed_in_account(request)
        ch = dd_chain(chain)
        if ch is None:
            return no("DigiDollar is on DigiByte, and this arcade has no DigiByte wallet for you")
        if not pubkey:
            pubkey = str(st.setting(chains.coinkey_key(account.pubkey, ch), "") or "").strip()
            if not pubkey:
                return no("unlock your wallet once here, so the arcade knows your DigiByte key", unlock=True)
        got = owner(account, ch, pubkey)
        if isinstance(got, str):
            return no(got)
        try:
            dd = v1.cmd("dd.coins", chain=ch.key, pubkey=got[1].hex()) or {}
        except V1Error as exc:
            return no(f"the arcade could not read DigiDollars: {exc}", 503)
        whole = bool(dd.get("whole"))
        floor = 0 if whole else floor_of(ch)
        coins = [{"txid": c["txid"], "vout": int(c["vout"]), "cents": int(c["cents"]), "height": int(c.get("height") or 0)}
                 for c in dd.get("coins") or []]
        return JSONResponse({"chain": ch.network, "address": dd.get("address") or "", "cents": sum(c["cents"] for c in coins),
                             "coins": coins, "since": max(floor - 1, 0), "whole": whole, "from_block": floor,
                             "earlier": "", "borrowed": bool(dd.get("borrowed"))}, headers={"Cache-Control": "no-store"})

    @app.post("/account/digidollar/send")
    def account_digidollar_send(request: Request, payload: Any = Body(None)):
        account = app.state.signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        ch = dd_chain(str(said.get("chain") or ""))
        if ch is None:
            return no("DigiDollar is on DigiByte, and this arcade has no DigiByte wallet for you")
        got = owner(account, ch, str(said.get("pubkey") or ""))
        if isinstance(got, str):
            return no(got)
        address, pub = got
        try:
            text = str(said.get("amount") or "").strip().lstrip("$").replace(",", "")
            dollars = decimal.Decimal(text)
            if dollars <= 0 or dollars != dollars.quantize(decimal.Decimal("0.01")):
                raise ValueError("an amount of dollars and cents, more than zero")
            cents = int(dollars * 100)
            to = str(said.get("to") or "").strip()
            if not to or not to.isalnum():
                raise ValueError("pay a DigiDollar address (DD... or TD...)")
            dd = v1.cmd("dd.coins", chain=ch.key, pubkey=pub.hex()) or {}
            held = [{"txid": c["txid"], "vout": int(c["vout"]), "cents": int(c["cents"])} for c in dd.get("coins") or []]
            dgb = [{"txid": c["txid"], "vout": c["vout"], "value": c["value"]} for c in CoinDB(v1, ch).unspent(address)]
            change = fundinglib.spend_for(address, ch.params)["change_script"]
            prepared = digidollib.prepare_send(ch.network, pub, held, dgb, change, to, cents)
            unsigned = digidollib.offer(prepared, ch.params, lambda method, params: v1.core(ch.network, method, *params),
                                        address, pub)
            app.state.quota(account, "send")
        except decimal.InvalidOperation:
            return no("type an amount of dollars, like 12.50")
        except (ValueError, ArithmeticError, ModuleFailed, fundinglib.FundingError) as exc:
            return no(str(exc))
        except V1Error as exc:
            return no(f"the arcade could not build it: {exc}", 503)
        offer = app.state.offers.add(account.pubkey, ch.network, unsigned, unsigned.what)
        return JSONResponse({"offer": offer.id, "chain": ch.network, **unsigned.as_json()},
                            headers={"Cache-Control": "no-store"})
