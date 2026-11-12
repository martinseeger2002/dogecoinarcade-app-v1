import os
"""Every app in the store, opened full screen in a VISIBLE browser, signed in: failed requests and errors per app.
python3 tests/browser_all_apps.py <site url> <shots> <creds.json> [seconds]"""
import json, re, sys, time, urllib.request
from collections import Counter, defaultdict
from playwright.sync_api import sync_playwright
base, shots, creds = sys.argv[1].rstrip("/"), sys.argv[2], json.load(open(sys.argv[3]))
wait = int(sys.argv[4]) if len(sys.argv) > 4 else 20
html = urllib.request.urlopen(base + "/apps").read().decode()
cards = re.findall(r'id="game-([0-9a-f]{64})"', html)
names = dict(re.findall(r'id="game-([0-9a-f]{64})".*?title="Play ([^"]*)"', html, re.S))
if os.environ.get("ONLY"):                              # ONLY=<name>: just the apps so named
    cards = [c for c in cards if names.get(c, "") == os.environ["ONLY"]]
bad, used = defaultdict(list), defaultdict(Counter)
with sync_playwright() as p:
    b = p.chromium.launch(headless=False, args=["--window-size=1400,900", "--autoplay-policy=no-user-gesture-required"])
    ctx = b.new_context(viewport={"width": 1400, "height": 860})
    pg = ctx.new_page()
    here = {"a": "(sign in)"}
    pg.on("pageerror", lambda e: bad[here["a"]].append(f"pageerror: {str(e)[:140]}"))
    def resp(r):
        path = r.url.replace(base, "").split("?")[0]
        if r.status >= 400 and not path.startswith("/content/"):
            bad[here["a"]].append(f"HTTP {r.status} {r.request.method} {path[:70]}")
        if path.startswith(("/r/", "/realtime/")):
            used[here["a"]]["/".join(path.split("/")[:3])] += 1
    ctx.on("response", resp)
    pg.goto(base + "/join"); pg.wait_for_load_state("networkidle")
    pg.fill("#in-tag", creds["tag"]); pg.fill("#in-pw", creds["password"]); pg.click("#enter")
    for i in range(60):
        time.sleep(1)
        if "/join" not in pg.url: break
    for card in cards:
        here["a"] = names.get(card, card[:12])
        try:
            pg.goto(base + f"/play/{card}?hop=1", timeout=60000)
        except Exception as e:
            bad[here["a"]].append(f"load: {str(e)[:100]}")
        time.sleep(wait)
        pg.screenshot(path=f"{shots}/app-{card[:10]}.png")
    b.close()
for card in cards:
    n = names.get(card, card[:12])
    rows = sorted(set(bad.get(n, [])))
    api = ", ".join(f"{k} x{v}" for k, v in used[n].most_common(4))
    print(f"{n[:32]:32} {'ok' if not rows else 'PROBLEMS'}  [{api}]")
    for r in rows[:6]: print(f"    {r}")
