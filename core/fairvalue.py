"""
A3. Fair-value model, and the market-data hub every strategy reads it from.

    P_up   = F( ln(S/K) / (sigma * sqrt(t)) )          P_down = 1 - P_up
      S     current settlement-reference price (the oracle; fallback: spot)
      K     window open price ("price to beat")
      sigma per-second EWMA vol of spot log returns x sigma_multiplier
      t     seconds remaining (floored at 1)
      F     standard normal CDF, or (toggle) a Student-t CDF with `df` degrees of freedom,
            rescaled to unit variance so sigma keeps its meaning and only the tails get fatter.

MarketData collects, per tick: spot (fast exchange), oracle (settlement index), the basis between
them, sigma, and each market's K; and it keeps the oracle's price history so K can be recovered when
the exchange doesn't publish it.
"""
from __future__ import annotations

import bisect
import math
from dataclasses import dataclass, asdict
from typing import Optional


# ------------------------------------------------------------------ distributions
def norm_cdf(x: float) -> float:
    return 0.5 * math.erfc(-x / math.sqrt(2.0))


def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the incomplete beta function (modified Lentz)."""
    tiny, eps = 1e-300, 3e-16
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 300):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            break
    return h


def betainc(a: float, b: float, x: float) -> float:
    """Regularized incomplete beta I_x(a, b)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    lbeta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log1p(-x)
    if x < (a + 1.0) / (a + b + 2.0):
        return math.exp(lbeta) * _betacf(a, b, x) / a
    return 1.0 - math.exp(lbeta) * _betacf(b, a, 1.0 - x) / b


def student_t_cdf(x: float, df: float) -> float:
    """CDF of the (standard, not rescaled) Student-t distribution."""
    if df <= 0:
        raise ValueError("df must be > 0")
    tail = 0.5 * betainc(df / 2.0, 0.5, df / (df + x * x))
    return 1.0 - tail if x > 0 else tail


def unit_variance_t_cdf(z: float, df: float) -> float:
    """Student-t scaled to variance 1 (df > 2): same sigma as the normal, fatter tails."""
    scale = math.sqrt(df / (df - 2.0)) if df > 2 else 1.0
    return student_t_cdf(z * scale, df)


# ------------------------------------------------------------------ the model
@dataclass
class FairValue:
    p_up: float
    p_down: float
    sigma: float          # per-second sigma actually used (multiplier applied)
    t: float              # seconds remaining used (floored at 1)
    S: float              # price used as "current" (oracle, or spot as fallback)
    K: float              # window open price
    z: float              # ln(S/K) / (sigma sqrt t)
    spot: Optional[float] = None
    oracle: Optional[float] = None
    basis: Optional[float] = None       # (spot - oracle) / oracle, None if either is missing
    s_source: str = "oracle"            # which price S came from
    k_source: str = "exchange"          # exchange (published) / oracle (first index print) / spot

    def of(self, outcome: str) -> float:
        return self.p_up if outcome == "UP" else self.p_down

    def as_tuple(self):
        return self.p_up, self.p_down, self.sigma, self.t, self.S, self.K

    def to_dict(self):
        return asdict(self)


def fair_value(S: float, K: float, sigma: float, t: float, *, sigma_multiplier: float = 1.0,
               dist: str = "normal", df: float = 4.0, min_t: float = 1.0) -> tuple[float, float, float]:
    """(P_up, P_down, z). Pure function: the unit-tested core of the model."""
    if S is None or K is None or S <= 0 or K <= 0:
        raise ValueError("S and K must be positive")
    if sigma is None or sigma <= 0:
        raise ValueError("sigma must be positive")
    t = max(float(t), min_t)
    sd = sigma * sigma_multiplier * math.sqrt(t)
    z = math.log(S / K) / sd
    p_up = unit_variance_t_cdf(z, df) if dist == "student_t" else norm_cdf(z)
    return p_up, 1.0 - p_up, z


