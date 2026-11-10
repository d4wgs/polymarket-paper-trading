"""
Order log: what every order looked like when the strategy DECIDED, and what it actually got.

LOGGING ONLY. Nothing here changes whether or how an order fills; the engine calls these functions around the
unchanged risk check / broker / maker code, and a failure in here is logged and swallowed, never raised.

One row per order in the `orders` table (core/storage.py):
  decision time   ts, best bid / ask of the side traded, displayed depth up to the order's limit (shares and $),
                  intended size ($ or shares), limit price, seconds left
  execution       exec_ts, the best bid / ask and depth-to-limit of the book the broker actually filled against
                  (the strategy's private copy of the book fetched after the latency)
  result          filled shares / $ / average price / fee, fill_ratio (filled / intended), slippage, status, reason
Fills point back to their order (fills.order_id) and carry decision_bid / decision_ask / decision_depth / slippage.

slippage is a COST per share, positive = worse than the decision-time touch:
  BUY   realized average price - decision best ask        SELL  decision best bid - realized average price
(prices in the ORDER's terms: on Polymarket US a buy of DOWN that closes an UP position is booked as a SELL of UP
at 1 - p; here it counts at p.) A maker fill below the ask therefore has negative slippage.

status   filled | partial | unfilled | rejected | resting (a maker bid that is still live)
reason   why it is not `filled`:
  risk_limit                 rejected (or clipped) by the risk limits / kill switch (detail = which one)
  latency_book_empty         no usable book could be fetched after the order latency
  no_liquidity               nothing on that side of the book, or not enough displayed size within the limit
  price_moved                the price was within the limit at decision time but not after the latency
  limit_not_marketable       the limit was already beyond the touch when the order was sent
  insufficient_cash          the account's cash was smaller than the order
  nothing_to_sell            a SELL with no shares held
  size_too_small             the fillable amount was below the exchange minimum / one contract step
  would_cross                a maker bid at or above the ask (not a maker order)
  quote_not_traded_through   a maker bid that was cancelled / replaced / expired before the market traded through it
  bad_order                  the broker refused the order as malformed

Orders that fail the same way again and again (a strategy retrying every tick) are ONE row per strategy / round /
side / reason with `attempts` counting the repeats and `last_ts` the latest one, so the table stays small and the
attempt count stays exact. Every order that fills (fully or partly) is its own row.
"""
from __future__ import annotations

import logging
from typing import Optional

from core.models import UP

log = logging.getLogger("orderlog")

FILLED_RATIO = 0.99        # >= this share of the intended size counts as filled (contract-step rounding)
EPS = 1e-9


def book_view(book, side: str, limit: Optional[float]) -> tuple:
    """(best bid, best ask, displayed shares up to `limit`, the same in dollars) for an order on this book."""
    if side == "BUY":
        levels = [(p, q) for p, q in book.asks if limit is None or p <= limit + EPS]
    else:
        levels = [(p, q) for p, q in book.bids if limit is None or p >= limit - EPS]
    return book.best_bid, book.best_ask, sum(q for _, q in levels), sum(p * q for p, q in levels)


def _book_of(snap, outcome: str):
    return snap.up if outcome == UP else snap.down


def _safe(fn):
    def wrapper(self, *a, **k):
        try:
            return fn(self, *a, **k)
        except Exception:
            log.exception("order log failed in %s (trading is not affected)", fn.__name__)
            return None
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


