"""
Clock-drift and uptime monitor (core/health.py): readings every minute, warn / critical alerts as events,
critical drift invalidates the overlapping rounds, restarts are recorded, and trading is never halted.

    python -m unittest discover -s tests -v
"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import health, validity
from core.health import Health
from core.storage import Storage

CFG = {"check_seconds": 60, "warn_seconds": 0.5, "critical_seconds": 1.5, "expected_latency_seconds": 0.15,
       "min_samples": 5, "method": "exchange"}


def store():
    return Storage(os.path.join(tempfile.mkdtemp(), "h.db"))


def feed(h, t0, drift, n=30, latency=(0.12, 0.2, 0.35, 0.6)):
    """n book polls starting at t0 (2s apart) on a clock that is `drift` seconds ahead of the exchange."""
    for i in range(n):
        h.sample(t0 + 2 * i, t0 + 2 * i - drift - latency[i % len(latency)])


def events(st):
    return [(r["kind"], json.loads(r["data"])) for r in st.db.execute("SELECT kind, data FROM events ORDER BY rowid")]


def readings(st):
    return [(r["level"], r["value"]) for r in st.db.execute("SELECT level, value FROM health ORDER BY rowid")]


class ClockTests(unittest.TestCase):
    def test_normal_operation_raises_no_alert(self):
        st = store()
        h = Health(st, CFG, ntp_reader=lambda: True)
        for minute in range(5):
            feed(h, 1000.0 + 60 * minute, drift=0.0)
            r = h.check(1060.0 + 60 * minute)
            self.assertEqual(r["level"], "ok")
            self.assertAlmostEqual(r["drift"], -0.03, places=2)       # fastest poll .12s - expected .15s
        self.assertEqual(events(st), [])
        self.assertEqual([lv for lv, _ in readings(st)], ["ok"] * 5)   # every reading is stored
        st.db.close()

    def test_drift_goes_warn_then_critical_and_flags_the_round(self):
        st = store()
        h = Health(st, CFG, ntp_reader=lambda: True)
        feed(h, 9600.0, drift=0.2)
        self.assertEqual(h.check(9660.0)["level"], "ok")
        feed(h, 9660.0, drift=0.7)
        self.assertEqual(h.check(9720.0)["level"], "warn")
        feed(h, 9720.0, drift=2.0)
        r = h.check(9780.0)
        self.assertEqual(r["level"], "critical")
        self.assertAlmostEqual(r["drift"], 2.0 - 0.03, places=2)
        ev = events(st)
        self.assertEqual([(k, d["level"]) for k, d in ev], [("clock_drift", "warn"), ("clock_drift", "critical")])
        self.assertAlmostEqual(ev[0][1]["drift"], 0.67, places=2)
        self.assertEqual([lv for lv, _ in readings(st)], ["ok", "warn", "critical"])
        # a fully recorded round that overlaps the critical alert is invalid (clock_drift); its neighbour is not
        for start in (9300, 9600):
            for i in range(150):
                st.db.execute("INSERT INTO snapshots (ts, asset, slug, spot, fv_up) VALUES (?,?,?,?,?)",
                              (start + 1 + 2 * i, "BTC-5m", f"btc-updown-5m-{start}", 100.0, 0.5))
        hit = validity.assess_round(st.db, "btc-updown-5m-9600", "BTC-5m", 9600, 9900, 2.0, None)
        ok = validity.assess_round(st.db, "btc-updown-5m-9300", "BTC-5m", 9300, 9600, 2.0, None)
        self.assertEqual((hit["valid"], hit["reason"], hit["clock_drift"]), (0, "clock_drift", 1))
        self.assertEqual((ok["valid"], ok["reason"]), (1, None))
        st.db.close()

    def test_a_clock_that_is_behind_alerts_too(self):
        st = store()
        h = Health(st, CFG, ntp_reader=lambda: True)
        feed(h, 1000.0, drift=-3.0)
        r = h.check(1060.0)
        self.assertEqual(r["level"], "critical")
        self.assertLess(r["drift"], -2.9)
        st.db.close()

    def test_too_few_samples_is_no_reading_not_an_alert(self):
        st = store()
        h = Health(st, CFG, ntp_reader=lambda: True)
        feed(h, 1000.0, drift=5.0, n=3)
        self.assertEqual(h.check(1060.0)["level"], "nodata")
        self.assertEqual(events(st), [])
        self.assertEqual(readings(st), [("nodata", None)])
        st.db.close()

    def test_losing_ntp_sync_warns(self):
        st = store()
        h = Health(st, {**CFG, "method": "both"}, ntp_reader=lambda: False)
        feed(h, 1000.0, drift=0.0)
        r = h.check(1060.0)
        self.assertEqual((r["level"], r["why"], r["ntp_synchronized"]), ("warn", "ntp_unsynchronized", False))
        self.assertEqual(events(st)[0][1]["level"], "warn")
        only_ntp = Health(st, {**CFG, "method": "ntp"}, ntp_reader=lambda: True)
        self.assertEqual(only_ntp.check(1120.0)["level"], "ok")
        st.db.close()

    def test_disabled_writes_nothing(self):
        st = store()
        h = Health(st, {"enabled": False})
        h.start(1000.0)
        feed(h, 1000.0, drift=9.0)
        h.tick(1100.0)
        self.assertEqual(st.db.execute("SELECT COUNT(*) FROM bot_runs").fetchone()[0], 0)
        self.assertEqual(readings(st), [])
        st.db.close()


class UptimeTests(unittest.TestCase):
    def test_starts_heartbeats_and_downtime(self):
        st = store()
        a = Health(st, CFG, ntp_reader=lambda: True)
        a.start(1000.0, "abc123")
        a.tick(1030.0)                                             # not due yet
        a.tick(1061.0)
        a.tick(1125.0)
        row = st.db.execute("SELECT process, started_at, last_seen, code_version FROM bot_runs").fetchone()
        self.assertEqual(tuple(row), ("bot", 1000.0, 1125.0, "abc123"))
        b = Health(st, CFG, ntp_reader=lambda: True)               # the bot restarts 8 minutes later
        b.start(1605.0, "def456")
        b.tick(1670.0)
        runs = st.db.execute("SELECT started_at, last_seen FROM bot_runs ORDER BY id").fetchall()
        self.assertEqual([tuple(r) for r in runs], [(1000.0, 1125.0), (1605.0, 1670.0)])
        text = "\n".join(health.report(st.db, now=1700.0))
        self.assertIn("2 start(s) recorded", text)
        self.assertIn("after 8m down", text)
        self.assertIn("RUNNING", text)
        self.assertIn("NOT RUNNING", "\n".join(health.report(st.db, now=5000.0)))
        # a start inside a round invalidates that round (and only that one)
        self.assertEqual(st.db.execute("SELECT COUNT(*) FROM bot_runs WHERE started_at > 1500 AND started_at <= 1800")
                         .fetchone()[0], 1)
        st.db.close()

    def test_report_shows_drift_and_last_alert(self):
        st = store()
        h = Health(st, CFG, ntp_reader=lambda: True)
        h.start(1000.0, "abc123")
        feed(h, 1000.0, drift=0.9)
        h.check(1060.0)
        feed(h, 1060.0, drift=0.0)
        h.check(1120.0)
        st.db.commit()
        text = "\n".join(health.report(st.db, now=1150.0))
        self.assertIn("drift -0.030s vs the exchange  [OK]", text)
        self.assertIn("last alert: WARN", text)
        self.assertIn("1 warning(s), 0 critical", text)
        empty = sqlite3.connect(":memory:")
        self.assertIn("no health records", health.report(empty, 0.0)[0])
        st.db.close()

    def test_recorder_heartbeat_is_written_to_its_own_database(self):
        from core.ws_store import WsStore
        path = os.path.join(tempfile.mkdtemp(), "ws.db")
        ws = WsStore(path, {}, clock=lambda: 5000.0)
        ws._housekeeping(5000.0)
        ws._housekeeping(5030.0)
        ws._housekeeping(5075.0)
        ws.flush()
        meta = dict(ws.db.execute("SELECT key, value FROM ws_meta").fetchall())
        self.assertEqual(float(meta["last_seen"]), 5075.0)
        self.assertIn("started_5000", meta)
        st = store()
        Health(st, CFG, ntp_reader=lambda: True).check(5080.0)
        text = "\n".join(health.report(st.db, now=5100.0, ws_con=ws.db))
        self.assertIn("RECORDER 1 start(s) recorded", text)
        self.assertIn("last seen 25s ago", text)
        ws.close()
        st.db.close()


class EngineTests(unittest.TestCase):
    def test_critical_drift_never_stops_trading(self):
        from tests.test_orderlog import Buy, book, make
        eng, clock = make([Buy("b", usd=25.0)], [book(0.51)])
        real_books = eng.feed.books

        def books_with_exchange_time(info):                        # the exchange's clock is 3s behind ours
            up, down = real_books(info)
            up.ts = down.ts = clock.t - 3.0
            return up, down
        eng.feed.books = books_with_exchange_time
        for _ in range(40):                                        # 80 seconds of ticks
            eng.tick(clock.t)
            clock.t += 2
        levels = [r[0] for r in eng.store.db.execute("SELECT level FROM health WHERE kind='clock'")]
        self.assertIn("critical", levels)
        ev = eng.store.db.execute("SELECT COUNT(*) FROM events WHERE strategy='_system' AND kind='clock_drift' "
                                  "AND data LIKE '%\"level\":\"critical\"%'").fetchone()[0]
        self.assertGreaterEqual(ev, 1)
        self.assertEqual(eng.store.db.execute("SELECT COUNT(*) FROM fills").fetchone()[0], 1)       # it traded
        self.assertGreater(eng.store.db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0], 35)  # and kept recording
        self.assertEqual(eng.store.db.execute("SELECT COUNT(*) FROM bot_runs").fetchone()[0], 1)
        eng.store.db.close()


if __name__ == "__main__":
    unittest.main()
