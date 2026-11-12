"""Rooms through the site (V1.0's door behind it): two guests in a TEST room, each hears the other, via /stream and
via the hub.   python tests/rooms_test.py <site url>"""
import json, sys, threading, time, requests
base = sys.argv[1].rstrip("/")
fails = []
def check(ok, m): print(("ok   " if ok else "FAIL ") + m); ok or fails.append(m)
h = requests.get(base + "/realtime/hello", timeout=15).json()
check(h.get("online") is True and "cert" in h, f"hello: online, cert words ({str(h)[:90]})")
a = requests.post(base + "/realtime/join", json={"game": "sitetest", "room": "r1"}, timeout=15).json()
b = requests.post(base + "/realtime/join", json={"game": "sitetest", "room": "r1"}, timeout=15).json()
check(a.get("ok") and b.get("ok") and a.get("token") and b.get("token"), f"two guests joined ({(a.get('me') or {}).get('id')}, {(b.get('me') or {}).get('id')})")
heard = []
def listen(tok):
    with requests.get(base + "/realtime/stream", params={"token": tok}, stream=True, timeout=(10, 30)) as r:
        for line in r.iter_lines(decode_unicode=True):
            if line and line.startswith("data: "):
                ev = json.loads(line[6:])
                if ev.get("type") == "message":
                    heard.append(ev); return
threading.Thread(target=listen, args=(b["token"],), daemon=True).start()
time.sleep(1.5)
s = requests.post(base + "/realtime/send", json={"token": a["token"], "data": "hello from a"}, timeout=15).json()
check(s.get("ok"), "a sent")
for _ in range(50):
    if heard: break
    time.sleep(0.2)
check(bool(heard) and heard[0].get("data") == "hello from a", f"b heard it on /stream ({str(heard[:1])[:120]})")
# the hub: a third seat (its own stream, never also read elsewhere) on one shared stream
b = requests.post(base + "/realtime/join", json={"game": "sitetest", "room": "r1"}, timeout=15).json()
hub = requests.post(base + "/realtime/hub/attach", json={"token": b["token"]}, timeout=15).json()
check(hub.get("ok") and hub.get("hub"), "hub attached")
got = []
def hub_listen():
    with requests.get(base + "/realtime/hub", params={"id": hub["hub"]}, stream=True, timeout=(10, 30)) as r:
        for line in r.iter_lines(decode_unicode=True):
            if line and line.startswith("data: "):
                ev = json.loads(line[6:])
                if (ev.get("ev") or {}).get("type") == "message":
                    got.append(ev); return
threading.Thread(target=hub_listen, daemon=True).start()
time.sleep(1.5)
requests.post(base + "/realtime/send", json={"token": a["token"], "data": "second"}, timeout=15)
for _ in range(50):
    if got: break
    time.sleep(0.2)
check(bool(got) and got[0]["k"] == b["token"] and got[0]["ev"].get("data") == "second", "b heard it on the hub, keyed by its seat")
ice = requests.get(base + "/realtime/ice", timeout=15).json()
check(ice.get("ok") and ice.get("iceServers"), f"ice: {ice.get('iceServers')}")
for t in (a, b): requests.post(base + "/realtime/leave", json={"token": t["token"]}, timeout=15)
print("ALL OK" if not fails else "%d FAILED" % len(fails))
