"""
PART C analytics: per-strategy scorecard, model calibration, PnL splits, maker adverse selection.
Pure pandas over the database (read-only). Used by the dashboard's "Strategy lab" section and
by tools/export_trades.py.

Definitions
  Trades              BUY fills (entries). Rounds = settled rounds the strategy had money in.
  PnL after fees      sum of finished trades' P&L (settled rounds + rounds fully exited early; fees inside).
  PnL before fees     PnL after fees + fees paid in those rounds - rebates.
  Win rate            rounds with P&L > 0 / rounds.
  Avg entry price     share-weighted average BUY price.
  Breakeven win rate  average (price + fee per share) paid on BUYs: the hit rate needed to break even
                      when held to resolution (for the sniper this is the number to beat).
  Avg edge at entry   mean of fair value - (fill price + fee per share), as recorded at the fill.
  Avg realized edge   mean over BUY fills of (1 if that side won else 0) - (price + fee per share):
                      what the entry actually earned per share if held to resolution.
  Sharpe (per trade)  mean / standard deviation of round P&L.
  Adverse selection   maker fills: fair value 5s after the fill minus at the fill, signed so that
                      negative = the model moved against us (we were picked off).
"""
from __future__ import annotations

import math
import sqlite3

import numpy as np
import pandas as pd

from core import metrics

TIME_LEFT_BUCKETS = [0, 30, 60, 120, 300, 600, 900, float("inf")]
TIME_LEFT_LABELS = ["0-30s", "30-60s", "1-2m", "2-5m", "5-10m", "10-15m", "15m+"]


def _ro(db_path):
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def load_lab(db_path: str, include_invalid: bool = False) -> dict[str, pd.DataFrame]:
    """include_invalid=False (default): rounds marked invalid (core/validity.py) are left out, as in metrics.load."""
    con = _ro(db_path)
    try:
        have = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        out = {
            "fills": pd.read_sql("SELECT * FROM fills", con),
            "settlements": pd.read_sql("SELECT * FROM settlements", con),
            "resolutions": pd.read_sql("SELECT slug, winner FROM resolutions", con)
            if "resolutions" in have else pd.DataFrame(columns=["slug", "winner"]),
            "events": pd.read_sql("SELECT strategy, kind, COUNT(*) AS n, MAX(ts) AS last_ts FROM events "
                                  "GROUP BY strategy, kind", con)
            if "events" in have else pd.DataFrame(columns=["strategy", "kind", "n", "last_ts"]),
        }
        from core.validity import invalid_rounds
        invalid = invalid_rounds(con) if "round_validity" in have else {}
    finally:
        con.close()
    metrics.apply_validity(out, invalid, include_invalid)
    f = out["fills"]
    for c in ("liquidity", "fair_value", "edge", "seconds_left", "fv_after_5s", "rebate"):
        if c not in f:
            f[c] = np.nan
    return out


def kind_of(asset: str) -> str:
    return asset.rsplit("-", 1)[-1] if isinstance(asset, str) and "-" in asset else str(asset)


def _with_winner(fills: pd.DataFrame, res: pd.DataFrame) -> pd.DataFrame:
    if not len(fills):
        return fills.assign(winner=pd.Series(dtype=object), won=pd.Series(dtype=float))
    f = fills.merge(res, on="slug", how="left")
    f["won"] = np.where(f["winner"].isna(), np.nan, (f["outcome"] == f["winner"]).astype(float))
    return f


