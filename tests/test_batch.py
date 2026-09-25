import json
from dataclasses import replace

import numpy as np
import pytest

from whalescan import cli
from whalescan.api.data_api import PositionHistory, TradePage
from whalescan.api.http import BlockedError
from whalescan.batch import Apis, run_batch
from whalescan.config import PathsCfg, load_config
from whalescan.models import BookSnapshot, ClosedPosition, LeaderboardEntry, Market, PricePoint, Trade
from whalescan.snapshot import write_json_atomic
from whalescan.store import LockedError

NOW = 1_790_000_000
DAY = 86400


def config(tmp):
    base = load_config()
    base = replace(base, gate=replace(base.gate, skill_mode="certified"))
    return replace(base, paths=PathsCfg(research_db=str(tmp / "research.duckdb"),
                                                 scores_parquet=str(tmp / "scores.parquet"),
                                                 snapshot_dir=str(tmp / "snapshot"), ledger_dir=str(tmp / "ledger")))


class FakeData:
    def __init__(self, positions, trades, *, block=False, redeemable=None, truncated=()):
        self.positions, self.rows, self.block, self.skipped = positions, trades, block, 0
        self.redeemable = redeemable or {}
        self.truncated = set(truncated)

    async def redeemable_positions(self, wallet):
        return PositionHistory(self.redeemable.get(wallet, []), True)

    async def leaderboard(self, *, period, order_by, pages):
        if self.block:
            raise BlockedError("BLOCKED by bot protection at test")
        return [LeaderboardEntry(w, i + 1, 1e6, 1e5, f"name{i}") for i, w in enumerate(sorted(self.positions))]

    async def closed_positions(self, wallet, *, since_ts=None):
        rows = [p for p in self.positions.get(wallet, []) if since_ts is None or p.ts >= since_ts]
        return PositionHistory(rows, True, truncated=wallet in self.truncated and since_ts is None)

    async def markets_traded(self, wallet):
        return 1 if wallet.startswith("0xfresh") else 500

    async def trades(self, *, user=None, min_usdc=None, since_ts=None):
        rows = [t for t in self.rows if (user is None or t.wallet == user)
                and (since_ts is None or t.ts >= since_ts) and (min_usdc is None or t.usdc >= min_usdc)]
        return TradePage(rows, True)


class FakeGamma:
    def __init__(self, markets):
        self.m, self.skipped = markets, 0

    async def markets(self, ids):
        return {i: self.m[i] for i in ids if i in self.m}

    async def created_ts(self, wallet):
        return NOW - DAY if wallet.startswith("0xfresh") else NOW - 400 * DAY


class FakeClob:
    skipped = 0

    async def book(self, token_id):
        return BookSnapshot(token_id, NOW * 1000, ((0.39, 5000.0),), ((0.41, 100000.0),))

    async def price_history(self, token_id, *, interval="1w", fidelity=60):
        return [PricePoint(NOW - 3600, 0.39), PricePoint(NOW, 0.40)]


def resolved(cid, winner):
    return Market(cid, f"Q {cid}", f"slug-{cid}", f"ev-{cid}", NOW - 10 * DAY, True, NOW - 10 * DAY,
                  (1.0, 0.0) if winner == 0 else (0.0, 1.0), (f"{cid}-y", f"{cid}-n"), False, 0.0, 1.0, 1e6,
                  ("Politics",))


def world(skilled=True, seed=5):
    """30 slightly-bad wallets, optionally one strongly skilled whale, and one live $20k trade."""
    rng = np.random.default_rng(seed)
    positions, markets = {}, {}
    wallets = [f"0xnull{k}" for k in range(30)] + (["0xwhale"] if skilled else [])
    for w in wallets:
        n, skill = (200, 0.3) if w == "0xwhale" else (60, -0.05)
        rows = []
        for i in range(n):
            p = float(rng.uniform(0.2, 0.8))
            won = rng.uniform() < min(max(p + skill, 0.01), 0.99)
            cid = f"{w}-c{i}"
            markets[cid] = resolved(cid, 0 if won else 1)
            rows.append(ClosedPosition(w, f"{cid}-y", cid, p, 2000.0, 0.0, 1.0 if won else 0.0, "Yes", 0, f"T{i}",
                                       f"ev-{cid}", NOW - 20 * DAY + i))
        positions[w] = rows
    markets["0xlive"] = Market("0xlive", "Will it happen?", "will-it-happen", "it-happens", NOW + 3 * DAY, False,
                               None, (0.4, 0.6), ("live-yes", "live-no"), False, 0.0, 1.0, 5e6, ("Politics",))
    trader = "0xwhale" if skilled else "0xnull0"
    trades = [Trade("0xt1", NOW - 3600, trader, "live-yes", "0xlive", "BUY", 0.40, 50_000.0, "it-happens",
                    "Will it happen?", "Yes", 0, None)]
    return positions, markets, trades


