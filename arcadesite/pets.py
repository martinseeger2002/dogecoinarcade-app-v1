"""Pets for games (ASHVALE's Doginal Dogs, Anti Doginal Dogs and Doginal Frogs): the two page API reads the original
arcade answered from its collection registry, which Arcade V1.0 does not answer (its walletsvc stubs them).

    GET /r/doginals/<address>                 the pets an account holds on Dogecoin: {address, items, unknown}
                                              items: {inscription, number, collection: {name}}
    GET /r/collection-item/doge/<inscription> one Doginal Dog or Doginal Frog: {inscription, number, collection, traits}

The operator chose this 2026-11-13 ("Website serves it"): the old registry roots were inscribed on the old Pepecoin
testnet and are gone, and V1.0 counts a collection only on its own items' chain.
- Who holds what: V1.0's own Dogecoin index (explorer.held) is the truth.
- Which collection, which number, which traits: the website's own lists, bundled in data/ and read once at start:
  doginal_dogs.json (10,000) and doginal_frogs.json (10,066: Frogs #69, #6969 and #10000 were never handed out and
  are in no list), built 2026-11-13 from every collection item's public record, with the known mistakes fixed
  (Doginal Dog #3079 listed as a second #3099; Frog #10001 carrying #69's traits). anti_doginal_dogs.json comes from
  the ADD repository's DM.json. Nothing outside this arcade is asked.
- A game asks with the address it knows (the player's Pepecoin testnet address); the account's Dogecoin address is
  the one it filed at sign-up. A Dogecoin address is asked about as it is.
Both reads are public (a holding on chain is public) and open to sandboxed game frames (CORS).
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

from arcade.script import b58check_decode
from fastapi.responses import JSONResponse

from .v1 import V1Error

DATA = Path(__file__).parent / "data"
ADD_NAME = "Anti Doginal Dogs"
CORS = {"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"}
INSCRIPTION_ID = re.compile(r"[0-9a-f]{64}i[0-9]+")
HELD_FOR = 300.0             # an account's pets are read again after 5 minutes


def _key(iid: str) -> str:
    """The lists' key for an inscription id: the txid alone for the first inscription of a transaction."""
    return iid[:-2] if iid.endswith("i0") else iid


class Collection:
    """One bundled list: {collection, slug, traits: [names], values: {name: [values]}, items: {key: [n, value index...]}}."""

    def __init__(self, path: Path):
        got = json.loads(path.read_text())
        self.name, self.slug = got["collection"], got["slug"]
        self.traits, self.values, self.items = got["traits"], got["values"], got["items"]

    def number(self, iid: str) -> int | None:
        row = self.items.get(_key(iid))
        return row[0] if row else None

    def item(self, iid: str) -> dict | None:
        row = self.items.get(_key(iid))
        if not row:
            return None
        traits = {t: self.values[t][i] for t, i in zip(self.traits, row[1:])}
        return {"inscription": iid, "number": row[0], "collection": {"name": self.name, "id": self.slug},
                "traits": {t: v for t, v in traits.items() if v != ""}}


def load() -> tuple[list[Collection], dict[str, int]]:
    pets = [Collection(DATA / "doginal_dogs.json"), Collection(DATA / "doginal_frogs.json")]
    adds = json.loads((DATA / "anti_doginal_dogs.json").read_text())["ids"]
    return pets, adds


def register(app, st, chains):
    v1 = st.v1
    lock = threading.Lock()
    held_kept: dict[str, tuple[float, dict]] = {}
    collections, adds = load()

    def doge():
        return chains.by_network("doge")

    def dogecoin_address(address: str) -> str:
        """The Dogecoin address to read: a Dogecoin address as it is, else the account's that filed `address`."""
        one = doge()
        if one is None:
            return ""
        try:
            version, payload = b58check_decode(address)
        except Exception:                                   # noqa: BLE001 -- not an address at all
            return ""
        if len(payload) == 20 and version in (one.params.pubkeyhash_version, one.params.scripthash_version):
            return address
        for name, value in st.settings().items():
            if value == address and name.startswith("address:"):
                pubkey = name.rsplit(":", 1)[-1]
                got = chains.account_address(pubkey, one)
                if got:
                    return got
        return ""

    def held_ids(address: str) -> set[str] | None:
        """Every inscription V1.0's Dogecoin index says the address holds; None when it cannot say."""
        out: set[str] = set()
        offset = 0
        try:
            while True:
                got = v1.cmd("explorer.held", _timeout=90, chain=doge().key, address=address, offset=offset,
                             limit=500, order="old") or {}
                rows = [r for r in got.get("items") or [] if isinstance(r, dict) and r.get("id")]
                out.update(str(r["id"]) for r in rows)
                offset += 500
                if not rows or offset >= int(got.get("total") or 0):
                    return out
        except V1Error:
            return None

    def pets_of(address: str) -> dict | None:
        with lock:
            hit = held_kept.get(address)
        if hit and time.time() - hit[0] < HELD_FOR:
            return hit[1]
        held = held_ids(address)
        if held is None:
            return None
        items = []
        for iid in held:
            for one in collections:
                n = one.number(iid)
                if n is not None:
                    items.append({"inscription": iid, "number": n, "collection": {"name": one.name}})
                    break
            else:
                if iid.endswith("i0") and iid[:-2] in adds:
                    items.append({"inscription": iid, "number": adds[iid[:-2]], "collection": {"name": ADD_NAME}})
        items.sort(key=lambda x: (x["collection"]["name"], x["number"]))
        got = {"address": address, "items": items, "unknown": 0}
        with lock:
            if len(held_kept) > 500:
                held_kept.clear()
            held_kept[address] = (time.time(), got)
        return got

    @app.get("/r/doginals/{address}")
    def r_doginals(address: str):
        address = address.strip()
        found = dogecoin_address(address)
        if not found:
            return JSONResponse({"address": address, "items": [], "unknown": 0,
                                 "note": "no Dogecoin address is known for that address"}, headers=CORS)
        got = pets_of(found)
        if got is None:
            return JSONResponse({"error": "this arcade's Dogecoin index could not say just now"}, status_code=503,
                                headers=CORS)
        return JSONResponse(got, headers=CORS)

    @app.get("/r/collection-item/{chain}/{iid}")
    def r_collection_item(chain: str, iid: str):
        iid = iid.strip().lower()
        item = None
        if chain == "doge" and INSCRIPTION_ID.fullmatch(iid):
            item = next((got for one in collections if (got := one.item(iid))), None)
        if not item:
            return JSONResponse({"error": "no collection this arcade knows names it"}, status_code=404, headers=CORS)
        return JSONResponse(item, headers=CORS)
