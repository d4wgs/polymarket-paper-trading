"""
S1. FAIR-VALUE TRADER

Every tick, compare the model's P_up / P_down to the best asks. Buy whichever side has
    edge = fair - (ask + taker fee per share) >= min_edge
skipping the first `skip_first_seconds` and the last `skip_last_seconds` of the window.
The order's price limit is the highest price that still gives min_edge, so latency can't turn
it into a bad fill. Hold to resolution by default; optional early exit when selling now beats
the model by more than `exit_threshold` (bid - fee - fair > exit_threshold, i.e. the holding's
edge has flipped negative by more than that).
"""
from core.models import UP, DOWN
from strategies.base import Strategy, TickContext


class FairValueTrader(Strategy):
    PARAMS = {
        "min_edge": 0.04,
        "skip_first_seconds": 30,
        "skip_last_seconds": 10,
        "max_entries_per_window": 1,
        "stake_usd": 25.0,              # dollars per entry (risk.max_usd_per_trade caps it too)
        "stake_fraction": None,         # used instead if stake_usd is null
        "early_exit": False,
        "exit_threshold": 0.03,
    }

    def on_tick(self, ctx: TickContext):
        if ctx.fv is None:
            return []
        orders = []
        if self.p["early_exit"] and ctx.has_position():
            for side in (UP, DOWN):
                held = ctx.position.shares(side)
                bid = ctx.book(side).best_bid
                if held > 1e-9 and bid is not None:
                    hold_edge = ctx.fair(side) - (bid - ctx.fee_per_share(bid))
                    if hold_edge < -self.p["exit_threshold"]:
                        orders.append(ctx.sell(side, reason=f"early exit: bid {bid:.3f} beats fair "
                                                            f"{ctx.fair(side):.3f} by {-hold_edge:.3f}"))
        if orders:
            return orders
        if ctx.seconds_elapsed < self.p["skip_first_seconds"] or ctx.seconds_left < self.p["skip_last_seconds"]:
            return []
        if ctx.entries_this_window >= self.p["max_entries_per_window"]:
            return []
        best = None
        for side in (UP, DOWN):
            e = ctx.edge(side)
            if e is not None and e >= self.p["min_edge"] and (best is None or e > best[1]):
                best = (side, e)
        if best is None:
            return []
        side, e = best
        limit = ctx.max_price_for_edge(side, self.p["min_edge"])
        if limit is None:
            return []
        ask = ctx.book(side).best_ask
        return [ctx.buy(side, usd=self.stake_usd(ctx), max_price=limit,
                        reason=f"fair {ctx.fair(side):.3f} vs ask {ask:.3f}: edge {e:+.3f}", decision_edge=e)]
