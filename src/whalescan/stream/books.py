"""Live L2 books for watched tokens, kept honest against the server's own top of book.

A book is usable only while it is known to be current:
- it belongs to the active subscription (late messages for dropped tokens are ignored);
- it agrees with the server's best bid/ask after every update (else: desynced until a fresh snapshot);
- the feed is up (a link drop desyncs every book: deltas missed while down can't be recovered).
"""

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
        self._heartbeat_ms = 0
        self.desynced: set[str] = set()
        self.subscribed: set[str] = set()

    def book(self, asset: str) -> whalecore.OrderBook | None:
        return self._books.get(asset)

    def subscribe(self, assets: Iterable[str]) -> None:
        """The token set the socket is (re)subscribing to; books outside it are dropped."""
        self.subscribed = set(assets)
        self.forget(set(self._books) - self.subscribed)

    def link_down(self) -> None:
        self.desynced |= set(self._books)

    def heartbeat(self, ms: int) -> None:
        """Any message on a healthy link proves quiet books are still current."""
        self._heartbeat_ms = max(self._heartbeat_ms, ms)

    def on_snapshot(self, snap: BookSnapshot) -> None:
        if snap.asset not in self.subscribed:
            return  # late message from a subscription we already dropped
        if snap.asset not in self.desynced and snap.ts_ms < self._as_of_ms.get(snap.asset, -1):
            return  # an older snapshot (e.g. slow REST reply) must not overwrite newer WS state
        b = self._books.setdefault(snap.asset, whalecore.OrderBook())
        try:
            b.apply_snapshot(_levels(snap.bids), _levels(snap.asks))
        except ValueError as e:
            log.warning("book %s rejected snapshot: %s", snap.asset, e)
            self.desynced.add(snap.asset)
            return
        self._as_of_ms[snap.asset] = snap.ts_ms
        self.desynced.discard(snap.asset)

    def on_changes(self, msg: PriceChanges) -> set[str]:
        """Apply one message; return the assets that just went out of sync (need a resnapshot)."""
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
            last = idx[-1]  # the server's top of book after the last change for this asset
            if b.crossed() or not (_same(b.best_bid(), msg.best_bids[last]) and _same(b.best_ask(), msg.best_asks[last])):
                newly.add(asset)
            else:
                self._as_of_ms[asset] = max(self._as_of_ms.get(asset, 0), msg.ts_ms)
        new = newly - self.desynced
        self.desynced |= newly
        return new

    def quote(self, asset: str, size_usdc: float) -> FollowQuote | None:
        """Cost to buy `size_usdc` now, or None when the book is unknown or not known to be current."""
        b = self._books.get(asset)
        if b is None or asset in self.desynced:
            return None
        walk = b.walk(whalecore.Side.ASK, size_usdc)
        return FollowQuote(
            vwap=walk.vwap if walk.filled_shares > 0 else math.nan,
            complete=walk.complete,
            book_as_of=max(self._as_of_ms.get(asset, 0), self._heartbeat_ms) // 1000,
            best_bid=b.best_bid(),
            best_ask=b.best_ask(),
            microprice=b.microprice(),
            levels=tuple(b.levels(whalecore.Side.ASK, 10)),
        )

    def forget(self, assets: Iterable[str]) -> None:
        for a in list(assets):
            self._books.pop(a, None)
            self._as_of_ms.pop(a, None)
            self.desynced.discard(a)
