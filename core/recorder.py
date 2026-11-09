"""
Websocket recorder for Polymarket US market data (runs as its OWN process, never inside the bot).

  wss://api.polymarket.us/v1/ws/markets   auth: X-PM-Access-Key / X-PM-Timestamp / X-PM-Signature on the
  handshake (Ed25519 over  timestamp + "GET" + "/v1/ws/markets";  signing is the official SDK's
  polymarket_us.auth.create_auth_headers, so it can't drift from what the exchange expects).

For every configured market (5m, 15m) it subscribes SUBSCRIPTION_TYPE_MARKET_DATA (book + stats) and
SUBSCRIPTION_TYPE_TRADE for the CURRENT round and the next `next_rounds` ones, adds the new round as the
clock rolls over, and drops finished rounds. One subscription per market slug (the limit is 100 per request).

Robustness: reconnect with exponential backoff (429/auth errors back off harder), re-subscribe on every
connect, a `ws_gaps` row on every connect/disconnect (so analysis knows where data may be missing), and a
watchdog that reconnects when nothing (not even a heartbeat) arrives for `stale_seconds`.

Credentials: PM_KEY_ID / PM_SECRET_KEY from the environment or the gitignored .env (core/credentials.py).
They are never logged or printed.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from typing import Callable, Optional

from core.polymarket_us import PolymarketUSFeed

log = logging.getLogger("recorder")

WS_URL = "wss://api.polymarket.us"
WS_PATH = "/v1/ws/markets"
MARKET_DATA, TRADE = "SUBSCRIPTION_TYPE_MARKET_DATA", "SUBSCRIPTION_TYPE_TRADE"
UNSUBSCRIBE_AFTER = 30           # seconds after a round ends before its subscriptions are dropped


# ---------------------------------------------------------------------------------------- sinks
class JsonlSink:
    """Probe mode: every raw message, one JSON line each, with the local receive time."""

    def __init__(self, path: str):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.fh = open(path, "a", encoding="utf-8")
        self.n = 0

    def write(self, recv_ts: float, msg: dict):
        self.fh.write(json.dumps({"recv": recv_ts, "msg": msg}, separators=(",", ":")) + "\n")
        self.n += 1
        if self.n % 50 == 0:
            self.fh.flush()

    def gap(self, ts: float, kind: str, detail: str = ""):
        self.fh.write(json.dumps({"recv": ts, "gap": kind, "detail": detail}) + "\n")
        self.fh.flush()

    def flush(self):
        self.fh.flush()

    def close(self):
        self.fh.close()


# ------------------------------------------------------------------------------------- planner
def target_slugs(feed: PolymarketUSFeed, markets: list[dict], now: float, next_rounds: int,
                 cache: dict[tuple, Optional[dict]]) -> dict[str, dict]:
    """{market slug: {asset, start, end}} for the current and next rounds. Blocking (REST); run in a thread.

    The market slug is READ from the event (never guessed). A miss (round not listed yet) is retried
    on the next call; hits are cached."""
    out: dict[str, dict] = {}
    for m in markets:
        W = int(m["window_seconds"])
        for k in range(0, next_rounds + 1):
            start = (int(now) // W + k) * W
            key = (m["slug_prefix"], start)
            info = cache.get(key)
            if info is None:
                mk = feed._event_market(m["slug_prefix"], start)
                if not mk:
                    continue
                info = cache[key] = {"slug": mk["slug"], "asset": m["asset"], "start": start, "end": start + W}
            out[info["slug"]] = info
    for key in [k for k in cache if k[1] < now - 3600]:
        del cache[key]
    return out


# ------------------------------------------------------------------------------------ recorder
class ResyncError(Exception):
    """The exchange refused a subscription; reconnect (after a short pause) and subscribe again."""


class Recorder:
    def __init__(self, sink, markets: list[dict], cfg: dict, creds: tuple[str, str],
                 clock: Callable[[], float] = time.time, connect=None, planner_every: float = 5.0,
                 feed=None):
        self.sink, self.markets, self.cfg = sink, markets, cfg or {}
        self.key_id, self.secret = creds
        self.clock = clock
        self._connect = connect                       # injectable for tests
        self.next_rounds = int(self.cfg.get("next_rounds", 1))
        self.stale_seconds = float(self.cfg.get("stale_seconds", 45))
        self.debounce = bool(self.cfg.get("debounce", False))
        self.planner_every = planner_every
        self.feed = feed or PolymarketUSFeed()
        self._cache: dict[tuple, Optional[dict]] = {}
        self.subscribed: dict[str, dict] = {}         # market slug -> info (this connection)
        self._sub_ids: list[str] = []                 # request ids of the live subscriptions
        self._gen = 0
        self.counts = {"msgs": 0, "book": 0, "trade": 0, "heartbeat": 0, "error": 0, "connects": 0}
        self._stopping = False
        self.resync_wait = 5.0
        self.rate_limit_wait = 30.0                   # seconds to back off after HTTP 429
        self.auth_fail_wait = 60.0                    # ... after a rejected handshake (401/403)

    # -- connection -------------------------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        from polymarket_us.auth import create_auth_headers          # official SDK signing
        return create_auth_headers(self.key_id, self.secret, "GET", WS_PATH)

    async def _open(self):
        if self._connect is not None:
            return await self._connect()
        from websockets.asyncio.client import connect
        return await connect(WS_URL + WS_PATH, additional_headers=self._headers(), open_timeout=15,
                             ping_interval=20, ping_timeout=20, max_size=2 ** 24)

    def request_stop(self):
        self._stopping = True

    async def run(self):
        backoff = 1.0
        while not self._stopping:
            ws = None
            try:
                ws = await self._open()
                self.counts["connects"] += 1
                self.sink.gap(self.clock(), "connect")
                log.info("Connected to %s%s", WS_URL, WS_PATH)
                backoff = 1.0
                await self._session(ws)
                if self._stopping:
                    break
                raise ConnectionError("session ended")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if self._stopping:
                    break
                status = getattr(getattr(e, "response", None), "status_code", None)
                self.sink.gap(self.clock(), "disconnect", f"{type(e).__name__} {status or ''}".strip())
                wait = backoff
                if isinstance(e, ResyncError):
                    wait = max(backoff, self.resync_wait)
                    log.warning("Subscription refused (%s); reconnecting in %.0fs", e, wait)
                elif status == 429:
                    wait = max(backoff, self.rate_limit_wait)
                    log.warning("Rate limited (429); waiting %.0fs", wait)
                elif status in (401, 403):
                    wait = max(backoff, self.auth_fail_wait)
                    log.error("Handshake rejected (HTTP %s): check PM_KEY_ID/PM_SECRET_KEY and that the system "
                              "clock is within 30s of real time. Retrying in %.0fs", status, wait)
                else:
                    log.warning("Disconnected (%s: %s); reconnecting in %.0fs", type(e).__name__, e, wait)
                await asyncio.sleep(wait)
                backoff = min(backoff * 2, 60.0)
            finally:
                self.subscribed, self._sub_ids = {}, []
                if ws is not None:
                    try:
                        await ws.close()
                    except Exception:
                        pass

    async def _session(self, ws):
        planner = asyncio.create_task(self._planner(ws))
        try:
            while not self._stopping:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=self.stale_seconds)
                except asyncio.TimeoutError:
                    raise TimeoutError(f"no data for {self.stale_seconds:.0f}s (watchdog)")
                self.handle(raw)
                if planner.done():
                    planner.result()           # surface a planner crash as a disconnect
        finally:
            planner.cancel()
            try:
                await planner
            except (asyncio.CancelledError, Exception):
                pass

    async def _planner(self, ws):
        """
        Keep ONE market-data subscription and ONE trade subscription, each covering every wanted slug.
        (The probe showed the exchange caps subscriptions PER CONNECTION -- "max subscriptions per connection
        reached" -- so a subscription per market runs out within an hour; one per type with a slug list doesn't.)
        When the wanted set changes (a round starts/ends) the old subscriptions are closed and new ones opened for
        the full new set. It has to be in that order: the exchange refuses a slug that is already subscribed
        ("slug already subscribed"), so opening the new one first would have it rejected. The hole is the round trip
        of two messages (milliseconds) and a `resubscribe` row is written to ws_gaps so it is on record.
        """
        while True:
            now = self.clock()
            try:
                found = await asyncio.to_thread(target_slugs, self.feed, self.markets, now,
                                                self.next_rounds, self._cache)
            except Exception as e:
                log.warning("Could not resolve market slugs (%s)", e)
                found = dict(self.subscribed)
            want = {s: i for s, i in found.items() if now <= i["end"] + UNSUBSCRIBE_AFTER}
            if want and set(want) != set(self.subscribed):
                for rid in self._sub_ids:
                    await ws.send(json.dumps({"unsubscribe": {"requestId": rid}}))
                if self._sub_ids:
                    self.sink.gap(self.clock(), "resubscribe", f"{len(want)} markets")
                self._gen += 1
                await self._subscribe(ws, sorted(want))
                self.subscribed = want
            await asyncio.sleep(self.planner_every)

    async def _subscribe(self, ws, slugs: list[str]):
        self._sub_ids = []
        for pre, typ in (("md", MARKET_DATA), ("tr", TRADE)):
            rid = f"{pre}:{self._gen}"
            req = {"subscribe": {"requestId": rid, "subscriptionType": typ, "marketSlugs": slugs}}
            if self.debounce and typ == MARKET_DATA:
                req["subscribe"]["responsesDebounced"] = True
            await ws.send(json.dumps(req))
            self._sub_ids.append(rid)
        log.info("Subscribed %d markets: %s", len(slugs), ", ".join(s.replace("cpc-btc-updown-", "") for s in slugs))

    # -- messages -------------------------------------------------------------------------------
    def handle(self, raw):
        recv = self.clock()
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        try:
            msg = json.loads(raw)
        except ValueError:
            self.sink.gap(recv, "bad_json", raw[:200])
            return
        self.counts["msgs"] += 1
        if "heartbeat" in msg:
            self.counts["heartbeat"] += 1
        elif "error" in msg:
            self.counts["error"] += 1
            log.warning("Server error message: %s", json.dumps(msg)[:300])
            rid = str(msg.get("requestId", ""))
            if rid.startswith(("md:", "tr:")):
                # a subscription was refused (cap hit, slug already subscribed, ...): the stream is incomplete, so
                # start over on a fresh connection rather than silently recording a partial feed
                self.sink.write(recv, msg)
                raise ResyncError(str(msg.get("error")))
        elif "marketData" in msg:
            self.counts["book"] += 1
        elif "trade" in msg:
            self.counts["trade"] += 1
        self.sink.write(recv, msg)


# ----------------------------------------------------------------------------------- disk guard
def disk_report(path: str) -> str:
    try:
        size = os.path.getsize(path) / 1e6
        wal = os.path.getsize(path + "-wal") / 1e6 if os.path.exists(path + "-wal") else 0
        free = shutil.disk_usage(os.path.dirname(os.path.abspath(path))).free / 1e9
        return f"{os.path.basename(path)} {size:.0f} MB (+{wal:.0f} MB wal), disk free {free:.1f} GB"
    except OSError as e:
        return f"disk usage unavailable ({e})"
