"""The cutout (room.media's cutout: blur, composite, transparent) in Chromium, no servers: the window's own cutout.js and
media.js and this site's media_pieces.js in one page, with a camera of the test's own (a canvas) and a stand-in
segmenter (red is the person), then the real network on a photo of a person.

    python3 tests/cutout_test.py <arcade-v1>/crates/arcade-desktop/dist [chromium,firefox]

Visible on $DISPLAY; CUTOUT_HEADLESS=1 runs it headless; CUTOUT_FIREFOX names another Firefox build. CUTOUT_SHOTS=<dir> keeps a picture of every case.
The window and viewer from before the cutout are those of the media bridge (git show claude/media-bridge, or
CUTOUT_OLD=<dir> with media.js and media_pieces.js). tests/cutout/person.jpg: NASA's portrait of Eileen Collins
(public domain), as scikit-image ships it."""
import functools, http.server, os, shutil, subprocess, sys, tempfile, threading
from pathlib import Path
from playwright.sync_api import sync_playwright

dist = Path(sys.argv[1])
browsers = (sys.argv[2] if len(sys.argv) > 2 else "chromium,firefox").split(",")
here = Path(__file__).parent
repo = here.parent
web = repo / "arcade-src/arcade/web"
fails = []


def check(ok, m):
    print(("ok   " if ok else "FAIL ") + m, flush=True)
    ok or fails.append(m)


def old(path_v1, path_site):
    """The window's media.js and the viewer's media_pieces.js from before the cutout, renamed so both can load."""
    od = os.environ.get("CUTOUT_OLD")
    if od:
        return (Path(od) / "media.js").read_text(), (Path(od) / "media_pieces.js").read_text()
    v1 = subprocess.run(["git", "-C", str(dist), "show", "origin/claude/media-bridge:" + path_v1], capture_output=True, text=True, check=True).stdout
    site = subprocess.run(["git", "-C", str(repo), "show", "origin/claude/media-bridge:" + path_site], capture_output=True, text=True, check=True).stdout
    return v1, site


root = Path(tempfile.mkdtemp(prefix="cutout-"))
for f in (dist / "cutout.js", dist / "cutout-model.bin", dist / "media.js", dist / "media-worklet.js",
          web / "templates/media_pieces.js", here / "cutout/cutout.html", here / "cutout/person.jpg"):
    shutil.copy(f, root / f.name)
ov, op = old("crates/arcade-desktop/dist/media.js", "arcade-src/arcade/web/templates/media_pieces.js")
(root / "old-media.js").write_text(ov.replace("window.ArcadeMedia =", "window.OldArcadeMedia ="))
(root / "old-media_pieces.js").write_text(op.replace("var ArcadePieces =", "var OldArcadePieces ="))


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(root)))
threading.Thread(target=srv.serve_forever, daemon=True).start()
url = f"http://127.0.0.1:{srv.server_address[1]}/cutout.html"
headless = os.environ.get("CUTOUT_HEADLESS") == "1"


def red(rgb):
    return rgb[0] > 180 and rgb[1] < 120 and rgb[2] < 120


def cutters(pg, name):
    """The cutter alone, with the stand-in: blur, composite, transparent."""
    r = pg.evaluate("cutter('blur')")
    pic = r["picture"]
    check(r["mode"] == "blur" and not r["errors"], f"{name}: blur runs ({r['mode']}, {r['errors'][:2]})")
    check(red(pic["person"]["rgb"]), f"{name}: blur: the person is kept ({pic['person']})")
    check(pic["corner"]["spread"] < 120, f"{name}: blur: the background's checkerboard is blurred (spread {pic['corner']['spread']}, was 208)")
    check(r["first"] and r["first"]["corner"]["spread"] < 120, f"{name}: blur: the very first picture is already blurred ({r['first'] and r['first']['corner']})")

    r = pg.evaluate("cutter('composite', true)")
    pic = r["picture"]
    check(r["mode"] == "composite", f"{name}: composite with a screen runs ({r['mode']}: {r['why']})")
    check(red(pic["person"]["rgb"]), f"{name}: composite: the person is kept ({pic['person']})")
    check(pic["corner"]["rgb"][2] > 150 and pic["corner"]["rgb"][0] < 40 and pic["corner"]["spread"] < 40,
          f"{name}: composite: the screen is behind the person ({pic['corner']})")
    check(pic["edge"]["rgb"][1] > 100 and pic["edge"]["rgb"][0] < 40, f"{name}: composite: the screen fills the picture ({pic['edge']})")

    r = pg.evaluate("cutter('composite', false)")
    check(r["mode"] == "blur" and "no screen" in r["why"], f"{name}: composite without a screen: blur, and says so ({r['mode']}: {r['why']})")
    check(any(s["want"] == "composite" and s["mode"] == "blur" for s in r["states"]), f"{name}: ... in a state ({r['states'][-2:]})")
    a = r.get("afterScreen") or {}
    check(a.get("mode") == "composite" and a["picture"]["corner"]["rgb"][2] > 150, f"{name}: a screen shared later: composite ({a})")

    r = pg.evaluate("cutter('transparent')")
    al = r["alpha"]
    check(r["mode"] == "transparent", f"{name}: transparent runs")
    check(al["corner"]["alpha"] < 10 and al["person"]["alpha"] > 245, f"{name}: transparent: the person alone ({al['person']['alpha']} in, {al['corner']['alpha']} out)")
    check(r["picture"]["corner"]["alpha"] == 255 and r["picture"]["corner"]["spread"] < 120,
          f"{name}: transparent: the picture that goes is the blurred one, opaque ({r['picture']['corner']})")
    check(r["mask"] and r["mask"]["w"] == 128 and r["mask"]["bytes"] < 3000, f"{name}: transparent: a small mask ({r['mask']})")



