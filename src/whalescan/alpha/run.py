"""Alpha hunt tests T1–T5 (pre-registration §3–§4).

    python -m whalescan.alpha.run explore   # OLD only: every test, BH correction, writes finalists.json
    python -m whalescan.alpha.run holdout   # NEW: only the finalists, exactly as frozen in finalists.json

`explore` drops every NEW row on load, so nothing it decides can depend on the holdout.
"""

from __future__ import annotations

import argparse
import os
import json
import logging
import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.inspection import permutation_importance
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import log_loss

from whalescan.alpha import SPLIT_TS
from whalescan.alpha.stats import (benjamini_hochberg, bucket, cluster_boot, cluster_boot_spearman, deflated_sharpe,
                                   monthly_ic, newey_west_t, quantile_cuts)

log = logging.getLogger(__name__)
DAY = 86400
OUT = Path(os.environ.get("ALPHA_OUT", "data/alpha"))
PRICE_BAND = (0.02, 0.95)  # copy entries outside it are near-certain "bond" buys or lottery tickets
EMBARGO_S = 7 * DAY
MIN_TRAIN_MONTHS = 6
MAX_FINALISTS = 10
CATS = ["POLITICS", "GEOPOLITICS", "ECONOMY", "TECH", "CULTURE"]

# (column, transform) — rank ICs ignore monotone transforms; the model sees the transformed value
FEATURES: dict[str, str] = {
    "age_days": "log1p", "log_markets_at_bet": "id", "first_bet": "id", "w_prior_bets": "log1p",
    "w_prior_resolved": "log1p", "w_prior_edge": "id", "w_prior_z": "id", "w_cat_share": "id",
    "w_bets_per_week": "log1p", "log_usdc": "id", "stake_vs_median": "log", "stake_vs_market": "id",
    "n_fills": "log1p", "whale_price": "id", "days_to_end": "log1p0", "offhours": "id", "weekend": "id",
    "m_age_days": "log1p", "impact": "id", "momentum_24h": "id", "crowd_before": "log1p",
    "fresh_cluster": "id", "opp_ratio": "log1p", "m_bigvol_before": "log1p",
    **{f"cat_{c}": "id" for c in CATS},
}
TARGETS = ["edge_copy", "ret_copy", "edge_whale", "mo_24h", "mtm_7d"]
COPY_TARGETS = ("edge_copy", "ret_copy")


def load(phase_rows: str) -> pd.DataFrame:
    b = pd.read_parquet(OUT / "bets.parquet")
    if phase_rows == "OLD":
        b = b[b["signal_ts"] < SPLIT_TS]
    b = b.copy()
    b["month"] = pd.to_datetime(b["signal_ts"], unit="s", utc=True).dt.strftime("%Y-%m")
    b["offhours"] = (b["hour_utc"] < 12).astype(float)  # 00–12 UTC: night in the Americas
    b["weekend"] = (b["weekday"] >= 5).astype(float)
    for c in CATS:
        b[f"cat_{c}"] = (b["category"] == c).astype(float)
    b["first_bet"] = b["first_bet"].astype(float)
    b["event_slug"] = b["event_slug"].fillna(b["condition_id"])
    for col, tr in FEATURES.items():
        x = b[col].astype(float)
        b[f"f_{col}"] = {"id": x, "log1p": np.log1p(x.clip(lower=0)), "log": np.log(x.clip(lower=1e-6)),
                         "log1p0": np.log1p(x.clip(lower=0))}[tr]
    b["in_band"] = b["entry_cost"].between(*PRICE_BAND)
    return b


def universe(b: pd.DataFrame) -> pd.DataFrame:
    """Bets a copier could have entered (priced at +15 min, inside the band)."""
    return b[b["in_band"]]


# ---------------------------------------------------------------- T1: one feature at a time
def t1(u: pd.DataFrame) -> list[dict[str, Any]]:
    rows = []
    for col in FEATURES:
        f = f"f_{col}"
        for y in TARGETS:
            ics = monthly_ic(u, f, y)
            t, p = newey_west_t(ics)
            rows.append({"test": "T1", "feature": col, "target": y, "months": int(len(ics)),
                         "mean_ic": float(ics.mean()) if len(ics) else None, "nw_t": t, "p": p,
                         "same_sign_share": float((np.sign(ics) == np.sign(ics.mean())).mean()) if len(ics) else None})
    return rows


