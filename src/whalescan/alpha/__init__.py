"""Alpha hunt (docs/superpowers/specs/2026-11-04-alpha-hunt-preregistration.md): research code, not the scanner.

OLD = signal time before 2026-10-01 (exploration); NEW = from 2026-10-01 (the holdout, touched once)."""

from datetime import UTC, datetime

SPLIT_TS = int(datetime(2026, 10, 1, tzinfo=UTC).timestamp())
