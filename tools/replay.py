"""
Replay recorded market data through the REAL engine, strategies and paper broker.

    python tools/replay.py --src data/paper_us.db --days 1                       # last day of the recording
    python tools/replay.py --src X.db --from 2026-11-08 --to 2026-11-09          # a date range (UTC)
    python tools/replay.py --src X.db --days 2 --holdout 1                       # in-sample vs newest 1 day
    python tools/replay.py --src X.db --days 1 --strategy s3_sniper_v2 --self-check
    python tools/replay.py --src X.db --days 1 --ws-trades data/ws_events.db     # maker fills from ws trade prints

* The source database is opened READ-ONLY; results go to --out (default data/replay.db, recreated each run).
  It refuses to write to the source.
* Statistics come from core/metrics.py (the same code as the dashboard's leaderboard).
* --self-check compares the replayed fills / P&L with what the live bot actually did in the same period.
"""
import argparse
import datetime as dt
import logging
import math
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
os.chdir(HERE)

import numpy as np
import pandas as pd

import config
from core import metrics
from core.replay import Source, run_replay


def parse_t(s: str) -> float:
    try:
        return float(s)
    except ValueError:
        d = dt.datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        return d.timestamp()


def slug_start(slug: str) -> int:
    try:
        return int(str(slug).rsplit("-", 1)[1])
    except (IndexError, ValueError):
        return 0


def split_data(d: dict, t_lo: float, t_hi: float) -> dict:
    """The same metrics input, restricted to rounds that STARTED in [t_lo, t_hi)."""
    out = dict(d)
    for t in ("fills", "settlements"):
        df = d[t]
        out[t] = df[df["slug"].map(slug_start).between(t_lo, t_hi, inclusive="left")] if len(df) else df
    out["equity"] = d["equity"].iloc[0:0]          # leaderboard rebuilds portfolio value from the ledger
    return out


def report(d: dict, title: str) -> pd.DataFrame:
    lb = metrics.leaderboard(d)
    if not len(lb):
        print(f"\n== {title}: no trades ==")
        return lb
    nf = d["fills"].groupby("strategy").size() if len(d["fills"]) else pd.Series(dtype=int)
    t = pd.DataFrame({
        "strategy": lb["Strategy"],
        "fills": lb["Strategy"].map(nf).fillna(0).astype(int),
        "rounds": lb["Windows"],
        "pnl $": lb["Realized P&L $"].round(2),
        "win %": lb["Win rate %"].round(1),
        "avg entry": lb["Avg entry price"].round(3),
        "fees $": lb["Fees $"].round(2),
        "t-stat": lb["t-stat"].round(2),
    })
    t = t[t["fills"] > 0].sort_values("pnl $", ascending=False)
    print(f"\n== {title} ==")
    print(t.to_string(index=False) if len(t) else "(no trades)")
    return lb


