"""
Plain data objects shared by every part of the bot.

Nothing in here talks to the internet or the database. These are just the
"nouns" of the system: an order book, a market, an order, a fill.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

UP, DOWN = "UP", "DOWN"
BUY, SELL = "BUY", "SELL"
TAKER, MAKER_BID, CANCEL = "taker", "maker_bid", "cancel"     # Order.kind


@dataclass
class Book:
    """One side's order book. Prices are 0-1 (cents on the dollar)."""
    bids: list[tuple[float, float]] = field(default_factory=list)  # (price, shares), best (highest) first
    asks: list[tuple[float, float]] = field(default_factory=list)  # (price, shares), best (lowest) first
    ts: Optional[float] = None   # when the exchange produced this book (unix seconds), if known

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0][0] if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0][0] if self.asks else None

    @property
    def mid(self) -> Optional[float]:
        if self.best_bid is not None and self.best_ask is not None:
            return (self.best_bid + self.best_ask) / 2
        return self.best_bid if self.best_ask is None else self.best_ask

    @property
    def spread(self) -> Optional[float]:
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_ask - self.best_bid

    def ask_depth_usd(self, max_price: float = 1.0) -> float:
        """Dollars you could spend buying, without paying more than max_price."""
        return sum(p * s for p, s in self.asks if p <= max_price)

    def bid_depth_shares(self, min_price: float = 0.0) -> float:
        """Shares you could sell, without receiving less than min_price."""
        return sum(s for p, s in self.bids if p >= min_price)

    def copy(self) -> "Book":
        return Book(list(self.bids), list(self.asks), self.ts)


# The UP and DOWN books are mirror images (an UP bid at p IS a DOWN ask at 1-p), so on a
# consistent pair of books UP ask + DOWN ask >= 1 and UP bid + DOWN bid <= 1. Anything else means
# the two books were captured at different moments -- trading on that would fill at prices that
# never existed together (e.g. a "pair" for under $1).
BOOK_TOLERANCE = 1e-6
MAX_BOOK_SKEW_SECONDS = 1.5


def books_consistent(up: "Book", down: "Book") -> bool:
    if up.best_ask is not None and down.best_ask is not None and up.best_ask + down.best_ask < 1 - BOOK_TOLERANCE:
        return False
    if up.best_bid is not None and down.best_bid is not None and up.best_bid + down.best_bid > 1 + BOOK_TOLERANCE:
        return False
    if up.ts is not None and down.ts is not None and abs(up.ts - down.ts) > MAX_BOOK_SKEW_SECONDS:
        return False
    return True


@dataclass
class MarketInfo:
    """One 15-minute window of one asset (e.g. 'Bitcoin Up or Down 2:15-2:30pm')."""
    asset: str
    slug: str
    window_start: float  # unix seconds
    window_end: float    # unix seconds
    token_up: str
    token_down: str
    question: str = ""
    fee_rate: float = 0.0
    fee_exponent: float = 1.0     # fee = shares * fee_rate * (p * (1-p)) ** fee_exponent
    min_order_shares: float = 0.0  # exchange minimum for orders sized in shares (Polymarket: 5)
    # --- Polymarket US ---
    single_instrument: bool = False  # one YES instrument: "buying DOWN" while long UP closes the UP position
    qty_step: float = 0.0            # contracts trade in steps of this (US: 0.01); 0 = any size
    min_notional: float = 1.00       # smallest marketable order in $ (polymarket.com: $1)
    exchange_slug: str = ""          # the exchange's own market slug, when it differs from `slug`
    price_to_beat: Optional[float] = None   # Up/Down open value (US publishes it a few seconds after start)


@dataclass
class Snapshot:
    """What the market looked like at one moment. Strategies read these."""
    ts: float
    market: MarketInfo
    up: Book
    down: Book
    index_price: Optional[float] = None    # settlement index (US: CF Benchmarks BRTI) at this moment
    fv: Any = None                         # core.fairvalue.FairValue at this moment (None if unavailable)
    spot: Optional[float] = None           # fast exchange spot price at this moment

    @property
    def seconds_left(self) -> float:
        return max(0.0, self.market.window_end - self.ts)

    @property
    def seconds_elapsed(self) -> float:
        return max(0.0, self.ts - self.market.window_start)


