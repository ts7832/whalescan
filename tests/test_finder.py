import math

import pandas as pd
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
    store.upsert_finder_prices("0xc-y", [(T + HOUR, 0.30), (T + 5 * HOUR, 0.46)])  # target for 0.25 is 0.45
    r = row(moves_frame(store, CFG), "0xw")
    assert r["move_hit"] == 1.0
    assert r["p0"] == pytest.approx(0.25)


def test_a_bet_that_never_reaches_the_target_is_a_known_miss(store):
    store.upsert_trades([fill("a", T, "0xw", "0xc", 30_000, price=0.25)])
    store.upsert_finder_prices("0xc-y", [(T + HOUR, 0.30), (T + 20 * HOUR, 0.35)])
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


def test_an_entry_never_uses_a_price_recorded_before_the_signal(store):
    # a gap after the signal (the only recorded print is stale, from before the bet) must give NO entry, not
    # a stale one that understates the real cost.
    store.upsert_trades([fill("a", T, "0xw", "0xc", 30_000, price=0.40)])
    store.upsert_finder_prices("0xc-y", [(T - 3600, 0.10)])  # only a pre-signal print exists
    r = row(moves_frame(store, CFG), "0xw")
    delay = CFG.study.entry_delays_min[0]
    assert math.isnan(r[f"entry_{delay}"]) and math.isnan(r[f"ret_{delay}"])


def test_small_buys_are_not_moves(store):
    store.upsert_trades([fill("a", T, "0xsmall", "0xc", CFG.finder.min_usdc - 1)])
    df = moves_frame(store, CFG)
    assert df.empty or "0xsmall" not in set(df["wallet"])


def test_the_copy_trade_columns_are_present_and_correct(store):
    store.upsert_trades([fill("a", T, "0xw", "0xc", 30_000, price=0.40)])
    store.upsert_finder_prices("0xc-y", [(T + 30, 0.40), (T + 4 * MIN, 0.42)])
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


# --- base_rates / qualify ---------------------------------------------------

from whalescan.finder import base_rates, qualify  # noqa: E402


def frame(rows):
    return pd.DataFrame(rows)


def move(idx_wallet, signal_ts, known_ts, bucket, hit):
    return {"wallet": idx_wallet, "signal_ts": signal_ts, "known_ts": known_ts, "bucket": bucket, "move_hit": hit}


def test_base_rates_are_learned_only_from_bets_with_a_known_outcome():
    train = frame([move("w", 0, 100, "0.40-0.60", 1.0), move("w", 1, 101, "0.40-0.60", 0.0),
                  move("w", 2, 102, "0.40-0.60", float("nan"))])  # unknown: must not count
    rates = base_rates(train)
    assert rates["0.40-0.60"] == pytest.approx(0.5)


def test_qualify_never_uses_a_bets_own_move_to_qualify_itself():
    # a wallet's ONLY bet, a hit in a bucket whose base rate is low: it must never look "informed" from its
    # own outcome — a real wallet with zero prior track record has nothing to be judged on yet.
    moves = frame([move("w", 1000, 1100, "0.02-0.20", 1.0)])
    rates = {"0.02-0.20": 0.1}
    assert not qualify(moves, rates, min_bets=1, min_z=0.0).iloc[0]


def test_a_move_known_after_the_signal_time_does_not_count_yet():
    moves = frame([move("w", 1000, 1100, "0.40-0.60", 1.0),  # earlier bet, known at 1100
                  move("w", 1099, 5000, "0.40-0.60", 1.0)])   # this bet fires at 1099 — before the first is known
    rates = {"0.40-0.60": 0.1}
    result = qualify(moves, rates, min_bets=1, min_z=0.0)
    assert not result.iloc[1]  # at signal_ts=1099, the first bet's known_ts=1100 hasn't arrived (1100 > 1099)


def test_a_move_known_exactly_at_the_signal_time_does_count():
    moves = frame([move("w", 1000, 1100, "0.40-0.60", 1.0), move("w", 1100, 5000, "0.40-0.60", 1.0)])
    rates = {"0.40-0.60": 0.1}
    result = qualify(moves, rates, min_bets=1, min_z=0.0)
    assert result.iloc[1]


