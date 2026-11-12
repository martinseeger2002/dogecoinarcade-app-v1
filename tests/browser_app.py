"""An inscribed app on the site, in a VISIBLE browser: sign in, open it full screen, watch its /r/, /realtime/ and
/content/ calls and errors.   python3 tests/browser_app.py <site url> <shots> <creds.json> <app name or card txid>"""
import json, sys, time
from collections import Counter
from playwright.sync_api import sync_playwright
base, shots, creds, which = sys.argv[1].rstrip("/"), sys.argv[2], json.load(open(sys.argv[3])), sys.argv[4]
calls, errs = Counter(), []
with sync_playwright() as p:
    b = p.chromium.launch(headless=False, args=["--window-size=1400,900", "--autoplay-policy=no-user-gesture-required"])
    ctx = b.new_context(viewport={"width": 1400, "height": 860})
    pg = ctx.new_page()
    pg.on("pageerror", lambda e: errs.append(f"top: {str(e)[:160]}"))
    def resp(r):
        path = r.url.replace(base, "").split("?")[0]
        if path.startswith(("/r/", "/realtime/", "/content/", "/account/", "/storage/", "/games/")):
            key = "/".join(path.split("/")[:3]) if not path.startswith("/content/") else "/content/<id>"
            calls[(key, r.status)] += 1
    ctx.on("response", resp)
    pg.goto(base + "/join"); pg.wait_for_load_state("networkidle")
    pg.fill("#in-tag", creds["tag"]); pg.fill("#in-pw", creds["password"]); pg.click("#enter")
    for i in range(60):
        time.sleep(1)
        if "/join" not in pg.url: break
    pg.goto(base + f"/play/{which}?hop=1"); time.sleep(45)
    pg.screenshot(path=f"{shots}/app-{which}.png")
    for f in pg.frames[1:]:
        try:
            f.evaluate("1")
        except Exception as e:
            errs.append(f"frame {f.url[:60]}: {str(e)[:80]}")
    print("at", pg.url)
    b.close()
for (k, s), n in sorted(calls.items()):
    print(f"  {s} {k} x{n}")
print("\n".join(errs[:15]) or "no page errors")
