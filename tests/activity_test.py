"""/account/activity (arcadesite/wallet.py): V1.0's tx.activity rows {height, txid, received, spent}, oldest first,
turned into the wallet page's rows {net, time, confirmations, other, fee}, newest first, 50 at a time.
Offline, V1.0 stood in by the shapes of crates/arcaded/src/sitesvc.rs and explorer.rs.

    .venv/bin/python tests/activity_test.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.environ.get("ARCADE_SRC", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "arcade-src")))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fastapi.testclient import TestClient  # noqa: E402

from arcadesite.app import create_app  # noqa: E402
from arcadesite.v1 import V1, V1Error  # noqa: E402

ME, THEM = "nUCBUJGBZjQUjwpLq6MSPbQKDgr7DPLQiL", "ncpXrSCQx667v4y92zSWidxs96N5TtPjTE"
fails = []
calls = []


def check(ok, m):
    print(("ok   " if ok else "FAIL ") + m)
    ok or fails.append(m)


# 120 rows, heights 1..120, oldest first as V1 gives them; every 10th one a send to THEM, every 7th an inscription
# (a spend whose outputs are all ours or data).
HIST = []
for h in range(1, 121):
    if h % 10 == 0:
        HIST.append({"height": h, "txid": f"{h:064x}", "received": 50_000, "spent": 300_000})
    elif h % 7 == 0:
        HIST.append({"height": h, "txid": f"{h:064x}", "received": 0, "spent": 100_000})
    else:
        HIST.append({"height": h, "txid": f"{h:064x}", "received": 100_000, "spent": 0})


def fake_cmd(self, _command, **p):
    calls.append(_command)
    if _command == "tx.activity":
        return {"chain": "test", "height": 125, "since": 0, "addresses": {a: {"confirmed": HIST, "waiting": []} for a in p["addresses"]}}
    if _command == "explorer.blocks":
        h = p["before"] - 1
        return [{"height": h, "time": 1_700_000_000 + h * 60}]
    if _command == "explorer.tx":
        h = int(p["txid"], 16)
        outs = [{"vout": 0, "value": 50_000, "address": ME, "tag": None}]
        if h % 10 == 0:
            outs.insert(0, {"vout": 0, "value": 240_000, "address": THEM, "tag": "them"})
        return {"txid": p["txid"], "fee": 10_000, "outputs": outs}
    raise V1Error(f"unknown method {_command}")


class Account:
    def __init__(self, pubkey):
        self.pubkey = pubkey


V1.cmd = fake_cmd
home = tempfile.mkdtemp()
json.dump({"v1": "http://127.0.0.1:1", "v1_home": home}, open(home + "/site.json", "w"))
app = create_app(home)
who = Account("02" + "11" * 32)
app.state.signed_in = lambda request: who
app.state.signed_in_account = lambda request: who
app.state.chains.account_address = lambda pubkey, chain: ME if chain.network == "test" else ""
c = TestClient(app)

r = c.get("/account/activity?chain=test")
check(r.status_code == 200, f"answers: {r.status_code} {r.text[:120]}")
j = r.json()
rows = j["rows"]
check(len(rows) == 50 and j["more"] is True and j["tip"] == 125, f"first page: 50 rows, more, tip ({len(rows)}, {j.get('more')})")
check(rows[0]["height"] == 120 and rows[-1]["height"] == 71, "newest first")
check(all("net" in x and "time" in x and "confirmations" in x for x in rows), "every row has net, time, confirmations")
top = rows[0]
check(top["net"] == -250_000 and top["other"] == THEM and top["other_tag"] == "them" and top["fee"] == 10_000,
      f"a send: net, to whom, fee ({top})")
check(top["time"] == 1_700_000_000 + 120 * 60 and top["confirmations"] == 6, "its block time and confirmations")
ins = next(x for x in rows if x["height"] == 119)
check(ins["net"] == -100_000 and "other" not in ins, f"an inscription-like spend has no other ({ins})")
recv = next(x for x in rows if x["height"] == 118)
check(recv["net"] == 100_000 and recv["spent"] == 0, "a receipt is positive")

r = c.get("/account/activity?chain=test&before=71").json()
check(len(r["rows"]) == 50 and r["rows"][0]["height"] == 70 and r["more"] is True, "second page from before=71")
r = c.get("/account/activity?chain=test&before=21").json()
check(len(r["rows"]) == 20 and r["more"] is False, "last page, no more")

n = calls.count("explorer.blocks")
c.get("/account/activity?chain=test")
check(calls.count("explorer.blocks") == n, "block times are kept, not asked again")

print("ALL OK" if not fails else f"{len(fails)} FAILED")
sys.exit(1 if fails else 0)
