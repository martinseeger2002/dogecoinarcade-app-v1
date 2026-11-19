"""Media pieces between an Arcade V1.0 app window and this site's viewer, in real browsers, no servers: the window's own
media.js and the viewer's media_pieces.js hand each other pieces in one page (camera, microphone and a screen each way,
the can't-play state, stopping). Then pieces recorded there are decoded in Firefox and WebKit.

    python3 tests/pieces_test.py <arcade-v1>/crates/arcade-desktop/dist [chromium,firefox,webkit]

Visible on $DISPLAY (fake camera and microphone). PIECES_FIREFOX / PIECES_WEBKIT name other browser builds."""
import functools, http.server, os, shutil, sys, tempfile, threading
from pathlib import Path
from playwright.sync_api import sync_playwright

dist = Path(sys.argv[1])
browsers = (sys.argv[2] if len(sys.argv) > 2 else "chromium,firefox,webkit").split(",")
here = Path(__file__).parent
site_js = here.parent / "arcade-src/arcade/web/templates/media_pieces.js"
fails = []


def check(ok, m):
    print(("ok   " if ok else "FAIL ") + m, flush=True)
    ok or fails.append(m)


root = Path(tempfile.mkdtemp(prefix="pieces-"))
for f in (dist / "media.js", dist / "media-worklet.js", site_js, here / "pieces/interop.html"):
    shutil.copy(f, root / f.name)
class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


handler = functools.partial(Quiet, directory=str(root))
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
url = f"http://127.0.0.1:{srv.server_address[1]}/interop.html"
FAKE = ["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream", "--autoplay-policy=no-user-gesture-required"]
recorded = []
try:
    with sync_playwright() as p:
        if "chromium" in browsers:
            b = p.chromium.launch(headless=False, args=FAKE + ["--window-size=900,700"])
            pg = b.new_page()
            pg.on("console", lambda m: m.type in ("error", "warning") and print("  chromium console:", m.text[:200]))
            pg.goto(url)
            r = pg.evaluate("run()")
            recorded = r.pop("recorded", [])
            print("  chromium can:", r["can"])
            check(not r["errors"], f"chromium: no page errors ({r['errors'][:2]})")
            check(r["winStart"].get("video") and r["winStart"].get("audio"), "the window's camera and microphone start")
            check(r["toSite"] > 20 and r["toWin"] > 20, f"pieces both ways ({r['toSite']} to the viewer, {r['toWin']} to the window)")
            check(sorted(r["siteTracks"]) == ["audio:live", "video:live"], f"the viewer has the window's picture and voice as tracks ({r['siteTracks']})")
            sv = r["siteVideo"]
            check(sv["w"] > 0 and sv.get("spread", 0) > 20, f"the viewer shows the window's camera ({sv})")
            check(r["siteLevel"] > 0.001, f"the viewer hears the window's microphone (level {r['siteLevel']:.3f})")
            tiles = {t["key"]: t for t in r["winTiles"]}
            vid = "guest-0000000b"
            check(tiles.get(vid, {}).get("w", 0) > 0 and tiles[vid].get("spread", 0) > 20, f"the window draws the viewer's camera ({tiles.get(vid)})")
            check(tiles.get(vid + "#screen", {}).get("w", 0) > 0, f"the window draws the viewer's screen ({tiles.get(vid + '#screen')})")
            check(r["winLevel"] > 0.001, f"the window hears the viewer's microphone (level {r['winLevel']:.3f})")
            check(any(e["type"] == "media" and e["member"] == vid and e["video"] and e["audio"] for e in r["winEvents"]),
                  "the window's page hears 'media' {video, audio} from the viewer")
            s = r["sorry"]
            check(s["track"] and s["w"] > 0 and s.get("spread", 0) > 40 and s["events"], f"no decoder: a can't-play picture and event ({s})")
            check(r["siteTracksAfter"] == 0, f"the window stopping takes its tracks away at the viewer ({r['siteTracksAfter']} left)")
            check(any(e["type"] == "media-off" and e["member"] == vid for e in r["winEventsAfter"]), f"the viewer stopping is heard as media-off ({r['winEventsAfter']})")
            chrome = b
        for name in [x for x in browsers if x != "chromium"]:
            if not recorded:
                print(f"skip {name}: nothing recorded (run chromium first)")
                continue
            kind = getattr(p, name)
            kw = {"firefox_user_prefs": {"media.autoplay.default": 0, "media.navigator.streams.fake": True, "media.navigator.permission.disabled": True}} if name == "firefox" else {}
            exe = os.environ.get("PIECES_" + name.upper())    # a browser build other than the one Playwright expects
            if exe:
                kw["executable_path"] = exe
            b = kind.launch(headless=False, **kw)
            pg = b.new_page()
            pg.on("console", lambda m, n=name: m.type in ("error", "warning") and print(f"  {n} console:", m.text[:200]))
            pg.goto(url)
            r = pg.evaluate("(p) => decodeOnly(p)", recorded)
            print(f"  {name} can:", r["can"])
            dec = r["can"]["decode"]
            if dec["video"]:
                check(r["canvas"] and r["canvas"]["w"] > 0 and r["canvas"].get("spread", 0) > 20, f"{name}: decodes the window's camera ({r['canvas']})")
                check("video" in r["tracks"], f"{name}: as a track the doors place ({r['tracks']}, element {r['video']})")
            else:
                check(bool(r["sorry"]) and any(e.get("type") == "media-unplayable" for e in r["events"]), f"{name}: can't decode VP8, says so ({r['sorry']})")
            if dec["audio"] and r["can"]["worklet"]:
                check(r["level"] > 0.001 and "audio" in r["tracks"], f"{name}: plays the window's microphone (level {r['level']:.3f})")
            else:
                check(any("notice" in e or e.get("what") == "audio" for e in r["events"]), f"{name}: can't play Opus, says so ({r['events'][:3]})")
            check(not r["errors"], f"{name}: no page errors ({r['errors'][:2]})")
            # The other way: this browser's viewer sends, a window (media.js, in Chromium) decodes.
            if r["can"]["encode"]["video"] and "chromium" in browsers:
                pg = b.new_page()
                pg.goto(url)
                e = pg.evaluate("encodeOnly()")
                kinds = sorted({__import__("base64").b64decode(x)[1] for x in e["pieces"]})
                check(len(e["pieces"]) > 20 and 2 in kinds, f"{name}: sends microphone pieces ({len(e['pieces'])}, kinds {kinds}, errors {e['errors'][:2]})")
                if 1 not in kinds:
                    # Some builds say they encode VP8 and then cannot: that must be said, not left silent.
                    check(any(x.get("type") == "media-unsendable" for x in e["said"]), f"{name}: can't encode VP8, says so ({e['said'][:2]})")
                wp = chrome.new_page()
                wp.goto(url)
                w = wp.evaluate("(p) => windowDecode(p)", e["pieces"])
                if 1 in kinds:
                    check(w["tile"] and w["tile"]["w"] > 0 and w["tile"].get("spread", 0) > 20, f"{name}: a window draws them ({w['tile']}, decoders {w['decoders']})")
                check("mic" in w["decoders"], f"{name}: a window plays its microphone (decoders {w['decoders']})")
                check(any(x["audio"] and (x["video"] or 1 not in kinds) for x in w["events"]), f"{name}: the window's page hears media ({w['events']})")
                wp.close()
            b.close()
finally:
    srv.shutdown()
    shutil.rmtree(root, ignore_errors=True)
print("ALL OK" if not fails else "%d FAILED" % len(fails))
sys.exit(1 if fails else 0)
