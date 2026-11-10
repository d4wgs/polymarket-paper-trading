"""
Health monitor: clock drift, and bot uptime / restarts. It only observes and records; it NEVER halts trading
(stopping the bot would create exactly the data gaps the round-validity rule exists to catch).

CLOCK DRIFT. Every round, tick and "seconds left" is timed off the machine's clock, and it was only checked at
startup. Now, every `check_seconds` (60):

  method "exchange"  Every order-book poll already carries the exchange's own timestamp (transactTime), so each
                     poll gives  offset = local receive time - exchange time  at no extra cost (no extra requests).
                     An offset is clock error PLUS network / processing delay (measured on the Pi: median 0.34 s,
                     1st percentile 0.11 s), so the reading is the SMALLEST offset of the last minute minus
                     `expected_latency_seconds` (0.15): the fastest reply of the minute is nearly pure clock error.
                     Positive = this clock is ahead of the exchange, negative = behind.
  method "ntp"       `timedatectl show -p NTPSynchronized` (Linux): is the system clock still NTP-synchronized?
                     It can't measure how far off the clock is, only that syncing stopped.
  method "both"      (default) the exchange reading sets the level; losing NTP sync raises at least a warning.

  |drift| >= warn_seconds (0.5)      -> WARNING in the log + an `events` row (strategy "_system", kind "clock_drift")
  |drift| >= critical_seconds (1.5)  -> the same with level "critical"; every round that overlaps it is marked
                                        invalid (reason clock_drift) when it is settled (core/validity.py)

Every reading is stored in `health` (kind "clock"), alerts or not, so the history of the clock can be read back.

UPTIME. `bot_runs` gets one row per process start (started_at, code version, host, pid) and its `last_seen` is
refreshed every `heartbeat_seconds` (60). Downtime = the hole between one run's last_seen and the next run's
started_at; a bot start inside a round also invalidates that round (reason restart). The websocket recorder keeps
its own record in ws_events.db (ws_meta `started_<ts>` / `last_seen`, ws_gaps connect / disconnect / stop).

    python tools/health.py        # current drift, last alert, runs and downtime, recorder, invalid rounds
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import statistics
import subprocess
import sys
import time
from typing import Callable, Optional

log = logging.getLogger("health")

DEFAULTS = {"enabled": True, "check_seconds": 60, "heartbeat_seconds": 60, "method": "both",
            "warn_seconds": 0.5, "critical_seconds": 1.5, "expected_latency_seconds": 0.15, "min_samples": 5}
SYSTEM = "_system"           # the `strategy` value of system events in the events table


def ntp_synchronized() -> Optional[bool]:
    """True / False from timedatectl, None when it can't be told (not Linux, no timedatectl, error)."""
    if not sys.platform.startswith("linux") or not shutil.which("timedatectl"):
        return None
    try:
        out = subprocess.run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
                             capture_output=True, text=True, timeout=2).stdout.strip()
    except Exception:
        return None
    return {"yes": True, "no": False}.get(out)


