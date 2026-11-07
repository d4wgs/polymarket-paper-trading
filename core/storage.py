"""
Everything the bot sees and does is written to one SQLite file (data/paper.db).
The dashboard only reads this file, so the bot and dashboard never interfere.

Tables
  strategies  - one row per strategy: class, parameters, bankroll
  fills       - every paper trade
  settlements - every finished 15-min window a strategy had a position in
  equity      - account value snapshots over time (for equity curves)
  snapshots   - top-of-book for every tick (lets you replay/backtest later)
  books       - the top N levels of both books for every tick (N = BOOK_LEVELS_LOGGED)
  resolutions - who won each round, and whether that was Polymarket's official result yet
  windows     - each round's exchange slug and "price to beat" (Up/Down open value)
  strategy_status - when a strategy hit its stop level and stopped trading (survives restarts)
  meta        - which exchange / fee model this database was recorded with
"""
from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import asdict

from core.models import Fill, Settlement, Snapshot

SCHEMA = """
CREATE TABLE IF NOT EXISTS strategies (
    name TEXT PRIMARY KEY, class TEXT, params TEXT, bankroll REAL, assets TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS fills (
    ts REAL, strategy TEXT, asset TEXT, slug TEXT, window_end REAL, outcome TEXT, side TEXT,
    shares REAL, avg_price REAL, usd REAL, fee REAL, reason TEXT, theta REAL, rebate REAL);
CREATE TABLE IF NOT EXISTS settlements (
    ts REAL, strategy TEXT, asset TEXT, slug TEXT, winner TEXT, up_shares REAL, down_shares REAL,
    net_cost REAL, payout REAL, pnl REAL);
CREATE TABLE IF NOT EXISTS equity (
    ts REAL, strategy TEXT, cash REAL, open_value REAL, equity REAL);
CREATE TABLE IF NOT EXISTS snapshots (
    ts REAL, asset TEXT, slug TEXT, seconds_left REAL,
    up_bid REAL, up_ask REAL, down_bid REAL, down_ask REAL, up_ask_depth REAL, down_ask_depth REAL,
    price_to_beat REAL, index_price REAL);
CREATE TABLE IF NOT EXISTS books (
    ts REAL, asset TEXT, slug TEXT, seconds_left REAL,
    up_bids TEXT, up_asks TEXT, down_bids TEXT, down_asks TEXT);   -- each: JSON [[price, shares], ...] best first
CREATE INDEX IF NOT EXISTS ix_books ON books(asset, ts);
CREATE INDEX IF NOT EXISTS ix_fills_strat ON fills(strategy);
CREATE INDEX IF NOT EXISTS ix_settle_strat ON settlements(strategy);
CREATE INDEX IF NOT EXISTS ix_eq_strat ON equity(strategy, ts);
CREATE INDEX IF NOT EXISTS ix_snap ON snapshots(asset, ts);
CREATE INDEX IF NOT EXISTS ix_snap_ts ON snapshots(ts);            -- dashboard: "last hour" query
CREATE INDEX IF NOT EXISTS ix_snap_slug ON snapshots(slug, ts);    -- dashboard: price path of one round
CREATE INDEX IF NOT EXISTS ix_settle_slug ON settlements(slug);
CREATE TABLE IF NOT EXISTS resolutions (
    slug TEXT PRIMARY KEY, asset TEXT, window_end REAL, winner TEXT, official INTEGER, ts REAL);
CREATE TABLE IF NOT EXISTS windows (
    slug TEXT PRIMARY KEY, asset TEXT, window_start REAL, window_end REAL, exchange_slug TEXT,
    price_to_beat REAL);
CREATE TABLE IF NOT EXISTS strategy_status (
    name TEXT PRIMARY KEY, stopped_at REAL, equity REAL, reason TEXT);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

# columns added after the first release: (table, column, type) -- added to older databases on open
MIGRATIONS = [
    ("fills", "theta", "REAL"), ("fills", "rebate", "REAL"),
    ("snapshots", "price_to_beat", "REAL"), ("snapshots", "index_price", "REAL"),
]
FILL_COLS = ("ts", "strategy", "asset", "slug", "window_end", "outcome", "side", "shares", "avg_price",
             "usd", "fee", "reason", "theta", "rebate")
SNAP_COLS = ("ts", "asset", "slug", "seconds_left", "up_bid", "up_ask", "down_bid", "down_ask",
             "up_ask_depth", "down_ask_depth", "price_to_beat", "index_price")


class Storage:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")   # lets the dashboard read while the bot writes
        self.db.executescript(SCHEMA)
        for table, col, typ in MIGRATIONS:
            cols = {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}
            if col not in cols:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
        self.db.commit()

    # ---------------------------------------------------------------- meta / status
    def set_meta(self, key: str, value: str):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, str(value)))
        self.db.commit()

    def get_meta(self, key: str, default=None):
        r = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r[0] if r else default

    def stopped_strategies(self) -> dict[str, dict]:
        return {r["name"]: dict(r) for r in self.db.execute("SELECT * FROM strategy_status")}

    def mark_stopped(self, name: str, ts: float, equity: float, reason: str):
        self.db.execute("INSERT OR REPLACE INTO strategy_status VALUES (?,?,?,?)", (name, ts, equity, reason))
        self.db.commit()

    def log_window(self, info):
        self.db.execute("INSERT OR REPLACE INTO windows VALUES (?,?,?,?,?,?)",
                        (info.slug, info.asset, info.window_start, info.window_end,
                         getattr(info, "exchange_slug", "") or info.slug, getattr(info, "price_to_beat", None)))

    def register_strategy(self, name, cls, params, bankroll, assets, ts) -> str | None:
        """Returns a warning string if this name was used before with different settings."""
        row = self.db.execute("SELECT class, params, bankroll, assets FROM strategies WHERE name=?",
                              (name,)).fetchone()
        p = json.dumps(params, sort_keys=True)
        a = json.dumps(sorted(assets) if assets is not None else None)
        if row is None:
            self.db.execute("INSERT INTO strategies VALUES (?,?,?,?,?,?)", (name, cls, p, bankroll, a, ts))
            self.db.commit()
            return None
        old_assets = row["assets"]
        try:   # older rows stored the list unsorted
            old = json.loads(old_assets) if old_assets else None
            old_assets = json.dumps(sorted(old) if old is not None else None)
        except (TypeError, ValueError):
            pass
        if row["class"] != cls or row["params"] != p or row["bankroll"] != bankroll or old_assets != a:
            return (f"Strategy '{name}' already exists in the database with different settings "
                    f"({row['class']} {row['params']}, bankroll {row['bankroll']}, markets {row['assets']}). "
                    f"Its history will be mixed. Give it a new name (e.g. '{name}_v2') if you changed "
                    f"the logic, parameters, bankroll or markets.")
        return None

    def log_fill(self, f: Fill):
        self.db.execute(f"INSERT INTO fills ({','.join(FILL_COLS)}) VALUES ({','.join('?' * len(FILL_COLS))})",
                        (f.ts, f.strategy, f.asset, f.slug, f.window_end, f.outcome, f.side,
                         f.shares, f.avg_price, f.usd, f.fee, f.reason,
                         getattr(f, "theta", None), getattr(f, "rebate", 0.0)))

    def log_settlement(self, s: Settlement):
        d = asdict(s)
        self.db.execute("INSERT INTO settlements VALUES (?,?,?,?,?,?,?,?,?,?)", tuple(d.values()))

    def log_equity(self, ts, strategy, cash, open_value):
        self.db.execute("INSERT INTO equity VALUES (?,?,?,?,?)", (ts, strategy, cash, open_value, cash + open_value))

    def log_snapshot(self, s: Snapshot):
        self.db.execute(f"INSERT INTO snapshots ({','.join(SNAP_COLS)}) VALUES ({','.join('?' * len(SNAP_COLS))})",
                        (s.ts, s.market.asset, s.market.slug, s.seconds_left,
                         s.up.best_bid, s.up.best_ask, s.down.best_bid, s.down.best_ask,
                         s.up.ask_depth_usd(), s.down.ask_depth_usd(),
                         getattr(s.market, "price_to_beat", None), getattr(s, "index_price", None)))

    def log_book(self, s: Snapshot, depth: int = 10):
        """Full order book (top `depth` levels per side) for every tick, as compact JSON."""
        def lv(levels):
            return json.dumps([[p, round(sz, 2)] for p, sz in levels[:depth]], separators=(",", ":"))
        self.db.execute("INSERT INTO books VALUES (?,?,?,?,?,?,?,?)",
                        (s.ts, s.market.asset, s.market.slug, s.seconds_left,
                         lv(s.up.bids), lv(s.up.asks), lv(s.down.bids), lv(s.down.asks)))

    def log_resolution(self, slug, asset, window_end, winner, official, ts):
        self.db.execute("INSERT OR REPLACE INTO resolutions VALUES (?,?,?,?,?,?)",
                        (slug, asset, window_end, winner, int(bool(official)), ts))

    def provisional_resolutions(self) -> list[dict]:
        """Rounds settled on locked prices that Polymarket hasn't officially closed yet."""
        return [dict(r) for r in self.db.execute("SELECT * FROM resolutions WHERE official=0")]

    def recent_resolutions(self, since_ts: float) -> list[dict]:
        """Results of rounds that ended after `since_ts`, oldest first."""
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM resolutions WHERE window_end >= ? ORDER BY window_end", (since_ts,))]

    def settled_slugs(self, slugs: list[str]) -> set[str]:
        """Which of these rounds some strategy had a settlement in."""
        if not slugs:
            return set()
        q = ",".join("?" * len(slugs))
        return {r[0] for r in self.db.execute(f"SELECT DISTINCT slug FROM settlements WHERE slug IN ({q})", slugs)}

    def correct_settlement(self, slug: str, new_winner: str) -> list[dict]:
        """
        Re-settle a round on the other side. Rewrites its settlement rows (winner, payout, P&L)
        and returns [{strategy, old_payout, new_payout, pnl}] so the brokers can fix their cash.
        Rewriting (not adding) rows keeps trade counts right and restarts consistent.
        """
        out = []
        rows = self.db.execute("SELECT rowid, * FROM settlements WHERE slug=?", (slug,)).fetchall()
        for r in rows:
            payout = r["up_shares"] if new_winner == "UP" else r["down_shares"]
            pnl = payout - r["net_cost"]
            self.db.execute("UPDATE settlements SET winner=?, payout=?, pnl=? WHERE rowid=?",
                            (new_winner, payout, pnl, r["rowid"]))
            out.append({"strategy": r["strategy"], "old_payout": r["payout"], "new_payout": payout, "pnl": pnl})
        return out

    def history_for(self, strategy: str) -> tuple[list[dict], list[dict]]:
        fills = [dict(r) for r in self.db.execute("SELECT * FROM fills WHERE strategy=? ORDER BY ts", (strategy,))]
        settles = [dict(r) for r in self.db.execute("SELECT * FROM settlements WHERE strategy=?", (strategy,))]
        return fills, settles

    def commit(self):
        self.db.commit()
