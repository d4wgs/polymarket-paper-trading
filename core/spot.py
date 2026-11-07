"""
A1. Spot reference: a live price from a fast exchange feed (Coinbase or Binance websocket).

The websocket runs in a background thread and keeps
  * the latest trade price,
  * one price per second (the last trade of each second) for the last few hours, and
  * the EWMA volatility estimator (core/volatility.py), updated once per second.

If the websocket is down, the engine's call to poll() falls back to the exchange's REST ticker
(at most once a second), so a dead socket degrades to slower data instead of no data.

Sources (config `spot.source`):
  coinbase   wss://ws-feed.exchange.coinbase.com     product e.g. BTC-USD   (works from the US)
  binance    wss://stream.binance.com:9443           symbol e.g. btcusdt    (blocked from US IPs)
  binanceus  wss://stream.binance.us:9443            symbol e.g. btcusd

Paper trading only READS public data.
"""
from __future__ import annotations

import bisect
import json
import logging
import threading
import time
from typing import Optional

import requests

from core.volatility import EwmaVol, sigma_from_closes

log = logging.getLogger("spot")

SOURCES = {
    "coinbase": {"ws": "wss://ws-feed.exchange.coinbase.com",
                 "rest": "https://api.exchange.coinbase.com/products/{product}/ticker",
                 "candles": "https://api.exchange.coinbase.com/products/{product}/candles?granularity=60"},
    "binance": {"ws": "wss://stream.binance.com:9443/ws/{symbol}@trade",
                "rest": "https://api.binance.com/api/v3/ticker/price?symbol={SYMBOL}",
                "candles": "https://api.binance.com/api/v3/klines?symbol={SYMBOL}&interval=1m&limit=300"},
    "binanceus": {"ws": "wss://stream.binance.us:9443/ws/{symbol}@trade",
                  "rest": "https://api.binance.us/api/v3/ticker/price?symbol={SYMBOL}",
                  "candles": "https://api.binance.us/api/v3/klines?symbol={SYMBOL}&interval=1m&limit=300"},
}


class PriceHistory:
    """One price per second (the last seen in that second), oldest first, capped in length."""

    def __init__(self, keep_seconds: int = 4 * 3600):
        self.keep = keep_seconds
        self.secs: list[int] = []
        self.px: list[float] = []

    def add(self, ts: float, price: float) -> bool:
        """Returns True when this price starts a new second."""
        sec = int(ts)
        if self.secs and sec < self.secs[-1]:
            return False                       # out of order: ignore
        if self.secs and sec == self.secs[-1]:
            self.px[-1] = price
            return False
        self.secs.append(sec)
        self.px.append(price)
        if len(self.secs) > self.keep * 1.2:
            cut = len(self.secs) - self.keep
            del self.secs[:cut], self.px[:cut]
        return True

    def at(self, ts: float, max_age: float = 5.0) -> Optional[float]:
        """Last price at or before `ts` (None if the nearest is older than max_age seconds)."""
        i = bisect.bisect_right(self.secs, int(ts)) - 1
        if i < 0 or ts - self.secs[i] > max_age + 1:
            return None
        return self.px[i]

    def last(self) -> tuple[Optional[int], Optional[float]]:
        return (self.secs[-1], self.px[-1]) if self.secs else (None, None)


