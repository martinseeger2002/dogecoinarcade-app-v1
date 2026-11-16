"""A made-up Digi-ID wallet for the tests: a DigiByte key that signs a Digi-ID URI as a Core-style message."""
import base64, os, secrets, sys
sys.path.insert(0, os.environ.get("ARCADE_SRC", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "arcade-src")))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from arcade import llm as ec
from arcade.script import b58check_encode
from arcadesite.hash160 import hash160              # RIPEMD-160 even where OpenSSL leaves it out

DGB_P2PKH = 0x1E            # DigiByte addresses start with D


class Wallet:
    """A Digi-ID wallet: a DigiByte key that signs a Digi-ID URI as a Core-style message."""
    def __init__(self, version=None):
        self.d = int.from_bytes(secrets.token_bytes(32), "big") % (ec._N - 1) + 1
        q = ec._mul(self.d, ec._G)
        self.pub = bytes([2 + (q[1] & 1)]) + q[0].to_bytes(32, "big")
        self.address = b58check_encode(DGB_P2PKH if version is None else version, hash160(self.pub))

    def sign(self, message, magic="DigiByte Signed Message:\n"):
        e = int.from_bytes(ec.message_hash(message, magic), "big")
        while True:
            k = int.from_bytes(secrets.token_bytes(32), "big") % (ec._N - 1) + 1
            R = ec._mul(k, ec._G)
            r = R[0] % ec._N
            s = pow(k, -1, ec._N) * (e + r * self.d) % ec._N
            if r and s:
                break
        recid = (R[1] & 1) | (2 if R[0] >= ec._N else 0)
        if s > ec._N // 2:                     # low s, as wallets sign: the other y
            s, recid = ec._N - s, recid ^ 1
        return base64.b64encode(bytes([27 + 4 + recid]) + r.to_bytes(32, "big") + s.to_bytes(32, "big")).decode()


