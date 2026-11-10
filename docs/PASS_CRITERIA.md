# Pass criteria

**Status: DRAFT (proposed 2026-11-09). Every threshold below is a proposal for you to edit. Once you accept
them, write the acceptance date here and do not change them afterwards: a criterion that moves after the data
is in is not a criterion.**

This file is written *before* the test results exist. It says, for every strategy variant that is running, what
it has to show before it counts as "worth a small real-money pilot". Nothing here says a strategy will make money
live: paper fills don't move the real book, each strategy sees its own private copy of the liquidity, and the fee
tier assumes each strategy is its own account. Passing means "not ruled out", not "profitable".

## The rules that apply to every variant

1. **Only valid rounds count.** Rounds marked invalid for data coverage (`round_validity`: gaps, restarts, clock
   drift; `core/validity.py`) are left out by every tool by default. Never judge with `--include-invalid`.
2. **Beat the control, not zero.** The main test is *strategy minus its no-signal control* on the rounds both
   traded (`python tools/validate.py`, section "STRATEGY MINUS ITS NO-SIGNAL CONTROL"). The 95% bootstrap confidence
   interval (resampling rounds) of the per-round difference must have its **lower end above 0**.
3. **Positive on its own.** Per-trade expectancy must have its 95% CI lower end above 0, and the win rate must be
   above the breakeven win rate (average entry price + fee per share).
4. **Multiple comparisons.** 14 strategy variants run on the same data, so the best one is flattered by luck. A
   variant passes only if the t-statistic of its paired difference is at least the **Bonferroni threshold**
   `tools/validate.py` prints for the number of variants compared (about **2.91** for 14; single-test 1.96 is
   not enough). Controls are not hypotheses and are not counted as variants.
5. **Pessimistic replay.** The variant must still satisfy rules 2 and 3 when the same period is replayed with
   **+1c extra slippage per share and a 500-1000 ms order latency**:
   `python tools/validate.py --src data/paper_us.db --profile pessimistic` (the profile uses 1000 ms; add
   `--latency-ms 500` for the easy end). With a websocket recording (`--ws-db data/ws_events.db`) the order fills
   against the book rebuilt 1000 ms after the decision; without one it fills against the next recorded book
   (about 2 s later, which is stricter).
6. **Execution quality.** From `python tools/fill_report.py`: fill rate at least 90%, average slippage at most
   +1.0c per share, and median displayed depth at least 5x the order size. Not flagged LOW CONFIDENCE ON DOLLARS.
7. **Minimum evidence.** At least the number of valid paired rounds in the table **and** at least 14 calendar days
   of running (so the result spans weekdays, a weekend and more than one volatility regime).
8. **Holdout: look once.** The newest 25% of rounds are the holdout (`--holdout-frac 0.25`). Develop and decide on
   the development part only. When a variant passes everything above, reveal the holdout **once** with
   `--reveal-holdout`; the paired difference must have the same sign there. Every reveal is appended to
   `data/holdout_reveals.log` and **counts as a peek**: a variant whose holdout has been revealed and failed is
   finished under that name.
9. **The freeze.** From the freeze date in `FREEZE.md`, any change to a variant (parameters, its code, the model,
   even a bug fix) creates a new stamped variant with a new name, and that variant's evidence count restarts at 0.
   The version stamp on every fill (`code_version`, `params_hash`, `source_hash`, `config_version`) is how this is
   checked: a variant's evidence must come from one `params_hash` and one `source_hash`.
10. **Model choice is fixed.** The Student-t model with df = 4 was chosen on the same data the strategies are
    evaluated on. It is a design decision, not a tuned parameter: do not re-tune it.

## Template

| Field | Meaning |
|---|---|
| Variant | the `name` in config.yaml (what results are stored under) |
| Hypothesis | what must be true for this to make money, in one sentence |
| Family / control | the no-signal control it is compared with (`controls:` in config.yaml) |
| Min valid rounds | paired rounds (both the variant and its control have a result) needed before judging |
| Pass threshold | what has to hold, on top of the rules above |
| Multiple comparisons | the adjustment applied |
| Date | when this row was written / accepted |

## One row per active variant (all DRAFT, written 2026-11-09)

"Standard" in the threshold column means rules 2-8 above, unchanged.

