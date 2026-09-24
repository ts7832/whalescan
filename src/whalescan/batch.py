"""Snapshot pipeline (spec §3): universe → positions → markets → scores → signals → validation → JSON."""

from __future__ import annotations

import asyncio
import logging
import subprocess
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from whalescan import __version__
from whalescan.api.clob import ClobApi
from whalescan.api.data_api import DataApi
from whalescan.api.gamma import GammaApi
from whalescan.api.http import ApiError, BlockedError, HttpClient
from whalescan.book import follow_quote
from whalescan.classify import Blocklist, wallet_flags
from whalescan.config import ROOT, Config
from whalescan.gate import GateContext, ScoreBook, aggregate, evaluate, needs_book
from whalescan.scoring import prepare_positions, score_wallets
from whalescan.snapshot import evaluation_json, whales_json, write_json_atomic, write_parquet_atomic
from whalescan.store import Store, trades_from_frame
from whalescan.validate import run_validation

log = logging.getLogger(__name__)
MARKET_MAX_AGE_S = 3600
FLAG_COLUMNS = ["wallet", "condition_id", "outcome_index", "avg_price", "total_bought"]


@dataclass
class Apis:
    data: Any  # DataApi-compatible
    gamma: Any  # GammaApi-compatible
    clob: Any  # ClobApi-compatible

    def skipped(self) -> int:
        return self.data.skipped + self.gamma.skipped + self.clob.skipped


@dataclass
class BatchReport:
    wallets_scanned: int = 0
    wallets_complete: int = 0
    tests: int = 0
    testable: int = 0
    certified_wallets: int = 0
    signals: int = 0
    contacts: int = 0
    api_errors: int = 0
    validation_ran: bool = False
    top: pd.DataFrame | None = None


async def _guarded(coro: Any, what: str) -> Any:
    """Await an API call; log and swallow ordinary API errors, but let blocks propagate."""
    try:
        return await coro
    except BlockedError:
        raise
    except ApiError as e:
        log.warning("%s failed: %s", what, e)
        return None


async def discover_universe(apis: Apis, store: Store, cfg: Config, now: int) -> dict[str, str]:
    u = cfg.universe
    sources: dict[str, str] = {}
    names: dict[str, str] = {}
    for period in u.leaderboard_periods:
        for order in u.leaderboard_orders:
            for entry in await apis.data.leaderboard(period=period, order_by=order, pages=u.leaderboard_pages):
                sources.setdefault(entry.wallet, "leaderboard")
                names.setdefault(entry.wallet, entry.name)
    page = await apis.data.trades(min_usdc=u.large_trade_min_usdc, since_ts=now - u.large_trade_lookback_days * 86400)
    store.upsert_trades(page.trades)
    volume: dict[str, float] = {}
    for t in page.trades:
        volume[t.wallet] = volume.get(t.wallet, 0.0) + t.usdc
    for wallet, _ in sorted(volume.items(), key=lambda kv: -kv[1]):
        sources.setdefault(wallet, "large_trades")
    store.set_wallet_names(names)
    return dict(list(sources.items())[:u.max_wallets])


async def refresh_positions(apis: Apis, store: Store, wallets: Mapping[str, str], concurrency: int, now: int) -> int:
    state = store.wallet_state()
    sem = asyncio.Semaphore(concurrency)
    failures = 0

    async def one(wallet: str, source: str) -> None:
        nonlocal failures
        prev = state.get(wallet)
        since = prev.max_ts if prev and prev.complete and prev.max_ts is not None else None
        async with sem:
            hist = await _guarded(apis.data.closed_positions(wallet, since_ts=since), f"positions {wallet}")
        if hist is None:
            failures += 1
            return
        store.upsert_positions(hist.positions)
        store.record_wallet_fetch(wallet, fetched_at=now, complete=hist.complete, source=source)

    await asyncio.gather(*(one(w, s) for w, s in wallets.items()))
    return failures


async def refresh_markets(apis: Apis, store: Store, now: int) -> None:
    ids = store.condition_ids_needing_refresh(now, MARKET_MAX_AGE_S)
    if ids:
        log.info("fetching metadata for %d markets", len(ids))
        store.upsert_markets((await apis.gamma.markets(ids)).values(), now)


