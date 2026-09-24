"""Live L2 books for watched tokens, kept honest against the server's own top of book."""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from collections.abc import Iterable

import numpy as np

import whalecore
from whalescan.book import FollowQuote
from whalescan.models import BookSnapshot
from whalescan.stream.messages import PriceChanges

log = logging.getLogger(__name__)
TOL = 1e-9


def _levels(rows: tuple[tuple[float, float], ...]) -> np.ndarray:
    return np.array(rows, dtype=np.float64).reshape(-1, 2)


def _same(ours: float | None, theirs: float | None) -> bool:
    if ours is None or theirs is None:
        return ours is None and theirs is None
    return math.isclose(ours, theirs, abs_tol=TOL)


class LiveBooks:
    def __init__(self) -> None:
        self._books: dict[str, whalecore.OrderBook] = {}
        self._as_of_ms: dict[str, int] = {}
        self.desynced: set[str] = set()

    def book(self, asset: str) -> whalecore.OrderBook | None:
        return self._books.get(asset)

    def on_snapshot(self, snap: BookSnapshot) -> None:
        b = self._books.setdefault(snap.asset, whalecore.OrderBook())
        b.apply_snapshot(_levels(snap.bids), _levels(snap.asks))
        self._as_of_ms[snap.asset] = snap.ts_ms
        self.desynced.discard(snap.asset)

    def on_changes(self, msg: PriceChanges) -> set[str]:
        """Apply one message; return the assets that just went out of sync (need a REST resnapshot)."""
        rows: dict[str, list[int]] = defaultdict(list)
        for i, asset in enumerate(msg.assets):
            if asset in self._books:
                rows[asset].append(i)
        newly: set[str] = set()
        for asset, idx in rows.items():
            b = self._books[asset]
            try:
                b.apply_deltas(np.array([msg.sides[i] for i in idx], dtype=np.uint8),
                               np.array([msg.prices[i] for i in idx], dtype=np.float64),
                               np.array([msg.sizes[i] for i in idx], dtype=np.float64))
            except ValueError as e:
                log.warning("book %s rejected deltas: %s", asset, e)
                newly.add(asset)
                continue
            last = idx[-1]
            if b.crossed() or not (_same(b.best_bid(), msg.best_bids[last]) and _same(b.best_ask(), msg.best_asks[last])):
                newly.add(asset)
            else:
                self._as_of_ms[asset] = msg.ts_ms
        new = newly - self.desynced
        self.desynced |= newly
        return new

    def quote(self, asset: str, size_usdc: float) -> FollowQuote | None:
        """Cost to buy `size_usdc` now, or None when the book is unknown or out of sync."""
        b = self._books.get(asset)
        if b is None or asset in self.desynced:
            return None
        walk = b.walk(whalecore.Side.ASK, size_usdc)
        return FollowQuote(
            vwap=walk.vwap if walk.filled_shares > 0 else math.nan,
            complete=walk.complete,
            book_as_of=self._as_of_ms.get(asset, 0) // 1000,
            best_bid=b.best_bid(),
            best_ask=b.best_ask(),
            microprice=b.microprice(),
            levels=tuple(b.levels(whalecore.Side.ASK, 10)),
        )

    def forget(self, assets: Iterable[str]) -> None:
        for a in assets:
            self._books.pop(a, None)
            self._as_of_ms.pop(a, None)
            self.desynced.discard(a)
