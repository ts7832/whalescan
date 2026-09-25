import json
from dataclasses import replace

import pandas as pd

from whalescan.api.data_api import TradePage
from whalescan.batch import Apis
from whalescan.config import PathsCfg, load_config
from whalescan.models import BookSnapshot, Market, Trade
from whalescan.scoring import SCORE_COLUMNS
from whalescan.sweep import run_sweep

NOW = 1_790_000_000
H, D = 3600, 86400
MARKET = Market("0xc", "Will X resign?", "x-resign", "x-resign", NOW + 30 * D, False, None, (0.4, 0.6), ("yes", "no"),
                False, 0.0, 1.0, 5e6, ("Politics",))


def config(tmp):
    return replace(load_config(), paths=PathsCfg(research_db=str(tmp / "research.duckdb"),
                                                 scores_parquet=str(tmp / "scores.parquet"),
                                                 snapshot_dir=str(tmp / "snapshot"), ledger_dir=str(tmp / "ledger")))


def fill(tx, ts, wallet="0xfresh", usdc=4_000.0, price=0.40):
    return Trade(tx, ts, wallet, "yes", "0xc", "BUY", price, usdc / price, "x-resign", "Will X resign?", "Yes", 0, None)


class FakeData:
    skipped = 0

    def __init__(self, fills):
        self.fills, self.calls = fills, []

    async def trades(self, *, user=None, min_usdc=None, since_ts=None):
        self.calls.append((min_usdc, since_ts))
        return TradePage([t for t in self.fills if since_ts is None or t.ts >= since_ts], True)

    async def markets_traded(self, wallet):
        return 1


class FakeGamma:
    skipped = 0

    async def markets(self, ids):
        return {i: MARKET for i in ids if i == "0xc"}

    async def created_ts(self, wallet):
        return NOW - D if wallet.startswith("0xfresh") else NOW - 900 * D


class FakeClob:
    skipped = 0

    async def book(self, token_id):
        return BookSnapshot(token_id, NOW * 1000, ((0.39, 5000.0),), ((0.41, 1e6),))

    async def price_history(self, token_id, *, interval="1w", fidelity=60):
        return []


def daily_snapshot(tmp):
    snap = tmp / "snapshot"
    snap.mkdir()
    (snap / "meta.json").write_text(json.dumps({"generated_at": NOW - 5 * H, "counts": {"wallets_scanned": 3000},
                                                "params": {}, "errors": {"api": 0}}))
    (snap / "whales.json").write_text("[]")
    pd.DataFrame(columns=SCORE_COLUMNS).to_parquet(snap / "scores.parquet")
    return snap


async def test_split_insider_bet_is_caught_by_the_sweep(tmp_path):
    snap = daily_snapshot(tmp_path)
    fills = [fill(f"0x{i}", NOW - 1800 + i * 30, usdc=4_000.0) for i in range(4)]   # $16k in four $4k fills
    data = FakeData(fills)
    report = await run_sweep(config(tmp_path), apis=Apis(data, FakeGamma(), FakeClob()), now=NOW)
    assert data.calls[0] == (1000, NOW - 24 * H)             # first run: every >= $1k fill of the last 24 h
    signals = json.loads((snap / "signals.json").read_text())
    assert [(s["kind"], s["status"]) for s in signals] == [("INSIDER", "INSIDER")] and signals[0]["n_fills"] == 4
    meta = json.loads((snap / "meta.json").read_text())
    assert meta["counts"]["wallets_scanned"] == 3000 and meta["counts"]["insiders"] == 1  # daily numbers kept
    assert meta["sweep_at"] == NOW and meta["generated_at"] == NOW
    assert report.insiders == 1


async def test_next_sweep_only_asks_for_new_fills_and_forgets_old_ones(tmp_path):
    daily_snapshot(tmp_path)
    data = FakeData([fill("0xa", NOW - 600)])
    apis = Apis(data, FakeGamma(), FakeClob())
    await run_sweep(config(tmp_path), apis=apis, now=NOW)
    await run_sweep(config(tmp_path), apis=apis, now=NOW + 15 * 60)
    assert data.calls[1] == (1000, NOW - 600 - 300)          # since the newest stored fill, 5 min overlap
    data.fills = []
    await run_sweep(config(tmp_path), apis=apis, now=NOW + 2 * D)
    from whalescan.store import Store
    with Store(tmp_path / "sweep.duckdb") as s:
        assert s.trades_frame().empty                          # older than the 24 h window: pruned


async def test_sweep_works_before_any_daily_run(tmp_path):
    report = await run_sweep(config(tmp_path), apis=Apis(FakeData([]), FakeGamma(), FakeClob()), now=NOW)
    assert report.signals == 0
    assert json.loads((tmp_path / "snapshot" / "meta.json").read_text())["sweep_at"] == NOW
