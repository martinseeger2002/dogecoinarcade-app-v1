"""An account's imported wallets: addresses it registered with each key's signature (wallet_imports.check_proof),
listed per chain in the settings as the original kept them (`extra_address:<network>:<pubkey>`).

The original had to read each new address's coins back from the chain (rescans, shared walks, asking other arcades),
so an import waited. Arcade V1.0 indexes every address, so an import is ready the moment it is registered: its coins
are tx.coins, its history tx.activity. Nothing to rescan, nothing ever busy.

    GET  /account/imported              (sidecar.py) the sealed record + each chain's imported addresses here
    POST /account/imported/address      register one (key proof, CAP_PER_CHAIN, PER_DAY)
    POST /account/imported/forget       take one off (its coins stay where they are)
    POST /account/imported/rescan       "ready" (V1.0 has every coin)
    GET  /account/imported/busy         never busy
"""
from __future__ import annotations

import threading
import time
from typing import Any

from fastapi import Body, Request
from fastapi.responses import JSONResponse

from arcade import wallet_imports as importslib

READY = {"state": "ready", "says": "Ready: this arcade has this wallet's coins."}


def entries(st, network: str, pubkey: str) -> list[dict]:
    return importslib.entries(st.setting(importslib.setting_key(network, pubkey), []))


def entry(st, network: str, pubkey: str, address: str) -> dict | None:
    return next((e for e in entries(st, network, pubkey) if e["a"] == address), None)


def view(st, one, pubkey: str, opened: set | None = None) -> list[dict]:
    """One chain's imported addresses with balances (V1.0 tx.coins, one call for all of them). `opened`: only those
    (network, address) pairs are read; the rest come back lazy, as the original's ?lazy=1."""
    listed = entries(st, one.network, pubkey)
    wanted = [e["a"] for e in listed if opened is None or (one.network, e["a"]) in opened]
    held: dict = {}
    if wanted:
        held = (st.v1.cmd("tx.coins", addresses=wanted, chain=one.key) or {}).get("addresses") or {}
    out = []
    for e in listed:
        row = {"address": e["a"], "kind": e["k"], "registered": e["t"], "since": None, **READY}
        if e["a"] not in wanted:
            row.update({"lazy": True, "spendable": None, "inscribed": None, "inscribed_n": None,
                        "unchecked": None, "unchecked_n": None})
        else:
            a = held.get(e["a"]) or {}
            free = (a.get("confirmed") or []) + (a.get("unconfirmed") or [])
            inscribed = a.get("held_back") or []
            row.update({"spendable": sum(int(c["value"]) for c in free),
                        "inscribed": sum(int(c["value"]) for c in inscribed), "inscribed_n": len(inscribed),
                        "unchecked": 0, "unchecked_n": 0})
        out.append(row)
    return out


def register(app, st, chains):
    lock = threading.Lock()

    def account_of(request: Request):
        return app.state.signed_in_account(request)

    def chain_asked(said: dict):
        net = str(said.get("chain") or "").strip()
        one = chains.by_network(net) if net else None
        if one is None or one.network not in {c.network for c in chains.account_chains()}:
            raise ValueError(f"no chain called {net} here")
        return one

    @app.get("/account/imported/busy")
    def imported_busy(request: Request, chain: str = ""):
        account_of(request)
        try:
            one = chain_asked({"chain": chain})
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=404)
        return JSONResponse({"chain": one.network, "busy": False}, headers={"Cache-Control": "no-store"})

    @app.post("/account/imported/address")
    def imported_address(request: Request, payload: Any = Body(None)):
        account = account_of(request)
        said = payload if isinstance(payload, dict) else {}
        try:
            one = chain_asked(said)
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        sync = bool(said.get("sync"))
        address = str(said.get("address") or "").strip()
        kind = str(said.get("kind") or "p2pkh")
        pubkey = str(said.get("pubkey") or "")
        try:
            importslib.check_proof(one.params, account.pubkey, one.network, address, pubkey, kind,
                                   str(said.get("signature") or ""))
        except importslib.ImportRefused as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        if sync and not (st.imported_wallets().get(account.pubkey) or {}).get("signed"):
            return JSONResponse({"detail": "this arcade has no signed record of your imported wallets yet"},
                                status_code=400)
        with lock:
            listed = entries(st, one.network, account.pubkey)
            if any(e["a"] == address for e in listed):
                return JSONResponse({"address": address, "chain": one.network, "already": True, **READY})
            if address == chains.account_address(account.pubkey, one):
                return JSONResponse({"detail": "that is already your arcade wallet's own address on this chain"},
                                    status_code=400)
            if len(listed) >= importslib.CAP_PER_CHAIN:
                return JSONResponse({"detail": f"An account can import at most {importslib.CAP_PER_CHAIN} wallets "
                                               f"on {one.label}. Remove one first."}, status_code=400)
            day_key = f"import_{'sync_' if sync else ''}day:{account.pubkey}"
            day = importslib.recent(st.setting(day_key, []))
            if len(day) >= importslib.PER_DAY:
                return JSONResponse({"detail": f"{importslib.PER_DAY} imports in a day is the most one account can "
                                               "make. Try again tomorrow."}, status_code=429)
            listed.append({"a": address, "k": kind, "p": pubkey.lower(), "t": int(time.time())})
            st.set_setting(importslib.setting_key(one.network, account.pubkey), listed)
            st.set_setting(day_key, day + [int(time.time())])
        return JSONResponse({"address": address, "chain": one.network, **READY})

    @app.post("/account/imported/rescan")
    def imported_rescan(request: Request, payload: Any = Body(None)):
        account = account_of(request)
        said = payload if isinstance(payload, dict) else {}
        try:
            one = chain_asked(said)
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        address = str(said.get("address") or "").strip()
        if entry(st, one.network, account.pubkey, address) is None:
            return JSONResponse({"detail": "that is not one of your imported wallets on this chain"},
                                status_code=403)
        return JSONResponse({"address": address, "chain": one.network, **READY})

    @app.post("/account/imported/forget")
    def imported_forget(request: Request, payload: Any = Body(None)):
        account = account_of(request)
        said = payload if isinstance(payload, dict) else {}
        try:
            one = chain_asked(said)
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        address = str(said.get("address") or "").strip()
        with lock:
            listed = entries(st, one.network, account.pubkey)
            kept = [e for e in listed if e["a"] != address]
            if len(kept) == len(listed):
                return JSONResponse({"address": address, "removed": False})
            st.set_setting(importslib.setting_key(one.network, account.pubkey), kept or None)
        return JSONResponse({"address": address, "removed": True})
