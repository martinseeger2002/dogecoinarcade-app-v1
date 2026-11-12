"""The rest of what a signed-in page asks on every load.

* The sealed record of an account's imported wallets (accounts.ImportedWallets): kept, served to other arcades
  the account is brought to (/signin/{tag}/imported), versioned and signed as in the original. Each chain's imported
  addresses and balances: imported.py (V1.0's address index, ready at once).
* What the original answered from its own coin lists (/account/coins/list, merge, backfill, reconcile): V1.0's
  address index has every coin, so there is nothing to keep or read back.
* Routes of features not on this site yet: their "nothing here" answer, in the shape the page reads, so a page
  draws without them instead of failing.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any

from fastapi import Body, Request
from fastapi.responses import JSONResponse

from arcade import accounts as accountslib
from arcade import apikeys as apikeylib
from arcade import wallet_imports as importslib

from . import imported as importedlib


def register(app, st, chains):
    def account_of(request):
        return app.state.signed_in_account(request)

    def proof_host() -> str:
        return next((h for h in sorted(st.public_hosts) if h), "")

    def record_answer(account, record_only: bool, opened: set | None = None) -> JSONResponse:
        record = st.imported_wallets()
        have = record.get(account.pubkey) or {"blob": "", "updated": 0, "version": 0, "signed": False}
        vault = st.vault().by_pubkey(account.pubkey)
        said = {"blob": have["blob"], "updated": have["updated"], "version": int(have.get("version") or 0),
                "signed": bool(have.get("signed")),
                "siblings": [{"digest": one["digest"], "version": one["version"], "blob": one["blob"]}
                             for one in record.siblings(account.pubkey)],
                "tag": str((vault or {}).get("tag") or ""), "host": proof_host(),
                "vault": json.loads(vault["blob"]) if vault else None}
        if record_only:
            said["record_only"] = True
        else:
            per_chain, names = {}, {}
            for one in chains.account_chains():
                names[one.network] = str(st.setting(importslib.name_key(one.network, account.pubkey), "") or "")
                try:
                    per_chain[one.network] = importedlib.view(st, one, account.pubkey, opened)
                except Exception:                           # noqa: BLE001 -- a chain V1.0 cannot read now
                    per_chain[one.network] = []
            said.update({"busy": {}, "no_imports": [], "import_closed": {}, "chains": per_chain, "names": names,
                         "cap": importslib.CAP_PER_CHAIN})
        return JSONResponse(said, headers={"Cache-Control": "no-store"})

    @app.get("/account/imported")
    def account_imported(request: Request, record: int = 0, lazy: int = 0, chain: str = "", address: str = ""):
        opened = {(chain, address.strip())} if lazy and chain and address.strip() else set()
        return record_answer(account_of(request), bool(record), opened if lazy else None)

    @app.post("/account/imported")
    def account_imported_put(request: Request, payload: Any = Body(None)):
        account = account_of(request)
        said = payload if isinstance(payload, dict) else {}
        if not st.vault().by_pubkey(account.pubkey):
            return JSONResponse({"detail": "this arcade keeps no sealed wallet for your account, so there is no "
                                           "password to seal imported wallets under here"}, status_code=404)
        merged = said.get("merged") or []
        if not isinstance(merged, list) or len(merged) > 2 * accountslib.SIBLINGS_MAX or not all(
                isinstance(d, str) and re.fullmatch(r"[0-9a-fA-F]{64}", d) for d in merged):
            return JSONResponse({"detail": "that merge is not readable"}, status_code=400)
        blob = said.get("blob")
        try:
            text = blob if isinstance(blob, str) else json.dumps(blob, separators=(",", ":")) \
                if isinstance(blob, dict) else ""
            out = st.imported_wallets().put(account.pubkey, text, said.get("version"),
                                            str(said.get("signature") or ""), merged=merged,
                                            merged_sig=str(said.get("merged_signature") or ""))
        except accountslib.ImportedMoved as exc:
            return JSONResponse({"detail": str(exc), "updated": exc.updated, "version": exc.version},
                                status_code=409)
        except (accountslib.AccountError, ValueError, TypeError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        return JSONResponse(out)

    @app.post("/account/imported/siblings/drop")
    def siblings_drop(request: Request, payload: Any = Body(None)):
        account = account_of(request)
        said = payload if isinstance(payload, dict) else {}
        merged = said.get("merged") or []
        if not isinstance(merged, list) or not 0 < len(merged) <= 2 * accountslib.SIBLINGS_MAX or not all(
                isinstance(d, str) and re.fullmatch(r"[0-9a-fA-F]{64}", d) for d in merged):
            return JSONResponse({"detail": "that merge is not readable"}, status_code=400)
        try:
            gone = st.imported_wallets().drop_siblings(account.pubkey, merged, str(said.get("merged_signature") or ""))
        except accountslib.AccountError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        return JSONResponse({"dropped": gone})

    bring_asked: dict[str, float] = {}

    @app.post("/account/imported/bring")
    def imported_bring(request: Request, payload: Any = Body(None)):
        """The signed record of imported wallets from the arcade an account was brought from: only an arcade this
        site knows, at most once a minute, kept only when signed by this account's own key and newer (adopt)."""
        import requests
        import urllib.parse
        account = account_of(request)
        said = payload if isinstance(payload, dict) else {}
        mine = st.vault().by_pubkey(account.pubkey)
        if not mine or not mine.get("tag"):
            return JSONResponse({"detail": "this arcade keeps no sealed wallet for your account"}, status_code=404)
        hosts = app.state.arcade_hosts()
        version = lambda: (st.imported_wallets().get(account.pubkey) or {}).get("version", 0)  # noqa: E731
        source = ""
        for cand in (str(said.get("from") or ""), str(st.setting(f"came_from:{account.pubkey}", "") or "")):
            cand = cand.rstrip("/").lower()
            if cand.removeprefix("https://") in hosts:
                source = cand if cand.startswith("https://") else "https://" + cand
                break
        if not source or time.time() - bring_asked.get(account.pubkey, 0.0) < 60:
            return JSONResponse({"asked": False, "version": version()})
        bring_asked[account.pubkey] = time.time()
        outcome = None
        try:
            got = requests.get(f"{source}/signin/{urllib.parse.quote(mine['tag'])}/imported", timeout=10,
                               headers={"User-Agent": "arcade-node"}, allow_redirects=False)
            had = got.json() if got.status_code == 200 else None
            if isinstance(had, dict) and isinstance(had.get("blob"), str):
                outcome = st.imported_wallets().adopt(mine["pubkey"], had["blob"], had.get("version"),
                                                      str(had.get("signature") or ""))
        except (requests.RequestException, ValueError, accountslib.AccountError):
            outcome = None
        return JSONResponse({"asked": True, "from": source, "outcome": outcome, "version": version()})

    @app.get("/signin/{tag}/imported")
    def signin_imported(request: Request, tag: str):
        """An account's signed record of imported wallets, for an arcade the account is brought to."""
        register_ = st.accounts()
        who = f"peerdata:{apikeylib.client_ip(request)}"
        now = int(time.time())
        if register_._rate_limited(who, now):
            return JSONResponse({"detail": "too many in a row. Wait a few minutes."}, status_code=429)
        register_._attempt(who, now)
        found = st.vault().get(tag)
        have = st.imported_wallets().get(found["pubkey"]) if found else None
        if not have or not have.get("signed"):
            return JSONResponse({"detail": "no signed record of imported wallets here by that name"}, status_code=404)
        return JSONResponse({"tag": found["tag"], "pubkey": found["pubkey"], "version": have["version"],
                             "blob": have["blob"], "signature": have["sig"]})

    # ---------------------------------------------------------------- coin lists: the address index has every coin
    @app.get("/account/coins/list")
    def coins_list(request: Request):
        account_of(request)
        return JSONResponse({"v": 1, "lists": {}})

    @app.post("/account/coins/merge")
    def coins_merge(request: Request):
        account_of(request)
        return JSONResponse({"skipped": "this arcade reads every coin from its address index"})

    @app.post("/account/coins/backfill")
    def coins_backfill(request: Request):
        account_of(request)
        return JSONResponse({"skipped": "this arcade reads every coin from its address index", "done": True})

    @app.post("/account/coins/reconcile")
    def coins_reconcile(request: Request):
        account_of(request)
        return JSONResponse({"skipped": "this arcade reads every coin from its address index"})

    # ---------------------------------------------------------------- the account's own small settings
    @app.get("/account/favorites")
    def favorites(request: Request):
        account = app.state.signed_in(request)
        owner = account.pubkey if account is not None else None
        return JSONResponse({"favorites": st.setting(f"favorites:{owner}", []) if owner else [],
                             "saved": owner is not None}, headers={"Cache-Control": "no-store"})

    @app.get("/account/homebg")
    def homebg(request: Request):
        account = app.state.signed_in(request)
        kept = str(st.setting(f"homebg:{account.pubkey}", "") or "") if account is not None else ""
        return JSONResponse({"id": kept}, headers={"Cache-Control": "no-store"})

    @app.post("/account/imported/name")
    def imported_name(request: Request, payload: Any = Body(None)):
        """A name for the account's imported wallets on one chain, kept as the original keeps it."""
        from arcade import wallet_imports as importslib
        account = account_of(request)
        said = payload if isinstance(payload, dict) else {}
        net = str(said.get("chain") or "")
        if chains.by_network(net) is None:
            return JSONResponse({"detail": "this arcade keeps no wallet on that chain"}, status_code=400)
        name = importslib.clean_name(said.get("name"))
        st.set_setting(importslib.name_key(net, account.pubkey), name or None)
        return JSONResponse({"chain": net, "name": name})

    @app.get("/account/dust")
    def dust(request: Request):
        """Payload dust (an account's own Class B multisig outputs) to sweep back. TODO: V1.0's tx.coins lists address
        outputs only, so these are not found yet; nothing is offered for sweeping until they are."""
        account_of(request)
        return JSONResponse({"chains": []})

    # ---------------------------------------------------------------- not on this site yet: nothing to show
    nothing = {
        "/account/escrows": {"escrows": [], "chain": "test"},
        "/account/arcades": {"rows": []},
        "/moderation/verdicts": {"enabled": False, "verdicts": {}},
    }

    def empty(answer):
        def handler(request: Request):
            return JSONResponse(answer, headers={"Cache-Control": "no-store"})
        return handler

    for path, answer in nothing.items():
        app.add_api_route(path, empty(answer), methods=["GET"], include_in_schema=False)
