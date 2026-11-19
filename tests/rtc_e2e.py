"""WebRTC end to end: an Arcade V1.0 app window and this site's viewer in one room, over a real WebRTC link that the
window's arcade makes for it (arcade-v1 rtcsvc.rs: ICE, DTLS-SRTP, SCTP and RTP in arcaded, since the window has no
RTCPeerConnection), seeing and hearing each other and sending fast data both ways.

Two local Arcade V1.0s, linked: the site's (A) and the window's (B). This site runs in front of A. The viewer page runs
the real doors (_page_doors, unchanged) and a test app in its sandboxed frame that joins with {fast: true} and uses
room.media and fast sends; the window page runs V1.0's own media.js on B over /rpc, with the signaling appwindow.js
relays to its arcade (realtime.rtc). Headless Chromium, fake camera and microphone.

    mode direct: the window's id is the lower, so its arcade offers; host candidates (and B asks A's STUN).
    mode relay:  the viewer's id is the lower and offers; both sides relay-only, through B's media relay (TURN).
    mode fallback: B's WebRTC side has no candidates (relay-only, no relay): the window tells the viewer 'p' and the
                 pair uses the arcades' lane and media pieces, as before WebRTC.

Run it in a network of its own (loopback and a dummy interface), so the test arcades reach nothing else:

    unshare -rn sh -c 'ip link set lo up && ip link add d0 type dummy && ip addr add 10.77.0.1/24 dev d0 &&
        ip link set d0 up && ip route add default dev d0 &&
        exec nice -n 19 python3 tests/rtc_e2e.py <arcaded> <arcade-v1>/crates/arcade-desktop/dist direct'
"""
import json, os, shutil, signal, subprocess, sys, tempfile, threading, time
from pathlib import Path

import httpx

arcaded, dist = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
MODE = sys.argv[3] if len(sys.argv) > 3 else "direct"
assert MODE in ("direct", "relay", "fallback"), MODE
RTC_UP = MODE != "fallback"
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


T = Path(tempfile.mkdtemp(prefix="rtc-e2e-"))
PA, PB, MA, MB, PS, SA, SB = 18581, 18582, 18591, 18592, 18583, 18571, 18572
HOST = "10.77.0.1"
procs = []


def arcade(name, http, mesh, stun, env, *extra):
    home = T / name
    log = open(T / f"{name}.log", "w")
    p = subprocess.Popen([str(arcaded), "--home", str(home), "--http-port", str(http), "--mesh-port", str(mesh),
                          "--no-seeds", "--stun-port", str(stun), *extra], stdout=log, stderr=subprocess.STDOUT,
                         env={**os.environ, **env}, preexec_fn=lambda: os.nice(19))
    procs.append(p)
    return home


def cookie(home):
    return tuple((home / ".cookie").read_text().strip().split(":", 1))


