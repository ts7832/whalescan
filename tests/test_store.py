import subprocess
import sys

import pandas as pd
import pytest

from whalescan.models import ClosedPosition, Market, Trade
from whalescan.store import LockedError, Store, trades_from_frame


def trade(tx="0x1", wallet="0xw", ts=100, side="BUY", fee=None) -> Trade:
    return Trade(tx, ts, wallet, "a1", "0xc1", side, 0.4, 100.0, "ev", "title", "Yes", 0, fee)


def position(wallet="0xw", asset="a1", cid="0xc1", ts=100, outcome_index=0) -> ClosedPosition:
    return ClosedPosition(wallet, asset, cid, 0.4, 100.0, 60.0, 1.0, "Yes", outcome_index, "t", "ev", ts)


def market(cid="0xc1", closed=True, prices=(1.0, 0.0)) -> Market:
    return Market(cid, "Q?", "slug", "ev", 2000, closed, 1500 if closed else None, prices, ("a1", "a2"),
                  True, 0.05, 1.0, 50000.0, ("Politics",))


def test_upserts_are_idempotent_and_dedupe_within_batch(tmp_path):
    with Store(tmp_path / "db.duckdb") as s:
        assert s.upsert_trades([trade(), trade()]) == 1
        s.upsert_trades([trade(), trade(tx="0x2")])
        assert len(s.trades_frame()) == 2


def test_trades_roundtrip_including_null_fee(tmp_path):
    with Store(tmp_path / "db.duckdb") as s:
        s.upsert_trades([trade(fee=None), trade(tx="0x2", fee=0.5)])
        back = sorted(trades_from_frame(s.trades_frame()), key=lambda t: t.tx_hash)
        assert back[0] == trade(fee=None)
        assert back[1].fee == 0.5


def test_trades_frame_filters(tmp_path):
    with Store(tmp_path / "db.duckdb") as s:
        s.upsert_trades([trade(tx="0x1", ts=10), trade(tx="0x2", ts=20, wallet="0xz")])
        assert len(s.trades_frame(since_ts=15)) == 1
        assert len(s.trades_frame(until_ts=15)) == 1
        assert len(s.trades_frame(wallets=["0xz"])) == 1
        assert len(s.trades_frame(wallets=[])) == 0


def test_markets_roundtrip(tmp_path):
    with Store(tmp_path / "db.duckdb") as s:
        s.upsert_markets([market()], now=1)
        assert s.markets_by_id(["0xc1"])["0xc1"] == market()
        assert s.markets_by_id() == {"0xc1": market()}
        assert s.markets_by_id([]) == {}


def test_positions_frame_resolution_and_completeness(tmp_path):
    with Store(tmp_path / "db.duckdb") as s:
        s.upsert_positions([position(cid="0xc1"), position(asset="a3", cid="0xc2"), position(asset="a4", cid="0xc3"),
                            position(asset="a5", cid="0xmissing")])
        s.upsert_markets([market("0xc1"), market("0xc2", prices=(0.5, 0.5)), market("0xc3", closed=False)], now=1)
        s.record_wallet_fetch("0xw", fetched_at=5, complete=True, source="leaderboard")
        df = s.positions_frame().set_index("condition_id")
        assert df.loc["0xc1", "winner_index"] == 0
        assert pd.isna(df.loc["0xc2", "winner_index"])        # fractional
        assert pd.isna(df.loc["0xc3", "winner_index"])        # still open
        assert pd.isna(df.loc["0xmissing", "winner_index"])   # unknown market
        assert bool(df.loc["0xc1", "complete"]) is True
        assert list(df.loc["0xc1", "tags"]) == ["Politics"]


def test_wallet_state_tracks_max_ts_and_names(tmp_path):
    with Store(tmp_path / "db.duckdb") as s:
        s.upsert_positions([position(ts=100), position(asset="a2", ts=300)])
        s.record_wallet_fetch("0xw", fetched_at=9, complete=False, source="trades")
        s.set_wallet_names({"0xw": "Whale"})
        st = s.wallet_state()["0xw"]
        assert (st.fetched_at, st.complete, st.max_ts, st.name) == (9, False, 300, "Whale")


def test_condition_ids_needing_refresh(tmp_path):
    with Store(tmp_path / "db.duckdb") as s:
        s.upsert_positions([position(cid="0xc1"), position(asset="a2", cid="0xnew")])
        s.upsert_trades([trade()])
        s.upsert_markets([market("0xc1", closed=False)], now=100)
        assert s.condition_ids_needing_refresh(now=150, open_max_age_s=3600) == {"0xnew"}
        assert s.condition_ids_needing_refresh(now=10_000, open_max_age_s=3600) == {"0xnew", "0xc1"}


def test_scores_and_meta(tmp_path):
    with Store(tmp_path / "db.duckdb") as s:
        df = pd.DataFrame([{"wallet": "0xw", "category": "ALL", "n": 3, "n_eff": 2.5, "edge": 0.1, "sigma": 0.05,
                            "post_edge": 0.08, "p_value": 0.01, "bh_pass": True, "certified": True, "flags": "",
                            "median_stake": 10.0, "as_of": 1}])
        s.replace_scores(df)
        s.replace_scores(df)
        assert len(s.scores_frame()) == 1
        s.set_meta("k", "v")
        assert s.get_meta("k") == "v" and s.get_meta("nope") is None


def test_second_writer_gets_a_clear_lock_error(tmp_path):
    with Store(tmp_path / "db.duckdb"):
        with pytest.raises(LockedError, match="another whalescan process"):
            Store(tmp_path / "db.duckdb")


def test_lock_is_released_when_holder_is_sigkilled(tmp_path):
    # flock locks belong to the process, not the file: a SIGKILL'd holder leaves db.lock on disk
    # but the OS releases the lock, so the next run must start normally.
    lock_path = tmp_path / "db.lock"
    code = (f"import time; from pathlib import Path; from whalescan.store import ProcessLock; "
            f"ProcessLock(Path({str(lock_path)!r})).acquire(); print('held', flush=True); time.sleep(60)")
    holder = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        with pytest.raises(LockedError):
            Store(tmp_path / "db.duckdb")
    finally:
        holder.kill()
        holder.wait()
    assert lock_path.exists()
    with Store(tmp_path / "db.duckdb"):
        pass
