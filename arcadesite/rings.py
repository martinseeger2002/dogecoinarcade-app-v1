"""Ringing a player (arcade.realtime.ring, the original's arcade/ring.py rules) for accounts on this site.

A ring wakes the person only when all hold, checked here: they switched Notifications on for that app (`ring:<pubkey>`
apps), the caller is in their address book (the browser hands over the book's addresses, `allow`), the caller is who
they say (their certificate: V1.0's realtime words signed by their key), and not too often (ring.Rings).

    GET/POST /account/ring/apps    which apps may ring this account
    POST     /account/ring/allow   who may (the address book's addresses)
    POST     /account/ring         ring an address, from an app
    GET      /account/ring/live    an open page checks in and hears what rings it
    GET      /account/ring/{id}    the ring a tapped notification opened

NEEDS-V1: a ring to a player on ANOTHER arcade. The original rang through the mesh room `arcade!ring/<address>`; V1.0's
room door takes game names of letters, digits and `._:-` only, and V1.0's own ring.* rings for its own player. Such a
ring is answered "rung: false" with the reason, never pretended.
"""
from __future__ import annotations

import base64
import hashlib
import re
import time
from typing import Any

from fastapi import Body, Request
from fastapi.responses import JSONResponse

from arcade import ring as ringlib
from arcade.script import b58check_encode, hash160

_P = 2 ** 256 - 2 ** 32 - 977
_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_G = (0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
      0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8)
MAGIC = "Pepecoin Signed Message:\n"


def _add(a, b):
    if a is None:
        return b
    if b is None:
        return a
    if a[0] == b[0] and (a[1] + b[1]) % _P == 0:
        return None
    lam = (3 * a[0] * a[0] * pow(2 * a[1], -1, _P)) if a == b else ((b[1] - a[1]) * pow(b[0] - a[0], -1, _P))
    x = (lam * lam - a[0] - b[0]) % _P
    return x, (lam * (a[0] - x) - a[1]) % _P


def _mul(k, pt):
    out = None
    while k:
        if k & 1:
            out = _add(out, pt)
        pt, k = _add(pt, pt), k >> 1
    return out


def _varstr(b: bytes) -> bytes:
    return (bytes([len(b)]) if len(b) < 0xfd else b"\xfd" + len(b).to_bytes(2, "little")) + b


