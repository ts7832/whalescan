from whalescan.history_store import HistoryStore
from whalescan.models import Trade

NOW = 1_790_000_000
DAY = 86400


def fill(tx, ts, wallet="0xw", cid="0xc", usdc=2000.0, price=0.5):
    return Trade(tx, ts, wallet, f"{cid}-y", cid, "BUY", price, usdc / price, "ev", "q", "Yes", 0, None)


def test_closed_markets_are_fetched_once_and_open_ones_are_refreshed_daily(tmp_path):
    with HistoryStore(tmp_path / "h.duckdb") as h:
        h.register_markets(["0xa", "0xb"], is_open=False)
        h.register_markets(["0xo"], is_open=True)
        assert set(h.markets_needing_fills(NOW)) == {"0xa", "0xb", "0xo"}
        h.mark_fills("0xa", NOW, complete=True)
        h.mark_fills("0xo", NOW, complete=True)
        assert set(h.markets_needing_fills(NOW + 3600)) == {"0xb"}
        assert set(h.markets_needing_fills(NOW + DAY + 1)) == {"0xb", "0xo"}  # open markets keep trading


def test_an_incomplete_fetch_is_retried(tmp_path):
    with HistoryStore(tmp_path / "h.duckdb") as h:
        h.register_markets(["0xa"], is_open=False)
        h.mark_fills("0xa", NOW, complete=False)
        assert h.markets_needing_fills(NOW + 60) == ["0xa"]


def test_a_market_that_closes_is_fetched_once_more_and_then_left_alone(tmp_path):
    with HistoryStore(tmp_path / "h.duckdb") as h:
        h.register_markets(["0xa"], is_open=True)
        h.mark_fills("0xa", NOW, complete=True)
        h.register_markets(["0xa"], is_open=False)  # it resolved since: its last fills must be read
        assert h.markets_needing_fills(NOW + 60) == ["0xa"]
        h.mark_fills("0xa", NOW + 60, complete=True)
        assert h.markets_needing_fills(NOW + 10 * DAY) == []


def test_refetching_fills_never_duplicates_them(tmp_path):
    with HistoryStore(tmp_path / "h.duckdb") as h:
        h.upsert_trades([fill("0x1", NOW), fill("0x2", NOW + 5)])
        h.upsert_trades([fill("0x1", NOW), fill("0x2", NOW + 5)])
        assert len(h.trades_frame()) == 2


def test_markets_traded_before_counts_only_earlier_distinct_markets(tmp_path):
    with HistoryStore(tmp_path / "h.duckdb") as h:
        h.upsert_trades([fill("0x1", NOW - 100, cid="0xa"), fill("0x2", NOW - 50, cid="0xa"),
                         fill("0x3", NOW - 10, cid="0xb"), fill("0x4", NOW, cid="0xc"), fill("0x5", NOW + 9, cid="0xd")])
        assert h.markets_traded_before("0xw", NOW) is None  # history not fetched: unknown, never guessed
        h.mark_wallet_history("0xw", NOW + 10)
        assert h.markets_traded_before("0xw", NOW) == 2  # 0xa and 0xb; the bet's own market and later ones excluded
        assert h.wallets_with_history() == {"0xw"}


def test_price_after_returns_the_first_price_at_or_after_the_delay(tmp_path):
    with HistoryStore(tmp_path / "h.duckdb") as h:
        h.upsert_prices("tok", [(NOW + 60, 0.41), (NOW + 300, 0.45), (NOW + 900, 0.50)])
        assert h.price_after("tok", NOW + 60, max_wait_s=600) == 0.41
        assert h.price_after("tok", NOW + 61, max_wait_s=600) == 0.45
        assert h.price_after("tok", NOW + 301, max_wait_s=600) == 0.50
        assert h.price_after("tok", NOW + 901, max_wait_s=600) is None  # nothing traded: no price, not a guess
        assert h.price_after("tok", NOW + 400, max_wait_s=60) is None   # next print too late to count


def test_price_windows_are_remembered(tmp_path):
    with HistoryStore(tmp_path / "h.duckdb") as h:
        assert not h.has_price_window("tok", NOW)
        h.mark_price_window("tok", NOW)
        assert h.has_price_window("tok", NOW)
