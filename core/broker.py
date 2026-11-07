"""
The paper broker: a fake brokerage account, one per strategy.

It fills orders against the REAL order book you just downloaded, walking
level by level like a real market order would. That matters: if you want
$200 of UP and the best ask only has $40 sitting at 0.62, the rest fills
at 0.63, 0.64... A naive simulator that fills everything at the mid price
makes almost every strategy look profitable. This one doesn't.

Fees come from the exchange's fee model (core/fees.py):
  Polymarket US:  Θ × contracts × p × (1-p), Θ by your volume tier, rounded to the cent (half-even)
  polymarket.com: 0.07 × contracts × p × (1-p), rounded to 5 decimals

Polymarket US has ONE instrument per market (YES = Up). "Buying DOWN" is selling YES, so buying
DOWN while you hold UP doesn't give you both sides -- it SELLS your UP (and only the excess, if
any, becomes a DOWN position). The broker records it exactly that way.
"""
from __future__ import annotations

import logging
import math
from typing import Optional

from core.fees import IntlFees
from core.models import BUY, SELL, UP, DOWN, Book, Fill, Order, Position, Settlement, Snapshot

log = logging.getLogger("broker")
# polymarket.com rejects marketable orders under $1 (sells too), so a leftover worth less than $1
# is held to the result. Each market carries its own minimum in MarketInfo.min_notional; this is
# the polymarket.com value, kept for older code.
MIN_NOTIONAL = 1.00


def taker_fee(shares: float, price: float, fee_rate: float, exponent: float = 1.0) -> float:
    """polymarket.com fee for one fill (5-decimal rounding)."""
    return round(shares * fee_rate * (price * (1 - price)) ** exponent, 5)


def _other(outcome: str) -> str:
    return DOWN if outcome == UP else UP


def _mirror(levels):
    """UP bids at p are DOWN asks at 1-p (and vice versa) on a one-instrument market."""
    return [(round(1 - p, 6), s) for p, s in levels]


