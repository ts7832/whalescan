# Strict insider rules — pre-registration

**Written:** 2026-09-30, BEFORE any of these rules were evaluated. Motivation (the user): experienced whale-call
accounts post "a handful a year", preferring quality over quantity; WHALESCAN's INSIDER rule (≤ 7 d, ≤ 10 markets,
≥ $10k) fires 4–12 times a week and tested NO-GO. Hypothesis: an edge exists only in far stricter, rarer cases.

All rules: BUY, news-category market (the insider categories), account age known and consistent, markets traded
known from a COMPLETE history (`markets_at_bet` counts this market), copier enters 15 min after the signal at the last
price + half spread + fee, skipped past the chase limit — exactly as in the evidence study.

| Rule | Account age at bet | Markets at bet | Position | Whale price |
|---|---|---|---|---|
| S1 brand-new, all-in | ≤ 1 day | 1 (first ever) | ≥ $25,000 | any in band |
| S2 brand-new long shot | ≤ 1 day | 1 | ≥ $25,000 | ≤ 0.35 |
| S3 whale-size fresh | ≤ 3 days | ≤ 3 | ≥ $50,000 | any in band |
| S4 extreme | ≤ 1 day | 1 | ≥ $50,000 | ≤ 0.35 |

Reported for every rule, win or lose: bets, distinct wallets, bets per year, hit rate, mean return per dollar with a
wallet-clustered 90 % CI, per-wallet t, over the whole period and over the test period (from the evidence study's
cut). No other slices will be reported as findings; anything else is labelled exploratory.

## Addendum — S5 confirmed-insider watchlist (added 2026-09-30, still before any evaluation)

Motivation (the user): the whale-call account keeps watching wallets it has confirmed as insiders and posts their
later bets — e.g. wallets repeatedly winning recurring MicroStrategy (MSTR) purchase-announcement markets.

- **Confirmation:** a wallet is confirmed by a *long-shot win*: while its account was ≤ 7 days old it bought
  ≥ $10,000 at a whale price ≤ 0.35 in a studied market, and that outcome won.
- **Confirmed from:** the resolution time of that market (earlier, nobody could know it was right) — no look-ahead.
- **Followed:** every later big buy (≥ $5,000, entered strictly after the confirmation time) by a confirmed wallet,
  in any studied market, whatever the wallet's age by then; the confirming bet itself is never scored.
- **Reported** exactly like S1–S4. If MSTR-type corporate-announcement markets turn out to be missing from the
  dataset (tagged outside the news categories), they are added to the dataset first and the rule is run once on
  the extended data; the addition is reported.
