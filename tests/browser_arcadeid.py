"""ARCADE ID across two wallets, in a real browser, against a running site with Digi-ID on and its gifts off: browser A
("the computer") turns ARCADE ID on and later signs in with it; browser B ("the phone", another account's wallet)
answers both codes from its own ARCADE ID page with its Dogecoin key. Nothing goes on chain.
    python tests/browser_arcadeid.py http://127.0.0.1:8462"""
import os, secrets, sys
from playwright.sync_api import sync_playwright

BASE = sys.argv[1].rstrip("/")
fails = []
def check(ok, m): print(("ok   " if ok else "FAIL ") + m); ok or fails.append(m)

def signup(page):
    page.goto(BASE + "/feed"); page.wait_for_function("typeof ARCADE_CHAIN !== 'undefined'", timeout=30000)
    tag = "aidt" + secrets.token_hex(3)
    return tag, page.evaluate("""async ([tag, pw]) => { const w = await import('/wallet.js');
        try { await w.signUp(tag, pw, ARCADE_CHAIN); return 'ok'; } catch (e) { return 'ERR ' + e.message; } }""",
        [tag, "correct horse battery " + secrets.token_hex(4)])

def code_on(page):
    page.wait_for_selector("dialog.digiid-dialog[open] a.digiid-open", timeout=20000)
    return page.get_attribute("dialog.digiid-dialog a.digiid-open", "href")

def answer(phone, code, chain="doge"):
    phone.goto(BASE + "/me/arcadeid"); phone.wait_for_selector("#aid-code", timeout=20000)
    for attempt in range(3):                 # the page's modules may still be loading (slower over the internet)
        phone.wait_for_timeout(2000)
        phone.fill("#aid-code", ""); phone.fill("#aid-code", code)
        try:
            phone.wait_for_selector("#aid-ask:not([hidden])", timeout=10000); break
        except Exception:
            if attempt == 2: raise
    host = phone.inner_text("#aid-host")
    phone.select_option("#aid-chain", chain)
    phone.click("#aid-go")
    phone.wait_for_function("/Done|n't|no|refused|error/i.test(document.getElementById('aid-said').textContent) && !/Signing/.test(document.getElementById('aid-said').textContent)", timeout=60000)
    return host, phone.inner_text("#aid-said")

with sync_playwright() as pw:
    b = pw.chromium.launch()
    computer = b.new_context().new_page(); phone = b.new_context().new_page()
    errors = []
    for p in (computer, phone): p.on("pageerror", lambda e: errors.append(str(e)))
    ta, sa = signup(computer); tb, sb = signup(phone)
    check(sa == "ok" and sb == "ok", f"two throwaway accounts: @{ta} (computer), @{tb} (phone): {sa} {sb}")
    phone.goto(BASE + "/me/arcadeid"); phone.wait_for_selector("#aid-code", timeout=20000)
    body = phone.inner_text("body")
    check("Built on Digi-ID" in body and "DigiByte community" in body, "the ARCADE ID page credits Digi-ID and the DigiByte community")

    computer.goto(BASE + "/me/arcadeid"); computer.wait_for_selector("#digiid-on:not([hidden])", timeout=20000)
    computer.click("#digiid-on")
    code = code_on(computer)
    check("DigiByte community" in computer.inner_text("dialog.digiid-dialog"), "the code's dialog credits Digi-ID too")
    host, said = answer(phone, code)
    check(host == BASE.split("://")[1] and said.startswith("Done"), f"the phone's wallet answers with its Dogecoin key: {host}: {said}")
    computer.wait_for_function("document.getElementById('digiid-said').textContent.includes('is on')", timeout=30000)
    computer.wait_for_function("document.getElementById('digiid-state').textContent.includes('Dogecoin')", timeout=15000)
    state = computer.inner_text("#digiid-state")
    check("Dogecoin" in state, f"on in the computer's browser, with the phone's Dogecoin ID: {state}")
    me = computer.evaluate("async () => (await (await fetch('/auth/who')).json()).pubkey")

    computer.evaluate("async () => { const w = await import('/wallet.js'); await w.signOut(ARCADE_CHAIN); }")
    computer.goto(BASE + "/feed"); computer.wait_for_selector("#signbar-digiid:not([hidden])", timeout=20000)
    check(computer.inner_text("#signbar-digiid").strip() == "ARCADE ID", "signed out: the sign-in bar offers ARCADE ID")
    computer.click("#signbar-digiid")
    code = code_on(computer)
    host, said = answer(phone, code)
    check(said.startswith("Done"), f"the phone answers the sign-in code: {said}")
    computer.wait_for_load_state("load"); computer.wait_for_timeout(5000)
    who = computer.evaluate("async () => (await (await fetch('/auth/who')).json()).pubkey")
    check(who == me, "the computer is signed in again as its own account, with no password")
    check(computer.evaluate("async () => { const w = await import('/wallet.js'); const h = await w.opened(ARCADE_CHAIN); return !!(h && h.pubkey); }"),
          "and its wallet is open")
    check(phone.evaluate("async () => (await (await fetch('/auth/who')).json()).pubkey") != me, "the phone is still its own account")
    check(not errors, f"no page errors: {errors[:3]}")
    b.close()
print("\n" + ("ALL PASSED" if not fails else f"{len(fails)} FAILED")); sys.exit(1 if fails else 0)