def transparent(pg):
    """Transparent as pieces, between windows and viewers of this version and from before the cutout."""
    for viewer in (False, True):
        who = "a viewer" if viewer else "a window"
        r = pg.evaluate("(v) => transparent(v)", viewer)
        check(not r["errors"], f"{who} sends transparent: no page errors ({r['errors'][:2]})")
        check(r["masks"] > 10 and r["pieces"] > r["masks"], f"{who} sends masks beside its pictures ({r['masks']} of {r['pieces']} pieces)")
        if not viewer:
            check(r["start"].get("cutout") == "transparent", f"start answers the cutout ({r['start']})")
        w = r["win"] or {}
        check(w and w["corner"]["alpha"] < 20 and w["person"]["alpha"] > 240 and red(w["person"]["rgb"]),
              f"{who} -> a window: the person alone, nothing behind ({w and (w['person'], w['corner'])})")
        check(any(e["transparent"] for e in r["winEvent"]), f"{who} -> a window: 'media' says transparent ({r['winEvent']})")
        v = r.get("viewer") or {}
        check(v and v["corner"]["alpha"] < 20 and v["person"]["alpha"] > 240, f"{who} -> a viewer: the person alone ({v and (v['person'], v['corner'])})")
        o = r["oldWin"] or {}
        check(o and o["corner"]["alpha"] == 255 and o["corner"]["spread"] < 120 and red(o["person"]["rgb"]),
              f"{who} -> an older window: an opaque, blurred picture ({o and (o['person'], o['corner'])})")
        o = r["oldViewer"] or {}
        check(r["oldViewerTrack"] and o and o["corner"]["alpha"] == 255 and o["corner"]["spread"] < 120,
              f"{who} -> an older viewer: an opaque, blurred picture ({o and o['corner']})")



def real(pg, name):
    """The real network on a photo of a person, in a worker and on the page."""
    for page in (False, True):
        r = pg.evaluate("(p) => realModel(p)", page)
        st, m = r["stats"], r.get("mask") or {}
        where = "on the page" if page else "in a worker"
        print(f"  {name}: real network {where}: ready in {r['ready']} ms; {st['masksPerSecond']} masks a second, {st['ms']} ms a mask ({st['where']}); "
              f"{st['framesPerSecond']} pictures a second at 640x480, {st['drawMs']} ms to draw one")
        # A worker the browser starves (a busy computer) gives way to the page by itself.
        check(st["mode"] == "blur" and st["where"] in (("page",) if page else ("worker", "page")), f"{name}: the real network runs {where} ({st})")
        check(m.get("face", 0) > 200 and m.get("suit", 0) > 200, f"{name}: real network: the person is in the mask ({m})")
        check(max(m.get("topLeft", 255), m.get("topRight", 255), m.get("right", 255)) < 60, f"{name}: real network: the room is not ({m})")
        if st["masksPerSecond"] < 5:
            print(f"  note: {st['masksPerSecond']} masks a second here would turn the cutout off (below 5); a busy or slow computer")


try:
    # Both copies are one file: the window's and the site's.
    check((dist / "cutout.js").read_bytes() == (web / "cutout/cutout.js").read_bytes(), "the site's cutout.js is the window's")
    check((dist / "cutout-model.bin").read_bytes() == (web / "cutout/cutout-model.bin").read_bytes(), "the site's model is the window's")
    check((web / "cutout/cutout-LICENSE.txt").is_file() and (dist / "cutout-LICENSE.txt").is_file(), "the model's license is beside it")
    with sync_playwright() as p:
        for name in browsers:
            kind, kw = getattr(p, name), {}
            exe = os.environ.get("CUTOUT_" + name.upper())    # a browser build other than the one Playwright expects
            if exe:
                kw["executable_path"] = exe
            if name == "chromium":
                kw["args"] = ["--autoplay-policy=no-user-gesture-required", "--window-size=1000,800"]
            if name == "firefox":
                kw["firefox_user_prefs"] = {"media.autoplay.default": 0}
            b = kind.launch(headless=headless, **kw)
            pg = b.new_page()
            pg.on("console", lambda m, n=name: m.type in ("error", "warning") and "404" not in m.text and print(f"  {n} console:", m.text[:200]))
            pg.goto(url)
            cutters(pg, name)
            # Windows (media.js) send and decode with WebCodecs' VP8, which only Chromium has both ways.
            if name == "chromium":
                transparent(pg)
            # The real network in a page of its own: the cases above leave their test cameras drawing.
            pg2 = b.new_page()
            pg2.goto(url)
            real(pg2, name)
            pg = pg2
            shots = os.environ.get("CUTOUT_SHOTS")
            if shots:
                Path(shots).mkdir(parents=True, exist_ok=True)
                pg.locator("#shots").screenshot(path=str(Path(shots) / (name + ".png")))
                print("  pictures:", Path(shots) / (name + ".png"))
            b.close()
finally:
    srv.shutdown()
    shutil.rmtree(root, ignore_errors=True)
print("ALL OK" if not fails else "%d FAILED" % len(fails))
sys.exit(1 if fails else 0)
