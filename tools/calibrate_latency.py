"""
Calibrate the replay's order-latency book against the live bot's own fills.

    python tools/calibrate_latency.py --src data/paper_us.db --ws-db data/ws_events.db
    python tools/calibrate_latency.py --src X.db --ws-db W.db --sweep 100,250,400,600 --strategy s3_sniper_v2

Only the period the websocket recorder covers qualifies (from its first book row, rounded up to a round start,
until the live data or the recording ends). It replays that period
  * with the old behaviour (decision-time book, --latency-book same), and
  * with the websocket latency book at each latency in --sweep (the configured latency is always included),
and compares each against the live fills / settlements in the source DB: fill match %, live vs replay P&L, and the
price / size differences on matched fills. Both inputs are opened read-only; replay output goes to --workdir.
Nothing here changes the live bot or its latency setting.
"""
import argparse
import contextlib
import datetime as dt
import io
import json
import math
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
os.chdir(HERE)

import numpy as np
import pandas as pd

import config
from core.replay import Source, run_replay
from tools.replay import self_check


def window(src_path: str, ws_path: str, round_s: int = 900):
    ws = sqlite3.connect(f"file:{ws_path}?mode=ro", uri=True)
    a, b = ws.execute("SELECT MIN(recv_ts), MAX(recv_ts) FROM ws_book").fetchone()
    ws.close()
    if a is None:
        raise SystemExit("ws_book is empty: nothing to calibrate against")
    _, hi = Source(src_path).span()
    t0 = math.ceil(a / round_s) * round_s
    t1 = min(b, hi) - round_s                      # leave the last round to settle
    return t0, t1, a, b


def one(src, ws_path, out, t0, t1, names, mode, latency_ms):
    kw = dict(latency_book=mode, latency_ms=latency_ms, only=names, warm_start=True)
    if mode == "ws":
        kw["ws_book_path"] = ws_path
    eng = run_replay(config, src, out, t0, t1, **kw)
    used = sum(1 for r in eng.latency_log if r[2] == "ws") if mode == "ws" else None
    total = len(eng.latency_log) if mode == "ws" else None
    with contextlib.redirect_stdout(io.StringIO()):
        t = self_check(src, out, names or list(eng.brokers), t0, t1)
    return t, used, total


def totals(t: pd.DataFrame) -> dict:
    L, M = t["live fills"].sum(), t["matched"].sum()
    w = t["matched"].clip(lower=1)
    return {"live_fills": int(L), "replay_fills": int(t["replay fills"].sum()), "matched": int(M),
            "match_pct": round(100 * M / max(L, 1), 1),
            "live_pnl": round(t["live pnl $"].sum(), 2), "replay_pnl": round(t["replay pnl $"].sum(), 2),
            "abs_pnl_gap": round(abs(t["replay pnl $"].sum() - t["live pnl $"].sum()), 2),
            "sum_abs_strategy_gap": round(t["d pnl $"].abs().sum(), 2),
            "mean_abs_dprice": round(float(np.average(t["mean |d price|"].fillna(0), weights=w)), 4),
            "mean_abs_dshares": round(float(np.average(t["mean |d shares|"].fillna(0), weights=w)), 3)}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", default=config.DB_PATH)
    ap.add_argument("--ws-db", default="data/ws_events.db")
    ap.add_argument("--sweep", default="100,250,400,600", help="latencies in ms for the websocket latency book")
    ap.add_argument("--strategy", action="append")
    ap.add_argument("--workdir", default="data/calibration")
    ap.add_argument("--json", help="write the full result here")
    args = ap.parse_args(argv)
    os.makedirs(args.workdir, exist_ok=True)
    names = [n for x in (args.strategy or []) for n in x.split(",")] or None
    t0, t1, a, b = window(args.src, args.ws_db)
    fmt = lambda t: dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%d %H:%M")
    hours = (t1 - t0) / 3600
    print(f"websocket book recording {fmt(a)} -> {fmt(b)} UTC; calibration window {fmt(t0)} -> {fmt(t1)} UTC "
          f"({hours:.1f} h)")
    if t1 <= t0:
        raise SystemExit("window too short to calibrate")
    cfg_ms = config.LATENCY_SECONDS * 1000.0
    sweep = sorted({float(x) for x in args.sweep.split(",") if x} | {cfg_ms})
    print(f"configured live latency: {cfg_ms:g} ms (config.yaml fills.latency_ms; not changed)")

    runs = {}
    runs["same"] = one(args.src, args.ws_db, os.path.join(args.workdir, "same.db"), t0, t1, names, "same", None)
    print("  ran: decision-time book (old behaviour)", flush=True)
    for ms in sweep:
        runs[f"ws@{ms:g}"] = one(args.src, args.ws_db, os.path.join(args.workdir, f"ws_{ms:g}.db"), t0, t1, names,
                                 "ws", ms)
        print(f"  ran: websocket latency book @ {ms:g} ms", flush=True)

    rows = []
    for k, (t, used, total) in runs.items():
        r = totals(t)
        r.update(mode=k, ws_books_used=used, latency_fetches=total)
        rows.append(r)
    summ = pd.DataFrame(rows).set_index("mode")
    print("\n== TOTALS vs the live bot (same period) ==")
    print(summ[["live_fills", "replay_fills", "matched", "match_pct", "live_pnl", "replay_pnl", "abs_pnl_gap",
                "sum_abs_strategy_gap", "mean_abs_dprice", "mean_abs_dshares", "ws_books_used",
                "latency_fetches"]].to_string())

    cfg_key = f"ws@{cfg_ms:g}"
    base, cal = runs["same"][0].set_index("strategy"), runs[cfg_key][0].set_index("strategy")
    per = pd.DataFrame({
        "live fills": base["live fills"], "match% same": base["match %"], f"match% ws@{cfg_ms:g}": cal["match %"],
        "live pnl": base["live pnl $"], "replay pnl same": base["replay pnl $"], f"replay pnl ws@{cfg_ms:g}": cal["replay pnl $"],
        "|gap| same": base["d pnl $"].abs(), f"|gap| ws@{cfg_ms:g}": cal["d pnl $"].abs(),
        "d price same": base["mean |d price|"], f"d price ws@{cfg_ms:g}": cal["mean |d price|"],
        "d shares same": base["mean |d shares|"], f"d shares ws@{cfg_ms:g}": cal["mean |d shares|"]})
    print("\n== PER STRATEGY: old decision-time book vs websocket latency book at the configured latency ==")
    print(per.round(2).to_string())

    # best-fit latency: lowest summed |P&L gap| per strategy, ties broken by match %
    best = summ.loc[[f"ws@{ms:g}" for ms in sweep]]
    by_match = best["match_pct"].idxmax()
    by_gap = best["sum_abs_strategy_gap"].idxmin()
    by_price = best["mean_abs_dprice"].idxmin()
    print(f"\nbest fit: highest fill match -> {by_match}; smallest summed |P&L gap| -> {by_gap}; "
          f"smallest mean |price diff| -> {by_price}  (configured: {cfg_ms:g} ms)")
    spread = best["match_pct"].max() - best["match_pct"].min()
    n_live = int(summ.loc["same", "live_fills"])
    print(f"sample: {n_live} live fills over {hours:.1f} h; match% varies only {spread:.1f} points across the sweep, "
          f"so a latency difference smaller than that is noise." if n_live else "no live fills in the window")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump({"window": [t0, t1], "configured_ms": cfg_ms, "totals": summ.reset_index().to_dict("records"),
                       "per_strategy": per.reset_index().to_dict("records")}, fh, indent=2, default=float)
    return summ, per


if __name__ == "__main__":
    main()
