"""
Append-only storage for the websocket recorder: data/ws_events.db (a SEPARATE sqlite file, so it can never
lock or bloat paper_us.db).

What the probe (data/ws_probe.jsonl, see README) showed, which decides this schema:
  * SUBSCRIPTION_TYPE_MARKET_DATA messages are FULL SNAPSHOTS, not deltas: every message carries the whole
    book (~100 price levels a side, ~8 KB) plus the `stats` block. There is nothing to "apply".
  * They arrive ~9-10 times a second PER MARKET and nearly every one differs (deep levels keep moving), so
    storing them raw would be ~15+ GB/day. We keep the top `book_levels` levels (default 10, what the bot's
    own `books` table keeps), the stats, and skip messages whose kept content is unchanged; rows per market
    are limited to one per `book_min_interval_ms`.
  * SUBSCRIPTION_TYPE_TRADE messages are one print each, unique `id`, always TRADE_STATE_NEW. Every print is
    kept (no throttling): they are what the maker-fill model needs.
  * exchange timestamps are nanosecond ISO strings; we store them as unix seconds next to OUR receive time.

Tables
  ws_book    one row per (kept) book snapshot: recv_ts, exch_ts, slug, state, best bid/ask, top-N levels as
             JSON [[price, qty], ...] best first, and the stats (last px, shares/notional traded, open interest, hi/lo)
  ws_trades  one row per print: recv_ts, trade_ts, slug, trade_id (unique), price, qty, maker/taker side + intent
             + outcome side + action, state
  ws_gaps    a row for every connect / disconnect / stop / bad message: data between a 'disconnect' and the
             next 'connect' may be missing
  ws_meta    key/value (schema version, recorder start times)
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from typing import Optional

from core.polymarket_us import _amount, _iso_ts
from core.recorder import disk_report

log = logging.getLogger("ws_store")

SCHEMA = """
CREATE TABLE IF NOT EXISTS ws_book (
    recv_ts REAL, exch_ts REAL, slug TEXT, state TEXT, best_bid REAL, best_ask REAL,
    bids TEXT, offers TEXT, n_bids INTEGER, n_offers INTEGER,
    last_px REAL, last_qty REAL, shares_traded REAL, notional_traded REAL, open_interest REAL,
    high_px REAL, low_px REAL);
