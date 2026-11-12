"""The Creator: making things on the chains, as the original makes them (arcade/web/app.py), with every coin, token,
inscription, collection and name read from Arcade V1.0. Each transaction is an offer the browser signs and
/account/sign (wallet.py) broadcasts; this module never signs and never broadcasts.

Pages
    GET  /create, /create/chain (the cookie arcade_chain: which chain this browser creates on), /create/name,
         /create/ord, /mintpad/new, /me/runs, /me/run/{id}
Arcade-ledger inscriptions (one file; a big one is a split and then its pieces; a collection "run" of pieces)
    POST /account/inscribe, /account/inscribe/abandon;  GET /account/inscribe/unfinished
    POST /account/run/upload, /account/run/start, /account/run/plan, /account/run, /account/run/stop,
         /account/run/retry, /account/run/delete, /account/run/piece, /account/run/done;  GET /account/run/item
    POST /account/mintpad/inscribe, /account/tokenpad/inscribe
    The original's books, same files in st.home: accountruns.sqlite, accountparts.sqlite, collections/ (uploads)
Ords (commit + reveals, arcade/ord_inscribe.py) and DRC-20 / PRC-20 sends (arcade/token20_send.py)
    POST /account/ord/inscribe, /account/ord/forget, /account/ord/quote, /account/ord/split, /account/ord/collection,
         /account/token20/send, /account/mesh/draft
    GET  /account/ord/runs, /account/ord/chains, /account/ord/name, /account/ord/confirmed, /account/names,
         /account/mesh/site
    ord-runs.json in st.home, as the original. A reveal spends a script-path commit: V1.0's tx.broadcast sends it to the
    coin's peers unpooled ("pooled": false), which is a send like any other.
Ledger tokens
    POST /account/token/create, /account/token/manage

NEEDS-V1: a collection's items (editions, picture digests, #1's rules) on a chain other than the home chain: V1.0's
  /r/collection reads the home chain only, so on the others a run is written down without the "already on the chain"
  check (each piece is still checked against the run's own digests).
NEEDS-V1: token names claimed by a transaction still in the mempool (the original's index.pending_names): only names
  this site offered and saw broadcast are remembered (an hour).
NEEDS-V1: /account/names (names held by an account's small coins: "names_on") answers no names.
NEEDS-V1: /account/mesh/site: the holder is V1.0's explorer.name; a name's current page (the "p":"mesh" records) is
  not answered (page: null).
NEEDS-V1: DRC-20 / PRC-20 held by the account's standing listings are not taken off what a send may move unless the
  exchange module sets app.state.listed_token20(account, network, tick) -> whole tokens.
"""
from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import re
import secrets
import shutil
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import Body, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from arcade import accountparts as accountpartslib
from arcade import accountruns as accountrunslib
from arcade import collections as collectionlib
from arcade import fees
from arcade import funding as fundinglib
from arcade import inscribe as inscribelib
from arcade import inscriptions as inscriptionlib
from arcade import media
from arcade import mintpad as mintpadlib
from arcade import state as statelib
from arcade import tags as taglib
from arcade import tokens as tokenlib
from arcade import txbuild
from arcade.ledger import COIN, AmountError, format_amount, parse_amount
from arcade.messaging import sender as sendermod
from arcade.script import b58check_decode, b58check_encode, hash160

from .actions import class_c_or_b
from .v1 import V1Error
from .wallet import CoinDB

CHAIN_COOKIE = "arcade_chain"
MAINNET_CREATE_FEE_PERMILLE = 5
MINTPAD_TEMPLATES = {
    "spotlight": "Spotlight: the cover, big, and the Mint button under it",
    "wall": "Wall: the whole collection as a dimmed mosaic behind the card",
    "gallery": "Gallery: a scrolling strip of the pieces above the button",
    "arcade": "Arcade: a pixel-font cabinet with an INSERT COIN button",
    "lottery": "Lottery: a flipping wall of the set and a reel that spins to the piece you win",
    "neon": "Neon: glowing pink and cyan on black",
    "polaroid": "Polaroid: the cover as an instant photo on warm paper",
    "terminal": "Terminal: green-screen console, scanlines and all",
    "minimal": "Minimal: clean white card, nothing but the piece and the button",
}
#: The network keeps at most 25 unconfirmed transactions in one chain; one short of it a run waits for a block.
MEMPOOL_CHAIN_ROOM = 24
SET_PIC_ROOM = 4
ORD_UNSTARTED_SECONDS = 1800
ORD_INDEX_PART_BYTES = 5000
ORD_LANES_MOST = 8
T20_SEND_SECONDS = 1800
T20_PENDING_SECONDS = 6 * 3600
NAME_ENDINGS = {"doge": [".doge"], "main": [".pepe", ".pep", ".\U0001f438", ".ribbit", ".frog", ".kek"],
                "test": [".pepe", ".pep"]}
COLLECTION_PAGE = 500


class TemplateChain:
    """What the Creator's templates read of a chain (`chain.title`, `chain.params`, `chain.is_mainnet` ...)."""

    def __init__(self, chain):
        self.chain = chain
        self.network = self.net = chain.network
        self.params = chain.params
        self.label = chain.label
        self.title = getattr(chain.params, "title", "") or chain.label
        self.is_mainnet = chain.is_mainnet
        self.is_addon = chain.network not in ("test", "main")


class ChainIndex:
    """The few questions the original asked of its token index for one chain, answered by V1.0."""

    def __init__(self, v1, chain):
        self.v1, self.chain = v1, chain
        self.home = chain.network == "test"

    def inscription(self, txid: str) -> dict | None:
        try:
            if self.home:
                code, row = self.v1.page(f"inscription/{txid}")
                return row if code == 200 and row else None
            got = self.v1.cmd("explorer.ledger_inscription", chain=self.chain.key, id=txid) or {}
            return got or None
        except V1Error:
            return None

    def collection_items(self, creator: str, name: str) -> list[dict]:
        """Every item of `creator`'s collection `name` ({edition, sha256, rawjson, ...}); [] off the home chain."""
        if not self.home or not creator or not name:
            return []
        out: list[dict] = []
        while len(out) < 20000:                             # paged by what comes back, up to the set's count
            code, got = self.v1.page(f"collection/{creator}/{name}", {"limit": COLLECTION_PAGE, "offset": len(out)})
            items = ((got or {}).get("items") or []) if code == 200 else []
            out += items
            if not items or len(out) >= int((got or {}).get("count") or 0):
                break
        return out

    def collection_editions(self, creator: str, name: str) -> set[int]:
        return {int(r["edition"]) for r in self.collection_items(creator, name) if r.get("edition") is not None}

    def collection_cover(self, creator: str, name: str) -> dict | None:
        items = [r for r in self.collection_items(creator, name) if r.get("edition") is not None]
        if not items:
            return None
        first = min(items, key=lambda r: int(r["edition"]))
        return {**first, "json": first.get("rawjson") or json.dumps(first.get("json") or {})}

    def property(self, pid: int) -> dict | None:
        try:
            p = self.v1.cmd("explorer.ledger_token", chain=self.chain.key, id=int(pid)) or {}
        except V1Error:
            return None
        if not p:
            return None
        return {"property_id": int(p.get("id", pid)), "propertyid": int(p.get("id", pid)), "name": p.get("name") or "",
                "divisible": int(p.get("type") or 2) == 2, "managed": bool(p.get("managed")),
                "issuer": p.get("issuer") or "", "data": p.get("data") or "", "url": p.get("url") or "",
                "category": p.get("category") or "", "subcategory": p.get("subcategory") or ""}

    def properties(self) -> list[dict]:
        out: list[dict] = []
        while len(out) < 20000:                             # V1.0 answers at most 50 a page, with the total
            got = self.v1.cmd("explorer.ledger_tokens", chain=self.chain.key, offset=len(out), limit=500) or {}
            items = got.get("items") or []
            out += [{"property_id": int(p.get("id") or 0), "name": p.get("name") or ""} for p in items]
            if not items or len(out) >= int(got.get("total") or 0):
                break
        return out

    def balance(self, address: str, pid: int) -> int:
        got = self.v1.cmd("explorer.address", chain=self.chain.key, address=address) or {}
        row = next((r for r in got.get("ledger_tokens") or [] if int(r.get("propertyid") or -1) == int(pid)), {})
        return int(row.get("units") or 0)


