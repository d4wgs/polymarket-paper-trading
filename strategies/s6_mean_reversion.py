"""
S6. MEAN REVERSION AFTER A PROBABILITY SHOCK

Shock: one side's market price (mid) moved >= shock_size within shock_lookback seconds while the
model's fair value for it moved < max_fair_move over the same period -- the crowd jumped, the
underlying didn't. Fade it: buy the OTHER side, if its edge >= min_edge.

Exit (exit_mode "revert", default): sell when the gap between market price and fair value has
closed by gap_close_frac (50%), or at the time stop (time_stop_seconds), whichever comes first.
exit_mode "resolution": hold to the end.

Default: 5-minute markets only (config). One entry per window. The entry's gap is kept in the
per-window scratchpad, so after a bot restart an open position is simply held to resolution.
"""
from core.models import UP, DOWN
from strategies.base import Strategy, TickContext


class ShockReversion(Strategy):
    PARAMS = {
        "shock_size": 0.12,
        "shock_lookback": 10,
        "max_fair_move": 0.04,
        "min_edge": 0.03,
        "exit_mode": "revert",           # revert | resolution
        "gap_close_frac": 0.5,
        "time_stop_seconds": 60,
        "skip_last_seconds": 15,
        "stake_usd": 25.0,
        "stake_fraction": None,
    }

    @staticmethod
    def _gap(snap) -> float | None:
        """Market minus model for UP (DOWN's gap is the negative)."""
        if snap.fv is None or snap.up.mid is None:
            return None
        return snap.up.mid - snap.fv.p_up

    def on_tick(self, ctx: TickContext):
        st = ctx.state
        # ---- exits
        entry = st.get("entry")
        if entry and ctx.position.shares(entry["side"]) > 1e-9:
            if self.p["exit_mode"] == "resolution":
                return []
            gap = self._gap(ctx.snap)
            closed = gap is not None and abs(gap) <= (1 - self.p["gap_close_frac"]) * abs(entry["gap0"])
            timed_out = ctx.ts - entry["ts"] >= self.p["time_stop_seconds"]
            if closed or timed_out:
                why = "gap closed" if closed else "time stop"
                return [ctx.sell(entry["side"], reason=f"shock fade exit: {why} (gap {entry['gap0']:+.3f} -> "
                                                       f"{gap if gap is not None else float('nan'):+.3f})")]
            return []
        if entry and ctx.entries_this_window > 0:
            return []                            # already in and out this window
        if ctx.fv is None or ctx.entries_this_window > 0 or ctx.seconds_left < self.p["skip_last_seconds"]:
            return []

        # ---- shock detection: compare with the snapshot ~shock_lookback seconds ago
        then = None
        for h in ctx.history:
            if ctx.ts - h.ts <= self.p["shock_lookback"]:
                then = h
                break
        if then is None or then.fv is None or then.up.mid is None or ctx.up.mid is None:
            return []
        d_mkt = ctx.up.mid - then.up.mid
        d_fair = ctx.fv.p_up - then.fv.p_up
        if abs(d_mkt) < self.p["shock_size"] or abs(d_fair) >= self.p["max_fair_move"]:
            return []
        side = DOWN if d_mkt > 0 else UP          # UP was bid up without reason -> buy DOWN, and vice versa
        e = ctx.edge(side)
        if e is None or e < self.p["min_edge"]:
            ctx.event("shock_no_edge", shock=round(d_mkt, 3), fair_move=round(d_fair, 3), side=side, edge=e)
            return []
        limit = ctx.max_price_for_edge(side, self.p["min_edge"])
        if limit is None:
            return []
        st["entry"] = {"side": side, "ts": ctx.ts, "gap0": self._gap(ctx.snap)}
        return [ctx.buy(side, usd=self.stake_usd(ctx), max_price=limit,
                        reason=f"fade shock: Up mid {d_mkt:+.3f} in {ctx.ts - then.ts:.0f}s, fair {d_fair:+.3f}",
                        shock=d_mkt, fair_move=d_fair, decision_edge=e)]
