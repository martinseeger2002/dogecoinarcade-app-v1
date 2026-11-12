"""Credits per model (2026-10-27: "Credits should not be shared across models each model has their own credit,
and those credits are stored by the operator of that model"; "when they buy credit they should buy it in amount of
credits with the cost shown in the coin that they're buying").

A seller's operator sets two things (Admin -> LLM):

* `credit_prices` -- what ONE credit costs in each coin or token it accepts: [{leg, label, per}], `per` a decimal
  amount of that coin or token. The same for all its models; only these coins and tokens are offered to buyers.
* `rates` -- what each model charges in credits: a chat model {"in": c, "out": c} credits per 1,000 tokens read and
  written (charged by length, the operator's choice), a picture model {"px": c} credits per 250 px of the longest side.

A buyer holds credits for one model at one seller, kept in the seller's llm.sqlite (`mcredit`), in thousandths of a
credit (MC) so a short reply can cost 0.4 credits and nothing rounds to free. A top-up names the model; what it brings
is the payment's worth divided by that coin's credit price.

Before credits, an operator set per-token prices per coin (`prices`, {leg, in, out} per token) and pictures a price per
picture-credit (imagegen `prices`, {leg, per}); a buyer's credit was coins per seller (`credit`). Read as credits
without changing what anything costs: one credit is what 1,000 written tokens cost (the first price row's `out`), a
chat model's rates keep that row's in/out ratio, a picture credit keeps its price; and each buyer's old coin credit
becomes credits for the seller's first model (the operator's choice), once, the first time the seller reads that buyer.
"""
from __future__ import annotations

import math
import sqlite3
import threading
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

#: Thousandths of a credit: the unit balances and charges are kept in.
MC = 1000
#: A typical reply, for the "about N credits a reply" a card shows: tokens read and written.
TYPICAL_IN, TYPICAL_OUT = 300, 400
#: Credits per 1,000 tokens / per 250 px at most (a typo of a million is refused).
RATE_MOST = Decimal(100000)

SCHEMA = """
CREATE TABLE IF NOT EXISTS mcredit (buyer TEXT NOT NULL, model TEXT NOT NULL, mc INTEGER NOT NULL DEFAULT 0,
                                    PRIMARY KEY (buyer, model));
CREATE TABLE IF NOT EXISTS mtopup (txid TEXT NOT NULL, leg TEXT NOT NULL, buyer TEXT NOT NULL, model TEXT NOT NULL,
                                   units INTEGER NOT NULL, mc INTEGER NOT NULL, at REAL NOT NULL,
                                   PRIMARY KEY (txid, leg));
CREATE TABLE IF NOT EXISTS mmigrated (buyer TEXT PRIMARY KEY, at REAL NOT NULL);
"""


def _dec(text: Any) -> Decimal:
    from .llm import price
    return price(text)


def rate_ok(value: Any) -> str:
    d = _dec(value)
    if d > RATE_MOST:
        from .llm import LlmError
        raise LlmError(f"at most {RATE_MOST} credits")
    return format(d, "f")


# ------------------------------------------------------------------------------------------------ what credits cost

def credit_prices(cfg: dict, icfg: dict | None = None) -> list[dict]:
    """[{leg, label, per}]: the operator's own, or read from the per-token / per-picture prices before credits."""
    if cfg.get("credit_prices"):
        return [dict(p) for p in cfg["credit_prices"]]
    out, seen = [], set()
    for p in cfg.get("prices") or []:
        per = _dec(p.get("out", 0)) * 1000 or _dec(p.get("in", 0)) * 1000
        if per > 0 and p.get("leg") not in seen:
            seen.add(p["leg"])
            out.append({"leg": p["leg"], "label": p.get("label") or p["leg"], "per": format(per.normalize(), "f")})
    for p in (icfg or {}).get("prices") or []:
        per = _dec(p.get("per", 0))
        if per > 0 and p.get("leg") not in seen:
            seen.add(p["leg"])
            out.append({"leg": p["leg"], "label": p.get("label") or p["leg"], "per": format(per.normalize(), "f")})
    return out


