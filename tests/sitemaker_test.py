"""An order placed for a website visitor (arcadesite/sitemaker.py), offline: V1.0's swap.place / swap.release, tx.coins and
explorer.inscription stood in here; the browser's part done in Python.

    .venv/bin/python tests/sitemaker_test.py
"""
import json, os, sys, tempfile, urllib.parse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import exchange_test as X  # noqa: E402

from arcade.config import NETWORKS  # noqa: E402
from arcade.script import b58check_encode, hash160  # noqa: E402
from arcadesite.v1 import V1, V1Error  # noqa: E402

COIN = 100_000_000
fails = []


def check(ok, m):
    print(("ok   " if ok else "FAIL ") + m)
    ok or fails.append(m)


ME_DOGE = b58check_encode(NETWORKS["doge"].pubkeyhash_version, hash160(b"\x02" + b"\x11" * 32))
ME_TEST = X.ME
OP_DOGE = b58check_encode(NETWORKS["doge"].pubkeyhash_version, hash160(b"\x02" + b"\x22" * 32))
OP_TEST = b58check_encode(NETWORKS["test"].pubkeyhash_version, hash160(b"\x02" + b"\x22" * 32))
INS = "ab" * 32 + "i0"
HELD = {}          # address -> confirmed coin total
OWNER = {INS: ME_DOGE}
PLACED, RELEASED = [], []
TOKENS = {}        # address -> {property id: units}
T20 = {}           # address -> FRGY held


def fake_cmd(self, _command, _timeout=None, **p):
    if _command == "chains.list":
        return [{"key": "peptest", "installed": True, "home": True, "indexes": ["ledger", "addresses"]},
                {"key": "doge", "installed": True, "indexes": ["addresses", "ord"]}]
    if _command == "tx.coins":
        return {"addresses": {a: {"confirmed": [{"txid": "cc" * 32, "vout": 0, "value": HELD[a], "height": 5}] if HELD.get(a) else [],
                                  "unconfirmed": [], "held_back": []} for a in p["addresses"]}}
    if _command == "tokens":
        return [{"tick": "frgy", "overall": str(T20.get(p["owner"], 0)), "transferable": "0"}]
    if _command == "explorer.token":
        return {"tick": p["tick"], "dec": 0}
    if _command == "explorer.address":
        return {"ledger_tokens": [{"propertyid": k, "units": str(u)} for k, u in TOKENS.get(p["address"], {}).items()]}
    if _command == "explorer.inscription":
        return {"id": p["id"], "owner": OWNER.get(p["id"]), "at": "dd" * 32 + ":0:0"}
    if _command == "swap.place":
        PLACED.append(p)
        return {"id": "0a0b0c0d", "give": p["give"], "get": p["get"], "status": "open"}
    if _command == "swap.release":
        RELEASED.append(p)
        return {"id": p["order"], "status": "released", "returned": [{"chain": "doge", "txids": ["ee" * 32]}]}
    if _command == "swap.book":
        return [{"id": "0a0b0c0d", "node": "ab" * 8, "give": {"chain": "doge", "kind": "ord", "asset": INS, "amount": "1"},
                 "get": {"chain": "peptest", "kind": "coin", "asset": "", "amount": str(3 * COIN)}}]
    return X.fake_cmd(self, _command, _timeout, **p)


