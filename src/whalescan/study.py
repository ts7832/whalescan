"""The evidence study's bet table (evidence-study spec §2–§4): one row per big news-market buy, with every fact as
it was known when the bet was placed, the rules it satisfied (decided by the production insider logic), and what a
copier would have paid and earned at each entry delay.
"""

from __future__ import annotations

import bisect
import logging
import math
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd

from whalescan.classify import Blocklist, category_for_tags
from whalescan.config import Config
from whalescan.gate import PositionEvent
from whalescan.history import signal_ts, study_events
from whalescan.history_store import HistoryStore
from whalescan.insider import account_age_days, age_is_inconsistent, evaluate_insider, near_miss_rule
from whalescan.models import Market, WalletProfile
from whalescan.study_math import copy_entry, copy_return

log = logging.getLogger(__name__)
DAY = 86400
LATE_PRINT_S = 600  # with no print yet at the entry time, accept the first one within 10 minutes after it
CROWD_WINDOW_S = DAY
INSIDER_CODES = ("I1", "I2", "I3", "I4", "I5")


def _week(ts: int) -> str:
    d = datetime.fromtimestamp(ts, UTC).date()
    return (d - timedelta(days=d.weekday())).isoformat()


class _Prices:
    """All recorded minute prices, per asset, sorted — one bulk read instead of a query per bet."""

    def __init__(self, store: HistoryStore) -> None:
        df = store.con.execute("SELECT asset, ts, price FROM h_prices ORDER BY asset, ts").df()
        self._by_asset: dict[str, tuple[list[int], list[float]]] = {
            a: (g["ts"].astype(int).tolist(), g["price"].astype(float).tolist()) for a, g in df.groupby("asset")}

    def has(self, asset: str, since: int, until: int) -> bool:
        ts, _ = self._by_asset.get(asset, ([], []))
        i = bisect.bisect_left(ts, since)
        return i < len(ts) and ts[i] <= until

    def at(self, asset: str, t: int, since: int) -> float | None:
        """The last price printed in [since, t] — what the copier knew at time t — or, if nothing printed yet,
        the first print within LATE_PRINT_S after t. Never interpolated."""
        ts, ps = self._by_asset.get(asset, ([], []))
        i = bisect.bisect_right(ts, t) - 1
        if i >= 0 and ts[i] >= since:
            return ps[i]
        j = i + 1
        if j < len(ts) and ts[j] <= t + LATE_PRINT_S:
            return ps[j]
        return None


class _MarketsBefore:
    """Distinct markets each fully-fetched wallet had traded before a time, from the first fill per market."""

    def __init__(self, store: HistoryStore) -> None:
        wallets = store.wallets_with_history()
        firsts: dict[str, list[int]] = {}
        if wallets:
            df = store.con.execute(
                """SELECT wallet, condition_id, min(ts) AS first FROM trades
                   WHERE list_contains(?, wallet) GROUP BY wallet, condition_id""", [sorted(wallets)]).df()
            for w, g in df.groupby("wallet"):
                firsts[w] = sorted(g["first"].astype(int).tolist())
        self._wallets, self._firsts = wallets, firsts

    def count(self, wallet: str, ts: int) -> int | None:
        if wallet not in self._wallets:
            return None
        return bisect.bisect_left(self._firsts.get(wallet, []), ts)


def _payout(m: Market, outcome_index: int) -> tuple[float | None, bool]:
    """(payout, irregular) for a resolved market; (None, False) while open."""
    if not m.closed:
        return None, False
    if m.winner_index() is None:
        return None, True
    return float(m.outcome_prices[outcome_index]), False


def _crowd(events: list[PositionEvent], floor: float) -> dict[str, int]:
    """Other wallets that had already made a big buy of the same outcome in the preceding 24 h (known at signal)."""
    by_asset: dict[str, list[PositionEvent]] = {}
    for e in events:
        if e.side == "BUY" and e.usdc >= floor:
            by_asset.setdefault(e.asset, []).append(e)
    out: dict[str, int] = {}
    for group in by_asset.values():
        for e in group:
            t = signal_ts(e)
            out[e.id] = len({o.wallet for o in group
                             if o.wallet != e.wallet and t - CROWD_WINDOW_S <= signal_ts(o) <= t})
    return out