async def run(tmp, w, block=False, redeemable=None, truncated=(), **kw):
    positions, markets, trades = w
    apis = Apis(FakeData(positions, trades, block=block, redeemable=redeemable, truncated=truncated),
                FakeGamma(markets), FakeClob())
    return await run_batch(config(tmp), apis=apis, now=NOW, **kw)


def read(tmp, name):
    def reject(c):
        raise AssertionError(f"non-JSON constant {c}")
    return json.loads((tmp / "snapshot" / name).read_text(), parse_constant=reject)


async def test_batch_produces_signal_for_certified_whale(tmp_path):
    report = await run(tmp_path, world(), skip_validation=True)
    assert report.certified_wallets == 1
    signals = read(tmp_path, "signals.json")
    assert [s["wallet"] for s in signals] == ["0xwhale"]
    s = signals[0]
    assert s["status"] == "SIGNAL" and s["tier"] == "A"
    assert s["quote"]["vwap"] == pytest.approx(0.41) and s["history"] and s["wallet_name"].startswith("name")
    assert {c["code"] for c in s["checks"]} == {f"G{i}" for i in range(1, 8)}
    assert s["asset"] == "live-yes"
    whales = read(tmp_path, "whales.json")
    assert whales[0]["wallet"] == "0xwhale" and whales[0]["certified"] and whales[0]["recent"]
    meta = read(tmp_path, "meta.json")
    assert meta["counts"]["signals"] == 1 and meta["mode"] == "SNAPSHOT"
    assert (tmp_path / "scores.parquet").exists()
    assert not (tmp_path / "snapshot" / "validation.json").exists()


async def test_batch_with_no_certified_whales_writes_snapshot(tmp_path):
    report = await run(tmp_path, world(skilled=False))
    assert report.certified_wallets == 0 and report.validation_ran
    assert read(tmp_path, "signals.json") == []
    contacts = read(tmp_path, "contacts.json")
    assert len(contacts) == 1 and contacts[0]["status"] == "REJECTED"
    assert all(not w["certified"] for w in read(tmp_path, "whales.json"))
    assert read(tmp_path, "validation.json")["verdict"] == "INSUFFICIENT DATA"
    assert read(tmp_path, "meta.json")["counts"]["certified_wallets"] == 0


async def test_score_only_stops_before_snapshot(tmp_path):
    report = await run(tmp_path, world(), stop_after_scoring=True)
    assert report.certified_wallets == 1 and list(report.top.wallet.unique()) == ["0xwhale"]
    assert not (tmp_path / "snapshot").exists()


async def test_second_run_is_incremental_and_stable(tmp_path):
    await run(tmp_path, world(), skip_validation=True)
    await run(tmp_path, world(), skip_validation=True)
    assert [s["wallet"] for s in read(tmp_path, "signals.json")] == ["0xwhale"]


def test_json_has_no_nan(tmp_path):
    write_json_atomic(tmp_path / "x.json", {"a": float("nan"), "b": [np.float64("inf"), np.int64(3)], "c": np.bool_(True)})
    assert json.loads((tmp_path / "x.json").read_text()) == {"a": None, "b": [None, 3], "c": True}
    assert not list(tmp_path.glob("*.tmp"))


async def test_blocked_run_keeps_previous_snapshot(tmp_path):
    snap = tmp_path / "snapshot"
    snap.mkdir()
    (snap / "meta.json").write_text('{"old": true}')
    with pytest.raises(BlockedError):
        await run(tmp_path, world(), block=True)
    assert json.loads((snap / "meta.json").read_text()) == {"old": True}


def test_cli_blocked_exit_code_keeps_snapshot(monkeypatch, capsys):
    async def blocked(*args, **kwargs):
        raise BlockedError("BLOCKED by bot protection at test")

    monkeypatch.setattr(cli, "run_batch", blocked)
    assert cli.main(["batch"]) == 3
    assert "BLOCKED" in capsys.readouterr().err


