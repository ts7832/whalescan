import math

import pandas as pd

from whalescan.strict_rules import STRICT_RULES, add_strict_rules

DAY = 86400
T = 1_780_000_000


def bet(wallet, t, *, age=0.5, markets=1.0, usdc=30_000.0, price=0.30, won=True, resolved=None, band=True,
        irregular=False):
    return {"wallet": wallet, "signal_ts": t, "age_days": age, "markets_at_bet": markets, "usdc": usdc,
            "whale_price": price, "won": won, "payout": math.nan if irregular else float(won),
            "irregular": irregular, "resolved_ts": resolved if resolved is not None else t + 5 * DAY,
            "r_baseline": band}


def flags(*rows):
    return add_strict_rules(pd.DataFrame(list(rows)))


def test_the_four_strict_entry_rules_match_their_preregistered_definitions():
    df = flags(bet("a", T),                                   # S1 + S2 (brand new, first market, $30k, 0.30)
               bet("b", T, price=0.60),                       # S1 only (not a long shot)
               bet("c", T, age=2.5, markets=3, usdc=60_000),  # S3 only
               bet("d", T, usdc=60_000),                      # S1, S2, S3, S4
               bet("e", T, markets=2),                        # nothing: had traded before
               bet("f", T, markets=math.nan),                 # nothing: history unknown
               bet("g", T, band=False))                       # nothing: price outside the band
    got = {w: [r for r in ("s1", "s2", "s3", "s4") if df.loc[df.wallet == w, r].item()] for w in df.wallet}
    assert got == {"a": ["s1", "s2"], "b": ["s1"], "c": ["s3"], "d": ["s1", "s2", "s3", "s4"], "e": [], "f": [],
                   "g": []}


def test_a_wallet_is_confirmed_only_once_its_long_shot_win_has_resolved():
    win = bet("w", T, age=3, markets=1, usdc=12_000, price=0.20, won=True, resolved=T + 10 * DAY)
    during = bet("w", T + 5 * DAY, age=8, markets=2, usdc=8_000, price=0.5)    # before the win resolved: unknown yet
    after = bet("w", T + 11 * DAY, age=14, markets=3, usdc=8_000, price=0.5)   # after: a confirmed-insider bet
    df = flags(win, during, after)
    assert list(df["s5"]) == [False, False, True]


def test_a_losing_or_short_priced_or_old_or_small_first_bet_confirms_nothing():
    for first in (bet("w", T, price=0.20, won=False),           # lost
                  bet("w", T, price=0.50, won=True),            # not a long shot
                  bet("w", T, age=30, price=0.20, won=True),    # not a fresh account
                  bet("w", T, usdc=9_000, price=0.20, won=True),  # too small
                  bet("w", T, price=0.20, won=True, irregular=True)):  # voided market proves nothing
        df = flags(first, bet("w", T + 30 * DAY, age=40, markets=5, usdc=9_000, price=0.5))
        assert not df["s5"].any()


def test_followed_bets_must_be_big_enough_and_tradeable():
    win = bet("w", T, age=1, usdc=12_000, price=0.20, won=True, resolved=T + DAY)
    df = flags(win, bet("w", T + 2 * DAY, usdc=4_000), bet("w", T + 3 * DAY, usdc=6_000, band=False),
               bet("w", T + 4 * DAY, usdc=6_000))
    assert list(df["s5"]) == [False, False, False, True]


def test_every_preregistered_rule_is_listed():
    assert list(STRICT_RULES) == ["s1", "s2", "s3", "s4", "s5"]
