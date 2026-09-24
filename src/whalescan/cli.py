"""Command line entry point: `whalescan score` and `whalescan batch`."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from dataclasses import replace
from pathlib import Path

import pandas as pd

from whalescan.api.http import BlockedError
from whalescan.batch import BatchReport, run_batch
from whalescan.config import load_config
from whalescan.live import Station
from whalescan.store import LockedError


def format_table(df: pd.DataFrame | None) -> str:
    if df is None or df.empty:
        return "NO CERTIFIED WALLETS"
    lines = [f"{'WALLET':<44} {'CATEGORY':<12} {'N':>5} {'EDGE':>7} {'POST':>7} {'P':>9}"]
    for r in df.itertuples(index=False):
        lines.append(f"{r.wallet:<44} {r.category:<12} {int(r.n):>5} {r.edge:>+7.3f} {r.post_edge:>+7.3f} "
                     f"{r.p_value:>9.2e}")
    return "\n".join(lines)


def summary(r: BatchReport) -> str:
    return (f"scanned {r.wallets_scanned} wallets ({r.wallets_complete} complete) · {r.tests} tests · "
            f"{r.testable} testable · {r.certified_wallets} certified · {r.signals} signals · "
            f"{r.contacts} contacts · {r.api_errors} API errors")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="whalescan", description="Statistically certified Polymarket whale scanner")
    parser.add_argument("--config", type=Path, default=None, help="path to config.toml")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name, text in (("score", "refresh and score wallets, print certified table"),
                       ("batch", "full pipeline: score, gate, validate, write snapshot")):
        p = sub.add_parser(name, help=text)
        p.add_argument("--max-wallets", type=int, default=None, help="override universe.max_wallets")
        if name == "batch":
            p.add_argument("--skip-validation", action="store_true")
            p.add_argument("--force-validation", action="store_true")
            p.add_argument("--publish", action="store_true", help="git commit + push data/snapshot afterwards")
    live = sub.add_parser("live", help="real-time station + dashboard on http://127.0.0.1:8765")
    live.add_argument("--host", default="127.0.0.1")
    live.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    # httpx logs every request at INFO; a full batch makes tens of thousands of them.
    logging.getLogger("httpx").setLevel(logging.DEBUG if args.verbose else logging.WARNING)
    cfg = load_config(args.config)
    if getattr(args, "max_wallets", None):
        cfg = replace(cfg, universe=replace(cfg.universe, max_wallets=args.max_wallets))
    try:
        if args.cmd == "live":
            print(f"WHALESCAN live -> http://{args.host}:{args.port}  (Ctrl-C to stop)")
            try:
                asyncio.run(Station(cfg).run(args.host, args.port))
            except KeyboardInterrupt:  # uvicorn re-raises Ctrl-C after its own clean shutdown
                pass
            print("WHALESCAN live stopped")
            return 0
        if args.cmd == "score":
            report = asyncio.run(run_batch(cfg, stop_after_scoring=True))
            print(format_table(report.top))
        else:
            report = asyncio.run(run_batch(cfg, skip_validation=args.skip_validation,
                                           force_validation=args.force_validation, publish=args.publish))
        print(summary(report))
    except LockedError as e:
        print(f"LOCKED: {e}", file=sys.stderr)
        return 2
    except BlockedError as e:
        print(f"BLOCKED: {e}\nPolymarket refused this machine. Run `whalescan batch --publish` from your own "
              f"computer instead; the previous snapshot was left untouched.", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
