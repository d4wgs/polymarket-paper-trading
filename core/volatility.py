"""
A2. Volatility estimator: EWMA standard deviation of 1-second log returns of the spot price.

    var_t = lam^dt * var_{t-1} + (1 - lam^dt) * r^2 / dt        lam = 0.5 ** (1 / half_life)

`r` is the log return since the previous sample and `dt` the seconds between them, so a gap
(a missed second, a slow tick) is handled as dt one-second returns of equal size: the estimate
stays "per second" whatever the sampling interval.
"""
from __future__ import annotations

import math
from typing import Optional


class EwmaVol:
    def __init__(self, half_life: float = 300.0, min_samples: int = 30, max_gap: float = 60.0):
        self.half_life = float(half_life)
        self.lam = 0.5 ** (1.0 / self.half_life)
        self.min_samples = min_samples   # sigma is None until this many returns were seen (unless seeded)
        self.max_gap = max_gap           # a longer gap (feed outage) is not counted as a return
        self.var: Optional[float] = None
        self.n = 0
        self.last_ts: Optional[float] = None
        self.last_px: Optional[float] = None

    def seed(self, sigma_per_second: float, samples: Optional[int] = None):
        """Start from an estimate (e.g. from 1-minute candles) so the bot can trade right away."""
        if sigma_per_second and sigma_per_second > 0:
            self.var = sigma_per_second ** 2
            self.n = max(self.n, samples if samples is not None else self.min_samples)

    def update(self, ts: float, price: float):
        if price is None or price <= 0:
            return
        if self.last_ts is None or ts <= self.last_ts:
            if self.last_ts is None:
                self.last_ts, self.last_px = ts, price
            return
        dt = ts - self.last_ts
        if dt <= self.max_gap:
            r = math.log(price / self.last_px)
            per_sec = r * r / dt
            if self.var is None:
                self.var = per_sec
            else:
                decay = self.lam ** dt
                self.var = decay * self.var + (1 - decay) * per_sec
            self.n += 1
        self.last_ts, self.last_px = ts, price

    @property
    def sigma(self) -> Optional[float]:
        """Per-second standard deviation of log returns (None while warming up)."""
        if self.var is None or self.n < self.min_samples:
            return None
        return math.sqrt(self.var)


def sigma_from_closes(closes: list[float], seconds_per_bar: float) -> Optional[float]:
    """Per-second sigma from a series of bar closes (oldest first), e.g. 1-minute candles."""
    rets = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
    if len(rets) < 5:
        return None
    var_bar = sum(r * r for r in rets) / len(rets)
    return math.sqrt(var_bar / seconds_per_bar)
