"""The evidence study's own DuckDB (evidence-study spec §2): a `Store` (fills, markets, profiles — same schema and
tested upserts as the scanner) plus what a historical rebuild needs to be incremental and look-ahead free."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from whalescan.store import Store

OPEN_REFRESH_S = 86400  # open markets keep trading: re-read their fills daily (for bet counts)

HISTORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS h_market_state (
  condition_id VARCHAR PRIMARY KEY, is_open BOOLEAN, fills_fetched_at BIGINT, fills_complete BOOLEAN);
ALTER TABLE h_market_state ADD COLUMN IF NOT EXISTS is_news BOOLEAN DEFAULT TRUE;
CREATE TABLE IF NOT EXISTS h_wallet_history (wallet VARCHAR PRIMARY KEY, fetched_at BIGINT);
ALTER TABLE h_wallet_history ADD COLUMN IF NOT EXISTS complete BOOLEAN;
ALTER TABLE h_wallet_history ADD COLUMN IF NOT EXISTS truncated BOOLEAN;
CREATE TABLE IF NOT EXISTS h_prices (asset VARCHAR, ts BIGINT, price DOUBLE, PRIMARY KEY (asset, ts));
CREATE TABLE IF NOT EXISTS h_price_windows (asset VARCHAR, t0 BIGINT, PRIMARY KEY (asset, t0));
-- Insider Finder's own 30h/10min slot windows: a separate table (not a wider key on h_price_windows) so the
-- already-built evidence-study database never needs its primary key altered.
CREATE TABLE IF NOT EXISTS h_finder_windows (asset VARCHAR, slot_ts BIGINT, PRIMARY KEY (asset, slot_ts));
"""


class HistoryStore(Store):
    def __init__(self, db_path: Path, *, lock: bool = True) -> None:
        super().__init__(db_path, lock=lock)
        self.con.execute(HISTORY_SCHEMA)

    def register_markets(self, ids: Iterable[str], *, is_open: bool, is_news: bool = True) -> None:
        """Track markets in the study window. A market that was open and is now closed needs one more fill read
        (its last trades before resolution), so a change to closed resets its fetch state. `is_news` marks a
        market as belonging to the evidence study's narrower category set; it only ever turns TRUE, never back
        to FALSE, so a market later widened into via the Insider Finder's broader listing keeps its news status."""
        rows = [(i, is_open, is_news) for i in ids]
        if not rows:
            return
        self.con.executemany(
            """INSERT INTO h_market_state (condition_id, is_open, is_news, fills_fetched_at, fills_complete)
               VALUES (?, ?, ?, NULL, FALSE)
               ON CONFLICT (condition_id) DO UPDATE SET
                 fills_complete = CASE WHEN h_market_state.is_open AND NOT excluded.is_open
                                       THEN FALSE ELSE h_market_state.fills_complete END,
                 is_open = excluded.is_open,
                 is_news = h_market_state.is_news OR excluded.is_news""", rows)

    def markets_needing_fills(self, now: int) -> list[str]:
        rows = self.con.execute(
            """SELECT condition_id FROM h_market_state
               WHERE NOT fills_complete OR (is_open AND fills_fetched_at < ?)
               ORDER BY condition_id""", [now - OPEN_REFRESH_S]).fetchall()
        return [r[0] for r in rows]

    def study_market_ids(self, *, closed_only: bool = False) -> set[str]:
        """Markets registered for the evidence study's own (narrower, news-category) view — unaffected by the
        Insider Finder's wider listing."""
        sql = "SELECT condition_id FROM h_market_state WHERE is_news" + (" AND NOT is_open" if closed_only else "")
        return {r[0] for r in self.con.execute(sql).fetchall()}

    def covered_market_ids(self, *, closed_only: bool = False) -> set[str]:
        """Every market registered for any reason (news or Insider Finder coverage)."""
        sql = "SELECT condition_id FROM h_market_state" + (" WHERE NOT is_open" if closed_only else "")
        return {r[0] for r in self.con.execute(sql).fetchall()}

    def mark_fills(self, condition_id: str, now: int, *, complete: bool) -> None:
        self.con.execute("UPDATE h_market_state SET fills_fetched_at = ?, fills_complete = ? WHERE condition_id = ?",
                         [now, complete, condition_id])

    def mark_wallet_history(self, wallet: str, now: int, *, complete: bool = True, truncated: bool = False) -> None:
        """Record a wallet's history fetch. Only a COMPLETE history can count markets traded; one cut short by the
        API's depth cap is recorded as truncated (it will never improve, so it is not refetched)."""
        self.con.execute("""INSERT OR REPLACE INTO h_wallet_history (wallet, fetched_at, complete, truncated)
                            VALUES (?, ?, ?, ?)""", [wallet, now, complete, truncated])

    def wallets_with_history(self) -> set[str]:
        """Wallets whose full trade history is stored (rows from before completeness was tracked do not count)."""
        return {r[0] for r in self.con.execute("SELECT wallet FROM h_wallet_history WHERE complete").fetchall()}

    def wallets_history_done(self) -> set[str]:
        """Wallets not worth fetching again: complete, or truncated at the API's depth cap."""
        return {r[0] for r in self.con.execute(
            "SELECT wallet FROM h_wallet_history WHERE complete OR truncated").fetchall()}

    def markets_traded_before(self, wallet: str, ts: int) -> int | None:
        """Distinct markets the wallet traded strictly before `ts` — None unless its full history was fetched
        (fills gathered per market only cover the study's markets, so they would under-count)."""
        if wallet not in self.wallets_with_history():
            return None
        return int(self.con.execute(
            "SELECT count(DISTINCT condition_id) FROM trades WHERE wallet = ? AND ts < ?", [wallet, ts]).fetchone()[0])

    def upsert_prices(self, asset: str, points: Iterable[tuple[int, float]]) -> None:
        rows = [(asset, int(t), float(p)) for t, p in points]
        if rows:
            self.con.executemany("INSERT OR REPLACE INTO h_prices VALUES (?, ?, ?)", rows)

    def price_after(self, asset: str, ts: int, *, max_wait_s: int) -> float | None:
        """First recorded price at or after `ts`, if one came within `max_wait_s` — never interpolated."""
        row = self.con.execute(
            "SELECT price FROM h_prices WHERE asset = ? AND ts >= ? AND ts <= ? ORDER BY ts LIMIT 1",
            [asset, ts, ts + max_wait_s]).fetchone()
        return None if row is None else float(row[0])

    def mark_price_window(self, asset: str, t0: int) -> None:
        self.con.execute("INSERT OR REPLACE INTO h_price_windows VALUES (?, ?)", [asset, t0])

    def has_price_window(self, asset: str, t0: int) -> bool:
        return self.con.execute("SELECT 1 FROM h_price_windows WHERE asset = ? AND t0 = ?",
                                [asset, t0]).fetchone() is not None

    def mark_finder_window(self, asset: str, slot_ts: int) -> None:
        self.con.execute("INSERT OR REPLACE INTO h_finder_windows VALUES (?, ?)", [asset, slot_ts])

    def has_finder_window(self, asset: str, slot_ts: int) -> bool:
        return self.con.execute("SELECT 1 FROM h_finder_windows WHERE asset = ? AND slot_ts = ?",
                                [asset, slot_ts]).fetchone() is not None
