"""Gamma API: market metadata (resolution, fees, tags)."""

from __future__ import annotations

from collections.abc import Iterable

from whalescan.api.http import HttpClient
from whalescan.models import Market
from whalescan.parsers import parse_many, parse_market

GAMMA_API = "https://gamma-api.polymarket.com"
CHUNK = 40  # condition ids per request; keeps URLs ~3 KB and under the 100-row cap


class GammaApi:
    def __init__(self, http: HttpClient) -> None:
        self._http = http
        self.skipped = 0

    async def markets(self, condition_ids: Iterable[str]) -> dict[str, Market]:
        ids = sorted({c.lower() for c in condition_ids})
        out: dict[str, Market] = {}
        for i in range(0, len(ids), CHUNK):
            chunk = ids[i:i + CHUNK]
            for closed in ("true", "false"):  # Gamma hides closed markets unless asked explicitly
                rows = await self._http.get_json(
                    f"{GAMMA_API}/markets",
                    [("condition_ids", c) for c in chunk] + [("closed", closed), ("include_tag", "true"), ("limit", 100)],
                )
                markets, skipped = parse_many(rows, parse_market)
                self.skipped += skipped
                out.update((m.condition_id, m) for m in markets)
        return out
