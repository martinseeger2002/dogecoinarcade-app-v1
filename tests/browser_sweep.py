"""Every page in a VISIBLE browser, signed out then signed in: page errors and failed requests, per page.
python3 tests/browser_sweep.py <site url> <shots> <creds.json>"""
import json, sys, time
from collections import defaultdict
from playwright.sync_api import sync_playwright
base, shots, creds = sys.argv[1].rstrip("/"), sys.argv[2], json.load(open(sys.argv[3]))
PUBLIC = ["/feed", "/feed?sort=new", "/apps", "/exchange", "/exchange?tab=tokens", "/exchange?tab=market",
          "/tokens", "/nfts", "/collections", "/listings", "/docs", "/affiliates", "/globe", "/instances", "/terms",
          "/u/claude", "/play/ashvale", "/join", "/restore"]
MINE = ["/me", "/me/wallet", "/me/wallet/tokens", "/me/nfts", "/me/messages", "/me/notifications", "/me/contacts",
        "/me/access", "/me/backup", "/create", "/create/ord", "/create/name", "/me/runs", "/home", "/desktop",
        "/apps", "/feed", "/exchange"]
bad = defaultdict(list)
with sync_playwright() as p:
    b = p.chromium.launch(headless=False, args=["--window-size=1280,900"])
    pg = b.new_page(viewport={"width": 1280, "height": 860})
    here = {"page": ""}
    pg.on("pageerror", lambda e: bad[here["page"]].append(f"pageerror: {str(e)[:160]}"))
    pg.on("response", lambda r: r.status >= 400 and r.status != 401 and bad[here["page"]].append(
        f"HTTP {r.status} {r.request.method} {r.url.replace(base, '').split('?')[0]}"))
    def visit(path, wait=4):
        here["page"] = path
        try:
            pg.goto(base + path, timeout=60000); pg.wait_for_load_state("networkidle", timeout=60000)
        except Exception as e:
            bad[path].append(f"load: {str(e)[:120]}")
        time.sleep(wait)
    for path in PUBLIC:
        visit(f"{path}")
    here["page"] = "(sign in)"
    pg.goto(base + "/join"); pg.wait_for_load_state("networkidle")
    pg.fill("#in-tag", creds["tag"]); pg.fill("#in-pw", creds["password"]); pg.click("#enter")
    for i in range(60):
        time.sleep(1)
        if "/join" not in pg.url: break
    for path in MINE:
        visit(f"{path}", 5)
        pg.screenshot(path=f"{shots}/sweep{path.replace('/', '_') or '_root'}.png")
    b.close()
seen = set()
for path in PUBLIC + MINE + ["(sign in)"]:
    if path in seen: continue
    seen.add(path)
    rows = sorted(set(bad.get(path, [])))
    print(f"{path:28} {'ok' if not rows else ''}")
    for r in rows: print(f"    {r}")
