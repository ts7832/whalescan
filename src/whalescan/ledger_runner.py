"""Recording and tracking calls for the Track Record ledger (Track Record spec §1-§4).

`record_calls` looks at every evaluation from one evaluate_window() run and logs the ones worth logging
(INSIDER/SNIPER alerts, and I2/I3/I4 near-misses) — each priced at the real cost of buying $1,000 from the
live book right now. `process_marks` advances every open call: settles it if its market has resolved,
otherwise records any checkpoint that is due.
"""

from __future__ import annotations

import logging
import math
from typing import Any

from whalescan.batch import Apis, WindowResult, _guarded
from whalescan.book import follow_quote
from whalescan.config import Config
from whalescan.gate import Evaluation
from whalescan.insider import account_age_days, near_miss_rule
from whalescan.ledger import Ledger
from whalescan.ledger_math import checkpoint_return, checkpoint_schedule, entry_cost, final_return
from whalescan.models import Market, WalletProfile

log = logging.getLogger(__name__)
DAY = 86400
MISSING_CHECKPOINT_GRACE_S = 24 * 3600


def _call_kind(e: Evaluation, profile: WalletProfile | None, cfg: Config) -> tuple[str, str | None] | None:
    """(kind, missed_rule) for an evaluation worth logging, or None. missed_rule is set only for NEAR_MISS.

    A wallet/market that fully qualifies (I1-I5 pass) but whose I6 failed only because the gate couldn't
    read a book still counts as INSIDER: record_calls does its own book fetch (or estimated fallback) for
    the entry price, independent of the gate's, so "no book at gate time" must not silently drop the call.
    """
    is_insider_ladder = bool(e.checks) and e.checks[0].code == "I1"
    if is_insider_ladder:
        checks = {c.code: c for c in e.checks}
        core_pass = all(checks[code].passed for code in ("I1", "I2", "I3", "I4", "I5"))
        book_readable_or_unknown = checks["I6"].passed or checks["I6"].detail == "NO BOOK"
        if core_pass and book_readable_or_unknown:
            return "INSIDER", None
        rule = near_miss_rule(e, profile, cfg.insider, cfg.ledger)
        return ("NEAR_MISS", rule) if rule else None
    if e.status == "SIGNAL":
        return "SNIPER", None
    return None


def _fee_market(call: dict[str, Any]) -> Market:
    """A minimal stand-in Market carrying only the fee schedule captured when the call was logged, used
    when a fresh Market isn't available at mark time."""
    return Market(call["condition_id"], "", "", "", None, False, None, (), (), call["fees_enabled"],
                  call["fee_rate"], call["fee_exponent"], 0.0, ())


async def _entry_quote(apis: Apis, cfg: Config, asset: str) -> tuple[float | None, bool]:
    """(vwap, complete) from walking the live book for cfg.ledger.entry_size_usdc, or (None, False)."""
    snap = await _guarded(apis.clob.book(asset), f"ledger entry book {asset}")
    if snap is None:
        return None, False
    q = follow_quote(snap, cfg.ledger.entry_size_usdc)
    if not q.complete or math.isnan(q.vwap):
        return None, False
    return q.vwap, True


def _build_call(call_id: str, kind: str, missed_rule: str | None, e: Evaluation, profile: WalletProfile | None,
                market: Market | None, entry_vwap: float, entry_estimated: bool, cfg: Config,
                now: int) -> dict[str, Any]:
    ev = e.event
    cost = entry_cost(entry_vwap, market) if market else entry_vwap
    end_ts = market.end_ts if market else None
    sched = checkpoint_schedule(now, end_ts, cfg.ledger)
    return {
        "id": call_id, "kind": kind, "missed_rule": missed_rule, "call_ts": now, "event_ts": ev.first_ts,
        "wallet": ev.wallet, "condition_id": ev.condition_id, "asset": ev.asset, "outcome": ev.outcome,
        "outcome_index": ev.outcome_index, "category": e.category, "tier": e.tier,
        "question": market.question if market else e.event.title, "end_ts": end_ts,
        "whale_price": ev.price, "usdc": ev.usdc, "n_fills": ev.n_fills,
        "entry_vwap": entry_vwap, "entry_estimated": entry_estimated, "entry_cost": cost,
        "fees_enabled": market.fees_enabled if market else False,
        "fee_rate": market.fee_rate if market else 0.0, "fee_exponent": market.fee_exponent if market else 1.0,
        "age_days": account_age_days(ev, profile), "markets_traded": profile.markets_traded if profile else None,
        "consensus": len(e.consensus), "volume": market.volume if market else None,
        "schedule": [{"day": round((t - now) / DAY), "due_ts": t} for t in sched],
        "checks": [{"code": c.code, "passed": c.passed, "detail": c.detail} for c in e.checks],
    }


async def record_calls(ledger: Ledger, result: WindowResult, apis: Apis, cfg: Config, now: int) -> int:
    """Log any new call surfaced by this evaluation window. Returns how many were logged."""
    logged = 0
    for e in result.evaluations:
        kind_rule = _call_kind(e, result.profiles.get(e.event.wallet), cfg)
        if kind_rule is None:
            continue
        kind, missed_rule = kind_rule
        call_id = f"{e.event.id}:{kind}"
        if ledger.has_call(call_id):
            continue
        market = result.markets.get(e.event.condition_id)
        vwap, complete = await _entry_quote(apis, cfg, e.event.asset)
        if complete:
            entry_vwap, entry_estimated = vwap, False
        else:
            entry_vwap, entry_estimated = e.event.price + cfg.validation.slippage, True
        ledger.append_call(_build_call(call_id, kind, missed_rule, e, result.profiles.get(e.event.wallet), market,
                                       entry_vwap, entry_estimated, cfg, now))
        logged += 1
    return logged


async def process_marks(ledger: Ledger, apis: Apis, cfg: Config, now: int) -> int:
    """Advance every open call: settle it if its market has resolved, else record any due checkpoint."""
    open_calls = ledger.open_calls()
    if not open_calls:
        return 0
    cids = {c["condition_id"] for c in open_calls}
    found = await _guarded(apis.gamma.markets(cids), "ledger markets") or {}
    processed = 0

    for call in open_calls:
        market = found.get(call["condition_id"])
        if market is None or not market.closed:
            continue
        payout = market.outcome_prices[call["outcome_index"]]
        irregular = market.winner_index() is None
        ledger.append_mark({"call_id": call["id"], "type": "SETTLEMENT", "day": None, "at": now,
                            "best_bid": None, "payout": payout, "irregular": irregular,
                            "return_pct": final_return(payout, call["entry_cost"])})
        processed += 1

    for call, day, due_ts in ledger.due_checkpoints(now):
        snap = await _guarded(apis.clob.book(call["asset"]), f"ledger checkpoint book {call['asset']}")
        best_bid = max((p for p, s in snap.bids if s > 0), default=None) if snap is not None else None
        if best_bid is None:
            if now - due_ts > MISSING_CHECKPOINT_GRACE_S:
                ledger.append_mark({"call_id": call["id"], "type": "CHECKPOINT", "day": day, "at": now,
                                    "best_bid": None, "return_pct": None, "missing": True})
                processed += 1
            continue
        market = found.get(call["condition_id"]) or _fee_market(call)
        ledger.append_mark({"call_id": call["id"], "type": "CHECKPOINT", "day": day, "at": now,
                            "best_bid": best_bid, "return_pct": checkpoint_return(best_bid, call["entry_cost"],
                                                                                 market)})
        processed += 1
    return processed
