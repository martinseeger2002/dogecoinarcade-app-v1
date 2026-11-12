"""Profiles and follows on the website (2026-11-12): a profile names an address on every coin, the one-time "update your
profile" card's routes, follows built for the browser to sign, the Feed's Following tab, and the Profiles app's page API.

Offline, with Arcade V1.0 stood in by the shapes of its `profile`, `follows` and `feed` answers (crates/arcaded/src/
api.rs, follows.rs).    .venv/bin/python tests/profiles_test.py
"""
import json
import os
import sys
import tempfile
import urllib.parse

sys.path.insert(0, os.environ.get("ARCADE_SRC", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "arcade-src")))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import exchange_test as X  # noqa: E402

from arcade.messaging import envelope as envelopelib  # noqa: E402
from arcade.messaging import feed as feedlib  # noqa: E402
from arcade.script import b58check_decode, b58check_encode  # noqa: E402
from arcadesite.v1 import V1, V1Error  # noqa: E402

fails = []
ME, FRIEND, OTHER = X.ME, X.MAKER, "nYRp3WvGwQ5hJ5vN6t7pyk1Ri3FSEiYKfz"
H = b58check_decode(ME)[1]
MAIN_H = bytes(range(20))
MAIN = b58check_encode(56, MAIN_H)
FRIEND_H = b58check_decode(FRIEND)[1]


def check(ok, m):
    print(("ok   " if ok else "FAIL ") + m)
    ok or fails.append(m)


LINKED = [{"chain": "main", "address": MAIN}, {"chain": "doge", "address": b58check_encode(30, MAIN_H)},
          {"chain": "ltc", "address": b58check_encode(48, MAIN_H)},
          {"chain": "shic", "address": "h160:" + FRIEND_H.hex()}]


def fake_cmd(self, _command, _timeout=None, **p):
    if _command == "chains.list":
        return [{"key": "peptest", "installed": True, "home": True, "indexes": ["ledger", "addresses"]},
                {"key": "pep", "installed": False, "borrowed": True, "indexes": []},
                {"key": "shic", "installed": False, "borrowed": True, "indexes": []},
                {"key": "ltc", "installed": True, "indexes": ["addresses"]}]
    if _command == "profile":
        who = p["who"]
        if who in ("@friend", FRIEND):
            return {"profile": {"address": FRIEND, "stated": True, "txid": "ab" * 32, "linked": LINKED, "tag": "friend"},
                    "follows": {"followers": 3, "following": 1}}
        if who in ("@me", ME):
            return {"profile": {"address": ME, "stated": True, "txid": "cd" * 32, "linked": LINKED[:3]},
                    "follows": {"followers": 0, "following": 1}}
        raise V1Error("no such profile")
    if _command == "follows":
        if p["who"] in (ME, "@me"):
            return {"address": ME, "followers": 0, "following": 1, "followers_list": [],
                    "following_list": [{"address": FRIEND, "tag": "friend"}]}
        return {"address": p["who"], "followers": 1, "following": 0, "followers_list": [{"address": ME, "tag": "me"}],
                "following_list": []}
    if _command == "feed":
        if p.get("author") == FRIEND:
            return {"posts": [{"txid": "01" * 32, "author": {"address": FRIEND}, "text": "older", "height": 10,
                               "age_hours": 5, "replies": []},
                              {"txid": "02" * 32, "author": {"address": FRIEND}, "text": "waiting", "height": None,
                               "age_hours": 0, "replies": []},
                              {"txid": "03" * 32, "author": {"address": FRIEND}, "text": "newer", "height": 12,
                               "age_hours": 1, "replies": []}], "total": 3}
        return {"posts": [], "total": 0}
    if _command == "explorer.tag":
        if p["tag"] == "friend":
            return {"home_address": FRIEND}
        raise V1Error("not claimed")
    return X.fake_cmd(self, _command, _timeout, **p)


def fake_page(self, path, params=None, timeout=None):
    if path.startswith("profile/"):
        return 200, {"tag": "friend", "pfp": "", "bio": "hello", "url": "", "picture": "", "content": ""}
    return X.fake_page(self, path, params, timeout)


