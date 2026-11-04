"""T0 (pre-registration §3): the earlier studies' exact rules, re-scored on OLD, NEW and ALL.

    python -m whalescan.alpha.t0 --history-db data/history.duckdb

- Evidence study: every candidate rule (INSIDER, NEAR_MISS, FRESH_ALL, BASELINE × horizon × price), copier entering
  15 min late, per dollar after fees and spread, wallet-bootstrap CI (exactly the study's own `rule_summary`).
- Insider Finder v2: the three pre-registered variants, base rates learned on OLD only.
- Live Track Record: every call the scanner logged, by kind and by period (calls are all from 2026-09-25 on).
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
from pathlib import Path
from typing import Any

import pandas as pd

from whalescan.alpha import SPLIT_TS
from whalescan.config import load_config
from whalescan.finder import base_rates, moves_frame, qualify
from whalescan.finder_report import VARIANTS
from whalescan.history_store import HistoryStore
from whalescan.study_report import _candidates
from whalescan.study_stats import rule_summary

log = logging.getLogger(__name__)
OUT = Path("data/alpha")
PERIODS = {"OLD": lambda d: d[d["signal_ts"] < SPLIT_TS], "NEW": lambda d: d[d["signal_ts"] >= SPLIT_TS],
           "ALL": lambda d: d}


def evidence(bets: pd.DataFrame, cfg) -> list[dict[str, Any]]:
    sc = cfg.study
    kw = dict(delay=sc.headline_delay_min, n_boot=sc.bootstrap, seed=sc.seed, min_wallets=sc.min_wallets)
    rows = []
    for name, mask in _candidates(bets, sc.headline_delay_min).items():
        d = bets.assign(_rule=mask)
        row = {"rule": name}
        for p, sel in PERIODS.items():
            row[p] = rule_summary(sel(d[d["payout"].notna()]), "_rule", **kw)
            row[p]["bets_fired"] = int(sel(d)["_rule"].sum())  # resolved or not
        rows.append(row)
    return rows


def finder(store: HistoryStore, cfg) -> list[dict[str, Any]]:
    sc = cfg.study
    kw = dict(delay=sc.headline_delay_min, n_boot=sc.bootstrap, seed=sc.seed, min_wallets=sc.min_wallets)
    df = moves_frame(store, cfg)
    rates = base_rates(df[df["signal_ts"] < SPLIT_TS])  # learned on OLD only
    rows = []
    for min_bets, min_z in VARIANTS:
        df["_rule"] = qualify(df, rates, min_bets, min_z)
        row = {"rule": f"FINDER {min_bets}/{min_z:g}"}
        for p, sel in PERIODS.items():
            row[p] = rule_summary(sel(df[df["payout"].notna()]), "_rule", **kw)
            row[p]["bets_fired"] = int(sel(df)["_rule"].sum())
        rows.append(row)
    return rows


def track_record() -> list[dict[str, Any]]:
    """Every scanner call (ledger branch) with its settlement, by kind × period; per-dollar return at the price
    the ledger recorded for the call."""
    def git_jsonl(path: str) -> list[dict[str, Any]]:
        txt = subprocess.check_output(["git", "show", f"origin/ledger:{path}"], text=True)
        return [json.loads(x) for x in txt.splitlines() if x.strip()]

    calls = pd.DataFrame(git_jsonl("calls.jsonl"))
    marks = pd.DataFrame(git_jsonl("marks.jsonl"))
    settled = marks[marks["type"] == "SETTLEMENT"].drop_duplicates("call_id", keep="last")
    d = calls.merge(settled[["call_id", "payout", "return_pct", "irregular"]], left_on="id", right_on="call_id",
                    how="left")
    d["signal_ts"] = d["call_ts"]
    rows = []
    for kind, g in d.groupby("kind"):
        row = {"rule": f"LIVE {kind}"}
        for p, sel in PERIODS.items():
            s = sel(g)
            r = s[s["return_pct"].notna() & ~s["irregular"].fillna(False).astype(bool)]
            row[p] = {"calls": int(len(s)), "settled": int(len(r)), "wallets": int(r["wallet"].nunique()),
                      "hit_rate": float((r["payout"] == 1.0).mean()) if len(r) else None,
                      "mean_return": float(r["return_pct"].mean()) if len(r) else None}
        rows.append(row)
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--history-db", default="data/history.duckdb")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    cfg = load_config()
    bets = pd.read_parquet(OUT / "bets.parquet")
    res = {"evidence": evidence(bets, cfg), "track_record": track_record()}
    with HistoryStore(Path(a.history_db)) as store:
        res["finder"] = finder(store, cfg)
    (OUT / "t0.json").write_text(json.dumps(res, indent=1, default=str))
    log.info("wrote %s", OUT / "t0.json")


if __name__ == "__main__":
    main()
