# WHALESCAN Track Record — Design Spec

**Date:** 2026-09-25 · **Status:** Approved in conversation, pending spec review
**One line:** Log every call WHALESCAN makes, price it the way you would really have entered, follow it on a fixed
schedule until its market settles, and learn from the results which calls are worth taking.

**Decisions made with the user:** log alerts **and** near-misses · entry = **live order-book cost of $1,000 at the
moment of the call, fees included** (the whale's own price recorded alongside) · checkpoints on **days 7, 14, 21, 28,
then monthly; quarterly after day 28 for markets settling 3+ years out** · final result at settlement · storage on
**GitHub (free)**, on its own branch.

---

## 1. What is a call

A call is one position event (fills by one wallet on one outcome, merged within the 10-minute window — Plan 1 §4.6)
that WHALESCAN surfaced. Three kinds:

| Kind | When it is logged |
|---|---|
| `INSIDER` | the event passes insider rules I1–I6 (it appears as an INSIDER alert) |
| `SNIPER` | the event passes G1–G7 in sniper mode (it appears as a SNIPER alert) |
| `NEAR_MISS` | a BUY in a news-category, open, non-blocklisted market by an unproven wallet, with the price not run away (I1 and I5 pass; I6 passes or merely has no book yet — the book is read when the call is recorded, §2), that fails **exactly one** of I2/I3/I4 **and only just**: account 7–30 days old, **or** 11–30 markets traded, **or** $5k–$10k in size (bands in `config.toml [ledger]`) |

- Each call is recorded **once** and never edited (an append-only record). Its id is `<event id>:<kind>`.
- If a near-miss later grows into an alert (more fills, same event), the alert is a **new** call with its own entry
  price at that moment. Both stay in the record: that is exactly the "would waiting have helped?" data we want.
- Calls are logged by the 15-minute sweep (the primary loop). The live station and the daily batch do not log, so there
  is one source of truth and no duplicates.

## 2. Entry: what you would really have paid

At the moment of the call, the sweep reads the live order book of the called outcome and walks the asks for
`entry_size_usdc` ($1,000):

- `entry_vwap` — average price paid across the levels consumed (C++ book walk, Plan 1 §4.1);
- `entry_fee` — taker fee per share at that price, `rate × (p(1−p))^exponent`;
- `entry_cost = entry_vwap + entry_fee` — cost per share; **every return is measured against this**;
- also recorded: whale price, best bid/ask, spread, book timestamp.

If no book can be read (network error, empty book), the call is still logged with `entry_estimated = true` and
`entry_vwap = whale price + validation.slippage`, so the analysis can include or exclude such rows.

## 3. Features stored with every call (for learning later)

Wallet: account age (days, at the call), markets traded, position size, number of fills, whale price, sniper stats
when relevant (resolved bets, win rate, p-value, edge). Market: category, tags, days until settlement, 24 h and total
volume, spread and ask depth within 5¢ at entry. Context: number of other big wallets (≥ gate floor) on the same side in
the last 24 h, number on the opposite side, UTC hour and weekday, whether the wallet was still adding when called,
which rule a near-miss missed, alert tier.

## 4. Tracking until settlement

**Checkpoint schedule**, computed when the call is logged and stored with it:
days **7, 14, 21, 28**, then every **30 days** (every **91 days** if the market's end date is ≥ 3 years after the call),
until the end date. Markets without an end date follow the monthly rule for up to 3 years.

**At each checkpoint** (processed by the first sweep at or after the due time; the actual time is recorded) the sweep
reads the book and records best bid, best ask and mid. The mark-to-market return assumes you **sell into the best bid**
and pay the taker fee:

`checkpoint return = (best_bid − fee(best_bid) − entry_cost) ÷ entry_cost`

No bids → the checkpoint is recorded with `return = null` (no one would buy).

**At settlement** (Gamma reports the market closed with final outcome prices):

`final return = (payout − entry_cost) ÷ entry_cost`, payout = the called outcome's final price (1 or 0; fractional
for 50/50 or voided markets, flagged `irregular = true`). Once settled, a call has no further checkpoints.

## 5. Storage: the `ledger` branch

A separate branch of the repo, never merged into `main`, holding:

| File | Content |
|---|---|
| `calls.jsonl` | one JSON object per call (§1–§4 fields), appended, never rewritten |
| `marks.jsonl` | one JSON object per checkpoint or settlement: `{call_id, type, at, day, best_bid, best_ask, mid, return_pct, payout?}` |
| `summary.json` | derived: totals, hit rates, average returns by kind/tier/checkpoint, the 100 most recent calls with latest mark, analysis results |

JSON Lines (one object per line) makes every change a small, readable git diff and keeps a permanent audit trail
(git history). At the expected volume (tens of calls a day) the files stay small for years; loading them is instant.

**Sync:** `scripts/ledger-sync.sh pull|push` keeps a clone of the branch in `data/ledger/` (gitignored in `main`).
Each sweep round: pull → update → push only if something changed, with rebase-and-retry. The sweep loop is the only
writer (one GitHub job at a time — concurrency group), so conflicts do not arise in normal operation.

## 6. Learning (built now; speaks when data allows)

`whalescan analyze` (also run after every sweep that settles something) writes an `analysis` block into `summary.json`:

1. **Outcome label.** A call is *scored* once settled (win = final return > 0). A secondary label, "up at day 28"
   (day-28 checkpoint return > 0), gives signal long before most markets settle; both are reported, never mixed.
2. **Winners vs losers, feature by feature** — median and mean of each numeric feature for winners and losers, with a
   Mann–Whitney U test (a rank test, no distribution assumptions) and its p-value; share of each categorical value
   (kind, tier, category, missed rule) among winners vs losers.
3. **Bucketed win rates** — each numeric feature split into quantile buckets (e.g. account age 0–1 d / 1–3 d / 3–7 d),
   win rate and mean return per bucket, so threshold changes can be read directly off the table.
4. **Model** (only with ≥ 200 scored calls): L2-regularised logistic regression on standardised features, 5-fold
   cross-validated AUC and coefficients. Below 200 the block says `INSUFFICIENT DATA` — a model on 40 rows would
   only memorise noise.
5. Minimum sample sizes are enforced everywhere (`[ledger] min_scored = 30`); smaller groups are shown as `n < 30`.

## 7. Dashboard

Panel 05 gets two tabs: **VALIDATION** (as today) and **TRACK RECORD**:
- headline: calls logged, open, settled; settled hit rate and mean return; mean return at day 7 / day 28 checkpoints;
  each split by INSIDER / SNIPER / NEAR_MISS;
- table of recent calls: time, kind (missed rule for near-misses), market, entry cost, latest mark, return %, status
  (OPEN / SETTLED WIN / SETTLED LOSS / IRREGULAR);
- analysis highlights once ≥ 30 calls are scored.

The page reads `summary.json` from the ledger branch via `raw.githubusercontent.com` (same mechanism as the alerts).

## 8. Backfill

On first run, the two insider alerts currently published (Texas governor, Serbia PM) are logged as calls with the
entry priced from the book **at backfill time**, flagged `backfilled = true` (their true call time is kept as
`first_seen_ts`). Nothing is priced retroactively at a better moment than was actually available.

## 9. Error handling

| Failure | Handling |
|---|---|
| Book unavailable at call time | log with estimated entry (§2), flagged |
| Book unavailable at a checkpoint | retry on the next sweep; after 24 h record the checkpoint with `return = null`, `missing = true` |
| Market missing from Gamma | checkpoints continue from the book; settlement waits |
| Ledger push rejected | pull --rebase and retry ×3; on failure keep local changes for the next round (nothing lost; append-only files merge cleanly) |
| Corrupt line in a `.jsonl` | skipped with a warning; never rewrites history |

## 10. Testing

Pure functions (schedule, returns, near-miss rule, analysis statistics) with exact expected values; the ledger store
with temp directories (append, dedupe, reload, due checkpoints, settlement); the sweep integration with fake APIs
(new call recorded with book entry, checkpoint processed when due, settlement closes the call, near-miss upgrade to a
new alert call); the sync script against a local bare repo; web reducer/formatting with Vitest.
