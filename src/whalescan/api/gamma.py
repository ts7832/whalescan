"""Gamma API: market metadata (resolution, fees, tags)."""

from __future__ import annotations

from collections.abc import Iterable

from whalescan.api.http import HttpClient
from whalescan.models import Market
from whalescan.parsers import parse_many, parse_market

GAMMA_API = "https://gamma-api.polymarket.com"
CHUNK = 100  # Gamma's maximum number of condition_ids per request (422 above it)


class GammaApi:
    def __init__(self, http: HttpClient) -> None:
        self._http = http
        self.skipped = 0

    async def markets(self, condition_ids: Iterable[str]) -> dict[str, Market]:
        """Metadata for the given markets. Gamma hides closed markets unless asked, so query closed
        markets first (almost all historical positions) and only the remainder as open."""
        out: dict[str, Market] = {}
        remaining = sorted({c.lower() for c in condition_ids})
        for closed in ("true", "false"):
            for i in range(0, len(remaining), CHUNK):
                chunk = remaining[i:i + CHUNK]
                rows = await self._http.get_json(
                    f"{GAMMA_API}/markets",
                    [("condition_ids", c) for c in chunk] + [("closed", closed), ("include_tag", "true"),
                                                             ("limit", CHUNK)],
                )
                markets, skipped = parse_many(rows, parse_market)
                self.skipped += skipped
                out.update((m.condition_id, m) for m in markets)
            remaining = [c for c in remaining if c not in out]
        return out
