"""The Wallet card's SegWit addresses (arcadesite/wallet.py) and paying SegWit / Taproot, on Bitcoin, against a stand-in
V1.0: a Native SegWit (bc1q) and a P2SH-nested (3...) address are filed with their key's proof, show their balance, and
a send is built from their own coins with change back to them; legacy, bc1q and bc1p addresses are paid, a later witness
version is refused.
    python tests/segwit_test.py"""
import os, sys, secrets
sys.path.insert(0, os.environ.get("ARCADE_SRC", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "arcade-src")))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fastapi import FastAPI
from fastapi.testclient import TestClient
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils
from arcade import wallet_imports
from arcade.config import NETWORKS
from arcade.script import b58check_encode, hash160, segwit_encode
from arcade.web import account as accountlib
from arcadesite import wallet
from arcadesite.addresses import _check_address
from arcadesite.chains import Chain, Chains
from arcadesite.v1 import V1Error

fails = []
def check(ok, m): print(("ok   " if ok else "FAIL ") + m); ok or fails.append(m)

N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141

def key():
    priv = ec.derive_private_key(int.from_bytes(secrets.token_bytes(32), "big") % (2**255) + 1, ec.SECP256K1())
    pub = priv.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.CompressedPoint)
    return priv, pub

def sign(priv, digest: bytes) -> str:
    r, s = utils.decode_dss_signature(priv.sign(digest, ec.ECDSA(utils.Prehashed(hashes.SHA256()))))
    return utils.encode_dss_signature(r, min(s, N - s)).hex()

P = NETWORKS["btc"]
ACCOUNT = "ab" * 32
_, legacy_pub = key()
legacy = b58check_encode(P.pubkeyhash_version, hash160(legacy_pub))
seg_priv, seg_pub = key()
native = segwit_encode("bc", 0, hash160(seg_pub))
nest_priv, nest_pub = key()
nested = wallet_imports.address_of(P, nest_pub, "p2sh-p2wpkh")
COINS = {legacy: [("aa" * 32, 0, 50_000)], native: [("bb" * 32, 1, 200_000)], nested: [("cc" * 32, 2, 300_000)]}

class FakeV1:
    def cmd(self, name, **kw):
        if name == "chains.list":
            return [{"key": "peptest", "installed": True, "indexes": ["addresses"]},
                    {"key": "btc", "installed": False, "borrowed": True, "indexes": []}]
        if name == "tx.coins":
            a = kw["addresses"][0]
            return {"addresses": {a: {"confirmed": [{"txid": t, "vout": v, "value": val, "height": 977000}
                                                     for t, v, val in COINS.get(a, [])]}}}
        raise V1Error(f"the stand-in has no {name}")

class Accounts:
    def charge(self, *a, **k): pass

class St:
    def __init__(self):
        self.v1 = FakeV1(); self.kv = {}
    def setting(self, k, d=None): return self.kv.get(k, d)
    def set_setting(self, k, v): self.kv[k] = v
    def settings(self): return dict(self.kv)
    def accounts(self): return Accounts()

class Account:
    pubkey = ACCOUNT

st = St()
chains = Chains(st, st.v1)
btc = Chain("btc", P)
st.kv[chains.address_key(ACCOUNT, btc)] = legacy
app = FastAPI()
app.state.signed_in = lambda request: Account()
app.state.signed_in_account = lambda request: Account()
wallet.register(app, st, chains)
c = TestClient(app)

check([x.network for x in chains.account_chains()] == ["test", "btc"], "Bitcoin is listed where V1.0 borrows it")

# paying: legacy, P2SH, bc1q (P2WPKH and P2WSH), bc1p; not a later version or another chain's
tap = segwit_encode("bc", 1, bytes(range(32)))
wsh = segwit_encode("bc", 0, bytes(range(32)))
v2 = segwit_encode("bc", 2, bytes(range(32)))
for a, ok, what in [(legacy, True, "legacy 1..."), (b58check_encode(5, bytes(20)), True, "P2SH 3..."),
                    (native, True, "bc1q P2WPKH"), (wsh, True, "bc1q P2WSH"), (tap, True, "bc1p Taproot"),
                    (v2, False, "a witness v2 address"), (segwit_encode("ltc", 0, bytes(20)), False, "a Litecoin ltc1q")]:
    said = _check_address(a, mainnet=btc, segwit_ok=True)
    check((said is None) == ok, f"{what}: {'paid' if ok else 'refused'} ({said})")

