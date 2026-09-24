from dataclasses import replace

from whalescan.book import FollowQuote
from whalescan.classify import Blocklist
from whalescan.config import load_config
from whalescan.gate import PositionEvent
from whalescan.insider import evaluate_insider, is_insider_candidate
from whalescan.models import Market, WalletProfile

CFG = load_config()
NOW = 1_790_000_000
D, H = 86400, 3600
BL = Blocklist(CFG.blocklist)
POLITICS = Market("0xc", "Will X resign?", "x-resign", "x-resign", NOW + 30 * D, False, None, (0.3, 0.7),
                  ("yes", "no"), True, 0.05, 1.0, 5e6, ("Politics",))


def ev(usdc=30_000.0, price=0.30, first_ts=NOW - H, side="BUY", wallet="0xnew"):
    return PositionEvent(wallet, "yes", "0xc", side, first_ts, first_ts + 60, usdc, usdc / price, price, 2, "x-resign",
                         "Will X resign?", "Yes", 0)


def prof(age_days=1.0, markets=2, at=NOW - H):
    return WalletProfile("0xnew", int(at - age_days * D), markets, NOW)


def q(vwap=0.31, complete=True):
    return FollowQuote(vwap, complete, NOW, 0.29, 0.31, 0.30, ((0.31, 1e5),))


def run(e=None, p=None, m=POLITICS, quote=None, cat="POLITICS"):
    return evaluate_insider(e or ev(), p if p is not None else prof(), m, cat, CFG.insider, BL, quote if quote is not None else q(), NOW)


def codes(r):
    return {c.code: c.passed for c in r.checks}


def test_fresh_big_concentrated_news_bet_is_a_tier_a_insider():
    r = run()
    assert r.status == "INSIDER" and r.tier == "A", r.checks
    assert r.checks[1].detail == "1.0D OLD" and r.checks[2].detail == "2 MARKETS"
    assert abs(r.max_entry - 0.35) < 1e-12 and r.post_edge is None


def test_older_or_smaller_is_tier_b():
    assert run(p=prof(age_days=5)).tier == "B"
    assert run(e=ev(usdc=15_000)).tier == "B"


def test_age_is_measured_at_the_first_fill_not_now():
    old_trade = ev(first_ts=NOW - 20 * D)
    p = WalletProfile("0xnew", NOW - 21 * D, 2, NOW)       # 1 day old when it bet, 21 days old today
    assert run(e=old_trade, p=p).checks[1].passed


def test_each_rule_can_reject():
    assert not codes(run(p=prof(age_days=30)))["I2"]
    assert run(p=WalletProfile("0xnew", None, 2, NOW)).checks[1].detail == "AGE UNKNOWN"
    assert not codes(run(p=prof(markets=40)))["I3"]
    assert not codes(run(e=ev(usdc=4_000)))["I4"]
    assert run(cat="SPORTS").checks[4].detail == "NOT A NEWS MARKET"
    assert run(m=None).checks[4].detail == "UNKNOWN MARKET"
    assert run(m=replace(POLITICS, closed=True)).checks[4].detail == "MARKET CLOSED"
    assert run(e=ev(price=0.97), quote=q(vwap=0.975)).checks[5].detail.startswith("PRICE 0.970")
    r = run(quote=q(vwap=0.40))
    assert r.status == "REJECTED" and r.checks[5].detail == "PRICE MOVED +0.100"


def test_no_book_is_reported_so_the_caller_can_fetch_one():
    r = run(quote=FollowQuote(float("nan"), False, NOW, None, None, None, ()))
    assert r.checks[5].detail == "BOOK TOO THIN"
    r = evaluate_insider(ev(), prof(), POLITICS, "POLITICS", CFG.insider, BL, None, NOW)
    assert r.checks[5].detail == "NO BOOK"


def test_sell_is_an_exit():
    assert run(e=ev(side="SELL")).status == "EXIT"


def test_candidate_prefilter_needs_no_profile():
    assert is_insider_candidate(ev(), "POLITICS", CFG.insider)
    assert not is_insider_candidate(ev(usdc=5_000), "POLITICS", CFG.insider)
    assert not is_insider_candidate(ev(), "SPORTS", CFG.insider)
    assert not is_insider_candidate(ev(side="SELL"), "POLITICS", CFG.insider)
