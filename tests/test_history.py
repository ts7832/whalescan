from dataclasses import replace

from whalescan.api.data_api import TradePage
from whalescan.batch import Apis
from whalescan.config import load_config
from whalescan.history import build_history
from whalescan.history_store import HistoryStore
from whalescan.models import Market, PricePoint, Trade

NOW = 1_790_000_000
DAY = 86400
CFG = load_config()


def mk(cid, tags=("Politics",), closed=True, volume=1e6, slug=None):
    return Market(cid, f"Q {cid}", slug or f"s-{cid}", f"ev-{cid}", NOW - 5 * DAY, closed, NOW - 5 * DAY,
                  (1.0, 0.0) if closed else (0.4, 0.6), (f"{cid}-y", f"{cid}-n"), False, 0.0, 1.0, volume, tags)


def fill(tx, ts, wallet, cid, usdc, price=0.40):
    return Trade(tx, ts, wallet, f"{cid}-y", cid, "BUY", price, usdc / price, f"ev-{cid}", "q", "Yes", 0, None)


class FakeGamma:
    skipped = 0

    def __init__(self, closed, opened=(), created=None):
        self.closed, self.opened, self.created = list(closed), list(opened), created or {}
        self.profile_calls: list[str] = []

    async def listed_markets(self, *, closed, end_min_ts, end_max_ts, min_volume):
        return self.closed if closed else self.opened

    async def created_ts(self, wallet):
        self.profile_calls.append(wallet)
        return self.created.get(wallet)


class FakeData:
    skipped = 0

    def __init__(self, by_market, by_wallet=None):
        self.by_market, self.by_wallet = by_market, by_wallet or {}
        self.market_calls: list[str] = []
        self.wallet_calls: list[str] = []

    async def trades(self, *, user=None, market=None, min_usdc=None, since_ts=None):
        if market is not None:
            self.market_calls.append(market)
            return TradePage([t for t in self.by_market.get(market, []) if min_usdc is None or t.usdc >= min_usdc], True)
        self.wallet_calls.append(user)
        return TradePage(self.by_wallet.get(user, []), True)


class FakeClob:
    skipped = 0

    def __init__(self):
        self.windows: list[tuple[str, int, int]] = []

    async def price_window(self, token_id, start_ts, end_ts, *, fidelity=1):
        self.windows.append((token_id, start_ts, end_ts))
        return [PricePoint(start_ts + 60, 0.41), PricePoint(start_ts + 900, 0.45)]


def world():
    fresh_bet = fill("0xa1", NOW - 20 * DAY, "0xfresh", "0xpol", 30_000)
    old_bet = fill("0xa2", NOW - 19 * DAY, "0xold", "0xpol", 30_000)
    small = fill("0xa3", NOW - 20 * DAY, "0xsmall", "0xpol", 1_500)
    sport = fill("0xa4", NOW - 20 * DAY, "0xfresh", "0xsport", 30_000)
    gamma = FakeGamma([mk("0xpol"), mk("0xsport", tags=("Sports",)), mk("0xupdown", slug="btc-updown-15m")],
                      created={"0xfresh": NOW - 21 * DAY, "0xold": NOW - 900 * DAY, "0xsmall": NOW - 21 * DAY})
    data = FakeData({"0xpol": [fresh_bet, old_bet, small], "0xsport": [sport]},
                    by_wallet={"0xfresh": [fresh_bet, fill("0xh1", NOW - 20 * DAY - 3600, "0xfresh", "0xother", 50)]})
    return Apis(data, gamma, FakeClob())


async def test_only_news_markets_are_studied_and_their_fills_fetched_once(tmp_path):
    apis = world()
    with HistoryStore(tmp_path / "h.duckdb") as h:
        rep = await build_history(apis, h, CFG, NOW)
        assert apis.data.market_calls == ["0xpol"]  # sports and blocklisted up/down markets are not studied
        assert rep.markets == 1 and rep.fills == 3
        again = await build_history(apis, h, CFG, NOW + 3600)
    assert apis.data.market_calls == ["0xpol"] and again.fills == 0
    assert again.profiles == 0 and again.wallet_histories == 0 and again.price_windows == 0


async def test_profiles_only_for_big_buyers_and_histories_only_for_young_wallets(tmp_path):
    apis = world()
    with HistoryStore(tmp_path / "h.duckdb") as h:
        rep = await build_history(apis, h, CFG, NOW)
        assert sorted(apis.gamma.profile_calls) == ["0xfresh", "0xold"]  # 0xsmall's $1.5k is below the floor
        assert apis.data.wallet_calls == ["0xfresh"]  # 0xold is 880 days old at the bet: no history needed
        assert rep.wallet_histories == 1
        assert h.markets_traded_before("0xfresh", NOW - 20 * DAY) == 1  # the earlier 0xother trade


async def test_price_windows_start_at_the_signal_and_cover_the_longest_delay(tmp_path):
    apis = world()
    cfg = replace(CFG, study=replace(CFG.study, baseline_sample=0))
    with HistoryStore(tmp_path / "h.duckdb") as h:
        await build_history(apis, h, cfg, NOW)
        assert apis.clob.windows == [("0xpol-y", NOW - 20 * DAY, NOW - 20 * DAY + (max(cfg.study.entry_delays_min)
                                                                                    + 10) * 60)]
        assert h.price_after("0xpol-y", NOW - 20 * DAY, max_wait_s=120) == 0.41


async def test_a_baseline_sample_of_older_wallets_is_priced_too(tmp_path):
    apis = world()
    with HistoryStore(tmp_path / "h.duckdb") as h:
        await build_history(apis, h, CFG, NOW)  # the default sample is large enough to include 0xold's bet
    assert sorted(t0 for _, t0, _ in apis.clob.windows) == [NOW - 20 * DAY, NOW - 19 * DAY]


def test_cli_history_builds_the_dataset_and_reports_what_it_fetched(monkeypatch, capsys):
    from whalescan import cli
    from whalescan.history import HistoryReport

    async def fake(cfg, **kwargs):
        return HistoryReport(markets=12, fills=3400, truncated_markets=1, profiles=250, wallet_histories=40,
                             price_windows=600)

    monkeypatch.setattr(cli, "run_history", fake)
    assert cli.main(["history"]) == 0
    out = capsys.readouterr().out
    assert "12 markets" in out and "3400 fills" in out and "600 price windows" in out and "1 truncated" in out


async def test_run_history_opens_the_configured_database(tmp_path):
    from dataclasses import replace

    from whalescan.config import PathsCfg
    from whalescan.history import run_history

    cfg = replace(CFG, paths=PathsCfg(research_db=str(tmp_path / "r.duckdb"), scores_parquet=str(tmp_path / "s.pq"),
                                      snapshot_dir=str(tmp_path / "snap"), ledger_dir=str(tmp_path / "ledger"),
                                      history_db=str(tmp_path / "history.duckdb"), archive_dir=str(tmp_path / "archive")))
    rep = await run_history(cfg, apis=world(), now=NOW)
    assert rep.markets == 1 and (tmp_path / "history.duckdb").exists()
