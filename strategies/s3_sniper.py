"""
S3. END-OF-WINDOW SNIPER (v2 and the v3 variants)

In the last `snipe_seconds` (60s) of every round, 5m and 15m alike, buy the side that is winning right
now and hold it to the result. One entry per round, no take profit / stop loss.

"Winning" is decided by `signal`:
    index (v2, default): the BTC index vs the price to beat (index >= price to beat -> Up; ties settle Up).
        The served index is a trailing 60-second average of BRTI, so at 60s left it covers the minute
        BEFORE the one the round settles on. It lags: when it disagrees with the market, the market (which
        trades on live spot) is usually right. If the index is missing it falls back to spot vs K.
    model (v3d): the fair-value model's P(Up) >= 0.5. The model uses live spot and already accounts for
        the settlement averaging (the part of the final minute that has elapsed is known), so it does not lag.
        Falls back to the index if the model has no value.

Optional gates on the ASK of the side we would buy (v2 has none, which is how it ended up paying 2-40c
for "winning" sides the market disagreed with, and losing $306 on 39 such trades):
    min_price: skip unless ask >= this. 0.50 = "the market agrees this side is ahead" (v3a),
               0.70 = "the market is confident" (v3b).
    max_price: skip if ask > this. Above ~0.97 a win pays under 3c and one loss erases ~30-60 wins (v3c).
A skipped tick is retried on the next tick (price can move into range), until `min_seconds_left`.

Breakeven hit rate = entry price + fee: buying at 97c needs a >97% hit rate -- watch "Win %" next to
"Avg price" on the dashboard.
"""
from core.models import UP, DOWN
from strategies.base import Strategy, TickContext


class Sniper(Strategy):
    PARAMS = {
        "snipe_seconds": 60,            # buy when this many seconds (or fewer) are left
        "min_seconds_left": 1,          # don't send an order this close to the end
        "signal": "index",              # index | model  (how "winning" is decided)
        "min_price": None,              # ask floor (None = no floor)
        "max_price": None,              # ask cap (None = no cap)
        "stake_usd": 25.0,
        "stake_fraction": None,
    }

    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        if self.p["signal"] not in ("index", "model"):
            raise ValueError(f"Sniper signal must be 'index' or 'model', got {self.p['signal']!r}")

    @staticmethod
    def winning_side(ctx: TickContext, signal: str = "index"):
        """(side, price now, price to beat, source) or None if it can't be told."""
        fv = ctx.fv
        if signal == "model" and fv is not None and fv.p_up is not None and fv.S and fv.K:
            k_used = fv.k_model or fv.K
            return (UP if fv.p_up >= 0.5 else DOWN), fv.S, k_used, f"model {fv.s_source}"
        k = ctx.price_to_beat
        idx = ctx.index_price
        if k and idx:
            return (UP if idx >= k else DOWN), idx, k, "index"
        if fv is not None and fv.S and fv.K:
            k_used = fv.k_model or fv.K
            return (UP if fv.S >= k_used else DOWN), fv.S, k_used, fv.s_source
        return None

    def on_tick(self, ctx: TickContext):
        if ctx.entries_this_window > 0:
            return []
        if not (self.p["min_seconds_left"] <= ctx.seconds_left <= self.p["snipe_seconds"]):
            return []
        w = self.winning_side(ctx, self.p["signal"])
        if w is None:
            return []
        side, now_px, k, src = w
        ask = ctx.book(side).best_ask
        if ask is None:
            return []
        lo, hi = self.p["min_price"], self.p["max_price"]
        if lo is not None and ask < lo:
            return []
        if hi is not None and ask > hi:
            return []
        limit = min(hi, ask + 0.02) if hi is not None else None    # never fill above the cap
        return [ctx.buy(side, usd=self.stake_usd(ctx), max_price=limit,
                        reason=f"last {ctx.seconds_left:.0f}s: {side} winning ({src} {now_px:,.2f} vs "
                               f"price to beat {k:,.2f}), ask {ask:.3f}")]
