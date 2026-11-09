"""
Out-of-sample validation maths for replayed (or live) results. Pure functions over the fills / settlements
DataFrames that core/metrics.load() returns, so they are unit-testable with synthetic data.

Vocabulary
  round   one (strategy, market round) with a final P&L  (metrics.closed_rounds; the leaderboard's "Windows")
  trade   one BUY fill. Several trades happen inside one round and are correlated, so every confidence
          interval here resamples ROUNDS, never fills.
Time split only: development = older rounds, holdout = the newest rounds (by the round's start time in its slug).
"""
from __future__ import annotations

import hashlib
import json
import math
import time
from statistics import NormalDist
from typing import Optional

import numpy as np
import pandas as pd

from core import metrics

# Evidence behind the replay-optimism flag (from the self-check on the recorded days): replay fills against the
# decision-time book, live fills against a book fetched ~250 ms later. On thin books that is optimistic.
OPTIMISM_EVIDENCE = ("self-check: fill match 94.9%; s3_sniper_v2 live -$349 vs replay -$236; 58 longshot rounds "
                     "(entries < $0.20) explain $132 of the $156 total gap; on the same data s3_sniper_v2 has 4% of "
                     "fills but 38% of shares below $0.20, so shares are counted too")
OPTIMISM_CAVEAT = ("replay is optimistic on thin books (live fills against a later book); trust win rate, sign and "
                   "ranking, not dollars.")


def slug_start(slug: str) -> int:
    try:
        return int(str(slug).rsplit("-", 1)[1])
    except (IndexError, ValueError):
        return 0


def round_table(fills: pd.DataFrame, sett: pd.DataFrame, markets: Optional[list[str]] = None) -> pd.DataFrame:
    """One row per (strategy, slug): start (round start, unix s), asset, pnl, n_buys, shares bought, usd bought,
    cost (usd + fees on buys), fees (all fills). `markets` = ['5m','15m'] keeps only those window kinds."""
    cr = metrics.closed_rounds(fills, sett)
    cols = ["strategy", "slug", "asset", "start", "pnl", "n_buys", "shares", "usd", "cost", "fees"]
    if not len(cr):
        return pd.DataFrame(columns=cols)
    if markets:
        cr = cr[cr["asset"].astype(str).map(lambda a: any(str(a).endswith("-" + m) for m in markets))]
    buys = fills[fills["side"] == "BUY"] if len(fills) else fills
    g = buys.groupby(["strategy", "slug"]).agg(n_buys=("shares", "size"), shares=("shares", "sum"),
                                              usd=("usd", "sum"), bfee=("fee", "sum")).reset_index()
    fees = (fills.groupby(["strategy", "slug"])["fee"].sum().rename("fees").reset_index()
            if len(fills) else pd.DataFrame(columns=["strategy", "slug", "fees"]))
    t = cr.merge(g, on=["strategy", "slug"], how="left").merge(fees, on=["strategy", "slug"], how="left")
    t["n_buys"] = t["n_buys"].fillna(0).astype(int)
    t["shares"] = t["shares"].fillna(0.0)
    t["usd"] = t["usd"].fillna(0.0)
    t["cost"] = t["usd"] + t["bfee"].fillna(0.0)
    t["fees"] = t["fees"].fillna(0.0)
    t["start"] = t["slug"].map(slug_start)
    t["pnl"] = t["pnl"].astype(float)
    return t[cols].reset_index(drop=True)


def split_rounds(rounds: pd.DataFrame, holdout_days: float = 0.0, dev_days: float = 0.0,
                 holdout_frac: float = 0.0) -> tuple[pd.DataFrame, pd.DataFrame, float]:
    """(development, holdout, cut). Strictly chronological: holdout = rounds starting at or after `cut`.
    cut = newest round start - holdout_days, or the start time of the newest `holdout_frac` of rounds. dev_days > 0
    additionally limits development to the `dev_days` just before the cut. Never random."""
    if not len(rounds):
        return rounds, rounds, float("nan")
    if holdout_days:
        cut = float(rounds["start"].max()) - holdout_days * 86400.0
    elif holdout_frac:
        cut = float(np.quantile(rounds["start"].to_numpy(), 1.0 - holdout_frac))
    else:
        cut = float(rounds["start"].max()) + 1.0                   # no holdout: everything is development
    dev = rounds[rounds["start"] < cut]
    if dev_days:
        dev = dev[dev["start"] >= cut - dev_days * 86400.0]
    hold = rounds[rounds["start"] >= cut]
    return dev, hold, cut


