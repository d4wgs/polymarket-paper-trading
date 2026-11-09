"""
Loads config.yaml (THE file you edit) and exposes it as the module-level settings the engine,
dashboard and tools read (EXCHANGE, MARKETS, STRATEGIES, ...). Nothing here needs editing.

Another file can be used with the environment variable PAPER_CONFIG=path/to/other.yaml.
"""
from __future__ import annotations

import inspect
import logging
import os

import yaml

from strategies.s1_fair_value import FairValueTrader
from strategies.s2_market_maker import MarketMaker
from strategies.s3_sniper import Sniper
from strategies.s4_momentum import CrowdMomentum, MinuteBuckets
from strategies.s5_complete_set import CompleteSetArb
from strategies.s6_mean_reversion import ShockReversion
from strategies.s7_nested_pair import NestedPair

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.environ.get("PAPER_CONFIG") or os.path.join(HERE, "config.yaml")

# config.yaml strategy key -> class   (add a new strategy module here once)
REGISTRY = {
    "s1_fair_value": FairValueTrader,
    "s2_market_maker": MarketMaker,
    "s3_sniper": Sniper,
    "s4_momentum": CrowdMomentum,
    "s4b_minute_buckets": MinuteBuckets,      # configured as s4_momentum.variant_4b
    "s5_complete_set": CompleteSetArb,
    "s6_mean_reversion": ShockReversion,
    "s7_nested_pair": NestedPair,
}

WINDOW_LENGTHS = {"5m": 300, "15m": 900, "1h": 3600}


def load(path: str = CONFIG_PATH) -> dict:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


RAW = load()
_eng = RAW.get("engine", {}) or {}
_fees = RAW.get("fees", {}) or {}
_fills = RAW.get("fills", {}) or {}
_port = RAW.get("portfolio", {}) or {}

EXCHANGE = RAW.get("exchange", "polymarket_us")

# ---------------------------------------------------------------- markets (asset is a config value)
MARKETS = []
ASSETS = RAW.get("assets") or []
for a in ASSETS:
    for w in a.get("windows", ["5m", "15m"]):
        if w not in WINDOW_LENGTHS:
            raise ValueError(f"config.yaml: unknown window {w!r} (use one of {sorted(WINDOW_LENGTHS)})")
        MARKETS.append({
            "asset": f"{a['symbol']}-{w}",
            "symbol": a["symbol"],
            "kind": w,
            "slug_prefix": a["slug_prefix"].format(window=w, symbol=a["symbol"].lower()),
            "window_seconds": WINDOW_LENGTHS[w],
            "index_asset": a.get("index_asset"),
            "fee_rate": a.get("fee_rate", 0.07),
            "name": f"{a['symbol']} · {w} rounds",
        })
if len({a["symbol"] for a in ASSETS}) > 1:
    logging.getLogger("config").warning("Several assets configured: the spot feed / volatility model "
                                        "currently follows only the first one (%s).", ASSETS[0]["symbol"])

# ---------------------------------------------------------------- shared components
SPOT = {**(RAW.get("spot") or {}),
        "product": ASSETS[0].get("spot_product", "BTC-USD") if ASSETS else "BTC-USD",
        "symbol": ASSETS[0].get("spot_symbol", "btcusdt") if ASSETS else "btcusdt"}
MODEL = RAW.get("model", {}) or {}
RISK = RAW.get("risk", {}) or {}
FEE_MODEL = _fees.get("model", "auto")
FEE_PEAK = float(_fees.get("fee_peak", 0.0156))
MAKER_REBATE_THETA = float(_fees.get("maker_rebate_theta", 0.0) or 0.0)
US_NEW_ACCOUNT = _fees.get("us_new_account")
US_THETA_OVERRIDE = _fees.get("us_theta_override")
LATENCY_SECONDS = float(_fills.get("latency_ms", 250)) / 1000.0
EXTRA_SLIPPAGE = float(_fills.get("extra_slippage", 0.0) or 0.0)
MAKER = {"conservative_queue": _fills.get("maker_conservative_queue", True),
         "adverse_selection_seconds": _fills.get("adverse_selection_seconds", 5)}

# ---------------------------------------------------------------- portfolio / engine
STARTING_BANKROLL = float(_port.get("starting_bankroll", 1000))
STOP_EQUITY = _port.get("stop_equity", 200)
POLL_SECONDS = _eng.get("poll_seconds", 2)
WINDOW_SECONDS = 900
EQUITY_EVERY_SECONDS = _eng.get("equity_every_seconds", 5)
EQUITY_HEARTBEAT_SECONDS = _eng.get("equity_heartbeat_seconds", 60)
SETTLE_CHECK_SECONDS = _eng.get("settle_check_seconds", 15)
SETTLE_WARN_SECONDS = _eng.get("settle_warn_seconds", 3600)
SETTLE_MAX_LOOKUPS = _eng.get("settle_max_lookups", 6)
PROVISIONAL_RECHECK_SECONDS = _eng.get("provisional_recheck_seconds", 60)
BOOK_LEVELS_LOGGED = _eng.get("book_levels_logged", 10)
BOOKS_KEEP_DAYS = _eng.get("books_keep_days", 3)
RECORDER = RAW.get("recorder", {}) or {}
EXPORT_DIR = _eng.get("export_dir", "data/exports")
DB_PATH = _eng.get("db_path") or ("data/paper_us.db" if EXCHANGE == "polymarket_us" else "data/paper.db")


# ---------------------------------------------------------------- strategies
def _assets_for(markets):
    if not markets:
        return None
    return [m["asset"] for m in MARKETS if m["kind"] in markets]


def _build(key: str, block: dict):
    cls = REGISTRY[block.get("class") or key]     # `class:` lets several variants share one implementation
    params = {k: v for k, v in (block.get("params") or {}).items()}
    doc = inspect.getdoc(inspect.getmodule(cls)) or ""
    return cls(block.get("name") or key, assets=_assets_for(block.get("markets")),
               bankroll=block.get("bankroll"), title=block.get("title"),
               description=block.get("description"), details=doc, **params)


def build_strategies(raw: dict) -> list:
    out = []
    for key, block in (raw.get("strategies") or {}).items():
        block = block or {}
        if (block.get("class") or key) not in REGISTRY:
            raise ValueError(f"config.yaml: unknown strategy {block.get('class') or key!r}. "
                             f"Known: {sorted(REGISTRY)}")
        if block.get("enabled", True):
            out.append(_build(key, block))
        v = block.get("variant_4b")
        if key == "s4_momentum" and v and v.get("enabled", True):
            out.append(_build("s4b_minute_buckets", v))
    return out


STRATEGIES = build_strategies(RAW)
