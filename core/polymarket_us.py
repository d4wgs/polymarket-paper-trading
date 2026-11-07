"""
Talks to Polymarket US's public market-data API (no login needed): https://gateway.polymarket.us
Docs: https://docs.polymarket.us/api-reference/introduction

  GET /v1/events/slug/{event slug}       -> the round's market slug, window and "price to beat"
  GET /v1/markets/{market slug}/book     -> the order book of the market's single YES instrument
  GET /v1/markets/{market slug}/settlement -> 1 (Yes = Up) or 0 (No = Down) once settled (final)
  GET /v1/asset-prices/history           -> the settlement index (CF Benchmarks BRTI) per second

How a BTC Up/Down round maps to this bot:
  * event slug   btc-updown-15m-2026-11-07-2100z   (UTC start time of the window)
  * market slug  cpc-btc-updown-15m-2026-11-07-2100z (read from the event, never guessed)
  * inside the bot a round is still "<prefix>-<unix start>" (e.g. btc-updown-15m-1794085200)
  * ONE instrument, YES = Up. The UP book is the YES book; the DOWN book is its mirror
    (DOWN ask = 1 - YES bid, DOWN bid = 1 - YES ask, same sizes), so the two can never disagree.
  * Up wins when the close value >= the open value (ties settle Up). Settlement is final.

Paper trading never sends an order anywhere. It only READS data.
"""
from __future__ import annotations

import datetime as dt
import logging
import threading
import time
from typing import Optional

import requests

from core.models import Book, MarketInfo, UP, DOWN

GATEWAY = "https://gateway.polymarket.us"
log = logging.getLogger("feed_us")


def slug_start(slug: str) -> Optional[int]:
    try:
        return int(slug.rsplit("-", 1)[1])
    except (IndexError, ValueError):
        return None


def event_slug(prefix: str, start: int) -> str:
    """'btc-updown-15m', 1794085200 -> 'btc-updown-15m-2026-11-07-2100z' (UTC start of the window)."""
    return f"{prefix}-{time.strftime('%Y-%m-%d-%H%M', time.gmtime(start))}z"


def _amount(x) -> Optional[float]:
    """API amounts look like {"value": "0.4800", "currency": "USD"} (or null)."""
    if isinstance(x, dict):
        x = x.get("value")
    try:
        return float(x) if x is not None else None
    except (TypeError, ValueError):
        return None


def _iso_ts(s) -> Optional[float]:
    """'2026-11-07T21:00:15.309232424Z' -> unix seconds (nanoseconds trimmed)."""
    if not s:
        return None
    try:
        s = s.rstrip("Z")
        if "." in s:
            head, frac = s.split(".", 1)
            s = f"{head}.{frac[:6]}"
        return dt.datetime.fromisoformat(s).replace(tzinfo=dt.timezone.utc).timestamp()
    except ValueError:
        return None


def parse_book(data: dict) -> tuple[Optional[Book], Optional[Book], Optional[str]]:
    """(up, down, state) from a /book response. UP = the YES book; DOWN = its mirror."""
    md = (data or {}).get("marketData") or {}
    bids = sorted(((_amount(l.get("px")), float(l.get("qty", 0))) for l in md.get("bids", []) or []),
                  key=lambda x: -x[0])
    asks = sorted(((_amount(l.get("px")), float(l.get("qty", 0))) for l in md.get("offers", []) or []),
                  key=lambda x: x[0])
    bids = [(p, q) for p, q in bids if p is not None and q > 0]
    asks = [(p, q) for p, q in asks if p is not None and q > 0]
    ts = _iso_ts(md.get("transactTime"))
    up = Book(bids=bids, asks=asks, ts=ts)
    down = Book(bids=[(round(1 - p, 6), q) for p, q in asks],     # someone selling YES at p = buying NO at 1-p
                asks=[(round(1 - p, 6), q) for p, q in bids], ts=ts)
    return up, down, md.get("state")


