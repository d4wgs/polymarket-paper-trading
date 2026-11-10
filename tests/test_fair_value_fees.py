"""
Unit tests for the shared components: fair_value(), fee(), the vol estimator, the market-data hub,
maker fill rules, risk limits, edge helpers and the engine's latency / maker plumbing.

    python -m unittest discover -s tests -v
"""
import math
import os
import sys
import tempfile
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.fairvalue import (FairValue, MarketData, fair_value, norm_cdf, student_t_cdf,
                            unit_variance_t_cdf)
from core.fees import PlaceholderFees, fee, round_cents, taker_fee_per_share
from core.maker import MakerBook
from core.models import DOWN, UP, Book, MarketInfo, Position, Snapshot
from core.risk import RiskManager
from core.volatility import EwmaVol, sigma_from_closes
from strategies.base import TickContext


class FairValueTests(unittest.TestCase):
    def test_at_the_money_is_one_half(self):
        p_up, p_down, z = fair_value(100_000, 100_000, 1e-4, 120)
        self.assertAlmostEqual(p_up, 0.5, places=12)
        self.assertAlmostEqual(p_down, 0.5, places=12)
        self.assertEqual(z, 0.0)

    def test_matches_formula(self):
        S, K, sigma, t = 100_050.0, 100_000.0, 1.2e-4, 90
        z = math.log(S / K) / (sigma * math.sqrt(t))
        p_up, p_down, z2 = fair_value(S, K, sigma, t)
        self.assertAlmostEqual(z2, z, places=12)
        self.assertAlmostEqual(p_up, 0.5 * (1 + math.erf(z / math.sqrt(2))), places=12)
        self.assertAlmostEqual(p_up + p_down, 1.0, places=12)

    def test_one_sigma_up_is_84_percent(self):
        sigma, t, K = 1e-4, 100, 50_000.0
        S = K * math.exp(sigma * math.sqrt(t))            # exactly z = 1
        self.assertAlmostEqual(fair_value(S, K, sigma, t)[0], 0.841344746, places=6)

    def test_symmetry_and_monotonicity(self):
        a = fair_value(101, 100, 1e-3, 60)[0]
        b = fair_value(100 ** 2 / 101, 100, 1e-3, 60)[0]   # mirror image of ln(S/K)
        self.assertAlmostEqual(a + b, 1.0, places=12)
        ps = [fair_value(100 + d, 100, 1e-3, 60)[0] for d in (-1, -0.5, 0, 0.5, 1)]
        self.assertEqual(ps, sorted(ps))

    def test_less_time_or_less_vol_means_more_certain(self):
        far = fair_value(100.1, 100, 1e-4, 300)[0]
        near = fair_value(100.1, 100, 1e-4, 10)[0]
        self.assertGreater(near, far)
        self.assertLess(fair_value(100.1, 100, 1e-4, 10, sigma_multiplier=2.0)[0], near)

    def test_t_is_floored_at_one_second(self):
        self.assertEqual(fair_value(100.01, 100, 1e-4, 0), fair_value(100.01, 100, 1e-4, 1))
        self.assertEqual(fair_value(100.01, 100, 1e-4, -5), fair_value(100.01, 100, 1e-4, 1))

    def test_bad_inputs_raise(self):
        for args in ((0, 100, 1e-4, 10), (100, 0, 1e-4, 10), (100, 100, 0, 10), (100, 100, None, 10)):
            with self.assertRaises(ValueError):
                fair_value(*args)

    def test_student_t_cdf_known_values(self):
        self.assertAlmostEqual(student_t_cdf(1.0, 1), 0.75, places=10)            # Cauchy
        self.assertAlmostEqual(student_t_cdf(2.131846786, 4), 0.95, places=7)     # t table, df=4
        self.assertAlmostEqual(student_t_cdf(-2.131846786, 4), 0.05, places=7)
        self.assertAlmostEqual(student_t_cdf(0.0, 4), 0.5, places=12)
        self.assertAlmostEqual(student_t_cdf(1.959964, 1e6), norm_cdf(1.959964), places=5)

    def test_student_t_has_fatter_tails_same_variance(self):
        z = 3.0
        n = fair_value(math.exp(z * 1e-4 * 10), 1.0, 1e-4, 100)[0]
        t = fair_value(math.exp(z * 1e-4 * 10), 1.0, 1e-4, 100, dist="student_t", df=4)[0]
        self.assertLess(t, n)                  # a 3-sigma lead is LESS safe with fat tails
        self.assertAlmostEqual(unit_variance_t_cdf(0.0, 4), 0.5)

    def test_averaged_settlement(self):
        from core.fairvalue import averaged_settlement, fair_value_averaged
        # A = 0 is exactly the point model
        self.assertEqual(fair_value_averaged(100.1, 100, 1e-4, 90, 0), fair_value(100.1, 100, 1e-4, 90))
        # before the averaging minute: same center, less variance (A/3 instead of A)
        c, sd = averaged_settlement(100.0, 1e-4, 120, 60)
        self.assertAlmostEqual(sd, 1e-4 * math.sqrt(60 + 20))
        # inside it: half the average is already known
        c, sd = averaged_settlement(102.0, 1e-4, 30, 60, observed_mean=100.0)
        self.assertAlmostEqual(c, 101.0)
        self.assertAlmostEqual(sd, 0.5 * 102 * 1e-4 * math.sqrt(10) / 101)
        # a late spike above K counts only partly: less certain than the point model says
        pt = fair_value(100.02, 100, 1e-4, 10)[0]
        av = fair_value_averaged(100.02, 100, 1e-4, 10, 60, observed_mean=99.99)[0]
        self.assertLess(av, pt)

    def test_market_data_interface(self):
        class Spot:
            def price(self, now): return 100_100.0
            def price_at(self, ts): return 100_100.0
            def sigma(self): return 1e-4
        md = MarketData(Spot(), {"sigma_multiplier": 1.0})
        info = MarketInfo("BTC-5m", "btc-updown-5m-1000", 1000, 1300, "u", "d", price_to_beat=100_000.0)
        md.register(info)
        md.on_oracle(1100, 100_050.0)
        p_up, p_down, sigma, t, S, K = md.fair_value(info.slug, now=1100)
        self.assertEqual((sigma, t, S, K), (1e-4, 200, 100_050.0, 100_000.0))     # S = oracle, not spot
        self.assertAlmostEqual(p_up, fair_value(100_050.0, 100_000.0, 1e-4, 200)[0])
        fv = md.compute(info, 1100)
        self.assertAlmostEqual(fv.basis, (100_100 - 100_050) / 100_050)
        # no published K -> first oracle print of the window; no oracle -> spot
        info2 = MarketInfo("BTC-5m", "btc-updown-5m-1300", 1300, 1600, "u", "d")
        md.on_oracle(1301, 99_990.0)
        self.assertEqual(md.window_open(info2), (99_990.0, "oracle"))
        md2 = MarketData(Spot(), {})
        fv2 = md2.compute(info, 1100)
        self.assertEqual((fv2.S, fv2.s_source), (100_100.0, "spot"))
        # strict mode: no published K -> no fair value at all
        strict = MarketData(Spot(), {"require_published_k": True})
        self.assertIsNone(strict.compute(info2, 1310))
        self.assertIsNotNone(strict.compute(info, 1100))
        # spot as S
        self.assertEqual(MarketData(Spot(), {"s_source": "spot"}).compute(info, 1100).S, 100_100.0)


