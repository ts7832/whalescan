import pytest

from whalescan.classify import Blocklist
from whalescan.config import load_config
from whalescan.finder_math import is_covered, is_price_market, known_at, move_hit, move_target
from whalescan.models import Market

CFG = load_config()
FC = CFG.finder
BL = Blocklist(CFG.blocklist)
NOW = 1_790_000_000
DAY = 86400


def mk(question, *, tags=("Politics",), volume=1e6, closed=False, end_ts=NOW + 30 * DAY, closed_ts=None):
    return Market("0xc", question, "s", "ev", end_ts, closed, closed_ts, (0.4, 0.6), ("y", "n"), False, 0.0, 1.0,
                  volume, tags)


# --- is_price_market -------------------------------------------------------

@pytest.mark.parametrize("question,expected", [
    ("Will MicroStrategy (MSTR) hit (HIGH) $125 Week of August 17 2026?", True),
    ("Will Bitcoin price close above $150,000 by 2027?", True),
    ("Will ETH trade below $2,000 in September?", True),
    ("Bitcoin Up or Down - September 30, 8AM ET", True),
    ("Will MicroStrategy announce a Bitcoin purchase August 25-31?", False),
    ("Will Roberto Sánchez Palomino win the 2026 Peruvian presidential election?", False),
    ("Clarity Act (H.R.3633) signed into law in 2026?", False),
])
def test_price_threshold_questions_are_recognised(question, expected):
    assert is_price_market(question) is expected


def test_price_market_check_is_case_insensitive():
    assert is_price_market("will btc REACH $200k this year?")


# --- is_covered --------------------------------------------------------------

def test_sports_markets_are_never_covered():
    assert not is_covered(mk("Falcons vs. Packers", tags=("Sports",)), CFG, BL)


def test_price_threshold_markets_are_never_covered_even_if_tagged_crypto():
    assert not is_covered(mk("Will BTC hit (HIGH) $150k?", tags=("Crypto",)), CFG, BL)


def test_a_low_volume_market_is_not_covered():
    assert not is_covered(mk("Will X happen?", volume=1.0), CFG, BL)


def test_a_blocklisted_market_is_not_covered():
    m = Market("0xc", "Will BTC be up or down at 3pm?", "btc-updown-15m", "ev", NOW + DAY, False, None, (0.4, 0.6),
              ("y", "n"), False, 0.0, 1.0, 1e6, ("Crypto",))
    assert not is_covered(m, CFG, BL)


def test_a_normal_non_sports_market_with_volume_is_covered():
    assert is_covered(mk("Will MicroStrategy announce a Bitcoin purchase?", tags=("Crypto",)), CFG, BL)
    assert is_covered(mk("Will X win the election?", tags=("Politics",)), CFG, BL)


# --- move_target -------------------------------------------------------------

def test_move_target_is_20_points_when_that_is_closer_to_one():
    assert move_target(0.25, FC) == pytest.approx(0.45)


def test_move_target_is_half_way_to_one_when_that_is_closer():
    assert move_target(0.90, FC) == pytest.approx(0.95)


def test_move_target_never_exceeds_one():
    assert move_target(0.99, FC) <= 1.0


# --- known_at ------------------------------------------------------------

def test_known_at_is_24h_after_the_signal_by_default():
    assert known_at(NOW, None, FC) == NOW + 24 * 3600


def test_known_at_is_capped_by_an_earlier_market_close():
    assert known_at(NOW, NOW + 3 * 3600, FC) == NOW + 3 * 3600


def test_known_at_ignores_a_later_market_close():
    assert known_at(NOW, NOW + 100 * 3600, FC) == NOW + 24 * 3600


# --- move_hit --------------------------------------------------------------

def test_a_point_reaching_the_target_inside_the_window_is_a_hit():
    points = [(NOW + 3600, 0.30), (NOW + 7200, 0.46)]
    assert move_hit(points, NOW, 0.25, None, FC) is True


def test_no_point_reaching_the_target_is_a_clean_miss():
    points = [(NOW + 3600, 0.30), (NOW + 20 * 3600, 0.40)]
    assert move_hit(points, NOW, 0.25, None, FC) is False


def test_a_point_reaching_the_target_after_the_window_does_not_count():
    points = [(NOW + 20 * 3600, 0.30), (NOW + 25 * 3600, 0.50)]  # the hit is past the 24h window
    assert move_hit(points, NOW, 0.25, None, FC) is False


def test_the_window_is_cut_by_an_earlier_market_close():
    # the only hitting point comes after the market closed at +3h: must not count
    points = [(NOW + 2 * 3600, 0.30), (NOW + 5 * 3600, 0.50)]
    assert move_hit(points, NOW, 0.25, NOW + 3 * 3600, FC) is False


def test_a_hit_before_an_earlier_market_close_still_counts():
    points = [(NOW + 1 * 3600, 0.50)]
    assert move_hit(points, NOW, 0.25, NOW + 3 * 3600, FC) is True


def test_no_recorded_points_is_unknown_not_a_miss():
    assert move_hit([], NOW, 0.25, None, FC) is None


def test_a_point_at_or_before_the_signal_time_does_not_count():
    points = [(NOW, 0.60), (NOW - 10, 0.90)]
    assert move_hit(points, NOW, 0.25, None, FC) is None