def averaged_settlement(S: float, sigma: float, t: float, avg_seconds: float,
                        observed_mean: Optional[float] = None) -> tuple[float, float]:
    """
    Polymarket US settles on the simple AVERAGE of the index over the last `avg_seconds` (60) before
    the close. Returns (center, sd) for ln(final average) under a driftless random walk:
      t >= A : center = S,  var = sigma^2 * ((t - A) + A/3)
      t <  A : the first (A - t) seconds of the average are already known (observed_mean):
               center = ((A - t) * observed_mean + t * S) / A,  sd = (t/A) * S * sigma * sqrt(t/3) / center
    avg_seconds = 0 gives the point-settlement model (center S, sd sigma*sqrt(t)).
    """
    A = float(avg_seconds or 0)
    if A <= 0:
        return S, sigma * math.sqrt(t)
    if t >= A:
        return S, sigma * math.sqrt((t - A) + A / 3.0)
    m = observed_mean if observed_mean else S
    w = t / A
    center = (1 - w) * m + w * S
    return center, max(w * S * sigma * math.sqrt(t / 3.0) / center, 1e-12)


def fair_value_averaged(S: float, K: float, sigma: float, t: float, avg_seconds: float,
                        observed_mean: Optional[float] = None, *, sigma_multiplier: float = 1.0,
                        dist: str = "normal", df: float = 4.0, min_t: float = 1.0) -> tuple[float, float, float]:
    """(P_up, P_down, z) when settlement is an average (see averaged_settlement)."""
    if S is None or K is None or S <= 0 or K <= 0:
        raise ValueError("S and K must be positive")
    if sigma is None or sigma <= 0:
        raise ValueError("sigma must be positive")
    t = max(float(t), min_t)
    center, sd = averaged_settlement(S, sigma * sigma_multiplier, t, avg_seconds, observed_mean)
    z = math.log(center / K) / sd
    p_up = unit_variance_t_cdf(z, df) if dist == "student_t" else norm_cdf(z)
    return p_up, 1.0 - p_up, z


# ------------------------------------------------------------------ market data hub
class OracleHistory:
    def __init__(self, keep_seconds: int = 4 * 3600):
        self.keep = keep_seconds
        self.ts: list[float] = []
        self.px: list[float] = []

    def add(self, ts: float, price: float):
        if self.ts and ts <= self.ts[-1]:
            if ts == self.ts[-1]:
                self.px[-1] = price
            return
        self.ts.append(ts)
        self.px.append(price)
        if len(self.ts) > 20000:
            cut = bisect.bisect_left(self.ts, ts - self.keep)
            del self.ts[:cut], self.px[:cut]

    def at(self, ts: float, max_age: float = 10.0) -> Optional[float]:
        i = bisect.bisect_right(self.ts, ts) - 1
        if i < 0 or ts - self.ts[i] > max_age:
            return None
        return self.px[i]

    def first_after(self, ts: float, within: float = 15.0) -> Optional[float]:
        i = bisect.bisect_left(self.ts, ts)
        if i >= len(self.ts) or self.ts[i] - ts > within:
            return None
        return self.px[i]