def test_qualification_needs_at_least_min_bets_known_moves():
    early = [move("w", i, i + 1, "0.40-0.60", 1.0) for i in range(4)]  # 4 hits, would easily clear min_z
    moves = frame(early + [move("w", 1000, 2000, "0.40-0.60", 1.0)])
    rates = {"0.40-0.60": 0.5}
    assert not qualify(moves, rates, min_bets=5, min_z=0.0).iloc[-1]  # only 4 prior known moves: not enough


def test_qualification_z_score_matches_the_hand_computed_value():
    # 10 prior known bets, base rate 0.5, 9 hits: E=5, V=2.5, z=(9-5)/sqrt(2.5) ~= 2.530
    early = [move("w", i, i + 1, "0.40-0.60", 1.0 if i < 9 else 0.0) for i in range(10)]
    moves = frame(early + [move("w", 1000, 2000, "0.40-0.60", 1.0)])
    rates = {"0.40-0.60": 0.5}
    assert qualify(moves, rates, min_bets=5, min_z=2.33).iloc[-1]  # z ~= 2.530 >= 2.33
    assert not qualify(moves, rates, min_bets=5, min_z=2.6).iloc[-1]  # z ~= 2.530 < 2.6


def test_a_wallet_exactly_matching_the_base_rate_does_not_qualify():
    early = [move("w", i, i + 1, "0.40-0.60", 1.0 if i < 5 else 0.0) for i in range(10)]  # 5/10, matches base rate
    moves = frame(early + [move("w", 1000, 2000, "0.40-0.60", 1.0)])
    rates = {"0.40-0.60": 0.5}
    assert not qualify(moves, rates, min_bets=5, min_z=0.5).iloc[-1]


# --- is_confirmed_alert (shared eligibility predicate for the live CONFIRMED badge) ----------------------

from whalescan.classify import Blocklist as _Blocklist  # noqa: E402
from whalescan.finder import is_confirmed_alert  # noqa: E402
from whalescan.gate import Check, Evaluation, PositionEvent  # noqa: E402

BL2 = _Blocklist(CFG.blocklist)
COVERED = mk("0xc")
SPORTS_MKT = mk("0xs", tags=("Sports",))


def ev(wallet="0xproven", side="BUY", usdc=30_000.0, price=0.40, status="REJECTED", cid="0xc"):
    pe = PositionEvent(wallet, f"{cid}-y", cid, side, T, T + 60, usdc, usdc / price, price, 1, f"ev-{cid}",
                       f"q {cid}", "Yes", 0)
    checks = (Check("G1", False, "N/A"),)
    return Evaluation(pe, "POLITICS", checks, status, None, None, None, None, None, None, ())


def test_is_confirmed_alert_requires_the_wallet_to_be_on_the_watchlist():
    assert not is_confirmed_alert(ev(), COVERED, set(), CFG, BL2)
    assert is_confirmed_alert(ev(), COVERED, {"0xproven"}, CFG, BL2)


def test_is_confirmed_alert_requires_a_buy():
    assert not is_confirmed_alert(ev(side="SELL"), COVERED, {"0xproven"}, CFG, BL2)


def test_is_confirmed_alert_excludes_expired_events():
    assert not is_confirmed_alert(ev(status="EXPIRED"), COVERED, {"0xproven"}, CFG, BL2)


def test_is_confirmed_alert_requires_the_tradeable_price_band():
    assert not is_confirmed_alert(ev(price=0.98), COVERED, {"0xproven"}, CFG, BL2)
    assert not is_confirmed_alert(ev(price=0.005), COVERED, {"0xproven"}, CFG, BL2)


def test_is_confirmed_alert_requires_a_covered_market():
    assert not is_confirmed_alert(ev(), SPORTS_MKT, {"0xproven"}, CFG, BL2)
    assert not is_confirmed_alert(ev(), None, {"0xproven"}, CFG, BL2)


def test_is_confirmed_alert_requires_the_finder_size_floor():
    assert not is_confirmed_alert(ev(usdc=CFG.finder.min_usdc - 1), COVERED, {"0xproven"}, CFG, BL2)
    assert is_confirmed_alert(ev(usdc=CFG.finder.min_usdc), COVERED, {"0xproven"}, CFG, BL2)
