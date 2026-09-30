"""Pure math for Insider Finder v2 (spec §1): which markets/bets are covered, the move target and whether a
bet's outcome moved the wallet's way before the market caught on — no I/O, so exhaustively testable.
"""

from __future__ import annotations

import re

from whalescan.classify import Blocklist, category_for_tags
from whalescan.config import Config, FinderCfg
from whalescan.models import Market

# Stock/crypto price-threshold markets ("hit $X", "above/below $X", "up or down") are noise, not insider signal —
# excluded even when tagged Crypto/Business, so a corporate-announcement market (e.g. "will MicroStrategy
# announce a purchase") stays covered while "will MSTR hit $125" does not.
#
# A bare "hit"/"reach(es)"/"above"/"below" also matches ordinary insider-category questions that happen to
# contain those common words ("hit Iran", "reach a ceasefire", "approval above 45%"), wrongly excluding core
# geopolitics/politics/economy markets (found by review; the original regex over-matched). Every branch here is
# anchored to an explicit price signal instead — a dollar amount, "(high|low)", "all-time high"/"ATH", or
# "up or down" — so a plain word without one of those never counts as a price-threshold market.
_DOLLAR = r"\$\s?[\d,]"
_PRICE_MARKET_RE = re.compile(
    rf"(up or down|all[\s-]time high|\bath\b|hit\s*\(?(high|low)\)?|"
    rf"(reach(es)?|dip to|trade|close|price of)\s+(above|below|{_DOLLAR})|"
    rf"(above|below)\s*{_DOLLAR}|{_DOLLAR})", re.IGNORECASE)


def is_price_market(question: str) -> bool:
    return bool(_PRICE_MARKET_RE.search(question))


def is_covered(market: Market, cfg: Config, blocklist: Blocklist) -> bool:
    """Any category except SPORTS, not a price-threshold market, not blocklisted, with real volume."""
    category = category_for_tags(market.tags, cfg.categories)
    if category == "SPORTS" or is_price_market(market.question):
        return False
    if blocklist.blocked(event_slug=market.event_slug, slug=market.slug, volume=market.volume):
        return False
    return True


def move_target(p0: float, cfg: FinderCfg) -> float:
    """p0 + 20 points, or half way to 1, whichever is closer — never above 1."""
    return min(1.0, p0 + min(cfg.move_points, (1.0 - p0) * cfg.move_fraction_to_one))


def known_at(t: int, end_ts: int | None, cfg: FinderCfg) -> int:
    """When a bet's move outcome is knowable: move_window_h after the signal, or the market's close, if earlier."""
    deadline = t + int(cfg.move_window_h * 3600)
    return deadline if end_ts is None else min(deadline, end_ts)


def move_hit(points: list[tuple[int, float]], t: int, p0: float, end_ts: int | None, cfg: FinderCfg) -> bool | None:
    """Did the price reach move_target(p0) at any point strictly after `t` and at/before known_at(t, end_ts)?
    None (unknown, not a miss) when no price was recorded in that window."""
    target = move_target(p0, cfg)
    deadline = known_at(t, end_ts, cfg)
    in_window = [p for ts, p in points if t < ts <= deadline]
    if not in_window:
        return None
    return any(p >= target for p in in_window)
