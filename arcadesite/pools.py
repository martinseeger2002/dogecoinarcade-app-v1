"""Prize pools and the referee (arcade/web/app.py ~20140-22760), with every pool, coin, inscription and balance read
from Arcade V1.0.

This site AS A REFEREE. app.dogecoinarcade.com is the referee named by live refereed pools, so these answer as the
original's did, with the original's key: arcade/referee.py `Referee(st.home)` -- `referee.key` and `referee.sqlite` in
the site home (copied there unchanged at migration: same file names). Judges run exactly as the original runs them
(referee.run_judge: `python -m arcade.refjudge`, quickjs==1.19.4, in the interpreter this site runs on).

    GET  /r/referee                  this site as a referee: its key, its judges' engine and caps
    POST /r/referee/seed             a seed for one wallet's run (a pool, a game's pools, or a judge)
    POST /r/referee/sign             the referee's signature over a claim whose replay wins
    POST /r/referee/attest           a signed verdict on one run, for the inscription that carries it
    GET  /r/verified/<judge>         V1.0's page API, with this referee's key trusted unless one is named

A player's side (door.py PUBLIC_POST), each building offers the browser signs; /account/sign (wallet.py) broadcasts:

    POST /account/referee/seed       a seed from the pool's referee (this site, or the node the pool names)
    POST /account/referee/attest     a verified result, from this referee or the one the page names
    POST /account/pools/open         register a pool's own address (a two-key one for a refereed pool)
    POST /account/pools/fund         the send that moves the pool's tokens (or coins for NFT lots) to it
    POST /account/pools/legs         the lots, for the tab to sign with the pool key and seal into the inscription
    POST /account/pools/close        everything at the pool's address back to the account
    POST /account/prize              the transaction that claims one lot (shown, not sent)
    POST /account/prize/sign         the claimer's signatures pasted on, the referee's asked for, broadcast
    POST /account/prize/take         a lot that pays its own claim ("fee": "pool"): nothing for the player to sign
    GET  /account/claimpools         this account's pools to finish or close

Where pools come from. The original read them from its own ledger index (_chain_pools). V1.0 serves
/r/prizepool/<key> and /r/claimpools/<page> -- which pool, whether deleted -- but its cards leave out what a claim is
built from (fund, each, first, the pool key, the referee's key, the phrase hash, the pieces). So the pool's own
inscription is read (/r/inscription/<txid>) and its JSON read exactly as _chain_pools read it, the two-key address
checked with referee.redeem_script / address_of. Lots, legs, payloads and scripts are the original's own functions
(funding.build_leg / build_partial / build_pool_close, listings.register / paste_leg / leg_terms, referee.*);
the few lines that only compose them (_pool_leg, _ask_payload, _token_lot, _fill_terms' pool part) are copied
from app.py unchanged in what they build.

NEEDS-V1:
  * pools are read on the page chain only (Pepecoin testnet, `test`): V1.0 indexes prize pools there alone. A pool
    asked for on another chain is "there is no such prize pool on this chain".
  * node-local claim listings (the original's listings.sqlite book) do not exist here: /account/claimpools answers
    `pools: []` and only the `setup` rows (this account's own pool addresses, kept in settings.json as before).
  * a piece's sale already waiting in the mempool (index.pending_swaps) is not asked; the lot's own coin being spent
    in the mempool (gettxout with the pool) still refuses a second claim, and the chain refuses a double spend.
  * coins the account just spent and has not seen confirmed (the original's _flights) are not excluded when a fund
    is built: V1.0's tx.coins already leaves out coins its mempool spends.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import threading
import time
from typing import Any

import requests
from fastapi import Body, Request
from fastapi.responses import JSONResponse

from arcade import accounts as accountslib
from arcade import fees
from arcade import funding as fundinglib
from arcade import inscriptions as inscriptionlib
from arcade import listings as listingslib
from arcade import media
from arcade import payload as P
from arcade import referee as refereelib
from arcade import swap as swaplib
from arcade import tags as taglib
from arcade import tokens as tokenlib
from arcade import txbuild
from arcade.encoding import EncodingError, decode_class_c, encode_class_c
from arcade.ledger import COIN, AmountError, format_amount, parse_amount
from arcade.script import b58check_decode, b58check_encode, hash160, iter_pushes, parse_output
from arcade.web import content as contentlib

from .v1 import V1, V1Error
from .wallet import CoinDB

#: The chain pools live on: V1.0's page chain (Pepecoin testnet).
POOL_CHAIN = "test"
POOL_TYPE = "application/vnd.arcade.prizepool"
#: The original's constants (app.py), unchanged.
SPLIT_EACH = 5 * fees.DUST_LIMIT
POOL_PAYS = 2 * fees.DUST_LIMIT
LOTS_AT_ONCE = 25
NFT_POOL_MOST = 25


# ---------------------------------------------------------------- small helpers copied from app.py (module level there)
def _said_int(said: dict, field: str, label: str, default: int = 0) -> int:
    value = said.get(field)
    if value is None or value == "":
        return default
    if isinstance(value, (list, dict)):
        raise ValueError(f"{label} has to be a number")
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{label} has to be a whole number") from None


def _above_dust(sats: int, what: str, params=None) -> None:
    least = fees.dust_limit(params)
    if sats < least:
        raise ValueError(f"the smallest {what} the network will carry is "
                         f"{least / 100_000_000:g} -- anything less is dust, "
                         f"and nobody would relay it")


def _listing_days(said: dict) -> int:
    try:
        return max(0, min(90, int(said.get("days") or 0)))
    except (TypeError, ValueError, OverflowError):
        return 0


# ---------------------------------------------------------------- V1.0 in the shapes the original's helpers read
class Rpc:
    """What `chain.rpc()` was: Core calls, through V1.0's Core-compatible port. V1.0's decoded transactions carry a
    scriptSig's hex and no `asm`, and its gettxout no `addresses`; both are filled in from the script itself, which is
    what Core's answer said (listings.paste_leg and _pool_claimers read them)."""

    def __init__(self, v1: V1, chain):
        self.v1, self.chain = v1, chain

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def call(self, method: str, *params):
        out = self.v1.core(self.chain.network, method, *params)
        if method in ("decoderawtransaction", "getrawtransaction") and isinstance(out, dict):
            for vin in out.get("vin") or []:
                sig = vin.get("scriptSig")
                if isinstance(sig, dict) and "asm" not in sig:
                    try:
                        sig["asm"] = " ".join(p.hex() for p in iter_pushes(bytes.fromhex(sig.get("hex") or "")))
                    except ValueError:
                        sig["asm"] = ""
        if method == "gettxout" and isinstance(out, dict):
            spk = out.setdefault("scriptPubKey", {})
            if "addresses" not in spk:
                try:
                    found = parse_output(str(spk.get("hex") or ""), 0, self.chain.params).address
                except Exception:                           # noqa: BLE001
                    found = None
                spk["addresses"] = [found] if found else []
        return out


class Index:
    """What the original's token index answered about one chain, from V1.0: inscriptions (row shape of the original's
    index: txid, number, owner, creator, content_type, json text, collection, edition, block_height), tokens,
    balances, and an inscription's content."""

    def __init__(self, v1: V1, chain):
        self.v1, self.chain = v1, chain
        self._balances: dict[str, list] = {}

    def inscription(self, key: Any) -> dict | None:
        key = str(key if key is not None else "").strip().lstrip("#").lower()
        if not key:
            return None
        try:
            if self.chain.network == POOL_CHAIN:
                code, row = self.v1.page(f"inscription/{key}")
                if code != 200 or not isinstance(row, dict) or not row.get("id"):
                    return None
                raw = row.get("rawjson")
                if raw is None:
                    raw = json.dumps(row.get("json")) if row.get("json") is not None else ""
                return {"txid": str(row["id"]).lower(), "number": row.get("number"), "owner": row.get("owner"),
                        "creator": row.get("creator"), "content_type": row.get("contenttype") or "",
                        "json": raw or "", "collection": row.get("collection"), "edition": row.get("edition"),
                        "block_height": row.get("block")}
            got = self.v1.cmd("explorer.ledger_inscription", chain=self.chain.key, id=key) or {}
        except V1Error:
            return None
        if not got or not got.get("txid"):
            return None
        return {"txid": str(got["txid"]).lower(), "number": got.get("number"), "owner": got.get("owner"),
                "creator": got.get("creator"), "content_type": got.get("content_type") or "",
                "json": got.get("json") or "", "collection": got.get("collection"), "edition": got.get("edition"),
                "block_height": got.get("block_height")}

    def inscription_content(self, txid: str) -> tuple[str, bytes] | None:
        if self.chain.network != POOL_CHAIN:
            return None
        try:
            r = self.v1.raw("GET", f"/content/{str(txid).lower()}")
        except V1Error:
            return None
        if r.status_code != 200:
            return None
        return r.headers.get("content-type", ""), r.content

    def property(self, pid: int) -> dict | None:
        try:
            if self.chain.network == POOL_CHAIN:
                code, p = self.v1.page(f"token/{int(pid)}")
                if code != 200 or not isinstance(p, dict) or not p:
                    return None
                return {"property_id": int(pid), "name": p.get("name"), "divisible": bool(p.get("divisible"))}
            p = self.v1.cmd("explorer.ledger_token", chain=self.chain.key, id=int(pid)) or {}
        except V1Error:
            return None
        if not p:
            return None
        return {"property_id": int(pid), "name": p.get("name"), "divisible": p.get("type") == 2}

    def _rows(self, address: str) -> list:
        if address not in self._balances:
            rows: list = []
            if self.chain.network == POOL_CHAIN:
                code, got = self.v1.page(f"balances/{address}", {"pending": 1})
                rows = got if code == 200 and isinstance(got, list) else []
            else:
                got = self.v1.cmd("tokens", owner=address, chain=self.chain.key) or {}
                listed = got if isinstance(got, list) else got.get("tokens") or got.get("items") or []
                rows = [{"propertyid": int(r.get("propertyid") or r.get("id") or -1),
                         "units": int(r.get("units") or r.get("balance_units") or 0), "leaving": 0}
                        for r in listed]
            self._balances[address] = rows
        return self._balances[address]

    def balance(self, address: str, pid: int) -> int:
        row = next((r for r in self._rows(address) if int(r.get("propertyid") or -1) == int(pid)), {})
        return int(row.get("units") or 0)

    def pending_out(self, address: str, pid: int) -> int:
        row = next((r for r in self._rows(address) if int(r.get("propertyid") or -1) == int(pid)), {})
        return int(row.get("leaving") or 0)


