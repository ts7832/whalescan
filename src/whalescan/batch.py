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
from whalescan.api.profiles import fetch_profile
from whalescan.gate import Evaluation, GateContext, ScoreBook, aggregate, evaluate, needs_book
from whalescan.insider import evaluate_insider, is_near_miss_candidate, needs_insider_book
from whalescan.parsers import ParseError
from whalescan.scoring import apply_history_window, prepare_positions, score_wallets
from whalescan.snapshot import evaluation_json, whales_json, write_json_atomic, write_parquet_atomic
from whalescan.store import Store, trades_from_frame
from whalescan.validate import insider_backtest, insider_verdict, run_validation

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
class WindowResult:
    """Everything evaluate_window produced, so callers (the daily batch, the sweep, and the Track Record
    ledger — record_calls) can build their own JSON or log calls without re-running the gate or re-fetching
    profiles."""

    signals: list[dict[str, Any]]
    contacts: list[dict[str, Any]]
    evaluations: list[Evaluation]
    markets: Mapping[str, Any]
    profiles: dict[str, Any]


@dataclass
class BatchReport:
    wallets_scanned: int = 0
    wallets_complete: int = 0
    tests: int = 0
    testable: int = 0
    certified_wallets: int = 0
    signals: int = 0
    insiders: int = 0
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
    except (ApiError, ParseError) as e:
        log.warning("%s failed: %s", what, e)
        return None


async def _run_all(coros: Any) -> None:
    """Run coroutines concurrently; the first failure cancels the rest and is re-raised as itself
    (not wrapped in an ExceptionGroup), so BlockedError still reaches the CLI's exit-code-3 path."""
    try:
        async with asyncio.TaskGroup() as tg:
            for c in coros:
                tg.create_task(c)
    except ExceptionGroup as eg:
        raise eg.exceptions[0] from None


def drop_stale_wallets(frame: pd.DataFrame, *, now: int, max_age_days: float) -> pd.DataFrame:
    """Wallets not refreshed recently (e.g. dropped from the universe) are not scored from old data."""
    if frame.empty:
        return frame
    fetched = pd.to_numeric(frame["fetched_at"], errors="coerce").fillna(0)
    return frame[fetched >= now - max_age_days * 86400]


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
    universe = dict(list(sources.items())[:u.max_wallets])
    log.info("universe: %d wallets (%d from leaderboards, %d large trades seen)", len(universe),
             sum(1 for s in universe.values() if s == "leaderboard"), len(page.trades))
    return universe


