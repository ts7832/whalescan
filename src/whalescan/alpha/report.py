"""Render the alpha hunt's results (t0.json, explore.json, holdout.json) as one markdown report.

    python -m whalescan.alpha.report   # -> data/alpha/report.md
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

OUT = Path(os.environ.get("ALPHA_OUT", "data/alpha"))


def pct(x: float | None, digits: int = 1) -> str:
    return "–" if x is None else f"{x * 100:+.{digits}f}%"


def cents(x: float | None) -> str:
    return "–" if x is None else f"{x * 100:+.1f}¢"


def ci(s: dict[str, Any] | None, fmt=pct) -> str:
    if not s or s.get("mean") is None:
        return "–"
    return f"{fmt(s['mean'])} [{fmt(s.get('lo'))}, {fmt(s.get('hi'))}]"


def t0_section(t0: dict[str, Any]) -> list[str]:
    out = ["## T0 — the earlier studies, re-scored", "",
           "Copier enters 15 minutes after the whale; return per dollar after fees and a 1¢ half-spread; "
           "90% interval resamples wallets. OLD = to 30 Sep, NEW = 1 Oct → now, ALL = both.", "",
           "| Rule | OLD bets | OLD return | NEW bets | NEW return | ALL bets | ALL return |",
           "|---|---|---|---|---|---|---|"]
    for r in t0["evidence"] + t0["finder"]:
        cells = []
        for p in ("OLD", "NEW", "ALL"):
            s = r[p]
            m = s.get("mean_return")
            cells += [f"{s.get('n_bets', 0)} ({s.get('n_wallets', 0)}w)",
                      "–" if m is None else f"{pct(m)} [{pct(s.get('ci_lo'))}, {pct(s.get('ci_hi'))}]"]
        out.append(f"| {r['rule']} | " + " | ".join(cells) + " |")
    out += ["", "**Live Track Record** (the scanner's own calls; return at the logged call price):", "",
            "| Kind | OLD calls / settled | OLD hit / return | NEW calls / settled | NEW wallets | NEW hit rate | "
            "NEW return per dollar [90% CI] | NEW median |", "|---|---|---|---|---|---|---|---|"]
    for r in t0["track_record"]:
        o, n = r["OLD"], r["NEW"]
        out.append(f"| {r['rule']} | {o['calls']} / {o['settled']} | "
                   + (f"{pct(o['hit_rate'], 0)} / {pct(o['mean_return'])}" if o["settled"] else "–")
                   + f" | {n['calls']} / {n['settled']} | {n['wallets']} | {pct(n['hit_rate'], 0)} | "
                   f"{pct(n['mean_return'])} [{pct(n.get('ci_lo'))}, {pct(n.get('ci_hi'))}] | {pct(n.get('median_return'))} |")
    return out + [""]


def explore_section(ex: dict[str, Any]) -> list[str]:
    u = ex["universe"]
    out = ["## Exploration on OLD (walk-forward, multiple-testing corrected)", "",
           f"{u['bets']:,} big news-market buys; {u['copyable']:,} copyable at +15 min inside the 2–95¢ band "
           f"({u['resolved']:,} resolved).", "",
           "### T1 — single features (monthly rank IC, Newey–West t, Benjamini–Hochberg q = 0.10)", "",
           "| Feature | IC vs copy edge | t | IC vs whale edge | t | IC vs 24h markout | t | passes BH (copy) |",
           "|---|---|---|---|---|---|---|---|"]
    by = {}
    for r in ex["T1"]:
        by.setdefault(r["feature"], {})[r["target"]] = r
    def f(r): return "–" if r is None or r.get("mean_ic") is None else f"{r['mean_ic']:+.3f}"
    def t(r): return "–" if r is None or r.get("nw_t") is None else f"{r['nw_t']:+.1f}"
    rows = sorted(by.items(), key=lambda kv: -abs((kv[1].get("edge_copy") or {}).get("nw_t") or 0))
    for feat, d in rows:
        bh = any(d.get(k, {}).get("bh_pass") for k in ("edge_copy", "ret_copy"))
        out.append(f"| {feat} | {f(d.get('edge_copy'))} | {t(d.get('edge_copy'))} | {f(d.get('edge_whale'))} | "
                   f"{t(d.get('edge_whale'))} | {f(d.get('mo_24h'))} | {t(d.get('mo_24h'))} | {'**yes**' if bh else 'no'} |")
    out += ["", "### Quintile strategies (favourable extreme quintile of each feature)", "",
            "| Strategy | bets | edge per share | return per dollar | fade (other side) |", "|---|---|---|---|---|"]
    for s in sorted(ex["T1_strategies"], key=lambda s: -(s["edge"]["mean"] or -9)):
        out.append(f"| {s['name']} | {s['edge']['n']} | {ci(s['edge'], cents)} | {ci(s['ret'])} | {ci(s['fade'], cents)} |")
    b = ex["best_strategy"]
    dsr = b["deflated"]
    out += ["", f"Best strategy by lower confidence bound: **{b['name']}**, {ci(b['edge'], cents)} per share; "
            f"deflated Sharpe probability {dsr.get('dsr') if dsr.get('dsr') is None else round(dsr['dsr'], 3)} "
            f"(weekly Sharpe {dsr.get('sr') and round(dsr['sr'], 3)} vs {dsr.get('sr0') and round(dsr['sr0'], 3)} "
            "expected from the best of the skill-less trials).", ""]
    t2 = ex["T2"]
    out += ["### T2 — gradient-boosted model, walk-forward (purged, 7-day embargo)", "",
            f"Out-of-fold bets: {t2['oof_bets']:,}. Rank IC of predictions with copy edge: "
            f"{t2['oof_ic'].get('ic') and round(t2['oof_ic']['ic'], 3)} "
            f"[{t2['oof_ic'].get('lo') and round(t2['oof_ic']['lo'], 3)}, {t2['oof_ic'].get('hi') and round(t2['oof_ic']['hi'], 3)}].", "",
            "| Prediction quintile | bets | edge per share | return per dollar |", "|---|---|---|---|"]
    for q in t2["quintiles"]:
        out.append(f"| Q{q['q'] + 1} | {q['n']} | {ci(q, cents)} | {pct(q['ret'])} |")
    out += ["", f"Does a model beat the market's own price as a probability? log-loss market "
            f"{t2['logloss_market']:.4f} vs model {t2['logloss_model']:.4f} → "
            f"**{'yes' if t2['beats_market'] else 'no'}**.", "",
            "Most important features (permutation importance, last OOF quarter): " +
            ", ".join(f"{r['feature']} ({r['importance']:+.4f})" for r in t2["importance"][:8]), ""]
    t3 = ex["T3"]
    if "spearman_halves" in t3:
        out += ["### T3 — does wallet skill persist?", "",
                f"{t3['wallets']} wallets with ≥ 5 resolved big bets in each half of their own history. Rank correlation "
                f"of shrunk edge, first half vs second half: **{t3['spearman_halves']:+.3f}** (t = {t3['t']:.1f}, "
                f"p = {t3['p']:.4f}).", "",
                "| First-half decile | wallets | first-half edge | second-half edge (whale) | second-half edge (copier) |",
                "|---|---|---|---|---|"]
        for d in t3["deciles"]:
            out.append(f"| D{int(d['d1']) + 1} | {d['n']} | {cents(d['e1'])} | {cents(d['e2'])} | {cents(d['copy2'])} |")
        out.append("")
    out += ["### T4 — fading the losers", "", "| Faded group | bets | edge per share (other side) |", "|---|---|---|"]
    for f_ in ex["T4"]:
        out.append(f"| {f_['name']} | {f_.get('n')} | {ci(f_, cents)} |")
    out += ["", "### T5 — market-wide: calibration and drift", "",
            "| Category | Entry price | bets | events | edge per share after costs |", "|---|---|---|---|---|"]
    for r in ex["T5"]:
        if r["test"] == "T5-cal" and r["category"] == "ALL":
            out.append(f"| ALL | {r['bucket']} | {r['n']} | {r['clusters']} | {ci(r, cents)} |")
    for r in ex["T5"]:
        if r["test"] == "T5-drift":
            out.append(f"| drift | {r['bucket']} | {r['n']} | {r['clusters']} | {ci(r)} per dollar |")
    out += ["", "Finalists frozen for the holdout: " + ", ".join(f["name"] for f in ex["finalists"]), ""]
    return out


def holdout_section(ho: list[dict[str, Any]]) -> list[str]:
    out = ["## Holdout — NEW (1 Oct → now), finalists only, touched once", "",
           "| Finalist | OLD edge per share | NEW edge per share | NEW return per dollar | NEW 7-day mark-to-market | "
           "NEW wallets | Verdict |", "|---|---|---|---|---|---|---|"]
    for r in ho:
        out.append(f"| {r['name']} | {ci(r['old'], cents)} | {ci(r['new'], cents)} | {ci(r['new_ret'])} | "
                   f"{ci(r['new_mtm_7d'])} | {r['new_wallets']} | **{r['verdict']}** |")
    return out + [""]


def main() -> None:
    parts = ["# WHALESCAN alpha hunt — results", "",
             "Pre-registration: docs/superpowers/specs/2026-11-04-alpha-hunt-preregistration.md", ""]
    for name, fn in (("t0.json", t0_section), ("explore.json", explore_section), ("holdout.json", holdout_section)):
        p = OUT / name
        if p.exists():
            parts += fn(json.loads(p.read_text()))
    (OUT / "report.md").write_text("\n".join(parts))
    print(OUT / "report.md")


if __name__ == "__main__":
    main()
