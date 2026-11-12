"""Check an OFFER before signing it, the way the browser's verifyOffer does.

The device never signs a digest it was handed. It parses the unsigned
transaction, checks that the outputs are what it asked for, and computes each
SIGHASH_ALL digest itself over its own P2PKH script.

Legacy sighash does not commit to input values, so the fee shown is only as
true as the input values the gateway reports; the device cross-checks them
against its cached COINS list when it has one.
"""
from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass, field

from .device_keys import b58decode_check, hash160


def dsha(b: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(b).digest()).digest()


def _varint(b: bytes, at: int) -> tuple[int, int]:
    n = b[at]
    if n < 0xFD:
        return n, at + 1
    size = {0xFD: 2, 0xFE: 4, 0xFF: 8}[n]
    return int.from_bytes(b[at + 1:at + 1 + size], "little"), at + 1 + size


def varint(n: int) -> bytes:
    if n < 0xFD:
        return bytes([n])
    if n <= 0xFFFF:
        return b"\xfd" + struct.pack("<H", n)
    if n <= 0xFFFFFFFF:
        return b"\xfe" + struct.pack("<I", n)
    return b"\xff" + struct.pack("<Q", n)


@dataclass
class TxIn:
    txid: bytes          # display order (big-endian hex order)
    vout: int
    script: bytes
    sequence: int


@dataclass
class TxOut:
    value: int
    script: bytes


@dataclass
class Tx:
    version: int
    ins: list[TxIn] = field(default_factory=list)
    outs: list[TxOut] = field(default_factory=list)
    locktime: int = 0

    @classmethod
    def parse(cls, raw: bytes) -> "Tx":
        at = 4
        version = struct.unpack("<i", raw[:4])[0]
        if raw[at:at + 2] == b"\x00\x01":
            raise ValueError("segwit offers are not supported on the T-Deck yet")
        tx = cls(version)
        n, at = _varint(raw, at)
        for _ in range(n):
            txid = raw[at:at + 32][::-1]
            vout = struct.unpack("<I", raw[at + 32:at + 36])[0]
            slen, at = _varint(raw, at + 36)
            script = raw[at:at + slen]
            at += slen
            seq = struct.unpack("<I", raw[at:at + 4])[0]
            at += 4
            tx.ins.append(TxIn(txid, vout, script, seq))
        n, at = _varint(raw, at)
        for _ in range(n):
            value = struct.unpack("<Q", raw[at:at + 8])[0]
            slen, at = _varint(raw, at + 8)
            tx.outs.append(TxOut(value, raw[at:at + slen]))
            at += slen
        tx.locktime = struct.unpack("<I", raw[at:at + 4])[0]
        if at + 4 != len(raw):
            raise ValueError("trailing bytes after the transaction")
        return tx

    def serialize(self, scripts: list[bytes] | None = None) -> bytes:
        out = struct.pack("<i", self.version) + varint(len(self.ins))
        for i, tin in enumerate(self.ins):
            s = tin.script if scripts is None else scripts[i]
            out += tin.txid[::-1] + struct.pack("<I", tin.vout) + varint(len(s)) + s + struct.pack("<I", tin.sequence)
        out += varint(len(self.outs))
        for o in self.outs:
            out += struct.pack("<Q", o.value) + varint(len(o.script)) + o.script
        return out + struct.pack("<I", self.locktime)

    def sighash_all(self, index: int, script_code: bytes) -> bytes:
        scripts = [script_code if i == index else b"" for i in range(len(self.ins))]
        return dsha(self.serialize(scripts) + struct.pack("<I", 1))

    def txid(self) -> bytes:
        return dsha(self.serialize())[::-1]


def p2pkh(h160: bytes) -> bytes:
    return b"\x76\xa9\x14" + h160 + b"\x88\xac"


def p2sh(h160: bytes) -> bytes:
    return b"\xa9\x14" + h160 + b"\x87"


def script_for(address: str, p2pkh_version: int, p2sh_version: int) -> bytes:
    version, h = b58decode_check(address)
    if version == p2pkh_version:
        return p2pkh(h)
    if version == p2sh_version:
        return p2sh(h)
    raise ValueError(f"{address} is not an address on this chain")


class OfferRefused(ValueError):
    pass


@dataclass
class Checked:
    digests: list[bytes]
    paid: int            # to the payee (send) or to others (mail)
    change: int
    fee: int


def check_offer(raw: bytes, input_values: list[int], own_pubkey: bytes, *,
                pay_script: bytes | None, amount: int | None,
                fee_cap: int, others_cap: int = 0) -> Checked:
    """Refuse unless the offer is exactly what this device asked for.

    A coin send (`pay_script` and `amount` given) must pay exactly `amount`
    to `pay_script`, the rest only back to us. A mail post (`pay_script`
    None) may pay at most `others_cap` in total to anything that is not us
    (the marker, multisig and notice outputs).
    """
    tx = Tx.parse(raw)
    own = p2pkh(hash160(own_pubkey))
    if len(input_values) != len(tx.ins):
        raise OfferRefused("input values do not match the inputs")
    for tin in tx.ins:
        if tin.script:
            raise OfferRefused("an input already carries a script")
    paid = change = 0
    for o in tx.outs:
        if o.script == own:
            change += o.value
        elif pay_script is not None and o.script == pay_script:
            paid += o.value
        elif pay_script is None:
            paid += o.value
        else:
            raise OfferRefused("an output pays somebody this device did not name")
    total_in = sum(input_values)
    fee = total_in - paid - change
    if fee < 0:
        raise OfferRefused("outputs spend more than the inputs hold")
    if fee > fee_cap:
        raise OfferRefused(f"fee {fee} is over the cap {fee_cap}")
    if pay_script is not None and paid != amount:
        raise OfferRefused(f"pays {paid}, asked {amount}")
    if pay_script is None and paid > others_cap:
        raise OfferRefused(f"pays others {paid}, cap {others_cap}")
    digests = [tx.sighash_all(i, own) for i in range(len(tx.ins))]
    return Checked(digests, paid, change, fee)


def assemble(raw: bytes, sigs: list[bytes], pubkey: bytes) -> bytes:
    """A signed P2PKH transaction (used for offline P2P payments)."""
    tx = Tx.parse(raw)
    scripts = [bytes([len(s)]) + s + bytes([len(pubkey)]) + pubkey for s in sigs]
    return tx.serialize(scripts)
