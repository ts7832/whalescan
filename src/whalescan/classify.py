"""Market categories, market blocklist, and behavioural wallet flags (spec §4.4)."""

from __future__ import annotations

import re
from collections.abc import Iterable

import pandas as pd

from whalescan.config import BlocklistCfg, CategoriesCfg, ScoringCfg


def category_for_tags(tags: Iterable[str], cfg: CategoriesCfg) -> str:
    lowered = {t.lower() for t in tags}
    for category in cfg.order:
        if lowered & set(cfg.tags.get(category, ())):
            return category
    return "OTHER"


class Blocklist:
    """Markets that are never scored or signalled: bot-dominated short-horizon markets and illiquid ones."""

    def __init__(self, cfg: BlocklistCfg) -> None:
        self._patterns = [re.compile(p) for p in cfg.slug_patterns]
        self._min_volume = cfg.min_market_volume

    def blocked(self, *, event_slug: str, slug: str = "", volume: float | None = None) -> bool:
        if any(p.search(event_slug) or p.search(slug) for p in self._patterns):
            return True
        return volume is not None and volume < self._min_volume


def wallet_flags(positions: pd.DataFrame, cfg: ScoringCfg) -> pd.Series:
    """Behavioural flags per wallet; flagged wallets are never certified."""
    if positions.empty:
        return pd.Series(dtype=object)
    df = positions.assign(stake=positions["total_bought"] * positions["avg_price"])
    total = df.groupby("wallet")["stake"].sum()
    farmer = df[df["avg_price"] >= cfg.farmer_price].groupby("wallet")["stake"].sum().reindex(total.index, fill_value=0.0)
    lottery = df[df["avg_price"] <= cfg.lottery_price].groupby("wallet")["stake"].sum().reindex(total.index, fill_value=0.0)
    both_sides = (df.groupby(["wallet", "condition_id"])["outcome_index"].nunique() > 1).groupby("wallet").mean()

    out: dict[str, str] = {}
    for wallet in total.index:
        flags = []
        if both_sides.get(wallet, 0.0) > cfg.mm_both_sides_share:
            flags.append("MARKET_MAKER")
        if total[wallet] > 0 and farmer[wallet] / total[wallet] > cfg.farmer_share:
            flags.append("FARMER")
        if total[wallet] > 0 and lottery[wallet] / total[wallet] > cfg.lottery_share:
            flags.append("LOTTERY")
        out[wallet] = ",".join(flags)
    return pd.Series(out, dtype=object)
