"""@tag claims on the website: a tag with someone else's claim waiting in the home chain's mempool is not offered
(the chain counts the first claim mined; a second one sent meanwhile is paid for and refused).
    .venv/bin/python tests/tagclaim_test.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.environ.get("ARCADE_SRC", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "arcade-src")))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import exchange_test as X  # noqa: E402

from arcadesite.v1 import V1, V1Error  # noqa: E402

fails = []
OTHER = "nOtherClaimerAddressxxxxxxxxxxxxx"
pool_down = []


def check(ok, m):
    print(("ok   " if ok else "FAIL ") + m)
    ok or fails.append(m)


def fake_cmd(self, _command, _timeout=None, **p):
    if _command == "explorer.tag":
        raise V1Error(f"@{p['tag']} is not claimed")
    if _command == "explorer.mempool":
        if pool_down:
            raise V1Error("down")
        return {"count": 2, "txs": [], "pending_tags": [["busy", OTHER], ["mine", X.ME]]}
    return X.fake_cmd(self, _command, _timeout, **p)


def main():
    saved = (V1.page, V1.cmd, V1.core)
    V1.page, V1.cmd, V1.core = X.fake_page, fake_cmd, X.fake_core
    try:
        home = tempfile.mkdtemp()
        json.dump({"v1": "http://127.0.0.1:1", "v1_home": home}, open(home + "/site.json", "w"))
        c, app = X.site(home, X.ME)
        v1 = app.state.site.v1

        check(v1.tag_pending("busy") == OTHER and v1.tag_pending("fresh") == "", "tag_pending reads the pool's claims")
        j = c.get("/signup/busy").json()
        check(j["free"] is False and "waiting for a block" in j["detail"], f"sign-up check: busy is not free ({j})")
        r = c.post("/account/claim", json={"tag": "busy"})
        check(r.status_code == 400 and "waiting for a block" in r.json().get("detail", ""),
              f"claim: someone else's waiting claim refuses it ({r.status_code} {r.text[:120]})")
        r = c.post("/account/claim", json={"tag": "mine"})
        check(r.status_code == 200 and r.json().get("offer"), f"claim: our own waiting claim may be sent again "
                                                              f"({r.status_code} {r.text[:120]})")
        r = c.post("/account/claim", json={"tag": "fresh"})
        check(r.status_code == 200 and r.json().get("offer"), f"claim: a free tag is offered ({r.status_code})")
        pool_down.append(1)
        check(v1.tag_pending("busy") == "", "the pool can't be asked: no claim waiting is assumed")
    finally:
        V1.page, V1.cmd, V1.core = saved
    print("ALL OK" if not fails else f"{len(fails)} FAILED")
    return not fails


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
