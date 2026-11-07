"""
S2. MARKET MAKER

Rests bids on BOTH Up and Down around the model's fair value:
    bid_up   = P_up   - half_spread - inventory_skew
    bid_down = P_down - half_spread + inventory_skew
    inventory_skew = skew_per_share * (Up held - Down held)
so whichever side we hold more of gets a lower bid (we buy less of it) and the other side a
higher one. bid_up + bid_down is kept below 1.00 (buying both would lock in a loss otherwise).

Requotes (cancel + new bid, losing queue position) only when fair value moved more than
requote_threshold since the last quote, or a bid is missing. Adverse-selection guard: if spot moved
more than guard_bps within the last second, pull all quotes for guard_cooldown_seconds.
Stops quoting in the last stop_quoting_seconds of a window; never quotes a side once the
inventory imbalance reaches max_imbalance_shares. Inventory is held to resolution.

Fills are simulated strictly (core/maker.py): only when the market trades THROUGH our price.
The engine records the fair value 5s after every fill; the dashboard shows the average move
against us (adverse selection).

On Polymarket US there is one instrument: a Down bid is an offer to sell Up at 1 - price, so
buying Down while long Up closes the Up position (inventory nets out).
"""
import math

from core.models import UP, DOWN
from strategies.base import Strategy, TickContext

TICK = 0.01


def _floor_tick(p: float) -> float:
    return math.floor(p / TICK + 1e-9) * TICK


class MarketMaker(Strategy):
    PARAMS = {
        "half_spread": 0.02,
        "skew_per_share": 0.0005,        # 100 shares of net inventory moves both quotes by 5c
        "requote_threshold": 0.01,
        "guard_bps": 5.0,                # spot move in 1s (basis points) that pulls quotes
        "guard_cooldown_seconds": 3,
        "stop_quoting_seconds": 45,
        "skip_first_seconds": 5,
        "quote_shares": 20,
        "max_imbalance_shares": 100,
        "min_price": 0.02,
        "max_price": 0.98,
    }

    def on_tick(self, ctx: TickContext):
        st = ctx.state
        fv = ctx.fv
        quoting = ctx.quotes
        if (fv is None or ctx.kill_switch or ctx.seconds_left <= self.p["stop_quoting_seconds"]
                or ctx.seconds_elapsed < self.p["skip_first_seconds"]):
            st.pop("fv_quoted", None)
            return [ctx.cancel()] if quoting else []

        # adverse-selection guard: spot jumped within the last second -> pull quotes and wait
        now_px, prev_px = ctx.md.spot_at(ctx.ts) if ctx.md else None, ctx.md.spot_at(ctx.ts - 1) if ctx.md else None
        if now_px and prev_px and abs(now_px / prev_px - 1) * 1e4 > self.p["guard_bps"]:
            st["cooldown_until"] = ctx.ts + self.p["guard_cooldown_seconds"]
            st.pop("fv_quoted", None)
            ctx.event("mm_guard", spot_move_bps=round((now_px / prev_px - 1) * 1e4, 2))
            return [ctx.cancel()] if quoting else []
        if ctx.ts < st.get("cooldown_until", 0):
            return [ctx.cancel()] if quoting else []

        last = st.get("fv_quoted")
        need = (last is None or abs(fv.p_up - last) > self.p["requote_threshold"]
                or set(quoting) != set(st.get("sides", [])))
        if not need:
            return []

        net = ctx.position.up - ctx.position.down
        skew = self.p["skew_per_share"] * net
        hs = self.p["half_spread"]
        bids = {UP: _floor_tick(fv.p_up - hs - skew), DOWN: _floor_tick(fv.p_down - hs + skew)}
        while bids[UP] + bids[DOWN] >= 1.0 - 1e-9:          # never pay >= $1 for a complete set
            bids[UP] = round(bids[UP] - TICK, 2)
            bids[DOWN] = round(bids[DOWN] - TICK, 2)
        allowed = {UP: net < self.p["max_imbalance_shares"], DOWN: -net < self.p["max_imbalance_shares"]}

        orders, sides = [ctx.cancel()], []
        for side in (UP, DOWN):
            price = bids[side]
            ask = ctx.book(side).best_ask
            if ask is not None and price >= ask:
                price = _floor_tick(ask - TICK)               # stay a maker: never cross the ask
            if not allowed[side] or not (self.p["min_price"] <= price <= self.p["max_price"]):
                continue
            orders.append(ctx.bid(side, round(price, 2), self.p["quote_shares"],
                                  reason=f"MM bid {price:.2f} (fair {fv.of(side):.3f}, inv {net:+.0f})",
                                  inventory=net, half_spread=hs))
            sides.append(side)
        st["fv_quoted"] = fv.p_up
        st["sides"] = sides
        return orders
