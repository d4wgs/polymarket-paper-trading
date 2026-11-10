"""
Round validity: which rounds were recorded well enough to count in the statistics (core/validity.py).

    python tools/round_validity.py                       # summary of data/paper_us.db
    python tools/round_validity.py --list                # also list every invalid round
    python tools/round_validity.py --backfill            # add verdicts for finished rounds that have none
    python tools/round_validity.py --backfill --dry-run  # show what the backfill would add, write nothing

The rule uses data coverage only (gaps between recorded ticks, tick count, missing spot / fair value, bot restarts,
critical clock drift) and never looks at P&L. The bot writes a verdict when it settles a round; --backfill is for
rounds recorded before this existed. It only INSERTS into the round_validity table: existing verdicts, fills and
every other row are left exactly as they are.
"""
import argparse
import datetime as dt
import os
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
os.chdir(HERE)

import config
from core import validity


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=config.DB_PATH)
    ap.add_argument("--backfill", action="store_true", help="add verdicts for finished rounds without one")
    ap.add_argument("--dry-run", action="store_true", help="with --backfill: report only, write nothing")
    ap.add_argument("--list", action="store_true", help="list the invalid rounds")
    args = ap.parse_args(argv)
    now = time.time()
    if args.backfill:
        ro = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
        if args.dry_run:
            n = validity.backfill(ro, ro, config.POLL_SECONDS, config.VALIDITY, now, dry_run=True)
        else:
            from core.storage import Storage
            st = Storage(args.db)                      # creates the table on an older database (additive)
            n = validity.backfill(st.db, st.db, config.POLL_SECONDS, config.VALIDITY, now)
            st.db.close()
        ro.close()
        print(f"backfill{' (dry run)' if args.dry_run else ''}: {n['checked']} round(s) without a verdict checked, "
              f"{n['added']} added, {n['invalid']} invalid {n['reasons'] or ''}".rstrip())
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    try:
        c = validity.counts(con)
        print(f"{args.db}: rule {validity.rule_text(config.VALIDITY, config.POLL_SECONDS)}")
        print(f"  {c['rounds']} round(s) have a verdict; {c['invalid']} invalid"
              + (f" ({100 * c['invalid'] / c['rounds']:.1f}%)" if c["rounds"] else "")
              + (f": {', '.join(f'{k} {v}' for k, v in c['reasons'].items())}" if c["reasons"] else ""))
        if c["rounds"]:
            day = con.execute("SELECT COUNT(*), SUM(valid = 0) FROM round_validity WHERE window_end > ?",
                              (now - 86400,)).fetchone()
            print(f"  last 24h: {day[0]} round(s), {day[1] or 0} invalid")
        if args.list and c["invalid"]:
            print(f"  {'round':<32} {'ended (UTC)':<17} {'reason':<34} ticks  gap s")
            for r in con.execute("SELECT slug, window_end, reason, ticks, expected, max_gap FROM round_validity "
                                 "WHERE valid = 0 ORDER BY window_end"):
                print(f"  {r[0]:<32} {dt.datetime.fromtimestamp(r[1], dt.timezone.utc):%m-%d %H:%M}       "
                      f"{r[2]:<34} {r[3]:>3}/{r[4]:<4.0f} {r[5]:>5.0f}")
    except sqlite3.OperationalError:
        print(f"{args.db} has no round_validity table yet (run with --backfill, or start the bot once).")
    finally:
        con.close()


if __name__ == "__main__":
    main()
