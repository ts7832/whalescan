"""`whalescan live`: the real-time station (spec §4.8).

Two WebSocket feeds in (every trade; order books of watched tokens), one WebSocket out (the dashboard).
All decisions are made by LiveState; this module only moves bytes, keeps books honest, and persists trades.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pandas as pd
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from whalescan import __version__
from whalescan.api.clob import ClobApi
from whalescan.api.data_api import DataApi
from whalescan.api.gamma import GammaApi
from whalescan.api.http import ApiError, BlockedError, HttpClient
from whalescan.api.profiles import fetch_profile
from whalescan.config import ROOT, Config
from whalescan.live_state import LiveState
from whalescan.models import BookSnapshot, Trade
from whalescan.parsers import ParseError
from whalescan.scoring import SCORE_COLUMNS
from whalescan.snapshot import clean
from whalescan.store import Store, trades_from_frame
from whalescan.stream.messages import parse_clob, parse_rtds
from whalescan.stream.ws_base import ReconnectingWS

log = logging.getLogger(__name__)

RTDS_URL = "wss://ws-live-data.polymarket.com"
RTDS_SUBSCRIBE = json.dumps({"action": "subscribe", "subscriptions": [{"topic": "activity", "type": "trades"}]})
CLOB_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
PERSIST_MIN_USDC = 1_000.0
BOOK_PUSH_INTERVAL_S = 0.25  # dirty books are flushed to the browser at most 4 times per second
MARKET_RETRY_S = 600
RESYNC_MIN_S, RESYNC_MAX_S = 5, 300
LINK_DOWN_STATES = {"disconnected", "silent", "backoff", "stopped"}


class Hub:
    """Fan-out of dashboard messages. Each client has a bounded queue; a slow client loses messages
    instead of growing memory without limit (it gets a full state again when it reconnects)."""

    def __init__(self, max_queue: int = 1000, state_provider: Callable[[], Any] | None = None) -> None:
        self._clients: list[tuple[asyncio.Queue[Any], asyncio.AbstractEventLoop | None]] = []
        self._callbacks: list[Callable[[Any], None]] = []
        self._max = max_queue
        self._state = state_provider
        self.dropped = 0

    def add(self) -> asyncio.Queue[Any]:
        try:
            loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        q: asyncio.Queue[Any] = asyncio.Queue(maxsize=self._max)
        self._clients.append((q, loop))
        return q

    def remove(self, q: asyncio.Queue[Any]) -> None:
        self._clients = [(c, lp) for c, lp in self._clients if c is not q]

    def subscribe_callback(self, fn: Callable[[Any], None]) -> None:
        self._callbacks.append(fn)

    def _put(self, q: asyncio.Queue[Any], msg: Any) -> None:
        try:
            q.put_nowait(msg)
        except asyncio.QueueFull:
            # A client that fell behind would silently diverge (e.g. miss a signal's EXPIRED). Instead,
            # throw away its backlog and give it one fresh full state to rebuild from.
            self.dropped += 1
            while not q.empty():
                q.get_nowait()
            if self._state is not None:
                q.put_nowait({"type": "state", "data": self._state()})

    def broadcast(self, msg: Any) -> None:
        for fn in self._callbacks:
            fn(msg)
        try:
            current: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            current = None
        for q, loop in list(self._clients):
            if loop is None or loop is current:
                self._put(q, msg)
            else:  # the server's loop runs in another thread (e.g. tests): hand over thread-safely
                loop.call_soon_threadsafe(self._put, q, msg)


def create_app(state_provider: Callable[[], dict[str, Any]], hub: Hub, static_dir: Path | None) -> FastAPI:
    # FastAPI resolves the handlers' type hints by name, so WebSocket must be a module-level import.
    app = FastAPI(title="WHALESCAN live", docs_url=None, redoc_url=None)

    @app.get("/api/state")
    def api_state() -> JSONResponse:
        return JSONResponse(clean(state_provider()))

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        # Browsers let any page open ws://127.0.0.1; only the dashboard's own origin may read the feed.
        origin = websocket.headers.get("origin")
        if origin and urlsplit(origin).netloc != websocket.headers.get("host"):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        q = hub.add()
        try:
            await websocket.send_json(clean({"type": "state", "data": state_provider()}))
            while True:
                await websocket.send_json(clean(await q.get()))
        except WebSocketDisconnect:
            pass
        finally:
            hub.remove(q)

    if static_dir is not None and static_dir.exists():
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="dashboard")
    return app


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


class Station:
    def __init__(self, cfg: Config, *, scores: pd.DataFrame | None = None, gamma: Any = None, clob: Any = None,
                 data: Any = None, clock: Callable[[], float] = time.time) -> None:
        self.cfg = cfg
        self.clock = clock
        self._http: HttpClient | None = None
        if gamma is None or clob is None or data is None:
            self._http = HttpClient(user_agent=cfg.http.user_agent, rate_per_s=cfg.http.rate_per_s,
                                    max_retries=cfg.http.max_retries, host_rates=cfg.http.host_rates)
        self.gamma = gamma or GammaApi(self._http)
        self.clob = clob or ClobApi(self._http)
        self.data = data or DataApi(self._http)
        self._scores_path = cfg.path(cfg.paths.scores_parquet)
        self._scores_mtime = self._scores_path.stat().st_mtime if self._scores_path.exists() else None
        if scores is None:
            scores = self._load_scores()
        snap_dir = cfg.path(cfg.paths.snapshot_dir)
        self.whales = _read_json(snap_dir / "whales.json") or []
        self.validation = _read_json(snap_dir / "validation.json")
        self.snapshot_meta = _read_json(snap_dir / "meta.json") or {}
        names = {w["wallet"]: w.get("name", "") for w in self.whales}
        self.state = LiveState(cfg, scores=scores, names=names)
        self.hub = Hub(state_provider=self.snapshot_state)
        self.store = Store(cfg.path(cfg.paths.research_db).with_name("live.duckdb"))
        self.watch: list[str] = []
        self._dirty_books: set[str] = set()
        self._resync_after: dict[str, tuple[float, float]] = {}  # asset -> (next attempt, current delay)
        self.clob_socket: Any = None
        self.rtds_socket: Any = None
        self.links: dict[str, str] = {"rtds": "starting", "clob": "starting"}
        self.feed_latency_ms: int | None = None
        self._pending: list[Trade] = []
        self._market_asked: dict[str, float] = {}
        self._profile_asked: dict[str, float] = {}
        self._stopping = False

    # ------------------------------------------------------------------ inputs

    def _load_scores(self) -> pd.DataFrame:
        if not self._scores_path.exists():
            log.error("no %s — run `whalescan score` or `whalescan batch` first; no wallet is certified",
                      self._scores_path)
            return pd.DataFrame(columns=SCORE_COLUMNS)
        return pd.read_parquet(self._scores_path)

    def _now(self) -> int:
        return int(self.clock())

    def _emit(self, msgs: list[dict[str, Any]]) -> None:
        for m in msgs:
            self.hub.broadcast(m)

    async def handle_rtds(self, raw: str) -> None:
        parsed = parse_rtds(raw)
        if parsed is None:
            return
        trade, sent_ms = parsed
        self.feed_latency_ms = max(0, int(self.clock() * 1000) - sent_ms)
        if trade.usdc >= PERSIST_MIN_USDC or trade.wallet in self.state.certified_wallets:
            self._pending.append(trade)
        self._emit(self.state.on_trade(trade, self._now()))

    async def handle_clob(self, raw: str) -> None:
        self.state.books.heartbeat(int(self.clock() * 1000))  # even a PONG proves the link is alive
        touched: set[str] = set()
        for item in parse_clob(raw):
            if isinstance(item, BookSnapshot):
                self.state.books.on_snapshot(item)
                touched.add(item.asset)
            else:
                self.state.books.on_changes(item)
                touched |= {a for a in item.assets if self.state.books.book(a) is not None}
        now = self._now()
        for asset in touched:
            self._emit(self.state.on_book(asset, now))
            self._dirty_books.add(asset)

    def flush_books(self) -> None:
        """Push the latest quote of every changed book that backs an open signal (coalesced, so the final
        state of a burst is never lost). Books without a signal aren't sent: nothing on screen uses them."""
        dirty, self._dirty_books = self._dirty_books, set()
        for asset in sorted(dirty & self.state.signal_assets()):
            q = self.state.books.quote(asset, self.cfg.gate.follow_size_usdc)
            if q is not None:
                self.hub.broadcast({"type": "book", "data": {"asset": asset, "quote": {
                    "vwap": q.vwap, "complete": q.complete, "book_as_of": q.book_as_of, "best_bid": q.best_bid,
                    "best_ask": q.best_ask, "microprice": q.microprice, "levels": [list(x) for x in q.levels]}}})

    # ------------------------------------------------------------------ maintenance

    async def _guard(self, coro: Any, what: str) -> Any:
        try:
            return await coro
        except BlockedError:
            raise
        except (ApiError, ParseError) as e:
            log.warning("%s failed: %s", what, e)
            return None

    async def _snapshot_book(self, asset: str) -> bool:
        snap = await self._guard(self.clob.book(asset), f"book {asset}")
        if snap is None:
            return False
        self.state.books.on_snapshot(snap)
        self._emit(self.state.on_book(asset, self._now()))
        self._dirty_books.add(asset)
        return asset not in self.state.books.desynced

    def apply_subscription(self) -> None:
        """Called whenever the CLOB socket (re)subscribes: only the subscribed books may be trusted."""
        self.state.books.subscribe(self.watch)

    async def tick(self) -> None:
        now = self._now()
        self._emit(self.state.expire(now))

        wanted = {c for c in self.state.missing_markets()
                  if now - self._market_asked.get(c, -1e18) >= MARKET_RETRY_S}
        if wanted:
            self._market_asked.update(dict.fromkeys(wanted, now))
            found = await self._guard(self.gamma.markets(wanted), "markets")
            if found:
                self._emit(self.state.set_markets(found, now))

        wanted_profiles = {w for w in self.state.missing_profiles()
                           if now - self._profile_asked.get(w, -1e18) >= MARKET_RETRY_S}
        if wanted_profiles:
            self._profile_asked.update(dict.fromkeys(wanted_profiles, now))
            sem = asyncio.Semaphore(self.cfg.http.concurrency)

            async def one(w: str) -> Any:
                async with sem:
                    return await self._guard(fetch_profile(self.gamma, self.data, w, now=now), f"profile {w}")

            got = [p for p in await asyncio.gather(*(one(w) for w in sorted(wanted_profiles))) if p is not None]
            if got:
                self._emit(self.state.set_profiles({p.wallet: p for p in got}, now))

        watch = self.state.watch_set(now)
        added = [a for a in watch if a not in self.watch]
        removed = sorted(set(self.watch) - set(watch))
        if added or removed:
            # Incremental ops on the open socket (verified live): the server sends a book snapshot for each
            # added token and stops updates for removed ones; untouched books keep streaming.
            self.watch = watch
            self.state.books.subscribe(watch)
            if self.clob_socket is not None:
                if added:
                    await self.clob_socket.send(json.dumps({"assets_ids": added, "operation": "subscribe"}))
                if removed:
                    await self.clob_socket.send(json.dumps({"assets_ids": removed, "operation": "unsubscribe"}))

        if self.links.get("clob") not in LINK_DOWN_STATES:  # REST can't help while deltas are being missed
            for asset in sorted(self.state.books.desynced):
                due, delay = self._resync_after.get(asset, (0.0, RESYNC_MIN_S / 2))
                if now < due:
                    continue
                if await self._snapshot_book(asset):
                    self._resync_after.pop(asset, None)
                else:  # e.g. 404 for a market that just resolved: back off instead of hammering
                    delay = min(RESYNC_MAX_S, delay * 2)
                    self._resync_after[asset] = (now + delay, delay)

        if self._pending:
            pending, self._pending = self._pending, []
            # DuckDB work runs in a thread so the event loop keeps reading sockets meanwhile
            await asyncio.to_thread(self.store.upsert_trades, pending)

        if self._scores_path.exists() and self._scores_path.stat().st_mtime != self._scores_mtime:
            self._scores_mtime = self._scores_path.stat().st_mtime
            log.info("scores changed on disk: reloading")
            self._emit(self.state.set_scores(await asyncio.to_thread(self._load_scores), now))

        self.hub.broadcast({"type": "status", "data": self.meta()})

    # ------------------------------------------------------------------ outputs

    def meta(self) -> dict[str, Any]:
        base = self.snapshot_meta
        st = self.state.state()
        return {
            "generated_at": self._now(),
            "version": __version__,
            "mode": "LIVE",
            "counts": {**base.get("counts", {}), "signals": len(st["signals"]), "contacts": len(st["contacts"]),
                       "insiders": self.state.insider_count(),
                       "certified_wallets": len(self.state.scores.certified_wallets())},
            "params": base.get("params", {"bh_q": self.cfg.scoring.bh_q, "min_usdc": self.cfg.gate.min_usdc,
                                          "conviction_k": self.cfg.gate.conviction_k,
                                          "follow_size_usdc": self.cfg.gate.follow_size_usdc,
                                          "min_net_edge": self.cfg.gate.min_net_edge,
                                          "signal_lookback_h": self.cfg.gate.signal_lookback_h}),
            "errors": {"api": self._http.errors if self._http else 0, "ws_dropped": self.hub.dropped},
            "validation_generated_at": base.get("validation_generated_at"),
            "link": dict(self.links),
            "feed_latency_ms": self.feed_latency_ms,
            "watching": len(self.watch),
        }

    def snapshot_state(self) -> dict[str, Any]:
        return {"meta": self.meta(), **self.state.state(), "whales": self.whales, "validation": self.validation}

    def store_trades(self) -> list[Trade]:
        return trades_from_frame(self.store.trades_frame())

    def close(self) -> None:
        self.store.close()

    # ------------------------------------------------------------------ run

    async def warm_up(self) -> None:
        """Rebuild the last 24 h from disk and REST so a restart doesn't start blind."""
        now = self._now()
        since = now - int(self.cfg.gate.signal_lookback_h * 3600)
        trades = trades_from_frame(self.store.trades_frame(since_ts=since))
        page = await self._guard(self.data.trades(min_usdc=self.cfg.gate.min_usdc, since_ts=since), "backfill")
        if page is not None:
            trades += page.trades
        sem = asyncio.Semaphore(self.cfg.http.concurrency)

        async def wallet_backfill(wallet: str) -> list[Trade]:
            async with sem:
                page = await self._guard(self.data.trades(user=wallet, since_ts=since), f"backfill {wallet}")
            return page.trades if page is not None else []

        for batch in await asyncio.gather(*(wallet_backfill(w) for w in sorted(self.state.certified_wallets))):
            trades += batch
        for t in sorted(trades, key=lambda t: t.ts):
            self.state.on_trade(t, now)
        log.info("warm-up: replayed %d trades, %d position events", len(trades), len(self.state.events))
        await self.tick()

    def _on_status(self, name: str) -> Callable[[str, dict[str, Any]], None]:
        def update(state: str, info: dict[str, Any]) -> None:
            self.links[name] = state
            if name == "clob" and state in LINK_DOWN_STATES:
                self.state.books.link_down()
        return update

    def _clob_subscription(self) -> list[str]:
        self.apply_subscription()
        # Always open the channel, even with no tokens yet: later incremental subscribes need it.
        return [json.dumps({"assets_ids": self.watch, "type": "market"})]

    async def _book_pusher(self) -> None:
        while not self._stopping:
            self.flush_books()
            await asyncio.sleep(BOOK_PUSH_INTERVAL_S)

    async def _maintenance(self) -> None:
        while not self._stopping:
            try:
                await self.tick()
            except BlockedError:
                raise
            except Exception:  # noqa: BLE001 — the station must keep running; log with traceback
                log.exception("maintenance tick failed")
            await asyncio.sleep(5)

    async def run(self, host: str = "127.0.0.1", port: int = 8765) -> None:
        import uvicorn

        await self.warm_up()
        self.rtds_socket = ReconnectingWS("rtds", RTDS_URL, subscribe=lambda: [RTDS_SUBSCRIBE],
                                          on_message=self.handle_rtds, on_status=self._on_status("rtds"),
                                          dead_after_s=30)
        self.clob_socket = ReconnectingWS("clob", CLOB_URL, subscribe=self._clob_subscription,
                                          on_message=self.handle_clob, on_status=self._on_status("clob"),
                                          app_ping="PING", dead_after_s=120)
        app = create_app(self.snapshot_state, self.hub, ROOT / "web" / "dist")
        server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning"))

        async def serve() -> None:
            await server.serve()  # returns on Ctrl-C
            self._stopping = True
            self.rtds_socket.stop()
            self.clob_socket.stop()

        log.info("WHALESCAN live on http://%s:%d", host, port)
        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(serve())
                tg.create_task(self.rtds_socket.run())
                tg.create_task(self.clob_socket.run())
                tg.create_task(self._maintenance())
                tg.create_task(self._book_pusher())
        finally:
            if self._pending:
                self.store.upsert_trades(self._pending)
            self.close()
            if self._http is not None:
                await self._http.aclose()