def bootstrap_expectancy(pnl: np.ndarray, n_buys: np.ndarray, n_boot: int = 2000, seed: int = 12345,
                         ci: float = 0.95) -> tuple[float, float]:
    """Bootstrap CI of per-trade expectancy = sum(round pnl) / sum(buy fills), resampling ROUNDS with replacement
    (fills inside a round are correlated). Fixed seed => reproducible."""
    pnl, n_buys = np.asarray(pnl, float), np.asarray(n_buys, float)
    m = len(pnl)
    if m < 2 or n_buys.sum() <= 0:
        return float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    out = np.empty(n_boot)
    step = max(1, 4_000_000 // m)                                  # chunk to bound memory
    for i in range(0, n_boot, step):
        k = min(step, n_boot - i)
        idx = rng.integers(0, m, size=(k, m))
        den = n_buys[idx].sum(axis=1)
        out[i:i + k] = np.where(den > 0, pnl[idx].sum(axis=1) / np.where(den > 0, den, 1.0), np.nan)
    a = (1 - ci) / 2
    return float(np.nanquantile(out, a)), float(np.nanquantile(out, 1 - a))


def summarize(r: pd.DataFrame, n_boot: int = 2000, seed: int = 12345) -> dict:
    """Stats for one strategy's rounds (one split)."""
    n = len(r)
    if not n:
        return {"rounds": 0, "trades": 0}
    pnl = r["pnl"].to_numpy(float)
    shares, cost = float(r["shares"].sum()), float(r["cost"].sum())
    trades = int(r["n_buys"].sum())
    lo, hi = bootstrap_expectancy(pnl, r["n_buys"].to_numpy(), n_boot, seed)
    return {"rounds": n, "trades": trades, "pnl": float(pnl.sum()),
            "win_rate": float((pnl > 0).mean()),
            "breakeven_win_rate": cost / shares if shares > 0 else float("nan"),     # (price + fee/share): $1 payout
            "avg_entry": float(r["usd"].sum()) / shares if shares > 0 else float("nan"),
            "expectancy": float(pnl.sum() / trades) if trades else float("nan"),
            "ci_lo": lo, "ci_hi": hi, "fees": float(r["fees"].sum()),
            "t_stat": metrics.t_stat_of(pnl)}


def entry_stats(fills: pd.DataFrame, rounds: pd.DataFrame, strategy: str) -> dict:
    """Average BUY entry price (USD-weighted per share) and the per-fill price list for the rounds given."""
    if not len(fills) or not len(rounds):
        return {"avg_entry": float("nan"), "prices": np.array([]), "shares": np.array([]), "slugs": []}
    keep = set(rounds["slug"])
    f = fills[(fills["strategy"] == strategy) & (fills["side"] == "BUY") & fills["slug"].isin(keep)]
    sh = f["shares"].to_numpy(float)
    return {"avg_entry": float(f["usd"].sum() / sh.sum()) if sh.sum() > 0 else float("nan"),
            "prices": f["avg_price"].to_numpy(float), "shares": sh, "slugs": list(f["slug"]),
            "ts": f["ts"].to_numpy(float), "outcomes": list(f["outcome"])}


def optimism_flag(prices: np.ndarray, shares: np.ndarray, depth: Optional[np.ndarray] = None,
                  price_floor: float = 0.20, price_share: float = 0.25, depth_ratio: float = 1.0,
                  depth_share: float = 0.25) -> dict:
    """LOW CONFIDENCE ON DOLLARS when more than `price_share` of fills are at entry price < `price_floor`, or (when
    book depth is known) more than `depth_share` of fills are larger than `depth_ratio` x the displayed best-ask
    depth. Returns {flag, low_price_share, thin_share (None if depth unknown), reasons}."""
    n = len(prices)
    out = {"flag": False, "low_price_share": float("nan"), "thin_share": None, "reasons": []}
    if not n:
        return out
    is_low = np.asarray(prices) < price_floor
    low = float(is_low.mean())
    sh = np.asarray(shares, float)
    low_sh = float(sh[is_low].sum() / sh.sum()) if len(sh) == n and sh.sum() > 0 else float("nan")
    out["low_price_share"], out["low_price_share_of_shares"] = low, low_sh
    # Cheap fills are few but hold most of the SHARES (a $25 stake buys 5x more shares at 0.10 than at 0.50), and
    # they are exactly what the thin-book optimism hits: s3_sniper_v2 has 4% of fills but 38% of shares below $0.20.
    if low > price_share:
        out["flag"] = True
        out["reasons"].append(f"{low * 100:.0f}% of fills at entry < ${price_floor:.2f} (limit {price_share * 100:.0f}%)")
    if low_sh > price_share:
        out["flag"] = True
        out["reasons"].append(f"{low_sh * 100:.0f}% of shares bought at entry < ${price_floor:.2f} (limit {price_share * 100:.0f}%)")
    if depth is not None and len(depth) == n:
        d = np.asarray(depth, float)
        known = ~np.isnan(d)
        if known.any():
            thin = float((np.asarray(shares)[known] > depth_ratio * d[known]).mean())
            out["thin_share"] = thin
            if thin > depth_share:
                out["flag"] = True
                out["reasons"].append(f"{thin * 100:.0f}% of fills larger than {depth_ratio:g}x displayed best-ask depth")
    return out


def multiple_comparisons(n_variants: int, alpha: float = 0.05) -> dict:
    """Bonferroni-adjusted threshold + the t you would expect from the BEST of n pure-noise variants."""
    n = max(1, n_variants)
    nd = NormalDist()
    return {"variants": n_variants, "alpha": alpha, "bonferroni_alpha": alpha / n,
            "t_threshold_single": nd.inv_cdf(1 - alpha / 2),
            "t_threshold_bonferroni": nd.inv_cdf(1 - alpha / (2 * n)),
            "expected_best_t_if_all_noise": nd.inv_cdf(1 - 1.0 / (2 * n)) if n > 1 else 0.0,
            "warn": n_variants >= 5}


def params_hash(obj) -> str:
    """Stable short hash of strategy parameters / run settings (recorded in the holdout reveal log)."""
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:16]


