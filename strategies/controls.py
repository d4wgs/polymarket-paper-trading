"""
NO-SIGNAL CONTROLS (2026-11-09)

A control trades in the SAME entry window, the SAME price band and at the SAME stake as its strategy family,
with the family's signal taken out. A strategy is then judged as "strategy minus its control" on the rounds both
traded (tools/validate.py prints the paired difference): if the control does as well, the signal adds nothing.

  SniperFavorite    sniper family (S3): in the last `snipe_seconds`, buy the market's favourite (the side with the
                    higher ask; a tie goes to Up) instead of the side the index / model says is winning.
                    Timing, price gates, retry behaviour and order are inherited from the Sniper unchanged.
  MomentumFavorite  momentum family (S4): the first side priced at >= `min_side_price` (mid) with ask <=
                    `max_entry`, inside the same window, WITHOUT the spot and oracle confirmation (and no edge gate).
  BucketsFavorite   minute-buckets family (S4b, 5-minute rounds): at the start of the final minute buy the
                    favourite (the side whose mid is higher, and >= `min_side_price`) if ask <= `max_entry`,
                    WITHOUT the one-minute-bucket condition.
  ShockRandom       shock family (S6, 5-minute rounds): on the same market shock (mid moved >= `shock_size` within
                    `shock_lookback` seconds) buy a RANDOM side instead of fading it; no fair-value or edge test.
                    The side is a fixed function of (seed, round), so a restart or a replay picks the same side.
                    Exit: sell after `time_stop_seconds` (the family's time stop; there is no model gap to close).

These are new classes in their own file on purpose: the families' source files (and so their source hashes and
track records) are untouched. The control <-> strategy pairing lives in config.yaml (`controls:`).
"""
import hashlib

from core.models import UP, DOWN
from strategies.base import Strategy, TickContext
from strategies.s3_sniper import Sniper


def favorite_by_ask(ctx: TickContext):
    """(side, its ask, the other side's ask): the side the market prices higher; a tie goes to Up."""
    a_up, a_dn = ctx.up.best_ask, ctx.down.best_ask
    if a_up is None or a_dn is None:
        return None
    return (UP, a_up, a_dn) if a_up >= a_dn else (DOWN, a_dn, a_up)


def seeded_side(seed, slug: str) -> str:
    """UP or DOWN, a fixed coin flip per (seed, round)."""
    h = hashlib.sha256(f"{seed}:{slug}".encode("utf-8")).digest()
    return UP if h[0] % 2 == 0 else DOWN


class SniperFavorite(Sniper):
    """Sniper timing and price gates; the side is the market's favourite, not the index / model signal."""

    @staticmethod
    def winning_side(ctx: TickContext, signal: str = "index"):
        fav = favorite_by_ask(ctx)
        if fav is None:
            return None
        side, ask, other = fav
        return side, ask, other, "control: favourite ask"


class MomentumFavorite(Strategy):
    PARAMS = {
        "min_side_price": 0.70,
        "max_entry": 0.90,
        "skip_first_seconds": 30,
        "skip_last_seconds": 10,
        "stake_usd": 25.0,
        "stake_fraction": None,
    }

    def on_tick(self, ctx: TickContext):
        if ctx.entries_this_window > 0:
            return []
        if ctx.seconds_elapsed < self.p["skip_first_seconds"] or ctx.seconds_left < self.p["skip_last_seconds"]:
            return []
        for side in (UP, DOWN):
            mid = ctx.book(side).mid
            if mid is None or mid < self.p["min_side_price"]:
                continue
            ask = ctx.book(side).best_ask
            if ask is None or ask > self.p["max_entry"]:
                return []
            return [ctx.buy(side, usd=self.stake_usd(ctx), max_price=self.p["max_entry"],
                            reason=f"control: crowd {mid:.2f} on {side}, no confirmation",
                            decision_edge=ctx.edge(side))]
        return []


class BucketsFavorite(Strategy):
    PARAMS = {
        "max_entry": 0.85,
        "entry_window_seconds": 10,
        "min_side_price": 0.50,
        "stake_usd": 25.0,
        "stake_fraction": None,
    }

    def on_tick(self, ctx: TickContext):
        if ctx.entries_this_window > 0 or ctx.kind != "5m":
            return []
        final_minute = ctx.snap.market.window_end - 60
        if not (final_minute <= ctx.ts <= final_minute + self.p["entry_window_seconds"]):
            return []
        m_up, m_dn = ctx.up.mid, ctx.down.mid
        if m_up is None or m_dn is None:
            return []
        side, mid = (UP, m_up) if m_up >= m_dn else (DOWN, m_dn)
        if self.p["min_side_price"] is not None and mid < self.p["min_side_price"]:
            return []
        ask = ctx.book(side).best_ask
        if ask is None or ask > self.p["max_entry"]:
            return []
        return [ctx.buy(side, usd=self.stake_usd(ctx), max_price=self.p["max_entry"],
                        reason=f"control: favourite {side} (mid {mid:.2f}) at the final minute",
                        decision_edge=ctx.edge(side))]


class ShockRandom(Strategy):
    PARAMS = {
        "shock_size": 0.12,
        "shock_lookback": 10,
        "time_stop_seconds": 60,
        "skip_last_seconds": 15,
        "seed": 12345,
        "stake_usd": 25.0,
        "stake_fraction": None,
    }

    def on_tick(self, ctx: TickContext):
        st = ctx.state
        entry = st.get("entry")
        if entry and ctx.position.shares(entry["side"]) > 1e-9:
            if ctx.ts - entry["ts"] >= self.p["time_stop_seconds"]:
                return [ctx.sell(entry["side"], reason="control exit: time stop")]
            return []
        if ctx.entries_this_window > 0 or ctx.seconds_left < self.p["skip_last_seconds"]:
            return []
        then = None
        for h in ctx.history:
            if ctx.ts - h.ts <= self.p["shock_lookback"]:
                then = h
                break
        if then is None or then.up.mid is None or ctx.up.mid is None:
            return []
        d_mkt = ctx.up.mid - then.up.mid
        if abs(d_mkt) < self.p["shock_size"]:
            return []
        side = seeded_side(self.p["seed"], ctx.snap.market.slug)
        if ctx.book(side).best_ask is None:
            return []
        st["entry"] = {"side": side, "ts": ctx.ts}
        return [ctx.buy(side, usd=self.stake_usd(ctx),
                        reason=f"control: shock {d_mkt:+.3f} in {ctx.ts - then.ts:.0f}s, random side {side}",
                        shock=d_mkt, decision_edge=ctx.edge(side))]
