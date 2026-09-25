import json

from whalescan.config import load_config
from whalescan.ledger_analysis import build_summary, mann_whitney_u, quantile_buckets

CFG = load_config()
NOW = 1_790_000_000
DAY = 86400


def test_mann_whitney_u_on_two_clearly_separated_groups():
    a = [1.0, 2.0, 3.0, 4.0]
    b = [5.0, 6.0, 7.0, 8.0]
    u, p = mann_whitney_u(a, b)
    assert u == 0.0  # hand-computed: every value in a ranks below every value in b
    assert p < 0.05


def test_mann_whitney_u_with_ties_matches_hand_computed_u():
    a = [1.0, 1.0, 2.0]
    b = [1.0, 2.0, 3.0]
    u, p = mann_whitney_u(a, b)
    assert u == 2.5  # hand-computed with averaged tie ranks
    assert 0.0 <= p <= 1.0


def test_mann_whitney_u_on_identical_distributions_gives_a_large_p_value():
    a = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    b = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    _u, p = mann_whitney_u(a, b)
    assert p > 0.5


def test_mann_whitney_u_needs_at_least_one_value_each_side():
    u, p = mann_whitney_u([], [1.0])
    assert u is None and p is None


def test_quantile_buckets_splits_values_and_reports_win_rate_and_mean_return():
    values = [float(i) for i in range(20)]
    wins = [v >= 10 for v in values]
    returns = [1.0 if w else -1.0 for w in wins]
    buckets = quantile_buckets(values, wins, returns, n_buckets=4)
    assert len(buckets) == 4
    assert sum(b["n"] for b in buckets) == 20
    assert buckets[0]["win_rate"] == 0.0 and buckets[-1]["win_rate"] == 1.0
    assert buckets[0]["mean_return"] == -1.0 and buckets[-1]["mean_return"] == 1.0
    assert buckets[0]["lo"] <= buckets[0]["hi"] <= buckets[1]["lo"]


def test_quantile_buckets_with_fewer_values_than_buckets_does_not_crash():
    buckets = quantile_buckets([1.0, 2.0], [True, False], [0.1, -0.1], n_buckets=4)
    assert sum(b["n"] for b in buckets) == 2


def call(id_, kind="INSIDER", age_days=3.0, markets=2, usdc=15000.0, call_ts=NOW, missed_rule=None, tier="B"):
    return {"id": id_, "kind": kind, "missed_rule": missed_rule, "call_ts": call_ts, "wallet": "0xw",
            "condition_id": "0xc", "asset": "yes", "outcome": "Yes", "category": "POLITICS", "tier": tier,
            "question": "q?", "end_ts": call_ts + 60 * DAY, "whale_price": 0.4, "usdc": usdc, "n_fills": 1,
            "entry_vwap": 0.41, "entry_estimated": False, "entry_cost": 0.42, "age_days": age_days,
            "markets_traded": markets, "consensus": 1, "volume": 1e6}


def settlement(call_id, return_pct, at):
    return {"call_id": call_id, "type": "SETTLEMENT", "day": None, "at": at, "best_bid": None, "payout": None,
            "irregular": False, "return_pct": return_pct}


def checkpoint(call_id, day, return_pct, at):
    return {"call_id": call_id, "type": "CHECKPOINT", "day": day, "at": at, "best_bid": 0.5,
            "return_pct": return_pct}


def test_summary_reports_insufficient_data_below_min_scored():
    calls = [call(f"c{i}") for i in range(5)]
    marks = [settlement(f"c{i}", 0.1, NOW) for i in range(5)]
    summary = build_summary(calls, marks, CFG, NOW)
    assert summary["analysis"]["status"] == "INSUFFICIENT DATA"
    assert summary["analysis"]["n_scored"] == 5


def test_summary_headline_totals_and_by_kind_hit_rate():
    calls = [call("a", kind="INSIDER"), call("b", kind="INSIDER"), call("c", kind="SNIPER")]
    marks = [settlement("a", 0.5, NOW), settlement("b", -0.3, NOW)]  # "c" still open
    summary = build_summary(calls, marks, CFG, NOW)
    assert summary["totals"] == {"calls": 3, "open": 1, "settled": 2}
    ins = summary["by_kind"]["INSIDER"]
    assert ins["calls"] == 2 and ins["settled"] == 2
    assert ins["hit_rate"] == 0.5  # one win (0.5), one loss (-0.3)
    assert ins["mean_return"] == (0.5 + -0.3) / 2
    sniper = summary["by_kind"]["SNIPER"]
    assert sniper["settled"] == 0 and sniper["hit_rate"] is None and sniper["mean_return"] is None


def test_summary_checkpoint_means_ignore_missing_marks():
    calls = [call("a")]
    marks = [checkpoint("a", 7, 0.2, NOW), {"call_id": "a", "type": "CHECKPOINT", "day": 14, "at": NOW,
                                            "best_bid": None, "return_pct": None, "missing": True}]
    summary = build_summary(calls, marks, CFG, NOW)
    assert summary["by_kind"]["INSIDER"]["day7_mean_return"] == 0.2
    assert summary["by_kind"]["INSIDER"]["day28_mean_return"] is None  # no day-28 mark at all


