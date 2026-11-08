"""
S4. CROWD-MOMENTUM CONFIRMATION

S4 (CrowdMomentum): buy a side when ALL agree:
    the market prices it at >= min_side_price (0.70, by mid),
    spot has moved its way since the window opened (spot now vs spot at the open), and
    the oracle agrees (oracle now vs K).
  Bought only if ask <= max_entry. No edge requirement by default -- every fill records the
  model's fair value and edge, so the dashboard shows whether this beats S1 or is "S1 with extra steps".

S4b (MinuteBuckets, 5-minute markets only): if spot moved the same direction in each of the first
`buckets` one-minute buckets, buy that direction at the start of the final minute, only if
ask <= max_entry (0.85).

Both: one entry per window, held to resolution.
"""
from core.models import UP, DOWN
from strategies.base import Strategy, TickContext


class CrowdMomentum(Strategy):
    PARAMS = {
        "min_side_price": 0.70,
        "max_entry": 0.90,
        "min_edge": None,                # e.g. 0.0 to also require non-negative model edge
        "skip_first_seconds": 30,
        "skip_last_seconds": 10,
        "stake_usd": 25.0,
        "stake_fraction": None,
    }

    def on_tick(self, ctx: TickContext):
        if ctx.entries_this_window > 0 or ctx.md is None:
            return []
        if ctx.seconds_elapsed < self.p["skip_first_seconds"] or ctx.seconds_left < self.p["skip_last_seconds"]:
            return []
        for side in (UP, DOWN):
            mid = ctx.book(side).mid
            if mid is None or mid < self.p["min_side_price"]:
                continue
            spot_open = ctx.md.spot_at(ctx.snap.market.window_start)
            spot_now = ctx.spot
            fv = ctx.fv
            if not spot_open or not spot_now or fv is None or fv.oracle is None:
                return []
            spot_up = spot_now > spot_open
            oracle_up = fv.oracle >= fv.K
            if spot_now == spot_open or spot_up != (side == UP) or oracle_up != (side == UP):
                return []
            ask = ctx.book(side).best_ask
            if ask is None or ask > self.p["max_entry"]:
                return []
            e = ctx.edge(side)
            if self.p["min_edge"] is not None and (e is None or e < self.p["min_edge"]):
                return []
            return [ctx.buy(side, usd=self.stake_usd(ctx), max_price=self.p["max_entry"],
                            reason=f"crowd {mid:.2f} + spot + oracle agree on {side}",
                            decision_edge=e, spot_move=spot_now / spot_open - 1)]
        return []


class MinuteBuckets(Strategy):
    PARAMS = {
        "buckets": 3,
        "max_entry": 0.85,
        "entry_window_seconds": 10,      # buy within this many seconds of the final minute starting
        "min_side_price": None,          # market-agreement gate: skip unless that side's mid >= this (v2: 0.50).
                                         # v1 bought 2-35c sides the market disagreed with and lost 5 of 5.
        "stake_usd": 25.0,
        "stake_fraction": None,
    }

    def on_tick(self, ctx: TickContext):
        if ctx.entries_this_window > 0 or ctx.md is None or ctx.kind != "5m":
            return []
        final_minute = ctx.snap.market.window_end - 60
        if not (final_minute <= ctx.ts <= final_minute + self.p["entry_window_seconds"]):
            return []
        start = ctx.snap.market.window_start
        marks = [ctx.md.spot_at(start + 60 * i) for i in range(self.p["buckets"] + 1)]
        if any(m is None for m in marks):
            return []
        moves = [b - a for a, b in zip(marks, marks[1:])]
        if all(m > 0 for m in moves):
            side = UP
        elif all(m < 0 for m in moves):
            side = DOWN
        else:
            return []
        mid = ctx.book(side).mid
        if self.p["min_side_price"] is not None and (mid is None or mid < self.p["min_side_price"]):
            ctx.event("buckets_market_disagrees", side=side, mid=mid)
            return []
        ask = ctx.book(side).best_ask
        if ask is None or ask > self.p["max_entry"]:
            ctx.event("buckets_too_expensive", side=side, ask=ask)
            return []
        return [ctx.buy(side, usd=self.stake_usd(ctx), max_price=self.p["max_entry"],
                        reason=f"{self.p['buckets']} one-minute buckets all {side}", decision_edge=ctx.edge(side))]
