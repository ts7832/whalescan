import math

import pytest

from whalescan.config import load_config
from whalescan.history_store import HistoryStore
from whalescan.models import Market, Trade, WalletProfile
from whalescan.study import bets_frame

NOW = 1_790_000_000
DAY, MIN = 86400, 60
CFG = load_config()
T = NOW - 30 * DAY  # bet time


def mk(cid, prices=(1.0, 0.0), closed=True):
    return Market(cid, f"Q {cid}", f"s-{cid}", f"ev-{cid}", NOW - 10 * DAY, closed, NOW - 10 * DAY if closed else None,
                  prices, (f"{cid}-y", f"{cid}-n"), False, 0.0, 1.0, 1e6, ("Politics",))


def fill(tx, ts, wallet, cid, usdc, price=0.40, idx=0):
    return Trade(tx, ts, wallet, f"{cid}-{'y' if idx == 0 else 'n'}", cid, "BUY", price, usdc / price, f"ev-{cid}",
                 "q", "Yes" if idx == 0 else "No", idx, None)


@pytest.fixture
def store(tmp_path):
    with HistoryStore(tmp_path / "h.duckdb") as h:
        h.upsert_markets([mk("0xpol"), mk("0xvoid", prices=(0.5, 0.5)), mk("0xopen", prices=(0.4, 0.6), closed=False),
                          mk("0xelse")], NOW)
        h.register_markets(["0xpol", "0xvoid"], is_open=False)
        h.register_markets(["0xopen"], is_open=True)
        yield h


def row(df, wallet, cid="0xpol"):
    r = df[(df["wallet"] == wallet) & (df["condition_id"] == cid)]
    assert len(r) == 1, r
    return r.iloc[0]


def test_rules_come_from_the_production_insider_logic_with_facts_as_of_the_bet(store):
    store.upsert_trades([fill("a", T, "0xfresh", "0xpol", 30_000), fill("b", T, "0xnear", "0xpol", 30_000),
                         fill("c", T, "0xold", "0xpol", 30_000),
                         fill("h1", T - DAY, "0xfresh", "0xelse", 50)])  # one earlier market in its own history
    store.upsert_profiles([WalletProfile("0xfresh", T - 2 * DAY, 999, NOW),  # today's count must be ignored
                           WalletProfile("0xnear", T - 10 * DAY, None, NOW),
                           WalletProfile("0xold", T - 900 * DAY, None, NOW)])
    store.mark_wallet_history("0xfresh", NOW)
    store.mark_wallet_history("0xnear", NOW)
    df = bets_frame(store, CFG)
    fresh, near, old = row(df, "0xfresh"), row(df, "0xnear"), row(df, "0xold")
    assert fresh["markets_at_bet"] == 2  # 0xelse before, plus this market (the live rule counts it too)
    assert fresh["r_insider"] and fresh["r_fresh"] and fresh["r_baseline"] and not fresh["r_near_miss"]
    assert near["r_near_miss"] and near["missed_rule"] == "I2" and not near["r_insider"] and near["r_fresh"]
    assert old["r_baseline"] and not old["r_fresh"] and not old["r_insider"]


def test_later_trades_never_leak_into_the_bet(store):
    store.upsert_trades([fill("a", T, "0xw", "0xpol", 30_000)] +
                        [fill(f"l{i}", T + DAY + i, "0xw", f"0xlater{i}", 50) for i in range(20)])
    store.upsert_profiles([WalletProfile("0xw", T - DAY, None, NOW)])
    store.mark_wallet_history("0xw", NOW)
    r = row(bets_frame(store, CFG), "0xw")
    assert r["markets_at_bet"] == 1 and r["r_insider"]


def test_unknown_markets_traded_fails_the_insider_rule(store):
    store.upsert_trades([fill("a", T, "0xw", "0xpol", 30_000)])
    store.upsert_profiles([WalletProfile("0xw", T - DAY, None, NOW)])  # young, but its history was never fetched
    r = row(bets_frame(store, CFG), "0xw")
    assert math.isnan(r["markets_at_bet"]) and not r["r_insider"] and r["r_fresh"]


def test_an_account_created_after_its_first_fill_is_not_fresh(store):
    store.upsert_trades([fill("a", T, "0xw", "0xpol", 30_000)])
    store.upsert_profiles([WalletProfile("0xw", T + 5 * DAY, None, NOW)])
    r = row(bets_frame(store, CFG), "0xw")
    assert math.isnan(r["age_days"]) and not r["r_fresh"] and not r["r_insider"]


def test_entry_is_priced_after_the_delay_and_the_return_is_per_dollar(store):
    store.upsert_trades([fill("a", T, "0xw", "0xpol", 30_000, price=0.40)])
    store.upsert_profiles([WalletProfile("0xw", T - 900 * DAY, None, NOW)])
    store.upsert_prices("0xpol-y", [(T + 30, 0.40), (T + 4 * MIN, 0.42), (T + 14 * MIN, 0.60)])
    r = row(bets_frame(store, CFG), "0xw")
    cost5 = 0.42 + CFG.study.half_spread  # the last print known 5 minutes after the signal (fees off here)
    assert r["entry_5"] == pytest.approx(cost5)
    assert r["ret_5"] == pytest.approx((1.0 - cost5) / cost5)  # the market resolved YES
    assert math.isnan(r["entry_15"]) and math.isnan(r["ret_15"])  # 0.60 ran past the chase limit: skipped
    assert r["payout"] == 1.0 and r["won"]


