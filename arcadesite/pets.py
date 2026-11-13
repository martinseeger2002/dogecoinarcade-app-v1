"""Pets for games (ASHVALE's Doginal Dogs, Anti Doginal Dogs and Doginal Frogs): the two page API reads the original
arcade answered from its collection registry, which Arcade V1.0 does not answer (its walletsvc stubs them).

    GET /r/doginals/<address>                 the pets an account holds on Dogecoin: {address, items, unknown}
                                              items: {inscription, number, collection: {name}}
    GET /r/collection-item/doge/<inscription> one Doginal Dog or Doginal Frog: {inscription, number, collection, traits}

The operator chose this 2026-11-13 ("Website serves it"): the old registry roots were inscribed on the old Pepecoin
testnet and are gone, and V1.0 counts a collection only on its own items' chain.
- Who holds what: V1.0's own Dogecoin index (explorer.held) is the truth. If it cannot answer, doggy.market's word
  stands (a pet is only a picture).
- Which collection, which number, which traits: doggy.market (api.doggy.market, the marketplace's public API), kept
  here once read (an inscription's traits never change). The Anti Doginal Dogs are not on doggy.market: their ids
  come from the collection's own list (data/anti_doginal_dogs.json, from the ADD repository's DM.json).
- A game asks with the address it knows (the player's Pepecoin testnet address); the account's Dogecoin address is
  the one it filed at sign-up. A Dogecoin address is asked about as it is.
Both reads are public (a holding on chain is public) and open to sandboxed game frames (CORS).
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from arcade.script import b58check_decode
from fastapi.responses import JSONResponse

from .v1 import V1Error

DOGGY = "https://api.doggy.market"
COLLECTIONS = {"doginaldogs": "Doginal Dogs", "doginalfrogs": "Doginal Frogs"}   # doggy.market id -> the game's name
ADD_NAME = "Anti Doginal Dogs"
CORS = {"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"}
INSCRIPTION_ID = re.compile(r"[0-9a-f]{64}i[0-9]+")
HELD_FOR = 300.0             # an account's pets are read again after 5 minutes
NOT_IN_FOR = 3600.0          # an inscription in no pet collection is asked about again after an hour
PAGE = 100

#: What doggy.market says wrongly, checked against the pictures (2026-10-17 and 2026-10-19 audits).
#: Doginal Dog #3079 (cbd2d8d7...) is listed as a second "#3099".
NUMBER_FIX = {("doginaldogs", "cbd2d8d7", 3099): 3079}
#: Doginal Frog #10001 carries #69's traits on chain and on the marketplace; its picture is this.
TRAITS_FIX = {("doginalfrogs", 10001): {"Body": "Zombie Pepe (Purple)", "Eyes": "Anon Visor", "Head": "Crown (Purple)",
                                       "Clothes": "Lumberjack (Yellow)", "Background": "Yellow"}}


def _get(path: str, timeout: float = 20.0):
    req = urllib.request.Request(DOGGY + path, headers={"Accept": "application/json",
                                                        "User-Agent": "dogecoinarcade-site (pets)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8") or "null")


def item_of(row: dict) -> dict | None:
    """A doggy.market inscription row -> the game's collection item, or None when it is in no pet collection."""
    nft = row.get("nft") if isinstance(row, dict) else None
    if not isinstance(nft, dict) or nft.get("collectionId") not in COLLECTIONS:
        return None
    iid = str(row.get("inscriptionId") or "")
    try:
        n = int(nft.get("itemId"))
    except (TypeError, ValueError):
        return None
    slug = nft["collectionId"]
    n = NUMBER_FIX.get((slug, iid[:8], n), n)
    traits = {str(k): str(v) for k, v in (nft.get("attributes") or {}).items()}
    traits.update(TRAITS_FIX.get((slug, n), {}))
    return {"inscription": iid, "number": n, "collection": {"name": COLLECTIONS[slug], "id": slug},
            "traits": traits}


def register(app, st, chains):
    v1 = st.v1
    lock = threading.Lock()
    held_kept: dict[str, tuple[float, dict]] = {}
    db_path = Path(st.home) / "pets.sqlite"
    adds = {f"{txid}i0": n for txid, n in json.loads(
        (Path(__file__).parent / "data" / "anti_doginal_dogs.json").read_text())["ids"].items()}

    def db():
        con = sqlite3.connect(db_path, timeout=10)
        con.execute("CREATE TABLE IF NOT EXISTS items (id TEXT PRIMARY KEY, item TEXT, at REAL)")
        return con

    def kept_item(iid: str):
        """(True, item or None) when kept and still good; (False, None) when it must be asked."""
        with db() as con:
            row = con.execute("SELECT item, at FROM items WHERE id = ?", (iid,)).fetchone()
        if not row:
            return False, None
        item = json.loads(row[0]) if row[0] else None
        if item is None and time.time() - row[1] > NOT_IN_FOR:
            return False, None
        return True, item

    def keep_item(iid: str, item: dict | None) -> None:
        with db() as con:
            con.execute("INSERT OR REPLACE INTO items (id, item, at) VALUES (?, ?, ?)",
                        (iid, json.dumps(item) if item else "", time.time()))

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

    def marketplace_pets(address: str) -> list[dict]:
        items = []
        for slug in COLLECTIONS:
            offset = 0
            while True:
                got = _get(f"/wallet/{address}/nfts/{slug}?offset={offset}&limit={PAGE}") or {}
                rows = got.get("data") or []
                for row in rows:
                    item = item_of(row)
                    if item:
                        items.append(item)
                offset += PAGE
                if not rows or offset >= int(got.get("total") or 0):
                    break
        return items

    def pets_of(address: str) -> dict:
        with lock:
            hit = held_kept.get(address)
        if hit and time.time() - hit[0] < HELD_FOR:
            return hit[1]
        held = held_ids(address)
        items, seen = [], set()
        try:
            for item in marketplace_pets(address):
                keep_item(item["inscription"], item)
                if (held is None or item["inscription"] in held) and item["inscription"] not in seen:
                    seen.add(item["inscription"])
                    items.append({k: item[k] for k in ("inscription", "number", "collection")})
        except (urllib.error.URLError, OSError, ValueError):
            pass                                            # the Anti Doginal Dogs still come from the index
        for iid in sorted(held or ()):
            if iid in adds:
                items.append({"inscription": iid, "number": adds[iid], "collection": {"name": ADD_NAME}})
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
        return JSONResponse(pets_of(found), headers=CORS)

    @app.get("/r/collection-item/{chain}/{iid}")
    def r_collection_item(chain: str, iid: str):
        iid = iid.strip().lower()
        if chain != "doge" or not INSCRIPTION_ID.fullmatch(iid):
            return JSONResponse({"error": "no collection this arcade knows names it"}, status_code=404, headers=CORS)
        known, item = kept_item(iid)
        if not known:
            try:
                item = item_of(_get(f"/inscriptions/{iid}") or {})
            except urllib.error.HTTPError as e:
                if e.code != 404:
                    return JSONResponse({"error": f"the marketplace could not say just now ({e.code})"},
                                        status_code=503, headers=CORS)
                item = None
            except (urllib.error.URLError, OSError, ValueError):
                return JSONResponse({"error": "the marketplace could not say just now"}, status_code=503, headers=CORS)
            keep_item(iid, item)
        if not item:
            return JSONResponse({"error": "no collection this arcade knows names it"}, status_code=404, headers=CORS)
        return JSONResponse(item, headers=CORS)
