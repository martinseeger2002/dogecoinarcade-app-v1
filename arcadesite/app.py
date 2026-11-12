"""The website: app.dogecoinarcade.com as its own server in front of Arcade V1.0 (2026-11-09, the operator: "the website
itself ... would be its own server that only uses the API or RPC from the arcade V1").

Config: <home>/site.json
    {"v1": "http://127.0.0.1:8420", "v1_home": "~/.local/share/arcade",
     "public_hosts": ["app.dogecoinarcade.com"], "local_hosts": []}
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from . import accountpages, actions, apps, auth, catalog, creator, digidollar, doginals, exchange, pools, explore, feed, imported, llm, marketplace, messages, ordsend, pages, realtime, rings, sidecar, swaptake, tokens, wallet
from .chains import Chains
from .state import SiteState
from .v1 import V1, V1Error


def load_config(home: str | os.PathLike) -> dict:
    path = Path(os.path.expanduser(str(home))) / "site.json"
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def create_app(home: str | os.PathLike) -> FastAPI:
    cfg = load_config(home)
    v1 = V1(cfg.get("v1", "http://127.0.0.1:8420"), cfg.get("v1_home", "~/.local/share/arcade"))
    st = SiteState(home, v1, public_hosts=cfg.get("public_hosts"), local_hosts=cfg.get("local_hosts"))
    chains = Chains(st, v1)
    app = FastAPI(title="Arcade website", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.site = st
    app.state.chains = chains

    @app.exception_handler(V1Error)
    def v1_down(request, exc: V1Error):
        return JSONResponse({"detail": f"the arcade behind this site did not answer: {exc}"}, status_code=503)

    @app.get("/healthz")
    def health():
        try:
            s = v1.cmd("status")
            return {"ok": True, "chain": s.get("chain"), "node": str(s.get("node") or "")[:16]}
        except V1Error as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=503)

    auth.register(app, st, chains)
    wallet.register(app, st, chains)
    digidollar.register(app, st, chains)
    actions.register(app, st, chains)
    tokens.register(app, st, chains)
    sidecar.register(app, st, chains)
    imported.register(app, st, chains)
    doginals.register(app, st, chains)
    import arcade
    template_dir = Path(cfg.get("templates") or Path(arcade.__file__).parent / "web" / "templates")
    pages.register(app, st, chains, template_dir)
    llm.register(app, st, chains)
    catalog.register(app, st, chains)
    pools.register(app, st, chains)
    apps.register(app, st, chains, template_dir)
    apps.register_pages(app, st, chains)
    realtime.register(app, st, chains)
    rings.register(app, st, chains)
    accountpages.register(app, st, chains)
    creator.register(app, st, chains)
    exchange.register(app, st, chains)
    marketplace.register(app, st, chains)
    swaptake.register(app, st, chains)
    ordsend.register(app, st, chains)
    feed.register(app, st, chains)
    messages.register(app, st, chains)
    explore.register(app, st, chains)
    return app
