"""
ARBITRAGE (hedge): buy the underdog early, then buy the other side to lock the pair.

Step 1: 15 seconds into the window, buy the side priced BELOW 50% (the underdog).
Step 2: buy the OPPOSITE side, the same number of shares:
          * as soon as the pair would cost <= `lock_in_pair_cost` (fees included) -- a real lock, or
          * in the last 60 seconds, whatever it costs (to stop the position riding to $0).

Holding the same number of Up and Down shares pays exactly $1 per pair no matter
who wins. So the trade makes money when
    (price paid for underdog) + (price paid for opposite side) + fees  <  $1.00
and loses a small guaranteed amount when it's more.

On Polymarket US a market is ONE instrument (YES = Up), so you can't own both sides: "buying the
opposite side" SELLS the shares you hold (the broker records it as a sale). The economics are the
same -- the trade locks in (sale price - price paid - fees) -- but the money comes back at once.
So on Polymarket US this is really "buy the underdog, then close it": at a profit as soon as that
profit is locked in, or at whatever it fetches in the last 60 seconds.

When a round is already decided, nobody sells the winning side (there is no ask at all).
The opposite side can't be bought then, so instead the unmatched underdog shares are sold
for whatever bid is left. If there's no bid either, nothing can be done and the position
rides to the result.

The state is read from the position itself, so it still works after a restart.
"""
from strategies.base import Strategy, TickContext

EPS = 0.01   # share differences smaller than this count as "matched"


class Arbitrage(Strategy):
    PARAMS = {
        "stake_fraction": 0.10,       # each new trade = 10% of the current portfolio value
        "enter_after_seconds": 15,    # leg 1: wait this long after the window opens
        "enter_before_seconds": 45,   # leg 1: don't open later than this (e.g. after a restart mid-round)
        "close_seconds_left": 60,     # leg 2: buy the opposite side at any price once this many seconds remain
        "lock_in_pair_cost": 0.98,    # leg 2 early: buy as soon as a pair costs <= this, fees included (0 = off)
    }

    def on_tick(self, ctx: TickContext):
        pos = ctx.position
        if pos.up > 1e-9 or pos.down > 1e-9:
            return self._close(ctx)

        # ---- leg 1 ----
        if ctx.traded_this_window:
            return []                                   # already traded this window (a FILL happened)
        if not (self.p["enter_after_seconds"] <= ctx.seconds_elapsed <= self.p["enter_before_seconds"]):
            return []
        if ctx.seconds_left <= self.p["close_seconds_left"]:
            return []                                   # too late to open a hedge
        for side in ("UP", "DOWN"):
            book = ctx.book(side)
            if book.mid is not None and book.mid < 0.5 and book.best_ask is not None:
                return [ctx.buy(side, usd=ctx.stake(self.p["stake_fraction"]),
                                reason=f"underdog {side} at {book.mid:.2f}, {ctx.seconds_elapsed:.0f}s in")]
        return []

    def _close(self, ctx: TickContext):
        """Leg 2: match the shares held on one side with the same number on the other."""
        pos = ctx.position
        held = "UP" if pos.up >= pos.down else "DOWN"
        other = "DOWN" if held == "UP" else "UP"
        need = pos.shares(held) - pos.shares(other)
        if need < EPS:
            return []                                   # matched: wait for the $1-per-pair payout

        late = ctx.seconds_left <= self.p["close_seconds_left"]
        if need < ctx.min_shares:
            return []                                   # below the exchange's minimum size; rides to the result
        ask = ctx.book(other).best_ask
        if ask is not None:
            if ask * need < ctx.min_notional:
                return []                               # leftover too small to trade; rides to the result
            fee = ctx.fee_per_share(ask)          # this account's taker fee (US: by volume tier)
            held_cost = pos.avg_cost(held)
            pair_cost = None if held_cost is None else held_cost + ask + fee
            lock = (self.p["lock_in_pair_cost"] > 0 and pair_cost is not None
                    and pair_cost <= self.p["lock_in_pair_cost"])
            if not (lock or late):
                return []
            why = (f"lock in: pair costs {pair_cost:.3f}" if lock
                   else f"close: buy {other} to match {need:.1f} {held}, {ctx.seconds_left:.0f}s left")
            return [ctx.buy(other, shares=need, reason=why)]

        # Nobody is selling `other` -> the round is (nearly) decided for `other`. Get out of the
        # unmatched `held` shares at whatever bid remains instead.
        if not late:
            return []
        bid = ctx.book(held).best_bid
        if bid is None or bid * need < ctx.min_notional:
            return []                                   # no buyers either: rides to the result
        return [ctx.sell(held, shares=need,
                         reason=f"no sellers of {other}: sell {need:.1f} unmatched {held} at {bid:.3f}")]
