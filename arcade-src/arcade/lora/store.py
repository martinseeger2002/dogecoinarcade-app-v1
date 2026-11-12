"""What a gateway must not forget across a restart.

Per device: who it is, its arcade session, the chains it sees, whose mail it
takes, how far through the arcade's mail it has read, and the mail it has not
yet confirmed. A T-Deck that is switched off for a week finds its outbox here.

The file holds session cookies, so it is created readable by its owner only.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS device(
  node INTEGER PRIMARY KEY, pubkey BLOB NOT NULL, tag TEXT NOT NULL DEFAULT '',
  cookie TEXT NOT NULL DEFAULT '', chains TEXT NOT NULL DEFAULT '[]',
  watch BLOB NOT NULL DEFAULT x'', mail_cursor INTEGER, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS outbox(
  node INTEGER NOT NULL, txid TEXT NOT NULL, type INTEGER NOT NULL, body BLOB NOT NULL,
  queued REAL NOT NULL, tries INTEGER NOT NULL DEFAULT 0, last_try REAL,
  PRIMARY KEY(node, txid));
CREATE TABLE IF NOT EXISTS receipt(
  node INTEGER NOT NULL, txid TEXT NOT NULL, sig BLOB NOT NULL, at REAL NOT NULL,
  PRIMARY KEY(node, txid));
"""


class Store:
    def __init__(self, path: str):
        self.path = path
        if path != ":memory:" and not os.path.exists(path):
            os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.executescript(SCHEMA)
        self.lock = threading.Lock()

    def _x(self, sql, args=()):
        with self.lock, self.db:
            return self.db.execute(sql, args).fetchall()

    # -- devices --------------------------------------------------------------
    def save_device(self, node, pubkey, tag, cookie, chains, watch, mail_cursor):
        self._x("INSERT INTO device(node,pubkey,tag,cookie,chains,watch,mail_cursor,updated) VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(node) DO UPDATE SET pubkey=excluded.pubkey, tag=excluded.tag, cookie=excluded.cookie, "
                "chains=excluded.chains, watch=excluded.watch, mail_cursor=excluded.mail_cursor, updated=excluded.updated",
                (node, pubkey, tag, cookie, json.dumps(chains), b"".join(sorted(watch)), mail_cursor, time.time()))

    def devices(self) -> list[dict]:
        rows = self._x("SELECT node,pubkey,tag,cookie,chains,watch,mail_cursor FROM device")
        return [{"node": r[0], "pubkey": bytes(r[1]), "tag": r[2], "cookie": r[3], "chains": json.loads(r[4]),
                 "watch": {bytes(r[5][i:i + 20]) for i in range(0, len(r[5]), 20)}, "mail_cursor": r[6]}
                for r in rows]

    def forget_device(self, node):
        self._x("DELETE FROM device WHERE node=?", (node,))

    # -- outbox ---------------------------------------------------------------
    def queue(self, node, txid, kind, body) -> bool:
        """False when that mail is already queued or was delivered."""
        if self.known(node, txid):
            return False
        self._x("INSERT INTO outbox(node,txid,type,body,queued) VALUES(?,?,?,?,?)",
                (node, txid, kind, body, time.time()))
        return True

    def pending(self, node) -> list[dict]:
        rows = self._x("SELECT txid,type,body,tries,last_try FROM outbox WHERE node=? ORDER BY queued", (node,))
        return [{"txid": r[0], "type": r[1], "body": bytes(r[2]), "tries": r[3], "last_try": r[4]} for r in rows]

    def tried(self, node, txid):
        self._x("UPDATE outbox SET tries=tries+1, last_try=? WHERE node=? AND txid=?", (time.time(), node, txid))

    def delivered(self, node, txid, sig) -> bool:
        if not self._x("SELECT 1 FROM outbox WHERE node=? AND txid=?", (node, txid)):
            return bool(self._x("SELECT 1 FROM receipt WHERE node=? AND txid=?", (node, txid)))
        self._x("INSERT OR REPLACE INTO receipt(node,txid,sig,at) VALUES(?,?,?,?)", (node, txid, sig, time.time()))
        self._x("DELETE FROM outbox WHERE node=? AND txid=?", (node, txid))
        return True

    def receipt(self, node, txid) -> dict | None:
        r = self._x("SELECT sig,at FROM receipt WHERE node=? AND txid=?", (node, txid))
        return {"sig": bytes(r[0][0]), "at": r[0][1]} if r else None

    def known(self, node, txid) -> bool:
        return bool(self._x("SELECT 1 FROM outbox WHERE node=? AND txid=? UNION "
                            "SELECT 1 FROM receipt WHERE node=? AND txid=?", (node, txid, node, txid)))
