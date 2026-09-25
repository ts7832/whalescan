"""Pure math for the Track Record ledger (spec: Track Record design, §2 and §4).

No I/O here — every function is a plain calculation, which is what makes it cheap to test exhaustively.
"""

from __future__ import annotations

from whalescan.config import LedgerCfg
from whalescan.models import Market

DAY = 86400


def checkpoint_schedule(call_ts: int, end_ts: int | None, cfg: LedgerCfg) -> list[int]:
    """Unix timestamps of every checkpoint due for a call made at `call_ts`.

    Days 7/14/21/28 (cfg.checkpoint_days), then every `monthly_step_days`. Once `end_ts` is known and
    is `quarterly_after_years`+ out, checkpoints past that cutoff switch to `quarterly_step_days`.
    With no known `end_ts`, the schedule stays monthly and stops at `quarterly_after_years` out (we
    don't know the market settles far enough away to justify guessing a quarterly cadence).
    """
    # No known end date: stay on the monthly cadence for up to quarterly_after_years and stop there
    # (we don't know it settles far out, so never guess quarterly). A known end >= that far out switches
    # to the quarterly cadence past the cutoff.
    three_years = call_ts + int(cfg.quarterly_after_years * 365 * DAY)
    horizon = end_ts if end_ts is not None else three_years
    quarterly_cutoff = three_years

    out: list[int] = []
    for d in cfg.checkpoint_days:
        t = call_ts + d * DAY
        if t > horizon:
            return out
        out.append(t)

    t = out[-1] if out else call_ts
    while True:
        step = cfg.quarterly_step_days if t >= quarterly_cutoff else cfg.monthly_step_days
        t = t + step * DAY
        if t > horizon:
            return out
        out.append(t)


def entry_cost(vwap: float, market: Market) -> float:
    """Cost per share of entering now: the book VWAP plus the taker fee at that price."""
    return vwap + market.fee_per_share(vwap)


def checkpoint_return(best_bid: float | None, cost: float, market: Market) -> float | None:
    """Mark-to-market return if sold into `best_bid` now (fee-adjusted). None when there is no bid."""
    if not best_bid:
        return None
    proceeds = best_bid - market.fee_per_share(best_bid)
    return (proceeds - cost) / cost


def final_return(payout: float, cost: float) -> float:
    """Return once the market has settled and paid `payout` per share (1 or 0, or fractional if irregular)."""
    return (payout - cost) / cost
