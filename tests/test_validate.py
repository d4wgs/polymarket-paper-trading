"""Holdout validation harness: synthetic strategies with a known edge / pure noise."""
import contextlib
import io
import json
import os
import sqlite3
import tempfile
import unittest

import numpy as np
import pandas as pd

from core import metrics, validation as V
from core.storage import SCHEMA

DAY = 86400
T0 = 1_800_000_000


def synth(seed=7, rounds_per_day=60, days=6, edge_p=0.60, cheap=False):
    """Two strategies over `days` days. 'edge' buys 10 shares at 0.50 and wins with prob edge_p (true expectancy per
    trade = 10*(edge_p*0.5 - (1-edge_p)*0.5) = +1.0 at 0.60); 'noise' wins with prob 0.5 (true expectancy 0).
    'longshot' buys at 0.10 (to exercise the optimism flag)."""
    rng = np.random.default_rng(seed)
    fills, sett = [], []
    for d in range(days):
        for i in range(rounds_per_day):
            start = T0 + d * DAY + i * 300
            slug = f"btc-updown-5m-{start}"
            for name, p, win_p in (("edge", 0.50, edge_p), ("noise", 0.50, 0.50), ("longshot", 0.10, 0.10)):
                win = rng.random() < win_p
                fills.append({"ts": start + 30, "strategy": name, "asset": "BTC-5m", "slug": slug, "window_end": start + 300,
                              "outcome": "UP", "side": "BUY", "shares": 10.0, "avg_price": p, "usd": 10 * p, "fee": 0.0,
                              "reason": "t", "theta": 0.0, "rebate": 0.0})
                pnl = 10 * (1 - p) if win else -10 * p
                sett.append({"ts": start + 300, "strategy": name, "asset": "BTC-5m", "slug": slug,
                             "winner": "UP" if win else "DOWN", "up_shares": 10.0, "down_shares": 0.0,
                             "net_cost": 10 * p, "payout": 10.0 if win else 0.0, "pnl": pnl})
    return pd.DataFrame(fills), pd.DataFrame(sett)


class ValidationMath(unittest.TestCase):
    def setUp(self):
        self.fills, self.sett = synth()

    def test_edge_ranks_above_noise_and_the_ci_brackets_the_truth(self):
        rep = V.build_report(self.fills, self.sett, strategies=["edge", "noise"], holdout_days=2, n_boot=1000)
        e, n = rep["strategies"]["edge"]["dev"], rep["strategies"]["noise"]["dev"]
        self.assertGreater(e["expectancy"], n["expectancy"])
        self.assertGreater(e["t_stat"], n["t_stat"])
        self.assertLessEqual(e["ci_lo"], 1.0)                      # true per-trade expectancy of 'edge' is +1.00
        self.assertGreaterEqual(e["ci_hi"], 1.0)
        self.assertLessEqual(n["ci_lo"], 0.0)                      # ... and of 'noise' is 0
        self.assertGreaterEqual(n["ci_hi"], 0.0)
        self.assertLess(e["ci_hi"] - e["ci_lo"], 1.2)

    def test_stats_columns(self):
        rep = V.build_report(self.fills, self.sett, strategies=["edge"], holdout_days=2)
        s = rep["strategies"]["edge"]["dev"]
        self.assertEqual(s["trades"], s["rounds"])                 # one buy fill per round here
        self.assertAlmostEqual(s["avg_entry"], 0.50)
        self.assertAlmostEqual(s["breakeven_win_rate"], 0.50)      # price 0.50, no fee
        self.assertAlmostEqual(s["fees"], 0.0)
        # t-stat equals the leaderboard's (same helper)
        r = V.round_table(self.fills, self.sett)
        dev, _, _ = V.split_rounds(r, holdout_days=2)
        self.assertAlmostEqual(s["t_stat"], metrics.t_stat_of(dev[dev.strategy == "edge"]["pnl"]))

    def test_split_is_chronological_and_never_random(self):
        r = V.round_table(self.fills, self.sett)
        dev, hold, cut = V.split_rounds(r, holdout_days=2)
        self.assertLess(dev["start"].max(), cut)
        self.assertGreaterEqual(hold["start"].min(), cut)
        self.assertLessEqual(dev["start"].max(), hold["start"].min())
        self.assertEqual(len(dev) + len(hold), len(r))
        dev2, hold2, cut2 = V.split_rounds(r, holdout_frac=0.25)
        self.assertLessEqual(dev2["start"].max(), hold2["start"].min())
        self.assertAlmostEqual(len(hold2) / len(r), 0.25, delta=0.02)
        d3, _, c3 = V.split_rounds(r, holdout_days=2, dev_days=1)
        self.assertGreaterEqual(d3["start"].min(), c3 - DAY)

    def test_holdout_is_not_even_computed_without_the_flag(self):
        hidden = V.build_report(self.fills, self.sett, holdout_days=2)
        self.assertFalse(hidden["holdout_revealed"])
        self.assertTrue(all("holdout" not in v for v in hidden["strategies"].values()))
        self.assertNotIn("holdout", json.dumps(hidden, default=float).replace("holdout_revealed", "")
                         .replace("has_holdout", "").replace("holdout_", ""))
        shown = V.build_report(self.fills, self.sett, holdout_days=2, reveal_holdout=True)
        self.assertTrue(all("holdout" in v for v in shown["strategies"].values()))

    def test_bootstrap_resamples_rounds_not_fills(self):
        # 3 fills in each of 50 rounds, all perfectly correlated: CI must reflect 50 independent rounds, not 150
        rng = np.random.default_rng(1)
        pnl = rng.choice([-3.0, 3.0], size=50)
        lo, hi = V.bootstrap_expectancy(pnl, np.full(50, 3), n_boot=2000, seed=3)
        per_round_se = pnl.std(ddof=1) / np.sqrt(50) / 3
        self.assertAlmostEqual((hi - lo) / 2, 1.96 * per_round_se, delta=0.35 * 1.96 * per_round_se)
        self.assertEqual(V.bootstrap_expectancy(pnl, np.full(50, 3), 500, 3), V.bootstrap_expectancy(pnl, np.full(50, 3), 500, 3))

    def test_longshot_strategy_is_flagged_low_confidence(self):
        rep = V.build_report(self.fills, self.sett, holdout_days=2)
        self.assertTrue(rep["strategies"]["longshot"]["dev"]["flag"]["flag"])
        self.assertFalse(rep["strategies"]["edge"]["dev"]["flag"]["flag"])
        text = V.render_header(rep) + V.render(rep, "dev")
        self.assertIn("LOW CONFIDENCE ON DOLLARS", text)
        self.assertIn("58 longshot rounds", text)                   # evidence is cited in the header

    def test_thin_depth_flag(self):
        f = V.optimism_flag(np.full(10, 0.5), np.full(10, 50.0), depth=np.full(10, 10.0))
        self.assertTrue(f["flag"])
        f = V.optimism_flag(np.full(10, 0.5), np.full(10, 5.0), depth=np.full(10, 10.0))
        self.assertFalse(f["flag"])

    def test_multiple_comparisons_warns_for_many_variants(self):
        m = V.multiple_comparisons(15)
        self.assertTrue(m["warn"])
        self.assertGreater(m["t_threshold_bonferroni"], m["t_threshold_single"])
        self.assertGreater(m["expected_best_t_if_all_noise"], 1.5)
        self.assertFalse(V.multiple_comparisons(2)["warn"])
        rep = V.build_report(*synth(), holdout_days=2)
        self.assertIn("variants", V.render_header(rep))

    def test_markets_filter(self):
        f, s = self.fills.copy(), self.sett.copy()
        f.loc[f.index[::2], "asset"] = "BTC-15m"
        s.loc[s.index[::2], "asset"] = "BTC-15m"
        r5 = V.round_table(f, s, ["5m"])
        self.assertTrue(r5["asset"].str.endswith("5m").all())
        self.assertLess(len(r5), len(V.round_table(f, s)))


