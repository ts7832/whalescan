"""Polymarket Data API v2: trades, positions, leaderboard. See docs/polymarket-api-notes.md.

v1 was retired on 2026-11-02 (every route answers HTTP 410). v2 wraps rows in {"data": [...], "pagination":
{"next_cursor": ...}}, pages by cursor only (an `offset` parameter is a 400), and spells fields in snake_case."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from whalescan.api.http import ApiError, HttpClient, Params
from whalescan.models import ClosedPosition, LeaderboardEntry, Trade
from whalescan.parsers import parse_closed_position, parse_leaderboard, parse_many, parse_open_position, parse_trade

log = logging.getLogger(__name__)

DATA_API = "https://data-api.polymarket.com"
CLOSED_PAGE = 50
TRADES_PAGE = 500
OPEN_PAGE = 500
LEADERBOARD_PAGE = 50
MAX_OFFSET = 10_000  # rows per listing we read at most (v1's depth cap, kept so histories stay comparable)


@dataclass(frozen=True)
class PositionHistory:
    """`complete`: usable for scoring — the full history, or its most recent contiguous time window.
    `truncated`: the API's depth limit cut it to that window (still unbiased: rows are time-sorted)."""

    positions: list[ClosedPosition]
    complete: bool
    truncated: bool = False


@dataclass(frozen=True)
class TradePage:
    trades: list[Trade]
    complete: bool


class DataApi:
    def __init__(self, http: HttpClient) -> None:
        self._http = http
        self.skipped = 0

    async def _page(self, path: str, params: Params, cursor: str | None) -> tuple[list[dict[str, Any]], str | None]:
        """One page of rows and the cursor of the next one (None at the end).

        Any error body raises: treating it as "end of history" would store a silently truncated or holed history
        that incremental refreshes never repair."""
        query = list(params) + ([("cursor", cursor)] if cursor else [])
        data = await self._http.get_json(f"{DATA_API}{path}", query)
        if not isinstance(data, dict) or not isinstance(data.get("data"), list):
            raise ApiError(f"error object from {path}: {str(data)[:200]}", status=200, body=str(data)[:500])
        nxt = (data.get("pagination") or {}).get("next_cursor")
        return data["data"], (str(nxt) if nxt else None)

    async def closed_positions(self, wallet: str, *, since_ts: int | None = None) -> PositionHistory:
        """All closed positions, newest first; stops at rows older than `since_ts` for incremental refresh.

        Sorted by TIMESTAMP on purpose: the default order is realized PnL descending, and a truncated fetch
        in that order would contain only winners. In time order, hitting the depth limit leaves the
        most recent window, which is still an unbiased sample — so it is returned as complete + truncated.
        """
        out: list[ClosedPosition] = []
        cursor: str | None = None
        while len(out) < MAX_OFFSET:
            rows, cursor = await self._page("/v2/positions", [
                ("user", wallet), ("status", "CLOSED"), ("limit", CLOSED_PAGE),
                ("sort_by", "TIMESTAMP"), ("sort_direction", "DESC"),
            ], cursor)
            batch, skipped = parse_many(rows, parse_closed_position)
            self.skipped += skipped
            if since_ts is not None:
                fresh = [p for p in batch if p.ts >= since_ts]
                out.extend(fresh)
                if len(fresh) < len(batch):
                    return PositionHistory(out, True)
            else:
                out.extend(batch)
            if cursor is None:
                return PositionHistory(out, True)
        return PositionHistory(out, True, truncated=True)

    async def redeemable_positions(self, wallet: str) -> PositionHistory:
        """Resolved positions not yet redeemed (status=REDEEMABLE).

        Closed positions only list what was sold or redeemed. Losing positions pay $0, so traders rarely redeem
        them: without this call a history is overwhelmingly winners (verified: a top wallet showed 97% wins among
        closed positions and 287 unredeemed losers here).
        """
        out: list[ClosedPosition] = []
        cursor: str | None = None
        while len(out) < MAX_OFFSET:
            rows, cursor = await self._page("/v2/positions", [
                ("user", wallet), ("status", "REDEEMABLE"), ("limit", OPEN_PAGE)], cursor)
            batch, skipped = parse_many([r for r in rows if r.get("redeemable", True)], parse_open_position)
            self.skipped += skipped
            out.extend(batch)
            if cursor is None:
                return PositionHistory(out, True)
        return PositionHistory(out, False)  # unknown row order: a capped fetch is not a trustworthy window

    async def trades(self, *, user: str | None = None, market: str | None = None, min_usdc: float | None = None,
                     since_ts: int | None = None) -> TradePage:
        """Trades newest first (maker and taker sides), optionally for one wallet or one market, stopping at
        `since_ts`. `complete` is False when the MAX_OFFSET row cap cut the history short."""
        base: list[tuple[str, str | int | float]] = [("limit", TRADES_PAGE), ("taker_only", "false")]
        if user:
            base.append(("user", user))
        if market:
            base.append(("condition", market))
        if min_usdc is not None:
            base += [("filter_type", "CASH"), ("filter_amount", int(min_usdc))]
        out: list[Trade] = []
        cursor: str | None = None
        while len(out) < MAX_OFFSET:
            rows, cursor = await self._page("/v2/trades", base, cursor)
            batch, skipped = parse_many(rows, parse_trade)
            self.skipped += skipped
            fresh = [t for t in batch if since_ts is None or t.ts >= since_ts]
            out.extend(fresh)
            if len(fresh) < len(batch) or cursor is None:
                return TradePage(out, True)
        return TradePage(out, False)

    async def markets_traded(self, wallet: str) -> int | None:
        """How many distinct markets the wallet has ever traded (v2 /user-stats `trades`; null for unknown users)."""
        data = await self._http.get_json(f"{DATA_API}/v2/user-stats", [("user", wallet)])
        stats = data.get("data") if isinstance(data, dict) else None
        return int(stats["trades"]) if isinstance(stats, dict) and stats.get("trades") is not None else None

    async def leaderboard(self, *, period: str, order_by: str, pages: int) -> list[LeaderboardEntry]:
        out: list[LeaderboardEntry] = []
        cursor: str | None = None
        for _ in range(pages):
            rows, cursor = await self._page("/v2/leaderboard", [
                ("time_period", period), ("order_by", order_by), ("limit", LEADERBOARD_PAGE)], cursor)
            batch, skipped = parse_many(rows, parse_leaderboard)
            self.skipped += skipped
            out.extend(batch)
            if cursor is None or not rows:
                break
        return out