class OrderLog:
    def __init__(self, store):
        self.store = store
        self.series: dict[tuple, int] = {}          # repeated failures: key -> orders.id

    # ------------------------------------------------------------------ taker orders
    @staticmethod
    def taker_reason(order, dview, eview, *, filled: bool, clipped: bool, held, cash) -> str:
        """Why a taker order that reached the broker filled nothing, or only part."""
        buy = order.side == "BUY"
        if not buy and (held or 0.0) <= EPS:
            return "nothing_to_sell"
        e_touch = eview[1] if buy else eview[0]
        d_touch = dview[1] if buy else dview[0]
        if e_touch is None:
            return "no_liquidity"

        def beyond(px):
            return order.limit is not None and (px > order.limit + EPS if buy else px < order.limit - EPS)
        if beyond(e_touch):
            return "price_moved" if (d_touch is not None and not beyond(d_touch)) else "limit_not_marketable"
        if clipped:
            return "risk_limit"
        if buy and order.usd is not None and cash is not None and cash < order.usd - EPS:
            return "insufficient_cash"
        want_sh = order.shares if order.shares is not None else (held if not buy else None)
        if want_sh is not None and eview[2] < want_sh - EPS:
            return "no_liquidity"
        if buy and order.usd is not None and eview[3] < order.usd * FILLED_RATIO:
            return "no_liquidity"
        return "no_liquidity" if filled else "size_too_small"

    @_safe
    def record(self, strategy: str, order, dsnap, *, kind: str = "taker", exec_ts=None, exec_view=None, fills=(),
               status: Optional[str] = None, reason: Optional[str] = None, detail: Optional[str] = None,
               held=None, cash=None, clipped=None, stamp: Optional[dict] = None) -> Optional[int]:
        """Write one order row (or count a repeat of an identical failure). Returns orders.id."""
        m = dsnap.market
        dview = book_view(_book_of(dsnap, order.outcome), order.side, order.limit)
        buy = order.side == "BUY"
        want_sh = order.shares if order.shares is not None else (held if not buy else None)
        want_usd = order.usd if buy else None
        sh = usd = fee = 0.0
        for f in fills:
            px = f.avg_price if f.outcome == order.outcome else 1.0 - f.avg_price      # in the order's terms
            sh += f.shares
            usd += f.shares * px
            fee += f.fee
        avg = usd / sh if sh > 1e-12 else None
        if want_sh:
            ratio = sh / want_sh
        elif want_usd:
            ratio = (usd + fee) / want_usd
        else:
            ratio = None
        if ratio is not None:
            ratio = max(0.0, min(1.0, ratio))
        if status is None:
            status = "unfilled" if sh <= 1e-12 else ("filled" if ratio is None or ratio >= FILLED_RATIO else "partial")
            if status != "filled" and reason is None and exec_view is not None:
                reason = self.taker_reason(order, dview, exec_view, filled=sh > 1e-12, clipped=clipped is not None,
                                           held=held, cash=cash)
        if clipped is not None and detail is None:
            detail = f"risk limits clipped the order to usd={clipped.usd} shares={clipped.shares}"
        touch = dview[1] if buy else dview[0]
        slip = None if avg is None or touch is None else (avg - touch if buy else touch - avg)
        ev = exec_view or (None, None, None, None)
        row = {"ts": dsnap.ts, "strategy": strategy, "asset": m.asset, "slug": m.slug, "kind": kind,
               "outcome": order.outcome, "side": order.side, "intended_usd": want_usd, "intended_shares": want_sh,
               "limit_px": order.limit, "seconds_left": m.window_end - dsnap.ts,
               "decision_bid": dview[0], "decision_ask": dview[1], "decision_depth": dview[2],
               "decision_depth_usd": dview[3],
               "exec_ts": exec_ts, "exec_bid": ev[0], "exec_ask": ev[1], "exec_depth": ev[2],
               "filled_shares": sh, "filled_usd": usd, "avg_price": avg, "fee": fee, "fill_ratio": ratio,
               "slippage": slip, "status": status, "reason": reason, "detail": detail, "attempts": 1,
               "last_ts": exec_ts if exec_ts is not None else dsnap.ts,
               "hedge": int(bool(getattr(order, "hedge", False))),
               "code_version": (stamp or {}).get("code_version"), "params_hash": (stamp or {}).get("params_hash")}
        if status in ("unfilled", "rejected"):
            key = (strategy, m.slug, kind, order.outcome, order.side, status, reason)
            rid = self.series.get(key)
            if rid is not None:
                self.store.bump_order(rid, row["last_ts"])
                return rid
            rid = self.series[key] = self.store.log_order(row)
            return rid
        return self.store.log_order(row)

    @staticmethod
    def fill_columns(order_id, fill, dsnap, depth=None) -> dict:
        """The decision-time columns stored ON the fill row (in the fill's own outcome / side terms)."""
        book = _book_of(dsnap, fill.outcome)
        buy = fill.side == "BUY"
        touch = book.best_ask if buy else book.best_bid
        slip = None if touch is None else (fill.avg_price - touch if buy else touch - fill.avg_price)
        return {"order_id": order_id, "decision_bid": book.best_bid, "decision_ask": book.best_ask,
                "decision_depth": depth, "slippage": slip}

    # ------------------------------------------------------------------ maker bids
    @_safe
    def quote_placed(self, strategy: str, order, snap, quote, stamp: Optional[dict] = None):
        """A resting bid went live: one row, status `resting`, updated as it fills or is cancelled."""
        dview = book_view(_book_of(snap, order.outcome), order.side, order.limit)
        quote.decision = dview
        quote.order_id = self.record(strategy, order, snap, kind="maker", status="resting", stamp=stamp)
        return quote.order_id

    @_safe
    def quote_filled(self, quote, fills, now: float):
        """Add a (partial) maker fill to the quote's order row. Maker fills are at OUR price, fee 0."""
        got = sum(f.shares for f in fills)
        quote.got = getattr(quote, "got", 0.0) + got
        if quote.order_id is None:
            return
        d = quote.decision or (None, None, None, None)
        self.store.update_order(quote.order_id, {
            "exec_ts": now, "last_ts": now, "filled_shares": quote.got, "filled_usd": quote.got * quote.price,
            "avg_price": quote.price, "fill_ratio": min(1.0, quote.got / quote.shares) if quote.shares else None,
            "slippage": (quote.price - d[1]) if d[1] is not None else None,
            "status": "filled" if quote.remaining <= EPS else "partial",
            "reason": None if quote.remaining <= EPS else "quote_not_traded_through"})

    @_safe
    def quote_closed(self, quote, why: str, now: float):
        """A resting bid was cancelled / replaced / expired. A full fill never gets here (it is already `filled`)."""
        if getattr(quote, "order_id", None) is None:
            return
        if quote.filled <= EPS:
            self.store.update_order(quote.order_id, {"status": "unfilled", "reason": "quote_not_traded_through",
                                                     "detail": why, "exec_ts": now, "last_ts": now,
                                                     "fill_ratio": 0.0})
        else:
            self.store.update_order(quote.order_id, {"status": "partial", "reason": "quote_not_traded_through",
                                                     "detail": why, "last_ts": now})

    def quote_fill_columns(self, quote, fill) -> dict:
        d = getattr(quote, "decision", None) or (None, None, None, None)
        return {"order_id": getattr(quote, "order_id", None), "decision_bid": d[0], "decision_ask": d[1],
                "decision_depth": d[2], "slippage": (quote.price - d[1]) if d[1] is not None else None}

    # ------------------------------------------------------------------ housekeeping
    def prune(self, slug_start_of, cutoff: float):
        """Forget the repeat-counters of rounds that started before `cutoff` (memory only)."""
        self.series = {k: v for k, v in self.series.items() if slug_start_of(k[1]) >= cutoff}


