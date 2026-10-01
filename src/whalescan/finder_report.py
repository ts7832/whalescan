"""`whalescan finder-study`: turn the Insider Finder's moves table into a go/no-go report (spec §2).

Unlike the evidence study's RULES (genuinely different candidate signals, the best picked on training data),
the three (min_bets, min_z) variants here are pre-registered robustness checks of ONE signal — picking a
"winner" among them after seeing results would be exactly the p-hacking pre-registration exists to prevent. All
three are reported honestly; the headline is always the PRIMARY variant ([finder]'s own configured values).
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from whalescan.config import Config
from whalescan.finder import base_rates, qualify
from whalescan.snapshot import write_json_atomic
from whalescan.study_stats import bets_per_period, bets_per_week, rule_summary, time_split

QUARTER_DAYS, HALF_YEAR_DAYS = 91, 182

VARIANTS = [(5, 2.33), (10, 2.33), (5, 3.0)]  # spec §1, pre-registered — never tuned on results


def _variant_key(min_bets: int, min_z: float) -> str:
    return f"{min_bets:g}/{min_z:g}"


def _pct(x: float | None) -> str:
    return "—" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x:+.1%}"


def finder_results(df: pd.DataFrame, cfg: Config) -> dict[str, Any]:
    sc, fc = cfg.study, cfg.finder
    d = sc.headline_delay_min
    kw = dict(n_boot=sc.bootstrap, seed=sc.seed, min_wallets=sc.min_wallets)
    resolved = df[df["payout"].notna()]
    _, test = time_split(resolved, sc.train_fraction)
    cut = int(test["signal_ts"].min()) if not test.empty else int(df["signal_ts"].max()) + 1 if len(df) else 0

    # Base rates use every bet (resolved or still open) before the cutoff: a move's outcome is known within 24h,
    # long before most markets settle, so restricting this to already-resolved bets would waste real information.
    rates = base_rates(df[df["signal_ts"] < cut])

    variants: dict[str, Any] = {}
    for min_bets, min_z in VARIANTS:
        col = f"_informed_{min_bets}_{min_z}"
        df[col] = qualify(df, rates, min_bets, min_z)
        train_rows = df[(df["signal_ts"] < cut) & df["payout"].notna()]
        test_rows = df[(df["signal_ts"] >= cut) & df["payout"].notna()]
        variants[_variant_key(min_bets, min_z)] = {
            "train": rule_summary(train_rows, col, delay=d, **kw),
            "test": rule_summary(test_rows, col, delay=d, **kw),
        }

    primary = _variant_key(fc.min_bets, fc.min_z)
    if primary not in variants:  # the configured primary isn't one of the three pre-registered variants
        primary = _variant_key(*VARIANTS[0])
    headline = {"variant": primary, "delay_min": d, "period": "test", "test_from": cut, **variants[primary]["test"]}

    primary_col = f"_informed_{fc.min_bets}_{fc.min_z}" if primary == _variant_key(fc.min_bets, fc.min_z) \
        else f"_informed_{VARIANTS[0][0]}_{VARIANTS[0][1]}"
    # Markets were only loaded because they ENDED within the lookback window, so weeks before that see only
    # long-lived markets and understate the true rate (same trap the evidence study hit) — restrict to covered weeks.
    covered_from = datetime.fromtimestamp(int(df["signal_ts"].max()) - fc.lookback_days * 86400, UTC).date()
    since_week = (covered_from - timedelta(days=covered_from.weekday())).isoformat() if len(df) else None
    since_ts = int(datetime(covered_from.year, covered_from.month, covered_from.day,
                            tzinfo=UTC).timestamp()) if len(df) else None
    week_rate = bets_per_week(df, primary_col, since_week=since_week)
    # A rare signal (especially the stricter variants) can show many zero WEEKS even while firing reliably
    # over a longer span — the weekly number alone understates how often it really fires.
    quarter_rate = bets_per_period(df, primary_col, period_days=QUARTER_DAYS, since_ts=since_ts)
    half_year_rate = bets_per_period(df, primary_col, period_days=HALF_YEAR_DAYS, since_ts=since_ts)

    checks = [
        {"check": "Out-of-sample return per dollar after costs is positive", "passed": (headline.get("mean_return") or -1) > 0},
        {"check": "...and so is the lower end of its 90% confidence interval", "passed": (headline.get("ci_lo") or -1) > 0},
        {"check": f"At least {sc.min_wallets} distinct wallets in the test period",
         "passed": headline.get("n_wallets", 0) >= sc.min_wallets},
    ]
    return {
        "generated_at": int(datetime.now(UTC).timestamp()),
        "coverage": {"bets": int(len(df)), "resolved": int(len(resolved)), "wallets": int(df["wallet"].nunique())
                    if len(df) else 0, "train_bets": int(len(df[df["signal_ts"] < cut])),
                    "test_bets": int(len(df[df["signal_ts"] >= cut]))},
        "headline": headline,
        "verdict": {"go": all(c["passed"] for c in checks), "checks": checks},
        "bets_per_week": week_rate,
        "bets_per_quarter": quarter_rate,
        "bets_per_half_year": half_year_rate,
        "variants": variants,
    }


def render_markdown(res: dict[str, Any]) -> str:
    h, cov = res["headline"], res["coverage"]
    lines = [
        "# Insider Finder v2 — evidence report", "",
        f"{cov['bets']} covered big bets ({cov['resolved']} resolved) by {cov['wallets']} wallets.", "",
        "## Headline", "",
        f"Variant **{h['variant']}** (min_bets/min_z), entering {h['delay_min']} minutes after the whale, judged "
        f"**out of sample** on the newest bets ({cov['test_bets']} in the test period):", "",
        f"- return per dollar after fees and spread: **{_pct(h.get('mean_return'))}** "
        f"(90% CI {_pct(h.get('ci_lo'))} to {_pct(h.get('ci_hi'))})",
        f"- {h.get('n_bets', 0)} bets from {h.get('n_wallets', 0)} wallets · hit rate {_pct(h.get('hit_rate'))} · "
        f"status {h.get('status')}", "",
        "## Go / no-go", "",
        f"**{'GO' if res['verdict']['go'] else 'NO-GO'}**", "",
        *[f"- [{'x' if c['passed'] else ' '}] {c['check']}" for c in res["verdict"]["checks"]], "",
        "## How often this fires", "",
        "A rare signal can show many zero-count weeks even while firing reliably over a longer span — all "
        "three cadences below use the same (primary-variant) calls.", "",
        "| Cadence | median | min | max | periods |", "|---|---|---|---|---|",
        f"| per week | {res['bets_per_week'].get('median')} | {res['bets_per_week'].get('min')} | "
        f"{res['bets_per_week'].get('max')} | {res['bets_per_week'].get('weeks')} |",
        f"| per quarter (~91d) | {res['bets_per_quarter'].get('median')} | {res['bets_per_quarter'].get('min')} | "
        f"{res['bets_per_quarter'].get('max')} | {res['bets_per_quarter'].get('periods')} |",
        f"| per half year (~182d) | {res['bets_per_half_year'].get('median')} | "
        f"{res['bets_per_half_year'].get('min')} | {res['bets_per_half_year'].get('max')} | "
        f"{res['bets_per_half_year'].get('periods')} |",
        "",
        "## Variants (all pre-registered, none cherry-picked)", "",
        "| min_bets/min_z | train bets | train return | test bets | test return | test CI |",
        "|---|---|---|---|---|---|",
        *[f"| {k} | {v['train']['n_bets']} | {_pct(v['train']['mean_return'])} | {v['test']['n_bets']} | "
          f"{_pct(v['test']['mean_return'])} | {_pct(v['test']['ci_lo'])} to {_pct(v['test']['ci_hi'])} |"
          for k, v in res["variants"].items()],
        "",
    ]
    return "\n".join(lines) + "\n"


def write_outputs(res: dict[str, Any], out_dir: Path, *, stamp: str | None = None) -> dict[str, Path]:
    stamp = stamp or datetime.now(UTC).strftime("%Y-%m-%d")
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {"json": out_dir / f"finder-{stamp}.json", "markdown": out_dir / f"insider-finder-{stamp}.md"}
    write_json_atomic(paths["json"], res)
    paths["markdown"].write_text(render_markdown(res))
    return paths


def run_finder_study(cfg: Config) -> dict[str, Any]:
    from whalescan.finder import moves_frame
    from whalescan.history_store import HistoryStore

    db = cfg.path(cfg.paths.history_db)
    with HistoryStore(db) as store:
        df = moves_frame(store, cfg)
    if df.empty:
        raise RuntimeError("no covered bets in the history database yet — run `whalescan history` first")
    res = finder_results(df, cfg)
    res["paths"] = write_outputs(res, db.parent / "history")
    return res
