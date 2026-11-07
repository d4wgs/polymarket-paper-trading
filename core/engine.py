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

import logging
import time
from collections import defaultdict

from core.broker import PaperBroker
from core.fees import make_fee_model
from core.models import Snapshot, UP, DOWN, books_consistent
from core.storage import Storage
from strategies.base import Strategy, TickContext

log = logging.getLogger("engine")

OFFICIAL, PROVISIONAL_GAVE_UP = 1, -1      # values of resolutions.official (0 = provisional)
KEEP_SECONDS = 6 * 3600                    # how long finished rounds stay in the in-memory caches
RESULTS_KEPT = 50                          # recent results per market (ctx.prev_windows)


def _slug_start(slug: str) -> int:
    try:
        return int(slug.rsplit("-", 1)[1])
    except (IndexError, ValueError):
        return 0


class Engine:
    def __init__(self, cfg, strategies: list[Strategy], feed, storage: Storage,
                 clock=time.time, sleep=time.sleep):
        self.cfg, self.strategies, self.feed, self.store = cfg, strategies, feed, storage
        self.clock, self.sleep = clock, sleep
        self.brokers: dict[str, PaperBroker] = {}
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
        self._last_equity = self._last_settle = self._last_status = 0.0
        self.skipped_ticks = 0                                               # ticks dropped: inconsistent books
        self.stopped: dict[str, dict] = {}                                   # strategy -> stop record
        self._windows_logged: set[str] = set()                               # slugs whose price-to-beat is saved
        self._last_skip_log = 0.0

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
        self.stopped = self.store.stopped_strategies()
        for name in self.stopped:
            if name in self.brokers:
                log.warning("%s stopped trading earlier (portfolio fell to $%.2f); it stays stopped. Give it a "
                            "new name to start it again.", name, self.stopped[name]["equity"])
        self.store.set_meta("exchange", self._setting("EXCHANGE", "polymarket_intl"))
        self.store.set_meta("stop_equity", self._setting("STOP_EQUITY", 0.0))
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
        self.record_equity(self.clock())
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
        self.store.commit()

    def _tick_asset(self, mcfg: dict, now: float):
        asset = mcfg["asset"]
        info = self.feed.current_market(mcfg, now)
        if info is None:
            return
        up, down = self.feed.books(info)
        if up is None or down is None:
            return
        if not books_consistent(up, down):
            # the two books were captured at different moments: fetch again once, else skip the tick
            up, down = self.feed.books(info)
            if up is None or down is None or not books_consistent(up, down):
                self.skipped_ticks += 1
                if now - self._last_skip_log >= 60:
                    self._last_skip_log = now
                    log.info("%s: UP and DOWN books disagree (captured at different moments); skipped the "
                             "tick (%d skipped so far)", asset, self.skipped_ticks)
                return
        index_fn = getattr(self.feed, "index_price", None)
        index = None
        if index_fn is not None:
            try:
                index = index_fn(info, now)
            except Exception:
                log.exception("Index price lookup failed for %s", asset)
        now = self.clock()   # fresh time: fetching books takes a moment, and strategies act on seconds_left
        snap = Snapshot(ts=now, market=info, up=up, down=down, index_price=index)
        if getattr(info, "price_to_beat", None) is not None and info.slug not in self._windows_logged:
            self.store.log_window(info)
            self._windows_logged.add(info.slug)
        self.store.log_snapshot(snap)
        self.store.log_book(snap, self._setting("BOOK_LEVELS_LOGGED", 10))
        self.marks[info.slug] = (up.mid, down.mid)

        new_window = self.current_slug.get(asset) != info.slug
        if new_window:
            self.current_slug[asset] = info.slug
            self.history[asset] = []
            self.unresolved.setdefault(info.slug, (asset, info.window_end))
            for s in self.strategies:
                self.state[(s.name, asset)] = {}

        prev_winner = self._prev_winner(info, now)
        prev_windows = self._recent_results(info)
        for s in self.strategies:
            if not s.wants(asset):
                continue
            broker = self.brokers[s.name]
            if self._check_stop(s.name, broker, now):
                continue                                 # hit the stop level: no more trading, ever
            ctx = TickContext(snap=snap, position=broker.position(snap), cash=broker.cash,
                              equity=broker.equity(self.marks),
                              fee_theta=broker.current_theta(now, info),
                              history=list(self.history[asset]),
                              prev_windows=list(prev_windows),
                              prev_winner=prev_winner,
                              state=self.state[(s.name, asset)],
                              traded_this_window=info.slug in broker.traded_slugs)
            try:
                if new_window:
                    s.on_window_start(ctx)
                orders = s.on_tick(ctx) or []
            except Exception:
                log.exception("Strategy %s crashed on tick; skipping it this tick", s.name)
                orders = []
            books = {UP: up.copy(), DOWN: down.copy()}   # private copy so strategies don't share liquidity
            for order in orders:
                try:
                    fills = broker.execute_many(order, snap, books)
                except Exception:
                    log.exception("Strategy %s sent a bad order %s; skipped", s.name, order)
                    continue
                for fill in fills:
                    self.store.log_fill(fill)
                    log.info("[%s] %s %s %.2f sh @ %.3f ($%.2f, fee $%.2f, theta %s) %s",
                             s.name, fill.side, fill.outcome, fill.shares, fill.avg_price,
                             fill.usd, fill.fee, fill.theta, fill.reason)
            self._check_stop(s.name, broker, now)
            pos = broker.positions.get(info.slug)
            if pos and pos.is_empty():
                broker.positions.pop(info.slug, None)   # don't keep empty positions around

        self.history[asset].append(snap)

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
        held = {slug for b in self.brokers.values() for slug in b.positions}
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

    def record_equity(self, now: float):
        for name, b in self.brokers.items():
            self.store.log_equity(now, name, b.cash, b.open_value(self.marks))

    def _print_status(self):
        parts = []
        for name, b in self.brokers.items():
            eq = b.cash + b.open_value(self.marks)
            parts.append(f"{name}: ${eq:,.2f} ({(eq / b.bankroll - 1) * 100:+.1f}%)"
                         + (" STOPPED" if name in self.stopped else ""))
        log.info("Equity | " + " | ".join(parts))
