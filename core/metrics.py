"""
Portfolio analytics. Pure functions over the database -> pandas tables.
The dashboard calls these; you can also import them in a notebook.
"""
from __future__ import annotations

import math
import sqlite3
import threading

import numpy as np
import pandas as pd

TZ = "America/New_York"


MAX_DD_SQL = """
SELECT strategy, MIN(equity / peak - 1) AS max_dd FROM (
    SELECT strategy, equity,
           MAX(equity) OVER (PARTITION BY strategy ORDER BY ts ROWS UNBOUNDED PRECEDING) AS peak
    FROM equity) GROUP BY strategy"""


def max_drawdowns(db_path: str) -> dict[str, float]:
    """Worst peak-to-trough drop per strategy over its WHOLE history (as a fraction, e.g. -0.12)."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return {r[0]: r[1] for r in con.execute(MAX_DD_SQL)}
    finally:
        con.close()


def _to_time(ts: pd.Series) -> pd.Series:
    """Unix seconds -> Eastern Time timestamps (works on empty columns too)."""
    return pd.to_datetime(ts.astype("float64"), unit="s", utc=True).dt.tz_convert(TZ)


class DrawdownTracker:
    """
    Same result as max_drawdowns(), but each call only reads the equity rows added since the last
    call (equity rows are append-only, so their rowid only grows). The first call reads everything
    once; after that a refresh costs a few hundred rows instead of the whole history, which keeps
    a dashboard that runs for months on a Raspberry Pi fast.
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.last_rowid = 0
        self.peak: dict[str, float] = {}
        self.dd: dict[str, float] = {}
        self._lock = threading.Lock()

    def update(self) -> dict[str, float]:
        with self._lock:          # several dashboard tabs may refresh at the same moment
            return self._update()

    def _update(self) -> dict[str, float]:
        con = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        try:
            cur = con.execute("SELECT rowid, strategy, equity FROM equity WHERE rowid > ? ORDER BY rowid",
                              (self.last_rowid,))
            while True:
                rows = cur.fetchmany(50_000)
                if not rows:
                    break
                for rowid, strat, eq in rows:
                    if eq is None:
                        continue
                    peak = max(self.peak.get(strat, eq), eq)
                    self.peak[strat] = peak
                    if peak > 0:
                        self.dd[strat] = min(self.dd.get(strat, 0.0), eq / peak - 1)
                self.last_rowid = rows[-1][0]
        finally:
            con.close()
        return dict(self.dd)


def _tables(con) -> set[str]:
    return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def load(db_path: str, equity_window: float | None = None, max_dd: dict | None = None,
         since: float | None = None) -> dict[str, pd.DataFrame]:
    """
    Read the database into DataFrames.
    equity_window: only load the last N seconds of the equity curve (plus each strategy's latest
                   row), so a dashboard refreshing every few seconds stays fast as history grows.
                   Pass `max_dd` (from max_drawdowns) so the drawdown still covers all history.
    since:         also load equity and snapshots back to this unix time (the dashboard's chart start).
    """
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    have = _tables(con)
    out = {t: pd.read_sql(f"SELECT * FROM {t}", con) for t in ("strategies", "fills", "settlements")}
    if equity_window is None:
        out["equity"] = pd.read_sql("SELECT * FROM equity", con)
    else:
        out["equity"] = pd.read_sql(
            "SELECT * FROM equity WHERE ts >= MIN((SELECT MAX(ts) FROM equity) - ?, ?) "
            "UNION SELECT e.* FROM equity e JOIN (SELECT strategy, MAX(ts) AS ts FROM equity GROUP BY strategy) m "
            "ON e.strategy = m.strategy AND e.ts = m.ts", con,
            params=(equity_window, since if since is not None else float("inf")))
    out["snapshots"] = pd.read_sql(
        "SELECT * FROM snapshots WHERE ts > MIN((SELECT MAX(ts) FROM snapshots) - 3600, ?)", con,
        params=(since if since is not None else float("inf"),))
    out["status"] = (pd.read_sql("SELECT * FROM strategy_status", con) if "strategy_status" in have
                     else pd.DataFrame(columns=["name", "stopped_at", "equity", "reason"]))
    meta = dict(con.execute("SELECT key, value FROM meta").fetchall()) if "meta" in have else {}
    con.close()
    out["meta"] = meta
    out["exchange"] = meta.get("exchange") or ("polymarket_us" if "_us" in db_path else "polymarket_intl")
    out["max_dd"] = max_dd
    for t in ("fills", "settlements", "equity", "snapshots"):
        # always add the column, even to an empty table (a new database has no settlements yet)
        out[t]["time"] = _to_time(out[t]["ts"])
    return out