async def build_signals(apis: Apis, store: Store, cfg: Config, scores: pd.DataFrame, blocklist: Blocklist,
                        now: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    g = cfg.gate
    since = now - int(g.signal_lookback_h * 3600)
    book = ScoreBook(scores, g.fallback_max_cat_positions)
    sem = asyncio.Semaphore(cfg.http.concurrency)

    async def wallet_trades(wallet: str) -> None:
        async with sem:
            page = await _guarded(apis.data.trades(user=wallet, since_ts=since), f"trades {wallet}")
        if page is not None:
            store.upsert_trades(page.trades)

    await asyncio.gather(*(wallet_trades(w) for w in sorted(book.certified_wallets())))
    page = await _guarded(apis.data.trades(min_usdc=g.min_usdc, since_ts=since), "large trades")
    if page is not None:
        store.upsert_trades(page.trades)
    await refresh_markets(apis, store, now)

    events = aggregate(trades_from_frame(store.trades_frame(since_ts=since)), g.aggregation_window_s)
    markets = store.markets_by_id({e.condition_id for e in events})
    ctx = GateContext(cfg=g, categories=cfg.categories, blocklist=blocklist, scores=book, markets=markets,
                      events=events, now=now)
    evaluations = [evaluate(e, ctx, None) for e in events]
    for i, e in enumerate(evaluations):
        if needs_book(e):
            snap = await _guarded(apis.clob.book(e.event.asset), f"book {e.event.asset}")
            if snap is not None:
                evaluations[i] = evaluate(e.event, ctx, follow_quote(snap, g.follow_size_usdc))

    names = {w: s.name for w, s in store.wallet_state().items()}
    signals = []
    for e in sorted((e for e in evaluations if e.status == "SIGNAL"),
                    key=lambda e: (e.tier or "Z", -(e.net_edge or 0.0))):
        history = await _guarded(apis.clob.price_history(e.event.asset), f"history {e.event.asset}")
        signals.append(evaluation_json(e, markets.get(e.event.condition_id), names, history))
    contacts = [evaluation_json(e, markets.get(e.event.condition_id), names, None)
                for e in sorted(evaluations, key=lambda e: -e.event.last_ts)
                if e.status != "SIGNAL" and e.event.usdc >= g.min_usdc][:g.max_contacts]
    return signals, contacts


async def maybe_validate(apis: Apis, store: Store, cfg: Config, eligible: pd.DataFrame, flags: pd.Series,
                         scores: pd.DataFrame, blocklist: Blocklist, now: int, *, force: bool) -> dict[str, Any] | None:
    last = store.get_meta("validation_at")
    if not force and last and now - int(last) < cfg.validation.every_hours * 3600:
        return None
    testable = scores[(scores["category"] == "ALL") & (scores["n_eff"] >= cfg.scoring.min_n_eff)
                      & (scores["flags"] == "")].sort_values("n_eff", ascending=False)
    wallets = list(testable["wallet"].head(cfg.validation.max_wallets))
    sem = asyncio.Semaphore(cfg.http.concurrency)

    async def history(wallet: str) -> None:
        async with sem:
            page = await _guarded(apis.data.trades(user=wallet), f"history {wallet}")
        if page is not None:
            store.upsert_trades(page.trades)

    await asyncio.gather(*(history(w) for w in wallets))
    await refresh_markets(apis, store, now)
    trades = trades_from_frame(store.trades_frame(wallets=wallets))
    markets = store.markets_by_id({t.condition_id for t in trades})
    report = run_validation(eligible, flags, trades, markets, cfg, blocklist, now=now)
    store.set_meta("validation_at", str(now))
    return report


def _meta_json(cfg: Config, report: BatchReport, now: int, validation_at: str | None) -> dict[str, Any]:
    g = cfg.gate
    return {
        "generated_at": now,
        "version": __version__,
        "mode": "SNAPSHOT",
        "counts": {
            "wallets_scanned": report.wallets_scanned, "wallets_complete": report.wallets_complete,
            "tests": report.tests, "testable": report.testable, "certified_wallets": report.certified_wallets,
            "signals": report.signals, "contacts": report.contacts,
        },
        "params": {
            "bh_q": cfg.scoring.bh_q, "min_usdc": g.min_usdc, "conviction_k": g.conviction_k,
            "follow_size_usdc": g.follow_size_usdc, "min_net_edge": g.min_net_edge,
            "signal_lookback_h": g.signal_lookback_h,
        },
        "errors": {"api": report.api_errors},
        "validation_generated_at": int(validation_at) if validation_at else None,
    }


def git_publish(snapshot_dir: Path, now: int) -> bool:
    """Commit and push the snapshot directory. Returns False when nothing changed."""
    subprocess.run(["git", "add", str(snapshot_dir)], check=True, cwd=ROOT)
    if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ROOT).returncode == 0:
        return False
    stamp = datetime.fromtimestamp(now, UTC).strftime("%Y-%m-%dT%H:%MZ")
    subprocess.run(["git", "commit", "-m", f"snapshot: {stamp}"], check=True, cwd=ROOT)
    subprocess.run(["git", "push"], check=True, cwd=ROOT)
    return True


