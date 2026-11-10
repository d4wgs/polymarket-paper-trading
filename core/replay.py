"""
Replay: feed RECORDED market data through the real engine, strategies and paper broker.

Nothing here changes how the live bot runs. The engine already takes an injectable clock/sleep
(Engine(clock=..., sleep=...), the same hook tools/simulate.py uses); replay supplies

  ReplayClock   a clock that only moves when the recording says so
  ReplayFeed    the `feed` interface (current_market / books / index_price / resolution_detail) read from the
                recorded `books`, `snapshots`, `windows` and `resolutions` tables
  ReplaySpot    the spot feed the fair-value model reads, fed from the recorded spot (and sigma)
  ReplayStorage a Storage that doesn't re-write the (huge) snapshots/books logs
  WsMakerBook   OPTIONAL: maker fills from recorded websocket trade prints (data/ws_events.db) instead of
                the conservative book-trade-through rule

The source database is opened READ-ONLY (sqlite URI mode=ro); results go to their own file (data/replay.db).

How a recorded tick is replayed: the bot logged one (snapshot, book) row per market per poll, stamped with
its clock after the fetch. Replay groups those rows into ticks (a market appears once per tick), sets the clock
to each row's timestamp as the engine "fetches" it, and lets the engine do everything else.

  WsBookReader  OPTIONAL (latency_book="ws"): the order-latency book rebuilt from the recorded websocket book
                snapshots (data/ws_events.db) as of decision time + latency

Known approximations (they are why the self-check exists; see tools/replay.py):
  * the live bot fetched a FRESH book `latency` (250 ms) after each taker decision. A recording only has a book
    every ~2 s, so the follow-up book is the next recorded one (`latency_book="next"`) or the decision book
    itself (`"same"`).
  * spot history is the recorded per-tick spot (about 1 per second), not the exchange's per-second last trade.
  * volatility: by default the recorded sigma is replayed exactly (sigma_mode="recorded"), otherwise it is
    re-estimated from the replayed spot ("ewma").
"""
from __future__ import annotations

import bisect
import dataclasses
import json
import sqlite3
import types
from dataclasses import dataclass
from typing import Optional

from core import validity
from core.broker import PaperBroker  # noqa: F401  (documented dependency: the real broker is used unchanged)
from core.engine import Engine
from core.fairvalue import MarketData
from core.maker import MakerBook
from core.models import Book, MarketInfo
from core.spot import SpotFeed
from core.storage import Storage


class ReplayClock:
    def __init__(self, start: float = 0.0):
        self.t = start

    def now(self) -> float:
        return self.t

    def sleep(self, dt: float):
        self.t += max(dt, 0.0)


@dataclass
class Ev:
    rowid: int
    ts: float
    asset: str
    slug: str
    up_bids: list
    up_asks: list
    down_bids: list
    down_asks: list
    spot: Optional[float]
    sigma: Optional[float]
    index: Optional[float]
    ptb: Optional[float]

    def books(self) -> tuple[Book, Book]:
        return (Book(bids=[tuple(x) for x in self.up_bids], asks=[tuple(x) for x in self.up_asks]),
                Book(bids=[tuple(x) for x in self.down_bids], asks=[tuple(x) for x in self.down_asks]))


EV_SQL = ("SELECT b.rowid, b.ts, b.asset, b.slug, b.up_bids, b.up_asks, b.down_bids, b.down_asks, "
          "s.spot, s.sigma, s.index_price, s.price_to_beat FROM books b "
          "LEFT JOIN snapshots s ON s.asset = b.asset AND s.ts = b.ts")


def _ev(r) -> Ev:
    return Ev(r[0], r[1], r[2], r[3], json.loads(r[4]), json.loads(r[5]), json.loads(r[6]), json.loads(r[7]),
              r[8], r[9], r[10], r[11])


