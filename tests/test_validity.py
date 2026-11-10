"""
Round validity (core/validity.py): a data-coverage verdict per round, identical for every strategy, never based
on P&L; statistics leave invalid rounds out by default and put them back, unchanged, on request.

    python -m unittest discover -s tests -v
"""
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from core import analytics, metrics, validity
from core.models import UP
from core.storage import Storage

CFG = {"max_gap_seconds": 10, "min_coverage": 0.90, "max_missing_share": 0.10}
POLL = 2.0


def ticks(start, end, skip=()):
    """Tick times every POLL seconds from start+1; `skip` = (from, to) stretches with no ticks."""
    out, t = [], start + 1.0
    while t < end:
        if not any(a <= t < b for a, b in skip):
            out.append(t)
        t += POLL
    return out


def verdict(ts, start=0.0, end=300.0, spot=None, fv=None, **kw):
    n = len(ts)
    return validity.assess(ts, spot or [True] * n, fv or [True] * n, start, end, POLL, CFG, **kw)


class AssessTests(unittest.TestCase):
    def test_clean_round_is_valid(self):
        v = verdict(ticks(0, 300))
        self.assertEqual((v["valid"], v["reason"], v["ticks"]), (1, None, 150))
        self.assertAlmostEqual(v["coverage"], 1.0)
        self.assertLessEqual(v["max_gap"], 2.0)

    def test_gap_inside_at_the_start_and_at_the_end(self):
        self.assertEqual(verdict(ticks(0, 300, [(100, 111)]))["reason"], "gap")           # 12s hole, 96% coverage
        self.assertEqual(verdict(ticks(0, 300, [(0, 12)]))["reason"], "gap")              # first tick 13s in
        self.assertEqual(verdict(ticks(0, 300, [(289, 300)]))["reason"], "gap")           # nothing in the last 11s
        self.assertEqual(verdict(ticks(0, 300, [(100, 108)]))["valid"], 1)                # an 8-10s hole is tolerated
        v = verdict(ticks(0, 300, [(100, 111)]))
        self.assertAlmostEqual(v["max_gap"], 12.0)

    def test_low_coverage(self):
        v = verdict(ticks(0, 300, [(50, 58), (90, 98), (130, 138), (170, 178), (210, 218)]))   # 5 x 8s holes
        self.assertEqual((v["valid"], v["reason"]), (0, "coverage"))
        self.assertLess(v["coverage"], 0.90)
        big = verdict(ticks(0, 300, [(100, 240)]))
        self.assertEqual(big["reason"], "gap,coverage")

    def test_missing_spot_or_fair_value(self):
        ts = ticks(0, 300)
        some = [i >= 20 for i in range(len(ts))]                                           # 20 of 150 missing = 13%
        self.assertEqual(verdict(ts, spot=some)["reason"], "no_spot")
        self.assertEqual(verdict(ts, fv=some)["reason"], "no_fair_value")
        few = [i >= 5 for i in range(len(ts))]                                             # 3%: fine (K arrives late)
        self.assertEqual(verdict(ts, fv=few)["valid"], 1)

    def test_restart_and_clock_drift(self):
        self.assertEqual(verdict(ticks(0, 300), restarts=1)["reason"], "restart")
        self.assertEqual(verdict(ticks(0, 300), clock_drift=2)["reason"], "clock_drift")
        self.assertEqual(verdict(ticks(0, 300, [(100, 130)]), restarts=1)["reason"], "gap,restart")

    def test_empty_round_and_ticks_outside_the_round_are_ignored(self):
        self.assertEqual(verdict([])["valid"], 0)
        v = verdict([-5.0] + ticks(0, 300) + [305.0])
        self.assertEqual((v["valid"], v["ticks"]), (1, 150))

    def test_the_rule_never_sees_results(self):
        import inspect
        src = inspect.getsource(validity.assess) + inspect.getsource(validity.assess_round)
        for word in ("pnl", "payout", "winner", "fills", "settlements"):
            self.assertNotIn(word, src)