def test_cli_locked_exit_code(monkeypatch, capsys):
    async def locked(*args, **kwargs):
        raise LockedError("held by another whalescan process")

    monkeypatch.setattr(cli, "run_batch", locked)
    assert cli.main(["score"]) == 2
    assert "LOCKED" in capsys.readouterr().err


def test_cli_max_wallets_override(monkeypatch):
    seen = {}

    async def fake(cfg, **kwargs):
        seen["max"] = cfg.universe.max_wallets
        seen["kwargs"] = kwargs
        from whalescan.batch import BatchReport
        return BatchReport()

    monkeypatch.setattr(cli, "run_batch", fake)
    assert cli.main(["batch", "--max-wallets", "50", "--skip-validation"]) == 0
    assert seen["max"] == 50 and seen["kwargs"]["skip_validation"] is True


def test_cli_quiets_per_request_http_logs(monkeypatch):
    import logging

    async def fake(cfg, **kwargs):
        from whalescan.batch import BatchReport
        return BatchReport()

    monkeypatch.setattr(cli, "run_batch", fake)
    logging.getLogger("httpx").setLevel(logging.NOTSET)
    assert cli.main(["batch", "--skip-validation"]) == 0
    assert logging.getLogger("httpx").level == logging.WARNING
    assert cli.main(["-v", "batch", "--skip-validation"]) == 0
    assert logging.getLogger("httpx").level == logging.DEBUG


async def test_long_phases_log_progress(tmp_path, caplog):
    import logging

    caplog.set_level(logging.INFO, logger="whalescan.batch")
    await run(tmp_path, world(), skip_validation=True)
    text = caplog.text
    assert "positions: 31/31 wallets" in text
    assert "universe: 31 wallets" in text


async def test_unredeemed_losers_are_scored_so_winner_only_histories_do_not_certify(tmp_path):
    positions, markets, trades = world()
    wins, losses = [], []
    for i in range(120):
        cid = f"0xfaker-c{i}"
        won = i % 2 == 0
        markets[cid] = resolved(cid, 0 if won else 1)
        row = ClosedPosition("0xfaker", f"{cid}-y", cid, 0.5, 2000.0, 1000.0 if won else 0.0, 1.0 if won else 0.0,
                             "Yes", 0, f"F{i}", f"ev-{cid}", NOW - 20 * DAY + i if won else 0)
        (wins if won else losses).append(row)
    positions["0xfaker"] = wins  # /closed-positions: only the redeemed winners
    report = await run(tmp_path, (positions, markets, trades), redeemable={"0xfaker": losses}, skip_validation=True)
    assert report.certified_wallets == 1  # the genuinely skilled whale only
    scores = {w["wallet"]: w for w in read(tmp_path, "whales.json")}
    assert "0xfaker" not in scores or not scores["0xfaker"]["certified"]


async def test_truncated_history_windows_both_sources(tmp_path):
    # The whale's /closed-positions was depth-capped (recent window only). Its ancient unredeemed losers,
    # resolved long before that window starts, must not be mixed in — that would bias it downward.
    positions, markets, trades = world()
    old_losers = []
    for i in range(400):
        cid = f"0xwhale-old{i}"
        markets[cid] = Market(cid, "old", f"slug-{cid}", f"ev-{cid}", NOW - 400 * DAY, True, NOW - 400 * DAY,
                              (0.0, 1.0), (f"{cid}-y", f"{cid}-n"), False, 0.0, 1.0, 1e6, ("Politics",))
        old_losers.append(ClosedPosition("0xwhale", f"{cid}-y", cid, 0.5, 2000.0, 0.0, 0.0, "Yes", 0, "old", "ev", 0))
    for _ in range(2):  # second run is incremental and must keep the window
        report = await run(tmp_path, (positions, markets, trades), redeemable={"0xwhale": old_losers},
                           truncated={"0xwhale"}, skip_validation=True)
        assert report.certified_wallets == 1


async def test_guarded_swallows_parse_errors_but_not_blocks():
    from whalescan.batch import _guarded
    from whalescan.parsers import ParseError

    async def bad_payload():
        raise ParseError("price history", {"error": "x"}, KeyError("history"))

    async def blocked():
        raise BlockedError("BLOCKED")

    assert await _guarded(bad_payload(), "history") is None
    with pytest.raises(BlockedError):
        await _guarded(blocked(), "x")


