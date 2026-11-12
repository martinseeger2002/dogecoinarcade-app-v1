"""Pure helpers copied unchanged from the original arcade's web app (arcade/web/app.py), which cannot be imported
without starting the old app: post rendering, time and amount filters, the secure-context check."""
from __future__ import annotations

import datetime as dt
import html
import re
import time
from typing import Any

from markupsafe import Markup
from fastapi import Request

from arcade.ledger import format_amount

def _fromjson(text: str):
    """A template filter: the parsed JSON, or None for anything that is not."""
    import json as _json
    try:
        return _json.loads(text) if text else None
    except ValueError:
        return None


def _ago(when: Any) -> str:
    """A template filter: how long ago, in the words people use out loud.

    A block time is a number nobody reads as a moment. "4 minutes ago" is
    what a sale feed is for -- whether the market is alive right now.
    """
    if not when:
        return ""             # no time at all, rather than 1970
    try:
        seconds = time.time() - float(when)
    except (TypeError, ValueError):
        return ""
    if seconds < 0:
        seconds = 0
    for size, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if seconds >= size:
            return f"{int(seconds // size)}{unit} ago"
    return "just now"


def _coins(sats: Any) -> str:
    """A template filter: satoshis as coins, for the totals a feed card keeps."""
    try:
        return format_amount(int(sats or 0), True)
    except (TypeError, ValueError):
        return "?"


#: An inscription named in a post: a bare id is not enough, because a post is
#: prose and sixty-four hex characters can be anything. `/content/<id>` is the
#: same spelling a token icon and a collection thumbnail use (D-103).
_CONTENT_IN_TEXT = re.compile(r"/content/([0-9a-f]{64})")


#: An @name in a post, a comment or a message (2026-09-26: "if someone is
#: @tagged in a post or comment it should show up like a link to their feed").
#: Matched on the ESCAPED text, so it can only ever wrap characters a tag may
#: have; not after a word character, so an email address stays an address.
_MENTION = re.compile(r"(?<![\w@/.])@([A-Za-z0-9_]{2,24})(?![\w@])")


def _mentions_linked(escaped: str) -> str:
    return _MENTION.sub(
        lambda m: f'<a class="mention" href="/u/{m.group(1).lower()}">@{m.group(1)}</a>',
        escaped)


def mentioned_in(text: str) -> set[str]:
    """Every @name a text names, lower case."""
    return {m.group(1).lower() for m in _MENTION.finditer(text or "")}


def post_html(text: str, drawable: dict[str, str] | None = None,
              frames: dict[str, str] | None = None) -> str:
    """One post's words as HTML: escaped first, then its inscriptions drawn.

    Everything is escaped before anything is added, so a post that contains
    angle brackets is a post that contains angle brackets. What is added back
    is decided HERE, from what the index says the inscription is -- never
    from anything the post says about itself (D-138).

    * a picture is shown;
    * a page goes in the sandboxed frame the viewer already uses, which is
      the one whose guarantees D-121 put back under test;
    * anything else stays a link, because a thing this node cannot identify
      is a thing it should not be drawing.
    """
    drawable = drawable or {}
    out = []
    last = 0
    for found in _CONTENT_IN_TEXT.finditer(text or ""):
        out.append(_mentions_linked(html.escape((text or "")[last:found.start()])))
        piece = found.group(1)
        kind = drawable.get(piece, "")
        if kind.startswith("image/"):
            out.append(f'<img class="postmedia" src="/content/{piece}" alt="" '
                       f'loading="lazy">')
        elif kind == "text/html":
            # Same sandbox as the inscription viewer: no same-origin, so it
            # cannot reach this page, this wallet or anybody's storage.
            # `frames` gives the pages host and the viewer's ticket, as the
            # piece page does, so an embed greets whoever reads the feed.
            src = html.escape((frames or {}).get(piece) or f"/content/{piece}")
            out.append(f'<iframe class="inscription-frame postmedia" '
                       f'src="{src}" loading="lazy" '
                       f'sandbox="allow-scripts allow-pointer-lock"></iframe>')
            # Full screen, inside the app so the wallet comes along (2026-09-29).
            out.append(f'<a class="fulllink" href="/inscriptions/{piece}/full">'
                       f'&#x2922; Full screen</a>')
        else:
            out.append(f'<a href="/inscriptions/{piece}/view">'
                       f'/content/{piece[:12]}…</a>')
        last = found.end()
    out.append(_mentions_linked(html.escape((text or "")[last:])))
    return "".join(out)


def inscriptions_in(text: str) -> list[str]:
    """Every inscription a post names, so a page can ask about them at once."""
    return _CONTENT_IN_TEXT.findall(text or "")


#: Hostnames a browser treats as a secure context over plain http. Everywhere
#: else -- a LAN address, a name on the local network -- `crypto.subtle` is
#: simply undefined, so a page that needs it must say why rather than fail
#: half way through somebody's signup with "undefined is not an object".
LOOPBACK = ("localhost", "127.0.0.1", "::1", "[::1]")


WHY_NOT_SECURE = (
    "This page was not served over https, so the browser will not let it use "
    "its own cryptography \u2014 and a wallet that cannot generate a key "
    "cannot sign anything. Open the wallet at localhost on the machine it "
    "runs on, or through the https address on its Remote page. This is a "
    "browser rule, not ours, and it is the right one: a key made over plain "
    "http is a key anybody on the network watched being made.")


def secure_context(request: Request) -> bool:
    """Whether `crypto.subtle` will exist on the page we are about to send."""
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    if proto == "https":
        return True
    name = (request.headers.get("host", "").rsplit(":", 1)[0]).strip().lower()
    # *.localhost too, as browsers treat it (an ssh tunnel opened as node.localhost:8430 is a secure context).
    return name in LOOPBACK or name.startswith("127.") or name.endswith(".localhost")


def _local_time(ts: Any, style: str = "full") -> Markup:
    """A moment, as the reader's clock says it (see base.html `arcadeLocalTimes`).
    `style`: "full" (date and time), "time" (hour:minute), "day" (weekday, time)."""
    try:
        moment = dt.datetime.fromtimestamp(float(ts), tz=dt.timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return Markup("")
    shown = {"time": "%H:%M", "day": "%a %H:%M"}.get(style, "%Y-%m-%d %H:%M")
    return Markup('<time class="lt" data-lt="{s}" datetime="{iso}">{text} UTC</time>').format(
        s=style, iso=moment.strftime("%Y-%m-%dT%H:%M:%SZ"), text=moment.strftime(shown))

