"""The pages: the original arcade's templates (arcade/web/templates), drawn with the context its render() gives them,
so the site looks and behaves as app.dogecoinarcade.com does now. A file in this package's templates/ folder of the
same name is drawn instead (the site's own changes, kept few).

The browser modules (/wallet.js, /coins.js, /signin.js, /messaging.js, /qrscan.js, /base.css, ...) are served from the
same files, fingerprinted the same way (one mark for all, since they import one another).
"""
from __future__ import annotations

import contextvars
import hashlib
import html
import json
import secrets
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from jinja2 import ChainableUndefined, ChoiceLoader, FileSystemLoader
from markupsafe import Markup

from arcade import modules as modulelib
from arcade.config import NETWORKS
from arcade.web import brand as brandlib

from . import legacy as L
from . import digidollar

#: The original's tabs (arcade/web/app.py NAV / ACCOUNT_NAV), less those this site does not serve yet.
ACCOUNT_NAV = [
    ("/me", "Your arcade", None, True),
    ("/apps", "Apps", "mainnet", True),
    ("/me/messages", "Messages", "testnet", True),
    ("/me/notifications", "Notifications", None, True),
    ("/feed", "Feed", "testnet", True),
    ("/me/wallet", "Wallet", None, True),
    ("/explore", "Explore", None, True),
]
PUBLIC_NAV = [
    ("/", "Overview", None, True),
    ("/apps", "Apps", "mainnet", True),
    ("/feed", "Feed", "testnet", True),
    ("/explore", "Explore", None, True),
]
ASSETS = {"/wallet.js": "wallet_js.js", "/coins.js": "coins.js", "/signin.js": "signin.js",
          "/messaging.js": "messaging_js.js", "/qrscan.js": "qrscan.js", "/base.css": "base_css.css",
          "/desk98.css": "desk98_css.css", "/deskubuntu.css": "deskubuntu_css.css",
          "/deskdogefeedx.css": "deskdogefeedx_css.css"}
COIN_FILES = {"main": "pep", "test": "pep-test"}
PAGE_CSP = "frame-ancestors 'self'; object-src 'none'; base-uri 'self'"
_NONCE: "contextvars.ContextVar[str]" = contextvars.ContextVar("site_csp_nonce", default="")


class _PageNonce:
    def __str__(self):
        return _NONCE.get()

    __html__ = __str__


class _TemplateChain:
    """What templates read of `state.messaging` (the tag chain): network, label, params."""

    def __init__(self, chain):
        self.network, self.params, self.label = chain.network, chain.params, chain.label
        self.net = chain.network
        self.is_mainnet = chain.is_mainnet


class _TemplateState:
    """The few `state.*` the templates read (base.html, my_wallet.html)."""

    def __init__(self, st, chains):
        self._st = st
        self.messaging = _TemplateChain(chains.account_chain())
        self.running_version = ""
        self.installed_version = ""
        self.is_stale = False
        self.generation = 0

    def setting(self, name, default=None):
        return self._st.setting(name, default)


def ord_wallet() -> dict:
    """The Wallet's ord chains (the original's ord_chains.wallet_table): which chains show Doginals and what they are
    called. Shown here from V1.0's ord indexes (doginals.py); sending, listing and swapping them are not on this site
    yet, so those are off (the page shows them view-only)."""
    from arcade import ord_chains as ordchainslib
    from .doginals import ORD_CHAINS
    return {n: {**v, "send": False, "list": False, "swap": False,
                "version": NETWORKS[n].pubkeyhash_version if n in NETWORKS else None}
            for n, v in ordchainslib.wallet_table().items() if n in ORD_CHAINS}


