# Alpha hunt — pre-registration (written 2026-11-04, before any October/November data was looked at)

**Question.** Does anything we can observe at the moment of a big Polymarket bet predict whether *copying* it
makes money after costs — per trade, or per wallet? If something survives an untouched holdout, WHALESCAN is
restarted on Data API v2 around it. If nothing does, it stays off.

## 1. Data and the three periods

| Name | Window (by signal time) | Role |
|---|---|---|
| **OLD** | 2025-05-03 → 2026-09-30 | exploration: everything may be searched, tuned and plotted here |
| **NEW** | 2026-10-01 → data end (≈ 2026-11-04) | **holdout**: fully built before analysis, then touched **once**, only for the finalists named by OLD |
| **ALL** | OLD ∪ NEW | reported for completeness (it is not independent evidence: OLD chose the finalists) |

Inside OLD, model selection uses **purged, embargoed walk-forward folds** (monthly, expanding window, 7-day
embargo between train and test, because one news event's bets resolve together and leak across a boundary).

Source: `history.duckdb` (built by `whalescan history`), extended to NEW with Data API v2 (the v1 API was retired
on 2026-11-02). Same market universe, same $1k fill floor, same aggregation into position events, same
look-ahead rules as the evidence study (2026-09-29 spec §2). Plus hourly price paths per traded outcome
(CLOB `/prices-history`) for markouts and pre-bet momentum.

## 2. What is measured (targets)

Price already encodes probability, so **win rate is never a target**. Each big buy *i* gets:

- **`ret_15`** (primary) — per-dollar return to resolution for a copier entering 15 minutes after the whale's last
  fill, after the market's taker fee and a 1¢ half-spread (exactly the evidence study's `copy_return`).
- **`edge_whale`** — `payout − whale_price`: does the information exist at all, even if not copyable.
- **Markouts** (high-frequency trading's standard toxic-flow test) — the bought outcome's price change from the
  whale's price to +1 h, +24 h, +7 d. These exist for unresolved markets too, so NEW is not limited to markets
  that happened to resolve by early November (which skews to short-dated markets).

Statistics are **clustered by event** (one news event's bets are one piece of evidence, not dozens) with the
wallet as a robustness check; confidence intervals come from an event-level block bootstrap (2,000 draws).

## 3. Tests

**T0 — Re-run of the earlier studies on NEW and ALL.** The evidence-study rules (INSIDER, NEAR_MISS, FRESH_ALL,
BASELINE × horizon/price filters), the Insider Finder variants, and the live Track Record (INSIDER, NEAR_MISS,
SNIPER, CONFIRMED) — unchanged definitions, reported for OLD, NEW and ALL.

**T1 — Per-trade features (one at a time).** For every feature: monthly rank information coefficient (Spearman
IC) with `ret_15`, `edge_whale` and the 24 h markout; mean IC, Newey–West t-stat across months, share of months
with the same sign; quintile spread (top − bottom quintile return) with event-bootstrap CI.
Features (all known at signal time):
- wallet: age (days), log markets traded before, first-ever bet flag, prior big bets, prior realised edge of its
  resolved big bets (empirical-Bayes shrunk), category concentration, trades per week;
- bet: log USDC, USDC ÷ wallet's median prior bet, USDC ÷ market big-fill volume before the bet, number of fills
  (order splitting), side of the book it bought (entry price), price bucket;
- timing: days to scheduled end, hour of day (UTC), weekday, market age;
- price path: price impact (price 10 min after vs before), 24 h pre-bet momentum (contrarian vs trend);
- crowd: other whales buying the same outcome in 24 h, **fresh-wallet cluster** (other ≤ 7-day-old wallets buying
  the same outcome within 6 h), big money on the opposite outcome in 24 h;
- market: category, big-fill volume to date, number of outcomes / neg-risk.

**T2 — Multivariate model.** Gradient-boosted trees (LightGBM, shallow, strongly regularised) and a regularised
logistic baseline predicting `won − entry price` (edge), trained in the walk-forward folds. Judged on
out-of-fold predictions only: decile spread of returns, and **whether the model's probability beats the market
price** (log-loss and Brier vs the entry price as the forecast). Feature attribution by permutation importance and
SHAP, on out-of-fold data.

**T3 — Wallet skill persistence.** For wallets with ≥ 5 resolved big bets in both halves of their own history
(time split, 7-day gap): rank correlation of shrunk edge between halves; decile transition table; and a
walk-forward strategy "copy wallets whose *prior* shrunk edge is in the top decile" (no look-ahead). Then which
wallet traits (T1's wallet features, calibration, hold-to-resolution share) predict a good second half.

**T4 — Fade.** For every T0 rule and T1 bottom quintile with a negative OLD return: buy the *opposite* outcome at
its own price (1 − p, plus spread and fee) at +15 min.

**T5 — Market-wide effects.** Favourite–longshot calibration (resolved outcome vs entry price, by price bucket ×
category); post-whale drift: do markouts continue (momentum) or reverse, and is a "trade the drift" rule
profitable after costs.

## 4. Multiple testing and the decision rule

Everything tried is counted. On OLD (walk-forward out-of-fold), tests are corrected with **Benjamini–Hochberg at
q = 0.10** across all feature × target tests; the best strategy's Sharpe is **deflated** for the number of
strategies tried (Bailey & López de Prado). At most **10 finalists** go to NEW.

| Verdict | Requirement |
|---|---|
| **REAL** (restart-worthy) | passes BH on OLD; on NEW same sign, after costs, event-bootstrap 90% CI excluding zero; ≥ 30 events and ≥ 10 wallets on NEW |
| **IN THE ROUGH** | passes BH on OLD; same sign on NEW but its CI includes zero — worth paper trading, not real money |
| **DEAD** | anything else |

A strategy additionally needs positive after-cost return in OLD's walk-forward *and* NEW, and a realistic
frequency (≥ 1 bet a week on average). Nothing found here trades real money before a forward paper period.
