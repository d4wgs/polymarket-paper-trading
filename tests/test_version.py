"""
Version stamping (core/version.py, deploy/stamp.py): every strategy registration, fill and settlement records
which code, which strategy source and which parameters produced it.

    python -m unittest discover -s tests -v
"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import version
from core.models import UP
from core.storage import MIGRATIONS, Storage
from strategies.base import Strategy
from strategies.s3_sniper import Sniper
from tests.test_s7_nested_pair import _engine


class BuyOnce(Strategy):
    PARAMS = {"stake_usd": 10.0, "edge": 0.5}

    def on_tick(self, ctx):
        return [ctx.buy(UP, usd=self.p["stake_usd"])] if not ctx.has_position() else []


class HashTests(unittest.TestCase):
    def test_params_hash_is_stable_and_sensitive(self):
        a = version.params_hash({"stake_usd": 25, "min_price": None, "markets": ["5m", "15m"]})
        self.assertEqual(a, version.params_hash({"markets": ["5m", "15m"], "min_price": None, "stake_usd": 25.0}))
        self.assertEqual(len(a), 12)
        self.assertNotEqual(a, version.params_hash({"stake_usd": 26, "min_price": None, "markets": ["5m", "15m"]}))
        self.assertNotEqual(a, version.params_hash({"stake_usd": 25, "min_price": 0.5, "markets": ["5m", "15m"]}))
        self.assertNotEqual(version.params_hash({"x": True}), version.params_hash({"x": 1}))   # bool is not 1

    def test_same_strategy_same_hash_across_instances(self):
        self.assertEqual(version.strategy_stamp(Sniper("a"))["params_hash"],
                         version.strategy_stamp(Sniper("b"))["params_hash"])
        self.assertNotEqual(version.strategy_stamp(Sniper("a"))["params_hash"],
                            version.strategy_stamp(Sniper("a", min_price=0.5))["params_hash"])

    def test_source_hash_ignores_line_endings_and_tracks_the_file(self):
        d = tempfile.mkdtemp()
        lf, crlf, other = (os.path.join(d, n) for n in ("lf.py", "crlf.py", "other.py"))
        open(lf, "wb").write(b"a = 1\nb = 2\n")
        open(crlf, "wb").write(b"a = 1\r\nb = 2\r\n")
        open(other, "wb").write(b"a = 1\nb = 3\n")
        self.assertEqual(version.file_hash(lf), version.file_hash(crlf))
        self.assertNotEqual(version.file_hash(lf), version.file_hash(other))
        self.assertEqual(version.source_hash(Sniper("a")), version.source_hash(Sniper))
        self.assertNotEqual(version.source_hash(Sniper), version.source_hash(BuyOnce))

    def test_version_file_and_dirty_flag(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "VERSION")
        self.assertIsNone(version.read_version_file(p))
        json.dump({"commit": "0123456789abcdef0123", "dirty": False}, open(p, "w"))
        self.assertEqual(version.format_version(version.read_version_file(p)), "0123456789ab")
        json.dump({"commit": "0123456789abcdef0123", "dirty": True}, open(p, "w"))
        self.assertEqual(version.format_version(version.read_version_file(p)), "0123456789ab-dirty")
        self.assertEqual(version.format_version(None), "unknown")

    def test_stamp_script_writes_a_readable_version_file(self):
        from deploy import stamp
        out = os.path.join(tempfile.mkdtemp(), "VERSION")
        rc = stamp.main(["--out", out])
        if rc != 0:
            self.skipTest("not a git checkout")
        v = version.read_version_file(out)
        self.assertEqual(len(v["commit"]), 40)
        self.assertIn("dirty", v)


class StampedRowsTests(unittest.TestCase):
    def test_new_fills_and_settlements_carry_the_stamp(self):
        s = BuyOnce("buyer", assets=["BTC-5m"])
        eng, clock = _engine([s], {"5m": 0.51, "15m": 0.40}, {})
        eng.cfg.CONFIG_VERSION = "cfg123"
        eng.setup()                                    # again, now with a config version
        eng.tick(clock.t)
        f = eng.store.db.execute("SELECT * FROM fills").fetchone()
        self.assertEqual(f["code_version"], version.code_version())
        self.assertEqual(f["params_hash"], version.params_hash(s.p))
        self.assertEqual(f["source_hash"], version.source_hash(BuyOnce))
        self.assertEqual(f["config_version"], "cfg123")
        eng._settle(f["slug"], f["asset"], f["window_end"], UP, True, clock.t + 400)
        st = eng.store.db.execute("SELECT * FROM settlements").fetchone()
        self.assertEqual((st["code_version"], st["params_hash"], st["config_version"]),
                         (version.code_version(), version.params_hash(s.p), "cfg123"))
        row = eng.store.db.execute("SELECT * FROM strategies WHERE name='buyer'").fetchone()
        self.assertEqual(row["params_hash"], version.params_hash(s.p))
        eng.store.db.close()


class RegistrationWarningTests(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "t.db")

    def reg(self, strategy, **kw):
        st = Storage(self.path)                        # a fresh Storage = a bot restart
        try:
            return st.register_strategy(strategy.name, type(strategy).__name__, strategy.p, 1000.0, None, 0.0,
                                        stamp={**version.strategy_stamp(strategy, "cfg"), **kw})
        finally:
            st.db.close()

    def versions(self):
        con = sqlite3.connect(self.path)
        try:
            return con.execute("SELECT params_hash, source_hash, code_version FROM strategy_versions "
                               "ORDER BY rowid").fetchall()
        finally:
            con.close()

    def test_unchanged_params_never_warn_across_restarts(self):
        self.assertIsNone(self.reg(BuyOnce("b")))
        self.assertIsNone(self.reg(BuyOnce("b")))
        self.assertIsNone(self.reg(BuyOnce("b", stake_usd=10)))            # 10 == 10.0
        self.assertEqual(len(self.versions()), 1)

    def test_changed_param_warns_naming_both_hashes(self):
        old, new = BuyOnce("b"), BuyOnce("b", edge=0.7)
        self.assertIsNone(self.reg(old))
        w = self.reg(new)
        self.assertIn(version.params_hash(old.p), w)
        self.assertIn(version.params_hash(new.p), w)
        self.assertIn("edge", w)                                           # the key that changed is named
        self.assertNotIn("stake_usd", w)
        self.assertIsNone(self.reg(new))                                   # recorded: the same settings don't warn again
        self.assertEqual(len(self.versions()), 2)

    def test_changed_source_warns_and_new_code_version_is_recorded_quietly(self):
        self.assertIsNone(self.reg(BuyOnce("b"), source_hash="aaaaaaaaaaaa"))
        w = self.reg(BuyOnce("b"), source_hash="bbbbbbbbbbbb")
        self.assertIn("source_hash aaaaaaaaaaaa -> bbbbbbbbbbbb", w)
        self.assertIsNone(self.reg(BuyOnce("b"), source_hash="bbbbbbbbbbbb", code_version="newcommit123"))
        self.assertEqual([v[2] for v in self.versions()][-1], "newcommit123")
        self.assertEqual(len(self.versions()), 3)


LEGACY_SCHEMA = """
CREATE TABLE strategies (name TEXT PRIMARY KEY, class TEXT, params TEXT, bankroll REAL, assets TEXT, created_at REAL);
CREATE TABLE fills (ts REAL, strategy TEXT, asset TEXT, slug TEXT, window_end REAL, outcome TEXT, side TEXT,
    shares REAL, avg_price REAL, usd REAL, fee REAL, reason TEXT, theta REAL, rebate REAL);
