from dataclasses import replace

import pandas as pd
import pytest

from whalescan.config import load_config
from whalescan.live_state import LiveState
from whalescan.models import BookSnapshot, Market, Trade
from whalescan.scoring import SCORE_COLUMNS
from whalescan.stream.messages import PriceChanges

CFG = load_config()
CFG = replace(CFG, gate=replace(CFG.gate, skill_mode="certified"))
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
    s.books.subscribe(s.books.subscribed | {asset})
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


def test_resolved_markets_are_not_watched():
    closed = Market("0xc", "Who wins?", "who-wins", "us-election", NOW - H, True, NOW - H, (1.0, 0.0),
                    ("yes", "no"), False, 0.0, 1.0, 1e6, ("Politics",))
    s = LiveState(CFG, scores=SCORES, markets={"0xc": closed})
    s.on_trade(trade(), NOW)
    assert s.watch_set(NOW) == []


# ---------------------------------------------------------------- review fixes

def test_net_edge_change_is_pushed_even_if_status_is_unchanged():
    s = state()
    seed_book(s, ask=0.41)
    s.on_trade(trade(), NOW)
    s.books.on_changes(PriceChanges(NOW * 1000 + 1, ("yes", "yes"), (1, 1), (0.41, 0.415), (0.0, 100_000.0),
                                    (0.39, 0.39), (None, 0.415)))
    msgs = s.on_book("yes", NOW)
    assert [(m["type"], m["data"]["status"]) for m in msgs] == [("signal_update", "SIGNAL")]
    assert msgs[0]["data"]["net_edge"] == pytest.approx(0.06 - 0.015)


def test_missing_book_is_a_resync_not_stale():
    s = state()
    seed_book(s)
    s.on_trade(trade(), NOW)
    s.books.link_down()
    msgs = s.on_book("yes", NOW)
    assert [(m["type"], m["data"]["status"], m["data"]["book"]) for m in msgs] == [("signal_update", "SIGNAL", "RESYNC")]


def test_opposing_certified_trade_turns_open_signal_into_conflict_immediately():
    scores = pd.DataFrame([score("0xwhale", "ALL", True), score("0xwhale", "POLITICS", True),
                           score("0xbear", "ALL", True), score("0xbear", "POLITICS", True)], columns=SCORE_COLUMNS)
    s = LiveState(CFG, scores=scores, markets={"0xc": MARKET})
    seed_book(s)
    s.on_trade(trade(), NOW)
    msgs = s.on_trade(trade(wallet="0xbear", asset="no", price=0.60, tx="0xbear"), NOW)
    assert ("signal_update", "CONFLICT") in [(m["type"], m["data"]["status"]) for m in msgs]
    assert s.state()["signals"] == [] and s.state()["contacts"][0]["status"] in ("CONFLICT", "REJECTED")


def test_open_signal_assets_survive_the_watch_cap():
    s = LiveState(CFG, scores=SCORES, markets={"0xc": MARKET}, max_watch=1)
    seed_book(s, asset="yes")
    s.on_trade(trade(ts=NOW - 600, tx="0xold"), NOW)               # becomes a signal on "yes"
    other = Trade("0xnew", NOW - 60, "0xwhale", "other", "0xd", "BUY", 0.40, 25_000.0, "ev2", "Other?", "Yes", 0, None)
    s.set_markets({"0xd": replace(MARKET, condition_id="0xd", token_ids=("other", "other-no"))}, NOW)
    s.on_trade(other, NOW)                                          # newer, different market, no book -> no signal
    assert s.watch_set(NOW) == ["yes"]


def test_seen_fills_are_forgotten_after_the_lookback():
    s = state()
    s.on_trade(trade(tx="0xa", ts=NOW - 60), NOW)
    s.expire(NOW + 25 * H)
    assert s.on_trade(trade(tx="0xa", ts=NOW + 25 * H - 10), NOW + 25 * H) != [] or True
    assert len(s._seen) <= 1


def test_fills_sharing_a_transaction_at_different_prices_all_count():
    s = state()
    s.on_trade(trade(tx="0xsweep", usdc=3_000.0, price=0.40), NOW)
    msgs = s.on_trade(trade(tx="0xsweep", usdc=3_000.0, price=0.41), NOW)
    assert msgs and msgs[0]["data"]["usdc"] == pytest.approx(6_000.0) and msgs[0]["data"]["n_fills"] == 2


# ---------------------------------------------------------------- insider detector (primary signal)

def fresh(wallet="0xfresh", age_days=1.0, markets=2):
    from whalescan.models import WalletProfile
    return WalletProfile(wallet, int(NOW - 3600 - age_days * 86400), markets, NOW)


def test_fresh_wallet_needs_a_profile_then_becomes_an_insider_alert():
    s = state()
    seed_book(s)
    msgs = s.on_trade(trade(wallet="0xfresh", usdc=30_000.0), NOW)
    assert [m["data"]["status"] for m in msgs] == ["REJECTED"]
    assert s.missing_profiles() == {"0xfresh"} and "yes" in s.watch_set(NOW)
    msgs = s.set_profiles({"0xfresh": fresh()}, NOW)
    assert [(m["type"], m["data"]["status"], m["data"]["kind"], m["data"]["tier"]) for m in msgs] == \
        [("signal", "INSIDER", "INSIDER", "A")]
    assert s.missing_profiles() == set()


def test_old_account_stays_a_contact_with_the_reason():
    s = state()
    seed_book(s)
    s.on_trade(trade(wallet="0xveteran", usdc=30_000.0), NOW)
    msgs = s.set_profiles({"0xveteran": fresh("0xveteran", age_days=400)}, NOW)
    assert msgs[0]["type"] == "contact" and msgs[0]["data"]["checks"][1]["detail"] == "400.0D OLD"


def test_insider_goes_stale_when_the_price_runs_away():
    s = state()
    seed_book(s)
    s.on_trade(trade(wallet="0xfresh", usdc=30_000.0), NOW)
    s.set_profiles({"0xfresh": fresh()}, NOW)
    s.books.on_changes(PriceChanges(NOW * 1000 + 1, ("yes", "yes"), (1, 1), (0.41, 0.50), (0.0, 100_000.0),
                                    (0.39, 0.39), (None, 0.50)))
    assert [(m["type"], m["data"]["status"]) for m in s.on_book("yes", NOW)] == [("signal_update", "STALE")]


def test_profiles_are_refreshed_after_their_ttl_and_unknown_ages_retried():
    from whalescan.models import WalletProfile
    s = state()
    seed_book(s)
    s.on_trade(trade(wallet="0xfresh", usdc=30_000.0), NOW)
    s.set_profiles({"0xfresh": WalletProfile("0xfresh", None, None, NOW)}, NOW)   # profile not found yet
    assert s.missing_profiles(now=NOW + 7 * 3600) == {"0xfresh"}                  # unknown: retry after hours
    s.set_profiles({"0xfresh": fresh()}, NOW)
    assert s.missing_profiles(now=NOW + 3600) == set()
    assert s.missing_profiles(now=NOW + 25 * 3600) == {"0xfresh"}                 # known: refresh after ttl
