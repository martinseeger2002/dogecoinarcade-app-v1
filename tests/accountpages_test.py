"""Account, profile and info pages (arcadesite/accountpages.py): the pure helpers offline, then every page and JSON
route against the dev V1.0 (site.json of $ARCADE_SITE_DEV (default ~/.arcade-site-dev), a scratch home so nothing is written there).

    .venv/bin/python tests/accountpages_test.py
"""
import json
import os
import re
import shutil
import sys
import tempfile
import types
import urllib.parse

sys.path.insert(0, os.environ.get("ARCADE_SRC", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "arcade-src")))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fastapi.testclient import TestClient

from arcadesite import accountpages as A
from arcadesite.app import create_app

fails = []


def check(ok, m):
    print(("ok   " if ok else "FAIL ") + m)
    ok or fails.append(m)


# ---------------------------------------------------------------- offline: the original index's answers, rebuilt
rows = [{"txid": f"{i:064x}", "number": i, "content_type": ct, "content_len": 1, "block_height": 10 + i,
         "collection": coll, "edition": ed, "creator": "nX", "name": name}
        for i, ct, coll, ed, name in ((1, "image/png", "Frogs", 2, "Frog #2"), (2, "image/png", "Frogs", 1, "Frog #1"),
                                      (3, "text/html", "", None, ""), (4, "application/vnd.arcade.save", "", None, ""),
                                      (5, "image/webp", "Frogs", 3, "Big Frog"))]
g = A._held_collections(rows)
frogs = next(x for x in g if x["collection"] == "Frogs")
check(frogs["count"] == 3 and frogs["latest"] == 15 and frogs["cover"][0]["txid"] == f"{5:064x}",
      "held_collections: count, latest, newest picture first")
check(next(x for x in g if x["collection"] == "")["count"] == 1, "held_collections: machine pieces left out")
page, total = A._held_pieces(rows, "Frogs")
check(total == 3 and [p["edition"] for p in page] == [1, 2, 3], "held_pieces: edition order")
check(A._held_pieces(rows, "Frogs", query="big")[1] == 1 and A._held_pieces(rows, "Frogs", query="#2")[1] == 2,
      "held_pieces: by name and by #edition")
check(A._held_pieces(rows, "Frogs", sort="recent")[0][0]["number"] == 5, "held_pieces: recent first")
c = A._collections_held(rows)
check(c[-1]["collection"] == "" and c[0]["covers"][0] == f"{5:064x}", "collections_held: loose last, covers")
check(A._home_pages(["tab:/feed", "store"]) == [["tab:/feed", "store"]], "home pages: a flat list is one page")
check(A._home_pages([["tab:/feed", "tab:/feed"]]) is None, "home pages: an icon once")
check(A._home_pages([[{"id": "f1", "folder": "  Games ", "apps": ["store"]}]])[0][0]["folder"] == "Games",
      "home pages: a folder")
check(A._home_dock(["store"] * 7) is None and A._home_dock(["store", "store"]) == ["store"], "home dock")

# ---------------------------------------------------------------- live, against the dev V1.0
home = tempfile.mkdtemp(prefix="acctpages-")
shutil.copy(os.path.join(os.path.expanduser(os.environ.get("ARCADE_SITE_DEV", "~/.arcade-site-dev")), "site.json"), home + "/site.json")
app = create_app(home)
# sidecar.py answers /account/nfts empty: this module's own answer replaces it (as the integrator wires it).
app.router.routes = [r for r in app.router.routes if getattr(r, "path", "") != "/account/nfts"]
st, chains = app.state.site, app.state.chains
A.register(app, st, chains)
client = TestClient(app)
BAD = re.compile(r"Traceback|jinja2\.|UndefinedError|Internal Server Error|is undefined")


def page(path, who="stranger", want=200, **kw):
    r = client.get(path, follow_redirects=False, **kw)
    ok = r.status_code == want and not (want == 200 and BAD.search(r.text))
    check(ok, f"{who}: GET {path} -> {r.status_code}" + ("" if ok else f" {r.text[:300]!r}"))
    return r


ASHVALE = "nmrRmZASYVZXA7hbzxXY4J3BYTPKgfea9c"
for p in ("/me/contacts", "/me/access", "/me/backup", "/me/wallet/tokens", "/me/nfts", "/me/notifications"):
    r = page(p, want=303)
    check(r.headers.get("location", "").startswith("/join"), f"stranger: {p} asks to sign in")
check(client.get("/account/tokens").status_code in (401, 403), "stranger: /account/tokens refused")
page("/docs")
r = page("/docs/features.md")
check("<h" in r.text, "docs: a document drawn")
check(page("/docs/no-such.md", want=303).headers["location"] == "/docs", "docs: no such document -> /docs")
check(page("/docs/multi-user.md", want=303).status_code == 303, "docs: a plan is not for a public site")
check(page("/guide", want=307).headers["location"] == "/docs/features.md", "guide -> features")
page("/affiliates")
j = client.get("/affiliates.json").json()
check(j.get("ok") is True and isinstance(j.get("sites"), list), "affiliates.json shape")
page("/terms", want=404)
page("/terms/left")
page("/instances")
page("/clone")
r = client.get("/globe")
check(r.status_code == 200 and "where the nodes are" in r.text and 'script nonce="' in r.text, "globe: the page")
j = client.get("/globe.json").json()
check(j.get("ok") and isinstance(j.get("members"), list) and j["chains"] >= 1,
      f"globe.json: {len(j.get('members') or [])} members over {j.get('chains')} chains")
