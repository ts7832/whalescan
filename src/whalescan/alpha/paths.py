"""Price paths around every big bet, for markouts (+1 h, +24 h, +7 d), 24 h pre-bet momentum and a +15 min entry
price for bets outside the evidence study's minute-price sample.

CLOB /prices-history refuses spans over ~15 days, so each asset's bets are covered greedily by 14-day windows
[s, s + 14 d] that each serve every bet t with s + 25 h <= t <= s + 14 d - 7 d - 1 h. Stored in their own DuckDB
file (data/alpha.duckdb) so this can run while `whalescan history` holds the study database.

    python -m whalescan.alpha.paths --bets-db data/history.duckdb [--limit N]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import time

import duckdb
import pandas as pd

from whalescan.api.clob import ClobApi
from whalescan.api.http import HttpClient
from whalescan.batch import _guarded
from whalescan.config import load_config
from whalescan.history import _bounded

log = logging.getLogger(__name__)
HOUR, DAY = 3600, 86400
SPAN = 14 * DAY
PRE, POST = 25 * HOUR, 7 * DAY + HOUR
FIDELITY_MIN = 10

SCHEMA = """
CREATE TABLE IF NOT EXISTS a_paths (asset VARCHAR, ts BIGINT, price DOUBLE, PRIMARY KEY (asset, ts));
CREATE TABLE IF NOT EXISTS a_windows (asset VARCHAR, start_ts BIGINT, n INTEGER, fetched_at BIGINT,
                                      PRIMARY KEY (asset, start_ts));
"""


def plan_windows(times: list[int]) -> list[int]:
    """Greedy cover of sorted bet times by windows; returns window start times."""
    starts: list[int] = []
    for t in sorted(times):
        if starts and starts[-1] + PRE <= t <= starts[-1] + SPAN - POST:
            continue
        starts.append(t - PRE)
    return starts


def bet_times(bets_db: str, min_usdc: float) -> dict[str, list[int]]:
    """(asset -> signal times) of every big BUY in any registered market, aggregated like the scanner does
    (approximation for planning only: the per-fill max timestamp of 10-minute groups is close enough to cover)."""
    con = duckdb.connect(bets_db, read_only=True)
    rows = con.execute(
        """WITH f AS (
             SELECT t.wallet, t.asset, t.ts, t.price * t.size AS usdc FROM trades t
             JOIN h_market_state s USING (condition_id)
             WHERE t.side = 'BUY' AND t.price * t.size >= 1000)
           SELECT asset, ts // 600 * 600 AS slot, max(ts) AS ts FROM f
           GROUP BY wallet, asset, slot HAVING sum(usdc) >= ?""", [min_usdc]).fetchall()
    con.close()
    out: dict[str, list[int]] = {}
    for asset, _slot, ts in rows:
        out.setdefault(asset, []).append(int(ts))
    return out


async def fetch(bets_db: str, alpha_db: str, min_usdc: float, limit: int | None) -> None:
    cfg = load_config()
    times = bet_times(bets_db, min_usdc)
    con = duckdb.connect(alpha_db)
    con.execute(SCHEMA)
    have = {(a, s) for a, s in con.execute("SELECT asset, start_ts FROM a_windows").fetchall()}
    now = int(time.time())
    todo = [(a, s) for a, ts in times.items() for s in plan_windows(ts) if (a, s) not in have and s < now - PRE]
    todo.sort()
    if limit:
        todo = todo[:limit]
    log.info("paths: %d assets, %d windows to fetch (%d stored)", len(times), len(todo), len(have))
    http = HttpClient(user_agent=cfg.http.user_agent, rate_per_s=cfg.http.rate_per_s,
                      max_retries=cfg.http.max_retries, host_rates=cfg.http.host_rates)
    clob = ClobApi(http)
    buf: list[tuple[str, int, float]] = []
    done: list[tuple[str, int, int, int]] = []

    def flush() -> None:
        """Bulk insert (row-by-row executemany is ~100x slower in DuckDB)."""
        if buf:
            con.register("buf", pd.DataFrame(buf, columns=["asset", "ts", "price"]))
            con.execute("INSERT OR IGNORE INTO a_paths SELECT * FROM buf")
            con.unregister("buf")
        if done:
            con.register("done", pd.DataFrame(done, columns=["asset", "start_ts", "n", "fetched_at"]))
            con.execute("INSERT OR REPLACE INTO a_windows SELECT * FROM done")
            con.unregister("done")
        buf.clear()
        done.clear()

    async def one(w: tuple[str, int]) -> None:
        asset, start = w
        pts = await _guarded(clob.price_window(asset, start, min(start + SPAN, now), fidelity=FIDELITY_MIN),
                             f"path {asset}")
        if pts is None:
            return
        buf.extend((asset, p.ts, p.price) for p in pts)
        done.append((asset, start, len(pts), now))
        if len(done) >= 300:
            flush()

    try:
        await _bounded(todo, one, cfg.http.concurrency, label="price paths")
    finally:
        flush()
        await http.aclose()
        con.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bets-db", default="data/history.duckdb")
    ap.add_argument("--alpha-db", default="data/alpha.duckdb")
    ap.add_argument("--min-usdc", type=float, default=5000)
    ap.add_argument("--limit", type=int)
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(fetch(a.bets_db, a.alpha_db, a.min_usdc, a.limit))


if __name__ == "__main__":
    main()
