"""Read-only page-API doors for apps on this website (arcadesite/feedweb.py, arcadesite/svcweb.py):
arcade.feed.read/channels/file over GET /r/feed*, and arcade.svc.ask over POST /r/svc/ask (free reads only, as each
service's offer names them in "reads", asked in this website's name, capped). Offline, V1.0 stood in.

    .venv/bin/python tests/appdoors_test.py
"""
import base64
import json
import os
import sys
import tempfile

sys.path.insert(0, os.environ.get("ARCADE_SRC", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "arcade-src")))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fastapi.testclient import TestClient  # noqa: E402

from arcadesite.app import create_app  # noqa: E402
from arcadesite.v1 import V1, V1Error  # noqa: E402

HOST = "ab" * 32       # a service that names its free reads
PAID = "cd" * 32       # a service that names none
fails = []
calls = []


def check(ok, m):
    print(("ok   " if ok else "FAIL ") + m)
    ok or fails.append(m)


POSTS = [{"txid": f"{i:064x}", "text": f"post {i}", "author": {"tag": "someone", "address": "nX"}, "likes": i,
          "replies": [], "height": 100 + i} for i in range(5)]
BIG = {"on": False}


def fake_cmd(self, _command, _timeout=None, **p):
    calls.append((_command, p))
    if _command == "feed":
        return {"posts": [x for x in POSTS if p.get("channel") in (None, "clips")][: p["limit"]], "total": 5}
    if _command == "feed.channels":
        return [{"channel": "clips", "posts": 5}]
    if _command == "feed.file":
        return {"type": "image/png", "data": "iVBORw0KGgo="}
    if _command == "status":
        return {"node_id": "ee" * 32}
    if _command == "svc.offers":
        return [{"service": "clip-host", "arcade": HOST, "offer": {"reads": ["get", "has"], "price": 1}},
                {"service": "clip-host", "arcade": PAID, "offer": {"price": 1}}]
    if _command == "svc.ask":
        if p["body"].get("what") == "get":
            n = (5 << 20) if BIG["on"] else 1000
            return {"last": {"ok": True, "len": n}, "answers": [{"ev": "piece", "blob": base64.b64encode(b"x" * n).decode()}]}
        return {"last": {"have": p["body"].get("shas") or []}, "answers": []}
    raise V1Error(f"unknown method {_command}")


V1.cmd = fake_cmd
home = tempfile.mkdtemp()
json.dump({"v1": "http://127.0.0.1:1", "v1_home": home}, open(home + "/site.json", "w"))
app = create_app(home)
app.state.signed_in = lambda request: None
c = TestClient(app)

print("arcade.feed")
r = c.get("/r/feed.js")
check(r.status_code == 200 and "arcade.feed" in r.text, "the shim is served")
r = c.get("/r/feed?channel=clips&order=new&limit=3")
j = r.json()
check(r.status_code == 200 and j["ok"] and len(j["result"]["posts"]) == 3 and j["result"]["total"] == 5,
      f"a channel's posts, limited ({r.status_code} {str(j)[:120]})")
sent = [p for cmd, p in calls if cmd == "feed"][-1]
check(sent == {"order": "new", "limit": 3, "offset": 0, "channel": "clips"}, f"V1 asked as the app window asks ({sent})")
n = len(calls)
c.get("/r/feed?channel=clips&order=new&limit=3")
check(len(calls) == n, "the same page again is answered from what was kept")
c.get("/r/feed?order=friends&limit=999")
sent = [p for cmd, p in calls if cmd == "feed"][-1]
check(sent["order"] == "popular" and sent["limit"] == 100, f"no reader-only orders, limit capped ({sent})")
check(c.get("/r/feed?tag=no%20way").status_code == 400, "a bad tag is refused")
check(c.get("/r/feed/channels").json()["result"] == [{"channel": "clips", "posts": 5}], "channels")
check(c.get("/r/feed/file?txid=" + "1" * 64).json()["result"]["type"] == "image/png", "a post's picture")
check(c.get("/r/feed/file?txid=nope").status_code == 400, "a bad txid is refused")

print("arcade.svc.ask")
r = c.get("/r/svc.js")
check(r.status_code == 200 and "/r/svc/ask" in r.text, "the shim asks /r/svc/ask")
ask = lambda to, body: c.post("/r/svc/ask", json={"to": to, "service": "clip-host", "body": body})  # noqa: E731
r = ask(HOST, {"what": "get", "sha": "aa" * 32, "at": 0, "len": 1000})
j = r.json()
check(r.status_code == 200 and j["ok"] and base64.b64decode(j["answers"][0]["blob"]) == b"x" * 1000,
      f"a free read passes, its blob back ({r.status_code} {str(j)[:100]})")
sent = [p for cmd, p in calls if cmd == "svc.ask"][-1]
check(sent["to"] == HOST and sent["service"] == "clip-host" and sent["timeout"] == 60, f"asked through V1 ({sent})")
r = ask(HOST, {"what": "buy"})
check(r.status_code == 403 and "free reads" in r.json()["error"], f"an ask the offer doesn't name is refused ({r.text[:120]})")
r = ask(PAID, {"what": "get"})
check(r.status_code == 403, "a service that names no free reads gets none")
check(ask("ff" * 32, {"what": "get"}).status_code == 404, "no offer from that arcade")
check(ask("nothex", {"what": "get"}).status_code == 400, "a bad node id")
check(ask(HOST, {"what": "has", "shas": ["a" * 64] * 400}).status_code == 413, "a body over 16 KB")
BIG["on"] = True
r = ask(HOST, {"what": "get", "sha": "aa" * 32, "at": 0, "len": 5 << 20})
check(r.status_code == 413, f"an answer over 4 MB is refused ({r.status_code})")
BIG["on"] = False
codes = [ask(HOST, {"what": "has", "shas": []}).status_code for _ in range(125)]
check(codes.count(429) > 0 and codes[0] == 200, f"a visitor's asks a minute are capped ({codes.count(200)} ok, {codes.count(429)} refused)")

print("ALL OK" if not fails else f"{len(fails)} FAILED")
sys.exit(1 if fails else 0)