def bets_frame(store: HistoryStore, cfg: Config) -> pd.DataFrame:
    ic, sc = cfg.insider, cfg.study
    floor = cfg.ledger.near_miss_usdc_min
    blocklist = Blocklist(cfg.blocklist)
    events = study_events(store, cfg)
    bets = [e for e in events if e.side == "BUY" and e.usdc >= floor]
    if not bets:
        return pd.DataFrame()
    markets = store.markets_by_id({e.condition_id for e in bets})
    profiles = store.profiles({e.wallet for e in bets})
    prices, before = _Prices(store), _MarketsBefore(store)
    crowd = _crowd(events, floor)
    delays = sc.entry_delays_min
    horizon = (max(delays) + 10) * 60

    rows: list[dict[str, Any]] = []
    for e in bets:
        m = markets.get(e.condition_id)
        if m is None:
            continue
        t = signal_ts(e)
        prof = profiles.get(e.wallet)
        n_before = before.count(e.wallet, e.first_ts)
        at_bet = None if n_before is None else n_before + 1  # the live /traded count includes this market
        profile_at_bet = WalletProfile(e.wallet, prof.created_ts if prof else None, at_bet, t)
        category = category_for_tags(m.tags, cfg.categories)
        ev_at_bet = evaluate_insider(e, profile_at_bet, replace(m, closed=False), category, ic, blocklist, None, t)
        checks = {c.code: c for c in ev_at_bet.checks}
        in_band = checks["I6"].detail == "NO BOOK"  # with no book given, I6 only fails when the price is out of band
        age = account_age_days(e, prof)
        payout, irregular = _payout(m, e.outcome_index)
        resolved = m.closed_ts or m.end_ts
        row: dict[str, Any] = {
            "id": e.id, "wallet": e.wallet, "condition_id": e.condition_id, "asset": e.asset,
            "event_slug": m.event_slug or e.event_slug, "question": m.question, "category": category,
            "outcome": e.outcome, "first_ts": e.first_ts, "signal_ts": t, "week": _week(t), "usdc": e.usdc,
            "whale_price": e.price, "n_fills": e.n_fills,
            "age_days": math.nan if age is None else age, "age_inconsistent": age_is_inconsistent(e, prof),
            "markets_at_bet": math.nan if at_bet is None else at_bet,
            "is_open": not m.closed, "payout": math.nan if payout is None else payout, "irregular": irregular,
            "won": payout == 1.0, "resolved_ts": resolved,
            "r_insider": all(checks[c].passed for c in INSIDER_CODES) and in_band,
            "missed_rule": near_miss_rule(ev_at_bet, profile_at_bet, ic, cfg.ledger),
            "r_fresh": age is not None and age <= sc.history_max_age_days and in_band,
            "r_baseline": in_band,
            "crowd_before": crowd.get(e.id, 0),
            "priced": prices.has(e.asset, t, t + horizon),
        }
        row["r_near_miss"] = row["missed_rule"] is not None
        for d in delays:
            when = t + d * 60
            entry = copy_entry(prices.at(e.asset, when, t), e.price, m, ic, sc)
            row[f"entry_{d}"] = math.nan if entry is None else entry.cost
            scored = entry is not None and payout is not None
            row[f"ret_{d}"] = copy_return(payout, entry) if scored else math.nan
            row[f"days_held_{d}"] = (resolved - when) / DAY if entry is not None and resolved else math.nan
        rows.append(row)
    log.info("study: %d bets (%d resolved cleanly, %d priced)", len(rows),
             sum(1 for r in rows if not math.isnan(r["payout"])), sum(1 for r in rows if r["priced"]))
    return pd.DataFrame(rows)
