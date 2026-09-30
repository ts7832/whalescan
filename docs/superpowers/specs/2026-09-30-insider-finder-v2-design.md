# Insider Finder v2 — Design Spec

**Date:** 2026-09-30 · **Status:** approved in conversation
**One line:** find wallets whose big bets are repeatedly followed by the price moving their way *before* the market
catches on; prove (out of sample, per dollar, after costs) that copying their next bets pays before alerting on them;
and badge every bet by a wallet whose earlier call we saw proven right.

**Context:** fresh-account rules tested NO-GO (evidence study 2026-09-30). This replaces "account is new" with
"wallet is early". Everything else (dataset, copy-trade model, statistics, sweep, ledger) is reused.

## 1. Definitions (pre-registered — do not tune on results)

- **Covered market:** any category except SPORTS, not blocklisted, volume ≥ `[blocklist].min_market_volume`, and not a
  **price-threshold market** — question matches (case-insensitive)
  `\b(up or down|above|below|reach(es)?|hit \(?(high|low)\)?|dip to|price of|close (above|below)|trade (above|below))\b`.
  (Stock/crypto price markets are noise; corporate-announcement markets such as MicroStrategy purchases stay in.)
- **Big bet:** a BUY position event (production `aggregate()`, fills ≥ `[sweep].fill_min_usdc`) of ≥ `[finder].min_usdc`
  ($5,000) in a covered market, whale price inside `[insider].price_min … price_max`.
- **Signal time** t = the event's last fill. **Entry price** p0 = the event's VWAP price.
- **Move target:** `p0 + min(move_points, (1 − p0) × move_fraction_to_one)` with 0.20 and 0.5 → "+20 points, or half way
  to 1, whichever is closer".
- **Move hit:** the bought outcome's price reaches the target at any recorded point in (t, t + 24 h]. **Known at**
  t + 24 h (or the market's close time, if earlier). A bet with no recorded prices in the window is *unknown*.
- **Base rate:** the probability a big bet hits its move, per whale-price bucket
  (0.02–0.2, 0.2–0.4, 0.4–0.6, 0.6–0.8, 0.8–0.9, 0.9–0.95), estimated on the **training period only**.
- **Wallet score at time T:** over the wallet's big bets with known-at ≤ T and a known result: hits h, expected
  E = Σ base_rate(bucket), variance V = Σ b(1 − b); z = (h − E) / √V.
- **INFORMED WALLET at T:** n ≥ `[finder].min_bets` (5) and z ≥ `[finder].min_z` (2.33, one-sided p ≈ 0.01).
- **Rule INFORMED:** every big bet by a wallet that is INFORMED at that bet's signal time. Pre-registered variants,
  all reported: (min_bets, min_z) ∈ {(5, 2.33), (10, 2.33), (5, 3.0)}.
- **CONFIRMED INSIDER (live only):** a wallet with at least one logged ledger call (INSIDER / NEAR_MISS / INFORMED)
  that settled WIN. It stays confirmed until it has ≥ `[finder].demote_after` (5) later settled calls whose mean
  per-dollar return is < 0.

## 2. The test (evidence-study machinery)

Copy each INFORMED bet 15 min after the signal at the last price + half spread + fee, skipped past the chase limit;
return per dollar at settlement. Time split as the evidence study (older ⅔ / newest ⅓ of resolved bets): base rates
from training only; qualification is walk-forward (only moves known before each bet); **headline = test period**,
wallet-clustered 90 % CI, per-wallet t, bets per week over covered weeks. **GO** requires mean > 0 and CI lower
bound > 0 with ≥ 10 wallets. Report local (git-ignored `data/history/`), never published.

## 3. Data

Extend `whalescan history` (incremental): list covered markets beyond news; fetch their fills; fetch **30 h price
windows at 10-min fidelity**, one per (asset, 6-hour slot) holding a big bet (window = slot start … +30 h, so every
bet in the slot has a full 24 h). Profiles / pre-bet histories are not needed for this rule.

## 4. Live (built now: CONFIRMED; built only on GO: INFORMED)

- Each sweep round loads confirmed wallets from the ledger; any big bet (≥ `[finder].min_usdc`, covered market) by one
  becomes an alert badged **CONFIRMED INSIDER** and is logged to the ledger (kind `CONFIRMED`).
- On GO: the daily job computes the INFORMED list into the (non-public) research cache; the sweep badges their big
  bets **INFORMED WALLET** and logs them (kind `INFORMED`).
- Dashboard: both badges visible on alert cards; the Track Record shows the new kinds.

## 5. Out of scope

Public 6 h delay / encrypted private channel, trades layer, markets table, analytics, research hub.
