"""Typed access to config.toml. Missing or unknown keys are errors, never silent defaults."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class PathsCfg:
    research_db: str
    scores_parquet: str
    snapshot_dir: str


@dataclass(frozen=True)
class HttpCfg:
    rate_per_s: float
    max_retries: int
    concurrency: int
    user_agent: str
    host_rates: dict[str, float]


@dataclass(frozen=True)
class UniverseCfg:
    max_wallets: int
    leaderboard_periods: tuple[str, ...]
    leaderboard_orders: tuple[str, ...]
    leaderboard_pages: int
    large_trade_min_usdc: float
    large_trade_lookback_days: int
    max_wallet_age_days: float


@dataclass(frozen=True)
class ScoringCfg:
    n_sims: int
    n_sims_screen: int
    screen_p: float
    seed: int
    min_n_eff: float
    bh_q: float
    min_post_edge: float
    price_min: float
    price_max: float
    winsor_quantile: float
    farmer_price: float
    farmer_share: float
    lottery_price: float
    lottery_share: float
    mm_both_sides_share: float


@dataclass(frozen=True)
class GateCfg:
    skill_mode: str
    aggregation_window_s: int
    min_usdc: float
    conviction_k: float
    price_min: float
    price_max: float
    min_hours_to_end: float
    follow_size_usdc: float
    min_net_edge: float
    max_entry_margin: float
    tier_a_net_edge: float
    tier_a_consensus: int
    conflict_window_h: float
    fallback_max_cat_positions: int
    signal_lookback_h: float
    max_contacts: int


@dataclass(frozen=True)
class SniperCfg:
    min_positions: int
    max_positions: int
    min_win_rate: float
    min_median_stake: float
    max_p_value: float


@dataclass(frozen=True)
class InsiderCfg:
    max_age_days: float
    tier_a_age_days: float
    max_markets: int
    min_usdc: float
    tier_a_usdc: float
    price_min: float
    price_max: float
    max_slippage: float
    categories: tuple[str, ...]
    profile_ttl_h: float


@dataclass(frozen=True)
class ValidationCfg:
    fold_days: int
    min_history_days: int
    slippage: float
    n_sims: int
    max_wallets: int
    every_hours: float
    min_signals: int
    min_wallets: int


@dataclass(frozen=True)
class BlocklistCfg:
    slug_patterns: tuple[str, ...]
    min_market_volume: float


@dataclass(frozen=True)
class CategoriesCfg:
    order: tuple[str, ...]
    tags: dict[str, tuple[str, ...]]


@dataclass(frozen=True)
class Config:
    paths: PathsCfg
    http: HttpCfg
    universe: UniverseCfg
    scoring: ScoringCfg
    gate: GateCfg
    sniper: SniperCfg
    insider: InsiderCfg
    validation: ValidationCfg
    blocklist: BlocklistCfg
    categories: CategoriesCfg

    def path(self, rel: str) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else ROOT / p


def _section(cls: type, name: str, raw: dict[str, Any]) -> Any:
    known = {f.name for f in fields(cls)}
    unknown = set(raw) - known
    if unknown:
        raise ValueError(f"config: unknown key(s) {name}.{sorted(unknown)}")
    kwargs = {}
    for f in fields(cls):
        if f.name not in raw:
            raise ValueError(f"config: missing {name}.{f.name}")
        value = raw[f.name]
        kwargs[f.name] = tuple(value) if isinstance(value, list) else value
    return cls(**kwargs)


def load_config(path: Path | None = None) -> Config:
    path = path or Path(os.environ.get("WHALESCAN_CONFIG", ROOT / "config.toml"))
    raw = tomllib.loads(path.read_text())
    cats = raw["categories"]
    return Config(
        paths=_section(PathsCfg, "paths", raw["paths"]),
        http=_section(HttpCfg, "http", raw["http"]),
        universe=_section(UniverseCfg, "universe", raw["universe"]),
        scoring=_section(ScoringCfg, "scoring", raw["scoring"]),
        gate=_section(GateCfg, "gate", raw["gate"]),
        sniper=_section(SniperCfg, "sniper", raw["sniper"]),
        insider=_section(InsiderCfg, "insider", raw["insider"]),
        validation=_section(ValidationCfg, "validation", raw["validation"]),
        blocklist=_section(BlocklistCfg, "blocklist", raw["blocklist"]),
        categories=CategoriesCfg(
            order=tuple(cats["order"]),
            tags={k: tuple(t.lower() for t in v) for k, v in cats["tags"].items()},
        ),
    )
