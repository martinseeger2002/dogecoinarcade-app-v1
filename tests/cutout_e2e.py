"""The cutout end to end: the real doors (this site's viewer) and an Arcade V1.0 window in one room.

As tests/pieces_e2e.py: two local Arcade V1.0s, linked (the site's A, the window's B), this site in front of A. Three
viewer pages run the doors: two of this version (between them WebRTC, with the masks on their own data channel) and
one from before the cutout (the media bridge's templates, git show origin/claude/media-bridge). The window runs V1.0's
media.js and cutout.js. The camera is Chromium's fake one; the person is a stand-in (an ellipse in the middle of the
picture: ArcadeCutout.stub), so what is cut is known. Visible on $DISPLAY.

    unshare -rn sh -c 'ip link set lo up && exec nice -n 19 python3 tests/cutout_e2e.py <arcaded> <arcade-v1>/crates/arcade-desktop/dist'
"""
import json, os, shutil, signal, subprocess, sys, tempfile, threading, time
from pathlib import Path

import httpx

arcaded, dist = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
repo = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(repo / "arcade-src"), str(repo)]
fails = []


def check(ok, m):
    print(("ok   " if ok else "FAIL ") + m, flush=True)
    ok or fails.append(m)


def wait_for(fn, secs, every=0.5):
    end = time.time() + secs
    while time.time() < end:
        try:
            v = fn()
            if v:
                return v
        except Exception:                       # noqa: BLE001 -- not yet
            pass
        time.sleep(every)
    return None


T = Path(tempfile.mkdtemp(prefix="cutout-e2e-"))
PA, PB, MA, MB, PS = 18581, 18582, 18591, 18592, 18583
procs = []


def arcade(name, http, mesh, *extra):
    home = T / name
    log = open(T / f"{name}.log", "w")
    p = subprocess.Popen([str(arcaded), "--home", str(home), "--http-port", str(http), "--mesh-port", str(mesh),
                          "--no-seeds", "--stun-port", "0", *extra], stdout=log, stderr=subprocess.STDOUT,
                         preexec_fn=lambda: os.nice(19))
    procs.append(p)
    return home


def cookie(home):
    return tuple((home / ".cookie").read_text().strip().split(":", 1))


# The stand-in person: an ellipse in the middle of the picture, whatever the camera shows.
STUB = """<script src="/cutout/cutout.js"></script><script>
ArcadeCutout.stub = function (img) {
  var out = new Uint8Array(img.width * img.height);
  for (var y = 0; y < img.height; y++) for (var x = 0; x < img.width; x++) {
    var dx = (x - img.width / 2) / (img.width * 0.22), dy = (y - img.height / 2) / (img.height * 0.4);
    out[y * img.width + x] = dx * dx + dy * dy < 1 ? 255 : 0;
  }
  return out;
};
</script>"""
# A tile's pixels: alpha in the middle and in a corner, and whether it plays.
LOOK = """(sel) => {
  const el = document.querySelector(sel);
  if (!el) return null;
  const w = el.width || el.videoWidth, h = el.height || el.videoHeight;
  if (!w || w < 4) return {w: w || 0};
  const c = document.createElement('canvas'); c.width = 64; c.height = 48; const g = c.getContext('2d');
  g.drawImage(el, 0, 0, 64, 48); const d = g.getImageData(0, 0, 64, 48).data;
  const at = (x, y) => d[(y * 64 + x) * 4 + 3];
  return {w, h, mid: at(32, 24), corner: at(2, 2), shown: el.style.display, opacity: el.style.opacity};
}"""

