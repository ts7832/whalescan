import pandas as pd
import pytest

from whalescan.config import load_config
from whalescan.live_state import LiveState
from whalescan.models import BookSnapshot, Market, Trade
from whalescan.scoring import SCORE_COLUMNS
from whalescan.stream.messages import PriceChanges

CFG = load_config()
NOW = 1_790_000_000
H = 3600


def score(wallet, category, certified, post_edge=0.06, median_stake=2000.0):
    return {"wallet": wallet, "category": category, "n": 100, "n_eff": 100.0, "edge": 0.08, "sigma": 0.02,
            "post_edge": post_edge, "p_value": 0.001 if certified else 0.5, "bh_pass": certified,
            "certified": certified, "flags": "", "median_stake": median_stake, "as_of": NOW}


SCORES = pd.DataFrame([score("0xwhale", "ALL", True), score("0xwhale", "POLITICS", True),
                       score("0xnobody", "ALL", False)], columns=SCORE_COLUMNS)
MARKET = Market("0xc", "Who wins?", "who-wins", "us-election", NOW + 48 * H, False, None, (0.4, 0.6),
                ("yes", "no"), False, 0.0, 1.0, 1e6, ("Politics",))


def trade(ts=NOW - 60, wallet="0xwhale", side="BUY", price=0.40, usdc=10_000.0, asset="yes", tx=None):
    return Trade(tx or f"0x{ts}{wallet}{side}", ts, wallet, asset, "0xc", side, price, usdc / price, "us-election",
                 "Who wins?", "Yes", 0 if asset == "yes" else 1, None)


def state(markets=None):
    return LiveState(CFG, scores=SCORES, markets={"0xc": MARKET} if markets is None else markets,
                     names={"0xwhale": "Whale"})


def seed_book(s, asset="yes", ask=0.41):
    s.books.on_snapshot(BookSnapshot(asset, NOW * 1000, ((0.39, 5000.0),), ((ask, 100_000.0),)))
    return s.on_book(asset, NOW)


def kinds(msgs):
    return [(m["type"], m["data"]["status"]) for m in msgs]


def test_small_trades_from_unknown_wallets_are_silent():
    assert state().on_trade(trade(wallet="0xrandom", usdc=50.0), NOW) == []


def test_large_uncertified_trade_is_a_contact():
    msgs = state().on_trade(trade(wallet="0xnobody"), NOW)
    assert kinds(msgs) == [("contact", "REJECTED")]
    assert msgs[0]["data"]["checks"][1]["detail"] == "NOT CERTIFIED"


def test_certified_trade_waits_for_book_then_becomes_a_signal():
    s = state()
    msgs = s.on_trade(trade(), NOW)
    assert kinds(msgs) == [("contact", "REJECTED")]            # G6: NO BOOK yet
    assert "yes" in s.watch_set(NOW)
    msgs = seed_book(s)
    assert kinds(msgs) == [("signal", "SIGNAL")]
    assert msgs[0]["data"]["wallet_name"] == "Whale" and msgs[0]["data"]["quote"]["vwap"] == pytest.approx(0.41)
    assert [x["id"] for x in s.state()["signals"]] == [msgs[0]["data"]["id"]]


def test_fills_within_the_window_merge_into_one_event():
    s = state()
    seed_book(s)
    a = s.on_trade(trade(ts=NOW - 300, usdc=3_000.0, tx="0x1"), NOW)
    b = s.on_trade(trade(ts=NOW - 100, usdc=3_000.0, tx="0x2"), NOW)
    assert a == []                                             # $3k alone is below the gate floor
    assert kinds(b) == [("signal", "SIGNAL")] and b[0]["data"]["usdc"] == 6_000.0 and b[0]["data"]["n_fills"] == 2


def test_duplicate_fill_is_ignored():
    s = state()
    s.on_trade(trade(tx="0xdup"), NOW)
    assert s.on_trade(trade(tx="0xdup"), NOW) == []


def test_signal_goes_stale_when_price_runs_past_max_entry_and_recovers():
    s = state()
    seed_book(s)
    sig = s.on_trade(trade(), NOW)[0]["data"]
    assert sig["max_entry"] < 0.46
    # one message: the 0.41 ask is lifted and a 0.47 ask appears (server top of book after the message: 0.47)
    s.books.on_changes(PriceChanges(NOW * 1000 + 1, ("yes", "yes"), (1, 1), (0.41, 0.47), (0.0, 100_000.0),
                                    (0.39, 0.39), (None, 0.47)))
    assert not s.books.desynced
    msgs = s.on_book("yes", NOW)
    assert kinds(msgs) == [("signal_update", "STALE")]
    s.books.on_changes(PriceChanges(NOW * 1000 + 3, ("yes",), (1,), (0.41,), (100_000.0,), (0.39,), (0.41,)))
    assert kinds(s.on_book("yes", NOW)) == [("signal_update", "SIGNAL")]


def test_sell_is_an_exit_contact():
    assert kinds(state().on_trade(trade(side="SELL"), NOW)) == [("contact", "EXIT")]


def test_unknown_market_is_requested_then_reevaluated():
    s = state(markets={})
    s.on_trade(trade(), NOW)
    assert s.missing_markets() == {"0xc"}
    seed_book(s)
    msgs = s.set_markets({"0xc": MARKET}, NOW)
    assert s.missing_markets() == set() and kinds(msgs) == [("signal", "SIGNAL")]


def test_watch_set_is_capped_and_most_recent_first():
    s = LiveState(CFG, scores=SCORES, markets={"0xc": MARKET}, max_watch=2)
    for i in range(3):
        s.on_trade(trade(ts=NOW - 1000 + i * 700, asset=f"a{i}", tx=f"0x{i}"), NOW)
    assert s.watch_set(NOW) == ["a2", "a1"]


def test_expiry_retires_signals_and_old_events():
    s = state()
    seed_book(s)
    s.on_trade(trade(), NOW)
    msgs = s.expire(NOW + 25 * H)
    assert kinds(msgs) == [("signal_update", "EXPIRED")]
    assert s.state()["signals"] == [] and s.watch_set(NOW + 25 * H) == []


def test_new_scores_reevaluate_open_events():
    s = state()
    seed_book(s)
    s.on_trade(trade(wallet="0xnobody"), NOW)
    promoted = pd.DataFrame([score("0xnobody", "ALL", True), score("0xnobody", "POLITICS", True)], columns=SCORE_COLUMNS)
    assert ("signal", "SIGNAL") in kinds(s.set_scores(promoted, NOW))
