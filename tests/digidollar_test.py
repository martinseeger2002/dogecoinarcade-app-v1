"""DigiDollar in the Wallet (arcadesite/digidollar.py) against a stand-in V1.0 on DigiByte test: the chain list takes in
borrowed chains, /account/digidollar reads V1.0's dd.coins for the account's own key only, and a send is an unsigned
offer that spends the account's DigiDollar coins by their Taproot key path and its DGB for the fee, paying the payee,
the DigiDollar change and the "DD" record.
    python tests/digidollar_test.py"""
import os, sys, secrets
sys.path.insert(0, os.environ.get("ARCADE_SRC", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "arcade-src")))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fastapi import FastAPI
from fastapi.testclient import TestClient
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from arcade import digidollar as digidollib
from arcade.config import NETWORKS
from arcade.script import b58check_encode, hash160
from arcade.web import account as accountlib
from arcadesite import digidollar
from arcadesite.chains import Chains
from arcadesite.v1 import V1Error

fails = []
def check(ok, m): print(("ok   " if ok else "FAIL ") + m); ok or fails.append(m)

def key():
    priv = ec.derive_private_key(int.from_bytes(secrets.token_bytes(32), "big") % (2**255) + 1, ec.SECP256K1())
    return priv.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.CompressedPoint)

pub, other = key(), key()
P = NETWORKS["dgbtest"]
address = b58check_encode(P.pubkeyhash_version, hash160(pub))
dd_script = bytes([0x51, 0x20]) + digidollib.output_key(pub)
p2pkh = bytes([0x76, 0xa9, 0x14]) + hash160(pub) + bytes([0x88, 0xac])
DD = [("aa" * 32, 0, 1500), ("bb" * 32, 1, 700)]                 # $15 and $7
DGB = [("cc" * 32, 0, 300_000_000)]                               # 3 DGB for the fee

class FakeV1:
    asked = []
    def cmd(self, name, **kw):
        self.asked.append((name, kw))
        if name == "chains.list":
            return [{"key": "peptest", "installed": True, "indexes": ["addresses"]},
                    {"key": "dgb", "installed": True, "indexes": ["addresses"]},
                    {"key": "dgbtest", "installed": False, "borrowed": True, "indexes": [], "floor": 635000},
                    {"key": "bel", "installed": False, "borrowed": True, "indexes": []},
                    {"key": "btc", "installed": False, "borrowed": False}]
        if name == "dd.coins":
            if kw["pubkey"] != pub.hex():
                raise V1Error("not the stand-in's key")
            return {"chain": kw["chain"], "address": digidollib.address("dgbtest", pub), "borrowed": True, "whole": False,
                    "cents": 2200, "coins": [{"txid": t, "vout": v, "cents": c, "height": 635100} for t, v, c in DD]}
        if name == "tx.coins":
            return {"addresses": {kw["addresses"][0]: {"confirmed": [{"txid": t, "vout": v, "value": val, "height": 635100} for t, v, val in DGB]}}}
        raise V1Error(f"the stand-in has no {name}")
    def core(self, chain, method, *params):
        assert method == "gettxout", method
        txid, vout = params[0], params[1]
        for t, v, _ in DD:
            if (t, v) == (txid, vout):
                return {"value": 0, "scriptPubKey": {"hex": dd_script.hex()}}
        for t, v, val in DGB:
            if (t, v) == (txid, vout):
                return {"value": val / 1e8, "scriptPubKey": {"hex": p2pkh.hex()}}
        return None

class St:
    def __init__(self):
        self.v1 = FakeV1(); self.kv = {}
    def setting(self, k, d=None):
        return self.kv.get(k, d)

class Account:
    pubkey = "ab" * 32

st = St()
chains = Chains(st, st.v1)
st.kv[chains.address_key(Account.pubkey, chains.account_chains()[0].__class__("dgbtest", P))] = address
app = FastAPI()
app.state.signed_in_account = lambda request: Account()
app.state.offers = accountlib.Offers()
app.state.quota = lambda account, kind: None
digidollar.register(app, st, chains)
c = TestClient(app)

nets = [one.network for one in chains.account_chains()]
check(nets == ["test", "dgb", "bel", "dgbtest"], f"borrowed chains are listed (in the site's order), absent ones not: {nets}")
check(digidollar.nets(chains) == ["dgb", "dgbtest"], "DigiDollar on both DigiByte chains")

r = c.get(f"/account/digidollar?chain=dgbtest&pubkey={pub.hex()}").json()
check(r.get("cents") == 2200 and r["address"].startswith("TD") and r["from_block"] == 635000 and not r["whole"],
      f"the account's DigiDollars, history from the borrow floor: {str(r)[:160]}")
r = c.get(f"/account/digidollar?chain=dgbtest&pubkey={other.hex()}")
check(r.status_code == 400 and "not this account's" in r.json()["detail"], "another key is refused")
r = c.get("/account/digidollar?chain=doge&pubkey=" + pub.hex())
check(r.status_code == 400, "not on a chain without DigiDollar")

payee = digidollib.address("dgbtest", other)
r = c.post("/account/digidollar/send", json={"chain": "dgbtest", "pubkey": pub.hex(), "to": payee, "amount": "16.50"})
off = r.json()
check(r.status_code == 200 and off.get("offer"), f"an offer: {str(off)[:200]}")
if r.status_code == 200:
    ins = [(i["txid"], i["vout"]) for i in off["inputs"]]
    check(ins[:2] == [(DD[0][0], 0), (DD[1][0], 1)] and (DGB[0][0], 0) in ins, f"spends both DigiDollar coins and DGB: {ins}")
    check(sum(1 for i in off["inputs"] if i.get("taproot")) == 2, "the DigiDollar coins by their Taproot key path")
    raw = off["raw"]
    check((bytes([0x51, 0x20]) + digidollib.key_of("dgbtest", payee)).hex() in raw, "pays the payee's DigiDollar output")
    check("6a024444" in raw, "carries the DD record")
    check("send $16.50" in off["what"] and "$5.50 back" in off["what"], f"says what it does: {off['what']}")
    check(0 < off["fee"] <= digidollib.MAX_FEE, f"a fee within the cap: {off['fee']}")
    check(len(off.get("sighashes") or []) == len(off["inputs"]), "a hash to sign per input")
r = c.post("/account/digidollar/send", json={"chain": "dgbtest", "pubkey": pub.hex(), "to": payee, "amount": "30"})
check(r.status_code == 400 and "you hold" in r.json()["detail"], f"more than it holds: {r.json().get('detail')}")
r = c.post("/account/digidollar/send", json={"chain": "dgbtest", "pubkey": pub.hex(), "to": payee, "amount": "1.234"})
check(r.status_code == 400, "dollars and cents only")

print("PASS" if not fails else f"{len(fails)} FAILED")
sys.exit(1 if fails else 0)
