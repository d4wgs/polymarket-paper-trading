"""
Round validity: was a round recorded well enough to count in the statistics?

The rule looks at DATA COVERAGE ONLY. It never reads P&L, fills, prices' direction or results, and one verdict per
round applies to every strategy alike. A round is INVALID when any of these holds (thresholds: config `validity:`):

  gap           more than `max_gap_seconds` (10) between two recorded ticks of the round, counting the stretch from
                the round's start to its first tick and from its last tick to its end
  coverage      fewer than `min_coverage` (90%) of the expected ticks were recorded (expected = round length /
                engine.poll_seconds)
  no_spot       more than `max_missing_share` (10%) of the recorded ticks had no spot price
  no_fair_value more than `max_missing_share` of the recorded ticks had no model fair value
  restart       the bot (re)started during the round (table `bot_runs`)
  clock_drift   a CRITICAL clock-drift alert was raised during the round (core/health.py, table `events`)

Evidence for the thresholds (716 complete rounds recorded 2026-11-07 .. 11-09): the worst gap per round has a 99th
percentile of 8.8 s (5m) / 12.3 s (15m) and the outages were 132-264 s; the rule marks 19 rounds (2.7%) invalid.

Verdicts live in `round_validity` (one row per round, written once when the round is settled; never rewritten).
Raw data and fills of invalid rounds are never deleted: core/metrics.load() and the tools leave them out of the
STATISTICS by default and always say how many rounds were left out and why (include_invalid=True puts them back).
A round with no verdict yet (still open, or older than the table and not backfilled) counts as valid.
"""
from __future__ import annotations

import sqlite3
from typing import Optional

DEFAULTS = {"max_gap_seconds": 10.0, "min_coverage": 0.90, "max_missing_share": 0.10}
REASONS = ("gap", "coverage", "no_spot", "no_fair_value", "restart", "clock_drift")
COLS = ("slug", "asset", "window_start", "window_end", "valid", "reason", "ticks", "expected", "coverage",
        "max_gap", "no_spot_share", "no_fv_share", "restarts", "clock_drift", "rule", "checked_at", "source")


def settings(cfg: Optional[dict]) -> dict:
    return {**DEFAULTS, **{k: v for k, v in (cfg or {}).items() if k in DEFAULTS and v is not None}}


def rule_text(cfg: Optional[dict], poll_seconds: float) -> str:
    c = settings(cfg)
    return (f"gap>{c['max_gap_seconds']:g}s|coverage<{c['min_coverage']:g}|missing>{c['max_missing_share']:g}"
            f"|poll={poll_seconds:g}s")


def assess(tick_ts: list, has_spot: list, has_fv: list, window_start: float, window_end: float,
           poll_seconds: float, cfg: Optional[dict] = None, restarts: int = 0, clock_drift: int = 0) -> dict:
    """Pure verdict for one round from its recorded tick times and which ticks had spot / fair value."""
    c = settings(cfg)
    keep = [i for i, t in enumerate(tick_ts) if window_start - 1e-6 <= t <= window_end + 1e-6]
    ts = sorted(tick_ts[i] for i in keep)
    n = len(ts)
    pts = [window_start] + ts + [window_end]
    max_gap = max(b - a for a, b in zip(pts, pts[1:]))
    expected = (window_end - window_start) / poll_seconds if poll_seconds > 0 else 0.0
    coverage = min(1.0, n / expected) if expected > 0 else 0.0
    no_spot = sum(1 for i in keep if not has_spot[i]) / n if n else 1.0
    no_fv = sum(1 for i in keep if not has_fv[i]) / n if n else 1.0
    reasons = []
    if max_gap > c["max_gap_seconds"]:
        reasons.append("gap")
    if coverage < c["min_coverage"]:
        reasons.append("coverage")
    if no_spot > c["max_missing_share"]:
        reasons.append("no_spot")
    if no_fv > c["max_missing_share"]:
        reasons.append("no_fair_value")
    if restarts:
        reasons.append("restart")
    if clock_drift:
        reasons.append("clock_drift")
    return {"valid": int(not reasons), "reason": ",".join(reasons) or None, "ticks": n, "expected": expected,
            "coverage": coverage, "max_gap": max_gap, "no_spot_share": no_spot, "no_fv_share": no_fv,
            "restarts": int(restarts), "clock_drift": int(clock_drift), "rule": rule_text(cfg, poll_seconds)}


def _tables(con) -> set:
    return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _snapshot_cols(con) -> set:
    return {r[1] for r in con.execute("PRAGMA table_info(snapshots)")}


def assess_round(con, slug: str, asset: str, window_start: float, window_end: float, poll_seconds: float,
                 cfg: Optional[dict] = None, have: Optional[set] = None, snap_cols: Optional[set] = None) -> dict:
    """The verdict for one round from a database's own records (snapshots, bot_runs, events). Read-only."""
    have = have if have is not None else _tables(con)
    snap_cols = snap_cols if snap_cols is not None else _snapshot_cols(con)
    spot = "spot" if "spot" in snap_cols else "NULL"
    fv = "fv_up" if "fv_up" in snap_cols else "NULL"
    rows = con.execute(f"SELECT ts, {spot}, {fv} FROM snapshots WHERE slug = ? ORDER BY ts", (slug,)).fetchall()
    restarts = 0
    if "bot_runs" in have:
        restarts = con.execute("SELECT COUNT(*) FROM bot_runs WHERE process = 'bot' AND started_at > ? AND "
                               "started_at <= ?", (window_start, window_end)).fetchone()[0]
    drift = 0
    if "events" in have:
        drift = con.execute("SELECT COUNT(*) FROM events WHERE strategy = '_system' AND kind = 'clock_drift' AND "
                            "ts >= ? AND ts <= ? AND data LIKE '%\"level\":\"critical\"%'",
                            (window_start, window_end)).fetchone()[0]
    out = assess([r[0] for r in rows], [r[1] is not None for r in rows], [r[2] is not None for r in rows],
                 window_start, window_end, poll_seconds, cfg, restarts, drift)
    out.update(slug=slug, asset=asset, window_start=window_start, window_end=window_end)
    return out


