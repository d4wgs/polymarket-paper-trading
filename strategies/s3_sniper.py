"""
S3. END-OF-WINDOW SNIPER (v2)

In the last `snipe_seconds` (60s) of every round, 5m and 15m alike, buy the side that is winning right
now and hold it to the result. No other filter: no z-score, no price cap, no edge, no feed check.

"Winning" = the BTC index vs the price to beat:
    index >= price to beat -> Up    (ties settle Up)
    index <  price to beat -> Down
The index is the 60-second average of BRTI that Polymarket US serves, the same quantity the round
settles on (its value at the close IS the settlement), so in the last minute it's the best read of the
result. If the index is missing it falls back to the model's S and K (spot vs the price to beat).

One entry per round. If the winning side has no ask (nobody selling), it retries every tick.
Breakeven hit rate = entry price + fee: buying at 97c needs a >97% hit rate, and one loss wipes out
~30 wins -- watch "Win %" next to "Avg price" on the dashboard.
"""
from core.models import UP, DOWN
from strategies.base import Strategy, TickContext


class Sniper(Strategy):
    PARAMS = {
        "snipe_seconds": 60,            # buy when this many seconds (or fewer) are left
        "min_seconds_left": 1,          # don't send an order this close to the end
        "stake_usd": 25.0,
        "stake_fraction": None,
    }

    @staticmethod
    def winning_side(ctx: TickContext):
        """(side, price now, price to beat, source) or None if it can't be told."""
        k = ctx.price_to_beat
        idx = ctx.index_price
        if k and idx:
            return (UP if idx >= k else DOWN), idx, k, "index"
        fv = ctx.fv
        if fv is not None and fv.S and fv.K:
            k_used = fv.k_model or fv.K
            return (UP if fv.S >= k_used else DOWN), fv.S, k_used, fv.s_source
        return None

    def on_tick(self, ctx: TickContext):
        if ctx.entries_this_window > 0:
            return []
        if not (self.p["min_seconds_left"] <= ctx.seconds_left <= self.p["snipe_seconds"]):
            return []
        w = self.winning_side(ctx)
        if w is None:
            return []
        side, now_px, k, src = w
        ask = ctx.book(side).best_ask
        if ask is None:
            return []
        return [ctx.buy(side, usd=self.stake_usd(ctx),
                        reason=f"last {ctx.seconds_left:.0f}s: {side} winning ({src} {now_px:,.2f} vs "
                               f"price to beat {k:,.2f}), ask {ask:.3f}")]
