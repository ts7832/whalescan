"""Cost-to-follow from an order book snapshot, computed by the C++ book engine."""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

import whalecore
from whalescan.models import BookSnapshot


@dataclass(frozen=True)
class FollowQuote:
    vwap: float  # NaN when nothing could be filled
    complete: bool
    book_as_of: int  # unix seconds
    best_bid: float | None
    best_ask: float | None
    microprice: float | None
    levels: tuple[tuple[float, float], ...]  # best asks, ascending


def _levels(rows: tuple[tuple[float, float], ...]) -> np.ndarray:
    return np.array(rows, dtype=np.float64).reshape(-1, 2)


def follow_quote(snap: BookSnapshot, size_usdc: float) -> FollowQuote:
    b = whalecore.OrderBook()
    b.apply_snapshot(_levels(snap.bids), _levels(snap.asks))
    walk = b.walk(whalecore.Side.ASK, size_usdc)
    return FollowQuote(
        vwap=walk.vwap if walk.filled_shares > 0 else math.nan,
        complete=walk.complete,
        book_as_of=snap.ts_ms // 1000,
        best_bid=b.best_bid(),
        best_ask=b.best_ask(),
        microprice=b.microprice(),
        levels=tuple(sorted(((p, s) for p, s in snap.asks if s > 0)))[:10],
    )
