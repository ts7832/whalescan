# Insider Finder v2 — Implementation Plan

**Spec:** `docs/superpowers/specs/2026-09-30-insider-finder-v2-design.md` (binding). **Branch:** `insider-finder`.
**Execution:** inline, TDD per task (watch each test fail first), one fresh Opus review at the end.

## Under the hood

- **"Early" vs "new":** a wallet can be old and still consistently buy before prices jump; that is the informed
  pattern. The move is measured without waiting for resolution.
- **Base rate:** favourites drift toward 1 anyway, so "the price went up" means nothing unless it happens more often
  than for similar bets. Each bet's expected hit chance comes from its price bucket, learned on older data only.
- **z-score:** how many standard deviations the wallet's hits exceed what luck predicts; 2.33 ≈ 1 % chance by luck.
- **Walk-forward qualification:** at each bet, the wallet's score may only use moves whose 24 h window had already
  finished — otherwise the bet being judged would help qualify its own wallet (look-ahead).

## Global constraints

Config in `config.toml [finder]`; reuse `aggregate`, `HistoryStore`, `copy_entry/copy_return`, `rule_summary`,
`time_split`, report writers. No look-ahead (each has a test). Private outputs stay git-ignored. Stage explicit paths.

## Review focus

1. A bet's own move (or any move finishing after its signal) never contributes to its wallet's qualification.
2. Base rates use training bets only; test-period bets never feed them.
3. Price-market regex and the SPORTS exclusion are applied exactly as pre-registered.
4. The CONFIRMED badge only uses ledger calls that had SETTLED before the new bet; demotion uses only later settled calls.
5. No private data (watch lists derived for live use, reports) is committed or published.

## Tasks

| # | Deliverable | Files | Tests written first (must fail before the code) |
|---|---|---|---|
| 1 | `[finder]` config + `FinderCfg`; pure `is_price_market(question)`, `is_covered(market, cfg, blocklist)`, `move_target(p0, cfg)`, `move_hit(points, t0, p0, end_ts, cfg) -> bool \| None` | `config.toml`, `config.py`, `src/whalescan/finder_math.py` | regex examples (hit/not hit incl. "MicroStrategy announce purchase" = covered, "MSTR hit (HIGH) $125" = price market); target at p0 = 0.25 → 0.45, 0.90 → 0.95; hit inside 24 h, miss, hit after 24 h = miss, window cut by market close, no points = None |
| 2 | History: `build_history(..., finder=True)` lists covered markets (not only news), fetches their fills; slot windows (asset, 6-h slot) of 30 h at fidelity 10 for big bets in closed covered markets; progress logs | `history.py`, `history_store.py` (window key includes span) | covered-market selection (sports/price excluded); one window per slot; second run fetches nothing; existing tests still pass |
| 3 | `moves_frame(store, cfg)`: one row per big bet — ids, wallet, signal_ts, known_ts, p0, bucket, move_hit (bool/NaN), plus the copy-trade columns from `bets_frame` (entry/ret per delay, payout, days_held) | `src/whalescan/finder.py` | known_ts = min(t + 24h, close); unknown when no prices; sports excluded |
| 4 | `base_rates(train)` per bucket; `qualify(moves, rates, min_bets, min_z) -> Series[bool]` walk-forward per bet (vectorised per wallet with a sorted known_ts cursor) | `finder.py` | own move excluded; a move known 1 s after the signal excluded; n and z thresholds exact on a hand example; rates from train only |
| 5 | `finder_results(df, cfg)` + `whalescan finder-study` → local report (headline test period, all 3 variants, bets/week, delay curve, GO/NO-GO), reusing `study_report` helpers | `finder_report.py`, `cli.py` | headline is test-only; variants all reported; strict JSON; CLI prints headline + path |
| 6 | Live CONFIRMED badge: `confirmed_wallets(ledger, now, cfg)` (settled WIN before now; demotion rule); sweep flags big bets by them (`kind: CONFIRMED`, badge text), ledger logs kind `CONFIRMED`; dashboard badge | `ledger_runner.py`/`finder.py`, `batch.py`/`sweep.py`, `web/src/...` | only settled-before-now wins confirm; demotion after 5 losing later calls; alert carries badge; web test for badge rendering helper |
| 7 | Real run: `whalescan history` (extended), `whalescan finder-study`; fresh Opus review; fix pass; merge. INFORMED live wiring only if GO (separate small plan) | — | — |