@dataclass
class Order:
    """
    A request from a strategy. Exactly one of `usd` or `shares` should be set.
      BUY  + usd=10       -> spend up to $10 (plus fee)
      BUY  + shares=20    -> buy up to 20 shares
      SELL + shares=None  -> sell everything you hold of that outcome
    `limit` = worst price you accept (max for buys, min for sells).

    kind:
      "taker"     (default) crosses the spread now; filled against the book as it looks after the
                  configured latency.
      "maker_bid" rests a bid for `shares` at `limit` (replacing this strategy's bid on that outcome);
                  filled only when the market trades THROUGH the price (core/maker.py).
      "cancel"    cancels this strategy's resting bid on `outcome` (or on both if outcome is None).
    then: optional callable(fills) -> Order | None, run after another latency delay against a freshly
          fetched book (multi-leg orders, e.g. complete-set arbitrage).
    meta: extra numbers the strategy wants recorded with the fill (e.g. the signal that fired).
    hedge: True for a BUY that completes a position this strategy already holds in ANOTHER market (e.g. the
          second leg of a cross-market pair). It reduces risk, so the kill switch and the open-markets limit
          don't block it (the dollar caps still apply). The engine only honours it while such a position exists.
    """
    outcome: Optional[str]  # UP or DOWN (None only for a cancel-all)
    side: str               # BUY or SELL
    usd: Optional[float] = None
    shares: Optional[float] = None
    limit: Optional[float] = None
    reason: str = ""
    kind: str = TAKER
    then: Optional[Callable[[list], Optional["Order"]]] = None
    meta: dict = field(default_factory=dict)
    hedge: bool = False


@dataclass
class Fill:
    ts: float
    strategy: str
    asset: str
    slug: str
    window_end: float
    outcome: str
    side: str
    shares: float
    avg_price: float
    usd: float      # shares * avg_price (before fee)
    fee: float
    reason: str = ""
    theta: Optional[float] = None   # taker fee coefficient used
    rebate: float = 0.0             # taker rebate earned on this fill (credited to cash)
    liquidity: str = "taker"        # taker / maker
    meta: dict = field(default_factory=dict)   # fair value, edge, spot, oracle, basis, sigma, ...


@dataclass
class Position:
    """A strategy's holdings in one 15-minute market."""
    asset: str
    slug: str
    window_end: float
    up: float = 0.0          # shares of UP
    down: float = 0.0        # shares of DOWN
    net_cost: float = 0.0    # cash out - cash in, fees included
    # What was paid for the shares BOUGHT (fees included), per outcome. Unlike net_cost these are
    # never reduced by sales, so avg_cost() stays the true entry price after a partial exit.
    bought_up: float = 0.0
    bought_down: float = 0.0
    buy_cost_up: float = 0.0
    buy_cost_down: float = 0.0

    def shares(self, outcome: str) -> float:
        return self.up if outcome == UP else self.down

    def avg_cost(self, outcome: str) -> Optional[float]:
        """Average price paid per share bought of `outcome`, fees included (None if never bought)."""
        bought = self.bought_up if outcome == UP else self.bought_down
        cost = self.buy_cost_up if outcome == UP else self.buy_cost_down
        return cost / bought if bought > 1e-9 else None

    def add_buy(self, outcome: str, shares: float, cost: float):
        if outcome == UP:
            self.up += shares
            self.bought_up += shares
            self.buy_cost_up += cost
        else:
            self.down += shares
            self.bought_down += shares
            self.buy_cost_down += cost
        self.net_cost += cost

    def add_sell(self, outcome: str, shares: float, proceeds: float):
        if outcome == UP:
            self.up -= shares
        else:
            self.down -= shares
        self.net_cost -= proceeds

    def is_empty(self) -> bool:
        """No shares and no money in or out (nothing worth keeping or settling)."""
        return self.up < 1e-9 and self.down < 1e-9 and abs(self.net_cost) < 1e-9


@dataclass
class Settlement:
    ts: float
    strategy: str
    asset: str
    slug: str
    winner: str
    up_shares: float
    down_shares: float
    net_cost: float
    payout: float
    pnl: float
