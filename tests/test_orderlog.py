"""
The order log (core/orderlog.py): decision-time price / depth, fill ratio, slippage and reason codes for every
order, without changing a single fill.

    python -m unittest discover -s tests -v
"""
import os
import sqlite3
import sys
import tempfile
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import orderlog
from core.engine import Engine
from core.models import DOWN, UP, Book, MarketInfo
from core.storage import Storage
from strategies.base import Strategy

START, END = 9600, 9900
THETA = 0.095


def book(up_ask, depth=1000.0, levels=None):
    """(UP book, DOWN book) of a one-instrument market. `levels`: explicit UP asks [(price, size), ...]."""
    asks = levels or [(up_ask, depth)]
    bid = round(asks[0][0] - 0.01, 6)
    up = Book(bids=[(bid, depth)], asks=list(asks))
    down = Book(bids=[(round(1 - p, 6), q) for p, q in asks], asks=[(round(1 - bid, 6), depth)])
    return up, down


class ScriptFeed:
    """books() returns the scripted books one call at a time (the last one repeats); None = no book."""

    def __init__(self, script):
        self.script, self.i = list(script), 0
        self.info = MarketInfo("BTC-5m", f"btc-updown-5m-{START}", START, END, "u", "d", single_instrument=True,
                               qty_step=0.01, min_notional=0.0, price_to_beat=100.0)

    def current_market(self, cfg, now):
        return self.info

    def books(self, info):
        b = self.script[min(self.i, len(self.script) - 1)]
        self.i += 1
        return (None, None) if b is None else (b[0].copy(), b[1].copy())

    def index_price(self, info, now):
        return None

    def resolution_detail(self, slug, window_end=None):
        return None, False


def make(strategies, script, risk=None):
    clock = types.SimpleNamespace(t=9700.0)
    cfg = types.SimpleNamespace(
        MARKETS=[{"asset": "BTC-5m", "kind": "5m", "window_seconds": 300}],
        STARTING_BANKROLL=1000.0, EXTRA_SLIPPAGE=0.0, POLL_SECONDS=2, SETTLE_CHECK_SECONDS=15,
        EQUITY_EVERY_SECONDS=5, SETTLE_WARN_SECONDS=3600, EXCHANGE="polymarket_us", STOP_EQUITY=None,
        LATENCY_SECONDS=0.25, RISK=risk or {}, MAKER={}, EXPORT_DIR=None)
    eng = Engine(cfg, strategies, ScriptFeed(script), Storage(os.path.join(tempfile.mkdtemp(), "t.db")),
                 clock=lambda: clock.t, sleep=lambda s: setattr(clock, "t", clock.t + s))
    eng.setup()
    return eng, clock


def ticks(eng, clock, n=1):
    for _ in range(n):
        eng.tick(clock.t)
        clock.t += 2


def orders(eng):
    return [dict(r) for r in eng.store.db.execute("SELECT * FROM orders ORDER BY id")]


def fills(eng):
    return [dict(r) for r in eng.store.db.execute("SELECT * FROM fills ORDER BY rowid")]


class Buy(Strategy):
    PARAMS = {"usd": None, "shares": None, "limit": None, "side": UP, "repeat": False}

    def on_tick(self, ctx):
        if ctx.has_position() and not self.p["repeat"]:
            return []
        return [ctx.buy(self.p["side"], usd=self.p["usd"], shares=self.p["shares"], max_price=self.p["limit"])]