async def run_batch(cfg: Config, *, apis: Apis | None = None, now: int | None = None, skip_validation: bool = False,
                    force_validation: bool = False, stop_after_scoring: bool = False,
                    publish: bool = False) -> BatchReport:
    now = now or int(time.time())
    http: HttpClient | None = None
    if apis is None:
        http = HttpClient(user_agent=cfg.http.user_agent, rate_per_s=cfg.http.rate_per_s,
                          max_retries=cfg.http.max_retries)
        apis = Apis(DataApi(http), GammaApi(http), ClobApi(http))
    report = BatchReport()
    blocklist = Blocklist(cfg.blocklist)
    try:
        with Store(cfg.path(cfg.paths.research_db)) as store:
            wallets = await discover_universe(apis, store, cfg, now)
            report.wallets_scanned = len(wallets)
            failures = await refresh_positions(apis, store, wallets, cfg.http.concurrency, now)
            await refresh_markets(apis, store, now)

            frame = store.positions_frame()
            complete = frame[frame["complete"].astype(bool)]
            report.wallets_complete = int(complete["wallet"].nunique())
            eligible = prepare_positions(frame, cfg.scoring, cfg.categories, blocklist)
            flags = wallet_flags(complete[FLAG_COLUMNS], cfg.scoring)
            scores = score_wallets(eligible, flags, cfg.scoring, as_of=now)
            store.replace_scores(scores)
            write_parquet_atomic(scores, cfg.path(cfg.paths.scores_parquet))
            certified = scores[scores["certified"].astype(bool)]
            report.tests = len(scores)
            report.testable = int(((scores["n_eff"] >= cfg.scoring.min_n_eff) & (scores["flags"] == "")).sum())
            report.certified_wallets = int(certified["wallet"].nunique())
            report.top = certified.sort_values("post_edge", ascending=False).head(25)
            report.api_errors = failures + (http.errors if http else 0) + apis.skipped()
            log.info("scored %d tests, %d certified wallets", report.tests, report.certified_wallets)
            if stop_after_scoring:
                return report

            signals, contacts = await build_signals(apis, store, cfg, scores, blocklist, now)
            validation = None
            if not skip_validation:
                validation = await maybe_validate(apis, store, cfg, eligible, flags, scores, blocklist, now,
                                                  force=force_validation)
                report.validation_ran = validation is not None
            report.signals, report.contacts = len(signals), len(contacts)
            report.api_errors = failures + (http.errors if http else 0) + apis.skipped()

            names = {w: s.name for w, s in store.wallet_state().items()}
            out = cfg.path(cfg.paths.snapshot_dir)
            write_json_atomic(out / "signals.json", signals)
            write_json_atomic(out / "contacts.json", contacts)
            write_json_atomic(out / "whales.json", whales_json(scores, eligible, names))
            if validation is not None:
                write_json_atomic(out / "validation.json", validation)
            write_json_atomic(out / "meta.json", _meta_json(cfg, report, now, store.get_meta("validation_at")))
    finally:
        if http is not None:
            await http.aclose()
    if publish:
        git_publish(cfg.path(cfg.paths.snapshot_dir), now)
    return report
