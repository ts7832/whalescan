"""`whalescan history`: rebuild months of large news-market bets for the evidence study (evidence-study spec §2).

Everything here is incremental — markets, fills, profiles, wallet histories and price windows already stored are
never fetched again — and look-ahead free: facts about a bet are only those knowable when it was placed.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any

from whalescan.api.data_api import MAX_OFFSET
from whalescan.batch import Apis, _guarded, _run_all
from whalescan.classify import Blocklist, category_for_tags
from whalescan.config import Config
from whalescan.finder_math import is_covered
from whalescan.gate import PositionEvent, aggregate
from whalescan.history_store import HistoryStore
from whalescan.insider import account_age_days
from whalescan.models import Market, WalletProfile
from whalescan.store import trades_from_frame

log = logging.getLogger(__name__)
DAY, HOUR = 86400, 3600
OPEN_HORIZON_S = 730 * DAY  # open markets ending up to two years out still count toward bets per week
PRICE_WINDOW_SLACK_MIN = 10
FINDER_SLOT_S = 6 * HOUR  # a slot's window (below) covers 24h from its END, so every bet inside gets a full 24h


@dataclass
class HistoryReport:
    markets: int = 0
    fills: int = 0
    truncated_markets: int = 0
    profiles: int = 0
    wallet_histories: int = 0
    price_windows: int = 0
    covered_markets: int = 0
    finder_windows: int = 0


def is_studied(market: Market, cfg: Config, blocklist: Blocklist) -> bool:
    """A news-category market (the insider categories) that the scanner would not blocklist."""
    return (category_for_tags(market.tags, cfg.categories) in cfg.insider.categories
            and not blocklist.blocked(event_slug=market.event_slug, slug=market.slug, volume=market.volume))


def study_events(store: HistoryStore, cfg: Config, *, closed_only: bool = False,
                 news_only: bool = True) -> list[PositionEvent]:
    """Position events in studied markets, built exactly as the live scanner builds them: only fills of at least
    [sweep].fill_min_usdc (wallet histories add smaller ones the scanner never sees), grouped by `aggregate()`.
    `news_only` (default) scopes to the evidence study's own narrower market set, unaffected by the Insider
    Finder's wider listing; pass False for the Finder's own (any-category-but-sports) coverage."""
    ids = store.study_market_ids(closed_only=closed_only) if news_only else store.covered_market_ids(closed_only=closed_only)
    frame = store.trades_frame()
    if frame.empty or not ids:
        return []
    frame = frame[frame["condition_id"].isin(ids) & (frame["price"] * frame["size"] >= cfg.sweep.fill_min_usdc)]
    return aggregate(trades_from_frame(frame), cfg.gate.aggregation_window_s)


def signal_ts(ev: PositionEvent) -> int:
    """When a copier could act: the scanner sees the whole position only once its last fill has happened."""
    return ev.last_ts


def _sample_key(event_id: str, seed: int) -> str:
    return hashlib.sha256(f"{seed}:{event_id}".encode()).hexdigest()


async def _bounded(items: Iterable[Any], fn: Callable[[Any], Awaitable[None]], concurrency: int, *,
                   label: str = "") -> None:
    """Run `fn` over `items`, at most `concurrency` at a time, logging progress every 10% (long phases take
    tens of minutes; a silent one is indistinguishable from a hung one)."""
    work = list(items)
    sem = asyncio.Semaphore(concurrency)
    step = max(1, len(work) // 10)
    done = 0

    async def one(item: Any) -> None:
        nonlocal done
        async with sem:
            await fn(item)
        done += 1
        if label and (done % step == 0 or done == len(work)):
            log.info("history: %s %d/%d", label, done, len(work))

    await _run_all(one(i) for i in work)


async def build_history(apis: Apis, store: HistoryStore, cfg: Config, now: int) -> HistoryReport:
    sc, rep = cfg.study, HistoryReport()
    blocklist = Blocklist(cfg.blocklist)
    start = now - sc.lookback_days * DAY
    floor = cfg.blocklist.min_market_volume

    # 1. The markets: closed in the window (scored), and still open (counted toward bets per week only).
    closed = [m for m in await apis.gamma.listed_markets(closed=True, end_min_ts=start, end_max_ts=now,
                                                         min_volume=floor) if is_studied(m, cfg, blocklist)]
    opened = [m for m in await apis.gamma.listed_markets(closed=False, end_min_ts=start,
                                                         end_max_ts=now + OPEN_HORIZON_S, min_volume=floor)
              if is_studied(m, cfg, blocklist)]
    store.upsert_markets(closed + opened, now)
    store.register_markets([m.condition_id for m in closed], is_open=False)
    store.register_markets([m.condition_id for m in opened], is_open=True)
    rep.markets = len(closed) + len(opened)
    log.info("history: %d studied markets (%d resolved, %d open)", rep.markets, len(closed), len(opened))

    # 1b. Insider Finder: the same listing, widened to any category but sports/price-threshold markets. A market
    # already registered above (is_news) keeps that status; this only ever adds coverage, never narrows it.
    cov_closed = [m for m in await apis.gamma.listed_markets(closed=True, end_min_ts=start, end_max_ts=now,
                                                             min_volume=floor) if is_covered(m, cfg, blocklist)]
    cov_opened = [m for m in await apis.gamma.listed_markets(closed=False, end_min_ts=start,
                                                             end_max_ts=now + OPEN_HORIZON_S, min_volume=floor)
                 if is_covered(m, cfg, blocklist)]
    store.upsert_markets(cov_closed + cov_opened, now)
    store.register_markets([m.condition_id for m in cov_closed], is_open=False, is_news=False)
    store.register_markets([m.condition_id for m in cov_opened], is_open=True, is_news=False)
    rep.covered_markets = len(cov_closed) + len(cov_opened)
    log.info("history: %d covered markets for the Insider Finder (%d resolved, %d open)", rep.covered_markets,
             len(cov_closed), len(cov_opened))

    # 2. Their fills (>= the sweep's floor), once per resolved market, daily for open ones — the union of both
    #    listings above, since markets_needing_fills does not distinguish how a market was registered.
    async def fills(cid: str) -> None:
        page = await _guarded(apis.data.trades(market=cid, min_usdc=cfg.sweep.fill_min_usdc), f"fills {cid}")
        if page is None:
            return
        store.upsert_trades(page.trades)
        truncated = not page.complete and len(page.trades) >= MAX_OFFSET  # the API's depth cap, not a failure
        store.mark_fills(cid, now, complete=page.complete or truncated)
        rep.fills += len(page.trades)
        rep.truncated_markets += int(truncated)

    todo = store.markets_needing_fills(now)
    log.info("history: reading fills of %d markets", len(todo))
    await _bounded(todo, fills, cfg.http.concurrency, label="fills")

    # 3. Account creation dates of every wallet that placed a big buy (age at the bet: no look-ahead).
    events = study_events(store, cfg)
    big = [e for e in events if e.side == "BUY" and e.usdc >= cfg.ledger.near_miss_usdc_min]
    wallets = {e.wallet for e in big}
    missing = sorted(wallets - store.profiles(wallets).keys())

    async def profile(wallet: str) -> None:
        created = await _guarded(apis.gamma.created_ts(wallet), f"profile {wallet}")
        store.upsert_profiles([WalletProfile(wallet, created, None, now)])
        rep.profiles += 1

    log.info("history: %d big buys by %d wallets; %d profiles to fetch", len(big), len(wallets), len(missing))
    await _bounded(missing, profile, cfg.http.concurrency, label="profiles")

    # 4. Full trade histories of young wallets, to count the markets they had traded before each bet.
    profiles = store.profiles(wallets)
    ages = {e.id: account_age_days(e, profiles.get(e.wallet)) for e in big}
    young = {e.wallet for e in big if ages[e.id] is not None and ages[e.id] <= sc.history_max_age_days}
    todo_wallets = sorted(young - store.wallets_history_done())

    async def history(wallet: str) -> None:
        page = await _guarded(apis.data.trades(user=wallet), f"history {wallet}")
        if page is None:
            return
        store.upsert_trades(page.trades)
        truncated = not page.complete and len(page.trades) >= MAX_OFFSET  # the depth cap: will never be complete
        if page.complete or truncated:  # a history that failed midway is simply retried next run
            store.mark_wallet_history(wallet, now, complete=page.complete, truncated=truncated)
            rep.wallet_histories += 1

    await _bounded(todo_wallets, history, cfg.http.concurrency, label="wallet histories")

    # 5. Minute prices after each scoreable bet: every young wallet's, plus a fixed sample of the rest (BASELINE).
    closed_ids = store.study_market_ids(closed_only=True)
    scoreable = [e for e in big if e.condition_id in closed_ids]
    fresh = [e for e in scoreable if e.wallet in young]
    rest = sorted((e for e in scoreable if e.wallet not in young), key=lambda e: _sample_key(e.id, sc.seed))
    wanted = {(e.asset, signal_ts(e)) for e in fresh + rest[:sc.baseline_sample]}
    windows = sorted(w for w in wanted if not store.has_price_window(*w))
    span = (max(sc.entry_delays_min) + PRICE_WINDOW_SLACK_MIN) * 60

    async def prices(window: tuple[str, int]) -> None:
        asset, t0 = window
        pts = await _guarded(apis.clob.price_window(asset, t0, t0 + span), f"prices {asset}")
        if pts is None:
            return
        store.upsert_prices(asset, [(p.ts, p.price) for p in pts])
        store.mark_price_window(asset, t0)
        rep.price_windows += 1

    log.info("history: %d price windows to fetch", len(windows))
    await _bounded(windows, prices, cfg.http.concurrency, label="price windows")

    # 6. Insider Finder: did the price move the wallet's way within 24h of each covered big bet? One 30h/10min
    # window per (asset, 6h slot) serves every bet placed in that slot, since the slot's latest possible bet
    # (at its very end) still gets a full 24h look before the window itself ends.
    fc = cfg.finder
    covered_events = study_events(store, cfg, news_only=False)
    finder_big = [e for e in covered_events if e.side == "BUY" and e.usdc >= fc.min_usdc]
    cov_closed_ids = store.covered_market_ids(closed_only=True)
    finder_scoreable = [e for e in finder_big if e.condition_id in cov_closed_ids]
    finder_span = int(fc.move_window_h * 3600) + FINDER_SLOT_S
    finder_slots = {(e.asset, (signal_ts(e) // FINDER_SLOT_S) * FINDER_SLOT_S) for e in finder_scoreable}
    finder_todo = sorted(w for w in finder_slots if not store.has_finder_window(*w))

    async def finder_prices(window: tuple[str, int]) -> None:
        asset, slot_ts = window
        pts = await _guarded(apis.clob.price_window(asset, slot_ts, slot_ts + finder_span, fidelity=10),
                             f"finder prices {asset}")
        if pts is None:
            return
        store.upsert_finder_prices(asset, [(p.ts, p.price) for p in pts])
        store.mark_finder_window(asset, slot_ts)
        rep.finder_windows += 1

    log.info("history: %d finder price windows to fetch", len(finder_todo))
    await _bounded(finder_todo, finder_prices, cfg.http.concurrency, label="finder price windows")
    return rep


async def run_history(cfg: Config, *, apis: Apis | None = None, now: int | None = None) -> HistoryReport:
    """Open the study database and bring it up to date (safe to re-run: nothing stored is fetched again)."""
    import time

    from whalescan.api.clob import ClobApi
    from whalescan.api.data_api import DataApi
    from whalescan.api.gamma import GammaApi
    from whalescan.api.http import HttpClient

    now = now or int(time.time())
    http: HttpClient | None = None
    if apis is None:
        http = HttpClient(user_agent=cfg.http.user_agent, rate_per_s=cfg.http.rate_per_s,
                          max_retries=cfg.http.max_retries, host_rates=cfg.http.host_rates)
        apis = Apis(DataApi(http), GammaApi(http), ClobApi(http))
    try:
        with HistoryStore(cfg.path(cfg.paths.history_db)) as store:
            return await build_history(apis, store, cfg, now)
    finally:
        if http is not None:
            await http.aclose()
