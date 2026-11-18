"""arcade.feed for app pages on this website (shims/feed.js): the public feed, read only, as Arcade V1.0's app window
reads it (its `feed`, `feed.channels` and `feed.file`), so an app that lists a channel's posts works here too. Writes
(posts, likes, replies, tips, follows) stay in the Arcade app. Answers are kept a few seconds: many readers of one app
ask for the same page.
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

from fastapi.responses import Response

from .v1 import V1Error

CORS = {"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"}
ORDERS = ("popular", "new")
KEEP_FOR = 10          # seconds an answer is reused
MAX_LIMIT = 100


def _json(v, status: int = 200) -> Response:
    return Response(json.dumps(v), status_code=status, media_type="application/json", headers=CORS)


def register(app, st, chains):
    v1 = st.v1
    shim = (Path(__file__).parent / "shims" / "feed.js").read_text()
    lock = threading.Lock()
    kept: dict[tuple, tuple[float, object]] = {}

    def read(key: tuple, cmd: str, **args):
        now = time.time()
        with lock:
            hit = kept.get(key)
            if hit and now - hit[0] < KEEP_FOR:
                return hit[1]
        got = v1.cmd(cmd, **args)
        with lock:
            if len(kept) > 2000:
                kept.clear()
            kept[key] = (now, got)
        return got

    @app.get("/r/feed.js")
    def r_feed_js():
        return Response(shim, media_type="application/javascript", headers={**CORS, "Cache-Control": "public, max-age=600"})

    @app.get("/r/feed")
    def r_feed(order: str = "popular", channel: str = "", tag: str = "", limit: int = 30, offset: int = 0):
        order = order if order in ORDERS else ("new" if order == "newest" else "popular")
        channel = channel.strip()[:40]
        tag = tag.strip().lstrip("@")[:40]
        if tag and not re.fullmatch(r"[A-Za-z0-9_.-]+", tag):
            return _json({"ok": False, "error": "tag: letters, digits, _ . -"}, 400)
        limit, offset = max(1, min(int(limit), MAX_LIMIT)), max(0, min(int(offset), 100_000))
        args = {"order": order, "limit": limit, "offset": offset}
        if channel:
            args["channel"] = channel
        if tag:
            args["tag"] = tag
        try:
            got = read(("feed", order, channel, tag, limit, offset), "feed", **args) or {}
        except V1Error as e:
            return _json({"ok": False, "error": str(e)}, 503)
        return _json({"ok": True, "result": {"posts": got.get("posts") or [], "total": got.get("total") or 0}})

    @app.get("/r/feed/channels")
    def r_feed_channels():
        try:
            return _json({"ok": True, "result": read(("channels",), "feed.channels")})
        except V1Error as e:
            return _json({"ok": False, "error": str(e)}, 503)

    @app.get("/r/feed/file")
    def r_feed_file(txid: str = ""):
        if not re.fullmatch(r"[0-9a-f]{64}", txid):
            return _json({"ok": False, "error": "txid: a post's transaction id"}, 400)
        try:
            return _json({"ok": True, "result": read(("file", txid), "feed.file", txid=txid)})
        except V1Error as e:
            return _json({"ok": False, "error": str(e)}, 404 if "no " in str(e).lower() else 503)