def scorecard(lab: dict, strategies: list[str], max_dd: dict | None = None,
              bankrolls: dict | None = None) -> pd.DataFrame:
    fills = _with_winner(lab["fills"], lab["resolutions"])
    sett = lab["settlements"]
    closed = metrics.closed_rounds(lab["fills"], sett)
    rows = []
    for name in strategies:
        f = fills[fills.strategy == name] if len(fills) else fills
        st = closed[closed.strategy == name] if len(closed) else closed     # finished trades
        buys = f[f.side == "BUY"] if len(f) else f
        pnl = st["pnl"].astype(float) if len(st) else pd.Series(dtype=float)
        settled = set(st["slug"]) if len(st) else set()
        f_set = f[f.slug.isin(settled)] if len(f) else f
        fees_settled = f_set["fee"].sum() - f_set["rebate"].fillna(0).sum() if len(f_set) else 0.0
        n = len(pnl)
        std = pnl.std(ddof=1) if n > 1 else float("nan")
        fee_ps = buys["fee"] / buys["shares"] if len(buys) else pd.Series(dtype=float)
        reb_ps = buys["rebate"].fillna(0) / buys["shares"] if len(buys) else pd.Series(dtype=float)
        sh = buys["shares"].sum() if len(buys) else 0.0
        realized = (buys["won"] - (buys["avg_price"] + fee_ps - reb_ps)).dropna() if len(buys) else pd.Series(dtype=float)
        rows.append({
            "strategy": name,
            "pnl_before_fees": (pnl.sum() + fees_settled) if n else 0.0,
            "pnl_after_fees": pnl.sum() if n else 0.0,
            "return_pct": pnl.sum() / bankrolls[name] * 100 if bankrolls and bankrolls.get(name) else np.nan,
            "trades": len(buys),
            "rounds": n,
            "win_rate": (pnl > 0).mean() if n else np.nan,
            "avg_entry": (buys["usd"].sum() / sh) if sh > 0 else np.nan,
            "breakeven_win_rate": (((buys["avg_price"] + fee_ps - reb_ps) * buys["shares"]).sum() / sh) if sh > 0 else np.nan,
            "hit_rate": (buys["won"] * buys["shares"]).sum() / buys.loc[buys["won"].notna(), "shares"].sum()
            if len(buys) and buys["won"].notna().any() else np.nan,
            "avg_edge_entry": buys["edge"].mean() if len(buys) else np.nan,
            "avg_realized_edge": realized.mean() if len(realized) else np.nan,
            "sharpe": pnl.mean() / std if n > 1 and std and std > 0 else np.nan,
            "t_stat": pnl.mean() / (std / math.sqrt(n)) if n > 1 and std and std > 0 else np.nan,
            "max_dd_pct": (max_dd or {}).get(name, np.nan) * 100 if max_dd and name in max_dd else np.nan,
            "fees": f["fee"].sum() if len(f) else 0.0,
            "maker_share": (f["liquidity"] == "maker").mean() if len(f) else np.nan,
        })
    return pd.DataFrame(rows)


def scorecard_by_kind(lab: dict, strategies: list[str], bankrolls: dict | None = None) -> pd.DataFrame:
    """The scorecard computed separately for each round length (5m, 15m): one row per strategy x market.
    Max drawdown is per account, not per market, so it is left out here."""
    kinds = sorted({kind_of(a) for t in ("fills", "settlements") for a in lab[t]["asset"].dropna().unique()
                    if len(lab[t])}, key=lambda k: (len(k), k))
    parts = []
    for kind in kinds:
        sub = {**lab}
        for t in ("fills", "settlements"):
            df = lab[t]
            sub[t] = df[df["asset"].map(kind_of) == kind] if len(df) else df
        sub["resolutions"] = lab["resolutions"]
        sc = scorecard(sub, strategies, max_dd=None, bankrolls=bankrolls)
        sc.insert(1, "kind", kind)
        parts.append(sc[sc["trades"] > 0])
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


def pnl_by_kind(lab: dict) -> pd.DataFrame:
    s = lab["settlements"]
    if not len(s):
        return pd.DataFrame(columns=["strategy", "kind", "rounds", "pnl"])
    s = s.assign(kind=s["asset"].map(kind_of))
    return s.groupby(["strategy", "kind"]).agg(rounds=("pnl", "size"), pnl=("pnl", "sum")).reset_index()


