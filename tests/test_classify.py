import pandas as pd

from whalescan.classify import Blocklist, category_for_tags, wallet_flags
from whalescan.config import load_config

CFG = load_config()


def test_category_uses_first_matching_bucket_case_insensitively():
    assert category_for_tags(["Sports", "NFL"], CFG.categories) == "SPORTS"
    assert category_for_tags(["Crypto", "Sports"], CFG.categories) == "CRYPTO"  # CRYPTO precedes SPORTS
    assert category_for_tags(["US Politics"], CFG.categories) == "POLITICS"
    assert category_for_tags([], CFG.categories) == "OTHER"
    assert category_for_tags(["Weather"], CFG.categories) == "OTHER"


def test_blocklist_patterns_and_volume():
    b = Blocklist(CFG.blocklist)
    assert b.blocked(event_slug="btc-updown-5m-1790250000")
    assert b.blocked(event_slug="eth-up-or-down-sept-24")
    assert b.blocked(event_slug="x", slug="sol-price-15m-candle-")
    assert b.blocked(event_slug="us-election", volume=500.0)
    assert not b.blocked(event_slug="us-election", volume=2_000_000.0)
    assert not b.blocked(event_slug="us-election", volume=None)


def rows(wallet, specs):
    return [{"wallet": wallet, "condition_id": c, "outcome_index": o, "avg_price": p, "total_bought": s}
            for c, o, p, s in specs]


def test_wallet_flags():
    df = pd.DataFrame(
        rows("0xmm", [("c1", 0, 0.5, 10), ("c1", 1, 0.5, 10), ("c2", 0, 0.5, 10), ("c2", 1, 0.5, 10), ("c3", 0, 0.5, 10)])
        + rows("0xfarm", [("c1", 0, 0.98, 1000), ("c2", 0, 0.5, 10)])
        + rows("0xlotto", [("c1", 0, 0.02, 100000), ("c2", 0, 0.5, 10)])
        + rows("0xclean", [("c1", 0, 0.4, 100), ("c2", 1, 0.6, 100)])
    )
    flags = wallet_flags(df, CFG.scoring)
    assert flags["0xmm"] == "MARKET_MAKER"
    assert flags["0xfarm"] == "FARMER"
    assert flags["0xlotto"] == "LOTTERY"
    assert flags["0xclean"] == ""


def test_wallet_flags_empty_frame():
    empty = pd.DataFrame(columns=["wallet", "condition_id", "outcome_index", "avg_price", "total_bought"])
    assert wallet_flags(empty, CFG.scoring).empty
