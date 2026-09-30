"""Insider Finder v2's move table (spec §1): one row per covered big bet, with whether the price moved the
wallet's way within 24 h ("informed"), plus the same copy-trade columns as the evidence study's bets_frame.
"""

from __future__ import annotations

import bisect
import logging
import math
from typing import Any

import pandas as pd

from whalescan.classify import Blocklist, category_for_tags
from whalescan.config import Config
from whalescan.finder_math import is_covered, known_at, move_hit
from whalescan.history import signal_ts, study_events
from whalescan.history_store import HistoryStore
from whalescan.study import _payout, _week
from whalescan.study_math import copy_entry, copy_return

log = logging.getLogger(__name__)
DAY = 86400

# Base-rate buckets for the whale's own price (spec §1) — fixed, not tuned. The top edge (0.95) sits above
# [insider].price_max (0.90), so that bucket is always empty in practice; kept for fidelity to the pre-registration.
BUCKET_EDGES = (0.02, 0.20, 0.40, 0.60, 0.80, 0.90, 0.95)


def bucket_of(p0: float) -> str | float:
    """The pre-registered price bucket p0 falls in, as "lo-hi"; NaN if outside every bucket (should not occur
    for a covered bet, which is already price-banded, but never silently mis-bucket one that somehow is)."""
    for lo, hi in zip(BUCKET_EDGES, BUCKET_EDGES[1:]):
        if lo <= p0 <= hi:
            return f"{lo:.2f}-{hi:.2f}"
    return math.nan


class _MovePrices:
    """All recorded minute prices, per asset, sorted — one bulk read, then a range query per bet (mirrors
    study.py's _Prices, but returns every point in a window rather than the single price known "as of" a time)."""

    def __init__(self, store: HistoryStore) -> None:
        df = store.con.execute("SELECT asset, ts, price FROM h_prices ORDER BY asset, ts").df()
        self._by_asset: dict[str, tuple[list[int], list[float]]] = {
            a: (g["ts"].astype(int).tolist(), g["price"].astype(float).tolist()) for a, g in df.groupby("asset")}

    def after(self, asset: str, since_exclusive: int, until_inclusive: int) -> list[tuple[int, float]]:
        ts, ps = self._by_asset.get(asset, ([], []))
        i = bisect.bisect_right(ts, since_exclusive)
        j = bisect.bisect_right(ts, until_inclusive)
        return list(zip(ts[i:j], ps[i:j]))

    def at_or_before(self, asset: str, t: int) -> float | None:
        """Latest recorded price at or before `t` — used for copy-trade entry pricing, same rule as study.py's
        _Prices.at (a late print up to a short grace period after `t` also counts there; moves_frame reuses the
        stricter "no look-ahead beyond the exact time" rule since it is only ever called at t <= known_ts)."""
        ts, ps = self._by_asset.get(asset, ([], []))
        i = bisect.bisect_right(ts, t) - 1
        return ps[i] if i >= 0 else None


def moves_frame(store: HistoryStore, cfg: Config) -> pd.DataFrame:
    fc, sc, ic = cfg.finder, cfg.study, cfg.insider
    blocklist = Blocklist(cfg.blocklist)
    events = study_events(store, cfg, news_only=False)
    bets = [e for e in events if e.side == "BUY" and e.usdc >= fc.min_usdc]
    if not bets:
        return pd.DataFrame()
    markets = store.markets_by_id({e.condition_id for e in bets})
    prices = _MovePrices(store)
    delays = sc.entry_delays_min

    rows: list[dict[str, Any]] = []
    for e in bets:
        m = markets.get(e.condition_id)
        # defence in depth: covered_market_ids() trusts registration-time filtering (build_history), so a
        # market mis-registered upstream (or, in older databases, registered before is_covered existed) must
        # never leak a sports/price-threshold/blocklisted row into the Finder's own numbers.
        if m is None or not is_covered(m, cfg, blocklist) or not (ic.price_min <= e.price <= ic.price_max):
            continue
        t = signal_ts(e)
        category = category_for_tags(m.tags, cfg.categories)
        payout, irregular = _payout(m, e.outcome_index)
        resolved = m.closed_ts or m.end_ts
        deadline = known_at(t, m.closed_ts if m.closed else None, fc)
        points = prices.after(e.asset, t, deadline)
        row: dict[str, Any] = {
            "id": e.id, "wallet": e.wallet, "condition_id": e.condition_id, "asset": e.asset,
            "event_slug": m.event_slug or e.event_slug, "question": m.question, "category": category,
            "outcome": e.outcome, "first_ts": e.first_ts, "signal_ts": t, "week": _week(t), "usdc": e.usdc,
            "p0": e.price, "bucket": bucket_of(e.price), "known_ts": deadline,
            "move_hit": math.nan if (h := move_hit(points, t, e.price, m.closed_ts if m.closed else None, fc)) is None
                       else float(h),
            "is_open": not m.closed, "payout": math.nan if payout is None else payout, "irregular": irregular,
            "won": payout == 1.0, "resolved_ts": resolved,
        }
        for d in delays:
            when = t + d * 60
            already_resolved = m.closed and resolved is not None and when >= resolved
            entry = None if already_resolved else copy_entry(prices.at_or_before(e.asset, when), e.price, m, ic, sc)
            row[f"entry_{d}"] = math.nan if entry is None else entry.cost
            scored = entry is not None and payout is not None
            row[f"ret_{d}"] = copy_return(payout, entry) if scored else math.nan
            row[f"days_held_{d}"] = (resolved - when) / DAY if entry is not None and resolved else math.nan
        rows.append(row)
    log.info("finder: %d covered big bets (%d with a known move outcome)", len(rows),
             sum(1 for r in rows if not (isinstance(r["move_hit"], float) and math.isnan(r["move_hit"]))))
    return pd.DataFrame(rows)
