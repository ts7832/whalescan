"""`whalescan study`: turn the bet table into the evidence report (evidence-study spec §5–§9).

The rule is picked on the older bets by the LOWER end of its confidence interval, from a small fixed menu, and
judged once on the newest bets — that test-period result is the headline. Position sizing is simulated on the
test period only, with win probabilities learned from the training period.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from whalescan.config import Config
from whalescan.snapshot import write_json_atomic
from whalescan.study_math import shrink
from whalescan.study_sim import Policy, bootstrap_paths, simulate
from whalescan.study_stats import bets_per_week, factor_table, horizon_table, rule_summary, time_split

RULES = {"INSIDER": "r_insider", "NEAR_MISS": "r_near_miss", "FRESH_ALL": "r_fresh", "BASELINE": "r_baseline"}
HORIZON_LIMITS = {"any horizon": None, "ends <= 7d": 7.0, "ends <= 30d": 30.0}  # scheduled end, known at the bet
PRICE_LIMITS = {"any price": None, "entry <= 0.85": 0.85}
FACTORS = ["age_days", "usdc", "whale_price", "markets_at_bet", "crowd_before", "n_fills", "days_to_end"]


def _candidates(df: pd.DataFrame, d: int) -> dict[str, pd.Series]:
    out: dict[str, pd.Series] = {}
    for rule, col in RULES.items():
        for hname, hmax in HORIZON_LIMITS.items():
            for pname, pmax in PRICE_LIMITS.items():
                mask = df[col].astype(bool)
                if hmax is not None:
                    mask = mask & (df["days_to_end"] <= hmax)
                if pmax is not None:
                    mask = mask & (df[f"entry_{d}"] <= pmax)
                name = rule if hmax is None and pmax is None else f"{rule} | {hname} | {pname}"
                out[name] = mask
    return out


def _p_model(train_rows: pd.DataFrame, d: int, k: float):
    """Win probability for an entry cost: the training win rate of similar-priced bets, shrunk toward the price."""
    tr = train_rows[train_rows[f"ret_{d}"].notna()]
    if tr.empty:
        return lambda cost: cost
    costs = tr[f"entry_{d}"].to_numpy(dtype=float)
    wins = tr["won"].to_numpy(dtype=float)
    edges = np.unique(np.quantile(costs, [0.25, 0.5, 0.75]))

    def p(cost: float) -> float:
        b = np.searchsorted(edges, cost, side="right")
        sel = np.searchsorted(edges, costs, side="right") == b
        n = int(sel.sum())
        return shrink(float(wins[sel].mean()) if n else cost, n, cost, k)

    return p


def _sim_frame(rows: pd.DataFrame, d: int, p) -> pd.DataFrame:
    s = rows[rows[f"ret_{d}"].notna()]
    return pd.DataFrame({
        "entry_ts": s["signal_ts"] + d * 60, "resolved_ts": s["resolved_ts"], "cost": s[f"entry_{d}"],
        "payout": s["payout"], "whale_usdc": s["usdc"], "event_slug": s["event_slug"], "asset": s["asset"],
        "p": [p(c) for c in s[f"entry_{d}"]]})


def study_results(df: pd.DataFrame, cfg: Config) -> dict[str, Any]:
    sc = cfg.study
    d = sc.headline_delay_min
    kw = dict(n_boot=sc.bootstrap, seed=sc.seed, min_wallets=sc.min_wallets)
    resolved = df[df["payout"].notna()]
    train, test = time_split(resolved, sc.train_fraction)
    cut = int(test["signal_ts"].min()) if not test.empty else None

    cands_tr, cands_te = _candidates(train, d), _candidates(test, d)
    candidates = {name: {"train": rule_summary(train[m], RULES[name.split(" | ")[0]], delay=d, **kw),
                         "test": rule_summary(test[cands_te[name]], RULES[name.split(" | ")[0]], delay=d, **kw)}
                  for name, m in cands_tr.items()}
    ok = {n: c for n, c in candidates.items() if c["train"]["status"] == "OK" and c["train"]["ci_lo"] is not None}
    chosen = max(ok, key=lambda n: ok[n]["train"]["ci_lo"]) if ok else "INSIDER"
    headline = {"rule": chosen, "delay_min": d, "period": "test", "test_from": cut, **candidates[chosen]["test"]}

    base_col = RULES[chosen.split(" | ")[0]]
    chosen_tr, chosen_te = train[cands_tr[chosen]], test[cands_te[chosen]]
    delay_curve = [{"delay_min": dd, **rule_summary(chosen_te, base_col, delay=dd, **kw)} for dd in sc.entry_delays_min]

    fresh_tr, fresh_te = train[train["r_fresh"].astype(bool)], test[test["r_fresh"].astype(bool)]
    factors = [factor_table(fresh_tr, fresh_te, f, f"ret_{d}") for f in FACTORS if f in df]

    p = _p_model(chosen_tr, d, sc.shrink_k)
    sim_bets = _sim_frame(chosen_te, d, p)
    policies = [Policy("flat 2% of the starting bankroll", "flat", 0.02),
                Policy("2% of current equity", "fraction", 0.02),
                Policy("2% of equity, add-on per extra wallet", "fraction", 0.02, aggregation="add"),
                Policy(f"{sc.kelly_fraction:g} Kelly (shrunk win rates)", "kelly", sc.kelly_fraction)]
    sizing = []
    for pol in policies:
        r = simulate(sim_bets, pol, sc)
        boot = bootstrap_paths(sim_bets, pol, sc, n=sc.bootstrap, seed=sc.seed)
        sizing.append({"policy": pol.name, "taken": r.taken, "final": r.final,
                       "return": (r.final - sc.bankroll) / sc.bankroll, "max_drawdown": r.max_drawdown,
                       "skipped_caps": r.skipped_caps, "skipped_cash": r.skipped_cash, **boot})

    best_p5 = max((s["p5_final"] for s in sizing if s["p5_final"] is not None), default=None)
    checks = [
        {"check": "Out-of-sample return per dollar after costs is positive",
         "passed": (headline.get("mean_return") or -1) > 0},
        {"check": "...and so is the lower end of its 90% confidence interval",
         "passed": (headline.get("ci_lo") or -1) > 0},
        {"check": f"At least {sc.min_wallets} distinct wallets in the test period",
         "passed": headline.get("n_wallets", 0) >= sc.min_wallets},
        {"check": "The best sizing rule's 5th-percentile path keeps >= 70% of the bankroll",
         "passed": best_p5 is not None and best_p5 >= 0.7 * sc.bankroll},
    ]
    return {
        "generated_at": int(datetime.now(UTC).timestamp()),
        "coverage": {"bets": int(len(df)), "resolved": int(len(resolved)), "open": int(df["is_open"].sum()),
                     "irregular": int(df["irregular"].sum()), "priced": int(df["priced"].sum()),
                     "wallets": int(df["wallet"].nunique()), "markets": int(df["condition_id"].nunique()),
                     "first_bet": int(df["signal_ts"].min()), "last_bet": int(df["signal_ts"].max()),
                     "train_bets": int(len(train)), "test_bets": int(len(test))},
        "headline": headline,
        "verdict": {"go": all(c["passed"] for c in checks), "checks": checks},
        "bets_per_week": {name: bets_per_week(df, col) for name, col in RULES.items()},
        "candidates": candidates,
        "delay_curve": delay_curve,
        "factors": factors,
        "horizon": horizon_table(chosen_te if not chosen_te.empty else resolved, delay=d),
        "sizing": sizing,
    }


def bet_log(df: pd.DataFrame, d: int) -> pd.DataFrame:
    """Every candidate bet with a plain status at the headline delay."""
    def status(r: Any) -> str:
        if r["is_open"]:
            return "OPEN"
        if r["irregular"]:
            return "IRREGULAR"
        if not r["priced"]:
            return "UNPRICED"
        if pd.isna(r[f"entry_{d}"]):
            return "SKIPPED"
        return "WIN" if r["won"] else "LOSS"

    out = df.copy()
    out.insert(0, "status", out.apply(status, axis=1))
    out.insert(1, "bet_time", pd.to_datetime(out["signal_ts"], unit="s", utc=True).dt.strftime("%Y-%m-%d %H:%M"))
    return out


def _pct(x: float | None) -> str:
    return "—" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x:+.1%}"


def render_markdown(res: dict[str, Any]) -> str:
    h, cov = res["headline"], res["coverage"]

    def day(ts: int) -> str:
        return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d")

    lines = [
        "# WHALESCAN evidence study", "",
        f"{cov['bets']} big news-market buys ({cov['resolved']} resolved, {cov['open']} still open, "
        f"{cov['irregular']} voided) by {cov['wallets']} wallets in {cov['markets']} markets, "
        f"{day(cov['first_bet'])} to {day(cov['last_bet'])}.", "",
        "## Headline", "",
        f"**{h['rule']}**, entering {h['delay_min']} minutes after the whale, judged **out of sample** on the newest "
        f"bets ({cov['test_bets']} resolved bets from {day(h['test_from']) if h.get('test_from') else '—'}):", "",
        f"- return **per dollar** after fees and spread: **{_pct(h.get('mean_return'))}** "
        f"(90% CI {_pct(h.get('ci_lo'))} to {_pct(h.get('ci_hi'))}, wallets resampled)",
        f"- {h.get('n_bets', 0)} bets from {h.get('n_wallets', 0)} wallets · hit rate {_pct(h.get('hit_rate'))} · "
        f"{h.get('n_skipped', 0)} skipped because the price ran away · per-wallet t = "
        f"{'—' if h.get('wallet_t') is None else round(h['wallet_t'], 2)}",
        f"- median days held {h.get('median_days_held') or '—'} · return per dollar per day "
        f"{_pct(h.get('return_per_day'))} · status {h.get('status')}", "",
        "## Go / no-go", "",
        f"**{'GO — start forward paper trading' if res['verdict']['go'] else 'NO-GO — do not risk real money'}**", "",
        *[f"- [{'x' if c['passed'] else ' '}] {c['check']}" for c in res["verdict"]["checks"]],
        "", "Even on GO, real money waits until the forward paper trader tracks this backtest over 30+ settled "
            "trades.", "",
        "## Bets per week", "", "| Rule | median | min | max | weeks |", "|---|---|---|---|---|",
        *[f"| {n} | {s['median']} | {s['min']} | {s['max']} | {s['weeks']} |" for n, s in res["bets_per_week"].items()],
        "", "## Candidate rules", "",
        "Chosen on the training period by the lower end of the confidence interval. Test-period columns for the "
        "other rules are shown for honesty but were not used to choose — with this many rules, the best test "
        "number is partly luck.", "",
        "| Rule | train bets | train return | train CI low | test bets | test return | test CI |",
        "|---|---|---|---|---|---|---|",
        *[f"| {n} | {c['train']['n_bets']} | {_pct(c['train']['mean_return'])} | {_pct(c['train']['ci_lo'])} | "
          f"{c['test']['n_bets']} | {_pct(c['test']['mean_return'])} | {_pct(c['test']['ci_lo'])} to "
          f"{_pct(c['test']['ci_hi'])} |" for n, c in res["candidates"].items()],
        "", "## Entry delay (test period, chosen rule)", "", "| Delay | bets | return | skipped |", "|---|---|---|---|",
        *[f"| {r['delay_min']} min | {r['n_bets']} | {_pct(r['mean_return'])} | {r['n_skipped']} |"
          for r in res["delay_curve"]],
        "", "## Factors (fresh-wallet bets)", "",
        "A factor matters only if its rank correlation with return has the same sign in training and test.", "",
        "| Factor | train ρ | test ρ | holds out of sample |", "|---|---|---|---|",
        *[f"| {f['factor']} | {'—' if f['train_spearman'] is None else round(f['train_spearman'], 3)} | "
          f"{'—' if f['test_spearman'] is None else round(f['test_spearman'], 3)} | {'yes' if f['holds'] else 'no'} |"
          for f in res["factors"]],
        "", "## Horizon (how long money is tied up)", "", "| Held | bets | return | per day |", "|---|---|---|---|",
        *[f"| {r['bucket']} | {r['n']} | {_pct(r['mean_return'])} | {_pct(r['return_per_day'])} |"
          for r in res["horizon"]],
        "", "## Position sizing (test period, paper bankroll)", "",
        "| Policy | bets | final | max drawdown | 5th pct final | 95th pct drawdown | P(loss) |",
        "|---|---|---|---|---|---|---|",
        *[f"| {s['policy']} | {s['taken']} | {s['final']:.0f} | {_pct(s['max_drawdown'])} | "
          f"{'—' if s['p5_final'] is None else round(s['p5_final'])} | {_pct(s['p95_max_drawdown'])} | "
          f"{_pct(s['p_loss'])} |" for s in res["sizing"]],
        "", "## Caveats", "",
        "- Historical order books were not recorded: entry = last traded price + half a spread + fee.",
        "- Account age uses the profile's creation date; profiles created after the first fill are excluded.",
        "- Markets traded before the bet are counted only for wallets whose full history was fetched.",
        "- Bets in markets that are still open are counted per week but not scored.",
    ]
    return "\n".join(lines) + "\n"


def write_outputs(res: dict[str, Any], df: pd.DataFrame, out_dir: Path, cfg: Config, *,
                  stamp: str | None = None) -> dict[str, Path]:
    stamp = stamp or datetime.now(UTC).strftime("%Y-%m-%d")
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {"json": out_dir / f"study-{stamp}.json", "csv": out_dir / f"bets-{stamp}.csv",
             "markdown": out_dir / f"evidence-study-{stamp}.md"}
    write_json_atomic(paths["json"], res)
    bet_log(df, cfg.study.headline_delay_min).to_csv(paths["csv"], index=False)
    paths["markdown"].write_text(render_markdown(res))
    return paths


def run_study(cfg: Config) -> dict[str, Any]:
    from whalescan.history_store import HistoryStore
    from whalescan.study import bets_frame

    db = cfg.path(cfg.paths.history_db)
    with HistoryStore(db) as store:
        df = bets_frame(store, cfg)
    if df.empty:
        raise RuntimeError("no bets in the history database yet — run `whalescan history` first")
    res = study_results(df, cfg)
    res["paths"] = write_outputs(res, df, db.parent / "history", cfg)
    return res
