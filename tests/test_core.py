"""
Regression tests for the accounting, strategies, engine and metrics.

    python -m unittest discover -s tests -v

No internet needed: everything runs on fake books and an in-memory feed.
"""
import os
import sys
import tempfile
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.broker import PaperBroker, taker_fee
from core.engine import Engine
from core.metrics import calibration, load, round_trips
from core.models import BUY, SELL, UP, DOWN, Book, MarketInfo, Order, Snapshot
from core.storage import Storage
from archive.strategies.arbitrage import Arbitrage
from strategies.base import TickContext
from archive.strategies.previous_winner import PreviousWinner
from archive.strategies.scalp import Scalp

FEE = 0.07


def market(slug="btc-updown-5m-1000", start=1000, window=300):
    return MarketInfo("BTC-5m", slug, start, start + window, "tu", "td", "q", FEE)


def snap(ts, up, down, m=None):
    return Snapshot(ts=ts, market=m or market(), up=up, down=down)


def book(bids=(), asks=()):
    return Book(bids=list(bids), asks=list(asks))


def ctx_for(broker, s, state=None):
    return TickContext(snap=s, position=broker.position(s), cash=broker.cash, state=state if state is not None else {},
                       traded_this_window=s.market.slug in broker.traded_slugs)


def fill_all(broker, orders, s):
    books = {UP: s.up.copy(), DOWN: s.down.copy()}
    return [broker.execute(o, s, books) for o in orders]


class BrokerTests(unittest.TestCase):
    def test_buy_walks_book_and_charges_fee(self):
        b = PaperBroker("t", 1000)
        s = snap(1010, book(asks=[(0.50, 10), (0.51, 100)]), book())
        f = b.execute(Order(UP, BUY, usd=10), s, {UP: s.up.copy(), DOWN: s.down.copy()})
        self.assertAlmostEqual(f.usd + f.fee, 10, places=4)
        self.assertGreater(f.avg_price, 0.50)            # some shares filled at the second level
        self.assertAlmostEqual(b.cash, 990, places=4)

    def test_avg_cost_survives_partial_sell(self):
        b = PaperBroker("t", 1000)
        s = snap(1010, book(bids=[(0.60, 1000)], asks=[(0.50, 1000)]), book())
        fill_all(b, [Order(UP, BUY, shares=40)], s)
        cost = b.positions[s.market.slug].avg_cost(UP)
        fill_all(b, [Order(UP, SELL, shares=20)], s)
        pos = b.positions[s.market.slug]
        self.assertAlmostEqual(pos.avg_cost(UP), cost)    # entry price unchanged by the sale
        self.assertAlmostEqual(pos.up, 20)

    def test_flat_position_without_mark_is_worth_zero(self):
        b = PaperBroker("t", 1000)
        s = snap(1010, book(bids=[(0.40, 1000)], asks=[(0.50, 1000)]), book())
        fill_all(b, [Order(UP, BUY, usd=20)], s)
        fill_all(b, [Order(UP, SELL)], s)                 # sold at a loss -> net_cost > 0, no shares
        self.assertGreater(b.positions[s.market.slug].net_cost, 0)
        self.assertEqual(b.open_value({}), 0.0)

    def test_open_value_uses_cost_or_one_sided_mark(self):
        b = PaperBroker("t", 1000)
        s = snap(1010, book(asks=[(0.50, 1000)]), book())
        fill_all(b, [Order(UP, BUY, shares=20)], s)
        slug = s.market.slug
        self.assertAlmostEqual(b.open_value({}), 20 * b.positions[slug].avg_cost(UP))
        self.assertAlmostEqual(b.open_value({slug: (None, 0.30)}), 20 * 0.70)

    def test_replay_matches_live(self):
        live = PaperBroker("t", 1000)
        s = snap(1010, book(bids=[(0.55, 1000)], asks=[(0.50, 1000)]), book(asks=[(0.49, 1000)]))
        fills = fill_all(live, [Order(UP, BUY, usd=20), Order(DOWN, BUY, usd=10)], s)
        fills += fill_all(live, [Order(UP, SELL, shares=5)], s)
        rows = [vars(f) for f in fills if f]
        again = PaperBroker("t", 1000)
        again.replay(rows, [])
        self.assertAlmostEqual(again.cash, live.cash)
        p1, p2 = live.positions[s.market.slug], again.positions[s.market.slug]
        for attr in ("up", "down", "net_cost", "buy_cost_up", "bought_down"):
            self.assertAlmostEqual(getattr(p1, attr), getattr(p2, attr))
        self.assertIn(s.market.slug, again.traded_slugs)

    def test_settle_and_correct(self):
        b = PaperBroker("t", 1000)
        s = snap(1010, book(asks=[(0.50, 1000)]), book())
        fill_all(b, [Order(UP, BUY, shares=20)], s)
        res = b.settle(s.market.slug, DOWN, 1400)
        self.assertEqual(res.payout, 0)
        b.correct_settlement(res.payout, 20)              # official result was UP after all
        self.assertAlmostEqual(b.cash, 1000 - 20 * 0.5 - taker_fee(20, 0.5, FEE) + 20)


