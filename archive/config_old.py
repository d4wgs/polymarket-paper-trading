"""
THE ONLY FILE YOU NORMALLY EDIT (besides writing strategies).
"""
from strategies.previous_winner import PreviousWinner
from strategies.last_60s import Last60s
from strategies.arbitrage import Arbitrage
from strategies.scalp import Scalp

# ----------------------------------------------------------------------------
# 0) WHICH EXCHANGE
# "polymarket_us"   -> Polymarket US (polymarket.us, CFTC-regulated). Order books, results and the
#                      fee schedule all come from Polymarket US. See core/fees.py for the fee rules.
# "polymarket_intl" -> polymarket.com (not available to US residents).
# ----------------------------------------------------------------------------
EXCHANGE = "polymarket_us"

# Polymarket US taker fees (https://docs.polymarket.us/fees, tiers effective 1:00 PM ET, Nov 6 2026):
#   Fee = Θ × contracts × p × (1-p), rounded to the cent (banker's rounding)
#   Θ by trailing 30-day taker volume: < $10k 0.095 | $10k-50k 0.085 | $50k-250k 0.075 | $250k+ 0.0695
# Each strategy is treated as its own account: it pays the tier ITS OWN volume earns.
#
# US_NEW_ACCOUNT: Polymarket US gives new accounts Θ = 0.0695 for their first 7 days.
#   None            -> established account, no boost (conservative; the boost only lasts a week)
#   "per_strategy"  -> treat each strategy as a brand-new account from its first fill
#   "2026-11-07"    -> the ET date of YOUR account's first fill (uses your real onboarding window)
US_NEW_ACCOUNT = None
US_THETA_OVERRIDE = None       # e.g. 0.0695 only if Polymarket assigned your account a fixed rate

# ----------------------------------------------------------------------------
# 1) WHICH MARKETS
# asset:          a UNIQUE label per market (engine state and assets=[...] filters use it).
# slug_prefix:    Polymarket US series, e.g. btc-updown-15m -> events btc-updown-15m-2026-11-07-2100z
# window_seconds: length of the round (300 = 5m, 900 = 15m, 3600 = 1h).
# index_asset:    the settlement index price logged every tick (BTC: CF Benchmarks BRTI).
# fee_rate:       only used when EXCHANGE = "polymarket_intl" (polymarket.com crypto rate 0.07).
# name:           (optional) friendly name shown on the dashboard.
# ----------------------------------------------------------------------------
MARKETS = [
    {"asset": "BTC-15m", "slug_prefix": "btc-updown-15m", "window_seconds": 900, "index_asset": "crypto:btc",
     "fee_rate": 0.07, "name": "Bitcoin · 15-minute rounds"},
    {"asset": "BTC-5m",  "slug_prefix": "btc-updown-5m",  "window_seconds": 300, "index_asset": "crypto:btc",
     "fee_rate": 0.07, "name": "Bitcoin · 5-minute rounds"},
]

# ----------------------------------------------------------------------------
# 2) PORTFOLIO RULES (every strategy is its own sub-portfolio)
# ----------------------------------------------------------------------------
STARTING_BANKROLL = 1000.0     # each strategy starts with $1,000
STAKE_FRACTION = 0.10          # each new trade = 10% of that strategy's CURRENT portfolio value
STOP_EQUITY = 200.0            # a strategy stops trading for good once its portfolio value is <= $200
                               # (bets already open still settle; restarts keep it stopped)

# ----------------------------------------------------------------------------
# 3) WHICH STRATEGIES
# Same class + different params = separate strategies you can compare.
# Names must be unique. If you change a strategy's logic or params, give it a
# NEW name (e.g. add _v2) so old and new results don't get mixed together.
# assets=["BTC-5m"] limits a strategy to one market; leave it out for all.
# ----------------------------------------------------------------------------
_SIZE = (f"Each trade uses {STAKE_FRACTION:.0%} of the strategy's current portfolio value "
         f"(it starts with ${STARTING_BANKROLL:,.0f}), and it stops trading for good if the portfolio "
         f"value falls to ${STOP_EQUITY:,.0f} or less.")
_FEES = ("Fees are Polymarket US taker fees: Θ × contracts × price × (1 − price), rounded to the cent, "
         "with Θ set by the strategy's own 30-day volume (0.095 below $10k, down to 0.0695 at $250k+).")

