"""Shared logins, in a VISIBLE browser: sign in on THIS site with an account that lives on another arcade
(@claude on app.dogecoinarcade.com). The site fetches its sealed vault from there, the browser opens it with the
password, and the account is brought here.   python3 tests/browser_signin_elsewhere.py <site url> <shots> <creds.json>"""
import json, sys, time
from playwright.sync_api import sync_playwright
base, shots, creds = sys.argv[1].rstrip("/"), sys.argv[2], json.load(open(sys.argv[3]))
log = []
with sync_playwright() as p:
    b = p.chromium.launch(headless=False, args=["--window-size=1280,900"])
    pg = b.new_page(viewport={"width": 1280, "height": 860})
    pg.on("pageerror", lambda e: log.append(f"pageerror [{pg.url}]: {e}"))
    pg.on("response", lambda r: (r.status >= 400 or "/signin/" in r.url or "/signup" in r.url) and log.append(
        f"HTTP {r.status} {r.request.method} {r.url.split('?')[0]}"))
    pg.goto(base + "/join"); pg.wait_for_load_state("networkidle")
    pg.fill("#in-tag", creds["tag"]); pg.fill("#in-pw", creds["password"])
    pg.click("#enter")
    ok = pg.get_by_role("button", name="Sign in on ")
    try:
        ok.wait_for(timeout=20000)                          # only when this site does not hold the wallet yet
        pg.screenshot(path=f"{shots}/s0-bring-here.png")
        ok.click()
    except Exception:
        print("(already held here: no bring-here question)")
    for i in range(60):
        time.sleep(1)
        if "/join" not in pg.url: break
    time.sleep(8)
    pg.screenshot(path=f"{shots}/s1-after-signin.png")
    who = pg.evaluate("fetch('/auth/who').then(r => r.json())")
    acct = pg.evaluate("fetch('/account').then(r => r.json())")
    print("landed on", pg.url)
    print("who:", (who.get("pubkey") or "")[:16], "| name:", acct.get("name"), "| tag:", acct.get("tag"), "| address:", acct.get("address"),
          "| spendable:", acct.get("spendable"), "| announced:", acct.get("announced"))
    pg.goto(base + "/me/wallet"); pg.wait_for_load_state("networkidle"); time.sleep(6)
    pg.screenshot(path=f"{shots}/s2-wallet.png")
    b.close()
print("\n".join(log[:30]))
