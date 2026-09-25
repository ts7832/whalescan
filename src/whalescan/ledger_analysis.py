"""Track Record analysis: headline stats, and winners-vs-losers feature comparison once there's enough
data to say anything (Track Record spec §6). Pure functions over the ledger's calls/marks — no I/O.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

import numpy as np

from whalescan.config import Config

KINDS = ("INSIDER", "SNIPER", "NEAR_MISS")
FEATURES = ("age_days", "markets_traded", "usdc", "consensus")
MODEL_MIN_SCORED = 200
CV_FOLDS = 5


def _finite(x: float | None) -> float | None:
    """None-through, and NaN/±Infinity become None — a summary must never carry a non-finite float."""
    if x is None:
        return None
    return x if math.isfinite(x) else None


def _mean(values: Sequence[float]) -> float | None:
    return _finite(float(np.mean(values))) if values else None


def mann_whitney_u(a: Sequence[float], b: Sequence[float]) -> tuple[float | None, float | None]:
    """(U statistic for group a, two-sided p-value) via the normal approximation with a tie correction.
    Ranks all of a and b together (ties get the average rank), asking whether a's ranks sit systematically
    below or above b's. None, None when either group is empty.
    """
    if not a or not b:
        return None, None
    n1, n2 = len(a), len(b)
    combined = np.concatenate([np.asarray(a, dtype=float), np.asarray(b, dtype=float)])
    order = np.argsort(combined, kind="stable")
    ranks = np.empty(len(combined))
    sorted_vals = combined[order]
    i = 0
    while i < len(sorted_vals):
        j = i
        while j < len(sorted_vals) and sorted_vals[j] == sorted_vals[i]:
            j += 1
        ranks[order[i:j]] = (i + 1 + j) / 2.0  # average rank of the tied block (1-indexed)
        i = j
    r1 = float(ranks[:n1].sum())
    u1 = r1 - n1 * (n1 + 1) / 2.0
    mean_u = n1 * n2 / 2.0

    _, counts = np.unique(combined, return_counts=True)
    tie_term = float(((counts**3 - counts).sum()))
    n = n1 + n2
    variance = n1 * n2 / 12.0 * ((n + 1) - tie_term / (n * (n - 1))) if n > 1 else 0.0
    if variance <= 0:
        return u1, 1.0
    z = (u1 - mean_u) / math.sqrt(variance)
    p = 2.0 * (1.0 - _norm_cdf(abs(z)))
    return u1, min(1.0, max(0.0, p))


def _norm_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def quantile_buckets(values: Sequence[float], wins: Sequence[bool], returns: Sequence[float],
                     n_buckets: int = 4) -> list[dict[str, Any]]:
    """Split `values` into up to `n_buckets` equal-count quantile buckets; win rate and mean return per bucket."""
    if not values:
        return []
    arr = np.asarray(values, dtype=float)
    order = np.argsort(arr, kind="stable")
    n = len(arr)
    buckets = min(n_buckets, n)
    edges = np.array_split(order, buckets)
    out = []
    for idx in edges:
        if len(idx) == 0:
            continue
        vals = arr[idx]
        w = [wins[i] for i in idx]
        r = [returns[i] for i in idx]
        out.append({"lo": float(vals.min()), "hi": float(vals.max()), "n": len(idx),
                    "win_rate": _mean([1.0 if x else 0.0 for x in w]), "mean_return": _mean(r)})
    return out


def _sigmoid(z: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))


def _fit_logistic(x: np.ndarray, y: np.ndarray, *, l2: float = 1.0, iters: int = 200,
                  lr: float = 0.5) -> np.ndarray:
    """L2-regularised logistic regression by gradient descent. x is standardised; a leading bias column of
    1s is expected. Returns the fitted weight vector."""
    n, d = x.shape
    w = np.zeros(d)
    for _ in range(iters):
        p = _sigmoid(x @ w)
        grad = x.T @ (p - y) / n
        grad[1:] += l2 * w[1:] / n  # never regularise the bias term
        w -= lr * grad
    return w


def _auc(y_true: np.ndarray, scores: np.ndarray) -> float | None:
    pos, neg = scores[y_true == 1], scores[y_true == 0]
    u, _ = mann_whitney_u(list(pos), list(neg))
    if u is None or len(pos) == 0 or len(neg) == 0:
        return None
    return _finite(u / (len(pos) * len(neg)))


def _fit_model(feature_rows: list[list[float]], labels: list[bool]) -> dict[str, Any]:
    n = len(labels)
    if n < MODEL_MIN_SCORED or len(set(labels)) < 2:
        return {"status": "INSUFFICIENT DATA", "auc": None, "coefficients": None, "n": n}

    x = np.asarray(feature_rows, dtype=float)
    y = np.asarray([1.0 if v else 0.0 for v in labels])
    mu, sigma = x.mean(axis=0), x.std(axis=0)
    sigma[sigma == 0] = 1.0
    xs = np.hstack([np.ones((n, 1)), (x - mu) / sigma])

    rng = np.random.default_rng(20260925)
    folds = rng.permutation(n) % CV_FOLDS
    fold_aucs = []
    for k in range(CV_FOLDS):
        test, train = folds == k, folds != k
        if len(set(y[train])) < 2 or len(set(y[test])) < 2:
            continue
        w = _fit_logistic(xs[train], y[train])
        auc = _auc(y[test], _sigmoid(xs[test] @ w))
        if auc is not None:
            fold_aucs.append(auc)

    w_full = _fit_logistic(xs, y)
    coefficients = {"bias": float(w_full[0]), **{f: float(w_full[i + 1]) for i, f in enumerate(FEATURES)}}
    return {"status": "OK", "auc": _mean(fold_aucs), "coefficients": coefficients, "n": n}


def _kind_stats(calls: list[dict[str, Any]], by_call: dict[str, dict[str, Any]], mark_days: dict[str, dict[int, dict]],
                settled: dict[str, dict[str, Any]]) -> dict[str, Any]:
    ids = [c["id"] for c in calls]
    open_ids = [i for i in ids if i not in settled]
    settled_marks = [settled[i] for i in ids if i in settled]
    returns = [m["return_pct"] for m in settled_marks if m["return_pct"] is not None]
    wins = [1.0 if r > 0 else 0.0 for r in returns]

    def day_mean(day: int) -> float | None:
        vals = [mark_days[i][day]["return_pct"] for i in ids
                if i in mark_days and day in mark_days[i] and mark_days[i][day]["return_pct"] is not None]
        return _mean(vals)

    return {
        "calls": len(ids), "open": len(open_ids), "settled": len(settled_marks),
        "hit_rate": _mean(wins) if returns else None,
        "mean_return": _mean(returns) if returns else None,
        "day7_mean_return": day_mean(7), "day28_mean_return": day_mean(28),
    }


def _recent_row(call: dict[str, Any], settled: dict[str, Any] | None, latest: dict[str, Any] | None) -> dict[str, Any]:
    if settled is not None:
        status = "WIN" if settled["return_pct"] is not None and settled["return_pct"] > 0 else "LOSS"
        latest_return = _finite(settled["return_pct"])
    else:
        status = "OPEN"
        latest_return = _finite(latest["return_pct"]) if latest else None
    return {"id": call["id"], "kind": call["kind"], "call_ts": call["call_ts"], "wallet": call["wallet"],
            "question": call["question"], "category": call["category"], "tier": call["tier"],
            "entry_cost": _finite(call["entry_cost"]), "status": status, "latest_return": latest_return,
            "missed_rule": call.get("missed_rule")}


def build_summary(calls: list[dict[str, Any]], marks: list[dict[str, Any]], cfg: Config, now: int) -> dict[str, Any]:
    settled = {m["call_id"]: m for m in marks if m["type"] == "SETTLEMENT"}
    mark_days: dict[str, dict[int, dict[str, Any]]] = {}
    latest: dict[str, dict[str, Any]] = {}
    for m in marks:
        if m["type"] == "CHECKPOINT":
            mark_days.setdefault(m["call_id"], {})[m["day"]] = m
        if m["call_id"] not in latest or m["at"] >= latest[m["call_id"]]["at"]:
            latest[m["call_id"]] = m

    by_call = {c["id"]: c for c in calls}
    open_n = sum(1 for c in calls if c["id"] not in settled)
    by_kind = {k: _kind_stats([c for c in calls if c["kind"] == k], by_call, mark_days, settled) for k in KINDS}

    recent = sorted(calls, key=lambda c: -c["call_ts"])[:100]
    recent_rows = [_recent_row(c, settled.get(c["id"]), latest.get(c["id"])) for c in recent]

    n_scored = len(settled)
    analysis: dict[str, Any] = {"status": "INSUFFICIENT DATA", "n_scored": n_scored, "features": {},
                                "buckets": {}, "model": {"status": "INSUFFICIENT DATA", "auc": None,
                                                         "coefficients": None, "n": n_scored}}
    if n_scored >= cfg.ledger.min_scored:
        analysis["status"] = "OK"
        scored_ids = [c["id"] for c in calls if c["id"] in settled]
        returns = [settled[i]["return_pct"] for i in scored_ids]
        wins = [r is not None and r > 0 for r in returns]
        feature_rows = []
        for feat in FEATURES:
            values = [by_call[i].get(feat) for i in scored_ids]
            pairs = [(v, w, r) for v, w, r in zip(values, wins, returns) if v is not None]
            if not pairs:
                continue
            fvals, fwins, frets = zip(*pairs)
            winner_vals = [v for v, w in zip(fvals, fwins) if w]
            loser_vals = [v for v, w in zip(fvals, fwins) if not w]
            u, p = mann_whitney_u(winner_vals, loser_vals)
            analysis["features"][feat] = {
                "n_winners": len(winner_vals), "n_losers": len(loser_vals),
                "winners_median": _finite(float(np.median(winner_vals))) if winner_vals else None,
                "losers_median": _finite(float(np.median(loser_vals))) if loser_vals else None,
                "winners_mean": _mean(winner_vals), "losers_mean": _mean(loser_vals), "p_value": _finite(p),
            }
            analysis["buckets"][feat] = quantile_buckets(list(fvals), list(fwins), list(frets))

        model_rows, model_labels = [], []
        for i in scored_ids:
            c = by_call[i]
            vals = [c.get(f) for f in FEATURES]
            if all(v is not None for v in vals):
                model_rows.append([float(v) for v in vals])
                model_labels.append(settled[i]["return_pct"] is not None and settled[i]["return_pct"] > 0)
        analysis["model"] = _fit_model(model_rows, model_labels)

    return {
        "generated_at": now,
        "totals": {"calls": len(calls), "open": open_n, "settled": len(settled)},
        "by_kind": by_kind,
        "recent": recent_rows,
        "analysis": analysis,
    }