class Source:
    """Read-only view of a recorded paper_us.db."""

    def __init__(self, path: str):
        self.path = path
        self.db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        self.windows = {r[0]: r[1:] for r in self.db.execute(
            "SELECT slug, asset, window_start, window_end, exchange_slug, price_to_beat FROM windows")}
        self.resolutions = {r[0]: (r[1], r[2]) for r in self.db.execute(
            "SELECT slug, winner, official FROM resolutions WHERE winner IN ('UP','DOWN')")}

    def span(self) -> tuple[float, float]:
        a, b = self.db.execute("SELECT MIN(ts), MAX(ts) FROM books").fetchone()
        return a, b

    def events(self, t0: float, t1: float):
        """Recorded ticks with t0 <= ts < t1, oldest first, as a streaming iterator."""
        cur = self.db.execute(EV_SQL + " WHERE b.ts >= ? AND b.ts < ? ORDER BY b.ts", (t0, t1))
        for r in cur:
            yield _ev(r)

    def next_event(self, asset: str, ts: float) -> Optional[Ev]:
        r = self.db.execute(EV_SQL + " WHERE b.asset = ? AND b.ts >= ? ORDER BY b.ts LIMIT 1", (asset, ts)).fetchone()
        return _ev(r) if r else None

    def copy_resolutions(self, out: sqlite3.Connection, t0: float, t1: float):
        """Seed the replay DB with the results of rounds that ended before the replay starts, so
        ctx.prev_winner / prev_windows work from the first tick (the live bot restores them the same way)."""
        rows = self.db.execute("SELECT slug, asset, window_end, winner, official, ts FROM resolutions "
                               "WHERE window_end >= ? AND window_end < ?", (t0, t1)).fetchall()
        out.executemany("INSERT OR REPLACE INTO resolutions VALUES (?,?,?,?,?,?)", rows)
        out.commit()


    def copy_state(self, out: sqlite3.Connection, t0: float, names: Optional[list] = None) -> dict:
        """
        Warm start: copy what the live bot had done BEFORE t0 (fills, settlements, stop records) into the
        replay DB, so Engine.setup() restores each strategy's real cash, open positions and stopped state
        exactly like a live restart does. Without it every strategy starts fresh at its bankroll, and
        strategies that had already lost money (or been stopped) behave differently from the live run.
        """
        out_cols = lambda t: [r[1] for r in out.execute(f"PRAGMA table_info({t})")]
        n = {}
        for table, tscol in (("fills", "ts"), ("settlements", "ts")):
            src_cols = {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}
            cols = [c for c in out_cols(table) if c in src_cols]
            q = f"SELECT {','.join(cols)} FROM {table} WHERE {tscol} < ?"
            args = [t0]
            if names:
                q += f" AND strategy IN ({','.join('?' * len(names))})"
                args += names
            rows = self.db.execute(q, args).fetchall()
            out.executemany(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", rows)
            n[table] = len(rows)
        q = "SELECT name, stopped_at, equity, reason FROM strategy_status WHERE stopped_at < ?"
        args = [t0]
        if names:
            q += f" AND name IN ({','.join('?' * len(names))})"
            args += names
        rows = self.db.execute(q, args).fetchall()
        out.executemany("INSERT OR REPLACE INTO strategy_status VALUES (?,?,?,?)", rows)
        n["stopped"] = len(rows)
        out.commit()
        return n


class WsBookReader:
    """
    The book as of an arbitrary time T, from the recorder's `ws_book` table (read-only handle).

    What the recorder learned (core/ws_store.py): MARKET_DATA messages are FULL SNAPSHOTS, not deltas, so there is
    nothing to apply: the book at T is simply the newest stored snapshot with recv_ts <= T (a snapshot whose kept
    content did not change is not stored again, and rows are throttled to one per market per `book_min_interval_ms`,
    so the state can be up to that interval behind). Only the top `book_levels` levels are kept, like the bot's own
    `books` table. ws_book is the YES (UP) instrument; the DOWN book is its mirror: DOWN bids = 1 - UP offers,
    DOWN asks = 1 - UP bids.

    A latency book is refused (the caller falls back to the decision-time book) when
      * no snapshot exists at or before T for the market      ("no_row")
      * the newest one is older than `max_age` seconds         ("stale")
      * a recorder gap overlaps [snapshot time, T]: from a `disconnect`/`stop` marker to the next `connect`, or a
        `bad_json` marker (a lost message)                     ("gap")
      * the book is crossed, has a price outside (0, 1), or both sides are empty   ("invalid")
    """

    def __init__(self, ws_path: str, max_age: float = 60.0):
        self.db = sqlite3.connect(f"file:{ws_path}?mode=ro", uri=True)
        self.max_age = max_age
        self.gaps = self._load_gaps()                       # [(start, end)], end = inf while still down
        self.first_ts = (self.db.execute("SELECT MIN(recv_ts) FROM ws_book").fetchone() or [None])[0]
        self.last_ts = (self.db.execute("SELECT MAX(recv_ts) FROM ws_book").fetchone() or [None])[0]

    def _load_gaps(self) -> list:
        rows = self.db.execute("SELECT ts, kind FROM ws_gaps ORDER BY ts").fetchall()
        gaps, down_since = [], None
        for ts, kind in rows:
            if kind in ("disconnect", "stop"):
                if down_since is None:
                    down_since = ts
            elif kind == "connect":
                if down_since is not None:
                    gaps.append((down_since, ts))
                    down_since = None
            elif kind == "bad_json":
                gaps.append((ts, ts))
        if down_since is not None:
            gaps.append((down_since, float("inf")))
        return gaps

    def _gap_between(self, a: float, b: float) -> bool:
        return any(g0 <= b and a <= g1 for g0, g1 in self.gaps)

    def book_at(self, exch_slug: str, t: float):
        """(up_book, down_book, age, None) or (None, None, None, reason)."""
        r = self.db.execute("SELECT recv_ts, bids, offers FROM ws_book WHERE slug = ? AND recv_ts <= ? "
                            "ORDER BY recv_ts DESC LIMIT 1", (exch_slug, t)).fetchone()
        if r is None:
            return None, None, None, "no_row"
        ts, bids_j, offers_j = r
        if t - ts > self.max_age:
            return None, None, None, "stale"
        if self._gap_between(ts, t):
            return None, None, None, "gap"
        bids = [(float(p), float(q)) for p, q in json.loads(bids_j or "[]") if q and q > 0]
        offers = [(float(p), float(q)) for p, q in json.loads(offers_j or "[]") if q and q > 0]
        if not bids and not offers:
            return None, None, None, "invalid"
        if any(not (0.0 < p < 1.0) for p, _ in bids + offers):
            return None, None, None, "invalid"
        if bids and offers and bids[0][0] >= offers[0][0]:
            return None, None, None, "invalid"
        up = Book(bids=bids, asks=offers)
        mirror = lambda side: [(round(1.0 - p, 4), q) for p, q in side]
        down = Book(bids=mirror(offers), asks=mirror(bids))
        return up, down, t - ts, None


class ReplayFeed:
    """The engine's `feed`, answering from the recording."""

    def __init__(self, src: Source, clock: ReplayClock, markets: list[dict], latency_book: str = "same",
                 ws_books: Optional["WsBookReader"] = None):
        if latency_book == "ws" and ws_books is None:
            raise ValueError('latency_book="ws" needs the websocket recording (ws_book_path)')
        self.src, self.clock, self.latency_book, self.ws_books = src, clock, latency_book, ws_books
        self.latency_log: list[tuple] = []     # (ts, slug, source, reason, age): one row per order-latency fetch
        self.by_asset = {m["asset"]: m for m in markets}
        self.pending: dict[str, Ev] = {}       # asset -> the recorded tick being replayed
        self.last_event: Optional[Ev] = None   # what the spot feed reads
        self._infos: dict[str, MarketInfo] = {}

    # -- tick control (used by the runner) ----------------------------------------------------------
    def set_tick(self, events: list[Ev]):
        self.pending = {e.asset: e for e in events}

    # -- feed interface -------------------------------------------------------------------------------
    def current_market(self, cfg: dict, now: float) -> Optional[MarketInfo]:
        ev = self.pending.get(cfg["asset"])
        if ev is None:
            return None
        info = self._infos.get(ev.slug)
        if info is None:
            info = self._infos[ev.slug] = self._make_info(ev, cfg)
        if ev.ptb is not None:
            info.price_to_beat = ev.ptb
        return info

    def _make_info(self, ev: Ev, cfg: dict) -> MarketInfo:
        w = self.src.windows.get(ev.slug)
        start = int(ev.slug.rsplit("-", 1)[1])
        W = int(cfg.get("window_seconds", 900))
        if w:
            _, ws, we, exch, _ = w
            exch = exch or ev.slug
        else:
            ws, we, exch = start, start + W, ev.slug
        return MarketInfo(asset=cfg["asset"], slug=ev.slug, window_start=ws, window_end=we,
                          token_up=exch, token_down=exch, question="(replay)", fee_rate=0.0,
                          single_instrument=True, qty_step=0.01, min_notional=0.0, exchange_slug=exch,
                          price_to_beat=None)

    def books(self, info: MarketInfo):
        ev = self.pending.pop(info.asset, None)
        if ev is not None:                       # the tick's own fetch: the clock moves to when it was recorded
            self.clock.t = max(self.clock.t, ev.ts)
        else:                                    # a follow-up fetch (after the order latency)
            ev = self._follow_up(info)
        self.last_event = ev
        return ev.books()

    def _follow_up(self, info: MarketInfo) -> Ev:
        base = self.last_event
        if self.latency_book == "ws":
            w = self.src.windows.get(info.slug)
            exch = (w[3] if w and w[3] else info.slug)
            up, down, age, why = self.ws_books.book_at(exch, self.clock.t)    # clock = decision time + latency
            if up is not None:
                self.latency_log.append((self.clock.t, info.slug, "ws", None, age))
                return dataclasses.replace(base, up_bids=[list(x) for x in up.bids], up_asks=[list(x) for x in up.asks],
                                           down_bids=[list(x) for x in down.bids], down_asks=[list(x) for x in down.asks])
            self.latency_log.append((self.clock.t, info.slug, "no-latency-book", why, None))
            return base                                      # fall back to the decision-time book
        if self.latency_book == "next":
            nxt = self.src.next_event(info.asset, self.clock.t)
            if nxt is not None and nxt.slug == info.slug:
                return nxt
        return base

    def index_price(self, info: MarketInfo, now: float):
        return self.last_event.index if self.last_event is not None else None

    def resolution_detail(self, slug: str, window_end: Optional[float] = None):
        """Official results from the recording, visible a couple of seconds after the round ends."""
        res = self.src.resolutions.get(slug)
        if res is None:
            return None, False
        start = int(slug.rsplit("-", 1)[1])
        end = window_end if window_end else start
        if self.clock.t < end + 2:
            return None, False
        return res[0], True

    def resolution(self, slug: str, window_end: Optional[float] = None):
        return self.resolution_detail(slug, window_end)[0]

    def prune(self, before_ts: float):
        for k in [k for k in self._infos if int(k.rsplit("-", 1)[1]) < before_ts]:
            del self._infos[k]


class ReplaySpot(SpotFeed):
    """Spot feed for the fair-value model, fed from the recorded spot of the tick being replayed."""

    def __init__(self, feed: ReplayFeed, half_life: float = 300.0, sigma_mode: str = "recorded",
                 sigma_multiplier: float = 1.0, min_samples: int = 30):
        super().__init__("coinbase", half_life=half_life, bootstrap=False, min_samples=min_samples)
        self.feed, self.sigma_mode, self.mult = feed, sigma_mode, sigma_multiplier or 1.0
        self._cur_spot: Optional[float] = None
        self._cur_sigma: Optional[float] = None

    def start(self):
        pass

    def poll(self, now: float):
        ev = self.feed.last_event
        self._cur_spot = ev.spot if ev is not None else None
        self._cur_sigma = (ev.sigma / self.mult) if ev is not None and ev.sigma else None
        if self._cur_spot:
            self.on_price(now, self._cur_spot)

    def price(self, now: float):
        return self._cur_spot

    def sigma(self):
        if self.sigma_mode == "recorded":
            return self._cur_sigma
        return self.vol.sigma


class ReplayStorage(Storage):
    """Like Storage, but the market-data logs (snapshots, books, stats) are not re-written.
    keep_snapshots=True keeps the snapshot rows (to compare the replayed model with the recorded one)."""

    keep_snapshots = False

    def log_snapshot(self, s):
        if self.keep_snapshots:
            super().log_snapshot(s)

    def log_book(self, s, depth: int = 10):
        pass

    def log_stats(self, *a, **k):
        pass


class WsMakerBook(MakerBook):
    """
    Maker fills from recorded websocket trade PRINTS instead of book trade-through.

    A resting bid is filled only by prints strictly THROUGH its price (an UP bid at p: prints at YES price < p;
    a DOWN bid at d: prints at YES price > 1 - d), counted since the quote went live; the print's size is the
    volume. A print at a better price than our bid means our bid would have been hit first, so no queue charge
    applies. Touching (print == our price) never fills, same as the default rule.
    """

    def __init__(self, ws_path: str, exchange_slug_of, **kw):
        super().__init__(**kw)
        self.ws = sqlite3.connect(f"file:{ws_path}?mode=ro", uri=True)
        self.exch = exchange_slug_of
        self._seen: dict[tuple, float] = {}            # quote key -> last trade_ts counted

    def check(self, strategy, slug, books, now):
        out = []
        exch = self.exch(slug)
        for outcome, q in list(self.for_market(strategy, slug).items()):
            if now < q.live_ts:
                continue
            key = (strategy, slug, outcome)
            since = self._seen.get(key, q.live_ts)
            self._seen[key] = now
            if outcome == "UP":
                cond, arg = "price < ?", q.price - 1e-9
            else:
                cond, arg = "price > ?", 1.0 - q.price + 1e-9
            (through,) = self.ws.execute(
                f"SELECT COALESCE(SUM(qty), 0) FROM ws_trades WHERE slug = ? AND trade_ts > ? AND trade_ts <= ? "
                f"AND {cond}", (exch, since, now, arg)).fetchone()
            take = min(q.remaining, through)
            take = int(take * 100) / 100.0
            if take <= 0:
                continue
            q.filled += take
            out.append((q, take))
            if q.remaining <= 1e-9:
                del self.quotes[key]
        return out


def group_ticks(events):
    """Group a time-ordered stream of recorded rows into ticks: a market appears at most once per tick."""
    group, seen = [], set()
    for e in events:
        if e.asset in seen:
            yield group
            group, seen = [], set()
        group.append(e)
        seen.add(e.asset)
    if group:
        yield group


def make_strategies(config_module):
    """Fresh strategy objects (replay never shares state with the live ones)."""
    return [type(s)(s.name, assets=s.assets, bankroll=s.bankroll, title=s.title, description=s.description,
                    details=s.details, **s.p) for s in config_module.STRATEGIES]


def run_replay(config_module, src_path: str, out_path: str, t0: float, t1: float, *, only: Optional[list] = None,
               ws_path: Optional[str] = None, sigma_mode: str = "recorded", latency_book: str = "same",
               progress=None, keep_snapshots: bool = False, warm_start: bool = True,
               ws_book_path: Optional[str] = None, latency_ms: Optional[float] = None,
               ws_book_max_age: float = 60.0, extra_slippage: Optional[float] = None) -> Engine:
    """Replay recorded ticks in [t0, t1) into `out_path`. The source is opened read-only."""
    import os
    if os.path.abspath(out_path) == os.path.abspath(src_path):
        raise ValueError("replay output must not be the source database")
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(out_path + suffix):
            os.remove(out_path + suffix)
    cfg = types.SimpleNamespace(**{k: getattr(config_module, k) for k in dir(config_module) if k.isupper()})
    cfg.DB_PATH = out_path
    cfg.EXPORT_DIR = None
    cfg.HEALTH = {"enabled": False}                 # a replay is not a bot run: no heartbeat rows, no clock readings
    if latency_ms is not None:                      # replay-only override (calibration sweep); the live bot is untouched
        cfg.LATENCY_SECONDS = float(latency_ms) / 1000.0
    if extra_slippage:                              # replay-only pessimism: added to fills.extra_slippage (per share)
        cfg.EXTRA_SLIPPAGE = float(getattr(cfg, "EXTRA_SLIPPAGE", 0.0) or 0.0) + float(extra_slippage)
    src = Source(src_path)
    clock = ReplayClock(t0)
    ws_books = WsBookReader(ws_book_path, ws_book_max_age) if latency_book == "ws" and ws_book_path else None
    feed = ReplayFeed(src, clock, cfg.MARKETS, latency_book, ws_books)
    strategies = make_strategies(config_module)
    if only:
        strategies = [s for s in strategies if s.name in only]
        missing = set(only) - {s.name for s in strategies}
        if missing:
            raise ValueError(f"unknown strategy name(s): {sorted(missing)}")
    mult = (cfg.MODEL or {}).get("sigma_multiplier", 1.0)
    spot = ReplaySpot(feed, half_life=cfg.MODEL.get("vol_half_life_seconds", 300), sigma_mode=sigma_mode,
                      sigma_multiplier=mult, min_samples=cfg.MODEL.get("vol_min_samples", 30))
    md = MarketData(spot, cfg.MODEL)
    store = ReplayStorage(out_path)
    store.keep_snapshots = keep_snapshots
    src.copy_resolutions(store.db, t0 - 6 * 3600, t0)
    if warm_start:
        # restrict to the strategies being replayed (all configured ones when `only` is not given)
        engine_names = [s.name for s in strategies]
        src.copy_state(store.db, t0, engine_names)
    engine = Engine(cfg, strategies, feed, store, clock=clock.now, sleep=clock.sleep, market_data=md)
    # Round validity: the replay output has no snapshots of its own, so the verdicts are taken from the recording
    # (its own round_validity rows where it has them, otherwise computed from its snapshots; the source stays
    # read-only) and the engine does not assess rounds itself.
    engine.assess_validity = False
    engine.validity_counts = validity.backfill(src.db, store.db, getattr(cfg, "POLL_SECONDS", 2),
                                               getattr(cfg, "VALIDITY", None), now=t1, since=t0 - 6 * 3600,
                                               until=t1 + 3600, source="replay")
    if ws_path:
        def exch_of(slug):
            w = src.windows.get(slug)
            return (w[3] if w and w[3] else slug)
        engine.maker = WsMakerBook(ws_path, exch_of, latency=engine.latency,
                                   conservative_queue=(getattr(cfg, "MAKER", {}) or {}).get("conservative_queue", True))
    engine.setup()
    n = 0
    for group in group_ticks(src.events(t0, t1)):
        clock.t = group[0].ts
        feed.set_tick(group)
        engine.tick(clock.t)
        n += 1
        if progress and n % 5000 == 0:
            progress(n, clock.t)
    clock.t = max(clock.t, t1) + 3600            # let the last rounds settle
    engine.settle_finished(clock.t)
    engine.record_equity(clock.t, force=True)
    engine.store.commit()
    if latency_book == "ws":                      # which latency fetches hit a websocket book and which fell back
        engine.store.db.execute("CREATE TABLE IF NOT EXISTS latency_books (ts REAL, slug TEXT, source TEXT, "
                                "reason TEXT, age REAL)")
        engine.store.db.executemany("INSERT INTO latency_books VALUES (?,?,?,?,?)", feed.latency_log)
        engine.store.commit()
    engine.latency_log = feed.latency_log
    engine.store.db.close()                       # results are complete; callers read the file
    src.db.close()
    if ws_books is not None:
        ws_books.db.close()
    engine.ticks_replayed = n
    return engine


def resolve_profile(raw_config: dict, name: Optional[str], ws_db: Optional[str] = None,
                    latency_ms: Optional[float] = None, latency_book: Optional[str] = None) -> dict:
    """
    A named replay profile from config.yaml (`replay: profiles:`), e.g. `pessimistic`: +1c extra slippage per share
    and a 1000 ms order latency. Explicit command-line values win over the profile's.
    latency_book "auto": the websocket latency book when a recording is available (the latency then really moves
    the fill to a later book), otherwise "next" = the next recorded book (~2 s later: stricter than 1000 ms).
    Returns {extra_slippage, latency_ms, latency_book, ws_db, note}.
    """
    import os
    out = {"extra_slippage": None, "latency_ms": latency_ms, "latency_book": latency_book, "ws_db": ws_db, "note": ""}
    if not name:
        return out
    profiles = ((raw_config or {}).get("replay") or {}).get("profiles") or {}
    if name not in profiles:
        raise ValueError(f"unknown replay profile {name!r} (config.yaml replay.profiles has: {sorted(profiles)})")
    p = profiles[name] or {}
    out["extra_slippage"] = float(p.get("extra_slippage", 0.0) or 0.0)
    if latency_ms is None and p.get("latency_ms") is not None:
        out["latency_ms"] = float(p["latency_ms"])
    book = latency_book or p.get("latency_book", "auto")
    if book == "auto":
        cand = ws_db or "data/ws_events.db"
        if os.path.exists(cand):
            book, out["ws_db"] = "ws", cand
        else:
            book = "next"
    out["latency_book"] = book
    how = {"ws": f"the websocket book {out['latency_ms']:g} ms after the decision",
           "next": "the NEXT recorded book (~2 s after the decision; no websocket recording was given)",
           "same": "the decision-time book (the latency does not change the book: weaker test)"}[book]
    out["note"] = (f"profile {name}: +{out['extra_slippage'] * 100:g}c extra slippage per share, order latency "
                   f"{out['latency_ms']:g} ms, fills against {how}")
    return out