def register(app, st, chains):
    v1 = st.v1
    offers = app.state.offers
    runs_book = accountrunslib.Runs(st.home / "accountruns.sqlite")
    parts_book = accountpartslib.Parts(st.home / "accountparts.sqlite")
    set_pics: dict[str, set[str]] = {}
    uploads: dict[str, str] = {}
    order_posts: dict[str, float] = {}
    named_tokens: list[dict] = []                         # tokens this site saw created, until indexed (an hour)
    t20_sends: dict[str, dict] = {}
    t20_pending: dict[tuple[str, str], list[dict]] = {}
    ord_lock = threading.Lock()
    ord_held: dict[str, tuple[bytes, Any]] = {}
    app.state.ord_held = ord_held

    def quota(account, kind: str, nbytes: int = 0, count: bool = True) -> None:
        """wallet.py's quota, its allowance refusal (accounts.AccountError) said as the 400 every route answers."""
        from arcade.accounts import AccountError
        try:
            app.state.quota(account, kind, nbytes, count=count)
        except AccountError as exc:
            raise ValueError(str(exc)) from None

    def render(request, template, **ctx):
        return app.state.render(request, template, **ctx)

    def signed_in(request):
        return app.state.signed_in(request)

    def signed_in_account(request):
        return app.state.signed_in_account(request)

    def to_join(request: Request):
        return RedirectResponse("/join?next=" + request.url.path, status_code=303)

    def index_of(chain) -> ChainIndex:
        return ChainIndex(v1, chain)

    def coin_pubkey(pubkey: str, chain) -> bytes:
        try:
            return bytes.fromhex(str(st.setting(chains.coinkey_key(pubkey, chain), "") or ""))
        except ValueError:
            return b""

    def build(chain, address: str, outputs: list, what: str, exclude=frozenset(), extra=None):
        return fundinglib.build(CoinDB(v1, chain), chain.params, address, outputs, rate=fees.MIN_FEE_PER_KB,
                                what=what, exclude=exclude, extra=extra)

    def chain_asked(said: dict, field: str = "chain"):
        """Which chain a request is about: the tag chain unless it says; by name, never quietly another."""
        wanted = str((said or {}).get(field, "") or "").strip().lower()
        if not wanted:
            return chains.account_chain()
        for one in chains.account_chains():
            if wanted in (one.network, one.label.lower(), str(getattr(one.params, "label", "")).lower()):
                return one
        raise ValueError(f"this node does not run {wanted}")

    def chain_on(network: str):
        one = chains.by_network(network)
        if one is None:
            raise HTTPException(409, f"this arcade keeps no {network} chain now")
        return one

    def creating_chain(request: Request):
        """The chain this browser creates on (the cookie /create/chain sets), else the tag chain."""
        wanted = request.cookies.get(CHAIN_COOKIE, "")
        if wanted and signed_in(request) is not None:
            one = chains.by_network(wanted)
            if one is not None and getattr(one.params, "class_b", True):
                return one
        return chains.account_chain()

    def any_address(pubkey: str, chain) -> str:
        said = chains.account_address(pubkey, chain)
        if said:
            return said
        sib = chains.by_network("main" if chain.params.real_coins else "test")
        base = chains.account_address(pubkey, sib) if sib is not None else ""
        if not base:
            return ""
        try:
            return b58check_encode(chain.params.pubkeyhash_version, b58check_decode(base)[1])
        except Exception:                                   # noqa: BLE001
            return ""

    def tx_status(chain, txid: str) -> dict | None:
        try:
            tx = v1.core(chain.network, "getrawtransaction", txid, 1) or {}
        except V1Error:
            return None
        n = int(tx.get("confirmations") or 0)
        return {"txid": txid, "confirmed": n > 0, "confirmations": max(n, 0), "conflicted": n < 0}

    # ---------------------------------------------------------------- the operator's fee on mainnet
    def fee_set(chain) -> int:
        try:
            return max(0, int(st.setting(f"inscribe_fee:{chain.network}", 0) or 0))
        except (TypeError, ValueError):
            return 0

    def fee_payee(chain) -> str:
        """The operator account's address on that chain (settings `operator`: its pubkey), as llm.py pays it."""
        op = str(st.setting("operator") or "").lower()
        return chains.account_address(op, chain) if op else ""

    def operator_fee(chain, payer: str, cost: int) -> tuple[list, dict | None]:
        """The original's _operator_fee: mainnet only, `mainnet_create_fee_permille` of the cost, never under the
        chain's floor (`inscribe_fee:<network>`, else its dust line); not paid by the operator to itself."""
        if not chain.is_mainnet:
            return [], None
        try:
            permille = int(st.setting("mainnet_create_fee_permille", MAINNET_CREATE_FEE_PERMILLE))
        except (TypeError, ValueError):
            permille = MAINNET_CREATE_FEE_PERMILLE
        floor = max(fee_set(chain), fees.dust_limit(chain.params))
        if permille <= 0 and not fee_set(chain):
            return [], None
        to = fee_payee(chain)
        if not to or to == payer:
            return [], None
        share = -(-max(0, int(cost)) * max(0, permille) // 1000)
        sats = max(share, floor)
        said = {"address": to, "value": sats, "permille": max(0, permille), "minimum": floor,
                "at_minimum": share < floor}
        return [(sats, txbuild.p2pkh_script(to, getattr(chain, "params", None)))], said

    # ---------------------------------------------------------------- pages
    @app.get("/create/chain")
    def create_chain(request: Request, to: str = "", back: str = "/create"):
        """Pick the chain this browser creates on."""
        where = back if back.startswith("/") and not back.startswith("//") else "/create"
        answer = RedirectResponse(where, status_code=303)

        def named(c) -> bool:
            return to in (c.network, c.network if c.network not in ("test", "main")
                          else ("main" if c.is_mainnet else "test"))
        chosen = next((c for c in chains.account_chains() if named(c)), None)
        if chosen is None or not getattr(chosen.params, "class_b", True):
            return answer                                   # coins only, or a chain this arcade does not keep
        answer.set_cookie(CHAIN_COOKIE, chosen.network, max_age=365 * 86400, samesite="lax",
                          secure=request.url.scheme == "https", path="/")
        return answer

    @app.get("/create", response_class=HTMLResponse)
    def create_page(request: Request):
        return render(request, "create.html", chain=TemplateChain(creating_chain(request)))

    @app.get("/create/name", response_class=HTMLResponse)
    def create_name_page(request: Request):
        return render(request, "create_name.html", chain=TemplateChain(creating_chain(request)))

    @app.get("/create/ord", response_class=HTMLResponse)
    def create_ord_page(request: Request):
        return render(request, "create_ord.html", chain=TemplateChain(creating_chain(request)))

    @app.get("/mintpad/new", response_class=HTMLResponse)
    def mintpad_new(request: Request):
        return render(request, "mintpad_new.html", chain=TemplateChain(creating_chain(request)),
                      templates=MINTPAD_TEMPLATES)

    @app.get("/me/runs", response_class=HTMLResponse)
    def my_runs(request: Request):
        account = signed_in(request)
        if account is None:
            return to_join(request)
        mine = list(runs_book.list(account=account.pubkey))
        mine.sort(key=lambda run: (run["status"] == "done", -run["created"]))
        labels = {one.network: one.label for one in chains.account_chains()}
        return render(request, "account_runs.html", chain=TemplateChain(chains.account_chain()),
                      runs=[{"run": r["id"], "name": r["name"], "items": r["items"], "sent": r["sent"],
                             "status": r["status"], "refused": r["failed_pieces"], "skipped": r["skipped"],
                             "next": r["next"], "chain": labels.get(r["network"], r["network"])} for r in mine])

    @app.get("/me/run/{run_id}", response_class=HTMLResponse)
    def my_run(request: Request, run_id: str, page: int = 1):
        account = signed_in(request)
        if account is None:
            return to_join(request)
        run = runs_book.get(run_id)
        if run is None or run["account"] != account.pubkey.lower():
            return RedirectResponse("/me/runs", status_code=303)
        per_page = 100
        pages = max(1, -(-run["items"] // per_page))
        page = max(1, min(page, pages))
        pieces = runs_book.pieces(run["id"], limit=per_page, offset=(page - 1) * per_page)
        chain = chains.by_network(run["network"]) or chains.account_chain()
        numbers: dict[str, int] = {}
        index = index_of(chain)
        for piece in pieces:
            if piece["txid"]:
                row = index.inscription(piece["txid"])
                if row and row.get("number") is not None:
                    numbers[piece["txid"]] = row["number"]
        return render(request, "account_run.html", run={**run, "refused": run["failed_pieces"]}, pieces=pieces,
                      numbers=numbers, chain=TemplateChain(chain), page=page, pages=pages)

    # ---------------------------------------------------------------- one inscription, or a split and its pieces
    @app.post("/account/inscribe")
    def account_inscribe(request: Request, payload: Any = Body(None)):
        """The next transaction of an inscription, as this account: `{content}` the start (one transaction, or a
        split), `{part, chunk, content}` piece n, `{run, piece, content}` a collection run's piece."""
        account = signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        if str(said.get("content_type", "")) == "application/vnd.arcade.orders+json":
            if time.time() - order_posts.get(account.pubkey, 0.0) < 60:
                return JSONResponse({"detail": "your orders were posted less than a minute ago; they stand as they are"},
                                    status_code=429)
            order_posts[account.pubkey] = time.time()
        try:
            chain = chain_asked(said)
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        address = chains.account_address(account.pubkey, chain)
        if not address:
            return JSONResponse({"detail": f"this account has no {chain.label} address yet"}, status_code=400)
        try:
            try:
                content = base64.b64decode(str(said.get("content", "")), validate=True)
            except ValueError:
                raise ValueError("the content has to arrive encoded in base64, the way a file read in the browser "
                                 "does") from None
            if str(said.get("part") or ""):
                return inscribe_piece(account, chain, address, said, content)
            if not content:
                raise ValueError("there is nothing to inscribe")
            kind = media.standard_type(str(said.get("content_type") or ""),
                                       str(said.get("name") or "")) or "application/octet-stream"
            if str(said.get("run") or ""):
                return inscribe_run_piece(account, chain, address, said, content, kind)
            return inscribe_start(account, chain, address, said, content, kind)
        except (fundinglib.FundingError, inscriptionlib.InscriptionError, V1Error, ValueError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)

    def how_it_is_split(plan) -> tuple:
        bodies = [p[inscriptionlib.CHUNK_HEADER_LEN:] for p in plan.payloads]
        stream = b"".join(bodies)
        _manifest, after = inscriptionlib.Manifest.decode(stream)
        at = len(stream) - len(after)
        size = len(bodies[0])
        return stream[:at], at, size, [b[max(0, at - n * size):] for n, b in enumerate(bodies)]

    def set_pictures(run, chain, piece) -> set[str]:
        have = set_pics.get(run["id"])
        if have is not None:
            return have
        said = inscriptionlib.collection_of(piece["json"])
        collection = (said[0] if said else "") or run["name"]
        try:
            found = {r["sha256"] for r in index_of(chain).collection_items(run["address"], collection)
                     if r.get("sha256")}
        except V1Error:
            return set()
        if len(set_pics) >= SET_PIC_ROOM:
            set_pics.pop(next(iter(set_pics)))
        set_pics[run["id"]] = found
        return found

    def set_landed(run_id: str, digest: str) -> None:
        have = set_pics.get(run_id)
        if have is not None:
            have.add(digest)

    def inscribe_run_piece(account, chain, address: str, said: dict, content: bytes, kind: str):
        run = runs_book.get(str(said.get("run") or ""))
        if run is None:
            raise ValueError("there is no run of that id")
        if run["account"] != account.pubkey.lower():
            raise ValueError("that run belongs to somebody else")
        if run["network"] != chain.network:
            raise ValueError(f"that run is on {run['network']}, not {chain.network}")
        if run["folder"]:
            raise ValueError("that run's pictures are on this node; ask for its pieces with /account/run/piece")
        try:
            edition = int(said.get("piece"))
        except (TypeError, ValueError):
            raise ValueError("which piece was not said") from None
        piece = runs_book.piece(run["id"], edition)
        if piece is None:
            raise ValueError("that run has no such piece")
        if piece["status"] == "sent":
            raise ValueError(f"{piece['name']} is already on the chain ({piece['txid']}). "
                             "Nothing has been paid for twice.")
        if piece["status"] == "failed":
            raise ValueError(f"{piece['name']} was refused ({piece['error']}); retry the run's refused pieces first")
        if piece["status"] == "skipped":
            raise ValueError(f"{piece['name']}: that picture is in the collection already, so this run does not pay "
                             "for it. The pieces after it are still here to ask for.")
        digest = hashlib.sha256(content).hexdigest()
        if piece.get("sha256"):
            if digest != piece["sha256"]:
                raise ValueError(f"those are not the bytes of {piece['name']} this run was written down with "
                                 f"({piece['image']}, {int(piece['size']):,} bytes). A piece is permanent and paid "
                                 "for, so the picture has to be the one the run was made from. Nothing has been paid "
                                 "for.")
        elif len(content) != int(piece["size"]):
            raise ValueError(f"{piece['name']} was written down at {int(piece['size']):,} bytes and these are "
                             f"{len(content):,}. The folder this run was made from is the one its pictures have to "
                             "come out of. Nothing has been paid for.")
        elif digest in set_pictures(run, chain, piece):
            runs_book.piece_skipped(run["id"], edition, "its picture is in the collection already")
            run = runs_book.get(run["id"])
            return JSONResponse({"already": f"{piece['name']}: that picture is in {run['name']} already, so nothing "
                                            "is paid for it.",
                                 "run": run["id"], "piece": edition, "items": run["items"], "sent": run["sent"],
                                 "skipped": run["skipped"], "chain": chain.network})
        else:
            runs_book.record_digest(run["id"], edition, digest)
        if run["status"] == "stopped":
            runs_book.set_status(run["id"], "running")
        answer = inscribe_start(
            account, chain, address, {**said, "name": piece["name"], "json": piece["json"]}, content,
            piece["content_type"] or kind, tag=bytes.fromhex(piece["inscription_id"]),
            on_sent=lambda txid, run_id=run["id"], n=edition, d=digest: (
                runs_book.record_piece(run_id, n, txid), set_landed(run_id, d)),
            charge=piece["status"] != "sending")
        runs_book.offer_piece(run["id"], edition)
        return answer

    def inscribe_start(account, chain, address: str, said: dict, content: bytes, kind: str,
                       tag: bytes | None = None, on_sent: Any = None, charge: bool = True):
        """The first transaction of an inscription: the piece, or the split."""
        label = str(said.get("name") or "").strip()[:60] or kind.split(";")[0]
        digest = hashlib.sha256(content).hexdigest()
        waiting = parts_book.find(account.pubkey, chain.network, digest)
        if waiting is not None:
            return inscribe_again(account, chain, address, waiting)
        tag = tag or secrets.token_bytes(8)
        plan = inscribelib.plan(content, kind, str(said.get("json", "")), inscription_id=tag)
        fee_out, fee_said = operator_fee(chain, address, int(round((plan.estimate.fee + plan.estimate.dust) * COIN)))
        if plan.chunks == 1:
            unsigned = build(chain, address,
                             class_c_or_b(chain, address, plan.payloads[0], coin_pubkey(account.pubkey, chain))
                             + fee_out, f"inscribe {label}")
            again = any(o.digest == digest for o in offers.waiting(account.pubkey) if o.network == chain.network)
            quota(account, "inscribe", len(content), count=charge and not again)
            offer = offers.add(account.pubkey, chain.network, unsigned, unsigned.what, digest=digest, done=on_sent)
            return JSONResponse({"offer": offer.id, "bytes": plan.content_len, "chunks": 1, "chain": chain.network,
                                 "operator_fee": fee_said, **unsigned.as_json()})
        manifest, at, chunk_len, contents = how_it_is_split(plan)
        piece = inscribelib.piece_size(plan)
        unsigned = build(chain, address, [(piece, txbuild.p2pkh_script(address, getattr(chain, "params", None)))] * plan.chunks + fee_out,
                         f"split into {plan.chunks:,} outputs, to inscribe {label} in {plan.chunks:,} transactions")
        quota(account, "inscribe", len(content), count=charge)
        job = parts_book.create(
            account=account.pubkey, address=address, network=chain.network, name=label,
            content_type=plan.content_type, json_text=plan.json, size=len(content), sha256=digest,
            inscription_id=tag.hex(), contents=contents, piece=piece, chunk_len=chunk_len, manifest=manifest,
            fee=plan.estimate.fee, dust=plan.estimate.dust, floor=chain.params.activation_height)
        offer = offers.add(account.pubkey, chain.network, unsigned, unsigned.what,
                           done=lambda txid, job=job: parts_book.note_split(job, txid))
        return JSONResponse({"offer": offer.id, "part": job, "split": "offered", "bytes": plan.content_len,
                             "chunks": plan.chunks, "chunk_len": chunk_len, "manifest_len": at, "sent": 0,
                             "next": 0, "chain": chain.network, "operator_fee": fee_said, **unsigned.as_json()})

    def inscribe_again(account, chain, address: str, row: dict):
        said = {"part": row["id"], "chunks": int(row["chunks"]), "chunk_len": int(row["chunk_len"]),
                "manifest_len": len(bytes.fromhex(row["manifest"])), "bytes": int(row["size"]), "name": row["name"],
                "sent": row["sent"], "next": row["next"], "chain": row["network"], "resumed": True}
        if row["split_txid"]:
            return JSONResponse(said)
        fee_out, fee_said = operator_fee(chain, address,
                                         int(round((float(row["fee"] or 0) + float(row["dust"] or 0)) * COIN)))
        unsigned = build(chain, address, [(int(row["piece"]), txbuild.p2pkh_script(address, getattr(chain, "params", None)))] * int(row["chunks"])
                         + fee_out, f"split into {int(row['chunks']):,} outputs, to inscribe {row['name']} in "
                                    f"{int(row['chunks']):,} transactions")
        offer = offers.add(account.pubkey, chain.network, unsigned, unsigned.what,
                           done=lambda txid, job=row["id"]: parts_book.note_split(job, txid))
        return JSONResponse({**said, "offer": offer.id, "split": "offered", "operator_fee": fee_said,
                             **unsigned.as_json()})

    def inscribe_piece(account, chain, address: str, said: dict, content: bytes):
        """One piece of an inscription already started, paid by its own output of the split."""
        job = str(said.get("part"))
        row = parts_book.get(job)
        if not row or row["account"] != account.pubkey or row["network"] != chain.network:
            raise ValueError("that inscription is not one this node is finishing for this account. Start it again "
                             "from the file.")
        if row["status"] in ("done", "stopped"):
            raise ValueError(f"that inscription is {row['status']}, so there is no piece of it left to offer.")
        try:
            n = int(said.get("chunk"))
        except (TypeError, ValueError):
            raise ValueError("which piece was not said") from None
        if not 0 <= n < int(row["chunks"]):
            raise ValueError(f"that inscription has {int(row['chunks']):,} pieces, numbered 0 to "
                             f"{int(row['chunks']) - 1}.")
        mine = [c for c in parts_book.chunks(job) if int(c["n"]) == n]
        if not mine:
            raise ValueError("that piece was never written down")
        chunk = mine[0]
        if chunk["status"] == "sent":
            raise ValueError(f"piece {n + 1} is already on the chain ({chunk['txid']}). Nothing has been paid for "
                             "twice.")
        if hashlib.sha256(content).digest() != bytes(chunk["digest"]):
            raise ValueError(f"those are not the bytes piece {n + 1} of that inscription was promised to. A piece is "
                             "permanent and paid for, and pieces of two different files make a third file nobody "
                             "chose -- so the file has to be the one that started this. Nothing has been paid for.")
        if not row["split_txid"]:
            raise ValueError("the split has not gone out yet, so there is nothing here to pay for this piece. Ask "
                             "for the file again and sign the split first.")
        how = tx_status(chain, row["split_txid"])
        if how is None:
            raise ValueError("this node cannot tell whether that split is in a block, so it will not offer a piece "
                             "that spends an output it cannot see.")
        if how["conflicted"]:
            parts_book.set_status(job, "stopped", note="the split was replaced by another transaction")
            raise ValueError("the transaction that was to fund this inscription is not coming -- something spending "
                             "the same coins reached a block first. Nothing further has been paid for. Ask again with "
                             "the file and it starts again.")
        if not how["confirmed"]:
            now = parts_book.get(job) or {}
            return JSONResponse({"part": job, "chunk": n, "chunks": int(row["chunks"]), "sent": now.get("sent", 0),
                                 "next": now.get("next"), "chunk_len": int(row["chunk_len"]),
                                 "manifest_len": len(bytes.fromhex(row["manifest"])),
                                 "waiting": "the split is in the mempool, and these pieces spend its outputs, so they "
                                            "go out together once it is in a block -- about a minute, and no minute "
                                            "in between them."})
        size = int(row["chunk_len"])
        body = bytes.fromhex(row["manifest"])[n * size:(n + 1) * size] + content
        try:
            gone = v1.core(chain.network, "gettxout", row["split_txid"], n, True) is None
        except V1Error:
            gone = False
        if gone and not int((parts_book.get(job) or {}).get("sent") or 0):
            parts_book.resplit(job)
            again = inscribe_again(account, chain, address, parts_book.get(job))
            said_again = json.loads(again.body)
            said_again["resplit"] = ("its coins were spent by a later send before any piece went out, so it sets "
                                     "aside new ones: one new split, then the pieces. Nothing has been paid for twice.")
            return JSONResponse(said_again)
        payload = inscriptionlib.Chunk(inscription_id=bytes.fromhex(row["inscription_id"]),
                                       countdown=int(row["chunks"]) - 1 - n, body=body).encode()
        what = f"piece {n + 1:,} of {int(row['chunks']):,} of {row['name']}, on the chain forever"
        outputs = class_c_or_b(chain, address, payload, coin_pubkey(account.pubkey, chain))
        if gone:
            unsigned = build(chain, address, outputs, what)
        else:
            unsigned = fundinglib.build_one(chain.params, address,
                                            {"txid": row["split_txid"], "vout": n, "value": int(row["piece"]),
                                             "address": address}, outputs, rate=fees.MIN_FEE_PER_KB, what=what)
        quota(account, "inscribe", count=False)
        parts_book.offer_chunk(job, n)
        offer = offers.add(account.pubkey, chain.network, unsigned, unsigned.what,
                           done=lambda txid, job=job, n=n: parts_book.record_chunk(job, n, txid))
        now = parts_book.get(job) or {}
        return JSONResponse({"offer": offer.id, "part": job, "chunk": n, "sent": now.get("sent", 0),
                             "next": now.get("next"), "chunks": int(row["chunks"]), "chain": chain.network,
                             **unsigned.as_json()})

    @app.get("/account/inscribe/unfinished")
    def account_inscribe_unfinished(request: Request):
        """This account's inscriptions that still owe pieces, and finished ones whose block has not landed."""
        account = signed_in_account(request)
        out = []
        for row in parts_book.list(account.pubkey):
            if row.get("status") == "done" and time.time() - float(row.get("created") or 0) < 2 * 86400:
                root = next((c["txid"] for c in parts_book.chunks(row["id"]) if int(c["n"]) == 0 and c.get("txid")),
                            "")
                one = chains.by_network(str(row.get("network") or ""))
                landed = True if one is None or not root else index_of(one).inscription(root) is not None
                if root and not landed:
                    out.append({"part": row["id"], "name": row.get("name") or "a file",
                                "chunks": int(row.get("chunks") or 0), "sent": int(row.get("sent") or 0),
                                "pending": True, "network": row.get("network") or ""})
                continue
            if row.get("status") not in ("open", "running", "failed"):
                continue
            out.append({"part": row["id"], "name": row.get("name") or "a file", "chunks": int(row.get("chunks") or 0),
                        "sent": int(row.get("sent") or 0), "bytes": int(row.get("size") or 0),
                        "sha256": row.get("sha256") or "", "split": bool(row.get("split_txid")),
                        "network": row.get("network") or ""})
        return JSONResponse({"unfinished": out})

    @app.post("/account/inscribe/abandon")
    def account_inscribe_abandon(request: Request, payload: Any = Body(None)):
        account = signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        row = parts_book.get(str(said.get("part") or ""))
        if not row or row.get("account") != account.pubkey:
            return JSONResponse({"detail": "that is not an inscription of yours"}, status_code=404)
        parts_book.set_status(row["id"], "stopped", note="given up by its owner")
        return JSONResponse({"ok": True})

    # ---------------------------------------------------------------- collection runs
    def upload_dir() -> Path:
        where = st.home / "collections"
        where.mkdir(parents=True, exist_ok=True, mode=0o700)
        return where

    def save_upload(files: list[UploadFile], root: Path | None = None) -> Path:
        """Uploaded files laid out the way HashLips does, by name (json/, images/)."""
        if root is None:
            root = upload_dir() / secrets.token_hex(4)
        (root / "json").mkdir(parents=True, exist_ok=True)
        (root / "images").mkdir(exist_ok=True)
        kept = 0
        for upload in files:
            name = Path(upload.filename or "").name
            if not name or name.startswith("."):
                continue
            if name == "_metadata.json" or (name.endswith(".json") and name[:-5].isdigit()):
                target = root / "json" / name
            elif name.lower().endswith(collectionlib.IMAGE_SUFFIXES):
                target = root / "images" / name
            else:
                continue
            with target.open("wb") as out:
                while True:
                    block = upload.file.read(1 << 20)
                    if not block:
                        break
                    out.write(block)
            kept += 1
        if not kept:
            raise ValueError("none of the chosen files were metadata or images.")
        return root

    def without(b, editions: set[int]):
        if not editions:
            return b
        return dataclasses.replace(b, items=[i for i in b.items if i.edition not in editions])

    def what_the_account_has(account, chain, address: str, b, label: str = "") -> dict[str, Any]:
        out: dict[str, Any] = {"blocked": "", "note": "", "skip": set()}
        collection = b.collection
        if not address or not collection:
            return out
        floor = chain.params.activation_height
        named = {collection, label.strip()[:60]} - {""}
        mine = [r for r in runs_book.list(account=account.pubkey, network=chain.network) if r["name"] in named]
        try:
            done = index_of(chain).collection_editions(address, collection)
        except V1Error:
            done = set()
        out["skip"] = {item.edition for item in b.items if item.edition in done}
        if out["skip"] and len(out["skip"]) == len(b.items):
            out["blocked"] = (f"{collection} is already on this chain from this address, all {len(out['skip']):,} "
                              "pieces of it. Inscribing it again would pay for a second copy of every item, and no "
                              "node would file the copies into the set. Nothing has been paid for.")
            return out
        if not out["skip"]:
            finished = [r for r in mine if r.get("floor") == floor and r["status"] == "done"]
            if finished:
                r = finished[-1]
                from .legacy import _ago
                out["blocked"] = (f"this account already inscribed {collection} on this chain -- run {r['id']}, "
                                  f"started {_ago(float(r.get('created') or 0))}, {r['items']:,} pieces. None of it "
                                  "is indexed yet, which is the minutes between the last piece being sent and its "
                                  "block. Wait for it rather than pay for the set twice. Nothing has been paid for.")
                return out
            if [r for r in mine if r.get("floor") != floor]:
                out["note"] = (f"a run of {collection} from before this chain started at {floor:,} is being "
                               "ignored: the pieces it sent are below the floor, so no node reads them.")
            return out
        left = len(b.items) - len(out["skip"])
        out["note"] = (f"{len(out['skip']):,} of these are already on this chain from this address, so this run is "
                       f"written down as the other {left:,}. A second copy joins nothing and costs again.")
        return out

    def run_ahead(account, chain):
        ahead = runs_book.due(account.pubkey, chain.network)
        if ahead is None:
            return None
        return JSONResponse({"detail": f"{ahead['name']} is still going, at {ahead['sent']} of {ahead['items']} "
                                       "pieces. Finish it or stop it before starting another. Nothing has been paid "
                                       "for."}, status_code=400)

    @app.post("/account/run/upload")
    def account_run_upload(request: Request, files: list[UploadFile] = File([]), upload: str = Form("")):
        account = signed_in_account(request)
        if upload:
            if not re.fullmatch(r"[0-9a-f]{8}", upload) or uploads.get(upload) != account.pubkey.lower():
                return JSONResponse({"detail": "that upload is not this account's (the node may have restarted): "
                                               "choose the folder again"}, status_code=400)
        else:
            upload = secrets.token_hex(4)
            uploads[upload] = account.pubkey.lower()
        try:
            save_upload(files, upload_dir() / upload)
        except (ValueError, OSError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        return JSONResponse({"upload": upload})

    @app.post("/account/run/start")
    def account_run_start(request: Request, files: list[UploadFile] = File([]), name: str = Form(""),
                          run_chain: str = Form(""), max_supply: str = Form(""), numbering: str = Form("continue"),
                          description: str = Form(""), url: str = Form(""), artist: str = Form(""),
                          icon: str = Form(""), upload: str = Form("")):
        """Write an uploaded collection down as this account's run. Nothing is inscribed."""
        account = signed_in_account(request)
        try:
            chain = chain_asked({"chain": run_chain})
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        address = chains.account_address(account.pubkey, chain)
        if not address:
            return JSONResponse({"detail": f"this account has no {chain.label} address yet"}, status_code=400)
        refused = run_ahead(account, chain)
        if refused is not None:
            return refused
        try:
            if upload:
                if not re.fullmatch(r"[0-9a-f]{8}", upload) or uploads.get(upload) != account.pubkey.lower():
                    raise ValueError("that upload is not this account's (the node may have restarted): choose the "
                                     "folder again")
                folder = upload_dir() / upload
            else:
                folder = save_upload(files)
            b = collectionlib.read_build(collectionlib.find_build(folder))
        except (collectionlib.CollectionError, ValueError, OSError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        return write_run(account, chain, address, b, str(b.folder), name=name, max_supply=max_supply,
                         numbering=numbering, description=description, url=url, artist=artist, icon=icon)

    @app.post("/account/run/plan")
    def account_run_plan(request: Request, payload: Any = Body(None)):
        """Write a collection down from what the browser read off the folder; its pictures stay in the browser."""
        account = signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        try:
            chain = chain_asked({"chain": said.get("run_chain") or said.get("chain")})
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        address = chains.account_address(account.pubkey, chain)
        if not address:
            return JSONResponse({"detail": f"this account has no {chain.label} address yet"}, status_code=400)
        refused = run_ahead(account, chain)
        if refused is not None:
            return refused
        try:
            b = collectionlib.build_from_manifest(said.get("items"))
        except collectionlib.CollectionError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        words = {k: str(said.get(k) or "") for k in
                 ("name", "max_supply", "numbering", "description", "url", "artist", "icon")}
        words["numbering"] = words["numbering"] or "continue"
        return write_run(account, chain, address, b, "", **words)

    def write_run(account, chain, address: str, b, folder: str, *, name: str = "", max_supply: str = "",
                  numbering: str = "continue", description: str = "", url: str = "", artist: str = "",
                  icon: str = ""):
        def digest_of(item) -> str:
            if getattr(item, "sha256", ""):
                return item.sha256
            if not folder:
                return ""
            return hashlib.sha256((b.folder / item.image).read_bytes()).hexdigest()
        index = index_of(chain)
        try:
            items_up = index.collection_items(address, b.collection)
        except V1Error:
            items_up = []
        taken = {int(r["edition"]) for r in items_up if r.get("edition") is not None}
        cap = None
        repeat_note = ""
        if taken:
            have = {r["sha256"] for r in items_up if r.get("sha256")}
            known = [(item, digest_of(item)) for item in b.items]
            fresh = [item for item, digest in known if not digest or digest not in have]
            blind = sum(1 for _item, digest in known if not digest)
            if not fresh:
                return JSONResponse({"detail": (
                    f"{b.collection} is already on this chain from this address, all {len(b.items):,} pieces of it. "
                    "Inscribing it again would pay for a second copy of every item, and no node would file the "
                    "copies into the set. Nothing has been paid for.")}, status_code=400)
            if len(fresh) < len(b.items):
                repeat_note = (f"{len(b.items) - len(fresh):,} of these are already on this chain from this address, "
                               f"so this run is written down as the other {len(fresh):,}. A second copy joins nothing "
                               "and costs again.")
                b = dataclasses.replace(b, items=fresh)
            if blind:
                repeat_note = (repeat_note + " " if repeat_note else "") + (
                    f"{blind:,} of them cannot be compared with what is on the chain from here -- their pictures stay "
                    "in your browser -- so each is checked when its turn comes, and one that is already in the "
                    "collection is skipped and costs nothing.")
            first = min((r for r in items_up if r.get("edition") is not None), key=lambda r: int(r["edition"]))
            cap = inscriptionlib.collection_details(first.get("rawjson") or "").get("supply")
            if numbering != "keep":
                b = collectionlib.renumbered(b, max(taken) + 1)
            top = max(item.edition for item in b.items)
            if cap and top > cap:
                room = max(0, cap - len(taken))
                return JSONResponse({"detail": (
                    f"{b.collection} is sealed at {cap:,} by its #1, and {len(taken):,} are on the chain, so there is "
                    f"room for {room:,} more, not {len(b.items):,}. Nothing has been paid for.")}, status_code=400)
        else:
            try:
                supply = int(str(max_supply or "0").replace(",", "").strip() or 0)
            except ValueError:
                return JSONResponse({"detail": "the maximum supply is a whole number, or 0 for no limit"},
                                    status_code=400)
            top = max(item.edition for item in b.items)
            if supply < 0 or (supply and supply < top):
                return JSONResponse({"detail": (
                    f"a maximum supply of {supply:,} is smaller than this build, whose editions go up to #{top:,}. "
                    f"Set it to at least {top:,}, or 0 for no limit.")}, status_code=400)
            details = {"description": description, "url": url, "artist": artist,
                       "icon": inscriptionlib.inscription_in(icon or ""), "supply": supply}
            b = collectionlib.with_details(b, details)
            cap = supply or None
        already = ({"blocked": "", "note": repeat_note, "skip": set()} if taken
                   else what_the_account_has(account, chain, address, b, name))
        if already["blocked"]:
            return JSONResponse({"detail": already["blocked"]}, status_code=400)
        if already["skip"]:
            b = without(b, already["skip"])
        try:
            run_id = runs_book.create(account.pubkey, address, b, chain.network,
                                      name=name.strip()[:60] or b.collection,
                                      floor=chain.params.activation_height, folder=folder)
        except (collectionlib.CollectionError, accountrunslib.TooBig, ValueError, OSError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        if already["note"]:
            runs_book.note(run_id, already["note"])
        run = runs_book.get(run_id)
        return JSONResponse({"run": run_id, "name": run["name"], "items": run["items"], "fee": run["fee"],
                             "dust": run["dust"], "chain": chain.network, "note": run["note"], "next": run["next"],
                             "supply": cap, "continues": bool(taken), "on_chain": len(taken)})

    @app.post("/account/run")
    def account_runs(request: Request):
        account = signed_in_account(request)
        mine = list(runs_book.list(account=account.pubkey))
        mine.sort(key=lambda r: (r["status"] == "done", -r["created"]))
        return JSONResponse({"runs": [{"run": r["id"], "name": r["name"], "items": r["items"], "sent": r["sent"],
                                       "status": r["status"], "sending": r["sending"], "refused": r["failed_pieces"],
                                       "skipped": r["skipped"], "note": r["note"], "next": r["next"],
                                       "chain": r["network"]} for r in mine]})

    def own_run_or_answer(request: Request, said: dict):
        account = signed_in_account(request)
        run = runs_book.get(str(said.get("run", "")))
        if run is None:
            return account, None, JSONResponse({"detail": "there is no run of that id"}, status_code=404)
        if run["account"] != account.pubkey.lower():
            return account, None, JSONResponse({"detail": "that run belongs to somebody else"}, status_code=403)
        return account, run, None

    @app.post("/account/run/stop")
    def account_run_stop(request: Request, payload: Any = Body(None)):
        _account, run, refused = own_run_or_answer(request, payload if isinstance(payload, dict) else {})
        if refused is not None:
            return refused
        runs_book.set_status(run["id"], "stopped", only_from=("open", "running", "failed"))
        run = runs_book.get(run["id"])
        return JSONResponse({"run": run["id"], "name": run["name"], "sent": run["sent"], "items": run["items"],
                             "status": run["status"]})

    @app.post("/account/run/retry")
    def account_run_retry(request: Request, payload: Any = Body(None)):
        _account, run, refused = own_run_or_answer(request, payload if isinstance(payload, dict) else {})
        if refused is not None:
            return refused
        if not run["failed_pieces"]:
            return JSONResponse({"run": run["id"], "retried": 0, "status": run["status"], "refused": []})
        reasons = [f"{p['name']}: {p['error']}" for p in runs_book.pieces(run["id"], status="failed")]
        left = runs_book.retry_failed(run["id"])
        run = runs_book.get(run["id"])
        return JSONResponse({"run": run["id"], "retried": left, "sent": run["sent"], "items": run["items"],
                             "status": run["status"], "refused": reasons})

    @app.post("/account/run/delete")
    def account_run_delete(request: Request, payload: Any = Body(None)):
        _account, run, refused = own_run_or_answer(request, payload if isinstance(payload, dict) else {})
        if refused is not None:
            return refused
        if run["sending"] and run["status"] != "stopped":
            return JSONResponse({"detail": "one of its pieces is out for a signature. Sign it, let it expire, or "
                                           "stop the run first -- a run deleted under a signature that later "
                                           "arrives leaves that inscription unrecorded. Nothing has been paid for."},
                                status_code=400)
        folder = Path(run["folder"]) if run["folder"] else None
        runs_book.delete(run["id"])
        set_pics.pop(run["id"], None)
        if folder is not None and folder.exists():
            try:
                inside = folder.resolve().is_relative_to(upload_dir().resolve())
            except OSError:
                inside = False
            if inside:
                shutil.rmtree(folder, ignore_errors=True)
        return JSONResponse({"run": run["id"], "name": run["name"], "forgotten": True})

    def chain_too_long(chain, unsigned) -> bool:
        for coin in unsigned.inputs:
            try:
                entry = v1.core(chain.network, "getmempoolentry", coin["txid"]) or {}
            except V1Error:
                continue
            if (int(entry.get("ancestorcount", 0)) >= MEMPOOL_CHAIN_ROOM
                    or int(entry.get("descendantcount", 0)) >= MEMPOOL_CHAIN_ROOM):
                return True
        return False

    @app.post("/account/run/piece")
    def account_run_piece(request: Request, payload: Any = Body(None)):
        """Offer the next piece of a run, or say that it is finished."""
        account, run, refused = own_run_or_answer(request, payload if isinstance(payload, dict) else {})
        if refused is not None:
            return refused
        chain = chain_on(run["network"])
        piece = runs_book.next_piece(run["id"])
        if piece is not None and not run["folder"]:
            if run["status"] == "stopped":
                runs_book.set_status(run["id"], "running")
            return JSONResponse({"held": "browser", "run": run["id"], "piece": piece["edition"],
                                 "items": run["items"], "sent": run["sent"], "skipped": run["skipped"],
                                 "name": piece["name"], "image": piece["image"], "bytes": int(piece["size"]),
                                 "chunks": int(piece["chunks"] or 1), "sha256": piece.get("sha256") or "",
                                 "content_type": piece["content_type"], "json": piece["json"],
                                 "chain": chain.network, "operator_fee": operator_fee(chain, run["address"], 0)[1]})
        if piece is None:
            if run["failed_pieces"]:
                return JSONResponse({"detail": f"{run['failed_pieces']:,} of {run['name']} refused and will not go "
                                               "out as this run is written. Nothing has been paid for. Retry them if "
                                               "the reason was on this node's side; if the build itself is short, "
                                               "upload the set again and it will be written down as only what is "
                                               "still missing."}, status_code=400)
            return JSONResponse({"run": run["id"], "sent": run["sent"], "skipped": run["skipped"],
                                 "items": run["items"], "finished": True})
        if int(piece.get("chunks") or 1) > 1:
            if not (Path(run["folder"]) / piece["image"]).is_file():
                runs_book.piece_failed(run["id"], piece["edition"], "the picture is not in the build this node kept")
                return JSONResponse({"detail": f"{piece['name']}: the picture is not in the build this node kept. "
                                               "Nothing has been paid for."}, status_code=400)
            if run["status"] == "stopped":
                runs_book.set_status(run["id"], "running")
            runs_book.offer_piece(run["id"], piece["edition"])
            return JSONResponse({"big": True, "run": run["id"], "piece": piece["edition"], "items": run["items"],
                                 "sent": run["sent"], "skipped": run["skipped"], "name": piece["name"],
                                 "bytes": int(piece["size"]), "chunks": int(piece["chunks"]),
                                 "content_type": piece["content_type"], "json": piece["json"],
                                 "chain": chain.network, "operator_fee": operator_fee(chain, run["address"], 0)[1]})
        try:
            content = (Path(run["folder"]) / piece["image"]).read_bytes()
            plan = inscribelib.plan(content, piece["content_type"], piece["json"],
                                    inscription_id=bytes.fromhex(piece["inscription_id"]))
        except (OSError, ValueError) as exc:
            runs_book.piece_failed(run["id"], piece["edition"], str(exc))
            return JSONResponse({"detail": f"{piece['name']} cannot be built from the build this node kept: {exc}. "
                                           "Nothing has been paid for, and the pieces after it are still here to "
                                           "ask for."}, status_code=400)
        fee_out, fee_said = operator_fee(chain, run["address"],
                                         int(round((plan.estimate.fee + plan.estimate.dust) * COIN)))
        try:
            outputs = class_c_or_b(chain, run["address"], plan.payloads[0],
                                   coin_pubkey(account.pubkey, chain)) + fee_out
            unsigned = build(chain, run["address"], outputs, f"inscribe {piece['name']}")
            if chain_too_long(chain, unsigned):
                raise ValueError(f"{MEMPOOL_CHAIN_ROOM} of this account's transactions are waiting for a block, "
                                 "which is as many in a row as the network holds, so the next piece waits for the "
                                 "next block: try again in 1 minute.")
            quota(account, "inscribe", len(content), count=piece.get("status") != "sending")
        except (fundinglib.FundingError, V1Error, ValueError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        if run["status"] == "stopped":
            runs_book.set_status(run["id"], "running")
        runs_book.offer_piece(run["id"], piece["edition"])
        offer = offers.add(account.pubkey, chain.network, unsigned, unsigned.what,
                           done=lambda txid, r=run["id"], e=piece["edition"]: runs_book.record_piece(r, e, txid))
        return JSONResponse({"offer": offer.id, "run": run["id"], "piece": piece["edition"], "items": run["items"],
                             "sent": run["sent"], "name": piece["name"], "bytes": plan.content_len,
                             "operator_fee": fee_said, "chain": chain.network, **unsigned.as_json()})

    @app.get("/account/run/item")
    def account_run_item(request: Request, run: str = "", edition: int = 0):
        """The bytes of one item of this account's own run, as uploaded; only files the run lists."""
        _account, row, refused = own_run_or_answer(request, {"run": run})
        if refused is not None:
            raise HTTPException(refused.status_code, json.loads(refused.body)["detail"])
        piece = runs_book.piece(row["id"], int(edition))
        if piece is None:
            raise HTTPException(404, "that run has no such item")
        if not row["folder"]:
            raise HTTPException(404, "the picture is not in the build this node kept")
        path = Path(row["folder"]) / piece["image"]
        if not path.is_file() or Path(row["folder"]).resolve() not in path.resolve().parents:
            raise HTTPException(404, "the picture is not in the build this node kept")
        return Response(path.read_bytes(), media_type=piece["content_type"], headers={"Cache-Control": "no-store"})

    @app.post("/account/run/done")
    def account_run_done(request: Request, payload: Any = Body(None)):
        said = payload if isinstance(payload, dict) else {}
        _account, row, refused = own_run_or_answer(request, said)
        if refused is not None:
            return refused
        txid = str(said.get("txid", "")).lower()
        if not re.fullmatch(r"[0-9a-f]{64}", txid):
            return JSONResponse({"detail": "that is not a transaction id"}, status_code=400)
        try:
            edition = int(said.get("piece"))
        except (TypeError, ValueError):
            return JSONResponse({"detail": "which item?"}, status_code=400)
        piece = runs_book.piece(row["id"], edition)
        if piece is None or (row["folder"] and int(piece.get("chunks") or 1) < 2):
            return JSONResponse({"detail": "that is not an item of more than one transaction"}, status_code=400)
        if piece["status"] != "sent":
            runs_book.record_piece(row["id"], edition, txid)
        return JSONResponse({"run": row["id"], "piece": edition, "sent": True})

    # ---------------------------------------------------------------- mintpads as inscriptions
    def mintpad_look(seller: str, name: str) -> dict:
        said = st.setting(f"mintpad:{seller}:{name}", {}) or {}
        look = said.get("template") if isinstance(said, dict) else None
        return {"template": look if look in MINTPAD_TEMPLATES else "spotlight",
                "blurb": str((said or {}).get("blurb") or "")[:300] if isinstance(said, dict) else ""}

    @app.post("/account/mintpad/inscribe")
    def account_mintpad_inscribe(request: Request, payload: Any = Body(None)):
        """The inscription that puts this account's mintpad for one of its collections on the chain."""
        account = signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        try:
            chain = chain_asked(said)
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        address = chains.account_address(account.pubkey, chain)
        name = str(said.get("collection") or "").strip()[:120]
        if not address or not name:
            return JSONResponse({"detail": "which collection?"}, status_code=400)
        # NEEDS-V1 (the exchange module): the original also accepted a collection it only had listings of.
        mine = chain.network != "test"
        if not mine:
            code, _got = v1.page(f"collection/{address}/{name}", {"limit": 1})
            mine = code == 200
        if not mine:
            return JSONResponse({"detail": "that is not one of your collections"}, status_code=404)
        look = mintpad_look(address, name)
        try:
            content = mintpadlib.account_page(address, name, look["template"], look["blurb"])
        except mintpadlib.MintpadError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        meta = json.dumps({"name": f"{name} mintpad",
                           "mintpad": {"creator": address, "collection": name, "look": look["template"]}})
        try:
            return inscribe_start(account, chain, address, {"name": f"{name} mintpad", "json": meta}, content,
                                  "text/html")
        except (fundinglib.FundingError, V1Error, ValueError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.post("/account/tokenpad/inscribe")
    def account_tokenpad_inscribe(request: Request, payload: Any = Body(None)):
        """The inscription of a token mintpad: `lot` of the token per mint, out of this account's own ask."""
        account = signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        try:
            chain = chain_asked(said)
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        address = chains.account_address(account.pubkey, chain)
        index = index_of(chain)
        try:
            pid = int_said(said, "property_id", "the token number")
            prop = index.property(pid)
            if prop is None:
                raise ValueError("which token?")
            lot = parse_amount(str(said.get("lot") or ""), bool(prop["divisible"]))
            total = parse_amount(str(said.get("total") or "0") or "0", bool(prop["divisible"])) \
                if str(said.get("total") or "").strip() else 0
            if lot <= 0:
                raise ValueError("how many tokens go in one mint?")
            look = str(said.get("look") or "counter")
            face = tokenlib.details(prop)
            if face.get("icon"):
                row = index.inscription(face["icon"])
                kind = str((row or {}).get("contenttype") or (row or {}).get("content_type") or "")
                if not row or not kind.startswith("image/"):
                    face["icon"] = ""
            content = mintpadlib.account_token_page(address, prop["property_id"], prop["name"], lot, total, look,
                                                    str(said.get("blurb") or ""), face.get("icon", ""))
        except (ValueError, AmountError, mintpadlib.MintpadError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        meta = json.dumps({"name": f"{prop['name']} mintpad",
                           "tokenpad": {"creator": address, "property_id": prop["property_id"], "lot": lot,
                                        "look": look}})
        try:
            return inscribe_start(account, chain, address, {"name": f"{prop['name']} mintpad", "json": meta},
                                  content, "text/html")
        except (fundinglib.FundingError, V1Error, ValueError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)

    # ---------------------------------------------------------------- ledger tokens
    def int_said(said: dict, field: str, label: str, default: int = 0) -> int:
        value = said.get(field)
        if value is None or value == "":
            return default
        if isinstance(value, (list, dict)):
            raise ValueError(f"{label} has to be a number")
        try:
            return int(value)
        except (TypeError, ValueError, OverflowError):
            raise ValueError(f"{label} has to be a number") from None

    def name_is_taken(name: str, chain) -> str:
        complaint = statelib.name_complaint(name)
        if complaint:
            return complaint
        wanted = statelib.name_key(name)
        try:
            taken = [p for p in index_of(chain).properties() if statelib.name_key(p["name"]) == wanted]
        except V1Error:
            return ""
        if taken:
            return (f"{taken[0]['name']} is already token #{taken[0]['property_id']} on this chain. One name is one "
                    "token: pick another.")
        now = time.time()
        named_tokens[:] = [t for t in named_tokens if now - t["at"] < 3600]
        for item in named_tokens:
            if item["network"] == chain.network and statelib.name_key(item["name"]) == wanted:
                return (f"this arcade broadcast a token called {name.strip()} a moment ago ({item['txid'][:12]}...). "
                        "Wait for its block rather than pay for the same name twice.")
        return ""

    @app.post("/account/token/create")
    def account_token_create(request: Request, payload: Any = Body(None)):
        """Offer to create a token as this account (type 50 or 54, unwrapped). Nothing is broadcast here."""
        account = signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        try:
            chain = chain_asked(said)
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        address = chains.account_address(account.pubkey, chain)
        if not address:
            return JSONResponse({"detail": f"this account has no {chain.label} address yet"}, status_code=400)
        name = str(said.get("name") or "")
        try:
            divisible = str(said.get("units") or "divisible") != "indivisible"
            managed = str(said.get("kind") or "fixed") == "managed"
            amount = None if managed else parse_amount(str(said.get("supply") or ""), divisible)
            complaint = name_is_taken(name, chain)
            if complaint:
                raise tokenlib.TokenError(complaint)
            icon = str(said.get("icon") or "")
            if icon.strip() and not tokenlib.icon_in(icon):
                raise tokenlib.TokenError("an icon is an inscription on this chain: paste its /content/ link or its "
                                          "id, not a picture from elsewhere.")
            body = tokenlib.issuance_payload(
                name=name, divisible=divisible, managed=managed, amount=amount,
                category=str(said.get("category") or ""), subcategory=str(said.get("subcategory") or ""),
                url=str(said.get("url") or ""), data=tokenlib.data_with_icon(str(said.get("data") or ""), icon))
            outputs = class_c_or_b(chain, address, body, coin_pubkey(account.pubkey, chain), wrap=False)
            what = f"create the token {name.strip()}"
            db = CoinDB(v1, chain)
            unsigned = fundinglib.build(db, chain.params, address, outputs, rate=fees.MIN_FEE_PER_KB, what=what)
            fee_out, fee_said = operator_fee(chain, address, unsigned.fee + sum(v for v, _ in outputs))
            if fee_out:
                unsigned = fundinglib.build(db, chain.params, address, outputs + fee_out, rate=fees.MIN_FEE_PER_KB,
                                            what=what)
            quota(account, "issue", len(body))
        except (tokenlib.TokenError, fundinglib.FundingError, AmountError, V1Error, ValueError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)

        def created(txid, n=name.strip(), net=chain.network):
            named_tokens.append({"name": n, "network": net, "txid": txid, "at": time.time()})
        offer = offers.add(account.pubkey, chain.network, unsigned, unsigned.what, done=created)
        return JSONResponse({"offer": offer.id, "name": name.strip(), "managed": managed, "chain": chain.network,
                             "operator_fee": fee_said, "class": ("B" if len(outputs) > 1 else "C"),
                             **unsigned.as_json()})

    @app.post("/account/token/manage")
    def account_token_manage(request: Request, payload: Any = Body(None)):
        """An issuer's move on a token this account issued: grant, revoke, or hand the issuer role on."""
        account = signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        try:
            chain = chain_asked(said)
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        address = chains.account_address(account.pubkey, chain)
        if not address:
            return JSONResponse({"detail": f"this account has no {chain.label} address yet"}, status_code=400)
        action = str(said.get("action") or "")
        try:
            index = index_of(chain)
            property_id = int_said(said, "property_id", "the token number")
            prop = index.property(property_id)
            if prop is None:
                raise tokenlib.TokenError(f"there is no token {property_id}.")
            if prop["issuer"] != address:
                raise tokenlib.TokenError(f"only {prop['name']}'s issuer can do that, and it is not this account.")
            note = str(said.get("note") or "")
            to, to_tag = None, ""
            if action in ("grant", "revoke"):
                if not prop["managed"]:
                    raise tokenlib.TokenError(f"{prop['name']} has a fixed supply: nothing can be granted or revoked, "
                                              "only sent.")
                amount = parse_amount(str(said.get("amount", "")), prop["divisible"])
                shown = f"{format_amount(amount, prop['divisible'])} {prop['name']}"
                if action == "grant":
                    if str(said.get("to") or "").strip():
                        to, to_tag = app.state.payee(said.get("to"), chain)
                        complaint = chains.check_address(to, chain)
                        if complaint:
                            raise ValueError(complaint)
                    if to == address:
                        to, to_tag = None, ""
                    body = tokenlib.grant_payload(property_id, amount, note)
                    what = f"grant {shown} to " + (f"@{to_tag}" if to_tag else (to or "yourself"))
                else:
                    held = index.balance(address, property_id)
                    if amount > held:
                        raise tokenlib.TokenError(f"this account holds {format_amount(held, prop['divisible'])} "
                                                  f"{prop['name']}, which is all it can revoke.")
                    body = tokenlib.revoke_payload(property_id, amount, note)
                    what = f"revoke {shown} (they are destroyed)"
            elif action == "issuer":
                to, to_tag = app.state.payee(said.get("to"), chain)
                complaint = chains.check_address(to, chain)
                if complaint:
                    raise ValueError(complaint)
                if to == address:
                    raise ValueError("this account is already the issuer.")
                body = tokenlib.change_issuer_payload(property_id)
                what = f"hand {prop['name']}'s issuer role to " + (f"@{to_tag}" if to_tag else to)
            else:
                raise ValueError("grant, revoke or issuer, please.")
            outputs = class_c_or_b(chain, address, body, coin_pubkey(account.pubkey, chain), wrap=False)
            if to:
                outputs.append((sendermod.OUTPUT_VALUE, txbuild.p2pkh_script(to)))
            unsigned = build(chain, address, outputs, what)
            quota(account, "send")
        except (taglib.TagError, tokenlib.TokenError, fundinglib.FundingError, AmountError, V1Error,
                ValueError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        offer = offers.add(account.pubkey, chain.network, unsigned, unsigned.what)
        return JSONResponse({"offer": offer.id, "action": action, "to": to or "", "to_tag": to_tag,
                             "property_id": property_id, "name": prop["name"], "chain": chain.network,
                             **unsigned.as_json()})

    # ---------------------------------------------------------------- DRC-20 / PRC-20 sends
    def coin_key_of(said: dict, address: str) -> bytes:
        try:
            key = bytes.fromhex(str(said.get("pubkey") or ""))
        except ValueError:
            key = b""
        if len(key) != 33 or txbuild.p2pkh_script(address) != b"\x76\xa9\x14" + hash160(key) + b"\x88\xac":
            raise ValueError("that is not the key of this account's address")
        return key

    def ord_walked(chain) -> int:
        return int((v1.cmd("ord", chain=chain.key) or {}).get("indexed_to") or 0)

    def t20_pending_units(chain, address: str, walked_to: int) -> dict[str, int]:
        now = time.time()
        kept, out = [], {}
        for e in t20_pending.get((chain.network, address), []):
            run = e["run"]
            if now - e["at"] > T20_PENDING_SECONDS:
                continue
            reveal = run["txids"].get("reveal")
            if reveal is None and now - e["at"] > T20_SEND_SECONDS:
                continue
            if reveal and walked_to:
                try:
                    conf = int((v1.core(chain.network, "getrawtransaction", reveal, 1) or {}).get("confirmations")
                               or 0)
                    height = int(v1.core(chain.network, "getblockcount")) - conf + 1 if conf else None
                except V1Error:
                    height = None
                if height is not None and height < walked_to:
                    continue
            kept.append(e)
            out[e["tick"]] = out.get(e["tick"], 0) + int(e["units"])
        t20_pending[(chain.network, address)] = kept
        return out

    def t20_listed_units(account, chain, tick: str, decimals: int) -> int:
        hook = getattr(app.state, "listed_token20", None)
        if hook is None:
            return 0
        return int(hook(account, chain.network, tick.lower()) or 0) * 10 ** int(decimals or 0)

    def note_send(chain, address: str, coin: dict, to: str, what: str):
        """funding.build_note_send's vouched path (the note's coin first, whole to `to`; clean coins pay the fee),
        without the original's inscription guard, which is V1.0's held_back here."""
        params = chain.params
        db = CoinDB(v1, chain)
        first = txbuild.p2pkh_script(to, getattr(chain, "params", None))
        paid = [(int(coin["value"]), first)]
        keep_out = {(coin["txid"], int(coin["vout"]))}
        rate = fees.rate_for(params, fees.MIN_FEE_PER_KB)
        net = getattr(params, "name", None)
        fee = fundinglib.price(2, paid, rate, change=True, params=params)
        fees_in = fundinglib.choose(db, address, fee, exclude=keep_out, network=net)
        fee = fundinglib.price(1 + len(fees_in), paid, rate, change=True, params=params)
        if sum(c["value"] for c in fees_in) < fee:
            fees_in = fundinglib.choose(db, address, fee, exclude=keep_out, network=net)
            fee = fundinglib.price(1 + len(fees_in), paid, rate, change=True, params=params)
        chosen = [dict(coin, vout=int(coin["vout"]), address=coin.get("address") or address)] + fees_in
        change = sum(c["value"] for c in fees_in) - fee
        outputs = list(paid)
        if change >= fees.dust_limit(params):
            outputs.append((change, txbuild.p2pkh_script(address, getattr(chain, "params", None))))
        else:
            fee += max(0, change)
            change = 0
        raw = fundinglib.build_raw_tx([(c["txid"], c["vout"]) for c in chosen], outputs)
        script = txbuild.p2pkh_script(address, getattr(chain, "params", None))
        hashes = [fundinglib.sighash(chosen, outputs, n, script).hex() for n in range(len(chosen))]
        return fundinglib.Unsigned(raw=raw, inputs=chosen, outputs=outputs, sighashes=hashes, fee=fee,
                                   change=change, what=what)

    @app.post("/account/token20/send")
    def account_token20_send(request: Request, payload: Any = Body(None)):
        """A DRC-20 / PRC-20 send in three offers: `{chain, tick, amount, to, pubkey}` the commit; `{send}` after
        each broadcast: the reveal (the note on this account's address), then the send of that coin."""
        from arcade import token20_send as t20
        from arcade.doginals import INDEX_READY_LAG
        account = signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        now = time.time()
        for k, v in list(t20_sends.items()):
            if now - v["at"] > T20_SEND_SECONDS:
                t20_sends.pop(k, None)
        sid = str(said.get("send") or "")
        try:
            if sid:
                run = t20_sends.get(sid)
                if run is None or run["pubkey"] != account.pubkey:
                    raise ValueError("that send is not one this arcade is holding (it may have expired): start again")
                return JSONResponse(t20_next(account, chain_on(run["chain"]), run))
            chain = next((c for c in chains.account_chains() if c.network == str(said.get("chain") or "")
                          and c.network in t20.PROTOCOL), None)
            if chain is None:
                raise ValueError("DRC-20 is on Dogecoin and PRC-20 on Pepecoin main")
            address = chains.account_address(account.pubkey, chain)
            # From one of the account's imported wallets (2026-11-12): its address holds the tokens, its key (sent as
            # `pubkey`, checked against it below) signs the commit, the note and the send in the browser.
            frm = str(said.get("from") or "").strip()
            if frm and frm != address:
                from .imported import entry as imported_entry
                e = imported_entry(st, chain.network, account.pubkey, frm)
                if e is None:
                    raise ValueError("that address is not one of this account's imported wallets")
                if str(e.get("k") or "p2pkh") != "p2pkh":
                    raise ValueError("tokens are sent from an imported wallet's ordinary (legacy) address only")
                address = frm
            if not address:
                raise ValueError(f"this account has no {chain.label} address yet")
            to, to_tag = app.state.payee(said.get("to", ""), chain)
            complaint = chains.check_address(to, chain)
            if complaint:
                raise ValueError(complaint)
            if to == address:
                raise ValueError("that is this wallet's own address")
            walked = ord_walked(chain)
            tip = int(v1.core(chain.network, "getblockcount") or 0)
            if not tip or walked < tip - INDEX_READY_LAG:
                raise ValueError(f"the arcades' {t20.PROTOCOL[chain.network].upper()} index is not at the chain's "
                                 f"tip yet (block {walked:,} of {tip:,}): a send waits for it, so the balance it is "
                                 "checked against is the whole one")
            tick = str(said.get("tick") or "").strip()
            held = v1.cmd("tokens", owner=address, chain=chain.key) or []
            held = held if isinstance(held, list) else held.get("tokens") or []
            row = next((b for b in held if str(b.get("tick")).lower() == tick.lower()), None)
            if row is None:
                raise ValueError(f"this wallet holds no {tick}")
            decimals = int((v1.cmd("explorer.token", chain=chain.key, tick=str(row["tick"])) or {}).get("dec") or 0)
            text, units = t20.amount_text(str(said.get("amount") or ""), decimals)
            on_way = t20_pending_units(chain, address, walked).get(str(row["tick"]).lower(), 0)
            listed = t20_listed_units(account, chain, str(row["tick"]), decimals)
            free = int(row.get("overall") or 0) - int(row.get("transferable") or 0) - on_way - listed
            if units > free:
                raise ValueError(f"that is more {row['tick']} than this wallet has to send"
                                 + (" (some is still on its way from an earlier send)" if on_way else "")
                                 + (" (some is listed on the exchange: unlist it first)" if listed else ""))
            coin_key = coin_key_of(said, address)
            content = t20.note(t20.PROTOCOL[chain.network], str(row["tick"]), text)
            pushes, redeem = t20.envelope(content, coin_key)
            commit_value = t20.POSTAGE + t20.reveal_fee(chain.params, pushes, redeem, fees.MIN_FEE_PER_KB)
            unsigned = build(chain, address, [(commit_value, t20.commit_script(redeem))],
                             f"send {text} {row['tick']} to {to} (1 of 3: the commit)")
            quota(account, "send")
            sid = secrets.token_urlsafe(12)
            run = {"pubkey": account.pubkey, "chain": chain.network, "address": address, "to": to, "to_tag": to_tag,
                   "tick": str(row["tick"]), "amount": text, "note": content.decode(),
                   "pushes": [p.hex() for p in pushes], "redeem": redeem.hex(), "commit_value": commit_value,
                   "step": "commit", "txids": {}, "at": now}
            t20_sends[sid] = run

            def committed(txid, r=run, u=units):
                r["txids"]["commit"] = txid
                t20_pending.setdefault((r["chain"], r["address"]), []).append(
                    {"tick": r["tick"].lower(), "units": u, "run": r, "at": time.time()})
            offer = offers.add(account.pubkey, chain.network, unsigned, unsigned.what, done=committed)
            send_fee = fundinglib.price(2, [(t20.POSTAGE, txbuild.p2pkh_script(to, getattr(chain, "params", None)))],
                                        fees.rate_for(chain.params, fees.MIN_FEE_PER_KB), change=True,
                                        params=chain.params)
            return JSONResponse({"send": sid, "step": "commit", "of": 3, "offer": offer.id, "note": run["note"],
                                 "to": to, "to_tag": to_tag, "chain": chain.network, "tick": run["tick"],
                                 "amount": text, "fees": {"commit": unsigned.fee, "reveal": commit_value - t20.POSTAGE,
                                                          "send": send_fee, "postage": t20.POSTAGE},
                                 **unsigned.as_json()})
        except (taglib.TagError, fundinglib.FundingError, ValueError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        except (V1Error, HTTPException) as exc:
            return JSONResponse({"detail": str(getattr(exc, "detail", exc))[:300]}, status_code=503)

    def t20_next(account, chain, run: dict) -> dict:
        from arcade import token20_send as t20
        step = run["step"]
        if step == "commit":
            txid = run["txids"].get("commit")
            if not txid:
                raise ValueError("the commit has not gone out yet: sign it first")
            tx = v1.core(chain.network, "getrawtransaction", txid, 1) or {}
            redeem = bytes.fromhex(run["redeem"])
            out = next((o for o in tx.get("vout") or []
                        if (o.get("scriptPubKey") or {}).get("hex") == t20.commit_script(redeem).hex()
                        and round(float(o["value"]) * 1e8) == run["commit_value"]), None)
            if out is None:
                raise ValueError("the commit on the chain is not the one this send made")
            unsigned = t20.build_reveal(chain.params, {"txid": txid, "vout": int(out["n"]),
                                                       "value": run["commit_value"]},
                                        [bytes.fromhex(p) for p in run["pushes"]], redeem, run["address"],
                                        what=f"send {run['amount']} {run['tick']} to {run['to']} (2 of 3: the note)")
            run["step"] = "reveal"
            offer = offers.add(account.pubkey, chain.network, unsigned, unsigned.what,
                               done=lambda t, r=run: (r["txids"].__setitem__("reveal", t),
                                                      fundinglib.NOTE_COINS.add((t, 0))))
            return {"step": "reveal", "of": 3, "offer": offer.id, "note": run["note"], "chain": chain.network,
                    **unsigned.as_json()}
        if step == "reveal":
            txid = run["txids"].get("reveal")
            if not txid:
                raise ValueError("the note has not gone out yet: sign it first")
            coin = {"txid": txid, "vout": 0, "value": t20.POSTAGE, "address": run["address"]}
            unsigned = note_send(chain, run["address"], coin, run["to"],
                                 f"send {run['amount']} {run['tick']} to {run['to']} (3 of 3: the send)")
            run["step"] = "send"
            offer = offers.add(account.pubkey, chain.network, unsigned, unsigned.what,
                               done=lambda t, r=run: (r["txids"].__setitem__("send", t),
                                                      r.__setitem__("step", "done")))
            return {"step": "send", "of": 3, "offer": offer.id, "note": run["note"], "to": run["to"],
                    "to_tag": run["to_tag"], "chain": chain.network, **unsigned.as_json()}
        if step == "done":
            return {"step": "done", "txids": run["txids"], "note": run["note"], "to": run["to"]}
        raise ValueError(f"the {step} has not gone out yet: sign it first")

    # ---------------------------------------------------------------- ords
    def ord_path() -> Path:
        return st.home / "ord-runs.json"

    def ord_runs() -> dict:
        try:
            return json.loads(ord_path().read_text())
        except (OSError, ValueError):
            return {}

    def ord_save(runs: dict) -> None:
        tmp = ord_path().with_suffix(".tmp")
        tmp.write_text(json.dumps(runs, separators=(",", ":")))
        tmp.replace(ord_path())

    def ord_update(rid: str, change) -> dict:
        with ord_lock:
            runs = ord_runs()
            run = runs.get(rid)
            if run is None:
                raise ValueError("that inscription is not one this arcade is holding")
            change(run)
            ord_save(runs)
            return run

    def ord_public(run: dict, rid: str) -> dict:
        from arcade import ord_inscribe as oi
        total = int(run["reveals"]) + 1
        return {"run": rid, "chain": run["chain"], "name": run.get("name", ""), "content_type": run["content_type"],
                "size": run["size"], "sha256": run["sha256"], "sent": len(run["txids"]), "of": total,
                "inscription": (run["txids"][1] + "i0") if len(run["txids"]) > 1 else "",
                "done": len(run["txids"]) >= total, "at": run["at"], "label": oi.rules_of(run["chain"]).label,
                "address": run["address"]}

    def ord_chain(network: str):
        from arcade import ord_inscribe as oi
        return next((c for c in chains.account_chains() if c.network == network and c.network in oi.RULES), None)

    def ord_plan_of(rid: str, run: dict, content: bytes | None = None):
        from arcade import ord_inscribe as oi
        held = ord_held.get(rid)
        if held is not None:
            return held
        if content is None:
            return None
        if hashlib.sha256(content).hexdigest() != run["sha256"]:
            raise ValueError("that is not the same file this inscription started with")
        chain = ord_chain(run["chain"])
        if chain is None:
            raise ValueError(f"this arcade keeps no {run['chain']} chain now")
        p = oi.plan(run["chain"], chain.params, run["content_type"], content, bytes.fromhex(run["coin_key"]),
                    run["address"])
        if p.commit_value != int(run["commit_value"]) or p.reveals != int(run["reveals"]):
            raise ValueError("that file does not plan out the way this inscription did")
        ord_held[rid] = (content, p)
        return ord_held[rid]

    @app.get("/account/ord/runs")
    def account_ord_runs(request: Request):
        account = signed_in_account(request)
        now = time.time()
        with ord_lock:
            runs = ord_runs()
            stale = [k for k, v in runs.items() if not v["txids"] and now - v["at"] > ORD_UNSTARTED_SECONDS]
            for k in stale:
                runs.pop(k, None)
            if stale:
                ord_save(runs)
        mine = [ord_public(v, k) for k, v in runs.items() if v["pubkey"] == account.pubkey and v["txids"]]
        mine.sort(key=lambda r: -r["at"])
        return JSONResponse({"runs": mine}, headers={"Cache-Control": "no-store"})

    @app.post("/account/ord/forget")
    def account_ord_forget(request: Request, payload: Any = Body(None)):
        account = signed_in_account(request)
        rid = str((payload if isinstance(payload, dict) else {}).get("run") or "")
        with ord_lock:
            runs = ord_runs()
            run = runs.get(rid)
            if run is None or run["pubkey"] != account.pubkey:
                return JSONResponse({"detail": "that inscription is not on your list"}, status_code=404)
            if run["txids"] and len(run["txids"]) < int(run["reveals"]) + 1:
                return JSONResponse({"detail": "that one is not finished: its coins wait in its lock until it is"},
                                    status_code=400)
            runs.pop(rid, None)
            ord_save(runs)
        ord_held.pop(rid, None)
        return JSONResponse({"ok": True})

    @app.post("/account/ord/collection")
    def account_ord_collection(request: Request, payload: Any = Body(None)):
        """A collection index for ords (arcade/collection_json.py): the root and its parts. Nothing is spent."""
        from arcade import collection_json as C
        signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        try:
            chain = str(said.get("chain") or "")
            if said.get("dm") is not None:
                rows = [r for r in list(said.get("dm") or []) if isinstance(r, dict)]
                items, traits = (C.from_dm(rows) if rows and "inscriptionId" in rows[0] else C.from_ow(rows))
            else:
                items, seen = [], []
                for row in said.get("items") or []:
                    t = {str(k).strip(): str(v).strip() for k, v in dict(row.get("traits") or {}).items()
                         if str(k).strip() and str(v).strip()}
                    for k in t:
                        if k not in seen:
                            seen.append(k)
                    items.append(C.Item(id=str(row.get("id") or "").strip().lower(),
                                        number=int(row.get("number") or 0), traits=t))
                traits = seen
            if not items:
                raise C.CollectionError("add at least one ord")
            name = str(said.get("name") or "").strip()
            if not name:
                raise C.CollectionError("a collection needs a name")
            per = C.PER_PART
            while True:
                root, parts = C.build(name[:80], chain, items, traits,
                                      description=str(said.get("description") or "")[:600],
                                      cover=str(said.get("cover") or "").strip().lower(),
                                      replaces=str(said.get("replaces") or "").strip().lower(), per_part=per)
                sizes = [len(json.dumps(x, separators=(",", ":"), ensure_ascii=False).encode()) for x in parts]
                if max(sizes) <= ORD_INDEX_PART_BYTES or per == 1:
                    break
                per = max(1, per * ORD_INDEX_PART_BYTES // max(sizes) - 1)
            return JSONResponse({"root": root, "parts": parts, "content_type": C.CONTENT_TYPE, "items": len(items),
                                 "traits": traits})
        except (C.CollectionError, ValueError, TypeError, KeyError, AttributeError) as exc:
            return JSONResponse({"detail": str(exc) if not isinstance(exc, KeyError) else f"a row has no {exc}"},
                                status_code=400)

    @app.get("/account/ord/chains")
    def account_ord_chains(request: Request):
        from arcade import ord_inscribe as oi
        signed_in_account(request)
        out = []
        for net in (*oi.CHAINS, "test"):
            chain = ord_chain(net)
            if chain is None:
                continue
            r = oi.rules_of(net)
            p = chain.params
            out.append({"chain": net, "label": {"main": "Pepecoin main", "test": "Pepecoin test"}.get(net, chain.label),
                        "coin": p.coin, "ord": r.label, "indexer": r.indexer, "part": r.part, "empty_ok": r.empty_ok,
                        "fee_per_kb": fees.rate_for(p, fees.MIN_FEE_PER_KB),
                        "soft_dust": bool(getattr(p, "soft_dust", True)), "postage": oi.POSTAGE,
                        "mainnet": bool(chain.is_mainnet), "max_bytes": oi.MAX_BYTES})
        return JSONResponse({"chains": out}, headers={"Cache-Control": "no-store"})

    def name_check(chain: str, name: str) -> dict:
        """Is `name` a name that counts, and is it free on `chain` (V1.0's explorer.name, its ord index's walk, and the
        blocks past it and the mempool read for a newer claim, arcade/name_check.py)?"""
        from arcade import name_check as name_checklib
        from arcade import ord_rules
        from arcade.doginals import INDEX_READY_LAG
        from .messages import V1Rpc
        said = name.strip()
        out: dict[str, Any] = {"name": said.lower(), "chain": chain, "endings": NAME_ENDINGS.get(chain, []),
                               "readings": {}, "valid": False, "taken": False, "ready": False, "why": ""}
        if not said:
            out["why"] = "Type a name, like yours.pepe"
            return out
        made = {rule: ord_rules.normalise(rule, "json", said) for rule in ord_rules.NAME_RULES}
        _label, _, ending = said.lower().rpartition(".")
        if not made["spec"]:
            out["why"] = ("A block map, not a name" if ending and ord_rules.is_block_map(ending)
                          else "One dot between the name and its ending; letters, digits and hyphens, a hyphen never "
                               "first or last, up to 63 characters each side")
            return out
        out["name"] = made["spec"]
        out["valid"] = True
        holder = chains.by_network(chain)
        if holder is None:
            out["why"] = "This arcade does not read that chain's ords, so it cannot say whether the name is free"
            return out
        winners: dict[str, Any] = {}
        try:
            got = v1.cmd("explorer.name", chain=holder.key, name=made["spec"]) or {}
            for r in got.get("readings") or []:
                winners[str(r.get("rule"))] = {"id": r.get("id"), "number": r.get("number"), "owner": r.get("owner")}
        except V1Error as exc:
            if "not claimed" not in str(exc):
                out["why"] = f"The name index could not say just now: {str(exc)[:200]}"
                return out
        for rule, what in ord_rules.NAME_RULES.items():
            out["readings"][rule] = {"counts": bool(made[rule]), "about": what, "winner": winners.get(rule)}
        out["taken"] = any(r["winner"] for r in out["readings"].values() if r["counts"])
        if out["taken"]:
            return out
        try:
            walked = ord_walked(holder)
            tip = int(v1.core(holder.network, "getblockcount"))
            out["walked_to"], out["tip"] = walked, tip
            if not walked or walked < tip - INDEX_READY_LAG:
                out["why"] = (f"The name index has read {walked:,} of {tip:,} blocks of this chain. Names can be "
                              "claimed here once it has read the whole chain, so nobody claims a name that is "
                              "already taken")
                return out
            waiting = name_checklib.newest_claims(V1Rpc(v1, holder.network), chain, walked, tip, made,
                                                  mweb=bool(getattr(holder.params, "mweb", False)))
        except Exception as exc:                            # noqa: BLE001 -- not free until it can be read
            out["why"] = f"Could not read the newest blocks for claims of it just now: {str(exc)[:200]}"
            return out
        if waiting:
            out["taken"], out["pending"] = True, waiting
            out["why"] = (f"Somebody's claim of {made['spec']} is already in the {waiting}" if waiting == "mempool"
                          else f"Somebody claimed {made['spec']} in {waiting}, which the index has not read yet")
            return out
        out["ready"] = True
        return out

    @app.get("/account/ord/name")
    def account_ord_name(request: Request, chain: str = "", name: str = ""):
        signed_in_account(request)
        return JSONResponse(name_check(chain, name), headers={"Cache-Control": "no-store"})

    @app.get("/account/names")
    def account_names(request: Request):
        signed_in_account(request)
        return JSONResponse({"names": [], "trouble": []}, headers={"Cache-Control": "no-store"})   # NEEDS-V1

    def mesh_site_for(account, name: str) -> dict:
        from arcade import mesh_web
        from arcade.doginal_index import STARTS
        made = mesh_web.site_name(name)
        mesh = mesh_web.is_mesh(made)
        out: dict[str, Any] = {"name": made, "claim": None, "holder": None, "mine": False, "page": None, "why": "",
                               "max": mesh_web.SITE_MAX if mesh else mesh_web.RECORD_MAX, "mesh": mesh,
                               "chain": mesh_web.NET if mesh else "", "address": ""}
        if not made:
            out["why"] = "A name, like yours.mesh or yours.pepe"
            return out
        nets = [mesh_web.NET] if mesh else [c.network for c in chains.account_chains() if c.network in STARTS]
        found, trouble = [], []
        for net in nets:
            chain = chains.by_network(net)
            if chain is None:
                continue
            try:
                got = v1.cmd("explorer.name", chain=chain.key, name=made) or {}
            except V1Error as exc:
                if "not claimed" not in str(exc):
                    trouble.append(f"{net}: {str(exc)[:160]}")
                continue
            first = next((r for r in got.get("readings") or [] if r.get("rule") == "spec"), None) \
                or next(iter(got.get("readings") or []), None)
            if not first:
                continue
            mine = any_address(account.pubkey, chain)
            holder = first.get("owner")
            found.append({"chain": net, "address": mine, "claim": first.get("id"), "holder": holder, "page": None,
                          "mine": bool(holder) and holder == mine})
        if not found:
            out["why"] = ("The name index could not say just now (" + "; ".join(trouble) + ")" if trouble
                          else f"Nobody has claimed {made} yet: claim it above first")
            return out
        pick = next((f for f in found if f["mine"]), found[0])
        out.update(pick)
        if not out["mine"]:
            out["why"] = (f"{made} is held by {out['holder']}, not by your address on {out['chain']}" if out["holder"]
                          else f"Nobody can say who holds {made} just now")
        return out

    @app.get("/account/mesh/site")
    def account_mesh_site(request: Request, name: str = ""):
        account = signed_in_account(request)
        try:
            return JSONResponse(mesh_site_for(account, name), headers={"Cache-Control": "no-store"})
        except Exception as exc:                            # noqa: BLE001 -- the index could not say
            return JSONResponse({"detail": str(exc)[:300]}, status_code=409)

    @app.post("/account/mesh/draft")
    def account_mesh_draft(request: Request, payload: Any = Body(None)):
        from arcade import mesh_web
        account = signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        pages = said.get("pages")
        if not isinstance(pages, dict):
            return JSONResponse({"detail": "pages: {path: text}"}, status_code=400)
        try:
            body = mesh_web.make(str(said.get("name") or ""),
                                 {str(k): (v if isinstance(v, dict) else str(v)) for k, v in pages.items()})
        except mesh_web.MeshError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        site = mesh_site_for(account, str(said.get("name") or ""))
        if not site["mine"]:
            return JSONResponse({"detail": site["why"] or "this account does not hold that name"}, status_code=403)
        return JSONResponse({"content": base64.b64encode(body).decode(), "size": len(body), "chain": site["chain"],
                             "content_type": "text/plain;charset=utf-8", "name": site["name"], "max": site["max"]})

    @app.post("/account/ord/inscribe")
    def account_ord_inscribe(request: Request, payload: Any = Body(None)):
        """An ord, one offer at a time: `{chain, content, content_type, name, pubkey}` the commit; `{run}` after each
        broadcast the next reveal (`{wait}`, `{need_content}`, `{done, inscription}`)."""
        from arcade import ord_inscribe as oi
        from arcade import ord_rules
        account = signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        try:
            content = None
            if said.get("content") is not None:
                try:
                    content = base64.b64decode(str(said.get("content") or ""), validate=True)
                except ValueError:
                    raise ValueError("the file has to arrive in base64") from None
            rid = str(said.get("run") or "")
            if rid:
                run = ord_runs().get(rid)
                if run is None or run["pubkey"] != account.pubkey:
                    raise ValueError("that inscription is not one this arcade is holding")
                return JSONResponse(ord_next(account, rid, run, content))
            chain = ord_chain(str(said.get("chain") or ""))
            if chain is None:
                raise ValueError("this arcade does not run that chain, or ords are not inscribed on it")
            address = chains.account_address(account.pubkey, chain)
            if not address:
                raise ValueError(f"this account has no {chain.label} address yet")
            coin_key = coin_key_of(said, address)
            if content is None:
                raise ValueError("choose a file to inscribe")
            kind = media.standard_type(str(said.get("content_type") or ""), str(said.get("name") or "")) \
                or "application/octet-stream"
            claim = ord_rules.name_claim(kind.encode(), content)
            if claim and claim[0] == "json":
                check = name_check(chain.network, claim[1])
                if not check["valid"] or check["taken"] or not check["ready"]:
                    raise ValueError(check["why"] or f"{check['name']} is taken")
            p = oi.plan(chain.network, chain.params, kind, content, coin_key, address)
            spent: frozenset = frozenset()
            db = CoinDB(v1, chain)
            lane = said.get("lane") if isinstance(said.get("lane"), dict) else None
            if lane is not None:
                want = (str(lane.get("txid") or "").lower(), int(lane.get("vout") or 0))
                if not re.fullmatch(r"[0-9a-f]{64}", want[0]):
                    raise ValueError("that lane's coin is not a coin")
                every = {(u["txid"], u["vout"]) for u in db.unspent(address)}
                spent = frozenset(every - {want})
            fee_out, fee_said = operator_fee(chain, address, int(oi.costs(p)["reveal_fees"]))
            unsigned = fundinglib.build(db, chain.params, address, [(p.commit_value, oi.p2sh(p.locks[0]))] + fee_out,
                                        rate=fees.MIN_FEE_PER_KB,
                                        what=f"inscribe {str(said.get('name') or kind)[:60]} as a "
                                             f"{oi.rules_of(chain.network).label} (1 of {p.reveals + 1})",
                                        exclude=spent)
            quota(account, "send")
            rid = secrets.token_urlsafe(12)
            run = {"pubkey": account.pubkey, "chain": chain.network, "address": address, "coin_key": coin_key.hex(),
                   "name": str(said.get("name") or "")[:120], "content_type": p.content_type, "size": p.size,
                   "sha256": p.sha256, "reveals": p.reveals, "commit_value": p.commit_value,
                   "lock0": oi.p2sh(p.locks[0]).hex(), "commit_vout": None, "txids": [], "at": time.time()}
            with ord_lock:
                runs = ord_runs()
                runs[rid] = run
                ord_save(runs)
            ord_held[rid] = (content, p)
            commit_vout = next(n for n, (_v, s) in enumerate(unsigned.outputs) if s == oi.p2sh(p.locks[0]))

            def committed(txid, r=rid, v=commit_vout):
                ord_update(r, lambda run: (run.__setitem__("txids", [txid]), run.__setitem__("commit_vout", v)))
            offer = offers.add(account.pubkey, chain.network, unsigned, unsigned.what, done=committed)
            c = oi.costs(p)
            mine = txbuild.p2pkh_script(address, getattr(chain, "params", None))
            change_vout = next((n for n, (_v, s) in enumerate(unsigned.outputs) if s == mine), None)
            return JSONResponse({"run": rid, "step": 0, "of": p.reveals + 1, "offer": offer.id, "chain": chain.network,
                                 "label": oi.rules_of(chain.network).label, "next_lock": p.locks[0].hex(),
                                 "change_vout": change_vout, "operator_fee": fee_said,
                                 "costs": {**c, "commit_fee": unsigned.fee,
                                           "operator_fee": int((fee_said or {}).get("value") or 0)},
                                 **unsigned.as_json()})
        except (fundinglib.FundingError, oi.OrdError, ValueError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        except Exception as exc:                            # noqa: BLE001 -- the arcade did not answer
            return JSONResponse({"detail": str(exc)[:300]}, status_code=503)

    @app.post("/account/ord/split")
    def account_ord_split(request: Request, payload: Any = Body(None)):
        """`{chain, lanes, each}`: `lanes` coins of `each` to this account's own address, one per lane."""
        from arcade import ord_inscribe as oi
        account = signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        try:
            chain = ord_chain(str(said.get("chain") or ""))
            if chain is None:
                raise ValueError("this arcade does not run that chain, or ords are not inscribed on it")
            address = chains.account_address(account.pubkey, chain)
            if not address:
                raise ValueError(f"this account has no {chain.label} address yet")
            lanes, each = int(said.get("lanes") or 0), int(said.get("each") or 0)
            if not 2 <= lanes <= ORD_LANES_MOST:
                raise ValueError(f"from 2 to {ORD_LANES_MOST} lanes")
            if each < max(fees.dust_limit(chain.params), oi.POSTAGE * 2):
                raise ValueError("each lane needs at least enough for one ord")
            unsigned = build(chain, address, [(each, txbuild.p2pkh_script(address, getattr(chain, "params", None)))] * lanes,
                             f"split into {lanes} coins of your own, one per lane of a collection")
            quota(account, "send")
            offer = offers.add(account.pubkey, chain.network, unsigned, unsigned.what)
            return JSONResponse({"offer": offer.id, "chain": chain.network, "vouts": list(range(lanes)),
                                 **unsigned.as_json()})
        except (fundinglib.FundingError, oi.OrdError, ValueError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        except Exception as exc:                            # noqa: BLE001
            return JSONResponse({"detail": str(exc)[:300]}, status_code=503)

    @app.get("/account/ord/confirmed")
    def account_ord_confirmed(request: Request, chain: str = "", txid: str = ""):
        signed_in_account(request)
        c = ord_chain(chain) or chains.by_network(chain)
        if c is None or not re.fullmatch(r"[0-9a-f]{64}", txid or ""):
            return JSONResponse({"detail": "which chain, which transaction"}, status_code=400)
        try:
            got = v1.core(c.network, "getrawtransaction", txid, 1) or {}
            return JSONResponse({"confirmations": int(got.get("confirmations") or 0)},
                                headers={"Cache-Control": "no-store"})
        except V1Error as exc:
            return JSONResponse({"confirmations": 0, "why": str(exc)[:120]}, headers={"Cache-Control": "no-store"})

    def ord_chain_too_long(chain, txids: list[str]) -> bool:
        """Would the next reveal stand on MAX_UNCONFIRMED unconfirmed transactions or more? A reveal V1.0 did not pool
        is in no mempool it can read, so the run's own count speaks then."""
        from arcade import ord_inscribe as oi
        try:
            entry = v1.core(chain.network, "getmempoolentry", txids[-1]) or {}
            return int(entry.get("ancestorcount") or 0) + 1 > oi.MAX_UNCONFIRMED
        except V1Error:
            pass
        try:
            last = v1.core(chain.network, "getrawtransaction", txids[-1], 1) or {}
            if int(last.get("confirmations") or 0) > 0:
                return False
        except V1Error:
            pass
        if len(txids) < oi.MAX_UNCONFIRMED:
            return False
        try:
            far = v1.core(chain.network, "getrawtransaction", txids[-oi.MAX_UNCONFIRMED], 1) or {}
        except V1Error:
            return True
        return not int(far.get("confirmations") or 0)

    def ord_quote_one(network: str, size: int, kind: str, payer: str = "") -> dict:
        """What one ord of `size` bytes costs on `network`, before it starts: the network's fees (commit and reveals),
        the coin it sits on (postage, stays the owner's), and the operator's fee on a mainnet. For a page's read
        (/r/ord-quote, the Token Minter, 2026-11-12) as well as /account/ord/quote."""
        from arcade import ord_inscribe as oi
        chain = ord_chain(network)
        if chain is None:
            raise ValueError("this arcade does not run that chain, or ords are not inscribed on it")
        key = bytes.fromhex("02" + "11" * 32)
        to = b58check_encode(chain.params.pubkeyhash_version, hash160(key))
        commit = fundinglib.price(1, [(oi.POSTAGE, b"\xa9\x14" + b"\x00" * 20 + b"\x87")],
                                  fees.rate_for(chain.params, fees.MIN_FEE_PER_KB), change=True, params=chain.params)
        p = oi.plan(chain.network, chain.params, kind, b"\x00" * int(size), key, to)
        _out, fee_said = operator_fee(chain, payer, sum(p.fees))
        operator = int((fee_said or {}).get("value") or 0)
        network_fees = commit + sum(p.fees)
        return {"chain": chain.network, "label": chain.label, "txs": p.reveals + 1, "network_fees": network_fees,
                "postage": oi.POSTAGE, "operator_fee": operator, "total": network_fees + oi.POSTAGE + operator,
                "mainnet": bool(chain.is_mainnet)}

    app.state.ord_quote = ord_quote_one

    @app.post("/account/ord/quote")
    def account_ord_quote(request: Request, payload: Any = Body(None)):
        """What a collection of ords costs before it starts: `{chain, files: [{size, content_type}]}`."""
        from arcade import ord_inscribe as oi
        account = signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        try:
            chain = ord_chain(str(said.get("chain") or ""))
            if chain is None:
                raise ValueError("this arcade does not run that chain, or ords are not inscribed on it")
            payer = chains.account_address(account.pubkey, chain) or ""
            files = list(said.get("files") or [])[:20000]
            key = bytes.fromhex("02" + "11" * 32)
            to = b58check_encode(chain.params.pubkeyhash_version, hash160(key))
            commit = fundinglib.price(1, [(oi.POSTAGE, b"\xa9\x14" + b"\x00" * 20 + b"\x87")],
                                      fees.rate_for(chain.params, fees.MIN_FEE_PER_KB), change=True,
                                      params=chain.params)
            out, sizes = [], {}
            for f in files:
                size = int(f.get("size") or 0)
                kind = str(f.get("content_type") or "application/octet-stream")[:80]
                if (size, kind) not in sizes:
                    p = oi.plan(chain.network, chain.params, kind, b"\x00" * size, key, to)
                    _out, fee_said = operator_fee(chain, payer, sum(p.fees))
                    sizes[(size, kind)] = {"txs": p.reveals + 1, "reveal_fees": sum(p.fees),
                                           "operator_fee": int((fee_said or {}).get("value") or 0)}
                out.append(sizes[(size, kind)])
            return JSONResponse({"chain": chain.network, "files": out, "commit_fee": commit, "postage": oi.POSTAGE,
                                 "txs": sum(x["txs"] for x in out),
                                 "fees": sum(x["reveal_fees"] for x in out) + commit * len(out),
                                 "operator": sum(x["operator_fee"] for x in out)})
        except (oi.OrdError, fundinglib.FundingError, ValueError, TypeError, AttributeError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)

    def ord_next(account, rid: str, run: dict, content: bytes | None) -> dict:
        from arcade import ord_inscribe as oi
        txids = list(run["txids"])
        total = int(run["reveals"]) + 1
        if not txids:
            raise ValueError("the first transaction has not gone out yet: sign it first")
        if len(txids) >= total:
            return {"done": True, **ord_public(run, rid)}
        held = ord_plan_of(rid, run, content)
        if held is None:
            return {"need_content": True, **ord_public(run, rid)}
        _content, p = held
        chain = ord_chain(run["chain"])
        if chain is None:
            raise ValueError(f"this arcade keeps no {run['chain']} chain now")
        i = len(txids) - 1
        if ord_chain_too_long(chain, txids):
            return {"wait": True, **ord_public(run, rid)}
        vout = int(run["commit_vout"]) if i == 0 else 0
        last = i + 1 == p.reveals
        unsigned = oi.build_reveal(chain.params, p, i, {"txid": txids[i], "vout": vout},
                                   what=f"inscribe {run.get('name') or run['content_type']} "
                                        f"({i + 2} of {total}{': the ord lands in your wallet' if last else ''})")

        def revealed(txid, r=rid, final=last):
            ord_update(r, lambda run: run["txids"].append(txid))
            if final:
                fundinglib.NOTE_COINS.add((txid, 0))
        offer = offers.add(account.pubkey, chain.network, unsigned, unsigned.what, done=revealed)
        return {"step": i + 1, "offer": offer.id, "chain": chain.network, "prev": txids[i], "prev_vout": vout,
                "next_lock": "" if last else p.locks[i + 1].hex(), "last": last, **ord_public(run, rid),
                **unsigned.as_json()}
