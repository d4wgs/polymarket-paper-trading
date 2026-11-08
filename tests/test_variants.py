"""
Tests for the 2026-11-08 variants: sniper gates / cap / model signal, S4b market gate, the config `class:`
option, and the per-market (5m / 15m) scorecard.

    python -m unittest discover -s tests -v
"""
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from core import analytics
from core.models import DOWN, UP
from strategies.s3_sniper import Sniper
from strategies.s4_momentum import MinuteBuckets
from tests.test_s3_s5_v2 import _ctx


def _fv(p_up, S=100_050.0, K=100_000.0):
    return types.SimpleNamespace(p_up=p_up, p_down=1 - p_up, S=S, K=K, k_model=K, s_source="spot", oracle=None)


class SniperGateTests(unittest.TestCase):
    def test_v2_defaults_have_no_gate(self):
        o = Sniper("s").on_tick(_ctx(0.01, 0.02, seconds_left=30, index=100_020.0))   # buys a 2c "winner"
        self.assertEqual(len(o), 1)

    def test_floor_skips_cheap_winners_and_retries(self):
        s = Sniper("s", min_price=0.50)
        self.assertEqual(s.on_tick(_ctx(0.30, 0.32, seconds_left=40, index=100_020.0)), [])
        o = s.on_tick(_ctx(0.60, 0.62, seconds_left=30, index=100_020.0))             # price moved into range
        self.assertEqual((len(o), o[0].outcome), (1, UP))

    def test_floor_applies_to_the_down_side_too(self):
        s = Sniper("s", min_price=0.70)
        # index below K -> Down; Down ask = 1 - up_bid = 0.30 -> below the floor
        self.assertEqual(s.on_tick(_ctx(0.70, 0.72, seconds_left=30, index=99_980.0)), [])
        o = s.on_tick(_ctx(0.10, 0.12, seconds_left=30, index=99_980.0))              # Down ask 0.90
        self.assertEqual(o[0].outcome, DOWN)

    def test_cap_skips_expensive_and_limits_the_fill(self):
        s = Sniper("s", min_price=0.70, max_price=0.97)
        self.assertEqual(s.on_tick(_ctx(0.97, 0.98, seconds_left=20, index=100_020.0)), [])
        o = s.on_tick(_ctx(0.94, 0.95, seconds_left=20, index=100_020.0))
        self.assertAlmostEqual(o[0].limit, 0.97)       # ask 0.95 + 2c, never above the cap

    def test_model_signal_overrides_a_lagging_index(self):
        # index says Up (above K) but the live model says Down (p_up 0.2)
        ctx = _ctx(0.20, 0.22, seconds_left=30, index=100_020.0)
        ctx.snap.fv = _fv(0.2, S=99_960.0)
        self.assertEqual(Sniper("s").on_tick(ctx)[0].outcome, UP)                      # v2: follows the index
        o = Sniper("s", signal="model").on_tick(ctx)
        self.assertEqual(o[0].outcome, DOWN)

    def test_model_signal_falls_back_to_index(self):
        o = Sniper("s", signal="model").on_tick(_ctx(0.60, 0.62, seconds_left=30, index=100_020.0))   # no fv
        self.assertEqual(o[0].outcome, UP)

    def test_bad_signal_rejected(self):
        with self.assertRaises(ValueError):
            Sniper("s", signal="oracle")


class _MD:
    def __init__(self, marks):
        self.marks = marks

    def spot_at(self, ts):
        return self.marks.get(int(ts) - 1000)


class MinuteBucketGateTests(unittest.TestCase):
    def _ctx(self, up_bid, up_ask):
        c = _ctx(up_bid, up_ask, seconds_left=58)
        c.md = _MD({0: 100.0, 60: 101.0, 120: 102.0, 180: 103.0})     # three rising minutes -> Up
        return c

    def test_v1_buys_a_cheap_side(self):
        self.assertEqual(len(MinuteBuckets("b").on_tick(self._ctx(0.19, 0.21))), 1)

    def test_gate_blocks_when_market_disagrees(self):
        b = MinuteBuckets("b", min_side_price=0.50)
        self.assertEqual(b.on_tick(self._ctx(0.19, 0.21)), [])
        self.assertEqual(len(b.on_tick(self._ctx(0.69, 0.71))), 1)


class ConfigTests(unittest.TestCase):
    def test_class_option_builds_variants(self):
        import config
        names = {s.name: s for s in config.STRATEGIES}
        self.assertIn("s3_sniper_v2", names)
        for n in ("s3_sniper_v3a_floor50", "s3_sniper_v3b_floor70", "s3_sniper_v3c_floor70_cap97",
                  "s3_sniper_v3d_model_floor50"):
            self.assertIsInstance(names[n], Sniper)
        self.assertEqual(names["s3_sniper_v3b_floor70"].p["min_price"], 0.70)
        self.assertEqual(names["s3_sniper_v3d_model_floor50"].p["signal"], "model")
        self.assertEqual(names["s4_momentum_edge0_v1"].p["min_edge"], 0.0)
        self.assertEqual(names["s4_momentum_min80_v1"].p["min_side_price"], 0.80)
        self.assertEqual(names["s4_momentum_15m_v1"].assets, ["BTC-15m"])
        self.assertEqual(names["s4b_minute_buckets_v2"].p["min_side_price"], 0.50)
        self.assertEqual(len(names), len(config.STRATEGIES))                           # names are unique


class ScorecardByKindTests(unittest.TestCase):
    def test_splits_5m_and_15m(self):
        fills = pd.DataFrame([
            dict(ts=1, strategy="a", asset="BTC-5m", slug="s5", outcome="UP", side="BUY", shares=10.0, avg_price=0.5,
                 usd=5.0, fee=0.1, liquidity="taker", fair_value=0.5, edge=0.0, seconds_left=30.0, rebate=0.0),
            dict(ts=2, strategy="a", asset="BTC-15m", slug="s15", outcome="UP", side="BUY", shares=10.0, avg_price=0.5,
                 usd=5.0, fee=0.1, liquidity="taker", fair_value=0.5, edge=0.0, seconds_left=30.0, rebate=0.0)])
        sett = pd.DataFrame([dict(ts=3, strategy="a", asset="BTC-5m", slug="s5", winner="UP", up_shares=10.0,
                                  down_shares=0.0, net_cost=5.1, payout=10.0, pnl=4.9),
                             dict(ts=4, strategy="a", asset="BTC-15m", slug="s15", winner="DOWN", up_shares=10.0,
                                  down_shares=0.0, net_cost=5.1, payout=0.0, pnl=-5.1)])
        res = pd.DataFrame([dict(slug="s5", winner="UP"), dict(slug="s15", winner="DOWN")])
        lab = {"fills": fills, "settlements": sett, "resolutions": res, "events": pd.DataFrame()}
        out = analytics.scorecard_by_kind(lab, ["a"])
        got = {r.kind: (r.trades, round(r.pnl_after_fees, 2)) for r in out.itertuples()}
        self.assertEqual(got, {"5m": (1, 4.9), "15m": (1, -5.1)})


if __name__ == "__main__":
    unittest.main()