def closed_rounds(fills: pd.DataFrame, sett: pd.DataFrame) -> pd.DataFrame:
    """
    One row per finished trade = (strategy, round) whose P&L is final:
      * every settled round (P&L from the settlement row), and
      * rounds the strategy has fully EXITED but that haven't ended / settled yet (e.g. a scalp or S5's
        leg 2): P&L = sale proceeds - purchase cost, fees included -- exactly what the settlement row
        will say later, since nothing is left to pay out.
    Without the second kind, a closed trade's profit sat in cash (portfolio value) but stayed out of
    "Realized P&L" until the round ended and was looked up, up to 15 minutes later.
    Columns: strategy, slug, asset, ts (settlement or exit time), pnl, settled.
    """
    cols = ["strategy", "slug", "asset", "ts", "pnl", "settled"]
    parts = []
    if len(sett):
        parts.append(sett[["strategy", "slug", "asset", "ts", "pnl"]].assign(settled=True))
    if len(fills):
        done = set(zip(sett["strategy"], sett["slug"])) if len(sett) else set()
        f = fills[[(a, b) not in done for a, b in zip(fills["strategy"], fills["slug"])]]
        if len(f):
            f = f.assign(signed=np.where(f["side"] == "BUY", f["shares"], -f["shares"]),
                         cash=np.where(f["side"] == "BUY", -(f["usd"] + f["fee"]), f["usd"] - f["fee"]))
            left = f.groupby(["strategy", "slug", "outcome"])["signed"].sum().abs().groupby(level=[0, 1]).max()
            g = f.groupby(["strategy", "slug"]).agg(asset=("asset", "first"), ts=("ts", "max"), pnl=("cash", "sum"))
            g = g[left.reindex(g.index) <= 1e-6].reset_index()
            if len(g):
                parts.append(g.assign(settled=False)[cols])
    if not parts:
        return pd.DataFrame(columns=cols)
    return pd.concat(parts, ignore_index=True)


def ledger_value(name: str, bankroll: float, fills: pd.DataFrame, sett: pd.DataFrame,
                 marks: pd.DataFrame | None = None) -> float:
    """
    Portfolio value rebuilt from the trade records alone: bankroll - purchases + sales + rebates + payouts,
    plus open bets at their latest mid (from `marks`: open_positions_marked rows) or, with no recent
    price, at what they cost. Used when a strategy has no recent equity row (retired, or the bot is off).
    """
    f = fills[fills.strategy == name] if len(fills) else fills
    cash = bankroll
    if len(f):
        cash += np.where(f["side"] == "BUY", -(f["usd"] + f["fee"]), f["usd"] - f["fee"]).sum()
        cash += f["rebate"].fillna(0).sum() if "rebate" in f else 0.0
    if len(sett):
        cash += sett.loc[sett.strategy == name, "payout"].sum()
    if marks is not None and len(marks):
        m = marks[marks.strategy == name]
        cash += np.where(m["value"].notna(), m["value"], m["net_cost"]).sum()
    return float(cash)