class MarketData:
    """
    Spot + oracle + vol + fair value for every market. One instance, shared by the engine and
    (read-only, via TickContext) every strategy.
    """

    def __init__(self, spot, model_cfg: Optional[dict] = None):
        cfg = model_cfg or {}
        self.spot = spot                                         # SpotFeed (or None)
        self.sigma_multiplier = float(cfg.get("sigma_multiplier", 1.0))
        self.dist = cfg.get("distribution", "normal")
        self.df = float(cfg.get("student_t_df", 4))
        self.min_t = float(cfg.get("min_seconds", 1))
        self.k_from_oracle = bool(cfg.get("k_fallback_to_oracle", True))
        self.s_source = cfg.get("s_source", "oracle")                   # oracle | spot (other = fallback)
        self.avg_seconds = float(cfg.get("settlement_average_seconds", 0) or 0)
        self.require_published_k = bool(cfg.get("require_published_k", False))
        self.oracle = OracleHistory()
        self.markets: dict[str, object] = {}                     # slug -> MarketInfo
        self.last: dict[str, FairValue] = {}                     # slug -> latest fair value
        self._k_cache: dict[str, tuple[float, str]] = {}

    # --------------------------------------------------------- inputs
    def on_oracle(self, ts: float, price: Optional[float]):
        if price:
            self.oracle.add(ts, price)

    def register(self, info):
        self.markets[info.slug] = info

    # --------------------------------------------------------- reads
    def spot_now(self, now: float) -> Optional[float]:
        return self.spot.price(now) if self.spot is not None else None

    def spot_at(self, ts: float) -> Optional[float]:
        return self.spot.price_at(ts) if self.spot is not None else None

    def oracle_at(self, ts: float) -> Optional[float]:
        return self.oracle.at(ts)

    def sigma(self) -> Optional[float]:
        return self.spot.sigma() if self.spot is not None else None

    def basis(self, now: float, oracle: Optional[float] = None) -> Optional[float]:
        spot = self.spot_now(now)
        oracle = oracle if oracle is not None else self.oracle.at(now)
        if not spot or not oracle:
            return None
        return (spot - oracle) / oracle

    def window_open(self, info) -> tuple[Optional[float], str]:
        """K: the exchange's published open price, else the first oracle print of the window, else spot."""
        ptb = getattr(info, "price_to_beat", None)
        if ptb:
            return ptb, "exchange"
        if self.require_published_k:
            return None, "none"
        if info.slug in self._k_cache:
            return self._k_cache[info.slug]
        if self.avg_seconds > 0 and self.s_source == "spot":
            # the rules' K is the average of the minute before the start: estimate it from spot
            px = [p for p in (self.spot_at(s) for s in range(int(info.window_start - self.avg_seconds),
                                                             int(info.window_start))) if p]
            if len(px) >= self.avg_seconds / 2:
                k = sum(px) / len(px)
                self._k_cache[info.slug] = (k, "spot_avg")
                return k, "spot_avg"
        if self.k_from_oracle:
            k = self.oracle.first_after(info.window_start)
            if k:
                self._k_cache[info.slug] = (k, "oracle")
                return k, "oracle"
        k = self.spot_at(info.window_start)
        if k:
            self._k_cache[info.slug] = (k, "spot")
            return k, "spot"
        return None, "none"

    def compute(self, info, now: float, oracle: Optional[float] = None) -> Optional[FairValue]:
        """Fair value of one market right now (None if spot/oracle, K or sigma is missing)."""
        self.register(info)
        spot = self.spot_now(now)
        if oracle is None:
            oracle = self.oracle.at(now)
        if self.s_source == "spot":
            S, s_src = (spot, "spot") if spot else (oracle, "oracle")
        else:
            S, s_src = (oracle, "oracle") if oracle else (spot, "spot")
        K, k_src = self.window_open(info)
        sigma = self.sigma()
        if not S or not K or not sigma:
            return None
        t = max(info.window_end - now, self.min_t)
        if self.avg_seconds > 0:
            p_up, p_down, z = fair_value_averaged(
                S, K, sigma, t, self.avg_seconds, self._observed_mean(info, now, s_src),
                sigma_multiplier=self.sigma_multiplier, dist=self.dist, df=self.df, min_t=self.min_t)
        else:
            p_up, p_down, z = fair_value(S, K, sigma, t, sigma_multiplier=self.sigma_multiplier,
                                         dist=self.dist, df=self.df, min_t=self.min_t)
        fv = FairValue(p_up=p_up, p_down=p_down, sigma=sigma * self.sigma_multiplier, t=t, S=S, K=K, z=z,
                       spot=spot, oracle=oracle,
                       basis=(spot - oracle) / oracle if spot and oracle else None,
                       s_source=s_src, k_source=k_src)
        self.last[info.slug] = fv
        return fv

    def _observed_mean(self, info, now: float, source: str) -> Optional[float]:
        """Mean of the price source over the part of the settlement-averaging minute already elapsed."""
        start = info.window_end - self.avg_seconds
        if now <= start:
            return None
        at = self.spot_at if source == "spot" else self.oracle_at
        px = [p for p in (at(sec) for sec in range(int(math.ceil(start)), int(now) + 1)) if p]
        return sum(px) / len(px) if px else None

    def fair_value(self, market_id: str, now: Optional[float] = None):
        """Spec interface: fair_value(market_id) -> (P_up, P_down, sigma, t, S, K), or None."""
        import time as _t
        info = self.markets.get(market_id)
        if info is None:
            return None
        fv = self.compute(info, now if now is not None else _t.time())
        return fv.as_tuple() if fv else None

    def prune(self, before_ts: float):
        for slug in [s for s, m in self.markets.items() if m.window_end < before_ts]:
            self.markets.pop(slug, None)
            self.last.pop(slug, None)
            self._k_cache.pop(slug, None)
