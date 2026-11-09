"""
Out-of-sample validation of the strategy variants, on replayed data.

    python tools/validate.py --src data/paper_us.db --holdout-days 2 --dev-days 5            # development only
    python tools/validate.py --src data/paper_us.db --holdout-days 2 --reveal-holdout        # ALSO the holdout (logged)
    python tools/validate.py --src X.db --holdout-frac 0.25 --strategies s3_sniper_v2 --markets 5m
    python tools/validate.py --replay-db data/replay.db --src X.db --holdout-days 1          # reuse an existing replay

* Time split only (never random): the newest --holdout-days (or newest --holdout-frac of rounds) is the holdout, the
  rest is development (optionally limited to --dev-days). The holdout is NOT computed or printed unless you pass
  --reveal-holdout, and every reveal is appended to data/holdout_reveals.log (when, strategies, parameter hash).
* Per strategy and split: trades, rounds, P&L, win rate, breakeven win rate, avg entry, per-trade expectancy with a
  bootstrap 95% CI that resamples ROUNDS (fixed --seed, --boot-n resamples), fees, t-stat.
* Prints the number of variants compared with a Bonferroni threshold; flags strategies whose replayed dollars are
  not trustworthy ("LOW CONFIDENCE ON DOLLARS"; settings in config.yaml `validate:`).
* Sources are opened read-only; replay output goes to --out (never to a live DB).
"""
import argparse
import datetime as dt
import json
import logging
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
os.chdir(HERE)

import pandas as pd

import config
from core import metrics, validation as V


def _csv_list(vals):
    return [n for x in (vals or []) for n in x.split(",") if n] or None


def _ws_used(db_path):
    """True when an existing replay output was produced with --latency-book ws (it has a latency_books table)."""
    import sqlite3
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        n = con.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='latency_books'").fetchone()[0]
        con.close()
        return bool(n)
    except sqlite3.Error:
        return False


def strategy_params(names):
    """The config.yaml blocks of the strategies being validated (what a tuning change would alter)."""
    out = {}
    for key, block in (config.RAW.get("strategies") or {}).items():
        block = block or {}
        if names is None or (block.get("name") or key) in names:
            out[block.get("name") or key] = block
    return out