class StrategyTests(unittest.TestCase):
    def test_default_caps_allow_sub_cent_ticks(self):
        b = PaperBroker("t", 1000)
        s = snap(1010, book(), book(asks=[(0.995, 100)], bids=[(0.004, 100)]))
        c = ctx_for(b, s)
        self.assertGreaterEqual(c.buy(DOWN, usd=5).limit, 0.995)
        self.assertLessEqual(c.sell(DOWN).limit, 0.004)

    def test_arbitrage_sells_when_other_side_has_no_ask(self):
        arb, b = Arbitrage("a"), PaperBroker("a", 1000)
        s = snap(1020, book(bids=[(0.30, 1000)], asks=[(0.31, 1000)]), book(bids=[(0.69, 1000)], asks=[(0.70, 1000)]))
        fill_all(b, arb.on_tick(ctx_for(b, s)), s)        # leg 1: underdog UP
        self.assertGreater(b.positions[s.market.slug].up, 0)
        decided = snap(1250, book(bids=[(0.02, 1000)]), book(bids=[(0.99, 1000)]))   # 50s left, no DOWN asks
        orders = arb.on_tick(ctx_for(b, decided))
        self.assertEqual([(o.outcome, o.side) for o in orders], [(UP, SELL)])

    def test_arbitrage_tops_up_partial_hedge(self):
        arb, b = Arbitrage("a"), PaperBroker("a", 1000)
        s = snap(1020, book(asks=[(0.40, 1000)]), book(asks=[(0.61, 1000)]))
        fill_all(b, arb.on_tick(ctx_for(b, s)), s)
        held = b.positions[s.market.slug].up
        late = snap(1250, book(), book(asks=[(0.80, held / 2)]))    # only half the shares on offer
        fill_all(b, arb.on_tick(ctx_for(b, late)), late)
        pos = b.positions[s.market.slug]
        self.assertAlmostEqual(pos.down, held / 2, places=6)
        late2 = snap(1253, book(), book(asks=[(0.80, 1000)]))
        orders = arb.on_tick(ctx_for(b, late2))
        self.assertAlmostEqual(orders[0].shares, held / 2, places=6)  # buys only the missing half

    def test_arbitrage_locks_in_cheap_pair_early(self):
        arb, b = Arbitrage("a"), PaperBroker("a", 1000)
        s = snap(1020, book(asks=[(0.40, 1000)]), book(asks=[(0.61, 1000)]))
        fill_all(b, arb.on_tick(ctx_for(b, s)), s)
        cheap = snap(1100, book(), book(asks=[(0.50, 1000)]))     # 200s left, pair ~0.92
        orders = arb.on_tick(ctx_for(b, cheap))
        self.assertTrue(orders and orders[0].outcome == DOWN and "lock in" in orders[0].reason)

    def test_scalp_spread_does_not_trigger_stop(self):
        sc, b = Scalp("s"), PaperBroker("s", 1000)
        st = {}
        s = snap(1020, book(bids=[(0.66, 1000)], asks=[(0.71, 1000)]), book(bids=[(0.29, 1000)], asks=[(0.34, 1000)]))
        fill_all(b, sc.on_tick(ctx_for(b, s, st)), s)
        nxt = snap(1023, s.up, s.down)                    # same book 3s later
        self.assertEqual(sc.on_tick(ctx_for(b, nxt, st)), [])
        drop = snap(1026, book(bids=[(0.62, 1000)], asks=[(0.65, 1000)]), s.down)   # bid -6% since entry
        self.assertIn("stop loss", sc.on_tick(ctx_for(b, drop, st))[0].reason)

    def test_no_late_entry_and_no_reentry_after_restart(self):
        sc, b = Scalp("s"), PaperBroker("s", 1000)
        late = snap(1000 + 200, book(bids=[(0.5, 1000)], asks=[(0.51, 1000)]), book(bids=[(0.48, 1000)], asks=[(0.49, 1000)]))
        self.assertEqual(sc.on_tick(ctx_for(b, late)), [])            # 200s in: too late to enter
        s = snap(1020, late.up, late.down)
        fill_all(b, sc.on_tick(ctx_for(b, s, {})), s)
        fill_all(b, [Order(UP, SELL)], s)                             # round trip done
        restarted = PaperBroker("s", 1000)
        restarted.replay([], [])
        restarted.traded_slugs = set(b.traded_slugs)
        restarted.positions = b.positions
        s2 = snap(1030, late.up, late.down)
        self.assertEqual(sc.on_tick(ctx_for(restarted, s2, {})), [])  # state wiped, but it still knows


