"""/account/doginal/send (arcadesite/ordsend.py): one inscription sent whole, offline, V1.0 stood in by the shapes of
crates/arcaded/src/sitesvc.rs (tx.coins) and explorer.rs (explorer.inscription, explorer.tx).

    .venv/bin/python tests/ordsend_test.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.environ.get("ARCADE_SRC", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "arcade-src")))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fastapi.testclient import TestClient  # noqa: E402

from arcade import txbuild  # noqa: E402
from arcadesite import ordsend  # noqa: E402
from arcadesite.app import create_app  # noqa: E402
from arcadesite.v1 import V1, V1Error  # noqa: E402

ME, THEM = "nUCBUJGBZjQUjwpLq6MSPbQKDgr7DPLQiL", "ncpXrSCQx667v4y92zSWidxs96N5TtPjTE"
COIN = 100_000_000
INS_TX, TWO_TX, OFF_TX, FEE_TX = "a1" * 32, "a2" * 32, "a3" * 32, "f0" * 32
ONE, TWO_A, TWO_B, OFF = INS_TX + "i0", TWO_TX + "i0", TWO_TX + "i1", OFF_TX + "i0"
fails = []


def check(ok, m):
    print(("ok   " if ok else "FAIL ") + m)
    ok or fails.append(m)


INSCRIPTIONS = {ONE: f"{INS_TX}:0:0", TWO_A: f"{TWO_TX}:0:0", TWO_B: f"{TWO_TX}:0:546", OFF: f"{OFF_TX}:0:500"}
ON_COIN = {INS_TX: [ONE], TWO_TX: [TWO_A, TWO_B], OFF_TX: [OFF]}


def fake_cmd(self, _command, **p):
    if _command == "tx.coins":
        return {"addresses": {a: {"confirmed": [{"txid": FEE_TX, "vout": 1, "value": 2 * COIN, "height": 5}],
                                  "unconfirmed": [],
                                  "held_back": [{"txid": t, "vout": 0, "value": 100_000, "height": 4} for t in ON_COIN]}
                              for a in p["addresses"]}}
    if _command == "explorer.inscription":
        if p["id"] not in INSCRIPTIONS:
            raise V1Error("no inscription")
        return {"id": p["id"], "at": INSCRIPTIONS[p["id"]], "owner": ME}
    if _command == "explorer.tx":
        return {"txid": p["txid"], "outputs": [{"vout": 0, "value": 100_000, "address": ME,
                                                "inscriptions": ON_COIN.get(p["txid"], [])}]}
    raise V1Error(f"unknown method {_command}")


class Account:
    def __init__(self, pubkey):
        self.pubkey = pubkey


V1.cmd = fake_cmd
ordsend.ORD_CHAINS = ordsend.ORD_CHAINS + ("test",)        # the test chain stands in for Dogecoin here
home = tempfile.mkdtemp()
json.dump({"v1": "http://127.0.0.1:1", "v1_home": home}, open(home + "/site.json", "w"))
app = create_app(home)
who = Account("02" + "11" * 32)
app.state.signed_in = lambda request: who
app.state.signed_in_account = lambda request: who
app.state.quota = lambda *a, **k: None
app.state.chains.account_address = lambda pubkey, chain: ME if chain.network == "test" else ""
app.state.listed_coins = lambda network, address: set()
c = TestClient(app)


def outputs_of(raw_hex):
    """(value, script) of a legacy transaction's outputs, from its bytes (as the browser reads the offer)."""
    b = bytes.fromhex(raw_hex)
    pos = 4

    def varint():
        nonlocal pos
        n = b[pos]; pos += 1
        if n < 0xfd:
            return n
        size = {0xfd: 2, 0xfe: 4, 0xff: 8}[n]
        v = int.from_bytes(b[pos:pos + size], "little"); pos += size
        return v
    for _ in range(varint()):
        pos += 36
        n = varint(); pos += n + 4
    out = []
    for _ in range(varint()):
        v = int.from_bytes(b[pos:pos + 8], "little"); pos += 8
        n = varint(); out.append((v, b[pos:pos + n])); pos += n
    return out


def send(**kw):
    body = {"chain": "test", "txid": INS_TX, "vout": 0, "inscription": ONE, "to": THEM, **kw}
    return c.post("/account/doginal/send", json=body)


r = send()
j = r.json()
check(r.status_code == 200 and j.get("offer"), f"an offer: {r.status_code} {str(j)[:160]}")
ins = j.get("inputs") or []
outs = outputs_of(j["raw"]) if j.get("raw") else []
check(ins and ins[0]["txid"] == INS_TX and ins[0]["vout"] == 0, "the inscribed coin is input 0")
check(outs and outs[0] == (100_000, txbuild.p2pkh_script(THEM)), f"its whole value to them as output 0 ({outs[:1]})")
check(all(i["txid"] != INS_TX for i in ins[1:]) and any(i["txid"] == FEE_TX for i in ins[1:]),
      "the fee from a clean coin, never another inscribed one")
check(len(outs) == 2 and outs[1][1] == txbuild.p2pkh_script(ME), "change back to the sender, last")
check(j.get("inscription") == ONE and j.get("amount") == 100_000, "the offer names the inscription and its coin")

r = send(txid=TWO_TX, inscription=TWO_A)
check(r.status_code == 400 and "2 inscriptions" in r.json()["detail"], f"a coin carrying two: refused ({r.json().get('detail')})")
r = send(txid=OFF_TX, inscription=OFF)
check(r.status_code == 400 and "koinu into its coin" in r.json()["detail"], "one not at offset 0: refused")
r = send(txid=TWO_TX)
check(r.status_code == 400 and "moved" in r.json()["detail"], "an inscription named on another coin: refused")
r = send(to=ME)
check(r.status_code == 400 and "own address" in r.json()["detail"], "to itself: refused")
r = send(txid="ee" * 32, inscription="ee" * 32 + "i0")
check(r.status_code == 400 and "not this wallet's" in r.json()["detail"], "a coin that is not this wallet's: refused")
app.state.listed_coins = lambda network, address: {(INS_TX, 0)}
r = send()
check(r.status_code == 400 and "listed" in r.json()["detail"], "a listed one: refused")
app.state.listed_coins = lambda network, address: set()
r = send(chain="doge")
check(r.status_code == 400, "a chain this site does not send on: refused")
r = send(**{"from": THEM, "pubkey": "02" + "33" * 32})
check(r.status_code == 400, f"from an address that is not an imported wallet: refused ({r.json().get('detail')})")
check(txbuild.p2pkh_script(THEM) != txbuild.p2pkh_script(ME), "sanity")

print("ALL OK" if not fails else f"{len(fails)} FAILED")
sys.exit(1 if fails else 0)
