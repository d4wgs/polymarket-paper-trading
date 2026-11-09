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
| `s3_sniper` | In the last 60s of every 5m and 15m round, buys whichever side is winning (index vs price to beat). No other filters. |
| `s4_momentum` | Side ≥ 70¢ **and** spot **and** oracle moved its way since the open. Variant **4b** (`variant_4b`, own bankroll, 5m only): 3 one-minute buckets in the same direction → buy at the final minute if ask ≤ 85¢. |
| `s5_complete_set` | 15s in, buys the pricier side (leg 1); buys the opposite side (leg 2) once leg 1's price is +10% or −5% (`pnl_basis: price`; `locked` counts fees and spread), or at 30s left. On Polymarket US leg 2 executes as selling leg 1. |
| `s6_mean_reversion` | Fades a ≥ 12¢ price jump in 10s that fair value didn't confirm; exits at 50% reversion or 60s. 5m only. |

### Variants added 2026-11-08 (each runs on its own $1,000 bankroll; the originals keep running as controls)

Live result of the originals after ~1 day: `s4_momentum_v1` +$189 (316 trades, t = 0.8), `s3_sniper_v2` −$196 (169 trades). The sniper's 39 trades that paid under 70¢ lost $306 because the index it trusts is a lagged 60-second average, so these variants test fixes. None is proven yet; judge them on fresh data.

| Config key (`class:`) | Name in the database | What changes |
|---|---|---|
| `s3_sniper_v3a` | `s3_sniper_v3a_floor50` | Sniper, but only buys if the winning side's ask is ≥ 50¢ (market agrees it is ahead) |
| `s3_sniper_v3b` | `s3_sniper_v3b_floor70` | Same with a 70¢ floor |
| `s3_sniper_v3c` | `s3_sniper_v3c_floor70_cap97` | 70¢ floor and a 97¢ cap (above that a win pays < 3¢) |
| `s3_sniper_v3d` | `s3_sniper_v3d_model_floor50` | "Winning" comes from the fair-value model (live spot + settlement averaging) instead of the lagged index; 50¢ floor |
| `s4_momentum_min80` | `s4_momentum_min80_v1` | S4 with the crowd price ≥ 80¢ |
| `s4_momentum_edge` | `s4_momentum_edge0_v1` | S4 that also needs non-negative model edge |
| `s4_momentum_15m` | `s4_momentum_15m_v1` | S4 on 15-minute rounds only |
| `s4b_minute_buckets_v2` | `s4b_minute_buckets_v2` | Minute buckets + the side's mid must be ≥ 50¢ |

A strategy block may set `class: <registry key>` so several variants share one implementation (see `config.yaml`). Offline replay of S4 on the logged ticks: the spot+oracle confirmation gave 79.2% wins / +$0.68 per trade vs 78.0% / +$0.49 for simply buying every favorite ≥ 70¢, so most of S4 is "buy the favorite".

**5m vs 15m.** The leaderboard, KPIs and charts combine both round lengths. The dashboard's *Strategy lab* has a section **5-minute vs 15-minute rounds** with the scorecard (P&L, win %, breakeven %, avg entry, t-stat, fees) split per strategy and round length, with a Both / 5m / 15m switch.

Old strategies (previous winner, last 60s, underdog, scalp) are in [`archive/`](archive/); their history stays in the database and shows on the dashboard as "retired".

## Turning strategies on/off and changing parameters

Each strategy has a block under `strategies:` in `config.yaml`:

```yaml
  s3_sniper:
    enabled: true               # false = not run at all
    name: s3_sniper_v2          # the name results are stored under
    markets: [5m, 15m]          # which round lengths it trades
    params:
      snipe_seconds: 60         # any key from the strategy's PARAMS
      stake_usd: 25             # (a param can also differ per market length: {5m: 10, 15m: 25})
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
| `risk` | Max $ per trade / per market, max open markets, basis kill switch (0.05%). Strategies stop for good at a $200 portfolio value (`portfolio.stop_equity`). |
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
python -m unittest discover -s tests -v      # 157 tests
python tools/simulate.py --hours 24          # offline smoke test -> data/sim_us.db
python run.py                                # the bot (Ctrl+C to stop)
streamlit run dashboard.py                   # http://localhost:8501
```

Restarting `run.py` resumes cash and open positions from the database. **Raspberry Pi:** copy the project (without `.venv` and `data`), run `bash deploy/raspberry-pi/setup.sh` (it installs `requirements.txt`, which now includes `pyyaml` and `websockets`).

## Disk, backups and secrets

