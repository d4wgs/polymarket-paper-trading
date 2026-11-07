"""
Independent check of the numbers the dashboard shows, recomputed from the raw database rows
(it does NOT use the broker or metrics code, so a bug there would show up as a mismatch).

    python tools/verify_db.py                 # data/paper_us.db
    python tools/verify_db.py data/sim_us.db

For each strategy it rebuilds cash from fills + settlements, values open positions at the latest
mid, and compares that with the bot's latest equity row. It also checks the stored data itself:
settlement arithmetic, duplicate rows, gaps in price recording and stuck/unsettled rounds.
"""
import os
import sqlite3
import sys
import time
from collections import defaultdict

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "data", "paper_us.db")
con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
con.row_factory = sqlite3.Row
now = time.time()
problems = []


def q(sql, *a):
    return con.execute(sql, a).fetchall()


print(f"Database: {path}")
for t in ("strategies", "fills", "settlements", "equity", "snapshots", "books", "resolutions", "windows"):
    r = q(f"SELECT COUNT(*) n FROM {t}")[0]
    print(f"  {t:12s} {r['n']:>10,}")

# ---------------------------------------------------------------- per-strategy reconciliation
latest_mid = {}
for r in q("SELECT s.slug, s.up_bid, s.up_ask FROM snapshots s JOIN "
           "(SELECT slug, MAX(ts) ts FROM snapshots GROUP BY slug) m ON s.slug=m.slug AND s.ts=m.ts"):
    vals = [v for v in (r["up_bid"], r["up_ask"]) if v is not None]
    latest_mid[r["slug"]] = sum(vals) / len(vals) if vals else None

print("\nPer strategy (recomputed from raw rows vs the bot's latest equity row):")
for s in q("SELECT name, bankroll FROM strategies"):
    name, cash = s["name"], s["bankroll"]
    fills = q("SELECT * FROM fills WHERE strategy=? ORDER BY ts", name)
    sett = {r["slug"]: r for r in q("SELECT * FROM settlements WHERE strategy=?", name)}
    pos = defaultdict(lambda: {"UP": 0.0, "DOWN": 0.0, "net": 0.0})
    for f in fills:
        p = pos[f["slug"]]
        if f["side"] == "BUY":
            cash -= f["usd"] + f["fee"]
            p[f["outcome"]] += f["shares"]
            p["net"] += f["usd"] + f["fee"]
        else:
            cash += f["usd"] - f["fee"]
            p[f["outcome"]] -= f["shares"]
            p["net"] -= f["usd"] - f["fee"]
        cash += f["rebate"] or 0.0
    realized = 0.0
    for slug, r in sett.items():
        cash += r["payout"]
        realized += r["pnl"]
        p = pos.get(slug)
        if p is None:
            problems.append(f"{name}: settlement for {slug} has no fills")
            continue
        exp_pay = p["UP"] if r["winner"] == "UP" else p["DOWN"]
        if abs(p["UP"] - r["up_shares"]) > 1e-6 or abs(p["DOWN"] - r["down_shares"]) > 1e-6:
            problems.append(f"{name}: {slug} settlement shares {r['up_shares']}/{r['down_shares']} "
                            f"!= fills {p['UP']:.4f}/{p['DOWN']:.4f}")
        if abs(exp_pay - r["payout"]) > 1e-6 or abs(r["payout"] - r["net_cost"] - r["pnl"]) > 1e-6 \
                or abs(p["net"] - r["net_cost"]) > 1e-6:
            problems.append(f"{name}: {slug} settlement arithmetic is off ({dict(r)})")
    open_val = 0.0
    for slug, p in pos.items():
        if slug in sett:
            continue
        m = latest_mid.get(slug)
        if m is None:
            open_val += 0.0 if (p["UP"] < 1e-9 and p["DOWN"] < 1e-9) else float("nan")
        else:
            open_val += p["UP"] * m + p["DOWN"] * (1 - m)
    eq = q("SELECT * FROM equity WHERE strategy=? ORDER BY ts DESC LIMIT 1", name)
    bot_eq = eq[0]["equity"] if eq else float("nan")
    bot_cash = eq[0]["cash"] if eq else float("nan")
    mine = cash + open_val
    flag = "" if abs(bot_cash - cash) < 0.01 else "  <-- CASH MISMATCH"
    print(f"  {name:20s} cash {cash:10.2f} (bot {bot_cash:10.2f}) open {open_val:8.2f} "
          f"equity {mine:10.2f} (bot {bot_eq:10.2f}) realized {realized:+9.2f} "
          f"fills {len(fills)} settled {len(sett)}{flag}")
    if flag:
        problems.append(f"{name}: cash rebuilt from fills ({cash:.2f}) != bot's cash ({bot_cash:.2f})")

# ---------------------------------------------------------------- data integrity
dups = q("SELECT strategy, slug, COUNT(*) n FROM settlements GROUP BY strategy, slug HAVING n > 1")
for r in dups:
    problems.append(f"duplicate settlement rows: {r['strategy']} {r['slug']} x{r['n']}")
dupf = q("SELECT ts, strategy, COUNT(*) n FROM fills GROUP BY ts, strategy, slug, outcome, side, shares HAVING n > 1")
if dupf:
    problems.append(f"{len(dupf)} duplicated fill rows")
unsettled = q("SELECT DISTINCT f.strategy, f.slug, f.window_end FROM fills f LEFT JOIN settlements s "
              "ON s.strategy=f.strategy AND s.slug=f.slug WHERE s.slug IS NULL AND f.window_end < ?", now - 900)
for r in unsettled:
    problems.append(f"{r['strategy']}: {r['slug']} ended {(now - r['window_end']) / 60:.0f} min ago and is not settled")
bad_eq = q("SELECT COUNT(*) n FROM equity WHERE ABS(cash + open_value - equity) > 1e-6")[0]["n"]
if bad_eq:
    problems.append(f"{bad_eq} equity rows where cash + open_value != equity")
future = q("SELECT COUNT(*) n FROM equity WHERE ts > ?", now + 60)[0]["n"]
if future:
    problems.append(f"{future} equity rows are timestamped in the future (clock problem)")

# gaps in price recording over the last 24h (per market): > 30s without a snapshot
print("\nPrice recording over the last 24h:")
for a in q("SELECT DISTINCT asset FROM snapshots"):
    ts = [r[0] for r in con.execute("SELECT ts FROM snapshots WHERE asset=? AND ts > ? ORDER BY ts",
                                    (a[0], now - 86400))]
    gaps = [(t0, t1) for t0, t1 in zip(ts, ts[1:]) if t1 - t0 > 30]
    lost = sum(t1 - t0 for t0, t1 in gaps)
    span = (ts[-1] - ts[0]) if len(ts) > 1 else 0
    print(f"  {a[0]:8s} {len(ts):>7,} snapshots, {len(gaps)} gaps > 30s ({lost / 60:.1f} min of {span / 60:.0f} min)")
    for t0, t1 in gaps[-5:]:
        print(f"           gap {time.strftime('%m-%d %H:%M:%S', time.localtime(t0))} -> "
              f"{time.strftime('%H:%M:%S', time.localtime(t1))} ({t1 - t0:.0f}s)")
last = q("SELECT MAX(ts) t FROM snapshots")[0]["t"]
if last is not None:
    print(f"  latest snapshot {now - last:.0f}s ago")

print("\n" + ("No problems found." if not problems else f"{len(problems)} problem(s):"))
for p in problems:
    print("  - " + p)