class FakeFeed:
    """A feed whose results we control: provisional first, official later."""
    def __init__(self):
        self.result = {}         # slug -> (winner, official)

    def current_market(self, cfg, now):
        start = int(now // 300) * 300
        return MarketInfo("BTC-5m", f"btc-updown-5m-{start}", start, start + 300, "u", "d", "q", FEE)

    def books(self, info):
        return book(bids=[(0.40, 1000)], asks=[(0.41, 1000)]), book(bids=[(0.58, 1000)], asks=[(0.59, 1000)])

    def resolution_detail(self, slug, window_end=None):
        return self.result.get(slug, (None, False))


def cfg(db):
    return types.SimpleNamespace(
        MARKETS=[{"asset": "BTC-5m", "slug_prefix": "btc-updown-5m", "window_seconds": 300, "fee_rate": FEE}],
        STARTING_BANKROLL=1000.0, POLL_SECONDS=3, EXTRA_SLIPPAGE=0.0, EQUITY_EVERY_SECONDS=30,
        SETTLE_CHECK_SECONDS=30, SETTLE_WARN_SECONDS=3600, BOOK_LEVELS_LOGGED=5, DB_PATH=db,
        SETTLE_MAX_LOOKUPS=6, PROVISIONAL_RECHECK_SECONDS=60)


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)   # Windows: open sqlite files
        self.db = os.path.join(self.dir.name, "t.db")

    def tearDown(self):
        self.dir.cleanup()

    def test_provisional_result_is_corrected(self):
        feed, store = FakeFeed(), Storage(self.db)
        eng = Engine(cfg(self.db), [PreviousWinner("pw")], feed, store)
        eng.setup()
        slug = "btc-updown-5m-3000"
        b = eng.brokers["pw"]
        s = snap(3020, *feed.books(None), m=MarketInfo("BTC-5m", slug, 3000, 3300, "u", "d", "q", FEE))
        for f in fill_all(b, [Order(UP, BUY, shares=20)], s):
            store.log_fill(f)
        eng.unresolved[slug] = ("BTC-5m", 3300)
        feed.result[slug] = (DOWN, False)                 # provisional: DOWN
        eng.settle_finished(3310)
        cash_after_loss = b.cash
        self.assertIn(slug, eng.provisional)
        feed.result[slug] = (UP, True)                    # official: UP
        eng.settle_finished(3400)
        self.assertNotIn(slug, eng.provisional)
        self.assertAlmostEqual(b.cash, cash_after_loss + 20)
        store.commit()
        row = store.db.execute("SELECT winner, payout FROM settlements WHERE slug=?", (slug,)).fetchone()
        self.assertEqual((row["winner"], row["payout"]), ("UP", 20))
        # a restart rebuilds the corrected cash
        eng2 = Engine(cfg(self.db), [PreviousWinner("pw")], feed, Storage(self.db))
        eng2.setup()
        self.assertAlmostEqual(eng2.brokers["pw"].cash, b.cash)

    def test_assets_change_warns(self):
        store = Storage(self.db)
        self.assertIsNone(store.register_strategy("x", "C", {}, 1000, None, 0))
        self.assertIsNone(store.register_strategy("x", "C", {}, 1000, None, 0))
        self.assertIsNotNone(store.register_strategy("x", "C", {}, 1000, ["BTC-5m"], 0))

    def test_settlement_lookups_are_capped(self):
        feed, store = FakeFeed(), Storage(self.db)
        calls = []
        orig = feed.resolution_detail
        feed.resolution_detail = lambda slug, we=None: (calls.append(slug), orig(slug, we))[1]
        c = cfg(self.db)
        c.SETTLE_MAX_LOOKUPS = 3
        eng = Engine(c, [], feed, store)
        eng.setup()
        for i in range(10):
            eng.unresolved[f"btc-updown-5m-{i * 300}"] = ("BTC-5m", i * 300 + 300)
        eng.settle_finished(10_000)
        self.assertEqual(len(calls), 3)


