"""Explore in a VISIBLE browser: Sub 1K grid, an inscription, a tick's holders, arcade assets.
python3 tests/browser_explore.py <site url> <screenshot dir>"""
import sys, time
from playwright.sync_api import sync_playwright
base, shots = sys.argv[1].rstrip("/"), sys.argv[2]
errors = []
with sync_playwright() as p:
    b = p.chromium.launch(headless=False, args=["--window-size=1280,900"])
    pg = b.new_page(viewport={"width": 1280, "height": 860})
    pg.on("pageerror", lambda e: errors.append(f"pageerror [{pg.url}]: {e}"))
    pg.on("response", lambda r: r.status >= 400 and errors.append(f"HTTP {r.status} {r.url}"))
    pg.goto(base + "/explore?net=doge"); pg.wait_for_load_state("networkidle"); time.sleep(6)
    pg.screenshot(path=f"{shots}/e1-doge-sub1k.png")
    pg.goto(base + "/explore?net=doge&n=12345"); pg.wait_for_load_state("networkidle"); time.sleep(5)
    pg.locator("#xpview").scroll_into_view_if_needed(); time.sleep(1)
    pg.screenshot(path=f"{shots}/e2-inscription.png")
    pg.goto(base + "/explore?net=doge&p=tokens"); pg.wait_for_load_state("networkidle"); time.sleep(2)
    pg.locator("#xptickpick input").fill("dogi"); time.sleep(6)
    pg.keyboard.press("ArrowDown"); pg.keyboard.press("Enter"); time.sleep(5)
    pg.screenshot(path=f"{shots}/e3-tick.png")
    pg.goto(base + "/explore?net=test&p=assets"); pg.wait_for_load_state("networkidle"); time.sleep(5)
    pg.screenshot(path=f"{shots}/e4-assets.png")
    b.close()
print("\n".join(errors[:20]) or "no page errors")
