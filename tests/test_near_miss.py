from whalescan.classify import Blocklist
from whalescan.config import load_config
from whalescan.gate import PositionEvent
from whalescan.insider import evaluate_insider, near_miss_rule
from whalescan.models import Market, WalletProfile

CFG = load_config()
IC, LC = CFG.insider, CFG.ledger
BL = Blocklist(CFG.blocklist)
NOW = 1_790_000_000
D, H = 86400, 3600
MARKET = Market("0xc", "q", "s", "ev", NOW + 30 * D, False, None, (0.3, 0.7), ("yes", "no"), True, 0.05, 1.0,
                5e6, ("Politics",))


def ev(usdc=30_000.0, price=0.30, first_ts=NOW - H):
    return PositionEvent("0xnew", "yes", "0xc", "BUY", first_ts, first_ts + 60, usdc, usdc / price, price, 2, "ev",
                         "q", "Yes", 0)


def prof(age_days=None, markets=2):
    created = None if age_days is None else int(NOW - H - age_days * D)
    return WalletProfile("0xnew", created, markets, NOW)


def run(e=None, p=None, quote_ok=True):
    from whalescan.book import FollowQuote
    q = FollowQuote(0.31, True, NOW, 0.29, 0.31, 0.30, ((0.31, 1e5),)) if quote_ok else None
    return evaluate_insider(e or ev(), p, MARKET, "POLITICS", IC, BL, q, NOW)


def rule(e=None, p=None, quote_ok=True):
    return near_miss_rule(run(e, p, quote_ok), p, IC, LC)


def test_age_just_over_the_line_is_a_near_miss():
    # IC.max_age_days = 7 (config); a wallet at 10 days is within near_miss_age_max_days (30)
    assert rule(p=prof(age_days=10)) == "I2"


def test_age_at_the_far_band_edge_is_still_a_near_miss():
    assert rule(p=prof(age_days=LC.near_miss_age_max_days)) == "I2"


def test_age_past_the_far_band_edge_is_not_a_near_miss():
    assert rule(p=prof(age_days=LC.near_miss_age_max_days + 0.5)) is None


def test_age_within_the_pass_band_is_not_a_near_miss():
    assert rule(p=prof(age_days=IC.max_age_days - 1)) is None  # this actually passes I2 -> not a miss at all


def test_markets_just_over_the_line_is_a_near_miss():
    assert rule(p=prof(age_days=1, markets=IC.max_markets + 5)) == "I3"


def test_markets_at_the_far_band_edge_is_still_a_near_miss():
    assert rule(p=prof(age_days=1, markets=LC.near_miss_markets_max)) == "I3"


def test_markets_past_the_far_band_edge_is_not_a_near_miss():
    assert rule(p=prof(age_days=1, markets=LC.near_miss_markets_max + 1)) is None


def test_usdc_just_under_the_line_is_a_near_miss():
    assert rule(e=ev(usdc=IC.min_usdc - 1), p=prof(age_days=1)) == "I4"


def test_usdc_at_the_far_band_edge_is_still_a_near_miss():
    assert rule(e=ev(usdc=LC.near_miss_usdc_min), p=prof(age_days=1)) == "I4"


def test_usdc_below_the_far_band_edge_is_not_a_near_miss():
    assert rule(e=ev(usdc=LC.near_miss_usdc_min - 1), p=prof(age_days=1)) is None


def test_two_simultaneous_failures_are_not_a_near_miss():
    assert rule(e=ev(usdc=IC.min_usdc - 1), p=prof(age_days=10)) is None


def test_price_moved_beyond_slippage_is_not_a_near_miss():
    from whalescan.book import FollowQuote
    q = FollowQuote(0.30 + IC.max_slippage + 0.10, True, NOW, 0.29, 0.42, 0.40, ((0.42, 1e5),))
    result = near_miss_rule(evaluate_insider(ev(), prof(age_days=10), MARKET, "POLITICS", IC, BL, q, NOW),
                            prof(age_days=10), IC, LC)
    assert result is None


def test_no_book_is_still_a_near_miss():
    assert rule(p=prof(age_days=10), quote_ok=False) == "I2"


def test_unknown_age_or_markets_is_not_a_near_miss():
    assert rule(p=prof(age_days=None, markets=2)) is None
    assert rule(p=None) is None


def test_a_full_pass_is_not_a_near_miss():
    assert rule(p=prof(age_days=1, markets=2)) is None


def test_a_sports_market_is_never_a_near_miss():
    result = near_miss_rule(evaluate_insider(ev(), prof(age_days=10), MARKET, "SPORTS", IC, BL, None, NOW),
                            prof(age_days=10), IC, LC)
    assert result is None