def test_a_bet_with_no_price_record_is_unpriced_not_a_loss(store):
    store.upsert_trades([fill("a", T, "0xw", "0xpol", 30_000)])
    store.upsert_profiles([WalletProfile("0xw", T - 900 * DAY, None, NOW)])
    r = row(bets_frame(store, CFG), "0xw")
    assert not r["priced"] and math.isnan(r["ret_5"])


def test_irregular_and_open_markets_are_never_scored(store):
    store.upsert_trades([fill("a", T, "0xw", "0xvoid", 30_000), fill("b", T, "0xw", "0xopen", 30_000)])
    store.upsert_profiles([WalletProfile("0xw", T - 900 * DAY, None, NOW)])
    store.upsert_prices("0xvoid-y", [(T + 30, 0.40)])
    store.upsert_prices("0xopen-y", [(T + 30, 0.40)])
    df = bets_frame(store, CFG)
    void, opened = row(df, "0xw", "0xvoid"), row(df, "0xw", "0xopen")
    assert void["irregular"] and math.isnan(void["ret_5"])
    assert opened["is_open"] and math.isnan(opened["ret_5"])  # still counted as a bet, but not scored


def test_same_side_crowd_counts_only_wallets_that_bet_earlier(store):
    store.upsert_trades([fill("a", T - 3600, "0xfirst", "0xpol", 20_000), fill("b", T, "0xme", "0xpol", 20_000),
                         fill("c", T + 3600, "0xlater", "0xpol", 20_000),
                         fill("d", T - 3600, "0xother_side", "0xpol", 20_000, idx=1)])
    store.upsert_profiles([WalletProfile(w, T - 900 * DAY, None, NOW) for w in ("0xfirst", "0xme", "0xlater",
                                                                             "0xother_side")])
    df = bets_frame(store, CFG)
    assert row(df, "0xme")["crowd_before"] == 1
    assert row(df, "0xfirst")["crowd_before"] == 0


def test_days_held_run_from_entry_to_resolution(store):
    store.upsert_trades([fill("a", T, "0xw", "0xpol", 30_000)])
    store.upsert_profiles([WalletProfile("0xw", T - 900 * DAY, None, NOW)])
    store.upsert_prices("0xpol-y", [(T + 30, 0.40)])
    r = row(bets_frame(store, CFG), "0xw")
    assert r["days_held_5"] == pytest.approx((NOW - 10 * DAY - (T + 5 * MIN)) / DAY)


def test_days_to_end_uses_the_scheduled_end_known_at_the_bet_not_the_actual_resolution(store):
    # "Will X happen by <date>?" markets resolve EARLY mostly when X happens: filtering on the actual resolution
    # time would quietly select winners. A horizon rule may only use the scheduled end date.
    from dataclasses import replace as _replace
    early = _replace(mk("0xearly"), end_ts=T + 60 * DAY, closed_ts=T + 2 * DAY)
    store.upsert_markets([early], NOW)
    store.register_markets(["0xearly"], is_open=False)
    store.upsert_trades([fill("a", T, "0xw", "0xearly", 30_000)])
    store.upsert_profiles([WalletProfile("0xw", T - 900 * DAY, None, NOW)])
    store.upsert_prices("0xearly-y", [(T + 30, 0.40)])
    r = row(bets_frame(store, CFG), "0xw", "0xearly")
    assert r["days_to_end"] == pytest.approx(60.0)
    assert r["days_held_5"] == pytest.approx((2 * DAY - 5 * MIN) / DAY)  # capital lock-up still uses reality


def test_small_buys_are_not_bets(store):
    store.upsert_trades([fill("a", T, "0xsmall", "0xpol", 2_000)])
    df = bets_frame(store, CFG)
    assert df.empty or "0xsmall" not in set(df["wallet"])


def test_no_entry_after_the_market_has_already_resolved(store):
    from dataclasses import replace as _replace
    fast = _replace(mk("0xfast"), end_ts=T + 3 * DAY, closed_ts=T + 10 * MIN)  # resolved 10 min after the signal
    store.upsert_markets([fast], NOW)
    store.register_markets(["0xfast"], is_open=False)
    store.upsert_trades([fill("a", T, "0xw", "0xfast", 30_000)])
    store.upsert_profiles([WalletProfile("0xw", T - 900 * DAY, None, NOW)])
    store.upsert_prices("0xfast-y", [(T + 30, 0.40), (T + 20 * MIN, 0.99)])
    r = row(bets_frame(store, CFG), "0xw", "0xfast")
    assert r["entry_5"] == r["entry_5"]  # 5 min: still open, a real entry
    assert math.isnan(r["entry_15"]) and math.isnan(r["ret_15"])  # 15 min: the outcome was already known
