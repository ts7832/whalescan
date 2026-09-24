"""Wallet skill scoring (spec §4.5): C++ Monte Carlo p-values, empirical-Bayes shrinkage, BH certification."""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

import whalecore
from whalescan.classify import Blocklist, category_for_tags
from whalescan.config import CategoriesCfg, ScoringCfg

log = logging.getLogger(__name__)

ELIGIBLE_COLUMNS = ["wallet", "condition_id", "asset", "category", "p", "y", "w", "stake", "closed_ts", "title", "outcome"]
SCORE_COLUMNS = ["wallet", "category", "n", "n_eff", "edge", "sigma", "post_edge", "p_value", "bh_pass",
                 "certified", "flags", "median_stake", "as_of", "win_rate"]


def apply_history_window(frame: pd.DataFrame) -> pd.DataFrame:
    """Drop positions in markets that closed before a wallet's history window starts.

    A depth-capped /closed-positions fetch only covers the most recent window, while unredeemed
    positions come from the wallet's whole life (mostly losers). Mixing them would bias the wallet
    downward, so both sources are cut to the same window. Unknown close time is kept.
    """
    if frame.empty or "history_start_ts" not in frame:
        return frame
    start = frame["history_start_ts"]
    keep = start.isna() | frame["closed_ts"].isna() | (frame["closed_ts"] >= start)
    return frame[keep.astype(bool)]


def prepare_positions(frame: pd.DataFrame, scoring: ScoringCfg, categories: CategoriesCfg,
                      blocklist: Blocklist) -> pd.DataFrame:
    """Resolved, complete-history, in-band, non-blocklisted positions with winsorized stakes."""
    empty = pd.DataFrame(columns=ELIGIBLE_COLUMNS)
    if frame.empty:
        return empty
    df = apply_history_window(frame)
    df = df[df["winner_index"].notna() & df["complete"].astype(bool)]
    df = df[(df["avg_price"] >= scoring.price_min) & (df["avg_price"] <= scoring.price_max) & (df["total_bought"] > 0)]
    if df.empty:
        return empty
    blocked = np.fromiter(
        (blocklist.blocked(event_slug=str(me or pe or ""), slug=str(ms or ""),
                           volume=None if pd.isna(v) else float(v))
         for me, pe, ms, v in zip(df["market_event_slug"], df["event_slug"], df["market_slug"], df["volume"])),
        dtype=bool, count=len(df))
    df = df[~blocked]
    if df.empty:
        return empty
    stake = df["total_bought"] * df["avg_price"]
    cap = stake.groupby(df["wallet"]).transform(lambda s: s.quantile(scoring.winsor_quantile))
    return pd.DataFrame({
        "wallet": df["wallet"].to_numpy(),
        "condition_id": df["condition_id"].to_numpy(),
        "asset": df["asset"].to_numpy(),
        "category": [category_for_tags(t, categories) for t in df["tags"]],
        "p": df["avg_price"].astype(float).to_numpy(),
        "y": (df["outcome_index"].astype(int).to_numpy() == df["winner_index"].astype(int).to_numpy()).astype(np.uint8),
        "w": np.minimum(stake, cap).to_numpy(),
        "stake": stake.to_numpy(),
        "closed_ts": df["closed_ts"].to_numpy(),
        "title": df["title"].to_numpy(),
        "outcome": df["outcome"].to_numpy(),
    })[ELIGIBLE_COLUMNS]


def benjamini_hochberg(p_values: np.ndarray, q: float) -> np.ndarray:
    """Boolean mask of tests that pass BH at false-discovery rate q."""
    m = len(p_values)
    passed = np.zeros(m, dtype=bool)
    if m == 0:
        return passed
    order = np.argsort(p_values, kind="stable")
    below = p_values[order] <= q * np.arange(1, m + 1) / m
    if below.any():
        k = int(np.max(np.flatnonzero(below)))
        passed[order[:k + 1]] = True
    return passed


