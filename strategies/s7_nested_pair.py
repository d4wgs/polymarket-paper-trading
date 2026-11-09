"""
S7. NESTED 5m / 15m PAIR

Every 15m round ends at the same moment as one 5m round (the 15m round's last five minutes), and both
settle on the SAME number: the 60-second BRTI average before that close. Only their prices to beat differ
(K15 is set at the 15m open, K5 ten minutes later). So, with S the settlement value:

    K5 >= K15:  buy 15m UP + 5m DOWN     S >= K5        -> 15m UP pays         ($1)
                                         K15 <= S < K5  -> both pay            ($2)
                                         S < K15        -> 5m DOWN pays        ($1)
    K5 <  K15:  buy 15m DOWN + 5m UP     (mirror image; ties settle UP in both, which keeps it >= $1)

The pair pays at least $1 whatever BTC does. If both legs cost less than $1 together (asks + taker fees),
the profit is locked in, plus a free chance at $2 when the close lands between the two prices to beat.

Execution (orders can only go to the market being ticked):
  * Leg 1: on a tick of the `leg1_market` (5m) market, read the partner's latest book (ctx.others). Walk both ask ladders
    together and take every level where (ask_a + fee) + (ask_b + fee) <= max_pair_cost, up to stake_usd for
    the pair. Buy this market's leg for that many contracts, price-capped at the worst level used.
  * Leg 2: on the partner market's tick (the 15m tick right after, in the same engine loop), buy the same
    number of contracts, as long as leg 1's cost + this ask + fee <=
    the opening limit + complete_slack (1.02 for the locked version). Finishing at a 1-3c loss beats holding
    a naked leg. If it can't fill, it retries every tick until min_seconds_left; then leg 1 is held to the
    result like any directional bet (logged as `leg2_abandoned`).
  * Everything is read from the positions, so a restart picks up a half-built pair.
  * One pair per round pair.

band_ev (variant): also open when 1 + P(band) - pair cost >= min_ev, with P(band) = |P15(UP) - P5(UP)| from
the fair-value model (both rounds settle on the same S, so the band's probability is the difference). That
trades on the model, so it is NOT riskless: a pair bought for $1.03 loses 3c whenever the close misses the band.

Checked on 44h of recorded books (2026-11-07 -> 11-09, 175 pairs): the two markets are normally priced
consistently (median best pair cost $1.19); only 4 pairs got to $0.99 or less, 2 of them with real depth.
Expect few trades and small, low-variance profits; near misses are logged as `pair_quote` events.
"""
import math

from core.models import UP, DOWN
from strategies.base import Strategy, TickContext


def _other(side):
    return DOWN if side == UP else UP


def pair_walk(asks_a, asks_b, fee, max_unit_cost, max_usd, step=0.01):
    """
    Walk two ask ladders together, taking matched contracts while one contract of each costs, fees
    included, <= max_unit_cost, and the total stays <= max_usd.
    Returns (contracts, total cost, worst price a, worst price b).
    """
    i = j = 0
    left_a = asks_a[0][1] if asks_a else 0.0
    left_b = asks_b[0][1] if asks_b else 0.0
    n = cost = 0.0
    worst_a = worst_b = None
    while i < len(asks_a) and j < len(asks_b):
        pa, pb = asks_a[i][0], asks_b[j][0]
        unit = pa + fee(pa) + pb + fee(pb)
        if unit > max_unit_cost + 1e-12:
            break
        take = min(left_a, left_b, (max_usd - cost) / unit)
        if step:
            take = math.floor(take / step + 1e-9) * step
        if take > 1e-9:
            n += take
            cost += take * unit
            worst_a, worst_b = pa, pb
        if (max_usd - cost) < unit * (step or 1e-9):
            break
        left_a -= take
        left_b -= take
        if left_a <= 1e-9:
            i += 1
            left_a = asks_a[i][1] if i < len(asks_a) else 0.0
        if left_b <= 1e-9:
            j += 1
            left_b = asks_b[j][1] if j < len(asks_b) else 0.0
    return round(n, 6), cost, worst_a, worst_b


