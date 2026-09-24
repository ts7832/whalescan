"""Parsing of live WebSocket frames (RTDS trade firehose, CLOB market channel). Malformed frames are dropped."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from whalescan.models import BookSnapshot, Trade
from whalescan.parsers import ParseError, parse_book, parse_trade

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PriceChanges:
    """One price_change message as parallel arrays, ready for OrderBook.apply_deltas (side 0 = bid, 1 = ask).
    best_bids/best_asks are the server's top of book after each change (None when that side is empty)."""

    ts_ms: int
    assets: tuple[str, ...]
    sides: tuple[int, ...]
    prices: tuple[float, ...]
    sizes: tuple[float, ...]
    best_bids: tuple[float | None, ...]
    best_asks: tuple[float | None, ...]


def _loads(raw: str) -> Any | None:
    if not raw or not raw.strip():
        return None  # keep-alive
    try:
        return json.loads(raw)
    except ValueError:
        return None  # e.g. "PONG"


def parse_rtds(raw: str) -> tuple[Trade, int] | None:
    """(trade, server send time in ms) for an activity/trades frame; None for anything else."""
    msg = _loads(raw)
    if not isinstance(msg, dict) or msg.get("topic") != "activity" or msg.get("type") != "trades":
        return None
    try:
        return parse_trade(msg["payload"]), int(msg["timestamp"])
    except (ParseError, KeyError, TypeError, ValueError) as e:
        log.debug("dropping RTDS frame: %s", e)
        return None


def _top(value: Any) -> float | None:
    p = float(value) if value not in (None, "") else 0.0
    return p if p > 0 else None


def _price_changes(d: dict[str, Any]) -> PriceChanges:
    rows = d["price_changes"]
    return PriceChanges(
        ts_ms=int(d.get("timestamp") or 0),
        assets=tuple(str(r["asset_id"]) for r in rows),
        sides=tuple(0 if str(r["side"]).upper() == "BUY" else 1 for r in rows),
        prices=tuple(float(r["price"]) for r in rows),
        sizes=tuple(float(r["size"]) for r in rows),
        best_bids=tuple(_top(r.get("best_bid")) for r in rows),
        best_asks=tuple(_top(r.get("best_ask")) for r in rows),
    )


def parse_clob(raw: str) -> list[BookSnapshot | PriceChanges]:
    msg = _loads(raw)
    if msg is None:
        return []
    out: list[BookSnapshot | PriceChanges] = []
    for d in msg if isinstance(msg, list) else [msg]:
        if not isinstance(d, dict):
            continue
        kind = d.get("event_type")
        try:
            if kind == "book":
                out.append(parse_book(d))
            elif kind == "price_change":
                out.append(_price_changes(d))
        except (ParseError, KeyError, TypeError, ValueError) as e:
            log.debug("dropping CLOB message: %s", e)
    return out