def leaderboard(d: dict[str, pd.DataFrame]) -> pd.DataFrame:
    strat, fills, sett, eq = d["strategies"], d["fills"], d["settlements"], d["equity"]
    status = d.get("status")
    stopped = set(status["name"]) if status is not None and len(status) else set()
    us = d.get("exchange") == "polymarket_us"
    closed = closed_rounds(fills, sett)
    latest_eq = eq["ts"].max() if len(eq) else None
    marks = None                       # open bets at the latest mid, only needed for stale strategies
    rows = []
    for _, s in strat.iterrows():
        name, bankroll = s["name"], s["bankroll"]
        f = fills[fills.strategy == name] if len(fills) else fills
        st = closed[closed.strategy == name] if len(closed) else closed      # finished trades
        e = eq[eq.strategy == name].sort_values("ts") if len(eq) else eq

        pnl = st["pnl"].astype(float) if len(st) else pd.Series(dtype=float)
        n = len(pnl)
        mean = pnl.mean() if n else 0.0
        std = pnl.std(ddof=1) if n > 1 else float("nan")
        t_stat = mean / (std / math.sqrt(n)) if n > 1 and std and std > 0 else float("nan")
        gross_win, gross_loss = pnl[pnl > 0].sum(), -pnl[pnl < 0].sum()

        buys = f[f.side == "BUY"] if len(f) else f
        # "Profit per $1" only counts money bought in rounds that have settled: realized P&L doesn't
        # include open rounds yet, so their purchases would drag the ratio toward zero.
        settled_slugs = set(st["slug"]) if len(st) else set()
        bought_settled = buys.loc[buys.slug.isin(settled_slugs), "usd"].sum() if len(buys) else 0.0
        bought = buys["usd"].sum() if len(buys) else 0.0
        # The bot's own value (cash + open bets at the mid) while it keeps recording this strategy; a
        # retired strategy's last row is frozen from before its final rounds paid out, so rebuild it.
        if len(e) and latest_eq is not None and latest_eq - e["ts"].iloc[-1] <= 120:
            equity_now = e["equity"].iloc[-1]
        else:
            if marks is None:
                marks = open_positions_marked(d)
            equity_now = ledger_value(name, bankroll, fills, sett, marks)
        if d.get("max_dd") is not None:
            max_dd = d["max_dd"].get(name, 0.0) or 0.0
        elif len(e):
            curve = e["equity"]
            max_dd = (curve / curve.cummax() - 1).min()
        else:
            max_dd = 0.0

        if len(f):
            # Polymarket US measures volume as contracts x price of the side received:
            # a buy counts what was paid, a sell counts contracts x (1 - sale price).
            vol = (np.where(f.side == "BUY", f["usd"], f["shares"] - f["usd"]).sum() if us else f["usd"].sum())
            last_theta = f["theta"].dropna().iloc[-1] if "theta" in f and f["theta"].notna().any() else float("nan")
            rebates = f["rebate"].fillna(0).sum() if "rebate" in f else 0.0
        else:
            vol, last_theta, rebates = 0.0, float("nan"), 0.0
        rows.append({
            "Strategy": name,
            "Status": "Stopped" if name in stopped else "Trading",
            "Equity $": equity_now,
            "Return %": (equity_now / bankroll - 1) * 100,
            "Realized P&L $": pnl.sum() if n else 0.0,
            "Windows": n,
            "Win rate %": (pnl > 0).mean() * 100 if n else float("nan"),
            "Avg P&L/window $": mean if n else float("nan"),
            "t-stat": t_stat,
            "Max DD %": max_dd * 100,
            "Profit factor": gross_win / gross_loss if gross_loss > 0 else float("nan"),
            "Edge per $1 bought": pnl.sum() / bought_settled if bought_settled > 0 else float("nan"),
            "Avg entry price": (buys["usd"].sum() / buys["shares"].sum()) if len(buys) else float("nan"),
            "Fees $": f["fee"].sum() if len(f) else 0.0,
            # fees per dollar that changed hands (buys and sells). Polymarket's fee is theta x p x (1-p) per
            # contract, i.e. theta x (1-p) per dollar paid: ~9% on a 5c longshot, ~1% on a 90c favorite.
            "Fee % of traded": (100 * f["fee"].sum() / f["usd"].sum()) if len(f) and f["usd"].sum() > 0 else float("nan"),
            "Bought $": bought,                                        # dollars spent buying shares
            "Volume $": vol,                    # US: exchange volume definition; intl: bought + sold
            "Fee theta": last_theta,            # taker fee coefficient on the latest fill
            "Rebates $": rebates,
        })
    return pd.DataFrame(rows)


