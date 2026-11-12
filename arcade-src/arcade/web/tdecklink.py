"""T-Decks over WiFi (2026-10-26: "You should be able to connect to Wi-Fi and stay connected to Wi-Fi and while
you're connected to Wi-Fi have sign in and be done through Wi-Fi" ... "When connected to Wi-Fi, all messages and
transactions should be sent through Wi-Fi").

A T-Deck on WiFi speaks the very frames it sends over LoRa (arcade-lora's PROTOCOL.md), posted here over HTTPS instead
of carried by a radio. They go to a gateway this arcade runs for WiFi T-Decks -- the same arcade/lora Gateway as the
radio one, with no radio: sign-in, balances, payments and letters all work unchanged.

POST /r/tdeck/link, body = token[16] + n x (u16 length + frame). The token is the T-Deck's own random secret, kept on
its flash; it names the T-Deck's place here (a node number made from it, so its letters cursor survives), and nobody
without it can read that place's answers. The account itself is proven the usual way, by HELLO/LOGIN with its key.
The answer is n x (u16 length + frame): the replies to what was posted, and whatever waited for it (letters). A post
with no frames waits up to POLL_WAIT seconds for something to come, so letters reach the T-Deck within seconds.
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections import deque

from fastapi import Request
from fastapi.responses import Response

log = logging.getLogger("arcade.tdecklink")

TOKEN = 16
#: A post with nothing to send waits this long for an answer to come (the T-Deck asks again at once). 1 s, not 4
#: (2026-10-26): a frame the T-Deck sends while a post waits goes out only when it ends.
POLL_WAIT = 1.0
#: The most frames one post may carry, the largest frame, and how many may wait for a T-Deck that stopped asking.
FRAMES_MAX = 40
FRAME_MAX = 256
QUEUE_MAX = 200
#: How often the gateway looks for letters, and how long a WiFi T-Deck that stopped asking is still served.
MAIL_EVERY = 10.0
GONE_AFTER = 600.0
STATE_FILE = "tdeck-wifi.sqlite"


def node_of(token: bytes) -> int:
    """The T-Deck's place: 0xE0000000 and up, clear of real radio node numbers in practice and of broadcast."""
    return 0xE0000000 | (int.from_bytes(hashlib.sha256(b"tdeck-wifi\n" + token).digest()[:4], "big") & 0x0FFFFFFF)


def unpack(body: bytes) -> tuple[bytes, list[bytes]]:
    if len(body) < TOKEN:
        raise ValueError("no token")
    token, at, frames = body[:TOKEN], TOKEN, []
    while at < len(body):
        if at + 2 > len(body) or len(frames) >= FRAMES_MAX:
            raise ValueError("garbled")
        n = int.from_bytes(body[at:at + 2], "big")
        if not 0 < n <= FRAME_MAX or at + 2 + n > len(body):
            raise ValueError("garbled")
        frames.append(body[at + 2:at + 2 + n])
        at += 2 + n
    return token, frames


def pack(frames) -> bytes:
    return b"".join(len(f).to_bytes(2, "big") + f for f in frames)


class WifiLink:
    """The gateway for WiFi T-Decks, made at the first post; its thread checks letters while any T-Deck is here."""

    def __init__(self, state, gateway_factory=None, clock=time.monotonic):
        self.state = state
        self.clock = clock
        self.gw = None
        self.factory = gateway_factory or self._gateway
        self.lock = threading.Lock()
        self.ready = threading.Condition(self.lock)
        self.queues: dict[int, deque] = {}
        self.seen: dict[int, float] = {}
        self.thread: threading.Thread | None = None

    def _gateway(self):
        from ..lora.gateway import Gateway
        from ..lora.store import Store
        from pathlib import Path
        from .lora import LoraService
        cfg = LoraService(self.state).config()
        arcade = f"http://127.0.0.1:{int(getattr(self.state, 'port', 0) or 8420)}"
        return Gateway(arcade, self._send, name="WiFi", networks=cfg["chains"], assets=cfg.get("assets") or [],
                       store=Store(str(Path(self.state.home) / STATE_FILE)))

    def _send(self, node: int, frame: bytes) -> bool:
        with self.lock:
            q = self.queues.setdefault(node, deque(maxlen=QUEUE_MAX))
            q.append(frame)
            self.ready.notify_all()
        return True

    def _start(self):
        if self.gw is None:
            self.gw = self.factory()
        if self.thread is None or not self.thread.is_alive():
            self.thread = threading.Thread(target=self._run, name="tdeck-wifi", daemon=True)
            self.thread.start()

    def _run(self):
        next_mail = 0.0
        while True:
            time.sleep(1)
            now = self.clock()
            with self.lock:
                for node in [n for n, t in self.seen.items() if now - t > GONE_AFTER]:
                    del self.seen[node]
                    self.queues.pop(node, None)
                if not self.seen:
                    self.thread = None
                    return                                 # nobody here: started again at the next post
            try:
                if now >= next_mail:
                    self.gw.poll()
                    next_mail = now + MAIL_EVERY
                self.gw.tick()
            except Exception:                              # noqa: BLE001 -- one bad turn; the next one goes on
                log.warning("tdeck wifi: gateway turn failed", exc_info=True)

    def post(self, token: bytes, frames: list[bytes], wait: float = POLL_WAIT) -> list[bytes]:
        node = node_of(token)
        with self.lock:
            self._start()
            self.seen[node] = self.clock()
        self.gw.heard(node)                                # on: whatever waits for it goes now
        for frame in frames:
            self.gw.receive(node, frame)                   # the answers land in its queue as they are made
        with self.lock:
            q = self.queues.setdefault(node, deque(maxlen=QUEUE_MAX))
            if not q and not frames and wait > 0:
                self.ready.wait_for(lambda: bool(q), timeout=wait)
            out = list(q)
            q.clear()
        return out


def register(app, state) -> None:
    link = WifiLink(state)
    state.tdeck_wifi = link

    @app.post("/r/tdeck/link")
    async def tdeck_link(request: Request):
        """A WiFi T-Deck's frames in, its answers out (module docstring)."""
        from starlette.concurrency import run_in_threadpool
        body = await request.body()
        if len(body) > TOKEN + FRAMES_MAX * (FRAME_MAX + 2):
            return Response("too big", status_code=413)
        try:
            token, frames = unpack(body)
        except ValueError as exc:
            return Response(str(exc), status_code=400)
        # every gateway call off the event loop: its httpx calls come back into this uvicorn
        out = await run_in_threadpool(link.post, token, frames)
        return Response(pack(out), media_type="application/octet-stream", headers={"Cache-Control": "no-store"})
