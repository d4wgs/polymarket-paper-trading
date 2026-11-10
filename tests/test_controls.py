"""
No-signal controls (strategies/controls.py) and the paired strategy-minus-control comparison.

    python -m unittest discover -s tests -v
"""
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

from core import validation as V
from core.models import DOWN, UP, Book, MarketInfo, Position, Snapshot
from strategies.base import TickContext
from strategies.controls import (BucketsFavorite, MomentumFavorite, ShockRandom, SniperFavorite, favorite_by_ask,
                                 seeded_side)
from strategies.s3_sniper import Sniper
from strategies.s4_momentum import CrowdMomentum, MinuteBuckets

THETA = 0.095


def snap(ts, up_ask, start=9600, length=300, index=None, k=100.0, fv=None, spread=0.01):
    m = MarketInfo(f"BTC-{length // 60}m", f"btc-updown-{length // 60}m-{start}", start, start + length, "u", "d",
                   single_instrument=True, qty_step=0.01, min_notional=0.0, price_to_beat=k)
    bid = round(up_ask - spread, 6)
    up = Book(bids=[(bid, 1000.0)], asks=[(up_ask, 1000.0)])
    down = Book(bids=[(round(1 - up_ask, 6), 1000.0)], asks=[(round(1 - bid, 6), 1000.0)])
    return Snapshot(ts=ts, market=m, up=up, down=down, index_price=index, fv=fv)


def ctx(s, entries=0, history=(), md=None, position=None, state=None):
    pos = position or Position(s.market.asset, s.market.slug, s.market.window_end)
    return TickContext(snap=s, position=pos, cash=1000.0, fee_theta=THETA, state={} if state is None else state,
                       entries_this_window=entries, history=list(history), md=md)


class SniperControlTests(unittest.TestCase):
    def test_same_timing_gates_and_size_as_the_sniper_with_the_signal_removed(self):
        for params in ({}, {"min_price": 0.70}, {"min_price": 0.70, "max_price": 0.97}):
            real, ctl = Sniper("s", **params), SniperFavorite("c", **params)
            for left in (120, 61, 60.5, 60, 59, 30, 5, 1, 0.5):
                for up_ask in (0.30, 0.55, 0.75, 0.98):
                    # the index agrees with the market here, so the real sniper and the control must act alike
                    s = snap(9900 - left, up_ask, index=101.0 if up_ask >= 0.5 else 99.0)
                    a, b = real.on_tick(ctx(s)), ctl.on_tick(ctx(s))
                    self.assertEqual(len(a), len(b), (params, left, up_ask))
                    if a:
                        self.assertEqual((a[0].outcome, a[0].usd, a[0].limit, a[0].side),
                                         (b[0].outcome, b[0].usd, b[0].limit, b[0].side))
            self.assertEqual(ctl.on_tick(ctx(snap(9850, 0.75, index=101.0), entries=1)), [])     # one entry per round

    def test_control_ignores_the_index_and_the_model(self):
        ctl, real = SniperFavorite("c"), Sniper("s")
        s = snap(9850, 0.30, index=101.0)                    # index says Up is winning; the market prices Down at .71
        self.assertEqual(real.on_tick(ctx(s))[0].outcome, UP)
        self.assertEqual(ctl.on_tick(ctx(s))[0].outcome, DOWN)
        self.assertEqual(ctl.on_tick(ctx(snap(9850, 0.30, index=None, k=None)))[0].outcome, DOWN)   # needs neither
        self.assertEqual(favorite_by_ask(ctx(snap(9850, 0.505)))[0], UP)                 # .505 vs .505: tie -> Up


class MomentumControlTests(unittest.TestCase):
    PARAMS = dict(min_side_price=0.70, max_entry=0.90, skip_first_seconds=30, skip_last_seconds=10)

    def md(self, open_px, now_px):
        return types.SimpleNamespace(spot_at=lambda ts: open_px)

    def test_same_window_and_band_as_s4_without_confirmation(self):
        real, ctl = CrowdMomentum("s", **self.PARAMS), MomentumFavorite("c", **self.PARAMS)
        fv = types.SimpleNamespace(oracle=101.0, K=100.0, of=lambda o: 0.8, basis=0.0)
        for elapsed in (5, 29, 30, 31, 150, 289, 290, 291, 299):
            for up_ask in (0.50, 0.69, 0.75, 0.90, 0.92):
                s = snap(9600 + elapsed, up_ask, fv=fv)
                s.spot = 101.0                                # spot and oracle both confirm Up
                a = real.on_tick(ctx(s, md=self.md(100.0, 101.0)))
                b = ctl.on_tick(ctx(s))                       # the control needs no market data hub at all
                self.assertEqual(len(a), len(b), (elapsed, up_ask))
                if a:
                    self.assertEqual((a[0].outcome, a[0].usd, a[0].limit), (b[0].outcome, b[0].usd, b[0].limit))

    def test_control_trades_when_spot_disagrees(self):
        real, ctl = CrowdMomentum("s", **self.PARAMS), MomentumFavorite("c", **self.PARAMS)
        fv = types.SimpleNamespace(oracle=99.0, K=100.0, of=lambda o: 0.8, basis=0.0)
        s = snap(9700, 0.80, fv=fv)
        s.spot = 99.0                                         # spot and oracle say Down; the crowd says Up
        self.assertEqual(real.on_tick(ctx(s, md=self.md(100.0, 99.0))), [])
        o = ctl.on_tick(ctx(s))
        self.assertEqual((o[0].outcome, o[0].usd, o[0].limit), (UP, 25.0, 0.90))
        self.assertEqual(ctl.on_tick(ctx(s, entries=1)), [])