class NestedPair(Strategy):
    PARAMS = {
        "max_pair_cost": 0.99,            # open when both legs cost <= this per contract (fees in): >= 1c locked
        "complete_slack": 0.03,           # leg 2 is still bought up to (opening limit + this) per pair: finishing
                                          # at a small loss beats holding a naked leg
        "leg1_market": "5m",              # open pairs only on this market's tick (null = either). The engine ticks
                                          # 5m then 15m, so leg 2 follows within the same loop, under a second later
        "band_ev": False,                 # also open on model EV (see above): 1 + P(band) - cost >= min_ev
        "min_ev": 0.02,
        "max_ev_pair_cost": 1.05,         # band_ev never pays more than this per pair
        "max_other_age_seconds": 3,       # the partner's book must be this fresh
        "min_seconds_left": 5,            # no new legs this close to the end
        "min_pair_shares": 5,             # smaller pairs aren't worth the legging risk
        "stake_usd": 50.0,                # dollars for the WHOLE pair (both legs); risk limits apply per leg
        "stake_fraction": None,
        "near_miss": 0.02,                # log a pair_quote event when cost <= max_pair_cost + this
    }

    # ------------------------------------------------------------------ pairing
    @staticmethod
    def partner(ctx: TickContext):
        """The other round that settles at the same moment (5m <-> 15m), or None."""
        me = ctx.snap.market
        sym = me.asset.split("-")[0]
        my_len = me.window_end - me.window_start
        for asset, om in ctx.others.items():
            m = om.snap.market
            if (asset.split("-")[0] == sym and abs(m.window_end - me.window_end) < 1
                    and abs((m.window_end - m.window_start) - my_len) > 1):
                return om
        return None

    @staticmethod
    def sides(ctx: TickContext, om):
        """(side here, side in the partner) or None until both prices to beat are published."""
        k_me, k_ot = ctx.price_to_beat, getattr(om.snap.market, "price_to_beat", None)
        if not k_me or not k_ot:
            return None
        me_is_long = ctx.window_seconds > (om.snap.market.window_end - om.snap.market.window_start)
        k15, k5 = (k_me, k_ot) if me_is_long else (k_ot, k_me)
        side15 = UP if k5 >= k15 else DOWN
        side5 = _other(side15)
        return (side15, side5) if me_is_long else (side5, side15)

    def p_band(self, ctx: TickContext, om, mine: str, theirs: str):
        """P(both legs pay) = P(mine) + P(theirs) - 1 under the model (both settle on the same value)."""
        fv_o = getattr(om.snap, "fv", None)
        if ctx.fv is None or fv_o is None:
            return None
        return max(0.0, ctx.fv.of(mine) + fv_o.of(theirs) - 1.0)

    def open_limit(self) -> float:
        """The most a pair may cost when it is opened (per contract, fees included)."""
        if self.p["band_ev"]:
            return max(self.p["max_pair_cost"], self.p["max_ev_pair_cost"])
        return self.p["max_pair_cost"]

    # ------------------------------------------------------------------ main
    def on_tick(self, ctx: TickContext):
        om = self.partner(ctx)
        if om is None:
            return []
        sd = self.sides(ctx, om)
        if sd is None:
            return []
        mine, theirs = sd
        held_me = ctx.position.shares(mine)
        held_ot = om.position.shares(theirs)
        step = getattr(ctx.snap.market, "qty_step", 0.01) or 0.01
        late = ctx.seconds_left < self.p["min_seconds_left"]
        st = ctx.state

        # ---- leg 2: the partner already holds more than this market does -> catch up
        need = math.floor((held_ot - held_me) / step + 1e-9) * step
        if need >= step:
            if late:
                if not st.get("abandoned"):
                    st["abandoned"] = True
                    ctx.event("leg2_abandoned", need=need, side=mine, ask=ctx.book(mine).best_ask)
                return []
            leg1 = om.position.avg_cost(theirs)
            if leg1 is None:
                return []
            cap = ctx.max_price_for_edge(mine, 0.0, fair=self.open_limit() + self.p["complete_slack"] - leg1)
            ask = ctx.book(mine).best_ask
            if cap is None or ask is None or ask > cap:
                if not st.get("leg2_wait_logged"):
                    st["leg2_wait_logged"] = True
                    ctx.event("leg2_waiting", need=need, side=mine, ask=ask, cap=cap, leg1_cost=leg1)
                return []
            return [ctx.buy(mine, shares=need, max_price=cap, hedge=True,   # completes leg 1: never risk-blocked
                            reason=f"pair leg 2: {mine} here vs {theirs} in {om.snap.market.asset} "
                                   f"(leg 1 {leg1:.3f}, cap {cap:.3f})",
                            pair_leg=2, partner=om.snap.market.slug, leg1_cost=leg1)]
        if held_me - held_ot >= step:
            return []                         # leg 1 is here: leg 2 goes on the partner's tick

        # ---- leg 1: one pair per round pair
        if late or ctx.kill_switch:
            return []
        if self.p["leg1_market"] and ctx.kind != self.p["leg1_market"]:
            return []
        pos_o = om.position
        if (ctx.position.bought_up + ctx.position.bought_down + pos_o.bought_up + pos_o.bought_down) > 1e-9:
            return []
        if ctx.ts - om.snap.ts > self.p["max_other_age_seconds"]:
            return []
        asks_me = ctx.book(mine).asks
        asks_ot = (om.snap.up if theirs == UP else om.snap.down).asks
        if not asks_me or not asks_ot:
            return []
        top = asks_me[0][0] + ctx.fee_per_share(asks_me[0][0]) + asks_ot[0][0] + ctx.fee_per_share(asks_ot[0][0])
        limit = self.p["max_pair_cost"]
        band = None
        if self.p["band_ev"]:
            band = self.p_band(ctx, om, mine, theirs)
            if band is not None:
                limit = max(limit, min(self.p["max_ev_pair_cost"], 1.0 + band - self.p["min_ev"]))
        if top <= self.p["max_pair_cost"] + self.p["near_miss"] and ctx.ts - st.get("quote_logged", 0) >= 30:
            st["quote_logged"] = ctx.ts
            ctx.event("pair_quote", pair_cost=round(top, 4), side=mine, partner_side=theirs,
                      ask=asks_me[0][0], partner_ask=asks_ot[0][0], p_band=band,
                      seconds_left=round(ctx.seconds_left, 1))
        if top > limit:
            return []
        n, cost, worst_me, _ = pair_walk(asks_me, asks_ot, ctx.fee_per_share, limit,
                                         self.stake_usd(ctx), step)
        if n < max(self.p["min_pair_shares"], ctx.min_shares) or worst_me is None:
            return []
        return [ctx.buy(mine, shares=n, max_price=worst_me,
                        reason=f"pair leg 1: {mine} here + {theirs} in {om.snap.market.asset} for "
                               f"{cost / n:.3f}/pair (K here {ctx.price_to_beat:,.2f})",
                        pair_leg=1, partner=om.snap.market.slug, pair_cost=cost / n, p_band=band)]