def make_db(rounds, strategies=("a", "b")):
    """A database with snapshots for each round and one $10 bet per strategy per round.
    rounds: {slug_start: (tick list, pnl per strategy)} for 5-minute rounds."""
    path = os.path.join(tempfile.mkdtemp(), "t.db")
    st = Storage(path)
    for name in strategies:
        st.register_strategy(name, "X", {}, 1000.0, None, 0.0)
    for start, (ts, pnl) in rounds.items():
        slug = f"btc-updown-5m-{start}"
        for t in ts:
            st.db.execute("INSERT INTO snapshots (ts, asset, slug, seconds_left, up_bid, up_ask, spot, fv_up) "
                          "VALUES (?,?,?,?,?,?,?,?)", (t, "BTC-5m", slug, start + 300 - t, 0.49, 0.51, 100.0, 0.5))
        st.db.execute("INSERT INTO windows VALUES (?,?,?,?,?,?)", (slug, "BTC-5m", start, start + 300, slug, 100.0))
        for name in strategies:
            st.db.execute("INSERT INTO fills (ts, strategy, asset, slug, window_end, outcome, side, shares, avg_price, "
                          "usd, fee, liquidity) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                          (start + 50, name, "BTC-5m", slug, start + 300, UP, "BUY", 20.0, 0.5, 10.0, 0.0, "taker"))
            p = pnl[name]
            st.db.execute("INSERT INTO settlements (ts, strategy, asset, slug, winner, up_shares, down_shares, "
                          "net_cost, payout, pnl) VALUES (?,?,?,?,?,?,?,?,?,?)",
                          (start + 320, name, "BTC-5m", slug, UP, 20.0, 0.0, 10.0, 10.0 + p, p))
    st.db.commit()
    return path, st


ROUNDS = {
    9000: (ticks(9000, 9300), {"a": +10.0, "b": -10.0}),                      # clean
    9300: (ticks(9300, 9600, [(9400, 9440)]), {"a": -10.0, "b": +10.0}),      # 40s gap
    9600: (ticks(9600, 9900), {"a": +10.0, "b": +10.0}),                      # the bot restarted in it
    9900: (ticks(9900, 10200), {"a": -10.0, "b": -10.0}),                     # clean
}


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.path, self.st = make_db(ROUNDS)
        self.st.db.execute("INSERT INTO bot_runs (process, started_at, last_seen) VALUES ('bot', 9700.0, 9900.0)")
        self.st.db.commit()

    def tearDown(self):
        self.st.db.close()

    def backfill(self):
        return validity.backfill(self.st.db, self.st.db, POLL, CFG, now=20000.0)

    def test_backfill_marks_gap_and_restart_rounds_once(self):
        n = self.backfill()
        self.assertEqual((n["checked"], n["added"], n["invalid"]), (4, 4, 2))
        got = {r[0]: (r[1], r[2]) for r in self.st.db.execute("SELECT slug, valid, reason FROM round_validity")}
        self.assertEqual(got, {"btc-updown-5m-9000": (1, None), "btc-updown-5m-9300": (0, "gap,coverage"),
                               "btc-updown-5m-9600": (0, "restart"), "btc-updown-5m-9900": (1, None)})
        again = self.backfill()                                            # idempotent: nothing is rewritten
        self.assertEqual((again["checked"], again["added"]), (0, 0))
        self.assertEqual(validity.counts(self.st.db),
                         {"rounds": 4, "invalid": 2, "reasons": {"coverage": 1, "gap": 1, "restart": 1}})

    def test_existing_verdict_is_never_overwritten(self):
        self.backfill()
        row = validity.assess_round(self.st.db, "btc-updown-5m-9300", "BTC-5m", 9300, 9600, POLL,
                                    {**CFG, "max_gap_seconds": 100, "min_coverage": 0.5})
        self.assertEqual(row["valid"], 1)                                  # a looser rule would pass it ...
        self.assertFalse(validity.record(self.st.db, row, 1.0, "test"))    # ... but the stored verdict stays
        self.assertEqual(self.st.db.execute("SELECT valid FROM round_validity WHERE slug='btc-updown-5m-9300'")
                         .fetchone()[0], 0)

    def test_critical_clock_drift_event_invalidates_the_round(self):
        self.st.log_event(9950.0, "_system", "", "", "clock_drift", {"level": "warn", "drift": 0.7})
        row = validity.assess_round(self.st.db, "btc-updown-5m-9900", "BTC-5m", 9900, 10200, POLL, CFG)
        self.assertEqual(row["valid"], 1)                                  # a warning alone does not
        self.st.log_event(9960.0, "_system", "", "", "clock_drift", {"level": "critical", "drift": 2.1})
        row = validity.assess_round(self.st.db, "btc-updown-5m-9900", "BTC-5m", 9900, 10200, POLL, CFG)
        self.assertEqual((row["valid"], row["reason"]), (0, "clock_drift"))

    def test_statistics_exclude_invalid_rounds_for_every_strategy_alike(self):
        self.backfill()
        d = metrics.load(self.path)
        self.assertEqual(d["validity"], {"invalid_rounds": 2, "excluded": True,
                                         "reasons": {"coverage": 1, "gap": 1, "restart": 1},
                                         "invalid_fills": 4, "invalid_settlements": 4})
        for name in ("a", "b"):                                            # the same two rounds are gone for both
            self.assertEqual(sorted(d["settlements"][d["settlements"].strategy == name]["slug"]),
                             ["btc-updown-5m-9000", "btc-updown-5m-9900"])
            self.assertEqual(len(d["fills"][d["fills"].strategy == name]), 2)
        lb = metrics.leaderboard(d).set_index("Strategy")
        self.assertEqual((lb.loc["a", "Windows"], lb.loc["a", "Realized P&L $"]), (2, 0.0))
        self.assertEqual((lb.loc["b", "Windows"], lb.loc["b", "Realized P&L $"]), (2, -20.0))
        # the ACCOUNT value still contains every trade (a: +10 -10 +10 -10 = 0; b: -10 +10 +10 -10 = 0)
        self.assertAlmostEqual(lb.loc["a", "Equity $"], 1000.0)
        self.assertAlmostEqual(lb.loc["b", "Equity $"], 1000.0)
        self.assertIn("EXCLUDE 2 round(s)", metrics.validity_note(d))
        self.assertIn("restart 1", metrics.validity_note(d))
        lab = analytics.load_lab(self.path)
        self.assertEqual(len(lab["settlements"]), 4)
        self.assertEqual(len(analytics.load_lab(self.path, include_invalid=True)["settlements"]), 8)

    def test_including_invalid_rounds_reproduces_the_old_numbers_exactly(self):
        before = os.path.join(tempfile.mkdtemp(), "before.db")
        self.st.db.commit()
        dst = sqlite3.connect(before)                                      # the database before any verdict existed
        self.st.db.backup(dst)
        dst.close()
        old = metrics.load(before)
        self.assertEqual(old["validity"]["invalid_rounds"], 0)
        self.backfill()
        new = metrics.load(self.path, include_invalid=True)
        for t in ("fills", "settlements"):
            pd.testing.assert_frame_equal(old[t], new[t])
        pd.testing.assert_frame_equal(metrics.leaderboard(old), metrics.leaderboard(new))
        pd.testing.assert_frame_equal(metrics.closed_rounds(old["fills"], old["settlements"]),
                                      metrics.closed_rounds(new["fills"], new["settlements"]))
        self.assertIn("INCLUDE 2 round(s)", metrics.validity_note(new))
        self.assertEqual(metrics.validity_note(old), "")
        # raw rows of invalid rounds are never deleted
        con = sqlite3.connect(self.path)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM fills").fetchone()[0], 8)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM settlements").fetchone()[0], 8)
        con.close()

    def test_replay_takes_verdicts_from_a_read_only_source(self):
        self.backfill()
        self.st.db.commit()
        src = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        out_path = os.path.join(tempfile.mkdtemp(), "replay.db")
        out = Storage(out_path)
        n = validity.backfill(src, out.db, POLL, {**CFG, "max_gap_seconds": 999}, now=20000.0, source="replay")
        self.assertEqual((n["added"], n["invalid"]), (4, 2))               # the source's own verdicts are reused
        self.assertEqual(validity.invalid_rounds(out.db),
                         {"btc-updown-5m-9300": "gap,coverage", "btc-updown-5m-9600": "restart"})
        src.close()
        out.db.close()


