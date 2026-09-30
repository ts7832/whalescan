import json
import math
from dataclasses import replace

import numpy as np
import pandas as pd

from whalescan.config import load_config
from whalescan.study_report import bet_log, render_markdown, study_results, write_outputs

CFG = load_config()
SC = replace(CFG.study, bootstrap=60, min_wallets=5)
CFG_FAST = replace(CFG, study=SC)
DAY = 86400
D = SC.headline_delay_min


def bets(n=300, train_edge=0.3, test_edge=0.3, seed=1, rules=("r_insider", "r_fresh", "r_baseline", "r_near_miss")):
    """Synthetic bet table: `n` resolved bets over time from 40 wallets; the older 2/3 win with `train_edge`
    above the price, the newest 1/3 with `test_edge`."""
    rng = np.random.default_rng(seed)
    rows = []
    for i in range(n):
        t = 1_780_000_000 + i * DAY // 2
        cost = 0.5
        edge = train_edge if i < n * SC.train_fraction else test_edge
        won = rng.uniform() < cost + edge
        row = {"id": f"b{i}", "wallet": f"w{i % 40}", "condition_id": f"c{i}", "asset": f"a{i}",
               "event_slug": f"e{i}", "question": f"Q{i}", "category": "POLITICS", "outcome": "Yes",
               "first_ts": t, "signal_ts": t, "week": pd.Timestamp(t, unit="s").to_period("W-SUN").start_time.date()
               .isoformat(), "usdc": 20_000.0, "whale_price": 0.5, "n_fills": 2, "age_days": 1.0,
               "age_inconsistent": False, "markets_at_bet": 1.0, "is_open": False, "payout": float(won),
               "irregular": False, "won": bool(won), "resolved_ts": t + 5 * DAY, "days_to_end": 5.0,
               "missed_rule": None,
               "crowd_before": 0, "priced": True}
        for r in ("r_insider", "r_fresh", "r_baseline", "r_near_miss"):
            row[r] = r in rules
        for d in SC.entry_delays_min:
            row[f"entry_{d}"] = cost
            row[f"ret_{d}"] = (float(won) - cost) / cost
            row[f"days_held_{d}"] = 5.0
        rows.append(row)
    return pd.DataFrame(rows)


def test_the_headline_is_the_test_period_not_the_training_period():
    res = study_results(bets(train_edge=0.3, test_edge=-0.3), CFG_FAST)
    h = res["headline"]
    assert h["period"] == "test"
    assert h["mean_return"] < 0  # the rule looked great in training and failed on unseen bets: we must say so


def test_the_rule_is_chosen_on_training_data_only():
    res = study_results(bets(), CFG_FAST)
    chosen = res["candidates"][res["headline"]["rule"]]
    ok = [c["train"]["ci_lo"] for c in res["candidates"].values() if c["train"]["status"] == "OK"]
    assert chosen["train"]["ci_lo"] == max(ok)


def test_sizing_is_simulated_on_the_test_period_with_several_policies():
    df = bets()
    res = study_results(df, CFG_FAST)
    names = [p["policy"] for p in res["sizing"]]
    assert any("Kelly" in n for n in names) and any("flat" in n for n in names)
    n_test = len(df) - int(len(df) * SC.train_fraction)
    for p in res["sizing"]:
        assert p["taken"] <= n_test
        assert {"final", "p5_final", "p95_max_drawdown", "p_loss"} <= set(p)


def test_go_no_go_says_no_when_the_test_period_loses():
    res = study_results(bets(train_edge=0.3, test_edge=-0.3), CFG_FAST)
    assert res["verdict"]["go"] is False
    assert any(not c["passed"] for c in res["verdict"]["checks"])


def test_go_no_go_says_go_when_every_check_passes():
    res = study_results(bets(n=600, train_edge=0.3, test_edge=0.3), CFG_FAST)
    assert res["verdict"]["go"] is True, res["verdict"]


def test_bet_log_gives_every_bet_a_plain_status():
    df = bets(n=6)
    df.loc[0, "is_open"] = True
    df.loc[0, "payout"] = math.nan
    df.loc[1, "irregular"] = True
    df.loc[1, "payout"] = math.nan
    df.loc[2, "priced"] = False
    df.loc[2, f"ret_{D}"] = math.nan
    df.loc[3, f"entry_{D}"] = math.nan  # the price ran past the chase limit
    df.loc[3, f"ret_{D}"] = math.nan
    df.loc[4, "won"], df.loc[4, "payout"], df.loc[4, f"ret_{D}"] = True, 1.0, 1.0
    df.loc[5, "won"], df.loc[5, "payout"], df.loc[5, f"ret_{D}"] = False, 0.0, -1.0
    log = bet_log(df, D)
    assert list(log["status"]) == ["OPEN", "IRREGULAR", "UNPRICED", "SKIPPED", "WIN", "LOSS"]


def test_outputs_are_strict_json_markdown_and_csv(tmp_path):
    df = bets()
    res = study_results(df, CFG_FAST)
    paths = write_outputs(res, df, tmp_path, CFG_FAST, stamp="2026-09-30")

    def reject(c):
        raise AssertionError(f"non-JSON constant {c}")

    json.loads(paths["json"].read_text(), parse_constant=reject)
    md = paths["markdown"].read_text()
    for section in ("Headline", "Bets per week", "Candidate rules", "Factors", "Horizon", "Position sizing",
                    "Go / no-go"):
        assert section in md
    assert len(pd.read_csv(paths["csv"])) == len(df)


def test_render_markdown_states_the_headline_in_plain_words():
    md = render_markdown(study_results(bets(), CFG_FAST))
    assert "per dollar" in md and "out of sample" in md.lower()


def test_cli_study_prints_the_headline_and_where_the_report_is(monkeypatch, capsys, tmp_path):
    from whalescan import cli

    def fake(cfg, **kwargs):
        return {"headline": {"rule": "INSIDER", "delay_min": 15, "period": "test", "mean_return": 0.08,
                             "ci_lo": -0.02, "ci_hi": 0.19, "n_bets": 120, "n_wallets": 31, "status": "OK"},
                "verdict": {"go": False}, "paths": {"markdown": tmp_path / "r.md"}}

    monkeypatch.setattr(cli, "run_study", fake)
    assert cli.main(["study"]) == 0
    out = capsys.readouterr().out
    assert "INSIDER" in out and "+8.0%" in out and "NO-GO" in out and "r.md" in out
