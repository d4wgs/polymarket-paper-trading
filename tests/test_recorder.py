"""Websocket recorder: storage schema/throttle/retention, reconnect + gaps + watchdog, subscriptions, secrets."""
import asyncio
import base64
import json
import logging
import os
import sqlite3
import tempfile
import time
import unittest

from core.recorder import Recorder, target_slugs
from core.ws_store import WsStore


def book_msg(slug="cpc-btc-updown-5m-X", bids=None, offers=None, shares="100", ts="2026-11-09T17:30:00.5Z"):
    bids = bids or [("0.49", "10"), ("0.48", "20")]
    offers = offers or [("0.51", "5"), ("0.52", "7")]
    lv = lambda xs: [{"px": {"value": p, "currency": "USD"}, "qty": q} for p, q in xs]
    return {"requestId": f"md:{slug}", "subscriptionType": "SUBSCRIPTION_TYPE_MARKET_DATA",
            "marketData": {"marketSlug": slug, "bids": lv(bids), "offers": lv(offers), "state": "MARKET_STATE_OPEN",
                           "stats": {"lastTradePx": {"value": "0.5"}, "lastTradeQty": "1.5", "sharesTraded": shares,
                                     "notionalTraded": {"value": "50"}, "openInterest": "40",
                                     "highPx": {"value": "0.6"}, "lowPx": {"value": "0.4"}},
                           "transactTime": ts}}


def trade_msg(tid="T1", slug="cpc-btc-updown-5m-X", price="0.50", qty="3.5"):
    return {"requestId": f"tr:{slug}", "subscriptionType": "SUBSCRIPTION_TYPE_TRADE",
            "trade": {"marketSlug": slug, "price": {"value": price, "currency": "USD"},
                      "quantity": {"value": qty, "currency": "USD"}, "tradeTime": "2026-11-09T17:30:01.25Z",
                      "maker": {"side": "ORDER_SIDE_BUY", "intent": "ORDER_INTENT_BUY_LONG",
                                "outcomeSide": "OUTCOME_SIDE_YES", "action": "ORDER_ACTION_BUY"},
                      "taker": {"side": "ORDER_SIDE_SELL", "intent": "ORDER_INTENT_SELL_LONG",
                                "outcomeSide": "OUTCOME_SIDE_YES", "action": "ORDER_ACTION_SELL"},
                      "id": tid, "state": "TRADE_STATE_NEW"}}


class WsStoreTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "ws.db")

    def store(self, **cfg):
        return WsStore(self.path, cfg)

    def rows(self, sql):
        c = sqlite3.connect(self.path)
        try:
            return c.execute(sql).fetchall()
        finally:
            c.close()

    def test_book_row_has_both_clocks_levels_and_stats(self):
        st = self.store(book_levels=1)
        st.write(1000.0, book_msg())
        st.flush()
        r = self.rows("SELECT recv_ts, exch_ts, slug, state, best_bid, best_ask, bids, n_bids, last_px, shares_traded, "
                      "open_interest FROM ws_book")[0]
        self.assertEqual(r[0], 1000.0)
        self.assertAlmostEqual(r[1], 1794207000.5, delta=100000)        # exchange time parsed to unix seconds
        self.assertEqual((r[2], r[3], r[4], r[5]), ("cpc-btc-updown-5m-X", "OPEN", 0.49, 0.51))
        self.assertEqual(json.loads(r[6]), [[0.49, 10.0]])               # only book_levels kept
        self.assertEqual(r[7], 2)                                       # but the full depth is counted
        self.assertEqual((r[8], r[9], r[10]), (0.5, 100.0, 40.0))

    def test_unchanged_and_fast_updates_are_not_stored_but_the_latest_is_not_lost(self):
        st = self.store(book_min_interval_ms=500)
        st.write(1000.0, book_msg())
        st.write(1000.1, book_msg())                                    # identical -> skipped
        st.write(1000.2, book_msg(shares="101"))                        # changed but inside the interval -> pending
        self.assertEqual(st.stored["book"], 1)
        st.write(1000.7, trade_msg())                                   # any later message flushes what is due
        st.flush()
        got = self.rows("SELECT shares_traded FROM ws_book ORDER BY recv_ts")
        self.assertEqual([g[0] for g in got], [100.0, 101.0])

    def test_trades_all_kept_and_deduplicated_by_id(self):
        st = self.store()
        st.write(1.0, trade_msg("A"))
        st.write(1.1, trade_msg("B", price="0.55"))
        st.write(1.2, trade_msg("A"))                                   # re-delivered after a reconnect
        st.flush()
        r = self.rows("SELECT trade_id, price, qty, maker_side, maker_intent, taker_side, taker_intent, "
                      "maker_outcome, state FROM ws_trades ORDER BY recv_ts")
        self.assertEqual([x[0] for x in r], ["A", "B"])
        self.assertEqual(r[0][1:], (0.5, 3.5, "BUY", "BUY_LONG", "SELL", "SELL_LONG", "YES", "NEW"))
        self.assertEqual(st.stored["dup_trade"], 1)

    def test_gap_rows(self):
        st = self.store()
        st.gap(5.0, "disconnect", "ConnectionClosed")
        self.assertEqual(self.rows("SELECT ts, kind, detail FROM ws_gaps"), [(5.0, "disconnect", "ConnectionClosed")])

    def test_retention_deletes_old_rows_only(self):
        st = self.store(book_keep_days=1, trades_keep_days=2)
        now = 10 * 86400.0
        st.write(now - 3 * 86400, book_msg(shares="1"))
        st.write(now - 3 * 86400, trade_msg("old"))
        st.write(now - 10, book_msg(shares="2"))
        st.write(now - 10, trade_msg("new"))
        st.flush()
        st.prune(now)                      # (writes also run the retention pass every 10 minutes of data time)
        self.assertEqual([r[0] for r in self.rows("SELECT trade_id FROM ws_trades")], ["new"])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM ws_book")[0][0], 1)



DAY = 86400.0
NOW = 100 * DAY


