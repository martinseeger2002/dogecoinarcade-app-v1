"""The keys an arcade account's 12 words make, as the T-Deck must make them.

This is the Python reference for the firmware's C code; both must agree with
the arcade's browser (signin.js, coins.js, messaging_js.js) byte for byte.

- seed:       BIP39, PBKDF2-HMAC-SHA512, 2048 rounds, salt "mnemonic"
- login key:  Ed25519 seed at the arcade branch 24946'/1'/0'  (account id)
- mail key:   X25519 secret at 24946'/0'/0'
- coin key:   secp256k1, real BIP32 m/44'/<coin type>'/0'/0/0

The arcade branch is NOT real BIP32: every step keeps IL as the key and IR as
the chain code, with no addition to the parent key.
"""
from __future__ import annotations

import hashlib
import hmac
import struct
import unicodedata

import coincurve
import nacl.bindings
import nacl.signing

H = 0x80000000
LOGIN_BRANCH = (24946 | H, 1 | H, 0 | H)
MAIL_BRANCH = (24946 | H, 0 | H, 0 | H)
# coins.js COIN_TYPE: anything not listed is 1
COIN_TYPE = {"main": 3, "test": 1, "regtest": 1, "doge": 3, "ltc": 3, "bel": 3, "lky": 3, "kot": 3, "wjk": 3}
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141


def normalise(phrase: str) -> str:
    return " ".join(unicodedata.normalize("NFKD", phrase).lower().split())


def seed(phrase: str, passphrase: str = "") -> bytes:
    return hashlib.pbkdf2_hmac("sha512", normalise(phrase).encode(),
                               ("mnemonic" + passphrase).encode(), 2048, 64)


def _master(seed_bytes: bytes) -> tuple[bytes, bytes]:
    i = hmac.new(b"Bitcoin seed", seed_bytes, hashlib.sha512).digest()
    return i[:32], i[32:]


def arcade_branch(seed_bytes: bytes, path) -> bytes:
    key, chain = _master(seed_bytes)
    for idx in path:
        i = hmac.new(chain, b"\x00" + key + struct.pack(">I", idx), hashlib.sha512).digest()
        key, chain = i[:32], i[32:]
    return key


def bip32(seed_bytes: bytes, path) -> bytes:
    key, chain = _master(seed_bytes)
    for idx in path:
        if idx & H:
            data = b"\x00" + key + struct.pack(">I", idx)
        else:
            data = coincurve.PrivateKey(key).public_key.format(True) + struct.pack(">I", idx)
        i = hmac.new(chain, data, hashlib.sha512).digest()
        k = (int.from_bytes(i[:32], "big") + int.from_bytes(key, "big")) % N
        key, chain = k.to_bytes(32, "big"), i[32:]
    return key


def hash160(b: bytes) -> bytes:
    return hashlib.new("ripemd160", hashlib.sha256(b).digest()).digest()


B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58check(version: int, payload: bytes) -> str:
    raw = bytes([version]) + payload
    raw += hashlib.sha256(hashlib.sha256(raw).digest()).digest()[:4]
    n = int.from_bytes(raw, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = B58[r] + out
    return "1" * (len(raw) - len(raw.lstrip(b"\0"))) + out


def b58decode_check(s: str) -> tuple[int, bytes]:
    n = 0
    for c in s:
        n = n * 58 + B58.index(c)
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    raw = b"\0" * (len(s) - len(s.lstrip("1"))) + raw
    body, check = raw[:-4], raw[-4:]
    if hashlib.sha256(hashlib.sha256(body).digest()).digest()[:4] != check:
        raise ValueError("bad address checksum")
    return body[0], body[1:]


class Account:
    """Everything one set of words gives a T-Deck."""

    def __init__(self, phrase: str):
        s = seed(phrase)
        self.login = nacl.signing.SigningKey(arcade_branch(s, LOGIN_BRANCH))
        self.login_pubkey = bytes(self.login.verify_key)
        self.mail_secret = arcade_branch(s, MAIL_BRANCH)
        self.mail_pubkey = nacl.bindings.crypto_scalarmult_base(self.mail_secret)
        self._seed = s
        self._coin = {}

    def coin_key(self, network: str) -> coincurve.PrivateKey:
        ct = COIN_TYPE.get(network, 1)
        if ct not in self._coin:
            self._coin[ct] = coincurve.PrivateKey(bip32(self._seed, (44 | H, ct | H, 0 | H, 0, 0)))
        return self._coin[ct]

    def coin_pubkey(self, network: str) -> bytes:
        return self.coin_key(network).public_key.format(True)

    def address(self, network: str, version: int) -> str:
        return b58check(version, hash160(self.coin_pubkey(network)))

    def sign_login(self, origin: str, nonce: str) -> bytes:
        msg = b"DogecoinArcade login\n" + origin.encode() + b"\n" + nonce.encode()
        return self.login.sign(msg).signature

    def sign_digest(self, network: str, digest: bytes) -> bytes:
        """DER, low-S, RFC 6979 (coincurve), plus SIGHASH_ALL."""
        return self.coin_key(network).sign(digest, hasher=None) + b"\x01"