# filing the SegWit addresses: proof needed, one per kind, never another account's
def proof(priv, pub, addr):
    return sign(priv, wallet_imports.proof_digest(ACCOUNT, "btc", addr, pub.hex()))

r = c.post("/account/segwit/register", json={"chain": "btc", "address": native, "pubkey": seg_pub.hex(),
                                              "address_signature": "30" + "00" * 8})
check(r.status_code == 400 and "not proved" in r.json()["detail"], f"a bad proof is refused: {r.json()}")
r = c.post("/account/segwit/register", json={"chain": "btc", "address": native, "pubkey": seg_pub.hex(),
                                              "address_signature": proof(seg_priv, seg_pub, native)})
check(r.status_code == 200 and r.json()["kind"] == "p2wpkh", f"the bc1q address is filed: {r.json()}")
r = c.post("/account/segwit/register", json={"chain": "btc", "address": nested, "pubkey": nest_pub.hex(),
                                              "address_signature": proof(nest_priv, nest_pub, nested)})
check(r.status_code == 200 and r.json()["kind"] == "p2sh-p2wpkh", f"the nested address is filed: {r.json()}")
check(st.kv.get(f"segwit_keyhash:{ACCOUNT}:btc") == hash160(nest_pub).hex(), "its key hash is kept for sends")
r = c.post("/account/segwit/register", json={"chain": "btc", "address": native})
check(r.status_code == 200, "filing it again needs no proof")
other = segwit_encode("bc", 0, bytes(20))
r = c.post("/account/segwit/register", json={"chain": "btc", "address": other})
check(r.status_code == 409, "a second bc1q address for the same account is refused")

r = c.get(f"/account/segwit/balance?network=btc&address={native}")
check(r.status_code == 200 and r.json()["balance"] == 200_000, f"its balance: {r.json()}")
r = c.get(f"/account/segwit/balance?network=btc&address={other}")
check(r.status_code == 400, "not an address this account filed")
r = c.get(f"/account/coins?chain=btc&address={native}")
check(r.status_code == 200 and len(r.json()["coins"]) == 1, "its coins list for this account")

# sends from each kind spend that kind's own coins, change back to it
def send(frm, to, amount):
    return c.post("/account/send", json={"chain": "btc", "to": to, "amount": amount, "from": frm})

r = send(native, tap, "0.001")
off = r.json()
check(r.status_code == 200 and [(i["txid"], i["vout"]) for i in off["inputs"]] == [("bb" * 32, 1)],
      f"from bc1q: spends its own coin: {str(off)[:200]}")
if r.status_code == 200:
    check(("5120" + bytes(range(32)).hex()) in off["raw"], "pays the Taproot output OP_1 <key>")
    check(("0014" + hash160(seg_pub).hex()) in off["raw"], "change back to the bc1q address")
r = send(nested, legacy, "0.001")
off = r.json()
check(r.status_code == 200 and [(i["txid"], i["vout"]) for i in off["inputs"]] == [("cc" * 32, 2)],
      f"from the nested address: spends its own coin: {str(off)[:200]}")
r = send("", native, "0.0001")
off = r.json()
check(r.status_code == 200 and [(i["txid"], i["vout"]) for i in off["inputs"]] == [("aa" * 32, 0)],
      f"from the legacy address (the default), paying bc1q: {str(off)[:160]}")
r = send(other, legacy, "0.001")
check(r.status_code == 400, "not from an address the account did not file")
r = send("", v2, "0.001")
check(r.status_code == 400 and "version" in r.json()["detail"], f"a v2 payee is refused: {r.json()}")

print("PASS" if not fails else f"{len(fails)} FAILED")
sys.exit(1 if fails else 0)
