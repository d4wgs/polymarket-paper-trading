"""
The contract every strategy follows. You should never need to edit this file.

A strategy is a class with:
  PARAMS            default settings (dict). Override per-instance in config.py.
  on_tick(ctx)      called every few seconds. Return a list of orders (or []).
  on_window_start   optional: called once when a new 15-min window begins.
  on_settle         optional: called when a window you traded resolves.

`ctx` (a TickContext) gives you everything about "right now":
  ctx.asset, ctx.seconds_left, ctx.seconds_elapsed
  ctx.up / ctx.down           -> Book objects: .best_bid .best_ask .mid .spread .asks .bids
  ctx.position.up / .down     -> shares you hold in THIS window
  ctx.cash                    -> uninvested cash
  ctx.equity                  -> portfolio value right now: cash + open bets at the mid
  ctx.stake(fraction)         -> dollars for a new trade: fraction x portfolio value (config: 10%)
  ctx.fee_theta               -> this account's taker fee coefficient right now (US: by volume tier)
  ctx.fee_per_share(price)    -> taker fee for one contract at `price` (before cent rounding)
  ctx.price_to_beat           -> Up/Down open value (US), None until published
  ctx.index_price             -> settlement index right now (US: CF Benchmarks BRTI), or None
  ctx.history                 -> earlier Snapshots from this window (oldest first)
  ctx.prev_winner             -> "UP"/"DOWN": who won the window just before this one (None if unknown yet)
  ctx.prev_windows            -> winners of the rounds right before this one, newest last (an unbroken
                                 run of consecutive rounds: it stops at the first unknown result)
  ctx.traded_this_window      -> True once an order of this strategy FILLED in this window
                                 (survives a bot restart, unlike ctx.state; use it, not your own flag,
                                 so a rejected order gets retried instead of skipping the round)
  ctx.buy(...) / ctx.sell(...)-> build an order to return
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from core.models import BUY, SELL, UP, Book, Order, Position, Settlement, Snapshot

MIN_PRICE, MAX_PRICE = 0.001, 0.999   # Polymarket's price range (its smallest tick is 0.001)


@dataclass
class TickContext:
    snap: Snapshot
    position: Position
    cash: float
    history: list[Snapshot] = field(default_factory=list)
    prev_windows: list[str] = field(default_factory=list)
    prev_winner: Optional[str] = None   # "UP"/"DOWN": winner of the window right before this one (None if unknown)
    state: dict = field(default_factory=dict)   # per-window scratchpad, reset each window (and on restart)
    traded_this_window: bool = False            # any fill in this window so far (rebuilt from the database)
    equity: Optional[float] = None              # cash + open bets at the mid (None -> cash)
    fee_theta: Optional[float] = None           # taker fee coefficient for this account right now

    # handy shortcuts
    @property
    def asset(self) -> str: return self.snap.market.asset
    @property
    def ts(self) -> float: return self.snap.ts
    @property
    def seconds_left(self) -> float: return self.snap.seconds_left
    @property
    def seconds_elapsed(self) -> float: return self.snap.seconds_elapsed
    @property
    def up(self) -> Book: return self.snap.up
    @property
    def down(self) -> Book: return self.snap.down

    @property
    def price_to_beat(self) -> Optional[float]: return getattr(self.snap.market, "price_to_beat", None)
    @property
    def index_price(self) -> Optional[float]: return getattr(self.snap, "index_price", None)
    @property
    def min_notional(self) -> float: return getattr(self.snap.market, "min_notional", 1.0)
    @property
    def min_shares(self) -> float: return getattr(self.snap.market, "min_order_shares", 0.0) or 0.0

    def stake(self, fraction: float) -> float:
        """Dollars for a new trade: `fraction` of the current portfolio value (never more than cash)."""
        eq = self.equity if self.equity is not None else self.cash
        return max(0.0, min(eq * fraction, self.cash))

    def fee_per_share(self, price: float) -> float:
        theta = self.fee_theta if self.fee_theta is not None else self.snap.market.fee_rate
        return theta * (price * (1 - price)) ** getattr(self.snap.market, "fee_exponent", 1.0)

    def book(self, outcome: str) -> Book:
        return self.up if outcome == UP else self.down

    def has_position(self) -> bool:
        return self.position.up > 1e-9 or self.position.down > 1e-9

    def buy(self, outcome: str, usd: Optional[float] = None, shares: Optional[float] = None,
            max_price: Optional[float] = None, reason: str = "") -> Order:
        """Market-style buy that won't pay above max_price (default: best ask + 2c, capped at 0.999)."""
        if max_price is None and self.book(outcome).best_ask is not None:
            # Near 0 and 1 Polymarket trades in 0.001 steps, so the cap must allow 0.991-0.999.
            max_price = min(MAX_PRICE, self.book(outcome).best_ask + 0.02)
        return Order(outcome, BUY, usd=usd, shares=shares, limit=max_price, reason=reason)

    def sell(self, outcome: str, shares: Optional[float] = None,
             min_price: Optional[float] = None, reason: str = "") -> Order:
        """Sell shares (all if shares=None), won't accept below min_price (default: best bid - 2c, floor 0.001)."""
        if min_price is None and self.book(outcome).best_bid is not None:
            min_price = max(MIN_PRICE, self.book(outcome).best_bid - 0.02)
        return Order(outcome, SELL, shares=shares, limit=min_price, reason=reason)


class Strategy:
    PARAMS: dict = {}

    def __init__(self, name: str, assets: Optional[list[str]] = None,
                 bankroll: Optional[float] = None, title: Optional[str] = None,
                 description: Optional[str] = None, details: Optional[str] = None, **overrides):
        unknown = set(overrides) - set(self.PARAMS)
        if unknown:
            raise ValueError(f"{type(self).__name__} got unknown params {unknown}. "
                             f"Valid: {sorted(self.PARAMS)}")
        self.name = name
        self.assets = assets          # None = trade every asset in config.MARKETS
        self.bankroll = bankroll      # None = use config.STARTING_BANKROLL
        self.p = {**self.PARAMS, **overrides}
        # Display only (used by the dashboard). Not part of the strategy's logic,
        # so changing them does NOT require a new strategy name.
        self.title = title or name
        self.description = description or ""      # one-line summary shown on the strategy card
        self.details = details or ""              # full "how it works" text shown on hover

    def wants(self, asset: str) -> bool:
        return self.assets is None or asset in self.assets

    # ---- override these ----
    def on_window_start(self, ctx: TickContext) -> None:
        pass

    def on_tick(self, ctx: TickContext) -> list[Order]:
        return []

    def on_settle(self, result: Settlement) -> None:
        pass