class PolymarketUSFeed:
    def __init__(self, window_seconds: int = 900, timeout: float = 4.0, attempts: int = 2):
        self.window_seconds = window_seconds
        self.timeout = timeout
        self.attempts = attempts
        self._local = threading.local()
        self._market_cache: dict[str, MarketInfo] = {}       # internal slug -> MarketInfo
        self._exchange_slug: dict[str, str] = {}             # internal slug -> market slug
        self._miss_until: dict[str, float] = {}
        self._ptb_try: dict[str, float] = {}
        self._index_cache: dict[str, tuple[float, Optional[float]]] = {}
        self._prefix_cfg: dict[str, dict] = {}
        self._book_miss_log: dict[str, float] = {}

    # ---------- low level ----------
    def _session(self) -> requests.Session:
        s = getattr(self._local, "session", None)
        if s is None:
            s = self._local.session = requests.Session()
            s.headers["User-Agent"] = "polymarket-paper-trader/1.0"
            s.headers["Cache-Control"] = "no-cache"
        return s

    def _get(self, path: str, params: Optional[dict] = None):
        """
        JSON, or None on 'not found' / failure. The API answers not-found as 404 or {"code": 5}.

        The gateway sits behind Cloudflare, which caches responses ("cache-control: public,
        max-age=30"), including "not found". Without a unique query string the bot would trade on
        order books up to 30s old, miss the first minutes of every round (the "no book yet" answer
        from the round's first second stays cached), and learn results minutes late. The `_` nonce
        makes every request go to the exchange itself (checked Nov 7 2026: cf-cache-status MISS).
        """
        params = {**(params or {}), "_": time.time_ns()}
        for attempt in range(self.attempts):
            try:
                r = self._session().get(f"{GATEWAY}{path}", params=params, timeout=self.timeout)
                if r.status_code == 429:           # docs: wait at least 1s, then back off
                    time.sleep(1.0 * (attempt + 1))
                    continue
                if r.status_code == 404:
                    return None
                data = r.json()
                if isinstance(data, dict) and data.get("code") == 5:     # gRPC NOT_FOUND
                    return None
                r.raise_for_status()
                return data
            except (requests.RequestException, ValueError) as e:
                log.warning("GET %s failed (%s), attempt %d", path, e, attempt + 1)
                if attempt + 1 < self.attempts:
                    time.sleep(0.25 * (attempt + 1))
        return None

    def _event_market(self, prefix: str, start: int) -> Optional[dict]:
        data = self._get(f"/v1/events/slug/{event_slug(prefix, start)}")
        ev = (data or {}).get("event") or {}
        markets = ev.get("markets") or []
        return markets[0] if markets else None

    # ---------- public ----------
    def current_market(self, cfg: dict, now: float) -> Optional[MarketInfo]:
        window = int(cfg.get("window_seconds", self.window_seconds))
        start = int(now // window) * window
        prefix = cfg["slug_prefix"]
        self._prefix_cfg[prefix] = cfg
        slug = f"{prefix}-{start}"
        info = self._market_cache.get(slug)
        if info is not None:
            self._refresh_price_to_beat(info, now)
            return info
        if now < self._miss_until.get(slug, 0):
            return None
        m = self._event_market(prefix, start)
        if m is None:
            # Not listed: wrong prefix, API lag, or a window skipped for scheduled maintenance.
            self._miss_until[slug] = now + 20
            log.info("No Polymarket US market for %s (%s). Will retry.", cfg["asset"], event_slug(prefix, start))
            return None
        terms = m.get("assetPriceTerms") or {}
        ws, we = _iso_ts(terms.get("windowStart")), _iso_ts(terms.get("windowEnd"))
        if ws is not None and abs(ws - start) > 1:
            log.warning("%s: window starts at %s, expected %s; skipping", m.get("slug"), ws, start)
            self._miss_until[slug] = now + 60
            return None
        info = MarketInfo(
            asset=cfg["asset"], slug=slug, window_start=start, window_end=we or start + window,
            token_up=m["slug"], token_down=m["slug"], question=m.get("question", ""),
            fee_rate=0.0,                      # fees come from the account's tier (core/fees.py)
            single_instrument=True,
            qty_step=0.01,                     # docs: most markets trade in 0.01-contract steps
            min_notional=0.0,                  # no minimum order value is published for Polymarket US
            exchange_slug=m["slug"],
            price_to_beat=_amount(terms.get("priceToBeat")),
        )
        tick = m.get("orderPriceMinTickSize")
        if tick not in (None, 0.01):
            log.warning("%s: tick size is %s (expected 0.01)", m["slug"], tick)
        self._market_cache[slug] = info
        self._exchange_slug[slug] = m["slug"]
        log.info("Tracking %s: %s (%s)", cfg["asset"], info.question, m["slug"])
        return info

    def _refresh_price_to_beat(self, info: MarketInfo, now: float):
        """The open value is fixed a few seconds after the window starts; fetch it once it exists."""
        if info.price_to_beat is not None or now < info.window_start + 2:
            return
        if now - self._ptb_try.get(info.slug, 0.0) < 10:
            return
        self._ptb_try[info.slug] = now
        prefix = info.slug.rsplit("-", 1)[0]
        m = self._event_market(prefix, int(info.window_start))
        if m:
            info.price_to_beat = _amount((m.get("assetPriceTerms") or {}).get("priceToBeat"))

    def books(self, info: MarketInfo) -> tuple[Optional[Book], Optional[Book]]:
        data = self._get(f"/v1/markets/{info.exchange_slug}/book")
        if not isinstance(data, dict):
            now = time.time()
            if now - self._book_miss_log.get(info.asset, 0.0) >= 60:
                self._book_miss_log[info.asset] = now
                log.info("%s: Polymarket US has no order book for %s right now; skipping it until it does",
                         info.asset, info.exchange_slug)
            return None, None
        up, down, state = parse_book(data)
        if state != "MARKET_STATE_OPEN":
            return None, None              # pre-open, halted, expired...: nothing can trade
        return up, down

    def index_price(self, info: MarketInfo, now: float) -> Optional[float]:
        """Latest per-second value of the settlement index (BRTI) from Polymarket US's price API."""
        cfg = self._prefix_cfg.get(info.slug.rsplit("-", 1)[0], {})
        asset = cfg.get("index_asset", "crypto:btc")
        cached = self._index_cache.get(asset)
        if cached and now - cached[0] < 1.0:
            return cached[1]
        data = self._get("/v1/asset-prices/history",
                         {"assets": asset, "bucket": "ASSET_PRICE_BUCKET_1S", "from": int(now) - 10})
        price = None
        for series in (data or {}).get("series", []) or []:
            candles = series.get("candles") or []
            if candles:
                price = _amount(candles[-1].get("close"))
        self._index_cache[asset] = (now, price)
        return price

    def _exchange_slug_for(self, slug: str) -> Optional[str]:
        if slug in self._exchange_slug:
            return self._exchange_slug[slug]
        prefix, start = slug.rsplit("-", 1)[0], slug_start(slug)
        if start is None:
            return None
        m = self._event_market(prefix, start)
        if m:
            self._exchange_slug[slug] = m["slug"]
            return m["slug"]
        return None

    def resolution_detail(self, slug: str, window_end: Optional[float] = None) -> tuple[Optional[str], bool]:
        """(winner, official). Polymarket US settlements are final, so a known result is official."""
        ms = self._exchange_slug_for(slug)
        if ms is None:
            return None, False
        data = self._get(f"/v1/markets/{ms}/settlement")
        if not isinstance(data, dict) or data.get("settlement") is None:
            return None, False
        try:
            value = float(data["settlement"])
        except (TypeError, ValueError):
            return None, False
        if value == 1:
            return UP, True
        if value == 0:
            return DOWN, True
        log.error("%s settled at %s (not $1/$0: alternative settlement). Check the market rules.", ms, value)
        return None, False

    def resolution(self, slug: str, window_end: Optional[float] = None) -> Optional[str]:
        return self.resolution_detail(slug, window_end)[0]

    def prune(self, before_ts: float):
        for d in (self._market_cache, self._exchange_slug, self._miss_until, self._ptb_try):
            for k in [k for k in d if (slug_start(k) or 0) < before_ts]:
                del d[k]
