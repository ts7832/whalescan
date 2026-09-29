from dataclasses import replace

import pytest

from whalescan.config import load_config
from whalescan.models import Market
from whalescan.study_math import copy_entry, copy_return, kelly_fraction, shrink

CFG = load_config()
IC, SC = CFG.insider, CFG.study
MARKET = Market("0xc", "q", "s", "ev", None, True, None, (1.0, 0.0), ("y", "n"), True, 0.05, 1.0, 1e6, ("Politics",))
NO_FEE = replace(MARKET, fees_enabled=False)


def test_entry_adds_half_spread_and_the_taker_fee():
    e = copy_entry(0.40, 0.39, MARKET, IC, SC)
    price = 0.40 + SC.half_spread
    assert e.price == pytest.approx(price)
    assert e.fee == pytest.approx(0.05 * price * (1 - price))
    assert e.cost == pytest.approx(e.price + e.fee)


def test_entry_is_skipped_when_the_price_ran_past_the_chase_limit():
    # the whale bought at 0.30; five minutes later it is 0.40 -> 10c + spread above: do not chase
    assert copy_entry(0.40, 0.30, NO_FEE, IC, SC) is None


def test_entry_just_inside_the_chase_limit_is_taken():
    whale = 0.30
    after = whale + IC.max_slippage - SC.half_spread
    assert copy_entry(after, whale, NO_FEE, IC, SC) is not None


def test_entry_is_skipped_outside_the_price_band():
    assert copy_entry(0.95, 0.95, NO_FEE, IC, SC) is None  # above price_max: no upside left to buy
    assert copy_entry(0.005, 0.005, NO_FEE, IC, SC) is None


def test_entry_is_skipped_without_a_post_bet_price():
    assert copy_entry(None, 0.30, NO_FEE, IC, SC) is None


def test_a_falling_price_after_the_whale_is_fine_to_buy():
    assert copy_entry(0.25, 0.30, NO_FEE, IC, SC).price == pytest.approx(0.25 + SC.half_spread)


def test_return_is_per_dollar_not_per_share():
    e = copy_entry(0.89, 0.89, NO_FEE, IC, SC)  # cost 0.90 per share
    assert copy_return(1.0, e) == pytest.approx(0.10 / 0.90)
    assert copy_return(0.0, e) == pytest.approx(-1.0)


def test_kelly_is_zero_without_an_edge_and_matches_the_formula_with_one():
    assert kelly_fraction(0.40, 0.40) == 0.0
    assert kelly_fraction(0.30, 0.40) == 0.0
    assert kelly_fraction(0.60, 0.40) == pytest.approx((0.60 - 0.40) / 0.60)
    assert kelly_fraction(0.99, 1.0) == 0.0  # nothing to win


def test_shrink_moves_a_small_sample_toward_the_market_price():
    assert shrink(0.9, 0, 0.5, 20) == pytest.approx(0.5)
    assert shrink(0.9, 20, 0.5, 20) == pytest.approx(0.7)
    assert shrink(0.9, 10_000, 0.5, 20) == pytest.approx(0.9, abs=1e-3)