def log_reveal(path: str, strategies: list[str], phash: str, now: Optional[float] = None) -> int:
    """Append one line per holdout reveal; returns how many times it has been revealed in total (incl. this)."""
    import os
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now if now is not None else time.time()))
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"revealed_at": ts, "strategies": sorted(strategies), "params_hash": phash}) + "\n")
    return reveal_count(path)


def reveal_count(path: str) -> int:
    import os
    if not os.path.exists(path):
        return 0
    with open(path, encoding="utf-8") as fh:
        return sum(1 for line in fh if line.strip())


# ------------------------------------------------------------------------------------------ orchestration
def make_depth_lookup(src_db: str):
    """depth(slug, ts, outcome) -> displayed best-ask depth (shares) at or just before ts, from the source DB's
    `snapshots` table (None-safe: returns NaN when unknown). Read-only."""
    import sqlite3
    con = sqlite3.connect(f"file:{src_db}?mode=ro", uri=True)
    try:
        s = pd.read_sql("SELECT slug, ts, up_ask_depth, down_ask_depth FROM snapshots ORDER BY slug, ts", con)
    finally:
        con.close()
    groups = {k: (g["ts"].to_numpy(float), g["up_ask_depth"].to_numpy(float), g["down_ask_depth"].to_numpy(float))
              for k, g in s.groupby("slug")}

    def depth(slug, ts, outcome):
        g = groups.get(slug)
        if g is None:
            return float("nan")
        i = int(np.searchsorted(g[0], ts, side="right")) - 1
        if i < 0:
            return float("nan")
        return float(g[1][i] if str(outcome).upper() in ("UP", "YES") else g[2][i])
    return depth


def build_report(fills: pd.DataFrame, sett: pd.DataFrame, *, strategies: Optional[list[str]] = None,
                 markets: Optional[list[str]] = None, holdout_days: float = 0.0, dev_days: float = 0.0,
                 holdout_frac: float = 0.0, reveal_holdout: bool = False, n_boot: int = 2000, seed: int = 12345,
                 depth_lookup=None, price_floor: float = 0.20, price_share: float = 0.25, depth_ratio: float = 1.0,
                 depth_share: float = 0.25, ws_latency_book: bool = False) -> dict:
    """The holdout is neither computed nor returned unless reveal_holdout=True (so it cannot leak by accident).
    `ws_latency_book`: the run filled against websocket order-latency books (Part 2); then the optimism flag's
    price rule is relaxed note-wise only (it still applies to older periods)."""
    rounds = round_table(fills, sett, markets)
    if strategies:
        rounds = rounds[rounds["strategy"].isin(strategies)]
    dev, hold, cut = split_rounds(rounds, holdout_days, dev_days, holdout_frac)
    names = sorted(rounds["strategy"].unique())
    has_holdout = bool(holdout_days or holdout_frac)

    def one(r, name):
        s = summarize(r[r["strategy"] == name], n_boot, seed)
        if not s["rounds"]:
            return s
        e = entry_stats(fills, r[r["strategy"] == name], name)
        depth = None
        if depth_lookup is not None and len(e["prices"]):
            depth = np.array([depth_lookup(sl, t, o) for sl, t, o in zip(e["slugs"], e["ts"], e["outcomes"])])
        s["flag"] = optimism_flag(e["prices"], e["shares"], depth, price_floor, price_share, depth_ratio, depth_share)
        s["flag"]["depth_checked"] = depth is not None
        return s

    out = {"cut": cut, "has_holdout": has_holdout, "markets": markets or ["all"], "n_boot": n_boot, "seed": seed,
           "ws_latency_book": ws_latency_book, "variants": len(names),
           "multiple": multiple_comparisons(len(names)),
           "thresholds": {"price_floor": price_floor, "price_share": price_share, "depth_ratio": depth_ratio,
                          "depth_share": depth_share},
           "evidence": OPTIMISM_EVIDENCE, "strategies": {}}
    for n in names:
        out["strategies"][n] = {"dev": one(dev, n)}
        if has_holdout and reveal_holdout:
            out["strategies"][n]["holdout"] = one(hold, n)
    out["holdout_revealed"] = bool(has_holdout and reveal_holdout)
    return out