class FeeTests(unittest.TestCase):
    def test_polymarket_us_published_formula(self):
        # theta x C x p x (1-p), rounded to the cent half-to-even
        self.assertEqual(fee(0.50, 100, "taker", theta=0.095), round_cents(0.095 * 100 * 0.25))
        self.assertEqual(fee(0.50, 100, "taker", theta=0.095), 2.38)        # 2.375 -> 2.38 (8 is even)
        self.assertEqual(fee(0.50, 100, "taker", theta=0.0695), 1.74)       # 1.7375 -> 1.74
        self.assertEqual(fee(0.90, 10, "taker", theta=0.095), 0.09)         # 0.0855 -> 0.09
        self.assertEqual(fee(0.50, 100), 2.38)                               # default: new account, tier 1

    def test_fee_peaks_at_fifty_cents_and_vanishes_at_edges(self):
        fees = [fee(p / 100, 1000, theta=0.095) for p in range(1, 100)]
        self.assertEqual(max(fees), fee(0.50, 1000, theta=0.095))
        self.assertEqual(fee(0.0, 1000), 0.0)
        self.assertEqual(fee(1.0, 1000), 0.0)
        self.assertAlmostEqual(fee(0.3, 1000, theta=0.095), fee(0.7, 1000, theta=0.095))

    def test_placeholder(self):
        self.assertAlmostEqual(fee(0.50, 1, model="placeholder", fee_peak=0.0156), 0.0156)
        self.assertAlmostEqual(fee(0.20, 100, model="placeholder", fee_peak=0.0156), 0.0156 * 4 * 0.16 * 100)
        pf = PlaceholderFees(0.0156)
        self.assertAlmostEqual(pf.fee([(100, 0.5)], pf.theta(0, None), None), 1.56)

    def test_maker_pays_nothing_rebate_is_negative(self):
        self.assertEqual(fee(0.50, 100, "maker"), 0.0)
        self.assertEqual(fee(0.50, 100, "maker", maker_rebate_theta=0.0035), -0.09)   # 0.0875 -> 0.09
        self.assertEqual(fee(0.50, 100, "maker", maker_rebate_theta=-0.0035), -0.09)  # sign-agnostic

    def test_intl_and_validation(self):
        self.assertEqual(fee(0.5, 100, model="polymarket_intl"), round(0.07 * 25, 5))
        with self.assertRaises(ValueError):
            fee(1.5, 1)
        with self.assertRaises(ValueError):
            fee(0.5, -1)
        with self.assertRaises(ValueError):
            fee(0.5, 1, side="limit")
        self.assertAlmostEqual(taker_fee_per_share(0.5, 0.095), 0.02375)


