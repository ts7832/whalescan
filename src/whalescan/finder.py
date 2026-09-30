"""Insider Finder v2's move table (spec §1): one row per covered big bet, with whether the price moved the
wallet's way within 24 h ("informed"), plus the same copy-trade columns as the evidence study's bets_frame.
"""

from __future__ import annotations

import bisect
import logging
import math
from typing import Any

import numpy as np
import pandas as pd

from whalescan.classify import Blocklist, category_for_tags
from whalescan.config import Config
from whalescan.finder_math import is_covered, known_at, move_hit
from whalescan.gate import Evaluation
from whalescan.history import signal_ts, study_events
from whalescan.history_store import HistoryStore
from whalescan.ledger import Ledger
from whalescan.models import Market
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
    """All recorded Finder price points, per asset, sorted — one bulk read, then a range query per bet. Reads
    ONLY h_finder_prices (never the evidence study's h_prices): a slot window that was never fetched must read
    as truly empty here, not silently filled in by the study's own, differently-scoped price data."""

    def __init__(self, store: HistoryStore) -> None:
        df = store.con.execute("SELECT asset, ts, price FROM h_finder_prices ORDER BY asset, ts").df()
        self._by_asset: dict[str, tuple[list[int], list[float]]] = {
            a: (g["ts"].astype(int).tolist(), g["price"].astype(float).tolist()) for a, g in df.groupby("asset")}

    def after(self, asset: str, since_exclusive: int, until_inclusive: int) -> list[tuple[int, float]]:
        ts, ps = self._by_asset.get(asset, ([], []))
        i = bisect.bisect_right(ts, since_exclusive)
        j = bisect.bisect_right(ts, until_inclusive)
        return list(zip(ts[i:j], ps[i:j]))

    def at_or_before(self, asset: str, t: int, since: int) -> float | None:
        """Latest recorded price in [since, t] — never a print from before `since` (the signal): a gap right
        after the signal must give no entry at all, not a stale pre-signal price that understates the cost."""
        ts, ps = self._by_asset.get(asset, ([], []))
        i = bisect.bisect_right(ts, t) - 1
        return ps[i] if i >= 0 and ts[i] >= since else None


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
            entry = None if already_resolved else copy_entry(prices.at_or_before(e.asset, when, t), e.price, m, ic, sc)
            row[f"entry_{d}"] = math.nan if entry is None else entry.cost
            scored = entry is not None and payout is not None
            row[f"ret_{d}"] = copy_return(payout, entry) if scored else math.nan
            row[f"days_held_{d}"] = (resolved - when) / DAY if entry is not None and resolved else math.nan
        rows.append(row)
    log.info("finder: %d covered big bets (%d with a known move outcome)", len(rows),
             sum(1 for r in rows if not (isinstance(r["move_hit"], float) and math.isnan(r["move_hit"]))))
    return pd.DataFrame(rows)


def base_rates(train: pd.DataFrame) -> dict[str, float]:
    """Per-bucket probability a covered big bet's price moves the wallet's way within 24h — learned only from
    bets with a KNOWN outcome, on the training period (spec §1: never the test period)."""
    known = train[train["move_hit"].notna()]
    if known.empty:
        return {}
    return known.groupby("bucket")["move_hit"].mean().to_dict()


