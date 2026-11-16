"""The website's only way to the chains: the Arcade V1.0 daemon on this same computer.

V1.0 answers four ways, all on its http port (127.0.0.1:8420 by default) and only to this computer:

    cmd(name, **params)       JSON-RPC 2.0 at /rpc with the cookie: every command, Local ones too
    core(chain, method, ...)  Core-compatible JSON-RPC at POST /chain/<key> (getrawtransaction, gettxout, ...)
    page(path)                the original's page API, GET /r/<...> (tag, token, balances, inscriptions, ...)
    raw(path)                 anything else on the port (/content/<id>, /app/<chain>/<id>)

The cookie (<home>/.cookie) is new each time arcaded starts, so it is read again when a call is refused.
No request carries X-Forwarded-For / Forwarded / X-Real-IP: V1.0 refuses those (a proxied stranger).
"""
from __future__ import annotations

import itertools
import json
import os
import threading
from pathlib import Path

import requests

#: The original's network names -> V1.0's chain keys.
CHAIN_KEY = {"main": "pep", "test": "peptest", "regtest": "pepregtest"}


def chain_key(network: str) -> str:
    return CHAIN_KEY.get(network, network)


class V1Error(RuntimeError):
    """V1.0 answered with an error (its own words), or could not be reached."""

    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


class V1:
    def __init__(self, base: str = "http://127.0.0.1:8420", home: str | os.PathLike = "~/.local/share/arcade",
                 timeout: float = 60.0):
        self.base = base.rstrip("/")
        self.home = Path(os.path.expanduser(str(home)))
        self.timeout = timeout
        self._ids = itertools.count(1)
        self._local = threading.local()
        self._cookie: str | None = None

    # ---------------------------------------------------------------- plumbing
    def _s(self) -> requests.Session:
        s = getattr(self._local, "s", None)
        if s is None:
            s = self._local.s = requests.Session()
            s.headers["User-Agent"] = "arcade-site"
        return s

    def _auth(self, fresh: bool = False):
        if fresh or self._cookie is None:
            self._cookie = (self.home / ".cookie").read_text().strip()
        user, _, password = self._cookie.partition(":")
        return (user, password)

    def _post(self, path: str, body, timeout: float | None = None):
        for attempt in (0, 1):
            try:
                r = self._s().post(self.base + path, json=body, auth=self._auth(fresh=attempt == 1),
                                   timeout=timeout or self.timeout)
            except requests.RequestException as e:
                raise V1Error(f"the arcade did not answer: {e}") from None
            if r.status_code == 401 and attempt == 0:
                continue                                    # arcaded restarted: a new cookie
            return r
        return r

    # ---------------------------------------------------------------- the four ways
    def cmd(self, _command: str, _timeout: float | None = None, **params):
        """A V1.0 command (docs/API.md), as JSON-RPC with the cookie. Its result, or V1Error."""
        r = self._post("/rpc", {"jsonrpc": "2.0", "id": next(self._ids), "method": _command, "params": params},
                       _timeout)
        try:
            out = r.json()
        except ValueError:
            raise V1Error(f"{_command}: HTTP {r.status_code}") from None
        if out.get("error"):
            raise V1Error(str(out["error"].get("message") or out["error"]), out["error"].get("code"))
        return out.get("result")

    def core(self, chain: str, method: str, *params, timeout: float | None = None):
        """A Core-style call on one chain V1.0 keeps (its key, or an original network name)."""
        key = chain_key(chain)
        r = self._post(f"/chain/{key}", {"jsonrpc": "1.0", "id": next(self._ids), "method": method,
                                         "params": list(params)}, timeout)
        try:
            out = r.json()
        except ValueError:
            raise V1Error(f"{method}: HTTP {r.status_code}") from None
        if out.get("error"):
            e = out["error"]
            raise V1Error(str(e.get("message") or e), e.get("code"))
        return out.get("result")

    def page(self, path: str, params: dict | None = None, timeout: float | None = None):
        """GET /r/<path> -> (status, JSON body). Not raised on 404: callers read the original's answers."""
        # A program on this computer reads the page API with the cookie (V1.0 since claude/page-api-cookie; app
        # pages carry a page pass instead). A refusal with a stale cookie is asked once more with a fresh one.
        for attempt in (0, 1):
            try:
                r = self._s().get(f"{self.base}/r/{path.lstrip('/')}", params=params, timeout=timeout or self.timeout,
                                  auth=self._auth(fresh=attempt == 1))
            except requests.RequestException as e:
                raise V1Error(f"the arcade did not answer: {e}") from None
            except OSError:
                r = self._s().get(f"{self.base}/r/{path.lstrip('/')}", params=params, timeout=timeout or self.timeout)
            if r.status_code not in (401, 403) or attempt:
                break
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, None

    def raw(self, method: str, path: str, headers: dict | None = None, data: bytes | None = None,
            params: dict | None = None, stream: bool = False, timeout: float | None = None) -> requests.Response:
        """Any other request on V1.0's port (content, app frames, the room door), passed through as it is."""
        h = {k: v for k, v in (headers or {}).items()
             if k.lower() not in ("x-forwarded-for", "forwarded", "x-real-ip", "host", "cookie", "authorization")}
        try:
            for attempt in (0, 1):
                auth = self._auth(fresh=attempt == 1) if path.startswith("/r/") else None
                r = self._s().request(method, self.base + path, headers=h, data=data, params=params, stream=stream,
                                      timeout=timeout or self.timeout, auth=auth)
                # arcaded makes a new cookie when it starts: a refusal is asked once more with the file's
                if auth is None or r.status_code not in (401, 403) or attempt:
                    return r
            return r
        except requests.RequestException as e:
            raise V1Error(f"the arcade did not answer: {e}") from None

    # ---------------------------------------------------------------- small helpers the site uses often
    def tag_owner(self, tag: str) -> str:
        """The address the chain says holds @tag ("" when none, or when the arcade can't say yet). From V1.0's tags
        index (explorer.tag: current to the newest block); /r/tag (the ledger's copy, a few blocks behind) only when
        that cannot be asked."""
        try:
            got = self.cmd("explorer.tag", tag=tag) or {}
            return str(got.get("home_address") or "")
        except V1Error as e:
            if "not claimed" in str(e):
                return ""
        try:
            got = self.page(f"tag/{tag}")[1]
        except V1Error:
            return ""
        return str((got or {}).get("address") or "")

    def tag_pending(self, tag: str) -> str:
        """The sender of a claim for @tag waiting in the home chain's mempool ("" when none, or when it can't be
        asked). The chain counts the first claim mined; a second one sent meanwhile is paid for and refused."""
        try:
            got = self.cmd("explorer.mempool") or {}
        except V1Error:
            return ""
        for row in got.get("pending_tags") or []:
            if isinstance(row, (list, tuple)) and len(row) >= 2 and row[0] == tag:
                return str(row[1])
        return ""

    def coins(self, addresses: list[str], chain: str = "test") -> dict:
        return self.cmd("tx.coins", addresses=list(addresses), chain=chain_key(chain))

    def hosts(self) -> list[str]:
        """Web hosts of the arcades V1.0 is linked to (wss://host/... peers): where sealed wallets may be."""
        out = []
        try:
            for p in self.cmd("peers") or []:
                a = str(p.get("addr") or "")
                if a.startswith("wss://"):
                    out.append(a[len("wss://"):].split("/", 1)[0].split(":", 1)[0].lower())
        except V1Error:
            pass
        return out


def dumps(v) -> str:
    return json.dumps(v, separators=(",", ":"))
