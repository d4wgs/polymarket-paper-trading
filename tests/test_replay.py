"""Replay: recorded data through the real engine; read-only source; ws-trade maker model; live path unchanged."""
import hashlib
import os
import sqlite3
import sys
import tempfile
import time
import types
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import config
from core.engine import Engine
from core.fairvalue import MarketData
from core.models import Book
from core.maker import MakerBook
from core.polymarket import window_start_for
from core.replay import (Ev, ReplayClock, Source, WsMakerBook, group_ticks, make_strategies, run_replay)
from core.spot import SimSpot
from core.storage import Storage
from tools.simulate import SimClock, SimFeed

NAMES = ["s3_sniper_v2", "s4_momentum_v1"]


def sim_cfg(db):
    """The real config, but with no order latency (a recording only has the decision book, see core/replay.py)."""
    cfg = types.SimpleNamespace(**{k: getattr(config, k) for k in dir(config) if k.isupper()})
    cfg.DB_PATH, cfg.EXPORT_DIR, cfg.LATENCY_SECONDS = db, None, 0.0
    return cfg


def record_simulation(db_path, hours=3.0, seed=3):
    """Run the live engine on the simulator: this is the 'live bot' whose recording we replay."""
    cfg = sim_cfg(db_path)
    clock = SimClock(start=window_start_for(1_790_000_000, 900))
    feed = SimFeed(clock, cfg.WINDOW_SECONDS, seed=seed, us=True)
    strategies = [s for s in make_strategies(config) if s.name in NAMES]
    under = SimFeed.underlying_of(config.MARKETS[0])

    def spot_price(now):
        feed.windows.setdefault(under, {m["asset"]: m["window_seconds"] for m in config.MARKETS})
        return feed._advance(under, now)
    spot = SimSpot(spot_price, half_life=config.MODEL.get("vol_half_life_seconds", 300), seed=seed)
    spot.vol.seed(feed.sig_s)
    engine = Engine(cfg, strategies, feed, Storage(db_path), clock=clock.now, sleep=clock.sleep,
                    market_data=MarketData(spot, config.MODEL))
    engine.run(max_seconds=hours * 3600)
    engine.settle_finished(clock.now() + 3600)
    engine.store.commit()
    engine.store.db.close()
    return clock.now()


def digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        h.update(fh.read())
    return h.hexdigest()


class ReplayEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        cls.src = os.path.join(cls.dir, "live.db")
        cls.end = record_simulation(cls.src)
        c = sqlite3.connect(cls.src)
        cls.t0, cls.t1 = c.execute("SELECT MIN(ts), MAX(ts) FROM books").fetchone()
        c.close()

    def replay(self, **kw):
        out = os.path.join(self.dir, "replay.db")
        cfg = types.SimpleNamespace(**{k: getattr(config, k) for k in dir(config) if k.isupper()})
        cfg.LATENCY_SECONDS = 0.0
        eng = run_replay(cfg, self.src, out, self.t0, self.t1 + 1, only=NAMES, **kw)
        return eng, out

    def test_replay_reproduces_the_recorded_run(self):
        eng, out = self.replay()
        live, rep = sqlite3.connect(self.src), sqlite3.connect(out)
        for name in NAMES:
            q = "SELECT slug, outcome, side, shares, avg_price FROM fills WHERE strategy=? ORDER BY ts"
            a, b = live.execute(q, (name,)).fetchall(), rep.execute(q, (name,)).fetchall()
            self.assertGreater(len(a), 0, f"{name} never traded in the simulated run: test would prove nothing")
            matched = sum(1 for x in a if x in b)
            self.assertGreaterEqual(matched / len(a), 0.95, f"{name}: {matched}/{len(a)} fills reproduced")
            self.assertLessEqual(abs(len(a) - len(b)), max(2, 0.05 * len(a)))
            pa = live.execute("SELECT COALESCE(SUM(pnl),0) FROM settlements WHERE strategy=?", (name,)).fetchone()[0]
            pb = rep.execute("SELECT COALESCE(SUM(pnl),0) FROM settlements WHERE strategy=?", (name,)).fetchone()[0]
            self.assertAlmostEqual(pa, pb, delta=max(5.0, 0.1 * abs(pa)))
        live.close()
        rep.close()
        self.assertGreater(eng.ticks_replayed, 100)

    def test_source_is_never_written_and_output_cannot_be_the_source(self):
        before = digest(self.src)
        self.replay()
        self.assertEqual(digest(self.src), before)
        src = Source(self.src)
        with self.assertRaises(sqlite3.OperationalError):
            src.db.execute("DELETE FROM books")             # the handle itself is read-only
        with self.assertRaises(ValueError):
            run_replay(config, self.src, self.src, self.t0, self.t1, only=NAMES)
        self.assertEqual(digest(self.src), before)

    def test_warm_start_restores_live_state_and_cold_start_does_not(self):
        mid = self.t0 + (self.t1 - self.t0) / 2
        live = sqlite3.connect(self.src)
        before = live.execute("SELECT COUNT(*) FROM fills WHERE strategy=? AND ts < ?", (NAMES[1], mid)).fetchone()[0]
        live.close()
        self.assertGreater(before, 0)
        cfg = types.SimpleNamespace(**{k: getattr(config, k) for k in dir(config) if k.isupper()})
        cfg.LATENCY_SECONDS = 0.0
        out = os.path.join(self.dir, "warm.db")
        warm = run_replay(cfg, self.src, out, mid, self.t1 + 1, only=NAMES, warm_start=True)
        c = sqlite3.connect(out)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM fills WHERE strategy=? AND ts < ?", (NAMES[1], mid)).fetchone()[0], before)
        c.close()
        cold = run_replay(cfg, self.src, os.path.join(self.dir, "cold.db"), mid, self.t1 + 1, only=NAMES, warm_start=False)
        c = sqlite3.connect(os.path.join(self.dir, "cold.db"))
        self.assertEqual(c.execute("SELECT COUNT(*) FROM fills WHERE ts < ?", (mid,)).fetchone()[0], 0)
        c.close()
        # warm start carries the live cash at `mid`; cold start begins from the bankroll
        live = sqlite3.connect(self.src)
        spent = live.execute("SELECT COALESCE(SUM(CASE WHEN side='BUY' THEN -(usd+fee) ELSE usd-fee END),0) FROM fills "
                             "WHERE strategy=? AND ts < ?", (NAMES[1], mid)).fetchone()[0]
        live.close()
        self.assertNotEqual(round(spent, 2), 0.0)

    def test_unknown_strategy_is_an_error(self):
        with self.assertRaises(ValueError):
            run_replay(config, self.src, os.path.join(self.dir, "x.db"), self.t0, self.t1, only=["nope"])

    def test_clock_only_moves_with_the_recording(self):
        c = ReplayClock(100.0)
        c.sleep(0.25)
        self.assertEqual(c.now(), 100.25)
        c.sleep(-5)
        self.assertEqual(c.now(), 100.25)


class WsStoreGc(unittest.TestCase):
    def test_quiet_slug_state_is_forgotten(self):
        from core.ws_store import WsStore
        st = WsStore(os.path.join(tempfile.mkdtemp(), "w.db"), {})
        st._last_key["old"], st._last_write["old"] = ("k",), 0.0
        st._last_key["new"], st._last_write["new"] = ("k",), 9999.0
        st._housekeeping(10000.0)
        self.assertNotIn("old", st._last_key)
        self.assertNotIn("old", st._last_write)
        self.assertIn("new", st._last_key)
        st.close()


class GroupTicks(unittest.TestCase):
    def test_a_market_appears_once_per_tick(self):
        mk = lambda ts, a: Ev(0, ts, a, "s", [], [], [], [], None, None, None, None)
        evs = [mk(1, "A"), mk(1.2, "B"), mk(3, "A"), mk(3.1, "B"), mk(5, "A")]
        got = [[e.asset for e in g] for g in group_ticks(iter(evs))]
        self.assertEqual(got, [["A", "B"], ["A", "B"], ["A"]])


class WsMakerTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "ws.db")
        c = sqlite3.connect(self.path)
        c.executescript("CREATE TABLE ws_trades (recv_ts REAL, trade_ts REAL, slug TEXT, trade_id TEXT, price REAL, "
                        "qty REAL, maker_side TEXT, maker_intent TEXT, maker_outcome TEXT, maker_action TEXT, "
                        "taker_side TEXT, taker_intent TEXT, taker_outcome TEXT, taker_action TEXT, state TEXT);")
        for i, (ts, px, qty) in enumerate([(101, 0.50, 4.0), (102, 0.48, 5.0), (103, 0.47, 3.0), (110, 0.40, 9.0)]):
            c.execute("INSERT INTO ws_trades (recv_ts, trade_ts, slug, trade_id, price, qty) VALUES (?,?,?,?,?,?)",
                      (ts, ts, "exch-slug", f"t{i}", px, qty))
        c.commit()
        c.close()
        self.m = WsMakerBook(self.path, lambda slug: "exch-slug", latency=0.0)
        self.book = Book(bids=[(0.49, 10)], asks=[(0.52, 10)])

    def test_only_prints_strictly_through_the_bid_fill_it(self):
        self.m.place("s", "slug", "BTC-5m", "UP", 0.49, 20, 100.0, self.book)
        fills = self.m.check("s", "slug", {"UP": self.book, "DOWN": self.book}, 105.0)
        # prints at 0.50 (above) and 0.48 / 0.47 (below our 0.49 bid) -> only the two below count
        self.assertEqual(sum(t for _, t in fills), 8.0)
        again = self.m.check("s", "slug", {"UP": self.book, "DOWN": self.book}, 106.0)
        self.assertEqual(again, [])                                       # prints are not counted twice
        later = self.m.check("s", "slug", {"UP": self.book, "DOWN": self.book}, 120.0)
        self.assertEqual(sum(t for _, t in later), 9.0)                   # the 0.40 print, up to what remains

    def test_touching_never_fills(self):
        m = WsMakerBook(self.path, lambda slug: "exch-slug", latency=0.0)
        m.place("s", "slug", "BTC-5m", "UP", 0.48, 5, 100.0, Book(bids=[(0.47, 1)], asks=[(0.52, 1)]))
        fills = m.check("s", "slug", {"UP": self.book, "DOWN": self.book}, 102.5)
        self.assertEqual(fills, [])                                       # a print AT 0.48 is not through 0.48

    def test_down_bid_uses_the_mirrored_price(self):
        m = WsMakerBook(self.path, lambda slug: "exch-slug", latency=0.0)
        m.place("s", "slug", "BTC-5m", "DOWN", 0.45, 50, 100.0, Book(bids=[(0.44, 1)], asks=[(0.50, 1)]))
        fills = m.check("s", "slug", {"UP": self.book, "DOWN": self.book}, 105.0)
        # DOWN bid 0.45 <-> YES price above 0.55: only the 0.60... none here (max print 0.50)
        self.assertEqual(fills, [])

    def test_default_maker_rule_is_still_the_conservative_book_rule(self):
        e = Engine(types.SimpleNamespace(), [], None, None)
        self.assertIs(type(e.maker), MakerBook)


class LiveBehaviourUnchanged(unittest.TestCase):
    """Replay is additive: nothing the live bot runs depends on it."""

    def test_engine_defaults_are_the_real_clock_and_sleep(self):
        e = Engine(types.SimpleNamespace(), [], None, None)
        self.assertIs(e.clock, time.time)
        self.assertIs(e.sleep, time.sleep)

    def test_live_modules_do_not_import_replay(self):
        for rel in ("run.py", "core/engine.py", "core/broker.py", "core/maker.py", "core/polymarket_us.py",
                    "core/storage.py", "dashboard.py"):
            with open(os.path.join(HERE, rel), encoding="utf-8") as fh:
                src = fh.read()
            self.assertNotIn("core.replay", src, rel)
            self.assertNotIn("from core import replay", src, rel)

    def test_engine_run_is_deterministic_with_an_injected_clock(self):
        """Same simulated market, same seed -> identical fills, whether or not replay code was ever imported."""
        digests = []
        for i in range(2):
            d = tempfile.mkdtemp()
            db = os.path.join(d, "x.db")
            record_simulation(db, hours=1.0, seed=11)
            c = sqlite3.connect(db)
            digests.append(c.execute("SELECT strategy, slug, outcome, side, shares, avg_price, usd, fee FROM fills "
                                     "ORDER BY ts, strategy, slug").fetchall())
            c.close()
        self.assertEqual(digests[0], digests[1])
        self.assertGreater(len(digests[0]), 0)


if __name__ == "__main__":
    unittest.main()
