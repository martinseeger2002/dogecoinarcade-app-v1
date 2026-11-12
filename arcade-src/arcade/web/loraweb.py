"""Admin -> Meshtastic (2026-10-24): the LoRa gateway's switch and status (web/lora.py), and the T-Deck firmware
for the page's web flasher.

Every route here is the admin panel's own (`admin_check`; writes need its header). The firmware comes from the
release on GitHub (dogecoinarcade/firmware, its `firmware.json` and the files it names), is checked against the
sha256 that file gives, and is kept under the arcade's home, so the browser flashes it from this arcade rather than
from a site it cannot reach (GitHub's downloads carry no CORS).
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

from fastapi import Body, Request
from fastapi.responses import JSONResponse, Response

from . import lora as loralib

#: The firmware's release, newest first: the fork of Meshtastic's firmware with the arcade app (GPL-3.0).
RELEASE = "https://github.com/dogecoinarcade/firmware/releases/latest/download/"
SOURCE = ["https://github.com/dogecoinarcade/firmware/tree/arcade",
          "https://github.com/dogecoinarcade/device-ui/tree/arcade"]
#: How long a fetched firmware.json is trusted before the release is asked again.
FRESH_SECONDS = 3600
_FILE = re.compile(r"^[A-Za-z0-9._-]{1,80}\.bin$")
_lock = threading.Lock()


def _folder(state) -> Path:
    path = Path(state.home) / "lora-firmware"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _get(url: str, timeout: float = 60.0) -> bytes:
    import requests
    got = requests.get(url, timeout=timeout)
    got.raise_for_status()
    return got.content


def firmware_info(state, fetch: Callable[[str], bytes] = _get) -> dict:
    """The release's firmware.json, asked of GitHub at most once an hour; the last one kept when it cannot be asked."""
    keep = _folder(state) / "firmware.json"
    with _lock:
        try:
            if keep.exists() and time.time() - keep.stat().st_mtime < FRESH_SECONDS:
                return json.loads(keep.read_text())
        except (OSError, ValueError):
            pass
        try:
            said = json.loads(fetch(RELEASE + "firmware.json"))
            if not isinstance(said, dict) or not isinstance(said.get("factory"), dict):
                raise ValueError("that firmware.json has no factory image")
            keep.write_text(json.dumps(said))
            return said
        except Exception:
            if keep.exists():
                return json.loads(keep.read_text())
            raise


def firmware_file(state, which: str, fetch: Callable[[str], bytes] = _get) -> bytes:
    """`factory` or `app`: the image the release's firmware.json names, fetched once and kept, its sha256 checked."""
    info = firmware_info(state, fetch)
    part = info.get(which)
    if not isinstance(part, dict) or not _FILE.match(str(part.get("file") or "")):
        raise ValueError(f"this release has no {which} image")
    want = str(part.get("sha256") or "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", want):
        raise ValueError("the release names no sha256 for that image")
    keep = _folder(state) / f"{want}.bin"
    if keep.exists():
        data = keep.read_bytes()
        if hashlib.sha256(data).hexdigest() == want:
            return data
    data = fetch(RELEASE + part["file"])
    if hashlib.sha256(data).hexdigest() != want:
        raise ValueError(f"the downloaded {part['file']} does not match its sha256: not served")
    keep.write_bytes(data)
    return data


