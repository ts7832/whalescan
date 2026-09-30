"""The pre-registered strict insider rules (docs/superpowers/specs/2026-09-30-strict-insider-rules-preregistration.md).

S1–S4 are rarer, more extreme versions of the insider pattern. S5 is the confirmed-insider watchlist: a wallet that
won a fresh-account long shot is followed from the moment that win RESOLVED (no earlier: nobody could know it was
right) — its later big buys are the calls. Definitions are fixed; do not tune them on results.
"""

from __future__ import annotations

import pandas as pd

STRICT_RULES = {
    "s1": "S1 brand-new, all-in: <= 1 day old, first market, >= $25k",
    "s2": "S2 brand-new long shot: S1 at a price <= 0.35",
    "s3": "S3 whale-size fresh: <= 3 days old, <= 3 markets, >= $50k",
    "s4": "S4 extreme: <= 1 day old, first market, >= $50k, price <= 0.35",
    "s5": "S5 confirmed insider: later >= $5k buys by a wallet that won a fresh long shot (from its resolution)",
}

CONFIRM_MAX_AGE_DAYS, CONFIRM_MIN_USDC, CONFIRM_MAX_PRICE = 7.0, 10_000.0, 0.35
FOLLOW_MIN_USDC = 5_000.0


def add_strict_rules(df: pd.DataFrame) -> pd.DataFrame:
    """Return `df` with boolean columns s1..s5. Needs: wallet, signal_ts, age_days, markets_at_bet, usdc,
    whale_price, won, payout, irregular, resolved_ts, r_baseline (in the tradeable price band)."""
    out = df.copy()
    band = out["r_baseline"].astype(bool)
    age, mkts = out["age_days"], out["markets_at_bet"]  # NaN (unknown) fails every comparison: never qualifies
    first = mkts == 1
    out["s1"] = band & (age <= 1) & first & (out["usdc"] >= 25_000)
    out["s2"] = out["s1"] & (out["whale_price"] <= 0.35)
    out["s3"] = band & (age <= 3) & (mkts <= 3) & (out["usdc"] >= 50_000)
    out["s4"] = band & (age <= 1) & first & (out["usdc"] >= 50_000) & (out["whale_price"] <= 0.35)

    confirming = (out["payout"].notna() & ~out["irregular"].astype(bool) & out["won"].astype(bool)
                  & (age <= CONFIRM_MAX_AGE_DAYS) & (out["usdc"] >= CONFIRM_MIN_USDC)
                  & (out["whale_price"] <= CONFIRM_MAX_PRICE))
    confirmed_at = out[confirming].groupby("wallet")["resolved_ts"].min()
    since = out["wallet"].map(confirmed_at)
    out["s5"] = band & (out["usdc"] >= FOLLOW_MIN_USDC) & since.notna() & (out["signal_ts"] > since)
    return out