def signer_of(message: str, sig_b64: str, version: int) -> str:
    """The P2PKH address whose key made a compact signed-message signature (Core signmessage), or ""."""
    try:
        sig = base64.b64decode(sig_b64)
        if len(sig) != 65:
            return ""
        head, r, s = sig[0], int.from_bytes(sig[1:33], "big"), int.from_bytes(sig[33:], "big")
        recid, compressed = (head - 27) & 3, head >= 31
        h = hashlib.sha256(hashlib.sha256(_varstr(MAGIC.encode()) + _varstr(message.encode())).digest()).digest()
        x = r + (recid // 2) * _N
        if x >= _P:
            return ""
        y = pow((x * x * x + 7) % _P, (_P + 1) // 4, _P)
        if (y & 1) != (recid & 1):
            y = _P - y
        e = int.from_bytes(h, "big")
        q = _mul(pow(r, -1, _N), _add(_mul(s, (x, y)), _mul((-e) % _N, _G)))
        if q is None:
            return ""
        pub = (bytes([2 + (q[1] & 1)]) + q[0].to_bytes(32, "big")) if compressed else \
            (b"\x04" + q[0].to_bytes(32, "big") + q[1].to_bytes(32, "big"))
        return b58check_encode(version, hash160(pub))
    except Exception:                                       # noqa: BLE001 -- unreadable is unproven
        return ""


def register(app, st, chains):
    v1 = st.v1
    rings = ringlib.Rings(lambda: None)
    tabs: dict[str, float] = {}
    waiting: dict[str, list[dict]] = {}
    version = chains.account_chain().params.pubkeyhash_version
    cert_kept: dict[str, Any] = {}

    def push():
        from arcade import push as pushlib
        return pushlib.Push(st.home) if pushlib.available() else None

    def owner(request: Request) -> tuple[str, str]:
        account = app.state.signed_in_account(request)
        return chains.account_address(account.pubkey, chains.account_chain()), account.pubkey

    def owners() -> dict[str, str]:
        out = {}
        for key, value in st.settings().items():
            if key.startswith("address:") and key.count(":") == 1 and isinstance(value, str) and value:
                out[value] = key.split(":", 1)[1]
        return out

    def rules(pubkey: str) -> dict:
        got = st.setting(f"ring:{pubkey}") or {}
        return {"apps": [a for a in got.get("apps") or [] if isinstance(a, str)],
                "allow": [a for a in got.get("allow") or [] if isinstance(a, str)]}

    def node_words() -> tuple[str, str]:
        hit = cert_kept.get("c")
        if hit and time.time() - hit[0] < 600:
            return hit[1]
        code, h = 0, None
        try:
            r = v1.raw("GET", "/realtime/hello")
            h = r.json() if r.status_code == 200 else None
        except Exception:                                   # noqa: BLE001
            h = None
        c = (h or {}).get("cert") or {}
        words = (str(c.get("network") or ""), str(c.get("node") or ""))
        cert_kept["c"] = (time.time(), words)
        return words

    def proved(address: str, expires: int, sig: str) -> bool:
        network, node = node_words()
        if not node or not expires or expires < time.time():
            return False
        message = f"arcade-realtime\n{network}\n{node}\n{address}\n{int(expires)}"
        return signer_of(message, sig, version) == address

    @app.get("/account/ring/apps")
    def ring_apps(request: Request):
        _, pubkey = owner(request)
        p = push()
        r = rules(pubkey)
        return JSONResponse({"apps": r["apps"], "allowed": len(r["allow"]), "push": p is not None,
                             "devices": len(p.devices(pubkey)) if p is not None else 0},
                            headers={"Cache-Control": "no-store"})

    @app.post("/account/ring/apps")
    def ring_apps_set(request: Request, payload: Any = Body(None)):
        _, pubkey = owner(request)
        said = payload if isinstance(payload, dict) else {}
        app_id = str(said.get("app") or "").lower()
        if not re.fullmatch(r"[0-9a-f]{64}", app_id):
            return JSONResponse({"detail": "that is not an app"}, status_code=400)
        kept = st.setting(f"ring:{pubkey}") or {}
        apps = [a for a in rules(pubkey)["apps"] if a != app_id]
        if said.get("on", True):
            if len(apps) >= ringlib.APPS_MAX:
                return JSONResponse({"detail": f"{ringlib.APPS_MAX} apps is the most"}, status_code=400)
            apps.append(app_id)
        st.set_setting(f"ring:{pubkey}", {**kept, "apps": apps})
        return JSONResponse({"ok": True, "apps": apps})

    @app.post("/account/ring/allow")
    def ring_allow_set(request: Request, payload: Any = Body(None)):
        _, pubkey = owner(request)
        said = payload if isinstance(payload, dict) else {}
        allow = said.get("allow")
        if not isinstance(allow, list) or len(allow) > ringlib.ALLOW_MAX:
            return JSONResponse({"detail": f"a list of at most {ringlib.ALLOW_MAX} addresses"}, status_code=400)
        allow = sorted({str(a) for a in allow if isinstance(a, str)
                        and re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{25,64}|[a-z0-9]{8,90}", str(a))})
        kept = st.setting(f"ring:{pubkey}") or {}
        st.set_setting(f"ring:{pubkey}", {**kept, "allow": allow})
        return JSONResponse({"ok": True, "allowed": len(allow)})

    @app.post("/account/ring")
    def ring_send(request: Request, payload: Any = Body(None)):
        me, _ = owner(request)
        said = payload if isinstance(payload, dict) else {}
        to, app_id = str(said.get("to") or "").strip()[:64], str(said.get("app") or "").lower()
        if not me:
            return JSONResponse({"ok": False, "error": "this account has no address here"}, status_code=400)
        if not re.fullmatch(r"[0-9a-f]{64}", app_id) or not to or to == me:
            return JSONResponse({"ok": False, "error": "ring whom, from which app?"}, status_code=400)
        try:
            expires = int(said.get("expires") or 0)
        except (TypeError, ValueError):
            expires = 0
        if not proved(me, expires, str(said.get("sig") or "")):
            return JSONResponse({"ok": False, "error": "unlock your wallet: a ring says who you are"}, status_code=400)
        if not rings.may_send(me):
            return JSONResponse({"ok": False, "error": "too many rings: wait a minute"}, status_code=429)
        held = owners()
        pubkey = held.get(to)
        if pubkey is None:
            return JSONResponse({"ok": True, "rung": False,
                                 "why": "they play on another arcade: rings between arcades are not on this one yet"})
        tab_open = time.time() - tabs.get(pubkey, 0) < ringlib.TAB_FOR
        p = push()
        r = rules(pubkey)
        if (p is None and not tab_open) or app_id not in r["apps"] or me not in r["allow"] \
                or not rings.may_ring(me, to):
            return JSONResponse({"ok": True, "rung": False})
        ring = {"id": ringlib.new_id(), "from": me, "to": to, "app": app_id, "at": time.time(),
                "line": ringlib.clean_line(said.get("line"), ringlib.RING_LINE),
                "link": str(said.get("link") or "")[:ringlib.RING_LINK]}
        rings.keep(ring)
        if tab_open:
            waiting.setdefault(pubkey, []).append(ring)
        if p is not None:
            import json
            p.tell(pubkey, f"ring:{ring['id']}", json.dumps({"f": me, "app": app_id, "line": ring["line"]}))
        return JSONResponse({"ok": True, "rung": True})

    def name_of(address: str) -> str:
        try:
            return str((v1.page(f"address/{address}")[1] or {}).get("tag") or "")
        except Exception:                                   # noqa: BLE001
            return ""

    def app_name(app_id: str) -> str:
        try:
            code, row = v1.page(f"inscription/{app_id}")
            j = (row or {}).get("json") if code == 200 else None
            return str((j.get("app") or {}).get("name") or j.get("name") or "an app") if isinstance(j, dict) else "an app"
        except Exception:                                   # noqa: BLE001
            return "an app"

    @app.get("/account/ring/live")
    def ring_live(request: Request):
        _, pubkey = owner(request)
        now = time.time()
        tabs[pubkey] = now
        kept = [r for r in waiting.get(pubkey, []) if now - r["at"] < ringlib.TAB_RING]
        if kept:
            waiting[pubkey] = kept
        else:
            waiting.pop(pubkey, None)
        out = []
        for ring in kept:
            tag = name_of(ring["from"])
            out.append({"id": ring["id"], "from": f"@{tag}" if tag else ring["from"][:10] + "…",
                        "app": app_name(ring["app"]), "line": ring["line"],
                        "url": f"/inscriptions/{ring['app']}/full?ring={ring['id']}"})
        return JSONResponse({"rings": out}, headers={"Cache-Control": "no-store"})

    @app.get("/account/ring/{ring_id}")
    def ring_get(request: Request, ring_id: str):
        me, _ = owner(request)
        ring = rings.get(ring_id)
        if ring is None or ring["to"] != me:
            return JSONResponse({"ok": False}, headers={"Cache-Control": "no-store"})
        tag = name_of(ring["from"])
        return JSONResponse({"ok": True, "from": ring["from"], "tag": tag, "app": ring["app"], "line": ring["line"],
                             "link": ring["link"], "at": ring["at"]}, headers={"Cache-Control": "no-store"})
