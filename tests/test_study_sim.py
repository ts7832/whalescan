from dataclasses import replace

import pandas as pd
import pytest

from whalescan.config import load_config
from whalescan.study_sim import Policy, bootstrap_paths, simulate

SC = load_config().study
DAY = 86400
NO_CAPS = replace(SC, bankroll=1000, max_bet_fraction=1.0, max_event_fraction=1.0, max_exposure_fraction=1.0,
                  max_whale_share=1.0)


def bet(entry, resolved, cost=0.5, payout=1.0, event="e1", asset="a1", whale=1e9, p=0.6):
    return {"entry_ts": entry, "resolved_ts": resolved, "cost": cost, "payout": payout, "whale_usdc": whale,
            "event_slug": event, "asset": asset, "p": p}


def df(*rows):
    return pd.DataFrame(list(rows))


FLAT_100 = Policy("flat", "flat", 0.10)  # 10% of the starting bankroll per bet = $100


def test_a_winning_flat_bet_pays_shares_times_payout():
    r = simulate(df(bet(0, DAY, cost=0.5, payout=1.0)), FLAT_100, NO_CAPS)
    assert r.final == pytest.approx(1000 - 100 + 200)
    assert r.taken == 1


def test_money_in_an_open_position_cannot_be_spent_until_it_settles():
    policy = Policy("big", "flat", 0.70)  # $700 per bet
    bets = df(bet(0, 10 * DAY, asset="a"), bet(DAY, 11 * DAY, asset="b"))  # the second arrives before the first pays
    r = simulate(bets, policy, NO_CAPS)
    assert r.taken == 2 and r.stakes == [pytest.approx(700), pytest.approx(300)]  # only $300 was left


def test_a_settled_position_frees_its_cash_for_later_bets():
    policy = Policy("big", "flat", 0.70)
    bets = df(bet(0, DAY, asset="a", payout=0.0), bet(2 * DAY, 3 * DAY, asset="b"))
    r = simulate(bets, policy, NO_CAPS)
    assert r.stakes == [pytest.approx(700), pytest.approx(300)]  # the loss left $300; nothing more to spend


def test_caps_limit_each_bet_each_event_total_exposure_and_the_whale_share():
    cfg = replace(NO_CAPS, max_bet_fraction=0.05, max_event_fraction=0.08, max_exposure_fraction=0.10,
                  max_whale_share=0.5)
    policy = Policy("greedy", "fraction", 1.0)
    bets = df(bet(0, 9 * DAY, event="e1", asset="a"), bet(1, 9 * DAY, event="e1", asset="b"),
              bet(2, 9 * DAY, event="e2", asset="c", whale=40), bet(3, 9 * DAY, event="e3", asset="d"))
    r = simulate(bets, policy, cfg)
    # 50 (bet cap) · 30 (event e1 cap 80) · 20 (half of the $40 whale) · 0 (total exposure cap 100 reached)
    assert r.stakes == [pytest.approx(50), pytest.approx(30), pytest.approx(20)] and r.skipped_caps == 1


def test_kelly_stakes_nothing_without_an_edge():
    bets = df(bet(0, DAY, cost=0.6, p=0.6), bet(1, DAY, cost=0.6, p=0.5, asset="b"))
    r = simulate(bets, Policy("kelly", "kelly", 0.25), NO_CAPS)
    assert r.taken == 0 and r.final == pytest.approx(1000)


def test_kelly_stakes_a_quarter_of_the_growth_optimal_fraction():
    r = simulate(df(bet(0, DAY, cost=0.4, p=0.6)), Policy("kelly", "kelly", 0.25), NO_CAPS)
    assert r.stakes == [pytest.approx(1000 * 0.25 * (0.6 - 0.4) / 0.6)]


def test_first_only_skips_a_second_wallet_on_the_same_outcome_but_add_on_takes_it():
    bets = df(bet(0, DAY, asset="a"), bet(1, DAY, asset="a"))
    assert simulate(bets, Policy("first", "flat", 0.1, aggregation="first"), NO_CAPS).taken == 1
    assert simulate(bets, Policy("add", "flat", 0.1, aggregation="add"), NO_CAPS).taken == 2


def test_max_drawdown_is_measured_from_the_peak():
    bets = df(bet(0, DAY, asset="a", payout=1.0), bet(2 * DAY, 3 * DAY, asset="b", payout=0.0))
    r = simulate(bets, FLAT_100, NO_CAPS)
    assert r.final == pytest.approx(1100 - 100)
    assert r.max_drawdown == pytest.approx(100 / 1100)


def test_bootstrap_paths_are_reproducible_and_summarised():
    bets = df(*[bet(i * DAY, i * DAY + 3 * DAY, asset=f"a{i}", payout=float(i % 3 != 0)) for i in range(40)])
    a = bootstrap_paths(bets, FLAT_100, NO_CAPS, n=50, seed=4)
    b = bootstrap_paths(bets, FLAT_100, NO_CAPS, n=50, seed=4)
    assert a == b
    assert set(a) >= {"median_final", "p5_final", "median_max_drawdown", "p95_max_drawdown", "p_loss"}
    assert a["p5_final"] <= a["median_final"]
