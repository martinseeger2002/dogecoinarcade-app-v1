"""Doginals, DRC-20 and the inscription card for an imported wallet, live against a V1.0 arcade on this computer.
A throwaway site home and account; the imported wallet is a real address (read only: nothing is sent), written into the
throwaway home's settings as the account's import.
    ARCADE_SRC=... python tests/doginals_test.py <v1 url> <v1 home> <network> <address> [<other network> <address> ...]"""
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

home = tempfile.mkdtemp(prefix="site-doginals-")
json.dump({"v1": sys.argv[1], "v1_home": sys.argv[2], "public_hosts": ["testserver"]}, open(home + "/site.json", "w"))
json.dump({}, open(home + "/settings.json", "w"))
c = TestClient(create_app(home))
key = SigningKey.generate(); pubkey = key.verify_key.encode().hex()
priv = ec.derive_private_key(int.from_bytes(secrets.token_bytes(32), "big") % (ORDER - 1) + 1, ec.SECP256K1())
pub = priv.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.CompressedPoint)
address = b58check_encode(NETWORKS["test"].pubkeyhash_version, hash160(pub))
tag = "dgtest" + secrets.token_hex(3)
ch = c.get("/auth/challenge").json()
r = c.post("/signup", json={"tag": tag, "pubkey": pubkey, "address": address, "coin_pubkey": pub.hex(),
                            "blob": {"sealed": "dGVzdA", "iterations": 600000}, "nonce": ch["nonce"], "origin": "http://testserver",
                            "signature": key.sign(accounts.login_message("http://testserver", ch["nonce"])).signature.hex(),
                            "address_signature": coin_sign(priv, wallet_imports.proof_digest(pubkey, "test", address, pub.hex())).hex()})
check(r.status_code == 200, f"signed up @{tag}")
pairs = list(zip(sys.argv[3::2], sys.argv[4::2]))
s = json.load(open(home + "/settings.json"))
for net, a in pairs:
    s[wallet_imports.setting_key(net, pubkey)] = [{"a": a, "k": "p2pkh", "p": "", "t": int(time.time())}]
json.dump(s, open(home + "/settings.json", "w"))

for net, a in pairs:
    t0 = time.time()
    r = c.get(f"/account/doginals/page?chain={net}&address={a}&size=30")
    got = r.json()
    check(r.status_code == 200, f"{net} doginals page: {r.status_code} in {time.time() - t0:.1f}s {str(got)[:120] if r.status_code != 200 else ''}")
    if r.status_code == 200:
        items = got.get("items") or []
        print(f"     counts {got.get('counts')} total {got.get('total')} pages {got.get('pages')} unread {got.get('unread')}"
              f" collections {[(x['name'], x['n']) for x in got.get('collections') or []][:5]}")
        print(f"     note: {got.get('error')}")
        check(all(g.get("id") and g.get("number") is not None for g in items), f"{net}: every item has an id and a number")
        if items:
            g = items[0]
            r = c.get(f"/account/doginal-card?chain={net}&id={g['id']}")
            card = r.json()
            check(r.status_code == 200 and card["inscription"].get("number") == g["number"],
                  f"{net} card #{g['number']}: {str(card)[:160]}")
            r = c.get(card.get("content") or "/x")
            check(r.status_code == 200, f"{net} card content loads ({r.headers.get('content-type')}, {len(r.content)} B)")
    if net in ("doge", "main"):
        r = c.get(f"/account/token20?chain={net}&address={a}")
        got = r.json()
        bal = got.get("balances") or []
        check(r.status_code == 200 and isinstance(got.get("balances"), list), f"{net} token20: {len(bal)} balances")
        print("     " + ", ".join(f"{b['tick']}={int(b['units']) // 10 ** b['decimals']:,}" for b in bal[:8]))
other = b58check_encode(NETWORKS["test"].pubkeyhash_version, hash160(b"\x02" + b"\x01" * 32))
check(c.get(f"/account/token20?chain=doge&address={other}").status_code in (403, 404), "somebody else's address: refused")
page = c.get("/me/wallet")
check(page.status_code == 200 and "drawToken20(t20, c20)" in page.text, "the wallet page carries the imported-wallet Tokens tab")
print("ALL OK" if not fails else "%d FAILED" % len(fails))