class PaperBroker:
    def __init__(self, strategy_name: str, bankroll: float, extra_slippage: float = 0.0, fee_model=None):
        self.name = strategy_name
        self.bankroll = bankroll
        self.cash = bankroll
        self.extra_slippage = extra_slippage
        self.fees = fee_model or IntlFees()
        self.positions: dict[str, Position] = {}   # slug -> Position
        # Slugs (rounds) this account has traded in and that haven't settled yet. Rebuilt from the
        # database on restart, so strategies can tell "I already traded this round" after a restart.
        self.traded_slugs: set[str] = set()
        self.entries: dict[str, int] = {}          # slug -> number of BUY fills that opened risk (this round)

    # ------------------------------------------------------------ helpers
    def position(self, snap: Snapshot) -> Position:
        m = snap.market
        if m.slug not in self.positions:
            self.positions[m.slug] = Position(m.asset, m.slug, m.window_end)
        return self.positions[m.slug]

    def current_theta(self, ts: float, market) -> float:
        return self.fees.theta(ts, market)

    def _walk(self, levels, side, limit, theta, market, max_shares=None, max_usd=None):
        """
        Simulate eating through the book. Returns (filled, left) where filled is [(shares, price)]
        per level. Budgets (max_usd) cover price + fee. Contract quantities are floored to the
        market's step (US: 0.01 contracts).
        """
        step = getattr(market, "qty_step", 0.0) or 0.0
        exp = getattr(market, "fee_exponent", 1.0)
        filled, shares, spent = [], 0.0, 0.0
        remaining = [list(l) for l in levels]
        for lvl in remaining:
            price, size = lvl
            if side == BUY and limit is not None and price > limit + 1e-9:
                break
            if side == SELL and limit is not None and price < limit - 1e-9:
                break
            fill_price = min(0.999, price + self.extra_slippage) if side == BUY else max(0.001, price - self.extra_slippage)
            take = size
            if max_shares is not None:
                take = min(take, max_shares - shares)
            if max_usd is not None:
                per_share = fill_price + theta * (fill_price * (1 - fill_price)) ** exp
                take = min(take, (max_usd - spent) / per_share)
            if step:
                take = math.floor(take / step + 1e-9) * step
            if take <= 1e-9:
                break
            filled.append((take, fill_price))
            shares += take
            spent += take * fill_price + theta * take * (fill_price * (1 - fill_price)) ** exp
            lvl[1] -= take
        left = [(p, s) for p, s in remaining if s > 1e-9]
        return filled, left

    def _fee(self, filled, side, theta, market) -> float:
        # Fees are charged on the instrument's trade price; p*(1-p) is the same for UP at p and
        # DOWN at 1-p, so the fee doesn't depend on which side of the book the order is expressed on.
        return self.fees.fee(filled, theta, market)

    @staticmethod
    def _trim_to_budget(filled, budget, fee_fn, step):
        """Fee rounding can push a budget-sized order a fraction of a cent over; give back contracts."""
        for _ in range(50):
            notional = sum(c * p for c, p in filled)
            excess = notional + fee_fn(filled) - budget
            if excess <= 1e-9 or not filled:
                return filled
            c, p = filled[-1]
            cut = excess / max(p, 1e-6)
            cut = max(math.ceil(cut / step - 1e-9), 1) * step if step else cut * 1.000001 + 1e-9
            filled = filled[:-1] + ([(c - cut, p)] if c - cut > 1e-9 else [])
        return filled

    # ------------------------------------------------------------ trading
    def execute(self, order: Order, snap: Snapshot, books: dict[str, Book]) -> Optional[Fill]:
        """Like execute_many, but returns only the first fill (or None)."""
        fills = self.execute_many(order, snap, books)
        return fills[0] if fills else None

    def execute_many(self, order: Order, snap: Snapshot, books: dict[str, Book]) -> list[Fill]:
        """
        `books` is this strategy's private copy of {UP: Book, DOWN: Book} for this tick.
        Filled liquidity is removed from it so two orders in one tick can't both take the same shares.
        Returns the fills ([] if rejected). On a one-instrument market a BUY of one side while holding
        the other comes back as a SELL of the held side (plus a BUY of any excess).
        """
        if order.outcome not in (UP, DOWN):
            raise ValueError(f"bad outcome {order.outcome}")
        if order.side not in (BUY, SELL):
            raise ValueError(f"bad side {order.side}")
        m = snap.market
        pos = self.position(snap)
        theta = self.fees.theta(snap.ts, m)
        min_notional = getattr(m, "min_notional", MIN_NOTIONAL)
        min_shares = getattr(m, "min_order_shares", 0.0) or 0.0
        step = getattr(m, "qty_step", 0.0) or 0.0
        book = books[order.outcome]

        if order.side == SELL:
            held = pos.shares(order.outcome)
            want = held if order.shares is None else min(order.shares, held)
            if want <= 1e-9:
                return []
            filled, left = self._walk(book.bids, SELL, order.limit, theta, m, max_shares=want)
            sh = sum(c for c, _ in filled)
            notional = sum(c * p for c, p in filled)
            if not filled or notional < min_notional or sh < min_shares - 1e-9:
                return []
            fee = self._fee(filled, SELL, theta, m)
            book.bids = left
            self._sync_mirror(books, order.outcome, m)
            return [self._book_fill(order.outcome, SELL, filled, fee, theta, order.reason, snap, pos)]

        # ---- BUY ----
        budget = min(order.usd if order.usd is not None else float("inf"), self.cash)
        held_other = pos.shares(_other(order.outcome)) if getattr(m, "single_instrument", False) else 0.0
        if held_other > 1e-9:
            # Closing part: no cash needed (you receive money for the side you sell), so only the
            # opening remainder is limited by the budget. Walk without a budget, then split.
            want = order.shares
            if want is None:   # dollar-sized: as many contracts as the dollars would buy
                f_dry, _ = self._walk(book.asks, BUY, order.limit, theta, m,
                                      max_usd=budget if budget != float("inf") else None)
                want = sum(c for c, _ in f_dry)
            filled, left = self._walk(book.asks, BUY, order.limit, theta, m, max_shares=want)
        else:
            filled, left = self._walk(book.asks, BUY, order.limit, theta, m, max_shares=order.shares,
                                      max_usd=budget if budget != float("inf") else None)
            if budget != float("inf") and filled:
                filled = self._trim_to_budget(filled, budget, lambda f: self._fee(f, BUY, theta, m), step)
        sh = sum(c for c, _ in filled)
        notional = sum(c * p for c, p in filled)
        if not filled or notional < min_notional:
            return []
        if order.shares is not None and sh < min_shares - 1e-9:
            return []

        close = min(sh, held_other)
        close_part, open_part, acc = [], [], 0.0
        for c, p in filled:
            take = min(c, max(close - acc, 0.0))
            if take > 1e-9:
                close_part.append((take, round(1 - p, 6)))     # selling the held side at 1 - p
            if c - take > 1e-9:
                open_part.append((c - take, p))
            acc += take
        total_fee = self._fee(filled, BUY, theta, m)
        exact = lambda part: sum(theta * c * p * (1 - p) for c, p in part)     # noqa: E731
        ex_all = exact(filled) or 1.0
        fee_close = total_fee * exact(close_part) / ex_all if close_part else 0.0
        fee_open = total_fee - fee_close
        if open_part and sum(c * p for c, p in open_part) + fee_open > self.cash + 1e-9:
            return []
        book.asks = left
        self._sync_mirror(books, order.outcome, m)
        fills = []
        if close_part:
            fills.append(self._book_fill(_other(order.outcome), SELL, close_part, fee_close, theta,
                                         (order.reason + " " if order.reason else "")
                                         + f"(buying {order.outcome} closes the {_other(order.outcome)} position)",
                                         snap, pos))
        if open_part:
            fills.append(self._book_fill(order.outcome, BUY, open_part, fee_open, theta, order.reason, snap, pos))
        return fills

    def _sync_mirror(self, books, outcome, market):
        """One-instrument market: taking UP asks also takes DOWN bids (same orders), and vice versa."""
        if not getattr(market, "single_instrument", False):
            return
        other = _other(outcome)
        books[other].bids = sorted(_mirror(books[outcome].asks), key=lambda x: -x[0])
        books[other].asks = sorted(_mirror(books[outcome].bids), key=lambda x: x[0])

    def fill_maker(self, outcome: str, shares: float, price: float, snap: Snapshot, rebate_theta: float = 0.0,
                   reason: str = "") -> list[Fill]:
        """
        A resting bid of ours was traded through: buy `shares` of `outcome` at OUR price, no fee
        (Polymarket makers pay none), plus the configured maker rebate. On a one-instrument market,
        buying the side opposite to what we hold closes that position first (like execute_many).
        Shares are capped by available cash.
        """
        m = snap.market
        pos = self.position(snap)
        held_other = pos.shares(_other(outcome)) if getattr(m, "single_instrument", False) else 0.0
        close = min(shares, held_other)
        open_sh = shares - close
        if open_sh > 1e-9 and open_sh * price > self.cash:
            open_sh = max(0.0, math.floor(self.cash / price * 100) / 100.0)
        fills = []
        if close > 1e-9:
            rb = abs(rebate_theta) * close * price * (1 - price)
            fills.append(self._book_fill(_other(outcome), SELL, [(close, round(1 - price, 6))], 0.0, None,
                                         (reason + " " if reason else "") + f"(maker: buying {outcome} closes "
                                         f"the {_other(outcome)} position)", snap, pos, liquidity="maker",
                                         rebate_override=rb))
        if open_sh > 1e-9:
            rb = abs(rebate_theta) * open_sh * price * (1 - price)
            fills.append(self._book_fill(outcome, BUY, [(open_sh, price)], 0.0, None, reason, snap, pos,
                                         liquidity="maker", rebate_override=rb))
        return fills

    def _book_fill(self, outcome, side, filled, fee, theta, reason, snap, pos, liquidity="taker",
                   rebate_override=None) -> Fill:
        sh = sum(c for c, _ in filled)
        notional = sum(c * p for c, p in filled)
        rebate = fee * self.fees.rebate_pct(snap.ts) if rebate_override is None else rebate_override
        if side == BUY:
            self.entries[snap.market.slug] = self.entries.get(snap.market.slug, 0) + 1
            self.cash -= notional + fee
            pos.add_buy(outcome, sh, notional + fee)
        else:
            self.cash += notional - fee
            pos.add_sell(outcome, sh, notional - fee)
        self.cash += rebate          # Polymarket US pays rebates weekly; credited here when earned
        if liquidity == "taker":     # taker fee tiers count taker volume only
            self.fees.record(snap.ts, side, sh, notional / sh)
        self.traded_slugs.add(snap.market.slug)
        m = snap.market
        return Fill(ts=snap.ts, strategy=self.name, asset=m.asset, slug=m.slug, window_end=m.window_end,
                    outcome=outcome, side=side, shares=sh, avg_price=notional / sh, usd=notional, fee=fee,
                    reason=reason, theta=theta, rebate=rebate, liquidity=liquidity)

    def settle(self, slug: str, winner: str, ts: float) -> Optional[Settlement]:
        pos = self.positions.pop(slug, None)
        self.traded_slugs.discard(slug)
        self.entries.pop(slug, None)
        if pos is None or pos.is_empty():
            return None
        payout = pos.up if winner == UP else pos.down     # each winning share pays $1
        self.cash += payout
        return Settlement(ts=ts, strategy=self.name, asset=pos.asset, slug=slug, winner=winner,
                          up_shares=pos.up, down_shares=pos.down, net_cost=pos.net_cost,
                          payout=payout, pnl=payout - pos.net_cost)

    def correct_settlement(self, old_payout: float, new_payout: float):
        """A round was settled on the wrong side: take back the old payout and pay the right one."""
        self.cash += new_payout - old_payout

    # ------------------------------------------------------------ valuation
    def open_value(self, marks: dict[str, tuple]) -> float:
        """
        Mark open positions to the latest mid. If only one side has a mid, the other side is
        valued at 1 - that mid. With no mark at all, shares still held are valued at what was
        paid for them (a fully sold position is worth $0, whatever it cost).
        """
        total = 0.0
        for slug, pos in self.positions.items():
            up_mid, down_mid = marks.get(slug, (None, None))
            if up_mid is None and down_mid is not None:
                up_mid = 1 - down_mid
            if down_mid is None and up_mid is not None:
                down_mid = 1 - up_mid
            if up_mid is not None:
                total += pos.up * up_mid + pos.down * down_mid
            else:
                total += pos.up * (pos.avg_cost(UP) or 0.0) + pos.down * (pos.avg_cost(DOWN) or 0.0)
        return total

    def equity(self, marks: dict[str, tuple]) -> float:
        return self.cash + self.open_value(marks)

    # ------------------------------------------------------------ restart support
    def replay(self, fills: list[dict], settlements: list[dict]):
        """Rebuild cash, open positions and fee-tier volume from the database after a restart."""
        settled = {s["slug"] for s in settlements}
        for f in fills:
            pos = self.positions.setdefault(f["slug"], Position(f["asset"], f["slug"], f["window_end"]))
            if f["side"] == BUY:
                if f["slug"] not in settled:
                    self.entries[f["slug"]] = self.entries.get(f["slug"], 0) + 1
                cost = f["usd"] + f["fee"]
                self.cash -= cost
                pos.add_buy(f["outcome"], f["shares"], cost)
            else:
                proceeds = f["usd"] - f["fee"]
                self.cash += proceeds
                pos.add_sell(f["outcome"], f["shares"], proceeds)
            self.cash += f.get("rebate") or 0.0
            if f["shares"] > 0 and (f.get("liquidity") or "taker") == "taker":
                self.fees.record(f["ts"], f["side"], f["shares"], f["usd"] / f["shares"])
            if f["slug"] not in settled:
                self.traded_slugs.add(f["slug"])
        for s in settlements:
            self.cash += s["payout"]
        for slug in settled:
            self.positions.pop(slug, None)
