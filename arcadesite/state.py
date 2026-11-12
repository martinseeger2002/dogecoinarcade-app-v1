"""What the website keeps itself: its settings, and the account register (seats, sessions, sealed wallets).

Nothing here is chain state. Coins, tags, posts and inscriptions are read from Arcade V1.0 (v1.py) every time.
The files and their shapes are the original arcade's own (arcade/accounts.py, settings.json), so an existing
arcade's accounts.sqlite and settings.json move here unchanged and every account keeps working.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from .v1 import V1

SETTINGS_FILE = "settings.json"
ACCOUNTS_FILE = "accounts.sqlite"


class SiteState:
    def __init__(self, home: str | os.PathLike, v1: V1, public_hosts: list[str] | None = None,
                 local_hosts: list[str] | None = None):
        self.home = Path(os.path.expanduser(str(home)))
        self.home.mkdir(parents=True, exist_ok=True)
        self.v1 = v1
        self._lock = threading.RLock()
        self._settings_lock = threading.Lock()
        self._accounts = None
        hosts = public_hosts if public_hosts is not None else (self.setting("public_hosts") or [])
        self.public_hosts = {h.lower() for h in hosts}
        self.local_hosts = {h.lower() for h in (local_hosts or [])}
        #: The inscribed pages' own name (sandboxed content), never a place to sign in.
        self.pages_hostname = str(self.setting("pages_host") or "").lower()

    # ---------------------------------------------------------------- settings.json (the original's format)
    def settings(self) -> dict:
        try:
            return json.loads((self.home / SETTINGS_FILE).read_text())
        except (OSError, ValueError):
            return {}

    def setting(self, name: str, default: Any = None) -> Any:
        value = self.settings().get(name)
        return default if value is None else value

    def set_setting(self, name: str, value: Any) -> None:
        """Written whole to a temporary file and renamed into place, under a lock (as the original)."""
        with self._settings_lock:
            path = self.home / SETTINGS_FILE
            try:
                data = json.loads(path.read_text()) if path.exists() else {}
            except ValueError:
                raise RuntimeError("settings.json cannot be read: not written over")
            if value is None:
                data.pop(name, None)
            else:
                data[name] = value
            tmp = path.with_suffix(".tmp-%d" % os.getpid())
            tmp.write_text(json.dumps(data, indent=1, sort_keys=True))
            os.replace(tmp, path)

    # ---------------------------------------------------------------- the register (arcade/accounts.py)
    def accounts(self):
        with self._lock:
            if self._accounts is None:
                from arcade import accounts as accounts_lib
                seats = self.setting("seats")
                self._accounts = accounts_lib.Accounts(self.home / ACCOUNTS_FILE,
                                                       seats=accounts_lib.SEATS if seats is None else int(seats))
            return self._accounts

    def vault(self):
        from arcade.accounts import Vault
        return Vault(self.accounts())

    def mailbox(self):
        from arcade.accounts import Mailbox
        return Mailbox(self.accounts())

    def imported_wallets(self):
        from arcade.accounts import ImportedWallets
        return ImportedWallets(self.accounts())

    def account_for(self, token: str):
        """The account a session token belongs to, or None."""
        if not token:
            return None
        try:
            return self.accounts().session(token)
        except Exception:                                   # noqa: BLE001 -- an expired or unknown token
            return None

    def now(self) -> int:
        return int(time.time())