def register(app, st, chains):
    v1: V1 = st.v1
    offers = app.state.offers
    referee = refereelib.Referee(st.home)
    lanes: set[str] = set()
    lanes_lock = threading.Lock()

    def index_of(chain) -> Index:
        return Index(v1, chain)

    def rpc_of(chain) -> Rpc:
        return Rpc(v1, chain)

    def begin_send(lane: str) -> bool:
        with lanes_lock:
            if lane in lanes:
                return False
            lanes.add(lane)
            return True

    def end_send(lane: str) -> None:
        with lanes_lock:
            lanes.discard(lane)

    def quota(account, kind: str) -> None:
        try:
            app.state.quota(account, kind)
        except accountslib.AccountError as exc:
            raise ValueError(str(exc))

    def chain_asked(said: dict):
        """The original's _chain_asked: the tag chain unless the request names one, by network or label."""
        wanted = str((said or {}).get("chain", "") or "").strip().lower()
        if not wanted:
            return chains.account_chain()
        for one in chains.account_chains():
            if wanted in (one.network, one.label.lower(), str(getattr(one.params, "label", "")).lower()):
                return one
        raise ValueError(f"this node does not run {wanted}")

    def account_address(pubkey: str, chain) -> str:
        return chains.account_address(pubkey, chain)

    def check_address(address: str, chain) -> str | None:
        return chains.check_address(address, chain)

    def tags_for(addresses) -> dict[str, str]:
        out = {}
        for a in {a for a in addresses if a}:
            try:
                code, got = v1.page(f"address/{a}")
                if code == 200 and (got or {}).get("tag"):
                    out[a] = str(got["tag"])
            except V1Error:
                pass
        return out

    def unlimited_address(address: str) -> bool:
        """settings.json `unlimited_accounts`: addresses, or @tags (their testnet owner or their linked mainnet)."""
        listed = st.setting("unlimited_accounts", []) or []
        if not address or not isinstance(listed, list):
            return False
        for item in listed:
            item = str(item or "").strip()
            if item == address:
                return True
            if item and taglib.looks_like_a_tag(item):
                tag = item.lstrip("@")
                try:
                    if v1.tag_owner(tag) == address:
                        return True
                    prof = (v1.cmd("profile", who=f"@{tag}") or {}).get("profile") or {}
                    if any(str(l.get("address") or "") == address for l in prof.get("linked") or []
                           if str(l.get("chain") or "") in ("main", "pep")):
                        return True
                except V1Error:
                    continue
        return False

    def pool_coins(chain, address: str) -> list[dict]:
        """Every coin at a pool's address (the original's utxos.unspent): confirmed, waiting, and any V1.0 holds back."""
        got = CoinDB(v1, chain).coins_of([address]).get(address) or {}
        return [{"txid": c["txid"], "vout": int(c["vout"]), "value": int(c["value"])}
                for c in (got.get("confirmed") or []) + (got.get("unconfirmed") or []) + (got.get("held_back") or [])]

    # ---------------------------------------------------------------- this account's own pools (settings.json)
    def pools_of(account) -> list[dict]:
        return [dict(p) for p in (st.setting(f"pools:{account.pubkey}", []) or [])]

    def pool_of(account, address: str, network: str) -> dict | None:
        return next((p for p in pools_of(account) if p["address"] == address and p["network"] == network), None)

    def save_pool(account, pool: dict) -> None:
        pools = [p for p in pools_of(account) if p["address"] != pool["address"]]
        st.set_setting(f"pools:{account.pubkey}", pools + [pool])

    # ---------------------------------------------------------------- pools on the chain
    def pool_from_row(chain, r: dict) -> dict | None:
        """One inscription read as _chain_pools read it, or None when it is no pool."""
        try:
            said = json.loads(r.get("json") or "{}") or {}
        except ValueError:
            return None
        if not isinstance(said, dict):
            return None
        p = said.get("prizepool")
        if not isinstance(p, dict) or not str(r.get("content_type") or "").startswith(POOL_TYPE):
            return None
        if p.get("creator") != r["creator"]:
            return None
        try:
            kind = "nft" if p.get("kind") == "nft" else "token"
            pool = {
                "txid": r["txid"], "number": r["number"], "creator": r["creator"],
                "kind": kind, "pieces": [str(x).lower() for x in (p.get("pieces") or [])],
                "first": int(p.get("first", 1)), "once": bool(p.get("once")),
                "name": str(said.get("name") or "")[:60],
                "pool": str(p["pool"]), "pubkey": str(p["pubkey"]),
                "index": int(p["index"]), "property_id": int(p.get("property_id") or 0),
                "lot": int(p.get("lot") or 0), "count": int(p["count"]),
                "price": int(p["price"]), "each": int(p["each"]),
                "fund": str(p["fund"]).lower(), "game": str(p.get("game") or "").lower(),
                "claim_hash": str(p["claim_hash"]).lower(), "height": r.get("block_height"),
                "referee": None, "redeem": "",
                "fee": "pool" if p.get("fee") == "pool" and p.get("referee") else ""}
            if p.get("referee"):
                ref = p["referee"]
                creator_pub = bytes.fromhex(str(p["creator_pubkey"]))
                if hash160(creator_pub) != b58check_decode(r["creator"])[1]:
                    raise ValueError("creator key")
                redeem = refereelib.redeem_script(
                    bytes.fromhex(str(p["pubkey"])), bytes.fromhex(str(ref["pubkey"])), creator_pub)
                if refereelib.address_of(redeem, chain.params) != str(p["pool"]):
                    raise ValueError("address")
                pool["referee"] = {
                    "node": str(ref.get("node") or ""), "pubkey": str(ref["pubkey"]).lower(),
                    "judge": str(ref["judge"]).lower(),
                    "require": ref.get("require") if isinstance(ref.get("require"), dict) else {"won": True},
                    "params": ref.get("params") if ref.get("params") is not None else {},
                    "seed_hours": refereelib.seed_seconds(ref) / 3600,
                    "facts": facts_spec(ref.get("facts")) if ref.get("facts") else {},
                    "session": ref.get("session") is True}
                pool["redeem"] = redeem.hex()
        except (KeyError, TypeError, ValueError, AttributeError):
            return None
        if pool["kind"] == "nft" and len(pool["pieces"]) != pool["count"]:
            return None
        return pool

    def chain_pool(chain, key: str) -> dict | None:
        """A pool by inscription txid or number: V1.0 says which and whether deleted, its inscription says the rest."""
        if chain.network != POOL_CHAIN:
            return None
        key = str(key or "").strip().lstrip("#")
        if not key or "/" in key:
            return None
        code, card = v1.page(f"prizepool/{key}")
        if code != 200 or not isinstance(card, dict) or not card.get("txid"):
            return None
        row = index_of(chain).inscription(card["txid"])
        if row is None:
            return None
        pool = pool_from_row(chain, row)
        if pool is None:
            return None
        pool["deleted"] = bool(card.get("deleted"))
        return pool

    def pools_of_game(chain, game_txid: str) -> list[dict]:
        """The undeleted pools tied to one page, oldest first (the order _chain_pools kept)."""
        if chain.network != POOL_CHAIN or not game_txid:
            return []
        code, got = v1.page(f"claimpools/{game_txid}")
        if code != 200 or not isinstance(got, dict):
            return []
        out = []
        for card in got.get("pools") or []:
            if card.get("deleted"):
                continue
            row = index_of(chain).inscription(card.get("txid"))
            pool = pool_from_row(chain, row) if row else None
            if pool is not None and pool["game"] == game_txid:
                pool["deleted"] = False
                out.append(pool)
        out.sort(key=lambda p: (p["height"] or 0, p["number"] or 0))
        return out

    def pool_refusal(chain, pool: dict | None, page: str = "") -> str:
        if pool is None:
            return "there is no such prize pool on this chain"
        if pool["deleted"]:
            return "that prize pool was deleted by its creator"
        if pool["game"]:
            index = index_of(chain)
            row = index.inscription(pool["game"])
            if row is None or row["owner"] != pool["creator"]:
                return ("this prize pays out only while its creator holds the page it "
                        "belongs to, and they no longer do")
            if page:
                asked = index.inscription(contentlib._key(page))
                if asked is None or asked["txid"] != pool["game"]:
                    return "this prize belongs to another page"
        return ""

    def pool_lots(chain, pool: dict) -> tuple[list[int], int]:
        free, waiting = [], 0
        index = index_of(chain)
        first = pool.get("first", 1)
        with rpc_of(chain) as rpc:
            for n in range(pool["count"]):
                vout = first + 2 * n
                if rpc.call("gettxout", pool["fund"], vout, True):
                    if pool.get("kind") == "nft":
                        row = index.inscription(pool["pieces"][n])
                        if row is None or row["owner"] != pool["pool"]:
                            continue
                    free.append(n)
                elif rpc.call("gettxout", pool["fund"], vout, False):
                    waiting += 1
        return free, waiting

    def ask_payload(row: dict, take: inscriptionlib.Leg) -> bytes:
        """app.py _ask_payload: a swap payload of one inscription for `take`, AnyData-wrapped, Class C."""
        body = inscriptionlib.Swap(
            give=inscriptionlib.Leg(inscriptionlib.LEG_INSCRIPTION, txid=bytes.fromhex(row["txid"])),
            take=take).encode()
        try:
            return encode_class_c(P.AnyData(data=body).encode())
        except EncodingError as exc:
            raise ValueError(
                f"{exc} A listing is written in one OP_RETURN because a leg has "
                f"two outputs and both are signed: what it sells, and what it "
                f"costs.") from None

    def pool_leg(chain, pool: dict, n: int):
        """Lot `n` of a pool, rebuilt from its public JSON (app.py _pool_leg)."""
        pays = POOL_PAYS if pool.get("fee") == "pool" else 0
        if pool.get("kind") == "nft" and pays:
            naming = encode_class_c(P.AnyData(data=inscriptionlib.Transfer(
                txid=bytes.fromhex(pool["pieces"][n])).encode()).encode())
        elif pool.get("kind") == "nft":
            naming = ask_payload({"txid": pool["pieces"][n]},
                                 inscriptionlib.Leg(inscriptionlib.LEG_COINS, amount=pool["price"]))
        else:
            naming = encode_class_c(P.SimpleSend(property_id=pool["property_id"], amount=pool["lot"]).encode())
        first = pool.get("first", 1)
        piece = {"txid": pool["fund"], "vout": first + 2 * n, "value": pool["each"], "address": pool["pool"]}
        coin = {"txid": pool["fund"], "vout": first + 1 + 2 * n, "value": pool["each"], "address": pool["pool"]}
        return fundinglib.build_leg(chain.params, pool["pool"], piece, coins=pool["price"],
                                    rate=fees.MIN_FEE_PER_KB, payload=naming, coin=coin,
                                    redeem=bytes.fromhex(pool.get("redeem") or ""), pool_pays=pays)

    # Claims this site broadcast, by pool; mempool transactions already read (app.py _pool_claims, _mempool_claims).
    pool_claims: dict[str, set] = {}
    mempool_claims: dict[str, tuple] = {}

    def pool_claimers(chain, pool: dict) -> set:
        """Who has claimed from a pool (app.py _pool_claimers): the signers of every claim paid to the pool's address,
        and of claims of its fund still in the mempool."""
        found = set(pool_claims.get(pool["txid"], set()))
        try:
            paid = {c["txid"] for c in pool_coins(chain, pool["pool"])}
        except Exception:                                   # noqa: BLE001
            paid = set()
        paid.discard(pool["fund"])

        def signers(tx: dict) -> set:
            out = set()
            if len(tx.get("vin") or []) == 2 and tx.get("vout"):
                last = (tx["vout"][-1].get("scriptPubKey") or {}).get("addresses") or []
                out.update(str(a) for a in last[:1])
            for spent in (tx.get("vin") or [])[2:]:
                asm = str((spent.get("scriptSig") or {}).get("asm") or "").split()
                if len(asm) == 2 and len(asm[1]) == 66:
                    out.add(b58check_encode(chain.params.pubkeyhash_version, hash160(bytes.fromhex(asm[1]))))
            return out

        with rpc_of(chain) as rpc:
            for txid in paid:
                try:
                    found |= signers(rpc.call("getrawtransaction", txid, 1))
                except Exception:                           # noqa: BLE001
                    continue
            try:
                ids = list(rpc.call("getrawmempool") or [])
            except Exception:                               # noqa: BLE001
                ids = []
            for txid in ids:
                if txid not in mempool_claims:
                    try:
                        tx = rpc.call("getrawtransaction", txid, 1)
                    except Exception:                       # noqa: BLE001
                        continue
                    vin = tx.get("vin") or []
                    mempool_claims[txid] = (str(vin[0].get("txid") or ""), signers(tx)) if vin else ("", set())
                fund, who = mempool_claims[txid]
                if fund == pool["fund"]:
                    found |= who
            for gone in set(mempool_claims) - set(ids):
                del mempool_claims[gone]
        return found

    # ---------------------------------------------------------------- lots and pieces (app.py)
    lots_flying: dict[tuple, dict[str, int]] = {}

    def token_lot(naming: bytes) -> tuple[int, int] | None:
        try:
            body = decode_class_c(bytes(naming)) if naming else None
            message = P.decode(body) if body else None
        except Exception:                                   # noqa: BLE001
            return None
        if isinstance(message, P.SimpleSend) and message.amount > 0:
            return int(message.property_id), int(message.amount)
        return None

    def sold_piece(row: dict) -> str:
        try:
            swap = listingslib.named_swap(bytes.fromhex(row["payload"] or ""))
        except ValueError:
            return ""
        if swap is None or swap.give.kind != inscriptionlib.LEG_INSCRIPTION:
            return ""
        return swap.give.txid.hex()

    def lot_words(chain, lot: tuple[int, int]) -> str:
        prop = index_of(chain).property(lot[0])
        if prop is None:
            return f"{lot[1]} of token #{lot[0]}"
        return f"{format_amount(lot[1], prop['divisible'])} {prop['name']}"

    def lot_room(chain, owner: str, property_id: int) -> int:
        flying = lots_flying.get((chain.network, owner, property_id), {})
        if flying:
            with rpc_of(chain) as rpc:
                for txid in list(flying):
                    try:
                        rpc.call("getmempoolentry", txid)
                    except Exception:                       # noqa: BLE001 -- landed
                        flying.pop(txid, None)
        held = index_of(chain).balance(owner, property_id)
        return int(held) - sum(flying.values())

    def piece_called(chain, listing: dict) -> str:
        lot = token_lot(bytes.fromhex(str(listing.get("payload") or "")))
        if lot:
            return lot_words(chain, lot)
        row = index_of(chain).inscription(sold_piece(listing))
        if not row:
            return ""
        try:
            data = json.loads(row.get("json") or "null")
        except ValueError:
            data = None
        name = data.get("name") if isinstance(data, dict) else None
        return f"{name} (#{row['number']})" if name else f"#{row['number']}"

    def claim_piece(chain, listing: dict) -> tuple[str, str]:
        """app.py _claim_piece: one completion of a lot (or piece) at a time here."""
        lot = token_lot(bytes.fromhex(str(listing.get("payload") or "")))
        if lot:
            lane = f"lot {listing['network']} {listing['owner']} {lot[0]}"
            if not begin_send(lane):
                return "", ("another claim of this token is going through right "
                            "now. Nothing was spent; try again in a moment.")
            try:
                short = lot_room(chain, listing["owner"], lot[0]) < lot[1]
            except Exception:                               # noqa: BLE001
                short = True
            if short:
                end_send(lane)
                return "", ("the seller no longer holds enough of that token for "
                            "this lot. Nothing was spent.")
            return lane, ""
        piece = sold_piece(listing)
        if not piece:
            return "", ""
        lane = f"piece {piece}"
        if not begin_send(lane):
            return "", ("another sale of this piece is going through right now. "
                        "Nothing was spent; look again in a minute.")
        return lane, ""

    def fill_terms(chain, address: str, listing: dict) -> tuple:
        """app.py _fill_terms, for a pool's lot: the claimer's coins behind the lot's two, the lot's outputs first."""
        if listing["owner"] == address:
            raise swaplib.SwapError("a wallet cannot fill its own order")
        index = index_of(chain)
        sold = sold_piece(listing)
        got = index.inscription(sold) if sold else None
        if sold and (got is None or got["owner"] != listing["owner"]):
            raise swaplib.SwapError(
                "the seller no longer holds that piece, so this listing is over. "
                "Nothing was spent.")
        naming = bytes.fromhex(str(listing["payload"] or ""))
        lot = token_lot(naming)
        if lot and lot_room(chain, listing["owner"], lot[0]) < lot[1]:
            raise swaplib.SwapError(
                "the seller no longer holds enough of that token for this lot, so "
                "it cannot be claimed. Nothing was spent.")
        foreign, outputs = listingslib.leg_terms(listing)
        named = listingslib.named_swap(naming)
        piece = (f"{listing['input']['txid'][:16]}…:{listing['input']['vout']}"
                 if named is None else swaplib.describe_leg(swaplib.leg_json(named.give, index)))
        if lot:
            piece = lot_words(chain, lot)
            outputs = list(outputs) + [(fees.DUST_LIMIT, txbuild.p2pkh_script(address, getattr(chain, "params", None)))]
        take = named.take if named is not None else None
        if take is not None and take.kind == inscriptionlib.LEG_TOKEN:
            left = index.balance(address, take.property_id) - index.pending_out(address, take.property_id)
            if left < take.amount:
                raise swaplib.SwapError(
                    f"this costs {swaplib.describe_leg(swaplib.leg_json(take, index))}, "
                    "and this account does not hold that much that is not already on "
                    "its way somewhere. Nothing was spent.")
            cost = swaplib.describe_leg(swaplib.leg_json(take, index))
        else:
            cost = swaplib.describe_leg(swaplib.leg_json(
                inscriptionlib.Leg(inscriptionlib.LEG_COINS, amount=int(listing["price"])), index))
        what = f"buy {piece} for {cost}"
        unsigned = fundinglib.build_partial(CoinDB(v1, chain), chain.params, address, foreign, outputs,
                                            rate=fees.MIN_FEE_PER_KB, what=what)
        return listing, unsigned, what

    # ---------------------------------------------------------------- the referee
    def facts_spec(raw: Any) -> dict:
        if not raw:
            return {}
        if not isinstance(raw, dict):
            raise ValueError('facts is {"tokens": [numbers], "collections": '
                             '[{"creator": address, "collection": name}]}')
        tokens = [int(x) for x in (raw.get("tokens") or [])]
        cols = [{"creator": str(c["creator"]), "collection": str(c["collection"])}
                for c in (raw.get("collections") or []) if isinstance(c, dict)]
        if len(tokens) > refereelib.FACT_TOKENS_MOST or len(cols) > refereelib.FACT_COLLECTIONS_MOST:
            raise ValueError(f"facts name at most {refereelib.FACT_TOKENS_MOST} tokens and "
                             f"{refereelib.FACT_COLLECTIONS_MOST} collections")
        attested = [str(j).strip().lstrip("#") for j in (raw.get("attested") or [])][:10]
        return {"tokens": tokens, "collections": cols, **({"attested": attested} if attested else {})}

    def attested_rows(chain, judge: str, referee_key: str, creator: str = "", limit: int = 100) -> list[dict]:
        """Verified results of `judge` signed by `referee_key`, newest first: V1.0's /r/verified (which checks each
        attestation as _attested_rows did), narrowed to one creator."""
        out, after = [], -1
        for _ in range(20):
            params = {"referee": referee_key, "limit": 200, **({"after": after} if after >= 0 else {})}
            code, got = v1.page(f"verified/{judge}", params)
            if code != 200 or not isinstance(got, dict):
                break
            rows = got.get("results") or []
            for r in rows:
                if creator and r.get("creator") != creator:
                    continue
                out.append({"id": r.get("id"), "number": r.get("number"), "creator": r.get("creator"),
                            "block": r.get("block"), "result": r.get("result"), "json": r.get("json")})
                if len(out) >= limit:
                    return out
            if not rows or got.get("next") is None:
                break
            after = int(got["next"])
        return out

    def facts_of(chain, ref: dict, address: str) -> dict | None:
        """What `address` holds of what a pool's judge is told about, now (app.py _facts_of)."""
        spec = ref.get("facts") or {}
        if not spec:
            return None
        index = index_of(chain)
        tokens = {str(pid): str(index.balance(address, int(pid))) for pid in spec.get("tokens", [])}
        wanted = {(c["creator"], c["collection"]) for c in spec.get("collections", [])}
        pieces = []
        if wanted and chain.network == POOL_CHAIN:
            offset = 0
            while offset < 2000:
                code, rows = v1.page(f"inscriptions/{address}", {"limit": 500, "offset": offset})
                rows = rows if code == 200 and isinstance(rows, list) else []
                for row in rows:
                    if (row.get("creator"), row.get("collection")) not in wanted:
                        continue
                    try:
                        data = json.loads(row.get("rawjson") or "null")
                    except ValueError:
                        data = None
                    pieces.append({"id": row["id"], "number": row.get("number"),
                                   "collection": row.get("collection"), "json": data})
                if len(rows) < 500:
                    break
                offset += 500
        results = []
        for judge in spec.get("attested") or []:
            try:
                row = judge_row(chain, judge)
            except refereelib.RefereeError:
                continue
            for r in attested_rows(chain, row["txid"], referee.pubkey.hex(), creator=address, limit=200):
                results.append({"id": r["id"], "number": r["number"], "judge": row["txid"],
                                "block": r["block"], "result": r["result"], "json": r["json"]})
        return {"tokens": tokens, "pieces": pieces, **({"attested": results} if spec.get("attested") else {})}

    def referee_terms(chain, spec: Any) -> dict:
        """What a pool's JSON says about its referee, checked when it is opened (app.py _referee_terms)."""
        if not isinstance(spec, dict):
            raise ValueError("a referee is {\"node\": ..., \"judge\": ..., \"require\": ...}")
        node = str(spec.get("node") or "").strip().rstrip("/")
        if node and not re.fullmatch(r"https?://[A-Za-z0-9.\-]+(:\d+)?", node):
            raise ValueError("a referee node is named by its address, like "
                             "https://app.dogecoinarcade.com")
        if not node:
            pubkey = referee.pubkey.hex()
        else:
            try:
                said = requests.get(node + "/r/referee", timeout=15).json()
                pubkey = str(said["pubkey"])
            except Exception:                               # noqa: BLE001
                raise ValueError(f"{node} did not answer as a referee") from None
        if len(bytes.fromhex(pubkey)) != 33:
            raise ValueError("that referee's key is not a public key")
        row = index_of(chain).inscription(contentlib._key(str(spec.get("judge") or "").strip().lstrip("#")))
        if row is None:
            raise ValueError("a refereed pool names its judge, an inscription of plain "
                             "JavaScript, and there is no such inscription")
        if "javascript" not in media.standard_type(str(row["content_type"])):
            raise ValueError(f"#{row['number']} is not JavaScript, so it cannot judge")
        require = spec.get("require") if isinstance(spec.get("require"), dict) else {"won": True}
        if not (require.get("won") is True or isinstance(require.get("score_min"), (int, float))):
            raise ValueError("a referee requires {\"won\": true} or {\"score_min\": N}")
        params = spec.get("params") if spec.get("params") is not None else {}
        if len(json.dumps(params)) > 4096:
            raise ValueError("a judge's params are at most 4 KB")
        hours = spec.get("seed_hours", 2)
        if (not isinstance(hours, (int, float)) or isinstance(hours, bool)
                or not 0 < hours <= refereelib.SEED_HOURS_MOST):
            raise ValueError(f"seed_hours is a number of hours, above 0 and at most "
                             f"{refereelib.SEED_HOURS_MOST}")
        facts = facts_spec(spec.get("facts"))
        return {"node": node, "pubkey": pubkey, "judge": row["txid"],
                "require": require, "params": params, "seed_hours": hours,
                **({"facts": facts} if facts else {}),
                **({"session": True} if spec.get("session") is True else {})}

    def referee_json(pool: dict) -> dict:
        if not pool.get("referee"):
            return {}
        return {"referee": pool["referee"], "creator_pubkey": pool["creator_pubkey"]}

    def referee_node(pool: dict) -> str:
        ref = pool["referee"]
        if ref["pubkey"] == referee.pubkey.hex():
            return ""
        if not ref["node"]:
            raise refereelib.RefereeError("this pool's referee is a node that did not say "
                                          "where it is, and it is not this one")
        return ref["node"]

    def judge_row(chain, key: str) -> dict:
        row = index_of(chain).inscription(contentlib._key(str(key or "").lstrip("#")))
        if row is None:
            raise refereelib.RefereeError("there is no such judge")
        if "javascript" not in media.standard_type(str(row["content_type"])):
            raise refereelib.RefereeError(f"#{row['number']} is not JavaScript, so it cannot judge")
        return row

    def referee_seed(chain, key: str, address: str, game: str = "", judge: str = "",
                     session: bool = False) -> dict:
        if judge:
            row = judge_row(chain, judge)
            complaint = check_address(address, chain)
            if complaint:
                raise refereelib.RefereeError(complaint)
            return referee.issue_seed("judge:" + row["txid"], address)
        if game:
            return referee_game_seed(chain, game, address, session=session)
        pool = chain_pool(chain, key)
        if pool is None or not pool.get("referee"):
            raise refereelib.RefereeError("this pool has no referee")
        if pool["referee"]["pubkey"] != referee.pubkey.hex():
            raise refereelib.RefereeError("this node is not that pool's referee")
        refusal = pool_refusal(chain, pool)
        if refusal:
            raise refereelib.RefereeError(refusal)
        complaint = check_address(address, chain)
        if complaint:
            raise refereelib.RefereeError(complaint)
        return referee.issue_seed(pool["txid"], address, refereelib.seed_seconds(pool["referee"]),
                                  facts=facts_of(chain, pool["referee"], address))

    def refereed_pools_of(chain, game: str) -> list[dict]:
        mine = referee.pubkey.hex()
        return [p for p in pools_of_game(chain, game)
                if p.get("referee") and p["referee"]["pubkey"] == mine and not pool_refusal(chain, p)]

    def referee_game_seed(chain, key: str, address: str, session: bool = False) -> dict:
        row = index_of(chain).inscription(contentlib._key(key))
        if row is None:
            raise refereelib.RefereeError("there is no such game")
        pools = refereed_pools_of(chain, row["txid"])
        if session:
            pools = [p for p in pools if p["referee"].get("session")]
            if not pools:
                raise refereelib.RefereeError("this game has no pool that takes a session seed")
        if not pools:
            raise refereelib.RefereeError("this game has no refereed pool this node referees")
        complaint = check_address(address, chain)
        if complaint:
            raise refereelib.RefereeError(complaint)
        facts = {p["txid"]: facts_of(chain, p["referee"], address) for p in pools if p["referee"].get("facts")}
        seconds = max(refereelib.seed_seconds(p["referee"]) for p in pools)
        said = referee.issue_seed("", address, seconds, facts=facts or None, game=row["txid"], session=session)
        return {**said, "pools": [p["txid"] for p in pools]}

    def judge_source(chain, txid: str) -> str:
        found = index_of(chain).inscription_content(txid)
        if found is None:
            raise refereelib.RefereeError("this node does not hold the pool's judge")
        return found[1].decode("utf-8", "replace")

    def referee_sign(chain, key: str, n: int, raw: str, replay: Any) -> dict:
        """The referee's half of a claim (app.py _referee_sign): a verdict on the replay, then a signature over the
        whole claim -- or a reason."""
        pool = chain_pool(chain, key)
        if pool is None or not pool.get("referee"):
            raise refereelib.RefereeError("this pool has no referee")
        ref = pool["referee"]
        if ref["pubkey"] != referee.pubkey.hex():
            raise refereelib.RefereeError("this node is not that pool's referee")
        refusal = pool_refusal(chain, pool)
        if refusal:
            raise refereelib.RefereeError(refusal)
        if not isinstance(replay, dict) or not replay.get("seed"):
            raise refereelib.RefereeError("this prize pays only for a replay of a win: "
                                          "the game has to send {seed, inputs}")
        seed = referee.seed_row(str(replay["seed"]))
        if seed is None or not (seed["pool"] == pool["txid"]
                                or (seed.get("game") and seed["game"] == pool["game"])):
            raise refereelib.RefereeError("that seed was not issued for this pool")
        digest = hashlib.sha256(str(raw).encode()).hexdigest()
        session = bool(seed.get("session"))
        if session and not ref.get("session"):
            raise refereelib.RefereeError("this pool does not take session seeds")
        used = "" if session else referee.used_at(seed["seed"], pool["txid"])
        if used and used != digest:
            raise refereelib.RefereeError("that seed was used; a new run needs a new seed")
        seed["used"] = used
        if seed.get("game") and isinstance(seed.get("facts"), dict):
            seed["facts"] = seed["facts"].get(pool["txid"])
        if seed["expires"] < time.time():
            raise refereelib.RefereeError("that seed expired; a new run needs a new seed")
        claimer = seed["address"]
        if not 0 <= n < pool["count"]:
            raise refereelib.RefereeError("no such lot in that pool")
        leg = pool_leg(chain, pool, n)
        with rpc_of(chain) as rpc:
            tx = rpc.call("decoderawtransaction", str(raw))
            vin, vout = tx.get("vin") or [], tx.get("vout") or []
            if len(vin) < (2 if pool.get("fee") == "pool" else 3) or any(
                    (vin[i]["txid"], int(vin[i]["vout"])) != (leg.inputs[i]["txid"], int(leg.inputs[i]["vout"]))
                    for i in range(2)):
                raise refereelib.RefereeError("that is not a claim of this lot")
            outputs = [(int(round(float(o["value"]) * COIN)), bytes.fromhex(o["scriptPubKey"]["hex"])) for o in vout]
            if outputs[:len(leg.outputs)] != list(leg.outputs):
                raise refereelib.RefereeError("that claim does not pay the pool what the lot asks")
            mine = txbuild.p2pkh_script(claimer, getattr(chain, "params", None))
            back = txbuild.p2pkh_script(pool["pool"], getattr(chain, "params", None))
            rest = outputs[len(leg.outputs):]
            if (not rest or rest[-1][1] != mine
                    or any(script not in (mine, back) for _, script in rest)):
                raise refereelib.RefereeError("that claim pays somebody other than the "
                                              "wallet that played")
            for spent in vin[2:]:
                prev = rpc.call("gettxout", spent["txid"], int(spent["vout"]), True)
                if not prev or prev["scriptPubKey"]["hex"] != mine.hex():
                    raise refereelib.RefereeError("that claim spends coins that are not "
                                                  "the player's")
        if pool.get("once") and claimer in pool_claimers(chain, pool):
            raise refereelib.RefereeError("this wallet has already claimed from this prize pool")
        if session:
            verdict = referee_session(chain, pool, ref, seed, n, digest, claimer, replay)
        elif not seed["used"]:
            if not unlimited_address(claimer):
                referee.pace(claimer)
            source = judge_source(chain, ref["judge"])
            params = ref["params"]
            params = {**(params if isinstance(params, dict) else {"params": params}), "claimer": claimer}
            if ref.get("facts"):
                params = {**(params if isinstance(params, dict) else {"params": params}),
                          "facts": refereelib.both(seed.get("facts"), facts_of(chain, ref, claimer))}
            verdict = referee.judge(source, seed["seed"], replay.get("inputs"), params)
            short = refereelib.meets(verdict, ref["require"])
            if short:
                raise refereelib.RefereeError(short)
            if not referee.use_seed(seed["seed"], digest, pool["txid"]):
                raise refereelib.RefereeError("that seed was used; a new run needs a new seed")
        else:
            verdict = {"won": True, "again": True}
        redeem = bytes.fromhex(pool["redeem"])
        raw_inputs = [{"txid": v["txid"], "vout": int(v["vout"])} for v in vin]
        sigs = [referee.sign(fundinglib.sighash(raw_inputs, outputs, i, redeem)).hex() for i in range(2)]
        return {"signatures": sigs, "verdict": verdict}

    def referee_session(chain, pool: dict, ref: dict, seed: dict, n: int, digest: str,
                        claimer: str, replay: dict) -> dict:
        """A claim on a SESSION seed (app.py _referee_session): the record so far, which only ever grows."""
        inputs = replay.get("inputs")
        if not isinstance(inputs, list):
            raise refereelib.RefereeError("a session's replay is the list of what happened, "
                                          "all of it so far, sent with every claim")
        with referee.session_lock(seed["seed"]):
            done = referee.session_paid_for(seed["seed"], pool["txid"], n)
            if done:
                if done != digest:
                    raise refereelib.RefereeError("that lot was already paid from this session")
                return {"won": True, "again": True}
            state_ = referee.session_state(seed["seed"])
            if len(inputs) < state_["upto"] or refereelib.record_sha(inputs[:state_["upto"]]) != state_["sha"]:
                raise refereelib.RefereeError("that replay does not continue this session's "
                                              "last one: a session's record only grows")
            if not unlimited_address(claimer):
                referee.pace(claimer + " " + seed["seed"], refereelib.SESSION_JUDGED_PER_MINUTE)
            source = judge_source(chain, ref["judge"])
            params = ref["params"]
            params = {**(params if isinstance(params, dict) else {"params": params}),
                      "claimer": claimer, "since": state_["upto"],
                      "paid": referee.session_paid(seed["seed"]), "prior": state_["prior"]}
            if ref.get("facts"):
                params["facts"] = refereelib.both(seed.get("facts"), facts_of(chain, ref, claimer))
            verdict = referee.judge(source, seed["seed"], inputs, params)
            short = refereelib.meets(verdict, ref["require"])
            if short:
                raise refereelib.RefereeError(short)
            referee.session_record(seed["seed"], pool["txid"], n, ref["params"], digest,
                                   len(inputs), refereelib.record_sha(inputs), verdict)
            return verdict

    def referee_verdict(chain, pool: dict, n: int, raw: str, replay: Any) -> list[bytes]:
        node = referee_node(pool)
        if not node:
            said = referee_sign(chain, pool["txid"], n, raw, replay)
        else:
            try:
                answer = requests.post(node + "/r/referee/sign", timeout=30, json={
                    "chain": chain.network, "pool": pool["txid"], "lot": n, "raw": raw, "replay": replay})
                said = answer.json()
            except Exception:                               # noqa: BLE001
                raise refereelib.RefereeError(
                    "this pool's referee did not answer; the prize is still there, "
                    "so try again later") from None
            if answer.status_code != 200:
                raise refereelib.RefereeError(str(said.get("detail") or "the referee refused"))
        return [bytes.fromhex(s) for s in said["signatures"]]

    def referee_url(value: Any) -> str:
        node = str(value or "").strip().rstrip("/")
        if not node:
            return ""
        if not re.fullmatch(r"https?://[A-Za-z0-9.\-]+(:\d+)?", node):
            raise refereelib.RefereeError("a referee is named by its address, like "
                                          "https://app.dogecoinarcade.com")
        try:
            if str(requests.get(node + "/r/referee", timeout=10).json().get("pubkey")) == referee.pubkey.hex():
                return ""
        except Exception:                                   # noqa: BLE001
            pass
        return node

    def ask_referee(node: str, path: str, body: dict) -> dict:
        try:
            answer = requests.post(node + path, timeout=30, json=body)
            said = answer.json()
        except Exception:                                   # noqa: BLE001
            raise refereelib.RefereeError("the referee did not answer; try again later") from None
        if answer.status_code != 200:
            raise refereelib.RefereeError(str(said.get("detail") or "the referee refused"))
        return said

    def referee_attest(chain, judge: str, seed_text: str, inputs: Any, params: Any,
                       address: str, content: str) -> dict:
        row = judge_row(chain, judge)
        if not re.fullmatch(r"[0-9a-fA-F]{64}", str(content or "")):
            raise refereelib.RefereeError("name the sha256 of the content to be inscribed")
        seed = referee.seed_row(str(seed_text or ""))
        if seed is None or seed["pool"] != "judge:" + row["txid"]:
            raise refereelib.RefereeError("that seed was not issued for this judge")
        if seed["address"] != address:
            raise refereelib.RefereeError("that seed was issued to another wallet")
        if seed["expires"] < time.time():
            raise refereelib.RefereeError("that seed expired; a new run needs a new seed")
        if referee.used_at(seed["seed"], "attest:" + row["txid"]):
            raise refereelib.RefereeError("that seed was used; a new run needs a new seed")
        referee.pace(address)
        found = index_of(chain).inscription_content(row["txid"])
        if found is None:
            raise refereelib.RefereeError("this node does not hold that judge")
        if params is not None and len(json.dumps(params)) > 4096:
            raise refereelib.RefereeError("a judge's params are at most 4 KB")
        given = {**(params if isinstance(params, dict) else {"params": params}), "claimer": address}
        verdict = referee.judge(found[1].decode("utf-8", "replace"), seed["seed"], inputs, given)
        if not referee.use_seed(seed["seed"], "attested", "attest:" + row["txid"]):
            raise refereelib.RefereeError("that seed was used; a new run needs a new seed")
        return {"attested": refereelib.attest(referee, row["txid"], verdict, address, str(content).lower())}

    # ================================================================ routes: this site as a referee
    @app.get("/r/referee")
    def r_referee():
        """This node as a referee: its key, and the engine and caps its judges run under."""
        return contentlib._json({
            "pubkey": referee.pubkey.hex(), "engine": refereelib.ENGINE,
            # The original's node address when it publishes game state (gamestate.py); this site publishes none.
            "state_publisher": "",
            "cpu_seconds": refereelib.CPU_SECONDS, "memory_bytes": refereelib.MEMORY_BYTES,
            "inputs_bytes": refereelib.INPUTS_BYTES, "judge_bytes": refereelib.JUDGE_BYTES,
            "seed_seconds": refereelib.SEED_SECONDS,
            "judged_per_minute": refereelib.JUDGED_PER_MINUTE})

    @app.post("/r/referee/seed")
    def r_referee_seed(payload: Any = Body(None)):
        said = payload if isinstance(payload, dict) else {}
        try:
            chain = chain_asked(said)
            return JSONResponse(referee_seed(chain, str(said.get("pool") or ""), str(said.get("address") or ""),
                                             game=str(said.get("game") or ""), judge=str(said.get("judge") or ""),
                                             session=said.get("session") is True))
        except (ValueError, refereelib.RefereeError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.post("/r/referee/sign")
    def r_referee_sign(payload: Any = Body(None)):
        said = payload if isinstance(payload, dict) else {}
        try:
            chain = chain_asked(said)
            return JSONResponse(referee_sign(chain, str(said.get("pool") or ""),
                                             _said_int(said, "lot", "the lot", -1), str(said.get("raw") or ""),
                                             said.get("replay")))
        except (ValueError, fundinglib.FundingError, refereelib.RefereeError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.post("/r/referee/attest")
    def r_referee_attest(payload: Any = Body(None)):
        said = payload if isinstance(payload, dict) else {}
        try:
            chain = chain_asked(said)
            return JSONResponse(referee_attest(
                chain, str(said.get("judge") or ""), str(said.get("seed") or ""),
                said.get("inputs"), said.get("params"), str(said.get("address") or ""),
                str(said.get("content") or "")))
        except (ValueError, refereelib.RefereeError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.get("/r/verified/{judge}")
    def r_verified(request: Request, judge: str, after: int = -1, limit: int = 50):
        """V1.0's verified results, trusting this referee's key unless the page names another (as the original)."""
        trusted = str(request.query_params.get("referee") or referee.pubkey.hex()).lower()
        params = {"referee": trusted, "limit": max(1, min(int(limit), 200)), **({"after": after} if after >= 0 else {})}
        code, got = v1.page(f"verified/{judge}", params)
        return contentlib._json(got if got is not None else {"error": "no answer"}, status=code)

    # ================================================================ routes: a player's side
    @app.post("/account/referee/seed")
    def account_referee_seed(request: Request, payload: Any = Body(None)):
        account = app.state.signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        try:
            chain = chain_asked(said)
            address = account_address(account.pubkey, chain)
            if not address:
                raise ValueError("this account has no address yet")
            if said.get("judge"):
                node = referee_url(said.get("referee"))
                if not node:
                    return JSONResponse(referee_seed(chain, "", address, judge=str(said["judge"])))
                got = ask_referee(node, "/r/referee/seed", {
                    "chain": chain.network, "judge": str(said["judge"]), "address": address})
                return JSONResponse({"seed": str(got["seed"]), "expires": int(got["expires"])})
            game = ""
            if said.get("game"):
                row = index_of(chain).inscription(contentlib._key(str(said["game"]).lstrip("#")))
                if row is None:
                    raise refereelib.RefereeError("there is no such game")
                game = row["txid"]
                pool = next((p for p in pools_of_game(chain, game) if p.get("referee")), None)
            else:
                pool = chain_pool(chain, str(said.get("pool") or ""))
            if pool is None or not pool.get("referee"):
                raise refereelib.RefereeError("this pool has no referee" if not game
                                              else "this game has no refereed pool")
            node = referee_node(pool)
            session = bool(game) and said.get("session") is True
            if not node:
                return JSONResponse(referee_seed(chain, pool["txid"], address, game=game, session=session))
            try:
                answer = requests.post(node + "/r/referee/seed", timeout=15, json={
                    "chain": chain.network, "pool": pool["txid"], "address": address,
                    **({"game": game} if game else {}), **({"session": True} if session else {})})
                got = answer.json()
            except Exception:                               # noqa: BLE001
                raise refereelib.RefereeError("this pool's referee did not answer; "
                                              "try again later") from None
            if answer.status_code != 200:
                raise refereelib.RefereeError(str(got.get("detail") or "the referee refused"))
            return JSONResponse({"seed": str(got["seed"]), "expires": int(got["expires"]),
                                 **({"pools": got.get("pools")} if got.get("pools") else {})})
        except (ValueError, refereelib.RefereeError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.post("/account/referee/attest")
    def account_referee_attest(request: Request, payload: Any = Body(None)):
        account = app.state.signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        try:
            chain = chain_asked(said)
            address = account_address(account.pubkey, chain)
            if not address:
                raise ValueError("this account has no address yet")
            node = referee_url(said.get("referee"))
            body = {"chain": chain.network, "judge": str(said.get("judge") or ""),
                    "seed": str(said.get("seed") or ""), "inputs": said.get("inputs"),
                    "params": said.get("params"), "address": address,
                    "content": str(said.get("content") or "")}
            if not node:
                return JSONResponse(referee_attest(chain, body["judge"], body["seed"], body["inputs"],
                                                   body["params"], address, body["content"]))
            return JSONResponse(ask_referee(node, "/r/referee/attest", body))
        except (ValueError, refereelib.RefereeError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)

    # ---------------------------------------------------------------- a pool's own address
    @app.post("/account/pools/open")
    def account_pools_open(request: Request, payload: Any = Body(None)):
        """Register a pool's own address, from the public key the browser derived."""
        account = app.state.signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        try:
            chain = chain_asked(said)
            main = account_address(account.pubkey, chain)
            if not main:
                raise ValueError("this account has no address yet")
            index = _said_int(said, "index", "the pool's number")
            if not 1 <= index < 2 ** 31:
                raise ValueError("a pool address is derived at an index from 1 to 2^31-1")
            pubkey = bytes.fromhex(str(said.get("pubkey") or ""))
            if len(pubkey) != 33:
                raise ValueError("that is not a public key")
            address = b58check_encode(chain.params.pubkeyhash_version, hash160(pubkey))
            ref = None
            if said.get("referee"):
                ref = referee_terms(chain, said["referee"])
                creator_pub = bytes.fromhex(str(said.get("creator_pubkey") or ""))
                if len(creator_pub) != 33 or not main or hash160(creator_pub) != b58check_decode(main)[1]:
                    raise ValueError("a refereed pool needs this wallet's own public key")
                redeem = refereelib.redeem_script(pubkey, bytes.fromhex(ref["pubkey"]), creator_pub)
                address = refereelib.address_of(redeem, chain.params)
        except (ValueError, refereelib.RefereeError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        held = pool_of(account, address, chain.network)
        if held is None:
            if any(p["index"] == index and p["network"] == chain.network for p in pools_of(account)):
                return JSONResponse({"detail": "that index already has a pool"}, status_code=400)
            held = {"address": address, "index": index, "network": chain.network, "created": time.time(),
                    **({"referee": ref, "redeem": redeem.hex(), "pubkey": pubkey.hex(),
                        "creator_pubkey": creator_pub.hex()} if ref else {})}
            save_pool(account, held)
            st.set_setting(f"pool_parent:{address}", main)
        return JSONResponse({"address": address, "index": held["index"], "referee": bool(held.get("referee"))})

    def pool_price(pool: dict, said: dict) -> int:
        if said.get("fee") == "pool":
            if not pool.get("referee"):
                raise ValueError('a pool that pays its own claims ("fee": "pool") needs a referee')
            return 0
        price = parse_amount(str(said.get("price", "")), True)
        _above_dust(price, "price")
        return price

    def bound_of(chain, main: str, said: dict) -> str:
        if not str(said.get("bound") or "").strip():
            return ""
        row = index_of(chain).inscription(contentlib._key(str(said["bound"]).strip().lstrip("#")))
        if row is None or row["owner"] != main:
            raise ValueError("a pool can be tied only to an inscription you hold")
        return row["txid"]

    @app.post("/account/pools/fund")
    def account_pools_fund(request: Request, payload: Any = Body(None)):
        """The one send that moves a pool's tokens and its coins (two a lot) from the account to the pool's address,
        and writes down what the pool is."""
        account = app.state.signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        try:
            chain = chain_asked(said)
            main = account_address(account.pubkey, chain)
            pool = pool_of(account, str(said.get("pool") or ""), chain.network)
            if pool is None:
                raise ValueError("that is not a pool of this account")
            index = index_of(chain)
            if str(said.get("kind") or "token") == "nft":
                return fund_nft_pool(account, chain, main, pool, said)
            prop = index.property(_said_int(said, "property_id", "the token number"))
            if prop is None:
                raise tokenlib.TokenError(f"there is no token {said.get('property_id')}.")
            units = parse_amount(str(said.get("lot", "")), prop["divisible"])
            count = int(said.get("count") or 0)
            if units <= 0 or not 1 <= count <= LOTS_AT_ONCE:
                raise ValueError(f"a lot above zero, and between 1 and {LOTS_AT_ONCE} lots")
            price = pool_price(pool, said)
            claim_hash = str(said.get("claim_hash") or "").strip().lower()
            if not re.fullmatch(r"[0-9a-f]{64}", claim_hash):
                raise ValueError("a pool needs the sha256 of its phrase")
            bound = bound_of(chain, main, said)
            free = lot_room(chain, main, prop["property_id"])        # no claim listings here (NEEDS-V1 above)
            if units * count > free:
                raise tokenlib.TokenError(
                    f"that is {format_amount(units * count, prop['divisible'])} "
                    f"{prop['name']}, and this account has "
                    f"{format_amount(max(0, free), prop['divisible'])} free")
            naming = encode_class_c(P.SimpleSend(property_id=prop["property_id"], amount=units * count).encode())
            outputs = ([(0, txbuild.op_return_script(naming))]
                       + [(SPLIT_EACH, txbuild.p2pkh_script(pool["address"], getattr(chain, "params", None)))] * (2 * count))
            unsigned = fundinglib.build(
                CoinDB(v1, chain), chain.params, main, outputs, rate=fees.MIN_FEE_PER_KB,
                what=(f"move {format_amount(units * count, prop['divisible'])} "
                      f"{prop['name']} and {2 * count} coins into a prize pool"))
            quota(account, "send")
        except (fundinglib.FundingError, tokenlib.TokenError, AmountError, ValueError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        pool.update({"property_id": prop["property_id"], "lot": str(units), "count": count,
                     "price": price, "days": _listing_days(said), "bound": bound,
                     "fee": "pool" if said.get("fee") == "pool" else "",
                     "claim_hash": claim_hash, "closed": False, "kind": "token",
                     "once": bool(said.get("once")), "first": 1})
        save_pool(account, pool)
        offer = offers.add(account.pubkey, chain.network, unsigned, unsigned.what)
        return JSONResponse({"offer": offer.id, "chain": chain.network, **unsigned.as_json()})

    def fund_nft_pool(account, chain, main: str, pool: dict, said: dict):
        index = index_of(chain)
        pieces = []
        for key in (said.get("pieces") or [])[:NFT_POOL_MOST + 1]:
            row = index.inscription(contentlib._key(str(key).strip().lstrip("#")))
            if row is None:
                raise ValueError(f"there is no inscription {key}")
            if row["owner"] != main:
                raise ValueError(f"#{row['number']} is not yours to put in a pool")
            if row["txid"] not in pieces:
                pieces.append(row["txid"])
        if not 1 <= len(pieces) <= NFT_POOL_MOST:
            raise ValueError(f"a pool holds 1 to {NFT_POOL_MOST} pieces")
        price = pool_price(pool, said)
        claim_hash = str(said.get("claim_hash") or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", claim_hash):
            raise ValueError("a pool needs the sha256 of its phrase")
        bound = bound_of(chain, main, said)
        count = len(pieces)
        outputs = [(SPLIT_EACH, txbuild.p2pkh_script(pool["address"], getattr(chain, "params", None)))] * (2 * count)
        unsigned = fundinglib.build(CoinDB(v1, chain), chain.params, main, outputs, rate=fees.MIN_FEE_PER_KB,
                                    what=f"move {2 * count} coins into a prize pool of {count} pieces")
        quota(account, "send")
        pool.update({"kind": "nft", "pieces": pieces, "count": count, "price": price,
                     "fee": "pool" if said.get("fee") == "pool" else "",
                     "days": _listing_days(said), "bound": bound, "claim_hash": claim_hash,
                     "closed": False, "once": bool(said.get("once")), "first": 0,
                     "property_id": 0, "lot": "0"})
        save_pool(account, pool)
        offer = offers.add(account.pubkey, chain.network, unsigned, unsigned.what)
        return JSONResponse({"offer": offer.id, "chain": chain.network, **unsigned.as_json()})

    @app.post("/account/pools/legs")
    def account_pools_legs(request: Request, payload: Any = Body(None)):
        """The lots of a pool this account just funded, for its tab to sign with the pool's key and seal into the
        pool inscription. Built from the funding transaction's own outputs."""
        account = app.state.signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        try:
            chain = chain_asked(said)
            pool = pool_of(account, str(said.get("pool") or ""), chain.network)
            if pool is None or not (pool.get("property_id") or pool.get("kind") == "nft"):
                raise ValueError("that is not a funded pool of this account")
            if pool.get("kind") == "nft":
                return nft_pool_legs(account, chain, pool, str(said.get("fund") or ""))
            fund = str(said.get("fund") or "").strip().lower()
            with rpc_of(chain) as rpc:
                tx = rpc.call("getrawtransaction", fund, 1)
            outs = tx.get("vout") or []
            count = int(pool["count"])
            script = txbuild.p2pkh_script(pool["address"], getattr(chain, "params", None)).hex()
            for vout in range(1, 1 + 2 * count):
                out = outs[vout] if vout < len(outs) else {}
                if (str((out.get("scriptPubKey") or {}).get("hex") or "") != script
                        or int(round(float(out.get("value", 0)) * COIN)) != SPLIT_EACH):
                    raise ValueError("that transaction did not fund this pool")
            shape = {"pool": pool["address"], "fund": fund, "each": SPLIT_EACH,
                     "property_id": int(pool["property_id"]), "lot": int(pool["lot"]),
                     "price": int(pool["price"]), "count": count,
                     "redeem": pool.get("redeem") or "", "fee": pool.get("fee") or ""}
            prop = index_of(chain).property(shape["property_id"])
            legs = []
            for n in range(count):
                leg = pool_leg(chain, shape, n)
                legs.append({**leg.as_json(), "chain": chain.network, "price": shape["price"],
                             "send": {"propertyid": shape["property_id"], "units": str(shape["lot"]),
                                      "amount": format_amount(shape["lot"], prop["divisible"]),
                                      "name": prop["name"]}})
        except Exception as exc:                            # noqa: BLE001
            return JSONResponse({"detail": str(exc)}, status_code=400)
        return JSONResponse({"chain": chain.network, "legs": legs,
                             "prizepool": {"v": 1, "creator": account_address(account.pubkey, chain),
                                           "pool": pool["address"], "index": int(pool["index"]),
                                           "property_id": shape["property_id"], "lot": shape["lot"],
                                           "count": count, "price": shape["price"],
                                           "each": SPLIT_EACH, "fund": fund,
                                           "game": pool.get("bound") or "",
                                           "claim_hash": pool["claim_hash"],
                                           "once": bool(pool.get("once")),
                                           **({"fee": "pool"} if pool.get("fee") == "pool" else {}),
                                           **referee_json(pool)}})

    def nft_pool_legs(account, chain, pool: dict, fund: str):
        fund = fund.strip().lower()
        with rpc_of(chain) as rpc:
            tx = rpc.call("getrawtransaction", fund, 1)
        outs = tx.get("vout") or []
        count = int(pool["count"])
        script = txbuild.p2pkh_script(pool["address"], getattr(chain, "params", None)).hex()
        for vout in range(0, 2 * count):
            out = outs[vout] if vout < len(outs) else {}
            if (str((out.get("scriptPubKey") or {}).get("hex") or "") != script
                    or int(round(float(out.get("value", 0)) * COIN)) != SPLIT_EACH):
                raise ValueError("that transaction did not fund this pool")
        shape = {"kind": "nft", "pool": pool["address"], "fund": fund, "each": SPLIT_EACH,
                 "pieces": list(pool["pieces"]), "price": int(pool["price"]),
                 "count": count, "first": 0, "redeem": pool.get("redeem") or "",
                 "fee": pool.get("fee") or ""}
        legs = [{**pool_leg(chain, shape, n).as_json(), "chain": chain.network, "price": shape["price"]}
                for n in range(count)]
        return JSONResponse({"chain": chain.network, "legs": legs, "prizepool": {
            "v": 1, "kind": "nft", "creator": account_address(account.pubkey, chain),
            "pool": pool["address"], "index": int(pool["index"]), "pieces": shape["pieces"],
            "count": count, "price": shape["price"], "each": SPLIT_EACH, "fund": fund,
            "first": 0, "game": pool.get("bound") or "", "claim_hash": pool["claim_hash"],
            "once": bool(pool.get("once")),
            **({"fee": "pool"} if pool.get("fee") == "pool" else {}),
            **referee_json(pool)}})

    @app.post("/account/pools/close")
    def account_pools_close(request: Request, payload: Any = Body(None)):
        """Cancel and close a pool: everything at its address back to the account in one transaction (an NFT pool:
        one per piece, and one for the rest), signed with the pool's key -- or, for a refereed pool, the creator's."""
        account = app.state.signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        try:
            chain = chain_asked(said)
            main = account_address(account.pubkey, chain)
            chained = chain_pool(chain, str(said.get("prize") or "")) if said.get("prize") else None
            if said.get("prize"):
                if chained is None or chained["creator"] != main:
                    raise ValueError("that prize pool is not this account's")
                pool = {"address": chained["pool"], "index": chained["index"],
                        "property_id": chained["property_id"], "network": chain.network,
                        "redeem": chained.get("redeem") or ""}
            else:
                pool = pool_of(account, str(said.get("pool") or ""), chain.network)
            if pool is None:
                raise ValueError("that is not a pool of this account")
            index = index_of(chain)
            naming = b""
            if chained is not None:
                _, waiting = pool_lots(chain, chained)
                left = index.balance(chained["pool"], chained["property_id"]) - waiting * chained["lot"]
                if left > 0:
                    naming = encode_class_c(P.SimpleSend(property_id=chained["property_id"], amount=left).encode())
            elif pool.get("property_id"):
                left = lot_room(chain, pool["address"], int(pool["property_id"]))
                if left > 0:
                    naming = encode_class_c(P.SimpleSend(property_id=int(pool["property_id"]), amount=left).encode())
            coins = pool_coins(chain, pool["address"])
            if chained is not None:
                known = {(c["txid"], int(c["vout"])) for c in coins}
                first = chained["first"]
                with rpc_of(chain) as rpc:
                    for vout in range(first, first + 2 * chained["count"]):
                        if (chained["fund"], vout) in known:
                            continue
                        if rpc.call("gettxout", chained["fund"], vout, True):
                            coins.append({"txid": chained["fund"], "vout": vout, "value": chained["each"]})
            if chained is not None and chained["kind"] == "nft":
                return close_nft_pool(account, chain, main, chained, coins)
            unsigned = fundinglib.build_pool_close(
                pool["address"], coins, main, fees.MIN_FEE_PER_KB, payload=naming,
                what="cancel and close a prize pool: its tokens and coins come back to you",
                redeem=bytes.fromhex(pool.get("redeem") or ""))
        except (fundinglib.FundingError, ValueError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        if chained is None:
            pool["closed"] = True
            save_pool(account, pool)
        offer = offers.add(account.pubkey, chain.network, unsigned, unsigned.what)
        return JSONResponse({"offer": offer.id, "chain": chain.network, "index": pool["index"],
                             "creator_signs": bool(pool.get("redeem")), **unsigned.as_json()})

    def close_nft_pool(account, chain, main: str, pool: dict, coins: list):
        index = index_of(chain)
        first = pool["first"]
        lot_coins = set()
        made = []
        for n, piece in enumerate(pool["pieces"]):
            a, b = (pool["fund"], first + 2 * n), (pool["fund"], first + 1 + 2 * n)
            lot_coins |= {a, b}
            held = [c for c in coins if (c["txid"], int(c["vout"])) in (a, b)]
            row = index.inscription(piece)
            if len(held) != 2 or row is None or row["owner"] != pool["pool"]:
                continue
            naming = encode_class_c(P.AnyData(data=inscriptionlib.Transfer(
                txid=bytes.fromhex(piece)).encode()).encode())
            unsigned = fundinglib.build_pool_close(
                pool["pool"], held, main, fees.MIN_FEE_PER_KB, payload=naming,
                what=f"send #{row['number']} back from a prize pool",
                redeem=bytes.fromhex(pool.get("redeem") or ""))
            made.append(offers.add(account.pubkey, chain.network, unsigned, unsigned.what))
        rest = [c for c in coins if (c["txid"], int(c["vout"])) not in lot_coins]
        if rest:
            unsigned = fundinglib.build_pool_close(
                pool["pool"], rest, main, fees.MIN_FEE_PER_KB,
                what="what claimers paid into a prize pool, back to you",
                redeem=bytes.fromhex(pool.get("redeem") or ""))
            made.append(offers.add(account.pubkey, chain.network, unsigned, unsigned.what))
        if not made:
            return JSONResponse({"detail": "that pool has nothing left to send back"}, status_code=400)
        return JSONResponse({"chain": chain.network, "index": pool["index"],
                             "creator_signs": bool(pool.get("redeem")), "offers": [
            {"offer": o.id, "chain": chain.network, **o.unsigned.as_json()} for o in made]})

    @app.get("/account/claimpools")
    def account_claimpools(request: Request, chain: str = ""):
        """This account's prize pools to see, finish or close. No node-local claim listings here (NEEDS-V1 above):
        `pools` is empty and `setup` lists the account's own pool addresses that hold anything."""
        account = app.state.signed_in_account(request)
        try:
            ctx = chain_asked({"chain": chain})
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        mine = [p for p in pools_of(account) if p["network"] == ctx.network]
        index = index_of(ctx)
        setup = []
        for p in mine:
            try:
                coins = pool_coins(ctx, p["address"])
                tokens = index.balance(p["address"], int(p["property_id"])) if p.get("property_id") else 0
            except V1Error:
                continue
            if not coins and not tokens:
                continue
            setup.append({"address": p["address"], "index": p["index"],
                          "coins": len(coins), "tokens": int(tokens),
                          "ready": bool(tokens) and len(coins) >= 2 * int(p.get("count") or 0) and not p.get("closed"),
                          "closing": bool(p.get("closed"))})
        return JSONResponse({"chain": ctx.network, "pools": [], "setup": setup,
                             "next_index": max([p["index"] for p in mine] + [0]) + 1},
                            headers={"Cache-Control": "no-store"})

    # ---------------------------------------------------------------- claiming a lot
    def prize_terms(chain, address: str, said: dict) -> tuple:
        """One lot of a chain pool, checked, and the transaction that claims it (app.py _prize_terms)."""
        pool = chain_pool(chain, str(said.get("pool") or ""))
        refusal = pool_refusal(chain, pool, str(said.get("page") or ""))
        if refusal:
            raise ValueError(refusal)
        secret = str(said.get("secret") or "").strip()
        if not secrets.compare_digest(hashlib.sha256(secret.encode()).hexdigest(), pool["claim_hash"]):
            raise ValueError("that is not this prize's phrase" if secret else "this prize opens only with its phrase")
        n = int(said.get("lot", -1))
        if not 0 <= n < pool["count"]:
            raise ValueError("no such lot in that pool")
        if pool.get("once") and address in pool_claimers(chain, pool):
            raise ValueError("this wallet has already claimed from this prize pool")
        leg = pool_leg(chain, pool, n)
        pays = POOL_PAYS if pool.get("fee") == "pool" else 0
        with rpc_of(chain) as rpc:
            listing = listingslib.Listings.register(
                None, rpc, raw=leg.raw, signatures=[str(s) for s in (said.get("signatures") or [])],
                pubkey=bytes.fromhex(pool["redeem"] or pool["pubkey"]), network=chain.network,
                owner=pool["pool"], price=pool["price"], seconds=listingslib.ANSWERED_FOR,
                record=False, claim_hash=pool["claim_hash"], bound=pool["game"], pool_pays=pays)
        listing["parent"] = pool["creator"]
        listing["id"] = ""
        if pays:
            foreign, outputs = listingslib.leg_terms(listing)
            outputs = list(outputs) + [(fees.DUST_LIMIT, txbuild.p2pkh_script(address, getattr(chain, "params", None)))]
            raw = txbuild.build_raw_tx([(c["txid"], int(c["vout"])) for c in foreign], outputs)
            what = f"claim {piece_called(chain, listing)}, its fee paid by the pool"
            unsigned = fundinglib.Unsigned(raw=raw, inputs=[dict(c) for c in foreign], outputs=outputs,
                                           fee=pays - fees.DUST_LIMIT, what=what, signed_from=len(foreign))
            return pool, listing, unsigned, what
        _, unsigned, what = fill_terms(chain, address, listing)
        return pool, listing, unsigned, what

    def broadcast(chain, raw: str) -> str:
        got = v1.cmd("tx.broadcast", hex=raw, chain=chain.key) or {}
        txid = str(got.get("txid") or "")
        if not txid:
            raise V1Error("the arcade did not say it sent it")
        return txid

    @app.post("/account/prize")
    def account_prize(request: Request, payload: Any = Body(None)):
        """Show an account the transaction that claims one lot of a chain pool."""
        account = app.state.signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        try:
            chain = chain_asked(said)
            address = account_address(account.pubkey, chain)
            if not address:
                raise ValueError("this account has no address yet")
            pool, listing, unsigned, _ = prize_terms(chain, address, said)
        except (fundinglib.FundingError, listingslib.ListingError, swaplib.SwapError, ValueError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        return JSONResponse({"chain": chain.network, "pool": pool["txid"],
                             "lot_index": int(said.get("lot")), "claim": True, "lot": True,
                             "seller": pool["creator"],
                             "seller_tag": tags_for([pool["creator"]]).get(pool["creator"], ""),
                             "price": pool["price"], "piece": piece_called(chain, listing),
                             "kind": pool["kind"], **unsigned.as_json()})

    @app.post("/account/prize/sign")
    def account_prize_sign(request: Request, payload: Any = Body(None)):
        """Paste the claimer's signatures onto the lot and broadcast it. The chain decides between two claims of one
        lot: they spend one coin."""
        account = app.state.signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        try:
            chain = chain_asked(said)
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        address = account_address(account.pubkey, chain)
        lane = f"account {account.pubkey}"
        if not begin_send(lane):
            return JSONResponse({"detail": "one of your transactions is still going; "
                                           "try again in a moment"}, status_code=409)
        piece_lane = ""
        try:
            try:
                pool, listing, unsigned, what = prize_terms(chain, address, said)
            except (fundinglib.FundingError, listingslib.ListingError, swaplib.SwapError, ValueError) as exc:
                return JSONResponse({"detail": str(exc)}, status_code=400)
            piece_lane, refusal = claim_piece(chain, listing)
            if refusal:
                return JSONResponse({"detail": refusal}, status_code=409)
            if str(said.get("raw") or "") != unsigned.raw:
                return JSONResponse({"detail": "this node would build a different "
                                     "transaction now; ask again"}, status_code=409)
            try:
                pubkey = bytes.fromhex(str(said.get("pubkey") or ""))
                if not pubkey or hash160(pubkey) != b58check_decode(address)[1]:
                    raise ValueError("that public key is not this account's")
                referee_sigs = None
                if pool.get("referee"):
                    referee_sigs = referee_verdict(chain, pool, int(said.get("lot")), unsigned.raw,
                                                   said.get("replay"))
                with rpc_of(chain) as rpc:
                    raw = listingslib.paste_leg(rpc, listing, unsigned,
                                                [str(x) for x in (said.get("claimer") or [])],
                                                pubkey, referee_sigs=referee_sigs)
                quota(account, "send")
                txid = broadcast(chain, raw)
            except (listingslib.ListingError, fundinglib.FundingError, swaplib.SwapError,
                    AmountError, ValueError, refereelib.RefereeError) as exc:
                return JSONResponse({"detail": str(exc)}, status_code=400)
            except Exception as exc:                        # noqa: BLE001
                return JSONResponse({"detail": f"the node refused it: {exc}"}, status_code=409)
            if pool["kind"] == "token":
                lots_flying.setdefault((chain.network, pool["pool"], pool["property_id"]), {})[txid] = pool["lot"]
            pool_claims.setdefault(pool["txid"], set()).add(address)
            return JSONResponse({"txid": txid, "what": what, "chain": chain.network,
                                 "piece": piece_called(chain, listing)})
        finally:
            end_send(lane)
            if piece_lane:
                end_send(piece_lane)

    @app.post("/account/prize/take")
    def account_prize_take(request: Request, payload: Any = Body(None)):
        """Claim a lot of a pool that pays its own claims ("fee": "pool"): the lot's two coins pay the block and the
        claimer's output, the pool's signatures came sealed with the phrase, and the referee signs the whole
        transaction only after the pool's judge passes the replay."""
        account = app.state.signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        try:
            chain = chain_asked(said)
            address = account_address(account.pubkey, chain)
            if not address:
                raise ValueError("this account has no address yet")
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        piece_lane = ""
        try:
            try:
                pool, listing, unsigned, what = prize_terms(chain, address, said)
                if pool.get("fee") != "pool" or not pool.get("referee"):
                    raise ValueError("this pool does not pay its own claims; claim it the ordinary way")
            except (fundinglib.FundingError, listingslib.ListingError, swaplib.SwapError, ValueError) as exc:
                return JSONResponse({"detail": str(exc)}, status_code=400)
            piece_lane, refusal = claim_piece(chain, listing)
            if refusal:
                return JSONResponse({"detail": refusal}, status_code=409)
            try:
                referee_sigs = referee_verdict(chain, pool, int(said.get("lot")), unsigned.raw, said.get("replay"))
                with rpc_of(chain) as rpc:
                    raw = listingslib.paste_leg(rpc, listing, unsigned, [], b"",
                                                referee_sigs=referee_sigs, pool_pays=True)
                quota(account, "claim")
                txid = broadcast(chain, raw)
            except (listingslib.ListingError, fundinglib.FundingError, swaplib.SwapError,
                    AmountError, ValueError, refereelib.RefereeError) as exc:
                return JSONResponse({"detail": str(exc)}, status_code=400)
            except Exception as exc:                        # noqa: BLE001
                return JSONResponse({"detail": f"the node refused it: {exc}"}, status_code=409)
            if pool["kind"] == "token":
                lots_flying.setdefault((chain.network, pool["pool"], pool["property_id"]), {})[txid] = pool["lot"]
            pool_claims.setdefault(pool["txid"], set()).add(address)
            return JSONResponse({"txid": txid, "what": what, "chain": chain.network,
                                 "piece": piece_called(chain, listing)})
        finally:
            if piece_lane:
                end_send(piece_lane)

    # For tests: the helpers, by name.
    app.state.pools = {"referee": referee, "chain_pool": chain_pool, "pool_from_row": pool_from_row,
                       "pool_leg": pool_leg, "pools_of_game": pools_of_game, "pool_refusal": pool_refusal,
                       "referee_sign": referee_sign, "Rpc": Rpc, "Index": Index}
