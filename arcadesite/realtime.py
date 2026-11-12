"""Realtime rooms for inscribed pages, through Arcade V1.0's room door (its /realtime/ routes for programs).

The viewer (_page_doors) asks these routes on its page's behalf, exactly as it asked the original's:

    GET  /realtime/hello                  V1.0's: is the mesh up, the certificate words to sign
    POST /realtime/join                   into a room: a signed-in account as its address (its browser signed V1.0's
                                          certificate words), a signed guest address, or a plain guest
    POST /realtime/send, /realtime/leave
    GET  /realtime/stream?token=          the room's events (server-sent), passed through from V1.0
    POST /realtime/hub/attach|detach, GET /realtime/hub?id=
                                          many rooms on one stream (kept here, fed by V1.0's streams)
    GET  /realtime/nodes                  ways in a browser may use: this site
    GET  /realtime/ice                    STUN, and a relay for a signed-in player: Arcade V1.0's media relays
                                          (realtime.ice: its own, or borrowed from linked arcades), else the original
                                          arcades' relays in node-relays.json, else the operator's Cloudflare TURN key

A signed-in account joins as its own address only: the address comes from its session, never from the request.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import secrets
import time

import httpx
from fastapi import Request

from .v1 import V1Error
from fastapi.responses import JSONResponse, Response, StreamingResponse

PUBLIC_STUN = "stun:stun.cloudflare.com:3478"
TURN_API = "https://rtc.live.cloudflare.com/v1/turn/keys/{key}/credentials/generate-ice-servers"
TURN_TTL = 6 * 3600
#: Arcade nodes' media relays (the original's mesh/turn.py, which every public original arcade runs on its STUN port), in
#: <home>/node-relays.json, readable by this user only: [{"name", "urls": ["turn:host:3478?transport=udp", ...],
#: "secret": hex}]. A node's secret is blake2b(its mesh key, 32 bytes, person "arcm-turn"): one way, so it lets this site
#: give out logins on that relay and nothing else. A login is "<expiry>:<who>" and an HMAC of it (turn.login).
NODE_RELAYS_FILE = "node-relays.json"
HEARTBEAT = 15.0
HUB_MAX_ROOMS = 16
HUB_IDLE = 120.0
CORS = {"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"}


def register(app, st, chains):
    v1 = st.v1
    base = v1.base
    client = httpx.AsyncClient(base_url=base, timeout=httpx.Timeout(30.0, read=None))
    hubs: dict[str, dict] = {}

    async def door(path: str, body: dict) -> Response:
        try:
            r = await client.post(f"/realtime/{path}", json=body, timeout=30.0)
        except httpx.HTTPError as e:
            return JSONResponse({"ok": False, "error": f"the arcade did not answer: {e}"}, status_code=503)
        return Response(r.content, status_code=r.status_code, media_type="application/json", headers=CORS)

    @app.get("/realtime/hello")
    async def hello(request: Request):
        """V1.0's hello, with what the viewer needs to sign a signed-in player in (the original's realtime_hello): the
        node and network beside the certificate, `who` is looking, and for an account the words its wallet signs
        (`sign`: message, expires, the chain whose key signs). Without `who`/`sign` the viewer joins as a guest, and a
        guest is never answered by a game's Bank (ASHVALE started signed-in players as new characters, 2026-11-11)."""
        try:
            r = await client.get("/realtime/hello", timeout=15.0)
            said = r.json()
        except (httpx.HTTPError, ValueError) as e:
            return JSONResponse({"online": False, "why": f"the arcade did not answer: {e}"}, headers=CORS)
        if r.status_code != 200 or not isinstance(said, dict):
            return Response(r.content, status_code=r.status_code, media_type="application/json", headers=CORS)
        cert = said.get("cert") or {}
        out = {"ok": True, **said, "node": cert.get("node"), "network": cert.get("network")}
        account = app.state.signed_in(request)
        home = chains.account_chain()
        address = chains.account_address(account.pubkey, home) if account is not None else ""
        if address and said.get("online") and cert.get("node") and cert.get("network"):
            tag = str((st.vault().by_pubkey(account.pubkey) or {}).get("tag") or "") or None
            out["who"] = {"kind": "account", "address": address, "tag": tag}
            expires = int(cert.get("expires") or 0)
            out["sign"] = {"message": f"arcade-realtime\n{cert['network']}\n{cert['node']}\n{address}\n{expires}",
                           "expires": expires,
                           "chain": {"network": home.network, "version": home.params.pubkeyhash_version}}
        else:
            out["who"] = {"kind": "guest", "address": None, "tag": None}
        headers = dict(CORS)
        if account is not None:
            headers.pop("Access-Control-Allow-Origin", None)    # who is signed in is this site's own page's business
        return JSONResponse(out, headers=headers)

    @app.post("/realtime/join")
    async def join(request: Request):
        try:
            body = await request.json()
        except ValueError:
            body = {}
        body = body if isinstance(body, dict) else {}
        out = {"game": str(body.get("game") or ""), "room": str(body.get("room") or "")}
        account = app.state.signed_in(request)
        if account is not None and body.get("sig"):
            out.update(address=chains.account_address(account.pubkey, chains.account_chain()),
                       expires=int(body.get("expires") or 0), sig=str(body["sig"]))
        elif body.get("sig") and body.get("address"):
            out.update(address=str(body["address"])[:64], expires=int(body.get("expires") or 0), sig=str(body["sig"]))
        return await door("join", out)

    @app.post("/realtime/send")
    async def send(request: Request):
        try:
            body = await request.json()
        except ValueError:
            body = {}
        body = body if isinstance(body, dict) else {}
        out = {"token": str(body.get("token") or "")}
        if "b64" in body:
            out["b64"] = str(body["b64"])
        else:
            out["data"] = str(body.get("data", ""))
        return await door("send", out)

    @app.post("/realtime/leave")
    async def leave(request: Request):
        try:
            body = await request.json()
        except ValueError:
            body = {}
        return await door("leave", {"token": str((body or {}).get("token") or "")})

    async def upstream(token: str):
        """V1.0's server-sent events for one seat, line by line."""
        async with client.stream("GET", "/realtime/stream", params={"token": token}) as r:
            if r.status_code != 200:
                yield "event: closed\ndata: " + json.dumps({"why": f"HTTP {r.status_code}"}) + "\n\n"
                return
            async for chunk in r.aiter_text():
                yield chunk

    @app.get("/realtime/stream")
    async def stream(request: Request, token: str = ""):
        async def events():
            try:
                async for chunk in upstream(token):
                    if await request.is_disconnected():
                        return
                    yield chunk
            except httpx.HTTPError:
                yield "event: closed\ndata: {\"why\":\"the arcade stopped answering\"}\n\n"
        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={**CORS, "X-Accel-Buffering": "no"})

    # ---------------------------------------------------------------- many rooms on one stream
    def tidy() -> None:
        now = time.monotonic()
        for hid, hub in list(hubs.items()):
            if not hub["streaming"] and now - hub["at"] > HUB_IDLE:
                for task in hub["pumps"].values():
                    task.cancel()
                hubs.pop(hid, None)

    async def pump(hub: dict, token: str) -> None:
        buf = ""
        try:
            async for chunk in upstream(token):
                buf += chunk
                while "\n\n" in buf:
                    block, buf = buf.split("\n\n", 1)
                    lines = block.split("\n")
                    if any(l.startswith("event: closed") for l in lines):
                        await hub["q"].put(("closed", token, "the seat is gone; join again"))
                        return
                    for l in lines:
                        if l.startswith("data: "):
                            await hub["q"].put(("data", token, l[6:]))
            await hub["q"].put(("closed", token, "the stream ended"))
        except asyncio.CancelledError:
            raise
        except httpx.HTTPError as e:
            await hub["q"].put(("closed", token, str(e)))
        finally:
            hub["pumps"].pop(token, None)

    @app.post("/realtime/hub/attach")
    async def hub_attach(request: Request):
        try:
            body = await request.json()
        except ValueError:
            body = {}
        token, hid = str((body or {}).get("token") or ""), str((body or {}).get("hub") or "")
        if not token:
            return JSONResponse({"ok": False, "error": "no such seat"}, status_code=404)
        tidy()
        hub = hubs.get(hid)
        if hub is None:
            hid = secrets.token_urlsafe(16)
            hub = hubs[hid] = {"q": asyncio.Queue(maxsize=4096), "pumps": {}, "streaming": 0, "at": time.monotonic()}
        if token not in hub["pumps"]:
            if len(hub["pumps"]) >= HUB_MAX_ROOMS:
                return JSONResponse({"ok": False, "error": "too many rooms on one stream"}, status_code=400)
            hub["pumps"][token] = asyncio.get_running_loop().create_task(pump(hub, token))
        return JSONResponse({"ok": True, "hub": hid}, headers=CORS)

    @app.post("/realtime/hub/detach")
    async def hub_detach(request: Request):
        try:
            body = await request.json()
        except ValueError:
            body = {}
        hub = hubs.get(str((body or {}).get("hub") or ""))
        task = hub["pumps"].pop(str((body or {}).get("token") or ""), None) if hub else None
        if task is not None:
            task.cancel()
        return JSONResponse({"ok": True}, headers=CORS)

    @app.get("/realtime/hub")
    async def hub_stream(request: Request, id: str = ""):
        hub = hubs.get(id)
        if hub is None:
            return JSONResponse({"ok": False, "error": "no such stream"}, status_code=404)

        async def events():
            hub["streaming"] += 1
            try:
                yield "retry: 2000\n\n"
                while not await request.is_disconnected():
                    try:
                        kind, token, ev = await asyncio.wait_for(hub["q"].get(), HEARTBEAT)
                    except asyncio.TimeoutError:
                        yield "event: ping\ndata: {}\n\n"
                        continue
                    if kind == "closed":
                        yield "event: closed\ndata: " + json.dumps({"k": token, "why": ev}) + "\n\n"
                    else:
                        yield "data: {\"k\": " + json.dumps(token) + ", \"ev\": " + ev + "}\n\n"
            finally:
                hub["streaming"] -= 1
                hub["at"] = time.monotonic()
        return StreamingResponse(events(), media_type="text/event-stream", headers={**CORS, "X-Accel-Buffering": "no"})

    # ---------------------------------------------------------------- ways in, and ICE
    @app.get("/realtime/nodes")
    def nodes(request: Request):
        hosts = sorted(st.public_hosts)
        own = [{"base": f"https://{h}", "node": None} for h in hosts if h]
        return JSONResponse({"ok": True, "nodes": own[:1]}, headers=CORS)

    relay_kept: dict[str, tuple[float, list]] = {}

    def turn_servers(key_id: str, token: str) -> list[dict]:
        """Fresh logins to the operator's Cloudflare TURN key (the original's turn_servers)."""
        import requests
        r = requests.post(TURN_API.format(key=key_id), timeout=10, json={"ttl": TURN_TTL},
                          headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        if not 200 <= r.status_code < 300:
            raise RuntimeError(f"Cloudflare answered {r.status_code}")
        said = r.json().get("iceServers")
        servers = said if isinstance(said, list) else [said] if isinstance(said, dict) else []
        return [{k: v for k, v in x.items() if k in ("urls", "username", "credential")}
                for x in servers if isinstance(x, dict) and x.get("username") and x.get("credential")]

    def relay_for(request: Request, app_id: str) -> list[dict]:
        """The operator's TURN relay (settings `turn`: key_id, token, apps), for a signed-in player only, in the apps
        the operator chose (none chosen: every app); kept half its life per player."""
        turn = st.setting("turn") or {}
        if not (isinstance(turn, dict) and turn.get("key_id") and turn.get("token")):
            return []
        chosen = turn.get("apps")
        if isinstance(chosen, list) and str(app_id or "").lower() not in chosen:
            return []
        account = app.state.signed_in(request)
        if account is None:
            return []
        kept = relay_kept.get(account.pubkey)
        if kept and time.time() - kept[0] < TURN_TTL / 2:
            return kept[1]
        try:
            servers = turn_servers(str(turn["key_id"]), str(turn["token"]))
        except Exception:                                   # noqa: BLE001 -- no relay this time: direct only
            return []
        relay_kept[account.pubkey] = (time.time(), servers)
        return servers

    def v1_relays(request: Request) -> list[dict]:
        """Logins on Arcade V1.0's media relays for a signed-in player (realtime.ice: this site's V1.0's own relay,
        or relays it borrows from linked arcades). Empty on a V1.0 without the relay."""
        account = app.state.signed_in(request)
        if account is None:
            return []
        try:
            got = v1.cmd("realtime.ice", _timeout=8.0, tag=f"site:{account.pubkey}") or {}
        except V1Error:
            return []
        return [x for x in got.get("iceServers") or [] if isinstance(x, dict) and x.get("urls") and x.get("username")]

    def node_relays(request: Request) -> list[dict]:
        """Logins on the arcade nodes' own media relays, for a signed-in player, in any app (the operator 2026-11-12:
        node relays rather than Cloudflare TURN). Each login lasts TURN_TTL and names the player only by a hash."""
        account = app.state.signed_in(request)
        if account is None:
            return []
        try:
            relays = json.loads((st.home / NODE_RELAYS_FILE).read_text())
        except (OSError, ValueError):
            return []
        who = hashlib.blake2b(str(account.pubkey).encode(), digest_size=6).hexdigest()
        username = f"{int(time.time() + TURN_TTL)}:{who}"
        out = []
        for r in relays if isinstance(relays, list) else []:
            try:
                secret = bytes.fromhex(str(r["secret"]))
                urls = [u for u in r["urls"] if isinstance(u, str) and u.startswith("turn:")]
            except (KeyError, TypeError, ValueError):
                continue
            if urls and len(secret) == 32:
                password = base64.b64encode(hmac.new(secret, username.encode(), hashlib.sha256).digest()).decode()
                out.append({"urls": urls, "username": username, "credential": password})
        return out

    @app.get("/realtime/ice")
    def ice(request: Request):
        servers = [{"urls": PUBLIC_STUN}] if st.setting("realtime_public_stun", True) is not False else []
        # The arcades' own relays first (V1.0's, then the original arcades' listed here); the operator's Cloudflare TURN
        # only when there is no arcade relay.
        relay = v1_relays(request) or node_relays(request) or relay_for(request, request.query_params.get("app", ""))
        servers.extend(relay)
        return JSONResponse({"ok": True, "iceServers": servers, "relay": bool(relay)}, headers=CORS)
