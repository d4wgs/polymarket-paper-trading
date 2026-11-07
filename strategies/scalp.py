"""
SCALP: a quick in-and-out trade inside one window.

15 seconds into the window, buy at the current market price. Then sell (at the
current market price) as soon as ANY of these happens:
  * take profit: what you could sell for is 10% above your cost per share
  * stop loss:   what you could sell for has dropped 5% since you bought
  * time:        1 minute (60s) is left in the window
One trade per window.

"Cost per share" includes the buy fee, and "what you could sell for" is the best bid,
so the take-profit is measured on real, achievable prices. The stop loss is measured
against the best bid AT ENTRY, not against the price paid: right after buying, the bid
already sits below what you paid by the spread plus the fee, and measuring against
the price paid would fire the stop on that gap alone, before the market moved at all.
(After a bot restart the entry bid is unknown, so the stop is measured from the bid at
the first tick after the restart.)

Which side to buy isn't dictated by the idea, so it's a setting: "favorite" (default)
buys whichever side is priced above 50c; "UP" or "DOWN" always buys that side.
The entry price is read from the position itself, so it keeps working after a restart.
"""
from strategies.base import Strategy, TickContext


class Scalp(Strategy):
    PARAMS = {
        "stake_fraction": 0.10,       # each new trade = 10% of the current portfolio value
        "enter_after_seconds": 15,    # buy this long after the window opens
        "enter_before_seconds": 45,   # don't open a trade later than this (e.g. after a restart mid-round)
        "take_profit": 0.10,          # sell when the sell price is +10% vs cost per share (fee included)
        "stop_loss": 0.05,            # sell when the sell price has fallen 5% since entry
        "exit_seconds_left": 60,      # sell whatever is left with this many seconds to go
        "side": "favorite",           # "favorite" | "UP" | "DOWN"
    }

    def _pick_side(self, ctx: TickContext):
        if self.p["side"] in ("UP", "DOWN"):
            return self.p["side"]
        up, down = ctx.up.mid, ctx.down.mid
        if up is None or down is None:
            return None
        return "UP" if up >= down else "DOWN"

    def on_tick(self, ctx: TickContext):
        pos = ctx.position

        # ---- exit logic (we hold something) ----
        if ctx.has_position():
            side = "UP" if pos.up >= pos.down else "DOWN"
            shares = pos.shares(side)
            bid = ctx.book(side).best_bid
            cost = pos.avg_cost(side)
            if bid is None or cost is None:
                return []
            if bid * shares < ctx.min_notional or shares < ctx.min_shares:
                return []                               # too small to sell (< $1 or < min size): held to the result
            if ctx.state.get("entry_bid") is None:
                ctx.state["entry_bid"] = bid            # restart (or no bid at entry): measure from now
            gain = bid / cost - 1
            drop = bid / ctx.state["entry_bid"] - 1
            if gain >= self.p["take_profit"]:
                why = f"take profit {gain:+.1%} vs cost"
            elif drop <= -self.p["stop_loss"]:
                why = f"stop loss: bid {drop:+.1%} since entry ({gain:+.1%} vs cost)"
            elif ctx.seconds_left <= self.p["exit_seconds_left"]:
                why = f"time exit {gain:+.1%} vs cost, {ctx.seconds_left:.0f}s left"
            else:
                return []
            return [ctx.sell(side, reason=why)]

        # ---- entry logic ----
        if ctx.traded_this_window:
            return []                                   # already traded this window (a FILL happened)
        if not (self.p["enter_after_seconds"] <= ctx.seconds_elapsed <= self.p["enter_before_seconds"]):
            return []
        if ctx.seconds_left <= self.p["exit_seconds_left"]:
            return []                                   # too late to open a trade
        side = self._pick_side(ctx)
        if side is None or ctx.book(side).best_ask is None:
            return []
        ctx.state["entry_bid"] = ctx.book(side).best_bid   # if this buy doesn't fill, the next try overwrites it
        return [ctx.buy(side, usd=ctx.stake(self.p["stake_fraction"]),
                        reason=f"enter {side} at {ctx.book(side).best_ask:.2f}, {ctx.seconds_elapsed:.0f}s in")]
