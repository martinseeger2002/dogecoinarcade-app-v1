# arcade-site

app.dogecoinarcade.com as its own website server in front of **Arcade V1.0**: every coin, tag, post and inscription
is read from the V1.0 daemon on the same computer (`/rpc` commands, Core RPC per chain, `/r/` reads). The site keeps
only what is its own: seats and sessions, sealed wallets (vaults), settings — the original arcade's files and shapes,
so accounts move here unchanged and keep signing in across arcades.

Reuses the original arcade's pure modules (`ARCADE_SRC`: accounts, tags, funding, fees, txbuild, wallet_imports, faucet).

    python3 -m venv .venv && .venv/bin/pip install fastapi==0.115.6 uvicorn==0.34.0 jinja2 pynacl requests httpx cryptography python-multipart quickjs==1.19.4
    ARCADE_SRC=/path/to/dogecoin-arcade .venv/bin/python -m arcadesite --home ~/.arcade-site --port 8460

`<home>/site.json`: `{"v1": "http://127.0.0.1:8420", "v1_home": "~/.local/share/arcade", "public_hosts": ["app.dogecoinarcade.com"]}`

Test (live, Pepecoin testnet, test coins): `ARCADE_SRC=... .venv/bin/python tests/live_step1.py http://127.0.0.1:8430 ~/.local/share/arcade-v1`

`ARCADE_SRC` defaults to `arcade-src/` in this repo: an unchanged copy of the `arcade/` package of the public
[dogecoin-arcade](https://github.com/martinseeger2002/dogecoin-arcade) repo that this site was built against.

Tests read a dev home from `$ARCADE_SITE_DEV` (default `~/.arcade-site-dev`) whose `site.json` names the V1.0 to use.
