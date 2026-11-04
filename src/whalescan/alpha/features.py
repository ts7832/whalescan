"""The alpha hunt's bet table: the evidence study's bets (big news-market buys, one row per position event) plus
every pre-registered feature (spec §3 T1) and target (spec §2). Every feature is known at the signal time.

    python -m whalescan.alpha.features --history-db data/history.duckdb --out data/alpha/bets.parquet
"""

from __future__ import annotations

import argparse
import logging
import math
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from whalescan.alpha import SPLIT_TS
from whalescan.config import load_config
from whalescan.history_store import HistoryStore
from whalescan.study import bets_frame

log = logging.getLogger(__name__)
HOUR, DAY = 3600, 86400
SHRINK_K = 5          # pseudo-bets of zero edge in a wallet's prior-edge estimate
FRESH_DAYS = 7        # "fresh wallet" for the cluster feature (= [insider].max_age_days)
CLUSTER_S = 6 * HOUR
OPPOSITE_S = DAY


def _fees(df: pd.DataFrame, markets: pd.DataFrame, price: pd.Series) -> pd.Series:
    """Taker fee per share at `price` with each market's fee schedule (Market.fee_per_share's formula)."""
    m = df[["condition_id"]].merge(markets, on="condition_id", how="left")
    rate = m["fee_rate"].fillna(0).to_numpy()
    expo = m["fee_exponent"].fillna(1).to_numpy()
    on = m["fees_enabled"].fillna(False).to_numpy().astype(bool)
    p = price.to_numpy()
    fee = np.where(on, rate * (p * (1 - p)) ** expo, 0.0)
    return pd.Series(fee, index=df.index)


def wallet_priors(b: pd.DataFrame) -> pd.DataFrame:
    """Per bet, from the same wallet's EARLIER big bets only: count, resolved count, shrunk edge, z-score of wins,
    median stake, share in this bet's category, bets per week, days since first seen."""
    out = []
    for _w, g in b.sort_values("signal_ts").groupby("wallet", sort=False):
        ts = g["signal_ts"].to_numpy()
        res = g["resolved_ts"].to_numpy(dtype=float)
        pay = g["payout"].to_numpy(dtype=float)
        price = g["whale_price"].to_numpy(dtype=float)
        usdc = g["usdc"].to_numpy(dtype=float)
        cat = g["category"].to_numpy()
        for i in range(len(g)):
            prior = slice(0, i)
            known = (res[prior] < ts[i]) & np.isfinite(pay[prior])
            e = (pay[prior] - price[prior])[known]
            var = (price[prior] * (1 - price[prior]))[known]
            n_prior = i
            out.append((g.index[i], n_prior, int(known.sum()),
                        float(e.sum() / (len(e) + SHRINK_K)),
                        float(e.sum() / math.sqrt(var.sum())) if var.sum() > 0 else 0.0,
                        float(np.median(usdc[prior])) if n_prior else math.nan,
                        float((cat[prior] == cat[i]).mean()) if n_prior else math.nan,
                        (ts[i] - ts[0]) / DAY if n_prior else 0.0))
    cols = ["idx", "w_prior_bets", "w_prior_resolved", "w_prior_edge", "w_prior_z", "w_median_stake",
            "w_cat_share", "w_days_seen"]
    p = pd.DataFrame(out, columns=cols).set_index("idx")
    p["w_bets_per_week"] = np.where(p["w_days_seen"] > 0, p["w_prior_bets"] / (p["w_days_seen"] / 7), np.nan)
    return p


