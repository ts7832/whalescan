from dataclasses import replace

import pandas as pd

from whalescan.book import FollowQuote
from whalescan.classify import Blocklist
from whalescan.config import load_config
from whalescan.gate import GateContext, PositionEvent, ScoreBook, aggregate, evaluate, needs_book
from whalescan.models import Market, Trade
from whalescan.scoring import SCORE_COLUMNS

CFG = load_config()
NOW = 1_790_000_000
H = 3600


def score(wallet, category, certified, post_edge=0.06, n=100, edge=0.08, median_stake=2000.0):
    return {"wallet": wallet, "category": category, "n": n, "n_eff": n, "edge": edge, "sigma": 0.02,
            "post_edge": post_edge, "p_value": 0.001 if certified else 0.5, "bh_pass": certified,
            "certified": certified, "flags": "", "median_stake": median_stake, "as_of": NOW, "win_rate": 0.6}


def book(*rows, fallback=CFG.gate.fallback_max_cat_positions):
    return ScoreBook(pd.DataFrame(list(rows), columns=SCORE_COLUMNS), fallback)


def market(cid="0xc", closed=False, end_ts=NOW + 48 * H, slug="us-election-winner", volume=1e6, fees=True):
    return Market(cid, "Who wins?", slug, "us-election", end_ts, closed, None, (0.4, 0.6), ("yes", "no"),
                  fees, 0.05, 1.0, volume, ("Politics",))


def event(wallet="0xwhale", asset="yes", side="BUY", usdc=10_000.0, price=0.40, ts=NOW - H, cid="0xc"):
    return PositionEvent(wallet, asset, cid, side, ts, ts + 60, usdc, usdc / price, price, 3, "us-election",
                         "Who wins?", "Yes", 0 if asset == "yes" else 1)


def quote(vwap=0.41, complete=True):
    return FollowQuote(vwap, complete, NOW, 0.39, 0.41, 0.40, ((0.41, 5000.0),))


CERTIFIED = [score("0xwhale", "ALL", True), score("0xwhale", "POLITICS", True)]


def ctx(scores=None, markets=None, events=(), now=NOW, historical=False, gate=CFG.gate):
    return GateContext(cfg=gate, categories=CFG.categories, blocklist=Blocklist(CFG.blocklist),
                       scores=scores or book(*CERTIFIED), markets={"0xc": market()} if markets is None else markets,
                       events=list(events), now=now, historical=historical)


def codes(e):
    return {c.code: c.passed for c in e.checks}


def trade(ts, wallet="0xw", side="BUY", price=0.4, size=100.0, asset="a"):
    return Trade(f"0x{ts}{wallet}{side}", ts, wallet, asset, "0xc", side, price, size, "ev", "t", "Yes", 0, None)


def test_aggregate_merges_within_window_and_splits_after():
    trades = [trade(0), trade(100, price=0.5), trade(700), trade(50, side="SELL"), trade(60, wallet="0xz")]
    events = aggregate(trades, 600)
    buys = [e for e in events if e.wallet == "0xw" and e.side == "BUY"]
    assert [(e.first_ts, e.n_fills) for e in buys] == [(0, 2), (700, 1)]
    assert abs(buys[0].price - (0.4 * 100 + 0.5 * 100) / 200) < 1e-12
    assert len(events) == 4


def test_all_gates_pass_is_a_tier_b_signal():
    e = evaluate(event(), ctx(), quote())
    assert e.status == "SIGNAL" and e.tier == "B", e.checks
    fee_follow = 0.05 * 0.41 * 0.59
    assert abs(e.net_edge - (0.06 - 0.01 - fee_follow)) < 1e-12
    assert abs(e.max_entry - (0.40 + 0.06 - 0.05 * 0.4 * 0.6 - 0.01)) < 1e-12


def test_high_net_edge_is_tier_a():
    scores = book(score("0xwhale", "ALL", True, post_edge=0.09), score("0xwhale", "POLITICS", True, post_edge=0.09))
    assert evaluate(event(), ctx(scores=scores), quote()).tier == "A"