def qualify(moves: pd.DataFrame, rates: dict[str, float], min_bets: int, min_z: float) -> pd.Series:
    """Boolean per row of `moves`: is the wallet INFORMED at THIS bet's signal_ts? Walk-forward — for each bet,
    only the SAME wallet's other bets whose move outcome was already known_at-or-before this bet's signal_ts
    contribute (never this bet's own move: known_ts is always strictly after its own signal_ts, so it can never
    appear among its own "already known" evidence; a defensive check below removes it if it somehow did)."""
    result = pd.Series(False, index=moves.index)
    if moves.empty:
        return result
    scored = moves[moves["move_hit"].notna()].copy()
    scored["b"] = scored["bucket"].map(rates)
    scored = scored[scored["b"].notna()]

    for wallet, g in moves.groupby("wallet", sort=False):
        wk = scored[scored["wallet"] == wallet].sort_values("known_ts", kind="stable")
        if wk.empty:
            continue
        known_ts_list = wk["known_ts"].tolist()
        h_arr = wk["move_hit"].to_numpy(dtype=float)
        b_arr = wk["b"].to_numpy(dtype=float)
        v_arr = b_arr * (1.0 - b_arr)
        cum_h = np.concatenate([[0.0], np.cumsum(h_arr)])
        cum_e = np.concatenate([[0.0], np.cumsum(b_arr)])
        cum_v = np.concatenate([[0.0], np.cumsum(v_arr)])
        pos_of_idx = {idx: i for i, idx in enumerate(wk.index)}

        for idx, row in g.iterrows():
            pos = bisect.bisect_right(known_ts_list, row["signal_ts"])
            n, h_sum, e_sum, v_sum = pos, cum_h[pos], cum_e[pos], cum_v[pos]
            self_pos = pos_of_idx.get(idx)
            if self_pos is not None and self_pos < pos:  # defensive; should not occur (known_ts > signal_ts)
                n -= 1
                h_sum -= h_arr[self_pos]
                e_sum -= b_arr[self_pos]
                v_sum -= v_arr[self_pos]
            if n < min_bets or v_sum <= 0:
                continue
            z = (h_sum - e_sum) / math.sqrt(v_sum)
            result.loc[idx] = bool(z >= min_z)
    return result


CONFIRMING_KINDS = ("INSIDER", "NEAR_MISS", "INFORMED")


def confirmed_wallets(ledger: Ledger, now: int, cfg: Config) -> set[str]:
    """Wallets whose Track Record shows a settled, clean WIN on an INSIDER/NEAR_MISS/INFORMED call that
    resolved strictly before `now` (spec §1: CONFIRMED INSIDER). A wallet is removed once it has accumulated
    at least [finder].demote_after settled CONFIRMED-kind calls of its own (its live track record since being
    confirmed) whose mean per-dollar return is negative."""
    fc = cfg.finder
    calls = {c["id"]: c for c in ledger.calls()}
    settled = {m["call_id"]: m for m in ledger.marks() if m["type"] == "SETTLEMENT"}

    confirmed: set[str] = set()
    for cid, c in calls.items():
        if c["kind"] not in CONFIRMING_KINDS:
            continue
        m = settled.get(cid)
        if m is None or m.get("irregular") or m["at"] >= now:
            continue
        if m["return_pct"] is not None and m["return_pct"] > 0:
            confirmed.add(c["wallet"])

    demoted: set[str] = set()
    for wallet in confirmed:
        later = [settled[cid]["return_pct"] for cid, c in calls.items()
                 if c["wallet"] == wallet and c["kind"] == "CONFIRMED" and cid in settled
                 and not settled[cid].get("irregular") and settled[cid]["return_pct"] is not None]
        if len(later) >= fc.demote_after and sum(later) / len(later) < 0:
            demoted.add(wallet)
    return confirmed - demoted


def is_confirmed_alert(e: Evaluation, market: Market | None, confirmed: set[str], cfg: Config,
                       blocklist: Blocklist) -> bool:
    """Is this evaluation a live confirmed-insider alert: a big BUY in the tradeable price band, in a covered
    market that hasn't expired or closed, by a wallet on the watchlist? Shared by sweep.py's live alert
    injection and ledger_runner.py's call logging, so both apply exactly the same eligibility."""
    ic, fc = cfg.insider, cfg.finder
    ev = e.event
    return (ev.wallet in confirmed and ev.side == "BUY" and e.status != "EXPIRED"
            and ev.usdc >= fc.min_usdc and ic.price_min <= ev.price <= ic.price_max
            and market is not None and is_covered(market, cfg, blocklist))
