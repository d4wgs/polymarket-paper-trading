"""
Tests for the like-for-like basis (the served index is a 60s average) and S1 v2's guards.

    python -m unittest discover -s tests -v
"""
import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.fairvalue import FairValue, MarketData
from core.models import DOWN, UP, Book, MarketInfo, Position, Snapshot
from strategies.base import TickContext
from strategies.s1_fair_value import FairValueTrader


class RampSpot:
    """Spot that rose linearly by $1/s for the last 60s and sits at `now_px` now."""
    def __init__(self, now, now_px, slope=1.0):
        self.now, self.now_px, self.slope = now, now_px, slope
    def price(self, now): return self.now_px
    def price_at(self, ts): return self.now_px - self.slope * (self.now - ts)
    def sigma(self): return 2e-5


class BasisTests(unittest.TestCase):
    def test_trend_is_not_basis(self):
        # Spot ramps +$60 over a minute. A 60s-average index of the SAME prices sits ~$30 below spot:
        # the old instantaneous comparison called that a 0.03% basis; like for like it is ~0.
        now = 10_000.0
        spot = RampSpot(now, 100_000.0)
        md = MarketData(spot, {"oracle_average_seconds": 60})
        idx = sum(spot.price_at(now - i) for i in range(60)) / 60
        self.assertAlmostEqual(md.basis(now, idx), 0.0, places=12)
        naive = (100_000.0 - idx) / idx
        self.assertGreater(naive, 2.5e-4)

    def test_constant_offset_is_basis(self):
        now = 10_000.0
        spot = RampSpot(now, 100_000.0, slope=0.0)
        md = MarketData(spot, {"oracle_average_seconds": 60})
        self.assertAlmostEqual(md.basis(now, 100_010.0), -10 / 100_010, places=12)

    def test_k_moved_into_spot_terms(self):
        # spot runs $10 under the index; spot == K - 10 means "at the money", not "below K"
        now = 1100.0
        spot = RampSpot(now, 99_990.0, slope=0.0)
        info = MarketInfo("BTC-5m", "btc-updown-5m-1000", 1000, 1300, "u", "d", price_to_beat=100_000.0)
        cfg = {"s_source": "spot", "oracle_average_seconds": 60}
        md = MarketData(spot, cfg)
        md.on_oracle(now, 100_000.0)
        fv = md.compute(info, now)
        self.assertAlmostEqual(fv.p_up, 0.5, places=6)
        self.assertAlmostEqual(fv.k_model, 99_990.0, places=6)
        off = MarketData(spot, {**cfg, "basis_adjust": False})
        off.on_oracle(now, 100_000.0)
        self.assertLess(off.compute(info, now).p_up, 0.45)

    def test_not_enough_history_is_none(self):
        class Short(RampSpot):
            def price_at(self, ts): return self.now_px if self.now - ts < 10 else None
        md = MarketData(Short(100.0, 50_000.0), {"oracle_average_seconds": 60})
        self.assertIsNone(md.basis(100.0, 50_000.0))


def _ctx(p_up, up_bid, up_ask, spot_then=None, spot=100.0, sigma=1e-4, seconds_left=200):
    info = MarketInfo("BTC-5m", "btc-updown-5m-1000", 1000, 1300, "u", "d", single_instrument=True,
                      qty_step=0.01, min_notional=0.0, price_to_beat=100.0)
    now = info.window_end - seconds_left
    fv = FairValue(p_up=p_up, p_down=1 - p_up, sigma=sigma, t=seconds_left, S=spot, K=100, z=0, spot=spot)
    up = Book(bids=[(up_bid, 500)], asks=[(up_ask, 500)])
    down = Book(bids=[(round(1 - up_ask, 6), 500)], asks=[(round(1 - up_bid, 6), 500)])

    class MD:
        def spot_at(self, ts): return spot_then if spot_then is not None else spot
    snap = Snapshot(ts=now, market=info, up=up, down=down, fv=fv, spot=spot)
    return TickContext(snap=snap, position=Position("BTC-5m", info.slug, 1300), cash=1000,
                       fee_theta=0.095, md=MD(), state={})


class S1V2Tests(unittest.TestCase):
    def test_trades_a_real_disagreement(self):
        s = FairValueTrader("s1")
        ctx = _ctx(p_up=0.70, up_bid=0.49, up_ask=0.50)       # blended fair 0.6025 -> edge ~0.079
        orders = s.on_tick(ctx)
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0].outcome, UP)
        fair = 0.5 * 0.70 + 0.5 * 0.495
        lim = orders[0].limit
        self.assertGreaterEqual(fair - lim - 0.095 * lim * (1 - lim), 0.04 - 1e-9)

    def test_shrinkage_kills_small_model_edges(self):
        # v1 would buy (model edge 0.054), but half-way to the market it is only ~0.007
        ctx = _ctx(p_up=0.58, up_bid=0.49, up_ask=0.50)
        self.assertEqual(FairValueTrader("s1").on_tick(ctx), [])
        self.assertEqual(len(FairValueTrader("v1", market_weight=0).on_tick(ctx)), 1)
        self.assertIn("s1_skipped", [k for k, _ in ctx.events])

    def test_huge_gap_is_skipped(self):
        ctx = _ctx(p_up=0.96, up_bid=0.45, up_ask=0.46)        # model 51c away from the market
        self.assertEqual(FairValueTrader("s1").on_tick(ctx), [])
        self.assertEqual(ctx.events[0][1]["why"], "model_gap")

    def test_longshot_is_skipped(self):
        ctx = _ctx(p_up=0.25, up_bid=0.06, up_ask=0.07)
        self.assertEqual(FairValueTrader("s1").on_tick(ctx), [])

    def test_jump_guard(self):
        # spot fell 0.2% in 5s with sigma 1e-4/s: ~9 sigmas -> the book is stale, don't trade
        ctx = _ctx(p_up=0.70, up_bid=0.49, up_ask=0.50, spot=100.0, spot_then=100.2)
        self.assertEqual(FairValueTrader("s1").on_tick(ctx), [])
        self.assertEqual(ctx.events[0][1]["why"], "jump")
        ctx = _ctx(p_up=0.70, up_bid=0.49, up_ask=0.50, spot=100.0, spot_then=100.2)
        self.assertEqual(len(FairValueTrader("s1", jump_guard_sigmas=None).on_tick(ctx)), 1)

    def test_one_sided_book_is_skipped(self):
        ctx = _ctx(p_up=0.70, up_bid=0.49, up_ask=0.50)
        ctx.snap.up.bids = []
        self.assertEqual([o for o in FairValueTrader("s1").on_tick(ctx) if o.outcome == UP], [])


if __name__ == "__main__":
    unittest.main()