def equity_curves(d) -> pd.DataFrame:
    eq = d["equity"]
    if not len(eq):
        return eq
    eq = eq.merge(d["strategies"][["name", "bankroll"]], left_on="strategy", right_on="name")
    eq["return_pct"] = (eq["equity"] / eq["bankroll"] - 1) * 100
    return eq.sort_values("ts")


def cumulative_by_window(d) -> pd.DataFrame:
    s = d["settlements"]
    if not len(s):
        return s.assign(window_n=pd.Series(dtype=float), cum_pnl=pd.Series(dtype=float))
    s = s.sort_values("ts").copy()
    s["window_n"] = s.groupby("strategy").cumcount() + 1
    s["cum_pnl"] = s.groupby("strategy")["pnl"].cumsum()
    return s


def calibration(d, bucket: float = 0.05) -> pd.DataFrame:
    """
    For every position held to the result: what price did we pay vs. how often did it win?
    Above the diagonal = the market underpriced what you bought (edge).
    Only shares still held when the round ended count -- shares sold early (e.g. by a scalp)
    never got paid by the result, so they say nothing about whether the price was fair.
    One point per (strategy, round, side), weighted by the shares held to the end.
    """
    f, s = d["fills"], d["settlements"]
    if not len(f) or not len(s):
        return pd.DataFrame()
    buys = f[f["side"] == "BUY"]
    g = buys.groupby(["strategy", "slug", "outcome"]).agg(buy_sh=("shares", "sum"), buy_usd=("usd", "sum")).reset_index()
    g = g[g["buy_sh"] > 1e-9]
    # contracts actually held when the round settled (from the settlement record: this also covers
    # positions closed by buying the other side on Polymarket US, which never appear as a SELL of it)
    b = g.merge(s[["strategy", "slug", "winner", "up_shares", "down_shares"]], on=["strategy", "slug"])
    b["held"] = np.where(b["outcome"] == "UP", b["up_shares"], b["down_shares"])
    b = b[b["held"] > 1e-6]
    if not len(b):
        return pd.DataFrame()
    b["avg_price"] = b["buy_usd"] / b["buy_sh"]
    b["won"] = (b["outcome"] == b["winner"]).astype(float)
    # + 1e-9: in floating point 0.60 / 0.05 = 11.999..., which would put 0.60 in the 0.55 bucket
    b["bucket"] = (np.floor(b["avg_price"] / bucket + 1e-9) * bucket).round(2)
    out = b.groupby(["strategy", "bucket"]).apply(
        lambda x: pd.Series({"avg_price": np.average(x["avg_price"], weights=x["held"]),
                             "win_rate": np.average(x["won"], weights=x["held"]),
                             "fills": len(x)}), include_groups=False).reset_index()
    return out


def break_even_curve(fee_rate: float, exponent: float = 1.0, n: int = 101) -> pd.DataFrame:
    """
    Win rate needed to break even when buying at price p and holding to the result, taker fee
    included: p + fee_rate * (p * (1-p)) ** exponent. At 50c with the crypto rate (0.07) that is
    51.75%, not 50%.
    """
    p = np.linspace(0.0, 1.0, n)
    return pd.DataFrame({"price": p, "win_rate": p + fee_rate * (p * (1 - p)) ** exponent})


def pnl_by_hour(d) -> pd.DataFrame:
    s = d["settlements"]
    if not len(s):
        return s
    s = s.copy()
    s["hour_et"] = s["time"].dt.hour
    return s.groupby(["strategy", "hour_et"])["pnl"].sum().reset_index()


def realized_timeline(d) -> pd.DataFrame:
    """Cumulative realized P&L per strategy over time (one point per finished trade, see closed_rounds)."""
    s = closed_rounds(d["fills"], d["settlements"])
    if len(s):
        s["time"] = _to_time(s["ts"])
    if not len(s):
        return s.assign(cum_pnl=pd.Series(dtype=float))     # same columns when nothing has settled yet
    s = s.sort_values("ts").copy()
    s["cum_pnl"] = s.groupby("strategy")["pnl"].cumsum()
    return s


