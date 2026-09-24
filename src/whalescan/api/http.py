"""Polite async HTTP: token-bucket rate limit, exponential backoff, and loud failure on bot-protection blocks."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import httpx

log = logging.getLogger(__name__)

Params = Sequence[tuple[str, str | int | float]]
Sleep = Callable[[float], Awaitable[None]]


class ApiError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.body = body


class BlockedError(ApiError):
    """The endpoint answered with a bot-protection challenge (e.g. Cloudflare 403). Retrying won't help."""


class TokenBucket:
    def __init__(self, rate_per_s: float, burst: int = 1, clock: Callable[[], float] = time.monotonic,
                 sleep: Sleep = asyncio.sleep) -> None:
        self._rate = rate_per_s
        self._capacity = float(max(1, burst))
        self._tokens = self._capacity
        self._clock = clock
        self._sleep = sleep
        self._updated = clock()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = self._clock()
                self._tokens = min(self._capacity, self._tokens + (now - self._updated) * self._rate)
                self._updated = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                await self._sleep((1.0 - self._tokens) / self._rate)


def _is_block(r: httpx.Response) -> bool:
    ctype = r.headers.get("content-type", "")
    return r.status_code == 403 and "application/json" not in ctype


def _retry_after(r: httpx.Response) -> float | None:
    value = r.headers.get("retry-after")
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


class HttpClient:
    def __init__(self, *, user_agent: str, rate_per_s: float, max_retries: int,
                 transport: httpx.AsyncBaseTransport | None = None, sleep: Sleep = asyncio.sleep,
                 backoff_s: float = 0.5, timeout_s: float = 30.0) -> None:
        self._client = httpx.AsyncClient(
            http2=transport is None,
            transport=transport,
            timeout=timeout_s,
            follow_redirects=True,
            headers={"User-Agent": user_agent, "Accept": "application/json"},
        )
        self._bucket = TokenBucket(rate_per_s, burst=max(1, int(rate_per_s)), sleep=sleep)
        self._max_retries = max_retries
        self._sleep = sleep
        self._backoff = backoff_s
        self.errors = 0

    async def get_json(self, url: str, params: Params = ()) -> Any:
        last = ""
        for attempt in range(self._max_retries + 1):
            await self._bucket.acquire()
            wait: float | None = None
            try:
                r = await self._client.get(url, params=list(params))
            except httpx.TransportError as e:
                last = f"transport error {e!r}"
            else:
                if r.status_code == 200:
                    try:
                        return r.json()
                    except ValueError as e:
                        raise ApiError(f"invalid JSON from {url}", status=200, body=r.text[:500]) from e
                if _is_block(r):
                    raise BlockedError(
                        f"BLOCKED by bot protection at {url} (HTTP {r.status_code}, server={r.headers.get('server')})",
                        status=r.status_code, body=r.text[:500])
                if r.status_code != 429 and r.status_code < 500:
                    raise ApiError(f"HTTP {r.status_code} from {url}: {r.text[:200]}",
                                   status=r.status_code, body=r.text[:500])
                last = f"HTTP {r.status_code}"
                wait = _retry_after(r)
            self.errors += 1
            if attempt < self._max_retries:
                delay = wait if wait is not None else self._backoff * 2**attempt
                log.info("retrying %s in %.1fs (%s)", url, delay, last)
                await self._sleep(delay)
        raise ApiError(f"giving up on {url} after {self._max_retries + 1} attempts: {last}")

    async def aclose(self) -> None:
        await self._client.aclose()
