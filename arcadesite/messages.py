"""Private messages for site accounts.

Every account's identity lives in its browser, so the site cannot tell which messages are whose: it hands a
signed-in browser every message envelope it has seen and the browser tries its key (the original's arrangement).
Those envelopes are collected by the original's own scanner (arcade/messaging/scanner.py, no identity: candidates
only) walking Pepecoin testnet through Arcade V1.0's Core-style RPC, into the site's own message store
(<home>/test.sqlite, the original's file: an arcade's store moves here as it is and the walk continues from it).

    GET  /account/messages?after=   envelopes after a cursor, for the browser to try
    GET  /account/who/{name}        where to write to somebody: their address and published key (V1.0 msg.who)
    POST /account/write             a sealed message -> a transaction offer (signed in the browser, /account/sign)
    GET  /me/messages               the page
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

from fastapi import Body, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from arcade import fees
from arcade import funding as fundinglib
from arcade import tags as taglib
from arcade import txbuild
from arcade.config import NETWORKS
from arcade.messaging import envelope as envelopelib
from arcade.messaging import sender as sendermod
from arcade.rpc import RpcError

from . import legacy as L
from .actions import class_c_or_b
from .v1 import V1, V1Error
from .wallet import CoinDB

log = logging.getLogger("arcadesite.messages")
STORE_FILE = "test.sqlite"
SCAN_EVERY = 10.0


class V1Rpc:
    """What the original's scanner asks of an RpcClient, answered by V1.0's Core-style RPC on one chain. V1.0 serves
    getblock at verbosity 0 and 1 only: verbosity 2 is the block's txids, each read with getrawtransaction."""

    def __init__(self, v1: V1, network: str):
        self.v1, self.network = v1, network

    def call(self, method: str, *params):
        if method == "getblock" and len(params) > 1 and params[1] == 2:
            return self.get_block(params[0], 2)
        try:
            return self.v1.core(self.network, method, *params)
        except V1Error as e:
            raise RpcError(e.code or -1, str(e), method) from None

    def get_block_count(self) -> int:
        return int(self.call("getblockcount"))

    def get_block_hash(self, height: int) -> str:
        return str(self.call("getblockhash", int(height)))

    def get_block(self, block_hash: str, verbosity: int = 2) -> dict[str, Any]:
        block = self.call("getblock", block_hash, 1)
        if verbosity == 2:
            block["tx"] = [self.call("getrawtransaction", t, True) for t in block.get("tx") or []]
        return block

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def register(app, st, chains):
    from arcade.messaging.scanner import Scanner
    from arcade.messaging.store import MessageStore
    v1 = st.v1
    params = NETWORKS["test"]
    path = st.home / STORE_FILE
    status: dict[str, Any] = {"height": None, "tip": None, "error": "", "at": 0.0}

    def store() -> MessageStore:
        return MessageStore(path)

    from arcade import push as pushlib
    push = pushlib.Push(st.home) if pushlib.available() else None

    def owners() -> dict[str, str]:
        """Every account address here -> the account (read per pass: a new account is covered at once)."""
        out: dict[str, str] = {}
        for key, value in st.settings().items():
            if key.startswith("address:") and isinstance(value, str) and value:
                out[value] = key.rsplit(":", 1)[1]
        return out

    def paid(txid: str) -> list[str]:
        tx = v1.core("test", "getrawtransaction", txid, True) or {}
        out = []
        for vout in tx.get("vout", []):
            spk = vout.get("scriptPubKey", {})
            out.extend(spk.get("addresses") or ([spk["address"]] if spk.get("address") else []))
        return out

    def wake(s) -> None:
        """Push: every new envelope, and which accounts here it paid (a message pays its recipient's address)."""
        if push is None:
            return
        held = owners()
        try:
            push.watch(lambda after: s.candidates_for_others(after, 200), s.newest_candidate, paid, held.get)
        except Exception as e:                              # noqa: BLE001 -- the next pass tries again
            log.info("push watch: %s", e)

    def walk(stop: threading.Event) -> None:
        """The scanner's loop: blocks then the pool, every few seconds, 2,000 blocks a turn while catching up."""
        while not stop.is_set():
            try:
                with store() as s:
                    sc = Scanner(V1Rpc(v1, "test"), params, s, identity=None)
                    got = sc.scan(max_blocks=2000)
                    sc.scan_mempool()
                    wake(s)
                    status.update(height=(s.scan_cursor(params.name) or (None,))[0],
                                  error="", at=time.time())
                caught_up = getattr(got, "blocks", 0) < 2000
            except Exception as e:                          # noqa: BLE001 -- told on the page, tried again
                status.update(error=str(e)[:200], at=time.time())
                log.warning("message walk: %s", e)
                caught_up = True
            stop.wait(SCAN_EVERY if caught_up else 0.1)

    stop = threading.Event()
    if st.setting("message_walk", True) is not False:
        threading.Thread(target=walk, args=(stop,), name="message-walk", daemon=True).start()
    app.state.message_status = status

    @app.get("/account/messages")
    def account_messages(request: Request, after: int = 0, limit: int = 200):
        app.state.signed_in_account(request)
        limit = max(1, min(int(limit), 500))
        with store() as s:
            rows = s.candidates_for_others(after=int(after), limit=limit)
            newest = s.newest_candidate()
        return JSONResponse({
            "cursor": rows[-1]["cursor"] if rows else int(after), "newest": newest,
            "more": bool(rows) and rows[-1]["cursor"] < newest,
            "candidates": [{"cursor": r["cursor"], "txid": r["txid"], "height": r["height"], "when": r["block_time"],
                            "from_address": r["sender_addr"], "type": r["msg_type"],
                            "msg_id": (r["msg_id"] or b"").hex() or None, "countdown": r["countdown"],
                            "payload": bytes(r["payload"]).hex()} for r in rows],
        }, headers={"Cache-Control": "no-store"})

    @app.get("/account/who/{name}")
    def account_who(request: Request, name: str):
        app.state.signed_in_account(request)
        wanted = (name or "").strip().lstrip("@")
        try:
            if not (25 <= len(wanted) <= 64 and wanted.isalnum()):
                wanted = "@" + taglib.validate(wanted)
            who = v1.cmd("msg.who", name=wanted) or {}
        except taglib.TagError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        except V1Error as exc:
            return JSONResponse({"detail": str(exc)}, status_code=404)
        address, tag = str(who.get("address") or ""), str(who.get("tag") or "")
        if not address:
            return JSONResponse({"detail": f"nobody holds {wanted} on this chain"}, status_code=404)
        if not who.get("key"):
            return JSONResponse({"address": address, "tag": tag, "key": None,
                                 "detail": "they have not published a messaging key, so there is nowhere to send it. "
                                           "Ask them to publish their tag -- it is one button."}, status_code=404)
        pfp = str(who.get("pfp") or "")
        return JSONResponse({"address": address, "tag": tag, "key": who["key"], "fingerprint": who.get("fingerprint"),
                             "name": who.get("name") or "", "face": f"/content/{pfp}" if pfp else ""})

    @app.post("/account/write")
    def account_write(request: Request, payload: Any = Body(None)):
        account = app.state.signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        chain = chains.account_chain()
        address = chains.account_address(account.pubkey, chain)
        if not address:
            return JSONResponse({"detail": "this account has no address yet"}, status_code=400)
        try:
            sealed = bytes.fromhex(str(said.get("sealed", "")))
            if not sealed:
                raise ValueError("there is nothing to send")
            to = str(said.get("to", "")).strip()
            complaint = chains.check_address(to, chain)
            if complaint:
                raise ValueError(complaint)
            body = envelopelib.Header(type=envelopelib.TYPE_SINGLE, clen=len(sealed)).encode() + sealed
            try:
                coin = bytes.fromhex(str(st.setting(chains.coinkey_key(account.pubkey, chain), "") or ""))
            except ValueError:
                coin = b""
            outputs = class_c_or_b(chain, address, body, coin)
            outputs.append((sendermod.OUTPUT_VALUE, txbuild.p2pkh_script(to)))
            unsigned = fundinglib.build(CoinDB(v1, chain), chain.params, address, outputs, rate=fees.MIN_FEE_PER_KB,
                                        what=f"a message to {to}")
            app.state.quota(account, "message", len(sealed))
        except (fundinglib.FundingError, ValueError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        offer = app.state.offers.add(account.pubkey, chain.network, unsigned, unsigned.what)
        return JSONResponse({"offer": offer.id, "bytes": len(sealed), "chain": chain.network, **unsigned.as_json()})

    # ---------------------------------------------------------------- push (arcade/push.py: its key, its devices)
    @app.get("/push/key")
    def push_key():
        return JSONResponse({"enabled": True, "key": push.public_key()} if push is not None else {"enabled": False})

    @app.post("/account/push/subscribe")
    def push_subscribe(request: Request, payload: Any = Body(None)):
        account = app.state.signed_in_account(request)
        if push is None:
            return JSONResponse({"detail": "this arcade does not send notifications"}, status_code=400)
        said = payload if isinstance(payload, dict) else {}
        keys = said.get("keys") if isinstance(said.get("keys"), dict) else {}
        try:
            push.subscribe(account.pubkey, str(said.get("endpoint", "")), str(keys.get("p256dh", "")),
                           str(keys.get("auth", "")))
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        return JSONResponse({"ok": True})

    @app.post("/account/push/unsubscribe")
    def push_unsubscribe(request: Request, payload: Any = Body(None)):
        account = app.state.signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        if push is not None:
            push.unsubscribe(account.pubkey, str(said.get("endpoint", "")))
        return JSONResponse({"ok": True})

    @app.get("/account/push/news")
    def push_news(request: Request):
        """What woke this account's phone: who wrote, and when. Never what."""
        account = app.state.signed_in_account(request)
        out = []
        for row in (push.news(account.pubkey) if push is not None else []):
            if str(row["txid"]).startswith(("ring:", "fill:")):
                continue                                    # rings and order fills: not on this site yet
            try:
                tag = str((v1.page(f"address/{row['sender']}")[1] or {}).get("tag") or "")
            except V1Error:
                tag = ""
            out.append({"from": f"@{tag}" if tag else str(row["sender"])[:10] + "\u2026", "txid": row["txid"],
                        "at": row["at"]})
        return JSONResponse({"news": out}, headers={"Cache-Control": "no-store"})

    @app.get("/me/messages", response_class=HTMLResponse)
    def my_messages(request: Request):
        if app.state.signed_in(request) is None:
            return RedirectResponse("/join?next=/me/messages", status_code=303)
        return app.state.render(request, "my_messages.html", chain=chains.account_chain(), when=L._local_time)

    return stop
