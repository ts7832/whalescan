"""DuckDB persistence. One writer process per file, enforced with an advisory lock file."""

from __future__ import annotations

import fcntl
import math
import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import astuple, dataclass, fields
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

from whalescan.models import ClosedPosition, Market, Trade, WalletProfile, resolved_winner

# One order can sweep several price levels in one transaction: every (price, size) is a separate fill.
TRADE_KEY = ["tx_hash", "wallet", "asset", "side", "price", "size"]
TRADES_DDL = """CREATE TABLE {name} (
  tx_hash VARCHAR, ts BIGINT, wallet VARCHAR, asset VARCHAR, condition_id VARCHAR, side VARCHAR,
  price DOUBLE, size DOUBLE, event_slug VARCHAR, title VARCHAR, outcome VARCHAR, outcome_index INTEGER,
  fee DOUBLE, PRIMARY KEY (tx_hash, wallet, asset, side, price, size))"""

SCHEMA = """
{trades};
CREATE TABLE IF NOT EXISTS positions (
  wallet VARCHAR, asset VARCHAR, condition_id VARCHAR, avg_price DOUBLE, total_bought DOUBLE,
  realized_pnl DOUBLE, cur_price DOUBLE, outcome VARCHAR, outcome_index INTEGER, title VARCHAR,
  event_slug VARCHAR, ts BIGINT, PRIMARY KEY (wallet, asset));
CREATE TABLE IF NOT EXISTS markets (
  condition_id VARCHAR PRIMARY KEY, question VARCHAR, slug VARCHAR, event_slug VARCHAR, end_ts BIGINT,
  closed BOOLEAN, closed_ts BIGINT, outcome_prices DOUBLE[], token_ids VARCHAR[], fees_enabled BOOLEAN,
  fee_rate DOUBLE, fee_exponent DOUBLE, volume DOUBLE, tags VARCHAR[], fetched_at BIGINT);
CREATE TABLE IF NOT EXISTS wallets (
  wallet VARCHAR PRIMARY KEY, fetched_at BIGINT, complete BOOLEAN, source VARCHAR, name VARCHAR);
ALTER TABLE wallets ADD COLUMN IF NOT EXISTS history_start_ts BIGINT;
CREATE TABLE IF NOT EXISTS wallet_scores (
  wallet VARCHAR, category VARCHAR, n INTEGER, n_eff DOUBLE, edge DOUBLE, sigma DOUBLE, post_edge DOUBLE,
  p_value DOUBLE, bh_pass BOOLEAN, certified BOOLEAN, flags VARCHAR, median_stake DOUBLE, as_of BIGINT,
  PRIMARY KEY (wallet, category));
ALTER TABLE wallet_scores ADD COLUMN IF NOT EXISTS win_rate DOUBLE;
CREATE TABLE IF NOT EXISTS meta (key VARCHAR PRIMARY KEY, value VARCHAR);
CREATE TABLE IF NOT EXISTS missing_markets (condition_id VARCHAR PRIMARY KEY, fetched_at BIGINT);
CREATE TABLE IF NOT EXISTS wallet_profiles (
  wallet VARCHAR PRIMARY KEY, created_ts BIGINT, markets_traded INTEGER, fetched_at BIGINT);
"""

TRADE_COLS = [f.name for f in fields(Trade)]
POSITION_COLS = [f.name for f in fields(ClosedPosition)]
MARKET_COLS = [f.name for f in fields(Market)]
SCORE_COLS = ["wallet", "category", "n", "n_eff", "edge", "sigma", "post_edge", "p_value", "bh_pass",
              "certified", "flags", "median_stake", "as_of", "win_rate"]


class LockedError(RuntimeError):
    pass


class ProcessLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise LockedError(
                f"{self.path} is held by another whalescan process — stop it or wait for it to finish") from None
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        self._fd = fd

    def release(self) -> None:
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None


@dataclass(frozen=True)
class WalletState:
    fetched_at: int
    complete: bool
    max_ts: int | None
    name: str


def _as_list(value: Any) -> list[Any]:
    if value is None or value is pd.NA or (isinstance(value, float) and math.isnan(value)):
        return []
    return list(value)


