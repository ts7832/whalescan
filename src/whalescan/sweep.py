"""The 15-minute insider sweep: WHALESCAN's primary loop, cheap enough to run all day on GitHub Actions.

Every run: fetch every fill >= [sweep].fill_min_usdc across all of Polymarket since the previous run (usually one
API request), keep a rolling 24 h window in data/sweep.duckdb, add fills up per wallet, look up the account age of
new big news-market bettors, and gate everything (insider rules; snipers from the daily scores). It rewrites only the
alert files of the snapshot; the daily batch owns dossiers, scores and backtests.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

import pandas as pd

from whalescan import __version__
from whalescan.api.clob import ClobApi
from whalescan.api.data_api import DataApi
from whalescan.api.gamma import GammaApi
from whalescan.api.http import HttpClient
from whalescan.batch import Apis, _guarded, evaluate_window, git_publish, refresh_markets
from whalescan.classify import Blocklist
from whalescan.config import Config
from whalescan.gate import ScoreBook
from whalescan.scoring import SCORE_COLUMNS
from whalescan.snapshot import write_json_atomic
from whalescan.store import Store

log = logging.getLogger(__name__)
OVERLAP_S = 300  # re-read the last 5 minutes: fills can appear in the API slightly late


@dataclass
class SweepReport:
    fills: int = 0
    complete: bool = True
    signals: int = 0
    insiders: int = 0
    contacts: int = 0


def _read_json(path: Any, default: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


async def run_sweep(cfg: Config, *, apis: Apis | None = None, now: int | None = None,
                    publish: bool = False) -> SweepReport:
    now = now or int(time.time())
    http: HttpClient | None = None
    if apis is None:
        http = HttpClient(user_agent=cfg.http.user_agent, rate_per_s=cfg.http.rate_per_s,
                          max_retries=cfg.http.max_retries, host_rates=cfg.http.host_rates)
        apis = Apis(DataApi(http), GammaApi(http), ClobApi(http))
    report = SweepReport()
    window = int(cfg.gate.signal_lookback_h * 3600)
    out = cfg.path(cfg.paths.snapshot_dir)
    try:
        with Store(cfg.path(cfg.paths.research_db).with_name("sweep.duckdb")) as store:
            last = store.latest_trade_ts()
            since = max(now - window, (last - OVERLAP_S) if last is not None else now - window)
            page = await _guarded(apis.data.trades(min_usdc=cfg.sweep.fill_min_usdc, since_ts=since), "sweep")
            if page is not None:
                store.upsert_trades(page.trades)
                report.fills, report.complete = len(page.trades), page.complete
                if not page.complete:
                    log.warning("sweep hit the API depth limit: some fills since %d were not read", since)
            store.prune_trades(now - window)
            await refresh_markets(apis, store, now)

            scores_path = out / "scores.parquet"
            scores = pd.read_parquet(scores_path) if scores_path.exists() else pd.DataFrame(columns=SCORE_COLUMNS)
            book = ScoreBook.for_config(scores, cfg)
            signals, contacts = await evaluate_window(apis, store, cfg, book, Blocklist(cfg.blocklist), now,
                                                      now - window)
    finally:
        if http is not None:
            await http.aclose()

    report.signals, report.contacts = len(signals), len(contacts)
    report.insiders = sum(1 for s in signals if s["kind"] == "INSIDER")
    meta = _read_json(out / "meta.json", {})
    counts = {**meta.get("counts", {}), "signals": report.signals, "insiders": report.insiders,
              "contacts": report.contacts}
    meta.update({"generated_at": now, "sweep_at": now, "version": __version__, "mode": "SNAPSHOT", "counts": counts})
    meta.setdefault("params", {"bh_q": cfg.scoring.bh_q, "min_usdc": cfg.gate.min_usdc,
                               "conviction_k": cfg.gate.conviction_k, "follow_size_usdc": cfg.gate.follow_size_usdc,
                               "min_net_edge": cfg.gate.min_net_edge, "signal_lookback_h": cfg.gate.signal_lookback_h})
    meta.setdefault("errors", {"api": 0})
    meta.setdefault("validation_generated_at", None)
    write_json_atomic(out / "signals.json", signals)
    write_json_atomic(out / "contacts.json", contacts)
    write_json_atomic(out / "meta.json", meta)
    log.info("sweep: %d fills read, %d alerts (%d insiders), %d contacts", report.fills, report.signals,
             report.insiders, report.contacts)
    if publish:
        git_publish(out, now)
    return report
