"""
LAST 60s: back whoever is winning with one minute to go.

In the last 60 seconds of a window, buy the side the market currently prices
above 50% (the current favorite) and hold to resolution. One trade per window.
If neither side is above 50% (a dead heat), it keeps checking until the window ends.
"""
from strategies.base import Strategy, TickContext


class Last60s(Strategy):
    PARAMS = {
        "stake_fraction": 0.10,       # each new trade = 10% of the current portfolio value
        "enter_seconds_left": 60,   # act once this many seconds (or fewer) remain
    }

    def on_tick(self, ctx: TickContext):
        # traded_this_window turns True only once an order FILLS; a rejected order is retried next tick
        if ctx.traded_this_window or ctx.has_position():
            return []
        if ctx.seconds_left > self.p["enter_seconds_left"]:
            return []
        for side in ("UP", "DOWN"):
            book = ctx.book(side)
            if book.mid is not None and book.mid > 0.5 and book.best_ask is not None:
                return [ctx.buy(side, usd=ctx.stake(self.p["stake_fraction"]),
                                reason=f"{side} favored at {book.mid:.2f}, {ctx.seconds_left:.0f}s left")]
        return []
