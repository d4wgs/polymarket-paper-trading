"""
S3. END-OF-WINDOW SNIPER

Active only when t <= snipe_window (60s on 5m markets, 120s on 15m). Buys the side the model says
is all but decided:
    z = |ln(S/K)| / (sigma * sqrt(t)) >= z_threshold          (default 2.5)
    winning side's ask between min_ask and max_ask            (0.90 - 0.98)
    edge = fair - (ask + fee) >= min_edge                      (0.01)
    basis filter: |spot - oracle| <= max_basis_frac * |S - K|  (0.25): skip when the two feeds disagree
                  by a meaningful part of the distance that decides the round
One entry per window, held to resolution.

Breakeven hit rate = entry price (+ fee). The dashboard shows this strategy's hit rate next to its
average entry price; skipped signals are logged as events so you can see what the filters cost.
"""
import math

from core.models import UP, DOWN
from strategies.base import Strategy, TickContext


class Sniper(Strategy):
    PARAMS = {
        "snipe_window": {"5m": 60, "15m": 120},
        "z_threshold": 2.5,
        "min_ask": 0.90,
        "max_ask": 0.98,
        "min_edge": 0.01,
        "max_basis_frac": 0.25,
        "min_seconds_left": 3,
        "stake_usd": 25.0,
        "stake_fraction": None,
    }

    def on_tick(self, ctx: TickContext):
        fv = ctx.fv
        if fv is None or ctx.entries_this_window > 0:
            return []
        if not (self.p["min_seconds_left"] <= ctx.seconds_left <= self.param("snipe_window", ctx)):
            return []
        z = abs(math.log(fv.S / fv.K)) / (fv.sigma * math.sqrt(fv.t))
        if z < self.p["z_threshold"]:
            return []
        side = UP if fv.S >= fv.K else DOWN
        ask = ctx.book(side).best_ask
        if ask is None or not (self.p["min_ask"] <= ask <= self.p["max_ask"]):
            return []
        e = ctx.edge(side)
        if e is None or e < self.p["min_edge"]:
            return []
        if fv.spot is None or fv.oracle is None:
            return []
        if abs(fv.spot - fv.oracle) > self.p["max_basis_frac"] * abs(fv.S - fv.K):
            if not ctx.state.get("basis_logged"):
                ctx.state["basis_logged"] = True
                ctx.event("snipe_skipped_basis", side=side, z=round(z, 2), ask=ask,
                          basis_usd=round(fv.spot - fv.oracle, 2), distance_usd=round(fv.S - fv.K, 2))
            return []
        limit = min(self.p["max_ask"], ctx.max_price_for_edge(side, self.p["min_edge"]) or 0)
        if limit < ask:
            return []
        return [ctx.buy(side, usd=self.stake_usd(ctx), max_price=limit,
                        reason=f"snipe z={z:.2f} ask {ask:.3f} fair {fv.of(side):.3f}", z=z, decision_edge=e)]