server = None
try:
    a_home = arcade("a", PA, MA, SA, {})
    # B's WebRTC side asks A's STUN (from its WebRTC socket); in relay mode it also allocates on its own relay, and only
    # relayed candidates go out.
    b_env = {"ARCADE_RTC_STUN": f"{HOST}:{SA}"}
    if MODE == "relay":
        b_env.update({"ARCADE_RTC_TURN": f"{HOST}:{SB}", "ARCADE_RTC_RELAY_ONLY": "1"})
    if MODE == "fallback":
        b_env = {"ARCADE_RTC_RELAY_ONLY": "1"}
    b_home = arcade("b", PB, MB, SB, b_env, "--mesh-peer", f"127.0.0.1:{MA}")
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
    # Every link's RTCPeerConnection is kept for the report (its stats when a check fails).
    force = ("(function(){ var P = window.RTCPeerConnection; window.__pcs = [];\n"
             "window.RTCPeerConnection = function(c){ var pc = c ? new P(c) : new P(); if (c && c.iceServers) window.__pcs.push(pc); return pc; };\n"
             "window.RTCPeerConnection.prototype = P.prototype; Object.keys(P).forEach(function(k){ window.RTCPeerConnection[k] = P[k]; }); })();\n")
    if MODE == "relay":
        ice = httpx.post(f"http://127.0.0.1:{PB}/rpc", auth=cookie(b_home), timeout=10,
                         json={"jsonrpc": "2.0", "id": 1, "method": "realtime.ice", "params": {"tag": "viewer", "lan": True}}).json()["result"]
        turn = [x for x in ice["iceServers"] if x.get("username")]
        check(bool(turn), f"B's relay gives the viewer a login ({ice})")
        force += ("(function(){ var P = window.RTCPeerConnection, T = " + json.dumps(turn) + ";\n"
                 "window.RTCPeerConnection = function(c){ if (c && c.iceServers) { c = Object.assign({}, c, {iceServers: T, iceTransportPolicy: 'relay'}); } "
                 "return c ? new P(c) : new P(); }; window.RTCPeerConnection.prototype = P.prototype;\n"
                 "Object.keys(P).forEach(function(k){ window.RTCPeerConnection[k] = P[k]; }); })();\n")
    stubs = ("var ARCADE_MEDIA = 'arcade-media:', ARCADE_PICTURES = 'arcade-pictures:', ARCADE_SIGNED = 'arcade-signed:', "
             "ARCADE_ME = '', ARCADE_GATE_SAYS = 'no';\n"
             + "".join(f"function {n}(){{ return Promise.resolve(null); }}\n" for n in names)
             + "".join(f"function {n}(){{ return true; }}\n" for n in yes)
             + "function arcadeGameOf(){ return Promise.resolve({}); }\nfunction arcadeGameGate(){ return Promise.resolve(true); }\n"
             # What reaches this page and its decoders, for the report when a check fails.
             + (repo / "tests/pieces/probe.js").read_text() + force)
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
        return FileResponse(repo / "tests/rtc/window.html")

    @front("/t/media.js")
    def t_media():
        return FileResponse(dist / "media.js", media_type="text/javascript")

    @front("/t/media-worklet.js")
    def t_worklet():
        return FileResponse(dist / "media-worklet.js", media_type="text/javascript")

    # One client for every call: the window sends a piece of media per call, and a new client for each (its
    # connection and set-up) took ~0.2 s here, too slow for a camera.
    b_client = {}

    @front("/t/rpc-b", "POST")
    async def t_rpc(request: Request):
        said = await request.json()
        if "c" not in b_client:
            b_client["c"] = httpx.AsyncClient(timeout=20, auth=cookie(b_home), limits=httpx.Limits(max_connections=32))
        r = await b_client["c"].post(f"http://127.0.0.1:{PB}/rpc",
                                     json={"jsonrpc": "2.0", "id": 1, "method": said["method"], "params": said["params"]})
        return JSONResponse(r.json())

    @front("/t/rpc-b-batch", "POST")
    async def t_rpc_batch(request: Request):
        said = await request.json()
        if "c" not in b_client:
            b_client["c"] = httpx.AsyncClient(timeout=20, auth=cookie(b_home), limits=httpx.Limits(max_connections=32))
        body = [{"jsonrpc": "2.0", "id": i + 1, "method": x["method"], "params": x["params"]} for i, x in enumerate(said)]
        r = await b_client["c"].post(f"http://127.0.0.1:{PB}/rpc", json=body)
        return JSONResponse(r.json())

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PS, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    check(bool(wait_for(lambda: httpx.get(f"http://127.0.0.1:{PS}/realtime/hello", timeout=2).json().get("online"), 30)),
          "the site answers, its arcade online")
    base = f"http://127.0.0.1:{PS}"

    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True, args=["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream",
                                                   "--autoplay-policy=no-user-gesture-required", "--window-size=900,700"])
        errors = []
        w = b.new_page()
        w.on("pageerror", lambda e: errors.append(f"window: {e}"))
        me = "guest-0000aaaa" if MODE == "direct" else "guest-ffffffff"
        w.goto(base + "/t/window.html?me=" + me)
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
        # The pair links over WebRTC: the viewer's data channels open to the window's arcade.
        direct = wait_for(lambda: frame.evaluate(f"log.peers[{json.dumps(wid)}]") == "direct", 90)
        check(bool(direct), f"the viewer's page sees the window 'direct' ({frame.evaluate('log.peers')})")
        if not RTC_UP:
            wdirect = wait_for(lambda: w.evaluate("state()")["peers"].get(vid) == "direct", 30)
            check(bool(wdirect), f"the window's lane reaches the viewer ({w.evaluate('state()')['peers']})")
            wlog = w.evaluate("state()")["log"]
            print("  signals the window heard:", wlog["signals"], " sent:", wlog["sent"], " errors:", wlog["errors"])
            check(any(x["t"] == "p" for x in wlog["sent"]) and not wlog["rtc"], f"no WebRTC link to be had: the window said 'p' ({wlog['sent']})")
        else:
            wdirect = wait_for(lambda: w.evaluate("state()")["log"]["rtc"].get(vid) == "up" and w.evaluate("state()")["peers"].get(vid) == "direct", 30)
            check(bool(wdirect), f"the window's arcade has the WebRTC link up ({w.evaluate('state()')['log']['rtc']})")
            wlog = w.evaluate("state()")["log"]
            print("  signals the window heard:", wlog["signals"], " sent:", wlog["sent"])
        who_offers = "o" if MODE == "direct" else "a"
        if RTC_UP:
            check(any(x["t"] == who_offers for x in wlog["sent"]) and not any(x["t"] == "p" for x in wlog["sent"]),
                  f"the window's arcade {'offered' if MODE == 'direct' else 'answered'}, and never fell back to pieces ({wlog['sent']})")
        st = w.evaluate("rtcStatus()")
        link = next((l for l in st["links"] if l["peer"] == vid), {})
        print("  B's WebRTC side:", {k: st.get(k) for k in ("port", "host", "srflx", "stun", "relays", "relay_only")}, "link:", link)
        print("  B's WebRTC log:", (st.get("log") or [])[:12])
        want_route = "relay" if MODE == "relay" else "direct"
        if RTC_UP:
            check(link.get("state") == "up" and link.get("route") == want_route, f"the link is up, {want_route} ({link})")
        if MODE == "relay":
            check(link.get("near") == "relay" and any(r.get("relayed") for r in st["relays"]), "B sends through its relay allocation")

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
        if not tile:
            print("  viewer's tiles:", v.evaluate("""() => Array.from(document.querySelectorAll('.arcade-media-layer video, .arcade-media-layer canvas')).map((el) =>
                ({tag: el.tagName, key: el.dataset.media, w: el.videoWidth || el.width, ready: el.readyState, shown: el.style.display, paused: el.paused,
                  tracks: el.srcObject ? el.srcObject.getTracks().map((t) => [t.kind, t.muted, t.readyState]) : null}))"""))
            print("  viewer's links:", v.evaluate("""async () => { const out = [];
                for (const pc of window.__pcs) { const s = await pc.getStats(); const r = [];
                  s.forEach((x) => { if (x.type === 'inbound-rtp' || x.type === 'outbound-rtp') r.push({type: x.type, mid: x.mid, ssrc: x.ssrc, kind: x.kind, packets: x.packetsReceived || x.packetsSent, frames: x.framesDecoded, codec: x.codecId, nack: x.nackCount, pli: x.pliCount}); });
                  out.push({state: pc.connectionState, sig: pc.signalingState, rtp: r, tracks: pc.getReceivers().map((x) => x.track && [x.track.kind, x.track.muted, x.track.readyState]),
                            remote: (pc.remoteDescription && pc.remoteDescription.sdp || '').split('\\r\\n').filter((l) => /^(m=|a=(mid|msid|ssrc:|sendonly|recvonly|inactive|rtpmap))/.test(l)).slice(0, 40)}); }
                return out; }"""))
            st = w.evaluate("rtcStatus()")
            print("  B's link:", [l for l in st["links"] if l["peer"] == vid])

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
        check(bool(fw), "the window hears the viewer's fast send" + (" (data channel)" if RTC_UP else " (lane)"))
        w.evaluate(f"sendFast({json.dumps(vid)}, 'from the window')")
        fv = wait_for(lambda: any(e["type"] == "fast" and e.get("data") == "from the window" for e in frame.evaluate("log.events")), 15)
        check(bool(fv), "the viewer's page hears the window's fast send")
        routes = frame.evaluate("fastRoutes()")
        print("  viewer fast routes:", routes)
        probe = v.evaluate("window.__probe")
        if RTC_UP:
            check((routes.get("routes") or {}).get(wid) == want_route, f"the viewer's own count of the pair says {want_route} ({routes})")
            check(not probe["kinds"], f"no media pieces reached the viewer: all of it came as RTP ({probe['kinds']})")
            st = w.evaluate("rtcStatus()")
            link = next((l for l in st["links"] if l["peer"] == vid), {})
            check(set(link.get("sending", [])) >= {1, 2} and link.get("receiving", 0) >= 2, f"camera and microphone both ways on the link ({link})")
            check(not link.get("problems"), f"no problems on the link ({link.get('problems')})")
        else:
            check(bool(probe["kinds"]), f"the window's media reached the viewer as pieces on the lane ({probe['kinds']})")

        # Stopping: the viewer stops, the window hears it stop.
        frame.evaluate("media({do: 'stop'})")
        off = wait_for(lambda: any(e["type"] == "media-off" and e["member"] == vid for e in w.evaluate("state()")["log"]["events"]), 15)
        check(bool(off), "the viewer stopping is heard as media-off at the window")
        check(not errors, f"no page errors ({errors[:3]})")
        # With no WebRTC to be had, the arcade's refusal is the one error the window may log.
        werr = [e for e in w.evaluate("state()")["log"]["errors"] if RTC_UP or not e.startswith("rtc:")]
        check(not werr, f"no window errors ({werr[:3]})")
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
    if not fails or os.environ.get("RTC_KEEP") != "1":
        shutil.rmtree(T, ignore_errors=True)
    else:
        print("kept", T)
print("ALL OK" if not fails else "%d FAILED" % len(fails))
sys.exit(1 if fails else 0)
