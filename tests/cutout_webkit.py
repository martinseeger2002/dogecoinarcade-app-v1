"""The cutout in WebKitGTK, the engine of Arcade V1.0's app windows: the cutter with the stand-in and the real network,
under the window's own policy (script-src 'self' 'wasm-unsafe-eval', as tauri.conf.json), and once without
'wasm-unsafe-eval' (the plain JavaScript must take over).

    /usr/bin/python3 tests/cutout_webkit.py <arcade-v1>/crates/arcade-desktop/dist

The system Python, with GTK's WebKit2 4.1 (gir1.2-webkit2-4.1). Visible on $DISPLAY."""
import functools, http.server, json, os, re, shutil, sys, tempfile, threading
from pathlib import Path

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("WebKit2", "4.1")
from gi.repository import GLib, Gtk, WebKit2  # noqa: E402

dist = Path(sys.argv[1])
here = Path(__file__).parent
fails = []


def check(ok, m):
    print(("ok   " if ok else "FAIL ") + m, flush=True)
    ok or fails.append(m)


root = Path(tempfile.mkdtemp(prefix="cutout-wk-"))
for f in (dist / "cutout.js", dist / "cutout-model.bin", here / "cutout/person.jpg"):
    shutil.copy(f, root / f.name)
# The test page's own script, out of its page: the window's policy runs no inline script.
page = (here / "cutout/cutout.html").read_text()
script = re.search(r"<script>\n(.*)</script>", page, re.S).group(1)
(root / "cutout-test.js").write_text(script)
(root / "wk.html").write_text('<!doctype html><meta charset="utf-8"><body style="background:#222"><div id="shots"></div>'
                              '<script src="cutout.js"></script><script src="cutout-test.js"></script>')
POLICY = {"wasm": "default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; style-src 'self' 'unsafe-inline'; img-src 'self' data:",
          "plain": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:"}


class Handler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Content-Security-Policy", POLICY["wasm" if "/wasm/" in self.path else "plain"])
        super().end_headers()

    def translate_path(self, path):
        return super().translate_path(path.replace("/wasm/", "/").replace("/plain/", "/"))

    def log_message(self, *a):
        pass


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Handler, directory=str(root)))
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{srv.server_address[1]}"

win = Gtk.Window(title="cutout in WebKitGTK")
win.set_default_size(1000, 700)
view = WebKit2.WebView()
s = view.get_settings()
s.set_enable_media_stream(True)
s.set_enable_webrtc(True)
s.set_media_playback_requires_user_gesture(False)
s.set_enable_write_console_messages_to_stdout(True)
win.add(view)
win.show_all()
loop = GLib.MainLoop()


def run(js, secs=60):
    """An async function body's answer, as JSON."""
    out = {}

    def done(v, res):
        try:
            out["v"] = json.loads(v.call_async_javascript_function_finish(res).to_json(0))
        except Exception as err:                # noqa: BLE001
            out["err"] = str(err)
        loop.quit()
    view.call_async_javascript_function("return JSON.parse(JSON.stringify(await (" + js + ")));", -1, None, None, None, None, done)
    GLib.timeout_add_seconds(secs, lambda: (out.setdefault("err", "timeout"), loop.quit()) and False)
    loop.run()
    if "err" in out:
        raise RuntimeError(out["err"])
    return out["v"]


def load(url):
    view.load_uri(url)
    GLib.timeout_add(3000, lambda: loop.quit() or False)
    loop.run()


def red(rgb):
    return rgb[0] > 180 and rgb[1] < 120 and rgb[2] < 120


try:
    for kind in ("wasm", "plain"):
        load(f"{base}/{kind}/wk.html")
        caps = run("(async () => ({canvasCapture: !!HTMLCanvasElement.prototype.captureStream, worker: typeof Worker, "
                   "wasm: typeof WebAssembly, filter: (() => { const g = document.createElement('canvas').getContext('2d'); g.filter = 'blur(2px)'; return g.filter; })(), "
                   "ua: navigator.userAgent}))()")
        print(f"  {kind}: {caps}")
        r = run("cutter('blur')")
        check(r["mode"] == "blur" and red(r["picture"]["person"]["rgb"]) and r["picture"]["corner"]["spread"] < 120,
              f"{kind}: blur in WebKitGTK ({r['mode']} {r['picture']['person']} {r['picture']['corner']}, {r['errors'][:2]})")
        r = run("cutter('composite', true)")
        check(r["mode"] == "composite" and r["picture"]["corner"]["rgb"][2] > 150, f"{kind}: composite in WebKitGTK ({r['picture']['corner']})")
        r = run("cutter('transparent')")
        check(r["alpha"]["corner"]["alpha"] < 10 and r["alpha"]["person"]["alpha"] > 245, f"{kind}: transparent in WebKitGTK ({r['alpha']['corner']}, {r['alpha']['person']})")
        r = run("(async () => { const b = await (await fetch('cutout-model.bin')).arrayBuffer(); const n = new ArcadeCutout.Net(b); "
                "const x = new Float32Array(256 * 144 * 3).fill(0.5); for (let i = 0; i < 5; i++) n.run(x); const t = performance.now(); "
                "for (let i = 0; i < 10; i++) n.run(x); return {fast: n.fast, ms: Math.round((performance.now() - t) / 10)}; })()")
        print(f"  {kind}: the network on the page: {r['ms']} ms a run ({'WebAssembly SIMD' if r['fast'] else 'JavaScript'})")
        check(r["fast"] == (kind == "wasm"), f"{kind}: WebAssembly {'used' if kind == 'wasm' else 'refused by the policy, JavaScript instead'}")
        r = run("realModel(false)", 90)
        st, m = r["stats"], r.get("mask") or {}
        print(f"  {kind}: real network: {st['masksPerSecond']} masks a second, {st['ms']} ms a mask ({st['where']}); "
              f"{st['framesPerSecond']} pictures a second, {st['drawMs']} ms to draw one")
        check(m.get("face", 0) > 200 and m.get("suit", 0) > 200 and max(m.get("topLeft", 255), m.get("topRight", 255)) < 60,
              f"{kind}: the real network's mask in WebKitGTK ({m})")
        shots = os.environ.get("CUTOUT_SHOTS")
        if shots:
            got = {}

            def snap(v, res):
                got["s"] = v.get_snapshot_finish(res)
                loop.quit()
            view.get_snapshot(WebKit2.SnapshotRegion.FULL_DOCUMENT, WebKit2.SnapshotOptions.NONE, None, snap)
            loop.run()
            Path(shots).mkdir(parents=True, exist_ok=True)
            got["s"].write_to_png(str(Path(shots) / f"webkit-{kind}.png"))
            print("  pictures:", Path(shots) / f"webkit-{kind}.png")
finally:
    srv.shutdown()
    shutil.rmtree(root, ignore_errors=True)
print("ALL OK" if not fails else "%d FAILED" % len(fails))
sys.exit(1 if fails else 0)