def register(app, state, *, admin_check: Callable) -> None:
    def service() -> loralib.LoraService:
        svc = getattr(state, "lora", None)
        if svc is None:                                   # an app made without its worker (tests, a tool)
            svc = state.lora = loralib.LoraService(state)
        return svc

    @app.get("/admin/api/lora")
    def admin_lora(request: Request):
        """The gateway's settings and how it is doing, the serial ports here, and the chains it may offer."""
        who = admin_check(request)
        svc = service()
        chains = [{"net": c.network, "label": c.title} for c in state.all_chains]
        return JSONResponse({"config": svc.config(), "status": svc.status(), "ports": loralib.serial_ports(),
                             "chains": chains, "local": bool(who.get("local")), "packages": list(loralib.PACKAGES)},
                            headers={"Cache-Control": "no-store"})

    @app.post("/admin/api/lora")
    def admin_lora_save(request: Request, payload: Any = Body(None)):
        """Save the settings and start, restart or stop the gateway to match (on a thread: a stop waits for the radio)."""
        admin_check(request, write=True)
        said = payload if isinstance(payload, dict) else {}
        svc = service()
        changes = {k: said[k] for k in ("enabled", "port", "name", "chains", "beacon", "assets") if k in said}
        try:
            cfg = svc.config()
            cfg.update(changes)
            if cfg.get("enabled") and not str(cfg.get("port") or "").strip():
                raise ValueError("pick the radio's serial port first")
            saved = svc.save(changes)
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        return JSONResponse({"ok": True, "config": saved})

    @app.get("/admin/api/lora/assets")
    def admin_lora_assets(request: Request, q: str = ""):
        """Tokens matching what was typed into Admin -> Meshtastic's search box (2026-10-26: "auto complete just
        like in the public token exchange"): each chain's issued tokens by name or number, then DRC-20 / PRC-20 ticks
        from the most complete copy of the ord index; a dozen at most."""
        admin_check(request)
        typed = str(q or "").strip().lower()[:40]
        out: list[dict] = []
        if not typed:
            return JSONResponse({"results": out})
        from ..config import NETWORKS
        for chain in state.token_chains:
            coin = NETWORKS[chain.network].coin
            try:
                props = state.token_index(chain).properties()
            except Exception:                              # noqa: BLE001 -- that chain cannot say just now
                props = []
            for p in props:
                name, pid = str(p.get("name") or ""), str(p.get("property_id"))
                if typed in name.lower() or typed == pid:
                    out.append({"key": f"{chain.network}-token-{pid}", "label": f"{name} \u00b7 {coin}"[:40],
                                "note": f"token #{pid}"})
        for net, proto in (("main", "PRC-20"), ("doge", "DRC-20")):
            index = state.ord_index(net)                       # opened on this ask (lazy ord indexes, 2026-10-27)
            if index is None or not any(c.network == net for c in state.token_chains):
                continue
            try:
                first = (index.lookup({"what": "ticks", "prefix": typed[:16]}).get("ticks") or {}).get("first") or []
            except Exception:                              # noqa: BLE001
                first = []
            for row in first:
                tick = str(row.get("tick") or "")
                if tick:
                    out.append({"key": f"{net}-token20-{tick.lower()}", "label": f"{tick} \u00b7 {proto}", "note": proto})
        return JSONResponse({"results": out[:12]}, headers={"Cache-Control": "no-store"})

    @app.post("/admin/api/lora/install")
    def admin_lora_install(request: Request):
        """pip install the gateway's packages (meshtastic, coincurve) into this arcade's own Python. At the node
        machine only: it runs a program on it."""
        who = admin_check(request, write=True)
        if not who.get("local"):
            return JSONResponse({"detail": "install the radio support at the node machine itself"}, status_code=403)
        service().install_packages()
        return JSONResponse({"ok": True})

    @app.get("/admin/api/lora/firmware")
    def admin_lora_firmware(request: Request):
        """The T-Deck firmware the flasher writes: the release's firmware.json (version, board, sizes, sha256s)."""
        admin_check(request)
        try:
            info = firmware_info(state)
        except Exception as exc:                          # noqa: BLE001 -- GitHub away and nothing kept
            return JSONResponse({"detail": f"the firmware release could not be read: {str(exc)[:200]}"},
                                status_code=503)
        return JSONResponse({**info, "source": info.get("source") or SOURCE}, headers={"Cache-Control": "no-store"})

    @app.get("/admin/api/lora/firmware/{which}.bin")
    def admin_lora_firmware_file(request: Request, which: str):
        admin_check(request)
        if which not in ("factory", "app"):
            return JSONResponse({"detail": "factory or app"}, status_code=404)
        try:
            data = firmware_file(state, which)
        except Exception as exc:                          # noqa: BLE001
            return JSONResponse({"detail": str(exc)[:300]}, status_code=503)
        return Response(data, media_type="application/octet-stream", headers={"Cache-Control": "no-store"})