def test_consensus_of_two_certified_whales_is_tier_a():
    scores = book(*CERTIFIED, score("0xother", "ALL", True), score("0xother", "POLITICS", True))
    other = event(wallet="0xother", ts=NOW - 2 * H)
    e = evaluate(event(), ctx(scores=scores, events=[other]), quote())
    assert e.tier == "A" and e.consensus == ("0xother", "0xwhale")


def test_uncertified_wallet_fails_g2():
    e = evaluate(event(), ctx(scores=book(score("0xwhale", "ALL", False))), quote())
    assert not codes(e)["G2"] and e.status == "REJECTED"


def test_unknown_wallet_fails_g2():
    e = evaluate(event(wallet="0xnobody"), ctx(), quote())
    assert not codes(e)["G2"] and e.checks[1].detail == "UNKNOWN WALLET"


def test_overall_certification_is_a_fallback_for_thin_categories():
    thin = book(score("0xwhale", "ALL", True), score("0xwhale", "POLITICS", False, n=5, edge=0.01))
    assert evaluate(event(), ctx(scores=thin), quote()).status == "SIGNAL"
    thick = book(score("0xwhale", "ALL", True), score("0xwhale", "POLITICS", False, n=50, edge=0.01))
    assert evaluate(event(), ctx(scores=thick), quote()).status == "REJECTED"
    negative = book(score("0xwhale", "ALL", True), score("0xwhale", "POLITICS", False, n=5, edge=-0.05))
    assert evaluate(event(), ctx(scores=negative), quote()).status == "REJECTED"


def test_conviction_gate():
    scores = book(score("0xwhale", "ALL", True, median_stake=4000.0), score("0xwhale", "POLITICS", True))
    e = evaluate(event(usdc=6000.0), ctx(scores=scores), quote())
    assert not codes(e)["G3"]
    assert not codes(evaluate(event(usdc=4000.0), ctx(), quote()))["G3"]


def test_price_band_gate():
    assert not codes(evaluate(event(price=0.97), ctx(), quote(vwap=0.975)))["G4"]


def test_unknown_market_fails_g5_without_crashing():
    e = evaluate(event(), ctx(markets={}), None)
    assert not codes(e)["G5"] and e.checks[4].detail == "UNKNOWN MARKET"
    assert e.status == "REJECTED" and e.fee is None


def test_blocklisted_and_near_end_markets_fail_g5():
    bot = ctx(markets={"0xc": market(slug="btc-updown-5m-1790000000")})
    assert not codes(evaluate(event(), bot, quote()))["G5"]
    soon = ctx(markets={"0xc": market(end_ts=NOW + H)})
    assert evaluate(event(), soon, quote()).checks[4].detail == "1.0H TO END"


def test_thin_book_fails_g6():
    e = evaluate(event(), ctx(), quote(vwap=float("nan"), complete=False))
    assert e.checks[5].detail == "BOOK TOO THIN" and e.status == "REJECTED"


def test_expensive_follow_fails_g6():
    e = evaluate(event(), ctx(), quote(vwap=0.46))
    assert not codes(e)["G6"] and e.net_edge < 0


def test_conflicting_certified_whale_suppresses_signal():
    scores = book(*CERTIFIED, score("0xbear", "ALL", True), score("0xbear", "POLITICS", True))
    against = event(wallet="0xbear", asset="no", price=0.6)
    e = evaluate(event(), ctx(scores=scores, events=[against]), quote())
    assert e.status == "CONFLICT" and not codes(e)["G7"]


def test_sell_is_an_exit():
    assert evaluate(event(side="SELL"), ctx(), None).status == "EXIT"


def test_old_events_and_closed_markets_expire():
    assert evaluate(event(ts=NOW - 30 * H), ctx(), quote()).status == "EXPIRED"
    assert evaluate(event(), ctx(markets={"0xc": market(closed=True)}), quote()).status == "EXPIRED"


def test_needs_book_only_when_g6_is_the_sole_blocker():
    assert needs_book(evaluate(event(), ctx(), None))
    assert not needs_book(evaluate(event(price=0.97), ctx(), None))
    assert not needs_book(evaluate(event(), ctx(), quote()))


