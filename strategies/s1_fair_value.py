"""
S1. FAIR-VALUE TRADER (v2)

Every tick, compare a fair price to the best asks and buy whichever side has
    edge = fair - (ask + taker fee per share) >= min_edge
skipping the first `skip_first_seconds` and the last `skip_last_seconds` of the window. The order's
price limit is the highest price that still gives min_edge, so latency can't turn it into a bad fill.
Hold to resolution by default; optional early exit when selling now beats fair value by more than
`exit_threshold`.

What changed from v1, and why (from the live data of 2026-11-07):
  * fair = (1 - market_weight) x model + market_weight x market mid. Over the settled rounds the
    market mid forecast the result better than the model (Brier 0.229 vs 0.249), and when the two
    disagreed by 5c+ the market had not moved toward the model 20s later. So the model's view is
    shrunk toward the market instead of being taken at face value; only big disagreements trade.
  * max_model_gap: if |model - mid| is larger than this, something is off (a feed glitch, a wrong K, or
    quotes that haven't caught up with a jump). Skip. v1's best trade (+$275 of its +$292) was one of
    these: it bought DOWN at 7.7c one second after a $35 drop, and the next book had DOWN at 71c.
  * jump guard: if spot moved more than `jump_guard_sigmas` sigmas in the last `jump_lookback_seconds`,
    skip. Right after a jump the resting quotes are stale; a real order from here would be racing
    faster bots for them, so a paper fill at those prices is not believable.
  * min_price / max_price: no longshots or near-certainties. The normal model's tails are its least
    reliable part, and a 5c contract costs 9% of the stake in fees at the base fee tier.
  * max_spread: a wide book makes the mid (and the shrinkage) meaningless.
Every skip of a signal that v1 would have taken is logged as an event (one per reason per round).
"""
import math

from core.models import UP, DOWN
from strategies.base import Strategy, TickContext


class FairValueTrader(Strategy):
    PARAMS = {
        "min_edge": 0.04,
        "market_weight": 0.5,           # 0 = trust the model alone (v1), 1 = the market price (never trades)
        "max_model_gap": 0.25,          # |model - market mid| above this -> skip (data or stale-quote problem)
        "min_price": 0.10,              # don't buy below / above these prices
        "max_price": 0.90,
        "max_spread": 0.05,             # skip when that side's bid/ask spread is wider
        "jump_lookback_seconds": 5,
        "jump_guard_sigmas": 3.0,       # spot move over the lookback, in sigmas, that pauses entries (null = off)
        "skip_first_seconds": 30,
        "skip_last_seconds": 10,
        "max_entries_per_window": 1,
        "stake_usd": 25.0,              # dollars per entry (risk.max_usd_per_trade caps it too)
        "stake_fraction": None,         # used instead if stake_usd is null
        "early_exit": False,
        "exit_threshold": 0.03,
    }

    # ------------------------------------------------------------------ helpers
    def blended_fair(self, ctx: TickContext, side: str):
        """(fair used for trading, model fair, market mid) for `side`; None if the book has no mid."""
        model = ctx.fair(side)
        book = ctx.book(side)
        if model is None or book.best_bid is None or book.best_ask is None:
            return None                     # a one-sided book has no meaningful mid
        mid = book.mid
        w = float(self.p["market_weight"] or 0.0)
        return (1 - w) * model + w * mid, model, mid

    def jump_sigmas(self, ctx: TickContext):
        """How many sigmas spot moved over the lookback (None if unknown)."""
        L = self.p["jump_lookback_seconds"]
        fv = ctx.fv
        if not L or fv is None or not fv.sigma or ctx.md is None or not ctx.spot:
            return None
        then = ctx.md.spot_at(ctx.ts - L)
        if not then:
            return None
        return abs(math.log(ctx.spot / then)) / (fv.sigma * math.sqrt(L))

    def _skip(self, ctx, why, **data):
        key = f"skip_{why}"
        if not ctx.state.get(key):
            ctx.state[key] = True
            ctx.event("s1_skipped", why=why, **data)

    # ------------------------------------------------------------------ logic
    def on_tick(self, ctx: TickContext):
        if ctx.fv is None:
            return []
        orders = []
        if self.p["early_exit"] and ctx.has_position():
            for side in (UP, DOWN):
                held = ctx.position.shares(side)
                bid = ctx.book(side).best_bid
                bf = self.blended_fair(ctx, side)
                if held > 1e-9 and bid is not None and bf is not None:
                    hold_edge = bf[0] - (bid - ctx.fee_per_share(bid))
                    if hold_edge < -self.p["exit_threshold"]:
                        orders.append(ctx.sell(side, reason=f"early exit: bid {bid:.3f} beats fair "
                                                            f"{bf[0]:.3f} by {-hold_edge:.3f}"))
        if orders:
            return orders
        if ctx.seconds_elapsed < self.p["skip_first_seconds"] or ctx.seconds_left < self.p["skip_last_seconds"]:
            return []
        if ctx.entries_this_window >= self.p["max_entries_per_window"]:
            return []

        best = None
        for side in (UP, DOWN):
            book = ctx.book(side)
            ask = book.best_ask
            raw = ctx.edge(side)                       # v1's signal: model alone
            bf = self.blended_fair(ctx, side)
            if ask is None or bf is None or raw is None or raw < self.p["min_edge"]:
                continue
            fair, model, mid = bf
            gap = model - mid
            if abs(gap) > self.p["max_model_gap"]:
                self._skip(ctx, "model_gap", side=side, model=round(model, 3), mid=round(mid, 3))
                continue
            if not (self.p["min_price"] <= ask <= self.p["max_price"]):
                self._skip(ctx, "price_range", side=side, ask=ask)
                continue
            if book.spread is not None and book.spread > self.p["max_spread"] + 1e-9:
                self._skip(ctx, "spread", side=side, spread=round(book.spread, 3))
                continue
            e = ctx.edge(side, fair=fair)
            if e is None or e < self.p["min_edge"]:
                self._skip(ctx, "shrunk_edge", side=side, model_edge=round(raw, 3), edge=round(e or 0, 3))
                continue
            if best is None or e > best[1]:
                best = (side, e, fair, model, mid)
        if best is None:
            return []
        side, e, fair, model, mid = best
        if self.p["jump_guard_sigmas"]:
            js = self.jump_sigmas(ctx)
            if js is not None and js > self.p["jump_guard_sigmas"]:
                self._skip(ctx, "jump", side=side, sigmas=round(js, 2))
                return []
        limit = ctx.max_price_for_edge(side, self.p["min_edge"], fair=fair)
        if limit is None:
            return []
        limit = min(limit, self.p["max_price"])
        ask = ctx.book(side).best_ask
        return [ctx.buy(side, usd=self.stake_usd(ctx), max_price=limit,
                        reason=f"fair {fair:.3f} (model {model:.3f}, mid {mid:.3f}) vs ask {ask:.3f}: "
                               f"edge {e:+.3f}", decision_edge=e, model_fair=model, market_mid=mid)]
