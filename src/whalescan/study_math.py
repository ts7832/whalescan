"""Pure math for the evidence study (evidence-study spec §3, §8): what a copier really pays, earns and should stake.

Every return here is per dollar risked, never per share: buying at 0.90 and winning is +11 %, losing is −100 %.
"""

from __future__ import annotations

from dataclasses import dataclass

from whalescan.config import InsiderCfg, StudyCfg
from whalescan.models import Market


@dataclass(frozen=True)
class Entry:
    price: float  # executable price per share: the observed price after the delay plus half a spread
    fee: float    # taker fee per share at that price
    cost: float   # price + fee: what one share really costs


def copy_entry(price_after: float | None, whale_price: float, market: Market, insider: InsiderCfg,
               study: StudyCfg) -> Entry | None:
    """The copier's entry `delay` after the whale, or None when it would not be taken: no price known, the price
    ran past the chase limit (as rule I6), or it left the tradeable band. A skipped bet is not a loss."""
    if price_after is None:
        return None
    price = price_after + study.half_spread
    if price - whale_price > insider.max_slippage:
        return None
    if not (insider.price_min <= price <= insider.price_max):
        return None
    fee = market.fee_per_share(price)
    return Entry(price, fee, price + fee)


def copy_return(payout: float, entry: Entry) -> float:
    """Return per dollar: (payout − cost) / cost, with payout 1 or 0 per share."""
    return (payout - entry.cost) / entry.cost


def kelly_fraction(p: float, cost: float) -> float:
    """Growth-optimal share of bankroll for a binary bet costing `cost` that pays 1 with probability `p`:
    (p − c) / (1 − c). Zero without an edge, or when there is nothing left to win."""
    if cost >= 1.0 or p <= cost:
        return 0.0
    return (p - cost) / (1.0 - cost)


def shrink(p_hat: float, n: int, price: float, k: float) -> float:
    """Pull an estimated win rate from `n` bets toward the market's own price with `k` pseudo-bets: few bets say
    little, and the market price is the best prior for a binary outcome."""
    return (n * p_hat + k * price) / (n + k)
