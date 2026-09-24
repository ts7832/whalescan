"""Walk-forward validation (spec §4.7): score on the past, test on the future, report honestly."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from whalescan.book import FollowQuote
from whalescan.classify import Blocklist
from whalescan.config import Config
from whalescan.gate import GateContext, ScoreBook, aggregate, evaluate
from whalescan.models import Market, Trade
from whalescan.scoring import score_wallets

DAY = 86400
CALIBRATION_BINS = (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)


@dataclass(frozen=True)
class Fold:
    cutoff: int
    end: int


def make_folds(first_ts: int, last_ts: int, *, fold_days: int, min_history_days: int) -> list[Fold]:
    folds = []
    step = fold_days * DAY
    t = first_ts + min_history_days * DAY
    while t + step <= last_ts:
        folds.append(Fold(t, t + step))
        t += step
    return folds


def _stats(rets: np.ndarray) -> dict[str, Any]:
    n = len(rets)
    if n == 0:
        return {"n": 0, "mean_ret": None, "hit_rate": None, "t_stat": None}
    mean = float(rets.mean())
    sd = float(rets.std(ddof=1)) if n > 1 else 0.0
    return {"n": n, "mean_ret": mean, "hit_rate": float((rets > 0).mean()),
            "t_stat": mean / (sd / math.sqrt(n)) if sd > 0 else None}


def run_validation(eligible: pd.DataFrame, flags: pd.Series, trades: list[Trade], markets: Mapping[str, Market],
                   cfg: Config, blocklist: Blocklist, *, now: int) -> dict[str, Any]:
    v, g = cfg.validation, cfg.gate
    rows: list[dict[str, Any]] = []
    fold_rows: list[dict[str, Any]] = []
    if not eligible.empty and trades:
        first = int(eligible["closed_ts"].min())
        last = min(max(t.ts for t in trades), now)
        for fold in make_folds(first, last, fold_days=v.fold_days, min_history_days=v.min_history_days):
            train = eligible[eligible["closed_ts"] < fold.cutoff]
            scores = score_wallets(train, flags, cfg.scoring, as_of=fold.cutoff, n_sims=v.n_sims)
            events = aggregate([t for t in trades if fold.cutoff <= t.ts < fold.end], g.aggregation_window_s)
            ctx = GateContext(cfg=g, categories=cfg.categories, blocklist=blocklist,
                              scores=ScoreBook.for_config(scores, cfg), markets=markets,
                              events=events, now=None, historical=True)
            fold_rets = []
            for ev in events:
                m = markets.get(ev.condition_id)
                winner = m.winner_index() if m else None
                if m is None or winner is None or ev.side != "BUY":
                    continue
                won = 1.0 if ev.outcome_index == winner else 0.0
                follow = min(0.99, ev.price + v.slippage)
                ret = won - follow - m.fee_per_share(follow)
                if ev.usdc >= g.min_usdc and g.price_min <= ev.price <= g.price_max:
                    rows.append({"group": "BASELINE", "ret": ret, "predicted": np.nan, "won": won})
                quote = FollowQuote(follow, True, ev.first_ts, None, None, None, ())
                e = evaluate(ev, ctx, quote)
                if e.status == "SIGNAL":
                    fold_rets.append(ret)
                    rows.append({"group": e.tier, "ret": ret, "won": won,
                                 "predicted": min(0.99, ev.price + (e.post_edge or 0.0))})
            fold_rows.append({"cutoff": fold.cutoff, "end": fold.end, "signals": len(fold_rets),
                              "mean_ret": float(np.mean(fold_rets)) if fold_rets else None})
    return _report(rows, fold_rows, cfg, now)


def _report(rows: list[dict[str, Any]], fold_rows: list[dict[str, Any]], cfg: Config, now: int) -> dict[str, Any]:
    v = cfg.validation
    df = pd.DataFrame(rows, columns=["group", "ret", "predicted", "won"])
    sig = df[df["group"].isin(["A", "B"])]
    groups = {
        "A": _stats(df.loc[df["group"] == "A", "ret"].to_numpy(float)),
        "B": _stats(df.loc[df["group"] == "B", "ret"].to_numpy(float)),
        "SIGNALS": _stats(sig["ret"].to_numpy(float)),
        "BASELINE": _stats(df.loc[df["group"] == "BASELINE", "ret"].to_numpy(float)),
    }
    calibration = []
    for lo, hi in zip(CALIBRATION_BINS[:-1], CALIBRATION_BINS[1:]):
        pred = sig["predicted"].astype(float)
        inside = sig[(pred >= lo) & ((pred < hi) if hi < 1.0 else (pred <= hi))]
        calibration.append({"lo": lo, "hi": hi, "n": len(inside),
                            "predicted": float(inside["predicted"].mean()) if len(inside) else None,
                            "realized": float(inside["won"].mean()) if len(inside) else None})
    s = groups["SIGNALS"]
    if s["n"] < v.min_signals:
        verdict = "INSUFFICIENT DATA"
    elif s["t_stat"] is not None and s["t_stat"] >= 2.0:
        verdict = "EDGE CONFIRMED"
    else:
        verdict = "EDGE NOT CONFIRMED"
    return {
        "generated_at": now,
        "verdict": verdict,
        "folds": fold_rows,
        "groups": groups,
        "calibration": calibration,
        "params": {"fold_days": v.fold_days, "min_history_days": v.min_history_days, "slippage": v.slippage,
                   "n_sims": v.n_sims},
        "caveats": [
            "Wallet universe is seeded from the current leaderboard, which leaks some future information into "
            "which wallets are studied.",
            "Positions exited before resolution are scored as if held to resolution from the average entry price.",
            f"Follow cost is modelled as whale price + {v.slippage:.3f}; historical order books were not recorded.",
            "Wallet behaviour flags and stake winsorization use each wallet's full history, not only the "
            "training slice of each fold.",
            "Returns are per share bought (payout minus price paid), not per dollar.",
        ],
    }