class EngineTests(unittest.TestCase):
    def test_settling_a_round_stores_its_verdict(self):
        from tests.test_orderlog import book, make
        eng, clock = make([], [book(0.51)])
        slug = f"btc-updown-5m-9600"
        clock.t = 9601.0
        while clock.t < 9899:                                              # a fully recorded round (no spot feed here)
            eng.tick(clock.t)
            clock.t += 2
        eng._settle(slug, "BTC-5m", 9900.0, UP, True, 9910.0)
        r = eng.store.db.execute("SELECT valid, reason, ticks, source FROM round_validity WHERE slug=?", (slug,)).fetchone()
        # (the test engine was started inside this round, which also counts as a restart)
        self.assertEqual((r["valid"], r["reason"], r["source"]), (0, "no_spot,no_fair_value,restart", "live"))
        self.assertGreaterEqual(r["ticks"], 148)
        eng.assess_validity = False                                        # replay mode: the engine stays out of it
        eng.unresolved["btc-updown-5m-9900"] = ("BTC-5m", 10200.0)
        eng._settle("btc-updown-5m-9900", "BTC-5m", 10200.0, UP, True, 10210.0)
        self.assertEqual(eng.store.db.execute("SELECT COUNT(*) FROM round_validity").fetchone()[0], 1)
        eng.store.db.close()


if __name__ == "__main__":
    unittest.main()