class MetricsTests(unittest.TestCase):
    def test_hedge_is_one_round_trip_and_sold_shares_skip_calibration(self):
        import pandas as pd
        fills = pd.DataFrame([
            dict(ts=1, strategy="a", asset="X", slug="s1", window_end=300, outcome="UP", side="BUY", shares=10, avg_price=0.4, usd=4.0, fee=0.1, reason=""),
            dict(ts=2, strategy="a", asset="X", slug="s1", window_end=300, outcome="DOWN", side="BUY", shares=10, avg_price=0.55, usd=5.5, fee=0.1, reason=""),
            dict(ts=3, strategy="b", asset="X", slug="s1", window_end=300, outcome="UP", side="BUY", shares=10, avg_price=0.5, usd=5.0, fee=0.1, reason=""),
            dict(ts=4, strategy="b", asset="X", slug="s1", window_end=300, outcome="UP", side="SELL", shares=10, avg_price=0.6, usd=6.0, fee=0.1, reason=""),
        ])
        sett = pd.DataFrame([
            dict(ts=301, strategy="a", asset="X", slug="s1", winner="UP", up_shares=10, down_shares=10, net_cost=9.7, payout=10, pnl=0.3),
            dict(ts=301, strategy="b", asset="X", slug="s1", winner="UP", up_shares=0, down_shares=0, net_cost=-0.8, payout=0, pnl=0.8),
        ])
        d = {"fills": fills, "settlements": sett}
        rt = round_trips(d)
        a = rt[rt.strategy == "a"]
        self.assertEqual(len(a), 1)
        self.assertEqual(a.iloc[0].outcome, "BOTH")
        self.assertAlmostEqual(a.iloc[0].pnl, 0.3)
        cal = calibration(d)
        self.assertEqual(set(cal.strategy), {"a"})        # b sold everything before the result