class Health:
    def __init__(self, store, cfg: Optional[dict] = None, clock: Callable[[], float] = time.time,
                 process: str = "bot", ntp_reader: Callable[[], Optional[bool]] = ntp_synchronized):
        self.cfg = {**DEFAULTS, **{k: v for k, v in (cfg or {}).items() if v is not None}}
        self.store, self.clock, self.process, self.ntp_reader = store, clock, process, ntp_reader
        self.enabled = bool(self.cfg["enabled"])
        self.samples: list[float] = []
        self.run_id: Optional[int] = None
        self._last_check = self._last_beat = None
        self.last: Optional[dict] = None             # the latest reading

    # ------------------------------------------------------------------ uptime
    def start(self, now: float, code_version: Optional[str] = None):
        """Record this process start (one bot_runs row)."""
        if not self.enabled:
            return
        try:
            cur = self.store.db.execute(
                "INSERT INTO bot_runs (process, started_at, last_seen, code_version, host, pid) VALUES (?,?,?,?,?,?)",
                (self.process, now, now, code_version, socket.gethostname(), os.getpid()))
            self.run_id = cur.lastrowid
            self.store.db.commit()
            self._last_check = self._last_beat = now
        except Exception:
            log.exception("Could not record the process start")

    def _heartbeat(self, now: float):
        if self.run_id is not None:
            self.store.db.execute("UPDATE bot_runs SET last_seen = ? WHERE id = ?", (now, self.run_id))
        self._last_beat = now

    # ------------------------------------------------------------------ clock
    def sample(self, local_ts: float, exchange_ts: Optional[float]):
        """One order-book poll: when we received it vs the exchange's own timestamp on it."""
        if self.enabled and exchange_ts:
            self.samples.append(local_ts - exchange_ts)

    def tick(self, now: float):
        """Called once per engine tick: refreshes the heartbeat and runs the clock check when due."""
        if not self.enabled:
            return
        try:
            if self._last_beat is None or now - self._last_beat >= self.cfg["heartbeat_seconds"]:
                self._heartbeat(now)
            if self._last_check is None or now - self._last_check >= self.cfg["check_seconds"]:
                self._last_check = now
                self.check(now)
        except Exception:
            log.exception("Health check failed (trading is not affected)")

    def check(self, now: float) -> dict:
        """One clock reading from the samples collected since the last one. Stored always; alerts when off."""
        c = self.cfg
        offsets, self.samples = self.samples, []
        method = c["method"]
        drift = min_off = med = None
        if method in ("exchange", "both") and len(offsets) >= c["min_samples"]:
            min_off, med = min(offsets), statistics.median(offsets)
            drift = min_off - c["expected_latency_seconds"]
        ntp = self.ntp_reader() if method in ("ntp", "both") else None
        if drift is None:
            level = "nodata" if method != "ntp" else "ok"
        elif abs(drift) >= c["critical_seconds"]:
            level = "critical"
        elif abs(drift) >= c["warn_seconds"]:
            level = "warn"
        else:
            level = "ok"
        why = None
        if ntp is False and level in ("ok", "nodata"):
            level, why = "warn", "ntp_unsynchronized"
        detail = {"level": level, "drift": None if drift is None else round(drift, 3),
                  "min_offset": None if min_off is None else round(min_off, 3),
                  "median_offset": None if med is None else round(med, 3), "samples": len(offsets),
                  "ntp_synchronized": ntp, "method": method}
        if why:
            detail["why"] = why
        self.store.db.execute("INSERT INTO health (ts, kind, level, value, detail) VALUES (?,?,?,?,?)",
                              (now, "clock", level, drift, json.dumps(detail, separators=(",", ":"))))
        if level in ("warn", "critical"):
            self.store.log_event(now, SYSTEM, "", "", "clock_drift", detail)
            shown = "unknown" if drift is None else f"{drift:+.2f}s"
            if level == "critical":
                log.error("CLOCK DRIFT CRITICAL: this clock is %s vs the exchange (limit %.1fs). Rounds overlapping "
                          "this are marked invalid for statistics. Trading continues.", shown, c["critical_seconds"])
            else:
                log.warning("Clock drift warning: %s vs the exchange (warn at %.1fs)%s", shown, c["warn_seconds"],
                            "; NTP is not synchronized" if ntp is False else "")
        self.last = {"ts": now, **detail}
        return self.last