def test_historical_mode_uses_event_time_and_ignores_closure():
    e = evaluate(event(ts=NOW - 100 * H), ctx(markets={"0xc": market(closed=True, end_ts=NOW)}, now=None,
                                               historical=True), quote())
    assert e.status == "SIGNAL"


def test_gate_thresholds_come_from_config():
    strict = replace(CFG.gate, min_net_edge=0.5)
    assert evaluate(event(), ctx(gate=strict), quote()).status == "REJECTED"


def test_historical_mode_ignores_events_from_the_future():
    scores = book(*CERTIFIED, score("0xbear", "ALL", True), score("0xbear", "POLITICS", True),
                  score("0xbull", "ALL", True), score("0xbull", "POLITICS", True))
    later_bear = event(wallet="0xbear", asset="no", price=0.6, ts=NOW + H)
    later_bull = event(wallet="0xbull", ts=NOW + 2 * H)
    ev = event(ts=NOW)
    hist = evaluate(ev, ctx(scores=scores, events=[later_bear, later_bull], now=None, historical=True), quote())
    assert hist.status == "SIGNAL" and hist.consensus == ("0xwhale",)
    earlier_bear = event(wallet="0xbear", asset="no", price=0.6, ts=NOW - H)
    assert evaluate(ev, ctx(scores=scores, events=[earlier_bear], now=None, historical=True), quote()).status == "CONFLICT"


# ---------------------------------------------------------------- sniper mode (the only historical-edge signal)

def sniper_book(**overrides):
    base = score("0xsniper", "ALL", False, post_edge=0.0, edge=0.40, median_stake=8000.0)
    base.update(n=12, n_eff=11.0, sigma=0.12, p_value=0.002, win_rate=0.917)
    base.update(overrides)
    return ScoreBook(pd.DataFrame([base], columns=SCORE_COLUMNS), CFG.gate.fallback_max_cat_positions,
                     mode="sniper", sniper=CFG.sniper)


def test_rare_big_winner_is_a_sniper_with_a_conservative_edge():
    v = sniper_book().view("0xsniper", "POLITICS")
    assert v.basis == "SNIPER"
    assert abs(v.post_edge - (0.40 - 0.12)) < 1e-12            # 1σ haircut, not the raw edge
    assert sniper_book().certified_wallets() == {"0xsniper"}


def test_sniper_rules_each_exclude():
    assert sniper_book(n=200).view("0xsniper", "X").basis == "NONE"            # trades too often
    assert sniper_book(n=5).view("0xsniper", "X").basis == "NONE"              # too little to judge
    assert sniper_book(win_rate=0.6).view("0xsniper", "X").basis == "NONE"
    assert sniper_book(median_stake=500.0).view("0xsniper", "X").basis == "NONE"
    assert sniper_book(p_value=0.2).view("0xsniper", "X").basis == "NONE"      # e.g. only bought heavy favourites
    assert sniper_book(flags="FARMER").view("0xsniper", "X").basis == "NONE"


def test_high_frequency_certified_wallet_is_not_a_signal_in_sniper_mode():
    row = score("0xquant", "ALL", True)
    row.update(n=900, win_rate=0.55)
    book_ = ScoreBook(pd.DataFrame([row, score("0xquant", "POLITICS", True)], columns=SCORE_COLUMNS),
                      CFG.gate.fallback_max_cat_positions, mode="sniper", sniper=CFG.sniper)
    assert book_.view("0xquant", "POLITICS").basis == "NONE"


def test_snipers_are_not_held_to_the_median_multiple_and_use_a_one_sigma_bound():
    book_ = sniper_book(median_stake=8000.0, edge=0.30, sigma=0.08)
    ctx_ = ctx(scores=book_, gate=replace(CFG.gate, skill_mode="sniper"))
    e = evaluate(event(wallet="0xsniper", usdc=8_000.0), ctx_, quote())
    assert e.status == "SIGNAL", e.checks                              # a typical-size sniper bet passes G3
    assert abs(e.post_edge - (0.30 - 0.08)) < 1e-12