def build(history_db: str, alpha_db: str) -> pd.DataFrame:
    cfg = load_config()
    with HistoryStore(Path(history_db)) as store:
        b = bets_frame(store, cfg)
        markets = store.con.execute(
            "SELECT condition_id, fees_enabled, fee_rate, fee_exponent, len(token_ids) AS n_outcomes FROM markets").df()
    log.info("features: %d bets", len(b))
    b = b.reset_index(drop=True)
    b["period"] = np.where(b["signal_ts"] < SPLIT_TS, "OLD", "NEW")

    con = duckdb.connect()
    con.execute(f"ATTACH '{history_db}' AS h (READ_ONLY)")
    con.execute(f"ATTACH '{alpha_db}' AS a (READ_ONLY)")
    con.register("bets", b[["id", "wallet", "asset", "condition_id", "first_ts", "signal_ts", "resolved_ts",
                            "age_days", "usdc"]])

    # ---- prices from the alpha paths (10-minute prints): the last print at or before a time, within a tolerance
    con.execute("CREATE OR REPLACE TEMP TABLE pa AS SELECT * FROM a.a_paths")
    con.execute("CREATE INDEX IF NOT EXISTS pa_idx ON pa(asset, ts)")
    probes = {  # name: (time expression, tolerance)
        "p_pre24": ("b.signal_ts - 86400", 6 * HOUR),
        "p_before": ("b.first_ts - 60", 6 * HOUR),
        "p_entry": ("b.signal_ts + 900", 25 * 60),
        "p_1h": ("b.signal_ts + 3600", 2 * HOUR),
        "p_24h": ("b.signal_ts + 86400", 12 * HOUR),
        "p_7d": ("b.signal_ts + 604800", 2 * DAY),
    }
    for name, (at, tol) in probes.items():
        con.execute(f"""
            CREATE OR REPLACE TEMP TABLE q_{name} AS
            SELECT b.id, p.price AS {name}, p.ts AS {name}_ts FROM bets b
            ASOF LEFT JOIN pa p ON p.asset = b.asset AND p.ts <= {at}""")
        q = con.execute(f"SELECT id, {name}, {name}_ts FROM q_{name}").df()
        b = b.merge(q, on="id", how="left")
        target = b.eval(at.replace("b.", ""))
        stale = (target - b[f"{name}_ts"]) > tol
        b.loc[stale, name] = np.nan
        b = b.drop(columns=[f"{name}_ts"])

    # ---- market and crowd context from the stored fills (>= $1k) and bets
    con.execute("""
        CREATE OR REPLACE TEMP TABLE mvol AS
        SELECT condition_id, ts, sum(price * size) OVER (PARTITION BY condition_id ORDER BY ts
                 ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS cum_usdc,
               min(ts) OVER (PARTITION BY condition_id) AS first_fill
        FROM h.trades WHERE price * size >= 1000""")
    mv = con.execute("""
        SELECT b.id, m.cum_usdc AS m_bigvol_before, b.first_ts - m.first_fill AS m_age_s
        FROM bets b ASOF LEFT JOIN mvol m ON m.condition_id = b.condition_id AND m.ts < b.first_ts""").df()
    b = b.merge(mv, on="id", how="left")
    b["m_bigvol_before"] = b["m_bigvol_before"].fillna(0.0)

    cl = con.execute(f"""
        SELECT b.id,
          count(DISTINCT o.wallet) FILTER (WHERE o.asset = b.asset AND o.age_days <= {FRESH_DAYS}
                                            AND o.signal_ts BETWEEN b.signal_ts - {CLUSTER_S} AND b.signal_ts)
            AS fresh_cluster,
          coalesce(sum(o.usdc) FILTER (WHERE o.asset <> b.asset
                                        AND o.signal_ts BETWEEN b.signal_ts - {OPPOSITE_S} AND b.signal_ts), 0)
            AS opposite_usdc
        FROM bets b LEFT JOIN bets o ON o.condition_id = b.condition_id AND o.wallet <> b.wallet
        GROUP BY b.id""").df()
    b = b.merge(cl, on="id", how="left")

    # ---- wallet priors (earlier big bets only)
    b = b.join(wallet_priors(b))

    # ---- simple features
    t = pd.to_datetime(b["signal_ts"], unit="s", utc=True)
    b["hour_utc"], b["weekday"] = t.dt.hour, t.dt.weekday
    b["log_usdc"] = np.log10(b["usdc"])
    b["stake_vs_median"] = b["usdc"] / b["w_median_stake"]
    b["stake_vs_market"] = b["usdc"] / (b["m_bigvol_before"] + b["usdc"])
    b["log_markets_at_bet"] = np.log1p(b["markets_at_bet"])
    b["first_bet"] = b["w_prior_bets"] == 0
    b["m_age_days"] = b["m_age_s"] / DAY
    b["impact"] = b["whale_price"] - b["p_before"]          # how far above the prevailing price it paid
    b["momentum_24h"] = b["p_before"] - b["p_pre24"]        # the bought outcome's move over the prior day
    b["opp_ratio"] = b["opposite_usdc"] / b["usdc"]
    b = b.merge(markets[["condition_id", "n_outcomes"]], on="condition_id", how="left")

    # ---- targets (spec §2)
    b["edge_whale"] = b["payout"] - b["whale_price"]
    spread = cfg.study.half_spread
    entry_px = (b["p_entry"] + spread).clip(upper=0.999)
    b["entry_cost"] = entry_px + _fees(b, markets, entry_px)
    b["ret_copy"] = (b["payout"] - b["entry_cost"]) / b["entry_cost"]          # per dollar, after costs
    b["edge_copy"] = b["payout"] - b["entry_cost"]                              # per share, after costs
    # markouts: price of the bought outcome after h minus the whale's price; once resolved, the payout
    for name, h in (("mo_1h", HOUR), ("mo_24h", DAY), ("mo_7d", 7 * DAY)):
        px = b[f"p_{name[3:]}"]
        done = b["resolved_ts"].notna() & (b["resolved_ts"] <= b["signal_ts"] + h) & b["payout"].notna()
        px = px.where(~done, b["payout"])
        b[name] = px - b["whale_price"]
    # mark-to-market: enter at +15 min, exit at +24 h / +7 d (paying another half-spread unless already resolved).
    # Exists for unresolved markets too, so the holdout is not limited to markets that happened to resolve early.
    for name, h, px_col in (("mtm_24h", DAY, "p_24h"), ("mtm_7d", 7 * DAY, "p_7d")):
        done = b["resolved_ts"].notna() & (b["resolved_ts"] <= b["signal_ts"] + h) & b["payout"].notna()
        exit_px = b[px_col].where(~done, b["payout"]) - np.where(done, 0.0, spread)
        b[name] = (exit_px - b["entry_cost"]) / b["entry_cost"]
    # fade: buy the other side now (binary markets only), per dollar, after costs
    opp_px = (1 - b["p_entry"] + spread).clip(lower=0.001, upper=0.999)
    opp_cost = opp_px + _fees(b, markets, opp_px)
    b["ret_fade"] = np.where(b["n_outcomes"] == 2, ((1 - b["payout"]) - opp_cost) / opp_cost, np.nan)
    b["edge_fade"] = np.where(b["n_outcomes"] == 2, (1 - b["payout"]) - opp_cost, np.nan)
    log.info("features: priced entry for %d / %d bets, resolved %d", b["p_entry"].notna().sum(), len(b),
             b["payout"].notna().sum())
    return b


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--history-db", default="data/history.duckdb")
    ap.add_argument("--alpha-db", default="data/alpha.duckdb")
    ap.add_argument("--out", default="data/alpha/bets.parquet")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    df = build(a.history_db, a.alpha_db)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(a.out, index=False)
    log.info("wrote %s (%d rows, %d columns)", a.out, len(df), df.shape[1])


if __name__ == "__main__":
    main()