def main(argv=None):
    cfgv = config.RAW.get("validate", {}) or {}
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", default=config.DB_PATH, help="recorded database (read-only)")
    ap.add_argument("--replay-db", help="analyse an existing replay output instead of replaying again")
    ap.add_argument("--out", default="data/validate_replay.db", help="where the replay result is written")
    ap.add_argument("--ws-db", nargs="?", const="data/ws_events.db", default=None, help="maker fills from ws trade prints")
    ap.add_argument("--latency-book", choices=["same", "ws"], default="same",
                    help="'ws': fill against order-latency books rebuilt from --ws-db (only valid where the recorder "
                         "has data; falls back to the decision book at gaps). Default: decision-time book.")
    ap.add_argument("--days", type=float, help="replay only the last N days of the recording")
    ap.add_argument("--holdout-days", type=float, default=0)
    ap.add_argument("--holdout-frac", type=float, default=0, help="alternative: newest X (0-1) share of rounds")
    ap.add_argument("--dev-days", type=float, default=0, help="limit development to the N days before the holdout")
    ap.add_argument("--strategies", action="append")
    ap.add_argument("--markets", help="5m, 15m or 5m,15m")
    ap.add_argument("--boot-n", type=int, default=int(cfgv.get("bootstrap_n", 2000)))
    ap.add_argument("--seed", type=int, default=int(cfgv.get("seed", 12345)))
    ap.add_argument("--reveal-holdout", action="store_true", help="print holdout results (logged; every peek counts)")
    ap.add_argument("--reveal-log", default=cfgv.get("reveal_log", "data/holdout_reveals.log"))
    ap.add_argument("--export-json")
    ap.add_argument("--export-csv")
    ap.add_argument("--summary-json", help="small dashboard-friendly summary (no per-round data)")
    ap.add_argument("--no-depth", action="store_true", help="skip the book-depth part of the optimism flag")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    names = _csv_list(args.strategies)
    markets = [m.strip() for m in args.markets.split(",")] if args.markets else None
    if args.reveal_holdout and not (args.holdout_days or args.holdout_frac):
        raise SystemExit("--reveal-holdout needs --holdout-days or --holdout-frac")

    if args.replay_db:
        out_db = args.replay_db
    else:
        from core.replay import Source, run_replay
        src = Source(args.src)
        lo, hi = src.span()
        span_days = args.days or ((args.holdout_days + args.dev_days) if (args.holdout_days and args.dev_days) else 0)
        t0, t1 = (max(lo, hi - span_days * 86400), hi + 1) if span_days else (lo, hi + 1)
        print(f"replaying {dt.datetime.fromtimestamp(t0, dt.timezone.utc):%Y-%m-%d %H:%M} -> "
              f"{dt.datetime.fromtimestamp(t1, dt.timezone.utc):%Y-%m-%d %H:%M} UTC (source read-only) ...", flush=True)
        ws_book = (args.ws_db or "data/ws_events.db") if args.latency_book == "ws" else None
        eng = run_replay(config, args.src, args.out, t0, t1, only=names, ws_path=args.ws_db, warm_start=True,
                         latency_book=args.latency_book, ws_book_path=ws_book)
        if ws_book:
            used = sum(1 for r in eng.latency_log if r[2] == "ws")
            ws_share = used / max(len(eng.latency_log), 1)
            print(f"latency fetches: {used} websocket books, {len(eng.latency_log) - used} fell back to the decision book")
        out_db = args.out

    d = metrics.load(out_db)
    ws_mode = bool(args.latency_book == "ws" and not args.replay_db) or _ws_used(out_db)
    depth = None if args.no_depth else V.make_depth_lookup(args.src)
    rep = V.build_report(
        d["fills"], d["settlements"], strategies=names, markets=markets, holdout_days=args.holdout_days,
        dev_days=args.dev_days, holdout_frac=args.holdout_frac, reveal_holdout=args.reveal_holdout,
        n_boot=args.boot_n, seed=args.seed, depth_lookup=depth,
        price_floor=float(cfgv.get("low_price_floor", 0.20)), price_share=float(cfgv.get("low_price_share", 0.25)),
        depth_ratio=float(cfgv.get("depth_ratio", 1.0)), depth_share=float(cfgv.get("depth_share", 0.25)),
        ws_latency_book=ws_mode)

    revealed = 0
    if rep["holdout_revealed"]:
        phash = V.params_hash({"params": strategy_params(names), "markets": markets, "holdout_days": args.holdout_days,
                               "holdout_frac": args.holdout_frac, "dev_days": args.dev_days})
        revealed = V.log_reveal(args.reveal_log, list(rep["strategies"]), phash)
    else:
        revealed = V.reveal_count(args.reveal_log)
    print()
    print(V.render_header(rep, revealed))
    if rep["has_holdout"]:
        cut = dt.datetime.fromtimestamp(rep["cut"], dt.timezone.utc)
        print(f"\nsplit: development = rounds before {cut:%Y-%m-%d %H:%M} UTC; holdout = rounds from then on")
    print("\n== DEVELOPMENT ==")
    print(V.render(rep, "dev"))
    if rep["holdout_revealed"]:
        print(f"\n== HOLDOUT (REVEALED; reveal #{revealed}) ==")
        print(V.render(rep, "holdout"))
    elif rep["has_holdout"]:
        print("\n== HOLDOUT == hidden. Pass --reveal-holdout to see it (each peek is logged).")
    flagged = [n for n, s in rep["strategies"].items() if s["dev"].get("flag", {}).get("flag")]
    if flagged:
        print("\nLOW CONFIDENCE ON DOLLARS: " + ", ".join(flagged) + "\n  " + V.OPTIMISM_CAVEAT)

    if args.export_json:
        with open(args.export_json, "w", encoding="utf-8") as fh:
            json.dump(rep, fh, indent=2, default=float)
    if args.export_csv:
        rows = []
        for n, parts in rep["strategies"].items():
            for split, s in parts.items():
                rows.append({"strategy": n, "split": split, **{k: v for k, v in s.items() if k != "flag"},
                             "low_confidence_dollars": s.get("flag", {}).get("flag"),
                             "low_price_share": s.get("flag", {}).get("low_price_share")})
        pd.DataFrame(rows).to_csv(args.export_csv, index=False)
    if args.summary_json:
        summ = {"generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                "variants": rep["variants"], "bonferroni_t": rep["multiple"]["t_threshold_bonferroni"],
                "holdout_reveals": revealed, "holdout_revealed_this_run": rep["holdout_revealed"],
                "strategies": {n: {sp: {k: s.get(k) for k in ("rounds", "trades", "pnl", "win_rate", "expectancy",
                                                                "ci_lo", "ci_hi", "t_stat")}
                                        | {"low_confidence_dollars": s.get("flag", {}).get("flag")}
                                   for sp, s in parts.items() if s.get("rounds")}
                               for n, parts in rep["strategies"].items()}}
        with open(args.summary_json, "w", encoding="utf-8") as fh:
            json.dump(summ, fh, indent=2, default=float)
    return rep


if __name__ == "__main__":
    main()
