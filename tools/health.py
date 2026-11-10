"""
Health at a glance (read-only): clock drift, last alert, bot starts and downtime, recorder, invalid rounds.

    python tools/health.py                      # data/paper_us.db (+ data/ws_events.db when it exists)
    python tools/health.py --db X.db --ws-db Y.db

CLOCK    the latest reading (this clock vs the exchange, see core/health.py), the last 24 hours and the last alert
BOT      every recorded start, how long it ran and how long the bot was down before it
ROUNDS   rounds of the last 24 hours marked invalid for statistics, by reason
RECORDER the websocket recorder's starts, heartbeat and disconnects (from its own database)
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
from core import health


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=config.DB_PATH)
    ap.add_argument("--ws-db", default=(config.RECORDER or {}).get("db_path", "data/ws_events.db"))
    args = ap.parse_args(argv)
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    ws = None
    if args.ws_db and os.path.exists(args.ws_db):
        ws = sqlite3.connect(f"file:{args.ws_db}?mode=ro", uri=True)
    try:
        lines = health.report(con, time.time(), ws)
    finally:
        con.close()
        if ws is not None:
            ws.close()
    print(f"Health: {args.db}")
    print("\n".join(lines))
    return lines


if __name__ == "__main__":
    main()
