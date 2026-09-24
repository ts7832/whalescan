"""A WebSocket client that survives the real world (spec §0.1, Part 5).

Connections die in three ways, each handled here:
- a clean or abrupt close           -> reconnect with exponential backoff + jitter;
- a half-dead link (no pongs)       -> the websockets library's protocol pings detect it and close;
- a live link that stopped talking  -> no message for `dead_after_s` counts as dead (app-level silence check).
Every (re)connection re-sends the subscription, because a new socket knows nothing about the old one.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any

from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

log = logging.getLogger(__name__)

OnMessage = Callable[[str], Awaitable[None] | None]
OnStatus = Callable[[str, dict[str, Any]], None]


class ReconnectingWS:
    def __init__(self, name: str, url: str, *, subscribe: Callable[[], list[str]], on_message: OnMessage,
                 on_status: OnStatus | None = None, dead_after_s: float = 30.0, ping_interval_s: float = 10.0,
                 app_ping: str | None = None, backoff_min_s: float = 1.0, backoff_max_s: float = 60.0,
                 rng: Callable[[], float] = random.random,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        if backoff_min_s <= 0 or backoff_max_s < backoff_min_s:
            raise ValueError("need 0 < backoff_min_s <= backoff_max_s")
        self.name, self.url = name, url
        self._subscribe, self._on_message, self._on_status = subscribe, on_message, on_status
        self._dead_after, self._ping_interval, self._app_ping = dead_after_s, ping_interval_s, app_ping
        self._backoff_min, self._backoff_max, self._rng, self._sleep = backoff_min_s, backoff_max_s, rng, sleep
        self._stopped = False
        self._ws: Any = None
        self._skip_backoff = False
        self._wake = asyncio.Event()  # set by stop()/reconnect_now() to cut a backoff sleep short
        self._session_messages = 0
        self.messages = 0
        self.connects = 0
        self.last_message_at: float | None = None

    def _status(self, state: str, **info: Any) -> None:
        log.info("%s: %s %s", self.name, state, info or "")
        if self._on_status:
            self._on_status(state, {"name": self.name, **info})

    def stop(self) -> None:
        self._stopped = True
        self._wake.set()
        if self._ws is not None:
            asyncio.ensure_future(self._ws.close())

    async def send(self, msg: str) -> bool:
        """Send on the open connection. False when there is none; the next (re)connect's
        subscription then carries the full state, so nothing is lost."""
        ws = self._ws
        if ws is None:
            return False
        try:
            await ws.send(msg)
            return True
        except (OSError, WebSocketException):
            return False

    def reconnect_now(self) -> None:
        """Drop the current connection and reconnect immediately (e.g. the subscription set changed)."""
        self._skip_backoff = True
        self._wake.set()
        if self._ws is not None:
            asyncio.ensure_future(self._ws.close())

    def _delay(self, attempt: int) -> float:
        base = min(self._backoff_max, self._backoff_min * 2**attempt)
        return min(self._backoff_max, base * (0.5 + self._rng()))  # jitter: spread reconnect storms

    async def _interruptible_sleep(self, delay: float) -> None:
        if self._sleep is not asyncio.sleep:  # injected clock (tests): keep it deterministic
            await self._sleep(delay)
            return
        try:
            await asyncio.wait_for(self._wake.wait(), delay)
        except TimeoutError:
            pass

    async def _pinger(self, ws: Any) -> None:
        while True:
            await asyncio.sleep(self._ping_interval)
            await ws.send(self._app_ping)

    async def _session(self, ws: Any) -> None:
        for msg in self._subscribe():
            await ws.send(msg)
        self.connects += 1
        self._status("connected", connects=self.connects)
        pinger = asyncio.create_task(self._pinger(ws)) if self._app_ping else None
        try:
            while not self._stopped:
                try:
                    raw = await asyncio.wait_for(ws.recv(), self._dead_after)
                except TimeoutError:
                    self._status("silent", seconds=self._dead_after)
                    return
                self.messages += 1
                self._session_messages += 1
                self.last_message_at = time.time()
                try:
                    result = self._on_message(raw if isinstance(raw, str) else raw.decode())
                    if inspect.isawaitable(result):
                        await result
                except Exception:  # noqa: BLE001 — a handler bug must not take the feed down; log it loudly
                    log.exception("%s: message handler failed", self.name)
        finally:
            if pinger:
                pinger.cancel()

    async def run(self) -> None:
        attempt = 0
        while not self._stopped:
            self._wake.clear()  # a stop()/reconnect_now() from here on cuts the next backoff short
            try:
                async with connect(self.url, ping_interval=self._ping_interval, ping_timeout=self._ping_interval * 2,
                                   max_size=None, open_timeout=20) as ws:
                    self._ws = ws
                    self._session_messages = 0
                    await self._session(ws)
            except (OSError, WebSocketException, TimeoutError) as e:
                if not self._stopped:
                    self._status("disconnected", error=repr(e)[:200])
            finally:
                self._ws = None
            if self._session_messages > 0:
                attempt = 0  # only a connection that actually delivered data proves the server is healthy
            if self._stopped:
                break
            if self._skip_backoff:
                self._skip_backoff = False
                continue
            delay = self._delay(attempt)
            attempt += 1
            self._status("backoff", seconds=round(delay, 2))
            await self._interruptible_sleep(delay)
            self._skip_backoff = False
        self._status("stopped")