def quintile_strategy(u: pd.DataFrame, col: str, sign: float) -> dict[str, Any]:
    """Cuts fixed on `u`; the favourable extreme quintile (top if sign > 0, else bottom)."""
    f = f"f_{col}"
    cuts = quantile_cuts(u[f], 5)
    q = bucket(u[f], cuts)
    if q.nunique() < 2:
        return {"cuts": cuts, "pick": None}
    pick = int(q.max()) if sign > 0 else int(q.min())
    return {"cuts": cuts, "pick": pick}


# ---------------------------------------------------------------- T2: model, walk-forward with purge + embargo
def model_features(train: pd.DataFrame | None = None) -> list[str]:
    """Every feature column, minus any (almost) never observed in `train` — the trees cannot bin an empty one."""
    cols = [f"f_{c}" for c in FEATURES]
    return cols if train is None else [c for c in cols if train[c].notna().sum() >= 50]


def _hgb() -> HistGradientBoostingRegressor:
    return HistGradientBoostingRegressor(max_depth=3, learning_rate=0.04, max_iter=250, min_samples_leaf=300,
                                         l2_regularization=1.0, random_state=0)


def t2(u: pd.DataFrame) -> dict[str, Any]:
    X = model_features()
    months = sorted(u["month"].unique())
    oof = pd.Series(np.nan, index=u.index)
    oof_logit = pd.Series(np.nan, index=u.index)
    for m in months[MIN_TRAIN_MONTHS:]:
        start = int(pd.Timestamp(m + "-01", tz="UTC").timestamp())
        test = u[(u["month"] == m) & u["edge_copy"].notna()]
        # purge: train only on bets whose OUTCOME was known before the test month (minus an embargo)
        train = u[(u["signal_ts"] < start - EMBARGO_S) & (u["resolved_ts"] < start - EMBARGO_S) & u["edge_copy"].notna()]
        if len(train) < 2000 or test.empty:
            continue
        X = model_features(train)
        mdl = _hgb().fit(train[X], train["edge_copy"])
        oof.loc[test.index] = mdl.predict(test[X])
        lr_X = train[X].fillna(0.0).assign(logit_p=np.log(train["entry_cost"] / (1 - train["entry_cost"])))
        lr = LogisticRegression(C=0.1, max_iter=2000).fit(lr_X, train["won"].astype(int))
        te_X = test[X].fillna(0.0).assign(logit_p=np.log(test["entry_cost"] / (1 - test["entry_cost"])))
        oof_logit.loc[test.index] = lr.predict_proba(te_X)[:, 1]
    d = u.assign(pred=oof, p_lr=oof_logit).dropna(subset=["pred"])
    d["pred_q"] = d.groupby("month")["pred"].transform(lambda s: pd.qcut(s.rank(method="first"), 5, labels=False))
    quint = [{"q": int(q), **cluster_boot(g, "edge_copy"), "ret": cluster_boot(g, "ret_copy")["mean"]}
             for q, g in d.groupby("pred_q")]
    top = d[d["pred_q"] == 4]
    weekly = top.groupby(pd.to_datetime(top["signal_ts"], unit="s").dt.to_period("W"))["edge_copy"].mean()
    m = d.dropna(subset=["p_lr"])
    p_mkt = m["entry_cost"].clip(1e-4, 1 - 1e-4)
    ll_mkt = log_loss(m["won"].astype(int), p_mkt)
    ll_lr = log_loss(m["won"].astype(int), m["p_lr"].clip(1e-4, 1 - 1e-4))
    # importance on the last OOF quarter, from a model trained before it (no look-ahead)
    last = months[-3]
    start = int(pd.Timestamp(last + "-01", tz="UTC").timestamp())
    tr = u[(u["resolved_ts"] < start - EMBARGO_S) & u["edge_copy"].notna()]
    te = u[(u["signal_ts"] >= start) & u["edge_copy"].notna()]
    X = model_features(tr)
    mdl = _hgb().fit(tr[X], tr["edge_copy"])
    pi = permutation_importance(mdl, te[X], te["edge_copy"], n_repeats=5, random_state=0)
    imp = sorted(({"feature": X[i][2:], "importance": float(pi.importances_mean[i])} for i in range(len(X))),
                 key=lambda r: -r["importance"])
    return {"oof_bets": int(len(d)), "oof_ic": cluster_boot_spearman(d, "pred", "edge_copy", n=300),
            "quintiles": quint, "top_quintile": cluster_boot(top, "edge_copy"),
            "top_quintile_ret": cluster_boot(top, "ret_copy"), "weekly_top": weekly.to_numpy().tolist(),
            "logloss_market": float(ll_mkt), "logloss_model": float(ll_lr), "beats_market": bool(ll_lr < ll_mkt),
            "importance": imp[:12]}