CREATE INDEX IF NOT EXISTS ix_wsbook ON ws_book(slug, recv_ts);
CREATE INDEX IF NOT EXISTS ix_wsbook_ts ON ws_book(recv_ts);
CREATE TABLE IF NOT EXISTS ws_trades (
    recv_ts REAL, trade_ts REAL, slug TEXT, trade_id TEXT, price REAL, qty REAL,
    maker_side TEXT, maker_intent TEXT, maker_outcome TEXT, maker_action TEXT,
    taker_side TEXT, taker_intent TEXT, taker_outcome TEXT, taker_action TEXT, state TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS ux_wstrades_id ON ws_trades(trade_id);
CREATE INDEX IF NOT EXISTS ix_wstrades ON ws_trades(slug, trade_ts);
CREATE INDEX IF NOT EXISTS ix_wstrades_ts ON ws_trades(recv_ts);
CREATE TABLE IF NOT EXISTS ws_gaps (ts REAL, kind TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS ws_meta (key TEXT PRIMARY KEY, value TEXT);
"""


def _short(enum: Optional[str]) -> Optional[str]:
    """'ORDER_SIDE_BUY' -> 'BUY', 'ORDER_INTENT_BUY_LONG' -> 'BUY_LONG', 'OUTCOME_SIDE_YES' -> 'YES'."""
    if not enum:
        return None
    for pre in ("ORDER_SIDE_", "ORDER_INTENT_", "OUTCOME_SIDE_", "ORDER_ACTION_", "TRADE_STATE_", "MARKET_STATE_"):
        if enum.startswith(pre):
            return enum[len(pre):]
    return enum


def _levels(side, n: int) -> list[list[float]]:
    out = []
    for lv in (side or [])[:n]:
        p = _amount(lv.get("px"))
        try:
            q = float(lv.get("qty", 0))
        except (TypeError, ValueError):
            q = 0.0
        if p is not None:
            out.append([p, round(q, 4)])
    return out


class WsStore:
    """The recorder's sink. Not thread-safe: used only from the recorder's event loop."""

    def __init__(self, path: str, cfg: Optional[dict] = None, clock=time.time):
        cfg = cfg or {}
        self.path = path
        self.clock = clock
        self.levels = int(cfg.get("book_levels", 10))
        self.min_interval = float(cfg.get("book_min_interval_ms", 500)) / 1000.0
        self.book_keep_days = cfg.get("book_keep_days", 2)
        self.trades_keep_days = cfg.get("trades_keep_days", 14)
        self.max_db_mb = cfg.get("max_db_mb", 3000)
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        self.db.execute("INSERT OR REPLACE INTO ws_meta VALUES ('schema', '1')")
        self.db.execute("INSERT OR REPLACE INTO ws_meta VALUES (?, ?)", (f"started_{int(clock())}", "recorder"))
        self.db.commit()
        self._last_key: dict[str, tuple] = {}         # slug -> kept content of the last stored book
        self._last_write: dict[str, float] = {}       # slug -> recv_ts of the last stored book
        self._pending: dict[str, tuple] = {}          # slug -> row skipped by the throttle (flushed later)
        self._uncommitted = 0
        self._last_commit = clock()
        self._last_prune = 0.0
        self._last_gc = 0.0
        self.stored = {"book": 0, "trade": 0, "dup_trade": 0, "skipped_book": 0}

    # ---------------------------------------------------------------------------- sink API
    def write(self, recv_ts: float, msg: dict):
        if "marketData" in msg:
            self._book(recv_ts, msg["marketData"])
        elif "trade" in msg:
            self._trade(recv_ts, msg["trade"])
        self._housekeeping(recv_ts)

    def gap(self, ts: float, kind: str, detail: str = ""):
        self._flush_pending()
        self.db.execute("INSERT INTO ws_gaps VALUES (?,?,?)", (ts, kind, detail))
        self.db.commit()
        self._uncommitted = 0

    def flush(self):
        self._flush_pending()
        self.db.commit()
        self._uncommitted = 0

    def close(self):
        try:
            self.flush()
        finally:
            self.db.close()

    # ---------------------------------------------------------------------------- rows
    def _book(self, recv_ts: float, md: dict):
        slug = md.get("marketSlug")
        if not slug:
            return
        bids, offers = _levels(md.get("bids"), self.levels), _levels(md.get("offers"), self.levels)
        st = md.get("stats") or {}
        stats = (_amount(st.get("lastTradePx")), _amount(st.get("lastTradeQty")), _amount(st.get("sharesTraded")),
                 _amount(st.get("notionalTraded")), _amount(st.get("openInterest")),
                 _amount(st.get("highPx")), _amount(st.get("lowPx")))
        state = _short(md.get("state"))
        key = (json.dumps(bids), json.dumps(offers), state, stats)
        nb, no = len(md.get("bids") or []), len(md.get("offers") or [])
        row = (recv_ts, _iso_ts(md.get("transactTime")), slug, state,
               bids[0][0] if bids else None, offers[0][0] if offers else None,
               key[0], key[1], nb, no) + stats
        if self._last_key.get(slug) == key:
            self.stored["skipped_book"] += 1
            self._pending.pop(slug, None)
            return
        last_state = self._last_key.get(slug, (None, None, None))[2]
        if recv_ts - self._last_write.get(slug, 0.0) < self.min_interval and state == last_state:
            self._pending[slug] = (key, row)          # newer than what is stored: written once the interval passes
            self.stored["skipped_book"] += 1
            return
        self._insert_book(slug, key, row)

    def _insert_book(self, slug, key, row):
        self.db.execute("INSERT INTO ws_book VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
        self._last_key[slug] = key
        self._last_write[slug] = row[0]
        self._pending.pop(slug, None)
        self._uncommitted += 1
        self.stored["book"] += 1

    def _flush_pending(self, now: Optional[float] = None):
        for slug, (key, row) in list(self._pending.items()):
            if now is None or now - self._last_write.get(slug, 0.0) >= self.min_interval:
                self._insert_book(slug, key, row)

    def _trade(self, recv_ts: float, t: dict):
        maker, taker = t.get("maker") or {}, t.get("taker") or {}
        cur = self.db.execute(
            "INSERT OR IGNORE INTO ws_trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (recv_ts, _iso_ts(t.get("tradeTime")), t.get("marketSlug"), t.get("id"),
             _amount(t.get("price")), _amount(t.get("quantity")),
             _short(maker.get("side")), _short(maker.get("intent")), _short(maker.get("outcomeSide")),
             _short(maker.get("action")),
             _short(taker.get("side")), _short(taker.get("intent")), _short(taker.get("outcomeSide")),
             _short(taker.get("action")), _short(t.get("state"))))
        if cur.rowcount:
            self.stored["trade"] += 1
            self._uncommitted += 1
        else:
            self.stored["dup_trade"] += 1

    # ---------------------------------------------------------------------------- upkeep
    def _housekeeping(self, now: float):
        self._flush_pending(now)
        if now - self._last_gc >= 600:               # forget slugs that have been quiet for an hour (rounds are over)
            self._last_gc = now
            for slug in [k for k, t in self._last_write.items() if now - t > 3600]:
                self._last_write.pop(slug, None)
                self._last_key.pop(slug, None)
                self._pending.pop(slug, None)
        if self._uncommitted and (self._uncommitted >= 500 or now - self._last_commit >= 1.0):
            self.db.commit()
            self._uncommitted, self._last_commit = 0, now
        if now - self._last_prune >= 600:
            self._last_prune = now
            try:
                self.prune(now)
            except Exception:
                log.exception("Retention pass failed; will retry")

    def prune(self, now: float, batch: int = 50_000) -> dict:
        """Retention + size guard. Deletes in batches so a long delete never blocks recording for long."""
        deleted = {"book": 0, "trades": 0}
        plan = []
        if self.book_keep_days:
            plan.append(("ws_book", "book", now - self.book_keep_days * 86400))
        if self.trades_keep_days:
            plan.append(("ws_trades", "trades", now - self.trades_keep_days * 86400))
        for table, name, cutoff in plan:
            n = self.db.execute(f"DELETE FROM {table} WHERE rowid IN "
                                f"(SELECT rowid FROM {table} WHERE recv_ts < ? LIMIT ?)", (cutoff, batch)).rowcount
            deleted[name] += n
        # hard size cap: the file never shrinks by itself, so judge by live pages (file - free pages)
        if self.max_db_mb:
            page = self.db.execute("PRAGMA page_size").fetchone()[0]
            total = self.db.execute("PRAGMA page_count").fetchone()[0]
            free = self.db.execute("PRAGMA freelist_count").fetchone()[0]
            live_mb = (total - free) * page / 1e6
            if live_mb > self.max_db_mb:
                cut = self.db.execute("SELECT recv_ts FROM ws_book ORDER BY recv_ts LIMIT 1 OFFSET "
                                      "(SELECT COUNT(*) / 2 FROM ws_book)").fetchone()
                if cut:
                    n = self.db.execute("DELETE FROM ws_book WHERE rowid IN (SELECT rowid FROM ws_book "
                                        "WHERE recv_ts < ? LIMIT ?)", (cut[0], batch)).rowcount
                    deleted["book"] += n
                    log.warning("ws_events.db live data %.0f MB > max_db_mb %s: trimmed %d old book rows",
                                live_mb, self.max_db_mb, n)
        self.db.commit()
        if any(deleted.values()):
            log.info("Retention deleted %s", deleted)
        log.info("Disk: %s", disk_report(self.path))
        return deleted