def credit_prices_for(cfg: dict, icfg: dict | None, picture: bool) -> list[dict]:
    """What a credit of one model costs: the operator's credit prices for every model; before them, a chat model's
    read from the per-token prices and a picture model's from the picture prices -- so nothing costs more than it did
    (a picture credit at 0.5 PEP stays 0.5, whatever 1,000 tokens cost)."""
    if cfg.get("credit_prices"):
        return [dict(p) for p in cfg["credit_prices"]]
    if picture:
        return [{"leg": p["leg"], "label": p.get("label") or p["leg"], "per": format(_dec(p.get("per", 0)).normalize(), "f")}
                for p in (icfg or {}).get("prices") or [] if _dec(p.get("per", 0)) > 0]
    return credit_prices({k: v for k, v in cfg.items() if k != "credit_prices"}, None)


def text_rate(cfg: dict, model: str) -> dict:
    """{"in", "out"} credits per 1,000 tokens for chat model `model` (0 and 0: free)."""
    got = (cfg.get("rates") or {}).get(model)
    if isinstance(got, dict) and ("in" in got or "out" in got):
        return {"in": format(_dec(got.get("in", 0)), "f"), "out": format(_dec(got.get("out", 0)), "f")}
    rows = [p for p in cfg.get("prices") or [] if _dec(p.get("out", 0)) > 0 or _dec(p.get("in", 0)) > 0]
    if not rows:
        return {"in": "0", "out": "0"}
    p_in, p_out = _dec(rows[0].get("in", 0)), _dec(rows[0].get("out", 0))
    if p_out > 0:
        return {"in": format((p_in / p_out).normalize(), "f"), "out": "1"}
    return {"in": "1", "out": "0"}


def picture_rate(icfg: dict, model: str) -> Decimal:
    """Credits per 250 px of the longest side for picture model `model`: its operator's rate, else 1 (a picture
    credit before credits was exactly that). 0: that model is free."""
    got = (icfg.get("rates") or {}).get(model)
    if isinstance(got, dict) and "px" in got:
        return _dec(got["px"])
    return Decimal(1)


def text_cost_mc(rate: dict, tokens_in: int, tokens_out: int) -> int:
    whole = _dec(rate.get("in", 0)) * int(tokens_in) + _dec(rate.get("out", 0)) * int(tokens_out)
    return int(math.ceil(whole * MC / 1000)) if whole > 0 else 0


def affordable_out(rate: dict, mc: int, tokens_in: int) -> int:
    """Tokens a balance of `mc` still writes once the prompt is paid (-1: unlimited, a free rate)."""
    r_in, r_out = _dec(rate.get("in", 0)), _dec(rate.get("out", 0))
    left = Decimal(mc) * 1000 / MC - r_in * int(tokens_in)
    if left < 0:
        return 0
    if r_out == 0:
        return -1
    return int(left / r_out)


def picture_cost_mc(per_px: Decimal, longest: int) -> int:
    return int(math.ceil(per_px * max(1, math.ceil(int(longest) / 250)) * MC)) if per_px > 0 else 0


def credits_text(mc: int) -> str:
    whole, frac = divmod(int(mc), MC)
    if not frac:
        return f"{whole:,}"
    return f"{whole:,}.{frac:03d}".rstrip("0")


def mc_for_payment(units: int, scale: int, per: Any) -> int:
    """Thousandths of a credit a payment of `units` (at `scale` units a coin) buys at `per` coins a credit."""
    p = _dec(per)
    if p <= 0:
        return 0
    return int((Decimal(int(units)) / Decimal(scale) / p * MC).to_integral_value(rounding="ROUND_FLOOR"))


# ------------------------------------------------------------------------------------------------ the seller's books