class VolTests(unittest.TestCase):
    def test_constant_returns(self):
        v = EwmaVol(half_life=300, min_samples=5)
        px = 100.0
        for i in range(400):
            v.update(i, px)
            px *= math.exp(1e-4 if i % 2 else -1e-4)
        self.assertAlmostEqual(v.sigma, 1e-4, delta=2e-6)

    def test_gap_counts_per_second(self):
        v = EwmaVol(half_life=10, min_samples=1)
        v.seed(1e-4)
        v.update(0, 100.0)
        v.update(4, 100.0 * math.exp(2e-4))       # 4s gap: per-second variance = (2e-4)^2 / 4 = (1e-4)^2
        self.assertAlmostEqual(v.sigma, 1e-4, places=10)

    def test_warmup_and_candles(self):
        v = EwmaVol(min_samples=3)
        v.update(0, 100)
        v.update(1, 101)
        self.assertIsNone(v.sigma)
        closes = [100 * math.exp(0.001 * (1 if i % 2 else -1) * i % 3) for i in range(50)]
        self.assertIsNotNone(sigma_from_closes(closes, 60))


def _info(single=True):
    return MarketInfo("BTC-5m", "btc-updown-5m-1000", 1000, 1300, "u", "d", single_instrument=single,
                      qty_step=0.01, min_notional=0.0, price_to_beat=100.0)


class MakerTests(unittest.TestCase):
    def test_touch_does_not_fill_trade_through_does(self):
        mb = MakerBook(latency=0.25, conservative_queue=True)
        book = Book(bids=[(0.40, 30)], asks=[(0.45, 50)])
        q = mb.place("mm", "s", "BTC-5m", UP, 0.40, 20, now=10, book=book)
        self.assertEqual(q.queue_ahead, 30)
        books = {UP: Book(bids=[(0.40, 50)], asks=[(0.40, 100)]), DOWN: Book()}
        self.assertEqual(mb.check("mm", "s", books, now=11), [])            # ask == our bid: touch only
        self.assertEqual(q.queue_ahead, 30)                                  # level didn't shrink: queue kept
        books = {UP: Book(bids=[(0.38, 10)], asks=[(0.39, 40)]), DOWN: Book()}
        hits = mb.check("mm", "s", books, now=12)                             # 40 through - 30 queue = 10
        self.assertEqual([(h[0].price, h[1]) for h in hits], [(0.40, 10)])
        hits = mb.check("mm", "s", books, now=13)                             # queue gone: rest fills
        self.assertEqual(hits[0][1], 10)
        self.assertEqual(mb.for_market("mm", "s"), {})

    def test_not_live_before_latency_and_never_crosses(self):
        mb = MakerBook(latency=1.0)
        self.assertIsNone(mb.place("mm", "s", "a", UP, 0.45, 10, 0, Book(asks=[(0.45, 5)])))
        mb.place("mm", "s", "a", UP, 0.44, 10, 0, Book(asks=[(0.45, 5)]))
        books = {UP: Book(asks=[(0.30, 100)]), DOWN: Book()}
        self.assertEqual(mb.check("mm", "s", books, now=0.5), [])
        self.assertEqual(mb.check("mm", "s", books, now=1.0)[0][1], 10)


