"""Polymarket Data API: trades, closed positions, leaderboard. See docs/polymarket-api-notes.md."""

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
MAX_OFFSET = 10_000


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

    async def _page(self, path: str, params: Params) -> list[dict[str, Any]] | None:
        """One page of rows, or None when the API refuses to paginate deeper (offset cap).

        Any other error body raises: treating it as "end of history" would store a silently
        truncated or holed history that incremental refreshes never repair."""
        try:
            data = await self._http.get_json(f"{DATA_API}{path}", params)
        except ApiError as e:
            if e.status == 400 and "offset" in e.body.lower():
                return None
            raise
        if not isinstance(data, list):
            raise ApiError(f"error object from {path}: {str(data)[:200]}", status=200, body=str(data)[:500])
        return data

    async def closed_positions(self, wallet: str, *, since_ts: int | None = None) -> PositionHistory:
        """All closed positions, newest first; stops at rows older than `since_ts` for incremental refresh.

        Sorted by TIMESTAMP on purpose: the default order is realizedPnl DESC, and a truncated fetch
        in that order would contain only winners. In time order, hitting the depth limit leaves the
        most recent window, which is still an unbiased sample — so it is returned as complete + truncated.
        A refusal before any page arrives is incomplete.
        """
        out: list[ClosedPosition] = []
        offset = 0
        while offset <= MAX_OFFSET:
            rows = await self._page("/closed-positions", [
                ("user", wallet), ("limit", CLOSED_PAGE), ("offset", offset),
                ("sortBy", "TIMESTAMP"), ("sortDirection", "DESC"),
            ])
            if rows is None:
                return PositionHistory(out, offset > 0, truncated=offset > 0)
            batch, skipped = parse_many(rows, parse_closed_position)
            self.skipped += skipped
            if since_ts is not None:
                fresh = [p for p in batch if p.ts >= since_ts]
                out.extend(fresh)
                if len(fresh) < len(batch):
                    return PositionHistory(out, True)
            else:
                out.extend(batch)
            if len(rows) < CLOSED_PAGE:
                return PositionHistory(out, True)
            offset += CLOSED_PAGE
        return PositionHistory(out, True, truncated=True)

    async def redeemable_positions(self, wallet: str) -> PositionHistory:
        """Resolved positions still sitting in /positions (redeemable=true).

        /closed-positions only lists positions that were sold or redeemed. Losing positions pay $0,
        so traders rarely redeem them: without this call a history is overwhelmingly winners
        (verified: a top wallet showed 97% wins in /closed-positions and 287 unredeemed losers here).
        """
        out: list[ClosedPosition] = []
        offset = 0
        while offset <= MAX_OFFSET:
            rows = await self._page("/positions", [("user", wallet), ("sizeThreshold", 0), ("limit", OPEN_PAGE),
                                                   ("offset", offset)])
            if rows is None:  # unknown row order: a capped fetch is not a trustworthy window
                return PositionHistory(out, False)
            batch, skipped = parse_many([r for r in rows if r.get("redeemable")], parse_open_position)
            self.skipped += skipped
            out.extend(batch)
            if len(rows) < OPEN_PAGE:
                return PositionHistory(out, True)
            offset += OPEN_PAGE
        return PositionHistory(out, False)

    async def trades(self, *, user: str | None = None, min_usdc: float | None = None,
                     since_ts: int | None = None) -> TradePage:
        """Trades newest first (maker and taker sides), stopping at `since_ts`."""
        base: list[tuple[str, str | int | float]] = [("limit", TRADES_PAGE), ("takerOnly", "false")]
        if user:
            base.append(("user", user))
        if min_usdc is not None:
            base += [("filterType", "CASH"), ("filterAmount", int(min_usdc))]
        out: list[Trade] = []
        offset = 0
        while offset <= MAX_OFFSET:
            rows = await self._page("/trades", [*base, ("offset", offset)])
            if rows is None:
                return TradePage(out, False)
            batch, skipped = parse_many(rows, parse_trade)
            self.skipped += skipped
            fresh = [t for t in batch if since_ts is None or t.ts >= since_ts]
            out.extend(fresh)
            if len(fresh) < len(batch) or len(rows) < TRADES_PAGE:
                return TradePage(out, True)
            offset += TRADES_PAGE
        return TradePage(out, False)

    async def markets_traded(self, wallet: str) -> int | None:
        """How many distinct markets the wallet has ever traded."""
        data = await self._http.get_json(f"{DATA_API}/traded", [("user", wallet)])
        return int(data["traded"]) if isinstance(data, dict) and data.get("traded") is not None else None

    async def leaderboard(self, *, period: str, order_by: str, pages: int) -> list[LeaderboardEntry]:
        out: list[LeaderboardEntry] = []
        for page in range(pages):
            rows = await self._page("/v1/leaderboard", [
                ("timePeriod", period), ("orderBy", order_by),
                ("limit", LEADERBOARD_PAGE), ("offset", page * LEADERBOARD_PAGE),
            ])
            if not rows:
                break
            batch, skipped = parse_many(rows, parse_leaderboard)
            self.skipped += skipped
            out.extend(batch)
            if len(rows) < LEADERBOARD_PAGE:
                break
        return out