check(page("/home", want=303).headers["location"] == "/join?next=/home", "home: the installed app signs in first")
page("/home?tab=1")
r = page("/desktop")
check("var APPS = [" in r.text, "desktop: the apps drawn")
check(client.get("/account/homeapps").json() == {"pages": None, "starred": [], "saved": False}, "homeapps: stranger")
check(client.get("/account/deskbg").json() == {"id": "", "fit": "fill"}, "deskbg: stranger")
check(client.get("/account/badges").json()["/me/notifications"] == 0, "badges: stranger")
r = page("/u/ashvale")
check(ASHVALE in r.text and "ASHVALE" in r.text, "profile: @ashvale's posts and header")
page("/u/dondogecon")
r = page("/u/zz-nobody-here")
r = page("/u/ashvale/wallet")
check("ASHVALE GOLD" in r.text, "profile wallet: tokens listed")
r = client.get("/u/ashvale/pieces?collection=" + urllib.parse.quote("ASHVALE Items"))
j = r.json()
check(r.status_code == 200 and len(j["pieces"]) >= 1 and j["more"] is False, f"profile pieces: {len(j['pieces'])}")
check(client.get("/u/zz-nobody-here/pieces").status_code == 404, "profile pieces: nobody -> 404")
page("/u/dondogecon/wallet")
posts = (st.v1.cmd("feed", order="new", limit=5) or {}).get("posts") or []
if posts:
    r = page(f"/feed/{posts[0]['txid']}/tip")
    check(posts[0]["txid"] in r.text, "tip: the post's form")
check(page("/feed/" + "00" * 32 + "/tip", want=303).headers["location"] == "/feed", "tip: nothing there -> /feed")

# Signed in, as an account holding @ashvale's address (pieces and tokens).
account = types.SimpleNamespace(pubkey="02" + "33" * 32, created=0, seen=0, released=None)
app.state.signed_in = lambda request: account
app.state.signed_in_account = lambda request: account
chains.account_address = lambda pubkey, chain: ASHVALE if chain.network == "test" else ""
for p in ("/me/contacts", "/me/access", "/me/backup", "/me/wallet/tokens", "/me/nfts", "/me/notifications",
          "/me/notifications?page=2", "/home", "/desktop", "/u/ashvale"):
    page(p, "account")
j = client.get("/account/tokens").json()
test = next((c for c in j["chains"] if c["network"] == "test"), None)
gold = next((t for t in (test or {}).get("tokens", []) if t["name"] == "ASHVALE GOLD"), None)
check(bool(gold) and gold["balance"] > 0 and gold["display"] and gold["issuer_is_me"],
      f"account tokens: ASHVALE GOLD held and issued ({gold and gold['display']})")
check(any(t["balance"] == 0 and t["issuer_is_me"] for t in test["tokens"]) if test else False,
      "account tokens: an issued token held at zero is listed")
j = client.get("/account/nfts").json()
test = next((c for c in j["chains"] if c["network"] == "test"), None)
check(bool(test) and len(test["pieces"]) == 200 and {"txid", "number", "content_type", "sale", "listing"}
      <= set(test["pieces"][0]), "account nfts: 200 pieces in the original's shape")
j = client.get("/account/nfts/collections?chain=test").json()
items = next((x for x in j["collections"] if x["collection"] == "ASHVALE Items"), None)
check(bool(items) and items["count"] >= 1 and "cover" in items, "account nfts collections: ASHVALE Items")
j = client.get("/account/nfts/pieces?chain=test&collection=" + urllib.parse.quote("ASHVALE Items")).json()
check(j["total"] == items["count"] and len(j["pieces"]) == min(60, j["total"]), "account nfts pieces: a page")
check(client.get("/account/nfts/collections?chain=nope").status_code == 404, "account nfts: an unknown chain")
j = client.get("/account/claimpools?chain=test").json()
check(j == {"chain": "test", "pools": [], "setup": [], "next_index": 1}, "claimpools: none (NEEDS-V1)")
j = client.get("/account/ring/apps").json()
check(set(j) == {"apps", "allowed", "push", "devices"}, "ring apps shape")
j = client.get("/account/badges").json()
check(set(j) >= {"/me/notifications", "/me/messages"}, f"badges: {j}")
r = client.post("/account/homeapps", json={"pages": [["tab:/feed", "store"]], "dock": ["store"]})
check(r.status_code == 200 and client.get("/account/homeapps").json()["pages"] == [["tab:/feed", "store"]],
      "homeapps: kept")
check(client.post("/account/homeapps", json={"pages": [["bad key"]]}).status_code == 400, "homeapps: refused")
r = client.post("/account/deskbg", json={"id": "ab" * 32 + "i0", "fit": "tile"})
check(r.status_code == 200 and client.get("/account/deskbg").json() == {"id": "ab" * 32 + "i0", "fit": "tile"},
      "deskbg: kept")
check(client.post("/account/mainnet", json={}).status_code == 400, "mainnet: no words, no mark")
check(client.post("/account/mainnet", json={"words_match": True}).json()["backed_up"] is True
      and st.setting(f"mainnet:{account.pubkey}") == "yes", "mainnet: marked")
r = page("/me/backup", "account")
st.set_setting("terms_gate", True)
r = page("/terms?back=/feed", "account")
check("version" in r.text or "accept" in r.text.lower(), "terms: drawn when the switch is on")
shutil.rmtree(home, ignore_errors=True)
print("ALL OK" if not fails else f"{len(fails)} FAILED")
sys.exit(1 if fails else 0)