class RiskTests(unittest.TestCase):
    def test_limits(self):
        r = RiskManager({"max_usd_per_trade": 50, "max_usd_per_market": 80, "max_open_markets": 2,
                         "basis_kill_pct": 0.05})
        kw = dict(price=0.5, open_markets=0, in_this_market=False, equity=1000)
        self.assertEqual(r.check_buy("s", usd=200, shares=None, market_cost=0, **kw).usd, 50)
        self.assertEqual(r.check_buy("s", usd=200, shares=None, market_cost=60, **kw).usd, 20)
        self.assertFalse(r.check_buy("s", usd=10, shares=None, market_cost=80, **kw).ok)
        self.assertEqual(r.check_buy("s", usd=None, shares=500, market_cost=0, **kw).shares, 100)
        self.assertFalse(r.check_buy("s", usd=10, shares=None, market_cost=0, price=0.5, open_markets=2,
                                     in_this_market=False, equity=1000).ok)
        # no daily loss stop: a big loss today doesn't block new trades (only the $200 equity stop does)
        self.assertTrue(r.check_buy("s", usd=10, shares=None, market_cost=0, price=0.5, open_markets=0,
                                    in_this_market=False, equity=250).ok)

    def test_kill_switch(self):
        r = RiskManager({"basis_kill_pct": 0.05})
        self.assertFalse(r.update_kill(0.0004, 1, 1, 0))
        self.assertTrue(r.update_kill(-0.0006, 1, 1, 0))
        self.assertFalse(r.check_buy("s", usd=10, shares=None, price=0.5, market_cost=0, open_markets=0,
                                     in_this_market=False, equity=1000).ok)
        self.assertTrue(r.update_kill(None, None, 1, 0))       # missing spot counts as tripped
        self.assertFalse(RiskManager({"require_both_feeds": False}).update_kill(None, None, 1, 0))


class EdgeHelperTests(unittest.TestCase):
    def _ctx(self, p_up=0.60, asks=((0.50, 100),)):
        info = _info()
        fv = FairValue(p_up=p_up, p_down=1 - p_up, sigma=1e-4, t=100, S=100, K=100, z=0)
        snap = Snapshot(ts=1100, market=info, up=Book(bids=[(0.48, 100)], asks=list(asks)),
                        down=Book(bids=[(0.50, 100)], asks=[(0.52, 100)]), fv=fv)
        return TickContext(snap=snap, position=Position("BTC-5m", info.slug, 1300), cash=1000, fee_theta=0.095)

    def test_edge_and_max_price(self):
        ctx = self._ctx()
        self.assertAlmostEqual(ctx.edge(UP), 0.60 - 0.50 - 0.095 * 0.25)
        lim = ctx.max_price_for_edge(UP, 0.04)
        self.assertGreaterEqual(0.60 - lim - 0.095 * lim * (1 - lim), 0.04 - 1e-12)
        nxt = lim + 0.001
        self.assertLess(0.60 - nxt - 0.095 * nxt * (1 - nxt), 0.04)
        self.assertIsNone(self._ctx(p_up=0.01).max_price_for_edge(UP, 0.04))

    def test_walk_cost_uses_depth(self):
        ctx = self._ctx(asks=((0.50, 10), (0.55, 10)))
        got, avg, fee_total = ctx.walk_cost(UP, 15)
        self.assertEqual(got, 15)
        self.assertAlmostEqual(avg, (10 * 0.50 + 5 * 0.55) / 15)
        self.assertEqual(ctx.kind, "5m")