# ---------------------------------------------------------------------------------------- report (read-only)
def _t(ts: Optional[float]) -> str:
    return "-" if not ts else time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def _dur(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 120:
        return f"{seconds:.0f}s"
    if seconds < 7200:
        return f"{seconds / 60:.0f}m"
    return f"{seconds / 3600:.1f}h"


def report(con, now: float, ws_con=None, runs: int = 8) -> list[str]:
    """Lines for tools/health.py from read-only connections (the bot's database; optionally the recorder's)."""
    have = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    out = []
    if "health" not in have:
        return ["This database has no health records yet (they are written by code deployed from 2026-11-09 on)."]
    last = con.execute("SELECT ts, level, value, detail FROM health WHERE kind='clock' ORDER BY ts DESC LIMIT 1").fetchone()
    if last is None:
        out.append("CLOCK   no reading yet")
    else:
        d = json.loads(last[3] or "{}")
        drift = "unknown (no exchange samples)" if last[2] is None else f"{last[2]:+.3f}s vs the exchange"
        out.append(f"CLOCK   drift {drift}  [{last[1].upper()}]  read {_dur(now - last[0])} ago ({_t(last[0])}); "
                   f"NTP synchronized: {d.get('ntp_synchronized')}; samples {d.get('samples')}")
        day = con.execute("SELECT COUNT(*), MAX(ABS(value)), SUM(level='warn'), SUM(level='critical'), "
                          "SUM(level='nodata') FROM health WHERE kind='clock' AND ts > ?", (now - 86400,)).fetchone()
        out.append(f"        last 24h: {day[0]} readings, largest |drift| "
                   f"{'-' if day[1] is None else format(day[1], '.3f') + 's'}, {day[2] or 0} warning(s), "
                   f"{day[3] or 0} critical, {day[4] or 0} without data")
        alert = con.execute("SELECT ts, level, value, detail FROM health WHERE kind='clock' AND level IN "
                            "('warn','critical') ORDER BY ts DESC LIMIT 1").fetchone()
        if alert:
            why = json.loads(alert[3] or "{}").get("why")
            out.append(f"        last alert: {alert[1].upper()} at {_t(alert[0])} ({_dur(now - alert[0])} ago), drift "
                       f"{'-' if alert[2] is None else format(alert[2], '+.3f') + 's'}{' (' + why + ')' if why else ''}")
        else:
            out.append("        last alert: none recorded")
    rows = con.execute("SELECT id, started_at, last_seen, code_version, host FROM bot_runs WHERE process='bot' "
                       "ORDER BY started_at DESC LIMIT ?", (runs,)).fetchall()[::-1]
    out.append(f"BOT     {con.execute('SELECT COUNT(*) FROM bot_runs').fetchone()[0]} start(s) recorded")
    prev_seen = None
    if rows:
        before = con.execute("SELECT last_seen FROM bot_runs WHERE process='bot' AND started_at < ? ORDER BY "
                             "started_at DESC LIMIT 1", (rows[0][1],)).fetchone()
        prev_seen = before[0] if before else None
    for i, (rid, start, seen, ver, host) in enumerate(rows):
        down = f"  after {_dur(start - prev_seen)} down" if prev_seen is not None else ""
        live = i == len(rows) - 1 and now - seen < 180
        out.append(f"        started {_t(start)}{down}; ran {_dur(seen - start)}"
                   f"{' and is RUNNING' if live else f'; last seen {_t(seen)}'}  [code {ver or '?'}, {host or '?'}]")
        prev_seen = seen
    if rows and now - rows[-1][2] >= 180:
        out.append(f"        NOT RUNNING: no heartbeat for {_dur(now - rows[-1][2])}")
    if "round_validity" in have:
        v = con.execute("SELECT COUNT(*), SUM(valid = 0) FROM round_validity WHERE window_end > ?", (now - 86400,)).fetchone()
        why = {}
        for (reason,) in con.execute("SELECT reason FROM round_validity WHERE valid = 0 AND window_end > ?", (now - 86400,)):
            for part in (reason or "unknown").split(","):
                why[part] = why.get(part, 0) + 1
        out.append(f"ROUNDS  last 24h: {v[0]} checked, {v[1] or 0} invalid"
                   + (f" ({', '.join(f'{k} {n}' for k, n in sorted(why.items()))})" if why else ""))
    if ws_con is not None:
        try:
            meta = dict(ws_con.execute("SELECT key, value FROM ws_meta").fetchall())
            starts = sorted(int(k.split("_", 1)[1]) for k in meta if k.startswith("started_"))
            seen = float(meta["last_seen"]) if meta.get("last_seen") else None
            g = ws_con.execute("SELECT SUM(kind='disconnect'), SUM(kind='connect'), MAX(ts) FROM ws_gaps WHERE ts > ?",
                               (now - 86400,)).fetchone()
            out.append(f"RECORDER {len(starts)} start(s) recorded; latest {_t(starts[-1]) if starts else '-'}; "
                       + (f"last seen {_dur(now - seen)} ago" if seen else "no heartbeat recorded yet")
                       + f"; last 24h: {g[0] or 0} disconnect(s), {g[1] or 0} connect(s)")
        except Exception as e:                                # an older recorder database
            out.append(f"RECORDER no uptime record readable ({e})")
    return out