def test_summary_recent_calls_are_capped_at_100_newest_first():
    calls = [call(f"c{i}", call_ts=NOW + i) for i in range(120)]
    summary = build_summary(calls, [], CFG, NOW + 200)
    assert len(summary["recent"]) == 100
    assert summary["recent"][0]["id"] == "c119"


def test_summary_recent_call_carries_its_latest_mark():
    calls = [call("a")]
    marks = [checkpoint("a", 7, 0.2, NOW + 7 * DAY), checkpoint("a", 14, 0.4, NOW + 14 * DAY)]
    summary = build_summary(calls, marks, CFG, NOW + 15 * DAY)
    [row] = summary["recent"]
    assert row["latest_return"] == 0.4 and row["status"] == "OPEN"


def test_summary_settled_call_status_is_win_or_loss():
    calls = [call("a"), call("b")]
    marks = [settlement("a", 0.3, NOW), settlement("b", -0.2, NOW)]
    summary = build_summary(calls, marks, CFG, NOW)
    statuses = {r["id"]: r["status"] for r in summary["recent"]}
    assert statuses == {"a": "WIN", "b": "LOSS"}


def test_summary_feature_analysis_appears_once_min_scored_is_reached():
    calls, marks = [], []
    for i in range(35):
        won = i % 2 == 0
        c = call(f"c{i}", age_days=1.0 if won else 20.0)  # winners systematically younger
        calls.append(c)
        marks.append(settlement(f"c{i}", 0.5 if won else -0.5, NOW))
    summary = build_summary(calls, marks, CFG, NOW)
    assert summary["analysis"]["status"] == "OK"
    age = summary["analysis"]["features"]["age_days"]
    assert age["n_winners"] == 18 and age["n_losers"] == 17
    assert age["winners_median"] < age["losers_median"]
    assert age["p_value"] is not None
    assert "age_days" in summary["analysis"]["buckets"]


def test_model_is_insufficient_data_below_200_scored():
    calls, marks = [], []
    for i in range(35):
        won = i % 2 == 0
        calls.append(call(f"c{i}"))
        marks.append(settlement(f"c{i}", 0.5 if won else -0.5, NOW))
    summary = build_summary(calls, marks, CFG, NOW)
    assert summary["analysis"]["model"]["status"] == "INSUFFICIENT DATA"
    assert summary["analysis"]["model"]["auc"] is None


def test_model_fits_with_200_or_more_scored_calls_and_reports_a_valid_auc():
    import random

    rng = random.Random(7)
    calls, marks = [], []
    for i in range(220):
        age = rng.uniform(0, 30)
        won = age < 10 and rng.random() < 0.8
        calls.append(call(f"c{i}", age_days=age))
        marks.append(settlement(f"c{i}", 0.6 if won else -0.4, NOW))
    summary = build_summary(calls, marks, CFG, NOW)
    model = summary["analysis"]["model"]
    assert model["status"] == "OK"
    assert 0.0 <= model["auc"] <= 1.0
    assert model["n"] == 220


def test_summary_never_contains_nan_or_infinity():
    calls = [call("a", kind="SNIPER")]  # no marks at all -> every SNIPER stat is missing, not NaN
    summary = build_summary(calls, [], CFG, NOW)
    json.dumps(summary, allow_nan=False)  # raises ValueError on any NaN/Infinity float


def test_summary_generated_at_is_now():
    assert build_summary([], [], CFG, NOW)["generated_at"] == NOW


def irregular_settlement(call_id, return_pct, at, payout=0.5):
    return {"call_id": call_id, "type": "SETTLEMENT", "day": None, "at": at, "best_bid": None, "payout": payout,
           "irregular": True, "return_pct": return_pct}


def test_irregular_settlements_get_their_own_status_not_win_or_loss():
    calls = [call("a")]
    marks = [irregular_settlement("a", -0.53, NOW)]
    summary = build_summary(calls, marks, CFG, NOW)
    [row] = summary["recent"]
    assert row["status"] == "IRREGULAR"


def test_irregular_settlements_are_excluded_from_hit_rate_and_mean_return():
    calls = [call("a", kind="INSIDER"), call("b", kind="INSIDER")]
    marks = [settlement("a", 0.5, NOW), irregular_settlement("b", -0.9, NOW)]
    stats = build_summary(calls, marks, CFG, NOW)["by_kind"]["INSIDER"]
    assert stats["settled"] == 2       # both count toward "settled"
    assert stats["hit_rate"] == 1.0    # only the clean win counts toward hit rate
    assert stats["mean_return"] == 0.5  # the irregular return doesn't drag the mean down


def test_irregular_settlements_are_excluded_from_feature_analysis():
    calls, marks = [], []
    for i in range(35):
        won = i % 2 == 0
        calls.append(call(f"c{i}", age_days=1.0 if won else 20.0))
        marks.append(settlement(f"c{i}", 0.5 if won else -0.5, NOW))
    calls.append(call("irregular", age_days=999.0))  # an outlier that must not leak into the comparison
    marks.append(irregular_settlement("irregular", 0.0, NOW))
    summary = build_summary(calls, marks, CFG, NOW)
    assert summary["analysis"]["n_scored"] == 35  # irregular doesn't count as "scored" for the analysis gate
    age = summary["analysis"]["features"]["age_days"]
    assert age["n_winners"] + age["n_losers"] == 35
