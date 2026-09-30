import json
from dataclasses import replace

import pandas as pd

from whalescan import cli
from whalescan.api.data_api import TradePage
from whalescan.batch import Apis
from whalescan.config import PathsCfg, load_config
from whalescan.ledger import Ledger
from whalescan.models import BookSnapshot, Market, Trade
from whalescan.sweep import SweepReport, run_sweep

NOW = 1_790_000_000
DAY = 86400

MARKET = Market("0xc", "Will X happen?", "x-happen", "x-happen", NOW + 30 * DAY, False, None, (0.4, 0.6),
                ("yes", "no"), True, 0.05, 1.0, 5e6, ("Politics",))


def config(tmp):
    base = load_config()
    return replace(base, paths=PathsCfg(research_db=str(tmp / "r.duckdb"), scores_parquet=str(tmp / "s.parquet"),
                                        snapshot_dir=str(tmp / "snap"), ledger_dir=str(tmp / "ledger"),
                                        history_db=str(tmp / "history.duckdb"), archive_dir=str(tmp / "archive")))


class FakeData:
    skipped = 0

    def __init__(self, trades):
        self.rows = trades

    async def trades(self, *, user=None, min_usdc=None, since_ts=None):
        rows = [t for t in self.rows if (since_ts is None or t.ts >= since_ts)
               and (min_usdc is None or t.usdc >= min_usdc)]
        return TradePage(rows, True)

    async def markets_traded(self, wallet):
        return 1


class FakeGamma:
    skipped = 0

    def __init__(self, extra=()):
        self.markets_by_id = {"0xc": MARKET, **dict(extra)}

    async def markets(self, ids):
        return {i: self.markets_by_id[i] for i in ids if i in self.markets_by_id}

    async def created_ts(self, wallet):
        return NOW - DAY


class FakeClob:
    skipped = 0

    async def book(self, token_id):
        return BookSnapshot(token_id, NOW * 1000, ((0.39, 5000.0),), ((0.41, 1e6),))

    async def price_history(self, token_id, *, interval="1w", fidelity=60):
        return []


def fresh_bet(usdc=30_000.0):
    return [Trade("0x1", NOW - 600, "0xfresh1", "yes", "0xc", "BUY", 0.40, usdc / 0.40, "x-happen", "q", "Yes",
                  0, None)]


async def test_a_sweep_round_logs_a_new_call_and_writes_a_summary(tmp_path):
    cfg = config(tmp_path)
    apis = Apis(FakeData(fresh_bet()), FakeGamma(), FakeClob())
    await run_sweep(cfg, apis=apis, now=NOW)

    led = Ledger(tmp_path / "ledger")
    calls = led.calls()
    assert len(calls) == 1 and calls[0]["kind"] == "INSIDER"

    summary = json.loads((tmp_path / "ledger" / "summary.json").read_text())
    assert summary["totals"]["calls"] == 1


async def test_a_later_sweep_round_does_not_relog_the_same_call(tmp_path):
    cfg = config(tmp_path)
    apis = Apis(FakeData(fresh_bet()), FakeGamma(), FakeClob())
    await run_sweep(cfg, apis=apis, now=NOW)
    await run_sweep(cfg, apis=apis, now=NOW + 60)
    assert len(Ledger(tmp_path / "ledger").calls()) == 1


async def test_a_sweep_round_advances_checkpoints_for_already_open_calls(tmp_path):
    cfg = config(tmp_path)
    apis = Apis(FakeData(fresh_bet()), FakeGamma(), FakeClob())
    await run_sweep(cfg, apis=apis, now=NOW)

    class LaterClob(FakeClob):
        async def book(self, token_id):
            return BookSnapshot(token_id, (NOW + 7 * DAY) * 1000, ((0.55, 5000.0),), ((0.57, 1e6),))

    later_apis = Apis(FakeData([]), FakeGamma(), LaterClob())
    await run_sweep(cfg, apis=later_apis, now=NOW + 7 * DAY + 120)
    marks = Ledger(tmp_path / "ledger").marks()
    assert [m["day"] for m in marks] == [7]


def test_cli_ledger_command_runs_a_sweep_round_and_reports_ledger_activity(monkeypatch, capsys):
    async def fake_sweep(cfg, **kwargs):
        return SweepReport(fills=3, signals=1, insiders=1, contacts=2)

    async def fake_ledger_status(cfg):
        return {"calls": 5, "open": 2, "settled": 3}

    monkeypatch.setattr(cli, "run_sweep", fake_sweep)
    monkeypatch.setattr(cli, "ledger_status", fake_ledger_status)
    assert cli.main(["ledger"]) == 0
    out = capsys.readouterr().out
    assert "5 calls" in out and "2 open" in out and "3 settled" in out