# ---------------------------------------------------------------- T3: wallet skill persistence
def _shrunk(e: np.ndarray, k: float = 5.0) -> float:
    return float(e.sum() / (len(e) + k))


def t3(b: pd.DataFrame, min_each: int = 5) -> dict[str, Any]:
    r = b[b["payout"].notna()].copy()
    r["e"] = r["payout"] - r["whale_price"]
    rows = []
    for w, g in r.sort_values("signal_ts").groupby("wallet"):
        if len(g) < 2 * min_each:
            continue
        mid = g["signal_ts"].iloc[len(g) // 2]
        first, second = g[g["signal_ts"] < mid], g[g["signal_ts"] >= mid + EMBARGO_S]
        if len(first) < min_each or len(second) < min_each:
            continue
        rows.append({"wallet": w, "n1": len(first), "n2": len(second), "e1": _shrunk(first["e"].to_numpy()),
                     "e2": _shrunk(second["e"].to_numpy()), "copy2": second["edge_copy"].mean()})
    w = pd.DataFrame(rows)
    if len(w) < 20:
        return {"wallets": int(len(w))}
    w["d1"] = pd.qcut(w["e1"].rank(method="first"), 10, labels=False)
    trans = w.groupby("d1").agg(n=("wallet", "size"), e1=("e1", "mean"), e2=("e2", "mean"),
                                copy2=("copy2", "mean")).reset_index().to_dict("records")
    rho = w["e1"].rank().corr(w["e2"].rank())
    n = len(w)
    t = rho * math.sqrt((n - 2) / max(1e-12, 1 - rho ** 2))
    from scipy import stats as st
    return {"wallets": int(n), "spearman_halves": float(rho), "t": float(t), "p": float(2 * st.t.sf(abs(t), n - 2)),
            "deciles": trans}


# ---------------------------------------------------------------- T5: market-wide effects
PRICE_BUCKETS = [0.02, 0.1, 0.2, 0.35, 0.5, 0.65, 0.8, 0.9, 0.95]


def t5(u: pd.DataFrame) -> list[dict[str, Any]]:
    rows = []
    pb = pd.cut(u["entry_cost"], PRICE_BUCKETS)
    for (cat, p), g in u.groupby([u["category"], pb], observed=True):
        s = cluster_boot(g, "edge_copy")
        rows.append({"test": "T5-cal", "category": cat, "bucket": str(p), **s})
    for (p,), g in u.groupby([pb], observed=True):
        rows.append({"test": "T5-cal", "category": "ALL", "bucket": str(p), **cluster_boot(g, "edge_copy")})
    for h in ("mtm_24h", "mtm_7d"):
        rows.append({"test": "T5-drift", "category": "ALL", "bucket": h, **cluster_boot(u, h)})
    return rows


# ---------------------------------------------------------------- explore (OLD only)
def explore() -> None:
    b = load("OLD")
    u = universe(b)
    log.info("explore: %d OLD bets, %d in the copy universe (%d resolved)", len(b), len(u), u["edge_copy"].notna().sum())
    res: dict[str, Any] = {"universe": {"bets": int(len(b)), "copyable": int(len(u)),
                                        "resolved": int(u["edge_copy"].notna().sum())}}

    # T1 + BH over every feature × target test
    t1_rows = t1(u)
    for r, keep in zip(t1_rows, benjamini_hochberg([r["p"] for r in t1_rows], 0.10)):
        r["bh_pass"] = keep
    res["T1"] = t1_rows
    log.info("T1: %d tests, %d pass BH", len(t1_rows), sum(r["bh_pass"] for r in t1_rows))

    # quintile strategies for every T1 feature (all evaluated: they count as trials for the deflated Sharpe)
    strategies = []
    for col in FEATURES:
        ics = next(r for r in t1_rows if r["feature"] == col and r["target"] == "edge_copy")
        sign = 1.0 if (ics["mean_ic"] or 0) >= 0 else -1.0
        qs = quintile_strategy(u, col, sign)
        if qs["pick"] is None:
            continue
        sel = u[bucket(u[f"f_{col}"], qs["cuts"]) == qs["pick"]]
        weekly = sel.groupby(pd.to_datetime(sel["signal_ts"], unit="s").dt.to_period("W"))["edge_copy"].mean()
        strategies.append({"name": f"T1:{col}", "feature": col, "sign": sign, **qs, "edge": cluster_boot(sel, "edge_copy"),
                           "ret": cluster_boot(sel, "ret_copy"), "fade": cluster_boot(sel, "edge_fade"),
                           "weekly": weekly.dropna().to_numpy().tolist()})
    res["T1_strategies"] = strategies

    res["T2"] = t2(u)
    log.info("T2: top quintile %s, beats market %s", res["T2"]["top_quintile"], res["T2"]["beats_market"])
    res["T3"] = t3(b)
    log.info("T3: %s", {k: v for k, v in res["T3"].items() if k != "deciles"})
    res["T5"] = t5(u)

    # T4: fade every strategy and evidence rule that LOST on OLD
    fades = [{"name": s["name"], **s["fade"]} for s in strategies if (s["edge"]["mean"] or 0) < 0]
    for rule in ("r_insider", "r_near_miss", "r_fresh", "r_baseline"):
        sel = u[u[rule].astype(bool)]
        if (cluster_boot(sel, "edge_copy")["mean"] or 0) < 0:
            fades.append({"name": f"fade:{rule}", **cluster_boot(sel, "edge_fade")})
    res["T4"] = fades

    # ---------------- deflated Sharpe for the best strategy, over every strategy tried
    trial_srs = [np.mean(s["weekly"]) / np.std(s["weekly"], ddof=1) for s in strategies
                 if len(s["weekly"]) > 10 and np.std(s["weekly"], ddof=1) > 0]
    best = max(strategies, key=lambda s: (s["edge"]["lo"] or -9))
    res["best_strategy"] = {"name": best["name"], "edge": best["edge"],
                            "deflated": deflated_sharpe(np.array(best["weekly"]), len(trial_srs) + 2,
                                                        float(np.std(trial_srs)))}

    # ---------------- finalists (frozen before NEW is opened)
    finalists: list[dict[str, Any]] = []
    passing = {r["feature"] for r in t1_rows if r["bh_pass"] and r["target"] in COPY_TARGETS}
    ranked = sorted((s for s in strategies if s["feature"] in passing),
                    key=lambda s: -abs(next(r for r in t1_rows if r["feature"] == s["feature"]
                                            and r["target"] == "edge_copy")["nw_t"] or 0))
    for s in ranked[:6]:
        finalists.append({"kind": "quintile", "name": s["name"], "feature": s["feature"], "cuts": s["cuts"],
                          "pick": s["pick"], "sign": s["sign"]})
    finalists.append({"kind": "model", "name": "T2:model top quintile"})
    finalists.append({"kind": "wallet", "name": "T3:prior wallet z top decile",
                      "cut": float(np.nanquantile(u.loc[u["w_prior_resolved"] >= 10, "w_prior_z"], 0.9))})
    good_fades = [f for f in res["T4"] if (f.get("lo") or -1) > 0]
    if good_fades:
        f = max(good_fades, key=lambda f: f["lo"])
        finalists.append({"kind": "fade", "name": f["name"]})
    cal = [r for r in res["T5"] if r["test"] == "T5-cal" and (r.get("lo") or -1) > 0 and r["n"] >= 200]
    if cal:
        c = max(cal, key=lambda r: r["lo"])
        finalists.append({"kind": "calibration", "name": f"T5:{c['category']} {c['bucket']}",
                          "category": c["category"], "bucket": c["bucket"]})
    res["finalists"] = finalists[:MAX_FINALISTS]
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "explore.json").write_text(json.dumps(res, indent=1, default=str))
    (OUT / "finalists.json").write_text(json.dumps(res["finalists"], indent=1))
    log.info("finalists frozen: %s", [f["name"] for f in res["finalists"]])


