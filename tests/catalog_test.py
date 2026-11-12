"""The public catalog pages (arcadesite/catalog.py): shapes offline, then every page live against the dev home
($ARCADE_SITE_DEV, default ~/.arcade-site-dev: a dev home whose site.json names a V1.0), as a stranger and as a signed-in account.

    .venv/bin/python tests/catalog_test.py
"""
import os
import re
import sys
import types
import urllib.parse

sys.path.insert(0, os.environ.get("ARCADE_SRC", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "arcade-src")))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fastapi.testclient import TestClient

from arcadesite import catalog
from arcadesite.app import create_app

fails = []


def check(ok, m):
    print(("ok   " if ok else "FAIL ") + m)
    ok or fails.append(m)


# ---------------------------------------------------------------- offline: the shapes
r = catalog._row({"id": "ab" * 32, "number": 7, "creator": "nA", "owner": "nB", "block": 5, "contenttype": "image/png",
                  "length": 10, "sha256": "ff", "transactions": 2, "held": True, "json": {"name": "x"},
                  "rawjson": '{"name":"x"}', "collection": "C", "edition": 3})
check(r["txid"] == "ab" * 32 and r["block_height"] == 5 and r["content_type"] == "image/png" and r["content_len"] == 10
      and r["chunks"] == 2 and r["json"] == '{"name":"x"}' and r["edition"] == 3, "describe() -> index row")
c = catalog._collection({"creator": "nA", "name": "Set", "count": 4, "firstnumber": 1, "lastnumber": 9,
                         "firstedition": 1, "lastedition": 4, "cover": "cd" * 32, "covertype": "image/png",
                         "supply": None})
check(c["collection"] == "Set" and c["cover_txid"] == "cd" * 32 and c["first_number"] == 1, "describe_collection -> row")
p = catalog._prop({"id": 121, "type": 2, "total": 100000000000, "holders": 1, "issuer": "nX", "name": "Demo",
                   "managed": 0, "ecosystem": 1, "creation_block": 5, "creation_txid": "aa"})
from arcade.ledger import format_amount
check(p["divisible"] and p["total_display"] == format_amount(100000000000, True) and p["holder_count"] == 1 and not p["managed"],
      "ledger token -> property row")
check(catalog._shown_units("40000", False) == (40000, "40,000") and catalog._shown_units("1.5", True)[0] == 150000000,
      "shown balances -> units")
check(catalog._count({"count": 3}) == 3 and catalog._count(7) == 7, "count in both shapes")

# ---------------------------------------------------------------- live
app = create_app(os.path.expanduser(os.environ.get("ARCADE_SITE_DEV", "~/.arcade-site-dev")))
before = len(app.router.routes)
catalog.register(app, app.state.site, app.state.chains)
# The integrator registers catalog before apps' /r/{rest:path} catch-all; here its routes are moved to the front.
app.router.routes[:] = app.router.routes[before:] + app.router.routes[:before]
client = TestClient(app, follow_redirects=False)
v1 = app.state.site.v1

BAD = re.compile(r"Traceback|jinja2\.|UndefinedError|Internal Server Error")


def page(path, who="stranger", want=200):
    got = client.get(path)
    ok = got.status_code == want and not BAD.search(got.text)
    check(ok, f"{who}: GET {path} -> {got.status_code}" + ("" if ok else f" {got.text[:300]}"))
    return got