class SpotFeed:
    def __init__(self, source: str = "coinbase", product: str = "BTC-USD", symbol: str = "btcusdt",
                 half_life: float = 300.0, stale_seconds: float = 5.0, bootstrap: bool = True,
                 min_samples: int = 30):
        if source not in SOURCES:
            raise ValueError(f"spot.source must be one of {sorted(SOURCES)}, not {source!r}")
        self.source, self.product, self.symbol = source, product, symbol.lower()
        self.stale_seconds = stale_seconds
        self.history = PriceHistory()
        self.vol = EwmaVol(half_life, min_samples=min_samples)
        self._lock = threading.Lock()
        self._last_trade: tuple[float, float] | None = None     # (local receive time, price)
        self._last_rest = 0.0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._bootstrap = bootstrap

    def _url(self, kind: str) -> str:
        return SOURCES[self.source][kind].format(product=self.product, symbol=self.symbol,
                                                 SYMBOL=self.symbol.upper())

    # ------------------------------------------------------------- lifecycle
    def start(self):
        if self._bootstrap:
            self._seed_vol()
        self._thread = threading.Thread(target=self._run, name="spot-ws", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _seed_vol(self):
        """Seed sigma from the last ~5 hours of 1-minute candles so the model works from the start."""
        try:
            data = requests.get(self._url("candles"), timeout=5,
                                headers={"User-Agent": "polymarket-paper-trader/1.0"}).json()
            if self.source == "coinbase":       # [[time, low, high, open, close, volume], ...] newest first
                closes = [float(c[4]) for c in sorted(data, key=lambda c: c[0])]
            else:                               # [[openTime, open, high, low, close, ...], ...] oldest first
                closes = [float(c[4]) for c in data]
            sig = sigma_from_closes(closes, 60.0)
            if sig:
                self.vol.seed(sig)
                log.info("Seeded volatility from %d one-minute candles: sigma = %.2e per second "
                         "(%.1f%% annualized)", len(closes), sig, sig * (365 * 86400) ** 0.5 * 100)
        except Exception as e:      # the EWMA simply warms up from live data instead
            log.warning("Could not seed volatility from candles (%s); warming up from live ticks", e)

    def _run(self):
        from websockets.sync.client import connect
        backoff = 1.0
        while not self._stop.is_set():
            try:
                url = self._url("ws")
                with connect(url, open_timeout=10, close_timeout=2, max_size=2 ** 22) as ws:
                    if self.source == "coinbase":
                        ws.send(json.dumps({"type": "subscribe", "product_ids": [self.product],
                                            "channels": ["ticker"]}))
                    log.info("Spot feed connected: %s %s", self.source, self.product if self.source == "coinbase"
                             else self.symbol)
                    backoff = 1.0
                    while not self._stop.is_set():
                        msg = ws.recv(timeout=30)
                        self._on_message(msg)
            except Exception as e:
                if self._stop.is_set():
                    break
                log.warning("Spot websocket error (%s); reconnecting in %.0fs", e, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    def _on_message(self, msg):
        d = json.loads(msg)
        px = None
        if self.source == "coinbase":
            if d.get("type") == "ticker" and d.get("price"):
                px = float(d["price"])
        elif d.get("e") == "trade" and d.get("p"):
            px = float(d["p"])
        if px:
            self.on_price(time.time(), px)

    # ------------------------------------------------------------- data in
    def on_price(self, ts: float, price: float):
        with self._lock:
            self._last_trade = (ts, price)
            if self.history.add(ts, price):
                self.vol.update(ts, price)

    def poll(self, now: float):
        """Called by the engine every tick: REST fallback when the websocket has gone quiet."""
        last = self._last_trade
        if last is not None and now - last[0] < self.stale_seconds:
            return
        if now - self._last_rest < 1.0:
            return
        self._last_rest = now
        try:
            d = requests.get(self._url("rest"), timeout=3,
                             headers={"User-Agent": "polymarket-paper-trader/1.0"}).json()
            px = float(d.get("price"))
            self.on_price(time.time(), px)
        except Exception as e:
            log.debug("Spot REST fallback failed: %s", e)

    # ------------------------------------------------------------- data out
    def price(self, now: float) -> Optional[float]:
        last = self._last_trade
        if last is None or now - last[0] > self.stale_seconds:
            return None
        return last[1]

    def price_at(self, ts: float) -> Optional[float]:
        with self._lock:
            return self.history.at(ts)

    def sigma(self) -> Optional[float]:
        return self.vol.sigma


class SimSpot(SpotFeed):
    """Simulator spot feed: reads the simulated underlying (no network), sampled on each engine tick."""

    def __init__(self, price_fn, half_life: float = 300.0, noise_bps: float = 0.5, seed: int = 1,
                 min_samples: int = 30):
        import random
        super().__init__("coinbase", half_life=half_life, bootstrap=False, min_samples=min_samples)
        self.price_fn = price_fn          # now -> underlying price
        self.noise = noise_bps / 1e4
        self.rng = random.Random(seed)

    def start(self):
        pass

    def poll(self, now: float):
        px = self.price_fn(now)
        if px:
            self.on_price(now, px * (1 + self.rng.gauss(0, self.noise)))

    def price(self, now: float) -> Optional[float]:
        last = self._last_trade
        return None if last is None else last[1]
