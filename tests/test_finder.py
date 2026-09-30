import math

import pytest

from whalescan.config import load_config
from whalescan.finder import moves_frame
from whalescan.history_store import HistoryStore
from whalescan.models import Market, Trade

NOW = 1_790_000_000
DAY, MIN, HOUR = 86400, 60, 3600
CFG = load_config()
T = NOW - 30 * DAY  # bet time


def mk(cid, tags=("Politics",), closed=True, end_ts=None, closed_ts=None, prices=(1.0, 0.0)):
    return Market(cid, f"Q {cid}", f"s-{cid}", f"ev-{cid}", end_ts if end_ts is not None else NOW - 10 * DAY,
                  closed, closed_ts if closed_ts is not None else (NOW - 10 * DAY if closed else None), prices,
                  (f"{cid}-y", f"{cid}-n"), False, 0.0, 1.0, 1e6, tags)


def fill(tx, ts, wallet, cid, usdc, price=0.25, idx=0):
    return Trade(tx, ts, wallet, f"{cid}-{'y' if idx == 0 else 'n'}", cid, "BUY", price, usdc / price, f"ev-{cid}",
                 "q", "Yes" if idx == 0 else "No", idx, None)


@pytest.fixture
def store(tmp_path):
    with HistoryStore(tmp_path / "h.duckdb") as h:
        h.upsert_markets([mk("0xc"), mk("0xopen", closed=False, end_ts=NOW + 30 * DAY),
                          mk("0xsport", tags=("Sports",))], NOW)
        h.register_markets(["0xc"], is_open=False, is_news=False)
        h.register_markets(["0xopen"], is_open=True, is_news=False)
        h.register_markets(["0xsport"], is_open=False, is_news=False)
        yield h


def row(df, wallet, cid="0xc"):
    r = df[(df["wallet"] == wallet) & (df["condition_id"] == cid)]
    assert len(r) == 1, r
    return r.iloc[0]


def test_known_ts_is_24h_after_the_signal_by_default(store):
    store.upsert_trades([fill("a", T, "0xw", "0xc", 30_000)])
    r = row(moves_frame(store, CFG), "0xw")
    assert r["known_ts"] == T + 24 * HOUR


def test_known_ts_is_capped_by_an_earlier_market_close(store):
    early = mk("0xearly", closed=True, closed_ts=T + 3 * HOUR)
    store.upsert_markets([early], NOW)
    store.register_markets(["0xearly"], is_open=False, is_news=False)
    store.upsert_trades([fill("a", T, "0xw", "0xearly", 30_000)])
    r = row(moves_frame(store, CFG), "0xw", "0xearly")
    assert r["known_ts"] == T + 3 * HOUR


def test_a_bet_with_no_recorded_price_is_unknown_not_a_miss(store):
    store.upsert_trades([fill("a", T, "0xw", "0xc", 30_000)])
    r = row(moves_frame(store, CFG), "0xw")
    assert math.isnan(r["move_hit"])


def test_a_bet_whose_price_moves_the_target_within_24h_is_a_hit(store):
    store.upsert_trades([fill("a", T, "0xw", "0xc", 30_000, price=0.25)])
    store.upsert_prices("0xc-y", [(T + HOUR, 0.30), (T + 5 * HOUR, 0.46)])  # target for 0.25 is 0.45
    r = row(moves_frame(store, CFG), "0xw")
    assert r["move_hit"] == 1.0
    assert r["p0"] == pytest.approx(0.25)


def test_a_bet_that_never_reaches_the_target_is_a_known_miss(store):
    store.upsert_trades([fill("a", T, "0xw", "0xc", 30_000, price=0.25)])
    store.upsert_prices("0xc-y", [(T + HOUR, 0.30), (T + 20 * HOUR, 0.35)])
    r = row(moves_frame(store, CFG), "0xw")
    assert r["move_hit"] == 0.0


def test_sports_markets_never_produce_a_row(store):
    store.upsert_trades([fill("a", T, "0xw", "0xsport", 30_000)])
    df = moves_frame(store, CFG)
    assert df.empty or "0xsport" not in set(df["condition_id"])


def test_open_markets_produce_a_row_but_are_never_scored(store):
    store.upsert_trades([fill("a", T, "0xw", "0xopen", 30_000)])
    r = row(moves_frame(store, CFG), "0xw", "0xopen")
    assert r["is_open"] and math.isnan(r["payout"])


def test_small_buys_are_not_moves(store):
    store.upsert_trades([fill("a", T, "0xsmall", "0xc", CFG.finder.min_usdc - 1)])
    df = moves_frame(store, CFG)
    assert df.empty or "0xsmall" not in set(df["wallet"])


def test_the_copy_trade_columns_are_present_and_correct(store):
    store.upsert_trades([fill("a", T, "0xw", "0xc", 30_000, price=0.40)])
    store.upsert_prices("0xc-y", [(T + 30, 0.40), (T + 4 * MIN, 0.42)])
    r = row(moves_frame(store, CFG), "0xw")
    assert 5 in CFG.study.entry_delays_min, "test assumes a 5-minute delay is configured"
    cost = 0.42 + CFG.study.half_spread  # at +5min, the last known price is the +4min print
    assert r["entry_5"] == pytest.approx(cost)
    assert r["ret_5"] == pytest.approx((1.0 - cost) / cost)  # 0xc resolved YES


@pytest.mark.parametrize("p0,expected", [(0.10, "0.02-0.20"), (0.25, "0.20-0.40"), (0.85, "0.80-0.90")])
def test_the_bucket_matches_the_preregistered_edges(store, p0, expected):
    store.upsert_trades([fill("a", T, "0xw", "0xc", 30_000, price=p0)])
    r = row(moves_frame(store, CFG), "0xw")
    assert r["bucket"] == expected
