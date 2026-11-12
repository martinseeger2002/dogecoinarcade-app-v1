"""Is a name free to claim right now (2026-10-27: "only allow claiming a name on a chain that has a complete name
index so that we don't accidentally let people try to claim names that are already claimed").

The ord index answers for every block it has walked. A complete index still stands a few blocks behind the tip (a walker
settles WALK_SETTLE blocks back, a linked one is heard every minute or so), and a claim sent a moment ago is in no block
at all. So before a name is called free, the blocks past the index's walk and the mempool are read here, straight from
the chain's Core, for any claim of the same name under any reading (ord_rules.NAME_RULES).
"""
from __future__ import annotations

from collections import OrderedDict

from . import ord_rules, rawchain

#: The most blocks past an index's walk that are read here; further behind than this, the index is not complete.
GAP_MAX = 40
#: The most mempool transactions read for one check (a bigger mempool: the name is not called free).
MEMPOOL_MAX = 4000
#: Claims found per block, by block hash: a block never changes, and a name being typed asks again and again.
_BLOCKS: OrderedDict[str, list[tuple[str, str]]] = OrderedDict()
_BLOCKS_KEPT = 400


class NameCheckError(RuntimeError):
    pass


def claims_in(tx: rawchain.Tx, dialect: str, every_input: bool = False) -> list[tuple[str, str]]:
    """(kind, raw name) of every name `tx` claims: an envelope complete in input 0 (and, where the chain's indexer reads
    every input, in any input) whose body is a name claim (ord_rules.name_claim)."""
    out = []
    for j, i in enumerate(tx.vin):
        if i["vout"] == -1 or (j and not every_input):
            continue
        script = i["script"]
        if b"ord" not in script[:8]:
            continue
        try:
            state, ctype, body, _tags = ord_rules.read([script], dialect)
        except Exception:                                    # noqa: BLE001 -- not an envelope after all
            continue
        if state != "complete":
            continue
        got = ord_rules.name_claim(ctype, body)
        if got:
            out.append(got)
    return out


def same_name(made: dict[str, str | None], kind: str, raw: str) -> bool:
    """Does a claim (kind, raw) name what `made` ({rule: the wanted name under it}) names, under any reading?"""
    return any(want and ord_rules.normalise(rule, kind, raw) == want for rule, want in made.items())


def _block_claims(rpc, height: int, dialect: str, every_input: bool, mweb: bool) -> list[tuple[str, str]]:
    block_hash = rpc.call("getblockhash", height)
    kept = _BLOCKS.get(block_hash)
    if kept is None:
        block = rawchain.read_block(bytes.fromhex(rpc.call("getblock", block_hash, 0)), block_hash, mweb=mweb)
        kept = [c for tx in block.txs for c in claims_in(tx, dialect, every_input)]
        _BLOCKS[block_hash] = kept
        while len(_BLOCKS) > _BLOCKS_KEPT:
            _BLOCKS.popitem(last=False)
    return kept


def newest_claims(rpc, net: str, walked_to: int, tip: int, made: dict[str, str | None], mweb: bool = False) -> str:
    """Where a claim of the name waits that the index has not read yet: "block N", "mempool", or "" for none. Reads the
    blocks from `walked_to` (the index's next block; one before it too, in case a walker counts the other way) to `tip`,
    then the mempool. NameCheckError when they cannot all be read."""
    dialect, every = ord_rules.dialect_of(net), ord_rules.every_input(net)
    if tip - walked_to > GAP_MAX:
        raise NameCheckError(f"the index is {tip - walked_to} blocks behind")
    for h in range(max(0, walked_to - 1), tip + 1):
        for kind, raw in _block_claims(rpc, h, dialect, every, mweb):
            if same_name(made, kind, raw):
                return f"block {h:,}"
    txids = list(rpc.call("getrawmempool") or [])
    if len(txids) > MEMPOOL_MAX:
        raise NameCheckError(f"the mempool holds {len(txids):,} transactions, too many to read for claims just now")
    for at in range(0, len(txids), 200):
        part = txids[at:at + 200]
        raws = None
        if hasattr(rpc, "batch"):
            try:
                raws = rpc.batch([("getrawtransaction", [t, 0]) for t in part])
            except Exception:                            # noqa: BLE001 -- one left the mempool: one call each
                raws = None
        if raws is None:                                 # (also a borrowed Core, mesh/coreshare, which has no batch)
            raws = []
            for t in part:
                try:
                    raws.append(rpc.call("getrawtransaction", t, 0))
                except Exception:                        # noqa: BLE001 -- gone from the mempool since: mined
                    raws.append(None)
        for t, raw in zip(part, raws):
            if not isinstance(raw, str):
                continue                                     # gone from the mempool since: mined, read next time
            try:
                tx = rawchain.read_tx(bytes.fromhex(raw), t)
            except Exception:                                # noqa: BLE001
                continue
            if any(same_name(made, kind, name) for kind, name in claims_in(tx, dialect, every)):
                return "mempool"
    # A claim mined while the mempool was being read left it unread: read any block that came meanwhile.
    now = int(rpc.call("getblockcount"))
    for h in range(tip + 1, min(now, tip + GAP_MAX) + 1):
        for kind, raw in _block_claims(rpc, h, dialect, every, mweb):
            if same_name(made, kind, raw):
                return f"block {h:,}"
    return ""
