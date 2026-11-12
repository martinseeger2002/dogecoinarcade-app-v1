"""Viewer tickets, kept on disk (2026-10-26, the ASHVALE/a tester session: a deploy restarted the worker and every open
inscribed page lost its ticket -- the game's /r/send answered 403 {"wallet": "off"} until the player reloaded; and the
one-hour life cut long game sessions off the same way).

A ticket is still an opaque random string naming one address on one chain for one page (app.py `_viewer_ticket`): no
address rides in the URL. They live in a small SQLite file under the arcade's home, so the worker a reload starts reads
the same tickets as the one it replaces, with memory in front for the page API's many asks. A ticket in use slides: once
half its life has passed it is renewed for another whole one, so a page left open and played all evening keeps working.
"""
from __future__ import annotations

import secrets
import sqlite3
import threading
import time
from pathlib import Path

LIFE = 12 * 3600
KEEP_IN_MEMORY = 20000


class Tickets:
    def __init__(self, path: Path | str | None, clock=time.time, life: float = LIFE):
        self.path = str(path) if path else ":memory:"
        self.clock = clock
        self.life = life
        self.lock = threading.Lock()
        self.cache: dict[str, tuple[str, str, str, float]] = {}
        self._db = sqlite3.connect(self.path, timeout=10, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("CREATE TABLE IF NOT EXISTS ticket (t TEXT PRIMARY KEY, address TEXT NOT NULL, "
                         "network TEXT NOT NULL, txid TEXT NOT NULL, until REAL NOT NULL)")
        self._last_sweep = 0.0

    def mint(self, address: str, network: str, txid: str) -> str:
        now = self.clock()
        t = secrets.token_urlsafe(18)
        row = (address, network, txid, now + self.life)
        with self.lock:
            self._sweep(now)
            self._db.execute("INSERT OR REPLACE INTO ticket VALUES (?,?,?,?,?)", (t,) + row)
            if len(self.cache) > KEEP_IN_MEMORY:
                self.cache.clear()
            self.cache[t] = row
        return t

    def get(self, t: str) -> tuple[str, str, str, float] | None:
        """(address, network, txid, until) of a live ticket, renewed when half its life is gone; None otherwise."""
        if not t:
            return None
        now = self.clock()
        with self.lock:
            row = self.cache.get(t)
            if row is None:
                found = self._db.execute("SELECT address, network, txid, until FROM ticket WHERE t=?", (t,)).fetchone()
                if found is None:
                    return None
                row = tuple(found)
                self.cache[t] = row
            if row[3] < now:
                self.cache.pop(t, None)
                return None
            if row[3] - now < self.life / 2:
                row = row[:3] + (now + self.life,)
                self.cache[t] = row
                self._db.execute("UPDATE ticket SET until=? WHERE t=?", (row[3], t))
            return row

    def _sweep(self, now: float) -> None:
        if now - self._last_sweep < 600:
            return
        self._last_sweep = now
        self._db.execute("DELETE FROM ticket WHERE until < ?", (now,))
        for k in [k for k, v in self.cache.items() if v[3] < now]:
            self.cache.pop(k, None)
