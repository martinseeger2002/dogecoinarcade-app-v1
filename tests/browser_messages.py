"""Messages in a VISIBLE browser: sign in (name + password), open /me/messages, let the browser try its key on the
site's envelopes.   python3 tests/browser_messages.py <site url> <shots> <creds.json>"""
import json, sys, time
from playwright.sync_api import sync_playwright
base, shots, creds = sys.argv[1].rstrip("/"), sys.argv[2], json.load(open(sys.argv[3]))
log = []
with sync_playwright() as p:
    b = p.chromium.launch(headless=False, args=["--window-size=1280,900"])
    pg = b.new_page(viewport={"width": 1280, "height": 860})
    pg.on("pageerror", lambda e: log.append(f"pageerror [{pg.url}]: {e}"))
    pg.on("response", lambda r: r.status >= 400 and log.append(f"HTTP {r.status} {r.request.method} {r.url.split('?')[0]}"))
    pg.goto(base + "/join"); pg.wait_for_load_state("networkidle")
    pg.fill("#in-tag", creds["tag"]); pg.fill("#in-pw", creds["password"]); pg.click("#enter")
    for i in range(60):
        time.sleep(1)
        if "/join" not in pg.url: break
    pg.goto(base + "/me/messages"); pg.wait_for_load_state("networkidle"); time.sleep(25)
    pg.screenshot(path=f"{shots}/m1-messages.png")
    print("page text:", pg.inner_text("main")[:700].replace("\n", " | ") if pg.query_selector("main") else "")
    b.close()
print("\n".join(log[:20]) or "no page errors")
