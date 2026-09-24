"""Strict-JSON snapshot files read by the dashboard. Writes are atomic (tmp file + rename)."""

from __future__ import annotations

import dataclasses
import json
import math
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from whalescan.gate import Evaluation
from whalescan.models import Market, PricePoint


def clean(obj: Any) -> Any:
    if obj is None or isinstance(obj, str):
        return obj
    if isinstance(obj, (bool, np.bool_)):
        return bool(obj)
    if isinstance(obj, (int, np.integer)):
        return int(obj)
    if isinstance(obj, (float, np.floating)):
        f = float(obj)
        return f if math.isfinite(f) else None
    if obj is pd.NA or obj is pd.NaT:
        return None
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: clean(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, Mapping):
        return {str(k): clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, np.ndarray)):
        return [clean(v) for v in obj]
    raise TypeError(f"cannot serialise {type(obj).__name__}")


def write_json_atomic(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(clean(obj), allow_nan=False, separators=(",", ":")))
    os.replace(tmp, path)


def write_parquet_atomic(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    df.to_parquet(tmp, index=False)
    os.replace(tmp, path)


def evaluation_json(e: Evaluation, market: Market | None, names: Mapping[str, str],
                    history: list[PricePoint] | None) -> dict[str, Any]:
    ev, q = e.event, e.quote
    return {
        "id": ev.id,
        "status": e.status,
        "tier": e.tier,
        "category": e.category,
        "wallet": ev.wallet,
        "wallet_name": names.get(ev.wallet, ""),
        "question": market.question if market else ev.title,
        "market_slug": market.slug if market else "",
        "event_slug": (market.event_slug if market else "") or ev.event_slug,
        "end_ts": market.end_ts if market else None,
        "outcome": ev.outcome,
        "side": ev.side,
        "price": ev.price,
        "usdc": ev.usdc,
        "shares": ev.shares,
        "first_ts": ev.first_ts,
        "last_ts": ev.last_ts,
        "n_fills": ev.n_fills,
        "post_edge": e.post_edge,
        "net_edge": e.net_edge,
        "max_entry": e.max_entry,
        "fee": e.fee,
        "quote": None if q is None else {
            "vwap": q.vwap, "complete": q.complete, "book_as_of": q.book_as_of, "best_bid": q.best_bid,
            "best_ask": q.best_ask, "microprice": q.microprice, "levels": [list(level) for level in q.levels],
        },
        "consensus": list(e.consensus),
        "checks": [{"code": c.code, "passed": c.passed, "detail": c.detail} for c in e.checks],
        "history": None if history is None else [{"t": p.ts, "p": p.price} for p in history],
    }


def whales_json(scores: pd.DataFrame, eligible: pd.DataFrame, names: Mapping[str, str], *, recent_n: int = 12,
                watchlist_n: int = 25) -> list[dict[str, Any]]:
    """Certified wallets plus a watchlist of the most significant uncertified, unflagged wallets."""
    if scores.empty:
        return []
    overall = scores[scores["category"] == "ALL"].set_index("wallet")
    certified = set(scores.loc[scores["certified"].astype(bool), "wallet"])
    watch = [w for w in overall[overall["flags"] == ""].sort_values("p_value").index if w not in certified]
    out = []
    for wallet in sorted(certified) + watch[:watchlist_n]:
        cats = scores[scores["wallet"] == wallet].sort_values("p_value")
        best = cats[cats["certified"].astype(bool)].sort_values("post_edge", ascending=False)
        recent = eligible[eligible["wallet"] == wallet].sort_values("closed_ts", ascending=False).head(recent_n)
        row = overall.loc[wallet] if wallet in overall.index else None
        out.append({
            "wallet": wallet,
            "name": names.get(wallet, ""),
            "certified": wallet in certified,
            "median_stake": None if row is None else row["median_stake"],
            "flags": "" if row is None else row["flags"],
            "best_category": best["category"].iloc[0] if len(best) else None,
            "best_post_edge": float(best["post_edge"].iloc[0]) if len(best) else None,
            "categories": cats[["category", "n", "n_eff", "edge", "post_edge", "p_value", "bh_pass",
                                "certified"]].to_dict("records"),
            "recent": [{"title": r.title, "outcome": r.outcome, "price": r.p, "won": bool(r.y), "stake": r.stake,
                        "closed_ts": r.closed_ts} for r in recent.itertuples(index=False)],
        })
    return sorted(out, key=lambda d: (not d["certified"], -(d["best_post_edge"] or 0.0)))
