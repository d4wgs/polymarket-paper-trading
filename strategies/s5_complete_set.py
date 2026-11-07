"""
S5. COMPLETE-SET ARBITRAGE

Every tick: what would `n` Up + `n` Down cost, walking the real asks of both books, fees included?
    cost per set = avg ask Up + avg ask Down + (fee Up + fee Down) / n
If cost <= 1.00 - min_profit, buy both legs (one set pays exactly $1 at resolution).

Leg risk is simulated honestly: leg 1 (the thinner book) is sent first and filled after the
latency; leg 2 is sent only then, filled after ANOTHER latency against a freshly fetched book, for
the shares leg 1 actually got, with a price limit that keeps the pair at break-even
(leg2_policy "breakeven") -- or at any price (leg2_policy "complete"). If leg 2 doesn't fill,
leg 1 is simply held to resolution and an `arb_leg2_failed` event is recorded.

Near-misses (cost between 1.00 and 1.01) are logged, at most one per near_miss_log_seconds per
market, plus a per-window summary (ticks seen, near-misses, opportunities, cheapest cost).

NOTE: On Polymarket US both outcomes are ONE order book (Down ask = 1 - Up bid), so a set always
costs 1 + spread + fees: this can never trigger there. It is meaningful on polymarket.com, where
Up and Down are separate books. The near-miss log shows how close it gets.
"""
from core.models import UP, DOWN, Order
from strategies.base import MAX_PRICE, Strategy, TickContext


class CompleteSetArb(Strategy):
    PARAMS = {
        "min_profit": 0.005,
        "near_miss_max_cost": 1.01,
        "near_miss_log_seconds": 5,
        "leg2_policy": "breakeven",      # breakeven | complete
        "stake_usd": 50.0,               # dollars for the whole set (both legs)
        "stake_fraction": None,
        "skip_last_seconds": 5,
    }

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._win: dict[str, dict] = {}          # asset -> this window's counters
        self._late_events: dict[str, list] = {}  # asset -> events produced by leg callbacks

    def on_window_start(self, ctx: TickContext):
        prev = self._win.get(ctx.asset)
        if prev and prev["ticks"]:
            ctx.event("arb_window_summary", **prev)
        self._win[ctx.asset] = {"slug": ctx.snap.market.slug, "ticks": 0, "near_misses": 0,
                                "opportunities": 0, "min_cost": None}

    def on_tick(self, ctx: TickContext):
        for kind, data in self._late_events.pop(ctx.asset, []):
            ctx.event(kind, **data)
        w = self._win.setdefault(ctx.asset, {"slug": ctx.snap.market.slug, "ticks": 0, "near_misses": 0,
                                             "opportunities": 0, "min_cost": None})
        a_up, a_dn = ctx.up.best_ask, ctx.down.best_ask
        if a_up is None or a_dn is None:
            return []
        target = self.stake_usd(ctx) / (a_up + a_dn)
        got_u, _, _ = ctx.walk_cost(UP, target)
        got_d, _, _ = ctx.walk_cost(DOWN, target)
        n = int(min(target, got_u, got_d) * 100) / 100.0
        if n <= 0:
            return []
        _, avg_u, fee_u = ctx.walk_cost(UP, n)
        _, avg_d, fee_d = ctx.walk_cost(DOWN, n)
        cost = avg_u + avg_d + (fee_u + fee_d) / n
        w["ticks"] += 1
        w["min_cost"] = cost if w["min_cost"] is None else min(w["min_cost"], cost)
        if 1.0 < cost <= self.p["near_miss_max_cost"]:
            w["near_misses"] += 1
            if ctx.ts - ctx.state.get("last_near_miss", 0) >= self.p["near_miss_log_seconds"]:
                ctx.state["last_near_miss"] = ctx.ts
                ctx.event("arb_near_miss", cost=round(cost, 5), ask_up=a_up, ask_down=a_dn, shares=n)
        if cost > 1.0 - self.p["min_profit"] or ctx.seconds_left < self.p["skip_last_seconds"]:
            return []
        w["opportunities"] += 1
        # leg 1 = the thinner book (the one more likely to vanish)
        depth = {UP: sum(s for _, s in ctx.up.asks), DOWN: sum(s for _, s in ctx.down.asks)}
        leg1 = UP if depth[UP] <= depth[DOWN] else DOWN
        leg2 = DOWN if leg1 == UP else UP
        worst1 = max(p for p, _ in (ctx.up.asks if leg1 == UP else ctx.down.asks)[:10])
        fee_ps = ctx.fee_per_share
        asset, policy = ctx.asset, self.p["leg2_policy"]

        def after_leg1(fills):
            f = [x for x in fills if x.outcome == leg1 and x.side == "BUY"]
            sh = sum(x.shares for x in f)
            if sh <= 0:
                self._late_events.setdefault(asset, []).append(("arb_leg1_unfilled", {"cost_seen": cost}))
                return None
            c1 = sum(x.usd + x.fee for x in f) / sh
            if policy == "complete":
                limit = MAX_PRICE
            else:   # highest leg-2 price that still makes the set break even after fees
                limit, p = None, 0.001
                while p <= MAX_PRICE + 1e-9:
                    if c1 + p + fee_ps(p) <= 1.0 + 1e-12:
                        limit = p
                    p = round(p + 0.001, 3)
            if limit is None:
                self._late_events.setdefault(asset, []).append(
                    ("arb_leg2_failed", {"why": "no break-even price", "leg1_cost": c1, "shares": sh}))
                return None

            def after_leg2(fills2):
                got = sum(x.shares for x in fills2 if x.side in ("BUY", "SELL"))
                if got < sh - 1e-6:
                    self._late_events.setdefault(asset, []).append(
                        ("arb_leg2_failed", {"why": "book moved during latency", "leg1_cost": c1,
                                             "leg1_shares": sh, "leg2_shares": got, "leg2_limit": limit}))
                else:
                    self._late_events.setdefault(asset, []).append(
                        ("arb_completed", {"leg1_cost": c1, "shares": sh, "cost_seen": cost}))
                return None
            return Order(leg2, "BUY", shares=sh, limit=limit, reason=f"arb leg 2 (leg 1 cost {c1:.4f})",
                         then=after_leg2, meta={"arb_leg": 2, "cost_seen": cost})

        return [ctx.buy(leg1, shares=n, max_price=worst1, reason=f"arb leg 1: set cost {cost:.4f}",
                        then=after_leg1, arb_leg=1, cost_seen=cost)]
