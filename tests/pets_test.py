"""Pets for games (arcadesite/pets.py): /r/doginals/<address> and /r/collection-item/doge/<id>, with Arcade V1.0's
explorer.held and doggy.market stood in.    .venv/bin/python tests/pets_test.py
"""
import json
import os
import sys
import tempfile
import urllib.error

sys.path.insert(0, os.environ.get("ARCADE_SRC", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "arcade-src")))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import exchange_test as X  # noqa: E402

from arcadesite import pets  # noqa: E402
from arcadesite.v1 import V1, V1Error  # noqa: E402

fails = []


def check(ok, m):
    print(("ok   " if ok else "FAIL ") + m)
    ok or fails.append(m)


from arcade.script import b58check_encode  # noqa: E402
DOGE = b58check_encode(30, bytes(range(1, 21)))           # a made-up Dogecoin address
DOGE2 = b58check_encode(30, bytes(range(2, 22)))          # another, never asked about
DOG = "39f7874c6bc2e7f0f6f70f49e03fc1756bd20d9e32e90ee31a81dd6f3dd58e96i0"
SOLD = "a8cd241e7c88126c335c7da4dfd7721cec2884fc1fdb23b445942dd1fa61f5b8i0"     # Doginal Dog #2608, held elsewhere
FROG = "682347bd1a6541915ed38ffdfaf47c462457a21845ebda406913545cf6c46fbei0"
CULT = "23b553ab2ab921c5429134d0c839c21e9abe83ed70246422943d53b1e3810fb9i0"     # Doginal Frog #10001
MISLABEL = "cbd2d8d7e8ca4011d77b5cb7b5e20f41f8e6ef8a99831edad8ca471ea7a8b06di0"  # Doginal Dog #3079
ADD = "92ff2ef9c64031532d72d9b58c5c286b6902a2ca546c1212b7cc0e0133633949i0"   # Anti Doginal Dog #165
DUP = "99f4afee" + "11" * 28 + "i0"
OTHER = "cd" * 32 + "i0"
HELD = [DOG, FROG, CULT, ADD, OTHER]
index_down = []


def fake_cmd(self, _command, _timeout=None, **p):
    if _command == "chains.list":
        return [{"key": "peptest", "installed": True, "home": True, "indexes": ["ledger", "addresses"]},
                {"key": "doge", "installed": True, "indexes": ["addresses", "ord"]}]
    if _command == "explorer.held":
        if index_down:
            raise V1Error("down")
        if p["address"] != DOGE:
            return {"items": [], "total": 0}
        return {"items": [{"id": i} for i in HELD[p["offset"]:p["offset"] + p["limit"]]], "total": len(HELD)}
    return X.fake_cmd(self, _command, _timeout, **p)


