"""The only place raw API JSON is touched. Anything malformed becomes a ParseError carrying the payload."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Any, TypeVar

from whalescan.models import BookSnapshot, ClosedPosition, LeaderboardEntry, Market, PricePoint, Trade

log = logging.getLogger(__name__)
T = TypeVar("T")


class ParseError(ValueError):
    def __init__(self, kind: str, payload: Any, cause: BaseException) -> None:
        super().__init__(f"cannot parse {kind}: {cause!r}")
        self.payload = payload


def iso_to_ts(value: str | None) -> int | None:
    """'2026-09-21T00:00:00Z', '2026-09-21 04:30:03+00' or '2026-09-21' -> unix seconds (UTC)."""
    if not value:
        return None
    s = value.strip().replace("Z", "+00:00")
    if len(s) >= 3 and s[-3] in "+-" and s[-2:].isdigit():  # '+00' -> '+00:00'
        s += ":00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp())


def _json_list(value: Any) -> list[Any]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, list):
        raise TypeError(f"expected list, got {type(value).__name__}")
    return value


def _side(value: Any) -> str:
    side = str(value).upper()
    if side not in ("BUY", "SELL"):
        raise ValueError(f"unknown side {value!r}")
    return side


def _guard(kind: str, fn: Callable[[dict[str, Any]], T]) -> Callable[[dict[str, Any]], T]:
    def wrapped(d: dict[str, Any]) -> T:
        try:
            return fn(d)
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            raise ParseError(kind, d, e) from e

    return wrapped


def _trade(d: dict[str, Any]) -> Trade:
    return Trade(
        tx_hash=str(d["transactionHash"]),
        ts=int(d["timestamp"]),
        wallet=str(d["proxyWallet"]).lower(),
        asset=str(d["asset"]),
        condition_id=str(d["conditionId"]).lower(),
        side=_side(d["side"]),
        price=float(d["price"]),
        size=float(d["size"]),
        event_slug=str(d.get("eventSlug") or ""),
        title=str(d.get("title") or ""),
        outcome=str(d.get("outcome") or ""),
        outcome_index=int(d.get("outcomeIndex", -1)),
        fee=float(d["fee"]) if d.get("fee") is not None else None,
    )


def _closed_position(d: dict[str, Any]) -> ClosedPosition:
    return ClosedPosition(
        wallet=str(d["proxyWallet"]).lower(),
        asset=str(d["asset"]),
        condition_id=str(d["conditionId"]).lower(),
        avg_price=float(d["avgPrice"]),
        total_bought=float(d["totalBought"]),
        realized_pnl=float(d.get("realizedPnl") or 0.0),
        cur_price=float(d.get("curPrice") or 0.0),
        outcome=str(d.get("outcome") or ""),
        outcome_index=int(d["outcomeIndex"]),
        title=str(d.get("title") or ""),
        event_slug=str(d.get("eventSlug") or ""),
        ts=int(d["timestamp"]),
    )


def _open_position(d: dict[str, Any]) -> ClosedPosition:
    """A row from /positions. Resolved-but-unredeemed rows (mostly losers, which nobody redeems for $0)
    never reach /closed-positions, so they are folded into the same history. ts=0: no close time exists."""
    return ClosedPosition(
        wallet=str(d["proxyWallet"]).lower(),
        asset=str(d["asset"]),
        condition_id=str(d["conditionId"]).lower(),
        avg_price=float(d["avgPrice"]),
        total_bought=float(d.get("totalBought") or d.get("size") or 0.0),
        realized_pnl=float(d.get("realizedPnl") or 0.0),
        cur_price=float(d.get("curPrice") or 0.0),
        outcome=str(d.get("outcome") or ""),
        outcome_index=int(d["outcomeIndex"]),
        title=str(d.get("title") or ""),
        event_slug=str(d.get("eventSlug") or ""),
        ts=0,
    )


def _market(d: dict[str, Any]) -> Market:
    fees = d.get("feeSchedule") or {}
    events = d.get("events") or []
    return Market(
        condition_id=str(d["conditionId"]).lower(),
        question=str(d.get("question") or ""),
        slug=str(d.get("slug") or ""),
        event_slug=str(events[0].get("slug") or "") if events else "",
        end_ts=iso_to_ts(d.get("endDate")),
        closed=bool(d.get("closed", False)),
        closed_ts=iso_to_ts(d.get("closedTime")),
        outcome_prices=tuple(float(x) for x in _json_list(d.get("outcomePrices"))),
        token_ids=tuple(str(x) for x in _json_list(d.get("clobTokenIds"))),
        fees_enabled=bool(d.get("feesEnabled", False)),
        fee_rate=float(fees.get("rate") or 0.0),
        fee_exponent=float(fees.get("exponent") or 1.0),
        volume=float(d.get("volumeNum") or d.get("volume") or 0.0),
        tags=tuple(str(t["label"]) for t in (d.get("tags") or []) if t.get("label")),
    )


def _leaderboard(d: dict[str, Any]) -> LeaderboardEntry:
    return LeaderboardEntry(
        wallet=str(d["proxyWallet"]).lower(),
        rank=int(d["rank"]),
        volume=float(d.get("vol") or 0.0),
        pnl=float(d.get("pnl") or 0.0),
        name=str(d.get("userName") or ""),
    )


def _book(d: dict[str, Any]) -> BookSnapshot:
    return BookSnapshot(
        asset=str(d["asset_id"]),
        ts_ms=int(d["timestamp"]),
        bids=tuple((float(x["price"]), float(x["size"])) for x in d.get("bids") or []),
        asks=tuple((float(x["price"]), float(x["size"])) for x in d.get("asks") or []),
    )


def _price_history(d: dict[str, Any]) -> list[PricePoint]:
    return [PricePoint(ts=int(x["t"]), price=float(x["p"])) for x in d["history"]]


parse_trade = _guard("trade", _trade)
parse_closed_position = _guard("closed position", _closed_position)
parse_open_position = _guard("open position", _open_position)
parse_market = _guard("market", _market)
parse_leaderboard = _guard("leaderboard entry", _leaderboard)
parse_book = _guard("book", _book)
parse_price_history = _guard("price history", _price_history)


def parse_many(rows: Iterable[dict[str, Any]], fn: Callable[[dict[str, Any]], T]) -> tuple[list[T], int]:
    """Parse rows, skipping (and logging) malformed ones. Returns (items, n_skipped)."""
    items: list[T] = []
    skipped = 0
    for row in rows:
        try:
            items.append(fn(row))
        except ParseError as e:
            skipped += 1
            log.warning("%s — payload: %s", e, str(e.payload)[:300])
    return items, skipped
