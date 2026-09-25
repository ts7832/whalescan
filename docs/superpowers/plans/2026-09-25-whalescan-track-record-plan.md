# WHALESCAN Track Record — Implementation Plan

**Goal:** every call WHALESCAN makes is logged with a realistic entry price, followed on the user's checkpoint schedule
until settlement, and analysed (winners vs losers) — stored for free on a `ledger` branch and shown on the dashboard.
**Spec:** `docs/superpowers/specs/2026-09-25-whalescan-track-record-design.md` (binding).
**Builds on:** `main` at 1c86c8d (insider scanner, 15-minute sweep loop, data branch).
**Execution:** inline, TDD per task, one independent whole-branch review at the end.

---

## Under the hood: the new ideas in this plan

**Append-only ledger.** Instead of a database table that gets updated, every fact is a new line in a file: "call X
was made at price 0.43", later "call X was worth 0.51 on day 7", later "call X settled at 1". Nothing is ever
overwritten, so the history can't be quietly rewritten — the same principle as an accounting ledger or a bank
statement. Current state (is X open? what's its latest return?) is *derived* by replaying the lines.

**JSON Lines (`.jsonl`).** One JSON object per line. Appending is just writing a line; reading is splitting on
newlines; a damaged line affects only itself. In git, each new call is a one-line diff — easy to review and audit.

**Git as a free database.** The ledger lives on its own branch. Every sweep that changes it makes a commit, so git
history is a complete, timestamped audit trail, hosted by GitHub for free. Fine at this scale (tens of calls a day);
at millions of rows a real database would be needed.

**Mark-to-market.** Valuing an open position at what it could be sold for *now*. On a prediction market you sell into
the **best bid** (the highest price a buyer is offering), not the mid-price — using the mid would flatter every
return. The taker fee on selling is subtracted too.

**Return %.** `(what you get − what you paid) ÷ what you paid`. Paid = entry cost per share (VWAP + fee). Bought at
0.43 all-in, settles at 1 → (1 − 0.43)/0.43 = +133%. Settles at 0 → −100%.

**Mann–Whitney U test.** Asks "do winners tend to have *smaller* account ages than losers?" by ranking all values
together and checking whether one group's ranks sit systematically lower. It doesn't assume a bell curve — important
here, because bet sizes and account ages are heavily skewed. It returns a p-value like the Monte Carlo test did.

**Logistic regression + AUC.** A model that turns features (age, size, …) into a probability of winning. It is
*cross-validated*: trained on 4/5 of the data and tested on the remaining 1/5, five times, so the score reflects
calls it has never seen. **AUC** is the probability that a random winner gets a higher score than a random loser:
0.5 = coin flip, 1.0 = perfect. With few rows such models memorise noise, hence the 200-call minimum.

---

## Global constraints

- Entry = order-book cost of `ledger.entry_size_usdc` ($1,000) at the call, fees included; whale price stored too.
- Checkpoints: days 7, 14, 21, 28, then every 30 days; every 91 days after day 28 if end ≥ 3 years after the call.
- Checkpoint value = best bid − taker fee; settlement value = final outcome price.
- Calls and marks are append-only; ids `<event id>:<kind>`; a call is logged at most once per kind.
- Only the 15-minute sweep writes the ledger.
- All thresholds in `config.toml [ledger]`.
- Stage explicit paths only; no `git add -A`.

## Review focus

1. A market that settles between two sweeps must produce exactly one SETTLED mark, and no checkpoint after it.
2. Sweeps that run late (GitHub drops runs) must still record each due checkpoint once, with the real time.
3. A near-miss that becomes an alert produces two calls with two entry prices — never an edited row.
4. Empty books and 50/50 resolutions must never produce NaN/Infinity in `summary.json`.
5. The ledger branch must never be force-pushed (unlike the data branch).

---

## Tasks

| # | Deliverable | Files | Tests written first |
|---|---|---|---|
| 1 | **Ledger math** (pure): `checkpoint_schedule(call_ts, end_ts, cfg) -> list[int]`; `entry_cost(vwap, market) -> float`; `checkpoint_return(best_bid, entry_cost, market) -> float \| None`; `final_return(payout, entry_cost) -> float` | `src/whalescan/ledger_math.py`, config `[ledger]` + `LedgerCfg` | exact schedules (short market, no end date, ≥ 3 y quarterly, end before day 7), return formulas incl. fees, no-bid → None |
| 2 | **Ledger store**: `Ledger(dir)` with `calls()`, `marks()`, `has_call(id)`, `append_call(dict)`, `append_mark(dict)`, `open_calls()`, `due_checkpoints(now) -> list[(call, day, due_ts)]`, `latest_mark(id)`; corrupt lines skipped | `src/whalescan/ledger.py` | append/reload, dedupe, due list after a late sweep (each due checkpoint once), settled call has no due checkpoints, corrupt line tolerated |
| 3 | **Near-miss rule**: `near_miss_rule(evaluation, profile, cfg) -> str \| None` ("I2"/"I3"/"I4" when exactly one fails within the bands, I1/I5 pass, and I6 passes or is only "NO BOOK") | `src/whalescan/insider.py` | each band edge, two failures → None, I6 "PRICE MOVED" → None, I6 "NO BOOK" still a near-miss |
| 4 | **Window result**: `evaluate_window` returns `WindowResult(signals, contacts, evaluations, markets, profiles)`; the insider prefilter uses `ledger.near_min_usdc` so $5–10k candidates get profiles | `src/whalescan/batch.py`, `src/whalescan/sweep.py` | batch/sweep tests keep passing; a $7k fresh-wallet bet now has a profile |
| 5 | **Recording + tracking** (async): `record_calls(ledger, result, apis, cfg, now)` (new INSIDER/SNIPER/NEAR_MISS calls, book entry, features); `process_marks(ledger, apis, cfg, now)` (due checkpoints from books; settlements from Gamma) | `src/whalescan/ledger_runner.py` | new call recorded with book entry; estimated entry when book fails; checkpoint due → mark; settlement → one SETTLED mark and closed; upgrade near-miss → alert = second call |
| 6 | **Summary + analysis**: `build_summary(calls, marks, cfg, now) -> dict` (totals, rates, recent calls, analysis block); Mann–Whitney U, quantile buckets, logistic regression with 5-fold AUC (numpy only) | `src/whalescan/ledger_analysis.py` | Mann–Whitney against a hand-computed example; buckets; model below/above 200; strict JSON |
| 7 | **Integration**: `scripts/ledger-sync.sh pull\|push`; sweep runs record → marks → summary each round; `whalescan ledger [--backfill]`; sweep workflow pulls/pushes the ledger | `scripts/`, `src/whalescan/sweep.py`, `cli.py`, `.github/workflows/sweep.yml`, `.gitignore` | sync against a local bare repo (init, push, pull, no force); CLI wiring; backfill logs current alerts once |
| 8 | **Dashboard**: panel 05 tabs VALIDATION / TRACK RECORD; `VITE_LEDGER_URL`; types; recent-calls table; headline stats | `web/src/...` | Vitest: summary loading/fallback, formatting of returns/status |
| 9 | **Real run + review**: backfill on the real alerts, one real sweep writing the ledger branch, fresh-context review, fixes, merge, deploy | — | — |