- **Retention.** The raw order-book log (`books`, ~50 MB/day) is trimmed to `engine.books_keep_days` (default 3) every 10 minutes; freed pages are reused, so the DB file stops growing once the window is full. Compact tables (`snapshots`, `fills`, `settlements`, `equity`, `market_stats`) are kept.
- **Backups.** `tools/backup_db.sh` (Pi, cron 03:30) makes a consistent gzip copy of the DB in `data/backups/` (3 kept). `tools/pull_backup.ps1` (Windows, scheduled task `PolymarketPiBackup`, register with `-Register`) triggers it and copies the result to `~/polymarket-backups` (14 kept). Older order books therefore live on in those off-Pi copies.
- **Volume stats.** Each REST book poll carries the market's `stats` (shares/notional traded, open interest, high/low/last). They're stored in `market_stats` whenever they change (30 s heartbeat).
- **API keys never go in git.** Put `PM_KEY_ID` and `PM_SECRET_KEY` (from polymarket.us/developer) in the environment or a gitignored `.env` (copy `.env.example`; `chmod 600` on the Pi); `core/credentials.py` reads them and nothing ever logs them. Only the websocket recorder needs a key; paper trading itself uses public data.

## Websocket recorder (separate process)

`python tools/ws_recorder.py` (systemd unit `deploy/raspberry-pi/polymarket-recorder.service`) connects to `wss://api.polymarket.us/v1/ws/markets` and records, for the current **and next** round of every configured market, `SUBSCRIPTION_TYPE_MARKET_DATA` (book + stats) and `SUBSCRIPTION_TYPE_TRADE`. It is its own process and writes its own file, `data/ws_events.db`, so it can never stop trading or lock/bloat `paper_us.db`. Handshake signing is the official `polymarket-us` SDK's (`X-PM-Access-Key` / `-Timestamp` / `-Signature`, Ed25519 over `timestamp + "GET" + "/v1/ws/markets"`); the system clock must be within 30 s of real time.

What a 30-minute probe showed (`python tools/ws_recorder.py --probe 1800` writes the raw messages to `data/ws_probe.jsonl`): book messages are **full snapshots**, not deltas (about 100 levels a side, ~8 KB, 5-10 per second per market, nearly every one different); trade messages are one print each with a unique `id`; the exchange sends no heartbeats. So the recorder keeps the top 10 levels plus stats, skips unchanged content and limits rows to one per market per 500 ms, and keeps **every** trade print.

