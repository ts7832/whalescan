from whalescan.api.data_api import TradePage
from whalescan.batch import Apis
from whalescan.config import load_config
from whalescan.history import build_history, study_events
from whalescan.history_store import HistoryStore
from whalescan.models import Market, PricePoint, Trade

NOW = 1_790_000_000
DAY, HOUR = 86400, 3600
CFG = load_config()


def mk(cid, tags=("Politics",), closed=True, volume=1e6, question=None):
    q = question or f"Q {cid}"
    return Market(cid, q, f"s-{cid}", f"ev-{cid}", NOW - 5 * DAY, closed, NOW - 5 * DAY,
                  (1.0, 0.0) if closed else (0.4, 0.6), (f"{cid}-y", f"{cid}-n"), False, 0.0, 1.0, volume, tags)


def fill(tx, ts, wallet, cid, usdc, price=0.40):
    return Trade(tx, ts, wallet, f"{cid}-y", cid, "BUY", price, usdc / price, f"ev-{cid}", "q", "Yes", 0, None)


class FakeGamma:
    skipped = 0

    def __init__(self, closed=(), opened=()):
        self.closed, self.opened = list(closed), list(opened)

    async def listed_markets(self, *, closed, end_min_ts, end_max_ts, min_volume):
        return self.closed if closed else self.opened

    async def created_ts(self, wallet):
        return None


class FakeData:
    skipped = 0

    def __init__(self, by_market):
        self.by_market = by_market
        self.market_calls: list[str] = []

    async def trades(self, *, user=None, market=None, min_usdc=None, since_ts=None):
        if market is not None:
            self.market_calls.append(market)
            return TradePage([t for t in self.by_market.get(market, []) if min_usdc is None or t.usdc >= min_usdc], True)
        return TradePage([], True)


class FakeClob:
    skipped = 0

    def __init__(self):
        self.calls: list[tuple[str, int, int, int]] = []

    async def price_window(self, token_id, start_ts, end_ts, *, fidelity=1):
        self.calls.append((token_id, start_ts, end_ts, fidelity))
        return [PricePoint(start_ts + 600, 0.50)]


def world():
    # 0xpol: a news market (already studied). 0xcrypto: covered but NOT news (Crypto tag, not a price market).
    # 0xsport: sports, never covered. 0xprice: a price-threshold market, never covered even though tagged Crypto.
    news_bet = fill("a1", NOW - 20 * DAY, "0xw1", "0xpol", 30_000)
    crypto_bet = fill("a2", NOW - 20 * DAY, "0xw2", "0xcrypto", 30_000)
    sport_bet = fill("a3", NOW - 20 * DAY, "0xw3", "0xsport", 30_000)
    price_bet = fill("a4", NOW - 20 * DAY, "0xw4", "0xprice", 30_000)
    gamma = FakeGamma([mk("0xpol"), mk("0xcrypto", tags=("Crypto",), question="Will MSTR announce a purchase?"),
                       mk("0xsport", tags=("Sports",)), mk("0xprice", tags=("Crypto",), question="Will BTC hit $150k?")])
    data = FakeData({"0xpol": [news_bet], "0xcrypto": [crypto_bet], "0xsport": [sport_bet], "0xprice": [price_bet]})
    return Apis(data, gamma, FakeClob())


async def test_covered_markets_beyond_news_are_registered_and_fetched(tmp_path):
    apis = world()
    with HistoryStore(tmp_path / "h.duckdb") as h:
        await build_history(apis, h, CFG, NOW)
        assert h.study_market_ids() == {"0xpol"}  # the evidence study's own view: unchanged, news only
        assert h.covered_market_ids() == {"0xpol", "0xcrypto"}  # widened: sports/price-threshold excluded
        assert sorted(apis.data.market_calls) == ["0xcrypto", "0xpol"]


async def test_covered_events_include_the_wider_set_while_study_events_stay_news_only(tmp_path):
    apis = world()
    with HistoryStore(tmp_path / "h.duckdb") as h:
        await build_history(apis, h, CFG, NOW)
        news = {e.condition_id for e in study_events(h, CFG)}
        covered = {e.condition_id for e in study_events(h, CFG, news_only=False)}
        assert news == {"0xpol"} and covered == {"0xpol", "0xcrypto"}


async def test_one_finder_price_window_per_asset_and_6h_slot_covers_every_bet_in_it(tmp_path):
    apis = world()
    slot_start = (NOW - 20 * DAY) // (6 * HOUR) * (6 * HOUR)
    with HistoryStore(tmp_path / "h.duckdb") as h:
        h.upsert_markets([mk("0xcrypto", tags=("Crypto",), question="Will MSTR announce a purchase?")], NOW)
        h.register_markets(["0xcrypto"], is_open=False, is_news=False)
        h.upsert_trades([fill("a1", NOW - 20 * DAY, "0xw1", "0xcrypto", 30_000),
                         fill("a2", NOW - 20 * DAY + 3600, "0xw2", "0xcrypto", 30_000)])  # same asset, same 6h slot
        h.mark_fills("0xcrypto", NOW, complete=True)
        await build_history(apis, h, CFG, NOW)
    calls = [c for c in apis.clob.calls if c[0] == "0xcrypto-y"]
    assert len(calls) == 1  # one window served both bets
    token, start, end, fidelity = calls[0]
    assert start == slot_start and end - start == int(CFG.finder.move_window_h * 3600) + 6 * HOUR and fidelity == 10


async def test_a_second_run_fetches_no_new_finder_windows(tmp_path):
    apis = world()
    with HistoryStore(tmp_path / "h.duckdb") as h:
        await build_history(apis, h, CFG, NOW)
        n = len(apis.clob.calls)
        await build_history(apis, h, CFG, NOW + HOUR)
    assert len(apis.clob.calls) == n