class FixTests(unittest.TestCase):
    """Regression tests for the 2026-11-07 review fixes."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = os.path.join(self.dir.name, "t.db")

    def tearDown(self):
        self.dir.cleanup()

    # 1) out-of-sync books
    def test_books_consistent(self):
        from core.models import books_consistent
        ok_up, ok_dn = book(bids=[(0.71, 10)], asks=[(0.72, 10)]), book(bids=[(0.28, 10)], asks=[(0.29, 10)])
        self.assertTrue(books_consistent(ok_up, ok_dn))
        # real example from the live database: asks sum to 0.99, i.e. a "pair" for under $1
        self.assertFalse(books_consistent(book(bids=[(0.67, 10)], asks=[(0.70, 10)]),
                                          book(bids=[(0.18, 10)], asks=[(0.29, 10)])))
        self.assertFalse(books_consistent(book(bids=[(0.60, 10)]), book(bids=[(0.45, 10)])))   # bids > 1
        self.assertTrue(books_consistent(book(bids=[(0.99, 10)]), book(bids=[(0.01, 10)])))    # decided round
        skew_up, skew_dn = ok_up.copy(), ok_dn.copy()
        skew_up.ts, skew_dn.ts = 100.0, 103.0
        self.assertFalse(books_consistent(skew_up, skew_dn))

    def test_engine_skips_tick_on_inconsistent_books(self):
        feed, store = FakeFeed(), Storage(self.db)
        bad = (book(bids=[(0.40, 1000)], asks=[(0.41, 1000)]), book(bids=[(0.50, 1000)], asks=[(0.52, 1000)]))
        calls = []
        feed.books = lambda info: (calls.append(1), bad)[1]
        eng = Engine(cfg(self.db), [], feed, store)
        eng.setup()
        eng._tick_asset(cfg(self.db).MARKETS[0], 3020)
        self.assertEqual(len(calls), 2)                   # fetched again once
        self.assertEqual(eng.skipped_ticks, 1)
        self.assertEqual(store.db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0], 0)

    # 2) calibration buckets
    def test_calibration_bucket_edges(self):
        import pandas as pd
        rows = [dict(ts=i, strategy="a", asset="X", slug=f"s{i}", window_end=300, outcome="UP", side="BUY",
                     shares=10, avg_price=p, usd=10 * p, fee=0.0, reason="") for i, p in enumerate((0.60, 0.70, 0.35))]
        sett = pd.DataFrame([dict(ts=301, strategy="a", asset="X", slug=f"s{i}", winner="UP", up_shares=10,
                                  down_shares=0, net_cost=1, payout=10, pnl=1) for i in range(3)])
        cal = calibration({"fills": pd.DataFrame(rows), "settlements": sett})
        self.assertEqual(sorted(cal.bucket), [0.35, 0.60, 0.70])

    # 3) results history
    def test_results_are_ordered_unbroken_and_survive_restart(self):
        feed, store = FakeFeed(), Storage(self.db)
        eng = Engine(cfg(self.db), [], feed, store)
        eng.setup()
        for start, w in ((3600, DOWN), (3000, UP), (3300, UP)):       # settled out of order
            slug = f"btc-updown-5m-{start}"
            eng.unresolved[slug] = ("BTC-5m", start + 300)
            feed.result[slug] = (w, True)
        eng.settle_finished(5000)
        store.commit()
        info = MarketInfo("BTC-5m", "btc-updown-5m-3900", 3900, 4200, "u", "d", "q", FEE)
        self.assertEqual(eng._recent_results(info), [UP, UP, DOWN])
        # rounds nobody traded are saved too
        self.assertEqual(store.db.execute("SELECT COUNT(*) FROM resolutions").fetchone()[0], 3)
        # a gap ends the run
        later = MarketInfo("BTC-5m", "btc-updown-5m-4500", 4500, 4800, "u", "d", "q", FEE)
        self.assertEqual(eng._recent_results(later), [])
        eng2 = Engine(cfg(self.db), [], feed, Storage(self.db), clock=lambda: 5000)
        eng2.setup()
        self.assertEqual(eng2._recent_results(info), [UP, UP, DOWN])

    # 4) profit per $1 ignores open rounds
    def test_edge_per_dollar_uses_settled_rounds_only(self):
        import pandas as pd
        from core.metrics import leaderboard
        f = pd.DataFrame([
            dict(ts=1, strategy="a", asset="X", slug="s1", window_end=300, outcome="UP", side="BUY", shares=20, avg_price=0.5, usd=10.0, fee=0.0, reason=""),
            dict(ts=2, strategy="a", asset="X", slug="s2", window_end=600, outcome="UP", side="BUY", shares=20, avg_price=0.5, usd=10.0, fee=0.0, reason=""),
        ])
        s = pd.DataFrame([dict(ts=301, strategy="a", asset="X", slug="s1", winner="UP", up_shares=20, down_shares=0,
                               net_cost=10, payout=20, pnl=10)])
        strat = pd.DataFrame([dict(name="a", bankroll=1000.0)])
        lb = leaderboard({"strategies": strat, "fills": f, "settlements": s, "equity": pd.DataFrame(columns=["strategy", "ts", "equity"])})
        self.assertAlmostEqual(lb.iloc[0]["Edge per $1 bought"], 1.0)    # 10 profit / 10 bought (not / 20)

    # 5) break-even line includes the fee
    def test_break_even_curve(self):
        from core.metrics import break_even_curve
        be = break_even_curve(0.07)
        self.assertAlmostEqual(be.loc[50, "win_rate"], 0.5175)

    # 6) a rejected order is retried
    def test_rejected_entry_is_retried(self):
        pw, b = PreviousWinner("pw"), PaperBroker("pw", 1000)
        st = {}
        s = snap(1020, book(asks=[(0.50, 1000)]), book())
        c = TickContext(snap=s, position=b.position(s), cash=b.cash, state=st, prev_winner=UP)
        tight = [Order(o.outcome, o.side, usd=o.usd, limit=0.40) for o in pw.on_tick(c)]   # limit below the ask
        self.assertEqual(fill_all(b, tight, s), [None])
        s2 = snap(1025, s.up, s.down)
        c2 = TickContext(snap=s2, position=b.position(s2), cash=b.cash, state=st, prev_winner=UP,
                         traded_this_window=s2.market.slug in b.traded_slugs)
        self.assertEqual(len(pw.on_tick(c2)), 1)          # tries again instead of skipping the round

    # 7) fee schedule from the API
    def test_market_fees_from_gamma(self):
        from core.polymarket import market_fees
        raw = {"feesEnabled": True, "feeSchedule": {"exponent": 1, "rate": 0.07, "takerOnly": True}}
        self.assertEqual(market_fees(raw, 0.0), (0.07, 1.0))
        self.assertEqual(market_fees({"feesEnabled": False}, 0.07), (0.0, 1.0))
        self.assertEqual(market_fees({}, 0.05), (0.05, 1.0))
        self.assertAlmostEqual(taker_fee(100, 0.5, 0.07), 1.75)      # Polymarket's documented example
        self.assertAlmostEqual(taker_fee(100, 0.3, 0.07), 1.47)

    # 8) minimum share size
    def test_min_order_shares(self):
        m = MarketInfo("BTC-5m", "btc-updown-5m-1000", 1000, 1300, "tu", "td", "q", FEE, min_order_shares=5)
        b = PaperBroker("t", 1000)
        s = snap(1010, book(bids=[(0.50, 1000)], asks=[(0.51, 1000)]), book(), m=m)
        fill_all(b, [Order(UP, BUY, usd=10)], s)                     # usd-sized buy: $1 rule only
        held = b.positions[m.slug].up
        self.assertIsNone(fill_all(b, [Order(UP, SELL, shares=3)], s)[0])   # 3 shares ($1.50) < 5-share minimum
        self.assertIsNotNone(fill_all(b, [Order(UP, SELL, shares=6)], s)[0])
        self.assertIsNone(fill_all(b, [Order(UP, BUY, shares=4)], s)[0])
        self.assertAlmostEqual(b.positions[m.slug].up, held - 6)


def us_market(slug="btc-updown-5m-1794085200", start=1794085200, window=300):
    return MarketInfo("BTC-5m", slug, start, start + window, "m", "m", "q", 0.0, single_instrument=True,
                      qty_step=0.01, min_notional=0.0, exchange_slug="cpc-" + slug)


class EmptyDatabaseTests(unittest.TestCase):
    """A brand-new database (e.g. a fresh Raspberry Pi) has no settlements yet: charts must still work."""

    def test_new_database_loads_with_all_chart_columns(self):
        from core import metrics
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            path = os.path.join(d, "new.db")
            store = Storage(path)
            store.register_strategy("x", "C", {}, 1000, None, 0)
            store.commit()
            data = metrics.load(path, equity_window=3600)
            for t in ("fills", "settlements", "equity", "snapshots"):
                self.assertIn("time", data[t])
            self.assertIn("cum_pnl", metrics.realized_timeline(data))
            self.assertIn("cum_pnl", metrics.cumulative_by_window(data))
            self.assertEqual(len(metrics.leaderboard(data)), 1)
            store.db.close()


class PolymarketUSTests(unittest.TestCase):
    """Numbers from https://docs.polymarket.us/fees (checked 2026-11-07)."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = os.path.join(self.dir.name, "t.db")

    def tearDown(self):
        self.dir.cleanup()

    def test_fee_examples_from_the_docs(self):
        from core.fees import USFees
        fm = USFees()
        m = us_market()
        self.assertAlmostEqual(fm.fee([(100, 0.50)], 0.0695, m), 1.74)     # "Max (100-lot at $0.50) $1.74"
        self.assertAlmostEqual(fm.fee([(1000, 0.50)], 0.0695, m), 17.38)   # 17.375 -> banker's -> 17.38
        self.assertAlmostEqual(fm.fee([(100, 0.01)], 0.0695, m), 0.07)     # schedule table, $0.01 row
        self.assertAlmostEqual(fm.fee([(100, 0.10)], 0.0695, m), 0.63)     # schedule table, $0.10 row
        self.assertAlmostEqual(fm.fee([(1, 0.50)], 0.0695, m), 0.02)       # 0.017375 -> 0.02
        self.assertAlmostEqual(fm.fee([(0.1, 0.50)], 0.0695, m), 0.00)     # tiny trades can round to $0

    def test_volume_definition_and_tiers(self):
        from core.fees import USFees, fill_volume
        self.assertAlmostEqual(fill_volume("BUY", 1000, 0.10), 100)        # docs: buy 1,000 at $0.10 -> $100
        self.assertAlmostEqual(fill_volume("SELL", 1000, 0.90), 100)       # docs: sell 1,000 at $0.90 -> $100
        self.assertAlmostEqual(fill_volume("SELL", 1000, 0.30), 700)       # docs: sell 1,000 at $0.30 -> $700
        fm, m = USFees(), us_market()
        day0 = 1794085200                                                  # Nov 7 2026
        self.assertEqual(fm.theta(day0, m), 0.095)                          # no volume -> tier 1
        fm.record(day0, "BUY", 30_000, 0.5)                                 # $15k of volume today
        self.assertEqual(fm.theta(day0 + 60, m), 0.095)                     # today's volume doesn't count yet
        self.assertEqual(fm.theta(day0 + 86400, m), 0.085)                  # next day: $15k -> tier 2
        self.assertEqual(fm.theta(day0 + 31 * 86400, m), 0.095)             # 30-day window has passed
        fm.record(day0, "BUY", 600_000, 0.5)                                # $315k total
        self.assertEqual(fm.theta(day0 + 86400, m), 0.0695)
        self.assertAlmostEqual(fm.rebate_pct(day0 + 86400), 0.03)           # $250k-$1M -> 3% taker rebate

    def test_new_account_window(self):
        from core.fees import USFees
        m = us_market()
        fm = USFees(new_account="per_strategy")
        t = 1794085200
        self.assertEqual(fm.theta(t, m), 0.0695)                            # day 1
        fm.record(t, "BUY", 40, 0.5)
        self.assertEqual(fm.theta(t + 6 * 86400, m), 0.0695)                # day 7
        self.assertEqual(fm.theta(t + 7 * 86400, m), 0.095)                 # day 8: tiers ($20 volume)
        fm2 = USFees(new_account="per_strategy")
        fm2.record(t, "BUY", 6_000, 0.5)                                    # $3k in the first week
        self.assertEqual(fm2.theta(t + 7 * 86400, m), 0.085)                # 3k x 30/7 = $12.9k -> tier 2

    def test_book_mirror_and_parse(self):
        from core.polymarket_us import parse_book, event_slug
        data = {"marketData": {"bids": [{"px": {"value": "0.4800"}, "qty": "400"}, {"px": {"value": "0.4700"}, "qty": "10"}],
                               "offers": [{"px": {"value": "0.5200"}, "qty": "3.93"}],
                               "state": "MARKET_STATE_OPEN", "transactTime": "2026-11-07T21:00:15.309232424Z"}}
        up, down, state = parse_book(data)
        self.assertEqual((up.best_bid, up.best_ask), (0.48, 0.52))
        self.assertEqual((down.best_bid, down.best_ask), (0.48, 0.52))     # NO bid = 1 - YES ask, NO ask = 1 - YES bid
        self.assertEqual(down.asks[0][1], 400)
        self.assertEqual(state, "MARKET_STATE_OPEN")
        self.assertEqual(event_slug("btc-updown-15m", 1794085200), "btc-updown-15m-2026-11-07-2100z")

    def test_buying_other_side_closes_position(self):
        from core.fees import USFees
        m = us_market()
        b = PaperBroker("s", 1000, fee_model=USFees())
        up = book(bids=[(0.60, 1000)], asks=[(0.62, 1000)])
        down = book(bids=[(0.38, 1000)], asks=[(0.40, 1000)])
        s = snap(m.window_start + 20, up, down, m=m)
        books = {UP: up.copy(), DOWN: down.copy()}
        f1 = b.execute_many(Order(UP, BUY, usd=62), s, books)
        self.assertEqual(len(f1), 1)
        # $62 must cover price + fee: 62 / (0.62 + 0.095 x 0.62 x 0.38) = 96.517 -> floored to 0.01 contracts
        self.assertAlmostEqual(f1[0].shares, 96.51)
        self.assertAlmostEqual(f1[0].usd + f1[0].fee, 62, delta=0.01)
        cash = b.cash
        f2 = b.execute_many(Order(DOWN, BUY, shares=50), s, {UP: up.copy(), DOWN: down.copy()})
        self.assertEqual([(f.outcome, f.side) for f in f2], [(UP, SELL)])   # sold 50 UP at 1 - 0.40
        self.assertAlmostEqual(f2[0].avg_price, 0.60)
        self.assertAlmostEqual(b.cash, cash + 50 * 0.60 - f2[0].fee)
        pos = b.positions[m.slug]
        self.assertAlmostEqual(pos.up, 46.51)
        self.assertAlmostEqual(pos.down, 0.0)
        f3 = b.execute_many(Order(DOWN, BUY, shares=60), s, {UP: up.copy(), DOWN: down.copy()})
        self.assertEqual([(f.outcome, f.side) for f in f3], [(UP, SELL), (DOWN, BUY)])   # close 46.51, open 13.49
        self.assertAlmostEqual(f3[0].shares, 46.51)
        self.assertAlmostEqual(f3[1].shares, 13.49)
        self.assertAlmostEqual(f3[0].fee + f3[1].fee, 1.37, places=6)     # one order: 0.095 x 60 x 0.4 x 0.6 = 1.368

    def test_stake_is_ten_percent_of_portfolio_value(self):
        b = PaperBroker("s", 1000)
        s = snap(1020, book(asks=[(0.50, 1000)]), book())
        c = TickContext(snap=s, position=b.position(s), cash=b.cash, equity=800.0)
        self.assertAlmostEqual(c.stake(0.10), 80.0)
        c2 = TickContext(snap=s, position=b.position(s), cash=50.0, equity=800.0)
        self.assertAlmostEqual(c2.stake(0.10), 50.0)                        # never more than free cash

    def test_strategy_stops_at_200_and_stays_stopped(self):
        feed, store = FakeFeed(), Storage(self.db)
        c = cfg(self.db)
        c.STOP_EQUITY = 200.0
        eng = Engine(c, [PreviousWinner("pw")], feed, store)
        eng.setup()
        eng.brokers["pw"].cash = 250.0
        self.assertFalse(eng._check_stop("pw", eng.brokers["pw"], 1000))
        eng.brokers["pw"].cash = 200.0
        self.assertTrue(eng._check_stop("pw", eng.brokers["pw"], 1001))
        eng.brokers["pw"].cash = 900.0
        self.assertTrue(eng._check_stop("pw", eng.brokers["pw"], 1002))    # stopped for good
        eng2 = Engine(c, [PreviousWinner("pw")], feed, Storage(self.db), clock=lambda: 3020)
        eng2.setup()
        self.assertIn("pw", eng2.stopped)                                   # survives a restart
        eng2.winners["btc-updown-5m-2700"] = UP
        eng2._tick_asset(c.MARKETS[0], 3020)                                # would buy UP if it were trading
        self.assertEqual(eng2.store.db.execute("SELECT COUNT(*) FROM fills").fetchone()[0], 0)

    def test_engine_trades_ten_percent_with_us_fees(self):
        feed, store = FakeFeed(), Storage(self.db)
        c = cfg(self.db)
        c.EXCHANGE, c.STOP_EQUITY = "polymarket_us", 200.0
        eng = Engine(c, [PreviousWinner("pw")], feed, store, clock=lambda: 3020)
        eng.setup()
        eng.winners["btc-updown-5m-2700"] = UP
        eng._tick_asset(c.MARKETS[0], 3020)
        f = store.db.execute("SELECT usd, fee, theta FROM fills").fetchone()
        self.assertAlmostEqual(f["usd"] + f["fee"], 100.0, delta=0.01)      # 10% of $1,000
        self.assertEqual(f["theta"], 0.095)
        self.assertAlmostEqual(f["fee"] * 100 % 1, 0.0, places=6)           # whole cents


