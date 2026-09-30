"""Permanent archive of every large fill the sweep reads (evidence-study spec §10).

The sweep keeps only a rolling 24 h of fills; this keeps all of them. Each round writes ONE new, immutable, gzipped
JSON-lines file with only the fills it had not seen before, so the `archive` branch grows by the data itself and
git never stores rewritten copies. These are public on-chain fills of other wallets — never the user's own trades.
"""

from __future__ import annotations

import gzip
import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from whalescan.models import Trade


def archive_round(root: Path, trades: list[Trade], now: int) -> Path | None:
    """Write this round's new fills to fills/<UTC date>/<HHMMSS>.jsonl.gz; None when there is nothing new."""
    if not trades:
        return None
    when = datetime.fromtimestamp(now, UTC)
    day = root / "fills" / when.strftime("%Y-%m-%d")
    day.mkdir(parents=True, exist_ok=True)
    path = day / f"{when.strftime('%H%M%S')}.jsonl.gz"
    n = 1
    while path.exists():  # two rounds in the same second never overwrite each other
        path = day / f"{when.strftime('%H%M%S')}-{n}.jsonl.gz"
        n += 1
    with gzip.open(path, "wt") as f:
        for t in sorted(trades, key=lambda t: (t.ts, t.tx_hash)):
            f.write(json.dumps({**asdict(t), "usdc": t.usdc}, separators=(",", ":")) + "\n")
    return path


def read_archive(root: Path) -> list[Trade]:
    fields = set(Trade.__dataclass_fields__)
    out: list[Trade] = []
    for path in sorted((root / "fills").glob("*/*.jsonl.gz")):
        with gzip.open(path, "rt") as f:
            out.extend(Trade(**{k: v for k, v in json.loads(line).items() if k in fields}) for line in f)
    return out