class BucketsControlTests(unittest.TestCase):
    def test_same_entry_window_as_s4b_without_the_bucket_test(self):
        real = MinuteBuckets("s", buckets=3, max_entry=0.85, entry_window_seconds=10, min_side_price=0.50)
        ctl = BucketsFavorite("c", max_entry=0.85, entry_window_seconds=10, min_side_price=0.50)
        rising = types.SimpleNamespace(spot_at=lambda ts: 100.0 + (ts - 9600) / 60.0)     # every bucket up
        for ts in (9830, 9839.9, 9840, 9845, 9850, 9850.1, 9870):
            for up_ask in (0.45, 0.60, 0.85, 0.86):
                s = snap(ts, up_ask)
                a, b = real.on_tick(ctx(s, md=rising)), ctl.on_tick(ctx(s))
                if up_ask >= 0.51:                            # Up is the favourite and the buckets say Up too
                    self.assertEqual(len(a), len(b), (ts, up_ask))
                    if a:
                        self.assertEqual((a[0].outcome, a[0].usd, a[0].limit), (b[0].outcome, b[0].usd, b[0].limit))
                else:
                    self.assertEqual(a, [])                   # buckets say Up but its mid is under 50c
        flat = types.SimpleNamespace(spot_at=lambda ts: 100.0)
        s = snap(9842, 0.70)
        self.assertEqual(real.on_tick(ctx(s, md=flat)), [])   # no bucket signal ...
        self.assertEqual(ctl.on_tick(ctx(s))[0].outcome, UP)  # ... the control still buys the favourite
        self.assertEqual(ctl.on_tick(ctx(snap(9842, 0.30)))[0].outcome, DOWN)
        self.assertEqual(ctl.on_tick(ctx(snap(10442, 0.70, start=9600, length=900))), [])   # 5-minute rounds only


class ShockControlTests(unittest.TestCase):
    def test_seeded_side_is_fixed_per_round_and_roughly_balanced(self):
        sides = [seeded_side(12345, f"btc-updown-5m-{9600 + 300 * i}") for i in range(400)]
        self.assertEqual(sides, [seeded_side(12345, f"btc-updown-5m-{9600 + 300 * i}") for i in range(400)])
        self.assertTrue(150 < sides.count(UP) < 250)
        self.assertNotEqual(sides, [seeded_side(999, f"btc-updown-5m-{9600 + 300 * i}") for i in range(400)])

    def test_enters_on_the_shock_with_no_model_and_exits_on_the_time_stop(self):
        ctl = ShockRandom("c")
        before, now = snap(9700, 0.50), snap(9708, 0.64)      # Up mid +14c in 8s
        state = {}
        o = ctl.on_tick(ctx(now, history=[before], state=state))
        want = seeded_side(12345, now.market.slug)
        self.assertEqual((len(o), o[0].outcome, o[0].usd, o[0].side), (1, want, 25.0, "BUY"))
        self.assertEqual(ctl.on_tick(ctx(snap(9708, 0.55), history=[before])), [])          # +5c: no shock
        self.assertEqual(ctl.on_tick(ctx(snap(9890, 0.64), history=[snap(9884, 0.50)])), [])  # last 15s: no entry
        pos = Position(now.market.asset, now.market.slug, 9900)
        pos.add_buy(want, 40.0, 25.0)
        self.assertEqual(ctl.on_tick(ctx(snap(9740, 0.60), entries=1, position=pos, state=state)), [])
        out = ctl.on_tick(ctx(snap(9769, 0.60), entries=1, position=pos, state=state))
        self.assertEqual((out[0].side, out[0].outcome), ("SELL", want))
        self.assertEqual(ctl.on_tick(ctx(snap(9780, 0.80), entries=1, history=[snap(9772, 0.60)], state=state)), [])


