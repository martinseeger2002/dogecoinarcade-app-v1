"""Explore's collections over V1.0's explorer.collections / collection / collection_of, with V1.0 stood in by the
shapes the ArcadeCoin session settled (2026-11-11). Offline."""
import os, sys, tempfile, json
sys.path.insert(0, os.environ.get("ARCADE_SRC", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "arcade-src")))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fastapi.testclient import TestClient
from arcadesite.app import create_app
from arcadesite.v1 import V1, V1Error

ROOT = "ab" * 32
ROW = {"kind": "registry", "id": ROOT + "i0", "name": "Test Frogs", "chain": "test", "supply": 3, "count": 3,
       "missing": 0, "cover": "cd" * 32 + "i0", "description": "frogs", "traits": ["Hat"], "publisher": "nX",
       "height": 100, "number": 7, "status": "active", "reason": "", "replaces": ""}
def fake_cmd(self, _command, _timeout=None, **p):
    if _command == "chains.list":
        return [{"key": "peptest", "installed": True, "home": True, "ord": True, "indexes": ["ledger"]}]
    if _command == "explorer.collections":
        return {"chain": "peptest", "total": 2, "offset": 0, "n": 30, "reading": False, "note": "",
                "items": [{"kind": "sub1k", "id": "sub1k", "name": "Sub 1K"}, ROW]}
    if _command == "explorer.collection":
        assert p["id"] == ROOT + "i0"
        return {**ROW, "total": 3, "offset": p.get("offset", 0),
                "items": [{"number": k, "id": f"{k:064x}i0", "inscription": f"{k:064x}i0", "n": 100 + k, "h": 5,
                           "ct": "image/png", "traits": {"Hat": "red"}, "content": {}} for k in (1, 2, 3)]}
    if _command == "explorer.collection_of":
        return {"id": p["id"], "collections": [{"kind": "sub1k", "id": "sub1k", "name": "Sub 1K", "number": 5},
                                               {"kind": "registry", "id": ROOT + "i0", "name": "Test Frogs", "number": 2}]}
    if _command == "explorer.inscription":
        return {"id": "11" * 32 + "i0", "n": 5, "ct": "image/png", "h": 9, "at": "aa:0:0", "owner": "nY"}
    if _command == "explorer.inscriptions":
        return {"total": 1000, "items": []}
    raise V1Error(f"unknown method {_command}")
V1.cmd = fake_cmd
home = tempfile.mkdtemp(); json.dump({"v1": "http://127.0.0.1:1", "v1_home": home}, open(home + "/site.json", "w"))
c = TestClient(create_app(home))
fails = []
def check(ok, m): print(("ok   " if ok else "FAIL ") + m); ok or fails.append(m)
j = c.get("/explore/api/collections?net=test").json()
check([r["kind"] for r in j["collections"]] == ["builtin", "registry"] and j["total"] == 2, "list: Sub 1K, then the registry root")
r = j["collections"][1]
check(r["txid"] == ROOT and r["name"] == "Test Frogs" and r["supply"] == 3 and r["source"] == "registry", "a registry row in the page's shape")
j = c.get(f"/explore/api/collection?net=test&root={ROOT}&offset=0").json()
check(len(j["items"]) == 3 and j["items"][0]["name"] == "Test Frogs #1" and j["items"][0]["inscription_number"] == 101, "items: Name #k, inscription numbers")
check(j["next"] is None and j["collection"]["kind"] == "registry", "the last page says so")
j = c.get("/explore/api/inscription?net=test&number=5").json()
check((j.get("collection") or {}).get("name") == "Test Frogs #2" and j["collection"]["txid"] == ROOT, "the viewer's In collection (registry, not Sub 1K)")
print("ALL OK" if not fails else "%d FAILED" % len(fails))