| Variant | Hypothesis | Family / control | Min valid rounds | Pass threshold | Multiple comparisons | Date |
|---|---|---|---|---|---|---|
| `s3_sniper_v2` | In the last 60 s the side the (lagged) index says is winning is underpriced. | sniper / `ctl_s3_fav` | 1,000 | Standard. | Bonferroni, 14 variants | 2026-11-09 DRAFT |
| `s3_sniper_v3a_floor50` | The index signal adds value once the market agrees the side is ahead (ask ≥ 50c). | sniper / `ctl_s3_fav` | 1,000 | Standard. | Bonferroni, 14 variants | 2026-11-09 DRAFT |
| `s3_sniper_v3b_floor70` | The index signal adds value when the market is confident (ask ≥ 70c). | sniper / `ctl_s3_fav70` | 1,000 | Standard. | Bonferroni, 14 variants | 2026-11-09 DRAFT |
| `s3_sniper_v3c_floor70_cap97` | As v3b, and skipping asks above 97c removes trades that cannot pay. | sniper / `ctl_s3_fav70_97` | 1,000 | Standard. | Bonferroni, 14 variants | 2026-11-09 DRAFT |
| `s3_sniper_v3d_model_floor50` | The live-spot model picks the winning side better than the market's favourite. | sniper / `ctl_s3_fav` | 1,000 | Standard. Note: it picks the same side as the control unless model and market disagree, so the difference comes from few rounds; also require at least 100 rounds where the two picked different sides. | Bonferroni, 14 variants | 2026-11-09 DRAFT |
| `s4_momentum_v1` | A favourite (≥ 70c) confirmed by spot and the index wins more often than its price implies. | momentum / `ctl_s4_fav70` | 2,000 | Standard. | Bonferroni, 14 variants | 2026-11-09 DRAFT |
| `s4_momentum_min80_v1` | The same confirmation is worth more when the crowd is at ≥ 80c. | momentum / `ctl_s4_fav80` | 2,000 | Standard. | Bonferroni, 14 variants | 2026-11-09 DRAFT |
| `s4_momentum_edge0_v1_t4` | Adding "model edge ≥ 0" to the confirmation filters out the losing favourites. | momentum / `ctl_s4_fav70` | 2,000 | Standard. | Bonferroni, 14 variants | 2026-11-09 DRAFT |
| `s4_momentum_15m_v1` | The confirmation works on 15-minute rounds. | momentum / `ctl_s4_fav70` (its 15m rounds) | 1,000 | Standard. | Bonferroni, 14 variants | 2026-11-09 DRAFT |
| `s4b_minute_buckets_v1` | Three one-minute moves in the same direction continue into the final minute. | minute buckets / `ctl_s4b_fav` | 150 | Standard. Note: it buys sides the control never buys (under 50c), so few rounds are paired; rule 3 must also hold on all its own rounds. | Bonferroni, 14 variants | 2026-11-09 DRAFT |
| `s4b_minute_buckets_v2` | As v1, when the market also prices that side at ≥ 50c. | minute buckets / `ctl_s4b_fav` | 150 | Standard. | Bonferroni, 14 variants | 2026-11-09 DRAFT |
| `s6_shock_reversion_v1_t4` | A ≥ 12c price jump the model does not confirm reverts. | shock / `ctl_s6_random` | 400 | Standard. | Bonferroni, 14 variants | 2026-11-09 DRAFT |
| `s7_nested_pair_v1` | A 5m + 15m pair bought for ≤ 99c pays ≥ $1 (locked) and sometimes $2. | no control (not a directional bet) | 100 completed pairs | Rule 3 on its own rounds; **zero** rounds that lost more than the 3c completion slack; fewer than 5% of pairs left with a naked leg (`leg2_abandoned` events); rules 5, 6, 8. | Bonferroni, 14 variants | 2026-11-09 DRAFT |
| `s7_nested_pair_ev_v1_t4` | Paying up to $1.05 for the pair is worth it when the model's band probability is high enough. | compared with `s7_nested_pair_v1` (no control) | 100 completed pairs | Rule 3 on its own rounds, and per-pair expectancy above the locked version's; rules 5, 6, 8. | Bonferroni, 14 variants | 2026-11-09 DRAFT |

## Controls (not hypotheses; they pass or fail nothing)

Each control bets the same $25 per entry as its family but has a **$25,000** paper bankroll instead of $1,000. A
control is expected to lose steadily (fees and spread, no signal), and one that reached the $200 stop would stop
trading and end the comparison. The bankroll does not change what it trades or its per-round P&L. Compare
controls in dollars per round, never in % return.

| Control | Removes | Used by |
|---|---|---|
| `ctl_s3_fav` | the index / model signal: buys the higher ask in the last 60 s | `s3_sniper_v2`, `v3a`, `v3d` |
| `ctl_s3_fav70` | the same, ask ≥ 70c | `s3_sniper_v3b_floor70` |
| `ctl_s3_fav70_97` | the same, 70c ≤ ask ≤ 97c | `s3_sniper_v3c_floor70_cap97` |
| `ctl_s4_fav70` | the spot + index confirmation: buys a side at mid ≥ 70c, ask ≤ 90c | `s4_momentum_v1`, `edge0_v1_t4`, `15m_v1` |
| `ctl_s4_fav80` | the same at mid ≥ 80c | `s4_momentum_min80_v1` |
| `ctl_s4b_fav` | the minute-bucket test: buys the favourite at the final minute | both `s4b` variants |
| `ctl_s6_random` | the direction: on the same shock buys a seeded-random side (seed 12345), sells after 60 s | `s6_shock_reversion_v1_t4` |

## How to run the check

```
python tools/round_validity.py                      # how many rounds are excluded, and why
python tools/fill_report.py                         # fill rate, slippage, depth vs size (rule 6)
python tools/validate.py --src data/paper_us.db --holdout-frac 0.25                         # rules 2-4, 7 (development only)
python tools/validate.py --src data/paper_us.db --holdout-frac 0.25 --profile pessimistic   # rule 5
python tools/validate.py --src data/paper_us.db --holdout-frac 0.25 --reveal-holdout        # rule 8: ONCE per variant
python tools/health.py                              # clock and uptime during the test
```
