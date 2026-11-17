"""Digi-ID in a real browser, against a running site (python -m arcadesite) with Digi-ID on and its gifts off:
sign up a throwaway account, turn Digi-ID on, sign out, sign in again with only a Digi-ID scan (a made-up DigiByte key
plays the wallet: it signs the code's URI and posts it to the callback, as DigiByte Go does).
    ARCADE_SRC=... python tests/browser_digiid.py http://127.0.0.1:8462"""
import os, secrets, sys, requests
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.environ.get("ARCADE_SRC", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "arcade-src")))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from playwright.sync_api import sync_playwright
from digiid_wallet import Wallet

BASE = sys.argv[1].rstrip("/")
# DIGIID_PIN=host=ip skips this computer's DNS for that host (a home resolver can hold "no such name" for a new record)
PIN = os.environ.get("DIGIID_PIN", "")
if PIN:
    import socket
    _host, _ip = PIN.split("=", 1)
    _real = socket.getaddrinfo
    socket.getaddrinfo = lambda h, *a, **k: _real(_ip if h == _host else h, *a, **k)
fails = []
def check(ok, m): print(("ok   " if ok else "FAIL ") + m); ok or fails.append(m)
w = Wallet()

def scan_dialog(page):
    page.wait_for_selector("dialog.digiid-dialog[open] a.digiid-open", timeout=20000)
    uri = page.get_attribute("dialog.digiid-dialog a.digiid-open", "href")
    for attempt in range(6):                 # a phone retries too; a home DNS that just learnt the name can flap
        try:
            r = requests.post(BASE + "/digiid/callback", json={"address": w.address, "uri": uri, "signature": w.sign(uri)},
                              timeout=20)
            break
        except requests.ConnectionError:
            if attempt == 5:
                raise
            __import__("time").sleep(5)
    return uri, r

with sync_playwright() as pw:
    b = pw.chromium.launch(args=[f"--host-resolver-rules=MAP {PIN.split('=')[0]} {PIN.split('=')[1]}"] if PIN else []); page = b.new_page(viewport={"width": 1200, "height": 800})
    errors = []; page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(BASE + "/feed"); page.wait_for_timeout(1500)
    tag = "digiidt" + secrets.token_hex(3); pw_ = "correct horse battery " + secrets.token_hex(4)
    said = page.evaluate("""async ([tag, pw]) => { const w = await import('/wallet.js');
        try { const r = await w.signUp(tag, pw, ARCADE_CHAIN); return 'ok ' + (r.tag || tag); } catch (e) { return 'ERR ' + e.message; } }""", [tag, pw_])
    check(said.startswith("ok"), f"signed up a throwaway @{tag}: {said[:120]}")
    page.goto(BASE + "/me/digiid"); page.wait_for_selector("#digiid-on:not([hidden])", timeout=20000)
    check("Off" in page.inner_text("#digiid-state"), "Digi-ID starts off")
    page.click("#digiid-on")
    uri, r = scan_dialog(page)
    host = BASE.split("://", 1)[1]
    check(r.status_code == 200 and uri.startswith(f"digiid://{host}/digiid/callback?x=")
          and uri.endswith("&u=1") == BASE.startswith("http:"),
          f"the dialog shows a Digi-ID code and the wallet's scan is accepted ({r.status_code})")
    page.wait_for_function("document.getElementById('digiid-said').textContent.includes('ARCADE ID is on')", timeout=20000)
    try:
        page.wait_for_function(f"document.getElementById('digiid-state').textContent.includes('{w.address}')", timeout=15000)
    except Exception:
        pass
    check(w.address in page.inner_text("#digiid-state"), f"on in this browser: {page.inner_text('#digiid-state')}")
    kept = page.evaluate("async () => { const d = await import('/digiid.js'); const c = await d.kept(); return c && Object.keys(c).sort().join(','); }")
    check(kept and "sealed" in kept and "phrase" not in kept, f"the browser keeps a sealed copy only: {kept}")
    me = page.evaluate("async () => (await (await fetch('/auth/who')).json()).pubkey")

    # sign out, then back in with Digi-ID
    page.evaluate("async () => { const w = await import('/wallet.js'); await w.signOut(ARCADE_CHAIN); }")
    page.goto(BASE + "/feed"); page.wait_for_timeout(1500)
    check(page.evaluate("async () => (await (await fetch('/auth/who')).json()).pubkey") is None, "signed out")
    page.wait_for_selector("#signbar-digiid:not([hidden])", timeout=20000)
    check(True, "the sign-in bar offers Digi-ID in this browser")
    page.click("#signbar-digiid")
    uri, r = scan_dialog(page)
    check(r.status_code == 200, f"scanned to sign in ({r.status_code})")
    page.wait_for_load_state("load"); page.wait_for_timeout(4000)
    who = page.evaluate("async () => (await (await fetch('/auth/who')).json()).pubkey")
    check(who == me, "signed in again as the same account, with no password")
    opened = page.evaluate("async () => { const w = await import('/wallet.js'); const h = await w.opened(ARCADE_CHAIN); return !!(h && h.pubkey); }")
    check(opened, "and the wallet is open (it can sign)")

    # another browser (no sealed copy): no Digi-ID button
    p2 = b.new_context().new_page(); p2.goto(BASE + "/feed"); p2.wait_for_timeout(3000)
    check(p2.is_visible("#signbar-digiid"), "a browser where it was never turned on still shows ARCADE ID")
    p2.click("#signbar-digiid"); p2.wait_for_timeout(500)
    said2 = p2.inner_text("#signbar-said")
    check("isn't on in this browser yet" in said2 and p2.is_visible("#signbar-said a[href='/me/arcadeid']"), f"and says how to turn it on there: {said2}")
    check(p2.evaluate("async () => (await (await fetch('/auth/who')).json()).pubkey") is None, "without signing anyone in")
    page.goto(BASE + "/me/digiid"); page.wait_for_selector("#digiid-off:not([hidden])", timeout=20000)
    page.click("#digiid-off"); page.wait_for_function("document.getElementById('digiid-said').textContent.includes('Turned off')", timeout=20000)
    check(page.evaluate("async () => !(await (await import('/digiid.js')).kept())"), "turned off: the sealed copy is gone")
    check(not errors, f"no page errors: {errors[:3]}")
    b.close()
print("\n" + ("ALL PASSED" if not fails else f"{len(fails)} FAILED")); sys.exit(1 if fails else 0)