class CreditBooks:
    """The `mcredit` side of a seller's llm.sqlite: each buyer's credits per model."""

    def __init__(self, home: Path | str):
        self.path = Path(home) / "llm.sqlite"
        self._lock = threading.Lock()
        with self._db() as db:
            db.executescript(SCHEMA)

    def _db(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        return _Closing(db)

    def balances(self, buyer: str) -> dict[str, int]:
        with self._db() as db:
            return {r["model"]: int(r["mc"]) for r in db.execute("SELECT model, mc FROM mcredit WHERE buyer=?", (buyer,))}

    def balance(self, buyer: str, model: str) -> int:
        with self._db() as db:
            row = db.execute("SELECT mc FROM mcredit WHERE buyer=? AND model=?", (buyer, model)).fetchone()
        return int(row["mc"]) if row else 0

    def credited(self, txid: str, leg: str) -> dict | None:
        with self._db() as db:
            row = db.execute("SELECT * FROM mtopup WHERE txid=? AND leg=?", (txid, leg)).fetchone()
            if row is None:                              # a payment counted before credits, as coin credit
                old = db.execute("SELECT * FROM topup WHERE txid=? AND leg=?", (txid, leg)).fetchone() \
                    if db.execute("SELECT name FROM sqlite_master WHERE name='topup'").fetchone() else None
                return {**dict(old), "model": "", "mc": 0} if old else None
        return dict(row)

    def add(self, txid: str, leg: str, buyer: str, model: str, units: int, mc: int) -> bool:
        """A top-up, once per payment (for anybody); False when that payment was already counted."""
        with self._lock, self._db() as db:
            if db.execute("SELECT name FROM sqlite_master WHERE name='topup'").fetchone() and \
                    db.execute("SELECT 1 FROM topup WHERE txid=? AND leg=?", (txid, leg)).fetchone():
                return False
            try:
                db.execute("INSERT INTO mtopup VALUES (?,?,?,?,?,?,?)", (txid, leg, buyer, model, int(units), int(mc),
                                                                         time.time()))
            except sqlite3.IntegrityError:
                return False
            db.execute("INSERT INTO mcredit VALUES (?,?,?) ON CONFLICT(buyer, model) DO UPDATE SET mc=mc+?",
                       (buyer, model, int(mc), int(mc)))
        return True

    def reserve(self, buyer: str, model: str, mc: int) -> bool:
        if mc <= 0:
            return True
        with self._lock, self._db() as db:
            return db.execute("UPDATE mcredit SET mc=mc-? WHERE buyer=? AND model=? AND mc>=?",
                              (int(mc), buyer, model, int(mc))).rowcount > 0

    def give(self, buyer: str, model: str, mc: int) -> None:
        if mc <= 0:
            return
        with self._lock, self._db() as db:
            db.execute("INSERT INTO mcredit VALUES (?,?,?) ON CONFLICT(buyer, model) DO UPDATE SET mc=mc+?",
                       (buyer, model, int(mc), int(mc)))

    def take(self, buyer: str, model: str, mc: int) -> int:
        """A reply's cost (never below zero); the balance left."""
        with self._lock, self._db() as db:
            db.execute("INSERT INTO mcredit VALUES (?,?,0) ON CONFLICT(buyer, model) DO "
                       "UPDATE SET mc=MAX(0, mc-?)", (buyer, model, int(mc)))
            row = db.execute("SELECT mc FROM mcredit WHERE buyer=? AND model=?", (buyer, model)).fetchone()
        return int(row["mc"]) if row else 0

    def migrate(self, buyer: str, model: str, prices: dict[str, Any], scale) -> int:
        """The buyer's coin credit from before credits, into `model`'s credits at the seller's credit prices, once:
        thousandths of a credit added. A coin with no credit price now stays as it was (shown, not lost)."""
        if not model:
            return 0
        with self._lock, self._db() as db:
            if db.execute("SELECT 1 FROM mmigrated WHERE buyer=?", (buyer,)).fetchone():
                return 0
            if not db.execute("SELECT name FROM sqlite_master WHERE name='credit'").fetchone():
                db.execute("INSERT OR IGNORE INTO mmigrated VALUES (?,?)", (buyer, time.time()))
                return 0
            added = 0
            for r in db.execute("SELECT leg, units FROM credit WHERE buyer=? AND units>0", (buyer,)).fetchall():
                per = prices.get(r["leg"])
                if per is None or r["leg"] == "free":
                    continue
                mc = mc_for_payment(int(r["units"]), scale(r["leg"]), per)
                if mc > 0:
                    db.execute("INSERT INTO mcredit VALUES (?,?,?) ON CONFLICT(buyer, model) DO UPDATE SET mc=mc+?",
                               (buyer, model, mc, mc))
                    db.execute("UPDATE credit SET units=0 WHERE buyer=? AND leg=?", (buyer, r["leg"]))
                    added += mc
            db.execute("INSERT OR IGNORE INTO mmigrated VALUES (?,?)", (buyer, time.time()))
        return added


class _Closing:
    def __init__(self, db):
        self.db = db

    def __enter__(self):
        return self.db

    def __exit__(self, kind, exc, tb):
        try:
            if kind is None:
                self.db.commit()
            else:
                self.db.rollback()
        finally:
            self.db.close()
        return False
