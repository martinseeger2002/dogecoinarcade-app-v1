"""The Exchange (arcadesite/exchange.py): its pages and the account's order-book offers.

Offline first, with V1.0 stood in by the shapes of crates/arcaded/src/marketsvc.rs and walletsvc.rs; then live against
the dev home (read-only: pages render, offers are built and never signed).

    .venv/bin/python tests/exchange_test.py [--offline]
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.environ.get("ARCADE_SRC", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "arcade-src")))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fastapi.testclient import TestClient  # noqa: E402

from arcade import payload as P  # noqa: E402
from arcade import txbuild  # noqa: E402
from arcadesite import exchange  # noqa: E402
from arcadesite.app import create_app  # noqa: E402
from arcadesite.v1 import V1, V1Error  # noqa: E402

COIN = 100_000_000
fails = []


def check(ok, m):
    print(("ok   " if ok else "FAIL ") + m)
    ok or fails.append(m)


class Account:
    def __init__(self, pubkey):
        self.pubkey = pubkey


def site(home, address, quota_free=True):
    """create_app plus this module (the integrator wires it into app.py), signed in as `address`."""
    app = create_app(home)
    who = Account("02" + "11" * 32)
    app.state.signed_in = lambda request: who
    app.state.signed_in_account = lambda request: who
    if quota_free:
        app.state.quota = lambda *a, **k: None
        app.state.offers.waiting = lambda pubkey: []      # the modules' quota is bound when they register: no cap here
        app.state.site.accounts().charge = lambda *a, **k: {}
    app.state.chains.account_address = lambda pubkey, chain: address if chain.network == "test" else ""
    if not any(getattr(r, "path", "") == "/exchange" for r in app.routes):
        exchange.register(app, app.state.site, app.state.chains)
    return TestClient(app), app


def no_jinja_error(r):
    return r.status_code == 200 and "Traceback" not in r.text and "jinja2" not in r.text.lower()


# ---------------------------------------------------------------- offline
ME = "nUCBUJGBZjQUjwpLq6MSPbQKDgr7DPLQiL"
MAKER = "ncpXrSCQx667v4y92zSWidxs96N5TtPjTE"
ORDER, MINE, PIECE, TRADE = "aa" * 32, "bb" * 32, "cc" * 32, "dd" * 32
TOKENS = {7: {"name": "Frog", "divisible": True, "issuer": ME}, 8: {"name": "Gem", "divisible": False, "issuer": MAKER}}
ASK = {"order": ORDER, "maker": MAKER, "base": 100 * COIN, "quote": 50 * COIN,
       "price": {"num": 50, "den": 100, "each": 0.5}, "pending": False, "crosses": False, "height": 10, "taking": 0}
MY_ASK = {**ASK, "order": MINE, "maker": ME, "base": 2 * COIN, "quote": 2 * COIN, "height": 11}


def fake_page(self, path, params=None, timeout=None):
    params = params or {}
    if path.startswith("token/"):
        pid = int(path.split("/")[1])
        t = TOKENS.get(pid)
        return (200, {"propertyid": pid, "name": t["name"], "divisible": t["divisible"], "issuer": t["issuer"],
                      "managed": False, "data": "", "url": ""}) if t else (404, {"error": "no such token"})
    if path == "market/book":
        if int(params.get("quote")) == 0 and int(params.get("base")) == 7:
            return 200, {"ok": True, "result": {"asks": [ASK, MY_ASK], "bids": [], "spread": None}}
        return 200, {"ok": True, "result": {"asks": [], "bids": [], "spread": None}}
    if path == "market/markets":
        return 200, {"ok": True, "result": [
            {"base": {"network": "peptest", "kind": "token", "asset": "7", "property_id": 7, "name": "Frog",
                      "divisible": True},
             "quote": {"network": "peptest", "kind": "coin", "asset": "PEPtest", "property_id": 0, "name": "PEPtest",
                       "divisible": True}, "venue": "ledger", "last": 0.5, "change_24h": None, "volume_24h": 0,
             "asks": 2, "bids": 0}]}
    if path == "market/trades":
        if int(params.get("base")) == 7 and int(params.get("quote")) == 0:
            return 200, {"ok": True, "result": [{"height": 9, "txid": TRADE, "base": 4 * COIN, "quote": 2 * COIN,
                                                 "side": "buy", "taker": None, "maker": ME, "at": 1_700_000_000,
                                                 "id": TRADE}]}
        return 200, {"ok": True, "result": []}
    if path.startswith("book/"):
        rows = [{"order": r["order"], "address": r["maker"], "tokens": r["base"], "coins": r["quote"],
                 "pending": False, "taking": 0} for r in (ASK, MY_ASK)]
        if params.get("address"):
            rows = [r for r in rows if r["address"] == params["address"]]
        if path != "book/7":
            rows = []
        return 200, {"property_id": 7, "name": "Frog", "divisible": True, "asks": rows, "bids": []}
    if path.startswith("balances/"):
        return 200, [{"propertyid": 7, "name": "Frog", "units": 5 * COIN, "divisible": True},
                     {"propertyid": 8, "name": "Gem", "units": 3, "divisible": False}]
    if path.startswith("address/"):
        return 200, {"address": path[8:], "tag": "maker" if path.endswith(MAKER) else ""}
    if path.startswith("inscription/"):
        return 200, {"id": PIECE, "number": 42, "owner": MAKER, "creator": MAKER, "rawjson": '{"name":"Lily"}',
                     "collection": None, "edition": None, "contenttype": "image/png", "held": True, "block": 5}
    if path.startswith("inscriptions/"):
        return 200, []
    return 404, {"error": "not a route of this arcade's page API"}


def fake_cmd(self, _command, _timeout=None, **p):
    if _command == "chains.list":
        return [{"key": "peptest", "installed": True, "home": True, "indexes": ["ledger", "addresses"]}]
    if _command == "tx.coins":
        return {"addresses": {a: {"confirmed": [{"txid": "ee" * 32, "vout": 0, "value": 10 * COIN, "height": 5},
                                                {"txid": "ef" * 32, "vout": 1, "value": 3 * COIN, "height": 6}],
                                  "unconfirmed": [], "held_back": []} for a in p["addresses"]}}
    if _command == "explorer.ledger_tokens":
        return {"items": [{"id": k, "name": v["name"], "type": 2 if v["divisible"] else 1, "issuer": v["issuer"]}
                          for k, v in TOKENS.items()]}
    if _command == "msg.who":
        return {"address": p["name"], "key": "ab" * 32}
    if _command == "status":
        return {"height": 12}
    if _command == "tx.activity":
        return {"addresses": {}}
    if _command == "explorer.tokens":
        return {"items": [{"tick": "dogi"}]}
    raise V1Error(f"unknown method {_command}")


def fake_core(self, chain, method, *params, timeout=None):
    raise V1Error("no core offline")


def offline():
    saved = (V1.page, V1.cmd, V1.core)
    V1.page, V1.cmd, V1.core = fake_page, fake_cmd, fake_core
    try:
        home = tempfile.mkdtemp()
        json.dump({"v1": "http://127.0.0.1:1", "v1_home": home}, open(home + "/site.json", "w"))
        c, app = site(home, ME)
        for path in ("/exchange", "/exchange?tab=tokens", "/exchange?tab=offers", "/exchange?tab=market",
                     "/exchange/pair/7", "/exchange/pairs/7/8", "/exchange/history", f"/exchange/trade/{TRADE}",
                     f"/exchange/sell/{PIECE}", "/listings"):
            r = c.get(path)
            check(no_jinja_error(r), f"offline page {path}: {r.status_code}")
        r = c.get("/exchange?tab=tokens")
        check("/exchange/pair/7" in r.text and "Frog" in r.text, "Tokens tab lists Frog/coin from /r/market/markets")
        r = c.get("/exchange/pair/7")
        check("0.5" in r.text and "@maker" in r.text or "maker" in r.text, "pair page draws the ask at 0.5")
        check(c.get("/exchange/pair/999").status_code == 404, "an unknown token's pair page is 404")
        r = c.get("/exchange/history")
        check(TRADE in r.text, "history: the account's own trade as maker")
        r = c.get(f"/exchange/trade/{TRADE}")
        check(MAKER not in r.text and "Taken from the book" in r.text, "trade page: a take, seller = maker")
        r = c.get("/exchange/pairs?base=7&quote=coin", follow_redirects=False)
        check(r.status_code == 303 and r.headers["location"] == "/exchange/pair/7", "open-a-pair -> /exchange/pair/7")
        r = c.get("/exchange/pairs?base=test-token-7&quote=test-token-8", follow_redirects=False)
        check(r.headers.get("location") == "/exchange/pairs/7/8", "token/token -> /exchange/pairs/7/8")
        j = c.get("/exchange/token20/search?q=do").json()
        check(isinstance(j.get("results"), list), f"token20 search answers ({j})")

        r = c.post("/account/order", json={"side": "ask", "property_id": 7, "amount": "1", "price": "0.5"})
        j = r.json()
        msg = P.MetaDExTrade(property_id_for_sale=7, amount_for_sale=COIN, property_id_desired=0,
                             amount_desired=COIN // 2).encode().hex()
        check(r.status_code == 200 and j.get("offer") and msg in j.get("raw", ""),
              f"/account/order ask: an offer carrying the MetaDExTrade ({r.status_code} {str(j)[:120]})")
        r = c.post("/account/order", json={"side": "ask", "property_id": 7, "amount": "6", "price": "0.5"})
        check(r.status_code == 400 and "holds" in r.json()["detail"], f"an ask beyond what is held is refused")
        r = c.post("/account/order", json={"side": "bid", "property_id": 7, "amount": "2", "price": "0.25"})
        check(r.status_code == 200 and r.json()["total"] == "0.5", "a bid: 2 at 0.25 = 0.5 coins")
        r = c.post("/account/order/cancel", json={"side": "ask", "property_id": 7})
        check(r.status_code == 200 and r.json()["every"] is True, "cancel a whole side (MetaDExCancelPair)")
        r = c.post("/account/order/cancel", json={"side": "ask", "property_id": 7, "price": "1"})
        msg = P.MetaDExCancelPrice(property_id_for_sale=7, amount_for_sale=2 * COIN, property_id_desired=0,
                                   amount_desired=2 * COIN).encode().hex()
        check(r.status_code == 200 and r.json()["every"] is False and msg in r.json()["raw"],
              f"cancel one price: MetaDExCancelPrice copied from the standing row ({r.json().get('detail', '')})")
        r = c.post("/account/order/cancel", json={"side": "ask", "property_id": 7, "price": "3"})
        check(r.status_code == 400 and "Standing: 1.00000000" in r.json()["detail"], "a price with nothing at it")
        r = c.post("/account/order/take", json={"order": ORDER, "amount": "10", "property_id": 7})
        j = r.json()
        pay = txbuild.p2pkh_script(MAKER).hex()
        take = P.MetaDExTake(property_id=7, amount=10 * COIN, order=bytes.fromhex(ORDER)).encode().hex()
        check(r.status_code == 200 and pay in j["raw"] and take in j["raw"] and j["coins"] == "5",
              f"take: 5 coins to the maker and the take naming the order ({str(j)[:140]})")
        r = c.post("/account/order/take", json={"order": MINE, "amount": "1", "property_id": 7})
        check(r.status_code == 400 and "your own" in r.json()["detail"], "taking one's own order is refused")
        j = c.post("/account/order/list", json={}).json()
        check(j.get("count") == 1 and j["orders"][0]["order"] == MINE and j["orders"][0]["price"] == "1.00000000",
              f"order list: the account's own ask ({str(j)[:140]})")
        j = c.post("/account/take", json={"order": ORDER, "amount": "1", "property_id": 7}).json()
        check(j.get("order") == ORDER and j.get("to") == "ab" * 32 and j.get("maker") == MAKER,
              f"/account/take: the queue's order and the maker's key ({str(j)[:120]})")
        r = c.post("/account/pair-order", json={"base": 7, "quote": 8, "side": "sell", "amount": "1", "price": "2"})
        msg = P.MetaDExTrade(property_id_for_sale=7, amount_for_sale=COIN, property_id_desired=8,
                             amount_desired=2).encode().hex()
        check(r.status_code == 200 and msg in r.json()["raw"], f"pair order: Frog for Gem ({r.json().get('detail')})")
        r = c.post("/account/offer", json={"piece": PIECE, "kind": "coins", "amount": "1"})
        check(r.status_code == 200 and r.json().get("presigned") is False and "#42" in r.json()["what"],
              f"offer on a piece ({r.json().get('detail') or r.json().get('what')})")
        r = c.post("/account/offer/bid", json={})
        check(r.status_code == 403 and r.json().get("node_trades") is False, "pre-signed offers: the refusal")
        r = c.post("/account/list", json={"piece": PIECE, "amount": "1"})
        check(r.status_code == 403, "listing with node-facilitated trades off: the original's refusal")
        r = c.post("/account/standing", json={"property_id": 7, "amount": "1", "price": "0.1"})
        check(r.status_code == 200 and r.json()["standing"]["price"] == "0.1", "a fill-when-back buy order stands")
        check("standing-" in c.get("/exchange/pair/7").text, "...and is drawn among the bids")
        r = c.post("/account/standing", json={"property_id": 7, "amount": "1", "price": "0.1", "away": True})
        check(r.status_code == 403, "an away buy order is refused (node-facilitated)")
    finally:
        V1.page, V1.cmd, V1.core = saved


# ---------------------------------------------------------------- live (dev home, read-only)
def live():
    home = os.path.expanduser(os.environ.get("ARCADE_SITE_DEV", "~/.arcade-site-dev"))
    c, app = site(home, "ncpXrSCQx667v4y92zSWidxs96N5TtPjTE")
    v1 = app.state.site.v1
    try:
        v1.cmd("status")
    except V1Error as e:
        check(False, f"live: the dev home's V1.0 answers ({e})")
        return
    for path in ("/exchange", "/exchange?tab=tokens", "/exchange/pair/3", "/exchange/pairs/3/4", "/exchange/history",
                 "/listings", "/exchange/token20/search?q=d"):
        r = c.get(path)
        check(r.status_code == 200 and "Traceback" not in r.text, f"live {path}: {r.status_code}")
    code, rows = v1.page("inscriptions", {"limit": 1})
    if code == 200 and rows:
        r = c.get(f"/exchange/sell/{rows[0]['id']}")
        check(no_jinja_error(r), f"live /exchange/sell/<#{rows[0].get('number')}>: {r.status_code}")
    j = c.post("/account/order/list", json={"property_id": 3}).json()
    check("orders" in j, f"live order list: {str(j)[:120]}")
    r = c.post("/account/order", json={"side": "bid", "property_id": 3, "amount": "1", "price": "0.001"})
    check(r.status_code in (200, 400) and ("offer" in r.json() or "detail" in r.json()),
          f"live bid offer built, nothing signed ({r.status_code} {str(r.json())[:120]})")


if __name__ == "__main__":
    offline()
    if "--offline" not in sys.argv:
        live()
    print("ALL OK" if not fails else "%d FAILED" % len(fails))