def main():
    V1.page, V1.cmd = X.fake_page, fake_cmd
    home = tempfile.mkdtemp()
    json.dump({"v1": "http://127.0.0.1:1", "v1_home": home}, open(home + "/site.json", "w"))
    c, app = X.site(home, X.ME)
    chains = app.state.chains
    chains.account_address = lambda pubkey, chain: {"test": ME_TEST, "doge": ME_DOGE}.get(chain.network, "")
    src = app.state.frame_src(None, X.PIECE + "i0")
    v = "?v=" + urllib.parse.parse_qs(urllib.parse.urlparse(src).query)["v"][0]
    coin = lambda ch, n: {"chain": ch, "kind": "coin", "asset": "", "amount": str(n)}

    r = c.post("/r/market/swap_place" + v, json={"give": coin("peptest", 5 * COIN), "get": coin("doge", 2 * COIN)})
    check(r.status_code == 400, "with the maker side off, an order is refused")
    app.state.site.set_setting("market_maker", True)
    app.state.site.set_setting("trade_cut", 1)
    app.state.site.set_setting("trade_cut_to", {"doge": OP_DOGE, "test": OP_TEST})
    app.state.site.set_setting("market_maker_cap", {"test": 10})

    # ---- coins for coins: the fee 1% of what the visitor gives, with the lock
    qid = c.post("/r/market/swap_place" + v, json={"give": coin("peptest", 5 * COIN), "get": coin("doge", 2 * COIN)}).json()["id"]
    q = c.post("/account/swap/make/quote", json={"id": qid}).json()
    check(q.get("fee") == "0.05" and q.get("fee_coin") and q.get("fund") == "5.08",
          f"quote: fee 0.05 of the 5 given; fund 5 + 0.01 spend + 0.05 fee + 0.02 margin ({q.get('fee')}, {q.get('fund')})")
    key = "11" * 32
    k = c.post("/account/swap/make/key", json={"id": qid, "key": key}).json()
    check(k.get("give_address", "").startswith("n"), f"the key's address on the give chain ({k.get('give_address')})")
    c.post("/account/swap/make/funded", json={"id": qid})
    st = c.post("/account/swap/make/step", json={"id": qid}).json()
    check(st.get("step") == "funded" and "waiting" in st.get("say", ""), "not placed before the funds are in a block")
    HELD[k["give_address"]] = int(5.08 * COIN)
    st = c.post("/account/swap/make/step", json={"id": qid}).json()
    p = PLACED[-1] if PLACED else {}
    check(st.get("step") == "placed" and p.get("key") == {"peptest": key, "doge": key}
          and p.get("payout") == {"peptest": ME_TEST, "doge": ME_DOGE}
          and p.get("maker_fee") == {"bps": 100, "to": {"peptest": OP_TEST}},
          f"placed in V1.0 with the key, the visitor's payouts and the maker fee ({p})")
    check(app.state.page_send(qid)["status"] == "sent" and app.state.page_send(qid)["txid"] == "0a0b0c0d",
          "the page's question answered with the order's id")
    over = c.post("/r/market/swap_place" + v, json={"give": coin("peptest", 50 * COIN), "get": coin("doge", 2 * COIN)}).json()["id"]
    r = c.post("/account/swap/make/quote", json={"id": over})
    check(r.status_code == 400 and "at most 10" in r.json()["detail"], "an order over the cap is refused")

    # ---- an inscription for coins: the fee from the coins it gets; the inscription must be the visitor's
    qid2 = c.post("/r/market/swap_place" + v, json={"give": {"chain": "doge", "kind": "ord", "asset": INS, "amount": "1"},
                                                    "get": coin("peptest", 3 * COIN)}).json()["id"]
    q2 = c.post("/account/swap/make/quote", json={"id": qid2}).json()
    check(q2.get("ord") and q2.get("at", {}).get("vout") == 0 and q2.get("fee") == "0.03" and q2.get("fee_coin"),
          f"an inscription's quote: where it is now, the fee 1% of the 3 PEP it gets ({q2.get('fee')}, {q2.get('at')})")
    OWNER[INS] = "somebody else"
    qid3 = c.post("/r/market/swap_place" + v, json={"give": {"chain": "doge", "kind": "ord", "asset": INS, "amount": "1"},
                                                    "get": coin("peptest", 3 * COIN)}).json()["id"]
    r = c.post("/account/swap/make/quote", json={"id": qid3})
    check(r.status_code == 400, "an inscription that isn't the visitor's is refused")

    # ---- an arcade token for coins: the fee 1% in the token itself, both sent to the order's address
    qt = c.post("/r/market/swap_place" + v, json={"give": {"chain": "peptest", "kind": "token", "asset": "7", "amount": "1000"},
                                                  "get": coin("doge", COIN)}).json()["id"]
    qtq = c.post("/account/swap/make/quote", json={"id": qt}).json()
    check(qtq.get("token") and qtq.get("token_units") == 1010 and qtq.get("token_fee") == 10 and qtq.get("property_id") == 7,
          f"a token's quote: 1000 + 10 fee in the token ({qtq.get('token_units')}, {qtq.get('token_fee')})")
    kt = c.post("/account/swap/make/key", json={"id": qt, "key": "33" * 32}).json()
    c.post("/account/swap/make/funded", json={"id": qt})
    HELD[kt["give_address"]] = 10 * COIN
    st = c.post("/account/swap/make/step", json={"id": qt}).json()
    check(st.get("step") == "funded" and "tokens" in st.get("say", ""), "not placed before the tokens arrive")
    TOKENS[kt["give_address"]] = {7: 1010}
    st = c.post("/account/swap/make/step", json={"id": qt}).json()
    check(st.get("step") == "placed" and PLACED[-1]["give"]["kind"] == "token" and PLACED[-1]["maker_fee"]["to"] == {"peptest": OP_TEST},
          f"placed with the fee to the operator on the token's chain ({PLACED[-1].get('maker_fee')})")

    # ---- a DRC-20 for coins: whole tokens, the fee rounded up in the token, sent by V1.0 from the order's key
    q20 = c.post("/r/market/swap_place" + v, json={"give": {"chain": "peptest", "kind": "token20", "asset": "frgy", "amount": "250"},
                                                   "get": coin("doge", COIN)}).json()["id"]
    qq = c.post("/account/swap/make/quote", json={"id": q20}).json()
    check(qq.get("t20") and qq.get("token_units") == 253 and qq.get("token_fee") == 3,
          f"a DRC-20's quote: 250 + 3 (1% rounded up) ({qq.get('token_units')}, {qq.get('token_fee')})")
    k20 = c.post("/account/swap/make/key", json={"id": q20, "key": "44" * 32}).json()
    c.post("/account/swap/make/funded", json={"id": q20})
    HELD[k20["give_address"]] = COIN
    st = c.post("/account/swap/make/step", json={"id": q20}).json()
    check(st.get("step") == "funded", "not placed before the tokens arrive")
    T20[k20["give_address"]] = 253
    st = c.post("/account/swap/make/step", json={"id": q20}).json()
    check(st.get("step") == "placed" and PLACED[-1]["give"]["kind"] == "token20" and PLACED[-1].get("maker_fee", {}).get("bps") == 100,
          f"placed, the fee for V1.0 to send in the token ({PLACED[-1].get('maker_fee')})")

    # ---- mine, and cancelling
    sw = c.get("/r/market/swaps").json()
    check(any(o.get("id") == "0a0b0c0d" for o in (sw.get("result") or {}).get("mine") or []), "the visitor's order shows as theirs")
    cid = c.post("/r/market/swap_cancel" + v, json={"order": "0a0b0c0d"}).json()["id"]
    r = c.post("/account/swap/make/cancel", json={"id": cid})
    check(r.status_code == 200 and RELEASED and RELEASED[-1]["order"] == "0a0b0c0d", f"cancelled: released in V1.0 ({r.json()})")
    check(app.state.page_send(cid)["status"] == "sent", "the cancel's question answered")
    print("\nALL OK" if not fails else f"\n{len(fails)} FAILED")
    return not fails


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
