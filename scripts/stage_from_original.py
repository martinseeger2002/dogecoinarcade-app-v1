"""Stage the original arcade's site data for this site: consistent copies (sqlite backup API, so the original may keep
running), into a folder to look at before anything moves. Nothing is written into the original's home.

    python3 scripts/stage_from_original.py ~/.dogecoinarcade /path/to/stage [--list]

Then (cutover, by hand): stop the original taking writes, stage again, copy the stage into the site's home.

Kept: accounts (seats, sessions, sealed wallets, mailboxes, imported-wallet records, faucet book, quotas), the message
store (envelopes + where the walk is), push (key + devices), the AI market's books, inscription runs and parts, the
referee's key and seeds, the exchange's site-held books, and settings.json filtered: every per-account key and the
site's own settings, without what V1.0's address index replaces (coin lists, backfills, Core and ord index state).
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import sys
from pathlib import Path

SQLITE = ["accounts.sqlite", "test.sqlite", "push.sqlite", "llm.sqlite", "accountruns.sqlite", "accountparts.sqlite",
          "referee.sqlite", "listings.sqlite", "standing.sqlite", "bids.sqlite", "swaps.sqlite", "swap-trades.sqlite",
          "apikeys.sqlite", "doginal_listings.sqlite"]
FILES = ["push-vapid.pem", "referee.key"]
#: Per-account settings (key prefix before ":"): moved.
ACCOUNT_KEYS = {"address", "coinkey", "mail_from", "wallet_name", "notif_seen", "homeapps", "segwit_watch",
                "segwit_keyhash", "wallets_hidden", "favorites", "mainnet", "boost", "extra_address", "came_from",
                "ring", "quota", "homebg", "backed_up", "share", "pool_parent", "import_day", "llm_pay_to",
                "announcing", "pools", "faucet"}
#: Plain settings of the site: moved.
SITE_KEYS = {"faucet", "faucet_daily", "faucet_real_coins", "seats", "public_hosts", "pages_host", "pages_origin",
             "operator", "unlimited_accounts", "llm", "llm_images", "turn", "brand", "brand_hosts", "install_terms",
             "affiliates", "import_from", "realtime_public_stun", "moderation", "modules", "look", "terms_gate",
             "terms_text"}
#: Replaced by V1.0 (its address index, its own indexes and Cores): dropped.
DROPPED = {"backfilled", "coinscan_all", "coinscan_owed", "core_rescanned", "coinlists_reconciled", "utxo_seeded",
           "ord_index", "core", "core_installed", "core_events", "ord_cores", "sites_index", "listings_import",
           "coinlists_reconcile_last", "faucet_supply", "faucet_topoff", "welcome", "announced", "flags", "mintpad",
           "fruit_stand", "auto_fill", "auto_sell", "cardwatch_trusted", "affiliates_heard"}


def stage(src: Path, dst: Path, listing: bool = False) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    for name in SQLITE:
        f = src / name
        if not f.exists():
            print(f"  -  {name} (none there)")
            continue
        if not listing:
            out = dst / name
            out.unlink(missing_ok=True)
            with sqlite3.connect(f"file:{f}?mode=ro", uri=True) as a, sqlite3.connect(out) as b:
                a.backup(b)
        print(f"  ok {name} ({f.stat().st_size:,} bytes)")
    for name in FILES:
        f = src / name
        if f.exists():
            if not listing:
                shutil.copy2(f, dst / name)
                (dst / name).chmod(0o600)
            print(f"  ok {name}")
        else:
            print(f"  -  {name} (none there)")
    settings = json.loads((src / "settings.json").read_text())
    kept, dropped, unknown = {}, [], []
    for key, value in settings.items():
        head = key.split(":", 1)[0]
        if (":" in key and head in ACCOUNT_KEYS) or key in SITE_KEYS:
            kept[key] = value
        elif head in DROPPED or key in DROPPED:
            dropped.append(key)
        else:
            unknown.append(key)
    if not listing:
        (dst / "settings.json").write_text(json.dumps(kept, indent=1, sort_keys=True))
        (dst / "settings.json").chmod(0o600)
    print(f"  ok settings.json: {len(kept)} kept, {len(dropped)} dropped (V1.0 does those)")
    if unknown:
        print(f"  ?? not kept, not known: {sorted({k.split(':', 1)[0] for k in unknown})}")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    stage(Path(sys.argv[1]).expanduser(), Path(sys.argv[2]).expanduser(), "--list" in sys.argv)