def _mc(p: np.ndarray, y: np.ndarray, w: np.ndarray, offsets: np.ndarray, n_sims: int,
        seed: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    res = whalecore.skill_mc_batch(p, y, w, offsets, n_sims=n_sims, seed=seed, n_threads=0)
    return (np.array([r.edge for r in res]), np.array([r.p_value for r in res]),
            np.array([r.n_eff for r in res]))


def _subset(p: np.ndarray, y: np.ndarray, w: np.ndarray, offsets: np.ndarray,
            idx: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    lo, hi = offsets[idx], offsets[idx + 1]
    take = np.concatenate([np.arange(a, b) for a, b in zip(lo, hi)])
    new_offsets = np.concatenate([[0], np.cumsum(hi - lo)]).astype(np.int64)
    return p[take], y[take], w[take], new_offsets


def score_wallets(eligible: pd.DataFrame, flags: pd.Series, scoring: ScoringCfg, *, as_of: int,
                  n_sims: int | None = None) -> pd.DataFrame:
    if eligible.empty:
        return pd.DataFrame(columns=SCORE_COLUMNS)
    n_full = n_sims or scoring.n_sims
    tests = (pd.concat([eligible, eligible.assign(category="ALL")], ignore_index=True)
             .sort_values(["wallet", "category"], kind="stable").reset_index(drop=True))
    tests["w2"] = tests["w"] ** 2
    tests["v"] = tests["w2"] * tests["p"] * (1.0 - tests["p"])
    agg = (tests.groupby(["wallet", "category"], sort=False)
           .agg(n=("p", "size"), sw=("w", "sum"), sv=("v", "sum"), win_rate=("y", "mean")).reset_index())
    offsets = np.concatenate([[0], np.cumsum(agg["n"].to_numpy())]).astype(np.int64)
    p = np.ascontiguousarray(tests["p"].to_numpy(np.float64))
    y = np.ascontiguousarray(tests["y"].to_numpy(np.uint8))
    w = np.ascontiguousarray(tests["w"].to_numpy(np.float64))

    # Pass 1: cheap screen for every test. Pass 2: full precision only where it can matter.
    n_screen = min(scoring.n_sims_screen, n_full)
    edge, p_value, n_eff = _mc(p, y, w, offsets, n_screen, scoring.seed)
    promising = np.flatnonzero(p_value < scoring.screen_p)
    if len(promising) and n_full > n_screen:
        sp, sy, sw, so = _subset(p, y, w, offsets, promising)
        p_value[promising] = _mc(sp, sy, sw, so, n_full, scoring.seed + 1)[1]
    log.info("scored %d tests (%d re-run at %d sims)", len(agg), len(promising), n_full)

    agg["edge"] = edge
    agg["p_value"] = p_value
    agg["n_eff"] = n_eff
    agg["sigma"] = np.sqrt(agg["sv"]) / agg["sw"]
    agg["flags"] = agg["wallet"].map(flags).fillna("").astype(str)
    agg["median_stake"] = agg["wallet"].map(eligible.groupby("wallet")["stake"].median())

    testable = ((agg["n_eff"] >= scoring.min_n_eff) & (agg["flags"] == "")).to_numpy()
    overall = testable & (agg["category"] == "ALL").to_numpy()
    s2 = agg["sigma"].to_numpy() ** 2
    tau2 = 0.0
    if overall.sum() >= 2:
        tau2 = max(0.0, float(np.var(agg["edge"].to_numpy()[overall], ddof=1) - s2[overall].mean()))
    agg["post_edge"] = agg["edge"] * (tau2 / (tau2 + s2)) if tau2 > 0 else 0.0

    bh = np.zeros(len(agg), dtype=bool)
    idx = np.flatnonzero(testable)
    bh[idx] = benjamini_hochberg(agg["p_value"].to_numpy()[idx], scoring.bh_q)
    agg["bh_pass"] = bh
    agg["certified"] = agg["bh_pass"] & (agg["post_edge"] >= scoring.min_post_edge)
    agg["as_of"] = as_of
    return agg[SCORE_COLUMNS]
