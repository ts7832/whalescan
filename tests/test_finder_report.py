import json
from dataclasses import replace

import numpy as np
import pandas as pd

from whalescan.config import load_config
from whalescan.finder_report import finder_results, write_outputs

CFG = load_config()
SC = replace(CFG.study, bootstrap=60, min_wallets=3, train_fraction=0.6)
CFG_FAST = replace(CFG, study=SC)
DAY = 86400
D = SC.headline_delay_min


def moves(n_informed=4, n_noise=8, per_wallet=20, train_edge=0.3, test_edge=0.3, seed=1, bucket="0.40-0.60"):
    """A synthetic moves table. `n_informed` wallets move the price far more often than the base rate (a real
    "informed" pattern — the base rate, estimated from ALL wallets including the noisy majority, sits well below
    them); `n_noise` wallets move it at exactly the base rate, so they never qualify. Bets are interleaved by
    time across all wallets (not wallet-by-wallet), so a global train/test time cutoff still gives every wallet
    both training bets (to qualify on) and test bets (to be judged on). Informed wallets' COPY-TRADE return is
    `train_edge` above cost in training and `test_edge` above cost in test — the GO/NO-GO signal to test against."""
    rng = np.random.default_rng(seed)
    wallets = [(f"i{k}", True) for k in range(n_informed)] + [(f"n{k}", False) for k in range(n_noise)]
    rows, t0, idx = [], 1_780_000_000, 0
    for k in range(per_wallet):
        is_train = k < per_wallet * SC.train_fraction
        for wallet, informed in wallets:
            t = t0 + idx * DAY // 2
            idx += 1
            hit_p = 0.90 if informed else 0.45
            hit = rng.uniform() < hit_p
            cost = 0.5
            edge = (train_edge if is_train else test_edge) if informed else 0.0
            won = rng.uniform() < cost + edge
            row = {"id": f"m{idx}", "wallet": wallet, "condition_id": f"c{idx}", "asset": f"a{idx}",
                   "event_slug": f"e{idx}", "question": f"Q{idx}", "category": "POLITICS", "outcome": "Yes",
                   "first_ts": t, "signal_ts": t, "known_ts": t + 24 * 3600,
                   "week": pd.Timestamp(t, unit="s").to_period("W-SUN").start_time.date().isoformat(),
                   "usdc": 20_000.0, "p0": cost, "bucket": bucket, "move_hit": float(hit), "is_open": False,
                   "payout": float(won), "irregular": False, "won": bool(won), "resolved_ts": t + 5 * DAY}
            for d in SC.entry_delays_min:
                row[f"entry_{d}"] = cost
                row[f"ret_{d}"] = (float(won) - cost) / cost
                row[f"days_held_{d}"] = 5.0
            rows.append(row)
    return pd.DataFrame(rows)


def test_the_headline_is_the_test_period_not_the_training_period():
    res = finder_results(moves(train_edge=0.3, test_edge=-0.3), CFG_FAST)
    assert res["headline"]["period"] == "test"
    assert res["headline"]["mean_return"] < 0  # good in training, bad in the unseen test period: must say so


def test_all_three_preregistered_variants_are_reported():
    res = finder_results(moves(), CFG_FAST)
    assert set(res["variants"]) == {"5/2.33", "10/2.33", "5/3"}
    for v in res["variants"].values():
        assert "train" in v and "test" in v


def test_the_headline_variant_is_the_configured_primary_not_cherry_picked():
    res = finder_results(moves(), CFG_FAST)
    key = f"{CFG_FAST.finder.min_bets:g}/{CFG_FAST.finder.min_z:g}"
    assert res["headline"]["variant"] == key


def test_go_no_go_says_no_when_the_test_period_loses():
    res = finder_results(moves(train_edge=0.3, test_edge=-0.3), CFG_FAST)
    assert res["verdict"]["go"] is False
    assert any(not c["passed"] for c in res["verdict"]["checks"])


def test_go_no_go_says_go_when_every_check_passes():
    res = finder_results(moves(train_edge=0.3, test_edge=0.3), CFG_FAST)
    assert res["verdict"]["go"] is True, res["verdict"]


def test_outputs_are_strict_json_and_markdown(tmp_path):
    df = moves()
    res = finder_results(df, CFG_FAST)
    paths = write_outputs(res, tmp_path, stamp="2026-09-30")

    def reject(c):
        raise AssertionError(f"non-JSON constant {c}")

    json.loads(paths["json"].read_text(), parse_constant=reject)
    md = paths["markdown"].read_text()
    for section in ("Headline", "Variants", "Bets per week", "Go / no-go"):
        assert section in md


def test_cli_finder_study_prints_the_headline_and_the_report_path(monkeypatch, capsys, tmp_path):
    from whalescan import cli

    def fake(cfg, **kwargs):
        return {"headline": {"variant": "5/2.33", "delay_min": 15, "period": "test", "mean_return": 0.06,
                             "ci_lo": -0.01, "ci_hi": 0.14, "n_bets": 80, "n_wallets": 22, "status": "OK"},
                "verdict": {"go": False}, "paths": {"markdown": tmp_path / "r.md"}}

    monkeypatch.setattr(cli, "run_finder_study", fake)
    assert cli.main(["finder-study"]) == 0
    out = capsys.readouterr().out
    assert "5/2.33" in out and "+6.0%" in out and "NO-GO" in out and "r.md" in out