async def test_a_ledger_error_never_blocks_alert_publishing(tmp_path, caplog):
    import logging

    cfg = config(tmp_path)
    closed_market = replace(MARKET, condition_id="0xbad", closed=True, closed_ts=NOW - 40 * DAY,
                            outcome_prices=(1.0, 0.0))
    apis = Apis(FakeData(fresh_bet()), FakeGamma({"0xbad": closed_market}), FakeClob())
    (tmp_path / "ledger").mkdir()
    # a hand-corrupted call whose stored outcome_index doesn't fit its (now closed) market -> would raise
    # with an unguarded IndexError inside process_marks' settlement path
    (tmp_path / "ledger" / "calls.jsonl").write_text(
        '{"id":"broken","kind":"INSIDER","condition_id":"0xbad","outcome_index":99,"entry_cost":0.4,'
        '"call_ts":1,"schedule":[],"wallet":"0xw","asset":"yes","question":"q","category":"POLITICS",'
        '"tier":"B","usdc":1,"missed_rule":null}\n')
    with caplog.at_level(logging.ERROR):
        report = await run_sweep(cfg, apis=apis, now=NOW)
    assert json.loads((tmp_path / "snap" / "signals.json").read_text())
    assert report.signals == 1
    assert "ledger" in caplog.text.lower()


async def test_each_sweep_round_archives_only_fills_it_has_not_seen(tmp_path):
    from whalescan.archive import read_archive

    cfg = config(tmp_path)
    first = fresh_bet()
    await run_sweep(cfg, apis=Apis(FakeData(first), FakeGamma(), FakeClob()), now=NOW)
    later = Trade("0x2", NOW + 60, "0xother", "yes", "0xc", "BUY", 0.40, 5000.0, "x-happen", "q", "Yes", 0, None)
    await run_sweep(cfg, apis=Apis(FakeData(first + [later]), FakeGamma(), FakeClob()), now=NOW + 900)
    assert [t.tx_hash for t in read_archive(tmp_path / "archive")] == ["0x1", "0x2"]  # the re-read 0x1 isn't repeated


async def test_an_archive_error_never_blocks_alert_publishing(tmp_path, monkeypatch, caplog):
    import logging

    from whalescan import sweep

    def broken(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(sweep, "archive_round", broken)
    cfg = config(tmp_path)
    with caplog.at_level(logging.ERROR):
        report = await run_sweep(cfg, apis=Apis(FakeData(fresh_bet()), FakeGamma(), FakeClob()), now=NOW)
    assert report.signals == 1 and json.loads((tmp_path / "snap" / "signals.json").read_text())
    assert "archive" in caplog.text.lower()


async def test_a_confirmed_wallets_new_bet_produces_a_badged_alert_and_a_confirmed_ledger_call(tmp_path):
    cfg = config(tmp_path)
    ledger_dir = tmp_path / "ledger"
    ledger_dir.mkdir(parents=True)
    led = Ledger(ledger_dir)
    # a proven insider: an earlier call that already settled as a clean win, strictly before `now`
    led.append_call({"id": "old:INSIDER", "kind": "INSIDER", "wallet": "0xproven", "outcome_index": 0,
                     "call_ts": NOW - 20 * DAY, "condition_id": "0xold", "asset": "old-y", "question": "old?",
                     "category": "POLITICS", "tier": "B", "entry_cost": 0.4, "missed_rule": None})
    led.append_mark({"call_id": "old:INSIDER", "type": "SETTLEMENT", "at": NOW - 10 * DAY, "return_pct": 0.5,
                     "irregular": False})

    # 0xproven is now an OLD, high-market-count account — it would never pass the ordinary insider checks —
    # but its confirmed status must still raise a badged alert on its next big buy.
    bet = [Trade("0x1", NOW - 600, "0xproven", "yes", "0xc", "BUY", 0.40, 30_000.0 / 0.40, "x-happen", "q", "Yes",
                 0, None)]

    class OldGamma(FakeGamma):
        async def created_ts(self, wallet):
            return NOW - 900 * DAY

    class OldData(FakeData):
        async def markets_traded(self, wallet):
            return 500

    apis = Apis(OldData(bet), OldGamma(), FakeClob())
    await run_sweep(cfg, apis=apis, now=NOW)

    signals = json.loads((tmp_path / "snap" / "signals.json").read_text())
    confirmed_alerts = [s for s in signals if s["kind"] == "CONFIRMED"]
    assert len(confirmed_alerts) == 1 and confirmed_alerts[0]["wallet"] == "0xproven"

    calls = Ledger(ledger_dir).calls()
    assert any(c["kind"] == "CONFIRMED" and c["wallet"] == "0xproven" for c in calls)
