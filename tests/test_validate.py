from dataclasses import replace

import numpy as np
import pandas as pd

from whalescan.classify import Blocklist
from whalescan.config import load_config
from whalescan.models import Market, Trade
from whalescan.scoring import ELIGIBLE_COLUMNS
from whalescan.validate import make_folds, run_validation

DAY = 86400
BASE = load_config()
BASE = replace(BASE, gate=replace(BASE.gate, skill_mode="certified"))
CFG = replace(BASE, validation=replace(BASE.validation, n_sims=5000))


def test_make_folds():
    folds = make_folds(0, 100 * DAY, fold_days=14, min_history_days=60)
    assert [(f.cutoff // DAY, f.end // DAY) for f in folds] == [(60, 74), (74, 88)]
    assert make_folds(0, 10 * DAY, fold_days=14, min_history_days=60) == []


def test_no_data_is_insufficient():
    report = run_validation(pd.DataFrame(columns=ELIGIBLE_COLUMNS), pd.Series(dtype=object), [], {}, CFG,
                            Blocklist(CFG.blocklist), now=0)
    assert report["verdict"] == "INSUFFICIENT DATA"
    assert report["groups"]["SIGNALS"]["n"] == 0
    assert len(report["caveats"]) == 5


def build_world(seed=3):
    """Skilled wallets beat the odds before the cutoff and keep winning after it."""
    rng = np.random.default_rng(seed)
    rows, trades, markets = [], [], {}
    wallets = [f"0xskilled{k}" for k in range(5)] + [f"0xnull{k}" for k in range(60)]
    for wallet in wallets:
        skilled = wallet.startswith("0xskilled")
        p = rng.uniform(0.1, 0.9, 300)
        y = (rng.uniform(size=300) < np.minimum(p + (0.2 if skilled else 0.0), 0.99)).astype(np.uint8)
        stake = rng.uniform(100, 2000, 300)
        closed = rng.uniform(0, 59 * DAY, 300).astype(int)
        for i in range(300):
            rows.append({"wallet": wallet, "condition_id": f"{wallet}-train{i}", "asset": f"{wallet}-a{i}",
                         "category": "POLITICS", "p": p[i], "y": y[i], "w": stake[i], "stake": stake[i],
                         "closed_ts": closed[i], "title": "t", "outcome": "Yes"})
        n_test = 10 if skilled else 2
        for j in range(n_test):
            cid = f"{wallet}-test{j}"
            won = (j < 8) if skilled else (j == 0)
            ts = 61 * DAY + j * 3600
            trades.append(Trade(f"0x{cid}", ts, wallet, f"{cid}-yes", cid, "BUY", 0.40, 25_000.0, "ev", "t", "Yes", 0,
                                None))
            markets[cid] = Market(cid, "q", "slug", "ev", ts + 10 * DAY, True, ts + 10 * DAY,
                                  (1.0, 0.0) if won else (0.0, 1.0), (f"{cid}-yes", f"{cid}-no"), False, 0.0, 1.0,
                                  1e6, ("Politics",))
    trades.append(Trade("0xlast", 75 * DAY, "0xnull0", "zz", "zz", "BUY", 0.5, 1.0, "ev", "t", "Yes", 0, None))
    return pd.DataFrame(rows, columns=ELIGIBLE_COLUMNS), trades, markets


def test_skilled_world_confirms_edge_out_of_sample():
    eligible, trades, markets = build_world()
    report = run_validation(eligible, pd.Series(dtype=object), trades, markets, CFG, Blocklist(CFG.blocklist),
                            now=100 * DAY)
    sig = report["groups"]["SIGNALS"]
    assert len(report["folds"]) == 1
    assert sig["n"] >= 30 and sig["mean_ret"] > 0.2
    assert report["verdict"] == "EDGE CONFIRMED"
    assert report["groups"]["BASELINE"]["n"] == 5 * 10 + 60 * 2
    assert report["groups"]["BASELINE"]["mean_ret"] < sig["mean_ret"]
    assert sum(b["n"] for b in report["calibration"]) == sig["n"]