def run(who):
    t = page("/tokens", who)
    check("Demo Bucks" in t.text or "#121" in t.text or "tokens/" in t.text, f"{who}: /tokens lists ledger tokens")
    page("/tokens?page=2", who)
    tok = page("/tokens/121", who)
    check("Demo Bucks" in tok.text and "Holders" in tok.text, f"{who}: /tokens/121 names it and its holders")
    page("/tokens/20", who)
    page("/tokens/999999", who, want=404)
    n = page("/nfts", who)
    check("/inscriptions/" in n.text and "/view" in n.text, f"{who}: /nfts lists pieces")
    page("/nfts?page=3", who)
    check(client.get("/inscriptions?page=2").headers.get("location") == "/nfts?page=2", f"{who}: /inscriptions -> /nfts")
    check(client.get("/inscriptions/6926").status_code == 307, f"{who}: bare piece -> /view")
    page("/inscriptions/not-a-key", who, want=404)
    img = page("/inscriptions/6926/view", who)
    check("Master Set" in img.text or "/content/01162a7c" in img.text, f"{who}: an image piece's page")
    page("/inscriptions/" + "01162a7cdf098924977647b22b0e76e42326680be714317011526802e5ddcced" + "/view", who)
    check(client.get("/inscriptions/" + "00" * 32 + "/view").status_code == 303, f"{who}: a missing piece -> redirect")
    co = page("/collections", who)
    check("/collections/" in co.text, f"{who}: /collections lists sets")
    one = page("/collections/nmrRmZASYVZXA7hbzxXY4J3BYTPKgfea9c/" + urllib.parse.quote("ASHVALE Saltmere Links"), who)
    check("Saltmere" in one.text and "Traits" in one.text, f"{who}: a collection with its traits")
    check(client.get("/collections/nX/none").status_code == 303, f"{who}: a missing collection -> /collections")
    check(client.get("/launches").headers.get("location") == "/exchange?tab=mintpads", f"{who}: /launches -> mintpads")
    page("/launches/16d9d0b814e1ddfefe95db1c46cb10310b4d4b9e0357d460968538d67c2f2e56", who)   # Demo Bucks' creation
    page("/launches/78b347e0489b31cf66540c6e611263022ad5b01eca6bd90bb3db39ac8e8b8cf4", who)   # Saltmere Links #1
    page("/launches/" + "00" * 32, who, want=404)
    mp = page("/mintpad/@ashvale/" + urllib.parse.quote("ASHVALE Saltmere Links"), who)
    check("Saltmere" in mp.text, f"{who}: a mintpad by @tag")
    j = client.get("/r/mintpad/nX/" + urllib.parse.quote("ASHVALE Saltmere Links")).json()
    check(j == catalog.EMPTY_PAD, f"{who}: /r/mintpad answers the empty shape")


# An HTML piece: its frame is frame_src (pages host + /content/<id>, + ?v=<ticket> when signed in).
code, html_rows = v1.page("inscriptions", {"limit": 100})
html_piece = next((x["id"] for x in html_rows if str(x.get("contenttype") or "").startswith("text/html")), None)

run("stranger")
if html_piece:
    got = page(f"/inscriptions/{html_piece}/view")
    src = re.search(r'class="inscription-frame" src="([^"]+)"', got.text)
    check(bool(src) and src.group(1).endswith(f"/content/{html_piece}") and "?v=" not in src.group(1),
          f"stranger: HTML frame src is the sandbox address, no ticket ({src.group(1) if src else None})")

# Signed in: an account whose address holds pieces and tokens (@ashvale's).
HOME = "nmrRmZASYVZXA7hbzxXY4J3BYTPKgfea9c"
account = types.SimpleNamespace(pubkey="02" + "11" * 32)
app.state.signed_in = lambda request: account
app.state.chains.account_address = lambda pubkey, chain: HOME
run("account")
tk = client.get("/tokens").text
check("Issued by" in tk and HOME in tk, "account: /tokens offers the account's create form")
nf = client.get("/nfts").text
check('id="trouble"' in nf, "account: /nfts offers the account's inscribe panels")
if html_piece:
    got = page(f"/inscriptions/{html_piece}/view", "account")
    src = re.search(r'class="inscription-frame" src="([^"]+)"', got.text)
    check(bool(src) and f"/content/{html_piece}?v=" in src.group(1),
          f"account: HTML frame src carries the reader's ticket ({src.group(1)[:90] if src else None})")
mine = client.get("/mintpad/" + HOME + "/" + urllib.parse.quote("ASHVALE Saltmere Links"))
check(mine.status_code == 200 and not BAD.search(mine.text), "account: its own mintpad (seller view)")

print("ALL OK" if not fails else f"{len(fails)} FAILED")
sys.exit(1 if fails else 0)
