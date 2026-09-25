from dataclasses import replace

import pytest

from whalescan.api.http import ApiError
from whalescan.batch import Apis, evaluate_window
from whalescan.classify import Blocklist
from whalescan.config import PathsCfg, load_config
from whalescan.gate import ScoreBook
from whalescan.ledger import Ledger
from whalescan.ledger_runner import process_marks, record_calls
from whalescan.models import BookSnapshot, Market, Trade
from whalescan.scoring import SCORE_COLUMNS
from whalescan.store import Store

import pandas as pd

NOW = 1_790_000_000
DAY = 86400

OPEN_MARKET = Market("0xc", "Will X happen?", "x-happen", "x-happen", NOW + 30 * DAY, False, None, (0.4, 0.6),
                     ("yes", "no"), True, 0.05, 1.0, 5e6, ("Politics",))


class FakeGamma:
    skipped = 0

    def __init__(self, markets=None):
        self.m = markets if markets is not None else {"0xc": OPEN_MARKET}

    async def markets(self, ids):
        return {i: self.m[i] for i in ids if i in self.m}

    async def created_ts(self, wallet):
        return NOW - DAY if wallet.startswith("0xfresh") else None


class FakeData:
    skipped = 0

    async def markets_traded(self, wallet):
        return 3


DEFAULT_BOOK = BookSnapshot("yes", NOW * 1000, ((0.39, 5000.0),), ((0.41, 1e6),))
NO_BOOK = object()  # sentinel: distinct from "book not given" (use DEFAULT_BOOK) and from a real snapshot


class FakeClob:
    skipped = 0

    def __init__(self, book=DEFAULT_BOOK):
        self._book = book

    async def book(self, token_id):
        if self._book is NO_BOOK:
            raise ApiError("no book", status=500, body="")
        return self._book

    async def price_history(self, token_id, *, interval="1w", fidelity=60):
        return []


def config(tmp):
    base = load_config()
    return replace(base, paths=PathsCfg(research_db=str(tmp / "r.duckdb"), scores_parquet=str(tmp / "s.parquet"),
                                        snapshot_dir=str(tmp / "snap"), ledger_dir=str(tmp / "ledger")))


async def window(cfg, apis, usdc=30_000.0, wallet="0xfresh1", price=0.40, tx="0x1"):
    with Store(cfg.path(cfg.paths.research_db)) as store:
        store.upsert_markets([OPEN_MARKET], now=NOW)
        store.upsert_trades([Trade(tx, NOW - 600, wallet, "yes", "0xc", "BUY", price, usdc / price, "x-happen",
                                   "q", "Yes", 0, None)])
        book = ScoreBook.for_config(pd.DataFrame(columns=SCORE_COLUMNS), cfg)
        return await evaluate_window(apis, store, cfg, book, Blocklist(cfg.blocklist), NOW, NOW - 3600)


async def test_a_new_insider_alert_is_logged_with_a_book_entry(tmp_path):
    cfg = config(tmp_path)
    apis = Apis(FakeData(), FakeGamma(), FakeClob())
    result = await window(cfg, apis)
    led = Ledger(tmp_path / "ledger")
    logged = await record_calls(led, result, apis, cfg, NOW)
    assert logged == 1
    [call] = led.calls()
    assert call["kind"] == "INSIDER" and call["wallet"] == "0xfresh1"
    assert call["entry_vwap"] == pytest.approx(0.41) and call["entry_estimated"] is False
    assert call["schedule"][0] == {"day": 7, "due_ts": NOW + 7 * DAY}


async def test_estimated_entry_when_the_book_cannot_be_read(tmp_path):
    cfg = config(tmp_path)
    apis = Apis(FakeData(), FakeGamma(), FakeClob(book=NO_BOOK))
    result = await window(cfg, apis)
    led = Ledger(tmp_path / "ledger")
    await record_calls(led, result, apis, cfg, NOW)
    [call] = led.calls()
    assert call["entry_estimated"] is True
    assert call["entry_vwap"] == 0.40 + cfg.validation.slippage