def _opt_int(value: Any) -> int | None:
    return None if value is None or pd.isna(value) else int(value)


def trades_from_frame(df: pd.DataFrame) -> list[Trade]:
    out = []
    for row in df[TRADE_COLS].itertuples(index=False):
        d = row._asdict()
        d["fee"] = None if d["fee"] is None or pd.isna(d["fee"]) else float(d["fee"])
        d["ts"] = int(d["ts"])
        d["outcome_index"] = int(d["outcome_index"])
        out.append(Trade(**d))
    return out


class Store:
    def __init__(self, db_path: Path, *, lock: bool = True) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = ProcessLock(db_path.with_suffix(".lock")) if lock else None
        if self._lock:
            self._lock.acquire()
        try:
            self.con = duckdb.connect(str(db_path))
            exists = self.con.execute(
                "SELECT count(*) FROM information_schema.tables WHERE table_name = 'trades'").fetchone()[0]
            self.con.execute(SCHEMA.format(trades="" if exists else TRADES_DDL.format(name="trades")))
            self._migrate_trades_key()
        except Exception:
            if self._lock:
                self._lock.release()
            raise

    def _migrate_trades_key(self) -> None:
        """Databases created before multi-fill transactions were understood key trades on
        (tx, wallet, asset, side), which silently dropped all but one fill of a sweep. Rebuild with the full key."""
        row = self.con.execute(
            """SELECT constraint_column_names FROM duckdb_constraints()
               WHERE table_name = 'trades' AND constraint_type = 'PRIMARY KEY'""").fetchone()
        if row is None or list(row[0]) == TRADE_KEY:
            return
        cols = ", ".join(TRADE_COLS)
        self.con.execute("BEGIN")
        self.con.execute(TRADES_DDL.format(name="trades_v2"))
        self.con.execute(f"INSERT INTO trades_v2 SELECT DISTINCT {cols} FROM trades")
        self.con.execute("DROP TABLE trades")
        self.con.execute("ALTER TABLE trades_v2 RENAME TO trades")
        self.con.execute("COMMIT")

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self.con.close()
        if self._lock:
            self._lock.release()

    def _upsert_frame(self, table: str, df: pd.DataFrame, key: Sequence[str]) -> int:
        if df.empty:
            return 0
        df = df.drop_duplicates(subset=list(key), keep="last")
        cols = ", ".join(df.columns)
        self.con.register("_incoming", df)
        try:
            self.con.execute(f"INSERT OR REPLACE INTO {table} ({cols}) SELECT {cols} FROM _incoming")
        finally:
            self.con.unregister("_incoming")
        return len(df)

    def upsert_trades(self, trades: Iterable[Trade]) -> int:
        df = pd.DataFrame([astuple(t) for t in trades], columns=TRADE_COLS)
        return self._upsert_frame("trades", df, TRADE_KEY)

    def upsert_positions(self, positions: Iterable[ClosedPosition]) -> int:
        df = pd.DataFrame([astuple(p) for p in positions], columns=POSITION_COLS)
        return self._upsert_frame("positions", df, ["wallet", "asset"])

    def upsert_markets(self, markets: Iterable[Market], now: int) -> int:
        rows = [(m.condition_id, m.question, m.slug, m.event_slug, m.end_ts, m.closed, m.closed_ts,
                 list(m.outcome_prices), list(m.token_ids), m.fees_enabled, m.fee_rate, m.fee_exponent,
                 m.volume, list(m.tags), now) for m in {m.condition_id: m for m in markets}.values()]
        if rows:
            placeholders = ", ".join("?" * 15)
            self.con.executemany(f"INSERT OR REPLACE INTO markets VALUES ({placeholders})", rows)
        return len(rows)

    def record_wallet_fetch(self, wallet: str, *, fetched_at: int, complete: bool, source: str) -> None:
        self.con.execute(
            """INSERT INTO wallets (wallet, fetched_at, complete, source) VALUES (?, ?, ?, ?)
               ON CONFLICT (wallet) DO UPDATE SET fetched_at = excluded.fetched_at,
               complete = excluded.complete, source = excluded.source""",
            [wallet, fetched_at, complete, source])

    def set_history_start(self, wallet: str, ts: int | None) -> None:
        """Start of a depth-capped history window (None = full history). Scoring ignores earlier markets."""
        self.con.execute("UPDATE wallets SET history_start_ts = ? WHERE wallet = ?", [ts, wallet])

    def set_wallet_names(self, names: Mapping[str, str]) -> None:
        rows = [(w, n) for w, n in names.items() if n]
        if rows:
            self.con.executemany(
                """INSERT INTO wallets (wallet, name) VALUES (?, ?)
                   ON CONFLICT (wallet) DO UPDATE SET name = excluded.name""", rows)

    def wallet_state(self) -> dict[str, WalletState]:
        rows = self.con.execute(
            """SELECT w.wallet, w.fetched_at, w.complete, p.max_ts, w.name FROM wallets w
               LEFT JOIN (SELECT wallet, max(ts) AS max_ts FROM positions GROUP BY wallet) p USING (wallet)""").fetchall()
        return {w: WalletState(int(f or 0), bool(c), _opt_int(m), n or "") for w, f, c, m, n in rows}

    def condition_ids_needing_refresh(self, now: int, open_max_age_s: int, *, missing_retry_s: int = 86400,
                                      settle_window_s: int = 14 * 86400) -> set[str]:
        """Markets to (re)fetch: unknown ones (unless Gamma recently didn't have them), stale open ones, and
        recently closed ones whose outcome prices are not final yet (UMA resolution lags market close)."""
        rows = self.con.execute(
            """WITH ids AS (SELECT condition_id FROM positions UNION SELECT condition_id FROM trades)
               SELECT ids.condition_id FROM ids
               LEFT JOIN markets m USING (condition_id)
               LEFT JOIN missing_markets x USING (condition_id)
               WHERE (m.condition_id IS NULL AND (x.fetched_at IS NULL OR x.fetched_at < ?))
                  OR (NOT m.closed AND m.fetched_at < ?)
                  OR (m.closed AND m.fetched_at < ?
                      AND COALESCE(m.closed_ts, m.fetched_at) >= ?
                      AND len(list_filter(m.outcome_prices, p -> p > 0 AND p < 1)) > 0)""",
            [now - missing_retry_s, now - open_max_age_s, now - open_max_age_s, now - settle_window_s]).fetchall()
        return {r[0] for r in rows}

    def mark_missing_markets(self, ids: Iterable[str], now: int) -> None:
        rows = [(i, now) for i in ids]
        if rows:
            self.con.executemany("INSERT OR REPLACE INTO missing_markets VALUES (?, ?)", rows)

    def positions_frame(self) -> pd.DataFrame:
        df = self.con.execute(
            """SELECT p.wallet, p.asset, p.condition_id, p.avg_price, p.total_bought, p.realized_pnl, p.outcome,
                      p.outcome_index, p.title, p.ts, p.event_slug, m.slug AS market_slug,
                      m.event_slug AS market_event_slug, COALESCE(m.closed, FALSE) AS closed, m.closed_ts,
                      m.volume, m.tags, m.outcome_prices, COALESCE(w.complete, FALSE) AS complete,
                      w.history_start_ts, w.fetched_at
               FROM positions p
               LEFT JOIN markets m USING (condition_id)
               LEFT JOIN wallets w USING (wallet)""").df()
        winners = [resolved_winner(bool(c), _as_list(op)) for c, op in zip(df["closed"], df["outcome_prices"])]
        df["winner_index"] = pd.array(winners, dtype="Int64")
        df["tags"] = [_as_list(t) for t in df["tags"]]
        return df.drop(columns=["outcome_prices"])

    def trades_frame(self, *, since_ts: int | None = None, until_ts: int | None = None,
                     wallets: Iterable[str] | None = None) -> pd.DataFrame:
        clauses, params = [], []
        if since_ts is not None:
            clauses.append("ts >= ?")
            params.append(since_ts)
        if until_ts is not None:
            clauses.append("ts < ?")
            params.append(until_ts)
        if wallets is not None:
            wl = list(wallets)
            if not wl:
                return pd.DataFrame(columns=TRADE_COLS)
            clauses.append("wallet IN (SELECT unnest(?))")
            params.append(wl)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return self.con.execute(f"SELECT {', '.join(TRADE_COLS)} FROM trades {where} ORDER BY ts", params).df()

    def latest_trade_ts(self) -> int | None:
        row = self.con.execute("SELECT max(ts) FROM trades").fetchone()
        return _opt_int(row[0]) if row else None

    def prune_trades(self, before_ts: int) -> None:
        self.con.execute("DELETE FROM trades WHERE ts < ?", [before_ts])

    def large_buy_trades(self, min_usdc: float) -> pd.DataFrame:
        """BUY fills of every (wallet, asset) position that adds up to at least `min_usdc`."""
        cols = ", ".join(f"t.{c}" for c in TRADE_COLS)
        return self.con.execute(
            f"""WITH big AS (SELECT wallet, asset FROM trades WHERE side = 'BUY'
                             GROUP BY wallet, asset HAVING sum(price * size) >= ?)
                SELECT {cols} FROM trades t JOIN big USING (wallet, asset) WHERE t.side = 'BUY' ORDER BY t.ts""",
            [min_usdc]).df()

    def markets_by_id(self, ids: Iterable[str] | None = None) -> dict[str, Market]:
        sql = f"SELECT {', '.join(MARKET_COLS)} FROM markets"
        params: list[Any] = []
        if ids is not None:
            wanted = list(ids)
            if not wanted:
                return {}
            sql += " WHERE condition_id IN (SELECT unnest(?))"
            params.append(wanted)
        out = {}
        for r in self.con.execute(sql, params).fetchall():
            d = dict(zip(MARKET_COLS, r, strict=True))
            d["outcome_prices"] = tuple(float(x) for x in _as_list(d["outcome_prices"]))
            d["token_ids"] = tuple(_as_list(d["token_ids"]))
            d["tags"] = tuple(_as_list(d["tags"]))
            d["end_ts"] = _opt_int(d["end_ts"])
            d["closed_ts"] = _opt_int(d["closed_ts"])
            out[d["condition_id"]] = Market(**d)
        return out

    def replace_scores(self, df: pd.DataFrame) -> None:
        self.con.execute("DELETE FROM wallet_scores")
        self._upsert_frame("wallet_scores", df.reindex(columns=SCORE_COLS), ["wallet", "category"])

    def scores_frame(self) -> pd.DataFrame:
        return self.con.execute(f"SELECT {', '.join(SCORE_COLS)} FROM wallet_scores").df()

    def upsert_profiles(self, profiles: Iterable[WalletProfile]) -> None:
        rows = [(p.wallet, p.created_ts, p.markets_traded, p.fetched_at) for p in profiles]
        if rows:
            self.con.executemany("INSERT OR REPLACE INTO wallet_profiles VALUES (?, ?, ?, ?)", rows)

    def profiles(self, wallets: Iterable[str]) -> dict[str, WalletProfile]:
        ids = list(wallets)
        if not ids:
            return {}
        rows = self.con.execute("SELECT * FROM wallet_profiles WHERE wallet IN (SELECT unnest(?))", [ids]).fetchall()
        return {w: WalletProfile(w, _opt_int(c), _opt_int(m), int(f)) for w, c, m, f in rows}

    def stale_profiles(self, wallets: Iterable[str], *, now: int, ttl_s: int, unknown_ttl_s: int | None = None) -> set[str]:
        """Wallets whose profile is missing or expired. Unknown profiles (no creation time) expire sooner, since
        a brand-new account's profile may simply not exist yet — but not on every run."""
        ids = set(wallets)
        unknown_ttl = ttl_s if unknown_ttl_s is None else unknown_ttl_s
        fresh = {w for w, p in self.profiles(ids).items()
                 if p.fetched_at >= now - (ttl_s if p.created_ts is not None else unknown_ttl)}
        return ids - fresh

    def get_meta(self, key: str) -> str | None:
        row = self.con.execute("SELECT value FROM meta WHERE key = ?", [key]).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.con.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", [key, value])