class SizeCapTests(unittest.TestCase):
    """The cap must act on ALL recorded tables (the old one trimmed only ws_book and could never reach it)."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "ws.db")

    def fill(self, st, days=10, trades_per_day=2500, books_per_day=500):
        """Synthetic recording: trades dominate (like the real 250 MB vs 150 MB per day), newest data at NOW."""
        pad = "x" * 40
        tr, bk = [], []
        for d in range(days):
            base = NOW - (d + 1) * DAY + 1.0
            for i in range(trades_per_day):
                ts = base + i * (DAY - 2) / trades_per_day
                tr.append((ts, ts, "slug", f"id{d}_{i}", 0.5, 1.0, "BUY", "BUY_LONG", "YES", "BUY", "SELL",
                           "SELL_LONG", "YES", "SELL", "NEW"))
            for i in range(books_per_day):
                ts = base + i * (DAY - 2) / books_per_day
                bk.append((ts, ts, "slug", "OPEN", 0.49, 0.51, "[[0.49,10.0]," + pad + "]", "[[0.51,5.0]]",
                           2, 2, 0.5, 1.0, 100.0, 50.0, 40.0, 0.6, 0.4))
        st.db.executemany("INSERT INTO ws_trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", tr)
        st.db.executemany("INSERT INTO ws_book VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", bk)
        for k, t in enumerate((NOW - 9 * DAY, NOW - 5 * DAY, NOW - 100.0)):
            st.db.execute("INSERT INTO ws_gaps VALUES (?,?,?)", (t, ("connect", "disconnect", "stop")[k], "m"))
        st.db.commit()

    def q(self, st, sql):
        return st.db.execute(sql).fetchall()

    def mk(self, **cfg):
        cfg.setdefault("book_keep_days", None)
        cfg.setdefault("trades_keep_days", None)
        cfg.setdefault("max_db_mb", 10_000)
        cfg.setdefault("vacuum_pause_ms", 0)
        cfg.setdefault("prune_budget_s", 600)
        return WsStore(self.path, cfg)

    def test_new_db_is_incremental_auto_vacuum(self):
        self.assertEqual(self.mk().db.execute("PRAGMA auto_vacuum").fetchone()[0], 2)

    def test_cap_converges_when_trades_alone_exceed_it_and_trims_both_tables(self):
        st = self.mk()
        self.fill(st)
        live0 = st.trimmer.live_bytes()
        trades0, books0 = self.q(st, "SELECT COUNT(*) FROM ws_trades")[0][0], self.q(st, "SELECT COUNT(*) FROM ws_book")[0][0]
        st.policy.max_bytes = live0 * 0.5                      # half the data must go: more than ws_book holds alone
        self.assertGreater(live0 * 0.5, 0)
        st.prune(NOW)
        self.assertLessEqual(st.trimmer.live_bytes(), st.policy.max_bytes)
        trades1, books1 = self.q(st, "SELECT COUNT(*) FROM ws_trades")[0][0], self.q(st, "SELECT COUNT(*) FROM ws_book")[0][0]
        self.assertLess(trades1, trades0)                      # the table the old cap ignored
        self.assertLess(books1, books0)
        # oldest-first: what is left is the newest data, and nothing inside the 24 h minimum was touched
        recent = NOW - 86400.0
        self.assertEqual(self.q(st, f"SELECT COUNT(*) FROM ws_trades WHERE recv_ts >= {recent}")[0][0], 2500)
        self.assertEqual(self.q(st, f"SELECT COUNT(*) FROM ws_book WHERE recv_ts >= {recent}")[0][0], 500)
        self.assertGreater(self.q(st, "SELECT MIN(recv_ts) FROM ws_trades")[0][0], NOW - 10 * DAY)

    def test_markers_survive_everything(self):
        st = self.mk(book_keep_days=1, trades_keep_days=1)
        self.fill(st)
        st.policy.max_bytes = 1.0                              # impossible cap: delete everything allowed
        st.prune(NOW)
        self.assertEqual([r[1] for r in self.q(st, "SELECT ts, kind FROM ws_gaps ORDER BY ts")],
                         ["connect", "disconnect", "stop"])
        self.assertEqual(self.q(st, "SELECT COUNT(*) FROM ws_meta WHERE key LIKE 'started_%'")[0][0], 1)

    def test_never_deletes_rows_newer_than_min_keep_and_warns_loudly_when_it_cannot_converge(self):
        st = self.mk(min_keep_hours=48)
        self.fill(st, days=1)                                   # everything is inside the 48 h minimum
        st.policy.max_bytes = 1.0
        n = self.q(st, "SELECT (SELECT COUNT(*) FROM ws_trades), (SELECT COUNT(*) FROM ws_book)")[0]
        with self.assertLogs("ws_trim", level="ERROR") as cm:
            st.prune(NOW)
        self.assertIn("CANNOT CONVERGE", cm.output[0])
        self.assertEqual(self.q(st, "SELECT (SELECT COUNT(*) FROM ws_trades), (SELECT COUNT(*) FROM ws_book)")[0], n)

    def test_warns_when_one_table_is_trimmed_to_its_floor_and_still_over(self):
        st = self.mk()
        self.fill(st, days=3)
        st.policy.max_bytes = 1.0
        st.prune(NOW)
        res = st.trimmer.run(NOW)
        self.assertEqual(self.q(st, f"SELECT COUNT(*) FROM ws_trades WHERE recv_ts < {NOW - DAY}")[0][0], 0)
        self.assertTrue(any("still over" in w or "CANNOT" in w for w in res["warnings"]))

    def test_file_and_wal_actually_shrink(self):
        st = self.mk()
        self.fill(st)
        st.db.commit()
        size0 = os.path.getsize(self.path) + (os.path.getsize(self.path + "-wal") if os.path.exists(self.path + "-wal") else 0)
        st.policy.max_bytes = st.trimmer.live_bytes() * 0.4
        st.policy.vacuum_min_free_bytes = 0
        st.prune(NOW)
        wal = os.path.getsize(self.path + "-wal") if os.path.exists(self.path + "-wal") else 0
        size1 = os.path.getsize(self.path) + wal
        self.assertLess(size1, size0 * 0.7)
        self.assertLess(wal, 100_000)                           # checkpoint(TRUNCATE) emptied the -wal

    def test_recorder_keeps_writing_during_and_after_a_budgeted_trim(self):
        st = self.mk(prune_budget_s=0.0, trim_batch_rows=500)
        self.fill(st)
        st.policy.max_bytes = st.trimmer.live_bytes() * 0.5
        passes = 0
        while True:
            st.prune(NOW)
            passes += 1
            st.write(NOW + passes, trade_msg(f"live{passes}"))  # the recorder writes between passes
            st.flush()
            if not st.trimmer.more or passes > 500:
                break
        self.assertGreater(passes, 2)                           # the budget really split it into several passes
        self.assertLessEqual(st.trimmer.live_bytes(), st.policy.max_bytes)
        self.assertEqual(self.q(st, "SELECT COUNT(*) FROM ws_trades WHERE trade_id LIKE 'live%'")[0][0], passes)
        st.write(NOW + 999, trade_msg("after"))
        st.flush()
        self.assertEqual(self.q(st, "SELECT COUNT(*) FROM ws_trades WHERE trade_id='after'")[0][0], 1)

    def test_per_table_retention_and_min_keep_floor(self):
        st = self.mk(book_keep_days=3, trades_keep_days=7, min_keep_hours=24)
        self.fill(st)
        st.prune(NOW)
        self.assertGreaterEqual(self.q(st, "SELECT MIN(recv_ts) FROM ws_book")[0][0], NOW - 3 * DAY)
        self.assertGreaterEqual(self.q(st, "SELECT MIN(recv_ts) FROM ws_trades")[0][0], NOW - 7 * DAY)
        self.assertLess(self.q(st, "SELECT MIN(recv_ts) FROM ws_trades")[0][0], NOW - 3 * DAY)   # trades kept longer
        st2 = self.mk(book_keep_days=0.1, trades_keep_days=0.1, min_keep_hours=24)               # keep < min_keep
        st2.prune(NOW)
        self.assertGreaterEqual(self.q(st2, "SELECT MIN(recv_ts) FROM ws_trades")[0][0], NOW - DAY)

    def test_dry_run_plan_is_read_only(self):
        st = self.mk()
        self.fill(st)
        st.close()
        from core.ws_trim import Policy, plan
        before = os.path.getsize(self.path)
        out = plan(self.path, Policy(max_bytes=1e5), NOW)
        self.assertEqual(os.path.getsize(self.path), before)
        self.assertEqual(out["markers"], 3)
        self.assertGreater(out["tables"]["ws_trades"]["cap_deletes"], 0)


class FakeWS:
    """Scripted websocket: items are raw messages, Exceptions to raise, or 'hang' (never answers)."""

    def __init__(self, script):
        self.script, self.sent, self.closed = list(script), [], False

    async def recv(self):
        if not self.script:
            await asyncio.sleep(3600)
        item = self.script.pop(0)
        if item == "hang":
            await asyncio.sleep(3600)
        if isinstance(item, Exception):
            raise item
        return item if isinstance(item, str) else json.dumps(item)

    async def send(self, data):
        self.sent.append(json.loads(data))

    async def close(self):
        self.closed = True


class FakeFeed:
    def __init__(self):
        self.calls = []

    def _event_market(self, prefix, start):
        self.calls.append((prefix, start))
        if start % 3 == 1:          # never "listed"
            return None
        return {"slug": f"cpc-{prefix}-{start}"}


class ListSink:
    def __init__(self):
        self.msgs, self.gaps = [], []

    def write(self, ts, msg):
        self.msgs.append(msg)

    def gap(self, ts, kind, detail=""):
        self.gaps.append((kind, detail))

    def flush(self):
        pass

    def close(self):
        pass


MARKETS = [{"asset": "BTC-5m", "slug_prefix": "btc-updown-5m", "window_seconds": 300},
           {"asset": "BTC-15m", "slug_prefix": "btc-updown-15m", "window_seconds": 900}]


class ConnClosed(Exception):
    pass


def make_recorder(connections, clock=time.time, **cfg):
    sink, feed = ListSink(), FakeFeed()
    feed._event_market = lambda prefix, start: {"slug": f"cpc-{prefix}-{start}"}
    it = iter(connections)

    async def connect():
        c = next(it, None) or FakeWS(["hang"])
        if isinstance(c, Exception):
            raise c
        return c
    rec = Recorder(sink, MARKETS, {"stale_seconds": 30, **cfg}, ("kid", "SECRETKEY123"), clock=clock,
                   connect=connect, planner_every=0.05, feed=feed)
    return rec, sink


async def run_for(rec, seconds):
    task = asyncio.create_task(rec.run())
    await asyncio.sleep(seconds)
    rec.request_stop()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


class RecorderTests(unittest.TestCase):
    def test_one_subscription_per_type_covers_current_and_next_round(self):
        ws = FakeWS([book_msg(), trade_msg(), {"heartbeat": {}}, "hang"])
        rec, sink = make_recorder([ws])
        asyncio.run(run_for(rec, 0.5))
        subs = [m["subscribe"] for m in ws.sent if "subscribe" in m]
        self.assertEqual(len(subs), 2)                                        # NOT one per market (connection cap)
        self.assertEqual({s["subscriptionType"] for s in subs},
                         {"SUBSCRIPTION_TYPE_MARKET_DATA", "SUBSCRIPTION_TYPE_TRADE"})
        slugs = subs[0]["marketSlugs"]
        self.assertEqual(len(slugs), 4)                                       # 2 markets x (current + next)
        self.assertEqual(slugs, subs[1]["marketSlugs"])
        now = int(time.time())
        self.assertIn(f"cpc-btc-updown-15m-{(now // 900 + 1) * 900}", slugs)
        self.assertIn(f"cpc-btc-updown-5m-{(now // 300 + 1) * 300}", slugs)
        self.assertEqual(rec.counts["book"], 1)
        self.assertEqual(rec.counts["trade"], 1)
        self.assertEqual(rec.counts["heartbeat"], 1)
        self.assertEqual(len(sink.msgs), 3)

    def test_subscription_cap_error_forces_a_clean_reconnect(self):
        err = {"requestId": "md:3", "error": "slug already subscribed: x"}
        a, b = FakeWS([err, "hang"]), FakeWS(["hang"])
        rec, sink = make_recorder([a, b])
        rec.resync_wait = 0.1
        asyncio.run(run_for(rec, 2.0))
        self.assertGreaterEqual(rec.counts["connects"], 2)
        self.assertEqual(sum(1 for m in b.sent if "subscribe" in m), 2)       # clean slate: 2 subscriptions again

    def test_disconnect_marks_gap_reconnects_and_resubscribes(self):
        first = FakeWS([book_msg(), ConnClosed("boom")])
        second = FakeWS([trade_msg(), "hang"])
        rec, sink = make_recorder([first, second])
        asyncio.run(run_for(rec, 2.0))
        kinds = [k for k, _ in sink.gaps]
        self.assertEqual(kinds[:3], ["connect", "disconnect", "connect"])
        self.assertEqual(sum(1 for m in second.sent if "subscribe" in m), 2)    # re-subscribed on the new connection
        self.assertEqual(rec.counts["connects"], 2)
        self.assertTrue(first.closed)

    def test_watchdog_reconnects_when_nothing_arrives(self):
        a, b = FakeWS(["hang"]), FakeWS(["hang"])
        rec, sink = make_recorder([a, b], stale_seconds=0.2)
        asyncio.run(run_for(rec, 2.5))
        self.assertGreaterEqual(rec.counts["connects"], 2)
        self.assertTrue(any("TimeoutError" in d for k, d in sink.gaps if k == "disconnect"))

    def test_429_backs_off_and_retries(self):
        class Resp:
            status_code = 429

        class Rejected(Exception):
            response = Resp()
        rec, sink = make_recorder([Rejected(), FakeWS(["hang"])])
        rec.rate_limit_wait = 0.2
        asyncio.run(run_for(rec, 2.0))
        self.assertIn(("disconnect", "Rejected 429"), sink.gaps)
        self.assertEqual(rec.counts["connects"], 1)

    def test_finished_rounds_are_unsubscribed(self):
        ws = FakeWS(["hang"])
        t = [1_790_000_000.0]
        rec, sink = make_recorder([ws], clock=lambda: t[0])

        async def go():
            task = asyncio.create_task(rec.run())
            await asyncio.sleep(0.4)
            t[0] += 2000                      # all earlier rounds are long over
            await asyncio.sleep(0.4)
            rec.request_stop()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        asyncio.run(go())
        order = [("unsub" if "unsubscribe" in m else "sub", (m.get("unsubscribe") or m["subscribe"])["requestId"])
                 for m in ws.sent]
        # the exchange rejects a slug that is already subscribed, so the old subscriptions must go FIRST
        self.assertEqual(order, [("sub", "md:1"), ("sub", "tr:1"), ("unsub", "md:1"), ("unsub", "tr:1"),
                                 ("sub", "md:2"), ("sub", "tr:2")])
        subs = [m["subscribe"] for m in ws.sent if "subscribe" in m]
        self.assertNotEqual(subs[0]["marketSlugs"], subs[2]["marketSlugs"])
        self.assertIn("resubscribe", [k for k, _ in sink.gaps])

    def test_bad_json_is_a_gap_not_a_crash(self):
        rec, sink = make_recorder([FakeWS(["{not json", book_msg(), "hang"])])
        asyncio.run(run_for(rec, 0.5))
        self.assertIn("bad_json", [k for k, _ in sink.gaps])
        self.assertEqual(rec.counts["book"], 1)

    def test_target_slugs_reads_slugs_and_retries_misses(self):
        feed = FakeFeed()
        cache = {}
        now = 3 * 900 * 1000 + 10             # window start % 3 patterns: some "not listed"
        out = target_slugs(feed, MARKETS, now, 1, cache)
        self.assertTrue(all(v["slug"].startswith("cpc-btc-updown") for v in out.values()))
        missing = [(p, s) for p, s in feed.calls if s % 3 == 1]
        n_calls = len(feed.calls)
        target_slugs(feed, MARKETS, now, 1, cache)
        self.assertEqual(len(feed.calls) - n_calls, len(missing))                  # only misses are asked again

    def test_secret_never_logged_and_handshake_signature_verifies(self):
        from nacl.signing import SigningKey
        key = SigningKey.generate()
        secret_b64 = base64.b64encode(bytes(key) + bytes(key.verify_key)).decode()      # 64 bytes: seed + public
        rec = Recorder(ListSink(), MARKETS, {}, ("my-key-id", secret_b64))
        h = rec._headers()
        self.assertEqual(h["X-PM-Access-Key"], "my-key-id")
        msg = f'{h["X-PM-Timestamp"]}GET/v1/ws/markets'.encode()
        key.verify_key.verify(msg, base64.b64decode(h["X-PM-Signature"]))             # raises if wrong
        # a failing connection must not put credentials into the log
        class Rejected(Exception):
            class response:
                status_code = 401
        rec2, _ = make_recorder([Rejected()])
        rec2.auth_fail_wait = 0.05
        with self.assertLogs("recorder", level="ERROR") as cm:
            asyncio.run(run_for(rec2, 0.3))
        self.assertNotIn("SECRETKEY123", "\n".join(cm.output))
        self.assertNotIn(secret_b64, "\n".join(cm.output))


if __name__ == "__main__":
    unittest.main()
