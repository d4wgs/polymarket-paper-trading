# Polymarket Paper Desk

Forward-tests six model-driven strategies on **Polymarket US** Bitcoin "Up or Down" **5-minute and 15-minute** rounds with fake money, real order books and the exchange's real fees. Every strategy runs on the same live data with its **own $1,000 paper bankroll**, and the dashboard compares them.

**Everything you tune is in [`config.yaml`](config.yaml).** Nothing is hardcoded in the code.

```
 Coinbase/Binance websocket ──► spot + 1s EWMA vol ─┐
 Polymarket US (BRTI index) ──► oracle + K ─────────┼─► fair value P_up = N(ln(S/K) / σ√t)
 Polymarket US order books ─────────────────────────┘          │
                                                     every strategy sees the same snapshot
                                                                │ orders
                         risk limits ─► paper broker (one per strategy): taker fills after latency
                                        against the re-fetched book; maker fills only on trade-through
                                                                │
                                         data/paper_us.db ◄─────┘ ◄── dashboard.py, daily CSV export
```

## The strategies

| Key in config.yaml | What it does |
|---|---|
| `s1_fair_value` | Buys the side whose model fair value beats ask + fee by ≥ 4¢. Skips the first 30s / last 10s. Holds (optional early exit). |
| `s2_market_maker` | Rests bids on both sides at fair ± 2¢, skewed by inventory; pulls quotes on fast spot moves and in the last 45s. Logs adverse selection. |
| `s3_sniper` | In the last 60s (5m) / 120s (15m): z ≥ 2.5, winner's ask 0.90–0.98, edge ≥ 1¢, spot/oracle basis < 25% of S−K. |
| `s4_momentum` | Side ≥ 70¢ **and** spot **and** oracle moved its way since the open. Variant **4b** (`variant_4b`, own bankroll, 5m only): 3 one-minute buckets in the same direction → buy at the final minute if ask ≤ 85¢. |
| `s5_complete_set` | Buys Up + Down when the pair costs ≤ $0.995 after fees (walking real depth); leg 2 is filled after latency on a fresh book. Logs near-misses. |
| `s6_mean_reversion` | Fades a ≥ 12¢ price jump in 10s that fair value didn't confirm; exits at 50% reversion or 60s. 5m only. |

Old strategies (previous winner, last 60s, underdog, scalp) are in [`archive/`](archive/); their history stays in the database and shows on the dashboard as "retired".

## Turning strategies on/off and changing parameters

Each strategy has a block under `strategies:` in `config.yaml`:

```yaml
  s3_sniper:
    enabled: true               # false = not run at all
    name: s3_sniper_v1          # the name results are stored under
    markets: [5m, 15m]          # which round lengths it trades
    params:
      z_threshold: 2.5          # any key from the strategy's PARAMS
      snipe_window: {5m: 60, 15m: 120}   # a param can differ per market length
      stake_usd: 25
```

1. Edit the value. **If you changed logic or parameters, also bump `name`** (e.g. `_v2`), or old and new results blend together (the bot warns you).
2. Restart the bot: `Ctrl+C`, then `python run.py` (on the Pi: `sudo systemctl restart polymarket-bot`).

A misspelled parameter stops the bot at startup with the list of valid names. Other blocks:

| Block | Controls |
|---|---|
| `assets` | The asset is a config value: symbol, round lengths, Polymarket slug, settlement index, spot product. Add ETH etc. here later. |
| `spot` | Spot feed source (`coinbase` default; `binance` is blocked from US IPs, use `binanceus`). |
| `model` | Vol half-life (300s), `sigma_multiplier`, `distribution: normal | student_t`, `student_t_df`. |
| `fees` | `model: auto` = Polymarket US's published schedule (below); `placeholder` = `fee_peak × 4p(1−p)` with a startup WARNING. Maker rebate rate (default 0). |
| `fills` | Taker latency (250 ms), conservative maker queue, adverse-selection horizon (5s). |
| `risk` | Max $ per trade / per market, max open markets, daily loss stop, basis kill switch (0.05%). |
| `portfolio` | Bankroll per strategy ($1,000), stop level. |
| `engine` | Poll interval, retention, CSV export folder, database path. |

## The model and the plumbing (Part A)

