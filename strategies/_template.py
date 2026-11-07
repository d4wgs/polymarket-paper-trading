"""
COPY THIS FILE to start a new strategy.

  1. Copy to strategies/my_idea.py and rename the class.
  2. Put every number you might want to tune in PARAMS.
  3. Write your rules in on_tick(). Return a list of orders, or [].
  4. Register the class in REGISTRY in config.py (one line), then add a block for it under
     `strategies:` in config.yaml (enabled, name, markets, params). All numbers live in config.yaml.

  The model is available too: ctx.fv (fair value), ctx.edge(side), ctx.max_price_for_edge(side, e),
  ctx.spot / ctx.oracle / ctx.basis, ctx.md.spot_at(ts). See strategies/base.py for the full list.

Things you can read inside on_tick(ctx):
  ctx.asset                 "BTC-15m" or "BTC-5m" (the label from config.MARKETS)
  ctx.seconds_left          seconds until this window closes (max 900 on 15m, 300 on 5m)
  ctx.seconds_elapsed       seconds since it opened
  ctx.up.best_ask           cheapest price to BUY "Up" right now (0-1)
  ctx.up.best_bid           best price to SELL "Up" right now
  ctx.up.mid, ctx.up.spread
  ctx.up.asks / ctx.up.bids full book: list of (price, shares)
  ctx.up.ask_depth_usd(0.60)  dollars available to buy at <= 60c
  ctx.down.*                same for "Down"
  ctx.position.up / .down   shares you already own this window
  ctx.cash                  free cash
  ctx.equity                portfolio value (cash + open bets at the mid)
  ctx.price_to_beat         the round's open value (Polymarket US), ctx.index_price = BRTI now
  ctx.fee_per_share(p)      this account's taker fee for one contract at price p
  ctx.history               earlier snapshots this window (h.up.mid, h.ts, h.seconds_left)
  ctx.prev_windows          winners of the rounds right before this one, newest last, e.g. ["UP","UP","DOWN"]
                            (an unbroken run: it stops at the first round whose result is unknown)
  ctx.state                 a dict you can write to; wiped at each new window AND on a bot restart
  ctx.traded_this_window    True if you already bought/sold this window (survives restarts --
                            check it so a restart doesn't make you trade the same window twice)
  self.p["my_param"]        your parameters

Orders you can return:
  ctx.buy("UP", usd=ctx.stake(0.10))       spend 10% of portfolio value on Up (won't pay > ask+2c)
  ctx.buy("UP", usd=20)                     spend $20 on Up
  ctx.buy("DOWN", usd=20, max_price=0.40)   only fill at <= 40c
  ctx.sell("UP")                            sell all your Up shares
  ctx.sell("UP", shares=10, min_price=0.7)  partial sell with a floor
"""
from strategies.base import Strategy, TickContext


class MyStrategy(Strategy):
    PARAMS = {
        "stake_fraction": 0.10,       # each new trade = 10% of the current portfolio value
        "entry_seconds_left": 300,   # only consider entering in the last 5 minutes
    }

    def on_tick(self, ctx: TickContext):
        # One entry per window. traded_this_window turns True once an order FILLS (and survives a
        # restart). Don't set your own "entered" flag before the fill: if the order is rejected
        # (price moved past the limit, under $1), you'd skip the round instead of retrying.
        if ctx.traded_this_window:
            return []
        if ctx.seconds_left > self.p["entry_seconds_left"]:
            return []

        # ---- your signal goes here ----
        signal = None          # set to "UP" or "DOWN" when you want to trade
        # --------------------------------

        if signal is None:
            return []
        return [ctx.buy(signal, usd=ctx.stake(self.p["stake_fraction"]), reason="template signal")]
