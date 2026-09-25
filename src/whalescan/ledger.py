"""The Track Record ledger store: append-only JSON Lines, replayed to answer questions (Track Record spec §5).

`calls.jsonl` — one line per call, appended once per (event, kind), never rewritten.
`marks.jsonl` — one line per checkpoint or settlement, appended, never rewritten.
Corrupt lines are skipped and logged; they never abort a load, so one bad line can't take the ledger down.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out = []
    for i, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except ValueError as e:
            log.warning("skipping corrupt line %d in %s: %s", i, path.name, e)
    return out


def _append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(row, separators=(",", ":")) + "\n")


class Ledger:
    def __init__(self, directory: Path | str) -> None:
        self.dir = Path(directory)
        self._calls_path = self.dir / "calls.jsonl"
        self._marks_path = self.dir / "marks.jsonl"

    def calls(self) -> list[dict[str, Any]]:
        return _read_jsonl(self._calls_path)

    def marks(self) -> list[dict[str, Any]]:
        return _read_jsonl(self._marks_path)

    def has_call(self, call_id: str) -> bool:
        return any(c["id"] == call_id for c in self.calls())

    def append_call(self, call: dict[str, Any]) -> None:
        _append_jsonl(self._calls_path, call)

    def append_mark(self, mark: dict[str, Any]) -> None:
        _append_jsonl(self._marks_path, mark)

    def open_calls(self) -> list[dict[str, Any]]:
        settled = {m["call_id"] for m in self.marks() if m["type"] == "SETTLEMENT"}
        return [c for c in self.calls() if c["id"] not in settled]

    def due_checkpoints(self, now: int) -> list[tuple[dict[str, Any], int, int]]:
        """(call, day, due_ts) for every checkpoint that is due and not yet marked, across all open calls.
        A sweep that runs late returns every day it missed, each exactly once — none are skipped."""
        marked_days = {(m["call_id"], m["day"]) for m in self.marks() if m["type"] == "CHECKPOINT"}
        out: list[tuple[dict[str, Any], int, int]] = []
        for c in self.open_calls():
            for entry in c["schedule"]:
                if entry["due_ts"] > now:
                    continue
                if (c["id"], entry["day"]) in marked_days:
                    continue
                out.append((c, entry["day"], entry["due_ts"]))
        return out

    def latest_mark(self, call_id: str) -> dict[str, Any] | None:
        marks = [m for m in self.marks() if m["call_id"] == call_id]
        if not marks:
            return None
        return max(marks, key=lambda m: m["at"])
