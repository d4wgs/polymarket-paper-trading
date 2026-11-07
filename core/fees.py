"""
Exchange fee models. The broker asks its fee model three things for every order:
  * theta(ts, market)            the taker fee coefficient for an order placed now
  * fee(levels, theta, market)   the fee charged for the fills (price levels) of one order
  * rebate_pct(ts)               share of that fee paid back as a taker rebate
and tells it about every fill (record) so volume-based tiers stay current.

POLYMARKET US  (https://docs.polymarket.us/fees -- checked 2026-11-07)
  Fee = Θ × C × p × (1 - p)      C = contracts, p = trade price of the instrument (YES side)
  Taker Θ by trailing taker volume (preceding 30 completed ET calendar days, excluding today),
  effective 1:00 PM ET, Nov 6 2026:
      below $10,000 -> 0.095 | $10,000-49,999 -> 0.085 | $50,000-249,999 -> 0.075 | $250,000+ -> 0.0695
  New accounts: Θ = 0.0695 on days 1-7 (day 1 = ET day of the first fill); on days 8-30 tiers use
  the higher of (first-7-day volume × 30/7) and trailing 30-day volume.
  Taker rebate (% of taker fees, paid weekly): <$250k 0% | $250k-999,999 3% | $1M-9,999,999 5% |
      $10M-24,999,999 10% | $25M-49,999,999 15% | $50M+ 30%
  Volume per fill = contracts × price of the side you received (buy: C×p, sell: C×(1-p)).
  Rounding: to the nearest $0.01, banker's rounding (half to even); on a multi-fill order the
  total can't exceed the rounded cumulative exact fee.
  Maker rebates are not modelled: every paper order is a taker order.

POLYMARKET (international, polymarket.com)  (https://docs.polymarket.com/trading/fees)
  Fee = C × feeRate × p × (1 - p), crypto feeRate 0.07, rounded to 5 decimals.
"""
from __future__ import annotations

import datetime as dt
from collections import defaultdict
from decimal import Decimal, ROUND_HALF_EVEN
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

US_TAKER_TIERS = [(0, 0.095), (10_000, 0.085), (50_000, 0.075), (250_000, 0.0695)]
US_TAKER_REBATE_TIERS = [(0, 0.0), (250_000, 0.03), (1_000_000, 0.05), (10_000_000, 0.10),
                         (25_000_000, 0.15), (50_000_000, 0.30)]
US_ONBOARDING_THETA = 0.0695
US_FEE_SOURCE = "https://docs.polymarket.us/fees (tiers effective 1:00 PM ET, Nov 6 2026)"


def et_day(ts: float) -> dt.date:
    return dt.datetime.fromtimestamp(ts, ET).date()


def round_cents(x: float) -> float:
    """Nearest $0.01, half to even (banker's rounding), as Polymarket US rounds fees."""
    return float(Decimal(repr(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN))


def _tier(table, volume):
    out = table[0][1]
    for threshold, value in table:
        if volume >= threshold:
            out = value
    return out


def fill_volume(side: str, shares: float, avg_price: float) -> float:
    """
    Polymarket US volume of one fill: contracts × price of the side you RECEIVED.
    In this bot every trade is a buy or sell of UP or DOWN at that side's price, so a buy's volume is
    what was paid (C×p) and a sell's is C×(1-p) -- e.g. selling UP at 0.90 receives the NO side at 0.10.
    """
    return shares * avg_price if side == "BUY" else shares * (1 - avg_price)


class IntlFees:
    """polymarket.com: per-market rate from the API (crypto 0.07), 5-decimal rounding, no tiers."""
    name = "polymarket_intl"

    def theta(self, ts, market) -> float:
        return market.fee_rate

    def fee(self, levels, theta, market) -> float:
        exp = getattr(market, "fee_exponent", 1.0)
        return sum(round(c * theta * (p * (1 - p)) ** exp, 5) for c, p in levels)

    def rebate_pct(self, ts) -> float:
        return 0.0

    def record(self, ts, side, shares, avg_price):
        pass


class USFees:
    """
    Polymarket US taker fees for ONE account (here: one strategy, so each strategy pays the tier its
    own volume earns -- what it would pay if you ran it alone on your account).

    new_account:
      None            -> an established account: tiers from trailing volume only (no onboarding boost)
      "per_strategy"  -> each strategy is a brand-new account; day 1 = the ET day of its first fill
      "YYYY-MM-DD"    -> day 1 of YOUR account's onboarding window (ET date of its first fill)
    theta_override: force one Θ (e.g. if Polymarket gave your account a manual override).
    """
    name = "polymarket_us"

    def __init__(self, new_account=None, theta_override=None):
        self.new_account = new_account
        self.theta_override = theta_override
        self.daily: dict[dt.date, float] = defaultdict(float)     # ET day -> taker volume
        self.first_day: dt.date | None = None
        if new_account not in (None, "per_strategy"):
            self.first_day = dt.date.fromisoformat(str(new_account))
        self._cache: dict[dt.date, tuple[float, float]] = {}

    def _account_day(self, day) -> int | None:
        """1 on the first fill's ET day, 2 the next, ...; None when there's no onboarding window."""
        if self.new_account is None or self.first_day is None:
            return None
        return (day - self.first_day).days + 1

    def _trailing(self, day) -> float:
        return sum(self.daily.get(day - dt.timedelta(days=i), 0.0) for i in range(1, 31))

    def _qualifying(self, day) -> float:
        vol = self._trailing(day)
        n = self._account_day(day)
        if n is not None and 8 <= n <= 30:
            first7 = sum(self.daily.get(self.first_day + dt.timedelta(days=i), 0.0) for i in range(7))
            vol = max(vol, first7 * 30 / 7)
        return vol

    def _tiers(self, day):
        # tiers are set once per ET day from completed days, so cache per day
        if day not in self._cache:
            q = self._qualifying(day)
            self._cache = {day: (_tier(US_TAKER_TIERS, q), _tier(US_TAKER_REBATE_TIERS, q))}
        return self._cache[day]

    def theta(self, ts, market) -> float:
        if self.theta_override is not None:
            return self.theta_override
        day = et_day(ts)
        n = self._account_day(day)
        if self.new_account == "per_strategy" and self.first_day is None:
            n = 1                                  # this order would be the account's first fill
        if n is not None and 1 <= n <= 7:
            return US_ONBOARDING_THETA
        return self._tiers(day)[0]

    def fee(self, levels, theta, market) -> float:
        exact = [theta * c * p * (1 - p) for c, p in levels]
        per_fill = sum(round_cents(x) for x in exact)
        return min(per_fill, round_cents(sum(exact)))

    def rebate_pct(self, ts) -> float:
        return self._tiers(et_day(ts))[1]

    def record(self, ts, side, shares, avg_price):
        day = et_day(ts)
        if self.first_day is None and self.new_account == "per_strategy":
            self.first_day = day
        self.daily[day] += fill_volume(side, shares, avg_price)
        self._cache.pop(day, None)     # today's tier doesn't change (it uses completed days), but be safe


def make_fee_model(cfg):
    """The fee model for one strategy account, from config.EXCHANGE."""
    if getattr(cfg, "EXCHANGE", "polymarket_intl") == "polymarket_us":
        return USFees(new_account=getattr(cfg, "US_NEW_ACCOUNT", None),
                      theta_override=getattr(cfg, "US_THETA_OVERRIDE", None))
    return IntlFees()
