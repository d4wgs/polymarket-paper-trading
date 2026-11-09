"""
Fake-market simulator. Runs your real strategies + engine + broker against
synthetic 15-minute markets, compressed in time (hours run in seconds).

    python tools/simulate.py              # 24 simulated hours -> data/sim.db
    python tools/simulate.py --hours 72 --seed 7

Use it to: check a new strategy doesn't crash, sanity-check the accounting,
and see the dashboard working on a weekend. The P&L here means NOTHING
about real markets -- the prices are invented.
"""
import argparse
import logging
import math
import os
import random
import sys
import types

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
os.chdir(HERE)

import config
from core.engine import Engine
from core.fairvalue import MarketData
from core.spot import SimSpot
from core.models import Book, MarketInfo, UP, DOWN
from core.polymarket import window_start_for
from core.storage import Storage


def norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


class SimClock:
    def __init__(self, start):
        self.t = start

    def now(self):
        return self.t

    def sleep(self, dt):
        self.t += max(dt, 0.001)


class SimFeed:
    """
    One random-walk spot price per UNDERLYING (BTC-15m and BTC-5m share the same Bitcoin price);
    market prices follow the 'fair' odds with noise and lag.

    Like the real books: once a round is all but decided, nobody sells the winning side (no asks
    at all) and nobody buys the losing side (no bids). Strategies must cope with that.
    """

    def __init__(self, clock, window_seconds=900, seed=1, annual_vol=0.50, us=False):
        self.clock, self.W = clock, window_seconds
        self.us = us          # Polymarket US: one YES book, DOWN is its mirror; 0.01-contract steps
        self.rng = random.Random(seed)
        self.sig_s = annual_vol / math.sqrt(365 * 24 * 3600)      # vol per sqrt(second)
        self.spot, self.last_t = {}, {}                            # underlying -> price / last update
        self.open_px, self.close_px = {}, {}                       # "<asset>-<start>" -> price
        self.bias = {}                                             # slug -> slow-moving mispricing
        self.windows = {}                                          # underlying -> {asset: window length}
        self.under = {}                                            # asset -> underlying

    @staticmethod
    def underlying_of(cfg):
        return cfg.get("underlying") or cfg["asset"].split("-")[0]

    def _advance(self, u, now):
        Ws = self.windows[u]
        t = self.last_t.get(u, now)
        s = self.spot.get(u, 100.0)
        while t < now:
            nxt_boundary = min(window_start_for(t, W) + W for W in Ws.values())
            step_to = min(now, nxt_boundary)
            dt = step_to - t
            s *= math.exp(self.sig_s * math.sqrt(dt) * self.rng.gauss(0, 1) - 0.5 * self.sig_s ** 2 * dt)
            t = step_to
            if t == nxt_boundary:
                for asset, W in Ws.items():
                    if int(t) % W == 0:
                        self.close_px[f"{asset}-{int(t - W)}"] = s
                        self.open_px[f"{asset}-{int(t)}"] = s
        self.spot[u], self.last_t[u] = s, now
        return s

    def current_market(self, cfg, now):
        asset = cfg["asset"]
        u = self.under[asset] = self.underlying_of(cfg)
        W = self.windows.setdefault(u, {})[asset] = int(cfg.get("window_seconds", self.W))
        start = window_start_for(now, W)
        self._advance(u, now)
        self.open_px.setdefault(f"{asset}-{start}", self.spot[u])
        extra = dict(single_instrument=True, qty_step=0.01, min_notional=0.0,
                     price_to_beat=self.open_px[f"{asset}-{start}"]) if self.us else {}
        return MarketInfo(asset=asset, slug=f"{cfg['slug_prefix']}-{start}", window_start=start,
                          window_end=start + W, token_up=f"{asset}-{start}-up",
                          token_down=f"{asset}-{start}-down", question=f"{asset} Up or Down (sim)",
                          fee_rate=cfg.get("fee_rate", 0.0), **extra)

    def books(self, info):
        now = self.clock.now()
        s = self._advance(self.under[info.asset], now)
        key = f"{info.asset}-{int(info.window_start)}"
        s0 = self.open_px[key]
        tau = max(info.window_end - now, 1.0)
        fair = norm_cdf(math.log(s / s0) / (self.sig_s * math.sqrt(tau)))
        b = self.bias.get(key, 0.0) * 0.97 + self.rng.gauss(0, 0.01)
        self.bias[key] = b
        mid = min(0.998, max(0.002, fair + b))
        if self.us:
            up = self._book(mid)
            down = Book(bids=[(round(1 - p, 2), q) for p, q in up.asks], asks=[(round(1 - p, 2), q) for p, q in up.bids])
            return up, down
        return self._book(mid), self._book(1 - mid)

    def index_price(self, info, now):
        return self.spot.get(self.under.get(info.asset))

    def _book(self, mid):
        """5 levels a side on a 1c grid. Levels outside 0.01-0.99 don't exist (that's how a
        decided round ends up with no asks on the winner and no bids on the loser)."""
        half = self.rng.choice([0.005, 0.01, 0.01, 0.015])
        bid0 = round(mid - half, 2)
        ask0 = max(round(mid + half, 2), round(bid0 + 0.01, 2))
        bids, asks = [], []
        for i in range(5):
            pb, pa = round(bid0 - i * 0.01, 2), round(ask0 + i * 0.01, 2)
            if 0.01 <= pb <= 0.99:
                bids.append((pb, self.rng.uniform(20, 400)))
            if 0.01 <= pa <= 0.99:
                asks.append((pa, self.rng.uniform(20, 400)))
        return Book(bids=bids, asks=asks)

    def resolution(self, slug, window_end=None):
        prefix, start = slug.rsplit("-", 1)
        asset = next((m["asset"] for m in config.MARKETS if m["slug_prefix"] == prefix), None)
        key = f"{asset}-{start}"
        if key not in self.close_px:
            return None
        return UP if self.close_px[key] >= self.open_px[key] else DOWN


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--db", default="data/sim_us.db" if getattr(config, "EXCHANGE", "") == "polymarket_us"
                    else "data/sim.db")
    args = ap.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    if os.path.exists(args.db):
        os.remove(args.db)                     # each sim run starts clean

    cfg = types.SimpleNamespace(**{k: getattr(config, k) for k in dir(config) if k.isupper()})
    cfg.DB_PATH = args.db
    cfg.EXPORT_DIR = None                     # simulated days must not land in the live CSV folder
    clock = SimClock(start=window_start_for(1_790_000_000, 900))
    feed = SimFeed(clock, cfg.WINDOW_SECONDS, seed=args.seed, us=getattr(cfg, "EXCHANGE", "") == "polymarket_us")
    # fresh strategy objects so a sim never shares state with a live run
    strategies = [type(s)(s.name, assets=s.assets, bankroll=s.bankroll, title=s.title,
                          description=s.description, details=s.details, **s.p) for s in config.STRATEGIES]
    under = SimFeed.underlying_of(config.MARKETS[0])

    def spot_price(now):
        feed.windows.setdefault(under, {m["asset"]: m["window_seconds"] for m in config.MARKETS})
        return feed._advance(under, now)
    spot = SimSpot(spot_price, half_life=config.MODEL.get("vol_half_life_seconds", 300), seed=args.seed)
    spot.vol.seed(feed.sig_s)                 # the sim's true vol, so the model works from the first tick
    md = MarketData(spot, config.MODEL)
    engine = Engine(cfg, strategies, feed, Storage(args.db), clock=clock.now, sleep=clock.sleep, market_data=md)
    engine.run(max_seconds=args.hours * 3600 + 120)
    engine.settle_finished(clock.now() + 3600)
    engine.record_equity(clock.now(), force=True)
    engine.store.commit()

    print(f"\nSimulated {args.hours:g}h -> {args.db}")
    for name, b in engine.brokers.items():
        eq = b.cash + b.open_value(engine.marks)
        print(f"  {name:22} equity ${eq:10,.2f}  ({(eq / b.bankroll - 1) * 100:+.2f}%)")
    print("\nOpen the dashboard with:  streamlit run dashboard.py   (pick the simulator in the sidebar)")


if __name__ == "__main__":
    main()
