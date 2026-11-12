"""The public feed, read from Arcade V1.0 (`feed`), drawn by the original's feed.html.

V1.0's posts become the original's feedview.Shown rows (the template's shape). What V1.0 says about "mine" and "my
opinion" is about its own wallet, so here a post is the reader's when its author is the reader's address, and the
reader's own likes are not marked (V1.0 does not answer that for another address yet).
"""
from __future__ import annotations

import re
import time

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse

from arcade import feedview
from arcade import tags as taglib
from arcade.messaging import feed as feedlib

from . import legacy as L
from .v1 import V1Error

PER_PAGE = 20
CONTENT = re.compile(r"/content/([0-9a-f]{64})")


def _shown(p: dict, me: str, reply: bool = False) -> feedview.Shown:
    author = str((p.get("author") or {}).get("address") or "")
    s = feedview.Shown(txid=str(p.get("txid") or ""), author=author, text=str(p.get("text") or ""),
                       height=int(p.get("height") or 0),
                       block_time=int(p.get("time") or p.get("block_time")
                                      or (time.time() - float(p.get("age_hours") or 0) * 3600)),
                       mine=bool(me) and author == me, edited=bool(p.get("edited")),
                       likes=int(p.get("likes") or 0), dislikes=int(p.get("dislikes") or 0),
                       shares=int(p.get("shares") or 0), tips=int(p.get("tips") or 0),
                       is_reply=reply, muted=bool(p.get("muted")))
    s.replies = [_shown(r, me, True) for r in p.get("replies") or []]
    return s


def register(app, st, chains):
    v1 = st.v1

    def me_of(request: Request) -> dict:
        account = app.state.signed_in(request)
        home, tag = "", None
        if account is not None:
            home = chains.account_address(account.pubkey, chains.account_chain())
            tag = (str((v1.page(f"address/{home}")[1] or {}).get("tag") or "") if home else "") \
                or (st.vault().by_pubkey(account.pubkey) or {}).get("tag") or None
        return {"tag": tag, "address": home, "chain": chains.account_chain(), "min": taglib.MIN_LENGTH,
                "max": taglib.MAX_LENGTH}

    def bylines(shown: list) -> dict:
        out, stack = {}, list(shown)
        while stack:
            p = stack.pop()
            stack.extend(p.replies)
            if p.author and p.author not in out:
                try:
                    who = v1.cmd("msg.who", name=p.author) or {}
                except V1Error:
                    who = {}
                pfp = str(who.get("pfp") or "")
                out[p.author] = {"tag": str(who.get("tag") or ""), "face": pfp}
        return out

    def drawable(shown: list) -> dict:
        """{inscription id: content type} for the pictures and pages the posts name (post_html draws those)."""
        out, stack = {}, list(shown)
        while stack:
            p = stack.pop()
            stack.extend(p.replies)
            for piece in CONTENT.findall(p.text or ""):
                if piece in out:
                    continue
                try:
                    code, row = v1.page(f"inscription/{piece}")
                    out[piece] = str((row or {}).get("contenttype") or "") if code == 200 else ""
                except V1Error:
                    out[piece] = ""
        return out

    #: The most accounts the Following feed reads (one V1.0 feed question each), newest follows first.
    FOLLOWING_CAP = 60

    def following_posts(address: str, offset: int, limit: int) -> tuple[list, int, str]:
        """Posts by the accounts `address` follows (Arcade V1.0 `follows`, newest act between two accounts counts),
        newest first: (page, total, note). One `feed` question per followed account, merged by height (waiting
        posts first)."""
        if not address:
            return [], 0, "Sign in to see the posts of the people you follow."
        try:
            people = [str(r.get("address") or "") for r in (v1.cmd("follows", who=address) or {}).get("following_list") or []]
        except V1Error:
            return [], 0, "The arcade behind this site does not keep follows yet."
        people = [a for a in people if a][:FOLLOWING_CAP]
        if not people:
            return [], 0, "You follow nobody yet. Follow people from their profiles, and their posts show here."
        rows, seen = [], set()
        for a in people:
            try:
                got = (v1.cmd("feed", order="new", author=a, limit=offset + limit) or {}).get("posts") or []
            except V1Error:
                continue
            for p in got:
                if p.get("txid") not in seen and str((p.get("author") or {}).get("address") or "") == a:
                    seen.add(p.get("txid"))
                    rows.append(p)
        rows.sort(key=lambda p: (p.get("height") is not None, -(p.get("height") or 0), float(p.get("age_hours") or 0)))
        return rows[offset:offset + limit], len(rows), "" if rows else "Nobody you follow has posted yet."

    app.state.following_posts = following_posts

    @app.get("/feed", response_class=HTMLResponse)
    def feed_page(request: Request, before: str | None = None, sort: str = "popular", post: str | None = None):
        if sort not in ("popular", "new", "friends", "following"):
            raise HTTPException(400, "sort is popular, new, friends or following")
        offset = 0
        if before not in (None, ""):
            if not str(before).isdigit():
                raise HTTPException(400, "that is not a feed cursor")
            offset = int(before)
        mine = me_of(request)
        wanted = (post or "").strip().lower()
        missing, named, following_note = "", "", ""
        if wanted and not re.fullmatch(r"[0-9a-f]{64}", wanted):
            named, posts, total = wanted, [], 0
        elif wanted:
            got = v1.cmd("feed", txid=wanted) or {}
            posts, total = got.get("posts") or [], 0
            if not posts:
                missing = wanted
        elif sort == "following":
            posts, total, following_note = following_posts(mine["address"], offset, PER_PAGE)
        else:
            got = v1.cmd("feed", order="new" if sort in ("new", "friends") else "popular", limit=PER_PAGE,
                         offset=offset) or {}
            posts, total = got.get("posts") or [], int(got.get("total") or 0)
        shown = [_shown(p, mine["address"]) for p in posts]
        cursor = str(offset + PER_PAGE) if not wanted and offset + PER_PAGE < total else None
        drawn = drawable(shown)
        return app.state.render(
            request, "feed.html", chain=chains.account_chain(), posts=shown, bylines=bylines(shown), drawable=drawn,
            frames={k: app.state.frame_src(request, k) for k, t in drawn.items() if t == "text/html"}, cursor=cursor, whose=None, missing=missing, not_an_id=bool(named), focus="",
            here="/feed" if sort == "popular" else f"/feed?sort={sort}", sort=sort, mine=mine,
            kinds=feedlib.BY_NAME, friends=[], when=L._local_time, node={}, loading=None,
            following_note=following_note)
