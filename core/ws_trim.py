"""
Retention + size cap for data/ws_events.db (the recorder's database).

Why this exists: the old logic only trimmed ws_book when over the cap, and measured live pages, not the file.
ws_trades is ~250 MB/day and ws_book ~150 MB/day on the Pi, so "trim only books" could never reach the cap.

What a pass does (Trimmer.run), in order, all bounded by a time budget so the recorder is never blocked for long:
  1. per-table time retention (book_keep_days / trades_keep_days)
  2. total size cap: oldest rows are deleted from ALL recorded tables in proportion to their size until the live
     data is under `trim_target_pct` of `max_db_mb`. If nothing trimmable is left and it is still over, it logs
     a loud ERROR (never silent).
  3. incremental vacuum in small chunks + WAL checkpoint(TRUNCATE) so the file and the -wal really shrink
     (needs auto_vacuum=INCREMENTAL: new DBs get it automatically, old ones via tools/db_convert_autovac.py).

Invariants: ws_gaps (connect/disconnect/stop/... markers) and ws_meta are never touched; rows newer than
`min_keep_hours` are never deleted, by retention or by the cap.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger("ws_trim")

TABLES = ("ws_book", "ws_trades")                    # the ONLY tables this module ever deletes from
# average on-disk bytes per row incl. indexes, observed on the Pi (~150 MB / 310k book rows, ~250 MB / 1.0M trades);
# only used to split a cap trim between tables; the loop re-measures real live bytes after every round.
ROW_BYTES = {"ws_book": 480.0, "ws_trades": 250.0}


@dataclass
class Policy:
    keep_days: dict = field(default_factory=lambda: {"ws_book": 7, "ws_trades": 7})
    min_keep_s: float = 24 * 3600.0
    max_bytes: float = 3000e6
    target_frac: float = 0.90
    batch: int = 20_000
    budget_s: float = 5.0
    vacuum_pages: int = 2000
    vacuum_pause_s: float = 0.02
    vacuum_min_free_bytes: float = 64e6

    @classmethod
    def from_cfg(cls, cfg: Optional[dict]) -> "Policy":
        cfg = cfg or {}
        mb = cfg.get("max_db_mb", 3000)
        return cls(
            keep_days={"ws_book": cfg.get("book_keep_days", 7), "ws_trades": cfg.get("trades_keep_days", 7)},
            min_keep_s=float(cfg.get("min_keep_hours", 24)) * 3600.0,
            max_bytes=float(mb) * 1e6 if mb else 0.0,
            target_frac=float(cfg.get("trim_target_pct", 90)) / 100.0,
            batch=int(cfg.get("trim_batch_rows", 20_000)),
            budget_s=float(cfg.get("prune_budget_s", 5.0)),
            vacuum_pages=int(cfg.get("vacuum_chunk_pages", 2000)),
            vacuum_pause_s=float(cfg.get("vacuum_pause_ms", 20)) / 1000.0,
            vacuum_min_free_bytes=float(cfg.get("vacuum_min_free_mb", 64)) * 1e6)


def file_bytes(path: str) -> tuple[int, int]:
    """(main file, -wal file) sizes in bytes."""
    main = os.path.getsize(path) if os.path.exists(path) else 0
    wal = os.path.getsize(path + "-wal") if os.path.exists(path + "-wal") else 0
    return main, wal


class Trimmer:
    def __init__(self, db: sqlite3.Connection, path: str, policy: Policy, sleep=time.sleep):
        self.db, self.path, self.p, self.sleep = db, path, policy, sleep
        self.more = False                  # a pass ran out of time budget with work left: call again soon
        self._warned_autovac = False

    # ------------------------------------------------------------------ measuring
    def _pragma(self, name: str) -> int:
        return int(self.db.execute(f"PRAGMA {name}").fetchone()[0])

    def live_bytes(self) -> float:
        return (self._pragma("page_count") - self._pragma("freelist_count")) * self._pragma("page_size")

    def free_bytes(self) -> float:
        return self._pragma("freelist_count") * self._pragma("page_size")

    def sizes(self) -> dict:
        main, wal = file_bytes(self.path)
        return {"live_mb": round(self.live_bytes() / 1e6, 1), "file_mb": round(main / 1e6, 1),
                "wal_mb": round(wal / 1e6, 1)}

    def _count(self, table: str, older_than: Optional[float] = None) -> int:
        if older_than is None:
            return self.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        return self.db.execute(f"SELECT COUNT(*) FROM {table} WHERE recv_ts < ?", (older_than,)).fetchone()[0]

    # ------------------------------------------------------------------ deleting
    def _delete_oldest(self, table: str, cutoff: float, limit: Optional[int], t0: float) -> int:
        """Delete up to `limit` (None = all) oldest rows with recv_ts < cutoff, in committed batches."""
        assert table in TABLES
        done = 0
        while limit is None or done < limit:
            step = self.p.batch if limit is None else min(self.p.batch, limit - done)
            n = self.db.execute(
                f"DELETE FROM {table} WHERE rowid IN (SELECT rowid FROM {table} WHERE recv_ts < ? "
                f"ORDER BY recv_ts LIMIT ?)", (cutoff, step)).rowcount
            self.db.commit()                           # lets the recorder's own pending rows through too
            done += n
            if n < step:
                break
            if time.monotonic() - t0 > self.p.budget_s:
                self.more = True
                break
        return done

    # ------------------------------------------------------------------ the pass
    def run(self, now: float) -> dict:
        t0 = time.monotonic()
        self.more = False
        res = {"deleted": {t: 0 for t in TABLES}, "before": self.sizes(), "warnings": []}
        floor = now - self.p.min_keep_s                # nothing at or after this is ever deleted
        for t in TABLES:
            keep = self.p.keep_days.get(t)
            if keep:
                cutoff = min(now - keep * 86400.0, floor)
                res["deleted"][t] += self._delete_oldest(t, cutoff, None, t0)
        if self.p.max_bytes:
            self._cap(floor, res, t0)
        self._vacuum(res, t0)
        if any(res["deleted"].values()):
            self._checkpoint()                          # the -wal never outlives a trim, even when no vacuum was due
        res["after"] = self.sizes()
        return res

    def _cap(self, floor: float, res: dict, t0: float):
        target = self.p.max_bytes * self.p.target_frac
        for _ in range(60):
            live = self.live_bytes()
            if live <= target:
                return
            excess = live - target
            avail = {t: self._count(t, floor) for t in TABLES}
            total = {t: self._count(t) for t in TABLES}
            usable = [t for t in TABLES if avail[t] > 0]
            if not usable:
                break
            weight = {t: total[t] * ROW_BYTES[t] for t in usable}
            wsum = sum(weight.values()) or 1.0
            progressed = 0
            for t in usable:
                rows = min(avail[t], max(1, int(excess * weight[t] / wsum / ROW_BYTES[t])))
                n = self._delete_oldest(t, floor, rows, t0)
                res["deleted"][t] += n
                progressed += n
                if n >= avail[t]:
                    res["warnings"].append(f"{t}: everything older than the {self.p.min_keep_s / 3600:.0f} h minimum "
                                           f"is now deleted but the DB is still over the cap")
            if not progressed:
                break
            if time.monotonic() - t0 > self.p.budget_s:      # always at least one batch of progress per pass
                self.more = True
                return
        live = self.live_bytes()
        if live > self.p.max_bytes and not self.more:
            msg = (f"SIZE CAP CANNOT CONVERGE: {self.path} live data {live / 1e6:.0f} MB > max_db_mb "
                   f"{self.p.max_bytes / 1e6:.0f}; rows {dict((t, self._count(t)) for t in TABLES)}; everything "
                   f"older than min_keep_hours ({self.p.min_keep_s / 3600:.0f} h) is already deleted. Raise max_db_mb "
                   f"or lower min_keep_hours / the recording rate.")
            res["warnings"].append(msg)
            log.error(msg)

    def _vacuum(self, res: dict, t0: float):
        free = self.free_bytes()
        if free < self.p.vacuum_min_free_bytes:
            return
        if self._pragma("auto_vacuum") != 2:
            main, _ = file_bytes(self.path)
            if self.p.max_bytes and main > self.p.max_bytes * 1.1 and not self._warned_autovac:
                self._warned_autovac = True
                msg = (f"{self.path} is {main / 1e6:.0f} MB on disk but auto_vacuum is off, so it cannot shrink: run "
                       f"tools/db_convert_autovac.py once (recorder stopped)")
                res["warnings"].append(msg)
                log.error(msg)
            return
        while self._pragma("freelist_count") > 0:
            self.db.execute(f"PRAGMA incremental_vacuum({self.p.vacuum_pages})").fetchall()   # must be stepped to completion
            self.db.commit()
            if self.p.vacuum_pause_s:
                self.sleep(self.p.vacuum_pause_s)
            if time.monotonic() - t0 > self.p.budget_s:
                self.more = True
                break
        self._checkpoint()

    def _checkpoint(self):
        try:                                           # TRUNCATE needs no reader; if one is busy it just returns
            self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        except sqlite3.OperationalError:
            pass


def plan(path: str, policy: Policy, now: float) -> dict:
    """Read-only estimate of what a pass would delete (no changes made). For tools/ws_trim.py --dry-run."""
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        tr = Trimmer(db, path, policy)
        floor = now - policy.min_keep_s
        out = {"sizes": tr.sizes(), "tables": {}, "markers": db.execute("SELECT COUNT(*) FROM ws_gaps").fetchone()[0]}
        for t in TABLES:
            keep = policy.keep_days.get(t)
            cutoff = min(now - keep * 86400.0, floor) if keep else None
            row = db.execute(f"SELECT COUNT(*), MIN(recv_ts), MAX(recv_ts) FROM {t}").fetchone()
            out["tables"][t] = {"rows": row[0], "oldest": row[1], "newest": row[2],
                                "est_mb": round(row[0] * ROW_BYTES[t] / 1e6, 1),
                                "retention_deletes": tr._count(t, cutoff) if cutoff is not None else 0,
                                "trimmable_for_cap": tr._count(t, floor)}
        live = tr.live_bytes() - sum(v["retention_deletes"] * ROW_BYTES[t] for t, v in out["tables"].items())
        target = policy.max_bytes * policy.target_frac if policy.max_bytes else None
        out["after_retention_live_mb"] = round(live / 1e6, 1)
        out["cap_target_mb"] = round(target / 1e6, 1) if target else None
        if target and live > target:
            avail = {t: out["tables"][t]["trimmable_for_cap"] - out["tables"][t]["retention_deletes"] for t in TABLES}
            wsum = sum(out["tables"][t]["est_mb"] for t in TABLES if avail[t] > 0) or 1.0
            excess = live - target
            for t in TABLES:
                if avail[t] > 0:
                    share = excess * out["tables"][t]["est_mb"] / wsum
                    out["tables"][t]["cap_deletes"] = min(avail[t], int(share / ROW_BYTES[t]))
            out["converges"] = excess <= sum(avail[t] * ROW_BYTES[t] for t in TABLES)
        return out
    finally:
        db.close()
