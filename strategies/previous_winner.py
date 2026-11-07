"""
PREVIOUS WINNER: follow the momentum of the round that just ended.

15 seconds into a new window, buy whichever side (Up or Down) won the PREVIOUS
window of the same market, and hold to resolution. One trade per window.

If the previous window's result isn't known yet, it keeps checking until
`give_up_after_seconds`; after that it skips the window.
"""
from strategies.base import Strategy, TickContext


class PreviousWinner(Strategy):
    PARAMS = {
        "stake_fraction": 0.10,       # each new trade = 10% of the current portfolio value
        "enter_after_seconds": 15,     # wait this long after the window opens
        "give_up_after_seconds": 90,   # skip the window if the previous result still isn't known
    }

    def on_tick(self, ctx: TickContext):
        # traded_this_window turns True only once an order actually FILLS, so a rejected order
        # (price moved past the limit, under $1) is simply retried on the next tick.
        if ctx.traded_this_window or ctx.has_position():
            return []
        if ctx.seconds_elapsed < self.p["enter_after_seconds"]:
            return []
        if ctx.seconds_elapsed > self.p["give_up_after_seconds"]:
            return []
        side = ctx.prev_winner
        if side is None or ctx.book(side).best_ask is None:
            return []
        return [ctx.buy(side, usd=ctx.stake(self.p["stake_fraction"]),
                        reason=f"previous round was {side}, {ctx.seconds_elapsed:.0f}s in")]
