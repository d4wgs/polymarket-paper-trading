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

    def test_size_cap_trims_books_not_trades(self):
        st = self.store(book_keep_days=None, trades_keep_days=None, max_db_mb=0.001, book_min_interval_ms=0)
        for i in range(400):
            st.write(1000.0 + i, book_msg(shares=str(i), bids=[(f"0.{40 + i % 9}", str(i))]))
        st.write(1.0, trade_msg("keep"))
        st.flush()
        before = self.rows("SELECT COUNT(*) FROM ws_book")[0][0]
        st.prune(5000.0)
        self.assertLess(self.rows("SELECT COUNT(*) FROM ws_book")[0][0], before)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM ws_trades")[0][0], 1)


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
