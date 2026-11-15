"""Digi-ID sign-in, against a V1.0 arcade on this computer (nothing is sent, nothing goes on chain): a made-up DigiByte
key plays the Digi-ID wallet. Turn Digi-ID on for a seat on one device, sign in with it from a fresh browser on that
device, and check what is refused: a wrong signature, another key, a reused code, another device, a device turned off.
    ARCADE_SRC=... python tests/digiid_test.py <v1 url> <v1 home>"""
import base64, hashlib, json, os, secrets, sys, tempfile
sys.path.insert(0, os.environ.get("ARCADE_SRC", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "arcade-src")))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fastapi.testclient import TestClient
from nacl.signing import SigningKey
from arcade import accounts
from arcade import llm as ec
from arcade.script import b58check_encode
from arcadesite import digiid
from arcadesite.app import create_app

fails = []
def check(ok, m): print(("ok   " if ok else "FAIL ") + m); ok or fails.append(m)

from digiid_wallet import Wallet


home = tempfile.mkdtemp(prefix="site-digiid-")
json.dump({"v1": sys.argv[1], "v1_home": sys.argv[2], "public_hosts": ["testserver"]}, open(home + "/site.json", "w"))
json.dump({"digiid": "on", "faucet": 0}, open(home + "/settings.json", "w"))
app = create_app(home)
c = TestClient(app)

# --- the signature check on its own
w = Wallet()
uri = "digiid://testserver/digiid/callback?x=00ff&u=1"
check(digiid.signed(uri, w.address, w.sign(uri)), "a Digi-ID signature checks out (DigiByte prefix)")
check(digiid.signed(uri, w.address, w.sign(uri, "Bitcoin Signed Message:\n")), "and under BitID's Bitcoin prefix")
check(not digiid.signed(uri + "1", w.address, w.sign(uri)), "a signature of another URI does not")
check(not digiid.signed(uri, Wallet().address, w.sign(uri)), "nor another address")

check(c.get("/digiid/status").json().get("on") is True, "Digi-ID is on (the site setting)")

# --- a seat, signed in with its own key (as the password sign-in ends)
seat = SigningKey.generate(); pubkey = seat.verify_key.encode().hex()
ch = c.get("/auth/challenge").json()
r = c.post("/auth/login", json={"pubkey": pubkey, "nonce": ch["nonce"], "origin": "http://testserver", "join": True,
                                "signature": seat.sign(accounts.login_message("http://testserver", ch["nonce"])).signature.hex()})
check(r.status_code == 200, f"signed in: {r.status_code} {r.text[:100]}")

device = secrets.token_hex(16)
other_device = secrets.token_hex(16)

def scan(client, purpose, dev, wallet=w, mangle=False):
    s = client.post("/digiid/start", json={"purpose": purpose, "device": dev}).json()
    sig = wallet.sign(s["uri"] + ("x" if mangle else ""))
    cb = client.post("/digiid/callback", json={"address": wallet.address, "uri": s["uri"], "signature": sig})
    return s, cb

r = TestClient(app).post("/digiid/start", json={"purpose": "link", "device": device})
check(r.status_code == 403, "turning Digi-ID on needs a signed-in browser")

s, cb = scan(c, "link", device, mangle=True)
check(cb.status_code == 400, f"a bad signature is refused by the callback: {cb.json().get('detail')}")
check(c.post("/digiid/poll", json={"nonce": s["nonce"], "poll": s["poll"]}).json()["state"] == "waiting",
      "and the code is still waiting")
check(s["uri"].startswith("digiid://testserver/digiid/callback?x=") and s["uri"].endswith("&u=1"),
      f"the URI names this host, plain http marked u=1: {s['uri']}")

s, cb = scan(c, "link", device)
check(cb.status_code == 200, f"the wallet's scan is accepted: {cb.text[:80]}")
check(c.post("/digiid/poll", json={"nonce": s["nonce"], "poll": "00" * 16}).json()["state"] == "expired",
      "polling with the wrong poll token gets nothing")
