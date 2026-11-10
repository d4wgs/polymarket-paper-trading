# FREEZE

**The official test started at 2026-11-09 22:49:32 EST (2026-11-10 03:49:32 UTC, unix 1794282572).**
That is the moment the bot on the Raspberry Pi started on the code and configuration below (`bot_runs` row 1).

| | |
|---|---|
| Code version (`VERSION` on the Pi, stamped on every fill) | `3e5276434128` = commit [`3e5276434128be64bc7eb54d6e58b95c4e4da9a6`](https://github.com/d4wgs/polymarket-paper-trading/commit/3e5276434128be64bc7eb54d6e58b95c4e4da9a6), pushed to `origin/main`, clean (not `-dirty`) |
| Config version (hash of the parsed `config.yaml`) | `d6fa2087d419` |
| Model | Student-t, df 4 (`model.distribution: student_t`), fixed: not to be re-tuned |
| Round-validity rule | `gap>10s\|coverage<0.9\|missing>0.1\|poll=2s`, plus bot restart and critical clock drift |
| Clock alerts | warn at 0.5 s, critical at 1.5 s (exchange timestamp + NTP status) |
| Pass criteria | [`docs/PASS_CRITERIA.md`](docs/PASS_CRITERIA.md) (thresholds are DRAFT until you accept them there) |

## The rule from this date on

**From the freeze time above, any change to a running variant creates a new stamped variant and restarts that
variant's evidence count at zero.** "Any change" means: a parameter, the variant's source file, the fair-value
model or its settings, the fill / fee / risk / validity rules, and bug fixes, however small. The changed variant
gets a new `name` in `config.yaml`; the old name is disabled (never deleted) and its history stays as it is.
Evidence for a variant counts only from fills carrying ONE `params_hash` and ONE `source_hash`, in rounds that
are valid (`round_validity.valid = 1`), after the freeze time.

Changes that touch nothing a variant decides or how it is filled or scored (a new report, a dashboard tweak,
documentation) do not restart anything, but they do change `code_version`, so they are visible on every fill.
A bot restart never restarts the evidence count; it only invalidates the rounds that were in progress.

## What is running

14 strategy variants and 7 no-signal controls. Hashes are read from the Pi's own `strategy_versions` table and
match what `config.yaml` at the commit above produces.

| Name | Kind | Class | params_hash | source_hash | Markets | Bankroll | Control |
|---|---|---|---|---|---|---|---|
| `s3_sniper_v2` | strategy | `Sniper` | `4df2dc69d54b` | `f8569c04e9b7` | 5m, 15m | $1,000 | `ctl_s3_fav` |
| `s3_sniper_v3a_floor50` | strategy | `Sniper` | `813a3f9f14b1` | `f8569c04e9b7` | 5m, 15m | $1,000 | `ctl_s3_fav` |
| `s3_sniper_v3b_floor70` | strategy | `Sniper` | `02c588a8d2b0` | `f8569c04e9b7` | 5m, 15m | $1,000 | `ctl_s3_fav70` |
| `s3_sniper_v3c_floor70_cap97` | strategy | `Sniper` | `30bace92ec19` | `f8569c04e9b7` | 5m, 15m | $1,000 | `ctl_s3_fav70_97` |
| `s3_sniper_v3d_model_floor50` | strategy | `Sniper` | `dc14585975ca` | `f8569c04e9b7` | 5m, 15m | $1,000 | `ctl_s3_fav` |
| `s4_momentum_v1` | strategy | `CrowdMomentum` | `8cc12541fac3` | `674775d5a75f` | 5m, 15m | $1,000 | `ctl_s4_fav70` |
| `s4b_minute_buckets_v1` | strategy | `MinuteBuckets` | `974437a90f0f` | `674775d5a75f` | 5m | $1,000 | `ctl_s4b_fav` |
| `s4_momentum_min80_v1` | strategy | `CrowdMomentum` | `f641c89e30f8` | `674775d5a75f` | 5m, 15m | $1,000 | `ctl_s4_fav80` |
| `s4_momentum_edge0_v1_t4` | strategy | `CrowdMomentum` | `b19eb3276a77` | `674775d5a75f` | 5m, 15m | $1,000 | `ctl_s4_fav70` |
| `s4_momentum_15m_v1` | strategy | `CrowdMomentum` | `8cc12541fac3` | `674775d5a75f` | 15m | $1,000 | `ctl_s4_fav70` |
| `s4b_minute_buckets_v2` | strategy | `MinuteBuckets` | `35430dae43a1` | `674775d5a75f` | 5m | $1,000 | `ctl_s4b_fav` |
| `s6_shock_reversion_v1_t4` | strategy | `ShockReversion` | `9acffd9d8abb` | `24c229efceec` | 5m | $1,000 | `ctl_s6_random` |
| `s7_nested_pair_v1` | strategy | `NestedPair` | `580883c18ba1` | `17948d195960` | 5m, 15m | $1,000 | - |
| `s7_nested_pair_ev_v1_t4` | strategy | `NestedPair` | `bccfb1e32e55` | `17948d195960` | 5m, 15m | $1,000 | - |
| `ctl_s3_fav` | control | `SniperFavorite` | `4df2dc69d54b` | `78259e54ddc8` | 5m, 15m | $25,000 | - |
| `ctl_s3_fav70` | control | `SniperFavorite` | `02c588a8d2b0` | `78259e54ddc8` | 5m, 15m | $25,000 | - |
| `ctl_s3_fav70_97` | control | `SniperFavorite` | `30bace92ec19` | `78259e54ddc8` | 5m, 15m | $25,000 | - |
| `ctl_s4_fav70` | control | `MomentumFavorite` | `dab14dd1e34a` | `888131162364` | 5m, 15m | $25,000 | - |
| `ctl_s4_fav80` | control | `MomentumFavorite` | `9915bce2af1e` | `888131162364` | 5m, 15m | $25,000 | - |
| `ctl_s4b_fav` | control | `BucketsFavorite` | `49afaf93a4b5` | `888131162364` | 5m | $25,000 | - |
| `ctl_s6_random` | control | `ShockRandom` | `e2bdfb04514d` | `888131162364` | 5m | $25,000 | - |

`s4_momentum_v1` and `s4_momentum_15m_v1` share a params_hash on purpose: same parameters, different markets.
A control shares its family's params_hash when its parameters are identical; its class and source differ.

Retired (disabled, history kept, no new trades): `s1_fair_value_v1`, `s1_fair_value_v2`, `s2_market_maker_v1`,
`s3_sniper_v1`, `s5_complete_set_v1`, `s5_complete_set_v2`, `s6_shock_reversion_v1`, `s4_momentum_edge0_v1`,
`s7_nested_pair_ev_v1`, and the four original `*_us` strategies.

## History before the freeze

Fills from before the freeze time have no version stamp (NULL = unversioned) and were made under earlier code and,
for the model-based strategies, the normal distribution. They stay in the database and on the dashboard, but they
are **not** evidence for the pass criteria. For the variants that kept their names (`s3_sniper_*`, `s4_momentum_v1`,
`s4_momentum_min80_v1`, `s4_momentum_15m_v1`, `s4b_minute_buckets_*`, `s7_nested_pair_v1`) count only rows with
`ts >= 1794282572`; the dashboard's all-time totals include the earlier period.

## How to check a variant's evidence is clean

```
python tools/health.py                 # clock drift, restarts, invalid rounds
python tools/round_validity.py --list  # which rounds are excluded, and why
python tools/fill_report.py            # fill rate, slippage, depth vs size
sqlite3 data/paper_us.db "SELECT strategy, code_version, params_hash, source_hash, COUNT(*) FROM fills WHERE ts >= 1794282572 GROUP BY 1,2,3,4"
```

More than one `params_hash` or `source_hash` for a name in that last query means the name was changed after the
freeze and its evidence is blended: rename it and start again.
