"""The market page API on this site (apps.py "the market page API", exchange.py /account/market/build): what Swap and
other market apps ask of arcade.market, answered for the signed-in reader. Offline, with V1.0 stood in by
exchange_test's shapes.

    .venv/bin/python tests/market_test.py
"""
import json
import os
import sys
import tempfile
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import exchange_test as X  # noqa: E402  (its fakes, its site(), its paths)

from arcade import payload as P  # noqa: E402
from arcade import txbuild  # noqa: E402
from arcadesite.v1 import V1  # noqa: E402

COIN, ME, MAKER, ORDER, MINE, PIECE = X.COIN, X.ME, X.MAKER, X.ORDER, X.MINE, X.PIECE
fails = []


def check(ok, m):
    print(("ok   " if ok else "FAIL ") + m)
    ok or fails.append(m)


def main():
    saved = (V1.page, V1.cmd, V1.core)
    V1.page, V1.cmd, V1.core = X.fake_page, X.fake_cmd, X.fake_core
    try:
        home = tempfile.mkdtemp()
        json.dump({"v1": "http://127.0.0.1:1", "v1_home": home}, open(home + "/site.json", "w"))
        c, app = X.site(home, ME)
        src = app.state.frame_src(None, PIECE + "i0")
        ticket = urllib.parse.parse_qs(urllib.parse.urlparse(src).query)["v"][0]
        v = f"?v={ticket}"

        r = c.get("/r/market.js")
        check(r.status_code == 200 and "window.arcade.market" in r.text, "/r/market.js is served")

        r = c.get("/r/market/holdings")
        check(r.status_code == 403 and r.json()["ok"] is False, "holdings without a reader: refused")
        h = c.get("/r/market/holdings" + v).json()["result"]
        coin = next(x for x in h if x["kind"] == "coin")
        frog = next(x for x in h if x.get("property_id") == 7)
        gem = next(x for x in h if x.get("property_id") == 8)
        check(coin["amount"] == 13 * COIN and coin["asset"] == "coin" and coin["network"] == "peptest",
              f"holdings: the reader's coin, 13 ({coin})")
        check(frog["amount"] == 5 * COIN and frog["divisible"] is True and frog["asset"] == "7" and gem["amount"] == 3,
              "holdings: the reader's tokens, in base units")

        m = c.get("/r/market/mine" + v).json()["result"]
        check([o["order"] for o in m["orders"]] == [MINE], f"mine: only the reader's own order ({m})")
        o = m["orders"][0]
        check(o["give"] == {"asset": 7, "amount": 2 * COIN} and o["get"] == {"asset": 0, "amount": 2 * COIN},
              "mine: an ask gives the token and gets the coin")

        mk = c.get("/r/market/markets").json()["result"]
        check(len(mk) == 1 and mk[0]["base"]["property_id"] == 7, "markets: from the exchange's copy")
        s = c.get("/r/market/swaps").json()["result"]
        check(s["book"] == [] and s["mine"] == [] and "Arcade app" in s["note"], "swaps: none here, and it says why")
        t = c.get("/r/market/trades?base=7&quote=0").json()["result"]
        check(len(t) == 1 and t[0]["txid"] == X.TRADE, "trades: from the exchange's copy")
        t = c.get("/r/market/trades?base=7&quote=coin").json()["result"]
        check(len(t) == 1, "trades: the coin named as coin, as the app asks")

        r = c.post("/r/market/swap_place" + v, json={"give": {}, "get": {}})
        check(r.status_code == 400 and "Arcade app" in r.json()["error"], "a swap across chains is refused, plainly")
        r = c.post("/r/market/place" + v, json={"network": "dgbtest", "give": {"asset": "DD", "amount": "100"},
                                                "get": {"asset": "DGB", "amount": "1"}})
        check(r.status_code == 400 and "Arcade app" in r.json()["error"], "a DigiDollar order is refused, plainly")
        r = c.post("/r/market/place", json={"give": {"asset": "7", "amount": str(COIN)},
                                            "get": {"asset": "coin", "amount": str(COIN // 2)}})
        check(r.status_code == 403, "a write without a reader: refused")
        r = c.post("/r/market/place" + v, json={"give": {"asset": "7", "amount": "0"}, "get": {"asset": "coin", "amount": "1"}})
        check(r.status_code == 400, "an amount of zero is refused")

        def ask(op, body):
            r = c.post(f"/r/market/{op}" + v, json=body)
            j = r.json()
            check(r.status_code == 202 and j.get("status") == "pending" and j.get("id"), f"{op}: a question filed ({j})")
            waiting = c.get("/account/pagesends").json()["requests"]
            row = next((w for w in waiting if w["id"] == j["id"]), {})
            check(row.get("kind") == "market" and row.get("op") == op, f"{op}: on the reader's card queue")
            check(c.get(f"/r/send/{j['id']}").json().get("status") == "pending", f"{op}: the page sees it waiting")
            return c.post("/account/market/build", json={"id": j["id"]})

        r = ask("place", {"give": {"asset": "7", "amount": str(COIN)}, "get": {"asset": "coin", "amount": str(COIN // 2)}})
        msg = P.MetaDExTrade(property_id_for_sale=7, amount_for_sale=COIN, property_id_desired=0,
                             amount_desired=COIN // 2).encode().hex()
        check(r.status_code == 200 and r.json().get("offer") and msg in r.json().get("raw", ""),
              f"place, a token for the coin: the MetaDExTrade ({str(r.json())[:160]})")
        r = ask("place", {"give": {"asset": "coin", "amount": str(COIN)}, "get": {"asset": "7", "amount": str(2 * COIN)}})
        msg = P.MetaDExTrade(property_id_for_sale=0, amount_for_sale=COIN, property_id_desired=7,
                             amount_desired=2 * COIN).encode().hex()
        check(r.status_code == 200 and msg in r.json().get("raw", ""), "place, the coin for a token (a bid)")
        r = ask("place", {"give": {"asset": "7", "amount": str(6 * COIN)}, "get": {"asset": "coin", "amount": "1"}})
        check(r.status_code == 400 and "free" in r.json()["detail"],
              f"place: more than is free (5 held) is refused ({r.json()})")
        r = ask("place", {"give": {"asset": "8", "amount": "1"}, "get": {"asset": "7", "amount": str(COIN)}})
        pairs = getattr(app.state.chains.account_chain().params, "token_pairs_from", None) is not None
        if pairs:
            msg = P.MetaDExTrade(property_id_for_sale=8, amount_for_sale=1, property_id_desired=7,
                                 amount_desired=COIN).encode().hex()
            check(r.status_code == 200 and msg in r.json().get("raw", ""), "place, a token for another token")
        else:
            check(r.status_code == 400 and "pairs" in r.json()["detail"], "place, token for token: not on here yet")
        r = ask("cancel", {"order": MINE})
        msg = P.MetaDExCancelPrice(property_id_for_sale=7, amount_for_sale=2 * COIN, property_id_desired=0,
                                   amount_desired=2 * COIN).encode().hex()
        check(r.status_code == 200 and msg in r.json().get("raw", ""), "cancel: the reader's order at its price")
        r = ask("cancel", {"order": ORDER})
        check(r.status_code == 400 and "not yours" in r.json()["detail"], "cancel: somebody else's order is refused")
        r = ask("take", {"order": ORDER, "amount": str(10 * COIN)})
        j = r.json()
        pay = txbuild.p2pkh_script(MAKER).hex()
        take = P.MetaDExTake(property_id=7, amount=10 * COIN, order=bytes.fromhex(ORDER)).encode().hex()
        check(r.status_code == 200 and pay in j.get("raw", "") and take in j.get("raw", ""),
              f"take: the coins to the maker and the take naming the order ({str(j)[:140]})")
        r = ask("take", {"order": MINE, "amount": "1"})
        check(r.status_code == 400 and "your own" in r.json()["detail"], "take: one's own order is refused")
        check(c.post("/account/market/build", json={"id": "nope"}).status_code == 404, "build: an unknown question")
    finally:
        V1.page, V1.cmd, V1.core = saved
    print("\nALL OK" if not fails else f"\n{len(fails)} FAILED")
    return not fails


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
