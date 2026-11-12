"""The operator's Marketplace tab (arcadesite/marketplace.py), offline with exchange_test's stand-in V1.0.
    .venv/bin/python tests/marketplace_test.py"""
import json, os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import exchange_test as X  # noqa: E402
from arcadesite.v1 import V1  # noqa: E402

fails = []
def check(ok, m):
    print(("ok   " if ok else "FAIL ") + m); ok or fails.append(m)

V1.page, V1.cmd, V1.core = X.fake_page, X.fake_cmd, X.fake_core
home = tempfile.mkdtemp()
json.dump({"v1": "http://127.0.0.1:1", "v1_home": home}, open(home + "/site.json", "w"))
c, app = X.site(home, X.ME)
op = "02" + "11" * 32
check(c.get("/admin").status_code == 403, "/admin: not the operator -> 403")
app.state.site.set_setting("operator", op)
r = c.get("/admin")
check(r.status_code == 200 and "Marketplace" in r.text and "Traceback" not in r.text, "/admin: the operator sees the Marketplace tab")
j = c.get("/admin/api/marketplace").json()
check(j["market_taker"] is False and j["market_maker"] is False and j["trade_cut"] == 0, "all off by default")
check(j["chains"][0]["pay_to"] == X.ME, f"the fee goes to the operator's own address by default ({j['chains'][0]})")
r = c.post("/admin/api/marketplace", json={"market_taker": True, "trade_cut": "0.5", "chains": {"test": {"cap": "100"}}})
j = r.json()
check(r.status_code == 200 and j["market_taker"] and not j["market_maker"] and j["trade_cut"] == 0.5 and j["chains"][0]["cap"] == "100", f"saved ({j})")
r = c.post("/admin/api/marketplace", json={"trade_cut": "9"})
check(r.status_code == 400, "a fee above 5 percent is refused")
r = c.post("/admin/api/marketplace", json={"chains": {"test": {"pay_to": "DBXu2kgc3xtvCUWFcxFE3r9hEYgmuaaCyD"}}})
check(r.status_code == 400 and "not a" in r.json()["detail"], "a Dogecoin address for Pepecoin testnet is refused")
r = c.post("/admin/api/marketplace", json={"chains": {"test": {"pay_to": X.MAKER}}})
check(r.status_code == 200 and r.json()["chains"][0]["pay_to"] == X.MAKER, "a payout address of the chain is kept")
print("\nALL OK" if not fails else f"\n{len(fails)} FAILED")
sys.exit(1 if fails else 0)
