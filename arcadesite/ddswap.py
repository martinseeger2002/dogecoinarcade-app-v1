"""DigiDollar locks for the website's swaps: V1.0's
crates/arcade-wallet/src/ddlock.rs in Python, byte for byte, so a website visitor's browser can lock, claim and refund
DigiDollars in a swap with an arcade's V1.0.

A lock is a Taproot output whose only ways out are two script leaves, under BIP-341's unspendable internal key:
    claim:  [OP_SIZE 32 OP_EQUALVERIFY] OP_SHA256 <H> OP_EQUALVERIFY <claimer> OP_CHECKSIG   witness: sig, secret, leaf, control
    refund: <time> OP_CHECKLOCKTIMEVERIFY OP_DROP <refunder> OP_CHECKSIG                       witness: sig, leaf, control
plus RESERVE DGB at the same output script for the spend's fee. The lock itself is a DigiDollar send to it (V1.0's
build_lock: the dollars to the lock, the dollar change, the reserve to the lock, the DGB change, the record); a claim
or a refund spends the dollar coin and the reserve, paying the dollars and what the reserve has left.
"""
from __future__ import annotations

from arcade import funding as fundinglib
from arcade import taproot as tap
from arcade.ddlib import ddtx
from arcade.ddlib import taproot as tr

#: A reserve of DGB beside the dollars, for the claim's or refund's own fee (ddlock.rs RESERVE).
RESERVE = 50_000_000
LEAF_VERSION = 0xC0
#: OP_SIZE 32 OP_EQUALVERIFY: a claim takes only a 32-byte secret (htlc.rs SIZE_32).
SIZE_32 = bytes([0x82, 0x01, 0x20, 0x88])
#: DigiByte's DigiDollar fee rules (crates/arcade-wallet/src/dd.rs).
FEE_PER_BYTE, LEAST_FEE, MAX_FEE, DUST = ddtx.FEE_PER_BYTE, ddtx.LEAST_FEE, ddtx.MAX_FEE, ddtx.DUST


class Lock:
    def __init__(self, hash32: bytes, claimer: bytes, refunder: bytes, time: int, sized: bool = False):
        """`claimer` and `refunder` are x-only keys (32 bytes; a compressed key's last 32)."""
        self.hash, self.time, self.sized = bytes(hash32), int(time), bool(sized)
        self.claimer = bytes(claimer[-32:])
        self.refunder = bytes(refunder[-32:])

    def claim_leaf(self) -> bytes:
        return (SIZE_32 if self.sized else b"") + b"\xa8" + tr.push(self.hash) + b"\x88" + tr.push(self.claimer) + b"\xac"

    def refund_leaf(self) -> bytes:
        return tr.push(tr.num(self.time)) + b"\xb1\x75" + tr.push(self.refunder) + b"\xac"

    def _tree(self):
        a, b = tr.leaf(self.claim_leaf()), tr.leaf(self.refund_leaf())
        return a, b, tr.branch(a, b)

    def output(self) -> tuple[bytes, int]:
        return tr.tweaked(tr.NUMS, self._tree()[2])

    def script(self) -> bytes:
        return tr.p2tr(self.output()[0])

    def control(self, claim: bool) -> bytes:
        a, b, _ = self._tree()
        return bytes([LEAF_VERSION | self.output()[1]]) + tr.NUMS + (b if claim else a)


def record(amounts: list[int]) -> bytes:
    """The DigiDollar record: OP_RETURN "DD" 2 <each dollar output's cents>."""
    return bytes([tr.OP_RETURN]) + tr.push(b"DD") + tr.push(tr.num(2)) + b"".join(tr.push(tr.num(a)) for a in amounts)


