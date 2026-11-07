"""
Talks to Polymarket's public (no login needed) APIs.

  Gamma API  -> finds the market for the current 15-minute window,
                gives us its two token IDs (UP and DOWN), and later
                tells us who won.
  CLOB API   -> the live order book (bids/asks) for each token.

Paper trading never sends an order anywhere. It only READS data.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import requests

from core.models import Book, MarketInfo, UP, DOWN

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"

log = logging.getLogger("feed")


def _maybe_json(x):
    """Gamma returns some lists as JSON-inside-a-string, e.g. '["Up","Down"]'."""
    if isinstance(x, str):
        try:
            return json.loads(x)
        except json.JSONDecodeError:
            return x
    return x


def window_start_for(now: float, window_seconds: int) -> int:
    """Round the clock down to the start of the current window (e.g. 2:15:00)."""
    return int(now // window_seconds) * window_seconds


def slug_start(slug: str) -> Optional[int]:
    """The unix start time at the end of a slug ('btc-updown-5m-1794082200' -> 1794082200)."""
    try:
        return int(slug.rsplit("-", 1)[1])
    except (IndexError, ValueError):
        return None


def market_fees(raw: dict, fallback_rate: float) -> tuple[float, float]:
    """
    (fee_rate, exponent) from Gamma's market data, e.g.
      "feesEnabled": true, "feeSchedule": {"rate": 0.07, "exponent": 1, "takerOnly": true, ...}
    fee = shares * rate * (p * (1-p)) ** exponent. Falls back to config.py's rate when missing.
    """
    if raw.get("feesEnabled") is False:
        return 0.0, 1.0
    sched = _maybe_json(raw.get("feeSchedule"))
    if isinstance(sched, dict) and sched.get("rate") is not None:
        try:
            rate, exp = float(sched["rate"]), float(sched.get("exponent", 1) or 1)
        except (TypeError, ValueError):
            return fallback_rate, 1.0
        if exp != 1:
            log.warning("Fee exponent is %s (only exponent 1 is confirmed against Polymarket's docs)", exp)
        return rate, exp
    return fallback_rate, 1.0


class PolymarketFeed:
    # Every call is a quick GET, so keep timeouts short: the whole bot is one loop, and a slow
    # request delays every strategy (a 60s exit on a 5-minute market can't wait 30s for an API).
    def __init__(self, window_seconds: int = 900, timeout: float = 4.0, attempts: int = 2):
        self.window_seconds = window_seconds
        self.timeout = timeout
        self.attempts = attempts
        self._local = threading.local()            # one HTTP session per thread (Session isn't thread-safe)
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="feed")
        self._market_cache: dict[str, Optional[MarketInfo]] = {}
        self._miss_until: dict[str, float] = {}   # slug -> don't retry before this time
        self._fee_warned: set[str] = set()

    # ---------- low level ----------
    def _session(self) -> requests.Session:
        s = getattr(self._local, "session", None)
        if s is None:
            s = self._local.session = requests.Session()
            s.headers["User-Agent"] = "polymarket-paper-trader/1.0"
        return s

    def _get(self, url: str, params: dict) -> Optional[object]:
        for attempt in range(self.attempts):
            try:
                r = self._session().get(url, params=params, timeout=self.timeout)
                if r.status_code == 429:          # rate limited -> back off briefly
                    time.sleep(0.75 * (attempt + 1))
                    continue
                if r.status_code == 404:
                    return None
                r.raise_for_status()
                return r.json()
            except (requests.RequestException, ValueError) as e:   # ValueError: bad JSON
                log.warning("GET %s failed (%s), attempt %d", url, e, attempt + 1)
                if attempt + 1 < self.attempts:
                    time.sleep(0.25 * (attempt + 1))
        return None

    def _gamma_market_by_slug(self, slug: str) -> Optional[dict]:
        """Return the raw Gamma 'market' dict for a slug (event slug or market slug)."""
        events = self._get(f"{GAMMA}/events", {"slug": slug})
        if isinstance(events, list) and events and events[0].get("markets"):
            return events[0]["markets"][0]
        markets = self._get(f"{GAMMA}/markets", {"slug": slug})
        if isinstance(markets, list) and markets:
            return markets[0]
        return None

    # ---------- public ----------
    def current_market(self, cfg: dict, now: float) -> Optional[MarketInfo]:
        """Find the market for the window we're in right now, for one asset config."""
        window = int(cfg.get("window_seconds", self.window_seconds))
        start = window_start_for(now, window)
        slug = f"{cfg['slug_prefix']}-{start}"
        if slug in self._market_cache and self._market_cache[slug] is not None:
            return self._market_cache[slug]
        if now < self._miss_until.get(slug, 0):
            return None

        raw = self._gamma_market_by_slug(slug)
        if raw is None:
            # Market not listed (wrong prefix, or API lag at the window boundary). Retry in 20s.
            self._miss_until[slug] = now + 20
            log.info("No market found for %s (slug %s). Will retry.", cfg["asset"], slug)
            return None

        outcomes = [str(o).lower() for o in _maybe_json(raw.get("outcomes", "[]"))]
        tokens = _maybe_json(raw.get("clobTokenIds", "[]"))
        if len(tokens) != 2 or len(outcomes) != 2:
            log.warning("Unexpected market shape for %s: %s / %s", slug, outcomes, tokens)
            self._miss_until[slug] = now + 60
            return None
        up_idx = 0 if outcomes[0] in ("up", "yes") else 1
        fee_rate, fee_exp = market_fees(raw, cfg.get("fee_rate", 0.0))
        try:
            min_shares = float(raw.get("orderMinSize") or 0.0)
        except (TypeError, ValueError):
            min_shares = 0.0
        info = MarketInfo(
            asset=cfg["asset"],
            slug=slug,
            window_start=start,
            window_end=start + window,
            token_up=str(tokens[up_idx]),
            token_down=str(tokens[1 - up_idx]),
            question=raw.get("question", ""),
            fee_rate=fee_rate,
            fee_exponent=fee_exp,
            min_order_shares=min_shares,
        )
        if abs(fee_rate - cfg.get("fee_rate", fee_rate)) > 1e-9 and cfg["asset"] not in self._fee_warned:
            self._fee_warned.add(cfg["asset"])
            log.warning("%s: Polymarket's fee rate is %.4f, config.py says %.4f. Using Polymarket's.",
                        cfg["asset"], fee_rate, cfg.get("fee_rate"))
        self._market_cache[slug] = info
        log.info("Tracking %s: %s", cfg["asset"], info.question or slug)
        return info

    def book(self, token_id: str) -> Optional[Book]:
        data = self._get(f"{CLOB}/book", {"token_id": token_id})
        if not isinstance(data, dict):
            return None
        # Sort explicitly -- the API's ordering is not "best first".
        bids = sorted(((float(l["price"]), float(l["size"])) for l in data.get("bids", [])), key=lambda x: -x[0])
        asks = sorted(((float(l["price"]), float(l["size"])) for l in data.get("asks", [])), key=lambda x: x[0])
        try:
            ts = float(data["timestamp"]) / 1000      # milliseconds
        except (KeyError, TypeError, ValueError):
            ts = None
        return Book(bids=bids, asks=asks, ts=ts)

    def books(self, info: MarketInfo) -> tuple[Optional[Book], Optional[Book]]:
        """Both books, fetched at the same time (half the wait, and both sides are from the same moment)."""
        up = self._pool.submit(self.book, info.token_up)
        down = self._pool.submit(self.book, info.token_down)
        return up.result(), down.result()

    def resolution_detail(self, slug: str, window_end: Optional[float] = None) -> tuple[Optional[str], bool]:
        """
        (winner, official): winner is 'UP' / 'DOWN' once the result is known, else None.

        Polymarket only flips `closed` after the oracle finalises, which for 5-minute
        markets can take several minutes -- but the prices lock to ~1.00 / ~0.00
        within seconds of the window ending. If `window_end` is given and has passed,
        we accept those locked prices as a PROVISIONAL result (official=False) instead of
        waiting for `closed`. The engine re-checks provisional results later and corrects
        them if the official result turns out different.
        """
        raw = self._gamma_market_by_slug(slug)
        if not raw:
            return None, False
        closed = bool(raw.get("closed"))
        ended = window_end is not None and time.time() >= window_end + 3
        if not closed and not ended:
            return None, False
        try:
            prices = [float(p) for p in _maybe_json(raw.get("outcomePrices", "[]"))]
            outcomes = [str(o).lower() for o in _maybe_json(raw.get("outcomes", "[]"))]
        except (TypeError, ValueError):
            return None, False
        if len(prices) != 2 or len(outcomes) != 2 or max(prices) < 0.99:
            return None, False
        winner_name = outcomes[prices.index(max(prices))]
        return (UP if winner_name in ("up", "yes") else DOWN), closed

    def resolution(self, slug: str, window_end: Optional[float] = None) -> Optional[str]:
        """'UP' / 'DOWN' once the market's result is known (provisional or official), else None."""
        return self.resolution_detail(slug, window_end)[0]

    def prune(self, before_ts: float):
        """Forget cached markets for windows that started before `before_ts` (keeps memory flat)."""
        for d in (self._market_cache, self._miss_until):
            for slug in [k for k in d if (slug_start(k) or 0) < before_ts]:
                del d[slug]
