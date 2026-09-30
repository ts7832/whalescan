import math

import numpy as np
import pandas as pd
import pytest

from whalescan.study_stats import (bets_per_week, factor_table, horizon_table, per_wallet_t, rule_summary, time_split,
                                   wallet_bootstrap_ci)

DAY = 86400


def frame(rows):
    return pd.DataFrame(rows)


def bets(n_wallets=20, per_wallet=5, mean=0.1, sd=0.5, seed=1, t0=1_700_000_000):
    rng = np.random.default_rng(seed)
    rows = []
    for w in range(n_wallets):
        for k in range(per_wallet):
            rows.append({"wallet": f"w{w}", "signal_ts": t0 + (w * per_wallet + k) * DAY, "ret_15": rng.normal(mean, sd),
                         "days_held_15": 10.0, "r_x": True, "won": True, "week": "2026-01-05", "is_open": False})
    return frame(rows)


def test_time_split_puts_the_newest_bets_in_the_test_set():
    df = bets()
    train, test = time_split(df, 0.667)
    assert train["signal_ts"].max() < test["signal_ts"].min()
    assert len(test) == pytest.approx(len(df) / 3, abs=1)


def test_wallet_bootstrap_is_wider_than_a_naive_one_when_one_wallet_dominates():
    # one wallet with 60 identical big winners; ten others losing a little
    rows = [{"wallet": "star", "ret_15": 1.0} for _ in range(60)] + \
           [{"wallet": f"w{i}", "ret_15": -0.1} for i in range(10) for _ in range(2)]
    df = frame(rows)
    mean, lo, hi = wallet_bootstrap_ci(df, "ret_15", n=2000, seed=3)
    assert mean == pytest.approx(df["ret_15"].mean())
    naive = df["ret_15"].std(ddof=1) / math.sqrt(len(df)) * 1.645
    assert (hi - lo) / 2 > 3 * naive  # the honest interval is far wider: it is really ~11 independent wallets


def test_wallet_bootstrap_90pct_interval_covers_the_truth_about_90pct_of_the_time():
    # a single interval may miss by chance; the method is right if it covers the true mean ~90% of the time
    hits = 0
    for s in range(200):
        df = bets(n_wallets=60, per_wallet=3, mean=0.05, sd=0.3, seed=1000 + s)
        _, lo, hi = wallet_bootstrap_ci(df, "ret_15", n=400, seed=s)
        hits += lo < 0.05 < hi
    assert 0.82 <= hits / 200 <= 0.97


def test_per_wallet_t_counts_each_wallet_once():
    df = frame([{"wallet": "a", "ret_15": 1.0}] * 50 + [{"wallet": "b", "ret_15": -1.0}] * 1
               + [{"wallet": "c", "ret_15": 0.0}] * 1)
    t, k = per_wallet_t(df, "ret_15")
    assert k == 3 and abs(t) < 1.5  # 50 bets from one wallet are not 50 pieces of evidence


def test_rule_summary_reports_per_dollar_hit_rate_ci_and_capital_use():
    df = bets(n_wallets=12, per_wallet=3, mean=0.1, seed=2)
    df["won"] = df["ret_15"] > 0
    s = rule_summary(df, "r_x", delay=15, n_boot=500, seed=1, min_wallets=10)
    assert s["n_bets"] == 36 and s["n_wallets"] == 12 and s["status"] == "OK"
    assert s["mean_return"] == pytest.approx(df["ret_15"].mean())
    assert s["hit_rate"] == pytest.approx(df["won"].mean())
    assert s["ci_lo"] < s["mean_return"] < s["ci_hi"]
    assert s["return_per_day"] == pytest.approx(df["ret_15"].mean() / 10.0)


def test_rule_summary_flags_too_few_wallets():
    s = rule_summary(bets(n_wallets=4, per_wallet=5), "r_x", delay=15, n_boot=100, seed=1, min_wallets=10)
    assert s["status"] == "INSUFFICIENT"


def test_rule_summary_ignores_unscored_bets():
    df = bets(n_wallets=12, per_wallet=2)
    df.loc[0, "ret_15"] = float("nan")  # skipped / unpriced / open
    assert rule_summary(df, "r_x", delay=15, n_boot=50, seed=1, min_wallets=1)["n_bets"] == 23


def test_bets_per_week_counts_by_bet_week_and_drops_partial_edge_weeks():
    weeks = ["2026-01-05"] * 2 + ["2026-01-12"] * 7 + ["2026-01-19"] * 5 + ["2026-01-26"] * 1
    df = frame([{"week": w, "r_x": True} for w in weeks] + [{"week": "2026-01-12", "r_x": False}])
    s = bets_per_week(df, "r_x")
    assert s["weeks"] == 2 and s["median"] == 6.0 and s["min"] == 5 and s["max"] == 7


def test_factor_buckets_come_from_train_and_are_applied_to_test():
    train = frame([{"age_days": a, "ret_15": 1.0 if a < 5 else -1.0} for a in range(10)])
    test = frame([{"age_days": a, "ret_15": 1.0 if a < 5 else -1.0} for a in (0, 1, 8, 9, 100)])
    t = factor_table(train, test, "age_days", "ret_15", n_buckets=2)
    assert [b["test_n"] for b in t["buckets"]] == [2, 3]  # 100 falls in the top train bucket, not a new one
    assert t["train_spearman"] < 0 and t["test_spearman"] < 0 and t["holds"]


def test_horizon_table_groups_by_days_held():
    df = frame([{"days_held_15": d, "ret_15": r} for d, r in [(0.5, 0.1), (3, 0.2), (3, 0.0), (40, 0.5), (200, 1.0)]])
    rows = {r["bucket"]: r for r in horizon_table(df, delay=15)}
    assert rows["<1d"]["n"] == 1 and rows["1-7d"]["n"] == 2 and rows["7-30d"]["n"] == 0
    assert rows["30-90d"]["n"] == 1 and rows[">90d"]["n"] == 1
    assert rows["1-7d"]["mean_return"] == pytest.approx(0.1)
    assert rows["1-7d"]["return_per_day"] == pytest.approx(0.1 / 3)