async def test_a_near_miss_is_logged_and_later_upgrades_to_a_separate_alert_call(tmp_path):
    cfg = config(tmp_path)
    apis = Apis(FakeData(), FakeGamma(), FakeClob())
    led = Ledger(tmp_path / "ledger")

    # first: a $7,500 fresh-wallet bet -> near miss on I4 (below insider min_usdc, above the near-miss floor)
    r1 = await window(cfg, apis, usdc=7_500.0, tx="0x1")
    await record_calls(led, r1, apis, cfg, NOW)
    kinds = [c["kind"] for c in led.calls()]
    assert kinds == ["NEAR_MISS"]
    assert led.calls()[0]["missed_rule"] == "I4"

    # later: more fills push the same wallet's position past $10k -> a genuine INSIDER alert
    with Store(cfg.path(cfg.paths.research_db)) as store:
        store.upsert_trades([Trade("0x2", NOW - 500, "0xfresh1", "yes", "0xc", "BUY", 0.40, 6_250.0, "x-happen",
                                   "q", "Yes", 0, None)])
        book = ScoreBook.for_config(pd.DataFrame(columns=SCORE_COLUMNS), cfg)
        r2 = await evaluate_window(apis, store, cfg, book, Blocklist(cfg.blocklist), NOW, NOW - 3600)
    logged = await record_calls(led, r2, apis, cfg, NOW)
    assert logged == 1
    kinds = sorted(c["kind"] for c in led.calls())
    assert kinds == ["INSIDER", "NEAR_MISS"]  # two distinct calls, the near-miss record untouched


async def test_recording_the_same_call_twice_does_not_duplicate(tmp_path):
    cfg = config(tmp_path)
    apis = Apis(FakeData(), FakeGamma(), FakeClob())
    result = await window(cfg, apis)
    led = Ledger(tmp_path / "ledger")
    await record_calls(led, result, apis, cfg, NOW)
    logged_again = await record_calls(led, result, apis, cfg, NOW)
    assert logged_again == 0 and len(led.calls()) == 1


async def test_due_checkpoint_is_recorded_with_the_current_best_bid(tmp_path):
    cfg = config(tmp_path)
    led = Ledger(tmp_path / "ledger")
    apis = Apis(FakeData(), FakeGamma(), FakeClob())
    result = await window(cfg, apis)
    await record_calls(led, result, apis, cfg, NOW)

    later_book = BookSnapshot("yes", (NOW + 7 * DAY) * 1000, ((0.55, 5000.0),), ((0.57, 1e6),))
    apis2 = Apis(FakeData(), FakeGamma(), FakeClob(book=later_book))
    processed = await process_marks(led, apis2, cfg, now=NOW + 7 * DAY + 60)
    assert processed == 1
    [mark] = led.marks()
    assert mark["type"] == "CHECKPOINT" and mark["day"] == 7 and mark["best_bid"] == 0.55
    assert mark["return_pct"] is not None


async def test_settlement_closes_the_call_with_exactly_one_mark_and_no_late_checkpoint(tmp_path):
    cfg = config(tmp_path)
    led = Ledger(tmp_path / "ledger")
    apis = Apis(FakeData(), FakeGamma(), FakeClob())
    result = await window(cfg, apis)
    await record_calls(led, result, apis, cfg, NOW)

    settled = replace(OPEN_MARKET, closed=True, closed_ts=NOW + 40 * DAY, outcome_prices=(1.0, 0.0))
    apis2 = Apis(FakeData(), FakeGamma({"0xc": settled}), FakeClob())
    processed = await process_marks(led, apis2, cfg, now=NOW + 40 * DAY)
    assert processed == 1
    marks = led.marks()
    assert [m["type"] for m in marks] == ["SETTLEMENT"]
    assert marks[0]["payout"] == 1.0 and marks[0]["irregular"] is False
    assert marks[0]["return_pct"] > 0
    assert led.open_calls() == []
    # a checkpoint that would otherwise have been due at this point must not also fire
    assert await process_marks(led, apis2, cfg, now=NOW + 100 * DAY) == 0


async def test_irregular_settlement_is_flagged(tmp_path):
    cfg = config(tmp_path)
    led = Ledger(tmp_path / "ledger")
    apis = Apis(FakeData(), FakeGamma(), FakeClob())
    result = await window(cfg, apis)
    await record_calls(led, result, apis, cfg, NOW)

    voided = replace(OPEN_MARKET, closed=True, closed_ts=NOW + 40 * DAY, outcome_prices=(0.5, 0.5))
    apis2 = Apis(FakeData(), FakeGamma({"0xc": voided}), FakeClob())
    await process_marks(led, apis2, cfg, now=NOW + 40 * DAY)
    assert led.marks()[0]["irregular"] is True


async def test_missing_book_at_a_checkpoint_retries_then_gives_up_after_24h(tmp_path):
    cfg = config(tmp_path)
    led = Ledger(tmp_path / "ledger")
    apis = Apis(FakeData(), FakeGamma(), FakeClob())
    result = await window(cfg, apis)
    await record_calls(led, result, apis, cfg, NOW)

    apis_no_book = Apis(FakeData(), FakeGamma(), FakeClob(book=NO_BOOK))
    assert await process_marks(led, apis_no_book, cfg, now=NOW + 7 * DAY + 60) == 0
    assert led.marks() == []
    processed = await process_marks(led, apis_no_book, cfg, now=NOW + 7 * DAY + 25 * 3600)
    assert processed == 1
    assert led.marks()[0]["missing"] is True and led.marks()[0]["return_pct"] is None