def pnl_by_time_left(lab: dict) -> pd.DataFrame:
    """Each settled round's P&L, bucketed by seconds left at its FIRST entry."""
    f, s = lab["fills"], lab["settlements"]
    if not len(f) or not len(s) or f["seconds_left"].isna().all():
        return pd.DataFrame(columns=["strategy", "bucket", "rounds", "pnl"])
    first = (f[(f.side == "BUY") & f["seconds_left"].notna()].sort_values("ts")
             .groupby(["strategy", "slug"]).first().reset_index()[["strategy", "slug", "seconds_left"]])
    m = s.merge(first, on=["strategy", "slug"])
    if not len(m):
        return pd.DataFrame(columns=["strategy", "bucket", "rounds", "pnl"])
    m["bucket"] = pd.cut(m["seconds_left"], TIME_LEFT_BUCKETS, labels=TIME_LEFT_LABELS, right=False)
    out = m.groupby(["strategy", "bucket"], observed=True).agg(rounds=("pnl", "size"), pnl=("pnl", "sum")).reset_index()
    out["bucket"] = out["bucket"].astype(str)
    return out


def model_calibration(lab: dict, step: float = 0.1) -> pd.DataFrame:
    """
    Every fill bucketed by the model's fair value of the side traded (0.5-0.6, 0.6-0.7, ...), against
    how often that side actually won. On the diagonal = the model is right.
    """
    f = _with_winner(lab["fills"], lab["resolutions"])
    if not len(f):
        return pd.DataFrame()
    f = f[f["fair_value"].notna() & f["won"].notna()]
    if not len(f):
        return pd.DataFrame()
    b = np.clip(np.floor(f["fair_value"] / step + 1e-9) * step, 0, 1 - step).round(2)
    f = f.assign(bucket=b)
    out = f.groupby(["strategy", "bucket"]).agg(trades=("won", "size"), win_rate=("won", "mean"),
                                                avg_fair=("fair_value", "mean")).reset_index()
    allf = f.groupby("bucket").agg(trades=("won", "size"), win_rate=("won", "mean"),
                                   avg_fair=("fair_value", "mean")).reset_index().assign(strategy="All trades")
    out = pd.concat([out, allf], ignore_index=True)
    out["label"] = out["bucket"].map(lambda x: f"{x:.1f}-{x + step:.1f}")
    return out


def model_calibration_ticks(db_path: str, step: float = 0.1) -> pd.DataFrame:
    """The model on EVERY logged tick (not just trades): fair value of Up vs. whether Up won."""
    con = _ro(db_path)
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(snapshots)")}
        if "fv_up" not in cols:
            return pd.DataFrame()
        df = pd.read_sql(
            "SELECT MIN(CAST(s.fv_up / ? AS INTEGER), ?) AS b, COUNT(*) AS ticks, "
            "AVG(CASE WHEN r.winner = 'UP' THEN 1.0 ELSE 0.0 END) AS win_rate, AVG(s.fv_up) AS avg_fair "
            "FROM snapshots s JOIN resolutions r ON r.slug = s.slug "
            "WHERE s.fv_up IS NOT NULL AND r.winner IN ('UP', 'DOWN') GROUP BY b",
            con, params=(step, int(round(1 / step)) - 1))
    finally:
        con.close()
    if len(df):
        df["bucket"] = (df["b"] * step).round(2)
    return df


def adverse_selection(lab: dict) -> pd.DataFrame:
    f = lab["fills"]
    if not len(f):
        return pd.DataFrame(columns=["strategy", "maker_fills", "avg_fv_move", "adverse_share"])
    m = f[(f["liquidity"] == "maker") & f["fv_after_5s"].notna() & f["fair_value"].notna()]
    if not len(m):
        return pd.DataFrame(columns=["strategy", "maker_fills", "avg_fv_move", "adverse_share"])
    move = np.where(m["side"] == "BUY", m["fv_after_5s"] - m["fair_value"], m["fair_value"] - m["fv_after_5s"])
    m = m.assign(move=move)
    return m.groupby("strategy").agg(maker_fills=("move", "size"), avg_fv_move=("move", "mean"),
                                     adverse_share=("move", lambda x: (x < 0).mean())).reset_index()
