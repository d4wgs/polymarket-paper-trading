"""
ONE-TIME conversion of an existing ws_events.db to auto_vacuum=INCREMENTAL (new DBs already get it).

    python tools/db_convert_autovac.py --db data/ws_events.db      # STOP the recorder first

Runs `PRAGMA auto_vacuum=2; VACUUM`, which rewrites the whole file: it needs free disk about equal to the DB size
and blocks writers for the duration (minutes on a Pi for a multi-GB file). Refuses to run if disk space is short.
After this, the recorder's trims shrink the file in small non-blocking chunks.
"""
import argparse
import os
import shutil
import sqlite3
import time


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", required=True)
    args = ap.parse_args()
    size = os.path.getsize(args.db)
    free = shutil.disk_usage(os.path.dirname(os.path.abspath(args.db))).free
    if free < size * 1.2:
        raise SystemExit(f"Not enough free disk: need ~{size * 1.2 / 1e9:.1f} GB, have {free / 1e9:.1f} GB")
    db = sqlite3.connect(args.db, timeout=60)
    cur = db.execute("PRAGMA auto_vacuum").fetchone()[0]
    print(f"auto_vacuum now {cur}; file {size / 1e6:.0f} MB")
    if cur == 2:
        print("Already incremental; nothing to do.")
        return
    t = time.time()
    db.execute("PRAGMA auto_vacuum=2")
    db.execute("VACUUM")
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    print(f"done in {time.time() - t:.0f}s; auto_vacuum={db.execute('PRAGMA auto_vacuum').fetchone()[0]}; "
          f"file {os.path.getsize(args.db) / 1e6:.0f} MB")
    db.close()


if __name__ == "__main__":
    main()