def _f(x, spec, dash="-"):
    return dash if x is None or (isinstance(x, float) and math.isnan(x)) else format(x, spec)


def render(rep: dict, split: str) -> str:
    """Text table for 'dev' or 'holdout'."""
    rows = []
    for name, d in rep["strategies"].items():
        s = d.get(split)
        if not s or not s.get("rounds"):
            continue
        flag = s["flag"]["flag"]
        rows.append((s["pnl"], f"{name:<30} {s['trades']:>6} {s['rounds']:>6} {s['pnl']:>10.2f} "
                     f"{_f(s['win_rate'] * 100, '.1f'):>6} {_f(s['breakeven_win_rate'] * 100, '.1f'):>7} "
                     f"{_f(s['avg_entry'], '.3f'):>6} {_f(s['expectancy'], '+.3f'):>8} "
                     f"[{_f(s['ci_lo'], '+.3f')},{_f(s['ci_hi'], '+.3f')}]".ljust(22) +
                     f" {s['fees']:>8.2f} {_f(s['t_stat'], '.2f'):>6}  {'LOW CONFIDENCE ON DOLLARS' if flag else ''}"))
    head = (f"{'strategy':<30} {'trades':>6} {'rounds':>6} {'pnl $':>10} {'win%':>6} {'b/e win%':>7} "
            f"{'entry':>6} {'E/trade':>8} {'95% CI (rounds)':<22} {'fees $':>8} {'t':>6}")
    rows.sort(key=lambda r: -r[0])
    return head + "\n" + "\n".join(r[1] for r in rows)


def render_header(rep: dict, reveal_count_: int = 0) -> str:
    m, t = rep["multiple"], rep["thresholds"]
    lines = ["VALIDATION  (time-based split; confidence intervals resample ROUNDS, seed %d, %d resamples)"
             % (rep["seed"], rep["n_boot"]),
             f"markets: {', '.join(rep['markets'])}    variants compared: {m['variants']}",
             f"Multiple comparisons: with {m['variants']} variants, single-test |t| > {m['t_threshold_single']:.2f} is NOT "
             f"enough: Bonferroni threshold |t| > {m['t_threshold_bonferroni']:.2f} (alpha {m['bonferroni_alpha']:.4f}); "
             f"the best of {m['variants']} pure-noise variants would show t ~ {m['expected_best_t_if_all_noise']:.2f} by chance."]
    if m["warn"]:
        lines.append(f"WARNING: {m['variants']} variants tested on the same data: the best one is likely flattered by luck. "
                     f"Judge by the holdout and the Bonferroni line, not the top row.")
    lines.append(f"Replay optimism flag (LOW CONFIDENCE ON DOLLARS): > {t['price_share'] * 100:.0f}% of fills (or of shares bought) at entry < "
                 f"${t['price_floor']:.2f}, or > {t['depth_share'] * 100:.0f}% of fills bigger than {t['depth_ratio']:g}x the "
                 f"displayed best-ask depth. Evidence: {rep['evidence']}.")
    lines.append("  Flagged: " + OPTIMISM_CAVEAT)
    if rep.get("ws_latency_book"):
        lines.append("  This run filled against websocket order-latency books where the recorder had data (more realistic "
                     "on thin books; fills in gaps and any period before 2026-11-09 13:19 local still use the decision "
                     "book, so the flag above still applies to those).")
    else:
        lines.append("  This run used decision-time books (no websocket latency book).")
    lines.append("b/e win% = (avg entry price + fee per share): the win rate needed to break even on hold-to-settlement "
                 "trades. E/trade = P&L per buy fill. t = mean/SE of per-round P&L (core/metrics).")
    if reveal_count_:
        lines.append(f"Holdout has been revealed {reveal_count_} time(s) in total (data/holdout_reveals.log).")
    return "\n".join(lines)
