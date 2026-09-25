from dataclasses import replace

import pytest

from whalescan.config import load_config
from whalescan.ledger_math import checkpoint_return, checkpoint_schedule, entry_cost, final_return
from whalescan.models import Market

CFG = load_config().ledger
DAY = 86400
NOW = 1_790_000_000

MARKET = Market("0xc", "q", "s", "ev", NOW + 60 * DAY, False, None, (0.4, 0.6), ("yes", "no"), True, 0.05, 1.0,
                1e6, ("Politics",))
NO_FEE_MARKET = replace(MARKET, fees_enabled=False)


def test_checkpoint_schedule_days_7_14_21_28_then_monthly():
    sched = checkpoint_schedule(NOW, NOW + 100 * DAY, CFG)
    days = [round((t - NOW) / DAY, 3) for t in sched]
    assert days == [7, 14, 21, 28, 58, 88]


def test_checkpoint_schedule_stops_at_the_market_end():
    sched = checkpoint_schedule(NOW, NOW + 10 * DAY, CFG)
    days = [round((t - NOW) / DAY, 3) for t in sched]
    assert days == [7]


def test_checkpoint_schedule_with_no_end_date_runs_monthly_for_up_to_3_years():
    sched = checkpoint_schedule(NOW, None, CFG)
    days = [round((t - NOW) / DAY, 1) for t in sched]
    assert days[:4] == [7, 14, 21, 28]
    assert days[-1] <= 3 * 365
    assert all(b - a == pytest.approx(30, abs=0.1) for a, b in zip(days[4:], days[5:]))


def test_checkpoint_schedule_switches_to_quarterly_past_3_years_out():
    end = NOW + 4 * 365 * DAY
    sched = checkpoint_schedule(NOW, end, CFG)
    days = [round((t - NOW) / DAY, 1) for t in sched]
    assert days[:4] == [7, 14, 21, 28]
    three_year_mark = 3 * 365
    monthly = [d for d in days if d <= three_year_mark]
    quarterly = [d for d in days if d > three_year_mark]
    assert len(monthly) > len(quarterly) > 0
    assert all(b - a == pytest.approx(91, abs=0.1) for a, b in zip(quarterly, quarterly[1:]))
    assert days[-1] <= (end - NOW) / DAY


def test_entry_cost_adds_the_taker_fee():
    # verified formula (spec): rate * (p(1-p))^exponent, fee=0.05 exponent=1
    vwap = 0.40
    fee = 0.05 * (0.40 * 0.60)
    assert entry_cost(vwap, MARKET) == pytest.approx(vwap + fee)


def test_entry_cost_is_just_vwap_when_fees_disabled():
    assert entry_cost(0.40, NO_FEE_MARKET) == pytest.approx(0.40)


def test_checkpoint_return_sells_into_the_best_bid_minus_fee():
    cost = entry_cost(0.40, MARKET)
    bid = 0.50
    fee = 0.05 * (0.50 * 0.50)
    expected = (bid - fee - cost) / cost
    assert checkpoint_return(bid, cost, MARKET) == pytest.approx(expected)


def test_checkpoint_return_is_none_when_there_is_no_bid():
    cost = entry_cost(0.40, MARKET)
    assert checkpoint_return(None, cost, MARKET) is None
    assert checkpoint_return(0.0, cost, MARKET) is None


def test_final_return_from_payout():
    cost = entry_cost(0.40, MARKET)
    assert final_return(1.0, cost) == pytest.approx((1.0 - cost) / cost)
    assert final_return(0.0, cost) == pytest.approx(-1.0)