p = c.post("/digiid/poll", json={"nonce": s["nonce"], "poll": s["poll"]}).json()
check(p.get("state") == "done" and len(p.get("half", "")) == 64 and p.get("address") == w.address,
      f"linked: this device gets its unlock half ({p.get('state')})")
half = p.get("half")
again = c.post("/digiid/callback", json={"address": w.address, "uri": s["uri"], "signature": w.sign(s["uri"])})
check(again.status_code == 400, "the same code can't be used twice")
st = c.get(f"/digiid/status?device={device}").json()
check(st.get("linked") == [w.address] and st.get("devices") == 1 and st.get("this_device") is True,
      f"status shows the link and this device: {st}")

# --- signing in with Digi-ID, from a fresh browser on the same device
fresh = TestClient(app)
s, cb = scan(fresh, "login", device)
p = fresh.post("/digiid/poll", json={"nonce": s["nonce"], "poll": s["poll"]}).json()
check(p.get("state") == "done" and p.get("half") == half and p.get("pubkey") == pubkey,
      "Digi-ID sign-in on this device hands back the same half and the account's key")

s, cb = scan(TestClient(app), "login", other_device)
p = c.post("/digiid/poll", json={"nonce": s["nonce"], "poll": s["poll"]}).json()
check(p.get("state") == "unknown" and "half" not in p, "another device gets nothing (it keeps no sealed copy)")

stranger = Wallet()
s, cb = scan(TestClient(app), "login", device, wallet=stranger)
p = c.post("/digiid/poll", json={"nonce": s["nonce"], "poll": s["poll"]}).json()
check(p.get("state") == "unknown", "another Digi-ID on this device gets nothing")

seat2 = SigningKey.generate(); c2 = TestClient(app)
ch = c2.get("/auth/challenge").json()
c2.post("/auth/login", json={"pubkey": seat2.verify_key.encode().hex(), "nonce": ch["nonce"], "origin": "http://testserver",
                             "join": True, "signature": seat2.sign(accounts.login_message("http://testserver", ch["nonce"])).signature.hex()})
s, cb = scan(c2, "link", secrets.token_hex(16))
p = c2.post("/digiid/poll", json={"nonce": s["nonce"], "poll": s["poll"]})
check(p.status_code == 409, f"a Digi-ID linked to one account can't be linked to another: {p.json().get('detail')}")

# --- turning it off
r = c.post("/digiid/off", json={"device": device})
check(r.json().get("forgotten") == 1, "turned off on this device")
s, cb = scan(TestClient(app), "login", device)
p = c.post("/digiid/poll", json={"nonce": s["nonce"], "poll": s["poll"]}).json()
check(p.get("state") == "unknown", "and Digi-ID no longer signs in there")
check(c.get("/digiid/status").json().get("linked") == [], "the link went with its last device")

json.dump({"digiid": "off"}, open(home + "/settings.json", "w"))
check(c.post("/digiid/start", json={"purpose": "login", "device": device}).status_code == 404,
      "with Digi-ID off, no codes are made")

# --- the node module switch: no setting, so the arcade's installed apps decide
json.dump({}, open(home + "/settings.json", "w"))
from arcadesite import v1 as v1mod
real = v1mod.V1.cmd
def fake(apps):
    def cmd(self, command, _timeout=None, **params):
        return apps if command == "apps.installed" else real(self, command, _timeout, **params)
    return cmd
for apps, want, what in (
        ([{"card": {"name": "Digi-ID", "category": "node-modules"}}], True, "the Digi-ID node module installed"),
        ([{"card": {"name": "Digi-ID", "category": "apps"}}], False, "an app named Digi-ID that isn't a node module"),
        ([{"card": {"name": "ArcadeCore", "category": "themes"}}], False, "no Digi-ID module")):
    v1mod.V1.cmd = fake(apps)
    on = TestClient(create_app(home)).get("/digiid/status").json().get("on")
    check(on is want, f"{what}: Digi-ID {'on' if want else 'off'}")
v1mod.V1.cmd = real

print("\n" + ("ALL PASSED" if not fails else f"{len(fails)} FAILED"))
sys.exit(1 if fails else 0)