def rounds_df(rows):
    return pd.DataFrame([{"strategy": s, "slug": f"r-{start}", "asset": "BTC-5m", "start": start, "pnl": pnl,
                          "n_buys": 1, "shares": 50.0, "usd": 25.0, "cost": 25.0, "fees": 0.0}
                         for s, start, pnl in rows])


class PairedTests(unittest.TestCase):
    def test_difference_uses_identical_rounds_only(self):
        r = rounds_df([("s", 100, 5.0), ("s", 200, -25.0), ("s", 300, 5.0), ("s", 400, 5.0),
                       ("c", 100, 2.0), ("c", 200, -25.0), ("c", 300, -25.0), ("c", 500, 9.0), ("c", 600, 9.0)])
        p = V.paired_vs_control(r, "s", "c", n_boot=500, seed=1)
        self.assertEqual((p["rounds"], p["strategy_only"], p["control_only"]), (3, 1, 2))   # rounds 100, 200, 300
        self.assertAlmostEqual(p["strategy_pnl"], 5.0 - 25.0 + 5.0)          # round 400 (strategy only) is not in it
        self.assertAlmostEqual(p["control_pnl"], 2.0 - 25.0 - 25.0)          # rounds 500/600 (control only) neither
        self.assertAlmostEqual(p["diff_total"], 3.0 + 0.0 + 30.0)
        self.assertAlmostEqual(p["diff_per_round"], 11.0)
        self.assertLessEqual(p["ci_lo"], 11.0)
        self.assertGreaterEqual(p["ci_hi"], 11.0)
        self.assertEqual(p, V.paired_vs_control(r, "s", "c", n_boot=500, seed=1))          # fixed seed
        none = V.paired_vs_control(r[r["start"] > 350], "s", "c")
        self.assertEqual((none["rounds"], none["strategy_only"], none["control_only"]), (0, 1, 2))
        self.assertIn("no common rounds", V.render_paired([none]))
        self.assertIn("+11.000", V.render_paired([p]))

    def test_bootstrap_resamples_rounds(self):
        lo, hi = V.bootstrap_mean(np.array([1.0] * 50 + [-1.0] * 50), 2000, 7)
        self.assertTrue(-0.5 < lo < 0 < hi < 0.5)
        self.assertEqual(V.bootstrap_mean(np.array([3.0] * 20), 200, 7), (3.0, 3.0))


class ConfigTests(unittest.TestCase):
    def test_every_control_matches_its_strategys_window_band_and_stake(self):
        import config
        by = {s.name: s for s in config.STRATEGIES}
        self.assertGreaterEqual(len(config.CONTROLS), 10)
        same = {"Sniper": ("snipe_seconds", "min_seconds_left", "min_price", "max_price", "stake_usd"),
                "CrowdMomentum": ("min_side_price", "max_entry", "skip_first_seconds", "skip_last_seconds", "stake_usd"),
                "MinuteBuckets": ("max_entry", "entry_window_seconds", "stake_usd"),
                "ShockReversion": ("shock_size", "shock_lookback", "time_stop_seconds", "skip_last_seconds", "stake_usd")}
        for s_name, c_name in config.CONTROLS.items():
            s, c = by[s_name], by[c_name]
            for key in same[type(s).__name__]:
                if key == "min_price" and c.p[key] is None and (s.p[key] or 0) <= 0.5:
                    continue      # the favourite's ask is always >= 50c (the two asks sum to >= $1): the floor is implied
                self.assertEqual(s.p[key], c.p[key], (s_name, c_name, key))
            self.assertTrue(set(s.assets) <= set(c.assets), (s_name, c_name))          # the control trades every market it does
            self.assertTrue(c_name.startswith("ctl_"))
        self.assertEqual(by["ctl_s6_random"].p["seed"], 12345)
        # controls are expected to lose steadily: a bankroll big enough that the $200 stop never ends the comparison
        for c_name in set(config.CONTROLS.values()):
            self.assertEqual(by[c_name].bankroll, 25000, c_name)
            self.assertEqual(by[c_name].p["stake_usd"], 25, c_name)          # ... while the bet size is unchanged
        for s_name in config.CONTROLS:
            self.assertIsNone(by[s_name].bankroll, s_name)                  # strategies keep the default $1,000
        # S4b v2's 50c floor is the control's; v1 has none (the control is the same for both by design)
        self.assertEqual(by["ctl_s4b_fav"].p["min_side_price"], 0.50)


if __name__ == "__main__":
    unittest.main()
