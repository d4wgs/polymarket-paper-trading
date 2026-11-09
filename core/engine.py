"""
The main loop. Every POLL_SECONDS it:
  1. finds the current 15-min market for each asset
  2. downloads both order books (UP and DOWN)
  3. hands that snapshot to every strategy and paper-fills their orders
  4. checks whether finished windows have resolved, and pays out winners
  5. periodically records each strategy's account value

Every strategy sees the exact same data at the same moment, so comparisons
between strategies are apples to apples.

Settlement happens on the PROVISIONAL result (prices locked at ~$1 / ~$0 right after
the window closes) so strategies learn the result quickly. Those rounds are re-checked
until Polymarket officially closes the market; if the official result is different,
the settlement is corrected (database rows and cash) and an ERROR is logged.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import time
from collections import defaultdict

from core.broker import PaperBroker
from core.fees import ET, et_day, fee_model_name, make_fee_model
from core.maker import MakerBook
from core.models import CANCEL, MAKER_BID, Order, Position, Snapshot, UP, DOWN, books_consistent
from core.risk import RiskManager
from core.storage import Storage
from strategies.base import OtherMarket, Strategy, TickContext

log = logging.getLogger("engine")

OFFICIAL, PROVISIONAL_GAVE_UP = 1, -1      # values of resolutions.official (0 = provisional)
KEEP_SECONDS = 6 * 3600                    # how long finished rounds stay in the in-memory caches
RESULTS_KEPT = 50                          # recent results per market (ctx.prev_windows)


def _fmt(x):
    return "-" if x is None else f"{x:.3f}"


def _slug_start(slug: str) -> int:
    try:
        return int(slug.rsplit("-", 1)[1])
    except (IndexError, ValueError):
        return 0


class Engine:
    def __init__(self, cfg, strategies: list[Strategy], feed, storage: Storage,
                 clock=time.time, sleep=time.sleep, market_data=None):
        self.cfg, self.strategies, self.feed, self.store = cfg, strategies, feed, storage
        self.clock, self.sleep = clock, sleep
        self.md = market_data                                                # core.fairvalue.MarketData
        self.latency = float(getattr(cfg, "LATENCY_SECONDS", 0.0) or 0.0)    # taker order latency
        maker_cfg = getattr(cfg, "MAKER", {}) or {}
        self.maker = MakerBook(latency=self.latency, conservative_queue=maker_cfg.get("conservative_queue", True))
        self.maker_rebate_theta = float(getattr(cfg, "MAKER_REBATE_THETA", 0.0) or 0.0)
        self.followup_seconds = float(maker_cfg.get("adverse_selection_seconds", 5))
        self.followups: list[tuple] = []                                     # (due, fill rowid, slug, outcome)
        self.risk = RiskManager(getattr(cfg, "RISK", {}) or {})
        self._export_day = None
        self._reject_logged: dict[tuple, float] = {}
        self.brokers: dict[str, PaperBroker] = {}
        self.retired: dict[str, PaperBroker] = {}                            # settle-only (no longer configured)
        self.history: dict[str, list[Snapshot]] = defaultdict(list)          # asset -> this window's snaps
        self.current_slug: dict[str, str] = {}                               # asset -> slug
        self.results: dict[str, dict[int, str]] = defaultdict(dict)        # asset -> {window start: winner}
        self.winners: dict[str, str] = {}                                    # slug -> "UP"/"DOWN" once known
        self.official: set[str] = set()                                      # slugs whose winner is official
        self._winner_try: dict[str, float] = {}                              # slug -> last on-demand lookup time
        self.state: dict[tuple, dict] = defaultdict(dict)                   # (strategy, asset) -> scratchpad
        self.unresolved: dict[str, tuple[str, float]] = {}                  # slug -> (asset, window_end)
        self.provisional: dict[str, list] = {}                              # slug -> [asset, window_end, last_check]
        self._warned: dict[str, float] = {}                                  # slug -> last "still unresolved" warning
        self.marks: dict[str, tuple] = {}                                    # slug -> (up_mid, down_mid)
        self._last_equity = self._last_settle = self._last_status = self._last_book_prune = 0.0
        self._stats_last: dict[str, tuple] = {}                              # slug -> ((shares, OI), logged at)
        self.skipped_ticks = 0                                               # ticks dropped: inconsistent books
        self.stopped: dict[str, dict] = {}                                   # strategy -> stop record
        self._windows_logged: set[str] = set()                               # slugs whose price-to-beat is saved
        self._last_skip_log = 0.0
        self.latest: dict[str, Snapshot] = {}                                # asset -> its latest snapshot
        self._equity_last: dict[str, tuple] = {}                             # strategy -> (cash, open value, saved at)

    def _setting(self, name, default):
        return getattr(self.cfg, name, default)

    # ------------------------------------------------------------------ setup
    def setup(self):
        names = [s.name for s in self.strategies]
        if len(names) != len(set(names)):
            raise ValueError(f"Strategy names must be unique: {names}")
        now = self.clock()
        for s in self.strategies:
            bankroll = s.bankroll or self.cfg.STARTING_BANKROLL
            warn = self.store.register_strategy(s.name, type(s).__name__, s.p, bankroll, s.assets, now)
            if warn:
                log.warning(warn)
            b = PaperBroker(s.name, bankroll, self.cfg.EXTRA_SLIPPAGE, fee_model=make_fee_model(self.cfg))
            fills, settles = self.store.history_for(s.name)
            if fills:
                b.replay(fills, settles)
                log.info("Restored %s: cash $%.2f, %d open positions", s.name, b.cash, len(b.positions))
            for slug, pos in b.positions.items():
                self.unresolved[slug] = (pos.asset, pos.window_end)
            self.brokers[s.name] = b
        # Retired strategies (in the database, no longer configured) may still hold bets: keep a
        # settle-only account for each, so those bets are paid out instead of staying open forever.
        for name in self.store.strategies_with_open_bets():
            if name in self.brokers:
                continue
            b = PaperBroker(name, self.store.bankroll_of(name) or self.cfg.STARTING_BANKROLL,
                            fee_model=make_fee_model(self.cfg))
            b.replay(*self.store.history_for(name))
            if b.positions:
                for slug, pos in b.positions.items():
                    self.unresolved[slug] = (pos.asset, pos.window_end)
                self.retired[name] = b
                log.info("Retired strategy %s still has %d open bet(s); they will be settled (no new trades)",
                         name, len(b.positions))
        self.stopped = self.store.stopped_strategies()
        for name in self.stopped:
            if name in self.brokers:
                log.warning("%s stopped trading earlier (portfolio fell to $%.2f); it stays stopped. Give it a "
                            "new name to start it again.", name, self.stopped[name]["equity"])
        self.store.set_meta("exchange", self._setting("EXCHANGE", "polymarket_intl"))
        self.store.set_meta("stop_equity", self._setting("STOP_EQUITY", 0.0))
        self.store.set_meta("fee_model", fee_model_name(self.cfg))
        for r in self.store.provisional_resolutions():
            self.provisional[r["slug"]] = [r["asset"], r["window_end"], 0.0]
            self.winners[r["slug"]] = r["winner"]
        # rebuild recent results so ctx.prev_windows / prev_winner survive a restart
        for r in self.store.recent_resolutions(now - KEEP_SECONDS):
            if r["winner"] in (UP, DOWN):
                self._record_result(r["asset"], r["slug"], r["winner"])
                self.winners.setdefault(r["slug"], r["winner"])
        if self.provisional:
            log.info("%d settled round(s) still waiting for Polymarket's official result", len(self.provisional))

    # ------------------------------------------------------------------ loop
    def run(self, max_seconds: float | None = None):
        self.setup()
        started = self.clock()
        log.info("Running %d strategies on %s", len(self.strategies),
                 ", ".join(m["asset"] for m in self.cfg.MARKETS))
        while max_seconds is None or self.clock() - started < max_seconds:
            t0 = self.clock()
            try:
                self.tick(t0)
            except KeyboardInterrupt:
                raise
            except Exception:
                log.exception("Tick failed; continuing")
            self.sleep(max(0.0, self.cfg.POLL_SECONDS - (self.clock() - t0)))
        self.record_equity(self.clock(), force=True)
        self.store.commit()

    def tick(self, now: float):
        for mcfg in self.cfg.MARKETS:
            try:
                self._tick_asset(mcfg, now)
            except Exception:
                # one market failing must not starve the others (or skip settlement/equity below)
                log.exception("Tick failed for %s; continuing", mcfg["asset"])
        for name, b in self.brokers.items():
            self._check_stop(name, b, now)
        if now - self._last_settle >= self.cfg.SETTLE_CHECK_SECONDS:
            self.settle_finished(now)
            self._last_settle = now
        if now - self._last_equity >= self.cfg.EQUITY_EVERY_SECONDS:
            self.record_equity(now)
            self._last_equity = now
        if now - self._last_status >= 60:
            self._print_status()
            self._last_status = now
        if now - self._last_book_prune >= 600:
            self._prune_books(now)
            self._last_book_prune = now
        self._daily_export(now)
        self.store.commit()

    def _log_stats(self, info, now: float):
        """Record the exchange's volume stats for the round when they change (or every 30s as a heartbeat)."""
        stats = (getattr(self.feed, "last_stats", None) or {}).get(info.slug)
        if not stats:
            return
        key = (stats.get("shares_traded"), stats.get("open_interest"))
        last = self._stats_last.get(info.slug)
        if last and last[0] == key and now - last[1] < 30:
            return
        self._stats_last[info.slug] = (key, now)
        self.store.log_stats(now, info.asset, info.slug, info.window_end - now, stats)

    def _prune_books(self, now: float):
        """Keep the raw order-book log (the biggest table by far) to BOOKS_KEEP_DAYS so the disk can't fill."""
        days = self._setting("BOOKS_KEEP_DAYS", None)
        if not days:
            return
        try:
            n = self.store.prune_books(now - days * 86400)
            if n:
                log.info("Deleted %d order-book rows older than %s days (BOOKS_KEEP_DAYS)", n, days)
        except Exception:
            log.exception("Pruning old order books failed; will retry")

    def _tick_asset(self, mcfg: dict, now: float):
        asset = mcfg["asset"]
        info = self.feed.current_market(mcfg, now)
        if info is None:
            return
        up, down = self._fetch_books(info, now)
        if up is None:
            return
        index_fn = getattr(self.feed, "index_price", None)
        index = None
        if index_fn is not None:
            try:
                index = index_fn(info, now)
            except Exception:
                log.exception("Index price lookup failed for %s", asset)
        now = self.clock()   # fresh time: fetching books takes a moment, and strategies act on seconds_left
        fv, spot = self._model(info, now, index)
        snap = Snapshot(ts=now, market=info, up=up, down=down, index_price=index, fv=fv, spot=spot)
        if getattr(info, "price_to_beat", None) is not None and info.slug not in self._windows_logged:
            self.store.log_window(info)
            self._windows_logged.add(info.slug)
        self.store.log_snapshot(snap)
        self.store.log_book(snap, self._setting("BOOK_LEVELS_LOGGED", 10))
        self._log_stats(info, now)
        self.marks[info.slug] = (up.mid, down.mid)
        self.latest[asset] = snap
        self._run_followups(info.slug, fv, now)

        new_window = self.current_slug.get(asset) != info.slug
        if new_window:
            old = self.current_slug.get(asset)
            self.current_slug[asset] = info.slug
            self.history[asset] = []
            self.unresolved.setdefault(info.slug, (asset, info.window_end))
            for s in self.strategies:
                self.state[(s.name, asset)] = {}
                if old:
                    self.maker.cancel(s.name, old)            # quotes don't outlive their round

        prev_winner = self._prev_winner(info, now)
        prev_windows = self._recent_results(info)
        pending = []                                          # (strategy, broker, order, decision snapshot)
        for s in self.strategies:
            if not s.wants(asset):
                continue
            broker = self.brokers[s.name]
            # resting maker bids first: did the market trade through any of them since the last tick?
            self._maker_fills(s, broker, snap)
            if self._check_stop(s.name, broker, now):
                self.maker.cancel(s.name, info.slug)
                continue                                 # hit the stop level: no more trading, ever
            if self.risk.kill_active:
                self.maker.cancel(s.name, info.slug)
            equity = broker.equity(self.marks)
            ctx = TickContext(snap=snap, position=broker.position(snap), cash=broker.cash,
                              equity=equity,
                              fee_theta=broker.current_theta(now, info),
                              history=list(self.history[asset]),
                              prev_windows=list(prev_windows),
                              prev_winner=prev_winner,
                              state=self.state[(s.name, asset)],
                              traded_this_window=info.slug in broker.traded_slugs,
                              md=self.md,
                              quotes=self.maker.for_market(s.name, info.slug),
                              entries_this_window=broker.entries.get(info.slug, 0),
                              kill_switch=self.risk.kill_active,
                              others=self._others(broker, asset))
            try:
                if new_window:
                    s.on_window_start(ctx)
                orders = s.on_tick(ctx) or []
            except Exception:
                log.exception("Strategy %s crashed on tick; skipping it this tick", s.name)
                orders = []
            for kind, data in ctx.events:
                self.store.log_event(now, s.name, asset, info.slug, kind, data)
            for order in orders:
                if order.kind == CANCEL:
                    self.maker.cancel(s.name, info.slug, order.outcome)
                elif order.kind == MAKER_BID:
                    self._place_quote(s, broker, order, snap)
                else:
                    pending.append((s, broker, order, snap))
            pos = broker.positions.get(info.slug)
            if pos and pos.is_empty():
                broker.positions.pop(info.slug, None)   # don't keep empty positions around

        self.history[asset].append(snap)

        # taker orders: they reach the exchange `latency` seconds later and fill against THAT book
        while pending:
            exec_snap = self._after_latency(info, snap)
            chained = []
            books = {}
            for s, broker, order, dsnap in pending:
                if exec_snap is None:
                    self.store.log_event(now, s.name, asset, info.slug, "order_dropped",
                                         {"why": "no consistent book after latency", "outcome": order.outcome,
                                          "side": order.side, "reason": order.reason})
                    continue
                if s.name not in books:     # private copy per strategy so strategies don't share liquidity
                    books[s.name] = {UP: exec_snap.up.copy(), DOWN: exec_snap.down.copy()}
                fills = self._execute_taker(s, broker, order, dsnap, exec_snap, books[s.name])
                if order.then is not None:
                    try:
                        nxt = order.then(fills)
                    except Exception:
                        log.exception("Strategy %s crashed in a chained order", s.name)
                        nxt = None
                    if nxt is not None:
                        chained.append((s, broker, nxt, dsnap))
            for s, broker, _, _ in pending:
                self._check_stop(s.name, broker, now)
                pos = broker.positions.get(info.slug)
                if pos and pos.is_empty():
                    broker.positions.pop(info.slug, None)
            pending = chained
            if exec_snap is not None:
                snap = exec_snap
                self.latest[asset] = snap

    def _others(self, broker, asset: str) -> dict:
        """Read-only view of the other markets for a strategy: latest snapshot + its position there."""
        out = {}
        for a, sn in self.latest.items():
            if a == asset:
                continue
            m = sn.market
            pos = broker.positions.get(m.slug) or Position(m.asset, m.slug, m.window_end)  # never stored
            out[a] = OtherMarket(sn, pos)
        return out

    # ------------------------------------------------------------------ data / model
    def _fetch_books(self, info, now):
        up, down = self.feed.books(info)
        if up is None or down is None:
            return None, None
        if not books_consistent(up, down):
            # the two books were captured at different moments: fetch again once, else skip the tick
            up, down = self.feed.books(info)
            if up is None or down is None or not books_consistent(up, down):
                self.skipped_ticks += 1
                if now - self._last_skip_log >= 60:
                    self._last_skip_log = now
                    log.info("%s: UP and DOWN books disagree (captured at different moments); skipped the "
                             "tick (%d skipped so far)", info.asset, self.skipped_ticks)
                return None, None
        return up, down

    def _model(self, info, now, index):
        """Fair value + spot for this market right now; updates the basis kill switch."""
        if self.md is None:
            return None, None
        spot_feed = self.md.spot
        if spot_feed is not None:
            try:
                spot_feed.poll(now)
            except Exception:
                log.exception("Spot feed poll failed")
        self.md.on_oracle(now, index)
        spot = self.md.spot_now(now)
        try:
            fv = self.md.compute(info, now, oracle=index)
        except Exception:
            log.exception("Fair value failed for %s", info.slug)
            fv = None
        # like-for-like basis (the index is a 60s average; see core/fairvalue.py)
        basis = fv.basis if fv is not None else self.md.basis(now, index)
        self.risk.update_kill(basis, spot, index, now)
        return fv, spot

    def _after_latency(self, info, snap):
        """The book as it looks `latency` seconds after the decision (None if it can't be fetched)."""
        if self.latency <= 0:
            return snap
        self.sleep(self.latency)
        now = self.clock()
        up, down = self._fetch_books(info, now)
        if up is None:
            return None
        fv, spot = self._model(info, now, snap.index_price)
        self.marks[info.slug] = (up.mid, down.mid)
        return Snapshot(ts=now, market=info, up=up, down=down, index_price=snap.index_price, fv=fv, spot=spot)

    def _fill_meta(self, fill, dsnap, decision_ts, liquidity_fee_per_share=None, extra=None):
        """What the model thought when the decision was made, and the edge actually obtained."""
        fv = getattr(dsnap, "fv", None)
        meta = {"decision_ts": decision_ts, "seconds_left": dsnap.market.window_end - decision_ts,
                "spot": getattr(dsnap, "spot", None), "oracle": dsnap.index_price}
        if fv is not None:
            fair = fv.of(fill.outcome)
            fee_ps = fill.fee / fill.shares if fill.shares > 0 else 0.0
            rebate_ps = (fill.rebate or 0.0) / fill.shares if fill.shares > 0 else 0.0
            if fill.side == "BUY":
                edge = fair - (fill.avg_price + fee_ps - rebate_ps)
            else:
                edge = (fill.avg_price - fee_ps + rebate_ps) - fair
            meta.update(fair_value=fair, edge=edge, basis=fv.basis, sigma=fv.sigma, k=fv.K)
        if extra:
            meta.update(extra)
        return meta

    # ------------------------------------------------------------------ execution
    def _risk_check(self, s, broker, order, snap):
        """None = rejected; else the order (possibly clipped). Closing trades are never blocked."""
        m = snap.market
        pos = broker.positions.get(m.slug)
        if order.side == "SELL":
            return order
        if pos is not None and getattr(m, "single_instrument", False):
            other = DOWN if order.outcome == UP else UP
            if pos.shares(other) > 1e-9 and (order.shares is not None and order.shares <= pos.shares(other) + 1e-9):
                return order                         # buying the other side = closing
        open_markets = sum(1 for p in broker.positions.values() if p.up > 1e-9 or p.down > 1e-9)
        in_market = pos is not None and (pos.up > 1e-9 or pos.down > 1e-9)
        # A hedge completes a position held in ANOTHER market (e.g. leg 2 of a cross-market pair). Blocking it
        # would leave that leg naked, so the kill switch / open-markets limit don't apply -- but only while
        # such a position really exists (a strategy can't use the flag to open fresh risk).
        hedge = bool(getattr(order, "hedge", False)) and any(
            slug != m.slug and (p.up > 1e-9 or p.down > 1e-9) for slug, p in broker.positions.items())
        dec = self.risk.check_buy(s.name, usd=order.usd, shares=order.shares, price=order.limit,
                                  market_cost=pos.net_cost if pos else 0.0, open_markets=open_markets,
                                  in_this_market=in_market, equity=broker.equity(self.marks), hedge=hedge)
        if not dec.ok:
            key = (s.name, m.slug, dec.reason)
            if snap.ts - self._reject_logged.get(key, 0.0) >= 60:     # one event per minute per reason
                self._reject_logged[key] = snap.ts
                self.store.log_event(snap.ts, s.name, m.asset, m.slug, "risk_reject",
                                     {"why": dec.reason, "outcome": order.outcome, "usd": order.usd,
                                      "shares": order.shares, "limit": order.limit, "reason": order.reason})
            return None
        if dec.reason == "clipped":
            order = Order(order.outcome, order.side, usd=dec.usd, shares=dec.shares, limit=order.limit,
                          reason=order.reason, kind=order.kind, then=order.then, meta=order.meta,
                          hedge=getattr(order, "hedge", False))
        return order

    def _log_unfilled(self, s, order, dsnap, exec_snap):
        """
        A taker order that passed the risk check but got no fill (the price moved past its limit during the
        latency, or the book was empty). Recorded as an `order_unfilled` event so the fill rate can be measured;
        at most one per strategy / round / side every 10s (orders that are retried every tick would flood it).
        """
        m = exec_snap.market
        key = (s.name, m.slug, "unfilled", order.outcome, order.side)
        if exec_snap.ts - self._reject_logged.get(key, 0.0) < 10:
            return
        self._reject_logged[key] = exec_snap.ts

        def touch(snap):
            book = snap.up if order.outcome == UP else snap.down
            return book.best_ask if order.side == "BUY" else book.best_bid
        self.store.log_event(exec_snap.ts, s.name, m.asset, m.slug, "order_unfilled",
                             {"outcome": order.outcome, "side": order.side, "usd": order.usd,
                              "shares": order.shares, "limit": order.limit,
                              "decision_px": touch(dsnap), "exec_px": touch(exec_snap),
                              "latency": round(exec_snap.ts - dsnap.ts, 3),
                              "seconds_left": round(exec_snap.seconds_left, 1), "reason": order.reason})

    def _execute_taker(self, s, broker, order, dsnap, exec_snap, books):
        checked = self._risk_check(s, broker, order, exec_snap)
        if checked is None:
            return []
        try:
            fills = broker.execute_many(checked, exec_snap, books)
        except Exception:
            log.exception("Strategy %s sent a bad order %s; skipped", s.name, order)
            return []
        if not fills:
            self._log_unfilled(s, checked, dsnap, exec_snap)
        for fill in fills:
            fill.meta = self._fill_meta(fill, dsnap, dsnap.ts, extra=order.meta)
            self.store.log_fill(fill)
            log.info("[%s] %s %s %.2f sh @ %.3f ($%.2f, fee $%.2f) fair %s edge %s | %s",
                     s.name, fill.side, fill.outcome, fill.shares, fill.avg_price, fill.usd, fill.fee,
                     _fmt(fill.meta.get("fair_value")), _fmt(fill.meta.get("edge")), fill.reason)
        return fills

    def _place_quote(self, s, broker, order, snap):
        m = snap.market
        if self.risk.kill_active:
            self.maker.cancel(s.name, m.slug, order.outcome)
            return
        checked = self._risk_check(s, broker, order, snap)
        if checked is None or not checked.shares:
            self.maker.cancel(s.name, m.slug, order.outcome)
            return
        book = snap.up if order.outcome == UP else snap.down
        fv = snap.fv
        self.maker.place(s.name, m.slug, m.asset, order.outcome, order.limit, checked.shares, snap.ts, book,
                         reason=order.reason,
                         meta={**order.meta, "fv_at_quote": fv.of(order.outcome) if fv else None})

    def _maker_fills(self, s, broker, snap):
        m = snap.market
        hits = self.maker.check(s.name, m.slug, {UP: snap.up, DOWN: snap.down}, snap.ts)
        for q, shares in hits:
            fills = broker.fill_maker(q.outcome, shares, q.price, snap, rebate_theta=self.maker_rebate_theta,
                                      reason=q.reason or "maker bid traded through")
            for fill in fills:
                extra = {**q.meta, "quote_price": q.price, "quoted_at": q.placed_ts, "queue_left": q.queue_ahead}
                fill.meta = self._fill_meta(fill, snap, snap.ts, extra=extra)
                rowid = self.store.log_fill(fill)
                if snap.fv is not None:
                    self.followups.append((snap.ts + self.followup_seconds, rowid, m.slug, fill.outcome))
                log.info("[%s] MAKER %s %s %.2f sh @ %.3f fair %s | %s", s.name, fill.side, fill.outcome,
                         fill.shares, fill.avg_price, _fmt(fill.meta.get("fair_value")), fill.reason)

    def _run_followups(self, slug, fv, now):
        """Record the fair value 5s after each maker fill (the adverse-selection measure)."""
        if not self.followups:
            return
        keep = []
        for due, rowid, fslug, outcome in self.followups:
            if now < due:
                keep.append((due, rowid, fslug, outcome))
                continue
            cur = fv if fslug == slug else (self.md.last.get(fslug) if self.md else None)
            if cur is not None:
                self.store.set_fill_followup(rowid, cur.of(outcome))
            elif now - due < 60:
                keep.append((due, rowid, fslug, outcome))
        self.followups = keep

    # ------------------------------------------------------------------ daily CSV export
    def _daily_export(self, now: float):
        folder = self._setting("EXPORT_DIR", None)
        if not folder:
            return
        today = et_day(now)
        if self._export_day == today:
            return
        first = self._export_day is None
        self._export_day = today
        day = today - dt.timedelta(days=1)
        path = os.path.join(folder, f"trades_{day.isoformat()}.csv")
        if first and os.path.exists(path):
            return
        t0 = dt.datetime.combine(day, dt.time(), ET).timestamp()
        t1 = dt.datetime.combine(today, dt.time(), ET).timestamp()
        try:
            n = self.store.export_fills_csv(path, t0, t1)
            log.info("Exported %d trades from %s (ET) to %s", n, day, path)
        except Exception:
            log.exception("Daily CSV export failed")

    def _record_result(self, asset: str, slug: str, winner: str):
        start = _slug_start(slug)
        if not start:
            return
        res = self.results[asset]
        res[start] = winner
        if len(res) > RESULTS_KEPT:
            for k in sorted(res)[:len(res) - RESULTS_KEPT]:
                del res[k]

    def _recent_results(self, info) -> list[str]:
        """
        Winners of the rounds right before this one, oldest first, newest last. Only an unbroken run
        of consecutive rounds is returned: if a result is missing (bot was off, result not known yet),
        the list stops there, so a strategy never mistakes a gap for a streak.
        """
        window = int(info.window_end - info.window_start)
        prefix = info.slug.rsplit("-", 1)[0]
        res = self.results.get(info.asset, {})
        out, start = [], int(info.window_start) - window
        while len(out) < RESULTS_KEPT:
            w = res.get(start) or self.winners.get(f"{prefix}-{start}")
            if w is None:
                break
            out.append(w)
            start -= window
        return out[::-1]

    def _resolve(self, slug: str, window_end: float) -> tuple:
        """(winner, official). Feeds without official/provisional detail are treated as official."""
        detail = getattr(self.feed, "resolution_detail", None)
        if detail is not None:
            return detail(slug, window_end)
        return self.feed.resolution(slug, window_end), True

    def _prev_winner(self, info, now: float):
        """
        Winner ("UP"/"DOWN") of the window immediately before this one, or None if not known yet.
        The regular settlement check only runs every SETTLE_CHECK_SECONDS, which can be too slow for
        a strategy acting 15s into a window, so ask the feed directly (at most every 5s, and only
        during the first 2 minutes of the window).
        """
        window = int(info.window_end - info.window_start)
        prefix = info.slug.rsplit("-", 1)[0]
        prev_slug = f"{prefix}-{int(info.window_start) - window}"
        if prev_slug in self.winners:
            return self.winners[prev_slug]
        if now - info.window_start > 120:
            return None
        if now - self._winner_try.get(prev_slug, 0.0) < 5:
            return None
        self._winner_try[prev_slug] = now
        winner, official = self._resolve(prev_slug, info.window_start)
        if winner is not None:
            self.winners[prev_slug] = winner
            if official:
                self.official.add(prev_slug)
        return winner

    # ------------------------------------------------------------------ settlement
    def settle_finished(self, now: float):
        """
        Pay out finished rounds. Each pass makes at most SETTLE_MAX_LOOKUPS API calls, with rounds
        that strategies hold money in first, so a backlog (or a slow API) can't stall the main loop;
        whatever is left is picked up on the next pass.
        """
        max_lookups = self._setting("SETTLE_MAX_LOOKUPS", 6)
        lookups = 0
        held = {slug for b in list(self.brokers.values()) + list(self.retired.values()) for slug in b.positions}
        order = sorted(self.unresolved.items(), key=lambda kv: (kv[0] not in held, kv[1][1]))
        for slug, (asset, window_end) in order:
            if now < window_end + 5:
                continue
            if slug in self.winners:                       # already learned (e.g. by _prev_winner)
                winner, official = self.winners[slug], slug in self.official
            else:
                if lookups >= max_lookups:
                    continue
                lookups += 1
                winner, official = self._resolve(slug, window_end)
            if winner is None:
                late = now - window_end
                if late > self.cfg.SETTLE_WARN_SECONDS and now - self._warned.get(slug, 0.0) >= 3600:
                    log.warning("%s still unresolved %.0f min after close", slug, late / 60)
                    self._warned[slug] = now
                continue
            self._settle(slug, asset, window_end, winner, official, now)
        self._reconcile(now, max(0, max_lookups - lookups))
        self._prune(now)

    def _settle(self, slug, asset, window_end, winner, official, now):
        del self.unresolved[slug]
        self._warned.pop(slug, None)
        self._record_result(asset, slug, winner)
        self.winners[slug] = winner
        if official:
            self.official.add(slug)
        settled_any = False
        for s in self.strategies:
            res = self.brokers[s.name].settle(slug, winner, now)
            if res:
                settled_any = True
                self.store.log_settlement(res)
                log.info("[%s] settled %s -> %s%s, P&L $%.2f", s.name, slug, winner,
                         "" if official else " (provisional)", res.pnl)
                try:
                    s.on_settle(res)
                except Exception:
                    log.exception("Strategy %s crashed in on_settle", s.name)
        for name, b in list(self.retired.items()):
            res = b.settle(slug, winner, now)
            if res:
                self.store.log_settlement(res)
                log.info("[%s] (retired) settled %s -> %s, P&L $%.2f", name, slug, winner, res.pnl)
            if not b.positions:
                del self.retired[name]
                log.info("Retired strategy %s: all open bets settled", name)
        # Every round's result is saved (not only rounds someone traded): restarts rebuild recent results
        # from it, and it is the ground truth for studying / replaying the logged order books.
        self.store.log_resolution(slug, asset, window_end, winner, OFFICIAL if official else 0, now)
        if not official:
            self.provisional[slug] = [asset, window_end, now]
        self.marks.pop(slug, None)

    def _reconcile(self, now: float, max_lookups: int):
        """Re-check provisional results against Polymarket's official one; correct any that differ."""
        recheck = self._setting("PROVISIONAL_RECHECK_SECONDS", 60)
        give_up = self._setting("PROVISIONAL_GIVE_UP_SECONDS", 48 * 3600)
        held = self.store.settled_slugs(list(self.provisional)) if self.provisional else set()
        # rounds that strategies had money in are re-checked first
        for slug, entry in sorted(self.provisional.items(), key=lambda kv: (kv[0] not in held, kv[1][2])):
            if max_lookups <= 0:
                break
            asset, window_end, last = entry
            if now - last < recheck:
                continue
            max_lookups -= 1
            entry[2] = now
            winner, official = self._resolve(slug, window_end)
            if winner is None or not official:
                if now - window_end > give_up:
                    log.warning("%s was never officially closed; keeping the provisional result %s",
                                slug, self.winners.get(slug))
                    self.store.log_resolution(slug, asset, window_end, self.winners.get(slug),
                                              PROVISIONAL_GAVE_UP, now)
                    del self.provisional[slug]
                continue
            old = self.winners.get(slug)
            del self.provisional[slug]
            self.official.add(slug)
            self.store.log_resolution(slug, asset, window_end, winner, OFFICIAL, now)
            if old == winner:
                continue
            self.winners[slug] = winner
            if _slug_start(slug) in self.results.get(asset, {}):
                self.results[asset][_slug_start(slug)] = winner
            rows = self.store.correct_settlement(slug, winner)
            if not rows:
                log.warning("%s: official result is %s, not the provisional %s (no strategy held it).",
                            slug, winner, old)
                continue
            log.error("%s: official result is %s but it was settled as %s. Correcting the settlement.",
                      slug, winner, old)
            for r in rows:
                b = self.brokers.get(r["strategy"])
                if b is not None:
                    b.correct_settlement(r["old_payout"], r["new_payout"])
                log.error("[%s] %s re-settled -> %s, P&L now $%.2f", r["strategy"], slug, winner, r["pnl"])

    def _prune(self, now: float):
        """Drop finished rounds from the in-memory caches so a bot running for weeks stays small."""
        cutoff = now - KEEP_SECONDS
        keep = set(self.unresolved) | set(self.provisional)
        for d in (self.winners, self._winner_try, self._warned):
            for slug in [k for k in d if _slug_start(k) < cutoff and k not in keep]:
                del d[slug]
        self.official = {s for s in self.official if _slug_start(s) >= cutoff or s in keep}
        prune = getattr(self.feed, "prune", None)
        if prune is not None:
            prune(cutoff)
        if self.md is not None:
            self.md.prune(now - 600)
        self._reject_logged = {k: v for k, v in self._reject_logged.items() if now - v < 3600}
        self._stats_last = {k: v for k, v in self._stats_last.items() if _slug_start(k) >= cutoff}

    def _check_stop(self, name: str, broker: PaperBroker, now: float) -> bool:
        """
        True if this strategy has stopped trading. It stops for good the first time its portfolio value
        (cash + open bets at the mid) is at or below STOP_EQUITY. Open bets still settle normally.
        """
        if name in self.stopped:
            return True
        limit = self._setting("STOP_EQUITY", None)
        if limit is None:
            return False
        eq = broker.equity(self.marks)
        if eq > limit + 1e-9:
            return False
        reason = f"portfolio value ${eq:,.2f} <= stop level ${limit:,.2f}"
        self.stopped[name] = {"name": name, "stopped_at": now, "equity": eq, "reason": reason}
        self.store.mark_stopped(name, now, eq, reason)
        log.warning("[%s] STOPPED trading: %s. Open bets will still settle.", name, reason)
        return True

    def record_equity(self, now: float, force: bool = False):
        """
        Save each strategy's account value -- only when it changed since its last saved row, plus a heartbeat
        row every EQUITY_HEARTBEAT_SECONDS (60) so the dashboard can tell the bot is alive. Most strategies are
        flat most of the time: writing every row made this table ~25 MB/day, 83% of it exact repeats.
        """
        heartbeat = self._setting("EQUITY_HEARTBEAT_SECONDS", 60)
        for name, b in self.brokers.items():
            cash, open_value = b.cash, b.open_value(self.marks)
            last = self._equity_last.get(name)
            if (not force and last is not None and now - last[2] < heartbeat
                    and abs(cash - last[0]) < 1e-9 and abs(open_value - last[1]) < 1e-9):
                continue
            self._equity_last[name] = (cash, open_value, now)
            self.store.log_equity(now, name, cash, open_value)

    def _print_status(self):
        parts = []
        for name, b in self.brokers.items():
            eq = b.cash + b.open_value(self.marks)
            parts.append(f"{name}: ${eq:,.2f} ({(eq / b.bankroll - 1) * 100:+.1f}%)"
                         + (" STOPPED" if name in self.stopped else ""))
        log.info("Equity | " + " | ".join(parts))