class TakerLogTests(unittest.TestCase):
    def test_full_fill_records_decision_and_execution_books(self):
        # decision book: ask .51 x 1000; the book fetched after the latency: ask .52 x 300 then .53 x 1000
        eng, clock = make([Buy("b", usd=25.0)], [book(0.51), book(0.52, levels=[(0.52, 300.0), (0.53, 1000.0)])])
        ticks(eng, clock)
        (o,), (f,) = orders(eng), fills(eng)
        self.assertEqual((o["kind"], o["outcome"], o["side"], o["status"], o["reason"]),
                         ("taker", UP, "BUY", "filled", None))
        self.assertEqual((o["intended_usd"], o["intended_shares"], o["limit_px"]), (25.0, None, 0.53))   # ask + 2c
        # decision time = the snapshot the strategy saw
        self.assertEqual((o["decision_bid"], o["decision_ask"], o["decision_depth"]), (0.50, 0.51, 1000.0))
        self.assertAlmostEqual(o["decision_depth_usd"], 510.0)
        self.assertAlmostEqual(o["ts"], 9700.0)
        # execution = the book the broker actually filled against (both levels are within the limit)
        self.assertEqual((o["exec_bid"], o["exec_ask"], o["exec_depth"]), (0.51, 0.52, 1300.0))
        self.assertAlmostEqual(o["exec_ts"], 9700.25)
        # result equals the fill row
        self.assertAlmostEqual(o["filled_shares"], f["shares"])
        self.assertAlmostEqual(o["avg_price"], f["avg_price"])
        self.assertAlmostEqual(o["fee"], f["fee"])
        self.assertAlmostEqual(o["avg_price"], 0.52)
        self.assertAlmostEqual(o["slippage"], 0.01)                      # paid 1c more than the ask at decision time
        self.assertGreaterEqual(o["fill_ratio"], 0.99)
        self.assertEqual(o["attempts"], 1)
        # the fill points at its order and carries the decision-time columns
        self.assertEqual((f["order_id"], f["decision_bid"], f["decision_ask"], f["decision_depth"]),
                         (o["id"], 0.50, 0.51, 1000.0))
        self.assertAlmostEqual(f["slippage"], 0.01)
        self.assertIsNotNone(o["code_version"])
        eng.store.db.close()

    def test_price_moved_is_one_row_with_a_repeat_count(self):
        eng, clock = make([Buy("b", usd=25.0, repeat=True)], [book(0.51), book(0.60)] * 3)
        ticks(eng, clock, 3)
        (o,) = orders(eng)
        self.assertEqual((o["status"], o["reason"], o["attempts"], o["filled_shares"], o["fill_ratio"]),
                         ("unfilled", "price_moved", 3, 0.0, 0.0))
        self.assertEqual((o["decision_ask"], o["exec_ask"], o["exec_depth"]), (0.51, 0.60, 0.0))
        self.assertIsNone(o["slippage"])
        self.assertGreater(o["last_ts"], o["ts"])
        self.assertEqual(fills(eng), [])
        eng.store.db.close()

    def test_limit_already_below_the_market(self):
        eng, clock = make([Buy("b", usd=10.0, limit=0.30)], [book(0.51)])
        ticks(eng, clock)
        self.assertEqual([(o["status"], o["reason"]) for o in orders(eng)], [("unfilled", "limit_not_marketable")])
        eng.store.db.close()

    def test_partial_fill_when_the_book_is_too_thin(self):
        eng, clock = make([Buy("b", shares=20.0, limit=0.51)], [book(0.51, levels=[(0.51, 5.0), (0.55, 1000.0)])])
        ticks(eng, clock)
        o = orders(eng)[0]
        self.assertEqual((o["status"], o["reason"], o["intended_shares"], o["filled_shares"], o["decision_depth"]),
                         ("partial", "no_liquidity", 20.0, 5.0, 5.0))
        self.assertAlmostEqual(o["fill_ratio"], 0.25)
        self.assertAlmostEqual(o["slippage"], 0.0)
        eng.store.db.close()

    def test_risk_rejection_and_kill_switch(self):
        eng, clock = make([Buy("b", usd=25.0, repeat=True)], [book(0.51)])
        eng.risk.kill_active = True
        ticks(eng, clock, 2)
        (o,) = orders(eng)
        self.assertEqual((o["status"], o["reason"], o["attempts"]), ("rejected", "risk_limit", 2))
        self.assertIn("kill switch", o["detail"])
        eng.store.db.close()

    def test_risk_clip_is_a_partial_with_reason_risk_limit(self):
        eng, clock = make([Buy("b", usd=100.0)], [book(0.51)], risk={"max_usd_per_trade": 50})
        ticks(eng, clock)
        o = orders(eng)[0]
        self.assertEqual((o["status"], o["reason"], o["intended_usd"]), ("partial", "risk_limit", 100.0))
        self.assertAlmostEqual(o["fill_ratio"], 0.5, places=2)
        self.assertIn("clipped", o["detail"])
        eng.store.db.close()

    def test_no_book_after_the_latency(self):
        eng, clock = make([Buy("b", usd=25.0)], [book(0.51), None, None])
        ticks(eng, clock)
        o = orders(eng)[0]
        self.assertEqual((o["status"], o["reason"], o["exec_ask"]), ("unfilled", "latency_book_empty", None))
        self.assertEqual(o["decision_ask"], 0.51)
        eng.store.db.close()

    def test_closing_buy_on_one_instrument_is_in_the_orders_terms(self):
        class InOut(Strategy):
            PARAMS = {}
            def on_tick(self, ctx):
                if not ctx.has_position() and ctx.entries_this_window == 0:
                    return [ctx.buy(UP, shares=10.0)]
                if ctx.position.up > 0:
                    return [ctx.buy(DOWN, shares=10.0)]             # booked as a SELL of UP at the UP bid
                return []
        eng, clock = make([InOut("io")], [book(0.51)])
        ticks(eng, clock, 2)
        o = orders(eng)[1]
        f = fills(eng)[1]
        self.assertEqual((f["outcome"], f["side"]), (UP, "SELL"))
        self.assertAlmostEqual(f["avg_price"], 0.50)
        self.assertEqual((o["outcome"], o["side"], o["status"]), (DOWN, "BUY", "filled"))
        self.assertAlmostEqual(o["avg_price"], 0.50)                # DOWN bought at 1 - UP bid
        self.assertAlmostEqual(o["decision_ask"], 0.50)
        self.assertAlmostEqual(o["slippage"], 0.0)
        self.assertEqual((f["decision_bid"], f["order_id"]), (0.50, o["id"]))     # the fill's own side: UP bid
        self.assertAlmostEqual(f["slippage"], 0.0)
        eng.store.db.close()