# ---------------------------------------------------------------------------------------- report (read-only)
def summary(con, since: float = 0.0, strategies=None, exclude_slugs=None) -> list[dict]:
    """
    Per strategy, from the `orders` table (any read-only sqlite3 connection):
      orders / attempts      order rows, and attempts including counted repeats of the same failure
      fill_rate              rows that filled (fully or partly) / all rows that were real orders
                             (a still-resting maker bid is not counted either way)
      full_fill_rate         rows that filled completely / the same denominator
      avg_fill_ratio         mean filled / intended over the rows that filled
      avg_slippage_c         mean slippage in CENTS per share over the rows that filled (see module doc: + = worse)
      wavg_slippage_c        the same, weighted by filled shares
      median_depth_x_size    median of (displayed depth up to the limit at decision time / intended shares); a
                             dollar-sized order's shares are estimated as $ / decision touch. < 1 = the order was
                             bigger than what was displayed
      unfilled               {reason: rows} for unfilled / rejected / partial rows; unfilled_attempts the same by attempts
    `exclude_slugs`: rounds to leave out (e.g. rounds marked invalid for data gaps).
    """
    import statistics
    q = "SELECT * FROM orders WHERE ts >= ?"
    args = [since]
    if strategies:
        q += f" AND strategy IN ({','.join('?' * len(strategies))})"
        args += list(strategies)
    cur = con.execute(q + " ORDER BY strategy, ts", args)
    cols = [c[0] for c in cur.description]
    out: dict[str, dict] = {}
    skip = set(exclude_slugs or ())
    for r in cur:
        r = dict(zip(cols, r))
        if r["slug"] in skip or r["status"] == "resting":
            continue
        s = out.setdefault(r["strategy"], {"strategy": r["strategy"], "orders": 0, "attempts": 0, "filled": 0,
                                           "full": 0, "_ratio": [], "_slip": [], "_w": [], "_depth": [],
                                           "unfilled": {}, "unfilled_attempts": {}})
        s["orders"] += 1
        s["attempts"] += r["attempts"] or 1
        got = (r["filled_shares"] or 0.0) > 1e-12
        if got:
            s["filled"] += 1
            s["full"] += r["status"] == "filled"
            if r["fill_ratio"] is not None:
                s["_ratio"].append(r["fill_ratio"])
            if r["slippage"] is not None:
                s["_slip"].append(r["slippage"])
                s["_w"].append(r["filled_shares"])
        if r["status"] != "filled":
            why = r["reason"] or r["status"]
            s["unfilled"][why] = s["unfilled"].get(why, 0) + 1
            s["unfilled_attempts"][why] = s["unfilled_attempts"].get(why, 0) + (r["attempts"] or 1)
        want = r["intended_shares"]
        touch = r["decision_ask"] if r["side"] == "BUY" else r["decision_bid"]
        if not want and r["intended_usd"] and touch:
            want = r["intended_usd"] / touch
        if want and r["decision_depth"] is not None:
            s["_depth"].append(r["decision_depth"] / want)
    rows = []
    for s in out.values():
        n, w = s["orders"], sum(s["_w"])
        rows.append({"strategy": s["strategy"], "orders": n, "attempts": s["attempts"],
                     "fill_rate": s["filled"] / n if n else None, "full_fill_rate": s["full"] / n if n else None,
                     "avg_fill_ratio": statistics.fmean(s["_ratio"]) if s["_ratio"] else None,
                     "avg_slippage_c": 100 * statistics.fmean(s["_slip"]) if s["_slip"] else None,
                     "wavg_slippage_c": 100 * sum(a * b for a, b in zip(s["_slip"], s["_w"])) / w if w > 0 else None,
                     "median_depth_x_size": statistics.median(s["_depth"]) if s["_depth"] else None,
                     "unfilled": dict(sorted(s["unfilled"].items())),
                     "unfilled_attempts": dict(sorted(s["unfilled_attempts"].items()))})
    return sorted(rows, key=lambda r: r["strategy"])


def render_summary(rows: list[dict]) -> str:
    def f(x, spec):
        return "-" if x is None else format(x, spec)
    head = (f"{'strategy':<32} {'orders':>6} {'tries':>6} {'fill%':>6} {'full%':>6} {'ratio':>6} {'slip c':>7} "
            f"{'wslip c':>7} {'depth/size':>10}  not filled (rows)")
    lines = [head]
    for r in rows:
        why = ", ".join(f"{k} {v}" for k, v in r["unfilled"].items()) or "-"
        lines.append(f"{r['strategy']:<32} {r['orders']:>6} {r['attempts']:>6} "
                     f"{f(r['fill_rate'] and r['fill_rate'] * 100 if r['fill_rate'] is not None else None, '.1f'):>6} "
                     f"{f(r['full_fill_rate'] * 100 if r['full_fill_rate'] is not None else None, '.1f'):>6} "
                     f"{f(r['avg_fill_ratio'], '.3f'):>6} {f(r['avg_slippage_c'], '+.2f'):>7} "
                     f"{f(r['wavg_slippage_c'], '+.2f'):>7} {f(r['median_depth_x_size'], '.1f'):>10}  {why}")
    return "\n".join(lines)