def self_check(src_path: str, out_path: str, names: list[str], t0: float, t1: float, tol: float = 3.0):
    """Compare replayed fills with the live fills table for rounds that started inside [t0, t1)."""
    import sqlite3
    live_con = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
    rep_con = sqlite3.connect(f"file:{out_path}?mode=ro", uri=True)
    stopped = {r[0] for r in live_con.execute("SELECT name FROM strategy_status WHERE stopped_at < ?", (t0,))}
    if stopped & set(names):
        print("\nself-check skips strategies the LIVE bot had already stopped (portfolio <= stop level) before the "
              f"period, because they could not trade live: {sorted(stopped & set(names))}")
        names = [n for n in names if n not in stopped]
    q = "SELECT * FROM fills WHERE strategy IN ({}) AND ts >= ? AND ts < ?".format(",".join("?" * len(names)))
    live = pd.read_sql(q, live_con, params=(*names, t0, t1))
    rep = pd.read_sql(q, rep_con, params=(*names, t0, t1))
    live = live[live["slug"].map(slug_start) >= t0]            # ignore positions opened before the replay started
    rep = rep[rep["slug"].map(slug_start) >= t0]
    ls = pd.read_sql("SELECT * FROM settlements WHERE strategy IN ({})".format(",".join("?" * len(names))),
                     live_con, params=names)
    rs = pd.read_sql("SELECT * FROM settlements WHERE strategy IN ({})".format(",".join("?" * len(names))),
                     rep_con, params=names)
    ls = ls[ls["slug"].map(slug_start).between(t0, t1, inclusive="left")]
    rs = rs[rs["slug"].map(slug_start).between(t0, t1, inclusive="left")]
    rows, shown = [], 0
    for name in names:
        L = live[live.strategy == name].sort_values("ts").to_dict("records")
        R = rep[rep.strategy == name].sort_values("ts").to_dict("records")
        used, matched, dp, dsh = set(), 0, [], []
        for r in R:
            best = None
            for i, l in enumerate(L):
                if i in used or l["slug"] != r["slug"] or l["outcome"] != r["outcome"] or l["side"] != r["side"]:
                    continue
                if abs(l["ts"] - r["ts"]) <= tol and (best is None or abs(l["ts"] - r["ts"]) < abs(L[best]["ts"] - r["ts"])):
                    best = i
            if best is not None:
                used.add(best)
                matched += 1
                dp.append(r["avg_price"] - L[best]["avg_price"])
                dsh.append(r["shares"] - L[best]["shares"])
        lp, rp = ls[ls.strategy == name]["pnl"].sum(), rs[rs.strategy == name]["pnl"].sum()
        rows.append({"strategy": name, "live fills": len(L), "replay fills": len(R), "matched": matched,
                     "live only": len(L) - matched, "replay only": len(R) - matched,
                     "match %": round(100 * matched / max(len(L), 1), 1),
                     "mean |d price|": round(float(np.mean(np.abs(dp))), 4) if dp else float("nan"),
                     "mean |d shares|": round(float(np.mean(np.abs(dsh))), 3) if dsh else float("nan"),
                     "live pnl $": round(lp, 2), "replay pnl $": round(rp, 2), "d pnl $": round(rp - lp, 2)})
    t = pd.DataFrame(rows)
    print("\n== SELF-CHECK: replay vs the live bot's own fills (same period) ==")
    print(t.to_string(index=False))
    tot_l, tot_r, tot_m = t["live fills"].sum(), t["replay fills"].sum(), t["matched"].sum()
    print(f"TOTAL live fills {tot_l}, replay fills {tot_r}, matched {tot_m} ({100 * tot_m / max(tot_l, 1):.1f}% of live); "
          f"live pnl ${t['live pnl $'].sum():.2f} vs replay ${t['replay pnl $'].sum():.2f}")
    return t


