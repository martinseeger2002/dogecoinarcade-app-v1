"""The operator's page, /admin, and its Marketplace tab (docs/MARKETPLACE-PLAN.md, 4): what this website lets its
visitors trade across chains, and the operator's fee.

    GET  /admin                     the operator's page (the signed-in account must be the `operator` setting)
    GET  /admin/api/marketplace     the Marketplace settings
    POST /admin/api/marketplace     save them

Settings (settings.json, the original's format), all off by default:
    market_taker        visitors may take cross-chain orders (their browser signs every step)
    market_maker        visitors may put cross-chain orders on the book that this site finishes for them
    market_maker_cap    {chain: whole coins} the most one such order may hold
    trade_cut           the operator's fee, percent (the original's name), on this site's side of a trade
    trade_cut_to        {chain: address} where it is paid; empty = the operator account's own address there

The fee is this site's side only (Martin 2026-11-11): a maker's fee goes to the maker's website's operator, a taker's
fee to the taker's website's operator.
"""
from __future__ import annotations

from typing import Any

from fastapi import Body, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from arcade.script import b58check_decode

#: The highest fee an operator may set, percent (the original's trade cut choices end at 5).
CUT_MAX = 5.0


def register(app, st, chains):
    def operator_only(request: Request):
        account = app.state.signed_in(request)
        operator = str(st.setting("operator") or "").lower()
        if account is None or not operator or account.pubkey.lower() != operator:
            raise HTTPException(403, "the operator's page")
        return account

    def payout(network: str) -> str:
        """Where this site's fee is paid on a chain: the setting, else the operator account's own address there."""
        said = (st.setting("trade_cut_to", {}) or {}).get(network) or ""
        if said:
            return said
        operator = str(st.setting("operator") or "").lower()
        one = chains.by_network(network)
        return chains.account_address(operator, one) if operator and one is not None else ""

    def current() -> dict:
        nets = [c.network for c in chains.account_chains()]
        caps = st.setting("market_maker_cap", {}) or {}
        return {"market_taker": bool(st.setting("market_taker", False)),
                "market_maker": bool(st.setting("market_maker", False)),
                "trade_cut": float(st.setting("trade_cut", 0) or 0),
                "chains": [{"network": n, "label": chains.by_network(n).label, "coin": chains.by_network(n).params.coin,
                            "cap": caps.get(n, ""), "pay_to": payout(n),
                            "pay_to_set": bool((st.setting("trade_cut_to", {}) or {}).get(n))} for n in nets]}

    app.state.market_settings = current
    app.state.market_payout = payout

    @app.get("/admin", response_class=HTMLResponse)
    def admin_page(request: Request, tab: str = "marketplace"):
        operator_only(request)
        return app.state.render(request, "admin_site.html", tab=tab, market=current())

    @app.get("/admin/api/marketplace")
    def admin_market_read(request: Request):
        operator_only(request)
        return JSONResponse(current(), headers={"Cache-Control": "no-store"})

    @app.post("/admin/api/marketplace")
    def admin_market_save(request: Request, payload: Any = Body(None)):
        operator_only(request)
        said = payload if isinstance(payload, dict) else {}
        try:
            cut = float(said.get("trade_cut", 0) or 0)
        except (TypeError, ValueError):
            return JSONResponse({"detail": "the fee is a percent, a number"}, status_code=400)
        if not 0 <= cut <= CUT_MAX:
            return JSONResponse({"detail": f"the fee is between 0 and {CUT_MAX:g} percent"}, status_code=400)
        nets = {c.network: c for c in chains.account_chains()}
        caps, pay_to = {}, {}
        for n, row in (said.get("chains") or {}).items() if isinstance(said.get("chains"), dict) else []:
            if n not in nets or not isinstance(row, dict):
                continue
            cap = str(row.get("cap") or "").strip()
            if cap:
                try:
                    if float(cap) <= 0:
                        raise ValueError
                except ValueError:
                    return JSONResponse({"detail": f"the cap on {nets[n].label} is a number of coins above zero"},
                                        status_code=400)
                caps[n] = cap
            to = str(row.get("pay_to") or "").strip()
            if to:
                try:
                    version, h160 = b58check_decode(to)
                except Exception:                           # noqa: BLE001 -- any bad text is the same answer
                    version, h160 = None, b""
                p = nets[n].params
                if len(h160) != 20 or version not in (p.pubkeyhash_version, p.scripthash_version):
                    return JSONResponse({"detail": f"{to} is not a {nets[n].label} address"}, status_code=400)
                pay_to[n] = to
        st.set_setting("market_taker", bool(said.get("market_taker")))
        st.set_setting("market_maker", bool(said.get("market_maker")))
        st.set_setting("market_maker_cap", caps)
        st.set_setting("trade_cut", cut)
        st.set_setting("trade_cut_to", pay_to)
        return JSONResponse(current())