CREATE TABLE settlements (ts REAL, strategy TEXT, asset TEXT, slug TEXT, winner TEXT, up_shares REAL,
    down_shares REAL, net_cost REAL, payout REAL, pnl REAL);
"""


class MigrationTests(unittest.TestCase):
    """Old databases gain the stamp columns once; existing rows are untouched and read as unversioned (NULL)."""

    def make_legacy(self):
        path = os.path.join(tempfile.mkdtemp(), "old.db")
        con = sqlite3.connect(path)
        con.executescript(LEGACY_SCHEMA)
        con.execute("INSERT INTO strategies VALUES ('old','BuyOnce',?,1000.0,'null',1.0)",
                    (json.dumps({"edge": 0.5, "stake_usd": 10.0}, sort_keys=True),))
        con.execute("INSERT INTO fills VALUES (1.0,'old','BTC-5m','s-300',600.0,'UP','BUY',10,0.5,5.0,0.02,'r',0.095,0)")
        con.execute("INSERT INTO settlements VALUES (700.0,'old','BTC-5m','s-300','UP',10,0,5.02,10,4.98)")
        con.commit()
        con.close()
        return path

    @staticmethod
    def dump(path):
        con = sqlite3.connect(path)
        try:
            cols = {t: [r[1] for r in con.execute(f"PRAGMA table_info({t})")]
                    for t in ("strategies", "fills", "settlements")}
            rows = {t: con.execute(f"SELECT * FROM {t}").fetchall() for t in cols}
            return cols, rows
        finally:
            con.close()

    def test_migration_is_idempotent_and_keeps_old_rows(self):
        path = self.make_legacy()
        Storage(path).db.close()
        first = self.dump(path)
        Storage(path).db.close()
        Storage(path).db.close()
        self.assertEqual(first, self.dump(path))
        cols, rows = first
        for table, col, _ in MIGRATIONS:
            if table in cols:
                self.assertEqual(cols[table].count(col), 1)
        f = dict(zip(cols["fills"], rows["fills"][0]))
        self.assertEqual((f["shares"], f["avg_price"], f["fee"]), (10, 0.5, 0.02))
        self.assertIsNone(f["code_version"])                 # unversioned
        self.assertIsNone(f["params_hash"])
        s = dict(zip(cols["settlements"], rows["settlements"][0]))
        self.assertEqual((s["pnl"], s["code_version"]), (4.98, None))

    def test_legacy_name_same_settings_is_quiet_changed_settings_names_hashes(self):
        path = self.make_legacy()
        st = Storage(path)
        self.assertIsNone(st.register_strategy("old", "BuyOnce", BuyOnce("old").p, 1000.0, None, 5.0,
                                               stamp=version.strategy_stamp(BuyOnce("old"))))
        st.db.close()
        path = self.make_legacy()
        st = Storage(path)
        new = BuyOnce("old", edge=0.9)
        w = st.register_strategy("old", "BuyOnce", new.p, 1000.0, None, 5.0, stamp=version.strategy_stamp(new))
        self.assertIn(version.params_hash({"edge": 0.5, "stake_usd": 10.0}), w)
        self.assertIn(version.params_hash(new.p), w)
        # the old strategies row itself is never rewritten
        self.assertEqual(json.loads(st.db.execute("SELECT params FROM strategies WHERE name='old'").fetchone()[0]),
                         {"edge": 0.5, "stake_usd": 10.0})
        st.db.close()


if __name__ == "__main__":
    unittest.main()
