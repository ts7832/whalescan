"""Insider detector: brand-new accounts placing big bets in markets where private information can exist.

This is WHALESCAN's primary signal. Unlike skill certification it needs no trading history: the lack of
history is the point. Rules (thresholds in config.toml [insider]):
  I1 a BUY                      I4 position >= min_usdc
  I2 account <= max_age_days old when it first bought     I5 news-type market, known, open, not blocklisted
  I3 <= max_markets traded ever I6 price in band and following now costs <= max_slippage more
Evidence behind the design: see the Plan 2 addendum (fresh wallets: informative in news markets, noise in sports).
"""

from __future__ import annotations

import math

from whalescan.book import FollowQuote
from whalescan.classify import Blocklist
from whalescan.config import InsiderCfg
from whalescan.gate import Check, Evaluation, PositionEvent
from whalescan.models import Market, WalletProfile

DAY = 86400


def is_insider_candidate(ev: PositionEvent, category: str, cfg: InsiderCfg) -> bool:
    """Cheap pre-filter before spending API calls on the wallet's profile."""
    return ev.side == "BUY" and ev.usdc >= cfg.min_usdc and category in cfg.categories


def needs_insider_book(e: Evaluation) -> bool:
    failed = e.failed()
    return len(failed) == 1 and failed[0].code == "I6" and failed[0].detail == "NO BOOK"


def account_age_days(ev: PositionEvent, profile: WalletProfile | None) -> float | None:
    if profile is None or profile.created_ts is None:
        return None
    return max(0.0, (ev.first_ts - profile.created_ts) / DAY)


def evaluate_insider(ev: PositionEvent, profile: WalletProfile | None, market: Market | None, category: str,
                     cfg: InsiderCfg, blocklist: Blocklist, quote: FollowQuote | None, now: int) -> Evaluation:
    checks = [Check("I1", ev.side == "BUY", "BUY" if ev.side == "BUY" else "SELL · EXIT")]

    age = account_age_days(ev, profile)
    checks.append(Check("I2", age is not None and age <= cfg.max_age_days,
                        "AGE UNKNOWN" if age is None else f"{age:.1f}D OLD"))

    n_markets = profile.markets_traded if profile else None
    checks.append(Check("I3", n_markets is not None and n_markets <= cfg.max_markets,
                        "MARKETS UNKNOWN" if n_markets is None else f"{n_markets} MARKETS"))

    checks.append(Check("I4", ev.usdc >= cfg.min_usdc, f"${ev.usdc:,.0f}"))

    if market is None:
        i5 = Check("I5", False, "UNKNOWN MARKET")
    elif market.closed:
        i5 = Check("I5", False, "MARKET CLOSED")
    elif blocklist.blocked(event_slug=market.event_slug or ev.event_slug, slug=market.slug, volume=market.volume):
        i5 = Check("I5", False, "BLOCKLISTED")
    elif category not in cfg.categories:
        i5 = Check("I5", False, "NOT A NEWS MARKET")
    else:
        i5 = Check("I5", True, category)
    checks.append(i5)

    if not (cfg.price_min <= ev.price <= cfg.price_max):
        checks.append(Check("I6", False, f"PRICE {ev.price:.3f} OUT OF BAND"))
    elif quote is None:
        checks.append(Check("I6", False, "NO BOOK"))
    elif not quote.complete or math.isnan(quote.vwap):
        checks.append(Check("I6", False, "BOOK TOO THIN"))
    else:
        moved = quote.vwap - ev.price
        checks.append(Check("I6", moved <= cfg.max_slippage,
                            f"FOLLOW {quote.vwap:.3f}" if moved <= cfg.max_slippage else f"PRICE MOVED {moved:+.3f}"))

    fee = market.fee_per_share(ev.price) if market else None
    if ev.side != "BUY":
        status = "EXIT"
    elif all(c.passed for c in checks):
        status = "INSIDER"
    else:
        status = "REJECTED"
    tier = None
    if status == "INSIDER":
        tier = "A" if (age is not None and age <= cfg.tier_a_age_days and ev.usdc >= cfg.tier_a_usdc) else "B"
    return Evaluation(ev, category, tuple(checks), status, tier, None, None, ev.price + cfg.max_slippage, fee, quote,
                      (ev.wallet,))
