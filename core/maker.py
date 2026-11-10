"""
A5 (maker side). Resting paper bids and when they count as filled.

Paper maker fills are the easiest place to fool yourself, so the rule is strict:

  * A bid is live only `latency` seconds after it was placed (it has to reach the exchange).
  * It is filled ONLY when the market trades THROUGH its price: some seller is resting at a price
    strictly BELOW our bid (best ask < our price). Merely touching (best ask == our price) never
    fills -- we can't see whether the trades at that level reached us in the queue.
  * Fill size = the ask volume visible strictly below our price, minus (conservative mode) the
    queue that was ahead of us at our price when we joined. Visible-through volume is the only
    trading volume a book poll can see, and charging the queue against it assumes the worst.
  * Queue ahead only ever decreases when our price level shrinks, and only by the part that is
    clearly gone (we assume cancellations came from BEHIND us -- the pessimistic case).
  * Every fill is at OUR price (that's what a maker gets), with the maker fee (0 on Polymarket)
    and the configured maker rebate.

With a REST book poll every couple of seconds, trade-throughs that come and go between polls are
missed. That understates fills, which is the safe direction.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from core.models import Book


@dataclass
class Quote:
    strategy: str
    slug: str
    asset: str
    outcome: str
    price: float
    shares: float
    placed_ts: float
    live_ts: float              # placed_ts + latency
    queue_ahead: float          # shares ahead of us at our price
    filled: float = 0.0
    reason: str = ""
    meta: dict = field(default_factory=dict)
    # order log only (core/orderlog.py); never read by the fill rules
    order_id: Optional[int] = None
    decision: Optional[tuple] = None    # (best bid, best ask, depth, depth $) when the bid was placed
    got: float = 0.0                    # shares the broker actually booked (can be capped by cash)

    @property
    def remaining(self) -> float:
        return max(0.0, self.shares - self.filled)


def level_size(levels, price: float) -> float:
    return sum(s for p, s in levels if abs(p - price) < 1e-9)


class MakerBook:
    def __init__(self, latency: float = 0.25, conservative_queue: bool = True):
        self.latency = latency
        self.conservative_queue = conservative_queue
        self.quotes: dict[tuple[str, str, str], Quote] = {}     # (strategy, slug, outcome) -> Quote
        self.on_close = None        # optional callback(quote, why) when a bid is cancelled / replaced / expired

    def _closed(self, q: Quote, why: str):
        if self.on_close is not None:
            self.on_close(q, why)

    # ------------------------------------------------------------- placing / cancelling
    def place(self, strategy: str, slug: str, asset: str, outcome: str, price: float, shares: float,
              now: float, book: Book, reason: str = "", meta: Optional[dict] = None) -> Optional[Quote]:
        """Rest a bid (replaces this strategy's bid on that outcome). Returns None if it would cross."""
        self.cancel(strategy, slug, outcome, why="replaced")
        if shares <= 0 or price <= 0:
            return None
        if book.best_ask is not None and price >= book.best_ask - 1e-9:
            return None      # would take liquidity: not a maker order
        ahead = level_size(book.bids, price)       # everyone already bidding our price is ahead of us
        q = Quote(strategy, slug, asset, outcome, round(price, 6), shares, now, now + self.latency,
                  queue_ahead=ahead, reason=reason, meta=dict(meta or {}))
        self.quotes[(strategy, slug, outcome)] = q
        return q

    def cancel(self, strategy: str, slug: str, outcome: Optional[str] = None, why: str = "cancelled") -> int:
        keys = [k for k in self.quotes if k[0] == strategy and k[1] == slug and (outcome is None or k[2] == outcome)]
        for k in keys:
            self._closed(self.quotes.pop(k), why)
        return len(keys)

    def cancel_strategy(self, strategy: str) -> int:
        keys = [k for k in self.quotes if k[0] == strategy]
        for k in keys:
            self._closed(self.quotes.pop(k), "cancelled")
        return len(keys)

    def for_market(self, strategy: str, slug: str) -> dict[str, Quote]:
        return {k[2]: q for k, q in self.quotes.items() if k[0] == strategy and k[1] == slug}

    def drop_expired(self, now: float, window_end_of) -> None:
        """Remove quotes for rounds that have ended."""
        for k in [k for k, q in self.quotes.items() if window_end_of(q.slug) is not None and now >= window_end_of(q.slug)]:
            self._closed(self.quotes.pop(k), "round_end")

    # ------------------------------------------------------------- fills
    def check(self, strategy: str, slug: str, books: dict[str, Book], now: float) -> list[tuple[Quote, float]]:
        """[(quote, shares filled now)] for this strategy's quotes in this market, given the current books."""
        out = []
        for outcome, q in list(self.for_market(strategy, slug).items()):
            book = books[outcome]
            if now < q.live_ts:
                continue
            through = sum(s for p, s in book.asks if p < q.price - 1e-9)
            if through <= 1e-9:
                # queue bookkeeping: the level shrank -> some of the queue ahead is gone (assume the least)
                at_level = level_size(book.bids, q.price)
                if at_level < q.queue_ahead:
                    q.queue_ahead = at_level
                continue
            avail = through - (q.queue_ahead if self.conservative_queue else 0.0)
            if self.conservative_queue:
                q.queue_ahead = max(0.0, q.queue_ahead - through)
            take = min(q.remaining, max(0.0, avail))
            take = int(take * 100) / 100.0          # 0.01-contract steps
            if take <= 0:
                continue
            q.filled += take
            out.append((q, take))
            if q.remaining <= 1e-9:
                del self.quotes[(strategy, slug, outcome)]
        return out
