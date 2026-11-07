"""
Export trades to CSV on demand. (The bot also writes data/exports/trades_YYYY-MM-DD.csv by itself
at each new ET day.)

    python tools/export_trades.py                    # yesterday (ET) from the live database
    python tools/export_trades.py --date 2026-11-07
    python tools/export_trades.py --all --db data/sim_us.db
    python tools/export_trades.py --scorecard        # also write the per-strategy scorecard
"""
import argparse
import datetime as dt
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
os.chdir(HERE)

import config
from core import analytics
from core.fees import ET
from core.storage import Storage


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=config.DB_PATH)
    ap.add_argument("--date", help="ET date YYYY-MM-DD (default: yesterday)")
    ap.add_argument("--all", action="store_true", help="every trade in the database")
    ap.add_argument("--out", default=config.EXPORT_DIR or "data/exports")
    ap.add_argument("--scorecard", action="store_true")
    a = ap.parse_args()

    store = Storage(a.db)
    if a.all:
        path = os.path.join(a.out, "trades_all.csv")
        n = store.export_fills_csv(path, 0, 1e12)
    else:
        day = dt.date.fromisoformat(a.date) if a.date else dt.datetime.now(ET).date() - dt.timedelta(days=1)
        t0 = dt.datetime.combine(day, dt.time(), ET).timestamp()
        path = os.path.join(a.out, f"trades_{day.isoformat()}.csv")
        t1 = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(), ET).timestamp()   # DST-safe day end
        n = store.export_fills_csv(path, t0, t1)
    print(f"{n} trades -> {path}")
    if a.scorecard:
        lab = analytics.load_lab(a.db)
        names = sorted(lab["fills"]["strategy"].unique()) if len(lab["fills"]) else []
        sc = analytics.scorecard(lab, names)
        p2 = os.path.join(a.out, "scorecard.csv")
        sc.to_csv(p2, index=False)
        print(f"scorecard ({len(sc)} strategies) -> {p2}")


if __name__ == "__main__":
    main()