def test_stale_wallets_are_not_scored():
    import pandas as pd

    from whalescan.batch import drop_stale_wallets
    frame = pd.DataFrame({"wallet": ["0xfresh", "0xstale", "0xnever"], "fetched_at": [NOW - DAY, NOW - 30 * DAY, None]})
    assert drop_stale_wallets(frame, now=NOW, max_age_days=14)["wallet"].tolist() == ["0xfresh"]


async def test_block_in_one_task_cancels_the_rest():
    import asyncio

    from whalescan.batch import _run_all
    finished = []

    async def slow(i):
        await asyncio.sleep(0.2)
        finished.append(i)

    async def blocked():
        raise BlockedError("BLOCKED")

    with pytest.raises(BlockedError):
        await _run_all([slow(1), blocked(), slow(2)])
    await asyncio.sleep(0.3)
    assert finished == []


def test_watchlist_requires_enough_evidence():
    import pandas as pd

    from whalescan.snapshot import whales_json
    from whalescan.scoring import ELIGIBLE_COLUMNS, SCORE_COLUMNS
    row = dict(category="ALL", n=5, edge=0.3, sigma=0.1, post_edge=0.0, bh_pass=False, certified=False, flags="",
               median_stake=10.0, as_of=0)
    scores = pd.DataFrame([dict(row, wallet="0xthin", n_eff=5.0, p_value=0.001),
                           dict(row, wallet="0xsolid", n_eff=60.0, p_value=0.2)], columns=SCORE_COLUMNS)
    out = whales_json(scores, pd.DataFrame(columns=ELIGIBLE_COLUMNS), {}, min_n_eff=20)
    assert [w["wallet"] for w in out] == ["0xsolid"]


def test_cli_live_starts_the_station(monkeypatch):
    seen = {}

    class FakeStation:
        def __init__(self, cfg):
            seen["cfg"] = cfg

        async def run(self, host, port):
            seen["addr"] = (host, port)

    monkeypatch.setattr(cli, "Station", FakeStation)
    assert cli.main(["live", "--port", "9999"]) == 0
    assert seen["addr"] == ("127.0.0.1", 9999)


def test_cli_live_ctrl_c_is_a_clean_exit(monkeypatch, capsys):
    class InterruptedStation:
        def __init__(self, cfg):
            pass

        async def run(self, host, port):
            raise KeyboardInterrupt

    monkeypatch.setattr(cli, "Station", InterruptedStation)
    assert cli.main(["live"]) == 0
    assert "stopped" in capsys.readouterr().out



async def test_fresh_wallet_big_news_bet_is_an_insider_alert_listed_first(tmp_path):
    positions, markets, trades = world()
    trades.append(Trade("0xins", NOW - 1800, "0xfresh1", "live-yes", "0xlive", "BUY", 0.40, 100_000.0, "it-happens",
                        "Will it happen?", "Yes", 0, None))
    await run(tmp_path, (positions, markets, trades), skip_validation=True)
    signals = read(tmp_path, "signals.json")
    assert [s["kind"] for s in signals][:1] == ["INSIDER"]
    ins = signals[0]
    assert ins["wallet"] == "0xfresh1" and ins["tier"] == "A" and ins["status"] == "INSIDER"
    assert {c["code"] for c in ins["checks"]} == {f"I{i}" for i in range(1, 7)}
    assert any(s["kind"] == "SKILL" for s in signals)          # the certified whale (certified mode in tests)
    assert read(tmp_path, "meta.json")["counts"]["insiders"] == 1


async def test_known_snipers_are_not_rejudged_as_insiders(tmp_path):
    positions, markets, trades = world()
    trades.append(Trade("0xhedge", NOW - 1800, "0xwhale", "live-no", "0xlive", "BUY", 0.60, 50_000.0, "it-happens",
                        "Will it happen?", "No", 1, None))                 # conflicting bet by the certified whale
    await run(tmp_path, (positions, markets, trades), skip_validation=True)
    whale_contacts = [c for c in read(tmp_path, "contacts.json") if c["wallet"] == "0xwhale"]
    assert whale_contacts and all(c["kind"] == "SKILL" for c in whale_contacts)


def test_cli_sweep_runs_the_insider_sweep(monkeypatch, capsys):
    seen = {}

    async def fake_sweep(cfg, **kwargs):
        from whalescan.sweep import SweepReport
        seen.update(kwargs)
        return SweepReport(fills=12, signals=1, insiders=1, contacts=3)

    monkeypatch.setattr(cli, "run_sweep", fake_sweep)
    assert cli.main(["sweep", "--publish"]) == 0
    assert seen == {"publish": True} and "1 insider" in capsys.readouterr().out
