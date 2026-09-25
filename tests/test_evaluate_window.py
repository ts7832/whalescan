from dataclasses import replace

import pandas as pd

from whalescan.batch import Apis, evaluate_window
from whalescan.classify import Blocklist
from whalescan.config import PathsCfg, load_config
from whalescan.gate import ScoreBook
from whalescan.models import BookSnapshot, Market, Trade
from whalescan.scoring import SCORE_COLUMNS
from whalescan.store import Store

NOW = 1_790_000_000
DAY = 86400

MARKET = Market("0xc", "Will X happen?", "x-happen", "x-happen", NOW + 30 * DAY, False, None, (0.4, 0.6),
                ("yes", "no"), False, 0.0, 1.0, 5e6, ("Politics",))


class FakeGamma:
    skipped = 0

    async def markets(self, ids):
        return {i: MARKET for i in ids if i == "0xc"}

    async def created_ts(self, wallet):
        return NOW - DAY if wallet.startswith("0xfresh") else None


class FakeData:
    skipped = 0

    async def markets_traded(self, wallet):
        return 1


class FakeClob:
    skipped = 0

    async def book(self, token_id):
        return BookSnapshot(token_id, NOW * 1000, ((0.39, 5000.0),), ((0.41, 1e6),))

    async def price_history(self, token_id, *, interval="1w", fidelity=60):
        return []


def config(tmp):
    base = load_config()
    return replace(base, paths=PathsCfg(research_db=str(tmp / "r.duckdb"), scores_parquet=str(tmp / "s.parquet"),
                                        snapshot_dir=str(tmp / "snap"), ledger_dir=str(tmp / "ledger")))


async def test_a_sub_threshold_fresh_wallet_bet_is_evaluated_and_profiled(tmp_path):
    cfg = config(tmp_path)
    apis = Apis(FakeData(), FakeGamma(), FakeClob())
    with Store(cfg.path(cfg.paths.research_db)) as store:
        store.upsert_markets([MARKET], now=NOW)
        # $7,500: below insider min_usdc (10,000) but within the near-miss band (>= 5,000)
        store.upsert_trades([Trade("0x1", NOW - 600, "0xfresh1", "yes", "0xc", "BUY", 0.40, 18_750.0, "x-happen",
                                   "q", "Yes", 0, None)])
        book = ScoreBook.for_config(pd.DataFrame(columns=SCORE_COLUMNS), cfg)
        result = await evaluate_window(apis, store, cfg, book, Blocklist(cfg.blocklist), NOW, NOW - 3600)

    assert result.signals == [] and result.contacts != []
    assert "0xc" in result.markets
    assert "0xfresh1" in result.profiles
    assert result.profiles["0xfresh1"].markets_traded == 1
    [ev] = result.evaluations
    assert ev.checks[0].code == "I1"  # went through the insider check ladder, not the plain sniper gate