def open_positions_marked(d) -> pd.DataFrame:
    """
    Open bets with a live value: shares x the latest mid of that side (from the snapshots),
    and the unrealized P&L that implies. Columns: strategy, asset, slug, outcome, shares,
    net_cost, mark, value, unrealized, seconds_left.
    """
    op = open_positions(d)
    if not len(op):
        return op
    snaps = d.get("snapshots")
    op = op.copy()
    op["mark"] = np.nan
    op["seconds_left"] = np.nan
    if snaps is not None and len(snaps):
        last = snaps.sort_values("ts").groupby("slug").tail(1).set_index("slug")
        for i, r in op.iterrows():
            if r.slug not in last.index:
                continue
            row = last.loc[r.slug]
            k = "up" if r.outcome == "UP" else "down"
            bid, ask = row[f"{k}_bid"], row[f"{k}_ask"]
            mids = [x for x in (bid, ask) if pd.notna(x)]
            op.at[i, "mark"] = sum(mids) / len(mids) if mids else np.nan
            op.at[i, "seconds_left"] = max(row["seconds_left"] - (snaps["ts"].max() - row["ts"]), 0)
    op["value"] = op["shares"] * op["mark"]
    op["unrealized"] = op["value"] - op["net_cost"]
    return op


def open_positions(d) -> pd.DataFrame:
    f, s = d["fills"], d["settlements"]
    if not len(f):
        return f
    settled = set(zip(s["strategy"], s["slug"])) if len(s) else set()
    o = f[[ (a, b) not in settled for a, b in zip(f["strategy"], f["slug"]) ]].copy()
    if not len(o):
        return o
    o["signed"] = np.where(o["side"] == "BUY", o["shares"], -o["shares"])
    o["net_usd"] = np.where(o["side"] == "BUY", o["usd"] + o["fee"], -(o["usd"] - o["fee"]))
    return (o.groupby(["strategy", "asset", "slug", "outcome"])
              .agg(shares=("signed", "sum"), net_cost=("net_usd", "sum")).reset_index()
              .query("shares > 1e-6"))