def main():
    saved = (V1.page, V1.cmd, V1.core)
    V1.page, V1.cmd, V1.core = fake_page, fake_cmd, X.fake_core
    captured = {}
    real_announce = envelopelib.build_key_announcement

    def capture(*a, **k):
        captured.update(k, _args=a)
        return real_announce(*a, **k)
    envelopelib.build_key_announcement = capture
    try:
        home = tempfile.mkdtemp()
        json.dump({"v1": "http://127.0.0.1:1", "v1_home": home}, open(home + "/site.json", "w"))
        c, app = X.site(home, ME)
        app.state.chains.account_address = lambda pubkey, chain: {"test": ME, "main": MAIN}.get(chain.network, "")
        app.state.chains._kept = None
        # A real account files its coin key at sign-up: an announcement naming every coin is Class B, which needs it.
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives import serialization
        pub = ec.generate_private_key(ec.SECP256K1()).public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.CompressedPoint).hex()
        acct = "02" + "11" * 32
        app.state.site.set_setting(app.state.chains.coinkey_key(acct, app.state.chains.account_chain()), pub)

        # ---- 1. a profile names an address on every coin, Bitcoin left out
        r = c.post("/account/profile", json={"key": "ab" * 32, "bio": "hi"})
        check(r.status_code == 200 and r.json().get("offer"), f"/account/profile builds an offer ({r.status_code} "
                                                              f"{str(r.json())[:120]})")
        body = real_announce(*captured.get("_args", ()), **{k: v for k, v in captured.items() if k != "_args"}) \
            if captured else b""
        print(f"     (the announcement is {len(body)} bytes: {'one output' if envelopelib.announcement_fits_one_output(body) else 'Class B'})")
        nets = set((captured.get("chains") or {}).keys())
        want = {"ltc", "dgb", "kot", "flop", "wjk", "bel", "lky", "shic", "jkc", "arc", "ltctest", "dgbtest", "arctest"}
        check(nets == want, f"the announcement names every coin but Pepecoin, Dogecoin (their own places) and Bitcoin "
                            f"({sorted(nets)})")
        ch = captured.get("chains") or {}
        check(ch.get("shic") == MAIN_H and ch.get("arc") == MAIN_H and ch.get("jkc") == MAIN_H,
              "real coins carry the mainnet key's hash")
        check(ch.get("arctest") == H and ch.get("ltctest") == H, "test coins carry the testnet key's hash")
        check("btc" not in ch, "no Bitcoin yet")

        # ---- 2. the one-time card: what the published profile is missing, and "not now"
        m = c.get("/account/profile/missing").json()
        check(set(m["missing"]) == want - {"ltc"} and not m["skipped"] and "Shibacoin" in m["labels"]
              and "ARCADE" in m["labels"], f"missing: every coin the profile names no address on ({m})")
        c.post("/account/profile/skip")
        check(c.get("/account/profile/missing").json()["skipped"] is True, "not now: remembered for this list")

        # ---- 3. follows, built for the browser to sign
        r = c.post("/account/follow", json={"who": "@friend"})
        j = r.json()
        note = feedlib.build(feedlib.FOLLOW, FRIEND_H + bytes(12))
        check(r.status_code == 200 and note.hex() in j.get("raw", "") and j.get("on") is True and j.get("who") == FRIEND,
              f"follow @friend: an offer carrying act 11 on the friend's hash and 12 zero bytes ({r.status_code})")
        r = c.post("/account/follow", json={"who": FRIEND, "on": False})
        check(r.status_code == 200 and feedlib.build(feedlib.UNFOLLOW, FRIEND_H + bytes(12)).hex() in r.json()["raw"],
              "unfollow by address: act 12")
        check(c.post("/account/follow", json={"who": ME}).status_code == 400, "following yourself is refused")
        check(c.post("/account/follow", json={"who": "@nobody"}).status_code == 400, "an unclaimed name is refused")
        check(feedlib.NAMES[11] == "follow" and feedlib.NAMES[12] == "unfollow", "the feed knows the two kinds")

        # ---- 5. the Feed's Following tab
        r = c.get("/feed?sort=following")
        at = [r.text.find(t * 32) for t in ("02", "03", "01")]
        check(r.status_code == 200 and "Following" in r.text and -1 < at[0] < at[1] < at[2],
              f"Following tab: the followed account's posts, waiting first, then newest ({at})")

        # ---- 4. the Profiles app's page API
        src = app.state.frame_src(None, X.PIECE + "i0")
        v = "?v=" + urllib.parse.parse_qs(urllib.parse.urlparse(src).query)["v"][0]
        f = c.get("/r/follows/@friend").json()
        check(f.get("followers") == 1 and f["followers_list"][0]["address"] == ME, f"/r/follows/@friend ({f})")
        p = c.get("/r/profile/friend").json()
        check(p.get("bio") == "hello" and p["follows"] == {"followers": 3, "following": 1} and len(p["linked"]) == 4,
              f"/r/profile/friend: the original's answer plus follows and linked ({p})")
        ff = c.get("/r/feed/following" + v).json()
        check([x["text"] for x in ff["posts"]] == ["waiting", "newer", "older"], f"/r/feed/following ({ff})")
        check(c.post("/r/follow", json={"who": "@friend"}).status_code == 403, "a write without a reader is refused")
        r = c.post("/r/follow" + v, json={"who": "@friend"})
        check(r.status_code == 202, f"/r/follow files a request ({r.status_code} {r.text[:120]})")
        r = c.post("/r/profile/publish" + v, json={"bio": "new line"})
        check(r.status_code == 202, "/r/profile/publish files a request")
        rows = c.get("/account/pagesends").json()["requests"]
        kinds = {(x["kind"], json.dumps(x.get("args"), sort_keys=True)) for x in rows}
        check(("follow", json.dumps({"on": True, "who": "@friend"}, sort_keys=True)) in kinds
              and ("profile", json.dumps({"bio": "new line"}, sort_keys=True)) in kinds,
              f"the reader's card is asked both ({kinds})")

        # ---- send any coin to a profile: @name on another coin
        r = c.post("/r/send" + v, json={"kind": "coins", "chain": "shic", "to": "@friend", "amount": "1"})
        sent = r.json()
        shic_addr = b58check_encode(63, FRIEND_H)
        row = app.state.page_send(sent.get("id", ""))
        check(r.status_code == 202 and row and row["to_address"] == shic_addr,
              f"@friend on Shibacoin pays the address their profile gives ({row and row['to_address']})")
        r = c.post("/r/send" + v, json={"kind": "coins", "chain": "ltc", "to": "@me", "amount": "1"})
        check(r.status_code == 202, "@me on Litecoin: linked address")
        r = c.post("/r/send" + v, json={"kind": "coins", "chain": "shic", "to": "@me", "amount": "1"})
        check(r.status_code == 400 and "Shibacoin" in r.json().get("error", ""),
              "a profile with no Shibacoin address: refused, saying so")
    finally:
        V1.page, V1.cmd, V1.core = saved
        envelopelib.build_key_announcement = real_announce
    print("ALL OK" if not fails else f"{len(fails)} FAILED")
    return not fails


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