def main():
    saved = V1.cmd
    V1.cmd = fake_cmd
    asked = []
    real_urlopen = __import__("urllib.request").request.urlopen
    __import__("urllib.request").request.urlopen = lambda *a, **k: asked.append(a) or real_urlopen(*a, **k)
    try:
        home = tempfile.mkdtemp()
        json.dump({"v1": "http://127.0.0.1:1", "v1_home": home}, open(home + "/site.json", "w"))
        c, app = X.site(home, X.ME)
        chains = app.state.chains
        chains._kept = None
        chains.account_address = lambda pubkey, chain: {"test": X.ME, "doge": DOGE}.get(chain.network, "")
        pub = "02" + "33" * 32
        app.state.site.set_setting(chains.address_key(pub, chains.account_chain()), X.ME)
        app.state.site.set_setting(chains.address_key(pub, chains.by_network("doge")), DOGE)

        # ---- 1. what an account holds, asked with its Pepecoin testnet address
        r = c.get(f"/r/doginals/{X.ME}")
        j = r.json()
        got = {(x["collection"]["name"], x["number"], x["inscription"]) for x in j.get("items", [])}
        check(r.status_code == 200 and j.get("address") == DOGE, f"the testnet address reads the account's Dogecoin "
                                                                 f"address ({j.get('address')})")
        check(r.headers.get("access-control-allow-origin") == "*", "open to sandboxed game frames (CORS)")
        check(("Doginal Dogs", 165, DOG) in got and ("Doginal Frogs", 9619, FROG) in got
              and ("Doginal Frogs", 10001, CULT) in got, f"its Doginal Dog and Doginal Frogs ({sorted(got)})")
        check(("Anti Doginal Dogs", 165, ADD) in got, "its Anti Doginal Dog, from the collection's own list")
        check(not any(i == SOLD for _, _, i in got), "one the index says it no longer holds is left out")
        check(not any(i == OTHER for _, _, i in got) and len(got) == 4, "nothing else")

        # ---- 2. asked again: kept for 5 minutes
        index_down.append(1)
        check(c.get(f"/r/doginals/{X.ME}").status_code == 200, "a second ask within 5 minutes is answered from what was kept")
        j = c.get(f"/r/doginals/{DOGE}").json()
        check(len(j["items"]) == 4, "a Dogecoin address is read as it is")
        j = c.get("/r/doginals/nYRp3WvGwQ5hJ5vN6t7pyk1Ri3FSEiYKfz").json()
        check(j["items"] == [] and "no Dogecoin address" in j.get("note", ""), "an address no account filed: none")
        index_down.clear()

        # ---- 3. one item: number and traits from the bundled lists
        r = c.get(f"/r/collection-item/doge/{DOG}")
        it = r.json()
        check(r.status_code == 200 and it["number"] == 165 and it["collection"]["name"] == "Doginal Dogs"
              and it["traits"]["Head"] == "Wizard" and it["traits"]["Fur Pattern"] == "Husky",
              f"a Doginal Dog's record ({it})")
        it = c.get(f"/r/collection-item/doge/{FROG}").json()
        check(it["number"] == 9619 and it["traits"]["Body"] == "Red Pepe" and "Special" not in it["traits"],
              f"a Doginal Frog's record, no empty traits ({it['traits']})")
        it = c.get(f"/r/collection-item/doge/{CULT}").json()
        check(it["traits"]["Body"] == "Zombie Pepe (Purple)" and it["traits"]["Special"] == "Cult of 69",
              "Doginal Frog #10001: its picture's traits, not #69's copy")
        it = c.get(f"/r/collection-item/doge/{MISLABEL}").json()
        check(it["number"] == 3079, "Doginal Dog #3079, listed by the marketplace as #3099")
        r = c.get(f"/r/collection-item/doge/{OTHER}")
        check(r.status_code == 404, "an inscription in no pet collection: 404")
        check(c.get(f"/r/collection-item/doge/{ADD}").status_code == 404, "an Anti Doginal Dog names its own dog in its page")
        check(c.get(f"/r/collection-item/pep/{DOG}").status_code == 404, "only Dogecoin")
        check(f"{DUP[:8]}" not in json.dumps(sorted(pets.json.loads(open(os.path.join(os.path.dirname(pets.__file__),
              "data", "anti_doginal_dogs.json")).read())["ids"])), "the second copy of ADD #8203 is not in the list")
        dogs, frogs = pets.load()[0]
        check(len(dogs.items) == 10000 and len({r[0] for r in dogs.items.values()}) == 10000, "10,000 Doginal Dogs, one each")
        check(len(frogs.items) == 10066 and len({r[0] for r in frogs.items.values()}) == 10066
              and not {69, 6969, 10000} & {r[0] for r in frogs.items.values()}, "10,066 Doginal Frogs, one each")

        # ---- 4. the index cannot say: 503, not an empty pocket
        index_down.append(1)
        r = c.get("/r/doginals/" + DOGE2)
        check(r.status_code == 503 and r.headers.get("access-control-allow-origin") == "*", "index down: 503")
        index_down.clear()
        check(not asked, "nothing outside this arcade is asked")
    finally:
        V1.cmd = saved
        __import__("urllib.request").request.urlopen = real_urlopen
    print("ALL OK" if not fails else f"{len(fails)} FAILED")
    return not fails


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