async def refresh_positions(apis: Apis, store: Store, wallets: Mapping[str, str], concurrency: int, now: int) -> int:
    state = store.wallet_state()
    sem = asyncio.Semaphore(concurrency)
    failures = 0
    done = 0
    step = max(1, len(wallets) // 20)

    async def one(wallet: str, source: str) -> None:
        nonlocal failures, done
        prev = state.get(wallet)
        since = prev.max_ts if prev and prev.complete and prev.max_ts is not None else None
        async with sem:
            hist = await _guarded(apis.data.closed_positions(wallet, since_ts=since), f"positions {wallet}")
            open_hist = await _guarded(apis.data.redeemable_positions(wallet), f"redeemable {wallet}")
        if hist is None or open_hist is None:
            failures += 1
            return
        # Unredeemed resolved positions first, then sold/redeemed ones, so a closed record wins a key clash.
        store.upsert_positions(open_hist.positions)
        store.upsert_positions(hist.positions)
        store.record_wallet_fetch(wallet, fetched_at=now, complete=hist.complete and open_hist.complete,
                                  source=source)
        if since is None:  # full fetch: (re)define the history window; incremental runs keep it
            start = min((p.ts for p in hist.positions), default=None) if hist.truncated else None
            store.set_history_start(wallet, start)
        done += 1
        if done % step == 0 or done == len(wallets):
            log.info("positions: %d/%d wallets", done, len(wallets))

    await _run_all(one(w, s) for w, s in wallets.items())
    return failures


async def refresh_markets(apis: Apis, store: Store, now: int) -> None:
    ids = store.condition_ids_needing_refresh(now, MARKET_MAX_AGE_S)
    if ids:
        log.info("fetching metadata for %d markets", len(ids))
        found = await apis.gamma.markets(ids)
        store.upsert_markets(found.values(), now)
        store.mark_missing_markets(ids - found.keys(), now)


async def build_signals(apis: Apis, store: Store, cfg: Config, scores: pd.DataFrame, blocklist: Blocklist,
                        now: int) -> WindowResult:
    g = cfg.gate
    since = now - int(g.signal_lookback_h * 3600)
    book = ScoreBook.for_config(scores, cfg)
    sem = asyncio.Semaphore(cfg.http.concurrency)

    async def wallet_trades(wallet: str) -> None:
        async with sem:
            page = await _guarded(apis.data.trades(user=wallet, since_ts=since), f"trades {wallet}")
        if page is not None:
            store.upsert_trades(page.trades)

    await _run_all(wallet_trades(w) for w in sorted(book.certified_wallets()))
    page = await _guarded(apis.data.trades(min_usdc=g.min_usdc, since_ts=since), "large trades")
    if page is not None:
        store.upsert_trades(page.trades)
    await refresh_markets(apis, store, now)

    return await evaluate_window(apis, store, cfg, book, blocklist, now, since)


async def evaluate_window(apis: Apis, store: Store, cfg: Config, book: ScoreBook, blocklist: Blocklist, now: int,
                          since: int) -> WindowResult:
    """Gate every position event in the stored trades since `since`: snipers via G1–G7, everyone else via the
    insider rules (including near-misses, for the Track Record ledger). Shared by the daily batch, the
    15-minute sweep, and (via .evaluations/.markets/.profiles) the ledger's record_calls."""
    g = cfg.gate
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

    evaluations, profiles = await _apply_insider_detector(apis, store, cfg, evaluations, markets, blocklist, now,
                                                           book.certified_wallets())

    names = {w: s.name for w, s in store.wallet_state().items()}
    signals = []
    # Insider alerts first (WHALESCAN's primary signal), then snipers; best tier first within each.
    for e in sorted((e for e in evaluations if e.status in ("SIGNAL", "INSIDER")),
                    key=lambda e: (e.status != "INSIDER", e.tier or "Z", -(e.net_edge or 0.0), -e.event.usdc)):
        history = await _guarded(apis.clob.price_history(e.event.asset), f"history {e.event.asset}")
        signals.append(evaluation_json(e, markets.get(e.event.condition_id), names, history))
    contacts = [evaluation_json(e, markets.get(e.event.condition_id), names, None)
                for e in sorted(evaluations, key=lambda e: -e.event.last_ts)
                if e.status not in ("SIGNAL", "INSIDER") and e.event.usdc >= g.min_usdc][:g.max_contacts]
    return WindowResult(signals, contacts, evaluations, markets, profiles)


async def _profiles(apis: Apis, store: Store, cfg: Config, wallets: set[str], now: int) -> dict[str, Any]:
    stale = store.stale_profiles(wallets, now=now, ttl_s=int(cfg.insider.profile_ttl_h * 3600),
                                 unknown_ttl_s=int(cfg.insider.unknown_profile_ttl_h * 3600))
    sem = asyncio.Semaphore(cfg.http.concurrency)

    async def one(w: str) -> Any:
        async with sem:
            return await _guarded(fetch_profile(apis.gamma, apis.data, w, now=now), f"profile {w}")

    store.upsert_profiles(p for p in await asyncio.gather(*(one(w) for w in sorted(stale))) if p is not None)
    return store.profiles(wallets)


async def _apply_insider_detector(apis: Apis, store: Store, cfg: Config, evaluations: list[Evaluation],
                                  markets: Mapping[str, Any], blocklist: Blocklist, now: int,
                                  proven: set[str]) -> tuple[list[Evaluation], dict[str, Any]]:
    """Large news-market buys by wallets that aren't proven snipers: judge them as possible insiders instead.
    Widened to the near-miss size floor (not just the insider min_usdc) so the ledger can log near-misses too;
    returns the updated evaluations plus every profile fetched, keyed by wallet."""
    ic = cfg.insider
    cands = [i for i, e in enumerate(evaluations)
             if e.status != "SIGNAL" and e.event.wallet not in proven and e.event.condition_id in markets
             and is_near_miss_candidate(e.event, e.category, ic, cfg.ledger)]
    if not cands:
        return evaluations, {}
    profiles = await _profiles(apis, store, cfg, {evaluations[i].event.wallet for i in cands}, now)
    out = list(evaluations)
    for i in cands:
        e = evaluations[i]
        m = markets.get(e.event.condition_id)
        ie = evaluate_insider(e.event, profiles.get(e.event.wallet), m, e.category, ic, blocklist, e.quote, now)
        if needs_insider_book(ie):
            snap = await _guarded(apis.clob.book(e.event.asset), f"book {e.event.asset}")
            if snap is not None:
                ie = evaluate_insider(e.event, profiles.get(e.event.wallet), m, e.category, ic, blocklist,
                                      follow_quote(snap, cfg.gate.follow_size_usdc), now)
        out[i] = ie
    return out, profiles


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

    await _run_all(history(w) for w in wallets)
    await refresh_markets(apis, store, now)
    trades = trades_from_frame(store.trades_frame(wallets=wallets))
    markets = store.markets_by_id({t.condition_id for t in trades})
    report = run_validation(eligible, flags, trades, markets, cfg, blocklist, now=now)

    # Insider backtest: every large buy we have on record (global feed + wallet histories), fresh accounts only.
    big = trades_from_frame(store.large_buy_trades(cfg.insider.min_usdc))
    await refresh_markets(apis, store, now)
    big_markets = store.markets_by_id({t.condition_id for t in big})
    from whalescan.classify import category_for_tags
    news_wallets = {t.wallet for t in big if (m := big_markets.get(t.condition_id)) is not None
                    and category_for_tags(m.tags, cfg.categories) in cfg.insider.categories}
    profiles = await _profiles(apis, store, cfg, news_wallets, now)
    report["groups"]["INSIDER"] = insider_backtest(big, big_markets, profiles, cfg, blocklist)
    report["insider_verdict"] = insider_verdict(report["groups"]["INSIDER"], cfg)
    report["caveats"] += [
        "Insider backtest: the verdict needs >= 10 distinct wallets and uses a per-wallet t-statistic, because one "
        "insider's many bets on one piece of news are not independent.",
        "Insider backtest survivorship: older history comes mostly from wallets selected for being active or "
        "profitable today, so fresh accounts that bet once and vanished are under-represented.",
        "Insider backtest skips the markets-traded rule (only today's count is known).",
    ]
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
            "signals": report.signals, "insiders": report.insiders, "contacts": report.contacts,
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
                          max_retries=cfg.http.max_retries, host_rates=cfg.http.host_rates)
        apis = Apis(DataApi(http), GammaApi(http), ClobApi(http))
    report = BatchReport()
    blocklist = Blocklist(cfg.blocklist)
    try:
        with Store(cfg.path(cfg.paths.research_db)) as store:
            wallets = await discover_universe(apis, store, cfg, now)
            report.wallets_scanned = len(wallets)
            failures = await refresh_positions(apis, store, wallets, cfg.http.concurrency, now)
            await refresh_markets(apis, store, now)

            frame = drop_stale_wallets(store.positions_frame(), now=now,
                                       max_age_days=cfg.universe.max_wallet_age_days)
            complete = frame[frame["complete"].astype(bool)]
            report.wallets_complete = int(complete["wallet"].nunique())
            eligible = prepare_positions(frame, cfg.scoring, cfg.categories, blocklist)
            flags = wallet_flags(apply_history_window(complete)[FLAG_COLUMNS], cfg.scoring)
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

            window = await build_signals(apis, store, cfg, scores, blocklist, now)
            signals, contacts = window.signals, window.contacts
            validation = None
            if not skip_validation:
                validation = await maybe_validate(apis, store, cfg, eligible, flags, scores, blocklist, now,
                                                  force=force_validation)
                report.validation_ran = validation is not None
            report.signals, report.contacts = len(signals), len(contacts)
            report.insiders = sum(1 for s in signals if s["kind"] == "INSIDER")
            report.api_errors = failures + (http.errors if http else 0) + apis.skipped()

            names = {w: s.name for w, s in store.wallet_state().items()}
            out = cfg.path(cfg.paths.snapshot_dir)
            write_parquet_atomic(scores, out / "scores.parquet")  # read by the 15-minute sweep
            write_json_atomic(out / "signals.json", signals)
            write_json_atomic(out / "contacts.json", contacts)
            write_json_atomic(out / "whales.json", whales_json(scores, eligible, names,
                                                               min_n_eff=cfg.scoring.min_n_eff))
            if validation is not None:
                write_json_atomic(out / "validation.json", validation)
            write_json_atomic(out / "meta.json", _meta_json(cfg, report, now, store.get_meta("validation_at")))
    finally:
        if http is not None:
            await http.aclose()
    if publish:
        git_publish(cfg.path(cfg.paths.snapshot_dir), now)
    return report
