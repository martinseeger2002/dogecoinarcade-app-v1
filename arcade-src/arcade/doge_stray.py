"""Coins sent to an account's TESTNET key's Dogecoin address, moved to its real Dogecoin address.

2026-10-17: six Anti Doginal Dogs went to @apple's DAKM6Q… -- the hash of @apple's testnet PEP key under
Dogecoin's version byte -- because whoever sent them took the testnet address and changed its first byte. An account's
Dogecoin key is its MAINNET key (coins.js COIN_TYPE doge = main = 3), so the wallet neither shows nor spends that
address. The coins are the account's all the same: its words hold the testnet key (m/44'/1'/0'/0/0). This builds the
one transaction that moves them home, signed in the account's own tab with that key:

    inputs   every coin carrying an inscription (each whole), then the plain coins that pay the fee
    outputs  each inscribed coin's exact value, in the same order, to the account's Dogecoin address -- so every
             inscription sits where it sat, at the same offset of its own output -- then the change, also home

Ord counts satoshis first in, first out: input i and output i hold the same value, so nothing an inscription sits on
crosses into another output. When the plain coins there cannot pay the fee, the account tops the address up with an
ordinary send first (`NeedFee`).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import funding
from .listings import _serialise
from .script import b58check_decode, b58check_encode
from .txbuild import build_raw_tx, p2pkh_script, push


class StrayError(ValueError):
    pass


class NeedFee(StrayError):
    def __init__(self, short: int):
        super().__init__(f"the fee needs {short} more koinu at that address")
        self.short = short


def stray_address(testnet_address: str, doge_version: int) -> str:
    """The Dogecoin address an account's testnet key would have: where those coins went."""
    return b58check_encode(doge_version, b58check_decode(testnet_address)[1])


@dataclass
class Move:
    raw: str
    inputs: list[dict[str, Any]]
    outputs: list[tuple[int, bytes]]
    sighashes: list[str]
    fee: int
    carried: list[dict] = field(default_factory=list)

    def as_json(self) -> dict:
        return {"raw": self.raw, "sighashes": self.sighashes, "fee": self.fee,
                "inputs": [{k: i[k] for k in ("txid", "vout", "value")} for i in self.inputs],
                "outputs": [{"value": v, "script": s.hex()} for v, s in self.outputs],
                "carried": self.carried}


def build_move(stray: str, home: str, inscribed: list[dict], plain: list[dict], rate: int) -> Move:
    """`inscribed`: coins at `stray` carrying inscriptions ({txid, vout, value, inscriptions}); `plain`: the rest."""
    if not inscribed:
        raise StrayError("nothing inscribed is at that address")
    if stray == home:
        raise StrayError("that is already the account's own Dogecoin address")
    to = p2pkh_script(home)
    outs = [(int(c["value"]), to) for c in inscribed]
    payers = sorted(plain, key=lambda c: -int(c["value"]))
    fee = funding.price(len(inscribed) + len(payers), outs, rate, change=True)
    have = sum(int(c["value"]) for c in payers)
    if have < fee:
        raise NeedFee(fee - have)
    outputs = list(outs)
    change = have - fee
    if change > 0:
        outputs.append((change, to))
    inputs = [{"txid": c["txid"], "vout": int(c["vout"]), "value": int(c["value"])} for c in inscribed + payers]
    raw = build_raw_tx([(i["txid"], i["vout"]) for i in inputs], outputs)
    mine = p2pkh_script(stray)
    hashes = [funding.sighash(inputs, outputs, n, mine).hex() for n in range(len(inputs))]
    return Move(raw=raw, inputs=inputs, outputs=outputs, sighashes=hashes, fee=fee,
                carried=[{"txid": c["txid"], "vout": int(c["vout"]), "inscriptions": c.get("inscriptions") or []}
                         for c in inscribed])


def assemble(move: Move, signatures: list[str], pubkey: bytes) -> str:
    if len(signatures) != len(move.inputs):
        raise StrayError(f"{len(move.inputs)} signatures were needed and {len(signatures)} came back")
    return _serialise([(i["txid"], i["vout"], push(bytes.fromhex(s)) + push(pubkey))
                       for i, s in zip(move.inputs, signatures)], move.outputs)
