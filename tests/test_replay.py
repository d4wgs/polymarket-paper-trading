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


# ---------------------------------------------------------------------------------- websocket latency book
import json

from core.replay import ReplayFeed, WsBookReader
from core.ws_store import SCHEMA as WS_SCHEMA

EXCH = "cpc-btc-updown-5m-1000"
SLUG = "btc-updown-5m-1000"


def make_ws_db(path, rows, gaps=()):
    """rows: (recv_ts, bids, offers); gaps: (ts, kind)."""
    c = sqlite3.connect(path)
    c.executescript(WS_SCHEMA)
    for ts, bids, offers in rows:
        c.execute("INSERT INTO ws_book (recv_ts, slug, state, bids, offers) VALUES (?,?,?,?,?)",
                  (ts, EXCH, "OPEN", json.dumps(bids), json.dumps(offers)))
    for ts, kind in gaps:
        c.execute("INSERT INTO ws_gaps VALUES (?,?,?)", (ts, kind, ""))
    c.commit()
    c.close()


A = ([[0.40, 10.0]], [[0.45, 5.0]])
B = ([[0.41, 20.0]], [[0.46, 6.0]])
C = ([[0.42, 30.0]], [[0.47, 7.0]])


class WsBookReaderTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "ws.db")

    def reader(self, rows, gaps=(), **kw):
        make_ws_db(self.path, rows, gaps)
        return WsBookReader(self.path, **kw)

    def test_picks_the_snapshot_in_force_at_decision_time_plus_latency(self):
        r = self.reader([(100.0, *A), (100.3, *B), (100.8, *C)])
        top = lambda t: r.book_at(EXCH, t)[0].bids[0][0]
        self.assertEqual(top(100.0 + 0.100), 0.40)             # 100 ms: still A
        self.assertEqual(top(100.0 + 0.250), 0.40)             # 250 ms: B only arrives at 300 ms
        self.assertEqual(top(100.0 + 0.400), 0.41)             # 400 ms: B
        self.assertEqual(top(100.0 + 0.600), 0.41)
        self.assertEqual(top(100.0 + 0.900), 0.42)             # 900 ms: C
        self.assertAlmostEqual(r.book_at(EXCH, 100.4)[2], 0.1)  # age of the snapshot used

    def test_down_book_is_the_mirror_of_the_yes_book(self):
        r = self.reader([(100.0, [[0.40, 10.0], [0.39, 4.0]], [[0.45, 5.0], [0.46, 8.0]])])
        up, down, _, why = r.book_at(EXCH, 100.1)
        self.assertIsNone(why)
        self.assertEqual(up.bids, [(0.40, 10.0), (0.39, 4.0)])
        self.assertEqual(up.asks, [(0.45, 5.0), (0.46, 8.0)])
        self.assertEqual(down.bids, [(0.55, 5.0), (0.54, 8.0)])      # 1 - UP offers, best (highest) first
        self.assertEqual(down.asks, [(0.60, 10.0), (0.61, 4.0)])     # 1 - UP bids, best (lowest) first

    def test_gaps_overlapping_the_window_refuse_the_book(self):
        r = self.reader([(100.0, *A), (100.7, *B)],
                        gaps=[(100.1, "disconnect"), (100.5, "connect"), (200.0, "bad_json")])
        self.assertIsNone(r.book_at(EXCH, 100.05)[3])                # before the gap: fine
        self.assertEqual(r.book_at(EXCH, 100.3)[3], "gap")           # inside the disconnect
        self.assertEqual(r.book_at(EXCH, 100.6)[3], "gap")           # newest row (100.0) predates the gap
        self.assertIsNone(r.book_at(EXCH, 100.8)[3])                 # a snapshot arrived after the reconnect
        self.assertEqual(r.book_at(EXCH, 100.8)[0].bids[0][0], 0.41)
        r2 = self.reader([(300.0, *A)], gaps=[(250.0, "stop")])      # stopped and never reconnected
        self.assertEqual(r2.gaps[-1][1], float("inf"))
        self.assertEqual(r2.book_at(EXCH, 300.1)[3], "gap")

    def test_invalid_stale_and_missing_books_are_refused(self):
        r = self.reader([(100.0, [[0.50, 1.0]], [[0.45, 1.0]]), (300.0, *A)], max_age=30)
        self.assertEqual(r.book_at(EXCH, 100.1)[3], "invalid")       # crossed
        self.assertEqual(r.book_at(EXCH, 50.0)[3], "no_row")
        self.assertEqual(r.book_at(EXCH, 340.0)[3], "stale")         # newest snapshot is 40 s old
        self.assertEqual(r.book_at("other-slug", 300.1)[3], "no_row")


class WsFeedTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "ws.db")
        self.src = types.SimpleNamespace(windows={SLUG: ("BTC-5m", 1000, 1300, EXCH, None)})
        self.info = types.SimpleNamespace(asset="BTC-5m", slug=SLUG)

    def feed(self, rows, gaps=(), mode="ws"):
        make_ws_db(self.path, rows, gaps)
        clock = ReplayClock(0.0)
        f = ReplayFeed(self.src, clock, [{"asset": "BTC-5m"}], mode, WsBookReader(self.path))
        f.last_event = Ev(1, 100.0, "BTC-5m", SLUG, [[0.30, 1.0]], [[0.35, 1.0]], [[0.65, 1.0]], [[0.70, 1.0]],
                          50000.0, 0.5, None, None)
        return f, clock

    def test_follow_up_uses_the_ws_book_at_decision_plus_latency_and_counts_it(self):
        f, clock = self.feed([(99.0, *A), (100.3, *B)])
        clock.t = 100.0 + 0.4                                         # the engine slept 400 ms
        up, down = f.books(self.info)
        self.assertEqual(up.bids[0], (0.41, 20.0))
        self.assertEqual(down.asks[0][0], 0.59)
        self.assertEqual([(r[2], r[3]) for r in f.latency_log], [("ws", None)])
        self.assertEqual(f.last_event.spot, 50000.0)                  # spot / sigma still those of the decision tick

    def test_gap_falls_back_to_the_decision_book_and_marks_it(self):
        f, clock = self.feed([(99.0, *A)], gaps=[(99.5, "disconnect"), (100.2, "connect")])
        clock.t = 100.25
        up, _ = f.books(self.info)
        self.assertEqual(up.bids[0], (0.30, 1.0))                     # the decision-time book
        self.assertEqual([(r[2], r[3]) for r in f.latency_log], [("no-latency-book", "gap")])

    def test_ws_mode_requires_the_recording(self):
        with self.assertRaises(ValueError):
            ReplayFeed(self.src, ReplayClock(0.0), [], "ws", None)


class WsLatencyRegression(unittest.TestCase):
    """The default (and 'same') behaviour is untouched, and ws mode with no usable ws data == the old behaviour."""

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        cls.src = os.path.join(cls.dir, "live.db")
        record_simulation(cls.src, hours=1.5)
        c = sqlite3.connect(cls.src)
        cls.t0, cls.t1 = c.execute("SELECT MIN(ts), MAX(ts) FROM books").fetchone()
        c.close()
        cls.empty_ws = os.path.join(cls.dir, "ws_empty.db")
        make_ws_db(cls.empty_ws, [])

    def run_(self, name, **kw):
        out = os.path.join(self.dir, name + ".db")
        run_replay(config, self.src, out, self.t0, self.t1 + 1, only=NAMES, latency_ms=250, **kw)
        c = sqlite3.connect(out)
        rows = c.execute("SELECT ts, strategy, slug, outcome, side, shares, avg_price, fee FROM fills "
                         "ORDER BY ts, strategy").fetchall()
        sett = c.execute("SELECT strategy, slug, pnl FROM settlements ORDER BY strategy, slug").fetchall()
        lb = c.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='latency_books'").fetchone()[0]
        c.close()
        return rows, sett, lb

    def test_option_off_is_identical_and_leaves_no_trace(self):
        default = self.run_("default")
        same = self.run_("same", latency_book="same")
        self.assertGreater(len(default[0]), 0)
        self.assertEqual(default[:2], same[:2])
        self.assertEqual(default[2], 0)                                # no latency_books table when the option is off

    def test_ws_mode_with_no_ws_data_falls_back_to_exactly_the_old_result(self):
        same = self.run_("same2", latency_book="same")
        ws = self.run_("out_ws_fallback", latency_book="ws", ws_book_path=self.empty_ws)
        self.assertEqual(same[:2], ws[:2])
        c = sqlite3.connect(os.path.join(self.dir, "out_ws_fallback.db"))
        n = c.execute("SELECT COUNT(*), SUM(source='no-latency-book') FROM latency_books").fetchone()
        c.close()
        self.assertGreater(n[0], 0)
        self.assertEqual(n[0], n[1])                                   # every fetch fell back

    def test_replay_never_writes_the_ws_database(self):
        before = digest(self.empty_ws)
        self.run_("ws_ro", latency_book="ws", ws_book_path=self.empty_ws)
        self.assertEqual(before, digest(self.empty_ws))
