# WHALESCAN Evidence Study — Design Spec

**Date:** 2026-09-29 · **Status:** Approved direction in conversation
**One line:** Replace "79 bets, 87% hit, +17¢/share" with an honest, out-of-sample, per-dollar, after-cost,
position-sized estimate of what copying WHALESCAN's calls would really earn — and how often it would bet.

**Decisions made with the user:** paper trading only until the evidence justifies real money (then a few hundred,
later a few thousand) · the data decides which settlement horizons to trade · the live-execution machine is decided
later · once real money is used, public alerts are delayed 6 h · nothing sensitive ever goes to GitHub (the repo is
public).

---

## 1. Questions the study must answer

1. **How many bets per week** would each candidate rule produce?
2. **What does a copy trade really return per dollar**, entering *after* the whale at a realistic price, paying fees
   and spread, counted so one prolific wallet cannot dominate?
3. **Which factors predict the return** (account age, bet size, price, time to settlement, category, number of fills,
   other big wallets on the same side, markets traded before the bet)?
4. **Is "settles within a week" better** than longer horizons, once capital lock-up is counted?
5. **Which sizing and aggregation rule** grows a bankroll best for an acceptable drawdown?
6. **Go / no-go:** is there enough evidence to risk real money, and on which rule?

## 2. Dataset (`whalescan history`)

**Markets:** every market whose Gamma tags map to an insider news category (`[insider].categories`), with volume ≥
`[blocklist].min_market_volume`, not blocklisted, that **closed within the lookback** (`[study].lookback_days`,
default 180). Open news markets are also listed, for bet *counts* only (§6).

**Fills:** every fill ≥ `[sweep].fill_min_usdc` ($1k) in those markets (`/trades?market=`, paginated to the API's
offset cap). Fills are grouped into position events with the **production** `aggregate()` (same 10-minute window),
so the study measures exactly what the live scanner would have seen.

**Wallet facts at bet time** (no look-ahead):
- account age at the event's first fill = first fill − profile `createdAt`; profiles created after the first fill are
  `AGE INCONSISTENT` and excluded from age-based rules (same rule as I2);
- markets traded **before** the bet, counted from the wallet's own trade history — fetched only for wallets younger
  than `[study].history_max_age_days` (older wallets fail I2 regardless).

**Market facts:** category, end/closed time, final outcome prices (payout; fractional results are *irregular* and
excluded from returns), fee schedule, volume.

**Post-bet prices:** minute-level `prices-history` for the outcome token from the bet to +60 min, sampled at the
entry delays in `[study].entry_delays_min` (default 1, 5, 15, 60).

**Storage:** `data/history.duckdb` (git-ignored, incremental: already-fetched markets, fills and profiles are not
fetched again). The finished bet log is exported as `history/bets.parquet` and published to the `archive` branch
(public on-chain data only).

## 3. The copy-trade model (per bet)

For entry delay *d*:
- `entry = price(T + d) + half_spread` where `half_spread = [study].half_spread` (historical books were not recorded);
- **skipped** if `entry − whale_price > [insider].max_slippage` (the chase limit, as I6) or `entry` is outside
  `[insider].price_min … price_max`;
- `cost = entry + fee(entry)` using the market's fee schedule;
- `return = (payout − cost) / cost` — **per dollar**, not per share;
- `days_held = resolution time − (T + d)`.

## 4. Candidate rules

- **INSIDER** — the production rule (I1–I6 with historical age and markets-traded).
- **NEAR_MISS** — the Track Record's widened bands.
- **FRESH_ALL** — any fresh (≤ 30 d) account's news buy ≥ $5k, as a looser benchmark.
- **BASELINE** — every news-market buy ≥ $5k (what "following big money" earns with no age filter).
- **Refined rules** proposed by the factor analysis (§5), e.g. INSIDER restricted to a price band or horizon.

## 5. Statistics

- **Walk-forward split by time.** Rules and factor thresholds are chosen on the older part (`[study].train_fraction`,
  default ⅔ of bets by time) and evaluated on the newest part. **The headline number is the test-period result.**
- **Clustered uncertainty.** Bets by one wallet are not independent. 90 % confidence intervals come from a bootstrap
  that resamples *wallets* (with all their bets); the per-wallet t-statistic is reported alongside.
- **Factors.** Quantile buckets and Spearman rank correlation of each factor against per-dollar return, on train and
  test separately; a factor counts only if its direction holds on the test period.
- **Minimum evidence.** A rule's result is reported as `INSUFFICIENT` below `[study].min_wallets` distinct wallets.

## 6. Bets per week

Count qualifying position events per calendar week by **bet time**, in resolved *and* open markets (resolved-only
counts would under-count the most recent weeks). Report median and range across weeks, per rule.

## 7. Horizon

Returns by time-to-resolution bucket (< 1 d, 1–7 d, 7–30 d, 30–90 d, > 90 d), plus **return per dollar per day
held** and the annualised growth each bucket implies — a 10 % return that ties money up for four months is worth less
than 3 % in two days.

## 8. Sizing and aggregation (bankroll simulation)

Chronological simulation from a starting paper bankroll (`[study].bankroll`, default 1,000), cash-constrained (money
in an open position is unavailable until it settles):
- **flat stake**, **fixed fraction** of bankroll, and **fractional Kelly**. Kelly's fraction for a binary bet at cost
  *c* with win probability *p* is `(p − c) / (1 − c)`; *p* is the bucket's train-period win rate **shrunk toward
  the market price**, then multiplied by `[study].kelly_fraction` (default ¼);
- caps: per bet (`max_bet_fraction`), per event, per wallet, total open exposure; stake ≤ `[study].max_whale_share` of
  the whale's own size (liquidity);
- aggregation of several qualifying wallets on the same outcome: *first only* vs *add-on per wallet*.

Uncertainty: 2,000 bootstrap paths resampling **weeks** keep streaks realistic; the report gives median and
5th-percentile final bankroll, median and 95th-percentile max drawdown, and the probability of ending below the start.

## 9. Report (`whalescan study`)

`docs/reports/<date>-evidence-study.md` plus `history/study.json`. Contents: data coverage; the full list of candidate
bets with WIN / LOSS / IRREGULAR and per-dollar return (CSV alongside); per-rule tables for §1 questions; the factor
table; horizon table; sizing comparison; **the headline number** (test period, per dollar, after costs, with a
wallet-clustered 90 % CI, at the chosen delay); and a **go / no-go** against pre-registered criteria:
- test-period mean return per dollar after costs > 0 and its CI lower bound > 0;
- at least `min_wallets` distinct wallets in the test period;
- the bankroll simulation's 5th-percentile path does not lose more than 30 %;
- afterwards, the live paper trader (next plan) must track the backtest over ≥ 30 settled trades.

## 10. Permanent fill archive

Every sweep round appends the fills it read (≥ $1k) to a daily file on the `archive` branch (append-only, never
force-pushed). These are public on-chain data, so publishing them is safe, and it stops the rolling 24 h window from
discarding the raw history.

## 11. Out of scope here (next plan)

The forward **paper trader** (bankroll, sizing, fills at the live book, P&L), the 6 h public delay, and any code that
holds a key or places orders.

## 12. Testing

Pure functions (copy-trade return, skip rules, Kelly, caps, bootstrap, walk-forward split, bucket statistics) with
exact expected values; the dataset builder against fake APIs (pagination, incremental re-runs, age/markets-traded
at bet time, no look-ahead); the simulation's cash constraint and caps; the archive writer; a smoke run on real data.
