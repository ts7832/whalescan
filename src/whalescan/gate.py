"""Signal gate (spec §4.6): merge fills into position events, then apply checks G1–G7."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

import pandas as pd

from whalescan.book import FollowQuote
from whalescan.classify import Blocklist, category_for_tags
from whalescan.config import CategoriesCfg, GateCfg
from whalescan.models import Market, Trade


@dataclass(frozen=True)
class PositionEvent:
    wallet: str
    asset: str
    condition_id: str
    side: str
    first_ts: int
    last_ts: int
    usdc: float
    shares: float
    price: float  # VWAP of the merged fills
    n_fills: int
    event_slug: str
    title: str
    outcome: str
    outcome_index: int

    @property
    def id(self) -> str:
        return f"{self.wallet}:{self.asset}:{self.side}:{self.first_ts}"


def _merge(group: list[Trade]) -> PositionEvent:
    first = group[0]
    usdc = sum(t.usdc for t in group)
    shares = sum(t.size for t in group)
    return PositionEvent(first.wallet, first.asset, first.condition_id, first.side, first.ts, group[-1].ts, usdc,
                         shares, usdc / shares if shares > 0 else first.price, len(group), first.event_slug,
                         first.title, first.outcome, first.outcome_index)


def aggregate(trades: Iterable[Trade], window_s: int) -> list[PositionEvent]:
    """Fills by the same (wallet, asset, side) within `window_s` of the first fill become one event."""
    def key(t: Trade) -> tuple[str, str, str]:
        return (t.wallet, t.asset, t.side)

    events: list[PositionEvent] = []
    group: list[Trade] = []
    for t in sorted(trades, key=lambda t: (key(t), t.ts)):
        if group and key(t) == key(group[0]) and t.ts - group[0].ts <= window_s:
            group.append(t)
        else:
            if group:
                events.append(_merge(group))
            group = [t]
    if group:
        events.append(_merge(group))
    return sorted(events, key=lambda e: (e.first_ts, e.id))


@dataclass(frozen=True)
class WalletView:
    basis: str  # "CATEGORY" | "OVERALL" | "NONE"
    post_edge: float
    edge: float
    p_value: float
    n_cat: int
    median_stake: float


class ScoreBook:
    def __init__(self, scores: pd.DataFrame, fallback_max_cat_positions: int) -> None:
        self._rows = {(r.wallet, r.category): r for r in scores.itertuples(index=False)}
        self._fallback = fallback_max_cat_positions

    def view(self, wallet: str, category: str) -> WalletView | None:
        overall = self._rows.get((wallet, "ALL"))
        if overall is None:
            return None
        cat = self._rows.get((wallet, category))
        median = float(overall.median_stake)
        if cat is not None and bool(cat.certified):
            return WalletView("CATEGORY", float(cat.post_edge), float(cat.edge), float(cat.p_value), int(cat.n), median)
        n_cat = int(cat.n) if cat is not None else 0
        cat_ok = cat is None or float(cat.edge) >= 0.0
        if bool(overall.certified) and n_cat < self._fallback and cat_ok:
            return WalletView("OVERALL", float(overall.post_edge), float(overall.edge), float(overall.p_value), n_cat,
                              median)
        return WalletView("NONE", 0.0, float(overall.edge), float(overall.p_value), n_cat, median)

    def certified(self, wallet: str, category: str) -> bool:
        v = self.view(wallet, category)
        return v is not None and v.basis != "NONE"

    def certified_wallets(self) -> set[str]:
        return {w for (w, _), r in self._rows.items() if bool(r.certified)}


@dataclass(frozen=True)
class Check:
    code: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class Evaluation:
    event: PositionEvent
    category: str
    checks: tuple[Check, ...]
    status: str  # SIGNAL | REJECTED | EXIT | CONFLICT | EXPIRED
    tier: str | None
    post_edge: float | None
    net_edge: float | None
    max_entry: float | None
    fee: float | None  # fee per share at the whale's price
    quote: FollowQuote | None
    consensus: tuple[str, ...]

    def failed(self) -> list[Check]:
        return [c for c in self.checks if not c.passed]


@dataclass(frozen=True)
class GateContext:
    cfg: GateCfg
    categories: CategoriesCfg
    blocklist: Blocklist
    scores: ScoreBook
    markets: Mapping[str, Market]
    events: Sequence[PositionEvent]
    now: int | None
    historical: bool = False

    def category(self, condition_id: str) -> str:
        m = self.markets.get(condition_id)
        return category_for_tags(m.tags, self.categories) if m else "OTHER"


def _g5(ev: PositionEvent, market: Market | None, ctx: GateContext, now: int) -> Check:
    if market is None:
        return Check("G5", False, "UNKNOWN MARKET")
    if ctx.blocklist.blocked(event_slug=market.event_slug or ev.event_slug, slug=market.slug, volume=market.volume):
        return Check("G5", False, "BLOCKLISTED")
    if market.closed and not ctx.historical:
        return Check("G5", False, "MARKET CLOSED")
    if market.end_ts is None:
        return Check("G5", True, "NO END DATE")
    hours = (market.end_ts - now) / 3600
    return Check("G5", hours >= ctx.cfg.min_hours_to_end, f"{hours:.1f}H TO END")


def evaluate(ev: PositionEvent, ctx: GateContext, quote: FollowQuote | None) -> Evaluation:
    g = ctx.cfg
    market = ctx.markets.get(ev.condition_id)
    category = ctx.category(ev.condition_id)
    now = ev.first_ts if ctx.historical or ctx.now is None else ctx.now
    view = ctx.scores.view(ev.wallet, category)
    certified = view is not None and view.basis != "NONE"
    window = g.conflict_window_h * 3600

    def certified_buy_nearby(o: PositionEvent) -> bool:
        # Historical evaluation may only see what existed at decision time: no later events.
        if ctx.historical and o.first_ts > ev.first_ts:
            return False
        return (o.side == "BUY" and abs(o.first_ts - ev.first_ts) <= window
                and ctx.scores.certified(o.wallet, category))

    checks = [Check("G1", ev.side == "BUY", "BUY" if ev.side == "BUY" else "SELL · EXIT")]

    if certified:
        checks.append(Check("G2", True, f"CERTIFIED {view.basis} · EDGE {view.post_edge:+.3f}"))
    else:
        checks.append(Check("G2", False, "UNKNOWN WALLET" if view is None else "NOT CERTIFIED"))

    median = view.median_stake if view else math.nan
    multiple = ev.usdc / median if median and median > 0 else math.nan
    g3 = ev.usdc >= g.min_usdc and not math.isnan(multiple) and multiple >= g.conviction_k
    checks.append(Check("G3", g3, f"${ev.usdc:,.0f} · {multiple:.1f}× MEDIAN" if view else f"${ev.usdc:,.0f}"))

    checks.append(Check("G4", g.price_min <= ev.price <= g.price_max, f"PRICE {ev.price:.3f}"))
    checks.append(_g5(ev, market, ctx, now))

    net: float | None = None
    if not certified or market is None:
        checks.append(Check("G6", False, "N/A"))
    elif quote is None:
        checks.append(Check("G6", False, "NO BOOK"))
    elif not quote.complete or math.isnan(quote.vwap):
        checks.append(Check("G6", False, "BOOK TOO THIN"))
    else:
        net = view.post_edge - (quote.vwap - ev.price) - market.fee_per_share(quote.vwap)
        checks.append(Check("G6", net >= g.min_net_edge, f"FOLLOW {quote.vwap:.3f} · NET {net:+.3f}"))

    opposing = [o for o in ctx.events
                if o.condition_id == ev.condition_id and o.asset != ev.asset and certified_buy_nearby(o)]
    n_opp = len({o.wallet for o in opposing})
    checks.append(Check("G7", not opposing, "NO CONFLICT" if not opposing else f"CONFLICT · {n_opp} OPPOSING"))

    consensus = {o.wallet for o in ctx.events if o.asset == ev.asset and certified_buy_nearby(o)}
    if certified and ev.side == "BUY":
        consensus.add(ev.wallet)

    post = view.post_edge if certified else None
    fee = market.fee_per_share(ev.price) if market else None
    max_entry = ev.price + post - (fee or 0.0) - g.max_entry_margin if post is not None else None

    expired = not ctx.historical and (
        (market is not None and market.closed)
        or (ctx.now is not None and ctx.now - ev.last_ts > g.signal_lookback_h * 3600))
    if expired:
        status = "EXPIRED"
    elif ev.side != "BUY":
        status = "EXIT"
    elif all(c.passed for c in checks):
        status = "SIGNAL"
    elif certified and not checks[6].passed:
        status = "CONFLICT"
    else:
        status = "REJECTED"

    tier = None
    if status == "SIGNAL":
        strong = (net is not None and net >= g.tier_a_net_edge) or len(consensus) >= g.tier_a_consensus
        tier = "A" if strong else "B"
    return Evaluation(ev, category, tuple(checks), status, tier, post, net, max_entry, fee, quote,
                      tuple(sorted(consensus)))


def needs_book(e: Evaluation) -> bool:
    """True when fetching an order book could turn this evaluation into a signal."""
    failed = e.failed()
    return e.status == "REJECTED" and len(failed) == 1 and failed[0].code == "G6" and failed[0].detail == "NO BOOK"
