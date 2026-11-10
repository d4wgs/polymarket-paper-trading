"""
Fill quality per strategy, from the order log (read-only; core/orderlog.py explains every column and reason code).

    python tools/fill_report.py                         # data/paper_us.db, everything recorded
    python tools/fill_report.py --days 1                # the last 24 hours
    python tools/fill_report.py --db data/replay.db --strategy s3_sniper_v2
    python tools/fill_report.py --include-invalid       # also count rounds marked invalid (data gaps, clock drift)

Columns: orders (rows) and tries (rows + counted repeats of the same failure), fill% (filled fully or partly),
full%, ratio (filled / intended on the orders that filled), slip c (cents per share worse than the price at
decision time; negative = better), wslip c (weighted by shares), depth/size (median displayed depth up to the
limit / intended size), and why orders did not fill.
"""
import argparse
import os
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
os.chdir(HERE)

import config
from core import orderlog


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=config.DB_PATH)
    ap.add_argument("--days", type=float, help="only orders from the last N days")
    ap.add_argument("--strategy", action="append", help="strategy name(s), comma separated or repeated")
    ap.add_argument("--include-invalid", action="store_true",
                    help="also count rounds marked invalid (default: excluded, like every other statistic)")
    args = ap.parse_args(argv)
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    try:
        have = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "orders" not in have:
            print(f"{args.db} has no order log yet (it is written by code deployed from 2026-11-09 on).")
            return []
        since = time.time() - args.days * 86400 if args.days else 0.0
        names = [n for x in (args.strategy or []) for n in x.split(",") if n] or None
        skip, note = set(), ""
        if "round_validity" in have:
            bad = con.execute("SELECT slug, reason FROM round_validity WHERE valid = 0").fetchall()
            if args.include_invalid:
                note = f"including {len(bad)} round(s) marked invalid"
            else:
                skip = {b[0] for b in bad}
                why = {}
                for _, reason in bad:
                    for part in (reason or "unknown").split(","):
                        why[part] = why.get(part, 0) + 1
                note = (f"excluded {len(bad)} invalid round(s)" + (f" ({', '.join(f'{k} {v}' for k, v in sorted(why.items()))})"
                                                                     if why else "") + "; --include-invalid to count them")
        rows = orderlog.summary(con, since, names, skip)
    finally:
        con.close()
    print(f"Order log: {args.db}" + (f", last {args.days:g} day(s)" if args.days else "") + (f"; {note}" if note else ""))
    print(orderlog.render_summary(rows) if rows else "(no orders recorded)")
    return rows


if __name__ == "__main__":
    main()