class LongRunningTests(unittest.TestCase):
    """Things that only matter when the bot runs 24/7 for months (Raspberry Pi)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = os.path.join(self.tmp.name, "t.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_every_us_request_bypasses_the_cdn_cache(self):
        """Cloudflare caches the gateway for 30s (and 'not found' for minutes): each GET must be unique."""
        from core.polymarket_us import PolymarketUSFeed
        feed, seen = PolymarketUSFeed(), []

        class Resp:
            status_code = 200

            def json(self):
                return {"ok": True}

            def raise_for_status(self):
                pass

        class Sess:
            headers = {}

            def get(self, url, params=None, timeout=None):
                seen.append(dict(params))
                return Resp()

        feed._local.session = Sess()
        feed._get("/v1/markets/x/book")
        feed._get("/v1/asset-prices/history", {"assets": "crypto:btc"})
        self.assertEqual(len({p["_"] for p in seen}), 2)                 # a different nonce every time
        self.assertEqual(seen[1]["assets"], "crypto:btc")               # caller's params are kept

    def test_incremental_drawdown_matches_full_scan(self):
        from core.metrics import DrawdownTracker, max_drawdowns
        store = Storage(self.db)
        curve = [1000, 1100, 990, 1050, 1200, 900, 950]
        tracker = DrawdownTracker(self.db)
        for i, eq in enumerate(curve):
            store.log_equity(i, "a", eq, 0.0)
            store.log_equity(i, "b", 1000 + i, 0.0)
            store.commit()
            if i in (2, 4):
                tracker.update()                       # read part of the history, then the rest later
        full, inc = max_drawdowns(self.db), tracker.update()
        self.assertAlmostEqual(inc["a"], full["a"])
        self.assertAlmostEqual(inc["a"], 900 / 1200 - 1)
        self.assertAlmostEqual(inc["b"], 0.0)
        store.db.close()

    def test_old_order_books_are_pruned_and_trades_kept(self):
        store = Storage(self.db)
        m = market()
        for ts in (100, 200, 300, 400):
            s = snap(ts, book([(0.4, 10)], [(0.5, 10)]), book([(0.5, 10)], [(0.6, 10)]), m)
            store.log_snapshot(s)
            store.log_book(s)
        store.commit()
        self.assertEqual(store.prune_books(250, batch=1), 1)     # batches: one row at a time
        self.assertEqual(store.prune_books(250), 1)
        self.assertEqual([r[0] for r in store.db.execute("SELECT ts FROM books ORDER BY ts")], [300, 400])
        self.assertEqual(store.db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0], 4)
        store.db.close()

    def test_engine_prunes_books_by_config(self):
        feed, store = FakeFeed(), Storage(self.db)
        c = cfg(self.db)
        c.BOOKS_KEEP_DAYS = 1
        m = market()
        for ts in (10, 2 * 86400):
            s = snap(ts, book([(0.4, 10)], [(0.5, 10)]), book([(0.5, 10)], [(0.6, 10)]), m)
            store.log_book(s)
        store.commit()
        eng = Engine(c, [], feed, store, clock=lambda: 2 * 86400 + 5)
        eng._prune_books(2 * 86400 + 5)
        self.assertEqual(store.db.execute("SELECT COUNT(*) FROM books").fetchone()[0], 1)
        store.db.close()


class MarketStatsTests(unittest.TestCase):
    def test_parse_stats_and_log(self):
        import tempfile, os
        from core.polymarket_us import parse_stats
        from core.storage import Storage
        data = {"marketData": {"transactTime": "2026-11-09T17:13:07.373262418Z", "stats": {
            "openPx": {"value": "0.4100"}, "lowPx": {"value": "0.0100"}, "highPx": {"value": "0.4600"},
            "lastTradePx": {"value": "0.0100"}, "lastTradeQty": "1.7800", "sharesTraded": "45739.5100",
            "notionalTraded": {"value": "9024.2636"}, "openInterest": "11435.5800"}}}
        st = parse_stats(data)
        self.assertEqual(st["shares_traded"], 45739.51)
        self.assertEqual(st["open_interest"], 11435.58)
        self.assertIsNone(parse_stats({"marketData": {}}))
        d = tempfile.mkdtemp()
        store = Storage(os.path.join(d, "t.db"))
        store.log_stats(1.0, "BTC-15m", "btc-updown-15m-1", 600, st)
        row = store.db.execute("SELECT * FROM market_stats").fetchone()
        self.assertEqual(row["notional_traded"], 9024.2636)
        store.db.close()


if __name__ == "__main__":
    unittest.main()
