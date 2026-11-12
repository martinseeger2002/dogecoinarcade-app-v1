"""DigiDollar locks for the website's swaps (arcadesite/ddswap.py), offline: the lock matches V1.0's ddlock.rs (vectors
printed by the Rust), the lock send's outputs are V1.0's build_lock's, and a claim and a refund sign by their leaves
(BIP-340 here, checked with arcade.taproot.verify), the claim's witness carrying the secret.

    .venv/bin/python tests/ddswap_test.py
"""
import hashlib, os, sys
sys.path.insert(0, os.environ.get("ARCADE_SRC", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "arcade-src")))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from arcade import funding as fundinglib  # noqa: E402
from arcade import taproot as tap  # noqa: E402
from arcade.ddlib import ddtx, taproot as tr  # noqa: E402
from arcadesite.ddswap import RESERVE, Lock, lock_send, spend  # noqa: E402

fails = []


def check(ok, m):
    print(("ok   " if ok else "FAIL ") + m)
    ok or fails.append(m)


def xonly(d: int) -> bytes:
    return tap.mul(d)[0].to_bytes(32, "big")


def sign(d: int, msg: bytes) -> bytes:
    """BIP-340, with no aux randomness (tests only)."""
    p = tap.mul(d)
    if p[1] & 1:
        d = tap.N - d
    px = p[0].to_bytes(32, "big")
    k = int.from_bytes(tap.tagged("BIP0340/nonce", d.to_bytes(32, "big") + px + msg), "big") % tap.N
    r = tap.mul(k)
    if r[1] & 1:
        k = tap.N - k
    rx = r[0].to_bytes(32, "big")
    e = int.from_bytes(tap.tagged("BIP0340/challenge", rx + px + msg), "big") % tap.N
    return rx + ((k + e * d) % tap.N).to_bytes(32, "big")


CK, RK = int.from_bytes(bytes([7]) * 32, "big"), int.from_bytes(bytes([9]) * 32, "big")
secret = bytes([5]) * 32
H = hashlib.sha256(secret).digest()
# V1.0's ddlock.rs for these keys and time (printed by the Rust, 2026-11-12)
V1_SCRIPT = {False: "5120617d0f34bc51cbb4b67e433b6c75d1b3e9d9e87e579300c765c50949b96e9f02",
             True: "5120892ce0dab7abcad59d96f46da74d3f4c18823aa3068d68ec2dbd5b414936a4a8"}
for sized in (False, True):
    L = Lock(H, xonly(CK), xonly(RK), 1_794_000_000, sized)
    check(L.script().hex() == V1_SCRIPT[sized], f"the lock's script is V1.0's (sized={sized})")

lock = Lock(H, xonly(CK), xonly(RK), 1_794_000_000, True)
# ---- the lock send: the dollars to the lock, the change, the reserve to the lock, the DGB change, the record
owner = xonly(RK)
held = [{"txid": "aa" * 32, "vout": 0, "cents": 5000}]
coins = [{"txid": "bb" * 32, "vout": 1, "value": 200_000_000}]
pay = bytes.fromhex("76a914" + "11" * 20 + "88ac")
prep = lock_send("dgbtest", owner, held, coins, pay, lock, 2500)
outs = [(v, bytes.fromhex(s)) for v, s in prep["outputs"]]
check(outs[0] == (0, lock.script()), "output 0: the dollars to the lock")
check(outs[1] == (0, tr.p2tr(tr.tweak(owner))), "output 1: the dollar change back")
check(outs[2] == (RESERVE, lock.script()), "output 2: the 0.5 DGB reserve to the lock")
check(outs[3][1] == pay and outs[-1][1][0] == 0x6A, "then the DGB change and the record")
rec = tr.pushes(outs[-1][1])
check([tr.read_num(x) for x in rec[3:]] == [2500, 2500], f"the record: $25 to the lock, $25 back ({rec})")

# ---- a claim: the dollars to the claimer's DigiDollar address, signed by the claim leaf, the secret in the witness
to = tr.p2tr(tr.tweak(xonly(CK)))
u = spend(lock, {"txid": "cc" * 32, "vout": 0}, 2500, {"txid": "cc" * 32, "vout": 2, "value": RESERVE}, to, pay, secret)
check(len(u.inputs) == 2 and u.locktime == 0 and u.inputs[0]["taproot"]["extra"] == [secret.hex()], "a claim: two inputs, no lock time, the secret")
sigs = [sign(CK, bytes.fromhex(h)).hex() for h in u.sighashes]
check(all(tap.verify(lock.claimer, bytes.fromhex(h), bytes.fromhex(sg)) for h, sg in zip(u.sighashes, sigs)), "the claimer's signatures verify")
raw = fundinglib.assemble(u, sigs, b"\x02" + xonly(CK))
check(secret.hex() in raw and lock.claim_leaf().hex() in raw and lock.control(True).hex() in raw,
      "the claim's witness: signature, secret, leaf, control")
try:
    spend(lock, {"txid": "cc" * 32, "vout": 0}, 2500, {"txid": "cc" * 32, "vout": 2, "value": RESERVE}, to, pay, b"\x06" * 32)
    check(False, "a wrong secret is refused")
except ValueError:
    check(True, "a wrong secret is refused")
# ---- a refund: the lock's time, a non-final sequence, the refund leaf
r = spend(lock, {"txid": "cc" * 32, "vout": 0}, 2500, {"txid": "cc" * 32, "vout": 2, "value": RESERVE}, to, pay, None)
check(r.locktime == 1_794_000_000 and r.inputs[0]["sequence"] == 0xFFFFFFFE and "extra" not in r.inputs[0]["taproot"],
      "a refund: the lock's time, non-final, no secret")
check(all(tap.verify(lock.refunder, bytes.fromhex(h), sign(RK, bytes.fromhex(h))) for h in r.sighashes), "the refunder's signatures verify")

print("\nALL OK" if not fails else f"\n{len(fails)} FAILED")
sys.exit(1 if fails else 0)
