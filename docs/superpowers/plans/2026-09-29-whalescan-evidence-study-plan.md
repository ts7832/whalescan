# WHALESCAN Evidence Study — Implementation Plan

**Goal:** an honest, out-of-sample, per-dollar, after-cost, position-sized answer to "would copying these calls make
money, and how often would it bet?" — plus a permanent archive of every large fill.
**Spec:** `docs/superpowers/specs/2026-09-29-whalescan-evidence-study-design.md` (binding).
**Builds on:** `main` at 27f8e46. **Execution:** inline, TDD per task, one fresh whole-branch review at the end.

---

## Under the hood: the ideas this plan relies on

**Per share vs per dollar.** "+17¢ per share, 87 % hit rate" hides the price. Buying at 0.90 and winning pays 10¢ on
90¢ risked (+11 %); losing costs all 90¢ (−100 %). Nine wins and one loss at 0.90 is +0 % per dollar, not +7¢ of
glory. Every number in this study is per dollar risked.

**Look-ahead bias.** Using any fact the trader could not have known at bet time — today's market count of a wallet,
the final volume of a market, prices after entry — makes a backtest look better than reality. Account age is taken
at the bet's first fill; markets traded are counted only before it; entry uses the price *after* the whale's fill.

**Entry delay.** You cannot buy at the whale's price: you see the fill later and others react first. The study
prices entry 1, 5, 15 and 60 minutes after the whale, adds half a spread and the fee, and skips the bet if the price
ran further than the chase limit. The delay curve tells us whether the 15-minute sweep is fast enough or the live
station is needed.

**Walk-forward (out-of-sample) testing.** If rules are tuned and scored on the same bets, the score is optimistic —
with enough knobs anything looks profitable. Rules are chosen on the older ⅔ of bets and scored once on the newest ⅓.

**Clustered bootstrap.** Ten bets from one insider on one piece of news are one piece of evidence, not ten. The
bootstrap resamples whole wallets (with all their bets) thousands of times; the spread of the results is the honest
uncertainty.

**Kelly sizing.** For a bet that costs *c* and wins with probability *p*, betting the fraction `(p − c)/(1 − c)` of
the bankroll maximises long-run growth — *if p is known*. It never is, and over-betting is ruinous, so the estimate is
shrunk toward the market price and only a quarter of Kelly is used, under hard caps.

**Capital lock-up.** Money in a position that settles in four months cannot be used meanwhile. The simulation is
cash-constrained, and returns are also expressed per dollar per day held.

---

## Global constraints

- Reuse production logic: `aggregate()`, `account_age_days`/`age_is_inconsistent`, the insider thresholds, fee math.
- No look-ahead anywhere (see above); every such fact has a test.
- Config in `config.toml [study]`; nothing hard-coded.
- The dataset and archive contain public market data only. No secrets, no user wallet data. Stage explicit paths.

## Review focus

1. Any look-ahead: a fact used at bet time that is only known later.
2. Returns per dollar with fees and the delayed entry, and skipped bets that are really skipped (not scored at 0).
3. The headline number is from the test period only; the rule was not tuned on it.
4. Bootstrap resamples wallets (CI) and weeks (bankroll paths), not individual bets.
5. The simulation cannot spend cash that is locked in open positions, and caps are enforced.
6. Incremental re-runs never duplicate fills or bets.

---

## Tasks

| # | Deliverable | Files | Tests written first |
|---|---|---|---|
| 1 | **Copy-trade math** (pure): `copy_entry(price_after, whale_price, market, cfg) -> Entry \| None` (half-spread, chase limit, band, fee); `copy_return(payout, entry)`; `kelly_fraction(p, c)`; `shrink(p_hat, n, c, k)` | `src/whalescan/study_math.py`, config `[study]` + `StudyCfg` | chase-limit skip, band skip, fee included, per-dollar return, Kelly edge cases (p ≤ c → 0), shrinkage limits |
| 2 | **History store**: `data/history.duckdb` tables markets/fills/profiles/wallet_history/prices; incremental "what's missing" queries | `src/whalescan/history_store.py` | idempotent upserts, missing-work queries, no duplicate fills |
| 3 | **History builder** `build_history(apis, store, cfg, now)`: list closed + open news markets (paginated Gamma), fills ≥ $1k per market (paginated `/trades?market=`), profiles, pre-bet wallet histories for young wallets, post-bet minute prices | `src/whalescan/history.py`, `api/gamma.py` (`closed_markets`), `api/data_api.py` (`market_trades`), `api/clob.py` (`price_window`) | pagination + offset cap, incremental second run fetches nothing new, only young wallets get histories |
| 4 | **Bet table** `bets_frame(store, cfg)`: production `aggregate()` → position events; age at first fill; markets traded before; rule flags (INSIDER, NEAR_MISS, FRESH_ALL, BASELINE); entry/return per delay; days held; category, horizon bucket, factors | `src/whalescan/study.py` | no look-ahead (later fills/markets don't leak), inconsistent ages excluded, irregular payouts excluded, skipped ≠ loss |
| 5 | **Statistics**: time split, wallet-clustered bootstrap CI, per-wallet t, bucket + Spearman factor tables (train/test), bets-per-week, horizon table | `src/whalescan/study_stats.py` | CI on a known distribution, clustering widens CI vs naive, split is by time, week counting |
| 6 | **Bankroll simulation**: cash-constrained chronological sim; flat / fixed-fraction / ¼-Kelly; caps; aggregation modes; week-block bootstrap paths | `src/whalescan/study_sim.py` | cash lock-up respected, caps enforced, Kelly zero when no edge, deterministic with seed |
| 7 | **Report + CLI**: `whalescan history`, `whalescan study` → markdown report, CSV bet log, JSON; go/no-go block | `src/whalescan/study_report.py`, `cli.py` | report sections present, headline uses test period, JSON strict |
| 8 | **Fill archive**: sweep appends round fills to `archive/fills/YYYY-MM-DD.jsonl.gz`; `scripts/archive-sync.sh` (never force-push); workflow wiring | `src/whalescan/archive.py`, `scripts/`, `.github/workflows/sweep.yml` | append-only, dedupe by fill key, day rollover, sync against a bare repo |
| 9 | **Real run + report + review**: build history on live data, run the study, fresh-context review, fixes, merge | — | — |