class ValidateCli(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = os.path.join(self.dir, "replay.db")
        con = sqlite3.connect(self.db)
        con.executescript(SCHEMA)
        f, s = synth()
        f.to_sql("fills", con, if_exists="append", index=False)
        s.to_sql("settlements", con, if_exists="append", index=False)
        con.commit()
        con.close()
        self.log = os.path.join(self.dir, "reveals.log")

    def run_cli(self, *extra):
        from tools import validate
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            validate.main(["--replay-db", self.db, "--src", self.db, "--no-depth", "--reveal-log", self.log,
                           "--holdout-days", "2", *extra])
        return buf.getvalue()

    def test_holdout_hidden_by_default_and_every_reveal_is_logged(self):
        out = self.run_cli()
        self.assertIn("hidden", out)
        self.assertNotIn("== HOLDOUT (REVEALED", out)
        self.assertFalse(os.path.exists(self.log))
        out = self.run_cli("--reveal-holdout", "--strategies", "edge")
        self.assertIn("== HOLDOUT (REVEALED; reveal #1)", out)
        out = self.run_cli("--reveal-holdout", "--strategies", "edge,noise")
        self.assertIn("reveal #2", out)
        lines = [json.loads(x) for x in open(self.log, encoding="utf-8")]
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[1]["strategies"], ["edge", "noise"])
        self.assertEqual(len(lines[0]["params_hash"]), 16)
        self.assertIn("revealed_at", lines[0])

    def test_exports(self):
        j, c, sm = (os.path.join(self.dir, n) for n in ("o.json", "o.csv", "s.json"))
        self.run_cli("--export-json", j, "--export-csv", c, "--summary-json", sm)
        self.assertIn("edge", json.load(open(j))["strategies"])
        self.assertIn("expectancy", open(c).readline())
        self.assertNotIn("holdout", json.load(open(sm))["strategies"]["edge"])


class MetricsUnchanged(unittest.TestCase):
    def test_t_stat_helper_matches_the_old_inline_formula(self):
        x = pd.Series([1.0, -2.0, 3.5, 0.5, -1.0])
        old = x.mean() / (x.std(ddof=1) / np.sqrt(len(x)))
        self.assertAlmostEqual(metrics.t_stat_of(x), old)
        self.assertTrue(np.isnan(metrics.t_stat_of(pd.Series([1.0]))))
        self.assertTrue(np.isnan(metrics.t_stat_of(pd.Series([2.0, 2.0]))))


if __name__ == "__main__":
    unittest.main()


class LongshotShareRule(unittest.TestCase):
    def test_few_fills_but_most_shares_cheap_is_flagged(self):
        prices = np.array([0.10] + [0.60] * 9)            # 10% of fills cheap ...
        shares = np.array([250.0] + [40.0] * 9)           # ... but 41% of the shares
        f = V.optimism_flag(prices, shares)
        self.assertTrue(f["flag"])
        self.assertIn("of shares", f["reasons"][0])
        self.assertFalse(V.optimism_flag(np.full(10, 0.6), np.full(10, 40.0))["flag"])
