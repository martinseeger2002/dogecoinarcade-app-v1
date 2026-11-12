"""The LoRa (Meshtastic) gateway, run by the arcade itself (2026-10-24: "Add a Meshtastic admin panel and a Web
flasher for our new firmware to the admin panel" ... "Incorporated as a option for all arcades").

The gateway is arcade/lora (copied from dogecoinarcade/arcade-lora): T-Decks talk to it over LoRa, and it talks to
this arcade over loopback HTTP like any browser. Here it is one thread in the worker that holds the run lock
(supervisor.RunLock), so exactly one gateway holds the serial port and a reload hands it over cleanly. Off by
default; Admin -> Meshtastic turns it on and picks the port, the name and the chains the T-Decks are offered.

What the gateway's author asked for (robin-ca, 2026-10-24): every gateway call off the event loop (its httpx calls come
back into this uvicorn); the SerialInterface built on this thread (it blocks while it reads the radio's config); and
the link rebuilt, on the same Gateway, when the port goes away or the library says the connection was lost -- the
Gateway's sessions and replay memory live on.
"""
from __future__ import annotations

import glob
import importlib.util
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

log = logging.getLogger("arcade.lora")

#: The extra's pins, as the gateway was tested with (arcade-lora's venv on .25, 2026-10-24).
#: pillow (2026-10-26): pictures on .mesh pages made small for a T-Deck (arcade/mesh_picture.py); without it the gateway
#: still runs and a picture says why it cannot come, so it is not one of the MODULES the panel insists on.
PACKAGES = ("meshtastic==2.7.11", "coincurve==21.0.0", "pillow>=10.0")
MODULES = ("meshtastic", "coincurve", "nacl", "httpx")
DEFAULTS = {"enabled": False, "port": "", "name": "arcade gateway", "chains": None, "beacon": False, "assets": []}
#: Tokens the T-Decks see (2026-10-26: "a search bar that auto complete just like in the public token exchange
#: that allows the operator to include any token for the T deck to see"): {"key", "label"}, the key as the exchange
#: names a leg -- "<net>-token-<property id>" or "<net>-token20-<tick>".
ASSET_KEY = re.compile(r"^[a-z0-9]{1,12}-(token-[0-9]{1,10}|token20-[a-z0-9]{1,16})$")
ASSETS_MAX = 24
BEACON_EVERY = 900.0
POLL_EVERY = 30.0
RETRY_EVERY = 20.0
STATE_FILE = "lora-gateway.sqlite"
#: Where a radio's serial port may be: nothing else is opened as one.
PORT_PREFIXES = ("/dev/tty", "/dev/serial/", "/dev/cu.")
#: The hand-run gateway's state (~/arcade-lora on .25): taken over once, so a T-Deck signed in there stays signed in.
OLD_STATE = Path.home() / ".arcade-lora-gateway.sqlite"


def missing_modules() -> list[str]:
    return [m for m in MODULES if importlib.util.find_spec(m) is None]


