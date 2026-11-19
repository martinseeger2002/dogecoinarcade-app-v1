"""Media pieces end to end: an Arcade V1.0 app window and this site's viewer in one room, seeing and hearing each other.

Two local Arcade V1.0s, linked: the site's (A) and the window's (B). This site runs in front of A. The viewer page runs
the real doors (_page_doors, with media_pieces.js) and a test app in its sandboxed frame that joins with {fast: true}
and uses room.media and fast sends; the window page runs V1.0's own media.js on B over /rpc (realtime.join with fast,
poll, lanes, media, fast), with appwindow.js's signal 'p'. Browsers: Chromium with a fake camera and microphone,
visible on $DISPLAY.

Run it in a network of its own, so the test arcades reach nothing but each other:

    unshare -rn sh -c 'ip link set lo up && exec nice -n 19 python3 tests/pieces_e2e.py <arcaded> <arcade-v1>/crates/arcade-desktop/dist'
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


T = Path(tempfile.mkdtemp(prefix="pieces-e2e-"))
PA, PB, MA, MB, PS = 18481, 18482, 18491, 18492, 18483
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


server = None
try:
    a_home = arcade("a", PA, MA)
    b_home = arcade("b", PB, MB, "--mesh-peer", f"127.0.0.1:{MA}")
    for home, port in ((a_home, PA), (b_home, PB)):
        ok = wait_for(lambda: httpx.get(f"http://127.0.0.1:{port}/realtime/hello", timeout=2).json().get("online")
                      and (home / ".cookie").exists(), 60)
        check(bool(ok), f"arcade on :{port} is up")
    linked = wait_for(lambda: httpx.post(f"http://127.0.0.1:{PB}/rpc", auth=cookie(b_home), timeout=5,
                                         json={"jsonrpc": "2.0", "id": 1, "method": "status", "params": {}}).status_code == 200, 20)

    # ---------------------------------------------------------------- the site, in front of A, with the test pages
    site = T / "site"
    site.mkdir()
    (site / "site.json").write_text(json.dumps({"v1": f"http://127.0.0.1:{PA}", "v1_home": str(a_home), "public_hosts": []}))
    import uvicorn
    from fastapi import Request
    from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
    from jinja2 import ChainableUndefined, Environment, FileSystemLoader
    from arcadesite.app import create_app

    app = create_app(site)
    env = Environment(loader=FileSystemLoader(str(repo / "arcade-src/arcade/web/templates")), undefined=ChainableUndefined)
    env.globals.update(name_of=lambda x: "", game_name=lambda x: "", modules_off=lambda: {})
    doors = env.get_template("_page_doors.js.html").render(
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
             + "function arcadeGameOf(){ return Promise.resolve({}); }\nfunction arcadeGameGate(){ return Promise.resolve(true); }\n"
             # What reaches this page and its decoders, for the report when a check fails.
             + (repo / "tests/pieces/probe.js").read_text())
    test_app = (repo / "tests/pieces/viewer_app.html").read_text()
    viewer_html = ("<!doctype html><meta charset=utf-8><title>site viewer (test)</title>"
                   "<body style='margin:0;background:#222;color:#eee;font:14px system-ui'>"
                   "<iframe class='inscription-frame' sandbox='allow-scripts' style='width:640px;height:400px;border:0' srcdoc=\""
                   + test_app.replace("&", "&amp;").replace('"', "&quot;") + "\"></iframe>"
                   "<script>\n" + stubs + doors + "\n</script>")

    def front(path, method="GET"):
        def deco(fn):
            (app.post if method == "POST" else app.get)(path)(fn)
            app.router.routes.insert(0, app.router.routes.pop())     # ahead of the site's own catch-alls
            return fn
        return deco

    @front("/t/viewer.html")
    def t_viewer():
        return HTMLResponse(viewer_html)

    @front("/t/window.html")
    def t_window():
        return FileResponse(repo / "tests/pieces/window.html")

    @front("/t/media.js")
    def t_media():
        return FileResponse(dist / "media.js", media_type="text/javascript")

    @front("/t/media-worklet.js")
    def t_worklet():
        return FileResponse(dist / "media-worklet.js", media_type="text/javascript")

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

    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch(headless=False, args=["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream",
                                                    "--autoplay-policy=no-user-gesture-required", "--window-size=900,700"])
        errors = []
        w = b.new_page()
        w.on("pageerror", lambda e: errors.append(f"window: {e}"))
        w.goto(base + "/t/window.html")
        wme = w.evaluate("start()")
        wid = wme["id"]
        v = b.new_page()
        v.on("pageerror", lambda e: errors.append(f"viewer: {e}"))
        v.on("console", lambda m: m.type in ("error", "warning") and print("  viewer console:", m.text[:200]))
        v.goto(base + "/t/viewer.html")
        frame = wait_for(lambda: v.frames[1] if len(v.frames) > 1 else None, 10)
        joined = frame.evaluate("join()")
        check(joined.get("ok") and joined.get("fast") and joined.get("media"), f"the viewer joins with the fast lane ({str(joined)[:160]})")
        vid = joined["me"]["id"]
        # The viewer finds the window on the arcades' lane: 'direct' for the page, with no WebRTC.
        direct = wait_for(lambda: frame.evaluate(f"log.peers[{json.dumps(wid)}]") == "direct", 90)
        check(bool(direct), f"the viewer's page sees the window 'direct' ({frame.evaluate('log.peers')})")
        wdirect = wait_for(lambda: w.evaluate("state()")["peers"].get(vid) == "direct", 30)
        check(bool(wdirect), f"the window's lane reaches the viewer ({w.evaluate('state()')['peers']})")
        sigs = w.evaluate("state()")["log"]["signals"]
        print("  signals the window heard from the viewer:", sigs)

        # Live: the window's camera to the viewer; the viewer's camera back.
        live = w.evaluate("goLive()")
        check(vid in live["shared"], f"the window shares with the viewer ({live})")
        vstart = frame.evaluate("media({do: 'start', video: true, audio: true})")
        check(vstart.get("ok"), f"the viewer's camera starts ({vstart})")
        for m in ({"do": "share", "peer": wid, "on": True}, {"do": "hear", "peer": wid, "on": True},
                  {"do": "place", "who": wid, "rect": {"x": 0, "y": 0, "w": 320, "h": 240}}):
            r = frame.evaluate(f"media({json.dumps(m)})")
            check(r.get("ok"), f"viewer media {m['do']} ({r})")
        got = wait_for(lambda: any(e["type"] == "media" and e["member"] == wid and e.get("video") and e.get("audio")
                                   for e in frame.evaluate("log.events")), 30)
        check(bool(got), "the viewer's page hears 'media' {video, audio} from the window")
        tile = wait_for(lambda: (lambda t: t if t and t["w"] > 0 and t["spread"] > 20 else None)(v.evaluate(f"""() => {{
            const el = document.querySelector('.arcade-media-layer video[data-media={json.dumps(wid)}]');
            if (!el || !el.videoWidth) return null;
            const c = document.createElement('canvas'); c.width = 32; c.height = 24; const g = c.getContext('2d');
            g.drawImage(el, 0, 0, 32, 24); const d = g.getImageData(0, 0, 32, 24).data; let lo = 255, hi = 0;
            for (let i = 0; i < d.length; i += 4) {{ const x = (d[i] + d[i + 1] + d[i + 2]) / 3; lo = Math.min(lo, x); hi = Math.max(hi, x); }}
            return {{w: el.videoWidth, h: el.videoHeight, spread: Math.round(hi - lo), shown: el.style.display, muted: el.muted}};
        }}""")), 30)
        check(bool(tile), f"the viewer shows the window's camera where its page placed it ({tile})")
        sorry = [e for e in frame.evaluate("log.events") if e["type"] == "media-unplayable"]
        check(not sorry, f"the viewer could play everything (no can't-play state: {sorry})")
        print("  viewer probe:", v.evaluate("window.__probe"))
        heard = wait_for(lambda: any(e["type"] == "media-speaking" and e["member"] == wid and e.get("on") for e in frame.evaluate("log.events")), 20)
        check(bool(heard), "the viewer hears the window speak (media-speaking)")
        wt = wait_for(lambda: (lambda s: s if s["tiles"].get(vid, {}).get("spread", 0) > 20 else None)(w.evaluate("state()")), 30)
        check(bool(wt), f"the window draws the viewer's camera ({wt and wt['tiles']})")
        wl = wait_for(lambda: w.evaluate("state()")["levels"].get(vid, 0) > 0.001, 20, 0.1)
        check(bool(wl), f"the window hears the viewer's microphone ({w.evaluate('state()')['levels']})")
        wev = w.evaluate("state()")["log"]["events"]
        check(any(e["type"] == "media" and e["member"] == vid and e.get("video") and e.get("audio") for e in wev),
              "the window's page hears 'media' {video, audio} from the viewer")

        # Fast data both ways over the lane.
        frame.evaluate(f"fast({json.dumps(wid)}, 'from the viewer')")
        fw = wait_for(lambda: any(x["data"] == "from the viewer" for x in w.evaluate("state()")["log"]["fast"]), 15)
        check(bool(fw), "the window hears the viewer's fast send (lane)")
        w.evaluate(f"sendFast({json.dumps(vid)}, 'from the window')")
        fv = wait_for(lambda: any(e["type"] == "fast" and e.get("data") == "from the window" for e in frame.evaluate("log.events")), 15)
        check(bool(fv), "the viewer's page hears the window's fast send")
        routes = frame.evaluate("fastRoutes()")
        print("  viewer fast routes:", routes)

        # Stopping: the viewer stops, the window hears it stop.
        frame.evaluate("media({do: 'stop'})")
        off = wait_for(lambda: any(e["type"] == "media-off" and e["member"] == vid for e in w.evaluate("state()")["log"]["events"]), 15)
        check(bool(off), "the viewer stopping is heard as media-off at the window")
        check(not errors, f"no page errors ({errors[:3]})")
        b.close()
    # The site's rooms as before (join, send, stream, hub, ice), on the same site.
    r = subprocess.run([sys.executable, str(repo / "tests/rooms_test.py"), base], capture_output=True, text=True, timeout=120)
    check(r.returncode == 0 and "ALL OK" in r.stdout, "rooms_test.py against this site: " + (r.stdout.strip().splitlines() or ["?"])[-1])
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
    if not fails or os.environ.get("PIECES_KEEP") != "1":
        shutil.rmtree(T, ignore_errors=True)
    else:
        print("kept", T)
print("ALL OK" if not fails else "%d FAILED" % len(fails))
sys.exit(1 if fails else 0)