def model_check(src_path: str, out_path: str, t0: float, t1: float):
    """Replayed fair value vs the recorded one on the same ticks (what feeds every strategy decision)."""
    import sqlite3
    a = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
    b = sqlite3.connect(f"file:{out_path}?mode=ro", uri=True)
    L = pd.read_sql("SELECT ts, asset, fv_up, spot, sigma, k, basis FROM snapshots WHERE ts >= ? AND ts < ?", a, params=(t0, t1))
    R = pd.read_sql("SELECT ts, asset, fv_up, spot, sigma, k, basis FROM snapshots", b)
    m = L.merge(R, on=["ts", "asset"], suffixes=("_live", "_rep")).dropna(subset=["fv_up_live", "fv_up_rep"])
    d = (m["fv_up_rep"] - m["fv_up_live"]).abs()
    print("\n== MODEL CHECK: replayed vs recorded fair value (P(UP)) on the same ticks ==")
    print(f"ticks compared {len(m)} | |d fv|: mean {d.mean():.4f}, median {d.median():.4f}, p95 {d.quantile(.95):.4f}, "
          f"max {d.max():.4f} | share within 0.01: {(d < 0.01).mean() * 100:.1f}%")
    for c in ("spot", "sigma", "k", "basis"):
        x = (m[c + "_rep"] - m[c + "_live"]).abs()
        print(f"  |d {c}|: mean {x.mean():.3g}, max {x.max():.3g}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", default=config.DB_PATH, help="recorded database (opened read-only)")
    ap.add_argument("--out", default="data/replay.db")
    ap.add_argument("--from", dest="t_from", help="UTC date/time (e.g. 2026-11-08 or 2026-11-08T14:00) or unix seconds")
    ap.add_argument("--to", dest="t_to")
    ap.add_argument("--days", type=float, help="the last N days of the recording (instead of --from/--to)")
    ap.add_argument("--holdout", type=float, default=0, help="reserve the newest N days; report in-sample vs holdout")
    ap.add_argument("--strategy", action="append", help="strategy name(s) from config.yaml (default: all enabled)")
    ap.add_argument("--ws-trades", nargs="?", const="data/ws_events.db", default=None, metavar="WS_DB",
                    help="maker fills from recorded websocket trade prints (default rule stays book trade-through)")
    ap.add_argument("--sigma", choices=["recorded", "ewma"], default="recorded")
    ap.add_argument("--latency-book", choices=["next", "same"], default="same",
                    help="book used for the order latency fill: the next recorded one, or the decision book")
    ap.add_argument("--model-check", action="store_true",
                    help="also compare the replayed fair value with the recorded one, tick by tick")
    ap.add_argument("--self-check", action="store_true", help="compare with the live fills in the source DB")
    args = ap.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    src = Source(args.src)
    lo, hi = src.span()
    if args.days:
        t0, t1 = hi - args.days * 86400, hi + 1
    else:
        t0 = parse_t(args.t_from) if args.t_from else lo
        t1 = parse_t(args.t_to) if args.t_to else hi + 1
    t0 = max(t0, lo)
    names = [n for x in (args.strategy or []) for n in x.split(",")] or None
    print(f"source {args.src}: recorded {dt.datetime.fromtimestamp(lo, dt.timezone.utc):%Y-%m-%d %H:%M} -> "
          f"{dt.datetime.fromtimestamp(hi, dt.timezone.utc):%Y-%m-%d %H:%M} UTC")
    print(f"replaying {dt.datetime.fromtimestamp(t0, dt.timezone.utc):%Y-%m-%d %H:%M} -> "
          f"{dt.datetime.fromtimestamp(t1, dt.timezone.utc):%Y-%m-%d %H:%M} UTC  "
          f"(maker fills: {'ws trade prints' if args.ws_trades else 'book trade-through'}, sigma: {args.sigma}, "
          f"latency book: {args.latency_book})")

    def prog(n, t):
        print(f"  ... {n} ticks, at {dt.datetime.fromtimestamp(t, dt.timezone.utc):%m-%d %H:%M}", flush=True)
    engine = run_replay(config, args.src, args.out, t0, t1, only=names, ws_path=args.ws_trades,
                        sigma_mode=args.sigma, latency_book=args.latency_book, progress=prog,
                        keep_snapshots=args.model_check)
    print(f"replayed {engine.ticks_replayed} ticks -> {args.out}")
    open_left = [(n, len(b.positions)) for n, b in engine.brokers.items() if b.positions]
    if open_left:
        print("note: positions with no recorded result stayed open:", open_left)

    d = metrics.load(args.out)
    if args.holdout:
        cut = t1 - args.holdout * 86400
        report(split_data(d, t0, cut), f"IN-SAMPLE  (rounds before {dt.datetime.fromtimestamp(cut, dt.timezone.utc):%Y-%m-%d %H:%M} UTC)")
        report(split_data(d, cut, t1), "HOLDOUT    (newest %g day(s), not used for anything else)" % args.holdout)
    else:
        report(split_data(d, t0, t1), "REPLAY")
    if args.model_check:
        model_check(args.src, args.out, t0, t1)
    if args.self_check:
        self_check(args.src, args.out, [s for s in (names or [b for b in engine.brokers])], t0, t1)


if __name__ == "__main__":
    main()
