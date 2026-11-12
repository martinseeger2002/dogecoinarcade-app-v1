"""Step 1, live against a V1.0 arcade on this computer (Pepecoin testnet, test coins only):
sign up (both proofs, welcome coins from V1.0's wallet), who, sign out/in, the sealed wallet, /account, a send to
itself signed by the coin key and broadcast through V1.0.   ARCADE_SRC=... python tests/live_step1.py <v1 url> <v1 home>"""
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

home = tempfile.mkdtemp(prefix="site-")
json.dump({"v1": sys.argv[1], "v1_home": sys.argv[2], "public_hosts": ["testserver"]}, open(home + "/site.json", "w"))
json.dump({"faucet": 100000000}, open(home + "/settings.json", "w"))          # 1 test PEP for this test
c = TestClient(create_app(home))
check(c.get("/healthz").json().get("ok"), "the site reaches V1.0")
check(c.get("/auth/who").json()["pubkey"] is None, "nobody signed in yet")

key = SigningKey.generate(); pubkey = key.verify_key.encode().hex()
priv = ec.derive_private_key(int.from_bytes(secrets.token_bytes(32), "big") % (ORDER - 1) + 1, ec.SECP256K1())
pub = priv.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.CompressedPoint)
address = b58check_encode(NETWORKS["test"].pubkeyhash_version, hash160(pub))
tag = "sitetest" + secrets.token_hex(3)
free = c.get(f"/signup/{tag}").json()
check(free.get("free") is True, f"/signup/{tag}: free ({free.get('detail') or 'here, chain, elsewhere'})")
check(c.get("/signup/ashvale").json().get("on_chain") is True, "/signup/ashvale: claimed on the chain (V1.0)")
ch = c.get("/auth/challenge").json()
body = {"tag": tag, "pubkey": pubkey, "address": address, "coin_pubkey": pub.hex(),
        "blob": {"sealed": "dGVzdA", "iterations": 600000}, "nonce": ch["nonce"], "origin": "http://testserver",
        "signature": key.sign(accounts.login_message("http://testserver", ch["nonce"])).signature.hex(),
        "address_signature": coin_sign(priv, wallet_imports.proof_digest(pubkey, "test", address, pub.hex())).hex()}
r = c.post("/signup", json=body); out = r.json()
check(r.status_code == 200 and out.get("address") == address, f"POST /signup: {r.status_code} {str(out)[:160]}")
check(out.get("given") == 100000000, f"welcome coins: given {out.get('given')} ({out.get('no_coins') or 'paid'})")
check(c.get("/auth/who").json().get("pubkey") == pubkey, "signed in after sign-up (session cookie)")
check(c.get(f"/signin/{tag}").json().get("pubkey") == pubkey, "/signin/<tag>: the sealed wallet comes back")
bad = dict(body, tag=tag + "x", address_signature="00")
check(c.post("/signup", json=bad).status_code == 400, "a sign-up without the address proof is refused")
c.post("/auth/logout")
check(c.get("/auth/who").json()["pubkey"] is None, "signed out")
ch = c.get("/auth/challenge").json()
r = c.post("/auth/login", json={"pubkey": pubkey, "nonce": ch["nonce"], "origin": "http://testserver",
                                "signature": key.sign(accounts.login_message("http://testserver", ch["nonce"])).signature.hex()})
check(r.status_code == 200, f"signed in again with the seat key: {r.status_code}")
for i in range(20):
    st = c.get("/account").json()
    if (st.get("spendable") or 0) >= 100000000: break
    time.sleep(1)
check(st.get("address") == address and st.get("name") == tag, f"/account: address and name ({st.get('name')})")
check((st.get("spendable") or 0) >= 100000000, f"/account: the welcome coin is spendable ({st.get('spendable')})")
r = c.post("/account/send", json={"chain": "test", "to": address, "amount": "0.5"}); off = r.json()
check(r.status_code == 200 and off.get("offer"), f"/account/send to itself: an offer ({str(off)[:200]})")
if off.get("offer"):
    sigs = [(coin_sign(priv, bytes.fromhex(h)) + b"\x01").hex() for h in off["sighashes"]]   # DER + SIGHASH_ALL, as coins.js
    r = c.post("/account/sign", json={"offer": off["offer"], "signatures": sigs, "pubkey": pub.hex()}); done = r.json()
    check(r.status_code == 200 and len(done.get("txid") or "") == 64, f"/account/sign: broadcast {str(done)[:160]}")
    from arcade.rpc import RpcClient
    from arcade.config import RpcCredentials
    u, _, pw_ = open(os.path.expanduser("~/.pepecoin-testnet/testnet3/.cookie")).read().strip().partition(":")
    core = RpcClient(RpcCredentials("127.0.0.1", 44873, u, pw_), timeout=30)
    known = False
    for i in range(12):
        try: core.call("getrawtransaction", done["txid"], True); known = True; break
        except Exception: time.sleep(5)
    check(known, "the send reached the network (a Pepecoin Core node elsewhere knows it)")
    wrong = c.post("/account/sign", json={"offer": off["offer"], "signatures": sigs, "pubkey": pub.hex()})
    check(wrong.status_code == 400, "the same offer can't be signed twice")
def sign_offer(off):
    sigs = [(coin_sign(priv, bytes.fromhex(h)) + b"\x01").hex() for h in off["sighashes"]]
    return c.post("/account/sign", json={"offer": off["offer"], "signatures": sigs, "pubkey": pub.hex()})

# --- the name, then a post under it (the feed needs a claimed name)
r = c.post("/account/claim", json={}); off = r.json()
check(r.status_code == 200 and off.get("offer"), f"/account/claim: an offer ({str(off)[:120]})")
r = sign_offer(off); check(r.status_code == 200, f"claim signed and broadcast: {str(r.json())[:120]}")
r = c.post("/account/post", json={"text": "early post"})
check(r.status_code == 400 and "claim your name" in r.text, "a post before the name is in a block is refused")
for i in range(60):
    if c.get(f"/signup/{tag}").json().get("on_chain"): break
    time.sleep(5)
check(c.get(f"/signup/{tag}").json().get("on_chain"), f"@{tag} is on the chain (V1.0's tags index)")
text = f"hello from the new site, test {secrets.token_hex(2)}"
r = c.post("/account/post", json={"text": text}); off = r.json()
check(r.status_code == 200 and off.get("offer"), f"/account/post: an offer ({str(off)[:120]})")
r = sign_offer(off); done = r.json(); check(r.status_code == 200, f"post signed and broadcast: {str(done)[:120]}")
from arcadesite.v1 import V1
v = V1(sys.argv[1], sys.argv[2])
seen = False
for i in range(60):
    posts = (v.cmd("feed", order="new", limit=20) or {}).get("posts") or []
    if any(p.get("txid") == done.get("txid") for p in posts): seen = True; break
    time.sleep(5)
check(seen, "the post is in V1.0's feed")
page = c.get("/feed?sort=new").text
check(text in page, "and on the site's feed page")
print("ALL OK" if not fails else "%d FAILED" % len(fails))
