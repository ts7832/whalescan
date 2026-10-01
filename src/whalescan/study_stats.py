"""Honest statistics for the evidence study (evidence-study spec §5–§7).

- Rules are chosen on the older bets and judged on the newest (`time_split`).
- Uncertainty resamples wallets, not bets: ten bets by one insider on one piece of news are one piece of evidence.
- Factor buckets are defined on the training period and applied unchanged to the test period.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

HORIZONS = [("<1d", 0.0, 1.0), ("1-7d", 1.0, 7.0), ("7-30d", 7.0, 30.0), ("30-90d", 30.0, 90.0),
            (">90d", 90.0, math.inf)]


def _num(x: float) -> float | None:
    return None if x is None or not math.isfinite(x) else float(x)


def time_split(df: pd.DataFrame, train_fraction: float) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Older `train_fraction` of bets by signal time, and the rest — never a random split, which would let the
    rule 'see' the same news period it is judged on."""
    ordered = df.sort_values("signal_ts", kind="stable")
    cut = int(len(ordered) * train_fraction)
    return ordered.iloc[:cut], ordered.iloc[cut:]


def wallet_bootstrap_ci(df: pd.DataFrame, col: str, *, n: int, seed: int,
                        alpha: float = 0.10) -> tuple[float | None, float | None, float | None]:
    """(mean, lower, upper) of `col` per bet, with a (1 − alpha) interval from resampling whole wallets."""
    d = df[["wallet", col]].dropna()
    if d.empty:
        return None, None, None
    g = d.groupby("wallet")[col].agg(["sum", "count"])
    sums, counts = g["sum"].to_numpy(), g["count"].to_numpy()
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(g), size=(n, len(g)))
    means = sums[idx].sum(axis=1) / counts[idx].sum(axis=1)
    lo, hi = np.quantile(means, [alpha / 2, 1 - alpha / 2])
    return _num(d[col].mean()), _num(lo), _num(hi)


def per_wallet_t(df: pd.DataFrame, col: str) -> tuple[float | None, int]:
    """t-statistic of the per-wallet mean returns, and the number of wallets — each wallet counted once."""
    m = df[["wallet", col]].dropna().groupby("wallet")[col].mean()
    k = len(m)
    if k < 2 or m.std(ddof=1) == 0:
        return None, k
    return _num(m.mean() / (m.std(ddof=1) / math.sqrt(k))), k


def rule_summary(df: pd.DataFrame, rule_col: str, *, delay: int, n_boot: int, seed: int,
                 min_wallets: int) -> dict[str, Any]:
    ret, days = f"ret_{delay}", f"days_held_{delay}"
    sub = df[df[rule_col].astype(bool)]
    scored = sub[sub[ret].notna()]
    n_wallets = int(scored["wallet"].nunique()) if not scored.empty else 0
    mean, lo, hi = wallet_bootstrap_ci(scored, ret, n=n_boot, seed=seed)
    t, _ = per_wallet_t(scored, ret)
    mean_days = scored[days].mean() if days in scored and not scored.empty else math.nan
    skipped = 0
    if {"priced", "payout"} <= set(sub.columns):
        skipped = int((sub["priced"].astype(bool) & sub["payout"].notna() & sub[ret].isna()).sum())
    return {
        "status": "OK" if n_wallets >= min_wallets else "INSUFFICIENT",
        "n_bets": int(len(scored)), "n_wallets": n_wallets, "n_skipped": skipped,
        "hit_rate": _num(scored["won"].astype(float).mean()) if not scored.empty else None,
        "mean_return": mean, "ci_lo": lo, "ci_hi": hi, "wallet_t": t,
        "median_days_held": _num(scored[days].median()) if days in scored and not scored.empty else None,
        "return_per_day": _num(mean / mean_days) if mean is not None and mean_days and mean_days > 0 else None,
    }


def bets_per_week(df: pd.DataFrame, rule_col: str, *, since_week: str | None = None) -> dict[str, Any]:
    """Qualifying bets per calendar week (by bet time, resolved or not). Only weeks from `since_week` count — the
    study loads markets that ENDED in its lookback, so earlier weeks see only long-lived markets and would drag
    the rate down. The first and last covered weeks are partial and dropped; weeks with no bet count as zero."""
    weeks = sorted(w for w in df["week"].unique() if since_week is None or w >= since_week)[1:-1]
    counts = df[df[rule_col].astype(bool)].groupby("week").size()
    per = [int(counts.get(w, 0)) for w in weeks]
    if not per:
        return {"weeks": 0, "median": None, "min": None, "max": None, "mean": None}
    return {"weeks": len(per), "median": float(np.median(per)), "min": min(per), "max": max(per),
            "mean": float(np.mean(per))}


