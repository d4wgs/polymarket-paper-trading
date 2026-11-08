"""
Accounting checks for the dashboard numbers: finished trades (closed_rounds), realized P&L, and the
portfolio value of strategies the bot no longer records (ledger_value).

    python -m unittest discover -s tests -v
"""
import os
import sys
import unittest

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import metrics

FCOLS = ["ts", "strategy", "asset", "slug", "window_end", "outcome", "side", "shares", "avg_price", "usd", "fee",
         "rebate", "liquidity", "theta"]


def _fill(ts, strat, slug, outcome, side, shares, price, fee):
    return [ts, strat, "BTC-5m", slug, 1300, outcome, side, shares, price, shares * price, fee, 0.0, "taker", 0.095]


def _data(fills, setts, equity, snaps=None):
    d = {"strategies": pd.DataFrame({"name": ["a", "b"], "bankroll": [1000.0, 1000.0]}),
         "fills": pd.DataFrame(fills, columns=FCOLS),
         "settlements": pd.DataFrame(setts, columns=["ts", "strategy", "asset", "slug", "winner", "up_shares",
                                                     "down_shares", "net_cost", "payout", "pnl"]),
         "equity": pd.DataFrame(equity, columns=["ts", "strategy", "cash", "open_value", "equity"]),
         "snapshots": pd.DataFrame(snaps or [], columns=["ts", "slug", "up_bid", "up_ask", "down_bid", "down_ask",
                                                         "seconds_left"]),
         "status": pd.DataFrame(columns=["name"]), "exchange": "polymarket_us", "max_dd": None}
    for t in ("fills", "settlements", "equity", "snapshots"):
        d[t]["time"] = metrics._to_time(d[t]["ts"])
    return d


class ClosedRoundsTests(unittest.TestCase):
    def test_early_exit_counts_before_the_round_settles(self):
        fills = [_fill(10, "a", "r1", "UP", "BUY", 100, 0.50, 2.38),
                 _fill(20, "a", "r1", "UP", "SELL", 100, 0.60, 2.28),    # fully exited, round not settled
                 _fill(30, "a", "r2", "UP", "BUY", 50, 0.40, 1.14)]      # still open
        c = metrics.closed_rounds(pd.DataFrame(fills, columns=FCOLS), pd.DataFrame(
            columns=["ts", "strategy", "asset", "slug", "pnl"]))
        self.assertEqual(list(c["slug"]), ["r1"])
        self.assertAlmostEqual(c["pnl"].iloc[0], 60 - 2.28 - (50 + 2.38))
        self.assertFalse(c["settled"].iloc[0])

    def test_settled_round_is_not_counted_twice(self):
        fills = [_fill(10, "a", "r1", "UP", "BUY", 100, 0.50, 2.38), _fill(20, "a", "r1", "UP", "SELL", 100, 0.60, 2.28)]
        sett = [[400, "a", "BTC-5m", "r1", "DOWN", 0, 0, -5.34, 0.0, 5.34]]
        d = _data(fills, sett, [])
        c = metrics.closed_rounds(d["fills"], d["settlements"])
        self.assertEqual(len(c), 1)
        self.assertTrue(c["settled"].iloc[0])
        # the early-exit P&L equals what the settlement later records
        early = metrics.closed_rounds(d["fills"], d["settlements"].iloc[0:0])
        self.assertAlmostEqual(early["pnl"].iloc[0], 5.34)


class LeaderboardValueTests(unittest.TestCase):
    def test_retired_strategy_value_is_rebuilt_from_trades(self):
        # "b" stopped being recorded at t=100 while holding a bet worth $30 at the mid; it then lost.
        fills = [_fill(10, "b", "r1", "UP", "BUY", 50, 0.60, 1.14)]
        sett = [[400, "b", "BTC-5m", "r1", "DOWN", 50, 0, 31.14, 0.0, -31.14]]
        eq = [[100, "b", 968.86, 30.0, 998.86], [1000, "a", 1000.0, 0.0, 1000.0]]
        lb = metrics.leaderboard(_data(fills, sett, eq)).set_index("Strategy")
        self.assertAlmostEqual(lb.loc["b", "Equity $"], 1000 - 31.14)
        self.assertAlmostEqual(lb.loc["b", "Realized P&L $"], -31.14)
        self.assertEqual(lb.loc["a", "Equity $"], 1000.0)         # live strategy: the bot's own row

    def test_realized_includes_early_exit(self):
        fills = [_fill(10, "a", "r1", "UP", "BUY", 100, 0.50, 2.38), _fill(20, "a", "r1", "UP", "SELL", 100, 0.60, 2.28)]
        eq = [[30, "a", 1005.34, 0.0, 1005.34], [30, "b", 1000.0, 0.0, 1000.0]]
        lb = metrics.leaderboard(_data(fills, [], eq)).set_index("Strategy")
        self.assertAlmostEqual(lb.loc["a", "Realized P&L $"], 5.34)
        self.assertEqual(lb.loc["a", "Windows"], 1)
        tl = metrics.realized_timeline(_data(fills, [], eq))
        self.assertAlmostEqual(tl["cum_pnl"].iloc[-1], 5.34)

    def test_ledger_value_uses_mid_for_open_bets(self):
        fills = [_fill(10, "b", "r1", "UP", "BUY", 50, 0.60, 1.14)]
        snaps = [[90, "r1", 0.70, 0.72, 0.28, 0.30, 200]]
        d = _data(fills, [], [[100, "a", 1000.0, 0.0, 1000.0]], snaps)
        v = metrics.ledger_value("b", 1000.0, d["fills"], d["settlements"], metrics.open_positions_marked(d))
        self.assertAlmostEqual(v, 1000 - 31.14 + 50 * 0.71)


if __name__ == "__main__":
    unittest.main()
