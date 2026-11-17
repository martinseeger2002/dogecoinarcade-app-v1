"""arcade.svc for app pages on this website (shims/svc.js): the mesh's offers, and this arcade's own models helper
(arcade-models) reading aloud and answering for visitors, who have no helper of their own (2026-11-16, GOAT Nation on
phones). Only speak_at and chat_at pass, within a daily allowance per visitor; what they cost is paid by this
arcade's helper from its own credit.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from fastapi import Body, Request
from fastapi.responses import Response

from .v1 import V1Error

CORS = {"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"}
SPEECH_CHARS_A_DAY = 60_000         # about an hour and a half of reading
CHATS_A_DAY = 60
AT_ONCE = 4                         # a visitor's requests in flight
SPEECH_MAX = 1000
CHAT_MAX_CHARS = 16_000
CHAT_MAX_TOKENS = 1200


def _json(v, status: int = 200) -> Response:
    return Response(json.dumps(v), status_code=status, media_type="application/json", headers=CORS)


def register(app, st, chains):
    v1 = st.v1
    shim = (Path(__file__).parent / "shims" / "svc.js").read_text()
    lock = threading.Lock()
    used: dict[str, dict] = {}       # visitor -> {day, chars, chats, now}
    node = {"id": None}

    def me() -> str:
        if not node["id"]:
            node["id"] = (v1.cmd("status") or {}).get("node_id")
        return node["id"]

    def visitor(request: Request) -> str:
        account = app.state.signed_in(request) if hasattr(app.state, "signed_in") else None
        if account is not None:
            return "acct:" + account.pubkey
        ip = request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "")
        return "ip:" + ip

    @app.get("/r/svc.js")
    def r_svc_js():
        return Response(shim, media_type="application/javascript", headers={**CORS, "Cache-Control": "public, max-age=600"})

    @app.get("/r/svc/offers")
    def r_svc_offers(service: str = ""):
        try:
            got = v1.cmd("svc.offers", **({"service": service} if service else {})) or []
        except V1Error as e:
            return _json({"ok": False, "error": str(e)}, 503)
        here = me()
        keep = ("service", "arcade", "tag", "name", "offer", "at")
        return _json({"ok": True, "offers": [{**{k: o.get(k) for k in keep}, "here": o.get("arcade") == here} for o in got]})

    @app.post("/r/svc/local")
    def r_svc_local(request: Request, payload: dict = Body(default={})):
        if not isinstance(payload, dict) or payload.get("service") != "models":
            return _json({"ok": False, "error": "only the models service is open on this website"}, 403)
        body = payload.get("body") if isinstance(payload.get("body"), dict) else {}
        what = body.get("what")
        if what == "speak_at":
            text = str(body.get("input") or "")
            if not text.strip() or len(text) > SPEECH_MAX:
                return _json({"ok": False, "error": f"send 1 to {SPEECH_MAX} characters at a time"}, 400)
            ask = {k: body[k] for k in ("what", "seller", "model", "voice", "input") if body.get(k) is not None}
            ask["response_format"] = "wav"
            cost = ("chars", len(text), SPEECH_CHARS_A_DAY)
        elif what == "chat_at":
            msgs = body.get("messages") if isinstance(body.get("messages"), list) else []
            size = sum(len(str(m.get("content") or "")) for m in msgs if isinstance(m, dict))
            if not msgs or size > CHAT_MAX_CHARS:
                return _json({"ok": False, "error": f"send 1 to {CHAT_MAX_CHARS} characters of messages"}, 400)
            ask = {k: body[k] for k in ("what", "seller", "model", "messages", "temperature") if body.get(k) is not None}
            ask["max_tokens"] = max(16, min(int(body.get("max_tokens") or 900), CHAT_MAX_TOKENS))
            cost = ("chats", 1, CHATS_A_DAY)
        else:
            return _json({"ok": False, "error": "this website reads aloud (speak_at) and answers (chat_at); the rest needs the Arcade app"}, 403)
        who, day = visitor(request), time.strftime("%Y-%m-%d")
        with lock:
            u = used.get(who)
            if not u or u["day"] != day:
                u = used[who] = {"day": day, "chars": 0, "chats": 0, "now": 0}
            if u["now"] >= AT_ONCE:
                return _json({"ok": False, "error": "too many at once: wait for one to finish"}, 429)
            if u[cost[0]] + cost[1] > cost[2]:
                return _json({"ok": False, "error": "that's today's allowance on this website; the Arcade app has no limit"}, 429)
            u[cost[0]] += cost[1]; u["now"] += 1
            if len(used) > 50_000:
                for k in [k for k, x in used.items() if x["day"] != day]:
                    used.pop(k, None)
        try:
            got = v1.cmd("svc.ask", _timeout=330, to=me(), service="models", body=ask, timeout=300) or {}
            last = got.get("last") if isinstance(got, dict) else None
            if isinstance(last, dict) and last.get("error"):
                raise V1Error(str(last["error"]))
            return _json({"ok": True, "result": last})
        except V1Error as e:
            with lock:
                u[cost[0]] -= cost[1]                   # a failure costs the visitor nothing
            return _json({"ok": False, "error": str(e)}, 502)
        finally:
            with lock:
                u["now"] -= 1
