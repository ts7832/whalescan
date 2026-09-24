"""Domain objects. Everything downstream of the API boundary uses these, never raw JSON."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


def resolved_winner(closed: bool, outcome_prices: Sequence[float]) -> int | None:
    """Index of the winning outcome if the market resolved cleanly (exactly one 1, rest 0), else None.

    Fractional resolutions (50/50 splits, voids) return None: the no-skill null y* ~ Bernoulli(p)
    cannot produce fractional outcomes, so such markets are excluded from scoring.
    """
    if not closed or len(outcome_prices) < 2:
        return None
    ones = [i for i, p in enumerate(outcome_prices) if p == 1.0]
    zeros = sum(1 for p in outcome_prices if p == 0.0)
    if len(ones) == 1 and zeros == len(outcome_prices) - 1:
        return ones[0]
    return None


@dataclass(frozen=True, slots=True)
class Trade:
    tx_hash: str
    ts: int
    wallet: str
    asset: str
    condition_id: str
    side: str  # "BUY" | "SELL"
    price: float
    size: float  # shares
    event_slug: str
    title: str
    outcome: str
    outcome_index: int
    fee: float | None

    @property
    def usdc(self) -> float:
        return self.price * self.size


@dataclass(frozen=True, slots=True)
class ClosedPosition:
    wallet: str
    asset: str
    condition_id: str
    avg_price: float
    total_bought: float  # shares
    realized_pnl: float
    cur_price: float
    outcome: str
    outcome_index: int
    title: str
    event_slug: str
    ts: int


@dataclass(frozen=True, slots=True)
class Market:
    condition_id: str
    question: str
    slug: str
    event_slug: str
    end_ts: int | None
    closed: bool
    closed_ts: int | None
    outcome_prices: tuple[float, ...]
    token_ids: tuple[str, ...]
    fees_enabled: bool
    fee_rate: float
    fee_exponent: float
    volume: float
    tags: tuple[str, ...]

    def winner_index(self) -> int | None:
        return resolved_winner(self.closed, self.outcome_prices)

    def fee_per_share(self, price: float) -> float:
        """Taker fee in USDC per share at `price`: rate × (p(1−p))^exponent (verified on live fills)."""
        if not self.fees_enabled or self.fee_rate <= 0:
            return 0.0
        return self.fee_rate * (price * (1.0 - price)) ** self.fee_exponent


@dataclass(frozen=True, slots=True)
class LeaderboardEntry:
    wallet: str
    rank: int
    volume: float
    pnl: float
    name: str


@dataclass(frozen=True, slots=True)
class BookSnapshot:
    asset: str
    ts_ms: int
    bids: tuple[tuple[float, float], ...]
    asks: tuple[tuple[float, float], ...]


@dataclass(frozen=True, slots=True)
class PricePoint:
    ts: int
    price: float
