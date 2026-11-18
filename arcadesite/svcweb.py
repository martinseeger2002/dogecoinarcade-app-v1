"""arcade.svc for app pages on this website (shims/svc.js): the mesh's offers, and this arcade's own models helper
(arcade-models) reading aloud and answering for visitors, who have no helper of their own (2026-11-16, GOAT Nation on
phones). Only speak_at and chat_at pass, within a daily allowance per visitor; what they cost is paid by this
arcade's helper from its own credit.

`ask` (POST /r/svc/ask): a service's free reads, asked in this website's arcade's name for visitors who have no
arcade of their own here. A service opts in by naming those asks in its offer, `"reads": ["get", "has"]`; any other
ask is refused (it may cost money or write), and needs the Arcade app. Capped: 16 KB asked, 4 MB answered, 60 s,
120 asks a minute and 4 at once per visitor, 16 at once in all.
"""
from __future__ import annotations

import json
import secrets
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
# Asks in this website's own name (POST /r/svc/ask): free reads only, as each service's offer names them.
ASK_MAX_BODY = 16 * 1024            # the ask's body, as JSON
ASK_MAX_ANSWER = 4 << 20            # the answer's bytes (its blobs, decoded)
ASK_WAIT = 60                       # seconds the service has (the edge in front of this site waits ~100)
ASK_AT_ONCE = 4                     # a visitor's asks in flight
ASKS_A_MINUTE = 120                 # a visitor's asks a minute
ASKS_AT_ONCE = 16                   # everyone's asks in flight
OFFERS_FOR = 30                     # seconds the offers are kept


def _json(v, status: int = 200) -> Response:
    return Response(json.dumps(v), status_code=status, media_type="application/json", headers=CORS)


def register(app, st, chains):
    v1 = st.v1
    shim = (Path(__file__).parent / "shims" / "svc.js").read_text()
    lock = threading.Lock()
    used: dict[str, dict] = {}       # visitor -> {day, chars, chats, now}
    node = {"id": None}
    jobs: dict[str, dict] = {}       # request id -> {state, result | error, at}

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
        # Answered at once with a job the page asks after (GET /r/svc/job/<id>): a voice server that was asleep can
        # take longer to wake than the edge in front of this site waits for one request.
        jid = secrets.token_urlsafe(12)
        job = jobs[jid] = {"state": "working", "at": time.time()}

        def run():
            try:
                got = v1.cmd("svc.ask", _timeout=330, to=me(), service="models", body=ask, timeout=300) or {}
                last = got.get("last") if isinstance(got, dict) else None
                if isinstance(last, dict) and last.get("error"):
                    raise V1Error(str(last["error"]))
                job.update(state="done", result=last)
            except Exception as e:  # noqa: BLE001
                with lock:
                    u[cost[0]] -= cost[1]               # a failure costs the visitor nothing
                job.update(state="failed", error=str(e))
            finally:
                with lock:
                    u["now"] -= 1
                    for k in [k for k, j in jobs.items() if time.time() - j["at"] > 900]:
                        jobs.pop(k, None)
        threading.Thread(target=run, daemon=True).start()
        return _json({"ok": True, "job": jid}, 202)

    # ------------------------------------------------------------ asks in this website's own name, reads only
    # A service names in its offer the asks that cost nothing and change nothing: `"reads": ["get", "has"]`. Only
    # those pass here, asked in this website's arcade's name, for every visitor within caps; every other ask (paid,
    # or writing) still needs the Arcade app, where the player asks in their own name.
    offers_kept: dict = {"at": 0.0, "rows": []}
    asking = {"now": 0}

    def offer_of(to: str, service: str) -> dict | None:
        now = time.time()
        if now - offers_kept["at"] > OFFERS_FOR:
            offers_kept["rows"] = v1.cmd("svc.offers") or []
            offers_kept["at"] = now
        for o in offers_kept["rows"]:
            if o.get("arcade") == to and o.get("service") == service:
                return o.get("offer") if isinstance(o.get("offer"), dict) else {}
        return None

    @app.post("/r/svc/ask")
    def r_svc_ask(request: Request, payload: dict = Body(default={})):
        to, service = str(payload.get("to") or ""), str(payload.get("service") or "")
        body = payload.get("body") if isinstance(payload.get("body"), dict) else None
        if not (len(to) == 64 and all(c in "0123456789abcdef" for c in to)) or not service or body is None:
            return _json({"ok": False, "error": "to (an arcade's node id), service and body {what, ...}"}, 400)
        if len(json.dumps(body)) > ASK_MAX_BODY:
            return _json({"ok": False, "error": f"body: at most {ASK_MAX_BODY} bytes"}, 413)
        try:
            offer = offer_of(to, service)
        except V1Error as e:
            return _json({"ok": False, "error": str(e)}, 503)
        if offer is None:
            return _json({"ok": False, "error": f"no {service} offer from that arcade"}, 404)
        reads = offer.get("reads") if isinstance(offer.get("reads"), list) else []
        if body.get("what") not in reads:
            return _json({"ok": False, "error": "this website asks a service only what its offer names as free reads "
                                                 f"({', '.join(map(str, reads)) or 'none'}); the rest needs the Arcade app"}, 403)
        who, minute = visitor(request), int(time.time() // 60)
        with lock:
            u = used.setdefault("ask:" + who, {"day": "", "chars": 0, "chats": 0, "now": 0, "min": 0, "n": 0})
            if u["min"] != minute:
                u["min"], u["n"] = minute, 0
            if u["now"] >= ASK_AT_ONCE or asking["now"] >= ASKS_AT_ONCE:
                return _json({"ok": False, "error": "too many at once: wait for one to finish"}, 429)
            if u["n"] >= ASKS_A_MINUTE:
                return _json({"ok": False, "error": f"at most {ASKS_A_MINUTE} asks a minute on this website"}, 429)
            u["n"] += 1; u["now"] += 1; asking["now"] += 1
        try:
            got = v1.cmd("svc.ask", _timeout=ASK_WAIT + 15, to=to, service=service, body=body, timeout=ASK_WAIT) or {}
        except V1Error as e:
            return _json({"ok": False, "error": str(e)}, 502)
        finally:
            with lock:
                u["now"] -= 1; asking["now"] -= 1
        answers = got.get("answers") if isinstance(got, dict) and isinstance(got.get("answers"), list) else []
        if sum(len(str(a.get("blob") or "")) for a in answers if isinstance(a, dict)) * 3 // 4 > ASK_MAX_ANSWER:
            return _json({"ok": False, "error": f"the answer is over {ASK_MAX_ANSWER >> 20} MB: ask for less"}, 413)
        return _json({"ok": True, "last": got.get("last") if isinstance(got, dict) else None, "answers": answers})

    @app.get("/r/svc/job/{jid}")
    def r_svc_job(jid: str):
        job = jobs.get(jid)
        if not job:
            return _json({"ok": False, "error": "no such request (they are kept 15 minutes)"}, 404)
        if job["state"] == "working":
            return _json({"ok": True, "state": "working"})
        out = jobs.pop(jid)
        if out["state"] == "failed":
            return _json({"ok": False, "error": out["error"]}, 502)
        return _json({"ok": True, "state": "done", "result": out["result"]})
