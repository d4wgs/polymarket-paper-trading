"""
Tests for S3 v2 (buy the winning side in the last 60s) and S5 v2 (legged complete set with
take profit / stop loss / time exit).

    python -m unittest discover -s tests -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.models import DOWN, UP, Book, MarketInfo, Position, Snapshot
from strategies.base import TickContext
from strategies.s3_sniper import Sniper
from strategies.s5_complete_set import CompleteSetArb

THETA = 0.095


def _ctx(up_bid, up_ask, seconds_left, index=None, k=100_000.0, window=300, entries=0, position=None):
    """One-instrument market: the Down book mirrors the Up book."""
    info = MarketInfo("BTC-5m", "btc-updown-5m-1000", 1000, 1000 + window, "u", "d", single_instrument=True,
                      qty_step=0.01, min_notional=0.0, price_to_beat=k)
    up = Book(bids=[(up_bid, 1000)], asks=[(up_ask, 1000)])
    down = Book(bids=[(round(1 - up_ask, 6), 1000)], asks=[(round(1 - up_bid, 6), 1000)])
    snap = Snapshot(ts=info.window_end - seconds_left, market=info, up=up, down=down, index_price=index)
    return TickContext(snap=snap, position=position or Position("BTC-5m", info.slug, info.window_end),
                       cash=1000, fee_theta=THETA, entries_this_window=entries, state={})


def _holding_up(shares=100.0, cost_per_share=0.60):
    pos = Position("BTC-5m", "btc-updown-5m-1000", 1300)
    pos.add_buy(UP, shares, shares * cost_per_share)
    return pos


class SniperV2Tests(unittest.TestCase):
    def test_buys_the_winning_side_in_the_last_minute(self):
        s = Sniper("s3")
        o = s.on_tick(_ctx(0.96, 0.97, seconds_left=50, index=100_020.0))
        self.assertEqual((len(o), o[0].outcome, o[0].usd), (1, UP, 25.0))
        o = s.on_tick(_ctx(0.30, 0.31, seconds_left=50, index=99_990.0))
        self.assertEqual(o[0].outcome, DOWN)

    def test_ties_go_up_and_no_other_filters(self):
        o = Sniper("s3").on_tick(_ctx(0.50, 0.51, seconds_left=10, index=100_000.0))   # coin flip, still buys
        self.assertEqual(o[0].outcome, UP)

    def test_only_last_60s_and_once_per_round(self):
        s = Sniper("s3")
        self.assertEqual(s.on_tick(_ctx(0.96, 0.97, seconds_left=61, index=100_020.0)), [])
        self.assertEqual(s.on_tick(_ctx(0.96, 0.97, seconds_left=40, index=100_020.0, entries=1)), [])

    def test_15m_rounds_too(self):
        o = Sniper("s3").on_tick(_ctx(0.96, 0.97, seconds_left=59, index=100_020.0, window=900))
        self.assertEqual(len(o), 1)


class CompleteSetV2Tests(unittest.TestCase):
    def test_leg1_is_the_pricier_side_after_15s(self):
        s = CompleteSetArb("s5")
        self.assertEqual(s.on_tick(_ctx(0.60, 0.62, seconds_left=300 - 14)), [])
        o = s.on_tick(_ctx(0.60, 0.62, seconds_left=300 - 15))
        self.assertEqual((o[0].outcome, o[0].side, o[0].usd), (UP, "BUY", 50.0))
        o = s.on_tick(_ctx(0.30, 0.32, seconds_left=200))            # Down (0.70) is pricier
        self.assertEqual(o[0].outcome, DOWN)
        self.assertEqual(s.on_tick(_ctx(0.30, 0.32, seconds_left=200, entries=1)), [])   # once per round

    # ---- pnl_basis: locked (whole set, fees and spread included)
    def test_locked_take_profit(self):
        # leg 1: Up at 0.60 all-in. Up now 0.70/0.71 -> Down ask 0.30: set 0.60 + 0.30 + fee ~0.92 -> +13%
        ctx = _ctx(0.70, 0.71, seconds_left=150, position=_holding_up())
        o = CompleteSetArb("s5", pnl_basis="locked").on_tick(ctx)
        self.assertEqual((o[0].outcome, o[0].shares, o[0].meta["exit"]), (DOWN, 100.0, "take_profit"))
        lim = o[0].limit
        self.assertGreaterEqual(1 - (0.60 + lim + THETA * lim * (1 - lim)), 0.10 * 0.60 - 1e-9)   # still +10% at the limit

    def test_locked_stop_loss_fires_on_spread_and_fees_alone(self):
        # unchanged market right after entry (Up 0.59/0.60): spread + two fees already lock about -6%
        o = CompleteSetArb("s5", pnl_basis="locked").on_tick(_ctx(0.59, 0.60, seconds_left=200, position=_holding_up()))
        self.assertEqual(o[0].meta["exit"], "stop_loss")

    # ---- pnl_basis: price (default): leg 1's price move, fees left out
    def test_entry_price_strips_the_fee(self):
        ctx = _ctx(0.59, 0.60, seconds_left=200)
        e = CompleteSetArb.entry_price(ctx, 0.60 + THETA * 0.60 * 0.40)
        self.assertAlmostEqual(e, 0.60, places=9)

    def test_price_take_profit(self):
        # entry ~0.5786 (0.60 all-in); Up bid 0.65 -> +12% -> leg 2 (Down) at 0.35
        o = CompleteSetArb("s5").on_tick(_ctx(0.65, 0.66, seconds_left=150, position=_holding_up()))
        self.assertEqual((o[0].outcome, o[0].meta["exit"]), (DOWN, "take_profit"))
        e = CompleteSetArb.entry_price(_ctx(0.5, 0.5, 100), 0.60)
        self.assertGreaterEqual((1 - o[0].limit - e) / e, 0.10 - 1e-3)       # limit keeps the +10%

    def test_price_stop_loss(self):
        o = CompleteSetArb("s5").on_tick(_ctx(0.50, 0.51, seconds_left=150, position=_holding_up()))
        self.assertEqual(o[0].meta["exit"], "stop_loss")

    def test_price_does_not_stop_out_right_after_entry(self):
        self.assertEqual(CompleteSetArb("s5").on_tick(_ctx(0.58, 0.59, seconds_left=200, position=_holding_up())), [])

    def test_hold_in_between_then_time_exit(self):
        s = CompleteSetArb("s5")
        self.assertEqual(s.on_tick(_ctx(0.58, 0.59, seconds_left=31, position=_holding_up())), [])
        o = s.on_tick(_ctx(0.58, 0.59, seconds_left=30, position=_holding_up()))
        self.assertEqual((o[0].outcome, o[0].meta["exit"]), (DOWN, "time"))

    def test_no_reentry_after_the_set_is_closed(self):
        self.assertEqual(CompleteSetArb("s5").on_tick(_ctx(0.60, 0.62, seconds_left=200, entries=1)), [])


class EngineCloseTests(unittest.TestCase):
    def test_leg2_on_us_closes_leg1(self):
        """Buying the opposite leg on a one-instrument market is booked as selling leg 1."""
        from core.broker import PaperBroker
        from core.fees import USFees
        b = PaperBroker("s5", 1000, fee_model=USFees())
        ctx = _ctx(0.60, 0.62, seconds_left=280)
        books = {UP: ctx.up.copy(), DOWN: ctx.down.copy()}
        b.execute_many(CompleteSetArb("s5").on_tick(ctx)[0], ctx.snap, books)
        held = b.positions[ctx.snap.market.slug].up
        ctx2 = _ctx(0.75, 0.76, seconds_left=150, position=b.positions[ctx.snap.market.slug])
        order = CompleteSetArb("s5").on_tick(ctx2)[0]
        fills = b.execute_many(order, ctx2.snap, {UP: ctx2.up.copy(), DOWN: ctx2.down.copy()})
        self.assertEqual([(f.outcome, f.side) for f in fills], [(UP, "SELL")])
        self.assertAlmostEqual(fills[0].shares, held)
        self.assertTrue(b.positions[ctx.snap.market.slug].is_empty() or b.positions[ctx.snap.market.slug].up < 1e-9)
        self.assertGreater(b.cash, 1000)          # +10% locked in, after both fees


if __name__ == "__main__":
    unittest.main()
