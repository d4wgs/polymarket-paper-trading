"""
S5. COMPLETE SET, LEGGED IN (v2)

  1. Leg 1: `entry_after_seconds` (15s) into the round, buy whichever side's ask is higher (the
     favorite; a tie goes to Up). One entry per round.
  2. Leg 2: buy the exact opposite side, the same number of contracts, on the same market. Once both
     legs are held, one of them pays $1 at the end whatever happens, so the result is locked in:
         set cost per contract = leg 1 cost + leg 2 cost   (fees included)
         profit                = $1 - set cost             (> 0 when the two legs sum to under $1)
     Leg 2 is bought on a take profit (+`take_profit_pct`, 10%), a stop loss (-`stop_loss_pct`, 5%), or
     whatever the result is once `exit_seconds_left` (30s) remain (time exit). `pnl_basis` sets what the
     percentages measure:
       "price"  (default) leg 1's price move: the price leg 2 implies for leg 1 (1 - leg 2 price) vs
                what was paid for leg 1, fees left out -- the usual meaning of a TP / SL.
       "locked" the whole set, fees and spread included: ($1 - set cost) / leg 1 cost. On real
                Polymarket US books this starts at about -7.5% the moment leg 1 fills (spread + two
                taker fees), so a 5% stop fires on the first tick in ~9 rounds out of 10 (checked on
                30 live rounds, 2026-11-07).
     If leg 2 can't fill (thin book, price moved), it is retried every tick; if the round ends first,
     leg 1 is held to the result.

On Polymarket US, Up and Down are ONE contract (Down = the "No" side of Up), so buying the opposite
leg is executed as selling leg 1 at the Up bid -- the same money: buying Down at 1 - bid is the same as
selling Up at bid. The trade log records it as a SELL of leg 1 "(buying X closes the Y position)".

Fees: each leg pays the taker fee (0.095 x p x (1-p) per contract at the base tier), so a round trip
at 50c costs ~4.75c per contract: about 9.5% of a 50c leg 1 before the market moves at all. That is
already close to the 5% stop. And a leg 1 bought above ~91c can never reach +10% before the round
ends (it can pay at most $1), so those positions end at the time exit.
"""
from core.models import UP, DOWN
from strategies.base import MAX_PRICE, Strategy, TickContext


class CompleteSetArb(Strategy):
    PARAMS = {
        "entry_after_seconds": 15,      # leg 1 this far into the round
        "take_profit_pct": 10.0,        # buy leg 2 when the locked profit is >= this % of leg 1's cost
        "stop_loss_pct": 5.0,           # ... or the locked loss is >= this %
        "exit_seconds_left": 30,        # ... or this few seconds are left (time exit)
        "pnl_basis": "price",           # price = leg 1 price move | locked = set result incl. fees + spread
        "stake_usd": 50.0,              # dollars for leg 1 (risk.max_usd_per_trade caps it too)
        "stake_fraction": None,
    }

    @staticmethod
    def _other(side):
        return DOWN if side == UP else UP

    @staticmethod
    def entry_price(ctx: TickContext, all_in: float) -> float:
        """Leg 1's price without its fee: solves all_in = e + theta * e * (1 - e) for e."""
        th = ctx.fee_theta or 0.0
        if th <= 0:
            return all_in
        b = 1 + th
        return (b - (b * b - 4 * th * all_in) ** 0.5) / (2 * th)

    def locked_result(self, ctx: TickContext, held: str, shares: float):
        """
        Buying `shares` of the opposite side right now, walking its real asks:
        (P&L % per pnl_basis, set cost per contract, leg 2 avg price) -- None if the book can't fill it.
        """
        c1 = ctx.position.avg_cost(held)                 # leg 1 cost per contract, fees included
        got, avg2, fee2 = ctx.walk_cost(self._other(held), shares)
        if not c1 or avg2 is None or got < shares - 1e-6:
            return None
        set_cost = c1 + avg2 + fee2 / shares
        if self.p["pnl_basis"] == "locked":
            pct = (1.0 - set_cost) / c1 * 100.0
        else:
            e = self.entry_price(ctx, c1)
            pct = ((1.0 - avg2) - e) / e * 100.0          # leg 1's price implied by leg 2, vs entry
        return pct, set_cost, avg2

    def tp_limit(self, ctx: TickContext, held: str):
        """Highest leg 2 price that still keeps the take-profit (so latency can't turn it into less)."""
        c1 = ctx.position.avg_cost(held)
        tp = self.p["take_profit_pct"] / 100.0
        if self.p["pnl_basis"] != "locked":
            lim = 1.0 - self.entry_price(ctx, c1) * (1 + tp)
            return round(int(lim * 1000 + 1e-9) / 1000, 3) if lim >= 0.001 else None
        target = 1.0 - c1 * (1 + tp)
        best, p = None, 0.001
        while p <= MAX_PRICE + 1e-9:
            if p + ctx.fee_per_share(p) <= target + 1e-12:
                best = p
            p = round(p + 0.001, 3)
        return best

    def on_tick(self, ctx: TickContext):
        if ctx.position.up > 1e-9 and ctx.position.down > 1e-9:
            return []                    # two-book exchange (polymarket.com): the set is complete, hold it
        held = UP if ctx.position.up > 1e-9 else (DOWN if ctx.position.down > 1e-9 else None)

        # ---- leg 2: close the set
        if held is not None:
            shares = ctx.position.shares(held)
            other = self._other(held)
            res = self.locked_result(ctx, held, shares)
            time_exit = ctx.seconds_left <= self.p["exit_seconds_left"]
            if res is None:
                if time_exit and ctx.book(other).best_ask is not None:
                    return [ctx.buy(other, shares=shares, reason="leg 2 (time exit, thin book)", exit="time")]
                return []
            pct, set_cost, avg2 = res
            why = (f"leg 2 {other} at {avg2:.3f}: {pct:+.1f}% ({self.p['pnl_basis']}), "
                   f"set cost {set_cost:.4f} incl. fees")
            if pct >= self.p["take_profit_pct"]:
                limit = self.tp_limit(ctx, held)
                if limit is not None:
                    return [ctx.buy(other, shares=shares, max_price=limit, reason=f"leg 2 take profit: {why}",
                                    exit="take_profit", locked_pct=round(pct, 2))]
            if pct <= -self.p["stop_loss_pct"]:
                return [ctx.buy(other, shares=shares, reason=f"leg 2 stop loss: {why}",
                                exit="stop_loss", locked_pct=round(pct, 2))]
            if time_exit:
                return [ctx.buy(other, shares=shares, reason=f"leg 2 time exit ({ctx.seconds_left:.0f}s left): {why}",
                                exit="time", locked_pct=round(pct, 2))]
            return []

        # ---- leg 1: the more expensive side, once per round
        if ctx.entries_this_window > 0:
            return []
        if ctx.seconds_elapsed < self.p["entry_after_seconds"] or ctx.seconds_left <= self.p["exit_seconds_left"]:
            return []
        a_up, a_dn = ctx.up.best_ask, ctx.down.best_ask
        if a_up is None or a_dn is None:
            return []
        side = UP if a_up >= a_dn else DOWN
        ask = a_up if side == UP else a_dn
        return [ctx.buy(side, usd=self.stake_usd(ctx),
                        reason=f"leg 1: {side} is the pricier side ({ask:.3f} vs {min(a_up, a_dn):.3f})")]
