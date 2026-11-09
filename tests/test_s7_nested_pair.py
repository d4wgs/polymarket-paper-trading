"""
Tests for S7 (nested 5m / 15m pair) and the engine's read-only view of the other markets (ctx.others).

    python -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.models import DOWN, UP, Book, MarketInfo, Position, Snapshot
from strategies.base import OtherMarket, TickContext
from strategies.s7_nested_pair import NestedPair, pair_walk

THETA = 0.095
fee = lambda p: THETA * p * (1 - p)

S15, S5, END = 9000, 9600, 9900        # 15m round 9000-9900; the 5m round 9600-9900 closes with it


def _info(kind, k):
    start = S15 if kind == "15m" else S5
    return MarketInfo(f"BTC-{kind}", f"btc-updown-{kind}-{start}", start, END, "u", "d", single_instrument=True,
                      qty_step=0.01, min_notional=0.0, price_to_beat=k)


def _books(up_ask, depth=1000, up_bid=None):
    """One instrument: Down ask = 1 - Up bid, Down bid = 1 - Up ask."""
    up_bid = round(up_ask - 0.01, 6) if up_bid is None else up_bid
    up = Book(bids=[(up_bid, depth)], asks=[(up_ask, depth)])
    down = Book(bids=[(round(1 - up_ask, 6), depth)], asks=[(round(1 - up_bid, 6), depth)])
    return up, down


def _snap(kind, k, up_ask, ts, depth=1000):
    up, down = _books(up_ask, depth)
    return Snapshot(ts=ts, market=_info(kind, k), up=up, down=down)


def _ctx(me, other, pos_me=None, pos_other=None):
    pos_me = pos_me or Position(me.market.asset, me.market.slug, END)
    pos_other = pos_other or Position(other.market.asset, other.market.slug, END)
    return TickContext(snap=me, position=pos_me, cash=1000, fee_theta=THETA, state={},
                       others={other.market.asset: OtherMarket(other, pos_other)})


def _holding(snap, side, shares, cost_ps):
    p = Position(snap.market.asset, snap.market.slug, END)
    p.add_buy(side, shares, shares * cost_ps)
    return p


class PairWalkTests(unittest.TestCase):
    def test_takes_levels_while_pair_is_cheap_enough(self):
        a = [(0.40, 10), (0.45, 10)]
        b = [(0.50, 15), (0.60, 100)]
        n, cost, wa, wb = pair_walk(a, b, fee, 0.99, 1000)
        # level 1: .40+.50 (+fees .0228+.0238 = .9466) for 10; then .45+.50 = .9973 > .99 -> stop
        self.assertAlmostEqual(n, 10)
        self.assertAlmostEqual(cost, 10 * (0.40 + fee(0.40) + 0.50 + fee(0.50)))
        self.assertEqual((wa, wb), (0.40, 0.50))

    def test_budget_caps_contracts(self):
        n, cost, _, _ = pair_walk([(0.40, 1000)], [(0.50, 1000)], fee, 0.99, 50)
        self.assertLessEqual(cost, 50 + 1e-9)
        self.assertAlmostEqual(n, 52.82, places=2)


class NestedPairTests(unittest.TestCase):
    def test_sides_follow_the_higher_price_to_beat(self):
        s = NestedPair("s7")
        # K5 > K15 -> 15m UP + 5m DOWN
        c15 = _ctx(_snap("15m", 100.0, 0.40, 9700), _snap("5m", 110.0, 0.50, 9700))
        self.assertEqual(s.sides(c15, s.partner(c15)), (UP, DOWN))
        c5 = _ctx(_snap("5m", 110.0, 0.50, 9700), _snap("15m", 100.0, 0.40, 9700))
        self.assertEqual(s.sides(c5, s.partner(c5)), (DOWN, UP))
        # K5 < K15 -> 15m DOWN + 5m UP
        c = _ctx(_snap("15m", 110.0, 0.40, 9700), _snap("5m", 100.0, 0.50, 9700))
        self.assertEqual(s.sides(c, s.partner(c)), (DOWN, UP))

    def test_leg1_when_pair_is_under_99c(self):
        s = NestedPair("s7", leg1_market=None)
        # 15m UP ask .40, 5m DOWN ask = 1 - 5m Up bid (.50) = .50 -> pair .9466 all-in
        ctx = _ctx(_snap("15m", 100.0, 0.40, 9700), _snap("5m", 110.0, 0.51, 9700))
        o = s.on_tick(ctx)
        self.assertEqual(len(o), 1)
        self.assertEqual(o[0].outcome, UP)
        self.assertEqual(o[0].limit, 0.40)
        self.assertGreater(o[0].shares, 50)
        self.assertEqual(o[0].meta["pair_leg"], 1)

    def test_leg1_only_on_the_configured_market(self):
        me, other = _snap("15m", 100.0, 0.40, 9700), _snap("5m", 110.0, 0.51, 9700)
        self.assertEqual(NestedPair("s7").on_tick(_ctx(me, other)), [])          # default: 5m only
        o = NestedPair("s7").on_tick(_ctx(other, me))
        self.assertEqual((len(o), o[0].outcome), (1, DOWN))

    def test_ev_leg2_cap_covers_the_opening_limit(self):
        s = NestedPair("s7ev", band_ev=True, max_ev_pair_cost=1.05, complete_slack=0.03)
        other = _snap("15m", 100.0, 0.40, 9700)
        held = _holding(other, UP, 60.0, 0.55)                       # opened at ~1.05 (EV): leg 2 must still be allowed
        o = s.on_tick(_ctx(_snap("5m", 110.0, 0.51, 9702), other, pos_other=held))
        self.assertEqual(len(o), 1)
        self.assertLessEqual(0.55 + o[0].limit + fee(o[0].limit), 1.08 + 1e-9)

    def test_no_trade_when_pair_too_expensive_or_partner_stale_or_not_nested(self):
        s = NestedPair("s7")
        self.assertEqual(s.on_tick(_ctx(_snap("15m", 100.0, 0.48, 9700), _snap("5m", 110.0, 0.51, 9700))), [])
        self.assertEqual(s.on_tick(_ctx(_snap("15m", 100.0, 0.40, 9700), _snap("5m", 110.0, 0.51, 9690))), [])
        # a 5m round that does NOT close with the 15m round
        other = Snapshot(ts=9700, market=MarketInfo("BTC-5m", "btc-updown-5m-9300", 9300, 9600, "u", "d",
                                                    price_to_beat=110.0), up=_books(0.51)[0], down=_books(0.51)[1])
        self.assertEqual(s.on_tick(_ctx(_snap("15m", 100.0, 0.40, 9700), other)), [])

    def test_needs_both_prices_to_beat(self):
        s = NestedPair("s7")
        self.assertEqual(s.on_tick(_ctx(_snap("15m", 100.0, 0.40, 9700), _snap("5m", None, 0.51, 9700))), [])

    def test_leg2_matches_leg1_with_a_price_cap(self):
        s = NestedPair("s7")
        me = _snap("5m", 110.0, 0.51, 9702)                        # DOWN ask here .50
        other = _snap("15m", 100.0, 0.40, 9700)
        ctx = _ctx(me, other, pos_other=_holding(other, UP, 60.0, 0.4228))
        o = s.on_tick(ctx)
        self.assertEqual((len(o), o[0].outcome, o[0].shares), (1, DOWN, 60.0))
        self.assertLessEqual(0.4228 + o[0].limit + fee(o[0].limit), 1.02 + 1e-9)
        self.assertEqual(o[0].meta["pair_leg"], 2)

    def test_leg2_waits_when_too_expensive_then_gives_up_late(self):
        s = NestedPair("s7")
        other = _snap("15m", 100.0, 0.40, 9700)
        held = _holding(other, UP, 60.0, 0.60)                       # leg 1 cost 60c: 5m DOWN at .50 is too much
        ctx = _ctx(_snap("5m", 110.0, 0.51, 9702), other, pos_other=held)
        self.assertEqual(s.on_tick(ctx), [])
        self.assertEqual(ctx.events[0][0], "leg2_waiting")
        late = _ctx(_snap("5m", 110.0, 0.51, END - 2), other, pos_other=held)
        self.assertEqual(s.on_tick(late), [])
        self.assertEqual(late.events[0][0], "leg2_abandoned")

    def test_one_pair_per_round_pair_and_leg1_side_waits(self):
        s = NestedPair("s7")
        me, other = _snap("15m", 100.0, 0.40, 9700), _snap("5m", 110.0, 0.51, 9700)
        # leg 1 held here -> nothing to do on this market
        self.assertEqual(s.on_tick(_ctx(me, other, pos_me=_holding(me, UP, 60, 0.42))), [])
        # pair complete -> no new pair
        self.assertEqual(s.on_tick(_ctx(me, other, pos_me=_holding(me, UP, 60, 0.42),
                                        pos_other=_holding(other, DOWN, 60, 0.52))), [])

    def test_band_ev_opens_above_a_dollar(self):
        s = NestedPair("s7ev", band_ev=True, min_ev=0.02, max_ev_pair_cost=1.05, leg1_market=None)
        me, other = _snap("15m", 100.0, 0.48, 9700), _snap("5m", 110.0, 0.51, 9700)   # pair ~1.047
        fv = lambda up: types.SimpleNamespace(of=lambda o: up if o == UP else 1 - up)
        me.fv, other.fv = fv(0.60), fv(0.50)                       # P(band) = .60 + .50 - 1 = .10
        o = s.on_tick(_ctx(me, other))
        self.assertEqual(len(o), 1)
        self.assertAlmostEqual(o[0].meta["p_band"], 0.10)
        self.assertEqual(NestedPair("s7", leg1_market=None).on_tick(_ctx(me, other)), [])   # locked-only: no


class EngineOthersTests(unittest.TestCase):
    """End to end: leg 1 on one market's tick, leg 2 on the other's, through the real engine and broker."""

    def test_both_legs_fill_through_the_engine(self):
        from core.engine import Engine
        from core.storage import Storage

        quotes = {"5m": 0.51, "15m": 0.40}           # 15m UP ask .40; 5m DOWN ask .50 -> pair .9466
        ks = {"5m": 110.0, "15m": 100.0}

        class Feed:
            def current_market(self, cfg, now):
                return _info(cfg["kind"], ks[cfg["kind"]])
            def books(self, info):
                return _books(quotes[info.asset.split("-")[1]])
            def index_price(self, info, now):
                return None
            def resolution_detail(self, slug, window_end=None):
                return None, False

        clock = types.SimpleNamespace(t=9700.0)
        cfg = types.SimpleNamespace(
            MARKETS=[{"asset": "BTC-5m", "kind": "5m", "window_seconds": 300},
                     {"asset": "BTC-15m", "kind": "15m", "window_seconds": 900}],
            STARTING_BANKROLL=1000.0, EXTRA_SLIPPAGE=0.0, POLL_SECONDS=2, SETTLE_CHECK_SECONDS=15,
            EQUITY_EVERY_SECONDS=5, SETTLE_WARN_SECONDS=3600, EXCHANGE="polymarket_us", STOP_EQUITY=None,
            LATENCY_SECONDS=0.25, RISK={"max_usd_per_trade": 50}, MAKER={}, EXPORT_DIR=None)
        tmp = tempfile.mkdtemp()
        eng = Engine(cfg, [NestedPair("s7")], Feed(), Storage(os.path.join(tmp, "t.db")),
                     clock=lambda: clock.t, sleep=lambda s: setattr(clock, "t", clock.t + s))
        eng.setup()
        eng.tick(clock.t)                 # 5m: no partner yet; 15m: leg 1 only goes on the 5m tick
        clock.t += 2
        eng.tick(clock.t)                 # 5m: sees the 15m book -> leg 1 (5m DOWN); 15m right after -> leg 2 (UP)
        clock.t += 2
        eng.tick(clock.t)                 # nothing more
        rows = eng.store.db.execute("SELECT asset, outcome, shares, avg_price FROM fills ORDER BY ts").fetchall()
        self.assertEqual([(r["asset"], r["outcome"]) for r in rows], [("BTC-5m", DOWN), ("BTC-15m", UP)])
        self.assertAlmostEqual(rows[0]["shares"], rows[1]["shares"])
        self.assertAlmostEqual(rows[0]["avg_price"] + rows[1]["avg_price"], 0.90)
        b = eng.brokers["s7"]
        self.assertNotIn("btc-updown-5m-9000", b.positions)        # ctx.others never creates positions
        eng.store.db.close()


def _engine(strategies, quotes, risk, ks=None):
    """A real engine + broker on a fake two-market feed (5m and 15m closing together)."""
    from core.engine import Engine
    from core.storage import Storage
    ks = ks or {"5m": 110.0, "15m": 100.0}

    class Feed:
        def current_market(self, cfg, now):
            return _info(cfg["kind"], ks[cfg["kind"]])
        def books(self, info):
            return _books(quotes[info.asset.split("-")[1]])
        def index_price(self, info, now):
            return None
        def resolution_detail(self, slug, window_end=None):
            return None, False

    clock = types.SimpleNamespace(t=9700.0)
    cfg = types.SimpleNamespace(
        MARKETS=[{"asset": "BTC-5m", "kind": "5m", "window_seconds": 300},
                 {"asset": "BTC-15m", "kind": "15m", "window_seconds": 900}],
        STARTING_BANKROLL=1000.0, EXTRA_SLIPPAGE=0.0, POLL_SECONDS=2, SETTLE_CHECK_SECONDS=15,
        EQUITY_EVERY_SECONDS=5, SETTLE_WARN_SECONDS=3600, EXCHANGE="polymarket_us", STOP_EQUITY=None,
        LATENCY_SECONDS=0.25, RISK=risk, MAKER={}, EXPORT_DIR=None)
    eng = Engine(cfg, strategies, Feed(), Storage(os.path.join(tempfile.mkdtemp(), "t.db")),
                 clock=lambda: clock.t, sleep=lambda s: setattr(clock, "t", clock.t + s))
    eng.setup()
    return eng, clock


class HedgeRiskTests(unittest.TestCase):
    """A hedge leg (completing a position held in another market) must not be blocked by the risk limits."""

    def test_risk_manager_lets_a_hedge_through_the_kill_switch_and_open_limit(self):
        from core.risk import RiskManager
        r = RiskManager({"max_usd_per_trade": 50, "max_open_markets": 1})
        r.kill_active = True
        kw = dict(usd=None, shares=10, price=0.5, market_cost=0.0, open_markets=1, in_this_market=False, equity=1000)
        self.assertFalse(r.check_buy("s", **kw).ok)
        self.assertTrue(r.check_buy("s", hedge=True, **kw).ok)
        r.kill_active = False
        self.assertFalse(r.check_buy("s", **kw).ok)                 # open-markets limit
        self.assertTrue(r.check_buy("s", hedge=True, **kw).ok)
        big = r.check_buy("s", hedge=True, **{**kw, "shares": 1000})  # dollar caps still apply to a hedge
        self.assertEqual((big.reason, big.shares), ("clipped", 100.0))

    def test_leg2_fills_although_the_open_markets_limit_is_reached(self):
        eng, clock = _engine([NestedPair("s7")], {"5m": 0.51, "15m": 0.40},
                             {"max_usd_per_trade": 50, "max_open_markets": 1})
        for _ in range(2):
            eng.tick(clock.t)
            clock.t += 2
        rows = eng.store.db.execute("SELECT asset, outcome FROM fills ORDER BY ts").fetchall()
        self.assertEqual([(r["asset"], r["outcome"]) for r in rows], [("BTC-5m", DOWN), ("BTC-15m", UP)])
        eng.store.db.close()

    def test_leg2_fills_while_the_kill_switch_is_on(self):
        eng, clock = _engine([NestedPair("s7")], {"5m": 0.51, "15m": 0.40}, {"max_usd_per_trade": 50})
        eng.tick(clock.t)                                           # both markets seen once; no trade yet
        clock.t += 2
        real = eng._execute_taker

        def trip_after_leg1(s, broker, order, dsnap, exec_snap, books):
            fills = real(s, broker, order, dsnap, exec_snap, books)
            eng.risk.kill_active = True                             # the switch trips between the two legs
            return fills
        eng._execute_taker = trip_after_leg1
        eng.tick(clock.t)
        rows = eng.store.db.execute("SELECT asset, outcome FROM fills ORDER BY ts").fetchall()
        self.assertEqual([(r["asset"], r["outcome"]) for r in rows], [("BTC-5m", DOWN), ("BTC-15m", UP)])
        eng.store.db.close()

    def test_hedge_flag_is_ignored_without_a_position_elsewhere(self):
        from strategies.base import Strategy

        class Cheat(Strategy):
            PARAMS = {}
            def on_tick(self, ctx):
                return [ctx.buy(UP, usd=10, hedge=True)] if not ctx.has_position() else []

        eng, clock = _engine([Cheat("cheat", assets=["BTC-5m"])], {"5m": 0.51, "15m": 0.40}, {"max_usd_per_trade": 50})
        eng.risk.kill_active = True
        eng.tick(clock.t)
        self.assertEqual(eng.store.db.execute("SELECT COUNT(*) FROM fills").fetchone()[0], 0)
        self.assertEqual(eng.store.db.execute("SELECT COUNT(*) FROM events WHERE kind='risk_reject'").fetchone()[0], 1)
        eng.store.db.close()


class UnfilledAndEquityTests(unittest.TestCase):
    def test_an_order_that_gets_no_fill_is_recorded(self):
        from strategies.base import Strategy

        class TooCheap(Strategy):
            PARAMS = {}
            def on_tick(self, ctx):
                return [ctx.buy(UP, usd=10, max_price=0.30, reason="limit under the ask")]

        eng, clock = _engine([TooCheap("cheap", assets=["BTC-5m"])], {"5m": 0.51, "15m": 0.40}, {})
        for _ in range(3):                                          # retried every tick: one event per 10s, not three
            eng.tick(clock.t)
            clock.t += 2
        rows = eng.store.db.execute("SELECT data FROM events WHERE kind='order_unfilled'").fetchall()
        self.assertEqual(len(rows), 1)
        import json
        d = json.loads(rows[0]["data"])
        self.assertEqual((d["outcome"], d["side"], d["limit"], d["decision_px"], d["exec_px"]),
                         (UP, "BUY", 0.30, 0.51, 0.51))
        self.assertEqual(eng.store.db.execute("SELECT COUNT(*) FROM fills").fetchone()[0], 0)
        eng.store.db.close()

    def test_equity_rows_only_when_the_value_changes_plus_a_heartbeat(self):
        eng, clock = _engine([NestedPair("s7")], {"5m": 0.60, "15m": 0.60}, {})     # pair too expensive: stays flat
        n = lambda: eng.store.db.execute("SELECT COUNT(*) FROM equity").fetchone()[0]
        eng.record_equity(clock.t)
        eng.record_equity(clock.t + 5)
        eng.record_equity(clock.t + 30)
        self.assertEqual(n(), 1)                                    # unchanged -> not written again
        eng.record_equity(clock.t + 61)
        self.assertEqual(n(), 2)                                    # heartbeat
        eng.brokers["s7"].cash -= 1.0
        eng.record_equity(clock.t + 62)
        self.assertEqual(n(), 3)                                    # changed -> written at once
        eng.record_equity(clock.t + 63, force=True)
        self.assertEqual(n(), 4)                                    # shutdown / end of replay
        eng.store.db.close()


if __name__ == "__main__":
    unittest.main()