class EngineIntegrationTests(unittest.TestCase):
    """Taker orders fill against the book fetched AFTER the latency; maker bids fill on trade-through."""

    def _engine(self, books_seq, strategy, latency=0.25):
        from core.engine import Engine
        from core.storage import Storage

        class Feed:
            def __init__(self):
                self.calls = 0
            def current_market(self, cfg, now):
                return _info()
            def books(self, info):
                b = books_seq[min(self.calls, len(books_seq) - 1)]
                self.calls += 1
                return b
            def index_price(self, info, now):
                return 100.0
            def resolution_detail(self, slug, window_end=None):
                return None, False

        class Spot:
            def poll(self, now): pass
            def price(self, now): return 100.0
            def price_at(self, ts): return 100.0
            def sigma(self): return 1e-3

        clock = types.SimpleNamespace(t=1100.0)
        cfg = types.SimpleNamespace(
            MARKETS=[{"asset": "BTC-5m", "slug_prefix": "btc-updown-5m", "window_seconds": 300}],
            STARTING_BANKROLL=1000.0, EXTRA_SLIPPAGE=0.0, POLL_SECONDS=2, SETTLE_CHECK_SECONDS=15,
            EQUITY_EVERY_SECONDS=5, SETTLE_WARN_SECONDS=3600, EXCHANGE="polymarket_us", STOP_EQUITY=None,
            LATENCY_SECONDS=latency, RISK={"max_usd_per_trade": 50}, MAKER={}, EXPORT_DIR=None)
        tmp = tempfile.mkdtemp()
        eng = Engine(cfg, [strategy], Feed(), Storage(os.path.join(tmp, "t.db")),
                     clock=lambda: clock.t, sleep=lambda s: setattr(clock, "t", clock.t + s),
                     market_data=MarketData(Spot(), {}))
        eng.setup()
        return eng, clock

    @staticmethod
    def _pair(up_bids, up_asks):
        up = Book(bids=up_bids, asks=up_asks)
        down = Book(bids=[(round(1 - p, 6), s) for p, s in up_asks], asks=[(round(1 - p, 6), s) for p, s in up_bids])
        return up, down

    def test_taker_fills_against_book_after_latency(self):
        from strategies.base import Strategy

        class Buyer(Strategy):
            def on_tick(self, ctx):
                return [] if ctx.entries_this_window else [ctx.buy(UP, usd=20, max_price=0.60)]

        decision = self._pair([(0.48, 100)], [(0.50, 100)])
        after = self._pair([(0.53, 100)], [(0.55, 100)])          # the ask moved up during the latency
        eng, clock = self._engine([decision, after], Buyer("b"))
        eng.tick(clock.t)
        row = eng.store.db.execute("SELECT avg_price, decision_ts, ts, fair_value, edge, spot, oracle, "
                                   "liquidity FROM fills").fetchone()
        self.assertAlmostEqual(row["avg_price"], 0.55)               # NOT 0.50
        self.assertAlmostEqual(row["ts"] - row["decision_ts"], 0.25)
        self.assertAlmostEqual(row["fair_value"], 0.5)               # S == K -> 0.5
        self.assertLess(row["edge"], 0)                              # 0.5 - 0.55 - fee
        self.assertEqual((row["spot"], row["oracle"], row["liquidity"]), (100.0, 100.0, "taker"))

    def test_risk_clip_and_maker_fill(self):
        from strategies.base import Strategy

        class Big(Strategy):
            def on_tick(self, ctx):
                if ctx.entries_this_window:
                    return []
                if not ctx.quotes:
                    return [ctx.buy(UP, usd=500, max_price=0.99), ctx.bid(DOWN, 0.40, 10)]
                return []

        b0 = self._pair([(0.48, 1000)], [(0.50, 1000)])
        b1 = self._pair([(0.63, 1000)], [(0.65, 1000)])    # DOWN ask = 0.37 < our 0.40 bid: traded through
        eng, clock = self._engine([b0, b0, b1], Big("big"))
        eng.tick(clock.t)
        usd = eng.store.db.execute("SELECT SUM(usd + fee) FROM fills WHERE liquidity='taker'").fetchone()[0]
        self.assertLessEqual(usd, 50 + 1e-9)                           # max_usd_per_trade
        clock.t += 2
        eng.tick(clock.t)
        mk = eng.store.db.execute("SELECT side, outcome, shares, avg_price, fee FROM fills "
                                  "WHERE liquidity='maker'").fetchall()
        # long UP, so a DOWN bid fill closes UP at 1 - 0.40 = 0.60 (one-instrument market)
        self.assertEqual([tuple(r) for r in mk], [("SELL", UP, 10.0, 0.6, 0.0)])