# ---------------------------------------------------------------- holdout (NEW, finalists only)
def _verdict(old: dict[str, Any], new: dict[str, Any], n_wallets: int) -> str:
    same = (old.get("mean") or 0) * (new.get("mean") or 0) > 0 and (new.get("mean") or 0) > 0
    if not same:
        return "DEAD"
    if (new.get("lo") or -1) > 0 and new.get("clusters", 0) >= 30 and n_wallets >= 10:
        return "REAL"
    return "IN THE ROUGH"


def holdout() -> None:
    finalists = json.loads((OUT / "finalists.json").read_text())
    full = load("ALL")
    old, new = universe(full[full["signal_ts"] < SPLIT_TS]), universe(full[full["signal_ts"] >= SPLIT_TS])
    out = []
    for f in finalists:
        if f["kind"] == "quintile":
            col = f"f_{f['feature']}"
            sel_old = old[bucket(old[col], f["cuts"]) == f["pick"]]
            sel_new = new[bucket(new[col], f["cuts"]) == f["pick"]]
            ic_new = cluster_boot_spearman(new, col, "edge_copy", n=300)
        elif f["kind"] == "model":
            tr = old[(old["resolved_ts"] < SPLIT_TS - EMBARGO_S) & old["edge_copy"].notna()]
            X = model_features(tr)
            mdl = _hgb().fit(tr[X], tr["edge_copy"])
            cut = float(np.quantile(mdl.predict(old[old["edge_copy"].notna()][X]), 0.8))
            sel_old = old[old["edge_copy"].notna()][mdl.predict(old[old["edge_copy"].notna()][X]) >= cut]
            sel_new = new[mdl.predict(new[X]) >= cut]
            ic_new = cluster_boot_spearman(new.assign(pred=mdl.predict(new[X])), "pred", "edge_copy", n=300)
        elif f["kind"] == "wallet":
            m_old = (old["w_prior_resolved"] >= 10) & (old["w_prior_z"] >= f["cut"])
            m_new = (new["w_prior_resolved"] >= 10) & (new["w_prior_z"] >= f["cut"])
            sel_old, sel_new, ic_new = old[m_old], new[m_new], None
        elif f["kind"] == "fade":
            rule = f["name"].split(":", 1)[1]
            if rule.startswith("r_"):
                sel_old, sel_new = old[old[rule].astype(bool)], new[new[rule].astype(bool)]
            else:
                s = next(s for s in json.loads((OUT / "explore.json").read_text())["T1_strategies"]
                         if s["name"] == rule)
                col = f"f_{s['feature']}"
                sel_old = old[bucket(old[col], s["cuts"]) == s["pick"]]
                sel_new = new[bucket(new[col], s["cuts"]) == s["pick"]]
            ic_new = None
        else:  # calibration bucket
            def pick(d: pd.DataFrame) -> pd.DataFrame:
                pb = pd.cut(d["entry_cost"], PRICE_BUCKETS).astype(str)
                m = pb == f["bucket"]
                return d[m & (d["category"] == f["category"])] if f["category"] != "ALL" else d[m]
            sel_old, sel_new, ic_new = pick(old), pick(new), None
        col = "edge_fade" if f["kind"] == "fade" else "edge_copy"
        mtm = "mtm_7d"
        o, n = cluster_boot(sel_old, col), cluster_boot(sel_new, col)
        row = {"name": f["name"], "old": o, "new": n, "new_ret": cluster_boot(sel_new, "ret_copy"),
               "new_mtm_7d": cluster_boot(sel_new, mtm) if f["kind"] != "fade" else None,
               "new_ic": ic_new, "new_wallets": int(sel_new.dropna(subset=[col])["wallet"].nunique()),
               "new_bets_all": int(len(sel_new))}
        row["verdict"] = _verdict(o, n, row["new_wallets"])
        out.append(row)
        log.info("holdout %-40s OLD %s NEW %s -> %s", f["name"], o["mean"], n["mean"], row["verdict"])
    (OUT / "holdout.json").write_text(json.dumps(out, indent=1, default=str))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("phase", choices=["explore", "holdout"])
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    {"explore": explore, "holdout": holdout}[a.phase]()


if __name__ == "__main__":
    main()
