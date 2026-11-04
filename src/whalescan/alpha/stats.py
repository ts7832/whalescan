"""Statistics for the alpha hunt (pre-registration §2, §4).

- Uncertainty resamples whole EVENTS (one news event's bets resolve together: one piece of evidence).
- Feature tests use monthly rank ICs with a Newey–West t-stat across months (autocorrelation-robust).
- Multiple testing: Benjamini–Hochberg; the best strategy's Sharpe is deflated for the number of trials.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd
from scipy import stats as st

N_BOOT = 2000
SEED = 20261104


def _f(x: Any) -> float | None:
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def cluster_boot(df: pd.DataFrame, col: str, *, cluster: str = "event_slug", n: int = N_BOOT, seed: int = SEED,
                 alpha: float = 0.10) -> dict[str, Any]:
    """Mean of `col` per bet with a (1 − alpha) interval from resampling whole clusters (ratio of sums)."""
    d = df[[cluster, col]].dropna()
    if d.empty:
        return {"n": 0, "clusters": 0, "mean": None, "lo": None, "hi": None, "p_le0": None}
    g = d.groupby(cluster)[col].agg(["sum", "count"])
    s, c = g["sum"].to_numpy(), g["count"].to_numpy()
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(g), size=(n, len(g)))
    means = s[idx].sum(axis=1) / c[idx].sum(axis=1)
    lo, hi = np.quantile(means, [alpha / 2, 1 - alpha / 2])
    return {"n": int(len(d)), "clusters": int(len(g)), "mean": _f(d[col].mean()), "lo": _f(lo), "hi": _f(hi),
            "p_le0": _f((means <= 0).mean())}


def cluster_boot_spearman(df: pd.DataFrame, x: str, y: str, *, cluster: str = "event_slug", n: int = 500,
                          seed: int = SEED, alpha: float = 0.10) -> dict[str, Any]:
    """Spearman correlation with an event-resampled interval (ranks recomputed in every draw)."""
    d = df[[cluster, x, y]].dropna()
    if len(d) < 30 or d[x].nunique() < 2:
        return {"n": int(len(d)), "ic": None, "lo": None, "hi": None, "p_two": None}
    ic = st.spearmanr(d[x], d[y]).statistic
    groups = [g.index.to_numpy() for _, g in d.groupby(cluster)]
    rng = np.random.default_rng(seed)
    xs, ys = d[x].to_numpy(), d[y].to_numpy()
    draws = []
    for _ in range(n):
        pick = np.concatenate([groups[i] for i in rng.integers(0, len(groups), len(groups))])
        loc = d.index.get_indexer(pick)
        draws.append(st.spearmanr(xs[loc], ys[loc]).statistic)
    draws = np.array(draws)
    lo, hi = np.nanquantile(draws, [alpha / 2, 1 - alpha / 2])
    p_two = 2 * min((draws <= 0).mean(), (draws >= 0).mean())
    return {"n": int(len(d)), "clusters": len(groups), "ic": _f(ic), "lo": _f(lo), "hi": _f(hi), "p_two": _f(p_two)}


def monthly_ic(df: pd.DataFrame, x: str, y: str, *, min_n: int = 50) -> pd.Series:
    """Spearman IC of feature `x` with target `y`, one per calendar month of signal time."""
    d = df[["month", x, y]].dropna()
    out = {}
    for m, g in d.groupby("month"):
        if len(g) >= min_n and g[x].nunique() > 1 and g[y].nunique() > 1:
            out[m] = st.spearmanr(g[x], g[y]).statistic
    return pd.Series(out, dtype=float).sort_index()


def monthly_rank_ic(df: pd.DataFrame, x: str, y: str, *, min_n: int = 50) -> pd.Series:
    """Per calendar month: Pearson correlation between the feature's within-month percentile rank (robust to the
    feature's outliers and scale drift) and the standardised target itself (whose mean is the economic quantity:
    ranking it would throw away exactly the magnitude a 1/0 payoff's edge lives in)."""
    d = df[["month", x, y]].dropna()
    out = {}
    for m, g in d.groupby("month"):
        if len(g) >= min_n and g[x].nunique() > 1 and g[y].std() > 0:
            out[m] = float(np.corrcoef(g[x].rank(pct=True), g[y])[0, 1])
    return pd.Series(out, dtype=float).sort_index()


def newey_west_t(series: pd.Series, lags: int | None = None) -> tuple[float | None, float | None]:
    """t-stat of the mean of a time series with Newey–West (Bartlett) standard errors, and its two-sided p."""
    x = series.dropna().to_numpy()
    T = len(x)
    if T < 4:
        return None, None
    lags = lags if lags is not None else max(1, int(4 * (T / 100) ** (2 / 9)))
    e = x - x.mean()
    var = e @ e / T
    for k in range(1, lags + 1):
        var += 2 * (1 - k / (lags + 1)) * (e[k:] @ e[:-k]) / T
    if var <= 0:
        return None, None
    t = x.mean() / math.sqrt(var / T)
    return _f(t), _f(2 * st.t.sf(abs(t), df=T - 1))


def benjamini_hochberg(p: list[float | None], q: float = 0.10) -> list[bool]:
    """Which hypotheses are rejected at false-discovery rate q (None p-values are never rejected)."""
    idx = [i for i, v in enumerate(p) if v is not None]
    m = len(idx)
    order = sorted(idx, key=lambda i: p[i])
    k_max = 0
    for rank, i in enumerate(order, start=1):
        if p[i] <= q * rank / m:
            k_max = rank
    keep = set(order[:k_max])
    return [i in keep for i in range(len(p))]


def deflated_sharpe(returns: np.ndarray, n_trials: int, sr_trials_std: float) -> dict[str, Any]:
    """Bailey & López de Prado (2014): probability the true Sharpe exceeds the best Sharpe expected from
    `n_trials` skill-less strategies (per-period returns; Sharpe not annualised)."""
    r = np.asarray(returns, dtype=float)
    r = r[np.isfinite(r)]
    T = len(r)
    if T < 10 or r.std(ddof=1) == 0:
        return {"sr": None, "sr0": None, "dsr": None, "T": T}
    sr = r.mean() / r.std(ddof=1)
    g3, g4 = st.skew(r), st.kurtosis(r, fisher=False)
    emc = 0.5772156649
    n = max(n_trials, 2)
    sr0 = sr_trials_std * ((1 - emc) * st.norm.ppf(1 - 1 / n) + emc * st.norm.ppf(1 - 1 / (n * math.e)))
    denom = math.sqrt(max(1e-12, 1 - g3 * sr + (g4 - 1) / 4 * sr ** 2))
    dsr = st.norm.cdf((sr - sr0) * math.sqrt(T - 1) / denom)
    return {"sr": _f(sr), "sr0": _f(sr0), "dsr": _f(dsr), "T": T, "skew": _f(g3), "kurt": _f(g4)}


def quantile_cuts(x: pd.Series, k: int = 5) -> list[float]:
    """Inner cut points of `k` quantile buckets (fixed on the period they were learned on)."""
    return np.unique(np.nanquantile(x.dropna(), np.linspace(0, 1, k + 1)[1:-1])).tolist()


def bucket(x: pd.Series, cuts: list[float]) -> pd.Series:
    edges = [-math.inf, *cuts, math.inf]
    return pd.cut(x, edges, labels=False, duplicates="drop")