class MakerLogTests(unittest.TestCase):
    class Bidder(Strategy):
        PARAMS = {"price": 0.45, "cancel_at": None}

        def on_tick(self, ctx):
            if self.p["cancel_at"] is not None and ctx.ts >= self.p["cancel_at"]:
                return [ctx.cancel()] if ctx.quotes else []
            return [] if (ctx.quotes or ctx.has_position()) else [ctx.bid(UP, self.p["price"], 20)]

    def test_resting_then_cancelled_is_quote_not_traded_through(self):
        eng, clock = make([self.Bidder("mm", cancel_at=9704.0)], [book(0.51)])
        ticks(eng, clock)
        self.assertEqual([(o["kind"], o["status"], o["intended_shares"], o["limit_px"], o["decision_ask"])
                          for o in orders(eng)], [("maker", "resting", 20.0, 0.45, 0.51)])
        ticks(eng, clock, 3)
        (o,) = orders(eng)
        self.assertEqual((o["status"], o["reason"], o["detail"], o["filled_shares"]),
                         ("unfilled", "quote_not_traded_through", "cancelled", 0.0))
        eng.store.db.close()

    def test_trade_through_fills_and_links_the_fill(self):
        # tick 1: bid .45 rests under ask .51; tick 2: the market trades down through it (ask .40)
        eng, clock = make([self.Bidder("mm")], [book(0.51), book(0.40)])
        ticks(eng, clock, 2)
        o, (f,) = orders(eng)[0], fills(eng)
        self.assertEqual((o["status"], o["filled_shares"], o["avg_price"], o["fill_ratio"]), ("filled", 20.0, 0.45, 1.0))
        self.assertAlmostEqual(o["slippage"], 0.45 - 0.51)          # bought 6c under the ask it saw
        self.assertEqual((f["liquidity"], f["order_id"], f["decision_ask"]), ("maker", o["id"], 0.51))
        eng.store.db.close()

    def test_bid_that_would_cross_is_rejected(self):
        eng, clock = make([self.Bidder("mm", price=0.55)], [book(0.51)])
        ticks(eng, clock)
        self.assertEqual([(o["status"], o["reason"]) for o in orders(eng)], [("rejected", "would_cross")])
        eng.store.db.close()