server = None
try:
    a_home = arcade("a", PA, MA)
    b_home = arcade("b", PB, MB, "--mesh-peer", f"127.0.0.1:{MA}")
    for home, port in ((a_home, PA), (b_home, PB)):
        ok = wait_for(lambda: httpx.get(f"http://127.0.0.1:{port}/realtime/hello", timeout=2).json().get("online")
                      and (home / ".cookie").exists(), 60)
        check(bool(ok), f"arcade on :{port} is up")
    wait_for(lambda: httpx.post(f"http://127.0.0.1:{PB}/rpc", auth=cookie(b_home), timeout=5,
                                json={"jsonrpc": "2.0", "id": 1, "method": "status", "params": {}}).status_code == 200, 20)

    site = T / "site"
    site.mkdir()
    (site / "site.json").write_text(json.dumps({"v1": f"http://127.0.0.1:{PA}", "v1_home": str(a_home), "public_hosts": []}))
    import uvicorn
    from fastapi import Request
    from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
    from jinja2 import ChainableUndefined, ChoiceLoader, Environment, FileSystemLoader
    from arcadesite.app import create_app

    app = create_app(site)
    # The doors from before the cutout, for the viewer that must still see a picture.
    old_t = T / "old-templates"
    old_t.mkdir()
    for name in ("_page_doors.js.html", "media_pieces.js"):
        (old_t / name).write_text(subprocess.run(["git", "-C", str(repo), "show", "origin/claude/media-bridge:arcade-src/arcade/web/templates/" + name],
                                                 capture_output=True, text=True, check=True).stdout)

    def doors(templates):
        # The old doors' own files first, the rest (what they include and did not change) from this version.
        env = Environment(loader=ChoiceLoader([FileSystemLoader(str(templates)), FileSystemLoader(str(repo / "arcade-src/arcade/web/templates"))]),
                          undefined=ChainableUndefined)
        env.globals.update(name_of=lambda x: "", game_name=lambda x: "", modules_off=lambda: {})
        return env.get_template("_page_doors.js.html").render(
            row={"txid": "ab" * 32, "owner": "", "creator": "", "json": {}}, csrf="test", viewer="guest",
            chain={"network": "test", "params": {"pubkeyhash_version": 56}})
    names = ["arcadeAsk", "arcadeKeys", "arcadePicturesUsed", "arcadeMediaUsed", "arcadeGroupsUsed", "arcadeContactsUsed",
             "arcadeMediaAsk", "arcadePicturesAsk", "arcadeContactsAsk", "arcadeAccessAsk", "arcadeGameAskAccess"]
    yes = ["arcadeMediaAllowed", "arcadePicturesAllowed", "arcadeGameAllowed", "arcadeMediaOn", "arcadePicturesOn",
           "arcadeGameOn", "arcadeGroupsOn", "arcadeContactsOn", "arcadeContactsAllowed"]
    stubs = ("var ARCADE_MEDIA = 'arcade-media:', ARCADE_PICTURES = 'arcade-pictures:', ARCADE_SIGNED = 'arcade-signed:', "
             "ARCADE_ME = '', ARCADE_GATE_SAYS = 'no';\n"
             + "".join(f"function {n}(){{ return Promise.resolve(null); }}\n" for n in names)
             + "".join(f"function {n}(){{ return true; }}\n" for n in yes)
             + "function arcadeGameOf(){ return Promise.resolve({}); }\nfunction arcadeGameGate(){ return Promise.resolve(true); }\n")
    test_app = (repo / "tests/pieces/viewer_app.html").read_text()

    def viewer_html(templates, title, stub):
        return ("<!doctype html><meta charset=utf-8><title>" + title + "</title>"
                "<body style='margin:0;background:#556;color:#eee;font:14px system-ui'>" + (STUB if stub else "") +
                "<iframe class='inscription-frame' sandbox='allow-scripts' style='width:640px;height:400px;border:0' srcdoc=\""
                + test_app.replace("&", "&amp;").replace('"', "&quot;") + "\"></iframe>"
                "<script>\n" + stubs + doors(templates) + "\n</script>")
    pages = {"viewer": viewer_html(repo / "arcade-src/arcade/web/templates", "viewer (cutout)", True),
             "old": viewer_html(old_t, "viewer (before the cutout)", False)}
    window_html = (repo / "tests/pieces/window.html").read_text().replace(
        '<script src="/t/media.js"></script>', '<script src="/t/cutout.js"></script><script src="/t/media.js"></script>' + STUB.split("</script>", 1)[1])

    def front(path, method="GET"):
        def deco(fn):
            (app.post if method == "POST" else app.get)(path)(fn)
            app.router.routes.insert(0, app.router.routes.pop())
            return fn
        return deco

    # Front routes go in ahead of those before them: the catch-all for files first, then the named pages.
    @front("/t/{name}")
    def t_file(name: str):
        if name not in ("media.js", "media-worklet.js", "cutout.js", "cutout-model.bin"):
            return Response(status_code=404)
        return FileResponse(dist / name, media_type="text/javascript" if name.endswith(".js") else "application/octet-stream")

    @front("/t/viewer/{which}")
    def t_viewer(which: str):
        return HTMLResponse(pages[which])

    @front("/t/window.html")
    def t_window():
        return HTMLResponse(window_html)

    @front("/t/rpc-b", "POST")
    async def t_rpc(request: Request):
        said = await request.json()
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(f"http://127.0.0.1:{PB}/rpc", auth=cookie(b_home),
                             json={"jsonrpc": "2.0", "id": 1, "method": said["method"], "params": said["params"]})
        return JSONResponse(r.json())

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PS, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    check(bool(wait_for(lambda: httpx.get(f"http://127.0.0.1:{PS}/realtime/hello", timeout=2).json().get("online"), 30)),
          "the site answers, its arcade online")
    base = f"http://127.0.0.1:{PS}"
    check(httpx.get(base + "/cutout/cutout.js").status_code == 200 and len(httpx.get(base + "/cutout/cutout-model.bin").content) > 200000
          and httpx.get(base + "/cutout/nothing.js").status_code == 404, "the site serves the cutout, its model, and nothing else there")

    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch(headless=os.environ.get("CUTOUT_HEADLESS") == "1", args=[
            "--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream", "--autoplay-policy=no-user-gesture-required",
            "--allow-loopback-in-peer-connection", "--disable-features=WebRtcHideLocalIpsWithMdns", "--window-size=900,700"])
        errors = []

        def page(url, label):
            pg = b.new_page()
            pg.on("pageerror", lambda e: errors.append(f"{label}: {e}"))
            pg.on("console", lambda m: m.type == "error" and print(f"  {label} console:", m.text[:200]))
            pg.goto(base + url)
            return pg

        w = page("/t/window.html", "window")
        wid = w.evaluate("start()")["id"]
        viewers = {}
        for label, url in (("v1", "/t/viewer/viewer"), ("v2", "/t/viewer/viewer"), ("old", "/t/viewer/old")):
            pg = page(url, label)
            fr = wait_for(lambda: pg.frames[1] if len(pg.frames) > 1 else None, 10)
            j = fr.evaluate("join()")
            check(j.get("ok") and j.get("media"), f"{label} joins with the fast lane")
            viewers[label] = (pg, fr, j["me"]["id"])
        v1, f1, id1 = viewers["v1"]
        v2, f2, id2 = viewers["v2"]
        vo, fo, ido = viewers["old"]
        for label, (pg, fr, vid) in viewers.items():
            if label == "v1":
                continue
            d = wait_for(lambda: fr.evaluate(f"log.peers[{json.dumps(id1)}]") == "direct", 90)
            check(bool(d), f"{label} has v1 'direct' (WebRTC between viewers)")
        d = wait_for(lambda: f1.evaluate(f"log.peers[{json.dumps(wid)}]") == "direct", 90)
        check(bool(d), "v1 has the window 'direct' (the arcades' lane)")
        wait_for(lambda: w.evaluate("state()")["peers"].get(id1) == "direct", 30)

        # v1: camera with the cutout, shared with all; each places v1's picture.
        r = f1.evaluate("media({do: 'start', video: true, audio: true, cutout: 'transparent'})")
        check(r.get("ok") and r.get("cutout") == "transparent", f"v1 starts its camera transparent ({r})")
        for other in (id2, ido, wid):
            r = f1.evaluate(f"media({{do: 'share', peer: {json.dumps(other)}, on: true}})")
            check(r.get("ok"), f"v1 shares with {other} ({r})")
        f1.evaluate("media({do: 'place', who: 'me', rect: {x: 330, y: 0, w: 160, h: 120}})")
        for fr in (f2, fo):
            fr.evaluate(f"media({{do: 'place', who: {json.dumps(id1)}, rect: {{x: 0, y: 0, w: 320, h: 240}}}})")
        w.evaluate(f"M.door(s, {{do: 'place', who: {json.dumps(id1)}, rect: {{x: 10, y: 60, w: 320, h: 240}}}})")

        cut = wait_for(lambda: (lambda t: t if t and t.get("w", 0) > 4 and t["corner"] < 20 and t["mid"] > 230 else None)(
            v2.evaluate(LOOK, f".arcade-media-layer canvas[data-cut='1'][data-media='{id1}']")), 40)
        seen = cut or v2.evaluate(LOOK, f".arcade-media-layer canvas[data-media='{id1}']")
        check(bool(cut), f"v2 (WebRTC) draws v1 alone, nothing behind ({seen})")
        ev = [e for e in f2.evaluate("log.events") if e["type"] == "media" and e["member"] == id1]
        check(any(e.get("transparent") for e in ev), f"v2's page hears 'media' transparent ({ev[-2:]})")
        vt = v2.evaluate(LOOK, f".arcade-media-layer video[data-media='{id1}']")
        check(vt and vt.get("opacity") == "0", f"v2 keeps v1's video (its sound) under the cut picture ({vt})")
        wt = wait_for(lambda: (lambda s: s if s and s.get(id1, {}).get("w", 0) > 4 else None)(w.evaluate("""() => {
            const md = s.media; if (!md) return null; const out = {};
            for (const k in md.tiles) { const el = md.tiles[k]; const c = document.createElement('canvas'); c.width = 64; c.height = 48;
              const g = c.getContext('2d'); g.drawImage(el, 0, 0, 64, 48); const d = g.getImageData(0, 0, 64, 48).data;
              out[k] = {w: el.width, mid: d[(24 * 64 + 32) * 4 + 3], corner: d[(2 * 64 + 2) * 4 + 3]}; }
            return out; }""")), 40)
        wt1 = (wt or {}).get(id1, {})
        check(wt1 and wt1["corner"] < 20 and wt1["mid"] > 230, f"the window (pieces) draws v1 alone ({wt1})")
        old = wait_for(lambda: (lambda t: t if t and t.get("w", 0) > 4 else None)(vo.evaluate(LOOK, f".arcade-media-layer video[data-media='{id1}']")), 40)
        check(old and old["corner"] == 255 and old["mid"] == 255, f"the older viewer shows v1's picture, opaque ({old})")
        check(not vo.evaluate("document.querySelectorAll('canvas[data-cut]').length"), "the older viewer has no cut picture")
        mine = wait_for(lambda: (lambda t: t if t and t.get("w", 0) > 4 and t["corner"] < 20 and t["mid"] > 230 else None)(
            v1.evaluate(LOOK, ".arcade-media-layer canvas[data-cut='1'][data-media='me']")), 20)
        check(bool(mine), f"v1 sees itself alone ({mine})")

        # Blur: the masks stop, every viewer shows the picture as it comes.
        r = f1.evaluate("media({do: 'cutout', mode: 'blur'})")
        check(r.get("ok") and r.get("cutout") == "blur", f"cutout('blur') mid-call ({r})")
        gone = wait_for(lambda: not v2.evaluate(f"document.querySelector(\".arcade-media-layer canvas[data-cut='1'][data-media='{id1}']\")"), 15)
        check(bool(gone), "v2 goes back to the plain (blurred) picture once the masks stop")
        r = f1.evaluate("media({do: 'cutout', mode: 'composite'})")
        check(r.get("ok") and r.get("cutout") == "blur" and "screen" in r.get("cutoutWhy", ""), f"composite with no screen: blur, and why ({r})")
        evs = [e for e in f1.evaluate("log.events") if e["type"] == "media-cutout"]
        r = f1.evaluate("media({do: 'cutout', mode: null})")
        check(r.get("ok") and r.get("cutout") == "off", f"cutout(null): the camera as it is ({r})")
        tracks = v1.evaluate("(() => { const v = document.querySelector(\".arcade-media-layer video[data-media='me']\"); "
                             "return v && v.srcObject ? v.srcObject.getVideoTracks().map(t => t.label + ':' + t.readyState) : null; })()")
        check(tracks and len(tracks) == 1 and tracks[0].endswith(":live") and tracks[0] != ":live", f"the camera's own track is back in the stream ({tracks})")
        f1.evaluate("media({do: 'stop'})")
        check(not errors, f"no page errors ({errors[:3]})")
        b.close()
finally:
    if server:
        server.should_exit = True
    for pr in procs:
        try:
            pr.send_signal(signal.SIGTERM)
            pr.wait(15)
        except Exception:                       # noqa: BLE001
            pr.kill()
    left = subprocess.run(["pgrep", "-f", str(T)], capture_output=True, text=True).stdout.split()
    for pid in left:
        try:
            os.kill(int(pid), signal.SIGKILL)
        except (ProcessLookupError, ValueError):
            pass
    print("test arcades stopped" + (f" (killed {left})" if left else ""))
    if not fails or os.environ.get("CUTOUT_KEEP") != "1":
        shutil.rmtree(T, ignore_errors=True)
    else:
        print("kept", T)
print("ALL OK" if not fails else "%d FAILED" % len(fails))
sys.exit(1 if fails else 0)