def serial_ports() -> list[dict]:
    """The serial ports a radio may be on: /dev/ttyUSB*, /dev/ttyACM*, each with its /dev/serial/by-id name."""
    names = {}
    for link in glob.glob("/dev/serial/by-id/*"):
        try:
            names[os.path.realpath(link)] = os.path.basename(link)
        except OSError:
            pass
    return [{"port": p, "name": names.get(p, "")} for p in sorted(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*"))]


class LoraService:
    def __init__(self, state):
        self.state = state
        self.thread: threading.Thread | None = None
        self.stop = threading.Event()
        self.gw = None
        self.link = None
        self.lost = threading.Event()
        self.error = ""
        self.since = 0.0
        self.install = {"state": "", "said": ""}
        self.lock = threading.Lock()

    # -- settings -----------------------------------------------------------------------------------------------------
    def config(self) -> dict:
        got = self.state.setting("lora") or {}
        out = dict(DEFAULTS)
        if isinstance(got, dict):
            out.update({k: got[k] for k in DEFAULTS if k in got})
        return out

    def save(self, changes: dict) -> dict:
        cfg = self.config()
        if "enabled" in changes:
            cfg["enabled"] = bool(changes["enabled"])
        if "port" in changes:
            port = str(changes["port"] or "").strip()
            if port and not port.startswith(PORT_PREFIXES):
                raise ValueError("a serial port is a /dev/tty... device")
            cfg["port"] = port
        if "name" in changes:
            cfg["name"] = (str(changes["name"] or "").strip() or DEFAULTS["name"])[:40]
        if "chains" in changes:
            nets = changes["chains"]
            known = {c.network for c in self.state.all_chains}
            cfg["chains"] = None if nets is None else [n for n in nets if n in known]
        if "beacon" in changes:
            cfg["beacon"] = bool(changes["beacon"])
        if "assets" in changes:
            known = {c.network for c in self.state.all_chains}
            seen, assets = set(), []
            for a in changes["assets"] or []:
                key = str((a or {}).get("key") or "").strip().lower() if isinstance(a, dict) else ""
                if not ASSET_KEY.match(key) or key.split("-", 1)[0] not in known or key in seen:
                    continue
                seen.add(key)
                assets.append({"key": key, "label": str(a.get("label") or key)[:40]})
            cfg["assets"] = assets[:ASSETS_MAX]
        if cfg["enabled"] and not cfg["port"]:
            raise ValueError("pick the radio's serial port first")
        self.state.set_setting("lora", cfg)
        self.apply()
        return cfg

    # -- running ------------------------------------------------------------------------------------------------------
    @property
    def running(self) -> bool:
        return bool(self.thread and self.thread.is_alive() and self.link is not None)

    def apply(self) -> None:
        """Start, restart or stop the gateway to match the settings. Never on the event loop: it may wait for a stop."""
        with self.lock:
            self._halt()
            cfg = self.config()
            if not cfg["enabled"]:
                self.error = ""
                return
            self.stop = threading.Event()
            self.thread = threading.Thread(target=self._run, args=(cfg, self.stop), name="arcade-lora", daemon=True)
            self.thread.start()

    def _halt(self) -> None:
        if self.thread is not None:
            self.stop.set()
            self.thread.join(timeout=30)
        self.thread = None
        self._close_link()
        self.gw = None

    def shutdown(self) -> None:
        with self.lock:
            self._halt()

    def _close_link(self) -> None:
        link, self.link = self.link, None
        if link is not None:
            try:
                link.close()
            except Exception:                              # noqa: BLE001 -- a radio already gone
                log.debug("lora: closing the link", exc_info=True)
            self._announce()

    def _announce(self) -> None:
        """Tell the linked arcades now that this one has (or no longer has) a gateway: their LoRa badge."""
        mesh = getattr(self.state, "mesh", None)
        try:
            if mesh is not None:
                mesh.say_who_runs_this()
        except Exception:                                  # noqa: BLE001 -- said again at the next minute's turn
            log.debug("lora: could not say so on the mesh", exc_info=True)

    def state_path(self) -> str:
        path = Path(self.state.home) / STATE_FILE
        if not path.exists() and OLD_STATE.exists():
            try:
                shutil.copy2(OLD_STATE, path)
                os.chmod(path, 0o600)
                log.info("lora: took over %s", OLD_STATE)
            except OSError:
                log.warning("lora: could not take over %s", OLD_STATE, exc_info=True)
        return str(path)

    def _gateway(self, cfg: dict):
        from ..lora.gateway import Gateway
        from ..lora.store import Store
        arcade = f"http://127.0.0.1:{int(getattr(self.state, 'port', 0) or 8420)}"
        return Gateway(arcade, self._send, name=cfg["name"], networks=cfg["chains"],
                       assets=cfg.get("assets") or [], store=Store(self.state_path()))

    def _send(self, node: int, frame: bytes) -> bool:
        link = self.link
        if link is None:
            return False
        return link.send(node, frame)

    def _connect(self, cfg: dict) -> None:
        from pubsub import pub
        from ..lora.link import MeshLink
        self.lost.clear()
        link = MeshLink(port=cfg["port"])
        link.on_heard(self.gw.heard)
        link.on_frame(self.gw.receive)
        self.link = link
        self._announce()

        def lost(interface=None, **_kw):
            if interface is None or interface is link.iface:
                self.lost.set()
        self._on_lost = lost                               # kept: pubsub holds weak references
        pub.subscribe(lost, "meshtastic.connection.lost")
        log.info("lora: gateway up on node %08x (%s) for this arcade", link.my_node, cfg["port"])

    def _run(self, cfg: dict, stop: threading.Event) -> None:
        missing = missing_modules()
        if missing:
            self.error = "the radio support is not installed (" + ", ".join(missing) + "): press Install"
            return
        try:
            self.gw = self._gateway(cfg)
        except Exception as exc:                           # noqa: BLE001
            self.error = f"the gateway did not start: {exc}"
            log.warning("lora: gateway did not start", exc_info=True)
            return
        next_beacon = next_poll = 0.0
        while not stop.is_set():
            if self.link is None:
                if not os.path.exists(cfg["port"]):
                    self.error = f"no radio on {cfg['port']}: plug it in (waiting for it)"
                    stop.wait(RETRY_EVERY)
                    continue
                try:
                    self._connect(cfg)
                    self.error, self.since = "", time.time()
                except Exception as exc:                   # noqa: BLE001 -- the port busy, a radio that did not answer
                    self.error = f"the radio on {cfg['port']} did not answer: {exc}"
                    log.warning("lora: could not open %s", cfg["port"], exc_info=True)
                    self._close_link()
                    stop.wait(RETRY_EVERY)
                    continue
            if self.lost.is_set() or not os.path.exists(cfg["port"]):
                self.error = "the radio went away: opening it again"
                log.warning("lora: the radio on %s went away", cfg["port"])
                self._close_link()
                stop.wait(3)
                continue
            now = time.monotonic()
            try:
                if cfg["beacon"] and now >= next_beacon:
                    self.link.broadcast(self.gw.beacon())
                    next_beacon = now + BEACON_EVERY
                if now >= next_poll:
                    self.gw.poll()
                    next_poll = now + POLL_EVERY
                self.gw.tick()
            except Exception:                              # noqa: BLE001 -- one bad turn; the next one goes on
                log.warning("lora: gateway turn failed", exc_info=True)
            stop.wait(1)
        self._close_link()

    # -- what the panel shows -----------------------------------------------------------------------------------------
    def status(self) -> dict:
        cfg = self.config()
        out = {"enabled": cfg["enabled"], "running": self.running, "error": self.error,
               "since": self.since if self.running else 0, "missing": missing_modules(), "install": dict(self.install)}
        link, gw = self.link, self.gw
        if link is not None:
            out["radio"] = _radio(link)
            out["link"] = dict(link.stats)
        if gw is not None:
            now = gw.clock()
            devices = []
            for dev in list(gw.devices.values()):
                ago = now - dev.last_heard if dev.last_heard != float("-inf") else None
                devices.append({"node": f"!{dev.node:08x}", "tag": dev.tag, "online": bool(dev.online),
                                "heard_ago": round(ago) if ago is not None else None,
                                "signed_in": bool(dev.arcade.signed_in) if hasattr(dev.arcade, "signed_in") else None,
                                "watching": len(dev.watch), "outbox": len(gw.store.pending(dev.node))})
            devices.sort(key=lambda d: (not d["online"], d["heard_ago"] if d["heard_ago"] is not None else 9e9))
            out["devices"] = devices
            try:
                out["outbox"] = int(gw.store._x("SELECT COUNT(*) FROM outbox")[0][0])
                out["receipts"] = int(gw.store._x("SELECT COUNT(*) FROM receipt")[0][0])
            except Exception:                              # noqa: BLE001
                pass
        return out

    def install_packages(self) -> None:
        """pip install the [lora] extra's packages into this arcade's own Python, on a thread; the panel asks again."""
        if self.install.get("state") == "running":
            return

        def go():
            self.install = {"state": "running", "said": ""}
            try:
                done = subprocess.run([sys.executable, "-m", "pip", "install", *PACKAGES], capture_output=True,
                                      text=True, timeout=900)
                importlib.invalidate_caches()
                ok = done.returncode == 0 and not missing_modules()
                self.install = {"state": "done" if ok else "failed",
                                "said": (done.stdout + done.stderr).strip()[-600:]}
                if ok and self.config()["enabled"] and not self.running:
                    self.apply()
            except Exception as exc:                       # noqa: BLE001
                self.install = {"state": "failed", "said": str(exc)[:600]}
        threading.Thread(target=go, name="arcade-lora-install", daemon=True).start()


def _radio(link) -> dict:
    """The radio's own facts, as far as the library has them: its node, region and preset, and the nodes it hears."""
    out = {"node": f"!{int(link.my_node):08x}"}
    iface = link.iface
    try:
        lora = iface.localNode.localConfig.lora
        from meshtastic.protobuf import config_pb2
        out["region"] = config_pb2.Config.LoRaConfig.RegionCode.Name(lora.region)
        out["preset"] = config_pb2.Config.LoRaConfig.ModemPreset.Name(lora.modem_preset)
    except Exception:                                      # noqa: BLE001 -- an older library, or not read yet
        pass
    try:
        me = (iface.nodesByNum or {}).get(int(link.my_node)) or {}
        user = me.get("user") or {}
        out["long_name"] = user.get("longName", "")
        out["short_name"] = user.get("shortName", "")
        out["hw"] = user.get("hwModel", "")
    except Exception:                                      # noqa: BLE001
        pass
    try:
        nodes = list((iface.nodes or {}).values())
        cutoff = time.time() - 3600
        out["nodes"] = len(nodes)
        out["nodes_hour"] = sum(1 for n in nodes if int(n.get("lastHeard") or 0) >= cutoff)
    except Exception:                                      # noqa: BLE001
        pass
    return out