def round_trips(d) -> pd.DataFrame:
    """
    One row per entry -> exit: all fills of one strategy on one outcome in one round.
      entry  = the buys (volume-weighted price)
      exit   = the sells, or, for shares still held when the round ended, the $1 / $0 payout
      P&L    = proceeds + payout - cost, fees included on both sides
    A round where the strategy bought BOTH sides (a hedge) is one row with outcome "BOTH":
    its two legs are one trade, and splitting them would always show one win and one loss.
    For those, entry/exit prices are per PAIR (Up + Down), so ~1.00 = break-even.
    Rounds that haven't finished yet show status "Open" with no P&L.
    """
    f, s = d["fills"], d["settlements"]
    cols = ["strategy", "asset", "slug", "outcome", "entry_ts", "exit_ts", "entry_price", "exit_price",
            "shares", "cost", "fees", "pnl", "return_pct", "hold_s", "exit_type", "window_end"]
    if not len(f):
        return pd.DataFrame(columns=cols)
    winners = {(r.strategy, r.slug): r.winner for r in s.itertuples()} if len(s) else {}
    rows = []
    for (strat, slug), rnd in f.sort_values("ts").groupby(["strategy", "slug"]):
        bought_sides = sorted(rnd.loc[rnd.side == "BUY", "outcome"].unique())
        groups = [("BOTH", rnd)] if len(bought_sides) == 2 else [(o, rnd[rnd.outcome == o]) for o in bought_sides]
        for outcome, g in groups:
            buys, sells = g[g.side == "BUY"], g[g.side == "SELL"]
            winner = winners.get((strat, slug))
            cost = (buys["usd"] + buys["fee"]).sum()
            sell_usd = sells["usd"].sum()
            proceeds = (sells["usd"] - sells["fee"]).sum()
            sides = ("UP", "DOWN") if outcome == "BOTH" else (outcome,)
            remaining = {o: max(buys.loc[buys.outcome == o, "shares"].sum()
                                - sells.loc[sells.outcome == o, "shares"].sum(), 0.0) for o in sides}
            if outcome == "BOTH":
                bought = min(buys.loc[buys.outcome == o, "shares"].sum() for o in sides)   # pairs
                entry_price = sum(buys.loc[buys.outcome == o, "usd"].sum() / buys.loc[buys.outcome == o, "shares"].sum()
                                  for o in sides)
            else:
                bought = buys["shares"].sum()
                entry_price = buys["usd"].sum() / bought
            window_end = g["window_end"].iloc[0]
            held = sum(remaining.values()) > 1e-6
            payout = 0.0
            if held and winner is None:
                exit_type, exit_ts, pnl = "Open", float("nan"), float("nan")
            else:
                if held:
                    payout = remaining.get(winner, 0.0)
                    if outcome == "BOTH":
                        exit_type = "Hedged to result"
                    else:
                        exit_type = "Held to result (won)" if payout > 0 else "Held to result (lost)"
                    exit_ts = window_end
                else:
                    exit_type, exit_ts = "Sold", sells["ts"].iloc[-1]
                pnl = proceeds + payout - cost
            rows.append({
                "strategy": strat, "asset": g["asset"].iloc[0], "slug": slug, "outcome": outcome,
                "entry_ts": buys["ts"].iloc[0], "exit_ts": exit_ts,
                "entry_price": entry_price,
                "exit_price": (sell_usd + payout) / bought if exit_type != "Open" and bought > 0 else float("nan"),
                "shares": bought, "cost": cost, "fees": g["fee"].sum(), "pnl": pnl,
                "return_pct": pnl / cost * 100 if cost > 0 else float("nan"),
                "hold_s": exit_ts - buys["ts"].iloc[0], "exit_type": exit_type, "window_end": window_end,
            })
    out = pd.DataFrame(rows, columns=cols)
    if len(out):
        out["entry_time"] = pd.to_datetime(out["entry_ts"], unit="s", utc=True).dt.tz_convert(TZ)
        out["exit_time"] = pd.to_datetime(out["exit_ts"], unit="s", utc=True).dt.tz_convert(TZ)
    return out.sort_values("entry_ts", ascending=False).reset_index(drop=True)


def price_path(db_path: str, slug: str, t0: float, t1: float) -> pd.DataFrame:
    """Top-of-book for one market between two unix times (the price path of a round trip)."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    df = pd.read_sql("SELECT * FROM snapshots WHERE slug=? AND ts BETWEEN ? AND ? ORDER BY ts",
                     con, params=(slug, t0 - 5, t1 + 5))
    con.close()
    df["time"] = _to_time(df["ts"])
    return df


def summary(d) -> pd.DataFrame:
    """
    The headline table: one row per strategy, plain-English columns.
    'Trades' = finished trades: rounds that resolved, plus rounds the strategy fully exited early.
    """
    lb = leaderboard(d)
    if not len(lb):
        return lb
    return pd.DataFrame({
        "Strategy": lb["Strategy"],
        "Portfolio value": lb["Equity $"],
        "Total return": lb["Return %"],
        "Realized P&L": lb["Realized P&L $"],
        "Trades": lb["Windows"],
        "Win rate": lb["Win rate %"],
        "Avg P&L per trade": lb["Avg P&L/window $"],
        "Fees": lb["Fees $"],
        "Fee %": lb["Fee % of traded"],
        "Money bet": lb["Bought $"],
        "Volume traded": lb["Volume $"],
        # extras kept for the optional "advanced" table
        "Luck check (t-stat)": lb["t-stat"],
        "Worst drop": lb["Max DD %"],
        "Profit per $1 bet": lb["Edge per $1 bought"],
        "Avg price paid": lb["Avg entry price"],
        "Status": lb["Status"],
        "Fee theta": lb["Fee theta"],
        "Rebates": lb["Rebates $"],
    })