STRATEGIES = [
    PreviousWinner(
        "previous_winner_us", stake_fraction=STAKE_FRACTION,
        title="Previous Winner",
        description="Copies the result of the last round: bets on whichever side won it.",
        details=(
            "**What it does:** Bets that the next round will go the same way as the round that just ended.\n\n"
            "**How it works:**\n"
            "1. A new round opens (every 5 or 15 minutes, depending on the market).\n"
            "2. It waits 15 seconds.\n"
            "3. It looks at who won the previous round of the same market (Up or Down) and buys that side.\n"
            "4. It holds until the round ends. A winning contract pays $1, a losing one $0.\n\n"
            f"One trade per round. If the previous result isn't known within 90 seconds, it skips the round. "
            f"{_SIZE} {_FEES}")),
    Last60s(
        "last_60s_us", stake_fraction=STAKE_FRACTION,
        title="Last 60 Seconds",
        description="With one minute left, bets on whichever side the market says is winning.",
        details=(
            "**What it does:** Backs the current favorite right before the round ends.\n\n"
            "**How it works:**\n"
            "1. It does nothing until the final 60 seconds of a round.\n"
            "2. It checks the price of Up and Down. A price above 50¢ means the market thinks that side "
            "is more likely to win.\n"
            "3. It buys the side priced above 50¢ and holds until the round ends.\n\n"
            "One trade per round. Favorites near the end are expensive (e.g. 90¢), so you win little when right "
            f"and lose a lot when wrong. {_SIZE} {_FEES}")),
    Arbitrage(
        "underdog_close_us", stake_fraction=STAKE_FRACTION,
        title="Underdog, then Close",
        description="Buys the underdog early, then sells it back once a profit is locked in (or at the end).",
        details=(
            "**What it does:** Buys the side priced below 50¢ early in the round and closes the position "
            "later. Polymarket US has a single contract per market, so you can't hold Up and Down at the "
            "same time: buying the other side simply sells what you hold.\n\n"
            "**How it works:**\n"
            "1. A new round opens and it waits 15 seconds (it won't open a trade later than 45 seconds in).\n"
            "2. It buys the side priced below 50¢ (the underdog).\n"
            "3. It closes the whole position as soon as doing so locks in at least 2¢ per contract after "
            "fees, or else in the final 60 seconds at whatever it fetches.\n\n"
            "**If the round is already decided** and nobody is bidding for the underdog, it can't be sold "
            "and rides to the result.\n\n"
            f"**Profit:** sale price − price paid − both fees. {_SIZE} {_FEES}")),
    Scalp(
        "scalp_us", stake_fraction=STAKE_FRACTION,
        title="Scalp",
        description="Buys 15 seconds into the round, then sells at +10% profit, a 5% price drop, or 1 minute before the round ends.",
        details=(
            "**What it does:** A quick in-and-out trade. It does not wait for the round's result; it tries to "
            "sell for a small gain or cut a small loss.\n\n"
            "**How it works:**\n"
            "1. A new round opens and it waits 15 seconds (it won't open a trade later than 45 seconds in).\n"
            "2. It buys the side the market favors (priced above 50¢) at the current price.\n"
            "3. It then sells everything at the current market price as soon as ONE of these happens:\n"
            "   - the sell price is **10% above** what it paid, fee included (take profit),\n"
            "   - the sell price has **dropped 5%** since it bought (stop loss), or\n"
            "   - **1 minute** is left in the round (time exit).\n\n"
            "One trade per round. The sell price is the best price a buyer is offering right now, so the "
            "targets are measured on prices you could really get. "
            f"{_SIZE} {_FEES}")),
]

# ----------------------------------------------------------------------------
# 4) ENGINE SETTINGS
# ----------------------------------------------------------------------------
POLL_SECONDS = 2               # Polymarket US allows 25 requests/s per IP; each poll uses ~2 per market
WINDOW_SECONDS = 900           # default window length; each market overrides it with its own "window_seconds"
EXTRA_SLIPPAGE = 0.00          # add e.g. 0.005 to pessimistically model latency (0.5c worse per fill)
EQUITY_EVERY_SECONDS = 5       # how often each strategy's account value is saved (the dashboard's equity curve)
SETTLE_CHECK_SECONDS = 15      # Polymarket US settles Up/Down rounds within about a minute of the close
SETTLE_WARN_SECONDS = 3600     # warn (hourly) if a market is still unresolved 1h after closing
SETTLE_MAX_LOOKUPS = 6         # max result lookups per settlement pass, so a backlog can't stall the loop
PROVISIONAL_RECHECK_SECONDS = 60   # (polymarket.com only) re-check provisional results until official
BOOK_LEVELS_LOGGED = 10        # order-book levels per side saved on every tick (table "books")
BOOKS_KEEP_DAYS = 30           # delete raw order books older than this (~50 MB/day for 2 markets, so ~1.5 GB
                               # kept). Trades, results, equity and top-of-book prices are always kept
                               # (~30 MB/day). None = keep forever (the Pi's free 19 GB then lasts ~8 months).
DB_PATH = "data/paper_us.db" if EXCHANGE == "polymarket_us" else "data/paper.db"
