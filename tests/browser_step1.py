"""Step 1 in a VISIBLE browser: sign up on the real page, see the account, the wallet and the feed.
python3 tests/browser_step1.py <site url> <screenshot dir>"""
import secrets, sys, time
from playwright.sync_api import sync_playwright

base, shots = sys.argv[1].rstrip("/"), sys.argv[2]
tag = "web" + secrets.token_hex(3)
pw = "orange-kayak-" + secrets.token_hex(4)
errors = []
with sync_playwright() as p:
    b = p.chromium.launch(headless=False, args=["--window-size=1280,900"])
    pg = b.new_page(viewport={"width": 1280, "height": 860})
    pg.on("pageerror", lambda e: errors.append(f"pageerror [{pg.url}]: {e}"))
    pg.on("response", lambda r: r.status >= 400 and errors.append(f"HTTP {r.status} {r.request.method} {r.url}"))
    pg.goto(base + "/join"); pg.wait_for_load_state("networkidle")
    pg.screenshot(path=f"{shots}/1-join.png")
    pg.fill("#tag", tag); pg.fill("#pw", pw); pg.fill("#pw2", pw)
    time.sleep(1.5)
    pg.click("#make-go")
    pg.wait_for_selector("#kept", state="visible", timeout=60000)
    pg.screenshot(path=f"{shots}/2-words.png")
    pg.check("#kept"); pg.click("#done")
    pg.wait_for_load_state("networkidle"); time.sleep(25)
    pg.screenshot(path=f"{shots}/3-after-signup.png")
    print("after sign-up at", pg.url)
    pg.goto(base + "/me/wallet"); pg.wait_for_load_state("networkidle"); time.sleep(6)
    pg.screenshot(path=f"{shots}/4-wallet.png", full_page=False)
    pg.goto(base + "/feed"); pg.wait_for_load_state("networkidle"); time.sleep(2)
    pg.screenshot(path=f"{shots}/5-feed.png")
    who = pg.evaluate("fetch('/auth/who').then(r => r.json())")
    acct = pg.evaluate("fetch('/account').then(r => r.json())")
    print("who:", who.get("pubkey", "")[:16], "| account:", acct.get("name"), acct.get("address"), "spendable", acct.get("spendable"))
    time.sleep(2)
    b.close()
print("tag", tag)
print("\n".join(errors[:30]) or "no page errors")
