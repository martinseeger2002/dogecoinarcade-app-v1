"""Imported wallets, live against a V1.0 arcade on this computer (Pepecoin testnet, test coins only): sign up, import a
second key's address (its key proof), pay it from the account, see its balance in /account/imported (V1.0's address
index: ready at once, no rescan), its coins and activity, send from it signed by ITS key, forget it.
    ARCADE_SRC=... python tests/imported_test.py <v1 url> <v1 home>"""
import json, os, secrets, sys, tempfile, time
sys.path.insert(0, os.environ.get("ARCADE_SRC", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "arcade-src")))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fastapi.testclient import TestClient
from nacl.signing import SigningKey
from arcade import accounts, wallet_imports
from arcade.config import NETWORKS
from arcade.script import b58check_encode, hash160
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils
from arcadesite.app import create_app

ORDER = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
fails = []
def check(ok, m): print(("ok   " if ok else "FAIL ") + m); ok or fails.append(m)
def coin_sign(priv, digest):
    r, s = utils.decode_dss_signature(priv.sign(digest, ec.ECDSA(utils.Prehashed(hashes.SHA256()))))
    return utils.encode_dss_signature(r, min(s, ORDER - s))
def new_key():
    priv = ec.derive_private_key(int.from_bytes(secrets.token_bytes(32), "big") % (ORDER - 1) + 1, ec.SECP256K1())
    pub = priv.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.CompressedPoint)
    return priv, pub, b58check_encode(NETWORKS["test"].pubkeyhash_version, hash160(pub))

home = tempfile.mkdtemp(prefix="site-imported-")
json.dump({"v1": sys.argv[1], "v1_home": sys.argv[2], "public_hosts": ["testserver"]}, open(home + "/site.json", "w"))
json.dump({"faucet": 100000000}, open(home + "/settings.json", "w"))          # 1 test PEP for this test
c = TestClient(create_app(home))

key = SigningKey.generate(); pubkey = key.verify_key.encode().hex()
priv, pub, address = new_key()
tag = "imptest" + secrets.token_hex(3)
ch = c.get("/auth/challenge").json()
body = {"tag": tag, "pubkey": pubkey, "address": address, "coin_pubkey": pub.hex(),
        "blob": {"sealed": "dGVzdA", "iterations": 600000}, "nonce": ch["nonce"], "origin": "http://testserver",
        "signature": key.sign(accounts.login_message("http://testserver", ch["nonce"])).signature.hex(),
        "address_signature": coin_sign(priv, wallet_imports.proof_digest(pubkey, "test", address, pub.hex())).hex()}
r = c.post("/signup", json=body)
check(r.status_code == 200, f"signed up @{tag}: {r.status_code} {r.text[:120]}")
for i in range(20):
    if (c.get("/account").json().get("spendable") or 0) >= 100000000: break
    time.sleep(1)

# --- import a second key's address
ipriv, ipub, iaddr = new_key()
proof = coin_sign(ipriv, wallet_imports.proof_digest(pubkey, "test", iaddr, ipub.hex())).hex()
r = c.post("/account/imported/address", json={"chain": "test", "address": iaddr, "kind": "p2pkh",
                                              "pubkey": ipub.hex(), "signature": "00" + proof[2:]})
check(r.status_code == 400, f"an import with a bad signature is refused ({r.json().get('detail')})")
r = c.post("/account/imported/address", json={"chain": "test", "address": iaddr, "kind": "p2pkh",
                                              "pubkey": ipub.hex(), "signature": proof})
check(r.status_code == 200 and r.json().get("state") == "ready", f"imported, ready at once: {r.text[:140]}")
r = c.post("/account/imported/address", json={"chain": "test", "address": iaddr, "kind": "p2pkh",
                                              "pubkey": ipub.hex(), "signature": proof})
check(r.json().get("already") is True, "importing it again says already")
check(c.get("/account/imported/busy?chain=test").json().get("busy") is False, "never busy")

def sign(off, p, pk):
    sigs = [(coin_sign(p, bytes.fromhex(h)) + b"\x01").hex() for h in off["sighashes"]]
    return c.post("/account/sign", json={"offer": off["offer"], "signatures": sigs, "pubkey": pk.hex()})

# --- pay it from the account
r = c.post("/account/send", json={"chain": "test", "to": iaddr, "amount": "0.4"}); off = r.json()
check(r.status_code == 200 and off.get("offer"), f"send 0.4 to the imported wallet: an offer {str(off)[:100]}")
r = sign(off, priv, pub); check(r.status_code == 200, f"broadcast: {r.text[:120]}")
row = {}
for i in range(30):
    rows = c.get("/account/imported").json().get("chains", {}).get("test") or []
    row = next((x for x in rows if x["address"] == iaddr), {})
    if (row.get("spendable") or 0) >= 40000000: break
    time.sleep(2)
check(row.get("spendable") == 40000000 and row.get("state") == "ready", f"its balance in /account/imported: {row}")
lazy = c.get("/account/imported?lazy=1").json()["chains"]["test"]
check(lazy and lazy[0].get("lazy") is True and lazy[0].get("spendable") is None, "?lazy=1 reads none")
opened = c.get(f"/account/imported?lazy=1&chain=test&address={iaddr}").json()["chains"]["test"]
check(opened[0].get("spendable") == 40000000, "?lazy=1 with it open reads it")
coins = c.get(f"/account/coins?chain=test&address={iaddr}").json().get("coins") or []
check(sum(x["value"] for x in coins) == 40000000, f"/account/coins for it: {len(coins)} coin(s)")
act = c.get(f"/account/activity?chain=test&address={iaddr}").json()
check(act.get("address") == iaddr and (act.get("rows") or act.get("waiting")), f"its activity: {str(act)[:140]}")
other = new_key()[2]
check(c.get(f"/account/coins?chain=test&address={other}").status_code == 403, "somebody else's address: refused")
check(c.post("/account/send", json={"chain": "test", "to": address, "amount": "0.1", "from": other}).status_code == 400,
      "a send from somebody else's address: refused")

# --- send from it, signed by its own key
r = c.post("/account/send", json={"chain": "test", "to": address, "amount": "0.2", "from": iaddr}); off = r.json()
check(r.status_code == 200 and off.get("offer"), f"send from the imported wallet: an offer {str(off)[:100]}")
r = sign(off, ipriv, ipub); done = r.json()
check(r.status_code == 200 and len(done.get("txid") or "") == 64, f"signed by its key and broadcast: {str(done)[:120]}")

# --- forget it
r = c.post("/account/imported/forget", json={"chain": "test", "address": iaddr})
check(r.json().get("removed") is True, "forgotten")
check(not (c.get("/account/imported").json()["chains"]["test"]), "no longer listed")
print("ALL OK" if not fails else "%d FAILED" % len(fails))
