"""
Retention / size-cap tool for the recorder DB (same logic the recorder runs every 10 minutes).

    python tools/ws_trim.py --db data/ws_events.db --dry-run      # read-only: what WOULD be deleted, projected size
    python tools/ws_trim.py --db /tmp/copy.db                    # really trim (use on a COPY to test)

Settings come from the `recorder:` block of config.yaml. Gap/connect/stop markers are never deleted.
"""
import argparse
import json
import logging
import os
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import config
from core.ws_trim import Policy, Trimmer, plan


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--now", type=float, default=None, help="unix time to treat as 'now' (default: newest row in the DB)")
    ap.add_argument("--max-db-mb", type=float, default=None)
    ap.add_argument("--vacuum-min-free-mb", type=float, default=None)
    ap.add_argument("--min-keep-hours", type=float, default=None, help="override (for tests on copies of young DBs)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = dict(config.RECORDER)
    if args.max_db_mb is not None:
        cfg["max_db_mb"] = args.max_db_mb
    if args.min_keep_hours is not None:
        cfg["min_keep_hours"] = args.min_keep_hours
    if args.vacuum_min_free_mb is not None:
        cfg["vacuum_min_free_mb"] = args.vacuum_min_free_mb
    pol = Policy.from_cfg(cfg)
    now = args.now
    if now is None:                       # a copy of an old DB: judge by its newest data, not today's clock
        c = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
        now = max(c.execute("SELECT COALESCE(MAX(recv_ts),0) FROM ws_book").fetchone()[0],
                  c.execute("SELECT COALESCE(MAX(recv_ts),0) FROM ws_trades").fetchone()[0]) or time.time()
        c.close()
    if args.dry_run:
        print(json.dumps(plan(args.db, pol, now), indent=2, default=str))
        return
    db = sqlite3.connect(args.db)
    db.execute("PRAGMA journal_mode=WAL")
    tr = Trimmer(db, args.db, pol)
    passes = 0
    while True:
        res = tr.run(now)
        passes += 1
        print(json.dumps(res, indent=2))
        if not tr.more:
            break
    markers = db.execute("SELECT kind, COUNT(*) FROM ws_gaps GROUP BY kind").fetchall()
    print(f"passes: {passes}; gap markers intact: {markers}")
    db.close()


if __name__ == "__main__":
    main()
