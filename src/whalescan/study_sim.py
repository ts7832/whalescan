"""Paper bankroll simulation for the evidence study (evidence-study spec §8).

Chronological and cash-constrained: a stake leaves the cash balance when the bet is entered and returns (times the
payout) only when its market settles. Equity counts open positions at cost, so caps and sizing never assume a
profit that has not happened yet.
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from whalescan.config import StudyCfg
from whalescan.study_math import kelly_fraction

WEEK = 7 * 86400
MIN_STAKE = 1.0  # a dollar: smaller orders are not worth placing


@dataclass(frozen=True)
class Policy:
    name: str
    sizing: str        # "flat": size × starting bankroll · "fraction": size × equity · "kelly": size × Kelly × equity
    size: float
    aggregation: str = "first"  # "first": one position per outcome · "add": every qualifying wallet adds to it


@dataclass
class SimResult:
    final: float
    taken: int = 0
    stakes: list[float] = field(default_factory=list)
    skipped_caps: int = 0
    skipped_cash: int = 0
    skipped_dup: int = 0
    skipped_no_edge: int = 0
    max_drawdown: float = 0.0
    peak_exposure: float = 0.0


def simulate(bets: pd.DataFrame, policy: Policy, cfg: StudyCfg, *, bankroll: float | None = None) -> SimResult:
    """`bets` columns: entry_ts, resolved_ts, cost (per share, fees included), payout (1 or 0), whale_usdc,
    event_slug, asset, p (estimated win probability; used by Kelly)."""
    start = cfg.bankroll if bankroll is None else bankroll
    cash = start
    open_q: list[tuple[int, int, float, float, str, str]] = []  # (resolved_ts, seq, stake, value, event, asset)
    by_event: dict[str, float] = {}
    by_asset: dict[str, float] = {}
    res = SimResult(final=start)
    peak, seq = start, 0

    def exposure() -> float:
        return sum(by_event.values())

    def track() -> None:
        nonlocal peak
        equity = cash + exposure()
        peak = max(peak, equity)
        res.max_drawdown = max(res.max_drawdown, (peak - equity) / peak if peak > 0 else 0.0)

    def settle_until(t: float) -> None:
        nonlocal cash
        while open_q and open_q[0][0] <= t:
            _, _, stake, value, event, asset = heapq.heappop(open_q)
            cash += value
            by_event[event] -= stake
            by_asset[asset] -= stake
            track()

    for b in bets.sort_values("entry_ts", kind="stable").itertuples(index=False):
        settle_until(b.entry_ts)
        equity = cash + exposure()
        if policy.aggregation == "first" and by_asset.get(b.asset, 0.0) > 0:
            res.skipped_dup += 1
            continue
        if policy.sizing == "flat":
            want = policy.size * start
        elif policy.sizing == "fraction":
            want = policy.size * equity
        else:
            want = policy.size * kelly_fraction(float(b.p), float(b.cost)) * equity
            if want <= 0:
                res.skipped_no_edge += 1
                continue
        room = min(cfg.max_bet_fraction * equity,
                   cfg.max_event_fraction * equity - by_event.get(b.event_slug, 0.0),
                   cfg.max_exposure_fraction * equity - exposure(),
                   cfg.max_whale_share * float(b.whale_usdc))
        stake = min(want, room)
        if stake < MIN_STAKE:
            res.skipped_caps += 1
            continue
        if cash < MIN_STAKE:
            res.skipped_cash += 1
            continue
        stake = min(stake, cash)
        cash -= stake
        by_event[b.event_slug] = by_event.get(b.event_slug, 0.0) + stake
        by_asset[b.asset] = by_asset.get(b.asset, 0.0) + stake
        heapq.heappush(open_q, (int(b.resolved_ts), seq, stake, stake / float(b.cost) * float(b.payout),
                                b.event_slug, b.asset))
        seq += 1
        res.taken += 1
        res.stakes.append(stake)
        res.peak_exposure = max(res.peak_exposure, exposure())
        track()
    settle_until(math.inf)
    res.final = cash
    return res


def bootstrap_paths(bets: pd.DataFrame, policy: Policy, cfg: StudyCfg, *, n: int, seed: int) -> dict[str, Any]:
    """Resample whole weeks of bets (keeping streaks and each bet's holding time) into `n` alternative histories
    of the same length, simulate each, and summarise the spread of outcomes."""
    if bets.empty:
        return {"paths": 0, "median_final": None, "p5_final": None, "median_max_drawdown": None,
                "p95_max_drawdown": None, "p_loss": None}
    t0 = int(bets["entry_ts"].min())
    week = ((bets["entry_ts"] - t0) // WEEK).astype(int)
    groups = [g for _, g in bets.groupby(week)]
    n_weeks = int(week.max()) + 1
    rng = np.random.default_rng(seed)
    finals, dds = [], []
    for _ in range(n):
        parts = []
        for i, k in enumerate(rng.integers(0, len(groups), size=n_weeks)):
            g = groups[k].copy()
            base = t0 + int(((g["entry_ts"].iloc[0] - t0) // WEEK) * WEEK)
            shift = t0 + i * WEEK - base
            g["entry_ts"] = g["entry_ts"] + shift
            g["resolved_ts"] = g["resolved_ts"] + shift
            g["asset"] = g["asset"].astype(str) + f"#{i}"
            g["event_slug"] = g["event_slug"].astype(str) + f"#{i}"
            parts.append(g)
        r = simulate(pd.concat(parts, ignore_index=True), policy, cfg)
        finals.append(r.final)
        dds.append(r.max_drawdown)
    f, d = np.array(finals), np.array(dds)
    return {"paths": n, "median_final": float(np.median(f)), "p5_final": float(np.quantile(f, 0.05)),
            "median_max_drawdown": float(np.median(d)), "p95_max_drawdown": float(np.quantile(d, 0.95)),
            "p_loss": float(np.mean(f < cfg.bankroll))}