def lock_send(net: str, owner: bytes, held: list[dict], coins: list[dict], pay_script: bytes, lock: Lock, cents: int,
              fresh=None) -> dict:
    """A DigiDollar send of `cents` into `lock`, prepared as ddtx.send prepares one (the sender's dollar coins by their
    key path, DGB coins for the reserve and the fee), with V1.0's build_lock's outputs: the dollars to the lock, the
    dollar change to the sender, RESERVE DGB to the lock, the DGB change, the record."""
    if not ddtx.LEAST_OUTPUT <= cents <= ddtx.MOST_OUTPUT:
        raise ValueError("a DigiDollar lock holds $1 to $100,000")
    spend, have = [], 0
    for coin in sorted(held, key=lambda c: -c["cents"]):
        if have >= cents:
            break
        if fresh is not None and not fresh(coin):
            continue
        spend.append(coin)
        have += coin["cents"]
    if have < cents:
        raise ValueError(f"you hold ${have / 100:,.2f} in DigiDollars here and this locks ${cents / 100:,.2f}")
    back = have - cents
    if 0 < back < ddtx.LEAST_OUTPUT:
        raise ValueError(f"that would leave ${back / 100:,.2f} in DigiDollars as change, under DigiDollar's $1 least")
    script = lock.script()
    outputs = [(0, script)] + ([(0, tr.p2tr(tr.tweak(owner)))] if back else []) + [(RESERVE, script)]
    rec = record([cents] + ([back] if back else []))
    chosen, _fee, change = ddtx.pay(coins, outputs + [(0, rec)], RESERVE, ddtx.P2PKH_INPUT, pay_script,
                                    extra=2 + ddtx.DD_INPUT * len(spend), fresh=fresh)
    outputs += ([(change, pay_script)] if change else []) + [(0, rec)]
    inputs = [{"txid": c["txid"], "vout": c["vout"], "taproot": {"key": True}} for c in spend]
    inputs += [{"txid": c["txid"], "vout": c["vout"]} for c in chosen]
    return {"chain": net, "version": ddtx.SEND_VERSION, "inputs": inputs, "outputs": [(v, s.hex()) for v, s in outputs],
            "what": f"lock ${cents / 100:,.2f} DigiDollar for a swap" + (f", ${back / 100:,.2f} back to you" if back else "")}


def spend(lock: Lock, dollars: dict, cents: int, reserve: dict, to: bytes, dgb_to: bytes,
          secret: bytes | None) -> fundinglib.Unsigned:
    """The unsigned claim (`secret`) or refund (None) of a lock: its dollar coin ({txid, vout}) and its reserve ({txid,
    vout, value}) by one leaf, the dollars to the P2TR script `to`, what the reserve has left after the fee to `dgb_to`
    (ddlock.rs build_spend). Each input carries its leaf and control block; a claim's carries the secret as `extra`,
    put between the signature and the leaf when it is assembled (funding._assemble_witness)."""
    import hashlib
    claim = secret is not None
    if claim and hashlib.sha256(secret).digest() != lock.hash:
        raise ValueError("that secret doesn't open this lock")
    leaf = lock.claim_leaf() if claim else lock.refund_leaf()
    locktime = 0 if claim else lock.time
    seq = 0xFFFFFFFF if claim else 0xFFFFFFFE
    rec = record([cents])
    size = 10 + 2 + 2 * (41 + 1 + 65 + 33 + len(leaf) + 65 + 1) + 9 + len(to) + 9 + len(dgb_to) + 9 + len(rec)
    fee = min(max(size * FEE_PER_BYTE, LEAST_FEE), MAX_FEE)
    left = int(reserve["value"]) - fee
    if left < 0:
        raise ValueError("the reserve doesn't cover the fee")
    outputs = [(0, to)] + ([(left, dgb_to)] if left >= DUST else []) + [(0, rec)]
    script = lock.script()
    t = {"leaf": leaf.hex(), "control": lock.control(claim).hex(), **({"extra": [secret.hex()]} if claim else {})}
    inputs = [{"txid": dollars["txid"], "vout": int(dollars["vout"]), "value": 0, "spent": script.hex(), "sequence": seq,
               "address": "", "taproot": dict(t)},
              {"txid": reserve["txid"], "vout": int(reserve["vout"]), "value": int(reserve["value"]), "spent": script.hex(),
               "sequence": seq, "address": "", "taproot": dict(t)}]
    rows = [{**c, "spent": script} for c in inputs]
    hashes = [tap.sighash(rows, outputs, n, version=ddtx.SEND_VERSION, locktime=locktime, leaf=leaf).hex() for n in range(2)]
    raw = fundinglib.serialize_segwit(inputs, outputs, locktime=locktime, version=ddtx.SEND_VERSION)
    return fundinglib.Unsigned(raw, inputs, outputs, hashes, fee=fee + (left if left < DUST else 0),
                               what=("claim" if claim else "refund") + f" ${cents / 100:,.2f} DigiDollar from a swap's lock",
                               version=ddtx.SEND_VERSION, locktime=locktime)