def record(con, row: dict, checked_at: float, source: str) -> bool:
    """Store a verdict. A round's verdict is written once: an existing row is left alone. True if inserted."""
    r = {**row, "checked_at": checked_at, "source": source}
    cur = con.execute(f"INSERT OR IGNORE INTO round_validity ({','.join(COLS)}) VALUES ({','.join('?' * len(COLS))})",
                      tuple(r.get(c) for c in COLS))
    return cur.rowcount > 0


def window_seconds(asset: str, default: float = 900.0) -> float:
    """'BTC-5m' -> 300 (the asset name ends with the round length)."""
    kind = str(asset).rsplit("-", 1)[-1]
    try:
        if kind.endswith("m"):
            return float(kind[:-1]) * 60
        if kind.endswith("h"):
            return float(kind[:-1]) * 3600
    except ValueError:
        pass
    return default


def _slug_start(slug: str) -> Optional[int]:
    try:
        return int(str(slug).rsplit("-", 1)[1])
    except (IndexError, ValueError):
        return None


def finished_rounds(con, since: float, until: float) -> list[tuple]:
    """[(slug, asset, window_start, window_end)] of recorded rounds that ended in (since, until]."""
    have = _tables(con)
    windows = {}
    if "windows" in have:
        windows = {r[0]: (r[1], r[2]) for r in con.execute("SELECT slug, window_start, window_end FROM windows")}
    out = []
    for slug, asset in con.execute("SELECT DISTINCT slug, asset FROM snapshots WHERE ts > ? AND ts <= ?",
                                   (since - 3600, until)):
        start = _slug_start(slug)
        if start is None:
            continue
        ws, we = windows.get(slug, (start, start + window_seconds(asset)))
        ws, we = ws or start, we or start + window_seconds(asset)
        if since < we <= until:
            out.append((slug, asset, ws, we))
    return sorted(out, key=lambda r: r[3])


def backfill(read_con, write_con, poll_seconds: float, cfg: Optional[dict], now: float, since: float = 0.0,
             until: Optional[float] = None, source: str = "backfill", dry_run: bool = False) -> dict:
    """
    Add verdicts for finished rounds that have none yet (insert-only; existing verdicts are never changed).
    `read_con` holds the recording (may be a read-only handle), `write_con` gets the rows (the same database for
    the live bot / the backfill tool; the replay output when replaying).
    """
    until = now - 5 if until is None else until
    done = set()
    if "round_validity" in _tables(write_con):
        done = {r[0] for r in write_con.execute("SELECT slug FROM round_validity")}
    have, cols = _tables(read_con), _snapshot_cols(read_con)
    src_rows = {}
    if read_con is not write_con and "round_validity" in have:      # a source that already has verdicts: reuse them
        cur = read_con.execute(f"SELECT {','.join(COLS)} FROM round_validity")
        src_rows = {r[0]: dict(zip(COLS, r)) for r in cur}
    n = {"checked": 0, "added": 0, "invalid": 0, "reasons": {}}
    for slug, asset, ws, we in finished_rounds(read_con, since, until):
        if slug in done:
            continue
        row = src_rows.get(slug) or assess_round(read_con, slug, asset, ws, we, poll_seconds, cfg, have, cols)
        n["checked"] += 1
        if not row["valid"]:
            n["invalid"] += 1
            for part in (row["reason"] or "").split(","):
                n["reasons"][part] = n["reasons"].get(part, 0) + 1
        if not dry_run and record(write_con, row, now, row.get("source") if slug in src_rows else source):
            n["added"] += 1
    if not dry_run:
        write_con.commit()
    return n


def invalid_rounds(con) -> dict[str, str]:
    """{slug: reason} of the rounds marked invalid ({} for a database without the table)."""
    try:
        return {r[0]: (r[1] or "unknown") for r in con.execute("SELECT slug, reason FROM round_validity WHERE valid = 0")}
    except sqlite3.Error:
        return {}


def counts(con) -> dict:
    """{'rounds': verdicts, 'invalid': n, 'reasons': {code: n}} for a database."""
    inv = invalid_rounds(con)
    try:
        total = con.execute("SELECT COUNT(*) FROM round_validity").fetchone()[0]
    except sqlite3.Error:
        total = 0
    return {"rounds": total, "invalid": len(inv), "reasons": reason_counts(inv.values())}


def reason_counts(reasons) -> dict:
    out: dict[str, int] = {}
    for r in reasons:
        for part in str(r or "unknown").split(","):
            out[part] = out.get(part, 0) + 1
    return dict(sorted(out.items()))