class RetiredStrategyTests(unittest.TestCase):
    def test_retired_strategy_open_bet_is_settled(self):
        from core.engine import Engine
        from core.models import Fill
        from core.storage import Storage
        from strategies.base import Strategy

        store = Storage(os.path.join(tempfile.mkdtemp(), "r.db"))
        store.register_strategy("old_one", "X", {}, 1000.0, None, 0)
        store.log_fill(Fill(ts=1000, strategy="old_one", asset="BTC-5m", slug="btc-updown-5m-900",
                            window_end=1200, outcome=UP, side="BUY", shares=10, avg_price=0.5, usd=5.0, fee=0.1))
        store.commit()

        class Feed:
            def resolution_detail(self, slug, window_end=None):
                return UP, True

        cfg = types.SimpleNamespace(MARKETS=[], STARTING_BANKROLL=1000.0, EXTRA_SLIPPAGE=0.0, POLL_SECONDS=2,
                                    SETTLE_CHECK_SECONDS=15, EQUITY_EVERY_SECONDS=5, SETTLE_WARN_SECONDS=3600,
                                    EXCHANGE="polymarket_us", STOP_EQUITY=None)
        eng = Engine(cfg, [Strategy("new_one")], Feed(), store, clock=lambda: 1300.0)
        eng.setup()
        self.assertIn("old_one", eng.retired)
        eng.settle_finished(1300.0)
        row = store.db.execute("SELECT payout, pnl FROM settlements WHERE strategy='old_one'").fetchone()
        self.assertEqual((row["payout"], round(row["pnl"], 6)), (10.0, 4.9))
        self.assertNotIn("old_one", eng.retired)


if __name__ == "__main__":
    unittest.main()


class DistributionModeTests(unittest.TestCase):
    """Normal vs Student-t (model.distribution): both are probabilities, both rise with spot - strike, and the
    Student-t (rescaled to unit variance) keeps more probability in the tails."""

    SIGMA, T = 4e-5, 240.0

    def p_up(self, gap_bps, dist, avg=0.0, t=None):
        from core.fairvalue import fair_value, fair_value_averaged
        K = 100_000.0
        S = K * (1 + gap_bps / 1e4)
        t = self.T if t is None else t
        if avg:
            return fair_value_averaged(S, K, self.SIGMA, t, avg, dist=dist, df=4)[0]
        return fair_value(S, K, self.SIGMA, t, dist=dist, df=4)[0]

    def test_both_modes_are_probabilities_and_monotonic_in_the_gap(self):
        gaps = [g / 2 for g in range(-400, 401)]                       # -200 .. +200 bps in 0.5 bp steps
        for dist in ("normal", "student_t"):
            for avg, t in ((0.0, None), (60.0, None), (60.0, 20.0)):   # point, averaged, inside the final minute
                ps = [self.p_up(g, dist, avg, t) for g in gaps]
                self.assertTrue(all(0.0 <= p <= 1.0 for p in ps), (dist, avg, t))
                self.assertTrue(all(b >= a - 1e-15 for a, b in zip(ps, ps[1:])), (dist, avg, t))
                self.assertLess(ps[0], 0.01)
                self.assertGreater(ps[-1], 0.99)
                self.assertAlmostEqual(self.p_up(0.0, dist, avg, t), 0.5, places=12)
                self.assertAlmostEqual(self.p_up(7.0, dist, avg, t) + self.p_up(-7.0, dist, avg, t), 1.0, places=3)   # ~symmetric

    def test_student_t_has_fatter_tails_than_the_normal(self):
        from core.fairvalue import norm_cdf, unit_variance_t_cdf
        for z in (2.5, 3.0, 4.0, 6.0):                                  # far from the strike: more tail mass
            self.assertGreater(1 - unit_variance_t_cdf(z, 4), 1 - norm_cdf(z))
            self.assertGreater(unit_variance_t_cdf(-z, 4), norm_cdf(-z))
        self.assertGreater((1 - unit_variance_t_cdf(3.0, 4)) / (1 - norm_cdf(3.0)), 3.0)
        # same variance, so it is MORE peaked near the strike (a small gap is priced more confidently)
        self.assertGreater(unit_variance_t_cdf(0.5, 4), norm_cdf(0.5))
        # in price terms: a far out-of-the-money side keeps a higher fair value under the Student-t
        self.assertGreater(self.p_up(-25.0, "student_t"), self.p_up(-25.0, "normal"))

    def test_the_sign_of_the_gap_decides_the_favourite_in_both_modes(self):
        """Why s3_sniper_v3d kept its name: P(Up) >= 0.5 is the same test under either distribution."""
        for g in (-50, -3, -0.01, 0.0, 0.01, 3, 50):
            self.assertEqual(self.p_up(g, "normal") >= 0.5, self.p_up(g, "student_t") >= 0.5)

    def test_config_uses_student_t_df4(self):
        import config
        self.assertEqual((config.MODEL.get("distribution"), config.MODEL.get("student_t_df")), ("student_t", 4))
