# Polymarket Paper Desk

Forward-test multiple strategies on **Polymarket US** (polymarket.us) Bitcoin Up/Down markets with fake money and real order books, then compare them on one dashboard. (Set `EXCHANGE = "polymarket_intl"` in `config.py` to use polymarket.com instead.)

**Portfolio rules (config.py):** every strategy is its own $1,000 sub-portfolio; each new trade uses 10% of that strategy's *current* portfolio value; a strategy stops trading for good once its portfolio value is $200 or less (open bets still settle, and a restart keeps it stopped).

**Polymarket US numbers used** (from [docs.polymarket.us](https://docs.polymarket.us/fees), checked Nov 7 2026):
- Taker fee = Θ × contracts × p × (1 − p), rounded to the nearest cent (banker's rounding). Θ by the strategy's trailing 30-day taker volume: 0.095 (< $10k), 0.085 ($10k–50k), 0.075 ($50k–250k), 0.0695 ($250k+). New accounts get 0.0695 for 7 days (`US_NEW_ACCOUNT`). Taker rebates from $250k of volume (3%–30%).
- Volume = contracts × price of the side you received (a sale counts contracts × (1 − price)).
- One instrument per market (Yes = Up). Buying Down while holding Up *sells* the Up. Contracts trade in 0.01 steps.
- Results come from Polymarket US's settlement endpoint (CF Benchmarks BRTI; a tie settles Up). Settlements are final.
- No taxes are modelled: neither exchange charges tax in its fees; any tax on profits is yours to work out.

```
 Polymarket APIs ──► feed ──► engine ──► every strategy sees the same snapshot
 (read-only)                    │            │
                                │            ▼ orders
                                │      paper broker (one per strategy)
                                │      walks the real book, charges real fees
                                ▼            │
                          data/paper.db ◄────┘   ◄── dashboard.py reads this
```

**You only touch two places:** `config.py` (which markets, which strategies) and `strategies/` (your ideas). Everything else is the reusable template.

---

## 1. Setup (Windows, one time)

1. Install **Python 3.11+** from python.org. On the first installer screen, tick **"Add python.exe to PATH"**.
2. Unzip this folder somewhere, e.g. `C:\Users\you\polymarket-paper`.
3. Open **PowerShell** in that folder (Shift + right-click inside the folder → "Open PowerShell window here") and run:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

If PowerShell blocks the activate script, run once: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.
Every new terminal: run `.\.venv\Scripts\Activate.ps1` first.

## 2. Confirm the market slugs (do this first)

```powershell
python tools/find_markets.py
```

(`find_markets.py` searches polymarket.com. For Polymarket US the bot finds each round itself from the series name, e.g. `btc-updown-15m` -> event `btc-updown-15m-2026-11-07-2100z`, and logs "No Polymarket US market" if a series doesn't exist.) On polymarket.com the bot tracks `btc-updown-15m-<time>` and `btc-updown-5m-<time>`. Each market in `config.py` has its own `asset` label (`BTC-15m`, `BTC-5m`), `window_seconds` (900 / 300) and `fee_rate` (Crypto 0.07). This script confirms the slugs exist.

## 3. Smoke test with the simulator (no internet needed)

```powershell
python tools/simulate.py --hours 24
streamlit run dashboard.py
```

Pick the simulator in the sidebar. Simulated prices are invented — use this only to check your strategy runs and the numbers add up, never to judge edge.

## 4. Run it live

Two terminals (activate the venv in both):

```powershell
python run.py                 # terminal 1: the bot (Ctrl+C to stop)
streamlit run dashboard.py    # terminal 2: dashboard at http://localhost:8501
```

Live Polymarket US data goes to `data/paper_us.db` (polymarket.com runs stay in `data/paper.db`). The dashboard only accepts connections from this PC (see `.streamlit/config.toml`).

**Restart charts from now** (dashboard sidebar) makes every chart start fresh from that moment. It doesn't touch the database or the bot, which keeps recording; tables and totals stay all-time. "Show full history" undoes it. If port 8501 is busy, Streamlit picks the next free one (8502, ...) and prints it.

Restarting `run.py` resumes cash and open positions from the database, and strategies know which rounds they already traded, so a restart never makes them trade a round twice. `data/bot.log` rotates at 5 MB (5 old files kept). The bot only runs while your PC is on and awake — set Windows sleep to "Never" while testing, or later move it to an always-on box (a Raspberry Pi or a $5/month cloud VM works fine).

### Running it on a Raspberry Pi (always on)

Copy the project to the Pi (without `.venv` and `data`), then on the Pi run `bash deploy/raspberry-pi/setup.sh`. It installs everything and sets up two services, `polymarket-bot` and `polymarket-dashboard`, that start on boot and restart if they crash. The data is collected on the Pi in `data/paper_us.db`. Open the dashboard from any device on your Wi-Fi at `http://<pi-name>.local:8501`. Useful commands: `systemctl status polymarket-bot polymarket-dashboard`, `sudo systemctl restart polymarket-bot`, `tail -f data/bot.log`.

---

## 5. Adding a strategy

1. Copy `strategies/_template.py` → `strategies/my_idea.py`, rename the class.
2. Put tunable numbers in `PARAMS`; write rules in `on_tick(ctx)`; return orders.
3. Register it in `config.py`:

```python
from strategies.my_idea import MyIdea
STRATEGIES = [
    PreviousWinner("previous_winner"),
    MyIdea("my_idea_aggressive", threshold=0.10),
    MyIdea("my_idea_conservative", threshold=0.20, assets=["BTC-5m"]),
]
```

4. `python tools/simulate.py` to make sure it doesn't crash, then restart `run.py`.
5. Run the tests: `python -m unittest discover -s tests -v`.

**Rule:** if you change a strategy's logic, params, bankroll or markets, give it a **new name** (`_v2`). Otherwise old and new results blend into one meaningless track record. The bot warns you if you forget. Old names stay in the database; the dashboard hides them unless you switch on "Show retired strategies". (A retired strategy isn't run any more, so bets it still had open when you renamed it never get paid out.)

Two habits keep a strategy correct across restarts: check `ctx.traded_this_window` before entering (it turns True only once an order fills, and survives a restart; a flag you set in `ctx.state` before the fill would skip the round when the order is rejected), and give entries a latest time (e.g. `enter_before_seconds`) so a bot started mid-round doesn't jump in late.

What a strategy can see and do is documented at the top of `_template.py`.

---

## 6. Reading the dashboard

| Metric | Meaning |
|---|---|
| Return % | Account value vs. starting bankroll (open positions marked at mid) |
| Windows | Number of 15-min markets the strategy had money in that have resolved |
| Win rate % | Share of those windows with positive P&L. Meaningless without avg entry price |
| Edge per $1 bought | Profit per dollar of shares bought — the cleanest "is there edge?" number |
| t-stat | Avg P&L ÷ its standard error. **Under ~2 = can't rule out luck** |
| Max DD % | Worst peak-to-trough drop |
| Calibration chart | Price paid vs. actual win rate, for shares held to the result. Above the diagonal = edge |
| Money bet / Volume | Money bet = dollars spent buying. Volume (Polymarket US) = contracts × price of the side received |
| Status | Trading, or Stopped (portfolio value hit the $200 stop) |
| Fee Θ / Rebates | Taker fee coefficient on the latest trade (by volume tier) / taker rebates earned |
| Round trips | One line per entry → exit. A round where a strategy bought both sides is one "Both" line |

---

## 7. Honest limitations (read before trusting results)

- **Fills are optimistic.** You "fill" at the book you saw up to `POLL_SECONDS` ago, instantly, with no queue and no one reacting. Real fills on fast markets are worse. Set `EXTRA_SLIPPAGE = 0.005`–`0.01` for a pessimistic view; a strategy that only works at 0 slippage doesn't work.
- **Taker only.** All orders cross the spread (so maker rebates never apply). Resting limit (maker) orders aren't simulated — modelling whether you'd get filled requires trade-by-trade data. Add later if a strategy needs it.
- **No market impact across ticks.** Your paper buys don't move the real book; the next tick shows untouched liquidity again. Keep sizes small relative to displayed depth.
- **Polling, not streaming.** REST polling every `POLL_SECONDS` (5s by default). Strategies that depend on reacting within <1s (e.g., latency arb against spot moves) need the WebSocket feed and a spot-price source — both are extension points, not built yet.
- **Sample size.** One window per 15 min ≈ 96/day per asset. Expect weeks, not days, before a t-stat means anything.
- **Decided rounds have no sellers.** Once a round is all but decided, nobody sells the winning side, so it can't be bought at any price. Strategies must handle a missing `best_ask` / `best_bid`. The simulator reproduces this.
- **Results settle provisionally.** Rounds pay out as soon as prices lock at ~$1 / ~$0 (seconds after close). The bot keeps re-checking until Polymarket officially closes the market; if the official winner differs, it corrects the settlement and logs an ERROR.
- **Minimum order size.** Like Polymarket, orders under $1 are rejected, and orders sized in shares (all sells, and buys given as shares) must be at least the market's minimum (`orderMinSize`, 5 shares today). A smaller leftover is held to the result.
- **Fees come from Polymarket.** Each market's fee schedule (rate and exponent) is read from the API when the bot finds it; `fee_rate` in `config.py` is only the fallback. A mismatch is logged once.
- **Out-of-sync books are skipped.** The UP and DOWN books are mirror images, so UP ask + DOWN ask can't be under $1 on books from the same moment. When it is (the two downloads caught different moments), the books are fetched again, and if they still disagree the tick is skipped, so no strategy fills at prices that never existed together.
- **Data is logged.** `snapshots` stores top-of-book every tick, so later you can replay history to test new ideas without waiting.
