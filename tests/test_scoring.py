from dataclasses import replace

import numpy as np
import pandas as pd

from whalescan.classify import Blocklist
from whalescan.config import load_config
from whalescan.scoring import ELIGIBLE_COLUMNS, SCORE_COLUMNS, benjamini_hochberg, prepare_positions, score_wallets
from whalescan.store import SCORE_COLS

CFG = load_config()
SC = replace(CFG.scoring, n_sims=20000)


def synthetic(n_null=200, n_skilled=5, per=300, skill=0.2, seed=1) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rows = []
    for k in range(n_null + n_skilled):
        wallet = f"0xskilled{k}" if k >= n_null else f"0xnull{k}"
        p = rng.uniform(0.1, 0.9, per)
        win_prob = np.minimum(p + (skill if k >= n_null else 0.0), 0.99)
        y = (rng.uniform(size=per) < win_prob).astype(np.uint8)
        stake = rng.uniform(10, 1000, per)
        for i in range(per):
            rows.append({"wallet": wallet, "condition_id": f"c{k}-{i}", "asset": f"a{k}-{i}", "category": "POLITICS",
                         "p": p[i], "y": y[i], "w": stake[i], "stake": stake[i], "closed_ts": i, "title": "t",
                         "outcome": "Yes"})
    return pd.DataFrame(rows, columns=ELIGIBLE_COLUMNS)


def test_score_columns_match_store():
    assert SCORE_COLUMNS == SCORE_COLS


def test_benjamini_hochberg_hand_example():
    p = np.array([0.01, 0.04, 0.03, 0.2])
    # sorted: .01<=.025, .03<=.05, .04<=.075, .2>.1 -> first three pass
    assert benjamini_hochberg(p, 0.1).tolist() == [True, True, True, False]
    assert benjamini_hochberg(np.array([]), 0.1).tolist() == []
    assert benjamini_hochberg(np.array([0.5, 0.9]), 0.1).tolist() == [False, False]


def test_skilled_wallets_are_certified_and_noise_mostly_is_not():
    eligible = synthetic()
    scores = score_wallets(eligible, pd.Series(dtype=object), SC, as_of=123)
    assert list(scores.columns) == SCORE_COLUMNS
    overall = scores[scores.category == "ALL"].set_index("wallet")
    skilled = [w for w in overall.index if w.startswith("0xskilled")]
    null = [w for w in overall.index if w.startswith("0xnull")]
    assert overall.loc[skilled, "certified"].sum() >= 4
    assert overall.loc[null, "certified"].sum() <= 3
    assert (overall["as_of"] == 123).all()
    certified = overall[overall.certified]
    assert ((certified.post_edge <= certified.edge) & (certified.post_edge > 0)).all()


def test_sigma_matches_formula():
    eligible = synthetic(n_null=3, n_skilled=0, per=40)
    scores = score_wallets(eligible, pd.Series(dtype=object), SC, as_of=0)
    one = eligible[eligible.wallet == "0xnull0"]
    expected = np.sqrt((one.w**2 * one.p * (1 - one.p)).sum()) / one.w.sum()
    got = scores[(scores.wallet == "0xnull0") & (scores.category == "ALL")].sigma.iloc[0]
    assert np.isclose(got, expected)


def test_small_samples_shrink_more():
    eligible = synthetic()
    tiny = pd.DataFrame([{"wallet": "0xtiny", "condition_id": f"t{i}", "asset": f"t{i}", "category": "POLITICS",
                          "p": 0.5, "y": 1, "w": 100.0, "stake": 100.0, "closed_ts": i, "title": "t", "outcome": "Yes"}
                         for i in range(25)])
    scores = score_wallets(pd.concat([eligible, tiny], ignore_index=True), pd.Series(dtype=object), SC, as_of=0)
    overall = scores[scores.category == "ALL"].set_index("wallet")
    tiny_factor = overall.loc["0xtiny", "post_edge"] / overall.loc["0xtiny", "edge"]
    big_factor = overall.loc["0xskilled200", "post_edge"] / overall.loc["0xskilled200", "edge"]
    assert tiny_factor < big_factor


def test_flagged_wallet_is_never_certified():
    eligible = synthetic()
    flags = pd.Series({"0xskilled200": "FARMER"})
    scores = score_wallets(eligible, flags, SC, as_of=0)
    row = scores[(scores.wallet == "0xskilled200") & (scores.category == "ALL")].iloc[0]
    assert not row["certified"] and not row["bh_pass"] and row["flags"] == "FARMER"


def test_empty_input_gives_empty_frame():
    empty = pd.DataFrame(columns=ELIGIBLE_COLUMNS)
    assert score_wallets(empty, pd.Series(dtype=object), SC, as_of=0).empty


def frame_row(**kw):
    base = {"wallet": "0xw", "asset": "a", "condition_id": "c", "avg_price": 0.5, "total_bought": 100.0,
            "realized_pnl": 0.0, "outcome": "Yes", "outcome_index": 0, "title": "t", "ts": 1, "event_slug": "ev",
            "market_slug": "m", "market_event_slug": "ev", "closed": True, "closed_ts": 10, "volume": 1e6,
            "tags": ["Politics"], "complete": True, "winner_index": 0}
    base.update(kw)
    return base


def test_prepare_excludes_incomplete_and_unresolved():
    frame = pd.DataFrame([
        frame_row(asset="ok"),
        frame_row(asset="incomplete", wallet="0xpartial", complete=False),
        frame_row(asset="unresolved", winner_index=None),
        frame_row(asset="too_cheap", avg_price=0.01),
        frame_row(asset="bot", market_event_slug="btc-updown-5m-1"),
        frame_row(asset="illiquid", volume=100.0),
        frame_row(asset="lost", outcome_index=1),
    ])
    frame["winner_index"] = frame["winner_index"].astype("Int64")
    out = prepare_positions(frame, CFG.scoring, CFG.categories, Blocklist(CFG.blocklist))
    assert list(out.columns) == ELIGIBLE_COLUMNS
    assert sorted(out.asset) == ["lost", "ok"]
    assert out.set_index("asset").loc["ok", "y"] == 1
    assert out.set_index("asset").loc["lost", "y"] == 0
    assert (out.category == "POLITICS").all()


def test_prepare_winsorizes_stakes_per_wallet():
    frame = pd.DataFrame([frame_row(asset=f"a{i}", total_bought=100.0) for i in range(19)]
                         + [frame_row(asset="whale", total_bought=1_000_000.0)])
    frame["winner_index"] = frame["winner_index"].astype("Int64")
    out = prepare_positions(frame, CFG.scoring, CFG.categories, Blocklist(CFG.blocklist)).set_index("asset")
    assert out.loc["whale", "w"] < out.loc["whale", "stake"]
    assert out.loc["a0", "w"] == out.loc["a0", "stake"]