class NoBehaviourChangeTests(unittest.TestCase):
    """Fills must be the same with the order log working, broken, or absent."""

    SCRIPT = [book(0.51), book(0.52, levels=[(0.52, 30.0), (0.53, 1000.0)]), book(0.60), book(0.61), book(0.40)]

    def run_once(self, breakage=None):
        eng, clock = make([Buy("a", usd=25.0), Buy("b", shares=40.0, limit=0.52), Buy("c", usd=10.0, limit=0.3),
                           MakerLogTests.Bidder("mm", price=0.45)], self.SCRIPT, risk={"max_usd_per_trade": 50})
        if breakage:
            breakage(eng)
        ticks(eng, clock, 3)
        cols = "strategy, ts, outcome, side, shares, avg_price, usd, fee, liquidity, reason"
        out = eng.store.db.execute(f"SELECT {cols} FROM fills ORDER BY rowid").fetchall()
        cash = {n: b.cash for n, b in eng.brokers.items()}
        n_orders = eng.store.db.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        eng.store.db.close()
        return [tuple(r) for r in out], cash, n_orders

    def test_fills_identical_when_the_order_log_fails_or_is_silent(self):
        base_fills, base_cash, n = self.run_once()
        self.assertGreater(len(base_fills), 2)
        self.assertGreater(n, 3)

        def explode(eng):
            def boom(*a, **k):
                raise RuntimeError("disk full")
            eng.store.log_order = boom
            eng.store.update_order = boom
            eng.store.bump_order = boom
        import logging
        logging.getLogger("orderlog").disabled = True
        try:
            broken_fills, broken_cash, n_broken = self.run_once(explode)
        finally:
            logging.getLogger("orderlog").disabled = False
        self.assertEqual(n_broken, 0)
        self.assertEqual(base_fills, broken_fills)
        self.assertEqual(base_cash, broken_cash)


class SummaryTests(unittest.TestCase):
    def test_summary_counts_fill_rate_slippage_and_reasons(self):
        eng, clock = make([Buy("a", usd=25.0), Buy("c", usd=10.0, limit=0.3, repeat=True)],
                          [book(0.51), book(0.52)] * 3)
        ticks(eng, clock, 3)
        rows = {r["strategy"]: r for r in orderlog.summary(eng.store.db)}
        a, c = rows["a"], rows["c"]
        self.assertEqual((a["orders"], a["fill_rate"], a["full_fill_rate"], a["unfilled"]), (1, 1.0, 1.0, {}))
        self.assertAlmostEqual(a["avg_slippage_c"], 1.0)
        self.assertAlmostEqual(a["median_depth_x_size"], 1000.0 / (25.0 / 0.51))
        self.assertEqual((c["orders"], c["attempts"], c["fill_rate"]), (1, 3, 0.0))
        self.assertEqual((c["unfilled"], c["unfilled_attempts"]),
                         ({"limit_not_marketable": 1}, {"limit_not_marketable": 3}))
        self.assertIn("limit_not_marketable 1", orderlog.render_summary(list(rows.values())))
        slug = eng.store.db.execute("SELECT slug FROM orders LIMIT 1").fetchone()[0]
        self.assertEqual(orderlog.summary(eng.store.db, exclude_slugs={slug}), [])
        eng.store.db.close()

    def test_old_database_gets_the_order_columns_once(self):
        from tests.test_version import LEGACY_SCHEMA
        path = os.path.join(tempfile.mkdtemp(), "old.db")
        con = sqlite3.connect(path)
        con.executescript(LEGACY_SCHEMA)
        con.execute("INSERT INTO fills VALUES (1.0,'old','BTC-5m','s-300',600.0,'UP','BUY',10,0.5,5.0,0.02,'r',0.095,0)")
        con.commit()
        con.close()
        for _ in range(2):
            st = Storage(path)
            cols = [r[1] for r in st.db.execute("PRAGMA table_info(fills)")]
            row = st.db.execute("SELECT order_id, decision_ask, slippage, shares FROM fills").fetchone()
            st.db.close()
            self.assertEqual(cols.count("order_id"), 1)
            self.assertEqual(tuple(row), (None, None, None, 10.0))


if __name__ == "__main__":
    unittest.main()