def bets_per_period(df: pd.DataFrame, rule_col: str, *, period_days: int, since_ts: int | None = None) -> dict[str, Any]:
    """Qualifying bets per `period_days`-day period (by bet time, resolved or not), anchored to the earliest
    covered bet (or `since_ts` if given). A rare signal can show many zero WEEKS even when it fires reliably
    over longer spans — this is the same idea as bets_per_week, generalised to a coarser period (a quarter,
    half a year) that is more honest about how rare the signal really is. The first and last periods are
    partial and dropped; periods with no qualifying bet count as zero."""
    d = df if since_ts is None else df[df["signal_ts"] >= since_ts]
    if d.empty:
        return {"periods": 0, "period_days": period_days, "median": None, "min": None, "max": None, "mean": None}
    period_s = period_days * 86400
    anchor = int(d["signal_ts"].min())
    bucket_of = ((d["signal_ts"] - anchor) // period_s).astype(int)
    # the full span, not just buckets that happen to contain a row: a period with literally no covered bet at
    # all must still count as a zero period, not vanish silently
    last_bucket = int(bucket_of.max())
    periods = list(range(last_bucket + 1))[1:-1]
    if not periods:
        return {"periods": 0, "period_days": period_days, "median": None, "min": None, "max": None, "mean": None}
    qualifying = bucket_of[d[rule_col].astype(bool).to_numpy()]
    counts = qualifying.value_counts()
    per = [int(counts.get(p, 0)) for p in periods]
    return {"periods": len(per), "period_days": period_days, "median": float(np.median(per)), "min": min(per),
            "max": max(per), "mean": float(np.mean(per))}


def _spearman(df: pd.DataFrame, a: str, b: str) -> float | None:
    d = df[[a, b]].dropna()
    if len(d) < 3 or d[a].nunique() < 2 or d[b].nunique() < 2:
        return None
    return _num(d[a].rank().corr(d[b].rank()))


def factor_table(train: pd.DataFrame, test: pd.DataFrame, factor: str, ret_col: str, *,
                 n_buckets: int = 4) -> dict[str, Any]:
    """Quantile buckets of `factor` fixed on train, mean return per bucket on train and test, and whether the
    rank correlation keeps its sign out of sample."""
    tr = train[[factor, ret_col]].dropna()
    te = test[[factor, ret_col]].dropna()
    if tr[factor].nunique() < 2:
        return {"factor": factor, "buckets": [], "train_spearman": None, "test_spearman": None, "holds": False}
    inner = np.unique(np.quantile(tr[factor], np.linspace(0, 1, n_buckets + 1)[1:-1]))
    edges = [-math.inf, *inner.tolist(), math.inf]
    buckets = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        a = tr[(tr[factor] > lo) & (tr[factor] <= hi)][ret_col]
        b = te[(te[factor] > lo) & (te[factor] <= hi)][ret_col]
        buckets.append({"lo": _num(lo), "hi": _num(hi), "train_n": int(len(a)), "train_mean": _num(a.mean()),
                        "test_n": int(len(b)), "test_mean": _num(b.mean()) if len(b) else None})
    s_tr, s_te = _spearman(tr, factor, ret_col), _spearman(te, factor, ret_col)
    holds = s_tr is not None and s_te is not None and s_tr * s_te > 0
    return {"factor": factor, "buckets": buckets, "train_spearman": s_tr, "test_spearman": s_te, "holds": holds}


def horizon_table(df: pd.DataFrame, *, delay: int) -> list[dict[str, Any]]:
    """Returns by how long money stays tied up, with return per dollar per day held."""
    ret, days = f"ret_{delay}", f"days_held_{delay}"
    d = df[[ret, days]].dropna()
    out = []
    for name, lo, hi in HORIZONS:
        b = d[(d[days] >= lo) & (d[days] < hi)]
        mean, md = (b[ret].mean(), b[days].mean()) if len(b) else (math.nan, math.nan)
        out.append({"bucket": name, "n": int(len(b)), "mean_return": _num(mean),
                    "mean_days": _num(md), "return_per_day": _num(mean / md) if len(b) and md > 0 else None})
    return out