Tables in `ws_events.db`: `ws_book` (recv_ts, exch_ts, slug, state, best bid/ask, top levels JSON, last price/qty, shares & notional traded, open interest, hi/lo), `ws_trades` (recv_ts, trade_ts, slug, trade_id unique, price, qty, maker/taker side, intent, outcome side, action, state), `ws_gaps` (a row on every connect/disconnect/stop: data between a `disconnect` and the next `connect` may be missing), `ws_meta`. Config: the `recorder:` block in `config.yaml`. **Retention and size cap** (`core/ws_trim.py`, runs every 10 minutes of data time): per-table retention (`book_keep_days`, `trades_keep_days`, 7 each, about 2.8 GB at today's rates of ~150 MB/day books and ~250 MB/day trades), plus a total cap `max_db_mb` over *all* recorded tables: when live data exceeds it, the oldest rows of `ws_book` and `ws_trades` are deleted in proportion to their size down to `trim_target_pct` of the cap. Rows newer than `min_keep_hours` (24) are never deleted, and `ws_gaps` / `ws_meta` markers never are; if the cap still cannot be met a `SIZE CAP CANNOT CONVERGE` error is logged. Deletes run in committed batches within `prune_budget_s`, then an incremental vacuum and `wal_checkpoint(TRUNCATE)` make the file and `-wal` actually shrink. An existing DB needs a one-time `python tools/db_convert_autovac.py --db data/ws_events.db` (recorder stopped) to enable `auto_vacuum=INCREMENTAL`; new DBs get it automatically. `python tools/ws_trim.py --db COPY.db --dry-run` shows what a pass would delete (test on a copy). Disk usage is logged every `disk_log_seconds`. Reconnects use exponential backoff (HTTP 429 and rejected handshakes back off harder), subscriptions are re-sent on every connect, and a watchdog reconnects after `stale_seconds` of silence.

## Replay (`tools/replay.py`)

Feeds recorded `books` + `snapshots` (spot, sigma, index, price-to-beat) + `windows` + official `resolutions` through the **real** engine, strategies and broker, with an injected clock (the engine already took `clock=`/`sleep=`; live behaviour is unchanged and a test pins that). The source DB is opened read-only and results go to their own file (`data/replay.db`).

```
python tools/replay.py --src data/paper_us.db --days 1                    # last day of the recording
python tools/replay.py --src X.db --from 2026-11-08 --to 2026-11-09       # UTC date range
python tools/replay.py --src X.db --days 3 --holdout 1                    # in-sample vs the newest day
python tools/replay.py --src X.db --strategy s3_sniper_v2 --self-check    # compare with the live bot's own fills
python tools/replay.py --src X.db --days 1 --ws-trades data/ws_events.db  # maker fills from ws trade prints
```

The report (trades, P&L, win rate, average entry, fees, t-stat) comes from `core/metrics.py`, the same code as the dashboard. Maker fills keep the conservative book trade-through rule unless `--ws-trades` is given (then a resting bid fills only on prints strictly through its price).
`--self-check` replays a period the live bot already traded and diffs fills and P&L against the live `fills`; `--model-check` compares the replayed fair value with the recorded one tick by tick. Known approximations: a recording has a book every ~2 s, so the order-latency re-fetch uses the decision book (`--latency-book next` uses the next recorded one, which matched worse); spot history is the recorded per-tick spot; thresholds that sit exactly on a decision boundary can flip. Back-up databases (`~/polymarket-backups`) can be replayed too, and the order books older than `books_keep_days` only exist in those.

## Holdout validation (`tools/validate.py`)

"Does this strategy hold up out of sample?" Replays the recording (or reuses `--replay-db`), splits rounds **by time** (never random) into development and a holdout (newest `--holdout-days`, or `--holdout-frac` of rounds), and reports per strategy: trades, rounds, P&L, win rate, breakeven win rate (average entry plus fee per share), average entry, per-trade expectancy with a bootstrap 95% CI that resamples **rounds** (fills in a round are correlated; fixed `--seed`, `--boot-n`), fees and t-stat (same helper as the dashboard leaderboard).

```
python tools/validate.py --src data/paper_us.db --holdout-days 2 --dev-days 5          # development only
python tools/validate.py --src data/paper_us.db --holdout-days 2 --reveal-holdout      # also the holdout (logged)
python tools/validate.py --src X.db --strategies s3_sniper_v2 --markets 5m --export-json out.json --export-csv out.csv
```

The holdout is neither computed nor printed without `--reveal-holdout`; every reveal appends the date, strategies and a parameter hash to `data/holdout_reveals.log`, so you can see how often you have peeked. With many variants on the same data the header prints the variant count, a Bonferroni threshold and a best-of-N warning.

**Replay-optimism caveat.** Replay fills against the decision-time book, while live fills hit a book fetched ~250 ms later, which is optimistic on thin books (self-check: `s3_sniper_v2` live -$349 vs replay -$236; 58 longshot rounds explain $132 of the $156 gap). Strategies with more than 25% of fills or of shares bought below $0.20 entry (or fills larger than the displayed best-ask depth) are marked **LOW CONFIDENCE ON DOLLARS**: trust win rate, sign and ranking, not dollars. Thresholds live in the `validate:` block of `config.yaml`.

## Honest limitations

- **The oracle feed is slow, and settlement is an average (measured 2026-11-07).** The BRTI series we can read from Polymarket US trails Coinbase spot by ~30–35 seconds, and the market rules settle on the *average of 60 BRTI prints in the final minute* (K is the same kind of average). So the model uses **spot as S** (`model.s_source: spot`) and **models the 60-second averaging** (`model.settlement_average_seconds: 60`). The spec's original point formula on the oracle is `s_source: oracle`, `settlement_average_seconds: 0` (give strategies new names if you switch). The oracle is still logged every tick and still drives the basis kill switch and the sniper's basis filter, but because of its lag, basis readings during fast moves are mostly delay, not disagreement.

- **Up and Down are one order book on Polymarket US** (Down ask = 1 − Up bid), so buying both at the same moment always costs $1 + spread + fees. S5 v2 therefore legs in: its profit comes only from the price moving its way between leg 1 and leg 2.
- **The book is polled every 2s (REST).** Maker trade-throughs that happen and vanish between polls are missed (understates maker fills); shocks shorter than a poll are invisible to S6.
- **The oracle (BRTI 1-second candles from Polymarket US) can lag spot by a second or two**, which shows up as basis. If the kill switch trips too often, look at the logged basis before loosening `basis_kill_pct`.
- **No market impact across ticks.** Paper buys don't move the real book. Keep stakes small relative to displayed depth.
- **The simulator's P&L means nothing.** Its prices are generated from the same kind of model, so model-based strategies look great there by construction. Use it only to check that things run.
- **Sample size.** ~288 five-minute and ~96 fifteen-minute rounds a day. Expect weeks before a t-stat above 2 means anything.