def register(app, st, chains, template_dir: Path):
    here = Path(__file__).parent / "templates"
    templates = Jinja2Templates(directory=str(template_dir))
    templates.env.loader = ChoiceLoader([FileSystemLoader(str(here)), FileSystemLoader(str(template_dir))])
    templates.env.undefined = ChainableUndefined
    # `|tojson` of a value this site does not give a template (an operator-only one): null, never a 500.
    from jinja2 import Undefined
    templates.env.policies["json.dumps_function"] = lambda obj, **kw: json.dumps(
        obj, default=lambda o: None if isinstance(o, Undefined) else str(o), **kw)
    tstate = _TemplateState(st, chains)
    v1 = st.v1
    names: dict[str, tuple[str, float]] = {}

    brandlib.use(str(st.setting("brand") or brandlib.own().key))

    # ---------------------------------------------------------------- globals and filters the templates use
    def coin_icon(net: Any) -> str:
        if isinstance(net, bool) or net is None:
            net = "main" if net else "test"
        name = COIN_FILES.get(str(net), str(net))
        return f"/coin-{name}.png" if (template_dir / f"coin-{name}.png").is_file() else ""

    def coin_of(where: Any) -> str:
        if isinstance(where, str) and where in NETWORKS:
            return NETWORKS[where].coin
        return "PEP" if where else "PEP TEST"

    def name_of(address: str) -> str:
        import time
        address = str(address or "").strip()
        if not address:
            return ""
        kept = names.get(address)
        if kept and time.time() - kept[1] < 60:
            return kept[0]
        try:
            tag = str((v1.page(f"address/{address}")[1] or {}).get("tag") or "")
        except Exception:                                   # noqa: BLE001
            tag = ""
        names[address] = (tag, time.time())
        return tag

    def who(address: Any, short: int = 12) -> Markup:
        address = str(address or "")
        if not address:
            return Markup("")
        tag = name_of(address)
        if tag:
            return Markup('<a href="/u/{t}" title="{a}">@{t}</a>').format(t=tag, a=address)
        shown = address if len(address) <= short + 1 else address[:short] + "…"
        return Markup('<span class="mono" title="{a}">{s}</span>').format(a=address, s=shown)

    def who_text(address: Any) -> str:
        address = str(address or "")
        tag = name_of(address)
        return f"@{tag}" if tag else (address[:12] + "…" if len(address) > 13 else address)

    def net_badge(where: Any = None) -> Markup:
        net = where if isinstance(where, str) and where in NETWORKS else ("main" if where is True else "test")
        params = NETWORKS[net]
        title = f"{params.label}: {'real ' + params.coin if params.real_coins else 'test coins, worth nothing'}"
        src = coin_icon(net)
        if src:
            return Markup('<span class="netbadge icon" title="{t}"><img class="neticon" src="{s}" alt="{c}"></span>'
                          ).format(t=title, s=src, c=params.coin)
        return Markup('<span class="netbadge" title="{t}">{c}</span>').format(t=title, c=params.coin)

    def chain_menu(account_side: bool = False) -> list[dict]:
        out = []
        for c in chains.account_chains():
            out.append({"network": c.network, "mainnet": bool(c.is_mainnet), "doge": c.network == "doge",
                        "ltc": c.network == "ltc", "ltctest": c.network == "ltctest", "letter": c.params.coin[:1],
                        "addon": c.network not in ("test", "main"), "coin": c.params.coin, "net": c.network,
                        "label": c.label, "ready": True, "synced": True, "icon": coin_icon(c.network)})
        return out

    g = templates.env.globals
    g.update({
        "brand": brandlib.CURRENT, "csp_nonce": _PageNonce(), "coin_icon": coin_icon,
        "coin_icons": lambda: {n: coin_icon(n) for n in NETWORKS if coin_icon(n)}, "coin_of": coin_of,
        "modules_off": lambda: modulelib.off(st.settings()), "name_of": name_of, "net_badge": net_badge,
        "supply_badge": lambda prop: Markup(""), "wall": lambda text, label="name": Markup.escape(text),
        "wall_text": lambda text, label="name": str(text or ""), "render_post": lambda text, drawable=None, frames=None:
            Markup(L.post_html(text, drawable, frames)), "screen_text": lambda text: "",
        "tip_chains": lambda: chain_menu(True), "chain_menu": chain_menu, "local_time": L._local_time,
        "fav_key": lambda href, net="": f"{str(href or '').split('?')[0]}@{net}" if net else str(href or ""),
        "game_name": lambda text: "", "leaked_url": lambda: "https://api.pwnedpasswords.com/range/",
        "node_trades": lambda: False, "ord_tabs": lambda *a, **k: [], "ord_tab_names": lambda: (),
        "ord_wallet": ord_wallet, "pair_faces": lambda *a, **k: {},
        "doginal_content": lambda i: f"/content/{i}", "doginal_frame": lambda i: f"/inscriptions/{i}/full",
    })
    templates.env.filters.update({
        "ago": L._ago, "coins": L._coins, "fromjson": L._fromjson, "who": who, "who_text": who_text,
        "hue": lambda text: int(hashlib.sha256(str(text or "").encode()).hexdigest()[:4], 16) % 360,
        "ask_price": lambda *a, **k: "", "describe_leg": lambda data: "",
    })

    # ---------------------------------------------------------------- assets, fingerprinted as the original
    kept: dict[str, Any] = {}

    def mark() -> str:
        stamps = tuple((template_dir / f).stat().st_mtime_ns for f in ASSETS.values())
        if kept.get("") and kept[""][0] == stamps:
            return kept[""][1]
        h = hashlib.sha256()
        for f in ASSETS.values():
            h.update((template_dir / f).read_bytes())
        kept.clear()
        kept[""] = (stamps, h.hexdigest()[:12])
        return kept[""][1]

    def versioned(text: str) -> str:
        m = None
        for path in ASSETS:
            if path in text:
                m = m or mark()
                for q in ('"', "'", "`"):
                    text = text.replace(f"{q}{path}{q}", f"{q}{path}?v={m}{q}")
        return text

    def asset(request: Request, path: str) -> Response:
        m = mark()
        if kept.get(path, (None,))[0] != m:
            kept[path] = (m, versioned((template_dir / ASSETS[path]).read_text()))
        keep = "public, max-age=31536000, immutable" if request.query_params.get("v") == m else "no-store"
        kind = "text/css" if path.endswith(".css") else "application/javascript"
        return Response(kept[path][1], media_type=kind, headers={"Cache-Control": keep})

    def asset_route(p: str):
        def handler(request: Request) -> Response:
            return asset(request, p)
        return handler

    for path in ASSETS:
        app.add_api_route(path, asset_route(path), methods=["GET"], include_in_schema=False)

    @app.get("/{name}.png", include_in_schema=False)
    def icons(name: str):
        f = template_dir / f"{name}.png"
        if not f.is_file() or "/" in name or ".." in name:
            return Response(status_code=404)
        return FileResponse(f, headers={"Cache-Control": "public, max-age=86400"})

    @app.get("/vendor/{name:path}", include_in_schema=False)
    def vendored(name: str):
        """The two libraries WebCrypto cannot replace, from the original's pinned copies (web/vendor/PROVENANCE.md)."""
        root = template_dir.parent / "vendor"
        if any(part in ("..", "") for part in Path(name).parts):
            return Response(status_code=404)
        target = (root / name).resolve()
        if not str(target).startswith(str(root.resolve())) or not target.is_file() or target.suffix != ".js":
            return Response(status_code=404)
        return Response(target.read_text(), media_type="application/javascript",
                        headers={"Cache-Control": "public, max-age=31536000, immutable"})

    @app.get("/content/{piece}/fit", include_in_schema=False)
    def content_fit(piece: str):
        """A card's frame (as the original): a picture drawn whole, anything else sent on to /content/<id>."""
        import urllib.parse
        if not all(c in "0123456789abcdefi" for c in piece.lower()) or len(piece) > 80:
            return Response(status_code=404)
        try:
            r = v1.raw("HEAD", f"/content/{piece}", headers={"referer": ""})
            kind, code = r.headers.get("content-type", ""), r.status_code
        except Exception:                                   # noqa: BLE001
            kind, code = "", 503
        if code != 200:
            note = "Not on this arcade" if code == 404 else "Not read yet. Open it to try again."
            page = ('<!doctype html><meta charset="utf-8"><style>html,body{margin:0;height:100%;background:transparent}'
                    'body{display:flex;align-items:center;justify-content:center;text-align:center;padding:8px;'
                    'box-sizing:border-box;font:12px/1.4 system-ui,sans-serif;color:#8a8a8a}</style>'
                    f'<p>{html.escape(note)}</p>')
            return Response(page, media_type="text/html", headers={"Cache-Control": "no-store"})
        if not kind.startswith("image/"):
            return RedirectResponse(f"/content/{urllib.parse.quote(piece)}", status_code=307)
        page = ('<!doctype html><meta charset="utf-8"><style>html,body{margin:0;height:100%;background:transparent}'
                'img{display:block;width:100%;height:100%;object-fit:contain;image-rendering:pixelated}</style>'
                f'<img src="/content/{html.escape(urllib.parse.quote(piece))}" alt="">')
        return Response(page, media_type="text/html", headers={"Cache-Control": "public, max-age=86400",
                                                               "Content-Security-Policy": "sandbox allow-scripts"})

    @app.get("/offline", response_class=HTMLResponse, include_in_schema=False)
    def offline(request: Request):
        return templates.TemplateResponse(request, "offline.html", {"request": request})

    @app.get("/sw.js", include_in_schema=False)
    def service_worker():
        body = templates.get_template("sw.js").render(revision="site")
        return Response(body, media_type="text/javascript",
                        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})

    @app.get("/events", include_in_schema=False)
    def events():
        """What an open page polls to decide whether to redraw: a generation, and the tag chain's tip from V1.0."""
        try:
            tip = v1.page("blockheight")[1]
        except Exception:                                   # noqa: BLE001
            tip = None
        return {"generation": 0, "tips": {"test": tip}, "checked": None, "sending": None}

    @app.get("/apps/store-icon.png", include_in_schema=False)
    def store_icon():
        return FileResponse(template_dir / "appstore-icon.png", media_type="image/png",
                            headers={"Cache-Control": "public, max-age=86400"})

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        return FileResponse(template_dir / "icon-32.png", media_type="image/png")

    @app.get("/bip39-english.txt", include_in_schema=False)
    def bip39():
        import arcade
        return FileResponse(Path(arcade.__file__).parent / "bip39-english.txt", media_type="text/plain",
                            headers={"Cache-Control": "public, max-age=86400"})

    # ---------------------------------------------------------------- render, as the original's
    def signed_in(request):
        return app.state.signed_in(request)

    def render(request: Request, template: str, **context: Any) -> HTMLResponse:
        _NONCE.set(secrets.token_urlsafe(16))
        account = signed_in(request)
        name = ""
        if account is not None:
            name = str((st.vault().by_pubkey(account.pubkey) or {}).get("tag") or "")
        nav = ACCOUNT_NAV if account is not None else PUBLIC_NAV
        base = {
            "request": request, "nav": nav, "nav_modules": {}, "tabs_kept": [], "theme_css": "", "public": True,
            "terms_link": None, "operator_here": False, "path": request.url.path, "state": tstate, "csrf": "",
            "notice": None, "notice_kind": None, "msg_net": tstate.messaging.label,
            "ledger_net": NETWORKS["main"].label, "approvals_waiting": 0, "unread_messages": 0,
            "notif_count": 0, "mail_count": 0, "account_here": account is not None, "account_name": name,
            "preload_wallet": account is not None, "stranger_here": account is None, "unread_board": 0,
            "offers_waiting": 0, "look": "web", "desk_default": "web", "phone_default": "web",
            "modules_on": modulelib.enabled(st.settings()), "edition_lite": False,
            "look_titles": modulelib.LOOK_TITLES, "edition_ask": False,
        }
        base.update(context)
        response = templates.TemplateResponse(request, template, base)
        response.headers["Cache-Control"] = "no-store, must-revalidate"
        body = versioned(response.body.decode("utf-8"))
        response.body = body.encode("utf-8")
        response.headers["content-length"] = str(len(response.body))
        response.headers["Content-Security-Policy"] = PAGE_CSP
        return response

    app.state.render = render

    def to_join(request: Request):
        return RedirectResponse("/join?next=" + html.escape(request.url.path), status_code=303)

    # ---------------------------------------------------------------- step 1's pages
    @app.get("/", response_class=HTMLResponse)
    def home(request: Request):
        if signed_in(request) is not None:
            return RedirectResponse("/me", status_code=303)
        return RedirectResponse("/feed", status_code=303)

    @app.get("/join", response_class=HTMLResponse)
    def join_page(request: Request):
        register_ = st.accounts()
        return render(request, "signup.html", chain=chains.account_chain(), fruit="", seats=register_.seats,
                      free=register_.free(), secure_enough=L.secure_context(request), why_not=L.WHY_NOT_SECURE)

    @app.get("/join/keys", response_class=HTMLResponse)
    def join_keys(request: Request):
        register_ = st.accounts()
        return render(request, "join.html", seats=register_.seats, free=register_.free(),
                      idle_days=register_.idle_days, first_peer="", repo_url="",
                      secure_enough=L.secure_context(request), why_not=L.WHY_NOT_SECURE)

    @app.get("/restore", response_class=HTMLResponse)
    def restore_page(request: Request):
        return render(request, "restore.html", chain=chains.account_chain())

    @app.get("/me", response_class=HTMLResponse)
    def my_arcade(request: Request):
        account = signed_in(request)
        if account is None:
            return to_join(request)
        register_ = st.accounts()
        name = str((st.vault().by_pubkey(account.pubkey) or {}).get("tag") or "")
        return render(request, "me.html", chain=chains.account_chain(),
                      hidden=st.setting(f"wallets_hidden:{account.pubkey}", []) or [], me_tag=name, node={},
                      when=lambda ts: L._local_time(ts), messaging={}, ledger={}, seats_free=register_.free(),
                      seats_total=register_.seats)

    @app.get("/me/wallet", response_class=HTMLResponse)
    def my_wallet(request: Request):
        account = signed_in(request)
        if account is None:
            return to_join(request)
        return render(request, "my_wallet.html", chain=chains.account_chain(),
                      hidden=st.setting(f"wallets_hidden:{account.pubkey}", []) or [], dd_nets=digidollar.nets(chains))

    return render
