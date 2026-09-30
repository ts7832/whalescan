"""Gamma API: market metadata (resolution, fees, tags)."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from collections.abc import Iterable

from whalescan.api.http import ApiError, BlockedError, HttpClient
from whalescan.models import Market
from whalescan.parsers import iso_to_ts, parse_many, parse_market

log = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com"
CHUNK = 100  # Gamma's maximum number of condition_ids per request (422 above it)
PROGRESS_MIN_CHUNKS = 20  # log progress only for batch-sized fetches, not the sweep's handful


class GammaApi:
    def __init__(self, http: HttpClient, *, concurrency: int = 8) -> None:
        self._http = http
        self._concurrency = concurrency  # in-flight requests; the per-host rate limit still applies
        self.skipped = 0

    async def created_ts(self, wallet: str) -> int | None:
        """Account creation time (unix s) from the public profile; None if Polymarket has no profile."""
        try:
            data = await self._http.get_json(f"{GAMMA_API}/public-profile", [("address", wallet)])
        except BlockedError:
            raise
        except ApiError as e:
            if e.status == 404:
                return None
            raise
        try:
            return iso_to_ts(data.get("createdAt")) if isinstance(data, dict) else None
        except (TypeError, ValueError, AttributeError):  # malformed createdAt: treat as unknown, never crash
            return None

    async def markets(self, condition_ids: Iterable[str]) -> dict[str, Market]:
        """Metadata for the given markets. Gamma hides closed markets unless asked, so query closed
        markets first (almost all historical positions) and only the remainder as open. Chunks within a
        pass run concurrently (sequential requests took hours for a large universe)."""
        out: dict[str, Market] = {}
        remaining = sorted({c.lower() for c in condition_ids})
        for closed in ("true", "false"):
            chunks = [remaining[i:i + CHUNK] for i in range(0, len(remaining), CHUNK)]
            sem = asyncio.Semaphore(self._concurrency)
            step = max(1, len(chunks) // 10)
            done = 0

            async def one(chunk: list[str]) -> None:
                nonlocal done
                async with sem:
                    rows = await self._chunk(chunk, closed)
                if rows is not None:
                    markets, skipped = parse_many(rows, parse_market)
                    self.skipped += skipped
                    out.update((m.condition_id, m) for m in markets)
                done += 1
                if len(chunks) >= PROGRESS_MIN_CHUNKS and (done % step == 0 or done == len(chunks)):
                    log.info("market metadata: %d/%d chunks", done, len(chunks))

            try:
                async with asyncio.TaskGroup() as tg:
                    for chunk in chunks:
                        tg.create_task(one(chunk))
            except ExceptionGroup as eg:  # re-raise as itself so BlockedError still reaches the CLI
                raise eg.exceptions[0] from None
            remaining = [c for c in remaining if c not in out]
        return out

    async def listed_markets(self, *, closed: bool, end_min_ts: int, end_max_ts: int,
                             min_volume: float) -> list[Market]:
        """Every market whose scheduled end falls in [end_min_ts, end_max_ts], closed or open, with at least
        `min_volume` traded. Uses /markets/keyset: plain /markets refuses offsets beyond ~2,000 (HTTP 422), and a
        single week can hold more markets than that. Stops if the API ever repeats a cursor."""
        def iso(ts: int) -> str:
            return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

        base = [("closed", "true" if closed else "false"), ("include_tag", "true"), ("limit", CHUNK),
                ("end_date_min", iso(end_min_ts)), ("end_date_max", iso(end_max_ts)),
                ("volume_num_min", int(min_volume))]
        out: list[Market] = []
        cursor: str | None = None
        seen: set[str] = set()
        while True:
            page = await self._http.get_json(f"{GAMMA_API}/markets/keyset",
                                             base + ([("after_cursor", cursor)] if cursor else []))
            rows = page.get("markets") if isinstance(page, dict) else None
            if not isinstance(rows, list):
                log.warning("gamma keyset returned %s instead of a page", type(page).__name__)
                self.skipped += 1
                return out
            markets, skipped = parse_many(rows, parse_market)
            self.skipped += skipped
            out.extend(markets)
            if rows and len(out) % (20 * CHUNK) < len(rows):
                log.info("market listing: %d markets so far", len(out))
            cursor = page.get("next_cursor")
            if not rows or not cursor or cursor in seen:
                return out
            seen.add(cursor)

    async def _chunk(self, chunk: list[str], closed: str) -> list | None:
        try:
            rows = await self._http.get_json(
                f"{GAMMA_API}/markets",
                [("condition_ids", c) for c in chunk] + [("closed", closed), ("include_tag", "true"),
                                                         ("limit", CHUNK)],
            )
        except BlockedError:
            raise
        except ApiError as e:  # one bad chunk must not abort a multi-hour batch
            log.warning("gamma chunk of %d ids failed: %s", len(chunk), e)
            self.skipped += 1
            return None
        if not isinstance(rows, list):
            log.warning("gamma returned %s instead of a list", type(rows).__name__)
            self.skipped += 1
            return None
        return rows
