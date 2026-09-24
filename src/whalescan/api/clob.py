"""CLOB REST: order book snapshots and price history."""

from __future__ import annotations

from whalescan.api.http import HttpClient
from whalescan.models import BookSnapshot, PricePoint
from whalescan.parsers import parse_book, parse_price_history

CLOB_API = "https://clob.polymarket.com"


class ClobApi:
    def __init__(self, http: HttpClient) -> None:
        self._http = http
        self.skipped = 0

    async def book(self, token_id: str) -> BookSnapshot:
        return parse_book(await self._http.get_json(f"{CLOB_API}/book", [("token_id", token_id)]))

    async def price_history(self, token_id: str, *, interval: str = "1w", fidelity: int = 60) -> list[PricePoint]:
        data = await self._http.get_json(
            f"{CLOB_API}/prices-history", [("market", token_id), ("interval", interval), ("fidelity", fidelity)])
        return parse_price_history(data)