- **Feeds.** Spot from a websocket (REST fallback if it goes quiet). Settlement reference = **CF Benchmarks BRTI**, which Polymarket US settles these markets on, read from Polymarket US. Every tick logs spot, oracle, **basis**, σ and P_up to `snapshots`. K = the exchange's published "price to beat" (fallback: first oracle print of the window).
- **Volatility.** EWMA of 1-second log returns, half-life 300s, seeded at startup from 5h of 1-minute candles so the model works immediately.
- **Fair value.** `P_up = N(ln(S/K)/(σ√t))`, S = spot (fallback oracle; configurable), t floored at 1s. With `settlement_average_seconds: 60` the close is modelled as the 60-second average the rules use: variance σ²·((t−60) + 20) before the final minute, and inside it the already-elapsed part of the average is known. Student-t option is rescaled to unit variance, so σ keeps its meaning and only the tails fatten. `MarketData.fair_value(market_id)` returns `(P_up, P_down, sigma, t, S, K)`.
- **Fees.** Confirmed at [docs.polymarket.us/fees](https://docs.polymarket.us/fees) on 2026-11-07: taker fee = **Θ × contracts × p × (1−p)**, rounded to the cent (half-to-even), Θ = 0.095 below $10k of 30-day volume (0.085 / 0.075 / 0.0695 above). Makers pay **no fee**; maker rebates only start at $10M of monthly volume, so they're 0 here. `core.fees.fee(price, size, side)` implements this.
- **Fills.** Taker orders wait `latency_ms`, the book is **fetched again**, and the order walks that book within its price limit (strategies set the limit at the worst price that still meets `min_edge`). Maker bids count only when the market trades **through** them (a seller strictly below our price), never on a touch, with the queue ahead of us charged first.
- **Every fill records** time, strategy, market, side, price, size, fee, maker/taker, model fair value, edge at entry (with the actual fill price), spot, oracle, basis, σ, seconds left, K and the strategy's own signal values (`extra`).
- **Risk.** Checked before every order that adds risk; exits are never blocked. The kill switch also trips if spot or oracle is missing (`require_both_feeds`).

## Comparing them (Part C)

The dashboard's **Strategy lab** section shows per strategy: PnL before/after fees, trades, win rate, hit rate vs. **breakeven** rate, average entry, average edge at entry vs. **realized** edge, Sharpe per trade, t-stat, max drawdown, fees, PnL by 5m/15m and by time-left-at-entry, plus the **model calibration chart** (fair-value buckets 0.0–0.1 … 0.9–1.0 vs. actual win rate, for trades and for every logged tick), maker adverse selection, and diagnostics (near-misses, skipped signals, risk rejections, failed arbitrage legs).

Trades are exported daily to `data/exports/trades_YYYY-MM-DD.csv` (ET day). On demand: `python tools/export_trades.py --all --scorecard`.

## Setup and running

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m unittest discover -s tests -v      # 64 tests
python tools/simulate.py --hours 24          # offline smoke test -> data/sim_us.db
python run.py                                # the bot (Ctrl+C to stop)
streamlit run dashboard.py                   # http://localhost:8501
```

Restarting `run.py` resumes cash and open positions from the database. **Raspberry Pi:** copy the project (without `.venv` and `data`), run `bash deploy/raspberry-pi/setup.sh` (it installs `requirements.txt`, which now includes `pyyaml` and `websockets`).

## Honest limitations

- **The oracle feed is slow, and settlement is an average (measured 2026-11-07).** The BRTI series we can read from Polymarket US trails Coinbase spot by ~30–35 seconds, and the market rules settle on the *average of 60 BRTI prints in the final minute* (K is the same kind of average). So the model uses **spot as S** (`model.s_source: spot`) and **models the 60-second averaging** (`model.settlement_average_seconds: 60`). The spec's original point formula on the oracle is `s_source: oracle`, `settlement_average_seconds: 0` (give strategies new names if you switch). The oracle is still logged every tick and still drives the basis kill switch and the sniper's basis filter, but because of its lag, basis readings during fast moves are mostly delay, not disagreement.

- **Complete-set arbitrage can't fire on Polymarket US.** Up and Down are one order book there (Down ask = 1 − Up bid), so a pair always costs $1 + spread + fees. S5 runs and logs how close it gets; it's only meaningful on polymarket.com (`exchange: polymarket_intl`, where you'd also set `risk.require_both_feeds: false` because the Chainlink oracle isn't wired in).
- **The book is polled every 2s (REST).** Maker trade-throughs that happen and vanish between polls are missed (understates maker fills); shocks shorter than a poll are invisible to S6.
- **The oracle (BRTI 1-second candles from Polymarket US) can lag spot by a second or two**, which shows up as basis. If the kill switch trips too often, look at the logged basis before loosening `basis_kill_pct`.
- **No market impact across ticks.** Paper buys don't move the real book. Keep stakes small relative to displayed depth.
- **The simulator's P&L means nothing.** Its prices are generated from the same kind of model, so model-based strategies look great there by construction. Use it only to check that things run.
- **Sample size.** ~288 five-minute and ~96 fifteen-minute rounds a day. Expect weeks before a t-stat above 2 means anything.
