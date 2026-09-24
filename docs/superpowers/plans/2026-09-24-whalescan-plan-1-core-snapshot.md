# WHALESCAN Plan 1 — Core Engine + Public Snapshot

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the C++ core (Monte Carlo skill engine + order book), the Python scoring/gating/validation pipeline, and a military-style dashboard published to GitHub Pages every 6 hours — a complete, demo-able WHALESCAN in snapshot mode.

**Architecture:** A Python package (`whalescan`) orchestrates async HTTP ingestion from Polymarket's public APIs into DuckDB, scores wallets with a nanobind-wrapped C++20 library (`whalecore`), gates recent whale trades into signals, runs walk-forward validation, and writes JSON snapshots. A Vite/React/TypeScript dashboard renders those JSON files; GitHub Actions runs the pipeline on a cron and deploys to Pages.

**Tech Stack:** C++20, nanobind, scikit-build-core, CMake, Catch2 · Python 3.12, uv, httpx (async, HTTP/2), numpy, pandas, DuckDB, pyarrow, pytest · TypeScript, React, Vite, Vitest, lightweight-charts, IBM Plex Mono · GitHub Actions + Pages.

**Spec:** `docs/superpowers/specs/2026-09-24-whalescan-design.md` (read Part 0 first).

**Scope of this plan:** spec milestones 1–2 plus the C++ order book (needed by gate G6 in snapshot mode). **Plan 2** (written after this plan lands, because it builds on these real interfaces) covers milestones 3–5: live station, reconnecting WebSockets, CLOB watch set, live BOOK panel, STALE re-evaluation, polish.

---

## Part 0 — Under the hood: the tooling in this plan

The spec's Part 0 explains the *domain* (REST vs WebSockets, Polymarket, order books, the statistics). This section explains the *tools* you will see in every task, so each command makes sense.

**uv and the lockfile.** `uv` is a fast Python project manager (written in Rust). `pyproject.toml` declares what the project needs ("httpx ≥ 0.27"); `uv sync` resolves exact versions, writes them to `uv.lock`, creates `.venv/`, and installs everything. Committing `uv.lock` means CI installs *exactly* the versions you tested. `uv run pytest` runs a command inside that environment without "activating" anything.

**How C++ becomes `import whalecore`.** `pyproject.toml` names **scikit-build-core** as the *build backend*. When uv installs the project, scikit-build-core runs **CMake**, which reads `CMakeLists.txt`, invokes clang (on your Mac) or gcc (on CI), and compiles `cpp/src/*.cpp` into a shared library named like `whalecore.cpython-312-darwin.so`. Python's import system treats such a `.so` exactly like a `.py` module. **nanobind** supplies the glue: `NB_MODULE(whalecore, m) { m.def("skill_mc", ...); }` registers C++ functions and classes, converting Python ints/floats/numpy arrays to C++ types and back. `[tool.uv] cache-keys` tells uv to rebuild whenever a `.cpp`/`.hpp` file changes, so `uv sync` after a C++ edit is all you need.

**The GIL, and why the bindings release it.** CPython has a Global Interpreter Lock: only one thread runs Python bytecode at a time. Our C++ Monte Carlo spawns its own `std::thread`s and never touches Python objects, so the binding wraps the call in `nb::gil_scoped_release` — while C++ crunches numbers, other Python code (e.g., async HTTP) keeps running.

**Two passes of Monte Carlo (screening).** A p-value estimated from N simulations has standard error ≈ √(p(1−p)/N). With 2,000 sims, a true p of 0.2 is measured ±0.009 — plenty to know a test is nowhere near significant. So we screen every test cheaply and spend 100,000 sims only where precision matters (p near BH thresholds). Same answer, ~20× less compute.

**Catch2 and pytest.** Catch2 is a C++ unit-test framework: `TEST_CASE("…") { REQUIRE(x == 3); }`. We build it as a separate executable (`whalecore_tests`) and `ctest` runs it. pytest is Python's equivalent: any `test_*` function in `tests/` runs; `assert` statements are the checks; `tmp_path` is a built-in *fixture* giving each test a fresh temp directory.

**Testing HTTP without the network: `httpx.MockTransport`.** httpx lets you swap its network layer for a Python function `handler(request) -> response`. Our API clients take the transport as a parameter, so tests feed them canned responses (pagination, 429 rate limits, Cloudflare blocks) deterministically and offline.

**asyncio in 60 seconds.** `async def` defines a *coroutine*; `await x` pauses it until `x` is ready (e.g., an HTTP response) and lets the *event loop* run other coroutines meanwhile. One thread can keep dozens of HTTP requests in flight. `asyncio.gather(*coros)` runs many concurrently; an `asyncio.Semaphore(8)` caps how many run at once. `asyncio.run(main())` starts the loop.

**Token bucket rate limiting.** A bucket holds up to B tokens and refills at R tokens/second. Each request takes one token; if the bucket is empty, the caller sleeps until one refills. This produces a smooth R requests/second with small bursts — polite to Polymarket and less likely to hit HTTP 429 ("too many requests").

**Exponential backoff.** On 429/5xx we wait 0.5s, 1s, 2s, 4s… (or whatever `Retry-After` says) before retrying. Doubling quickly backs off from an overloaded server instead of hammering it.

**DuckDB upserts.** `INSERT OR REPLACE INTO t SELECT * FROM df` inserts rows, replacing any with the same PRIMARY KEY. Re-running a step therefore never duplicates data (idempotency). DuckDB can query a pandas DataFrame directly by name after `con.register("df", df)`.

**Atomic writes and file locks.** Writing a JSON file in place risks a reader seeing half a file. We write `x.json.tmp` then `os.replace(tmp, x.json)` — a rename is atomic on POSIX, so readers see either the old or the new file, never a mix. `fcntl.flock` places an OS-level lock on a file; a second process asking for the same lock fails immediately, which is how we stop two batch runs from colliding on one DuckDB file.

**Vite, React, TypeScript.** TypeScript is JavaScript with types (the compiler catches `signal.pric` typos). React builds UIs from functions that return markup (JSX) given state; when state changes React re-renders. Vite is the dev server and bundler: `npm run dev` serves with instant reload; `npm run build` emits static `dist/` files any web host (GitHub Pages) can serve.

**GitHub Actions anatomy.** A workflow YAML has triggers (`on: push`, `on: schedule: cron`), jobs (each on a fresh VM), and steps (shell commands or reusable `uses:` actions). `actions/cache` persists files (our DuckDB) between runs keyed by name. Pages deployment uploads `web/dist` as an artifact and publishes it.

---

## Global Constraints

- Python `>=3.12` (pinned to 3.12 via `.python-version`); C++ standard C++20; CMake `>=3.26`.
- Every HTTP request sends `User-Agent: whalescan/0.1 (+https://github.com/whalescan)`; the default Python UA is blocked by Cloudflare (verified: 403 `text/plain`, `server: cloudflare`).
- `/closed-positions` is always called with `sortBy=TIMESTAMP&sortDirection=DESC`, page size 50, paginated to exhaustion; the API's default order is `realizedPnl DESC` (verified), which would bias scoring.
- Data API offset cap: `offset > 10000` returns HTTP 400 JSON `{"error": "max historical trades offset of 10000 exceeded"}`; hitting it marks the history `incomplete`.
- Gamma `/markets` hides closed markets unless `closed=true`; always query `closed=true` and `closed=false`; max 100 rows per call; `include_tag=true` returns market tags.
- A position counts for scoring only if its market is `closed` and `outcomePrices` is exactly one `1` and the rest `0`.
- Taker fee in USDC per share: `rate × (p(1−p))^exponent` from Gamma `feeSchedule`, 0 when `feesEnabled` is false (verified against live fills).
- All thresholds live in `config.toml`; code never hard-codes gate or scoring constants.
- Snapshot JSON must be strict JSON: no `NaN`/`Infinity` (browsers reject them) — non-finite numbers become `null`.
- Dashboard: IBM Plex Mono everywhere, dark only, palette `#07090B #0C1014 #1C242C #C9D1D9 #6B7785 #FFB000 #3DDC84 #FF4D4D #4FC3F7`.
- The tool never places orders and never handles keys.
- €0 infrastructure: no paid services, no proxies.

## Review Focus

1. **Truncated wallet history** (offset cap / HTTP 400 at depth) — the wallet must be marked incomplete and never certified; pinned in Task 7 (`test_closed_positions_offset_cap_marks_incomplete`) and Task 10 (`test_prepare_excludes_incomplete_and_unresolved`).
2. **Trade in a market Gamma doesn't return** — the event must appear in CONTACTS with `G5 UNKNOWN MARKET`, and the batch must continue; pinned in Task 11 (`test_unknown_market_fails_g5_without_crashing`).
3. **Empty or thin order book** — G6 must fail with `BOOK TOO THIN`, and no `NaN` may leak into JSON; pinned in Task 4 (`walk on empty book`), Task 11 (`test_thin_book_fails_g6`), Task 13 (`test_json_has_no_nan`).
4. **Zero certified whales** (first run, strict BH) — the snapshot is still written and the dashboard shows explicit empty states; pinned in Task 13 (`test_batch_with_no_certified_whales_writes_snapshot`) and Task 14 (`signalsEmptyMessage` test).
5. **Bot-protection block** (Cloudflare 403) — no retries, `BlockedError`, CLI exit code 3, and the previous snapshot left untouched; pinned in Task 6 (`test_cloudflare_block_raises_without_retry`) and Task 13 (`test_cli_blocked_exit_code_keeps_snapshot`).

---

## File Map

```
whalescan/
├── pyproject.toml, .python-version, uv.lock, config.toml, CMakeLists.txt, .gitignore, README.md
├── cpp/include/whalecore/{version,montecarlo,orderbook}.hpp
├── cpp/src/{version,montecarlo,orderbook,bindings}.cpp
├── cpp/tests/{test_version,test_montecarlo,test_orderbook}.cpp
├── scripts/{test-cpp.sh, smoke.py, record_fixtures.py}
├── src/whalescan/
│   ├── __init__.py, config.py            (Task 1)
│   ├── models.py, parsers.py             (Task 5)
│   ├── api/__init__.py, api/http.py      (Task 6)
│   ├── api/data_api.py, api/gamma.py, api/clob.py   (Task 7)
│   ├── store.py                          (Task 8)
│   ├── classify.py                       (Task 9)
│   ├── scoring.py                        (Task 10)
│   ├── book.py, gate.py                  (Task 11)
│   ├── validate.py                       (Task 12)
│   └── snapshot.py, batch.py, cli.py     (Task 13)
├── tests/ (one test module per source module; fixtures in tests/fixtures/real/)
├── web/ (Task 14)
├── docs/polymarket-api-notes.md (Task 2)
└── .github/workflows/{smoke,ci,snapshot}.yml (Tasks 2, 15)
```

---

### Task 1: Scaffold, build toolchain, config

**Files:**
- Create: `pyproject.toml`, `.python-version`, `.gitignore`, `CMakeLists.txt`, `config.toml`
- Create: `cpp/include/whalecore/version.hpp`, `cpp/src/version.cpp`, `cpp/src/bindings.cpp`, `cpp/tests/test_version.cpp`, `scripts/test-cpp.sh`
- Create: `src/whalescan/__init__.py`, `src/whalescan/config.py`
- Test: `tests/test_build.py`, `tests/test_config.py`

**Interfaces:**
- Produces: `import whalecore; whalecore.version() -> str`; `whalescan.config.load_config(path: Path | None = None) -> Config`; `Config.path(rel: str) -> Path`; dataclasses `PathsCfg, HttpCfg, UniverseCfg, ScoringCfg, GateCfg, ValidationCfg, BlocklistCfg, CategoriesCfg` with exactly the keys in `config.toml` below; `whalescan.config.ROOT: Path`.

- [ ] **Step 1: Write build files**

`.python-version`:
```
3.12
```

`.gitignore`:
```
.venv/
build/
__pycache__/
.pytest_cache/
*.egg-info/
data/*.duckdb
data/*.duckdb.wal
data/*.lock
data/scores.parquet
web/node_modules/
web/dist/
web/public/data/
.DS_Store
```

`pyproject.toml`:
```toml
[build-system]
requires = ["scikit-build-core>=0.10", "nanobind>=2.4"]
build-backend = "scikit_build_core.build"

[project]
name = "whalescan"
version = "0.1.0"
description = "Statistically certified Polymarket whale scanner"
requires-python = ">=3.12"
dependencies = [
  "httpx[http2]>=0.27",
  "numpy>=2.0",
  "pandas>=2.2",
  "duckdb>=1.1",
  "pyarrow>=17",
]

[project.scripts]
whalescan = "whalescan.cli:main"

[dependency-groups]
dev = ["pytest>=8.3", "pytest-asyncio>=0.24"]

[tool.scikit-build]
wheel.packages = ["src/whalescan"]
cmake.build-type = "Release"
build-dir = "build/{wheel_tag}"

[tool.uv]
cache-keys = [
  { file = "pyproject.toml" },
  { file = "CMakeLists.txt" },
  { file = "cpp/**/*.cpp" },
  { file = "cpp/**/*.hpp" },
]

[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
asyncio_default_fixture_loop_scope = "function"
```

`CMakeLists.txt`:
```cmake
cmake_minimum_required(VERSION 3.26)
project(whalecore LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

option(WHALECORE_BUILD_PYTHON "Build the Python extension module" ON)
option(WHALECORE_BUILD_TESTS "Build the Catch2 test executable" OFF)

find_package(Threads REQUIRED)

add_library(whalecore_lib STATIC
  cpp/src/version.cpp
)
target_include_directories(whalecore_lib PUBLIC cpp/include)
target_link_libraries(whalecore_lib PUBLIC Threads::Threads)
set_target_properties(whalecore_lib PROPERTIES POSITION_INDEPENDENT_CODE ON)
target_compile_options(whalecore_lib PRIVATE -O3 -Wall -Wextra -Wpedantic)

if(WHALECORE_BUILD_PYTHON)
  find_package(Python 3.12 REQUIRED COMPONENTS Interpreter Development.Module)
  find_package(nanobind CONFIG REQUIRED)
  nanobind_add_module(whalecore NB_STATIC cpp/src/bindings.cpp)
  target_link_libraries(whalecore PRIVATE whalecore_lib)
  install(TARGETS whalecore LIBRARY DESTINATION .)
endif()

if(WHALECORE_BUILD_TESTS)
  include(FetchContent)
  FetchContent_Declare(Catch2
    GIT_REPOSITORY https://github.com/catchorg/Catch2.git
    GIT_TAG v3.7.1)
  FetchContent_MakeAvailable(Catch2)
  add_executable(whalecore_tests
    cpp/tests/test_version.cpp
  )
  target_link_libraries(whalecore_tests PRIVATE whalecore_lib Catch2::Catch2WithMain)
  include(CTest)
  list(APPEND CMAKE_MODULE_PATH ${catch2_SOURCE_DIR}/extras)
  include(Catch)
  catch_discover_tests(whalecore_tests)
endif()
```

`scripts/test-cpp.sh` (then `chmod +x scripts/test-cpp.sh`):
```bash
#!/usr/bin/env bash
# Build and run the C++ unit tests without Python.
set -euo pipefail
cd "$(dirname "$0")/.."
cmake -S . -B build/cpp -DWHALECORE_BUILD_TESTS=ON -DWHALECORE_BUILD_PYTHON=OFF -DCMAKE_BUILD_TYPE=Release
cmake --build build/cpp -j
ctest --test-dir build/cpp --output-on-failure
```

- [ ] **Step 2: Write the failing tests**

`cpp/tests/test_version.cpp`:
```cpp
#include <catch2/catch_test_macros.hpp>
#include <string>

#include "whalecore/version.hpp"

TEST_CASE("version string is semver") {
    REQUIRE(std::string(whalecore::version()) == "0.1.0");
}
```

`tests/test_build.py`:
```python
import whalecore


def test_extension_imports_and_reports_version():
    assert whalecore.version() == "0.1.0"
```

`tests/test_config.py`:
```python
from pathlib import Path

import pytest

from whalescan.config import ROOT, load_config


def test_loads_repo_config():
    cfg = load_config()
    assert cfg.scoring.bh_q == 0.10
    assert cfg.gate.min_usdc == 5000
    assert cfg.http.user_agent.startswith("whalescan/")
    assert set(cfg.categories.order) <= set(cfg.categories.tags)
    assert all(t == t.lower() for tags in cfg.categories.tags.values() for t in tags)


def test_relative_paths_resolve_from_repo_root(tmp_path):
    cfg = load_config()
    assert cfg.path("data/x.duckdb") == ROOT / "data/x.duckdb"
    assert cfg.path(str(tmp_path / "abs.duckdb")) == tmp_path / "abs.duckdb"


def test_missing_key_is_an_error(tmp_path):
    text = (ROOT / "config.toml").read_text().replace("bh_q = 0.10\n", "")
    p = tmp_path / "c.toml"
    p.write_text(text)
    with pytest.raises(ValueError, match="scoring.bh_q"):
        load_config(p)


def test_unknown_key_is_an_error(tmp_path):
    text = (ROOT / "config.toml").read_text().replace("bh_q = 0.10\n", "bh_q = 0.10\nbhq = 1\n")
    p = tmp_path / "c.toml"
    p.write_text(text)
    with pytest.raises(ValueError, match="bhq"):
        load_config(p)
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv sync` — Expected: FAIL (CMake error: `cpp/src/version.cpp` / `bindings.cpp` not found).

- [ ] **Step 4: Write the implementation**

`cpp/include/whalecore/version.hpp`:
```cpp
#pragma once

namespace whalecore {
const char* version();
}  // namespace whalecore
```

`cpp/src/version.cpp`:
```cpp
#include "whalecore/version.hpp"

namespace whalecore {
const char* version() { return "0.1.0"; }
}  // namespace whalecore
```

`cpp/src/bindings.cpp`:
```cpp
#include <nanobind/nanobind.h>

#include "whalecore/version.hpp"

namespace nb = nanobind;

NB_MODULE(whalecore, m) {
    m.doc() = "WHALESCAN C++ core: Monte Carlo skill engine and order book";
    m.def("version", &whalecore::version, "Library version string.");
}
```

`src/whalescan/__init__.py`:
```python
"""WHALESCAN — statistically certified Polymarket whale scanner."""

__version__ = "0.1.0"
```

`config.toml`:
```toml
# Every threshold WHALESCAN uses. Paths are relative to the repo root unless absolute.

[paths]
research_db = "data/research.duckdb"
scores_parquet = "data/scores.parquet"
snapshot_dir = "data/snapshot"

[http]
rate_per_s = 5.0
max_retries = 5
concurrency = 8
user_agent = "whalescan/0.1 (+https://github.com/whalescan)"

[universe]
max_wallets = 3000
leaderboard_periods = ["ALL", "MONTH"]
leaderboard_orders = ["PNL", "VOL"]
leaderboard_pages = 10
large_trade_min_usdc = 5000
large_trade_lookback_days = 30

[scoring]
n_sims = 100000
n_sims_screen = 2000
screen_p = 0.2
seed = 20260924
min_n_eff = 20
bh_q = 0.10
min_post_edge = 0.03
price_min = 0.03
price_max = 0.97
winsor_quantile = 0.95
farmer_price = 0.95
farmer_share = 0.60
lottery_price = 0.05
lottery_share = 0.60
mm_both_sides_share = 0.30

[gate]
aggregation_window_s = 600
min_usdc = 5000
conviction_k = 2.0
price_min = 0.05
price_max = 0.95
min_hours_to_end = 2.0
follow_size_usdc = 1000
min_net_edge = 0.02
max_entry_margin = 0.01
tier_a_net_edge = 0.05
tier_a_consensus = 2
conflict_window_h = 24
fallback_max_cat_positions = 10
signal_lookback_h = 24
max_contacts = 300

[validation]
fold_days = 14
min_history_days = 60
slippage = 0.015
n_sims = 20000
max_wallets = 300
every_hours = 24
min_signals = 30

[blocklist]
slug_patterns = ["updown", "up-or-down", "-(5|15)m-"]
min_market_volume = 10000

[categories]
order = ["CRYPTO", "POLITICS", "GEOPOLITICS", "ECONOMY", "TECH", "SPORTS", "CULTURE"]

[categories.tags]
CRYPTO = ["crypto", "bitcoin", "ethereum", "solana", "crypto prices"]
POLITICS = ["politics", "elections", "us politics", "global elections", "trump"]
GEOPOLITICS = ["geopolitics", "world", "middle east", "ukraine", "israel", "china"]
ECONOMY = ["economy", "fed", "finance", "business", "stocks", "economic policy"]
TECH = ["tech", "ai", "science", "big tech"]
SPORTS = ["sports", "esports", "nfl", "nba", "mlb", "nhl", "soccer", "games", "tennis", "ufc"]
CULTURE = ["culture", "pop culture", "movies", "music", "awards", "mentions", "tweets", "celebrities"]
```

`src/whalescan/config.py`:
```python
"""Typed access to config.toml. Missing or unknown keys are errors, never silent defaults."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class PathsCfg:
    research_db: str
    scores_parquet: str
    snapshot_dir: str


@dataclass(frozen=True)
class HttpCfg:
    rate_per_s: float
    max_retries: int
    concurrency: int
    user_agent: str


@dataclass(frozen=True)
class UniverseCfg:
    max_wallets: int
    leaderboard_periods: tuple[str, ...]
    leaderboard_orders: tuple[str, ...]
    leaderboard_pages: int
    large_trade_min_usdc: float
    large_trade_lookback_days: int


@dataclass(frozen=True)
class ScoringCfg:
    n_sims: int
    n_sims_screen: int
    screen_p: float
    seed: int
    min_n_eff: float
    bh_q: float
    min_post_edge: float
    price_min: float
    price_max: float
    winsor_quantile: float
    farmer_price: float
    farmer_share: float
    lottery_price: float
    lottery_share: float
    mm_both_sides_share: float


@dataclass(frozen=True)
class GateCfg:
    aggregation_window_s: int
    min_usdc: float
    conviction_k: float
    price_min: float
    price_max: float
    min_hours_to_end: float
    follow_size_usdc: float
    min_net_edge: float
    max_entry_margin: float
    tier_a_net_edge: float
    tier_a_consensus: int
    conflict_window_h: float
    fallback_max_cat_positions: int
    signal_lookback_h: float
    max_contacts: int


@dataclass(frozen=True)
class ValidationCfg:
    fold_days: int
    min_history_days: int
    slippage: float
    n_sims: int
    max_wallets: int
    every_hours: float
    min_signals: int


@dataclass(frozen=True)
class BlocklistCfg:
    slug_patterns: tuple[str, ...]
    min_market_volume: float


@dataclass(frozen=True)
class CategoriesCfg:
    order: tuple[str, ...]
    tags: dict[str, tuple[str, ...]]


@dataclass(frozen=True)
class Config:
    paths: PathsCfg
    http: HttpCfg
    universe: UniverseCfg
    scoring: ScoringCfg
    gate: GateCfg
    validation: ValidationCfg
    blocklist: BlocklistCfg
    categories: CategoriesCfg

    def path(self, rel: str) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else ROOT / p


def _section(cls: type, name: str, raw: dict[str, Any]) -> Any:
    known = {f.name for f in fields(cls)}
    unknown = set(raw) - known
    if unknown:
        raise ValueError(f"config: unknown key(s) {name}.{sorted(unknown)}")
    kwargs = {}
    for f in fields(cls):
        if f.name not in raw:
            raise ValueError(f"config: missing {name}.{f.name}")
        value = raw[f.name]
        kwargs[f.name] = tuple(value) if isinstance(value, list) else value
    return cls(**kwargs)


def load_config(path: Path | None = None) -> Config:
    path = path or Path(os.environ.get("WHALESCAN_CONFIG", ROOT / "config.toml"))
    raw = tomllib.loads(path.read_text())
    cats = raw["categories"]
    return Config(
        paths=_section(PathsCfg, "paths", raw["paths"]),
        http=_section(HttpCfg, "http", raw["http"]),
        universe=_section(UniverseCfg, "universe", raw["universe"]),
        scoring=_section(ScoringCfg, "scoring", raw["scoring"]),
        gate=_section(GateCfg, "gate", raw["gate"]),
        validation=_section(ValidationCfg, "validation", raw["validation"]),
        blocklist=_section(BlocklistCfg, "blocklist", raw["blocklist"]),
        categories=CategoriesCfg(
            order=tuple(cats["order"]),
            tags={k: tuple(t.lower() for t in v) for k, v in cats["tags"].items()},
        ),
    )
```

- [ ] **Step 5: Build and run all tests**

Run: `uv sync && uv run pytest -q && scripts/test-cpp.sh`
Expected: `uv sync` compiles the extension (first build ~30–60 s); pytest `5 passed`; ctest `100% tests passed`.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "build: scaffold uv + scikit-build-core + nanobind toolchain and typed config"
```

---

### Task 2: API groundwork — notes, real fixtures, GitHub smoke test

**Files:**
- Create: `docs/polymarket-api-notes.md`, `scripts/record_fixtures.py`, `scripts/smoke.py`, `.github/workflows/smoke.yml`
- Create (generated): `tests/fixtures/real/*.json`

**Interfaces:**
- Produces: `tests/fixtures/real/{trades_large,closed_positions,leaderboard,markets_closed,markets_open,book,prices_history}.json` — real API payloads (arrays of rows, or a single object for `book`/`prices_history`) used by Task 5 parser tests.

- [ ] **Step 1: Write the API notes**

`docs/polymarket-api-notes.md`:
```markdown
# Polymarket public API — verified behaviour (2026-09-24)

All endpoints are public, JSON, no auth. Always send a custom User-Agent:
Python's default (`Python-urllib/x`) gets `403 text/plain` from `server: cloudflare`.

## Data API — https://data-api.polymarket.com
- `GET /trades?limit≤1000&offset&user&takerOnly=false&filterType=CASH&filterAmount=<usd>`
  newest first. `offset > 10000` → **HTTP 400** `{"error":"max historical trades offset of 10000 exceeded"}`.
  Row: proxyWallet, side, asset, conditionId, size, price, timestamp(s), title, slug, eventSlug,
  outcome, outcomeIndex, name, pseudonym, transactionHash.
- `GET /closed-positions?user&limit(max 50)&offset&sortBy=TIMESTAMP&sortDirection=DESC`
  **Default sort is realizedPnl DESC** — page 1 of a top wallet is 100% winners. Always sort by TIMESTAMP.
  Row: proxyWallet, asset, conditionId, avgPrice, totalBought, realizedPnl, curPrice, title, slug,
  eventSlug, outcome, outcomeIndex, oppositeOutcome, oppositeAsset, endDate, timestamp.
- `GET /v1/leaderboard?timePeriod=(DAY|WEEK|MONTH|ALL)&orderBy=(PNL|VOL)&limit(max 50)&offset`
  deep pagination works (offset 2000 OK). Row: rank(str), proxyWallet, userName, vol, pnl.

## Gamma — https://gamma-api.polymarket.com
- `GET /markets?condition_ids=a&condition_ids=b&closed=(true|false)&include_tag=true&limit≤100`
  Repeated `condition_ids` params; comma-joined does NOT work. Closed markets only appear with `closed=true`.
  Fields: conditionId, question, slug, endDate (ISO Z), closed, closedTime ("2026-09-21 04:30:03+00"),
  outcomePrices (JSON string, e.g. "[\"0\", \"1\"]"), clobTokenIds (JSON string), feesEnabled,
  feeSchedule {rate, exponent, takerOnly, rebateRate}, volumeNum, umaResolutionStatus, events[{slug}], tags[{label}].

## CLOB — https://clob.polymarket.com
- `GET /book?token_id=` → {market, asset_id, timestamp(ms, string), hash, bids[{price,size}] ascending, asks[...]} (strings).
- `GET /prices-history?market=<token_id>&interval=1w&fidelity=60` → {history:[{t, p}]}.
- `GET /fee-rate?token_id=` → {base_fee} (bps-like; we use Gamma feeSchedule instead).

## Fees
Taker fee (USDC) = shares × rate × (p·(1−p))^exponent. Verified on live fills, e.g. BUY 2048 @ 0.63,
rate 0.05, exponent 1 → 23.86944 (exact).

## WebSockets (Plan 2)
- RTDS `wss://ws-live-data.polymarket.com` subscribe `{"action":"subscribe","subscriptions":[{"topic":"activity","type":"trades"}]}`
  → messages `{connection_id, payload:{...trade..., fee}}`; blank keep-alive frames occur.
- CLOB market `wss://ws-subscriptions-clob.polymarket.com/ws/market` send `{"assets_ids":[...],"type":"market"}`
  → first a list with a `book` snapshot, then `{market, price_changes:[{asset_id, price, size, side, hash, best_bid, best_ask}]}`.
```

- [ ] **Step 2: Write the fixture recorder**

`scripts/record_fixtures.py`:
```python
"""Record small real API payloads into tests/fixtures/real/ for parser tests. Run manually; results are committed."""

import json
import sys
from pathlib import Path

import httpx

OUT = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "real"
UA = {"User-Agent": "whalescan/0.1 (+https://github.com/whalescan)"}
WALLET = "0x5268527977f700f9bf9b6d5cd843859e4e70135d"


def get(client: httpx.Client, url: str, params: list[tuple[str, str]]) -> object:
    r = client.get(url, params=params)
    r.raise_for_status()
    return r.json()


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    with httpx.Client(headers=UA, timeout=30) as c:
        trades = get(c, "https://data-api.polymarket.com/trades",
                     [("limit", "20"), ("takerOnly", "false"), ("filterType", "CASH"), ("filterAmount", "5000")])
        closed = get(c, "https://data-api.polymarket.com/closed-positions",
                     [("user", WALLET), ("limit", "50"), ("sortBy", "TIMESTAMP"), ("sortDirection", "DESC")])
        board = get(c, "https://data-api.polymarket.com/v1/leaderboard",
                    [("timePeriod", "ALL"), ("orderBy", "PNL"), ("limit", "20")])
        cids = sorted({row["conditionId"] for row in closed})[:20]
        m_closed = get(c, "https://gamma-api.polymarket.com/markets",
                       [("condition_ids", x) for x in cids] + [("closed", "true"), ("include_tag", "true"), ("limit", "100")])
        m_open = get(c, "https://gamma-api.polymarket.com/markets",
                     [("closed", "false"), ("include_tag", "true"), ("limit", "10"), ("order", "volume24hr"), ("ascending", "false")])
        token = json.loads(m_open[0]["clobTokenIds"])[0]
        book = get(c, "https://clob.polymarket.com/book", [("token_id", token)])
        hist = get(c, "https://clob.polymarket.com/prices-history", [("market", token), ("interval", "1w"), ("fidelity", "60")])
    for name, data in {
        "trades_large": trades, "closed_positions": closed, "leaderboard": board,
        "markets_closed": m_closed, "markets_open": m_open, "book": book, "prices_history": hist,
    }.items():
        (OUT / f"{name}.json").write_text(json.dumps(data, indent=1))
        print(f"wrote {name}.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 3: Record fixtures**

Run: `uv run python scripts/record_fixtures.py && ls tests/fixtures/real`
Expected: 7 `wrote …` lines; 7 JSON files present. Open `closed_positions.json` and confirm it contains both `"curPrice": 0` and `"curPrice": 1` rows (proves TIMESTAMP sort).

- [ ] **Step 4: Write the smoke test script and workflow**

`scripts/smoke.py` (stdlib only, so it runs before any install):
```python
"""Fail loudly if any Polymarket endpoint is blocked from this machine (used on GitHub runners)."""

import json
import sys
import urllib.error
import urllib.request

UA = "whalescan/0.1 (+https://github.com/whalescan)"
URLS = [
    "https://data-api.polymarket.com/trades?limit=1",
    "https://data-api.polymarket.com/closed-positions?user=0x5268527977f700f9bf9b6d5cd843859e4e70135d&limit=1&sortBy=TIMESTAMP",
    "https://data-api.polymarket.com/v1/leaderboard?limit=1",
    "https://gamma-api.polymarket.com/markets?limit=1",
    "https://clob.polymarket.com/markets?limit=1",
]


def main() -> int:
    failed = 0
    for url in URLS:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                json.load(r)
                print(f"OK      {r.status} {url}")
        except urllib.error.HTTPError as e:
            failed += 1
            print(f"BLOCKED {e.code} {e.headers.get('server')} {url}")
        except Exception as e:  # noqa: BLE001 — report anything else as a failure too
            failed += 1
            print(f"ERROR   {e!r} {url}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
```

`.github/workflows/smoke.yml`:
```yaml
name: api-smoke
on:
  workflow_dispatch:
  push:
    branches: [main]
    paths: ["scripts/smoke.py", ".github/workflows/smoke.yml"]
jobs:
  smoke:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: python scripts/smoke.py
```

- [ ] **Step 5: Run the smoke script locally**

Run: `uv run python scripts/smoke.py`
Expected: five `OK      200` lines, exit code 0.

- [ ] **Step 6: Commit, create the GitHub repo, run the smoke test on a runner**

```bash
git add -A
git commit -m "chore: API notes, real fixtures, GitHub runner smoke test"
```
**Ask the user before this outward-facing step:** create the public repo and push.
```bash
gh repo create whalescan --public --source . --push
gh workflow run api-smoke && sleep 5 && gh run watch "$(gh run list --workflow api-smoke -L1 --json databaseId -q '.[0].databaseId')"
```
Expected: the run succeeds with five `OK` lines. If any line says `BLOCKED`, record it in `docs/polymarket-api-notes.md` under a new `## GitHub runners` heading and tell the user that snapshot publishing will use the local fallback (`whalescan batch --publish`); the plan continues unchanged.

---

### Task 3: C++ Monte Carlo skill engine

**Files:**
- Create: `cpp/include/whalecore/montecarlo.hpp`, `cpp/src/montecarlo.cpp`, `cpp/tests/test_montecarlo.cpp`
- Modify: `CMakeLists.txt` (add sources), `cpp/src/bindings.cpp`
- Test: `tests/test_whalecore_mc.py`

**Interfaces:**
- Produces (C++): `whalecore::SkillResult{edge, p_value, null_mean, null_sd, n_eff}`; `skill_mc(span<const double> prices, span<const uint8_t> outcomes, span<const double> weights, uint32_t n_sims, uint64_t seed)`; `skill_mc_batch(prices, outcomes, weights, span<const int64_t> offsets, n_sims, seed, unsigned n_threads) -> vector<SkillResult>`.
- Produces (Python): `whalecore.skill_mc(prices: f64[n], outcomes: u8[n], weights: f64[n], n_sims=100000, seed=42) -> SkillResult`; `whalecore.skill_mc_batch(prices, outcomes, weights, offsets: i64[m+1], n_sims=100000, seed=42, n_threads=0) -> list[SkillResult]`; invalid input raises `ValueError`.

- [ ] **Step 1: Write the failing C++ tests**

`cpp/tests/test_montecarlo.cpp`:
```cpp
#include <catch2/catch_test_macros.hpp>
#include <catch2/matchers/catch_matchers_floating_point.hpp>

#include <cmath>
#include <cstdint>
#include <random>
#include <stdexcept>
#include <vector>

#include "whalecore/montecarlo.hpp"

using Catch::Matchers::WithinAbs;
using whalecore::skill_mc;
using whalecore::skill_mc_batch;

TEST_CASE("edge is the stake-weighted mean of outcome minus price") {
    std::vector<double> p{0.2, 0.4};
    std::vector<std::uint8_t> y{1, 1};
    std::vector<double> w{1.0, 3.0};
    const auto r = skill_mc(p, y, w, 1000, 1);
    REQUIRE_THAT(r.edge, WithinAbs(0.65, 1e-12));      // (0.8*1 + 0.6*3) / 4
    REQUIRE_THAT(r.n_eff, WithinAbs(1.6, 1e-12));      // 4^2 / 10
}

TEST_CASE("a wallet that lost every coin flip is not significant") {
    std::vector<double> p(50, 0.5), w(50, 1.0);
    std::vector<std::uint8_t> y(50, 0);
    REQUIRE(skill_mc(p, y, w, 5000, 3).p_value > 0.99);
}

TEST_CASE("an impossible record gets the minimum p-value") {
    std::vector<double> p(200, 0.3), w(200, 1.0);
    std::vector<std::uint8_t> y(200, 1);
    REQUIRE_THAT(skill_mc(p, y, w, 10000, 5).p_value, WithinAbs(1.0 / 10001.0, 1e-15));
}

TEST_CASE("null distribution is centred on zero") {
    std::mt19937_64 gen(9);
    std::uniform_real_distribution<double> up(0.1, 0.9), uw(1.0, 50.0);
    std::vector<double> p(80), w(80);
    std::vector<std::uint8_t> y(80, 1);
    for (int i = 0; i < 80; ++i) { p[i] = up(gen); w[i] = uw(gen); }
    const auto r = skill_mc(p, y, w, 20000, 13);
    REQUIRE(std::abs(r.null_mean) < 4.0 * r.null_sd / std::sqrt(20000.0));
}

TEST_CASE("p-values of no-skill wallets are calibrated") {
    constexpr int wallets = 2000, per = 50;
    std::mt19937_64 gen(7);
    std::uniform_real_distribution<double> up(0.1, 0.9), uw(1.0, 100.0), u(0.0, 1.0);
    std::vector<double> p, w;
    std::vector<std::uint8_t> y;
    std::vector<std::int64_t> offsets{0};
    for (int k = 0; k < wallets; ++k) {
        for (int i = 0; i < per; ++i) {
            const double pi = up(gen);
            p.push_back(pi);
            w.push_back(uw(gen));
            y.push_back(u(gen) < pi ? 1 : 0);
        }
        offsets.push_back(offsets.back() + per);
    }
    const auto res = skill_mc_batch(p, y, w, offsets, 2000, 11, 0);
    int below = 0;
    for (const auto& r : res) below += r.p_value < 0.1 ? 1 : 0;
    const double frac = below / static_cast<double>(wallets);
    REQUIRE(frac > 0.08);
    REQUIRE(frac < 0.12);
}

TEST_CASE("batch results do not depend on thread count") {
    std::mt19937_64 gen(21);
    std::uniform_real_distribution<double> up(0.1, 0.9), u(0.0, 1.0);
    std::vector<double> p, w;
    std::vector<std::uint8_t> y;
    std::vector<std::int64_t> offsets{0};
    for (int k = 0; k < 50; ++k) {
        for (int i = 0; i < 30; ++i) { const double pi = up(gen); p.push_back(pi); w.push_back(1.0 + i); y.push_back(u(gen) < pi); }
        offsets.push_back(offsets.back() + 30);
    }
    const auto a = skill_mc_batch(p, y, w, offsets, 3000, 99, 1);
    const auto b = skill_mc_batch(p, y, w, offsets, 3000, 99, 4);
    REQUIRE(a.size() == 50);
    for (std::size_t i = 0; i < a.size(); ++i) {
        REQUIRE(a[i].edge == b[i].edge);
        REQUIRE(a[i].p_value == b[i].p_value);
    }
}

TEST_CASE("empty wallet slot yields NaN edge and p = 1") {
    std::vector<double> p{0.5}, w{1.0};
    std::vector<std::uint8_t> y{1};
    std::vector<std::int64_t> offsets{0, 0, 1};
    const auto res = skill_mc_batch(p, y, w, offsets, 100, 1, 2);
    REQUIRE(std::isnan(res[0].edge));
    REQUIRE(res[0].p_value == 1.0);
    REQUIRE(res[1].edge == 0.5);
}

TEST_CASE("invalid inputs throw") {
    std::vector<double> p{0.5}, w{1.0};
    std::vector<std::uint8_t> y{1};
    std::vector<double> bad_p{1.0}, bad_w{0.0}, two{0.5, 0.5};
    std::vector<std::uint8_t> bad_y{2};
    REQUIRE_THROWS_AS(skill_mc(two, y, w, 10, 1), std::invalid_argument);
    REQUIRE_THROWS_AS(skill_mc(bad_p, y, w, 10, 1), std::invalid_argument);
    REQUIRE_THROWS_AS(skill_mc(p, bad_y, w, 10, 1), std::invalid_argument);
    REQUIRE_THROWS_AS(skill_mc(p, y, bad_w, 10, 1), std::invalid_argument);
    REQUIRE_THROWS_AS(skill_mc(p, y, w, 0, 1), std::invalid_argument);
    std::vector<double> none;
    std::vector<std::uint8_t> none_y;
    REQUIRE_THROWS_AS(skill_mc(none, none_y, none, 10, 1), std::invalid_argument);
    std::vector<std::int64_t> bad_offsets{0, 2};
    REQUIRE_THROWS_AS(skill_mc_batch(p, y, w, bad_offsets, 10, 1, 1), std::invalid_argument);
}
```

In `CMakeLists.txt`, add `cpp/tests/test_montecarlo.cpp` to `add_executable(whalecore_tests ...)`.

- [ ] **Step 2: Run to verify failure**

Run: `scripts/test-cpp.sh`
Expected: FAIL — `whalecore/montecarlo.hpp: No such file or directory`.

- [ ] **Step 3: Implement**

`cpp/include/whalecore/montecarlo.hpp`:
```cpp
#pragma once

#include <cstdint>
#include <span>
#include <vector>

namespace whalecore {

// Result of testing one betting record against the "no skill" null hypothesis,
// under which a position bought at price p wins with probability exactly p.
struct SkillResult {
    double edge;       // stake-weighted mean of (outcome - price)
    double p_value;    // (1 + #{simulated edge >= observed}) / (1 + n_sims)
    double null_mean;  // mean simulated edge (should be ~0)
    double null_sd;    // sd of simulated edge
    double n_eff;      // effective sample size (sum w)^2 / sum w^2
};

// prices in (0,1), outcomes 0/1, weights > 0, equal non-zero length; n_sims >= 1.
SkillResult skill_mc(std::span<const double> prices, std::span<const std::uint8_t> outcomes,
                     std::span<const double> weights, std::uint32_t n_sims, std::uint64_t seed);

// Many records in CSR layout: record i owns rows [offsets[i], offsets[i+1]).
// Each record i uses seed splitmix64(seed ^ i), so results are independent of n_threads.
// n_threads == 0 means hardware concurrency. Empty records yield {NaN, 1, 0, 0, 0}.
std::vector<SkillResult> skill_mc_batch(std::span<const double> prices,
                                        std::span<const std::uint8_t> outcomes,
                                        std::span<const double> weights,
                                        std::span<const std::int64_t> offsets, std::uint32_t n_sims,
                                        std::uint64_t seed, unsigned n_threads);

}  // namespace whalecore
```

`cpp/src/montecarlo.cpp`:
```cpp
#include "whalecore/montecarlo.hpp"

#include <algorithm>
#include <atomic>
#include <cmath>
#include <limits>
#include <random>
#include <stdexcept>
#include <thread>

namespace whalecore {
namespace {

std::uint64_t splitmix64(std::uint64_t x) {
    x += 0x9E3779B97F4A7C15ULL;
    x = (x ^ (x >> 30)) * 0xBF58476D1CE4E5B9ULL;
    x = (x ^ (x >> 27)) * 0x94D049BB133111EBULL;
    return x ^ (x >> 31);
}

// Top 53 bits of a 64-bit draw -> uniform double in [0, 1).
inline double to_unit(std::uint64_t r) { return static_cast<double>(r >> 11) * 0x1.0p-53; }

void validate(std::span<const double> p, std::span<const std::uint8_t> y, std::span<const double> w,
              std::uint32_t n_sims) {
    if (p.size() != y.size() || p.size() != w.size())
        throw std::invalid_argument("prices, outcomes and weights must have equal length");
    if (n_sims == 0) throw std::invalid_argument("n_sims must be >= 1");
    for (std::size_t i = 0; i < p.size(); ++i) {
        if (!(p[i] > 0.0 && p[i] < 1.0)) throw std::invalid_argument("prices must be in (0, 1)");
        if (y[i] > 1) throw std::invalid_argument("outcomes must be 0 or 1");
        if (!(w[i] > 0.0) || !std::isfinite(w[i]))
            throw std::invalid_argument("weights must be positive and finite");
    }
}

// Assumes validated, non-empty input.
SkillResult run(std::span<const double> p, std::span<const std::uint8_t> y,
                std::span<const double> w, std::uint32_t n_sims, std::uint64_t seed) {
    const std::size_t n = p.size();
    double sw = 0.0, sw2 = 0.0;
    for (const double wi : w) { sw += wi; sw2 += wi * wi; }

    std::vector<double> wn(n);
    double observed = 0.0;
    for (std::size_t i = 0; i < n; ++i) {
        wn[i] = w[i] / sw;
        observed += wn[i] * (static_cast<double>(y[i]) - p[i]);
    }

    std::mt19937_64 rng(seed);
    constexpr double tol = 1e-12;  // ties count as "at least as good"
    std::uint64_t at_least = 0;
    double sum = 0.0, sumsq = 0.0;
    for (std::uint32_t k = 0; k < n_sims; ++k) {
        double s = 0.0;
        for (std::size_t i = 0; i < n; ++i) {
            const double yi = to_unit(rng()) < p[i] ? 1.0 : 0.0;
            s += wn[i] * (yi - p[i]);
        }
        at_least += s >= observed - tol ? 1 : 0;
        sum += s;
        sumsq += s * s;
    }
    const double mean = sum / n_sims;
    const double var = std::max(0.0, sumsq / n_sims - mean * mean);
    return {observed, (1.0 + static_cast<double>(at_least)) / (1.0 + n_sims), mean, std::sqrt(var),
            sw * sw / sw2};
}

}  // namespace

SkillResult skill_mc(std::span<const double> prices, std::span<const std::uint8_t> outcomes,
                     std::span<const double> weights, std::uint32_t n_sims, std::uint64_t seed) {
    validate(prices, outcomes, weights, n_sims);
    if (prices.empty()) throw std::invalid_argument("need at least one position");
    return run(prices, outcomes, weights, n_sims, seed);
}

std::vector<SkillResult> skill_mc_batch(std::span<const double> prices,
                                        std::span<const std::uint8_t> outcomes,
                                        std::span<const double> weights,
                                        std::span<const std::int64_t> offsets, std::uint32_t n_sims,
                                        std::uint64_t seed, unsigned n_threads) {
    validate(prices, outcomes, weights, n_sims);
    if (offsets.empty() || offsets.front() != 0 ||
        offsets.back() != static_cast<std::int64_t>(prices.size()))
        throw std::invalid_argument("offsets must start at 0 and end at len(prices)");
    for (std::size_t i = 1; i < offsets.size(); ++i)
        if (offsets[i] < offsets[i - 1]) throw std::invalid_argument("offsets must be non-decreasing");

    const std::size_t m = offsets.size() - 1;
    std::vector<SkillResult> out(m);
    unsigned threads = n_threads ? n_threads : std::max(1u, std::thread::hardware_concurrency());
    threads = static_cast<unsigned>(std::min<std::size_t>(threads, std::max<std::size_t>(m, 1)));

    std::atomic<std::size_t> next{0};
    auto worker = [&] {
        for (std::size_t i = next.fetch_add(1); i < m; i = next.fetch_add(1)) {
            const auto lo = static_cast<std::size_t>(offsets[i]);
            const auto hi = static_cast<std::size_t>(offsets[i + 1]);
            if (lo == hi) {
                out[i] = {std::numeric_limits<double>::quiet_NaN(), 1.0, 0.0, 0.0, 0.0};
                continue;
            }
            out[i] = run(prices.subspan(lo, hi - lo), outcomes.subspan(lo, hi - lo),
                         weights.subspan(lo, hi - lo), n_sims, splitmix64(seed ^ i));
        }
    };
    std::vector<std::thread> pool;
    for (unsigned k = 1; k < threads; ++k) pool.emplace_back(worker);
    worker();
    for (auto& t : pool) t.join();
    return out;
}

}  // namespace whalecore
```

In `CMakeLists.txt`, change the library sources to:
```cmake
add_library(whalecore_lib STATIC
  cpp/src/version.cpp
  cpp/src/montecarlo.cpp
)
```

- [ ] **Step 4: Run C++ tests**

Run: `scripts/test-cpp.sh`
Expected: all test cases pass (the calibration case takes ~1 s).

- [ ] **Step 5: Write the failing Python binding test**

`tests/test_whalecore_mc.py`:
```python
import numpy as np
import pytest

import whalecore


def test_skill_mc_from_numpy():
    r = whalecore.skill_mc(np.array([0.2, 0.4]), np.array([1, 1], dtype=np.uint8), np.array([1.0, 3.0]), n_sims=500, seed=1)
    assert r.edge == pytest.approx(0.65)
    assert 0 < r.p_value <= 1
    assert r.n_eff == pytest.approx(1.6)


def test_batch_returns_one_result_per_record():
    p = np.full(10, 0.5)
    y = np.array([1, 0] * 5, dtype=np.uint8)
    w = np.ones(10)
    offsets = np.array([0, 4, 10], dtype=np.int64)
    res = whalecore.skill_mc_batch(p, y, w, offsets, n_sims=200, seed=3, n_threads=2)
    assert len(res) == 2
    assert res[0].edge == pytest.approx(0.0)


def test_batch_with_no_records():
    empty = np.array([], dtype=np.float64)
    res = whalecore.skill_mc_batch(empty, np.array([], dtype=np.uint8), empty, np.array([0], dtype=np.int64), n_sims=10)
    assert res == []


def test_invalid_input_raises_value_error():
    with pytest.raises(ValueError, match="prices must be in"):
        whalecore.skill_mc(np.array([1.5]), np.array([1], dtype=np.uint8), np.array([1.0]), n_sims=10)
```

- [ ] **Step 6: Add the bindings**

Replace `cpp/src/bindings.cpp` with:
```cpp
#include <nanobind/nanobind.h>
#include <nanobind/ndarray.h>
#include <nanobind/stl/string.h>
#include <nanobind/stl/vector.h>

#include <cstdint>
#include <span>
#include <string>

#include "whalecore/montecarlo.hpp"
#include "whalecore/version.hpp"

namespace nb = nanobind;
using namespace nb::literals;

template <class T>
using Vec = nb::ndarray<const T, nb::ndim<1>, nb::c_contig, nb::device::cpu>;

template <class T>
std::span<const T> as_span(const Vec<T>& a) {
    return {a.data(), a.shape(0)};
}

NB_MODULE(whalecore, m) {
    m.doc() = "WHALESCAN C++ core: Monte Carlo skill engine and order book";
    m.def("version", &whalecore::version, "Library version string.");

    nb::class_<whalecore::SkillResult>(m, "SkillResult")
        .def_ro("edge", &whalecore::SkillResult::edge)
        .def_ro("p_value", &whalecore::SkillResult::p_value)
        .def_ro("null_mean", &whalecore::SkillResult::null_mean)
        .def_ro("null_sd", &whalecore::SkillResult::null_sd)
        .def_ro("n_eff", &whalecore::SkillResult::n_eff)
        .def("__repr__", [](const whalecore::SkillResult& r) {
            return "SkillResult(edge=" + std::to_string(r.edge) + ", p_value=" + std::to_string(r.p_value) +
                   ", n_eff=" + std::to_string(r.n_eff) + ")";
        });

    m.def(
        "skill_mc",
        [](Vec<double> prices, Vec<std::uint8_t> outcomes, Vec<double> weights, std::uint32_t n_sims,
           std::uint64_t seed) {
            const auto p = as_span(prices);
            const auto y = as_span(outcomes);
            const auto w = as_span(weights);
            nb::gil_scoped_release release;  // pure C++ from here; let other Python threads run
            return whalecore::skill_mc(p, y, w, n_sims, seed);
        },
        "prices"_a, "outcomes"_a, "weights"_a, "n_sims"_a = 100000, "seed"_a = 42,
        "Monte Carlo test of one betting record against the no-skill null.");

    m.def(
        "skill_mc_batch",
        [](Vec<double> prices, Vec<std::uint8_t> outcomes, Vec<double> weights, Vec<std::int64_t> offsets,
           std::uint32_t n_sims, std::uint64_t seed, unsigned n_threads) {
            const auto p = as_span(prices);
            const auto y = as_span(outcomes);
            const auto w = as_span(weights);
            const auto o = as_span(offsets);
            nb::gil_scoped_release release;
            return whalecore::skill_mc_batch(p, y, w, o, n_sims, seed, n_threads);
        },
        "prices"_a, "outcomes"_a, "weights"_a, "offsets"_a, "n_sims"_a = 100000, "seed"_a = 42,
        "n_threads"_a = 0, "Multithreaded skill_mc over CSR-packed records.");
}
```

- [ ] **Step 7: Rebuild and run all tests**

Run: `uv sync && uv run pytest -q && scripts/test-cpp.sh`
Expected: all pass. (If `uv sync` did not rebuild, run `uv sync --reinstall-package whalescan`.)

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "feat(whalecore): multithreaded Monte Carlo skill significance engine"
```

---

### Task 4: C++ order book engine

**Files:**
- Create: `cpp/include/whalecore/orderbook.hpp`, `cpp/src/orderbook.cpp`, `cpp/tests/test_orderbook.cpp`
- Modify: `CMakeLists.txt`, `cpp/src/bindings.cpp`
- Test: `tests/test_whalecore_book.py`

**Interfaces:**
- Produces (Python): `whalecore.Side.BID | ASK`; `whalecore.OrderBook()` with `apply_snapshot(bids: f64[n,2], asks: f64[n,2])`, `apply_delta(side, price, size)`, `clear()`, `best_bid() / best_ask() / mid() / spread() / microprice() -> float | None`, `depth(side, ticks_from_best: int) -> float`, `walk(side, notional_usdc) -> WalkResult{vwap, filled_usdc, filled_shares, levels_consumed, worst_price, complete}`, `imbalance(levels: int) -> float`, `crossed() -> bool`, `level_count(side) -> int`. `walk(Side.ASK, x)` = buying x USDC; `walk(Side.BID, x)` = selling x USDC. Invalid input raises `ValueError`.

- [ ] **Step 1: Write the failing C++ tests**

`cpp/tests/test_orderbook.cpp`:
```cpp
#include <catch2/catch_test_macros.hpp>
#include <catch2/matchers/catch_matchers_floating_point.hpp>

#include <cmath>
#include <stdexcept>
#include <vector>

#include "whalecore/orderbook.hpp"

using Catch::Matchers::WithinAbs;
using whalecore::OrderBook;
using whalecore::Side;

static OrderBook make_book() {
    OrderBook b;
    // Polymarket sends bids ascending; order must not matter.
    std::vector<double> bids{0.39, 10, 0.399, 50, 0.40, 100};
    std::vector<double> asks{0.50, 100, 0.52, 200, 0.55, 1000};
    b.apply_snapshot(bids, asks);
    return b;
}

TEST_CASE("snapshot sets best levels") {
    const auto b = make_book();
    REQUIRE_THAT(*b.best_bid(), WithinAbs(0.40, 1e-12));
    REQUIRE_THAT(*b.best_ask(), WithinAbs(0.50, 1e-12));
    REQUIRE_THAT(*b.mid(), WithinAbs(0.45, 1e-12));
    REQUIRE_THAT(*b.spread(), WithinAbs(0.10, 1e-12));
    REQUIRE(b.level_count(Side::Bid) == 3);
}

TEST_CASE("deltas update and erase levels") {
    auto b = make_book();
    b.apply_delta(Side::Bid, 0.40, 0.0);
    REQUIRE_THAT(*b.best_bid(), WithinAbs(0.399, 1e-12));
    b.apply_delta(Side::Ask, 0.49, 25.0);
    REQUIRE_THAT(*b.best_ask(), WithinAbs(0.49, 1e-12));
    b.apply_delta(Side::Ask, 0.49, 5.0);
    REQUIRE(b.level_count(Side::Ask) == 4);
}

TEST_CASE("snapshot replaces previous state") {
    auto b = make_book();
    std::vector<double> bids{0.10, 1}, asks{0.90, 1};
    b.apply_snapshot(bids, asks);
    REQUIRE(b.level_count(Side::Bid) == 1);
    REQUIRE_THAT(*b.best_ask(), WithinAbs(0.90, 1e-12));
}

TEST_CASE("microprice leans toward the thinner side") {
    OrderBook b;
    std::vector<double> bids{0.40, 100}, asks{0.42, 300};
    b.apply_snapshot(bids, asks);
    REQUIRE_THAT(*b.microprice(), WithinAbs(0.405, 1e-12));  // (0.40*300 + 0.42*100) / 400
}

TEST_CASE("walking the asks across levels") {
    const auto b = make_book();
    const auto w = b.walk(Side::Ask, 100.0);  // $50 at 0.50, then $50 at 0.52
    const double shares = 100.0 + 50.0 / 0.52;
    REQUIRE(w.complete);
    REQUIRE(w.levels_consumed == 2);
    REQUIRE_THAT(w.filled_usdc, WithinAbs(100.0, 1e-9));
    REQUIRE_THAT(w.filled_shares, WithinAbs(shares, 1e-9));
    REQUIRE_THAT(w.vwap, WithinAbs(100.0 / shares, 1e-12));
    REQUIRE_THAT(w.worst_price, WithinAbs(0.52, 1e-12));
}

TEST_CASE("walking the bids sells into the highest bids first") {
    const auto b = make_book();
    const auto w = b.walk(Side::Bid, 20.0);
    REQUIRE(w.levels_consumed == 1);
    REQUIRE_THAT(w.vwap, WithinAbs(0.40, 1e-12));
}

TEST_CASE("walk beyond available liquidity is incomplete") {
    OrderBook b;
    std::vector<double> bids{}, asks{0.5, 10};
    b.apply_snapshot(bids, asks);
    const auto w = b.walk(Side::Ask, 100.0);
    REQUIRE_FALSE(w.complete);
    REQUIRE_THAT(w.filled_usdc, WithinAbs(5.0, 1e-12));
}

TEST_CASE("walk on empty book") {
    OrderBook b;
    const auto w = b.walk(Side::Ask, 100.0);
    REQUIRE_FALSE(w.complete);
    REQUIRE(std::isnan(w.vwap));
    REQUIRE(w.levels_consumed == 0);
    REQUIRE_FALSE(b.best_ask().has_value());
    REQUIRE_FALSE(b.microprice().has_value());
}

TEST_CASE("depth sums USDC within N ticks of best") {
    const auto b = make_book();
    // tick = 0.0001; 0.40 and 0.399 are within 10 ticks, 0.39 is not
    REQUIRE_THAT(b.depth(Side::Bid, 10), WithinAbs(0.40 * 100 + 0.399 * 50, 1e-9));
}

TEST_CASE("imbalance over top levels") {
    OrderBook b;
    std::vector<double> bids{0.40, 300}, asks{0.42, 100};
    b.apply_snapshot(bids, asks);
    REQUIRE_THAT(b.imbalance(5), WithinAbs(0.5, 1e-12));
}

TEST_CASE("crossed book is detected") {
    auto b = make_book();
    REQUIRE_FALSE(b.crossed());
    b.apply_delta(Side::Bid, 0.60, 1.0);
    REQUIRE(b.crossed());
}

TEST_CASE("invalid input throws") {
    OrderBook b;
    std::vector<double> odd{0.5}, ok{};
    REQUIRE_THROWS_AS(b.apply_snapshot(odd, ok), std::invalid_argument);
    REQUIRE_THROWS_AS(b.apply_delta(Side::Bid, 1.5, 1.0), std::invalid_argument);
    REQUIRE_THROWS_AS(b.apply_delta(Side::Bid, 0.5, -1.0), std::invalid_argument);
    REQUIRE_THROWS_AS(b.walk(Side::Ask, 0.0), std::invalid_argument);
}
```

Add `cpp/tests/test_orderbook.cpp` to the test executable in `CMakeLists.txt`.

- [ ] **Step 2: Run to verify failure**

Run: `scripts/test-cpp.sh` — Expected: FAIL, `whalecore/orderbook.hpp` not found.

- [ ] **Step 3: Implement**

`cpp/include/whalecore/orderbook.hpp`:
```cpp
#pragma once

#include <cstddef>
#include <cstdint>
#include <functional>
#include <map>
#include <optional>
#include <span>

namespace whalecore {

enum class Side : std::uint8_t { Bid = 0, Ask = 1 };

struct WalkResult {
    double vwap;           // NaN when nothing filled
    double filled_usdc;
    double filled_shares;
    int levels_consumed;
    double worst_price;    // NaN when nothing filled
    bool complete;         // the full notional was filled
};

// L2 order book for one outcome token. Prices are stored as integer ticks of 0.0001
// (Polymarket tick sizes 0.01 / 0.001 are exact multiples), avoiding float map keys.
class OrderBook {
public:
    static constexpr double kTicksPerUnit = 10000.0;

    // Flat interleaved levels [price0, size0, price1, size1, ...]; replaces the whole book.
    void apply_snapshot(std::span<const double> bids, std::span<const double> asks);
    // size == 0 removes the level.
    void apply_delta(Side side, double price, double size);
    void clear();

    std::optional<double> best_bid() const;
    std::optional<double> best_ask() const;
    std::optional<double> mid() const;
    std::optional<double> spread() const;
    std::optional<double> microprice() const;
    // USDC resting within `ticks_from_best` ticks of the best price on `side`.
    double depth(Side side, int ticks_from_best) const;
    // Side::Ask consumes asks (a buy); Side::Bid consumes bids (a sell).
    WalkResult walk(Side side, double notional_usdc) const;
    // (bid size - ask size) / total over the top `levels` levels; 0 when empty.
    double imbalance(int levels) const;
    bool crossed() const;
    std::size_t level_count(Side side) const;

    static std::int32_t to_ticks(double price);
    static double to_price(std::int32_t ticks);

private:
    std::map<std::int32_t, double, std::greater<>> bids_;
    std::map<std::int32_t, double> asks_;
};

}  // namespace whalecore
```

`cpp/src/orderbook.cpp`:
```cpp
#include "whalecore/orderbook.hpp"

#include <cmath>
#include <limits>
#include <stdexcept>

namespace whalecore {
namespace {

void check_size(double size) {
    if (!(size >= 0.0) || !std::isfinite(size)) throw std::invalid_argument("size must be finite and >= 0");
}

template <class Map>
void load(Map& book, std::span<const double> flat) {
    if (flat.size() % 2 != 0) throw std::invalid_argument("levels must be (price, size) pairs");
    book.clear();
    for (std::size_t i = 0; i < flat.size(); i += 2) {
        check_size(flat[i + 1]);
        if (flat[i + 1] > 0.0) book[OrderBook::to_ticks(flat[i])] = flat[i + 1];
    }
}

template <class Map>
void walk_levels(const Map& book, double notional, WalkResult& r) {
    double remaining = notional;
    for (const auto& [ticks, size] : book) {
        if (remaining <= 0.0) break;
        const double px = OrderBook::to_price(ticks);
        if (px <= 0.0) continue;
        const double cost = px * size;
        ++r.levels_consumed;
        r.worst_price = px;
        if (cost >= remaining) {
            r.filled_shares += remaining / px;
            r.filled_usdc += remaining;
            remaining = 0.0;
        } else {
            r.filled_shares += size;
            r.filled_usdc += cost;
            remaining -= cost;
        }
    }
    r.complete = remaining <= 1e-9 * notional;
}

template <class Map>
double top_size(const Map& book, int levels) {
    double s = 0.0;
    int k = 0;
    for (auto it = book.begin(); it != book.end() && k < levels; ++it, ++k) s += it->second;
    return s;
}

}  // namespace

std::int32_t OrderBook::to_ticks(double price) {
    if (!(price >= 0.0 && price <= 1.0)) throw std::invalid_argument("price must be within [0, 1]");
    return static_cast<std::int32_t>(std::llround(price * kTicksPerUnit));
}

double OrderBook::to_price(std::int32_t ticks) { return ticks / kTicksPerUnit; }

void OrderBook::apply_snapshot(std::span<const double> bids, std::span<const double> asks) {
    load(bids_, bids);
    load(asks_, asks);
}

void OrderBook::apply_delta(Side side, double price, double size) {
    check_size(size);
    const auto t = to_ticks(price);
    if (side == Side::Bid) {
        if (size == 0.0) bids_.erase(t); else bids_[t] = size;
    } else {
        if (size == 0.0) asks_.erase(t); else asks_[t] = size;
    }
}

void OrderBook::clear() {
    bids_.clear();
    asks_.clear();
}

std::optional<double> OrderBook::best_bid() const {
    if (bids_.empty()) return std::nullopt;
    return to_price(bids_.begin()->first);
}

std::optional<double> OrderBook::best_ask() const {
    if (asks_.empty()) return std::nullopt;
    return to_price(asks_.begin()->first);
}

std::optional<double> OrderBook::mid() const {
    const auto b = best_bid(), a = best_ask();
    if (!b || !a) return std::nullopt;
    return (*b + *a) / 2.0;
}

std::optional<double> OrderBook::spread() const {
    const auto b = best_bid(), a = best_ask();
    if (!b || !a) return std::nullopt;
    return *a - *b;
}

std::optional<double> OrderBook::microprice() const {
    const auto b = best_bid(), a = best_ask();
    if (!b || !a) return std::nullopt;
    const double bs = bids_.begin()->second, as = asks_.begin()->second;
    return (*b * as + *a * bs) / (bs + as);
}

double OrderBook::depth(Side side, int ticks_from_best) const {
    double usdc = 0.0;
    auto sum = [&](const auto& book) {
        if (book.empty()) return;
        const auto best = book.begin()->first;
        for (const auto& [t, sz] : book) {
            if (std::abs(t - best) > ticks_from_best) break;
            usdc += to_price(t) * sz;
        }
    };
    if (side == Side::Bid) sum(bids_); else sum(asks_);
    return usdc;
}

WalkResult OrderBook::walk(Side side, double notional_usdc) const {
    if (!(notional_usdc > 0.0) || !std::isfinite(notional_usdc))
        throw std::invalid_argument("notional_usdc must be positive");
    constexpr double nan = std::numeric_limits<double>::quiet_NaN();
    WalkResult r{nan, 0.0, 0.0, 0, nan, false};
    if (side == Side::Ask) walk_levels(asks_, notional_usdc, r); else walk_levels(bids_, notional_usdc, r);
    if (r.filled_shares > 0.0) r.vwap = r.filled_usdc / r.filled_shares;
    return r;
}

double OrderBook::imbalance(int levels) const {
    const double b = top_size(bids_, levels), a = top_size(asks_, levels);
    return (a + b) > 0.0 ? (b - a) / (b + a) : 0.0;
}

bool OrderBook::crossed() const {
    return !bids_.empty() && !asks_.empty() && bids_.begin()->first >= asks_.begin()->first;
}

std::size_t OrderBook::level_count(Side side) const {
    return side == Side::Bid ? bids_.size() : asks_.size();
}

}  // namespace whalecore
```

Add `cpp/src/orderbook.cpp` to `whalecore_lib` sources in `CMakeLists.txt`.

- [ ] **Step 4: Run C++ tests**

Run: `scripts/test-cpp.sh` — Expected: all pass.

- [ ] **Step 5: Write the failing Python test**

`tests/test_whalecore_book.py`:
```python
import math

import numpy as np
import pytest

import whalecore


def levels(*pairs):
    return np.array(pairs, dtype=np.float64).reshape(-1, 2)


def test_book_from_numpy_and_walk():
    b = whalecore.OrderBook()
    b.apply_snapshot(levels((0.40, 100.0)), levels((0.50, 100.0), (0.52, 200.0)))
    w = b.walk(whalecore.Side.ASK, 100.0)
    assert w.complete
    assert w.vwap == pytest.approx(100.0 / (100.0 + 50.0 / 0.52))
    assert b.best_bid() == pytest.approx(0.40)


def test_empty_book_returns_none_and_nan():
    b = whalecore.OrderBook()
    b.apply_snapshot(levels(), levels())
    assert b.best_ask() is None
    w = b.walk(whalecore.Side.ASK, 50.0)
    assert not w.complete and math.isnan(w.vwap)


def test_bad_price_raises_value_error():
    b = whalecore.OrderBook()
    with pytest.raises(ValueError):
        b.apply_delta(whalecore.Side.BID, 2.0, 1.0)
```

- [ ] **Step 6: Add the bindings**

In `cpp/src/bindings.cpp` add includes:
```cpp
#include <nanobind/stl/optional.h>

#include "whalecore/orderbook.hpp"
```
add after the `Vec` alias:
```cpp
using Levels = nb::ndarray<const double, nb::shape<-1, 2>, nb::c_contig, nb::device::cpu>;

static std::span<const double> flat(const Levels& a) { return {a.data(), a.shape(0) * 2}; }
```
and append inside `NB_MODULE` (after `skill_mc_batch`):
```cpp
    using whalecore::OrderBook;
    using whalecore::Side;
    using whalecore::WalkResult;

    nb::enum_<Side>(m, "Side").value("BID", Side::Bid).value("ASK", Side::Ask);

    nb::class_<WalkResult>(m, "WalkResult")
        .def_ro("vwap", &WalkResult::vwap)
        .def_ro("filled_usdc", &WalkResult::filled_usdc)
        .def_ro("filled_shares", &WalkResult::filled_shares)
        .def_ro("levels_consumed", &WalkResult::levels_consumed)
        .def_ro("worst_price", &WalkResult::worst_price)
        .def_ro("complete", &WalkResult::complete);

    nb::class_<OrderBook>(m, "OrderBook")
        .def(nb::init<>())
        .def("apply_snapshot",
             [](OrderBook& b, Levels bids, Levels asks) { b.apply_snapshot(flat(bids), flat(asks)); },
             "bids"_a, "asks"_a, "Replace the book with (price, size) rows.")
        .def("apply_delta", &OrderBook::apply_delta, "side"_a, "price"_a, "size"_a)
        .def("clear", &OrderBook::clear)
        .def("best_bid", &OrderBook::best_bid)
        .def("best_ask", &OrderBook::best_ask)
        .def("mid", &OrderBook::mid)
        .def("spread", &OrderBook::spread)
        .def("microprice", &OrderBook::microprice)
        .def("depth", &OrderBook::depth, "side"_a, "ticks_from_best"_a)
        .def("walk", &OrderBook::walk, "side"_a, "notional_usdc"_a)
        .def("imbalance", &OrderBook::imbalance, "levels"_a = 5)
        .def("crossed", &OrderBook::crossed)
        .def("level_count", &OrderBook::level_count, "side"_a);
```

- [ ] **Step 7: Rebuild and run everything**

Run: `uv sync && uv run pytest -q && scripts/test-cpp.sh` — Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "feat(whalecore): L2 order book with microprice, depth and book walking"
```

---
### Task 5: Domain models and boundary parsers

**Files:**
- Create: `src/whalescan/models.py`, `src/whalescan/parsers.py`
- Test: `tests/test_parsers.py`

**Interfaces:**
- Consumes: `tests/fixtures/real/*.json` (Task 2).
- Produces: frozen dataclasses `Trade, ClosedPosition, Market, LeaderboardEntry, BookSnapshot, PricePoint` (fields below); `resolved_winner(closed: bool, outcome_prices: Sequence[float]) -> int | None`; `Market.winner_index() -> int | None`; `Market.fee_per_share(price: float) -> float`; `Trade.usdc -> float`; parsers `parse_trade, parse_closed_position, parse_market, parse_leaderboard, parse_book, parse_price_history`; `parse_many(rows, fn) -> tuple[list, int]` (items, n_skipped); `ParseError(ValueError)` with `.payload`; `iso_to_ts(s: str | None) -> int | None`.

- [ ] **Step 1: Write the failing tests**

`tests/test_parsers.py`:
```python
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from whalescan.models import Market, resolved_winner
from whalescan.parsers import (
    ParseError,
    iso_to_ts,
    parse_book,
    parse_closed_position,
    parse_leaderboard,
    parse_many,
    parse_market,
    parse_price_history,
    parse_trade,
)

REAL = Path(__file__).parent / "fixtures" / "real"

TRADE = {
    "proxyWallet": "0x23C8A4C266D10BA5846837EAC391FEA89ED6F293", "side": "BUY",
    "asset": "267958", "conditionId": "0x3B49", "size": 23340.21, "price": 0.5514416194,
    "timestamp": 1790249874, "title": "Dota 2: NAVI vs LGD", "slug": "dota2-navi-lgd",
    "eventSlug": "dota2-navi-lgd-2026-09-24", "outcome": "Natus Vincere", "outcomeIndex": 0,
    "transactionHash": "0x4435",
}

MARKET = {
    "conditionId": "0x8B7F", "question": "Spread: KC (-5.5)", "slug": "nfl-ind-kc-spread",
    "endDate": "2026-09-21T00:00:00Z", "closed": True, "closedTime": "2026-09-21 04:30:03+00",
    "outcomePrices": "[\"0\", \"1\"]", "clobTokenIds": "[\"111\", \"222\"]", "feesEnabled": True,
    "feeSchedule": {"exponent": 1, "rate": 0.05, "takerOnly": True, "rebateRate": 0.15},
    "volumeNum": 1685012.1, "events": [{"slug": "nfl-ind-kc-2026-09-21"}],
    "tags": [{"label": "Sports"}, {"label": "NFL"}],
}


def test_trade_normalises_wallet_and_condition():
    t = parse_trade(TRADE)
    assert t.wallet == "0x23c8a4c266d10ba5846837eac391fea89ed6f293"
    assert t.condition_id == "0x3b49"
    assert t.usdc == pytest.approx(23340.21 * 0.5514416194)
    assert t.fee is None


def test_trade_missing_field_raises_with_payload():
    bad = dict(TRADE)
    del bad["price"]
    with pytest.raises(ParseError) as exc:
        parse_trade(bad)
    assert exc.value.payload is bad


def test_trade_rejects_unknown_side():
    with pytest.raises(ParseError):
        parse_trade(dict(TRADE, side="HOLD"))


def test_market_fields_and_resolution():
    m = parse_market(MARKET)
    assert m.condition_id == "0x8b7f"
    assert m.event_slug == "nfl-ind-kc-2026-09-21"
    assert m.token_ids == ("111", "222")
    assert m.tags == ("Sports", "NFL")
    assert m.winner_index() == 1
    assert m.closed_ts == int(datetime(2026, 9, 21, 4, 30, 3, tzinfo=UTC).timestamp())
    assert m.end_ts == int(datetime(2026, 9, 21, tzinfo=UTC).timestamp())


def test_fee_matches_live_fill():
    m = parse_market(MARKET)
    # verified live fill: BUY 2048 shares @ 0.63, rate 0.05, exponent 1 -> fee 23.86944 USDC
    assert 2048 * m.fee_per_share(0.63) == pytest.approx(23.86944, abs=1e-5)
    assert parse_market(dict(MARKET, feesEnabled=False)).fee_per_share(0.63) == 0.0


@pytest.mark.parametrize(
    ("closed", "prices", "expected"),
    [
        (True, [0.0, 1.0], 1),
        (True, [1.0, 0.0], 0),
        (True, [0.5, 0.5], None),        # fractional resolution is discarded
        (False, [0.0005, 0.9995], None),  # still trading
        (True, [], None),
    ],
)
def test_resolved_winner(closed, prices, expected):
    assert resolved_winner(closed, prices) == expected


def test_iso_formats():
    assert iso_to_ts(None) is None
    assert iso_to_ts("") is None
    assert iso_to_ts("2026-09-21") == int(datetime(2026, 9, 21, tzinfo=UTC).timestamp())


def test_closed_position_book_leaderboard_history():
    cp = parse_closed_position({
        "proxyWallet": "0xAB", "asset": "1", "conditionId": "0xC", "avgPrice": 0.49, "totalBought": 100.0,
        "realizedPnl": 51.0, "curPrice": 1, "outcome": "IND", "outcomeIndex": 1, "title": "t",
        "eventSlug": "e", "timestamp": 1789965015,
    })
    assert cp.wallet == "0xab" and cp.outcome_index == 1
    book = parse_book({"asset_id": "9", "timestamp": "1790250466788", "bids": [{"price": "0.01", "size": "155.15"}], "asks": []})
    assert book.ts_ms == 1790250466788 and book.bids == ((0.01, 155.15),)
    lb = parse_leaderboard({"rank": "1", "proxyWallet": "0xAA", "userName": "x", "vol": 10.0, "pnl": 2.0})
    assert lb.rank == 1 and lb.wallet == "0xaa"
    hist = parse_price_history({"history": [{"t": 1, "p": 0.5}]})
    assert hist[0].price == 0.5


def test_parse_many_counts_skips():
    items, skipped = parse_many([TRADE, {"junk": 1}], parse_trade)
    assert len(items) == 1 and skipped == 1


@pytest.mark.parametrize(
    ("name", "fn"),
    [
        ("trades_large", parse_trade),
        ("closed_positions", parse_closed_position),
        ("leaderboard", parse_leaderboard),
        ("markets_closed", parse_market),
        ("markets_open", parse_market),
    ],
)
def test_every_real_row_parses(name, fn):
    rows = json.loads((REAL / f"{name}.json").read_text())
    assert rows, f"{name}.json is empty"
    for row in rows:
        fn(row)


def test_real_book_and_history_parse():
    assert parse_book(json.loads((REAL / "book.json").read_text())).asset
    assert parse_price_history(json.loads((REAL / "prices_history.json").read_text()))


def test_real_closed_markets_resolve_to_a_winner():
    rows = json.loads((REAL / "markets_closed.json").read_text())
    winners = [parse_market(r).winner_index() for r in rows]
    assert any(w is not None for w in winners)


def test_market_dataclass_is_hashable():
    assert hash(parse_market(MARKET)) == hash(parse_market(MARKET))
    assert isinstance(parse_market(MARKET), Market)
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_parsers.py -q` — Expected: FAIL, `ModuleNotFoundError: whalescan.models`.

- [ ] **Step 3: Implement**

`src/whalescan/models.py`:
```python
"""Domain objects. Everything downstream of the API boundary uses these, never raw JSON."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


def resolved_winner(closed: bool, outcome_prices: Sequence[float]) -> int | None:
    """Index of the winning outcome if the market resolved cleanly (exactly one 1, rest 0), else None.

    Fractional resolutions (50/50 splits, voids) return None: the no-skill null y* ~ Bernoulli(p)
    cannot produce fractional outcomes, so such markets are excluded from scoring.
    """
    if not closed or len(outcome_prices) < 2:
        return None
    ones = [i for i, p in enumerate(outcome_prices) if p == 1.0]
    zeros = sum(1 for p in outcome_prices if p == 0.0)
    if len(ones) == 1 and zeros == len(outcome_prices) - 1:
        return ones[0]
    return None


@dataclass(frozen=True, slots=True)
class Trade:
    tx_hash: str
    ts: int
    wallet: str
    asset: str
    condition_id: str
    side: str  # "BUY" | "SELL"
    price: float
    size: float  # shares
    event_slug: str
    title: str
    outcome: str
    outcome_index: int
    fee: float | None

    @property
    def usdc(self) -> float:
        return self.price * self.size


@dataclass(frozen=True, slots=True)
class ClosedPosition:
    wallet: str
    asset: str
    condition_id: str
    avg_price: float
    total_bought: float  # shares
    realized_pnl: float
    cur_price: float
    outcome: str
    outcome_index: int
    title: str
    event_slug: str
    ts: int


@dataclass(frozen=True, slots=True)
class Market:
    condition_id: str
    question: str
    slug: str
    event_slug: str
    end_ts: int | None
    closed: bool
    closed_ts: int | None
    outcome_prices: tuple[float, ...]
    token_ids: tuple[str, ...]
    fees_enabled: bool
    fee_rate: float
    fee_exponent: float
    volume: float
    tags: tuple[str, ...]

    def winner_index(self) -> int | None:
        return resolved_winner(self.closed, self.outcome_prices)

    def fee_per_share(self, price: float) -> float:
        """Taker fee in USDC per share at `price`: rate × (p(1−p))^exponent (verified on live fills)."""
        if not self.fees_enabled or self.fee_rate <= 0:
            return 0.0
        return self.fee_rate * (price * (1.0 - price)) ** self.fee_exponent


@dataclass(frozen=True, slots=True)
class LeaderboardEntry:
    wallet: str
    rank: int
    volume: float
    pnl: float
    name: str


@dataclass(frozen=True, slots=True)
class BookSnapshot:
    asset: str
    ts_ms: int
    bids: tuple[tuple[float, float], ...]
    asks: tuple[tuple[float, float], ...]


@dataclass(frozen=True, slots=True)
class PricePoint:
    ts: int
    price: float
```

`src/whalescan/parsers.py`:
```python
"""The only place raw API JSON is touched. Anything malformed becomes a ParseError carrying the payload."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Any, TypeVar

from whalescan.models import BookSnapshot, ClosedPosition, LeaderboardEntry, Market, PricePoint, Trade

log = logging.getLogger(__name__)
T = TypeVar("T")


class ParseError(ValueError):
    def __init__(self, kind: str, payload: Any, cause: BaseException) -> None:
        super().__init__(f"cannot parse {kind}: {cause!r}")
        self.payload = payload


def iso_to_ts(value: str | None) -> int | None:
    """'2026-09-21T00:00:00Z', '2026-09-21 04:30:03+00' or '2026-09-21' -> unix seconds (UTC)."""
    if not value:
        return None
    s = value.strip().replace("Z", "+00:00")
    if len(s) >= 3 and s[-3] in "+-" and s[-2:].isdigit():  # '+00' -> '+00:00'
        s += ":00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp())


def _json_list(value: Any) -> list[Any]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, list):
        raise TypeError(f"expected list, got {type(value).__name__}")
    return value


def _side(value: Any) -> str:
    side = str(value).upper()
    if side not in ("BUY", "SELL"):
        raise ValueError(f"unknown side {value!r}")
    return side


def _guard(kind: str, fn: Callable[[dict[str, Any]], T]) -> Callable[[dict[str, Any]], T]:
    def wrapped(d: dict[str, Any]) -> T:
        try:
            return fn(d)
        except (KeyError, TypeError, ValueError) as e:
            raise ParseError(kind, d, e) from e

    return wrapped


def _trade(d: dict[str, Any]) -> Trade:
    return Trade(
        tx_hash=str(d["transactionHash"]),
        ts=int(d["timestamp"]),
        wallet=str(d["proxyWallet"]).lower(),
        asset=str(d["asset"]),
        condition_id=str(d["conditionId"]).lower(),
        side=_side(d["side"]),
        price=float(d["price"]),
        size=float(d["size"]),
        event_slug=str(d.get("eventSlug") or ""),
        title=str(d.get("title") or ""),
        outcome=str(d.get("outcome") or ""),
        outcome_index=int(d.get("outcomeIndex", -1)),
        fee=float(d["fee"]) if d.get("fee") is not None else None,
    )


def _closed_position(d: dict[str, Any]) -> ClosedPosition:
    return ClosedPosition(
        wallet=str(d["proxyWallet"]).lower(),
        asset=str(d["asset"]),
        condition_id=str(d["conditionId"]).lower(),
        avg_price=float(d["avgPrice"]),
        total_bought=float(d["totalBought"]),
        realized_pnl=float(d.get("realizedPnl") or 0.0),
        cur_price=float(d.get("curPrice") or 0.0),
        outcome=str(d.get("outcome") or ""),
        outcome_index=int(d["outcomeIndex"]),
        title=str(d.get("title") or ""),
        event_slug=str(d.get("eventSlug") or ""),
        ts=int(d["timestamp"]),
    )


def _market(d: dict[str, Any]) -> Market:
    fees = d.get("feeSchedule") or {}
    events = d.get("events") or []
    return Market(
        condition_id=str(d["conditionId"]).lower(),
        question=str(d.get("question") or ""),
        slug=str(d.get("slug") or ""),
        event_slug=str(events[0].get("slug") or "") if events else "",
        end_ts=iso_to_ts(d.get("endDate")),
        closed=bool(d.get("closed", False)),
        closed_ts=iso_to_ts(d.get("closedTime")),
        outcome_prices=tuple(float(x) for x in _json_list(d.get("outcomePrices"))),
        token_ids=tuple(str(x) for x in _json_list(d.get("clobTokenIds"))),
        fees_enabled=bool(d.get("feesEnabled", False)),
        fee_rate=float(fees.get("rate") or 0.0),
        fee_exponent=float(fees.get("exponent") or 1.0),
        volume=float(d.get("volumeNum") or d.get("volume") or 0.0),
        tags=tuple(str(t["label"]) for t in (d.get("tags") or []) if t.get("label")),
    )


def _leaderboard(d: dict[str, Any]) -> LeaderboardEntry:
    return LeaderboardEntry(
        wallet=str(d["proxyWallet"]).lower(),
        rank=int(d["rank"]),
        volume=float(d.get("vol") or 0.0),
        pnl=float(d.get("pnl") or 0.0),
        name=str(d.get("userName") or ""),
    )


def _book(d: dict[str, Any]) -> BookSnapshot:
    return BookSnapshot(
        asset=str(d["asset_id"]),
        ts_ms=int(d["timestamp"]),
        bids=tuple((float(x["price"]), float(x["size"])) for x in d.get("bids") or []),
        asks=tuple((float(x["price"]), float(x["size"])) for x in d.get("asks") or []),
    )


def _price_history(d: dict[str, Any]) -> list[PricePoint]:
    return [PricePoint(ts=int(x["t"]), price=float(x["p"])) for x in d["history"]]


parse_trade = _guard("trade", _trade)
parse_closed_position = _guard("closed position", _closed_position)
parse_market = _guard("market", _market)
parse_leaderboard = _guard("leaderboard entry", _leaderboard)
parse_book = _guard("book", _book)
parse_price_history = _guard("price history", _price_history)


def parse_many(rows: Iterable[dict[str, Any]], fn: Callable[[dict[str, Any]], T]) -> tuple[list[T], int]:
    """Parse rows, skipping (and logging) malformed ones. Returns (items, n_skipped)."""
    items: list[T] = []
    skipped = 0
    for row in rows:
        try:
            items.append(fn(row))
        except ParseError as e:
            skipped += 1
            log.warning("%s — payload: %s", e, str(e.payload)[:300])
    return items, skipped
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_parsers.py -q` — Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: domain models and boundary parsers with real-payload tests"
```

---

### Task 6: Async HTTP client with rate limiting, retries and block detection

**Files:**
- Create: `src/whalescan/api/__init__.py` (empty), `src/whalescan/api/http.py`
- Test: `tests/test_http.py`

**Interfaces:**
- Produces: `ApiError(RuntimeError)` with `.status: int | None` and `.body: str`; `BlockedError(ApiError)`; `TokenBucket(rate_per_s, burst=1, clock=time.monotonic, sleep=asyncio.sleep)` with `async acquire()`; `HttpClient(*, user_agent, rate_per_s, max_retries, transport=None, sleep=asyncio.sleep, backoff_s=0.5, timeout_s=30.0)` with `async get_json(url: str, params: Sequence[tuple[str, str | int | float]] = ()) -> Any`, `async aclose()`, and counter attribute `errors: int`; `Params` type alias.

- [ ] **Step 1: Write the failing tests**

`tests/test_http.py`:
```python
import httpx
import pytest

from whalescan.api.http import ApiError, BlockedError, HttpClient, TokenBucket


class Sleeps:
    def __init__(self):
        self.calls: list[float] = []

    async def __call__(self, s: float) -> None:
        self.calls.append(s)


def client(handler, sleep=None, retries=3):
    return HttpClient(
        user_agent="whalescan/test", rate_per_s=1000.0, max_retries=retries,
        transport=httpx.MockTransport(handler), sleep=sleep or Sleeps(),
    )


async def test_returns_json_and_sends_user_agent_and_repeated_params():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["ua"] = request.headers["user-agent"]
        seen["ids"] = request.url.params.get_list("condition_ids")
        return httpx.Response(200, json=[{"ok": 1}])

    c = client(handler)
    assert await c.get_json("https://x.test/m", [("condition_ids", "a"), ("condition_ids", "b")]) == [{"ok": 1}]
    assert seen == {"ua": "whalescan/test", "ids": ["a", "b"]}
    await c.aclose()


async def test_retries_429_with_backoff_then_succeeds():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(429) if calls["n"] < 3 else httpx.Response(200, json={"ok": True})

    sleeps = Sleeps()
    c = client(handler, sleeps)
    assert await c.get_json("https://x.test/") == {"ok": True}
    assert sleeps.calls == [0.5, 1.0]
    assert c.errors == 2


async def test_retry_after_header_is_respected():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(503, headers={"Retry-After": "7"}) if calls["n"] == 1 else httpx.Response(200, json=1)

    sleeps = Sleeps()
    assert await client(handler, sleeps).get_json("https://x.test/") == 1
    assert sleeps.calls == [7.0]


async def test_gives_up_after_max_retries():
    c = client(lambda r: httpx.Response(500, text="boom"), retries=2)
    with pytest.raises(ApiError, match="giving up"):
        await c.get_json("https://x.test/")


async def test_cloudflare_block_raises_without_retry():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(403, text="Forbidden", headers={"server": "cloudflare", "content-type": "text/plain"})

    with pytest.raises(BlockedError, match="BLOCKED"):
        await client(handler).get_json("https://x.test/")
    assert calls["n"] == 1


async def test_client_error_is_not_retried_and_keeps_status_and_body():
    body = '{"error":"max historical trades offset of 10000 exceeded"}'
    c = client(lambda r: httpx.Response(400, text=body, headers={"content-type": "application/json"}))
    with pytest.raises(ApiError) as exc:
        await c.get_json("https://x.test/")
    assert exc.value.status == 400
    assert "offset" in exc.value.body
    assert not isinstance(exc.value, BlockedError)


async def test_transport_error_is_retried():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("down", request=request)
        return httpx.Response(200, json=[])

    assert await client(handler).get_json("https://x.test/") == []


async def test_token_bucket_waits_when_empty():
    now = {"t": 0.0}
    sleeps = []

    async def sleep(s):
        sleeps.append(s)
        now["t"] += s

    bucket = TokenBucket(2.0, burst=1, clock=lambda: now["t"], sleep=sleep)
    await bucket.acquire()
    await bucket.acquire()
    assert sleeps == [pytest.approx(0.5)]
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_http.py -q` — Expected: FAIL, module not found.

- [ ] **Step 3: Implement**

Create empty `src/whalescan/api/__init__.py`.

`src/whalescan/api/http.py`:
```python
"""Polite async HTTP: token-bucket rate limit, exponential backoff, and loud failure on bot-protection blocks."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import httpx

log = logging.getLogger(__name__)

Params = Sequence[tuple[str, str | int | float]]
Sleep = Callable[[float], Awaitable[None]]


class ApiError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.body = body


class BlockedError(ApiError):
    """The endpoint answered with a bot-protection challenge (e.g. Cloudflare 403). Retrying won't help."""


class TokenBucket:
    def __init__(self, rate_per_s: float, burst: int = 1, clock: Callable[[], float] = time.monotonic,
                 sleep: Sleep = asyncio.sleep) -> None:
        self._rate = rate_per_s
        self._capacity = float(max(1, burst))
        self._tokens = self._capacity
        self._clock = clock
        self._sleep = sleep
        self._updated = clock()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = self._clock()
                self._tokens = min(self._capacity, self._tokens + (now - self._updated) * self._rate)
                self._updated = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                await self._sleep((1.0 - self._tokens) / self._rate)


def _is_block(r: httpx.Response) -> bool:
    ctype = r.headers.get("content-type", "")
    return r.status_code == 403 and "application/json" not in ctype


def _retry_after(r: httpx.Response) -> float | None:
    value = r.headers.get("retry-after")
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


class HttpClient:
    def __init__(self, *, user_agent: str, rate_per_s: float, max_retries: int,
                 transport: httpx.AsyncBaseTransport | None = None, sleep: Sleep = asyncio.sleep,
                 backoff_s: float = 0.5, timeout_s: float = 30.0) -> None:
        self._client = httpx.AsyncClient(
            http2=transport is None,
            transport=transport,
            timeout=timeout_s,
            follow_redirects=True,
            headers={"User-Agent": user_agent, "Accept": "application/json"},
        )
        self._bucket = TokenBucket(rate_per_s, burst=max(1, int(rate_per_s)), sleep=sleep)
        self._max_retries = max_retries
        self._sleep = sleep
        self._backoff = backoff_s
        self.errors = 0

    async def get_json(self, url: str, params: Params = ()) -> Any:
        last = ""
        for attempt in range(self._max_retries + 1):
            await self._bucket.acquire()
            wait: float | None = None
            try:
                r = await self._client.get(url, params=list(params))
            except httpx.TransportError as e:
                last = f"transport error {e!r}"
            else:
                if r.status_code == 200:
                    try:
                        return r.json()
                    except ValueError as e:
                        raise ApiError(f"invalid JSON from {url}", status=200, body=r.text[:500]) from e
                if _is_block(r):
                    raise BlockedError(
                        f"BLOCKED by bot protection at {url} (HTTP {r.status_code}, server={r.headers.get('server')})",
                        status=r.status_code, body=r.text[:500])
                if r.status_code != 429 and r.status_code < 500:
                    raise ApiError(f"HTTP {r.status_code} from {url}: {r.text[:200]}",
                                   status=r.status_code, body=r.text[:500])
                last = f"HTTP {r.status_code}"
                wait = _retry_after(r)
            self.errors += 1
            if attempt < self._max_retries:
                delay = wait if wait is not None else self._backoff * 2**attempt
                log.info("retrying %s in %.1fs (%s)", url, delay, last)
                await self._sleep(delay)
        raise ApiError(f"giving up on {url} after {self._max_retries + 1} attempts: {last}")

    async def aclose(self) -> None:
        await self._client.aclose()
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_http.py -q` — Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat(api): async HTTP client with token bucket, backoff and Cloudflare block detection"
```

---

### Task 7: Polymarket API clients (Data API, Gamma, CLOB)

**Files:**
- Create: `src/whalescan/api/data_api.py`, `src/whalescan/api/gamma.py`, `src/whalescan/api/clob.py`
- Test: `tests/test_apis.py`

**Interfaces:**
- Consumes: `HttpClient.get_json`, `ApiError` (Task 6); parsers (Task 5).
- Produces:
  - `DataApi(http)`: `async closed_positions(wallet: str, *, since_ts: int | None = None) -> PositionHistory`; `async trades(*, user: str | None = None, min_usdc: float | None = None, since_ts: int | None = None) -> TradePage`; `async leaderboard(*, period: str, order_by: str, pages: int) -> list[LeaderboardEntry]`. Dataclasses `PositionHistory(positions: list[ClosedPosition], complete: bool)`, `TradePage(trades: list[Trade], complete: bool)`. Module constants `CLOSED_PAGE = 50`, `TRADES_PAGE = 500`, `LEADERBOARD_PAGE = 50`, `MAX_OFFSET = 10_000`.
  - `GammaApi(http)`: `async markets(condition_ids: Iterable[str]) -> dict[str, Market]` (keys lowercase), constant `CHUNK = 40`.
  - `ClobApi(http)`: `async book(token_id: str) -> BookSnapshot`; `async price_history(token_id: str, *, interval: str = "1w", fidelity: int = 60) -> list[PricePoint]`.
  - Every client exposes `skipped: int` (rows dropped by `parse_many`).

- [ ] **Step 1: Write the failing tests**

`tests/test_apis.py`:
```python
import httpx

from whalescan.api import data_api as data_api_module
from whalescan.api.clob import ClobApi
from whalescan.api.data_api import DataApi
from whalescan.api.gamma import GammaApi
from whalescan.api.http import HttpClient


async def _noop(_):
    return None


def http(handler) -> HttpClient:
    return HttpClient(user_agent="t", rate_per_s=1e6, max_retries=0, transport=httpx.MockTransport(handler), sleep=_noop)


def pos(i: int, ts: int) -> dict:
    return {"proxyWallet": "0xw", "asset": str(i), "conditionId": f"0xc{i}", "avgPrice": 0.5, "totalBought": 10,
            "realizedPnl": 1, "curPrice": 1, "outcome": "Yes", "outcomeIndex": 0, "title": "t", "eventSlug": "e",
            "timestamp": ts}


def trade(i: int, ts: int) -> dict:
    return {"proxyWallet": "0xw", "side": "BUY", "asset": str(i), "conditionId": "0xc", "size": 100, "price": 0.5,
            "timestamp": ts, "transactionHash": f"0x{i}", "outcomeIndex": 0}


async def test_closed_positions_paginates_by_timestamp_until_short_page():
    seen = []

    def handler(request):
        q = request.url.params
        seen.append((q["sortBy"], q["sortDirection"], int(q["offset"]), int(q["limit"])))
        off = int(q["offset"])
        n = 50 if off < 100 else 7
        return httpx.Response(200, json=[pos(off + k, 1000 - off - k) for k in range(n)])

    hist = await DataApi(http(handler)).closed_positions("0xw")
    assert hist.complete and len(hist.positions) == 107
    assert seen == [("TIMESTAMP", "DESC", 0, 50), ("TIMESTAMP", "DESC", 50, 50), ("TIMESTAMP", "DESC", 100, 50)]


async def test_closed_positions_offset_cap_marks_incomplete():
    def handler(request):
        if int(request.url.params["offset"]) >= 100:
            return httpx.Response(400, json={"error": "max historical trades offset of 10000 exceeded"})
        return httpx.Response(200, json=[pos(k, 5000 - k) for k in range(50)])

    hist = await DataApi(http(handler)).closed_positions("0xw")
    assert not hist.complete
    assert len(hist.positions) == 100


async def test_closed_positions_error_object_with_200_marks_incomplete():
    hist = await DataApi(http(lambda r: httpx.Response(200, json={"error": "nope"}))).closed_positions("0xw")
    assert not hist.complete and hist.positions == []


async def test_closed_positions_stops_at_local_offset_cap(monkeypatch):
    monkeypatch.setattr(data_api_module, "MAX_OFFSET", 100)
    hist = await DataApi(http(lambda r: httpx.Response(200, json=[pos(k, 9999) for k in range(50)]))).closed_positions("0xw")
    assert not hist.complete
    assert len(hist.positions) == 150  # offsets 0, 50, 100


async def test_closed_positions_incremental_stops_at_since_ts():
    pages = {0: [pos(k, 100 - k) for k in range(50)], 50: [pos(50 + k, 50 - k) for k in range(50)]}
    hist = await DataApi(http(lambda r: httpx.Response(200, json=pages[int(r.url.params["offset"])]))).closed_positions(
        "0xw", since_ts=80)
    assert hist.complete
    assert [p.ts for p in hist.positions] == list(range(100, 79, -1))


async def test_trades_filters_and_stops_at_since_ts():
    seen = {}

    def handler(request):
        seen.update(dict(request.url.params))
        return httpx.Response(200, json=[trade(k, 1000 - k * 10) for k in range(500)])

    page = await DataApi(http(handler)).trades(min_usdc=5000, since_ts=900)
    assert page.complete
    assert min(t.ts for t in page.trades) >= 900
    assert seen["filterType"] == "CASH" and seen["filterAmount"] == "5000" and seen["takerOnly"] == "false"


async def test_leaderboard_pages():
    def handler(request):
        off = int(request.url.params["offset"])
        rows = [] if off >= 100 else [{"rank": str(off + k + 1), "proxyWallet": f"0x{off + k}", "vol": 1, "pnl": 1}
                                      for k in range(50)]
        return httpx.Response(200, json=rows)

    board = await DataApi(http(handler)).leaderboard(period="ALL", order_by="PNL", pages=5)
    assert len(board) == 100 and board[0].rank == 1


async def test_gamma_queries_closed_and_open_in_chunks():
    calls = []

    def handler(request):
        ids = request.url.params.get_list("condition_ids")
        closed = request.url.params["closed"]
        calls.append((len(ids), closed, request.url.params["include_tag"]))
        rows = [{"conditionId": i.upper(), "closed": closed == "true", "outcomePrices": "[\"1\",\"0\"]"}
                for i in ids if (int(i[2:]) % 2 == 0) == (closed == "true")]
        return httpx.Response(200, json=rows)

    ids = [f"0x{i}" for i in range(85)]
    markets = await GammaApi(http(handler)).markets(ids + ["0X1"])
    assert len(markets) == 85
    assert markets["0x2"].closed and not markets["0x3"].closed
    assert sorted(calls) == sorted([(40, "true", "true"), (40, "false", "true"), (40, "true", "true"),
                                    (40, "false", "true"), (5, "true", "true"), (5, "false", "true")])


async def test_gamma_skips_malformed_rows():
    api = GammaApi(http(lambda r: httpx.Response(200, json=[{"question": "no id"}])))
    assert await api.markets(["0x1"]) == {}
    assert api.skipped == 2


async def test_clob_book_and_history():
    def handler(request):
        if request.url.path == "/book":
            return httpx.Response(200, json={"asset_id": request.url.params["token_id"], "timestamp": "5000",
                                             "bids": [{"price": "0.4", "size": "10"}], "asks": []})
        return httpx.Response(200, json={"history": [{"t": 1, "p": 0.4}, {"t": 2, "p": 0.45}]})

    clob = ClobApi(http(handler))
    book = await clob.book("123")
    assert book.asset == "123" and book.bids == ((0.4, 10.0),)
    assert [p.price for p in await clob.price_history("123")] == [0.4, 0.45]


def test_page_constants_match_api_limits():
    assert data_api_module.CLOSED_PAGE == 50
    assert data_api_module.MAX_OFFSET == 10_000
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_apis.py -q` — Expected: FAIL, module not found.

- [ ] **Step 3: Implement**

`src/whalescan/api/data_api.py`:
```python
"""Polymarket Data API: trades, closed positions, leaderboard. See docs/polymarket-api-notes.md."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from whalescan.api.http import ApiError, HttpClient, Params
from whalescan.models import ClosedPosition, LeaderboardEntry, Trade
from whalescan.parsers import parse_closed_position, parse_leaderboard, parse_many, parse_trade

log = logging.getLogger(__name__)

DATA_API = "https://data-api.polymarket.com"
CLOSED_PAGE = 50
TRADES_PAGE = 500
LEADERBOARD_PAGE = 50
MAX_OFFSET = 10_000


@dataclass(frozen=True)
class PositionHistory:
    positions: list[ClosedPosition]
    complete: bool


@dataclass(frozen=True)
class TradePage:
    trades: list[Trade]
    complete: bool


class DataApi:
    def __init__(self, http: HttpClient) -> None:
        self._http = http
        self.skipped = 0

    async def _page(self, path: str, params: Params) -> list[dict[str, Any]] | None:
        """One page of rows, or None when the API refuses to paginate deeper (offset cap)."""
        try:
            data = await self._http.get_json(f"{DATA_API}{path}", params)
        except ApiError as e:
            if e.status == 400 and "offset" in e.body.lower():
                return None
            raise
        if isinstance(data, dict):
            log.warning("error object from %s: %s", path, str(data)[:200])
            return None
        return data

    async def closed_positions(self, wallet: str, *, since_ts: int | None = None) -> PositionHistory:
        """All closed positions, newest first; stops at rows older than `since_ts` for incremental refresh.

        Sorted by TIMESTAMP on purpose: the default order is realizedPnl DESC, and a truncated fetch
        in that order would contain only winners.
        """
        out: list[ClosedPosition] = []
        offset = 0
        while offset <= MAX_OFFSET:
            rows = await self._page("/closed-positions", [
                ("user", wallet), ("limit", CLOSED_PAGE), ("offset", offset),
                ("sortBy", "TIMESTAMP"), ("sortDirection", "DESC"),
            ])
            if rows is None:
                return PositionHistory(out, False)
            batch, skipped = parse_many(rows, parse_closed_position)
            self.skipped += skipped
            if since_ts is not None:
                fresh = [p for p in batch if p.ts >= since_ts]
                out.extend(fresh)
                if len(fresh) < len(batch):
                    return PositionHistory(out, True)
            else:
                out.extend(batch)
            if len(rows) < CLOSED_PAGE:
                return PositionHistory(out, True)
            offset += CLOSED_PAGE
        return PositionHistory(out, False)

    async def trades(self, *, user: str | None = None, min_usdc: float | None = None,
                     since_ts: int | None = None) -> TradePage:
        """Trades newest first (maker and taker sides), stopping at `since_ts`."""
        base: list[tuple[str, str | int | float]] = [("limit", TRADES_PAGE), ("takerOnly", "false")]
        if user:
            base.append(("user", user))
        if min_usdc is not None:
            base += [("filterType", "CASH"), ("filterAmount", int(min_usdc))]
        out: list[Trade] = []
        offset = 0
        while offset <= MAX_OFFSET:
            rows = await self._page("/trades", [*base, ("offset", offset)])
            if rows is None:
                return TradePage(out, False)
            batch, skipped = parse_many(rows, parse_trade)
            self.skipped += skipped
            fresh = [t for t in batch if since_ts is None or t.ts >= since_ts]
            out.extend(fresh)
            if len(fresh) < len(batch) or len(rows) < TRADES_PAGE:
                return TradePage(out, True)
            offset += TRADES_PAGE
        return TradePage(out, False)

    async def leaderboard(self, *, period: str, order_by: str, pages: int) -> list[LeaderboardEntry]:
        out: list[LeaderboardEntry] = []
        for page in range(pages):
            rows = await self._page("/v1/leaderboard", [
                ("timePeriod", period), ("orderBy", order_by),
                ("limit", LEADERBOARD_PAGE), ("offset", page * LEADERBOARD_PAGE),
            ])
            if not rows:
                break
            batch, skipped = parse_many(rows, parse_leaderboard)
            self.skipped += skipped
            out.extend(batch)
            if len(rows) < LEADERBOARD_PAGE:
                break
        return out
```

`src/whalescan/api/gamma.py`:
```python
"""Gamma API: market metadata (resolution, fees, tags)."""

from __future__ import annotations

from collections.abc import Iterable

from whalescan.api.http import HttpClient
from whalescan.models import Market
from whalescan.parsers import parse_many, parse_market

GAMMA_API = "https://gamma-api.polymarket.com"
CHUNK = 40  # condition ids per request; keeps URLs ~3 KB and under the 100-row cap


class GammaApi:
    def __init__(self, http: HttpClient) -> None:
        self._http = http
        self.skipped = 0

    async def markets(self, condition_ids: Iterable[str]) -> dict[str, Market]:
        ids = sorted({c.lower() for c in condition_ids})
        out: dict[str, Market] = {}
        for i in range(0, len(ids), CHUNK):
            chunk = ids[i:i + CHUNK]
            for closed in ("true", "false"):  # Gamma hides closed markets unless asked explicitly
                rows = await self._http.get_json(
                    f"{GAMMA_API}/markets",
                    [("condition_ids", c) for c in chunk] + [("closed", closed), ("include_tag", "true"), ("limit", 100)],
                )
                markets, skipped = parse_many(rows, parse_market)
                self.skipped += skipped
                out.update((m.condition_id, m) for m in markets)
        return out
```

`src/whalescan/api/clob.py`:
```python
"""CLOB REST: order book snapshots and price history."""

from __future__ import annotations

from whalescan.api.http import HttpClient
from whalescan.models import BookSnapshot, PricePoint
from whalescan.parsers import parse_book, parse_price_history

CLOB_API = "https://clob.polymarket.com"


class ClobApi:
    def __init__(self, http: HttpClient) -> None:
        self._http = http
        self.skipped = 0

    async def book(self, token_id: str) -> BookSnapshot:
        return parse_book(await self._http.get_json(f"{CLOB_API}/book", [("token_id", token_id)]))

    async def price_history(self, token_id: str, *, interval: str = "1w", fidelity: int = 60) -> list[PricePoint]:
        data = await self._http.get_json(
            f"{CLOB_API}/prices-history", [("market", token_id), ("interval", interval), ("fidelity", fidelity)])
        return parse_price_history(data)
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_apis.py -q` — Expected: all pass.

- [ ] **Step 5: Live sanity check (network)**

Run:
```bash
uv run python -c "
import asyncio
from whalescan.api.http import HttpClient
from whalescan.api.data_api import DataApi
async def main():
    h = HttpClient(user_agent='whalescan/0.1', rate_per_s=5, max_retries=3)
    hist = await DataApi(h).closed_positions('0x5268527977f700f9bf9b6d5cd843859e4e70135d')
    wins = sum(p.cur_price == 1 for p in hist.positions)
    print(len(hist.positions), 'positions', 'complete' if hist.complete else 'INCOMPLETE', 'wins', wins)
    await h.aclose()
asyncio.run(main())"
```
Expected: a few hundred positions, `complete`, and `wins` strictly between 0 and the total.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat(api): Data API, Gamma and CLOB clients with bias-safe pagination"
```

---

### Task 8: DuckDB store with process lock

**Files:**
- Create: `src/whalescan/store.py`
- Test: `tests/test_store.py`

**Interfaces:**
- Consumes: models (Task 5).
- Produces: `LockedError(RuntimeError)`; `ProcessLock(path: Path)` with `acquire()`/`release()`; `WalletState(fetched_at: int, complete: bool, max_ts: int | None, name: str)`; `Store(db_path: Path, *, lock: bool = True)` (context manager) with:
  `upsert_trades(trades) -> int`, `upsert_positions(positions) -> int`, `upsert_markets(markets, now: int) -> int`,
  `record_wallet_fetch(wallet: str, *, fetched_at: int, complete: bool, source: str) -> None`,
  `set_wallet_names(names: Mapping[str, str]) -> None`, `wallet_state() -> dict[str, WalletState]`,
  `condition_ids_needing_refresh(now: int, open_max_age_s: int) -> set[str]`,
  `positions_frame() -> pd.DataFrame` (columns below), `trades_frame(*, since_ts: int | None = None, until_ts: int | None = None, wallets: Iterable[str] | None = None) -> pd.DataFrame` (columns = Trade fields),
  `markets_by_id(ids: Iterable[str] | None = None) -> dict[str, Market]`, `replace_scores(df)`, `scores_frame() -> pd.DataFrame`, `get_meta(key) -> str | None`, `set_meta(key, value)`, `close()`.
  Module function `trades_from_frame(df) -> list[Trade]`.
  `positions_frame()` columns: `wallet, asset, condition_id, avg_price, total_bought, realized_pnl, outcome, outcome_index, title, ts, event_slug, market_slug, market_event_slug, closed, closed_ts, volume, tags (list[str]), complete (bool), winner_index (Int64, <NA> when unresolved)`.

- [ ] **Step 1: Write the failing tests**

`tests/test_store.py`:
```python
import subprocess
import sys

import pandas as pd
import pytest

from whalescan.models import ClosedPosition, Market, Trade
from whalescan.store import LockedError, Store, trades_from_frame


def trade(tx="0x1", wallet="0xw", ts=100, side="BUY", fee=None) -> Trade:
    return Trade(tx, ts, wallet, "a1", "0xc1", side, 0.4, 100.0, "ev", "title", "Yes", 0, fee)


def position(wallet="0xw", asset="a1", cid="0xc1", ts=100, outcome_index=0) -> ClosedPosition:
    return ClosedPosition(wallet, asset, cid, 0.4, 100.0, 60.0, 1.0, "Yes", outcome_index, "t", "ev", ts)


def market(cid="0xc1", closed=True, prices=(1.0, 0.0)) -> Market:
    return Market(cid, "Q?", "slug", "ev", 2000, closed, 1500 if closed else None, prices, ("a1", "a2"),
                  True, 0.05, 1.0, 50000.0, ("Politics",))


def test_upserts_are_idempotent_and_dedupe_within_batch(tmp_path):
    with Store(tmp_path / "db.duckdb") as s:
        assert s.upsert_trades([trade(), trade()]) == 1
        s.upsert_trades([trade(), trade(tx="0x2")])
        assert len(s.trades_frame()) == 2


def test_trades_roundtrip_including_null_fee(tmp_path):
    with Store(tmp_path / "db.duckdb") as s:
        s.upsert_trades([trade(fee=None), trade(tx="0x2", fee=0.5)])
        back = sorted(trades_from_frame(s.trades_frame()), key=lambda t: t.tx_hash)
        assert back[0] == trade(fee=None)
        assert back[1].fee == 0.5


def test_trades_frame_filters(tmp_path):
    with Store(tmp_path / "db.duckdb") as s:
        s.upsert_trades([trade(tx="0x1", ts=10), trade(tx="0x2", ts=20, wallet="0xz")])
        assert len(s.trades_frame(since_ts=15)) == 1
        assert len(s.trades_frame(until_ts=15)) == 1
        assert len(s.trades_frame(wallets=["0xz"])) == 1
        assert len(s.trades_frame(wallets=[])) == 0


def test_markets_roundtrip(tmp_path):
    with Store(tmp_path / "db.duckdb") as s:
        s.upsert_markets([market()], now=1)
        assert s.markets_by_id(["0xc1"])["0xc1"] == market()
        assert s.markets_by_id() == {"0xc1": market()}
        assert s.markets_by_id([]) == {}


def test_positions_frame_resolution_and_completeness(tmp_path):
    with Store(tmp_path / "db.duckdb") as s:
        s.upsert_positions([position(cid="0xc1"), position(asset="a3", cid="0xc2"), position(asset="a4", cid="0xc3"),
                            position(asset="a5", cid="0xmissing")])
        s.upsert_markets([market("0xc1"), market("0xc2", prices=(0.5, 0.5)), market("0xc3", closed=False)], now=1)
        s.record_wallet_fetch("0xw", fetched_at=5, complete=True, source="leaderboard")
        df = s.positions_frame().set_index("condition_id")
        assert df.loc["0xc1", "winner_index"] == 0
        assert pd.isna(df.loc["0xc2", "winner_index"])        # fractional
        assert pd.isna(df.loc["0xc3", "winner_index"])        # still open
        assert pd.isna(df.loc["0xmissing", "winner_index"])   # unknown market
        assert bool(df.loc["0xc1", "complete"]) is True
        assert list(df.loc["0xc1", "tags"]) == ["Politics"]


def test_wallet_state_tracks_max_ts_and_names(tmp_path):
    with Store(tmp_path / "db.duckdb") as s:
        s.upsert_positions([position(ts=100), position(asset="a2", ts=300)])
        s.record_wallet_fetch("0xw", fetched_at=9, complete=False, source="trades")
        s.set_wallet_names({"0xw": "Whale"})
        st = s.wallet_state()["0xw"]
        assert (st.fetched_at, st.complete, st.max_ts, st.name) == (9, False, 300, "Whale")


def test_condition_ids_needing_refresh(tmp_path):
    with Store(tmp_path / "db.duckdb") as s:
        s.upsert_positions([position(cid="0xc1"), position(asset="a2", cid="0xnew")])
        s.upsert_trades([trade()])
        s.upsert_markets([market("0xc1", closed=False)], now=100)
        assert s.condition_ids_needing_refresh(now=150, open_max_age_s=3600) == {"0xnew"}
        assert s.condition_ids_needing_refresh(now=10_000, open_max_age_s=3600) == {"0xnew", "0xc1"}


def test_scores_and_meta(tmp_path):
    with Store(tmp_path / "db.duckdb") as s:
        df = pd.DataFrame([{"wallet": "0xw", "category": "ALL", "n": 3, "n_eff": 2.5, "edge": 0.1, "sigma": 0.05,
                            "post_edge": 0.08, "p_value": 0.01, "bh_pass": True, "certified": True, "flags": "",
                            "median_stake": 10.0, "as_of": 1}])
        s.replace_scores(df)
        s.replace_scores(df)
        assert len(s.scores_frame()) == 1
        s.set_meta("k", "v")
        assert s.get_meta("k") == "v" and s.get_meta("nope") is None


def test_second_writer_gets_a_clear_lock_error(tmp_path):
    with Store(tmp_path / "db.duckdb"):
        with pytest.raises(LockedError, match="another whalescan process"):
            Store(tmp_path / "db.duckdb")


def test_lock_is_released_when_holder_is_sigkilled(tmp_path):
    # flock locks belong to the process, not the file: a SIGKILL'd holder leaves db.lock on disk
    # but the OS releases the lock, so the next run must start normally.
    lock_path = tmp_path / "db.lock"
    code = (f"import time; from pathlib import Path; from whalescan.store import ProcessLock; "
            f"ProcessLock(Path({str(lock_path)!r})).acquire(); print('held', flush=True); time.sleep(60)")
    holder = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        with pytest.raises(LockedError):
            Store(tmp_path / "db.duckdb")
    finally:
        holder.kill()
        holder.wait()
    assert lock_path.exists()
    with Store(tmp_path / "db.duckdb"):
        pass
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_store.py -q` — Expected: FAIL, module not found.

- [ ] **Step 3: Implement**

`src/whalescan/store.py`:
```python
"""DuckDB persistence. One writer process per file, enforced with an advisory lock file."""

from __future__ import annotations

import fcntl
import math
import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import astuple, dataclass, fields
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

from whalescan.models import ClosedPosition, Market, Trade, resolved_winner

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
  tx_hash VARCHAR, ts BIGINT, wallet VARCHAR, asset VARCHAR, condition_id VARCHAR, side VARCHAR,
  price DOUBLE, size DOUBLE, event_slug VARCHAR, title VARCHAR, outcome VARCHAR, outcome_index INTEGER,
  fee DOUBLE, PRIMARY KEY (tx_hash, wallet, asset, side));
CREATE TABLE IF NOT EXISTS positions (
  wallet VARCHAR, asset VARCHAR, condition_id VARCHAR, avg_price DOUBLE, total_bought DOUBLE,
  realized_pnl DOUBLE, cur_price DOUBLE, outcome VARCHAR, outcome_index INTEGER, title VARCHAR,
  event_slug VARCHAR, ts BIGINT, PRIMARY KEY (wallet, asset));
CREATE TABLE IF NOT EXISTS markets (
  condition_id VARCHAR PRIMARY KEY, question VARCHAR, slug VARCHAR, event_slug VARCHAR, end_ts BIGINT,
  closed BOOLEAN, closed_ts BIGINT, outcome_prices DOUBLE[], token_ids VARCHAR[], fees_enabled BOOLEAN,
  fee_rate DOUBLE, fee_exponent DOUBLE, volume DOUBLE, tags VARCHAR[], fetched_at BIGINT);
CREATE TABLE IF NOT EXISTS wallets (
  wallet VARCHAR PRIMARY KEY, fetched_at BIGINT, complete BOOLEAN, source VARCHAR, name VARCHAR);
CREATE TABLE IF NOT EXISTS wallet_scores (
  wallet VARCHAR, category VARCHAR, n INTEGER, n_eff DOUBLE, edge DOUBLE, sigma DOUBLE, post_edge DOUBLE,
  p_value DOUBLE, bh_pass BOOLEAN, certified BOOLEAN, flags VARCHAR, median_stake DOUBLE, as_of BIGINT,
  PRIMARY KEY (wallet, category));
CREATE TABLE IF NOT EXISTS meta (key VARCHAR PRIMARY KEY, value VARCHAR);
"""

TRADE_COLS = [f.name for f in fields(Trade)]
POSITION_COLS = [f.name for f in fields(ClosedPosition)]
MARKET_COLS = [f.name for f in fields(Market)]
SCORE_COLS = ["wallet", "category", "n", "n_eff", "edge", "sigma", "post_edge", "p_value", "bh_pass",
              "certified", "flags", "median_stake", "as_of"]


class LockedError(RuntimeError):
    pass


class ProcessLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise LockedError(
                f"{self.path} is held by another whalescan process — stop it or wait for it to finish") from None
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        self._fd = fd

    def release(self) -> None:
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None


@dataclass(frozen=True)
class WalletState:
    fetched_at: int
    complete: bool
    max_ts: int | None
    name: str


def _as_list(value: Any) -> list[Any]:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return []
    return list(value)


def _opt_int(value: Any) -> int | None:
    return None if value is None or pd.isna(value) else int(value)


def trades_from_frame(df: pd.DataFrame) -> list[Trade]:
    out = []
    for row in df[TRADE_COLS].itertuples(index=False):
        d = row._asdict()
        d["fee"] = None if d["fee"] is None or pd.isna(d["fee"]) else float(d["fee"])
        d["ts"] = int(d["ts"])
        d["outcome_index"] = int(d["outcome_index"])
        out.append(Trade(**d))
    return out


class Store:
    def __init__(self, db_path: Path, *, lock: bool = True) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = ProcessLock(db_path.with_suffix(".lock")) if lock else None
        if self._lock:
            self._lock.acquire()
        try:
            self.con = duckdb.connect(str(db_path))
            self.con.execute(SCHEMA)
        except Exception:
            if self._lock:
                self._lock.release()
            raise

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self.con.close()
        if self._lock:
            self._lock.release()

    def _upsert_frame(self, table: str, df: pd.DataFrame, key: Sequence[str]) -> int:
        if df.empty:
            return 0
        df = df.drop_duplicates(subset=list(key), keep="last")
        cols = ", ".join(df.columns)
        self.con.register("_incoming", df)
        try:
            self.con.execute(f"INSERT OR REPLACE INTO {table} ({cols}) SELECT {cols} FROM _incoming")
        finally:
            self.con.unregister("_incoming")
        return len(df)

    def upsert_trades(self, trades: Iterable[Trade]) -> int:
        df = pd.DataFrame([astuple(t) for t in trades], columns=TRADE_COLS)
        return self._upsert_frame("trades", df, ["tx_hash", "wallet", "asset", "side"])

    def upsert_positions(self, positions: Iterable[ClosedPosition]) -> int:
        df = pd.DataFrame([astuple(p) for p in positions], columns=POSITION_COLS)
        return self._upsert_frame("positions", df, ["wallet", "asset"])

    def upsert_markets(self, markets: Iterable[Market], now: int) -> int:
        rows = [(m.condition_id, m.question, m.slug, m.event_slug, m.end_ts, m.closed, m.closed_ts,
                 list(m.outcome_prices), list(m.token_ids), m.fees_enabled, m.fee_rate, m.fee_exponent,
                 m.volume, list(m.tags), now) for m in {m.condition_id: m for m in markets}.values()]
        if rows:
            placeholders = ", ".join("?" * 15)
            self.con.executemany(f"INSERT OR REPLACE INTO markets VALUES ({placeholders})", rows)
        return len(rows)

    def record_wallet_fetch(self, wallet: str, *, fetched_at: int, complete: bool, source: str) -> None:
        self.con.execute(
            """INSERT INTO wallets (wallet, fetched_at, complete, source) VALUES (?, ?, ?, ?)
               ON CONFLICT (wallet) DO UPDATE SET fetched_at = excluded.fetched_at,
               complete = excluded.complete, source = excluded.source""",
            [wallet, fetched_at, complete, source])

    def set_wallet_names(self, names: Mapping[str, str]) -> None:
        rows = [(w, n) for w, n in names.items() if n]
        if rows:
            self.con.executemany(
                """INSERT INTO wallets (wallet, name) VALUES (?, ?)
                   ON CONFLICT (wallet) DO UPDATE SET name = excluded.name""", rows)

    def wallet_state(self) -> dict[str, WalletState]:
        rows = self.con.execute(
            """SELECT w.wallet, w.fetched_at, w.complete, p.max_ts, w.name FROM wallets w
               LEFT JOIN (SELECT wallet, max(ts) AS max_ts FROM positions GROUP BY wallet) p USING (wallet)""").fetchall()
        return {w: WalletState(int(f or 0), bool(c), _opt_int(m), n or "") for w, f, c, m, n in rows}

    def condition_ids_needing_refresh(self, now: int, open_max_age_s: int) -> set[str]:
        rows = self.con.execute(
            """WITH ids AS (SELECT condition_id FROM positions UNION SELECT condition_id FROM trades)
               SELECT ids.condition_id FROM ids LEFT JOIN markets m USING (condition_id)
               WHERE m.condition_id IS NULL OR (NOT m.closed AND m.fetched_at < ?)""",
            [now - open_max_age_s]).fetchall()
        return {r[0] for r in rows}

    def positions_frame(self) -> pd.DataFrame:
        df = self.con.execute(
            """SELECT p.wallet, p.asset, p.condition_id, p.avg_price, p.total_bought, p.realized_pnl, p.outcome,
                      p.outcome_index, p.title, p.ts, p.event_slug, m.slug AS market_slug,
                      m.event_slug AS market_event_slug, COALESCE(m.closed, FALSE) AS closed, m.closed_ts,
                      m.volume, m.tags, m.outcome_prices, COALESCE(w.complete, FALSE) AS complete
               FROM positions p
               LEFT JOIN markets m USING (condition_id)
               LEFT JOIN wallets w USING (wallet)""").df()
        winners = [resolved_winner(bool(c), _as_list(op)) for c, op in zip(df["closed"], df["outcome_prices"])]
        df["winner_index"] = pd.array(winners, dtype="Int64")
        df["tags"] = [_as_list(t) for t in df["tags"]]
        return df.drop(columns=["outcome_prices"])

    def trades_frame(self, *, since_ts: int | None = None, until_ts: int | None = None,
                     wallets: Iterable[str] | None = None) -> pd.DataFrame:
        clauses, params = [], []
        if since_ts is not None:
            clauses.append("ts >= ?")
            params.append(since_ts)
        if until_ts is not None:
            clauses.append("ts < ?")
            params.append(until_ts)
        if wallets is not None:
            wl = list(wallets)
            if not wl:
                return pd.DataFrame(columns=TRADE_COLS)
            clauses.append("wallet IN (SELECT unnest(?))")
            params.append(wl)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return self.con.execute(f"SELECT {', '.join(TRADE_COLS)} FROM trades {where} ORDER BY ts", params).df()

    def markets_by_id(self, ids: Iterable[str] | None = None) -> dict[str, Market]:
        sql = f"SELECT {', '.join(MARKET_COLS)} FROM markets"
        params: list[Any] = []
        if ids is not None:
            wanted = list(ids)
            if not wanted:
                return {}
            sql += " WHERE condition_id IN (SELECT unnest(?))"
            params.append(wanted)
        out = {}
        for r in self.con.execute(sql, params).fetchall():
            d = dict(zip(MARKET_COLS, r, strict=True))
            d["outcome_prices"] = tuple(float(x) for x in _as_list(d["outcome_prices"]))
            d["token_ids"] = tuple(_as_list(d["token_ids"]))
            d["tags"] = tuple(_as_list(d["tags"]))
            d["end_ts"] = _opt_int(d["end_ts"])
            d["closed_ts"] = _opt_int(d["closed_ts"])
            out[d["condition_id"]] = Market(**d)
        return out

    def replace_scores(self, df: pd.DataFrame) -> None:
        self.con.execute("DELETE FROM wallet_scores")
        self._upsert_frame("wallet_scores", df[SCORE_COLS], ["wallet", "category"])

    def scores_frame(self) -> pd.DataFrame:
        return self.con.execute(f"SELECT {', '.join(SCORE_COLS)} FROM wallet_scores").df()

    def get_meta(self, key: str) -> str | None:
        row = self.con.execute("SELECT value FROM meta WHERE key = ?", [key]).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.con.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", [key, value])
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_store.py -q` — Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: DuckDB store with idempotent upserts and single-writer lock"
```

---
### Task 9: Classification — categories, blocklist, wallet flags

**Files:**
- Create: `src/whalescan/classify.py`
- Test: `tests/test_classify.py`

**Interfaces:**
- Consumes: `CategoriesCfg`, `BlocklistCfg`, `ScoringCfg` (Task 1).
- Produces: `category_for_tags(tags: Iterable[str], cfg: CategoriesCfg) -> str` (one of `cfg.order` or `"OTHER"`); `Blocklist(cfg: BlocklistCfg)` with `blocked(*, event_slug: str, slug: str = "", volume: float | None = None) -> bool`; `wallet_flags(positions: pd.DataFrame, cfg: ScoringCfg) -> pd.Series` (index wallet → comma-joined subset of `MARKET_MAKER,FARMER,LOTTERY`, `""` when clean; input columns `wallet, condition_id, outcome_index, avg_price, total_bought`).

- [ ] **Step 1: Write the failing tests**

`tests/test_classify.py`:
```python
import pandas as pd

from whalescan.classify import Blocklist, category_for_tags, wallet_flags
from whalescan.config import load_config

CFG = load_config()


def test_category_uses_first_matching_bucket_case_insensitively():
    assert category_for_tags(["Sports", "NFL"], CFG.categories) == "SPORTS"
    assert category_for_tags(["Crypto", "Sports"], CFG.categories) == "CRYPTO"  # CRYPTO precedes SPORTS
    assert category_for_tags(["US Politics"], CFG.categories) == "POLITICS"
    assert category_for_tags([], CFG.categories) == "OTHER"
    assert category_for_tags(["Weather"], CFG.categories) == "OTHER"


def test_blocklist_patterns_and_volume():
    b = Blocklist(CFG.blocklist)
    assert b.blocked(event_slug="btc-updown-5m-1790250000")
    assert b.blocked(event_slug="eth-up-or-down-sept-24")
    assert b.blocked(event_slug="x", slug="sol-price-15m-candle-")
    assert b.blocked(event_slug="us-election", volume=500.0)
    assert not b.blocked(event_slug="us-election", volume=2_000_000.0)
    assert not b.blocked(event_slug="us-election", volume=None)


def rows(wallet, specs):
    return [{"wallet": wallet, "condition_id": c, "outcome_index": o, "avg_price": p, "total_bought": s}
            for c, o, p, s in specs]


def test_wallet_flags():
    df = pd.DataFrame(
        rows("0xmm", [("c1", 0, 0.5, 10), ("c1", 1, 0.5, 10), ("c2", 0, 0.5, 10), ("c2", 1, 0.5, 10), ("c3", 0, 0.5, 10)])
        + rows("0xfarm", [("c1", 0, 0.98, 1000), ("c2", 0, 0.5, 10)])
        + rows("0xlotto", [("c1", 0, 0.02, 100000), ("c2", 0, 0.5, 10)])
        + rows("0xclean", [("c1", 0, 0.4, 100), ("c2", 1, 0.6, 100)])
    )
    flags = wallet_flags(df, CFG.scoring)
    assert flags["0xmm"] == "MARKET_MAKER"
    assert flags["0xfarm"] == "FARMER"
    assert flags["0xlotto"] == "LOTTERY"
    assert flags["0xclean"] == ""


def test_wallet_flags_empty_frame():
    empty = pd.DataFrame(columns=["wallet", "condition_id", "outcome_index", "avg_price", "total_bought"])
    assert wallet_flags(empty, CFG.scoring).empty
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_classify.py -q` — Expected: FAIL, module not found.

- [ ] **Step 3: Implement**

`src/whalescan/classify.py`:
```python
"""Market categories, market blocklist, and behavioural wallet flags (spec §4.4)."""

from __future__ import annotations

import re
from collections.abc import Iterable

import pandas as pd

from whalescan.config import BlocklistCfg, CategoriesCfg, ScoringCfg


def category_for_tags(tags: Iterable[str], cfg: CategoriesCfg) -> str:
    lowered = {t.lower() for t in tags}
    for category in cfg.order:
        if lowered & set(cfg.tags.get(category, ())):
            return category
    return "OTHER"


class Blocklist:
    """Markets that are never scored or signalled: bot-dominated short-horizon markets and illiquid ones."""

    def __init__(self, cfg: BlocklistCfg) -> None:
        self._patterns = [re.compile(p) for p in cfg.slug_patterns]
        self._min_volume = cfg.min_market_volume

    def blocked(self, *, event_slug: str, slug: str = "", volume: float | None = None) -> bool:
        if any(p.search(event_slug) or p.search(slug) for p in self._patterns):
            return True
        return volume is not None and volume < self._min_volume


def wallet_flags(positions: pd.DataFrame, cfg: ScoringCfg) -> pd.Series:
    """Behavioural flags per wallet; flagged wallets are never certified."""
    if positions.empty:
        return pd.Series(dtype=object)
    df = positions.assign(stake=positions["total_bought"] * positions["avg_price"])
    total = df.groupby("wallet")["stake"].sum()
    farmer = df[df["avg_price"] >= cfg.farmer_price].groupby("wallet")["stake"].sum().reindex(total.index, fill_value=0.0)
    lottery = df[df["avg_price"] <= cfg.lottery_price].groupby("wallet")["stake"].sum().reindex(total.index, fill_value=0.0)
    both_sides = (df.groupby(["wallet", "condition_id"])["outcome_index"].nunique() > 1).groupby("wallet").mean()

    out: dict[str, str] = {}
    for wallet in total.index:
        flags = []
        if both_sides.get(wallet, 0.0) > cfg.mm_both_sides_share:
            flags.append("MARKET_MAKER")
        if total[wallet] > 0 and farmer[wallet] / total[wallet] > cfg.farmer_share:
            flags.append("FARMER")
        if total[wallet] > 0 and lottery[wallet] / total[wallet] > cfg.lottery_share:
            flags.append("LOTTERY")
        out[wallet] = ",".join(flags)
    return pd.Series(out, dtype=object)
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_classify.py -q` — Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: category mapping, market blocklist and wallet behaviour flags"
```

---

### Task 10: Scoring — eligibility, Monte Carlo, shrinkage, BH certification

**Files:**
- Create: `src/whalescan/scoring.py`
- Test: `tests/test_scoring.py`

**Interfaces:**
- Consumes: `whalecore.skill_mc_batch` (Task 3); `Blocklist`, `category_for_tags` (Task 9); `Store.positions_frame()` column set (Task 8).
- Produces: `ELIGIBLE_COLUMNS = ["wallet", "condition_id", "asset", "category", "p", "y", "w", "stake", "closed_ts", "title", "outcome"]`; `SCORE_COLUMNS` (identical to `store.SCORE_COLS`); `prepare_positions(frame, scoring: ScoringCfg, categories: CategoriesCfg, blocklist: Blocklist) -> pd.DataFrame[ELIGIBLE_COLUMNS]`; `benjamini_hochberg(p_values: np.ndarray, q: float) -> np.ndarray[bool]`; `score_wallets(eligible, flags: pd.Series, scoring: ScoringCfg, *, as_of: int, n_sims: int | None = None) -> pd.DataFrame[SCORE_COLUMNS]` — one row per (wallet, category) plus (wallet, "ALL").

- [ ] **Step 1: Write the failing tests**

`tests/test_scoring.py`:
```python
from dataclasses import replace

import numpy as np
import pandas as pd

from whalescan.classify import Blocklist
from whalescan.config import load_config
from whalescan.scoring import ELIGIBLE_COLUMNS, SCORE_COLUMNS, benjamini_hochberg, prepare_positions, score_wallets
from whalescan.store import SCORE_COLS

CFG = load_config()
SC = replace(CFG.scoring, n_sims=20000)


def synthetic(n_null=200, n_skilled=5, per=300, skill=0.2, seed=1) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rows = []
    for k in range(n_null + n_skilled):
        wallet = f"0xskilled{k}" if k >= n_null else f"0xnull{k}"
        p = rng.uniform(0.1, 0.9, per)
        win_prob = np.minimum(p + (skill if k >= n_null else 0.0), 0.99)
        y = (rng.uniform(size=per) < win_prob).astype(np.uint8)
        stake = rng.uniform(10, 1000, per)
        for i in range(per):
            rows.append({"wallet": wallet, "condition_id": f"c{k}-{i}", "asset": f"a{k}-{i}", "category": "POLITICS",
                         "p": p[i], "y": y[i], "w": stake[i], "stake": stake[i], "closed_ts": i, "title": "t",
                         "outcome": "Yes"})
    return pd.DataFrame(rows, columns=ELIGIBLE_COLUMNS)


def test_score_columns_match_store():
    assert SCORE_COLUMNS == SCORE_COLS


def test_benjamini_hochberg_hand_example():
    p = np.array([0.01, 0.04, 0.03, 0.2])
    # sorted: .01<=.025, .03<=.05, .04<=.075, .2>.1 -> first three pass
    assert benjamini_hochberg(p, 0.1).tolist() == [True, True, True, False]
    assert benjamini_hochberg(np.array([]), 0.1).tolist() == []
    assert benjamini_hochberg(np.array([0.5, 0.9]), 0.1).tolist() == [False, False]


def test_skilled_wallets_are_certified_and_noise_mostly_is_not():
    eligible = synthetic()
    scores = score_wallets(eligible, pd.Series(dtype=object), SC, as_of=123)
    assert list(scores.columns) == SCORE_COLUMNS
    overall = scores[scores.category == "ALL"].set_index("wallet")
    skilled = [w for w in overall.index if w.startswith("0xskilled")]
    null = [w for w in overall.index if w.startswith("0xnull")]
    assert overall.loc[skilled, "certified"].sum() >= 4
    assert overall.loc[null, "certified"].sum() <= 3
    assert (overall["as_of"] == 123).all()
    certified = overall[overall.certified]
    assert ((certified.post_edge <= certified.edge) & (certified.post_edge > 0)).all()


def test_sigma_matches_formula():
    eligible = synthetic(n_null=3, n_skilled=0, per=40)
    scores = score_wallets(eligible, pd.Series(dtype=object), SC, as_of=0)
    one = eligible[eligible.wallet == "0xnull0"]
    expected = np.sqrt((one.w**2 * one.p * (1 - one.p)).sum()) / one.w.sum()
    got = scores[(scores.wallet == "0xnull0") & (scores.category == "ALL")].sigma.iloc[0]
    assert np.isclose(got, expected)


def test_small_samples_shrink_more():
    eligible = synthetic()
    tiny = pd.DataFrame([{"wallet": "0xtiny", "condition_id": f"t{i}", "asset": f"t{i}", "category": "POLITICS",
                          "p": 0.5, "y": 1, "w": 100.0, "stake": 100.0, "closed_ts": i, "title": "t", "outcome": "Yes"}
                         for i in range(25)])
    scores = score_wallets(pd.concat([eligible, tiny], ignore_index=True), pd.Series(dtype=object), SC, as_of=0)
    overall = scores[scores.category == "ALL"].set_index("wallet")
    tiny_factor = overall.loc["0xtiny", "post_edge"] / overall.loc["0xtiny", "edge"]
    big_factor = overall.loc["0xskilled200", "post_edge"] / overall.loc["0xskilled200", "edge"]
    assert tiny_factor < big_factor


def test_flagged_wallet_is_never_certified():
    eligible = synthetic()
    flags = pd.Series({"0xskilled200": "FARMER"})
    scores = score_wallets(eligible, flags, SC, as_of=0)
    row = scores[(scores.wallet == "0xskilled200") & (scores.category == "ALL")].iloc[0]
    assert not row.certified and not row.bh_pass and row.flags == "FARMER"


def test_empty_input_gives_empty_frame():
    empty = pd.DataFrame(columns=ELIGIBLE_COLUMNS)
    assert score_wallets(empty, pd.Series(dtype=object), SC, as_of=0).empty


def frame_row(**kw):
    base = {"wallet": "0xw", "asset": "a", "condition_id": "c", "avg_price": 0.5, "total_bought": 100.0,
            "realized_pnl": 0.0, "outcome": "Yes", "outcome_index": 0, "title": "t", "ts": 1, "event_slug": "ev",
            "market_slug": "m", "market_event_slug": "ev", "closed": True, "closed_ts": 10, "volume": 1e6,
            "tags": ["Politics"], "complete": True, "winner_index": 0}
    base.update(kw)
    return base


def test_prepare_excludes_incomplete_and_unresolved():
    frame = pd.DataFrame([
        frame_row(asset="ok"),
        frame_row(asset="incomplete", wallet="0xpartial", complete=False),
        frame_row(asset="unresolved", winner_index=None),
        frame_row(asset="too_cheap", avg_price=0.01),
        frame_row(asset="bot", market_event_slug="btc-updown-5m-1"),
        frame_row(asset="illiquid", volume=100.0),
        frame_row(asset="lost", outcome_index=1),
    ])
    frame["winner_index"] = frame["winner_index"].astype("Int64")
    out = prepare_positions(frame, CFG.scoring, CFG.categories, Blocklist(CFG.blocklist))
    assert list(out.columns) == ELIGIBLE_COLUMNS
    assert sorted(out.asset) == ["lost", "ok"]
    assert out.set_index("asset").loc["ok", "y"] == 1
    assert out.set_index("asset").loc["lost", "y"] == 0
    assert (out.category == "POLITICS").all()


def test_prepare_winsorizes_stakes_per_wallet():
    frame = pd.DataFrame([frame_row(asset=f"a{i}", total_bought=100.0) for i in range(19)]
                         + [frame_row(asset="whale", total_bought=1_000_000.0)])
    frame["winner_index"] = frame["winner_index"].astype("Int64")
    out = prepare_positions(frame, CFG.scoring, CFG.categories, Blocklist(CFG.blocklist)).set_index("asset")
    assert out.loc["whale", "w"] < out.loc["whale", "stake"]
    assert out.loc["a0", "w"] == out.loc["a0", "stake"]
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_scoring.py -q` — Expected: FAIL, module not found.

- [ ] **Step 3: Implement**

`src/whalescan/scoring.py`:
```python
"""Wallet skill scoring (spec §4.5): C++ Monte Carlo p-values, empirical-Bayes shrinkage, BH certification."""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

import whalecore
from whalescan.classify import Blocklist, category_for_tags
from whalescan.config import CategoriesCfg, ScoringCfg

log = logging.getLogger(__name__)

ELIGIBLE_COLUMNS = ["wallet", "condition_id", "asset", "category", "p", "y", "w", "stake", "closed_ts", "title", "outcome"]
SCORE_COLUMNS = ["wallet", "category", "n", "n_eff", "edge", "sigma", "post_edge", "p_value", "bh_pass",
                 "certified", "flags", "median_stake", "as_of"]


def prepare_positions(frame: pd.DataFrame, scoring: ScoringCfg, categories: CategoriesCfg,
                      blocklist: Blocklist) -> pd.DataFrame:
    """Resolved, complete-history, in-band, non-blocklisted positions with winsorized stakes."""
    empty = pd.DataFrame(columns=ELIGIBLE_COLUMNS)
    if frame.empty:
        return empty
    df = frame[frame["winner_index"].notna() & frame["complete"].astype(bool)]
    df = df[(df["avg_price"] >= scoring.price_min) & (df["avg_price"] <= scoring.price_max) & (df["total_bought"] > 0)]
    if df.empty:
        return empty
    blocked = np.fromiter(
        (blocklist.blocked(event_slug=str(me or pe or ""), slug=str(ms or ""),
                           volume=None if pd.isna(v) else float(v))
         for me, pe, ms, v in zip(df["market_event_slug"], df["event_slug"], df["market_slug"], df["volume"])),
        dtype=bool, count=len(df))
    df = df[~blocked]
    if df.empty:
        return empty
    stake = df["total_bought"] * df["avg_price"]
    cap = stake.groupby(df["wallet"]).transform(lambda s: s.quantile(scoring.winsor_quantile))
    return pd.DataFrame({
        "wallet": df["wallet"].to_numpy(),
        "condition_id": df["condition_id"].to_numpy(),
        "asset": df["asset"].to_numpy(),
        "category": [category_for_tags(t, categories) for t in df["tags"]],
        "p": df["avg_price"].astype(float).to_numpy(),
        "y": (df["outcome_index"].astype(int).to_numpy() == df["winner_index"].astype(int).to_numpy()).astype(np.uint8),
        "w": np.minimum(stake, cap).to_numpy(),
        "stake": stake.to_numpy(),
        "closed_ts": df["closed_ts"].to_numpy(),
        "title": df["title"].to_numpy(),
        "outcome": df["outcome"].to_numpy(),
    })[ELIGIBLE_COLUMNS]


def benjamini_hochberg(p_values: np.ndarray, q: float) -> np.ndarray:
    """Boolean mask of tests that pass BH at false-discovery rate q."""
    m = len(p_values)
    passed = np.zeros(m, dtype=bool)
    if m == 0:
        return passed
    order = np.argsort(p_values, kind="stable")
    below = p_values[order] <= q * np.arange(1, m + 1) / m
    if below.any():
        k = int(np.max(np.flatnonzero(below)))
        passed[order[:k + 1]] = True
    return passed


def _mc(p: np.ndarray, y: np.ndarray, w: np.ndarray, offsets: np.ndarray, n_sims: int,
        seed: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    res = whalecore.skill_mc_batch(p, y, w, offsets, n_sims=n_sims, seed=seed, n_threads=0)
    return (np.array([r.edge for r in res]), np.array([r.p_value for r in res]),
            np.array([r.n_eff for r in res]))


def _subset(p: np.ndarray, y: np.ndarray, w: np.ndarray, offsets: np.ndarray,
            idx: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    lo, hi = offsets[idx], offsets[idx + 1]
    take = np.concatenate([np.arange(a, b) for a, b in zip(lo, hi)])
    new_offsets = np.concatenate([[0], np.cumsum(hi - lo)]).astype(np.int64)
    return p[take], y[take], w[take], new_offsets


def score_wallets(eligible: pd.DataFrame, flags: pd.Series, scoring: ScoringCfg, *, as_of: int,
                  n_sims: int | None = None) -> pd.DataFrame:
    if eligible.empty:
        return pd.DataFrame(columns=SCORE_COLUMNS)
    n_full = n_sims or scoring.n_sims
    tests = (pd.concat([eligible, eligible.assign(category="ALL")], ignore_index=True)
             .sort_values(["wallet", "category"], kind="stable").reset_index(drop=True))
    tests["w2"] = tests["w"] ** 2
    tests["v"] = tests["w2"] * tests["p"] * (1.0 - tests["p"])
    agg = (tests.groupby(["wallet", "category"], sort=False)
           .agg(n=("p", "size"), sw=("w", "sum"), sv=("v", "sum")).reset_index())
    offsets = np.concatenate([[0], np.cumsum(agg["n"].to_numpy())]).astype(np.int64)
    p = np.ascontiguousarray(tests["p"].to_numpy(np.float64))
    y = np.ascontiguousarray(tests["y"].to_numpy(np.uint8))
    w = np.ascontiguousarray(tests["w"].to_numpy(np.float64))

    # Pass 1: cheap screen for every test. Pass 2: full precision only where it can matter.
    n_screen = min(scoring.n_sims_screen, n_full)
    edge, p_value, n_eff = _mc(p, y, w, offsets, n_screen, scoring.seed)
    promising = np.flatnonzero(p_value < scoring.screen_p)
    if len(promising) and n_full > n_screen:
        sp, sy, sw, so = _subset(p, y, w, offsets, promising)
        p_value[promising] = _mc(sp, sy, sw, so, n_full, scoring.seed + 1)[1]
    log.info("scored %d tests (%d re-run at %d sims)", len(agg), len(promising), n_full)

    agg["edge"] = edge
    agg["p_value"] = p_value
    agg["n_eff"] = n_eff
    agg["sigma"] = np.sqrt(agg["sv"]) / agg["sw"]
    agg["flags"] = agg["wallet"].map(flags).fillna("").astype(str)
    agg["median_stake"] = agg["wallet"].map(eligible.groupby("wallet")["stake"].median())

    testable = ((agg["n_eff"] >= scoring.min_n_eff) & (agg["flags"] == "")).to_numpy()
    overall = testable & (agg["category"] == "ALL").to_numpy()
    s2 = agg["sigma"].to_numpy() ** 2
    tau2 = 0.0
    if overall.sum() >= 2:
        tau2 = max(0.0, float(np.var(agg["edge"].to_numpy()[overall], ddof=1) - s2[overall].mean()))
    agg["post_edge"] = agg["edge"] * (tau2 / (tau2 + s2)) if tau2 > 0 else 0.0

    bh = np.zeros(len(agg), dtype=bool)
    idx = np.flatnonzero(testable)
    bh[idx] = benjamini_hochberg(agg["p_value"].to_numpy()[idx], scoring.bh_q)
    agg["bh_pass"] = bh
    agg["certified"] = agg["bh_pass"] & (agg["post_edge"] >= scoring.min_post_edge)
    agg["as_of"] = as_of
    return agg[SCORE_COLUMNS]
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_scoring.py -q` — Expected: all pass in a few seconds.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: wallet scoring with two-pass Monte Carlo, shrinkage and BH certification"
```

---

### Task 11: Follow quotes and the signal gate

**Files:**
- Create: `src/whalescan/book.py`, `src/whalescan/gate.py`
- Test: `tests/test_book.py`, `tests/test_gate.py`

**Interfaces:**
- Consumes: `whalecore.OrderBook`, `whalecore.Side` (Task 4); `BookSnapshot`, `Market`, `Trade` (Task 5); `Blocklist`, `category_for_tags` (Task 9); `SCORE_COLUMNS` frame (Task 10).
- Produces:
  - `book.py`: `FollowQuote(vwap: float, complete: bool, book_as_of: int, best_bid: float | None, best_ask: float | None, microprice: float | None, levels: tuple[tuple[float, float], ...])`; `follow_quote(snap: BookSnapshot, size_usdc: float) -> FollowQuote` (levels = best 10 asks ascending).
  - `gate.py`: `PositionEvent(wallet, asset, condition_id, side, first_ts, last_ts, usdc, shares, price, n_fills, event_slug, title, outcome, outcome_index)` with `.id`; `aggregate(trades, window_s) -> list[PositionEvent]`; `WalletView(basis, post_edge, edge, p_value, n_cat, median_stake)`; `ScoreBook(scores, fallback_max_cat_positions)` with `view(wallet, category) -> WalletView | None`, `certified(wallet, category) -> bool`, `certified_wallets() -> set[str]`; `Check(code, passed, detail)`; `Evaluation(event, category, checks, status, tier, post_edge, net_edge, max_entry, fee, quote, consensus)` with `.failed() -> list[Check]`; `GateContext(cfg, categories, blocklist, scores, markets, events, now, historical=False)` with `.category(condition_id) -> str`; `evaluate(event, ctx, quote: FollowQuote | None) -> Evaluation`; `needs_book(e: Evaluation) -> bool`. Status ∈ `SIGNAL | REJECTED | EXIT | CONFLICT | EXPIRED`; tier ∈ `A | B | None`.

- [ ] **Step 1: Write the failing tests**

`tests/test_book.py`:
```python
import math

from whalescan.book import follow_quote
from whalescan.models import BookSnapshot


def test_follow_quote_walks_asks():
    snap = BookSnapshot("t", 1_790_000_000_500, bids=((0.39, 100.0),), asks=((0.42, 1000.0), (0.41, 10000.0)))
    q = follow_quote(snap, 500.0)
    assert q.complete and abs(q.vwap - 0.41) < 1e-12
    assert q.book_as_of == 1_790_000_000
    assert q.best_bid == 0.39 and q.best_ask == 0.41
    assert q.levels == ((0.41, 10000.0), (0.42, 1000.0))


def test_follow_quote_on_empty_book():
    q = follow_quote(BookSnapshot("t", 0, (), ()), 500.0)
    assert not q.complete and math.isnan(q.vwap) and q.best_ask is None and q.levels == ()
```

`tests/test_gate.py`:
```python
from dataclasses import replace

import pandas as pd

from whalescan.book import FollowQuote
from whalescan.classify import Blocklist
from whalescan.config import load_config
from whalescan.gate import GateContext, PositionEvent, ScoreBook, aggregate, evaluate, needs_book
from whalescan.models import Market, Trade
from whalescan.scoring import SCORE_COLUMNS

CFG = load_config()
NOW = 1_790_000_000
H = 3600


def score(wallet, category, certified, post_edge=0.06, n=100, edge=0.08, median_stake=2000.0):
    return {"wallet": wallet, "category": category, "n": n, "n_eff": n, "edge": edge, "sigma": 0.02,
            "post_edge": post_edge, "p_value": 0.001 if certified else 0.5, "bh_pass": certified,
            "certified": certified, "flags": "", "median_stake": median_stake, "as_of": NOW}


def book(*rows, fallback=CFG.gate.fallback_max_cat_positions):
    return ScoreBook(pd.DataFrame(list(rows), columns=SCORE_COLUMNS), fallback)


def market(cid="0xc", closed=False, end_ts=NOW + 48 * H, slug="us-election-winner", volume=1e6, fees=True):
    return Market(cid, "Who wins?", slug, "us-election", end_ts, closed, None, (0.4, 0.6), ("yes", "no"),
                  fees, 0.05, 1.0, volume, ("Politics",))


def event(wallet="0xwhale", asset="yes", side="BUY", usdc=10_000.0, price=0.40, ts=NOW - H, cid="0xc"):
    return PositionEvent(wallet, asset, cid, side, ts, ts + 60, usdc, usdc / price, price, 3, "us-election",
                         "Who wins?", "Yes", 0 if asset == "yes" else 1)


def quote(vwap=0.41, complete=True):
    return FollowQuote(vwap, complete, NOW, 0.39, 0.41, 0.40, ((0.41, 5000.0),))


CERTIFIED = [score("0xwhale", "ALL", True), score("0xwhale", "POLITICS", True)]


def ctx(scores=None, markets=None, events=(), now=NOW, historical=False, gate=CFG.gate):
    return GateContext(cfg=gate, categories=CFG.categories, blocklist=Blocklist(CFG.blocklist),
                       scores=scores or book(*CERTIFIED), markets={"0xc": market()} if markets is None else markets,
                       events=list(events), now=now, historical=historical)


def codes(e):
    return {c.code: c.passed for c in e.checks}


def trade(ts, wallet="0xw", side="BUY", price=0.4, size=100.0, asset="a"):
    return Trade(f"0x{ts}{wallet}{side}", ts, wallet, asset, "0xc", side, price, size, "ev", "t", "Yes", 0, None)


def test_aggregate_merges_within_window_and_splits_after():
    trades = [trade(0), trade(100, price=0.5), trade(700), trade(50, side="SELL"), trade(60, wallet="0xz")]
    events = aggregate(trades, 600)
    buys = [e for e in events if e.wallet == "0xw" and e.side == "BUY"]
    assert [(e.first_ts, e.n_fills) for e in buys] == [(0, 2), (700, 1)]
    assert abs(buys[0].price - (0.4 * 100 + 0.5 * 100) / 200) < 1e-12
    assert len(events) == 4


def test_all_gates_pass_is_a_tier_b_signal():
    e = evaluate(event(), ctx(), quote())
    assert e.status == "SIGNAL" and e.tier == "B", e.checks
    fee_follow = 0.05 * 0.41 * 0.59
    assert abs(e.net_edge - (0.06 - 0.01 - fee_follow)) < 1e-12
    assert abs(e.max_entry - (0.40 + 0.06 - 0.05 * 0.4 * 0.6 - 0.01)) < 1e-12


def test_high_net_edge_is_tier_a():
    scores = book(score("0xwhale", "ALL", True, post_edge=0.09), score("0xwhale", "POLITICS", True, post_edge=0.09))
    assert evaluate(event(), ctx(scores=scores), quote()).tier == "A"


def test_consensus_of_two_certified_whales_is_tier_a():
    scores = book(*CERTIFIED, score("0xother", "ALL", True), score("0xother", "POLITICS", True))
    other = event(wallet="0xother", ts=NOW - 2 * H)
    e = evaluate(event(), ctx(scores=scores, events=[other]), quote())
    assert e.tier == "A" and e.consensus == ("0xother", "0xwhale")


def test_uncertified_wallet_fails_g2():
    e = evaluate(event(), ctx(scores=book(score("0xwhale", "ALL", False))), quote())
    assert not codes(e)["G2"] and e.status == "REJECTED"


def test_unknown_wallet_fails_g2():
    e = evaluate(event(wallet="0xnobody"), ctx(), quote())
    assert not codes(e)["G2"] and e.checks[1].detail == "UNKNOWN WALLET"


def test_overall_certification_is_a_fallback_for_thin_categories():
    thin = book(score("0xwhale", "ALL", True), score("0xwhale", "POLITICS", False, n=5, edge=0.01))
    assert evaluate(event(), ctx(scores=thin), quote()).status == "SIGNAL"
    thick = book(score("0xwhale", "ALL", True), score("0xwhale", "POLITICS", False, n=50, edge=0.01))
    assert evaluate(event(), ctx(scores=thick), quote()).status == "REJECTED"
    negative = book(score("0xwhale", "ALL", True), score("0xwhale", "POLITICS", False, n=5, edge=-0.05))
    assert evaluate(event(), ctx(scores=negative), quote()).status == "REJECTED"


def test_conviction_gate():
    scores = book(score("0xwhale", "ALL", True, median_stake=4000.0), score("0xwhale", "POLITICS", True))
    e = evaluate(event(usdc=6000.0), ctx(scores=scores), quote())
    assert not codes(e)["G3"]
    assert not codes(evaluate(event(usdc=4000.0), ctx(), quote()))["G3"]


def test_price_band_gate():
    assert not codes(evaluate(event(price=0.97), ctx(), quote(vwap=0.975)))["G4"]


def test_unknown_market_fails_g5_without_crashing():
    e = evaluate(event(), ctx(markets={}), None)
    assert not codes(e)["G5"] and e.checks[4].detail == "UNKNOWN MARKET"
    assert e.status == "REJECTED" and e.fee is None


def test_blocklisted_and_near_end_markets_fail_g5():
    bot = ctx(markets={"0xc": market(slug="btc-updown-5m-1790000000")})
    assert not codes(evaluate(event(), bot, quote()))["G5"]
    soon = ctx(markets={"0xc": market(end_ts=NOW + H)})
    assert evaluate(event(), soon, quote()).checks[4].detail == "1.0H TO END"


def test_thin_book_fails_g6():
    e = evaluate(event(), ctx(), quote(vwap=float("nan"), complete=False))
    assert e.checks[5].detail == "BOOK TOO THIN" and e.status == "REJECTED"


def test_expensive_follow_fails_g6():
    e = evaluate(event(), ctx(), quote(vwap=0.46))
    assert not codes(e)["G6"] and e.net_edge < 0


def test_conflicting_certified_whale_suppresses_signal():
    scores = book(*CERTIFIED, score("0xbear", "ALL", True), score("0xbear", "POLITICS", True))
    against = event(wallet="0xbear", asset="no", price=0.6)
    e = evaluate(event(), ctx(scores=scores, events=[against]), quote())
    assert e.status == "CONFLICT" and not codes(e)["G7"]


def test_sell_is_an_exit():
    assert evaluate(event(side="SELL"), ctx(), None).status == "EXIT"


def test_old_events_and_closed_markets_expire():
    assert evaluate(event(ts=NOW - 30 * H), ctx(), quote()).status == "EXPIRED"
    assert evaluate(event(), ctx(markets={"0xc": market(closed=True)}), quote()).status == "EXPIRED"


def test_needs_book_only_when_g6_is_the_sole_blocker():
    assert needs_book(evaluate(event(), ctx(), None))
    assert not needs_book(evaluate(event(price=0.97), ctx(), None))
    assert not needs_book(evaluate(event(), ctx(), quote()))


def test_historical_mode_uses_event_time_and_ignores_closure():
    e = evaluate(event(ts=NOW - 100 * H), ctx(markets={"0xc": market(closed=True, end_ts=NOW)}, now=None,
                                               historical=True), quote())
    assert e.status == "SIGNAL"


def test_gate_thresholds_come_from_config():
    strict = replace(CFG.gate, min_net_edge=0.5)
    assert evaluate(event(), ctx(gate=strict), quote()).status == "REJECTED"
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_book.py tests/test_gate.py -q` — Expected: FAIL, modules not found.

- [ ] **Step 3: Implement**

`src/whalescan/book.py`:
```python
"""Cost-to-follow from an order book snapshot, computed by the C++ book engine."""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

import whalecore
from whalescan.models import BookSnapshot


@dataclass(frozen=True)
class FollowQuote:
    vwap: float  # NaN when nothing could be filled
    complete: bool
    book_as_of: int  # unix seconds
    best_bid: float | None
    best_ask: float | None
    microprice: float | None
    levels: tuple[tuple[float, float], ...]  # best asks, ascending


def _levels(rows: tuple[tuple[float, float], ...]) -> np.ndarray:
    return np.array(rows, dtype=np.float64).reshape(-1, 2)


def follow_quote(snap: BookSnapshot, size_usdc: float) -> FollowQuote:
    b = whalecore.OrderBook()
    b.apply_snapshot(_levels(snap.bids), _levels(snap.asks))
    walk = b.walk(whalecore.Side.ASK, size_usdc)
    return FollowQuote(
        vwap=walk.vwap if walk.filled_shares > 0 else math.nan,
        complete=walk.complete,
        book_as_of=snap.ts_ms // 1000,
        best_bid=b.best_bid(),
        best_ask=b.best_ask(),
        microprice=b.microprice(),
        levels=tuple(sorted(((p, s) for p, s in snap.asks if s > 0)))[:10],
    )
```

`src/whalescan/gate.py`:
```python
"""Signal gate (spec §4.6): merge fills into position events, then apply checks G1–G7."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

import pandas as pd

from whalescan.book import FollowQuote
from whalescan.classify import Blocklist, category_for_tags
from whalescan.config import CategoriesCfg, GateCfg
from whalescan.models import Market, Trade


@dataclass(frozen=True)
class PositionEvent:
    wallet: str
    asset: str
    condition_id: str
    side: str
    first_ts: int
    last_ts: int
    usdc: float
    shares: float
    price: float  # VWAP of the merged fills
    n_fills: int
    event_slug: str
    title: str
    outcome: str
    outcome_index: int

    @property
    def id(self) -> str:
        return f"{self.wallet}:{self.asset}:{self.side}:{self.first_ts}"


def _merge(group: list[Trade]) -> PositionEvent:
    first = group[0]
    usdc = sum(t.usdc for t in group)
    shares = sum(t.size for t in group)
    return PositionEvent(first.wallet, first.asset, first.condition_id, first.side, first.ts, group[-1].ts, usdc,
                         shares, usdc / shares if shares > 0 else first.price, len(group), first.event_slug,
                         first.title, first.outcome, first.outcome_index)


def aggregate(trades: Iterable[Trade], window_s: int) -> list[PositionEvent]:
    """Fills by the same (wallet, asset, side) within `window_s` of the first fill become one event."""
    def key(t: Trade) -> tuple[str, str, str]:
        return (t.wallet, t.asset, t.side)

    events: list[PositionEvent] = []
    group: list[Trade] = []
    for t in sorted(trades, key=lambda t: (key(t), t.ts)):
        if group and key(t) == key(group[0]) and t.ts - group[0].ts <= window_s:
            group.append(t)
        else:
            if group:
                events.append(_merge(group))
            group = [t]
    if group:
        events.append(_merge(group))
    return sorted(events, key=lambda e: (e.first_ts, e.id))


@dataclass(frozen=True)
class WalletView:
    basis: str  # "CATEGORY" | "OVERALL" | "NONE"
    post_edge: float
    edge: float
    p_value: float
    n_cat: int
    median_stake: float


class ScoreBook:
    def __init__(self, scores: pd.DataFrame, fallback_max_cat_positions: int) -> None:
        self._rows = {(r.wallet, r.category): r for r in scores.itertuples(index=False)}
        self._fallback = fallback_max_cat_positions

    def view(self, wallet: str, category: str) -> WalletView | None:
        overall = self._rows.get((wallet, "ALL"))
        if overall is None:
            return None
        cat = self._rows.get((wallet, category))
        median = float(overall.median_stake)
        if cat is not None and bool(cat.certified):
            return WalletView("CATEGORY", float(cat.post_edge), float(cat.edge), float(cat.p_value), int(cat.n), median)
        n_cat = int(cat.n) if cat is not None else 0
        cat_ok = cat is None or float(cat.edge) >= 0.0
        if bool(overall.certified) and n_cat < self._fallback and cat_ok:
            return WalletView("OVERALL", float(overall.post_edge), float(overall.edge), float(overall.p_value), n_cat,
                              median)
        return WalletView("NONE", 0.0, float(overall.edge), float(overall.p_value), n_cat, median)

    def certified(self, wallet: str, category: str) -> bool:
        v = self.view(wallet, category)
        return v is not None and v.basis != "NONE"

    def certified_wallets(self) -> set[str]:
        return {w for (w, _), r in self._rows.items() if bool(r.certified)}


@dataclass(frozen=True)
class Check:
    code: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class Evaluation:
    event: PositionEvent
    category: str
    checks: tuple[Check, ...]
    status: str  # SIGNAL | REJECTED | EXIT | CONFLICT | EXPIRED
    tier: str | None
    post_edge: float | None
    net_edge: float | None
    max_entry: float | None
    fee: float | None  # fee per share at the whale's price
    quote: FollowQuote | None
    consensus: tuple[str, ...]

    def failed(self) -> list[Check]:
        return [c for c in self.checks if not c.passed]


@dataclass(frozen=True)
class GateContext:
    cfg: GateCfg
    categories: CategoriesCfg
    blocklist: Blocklist
    scores: ScoreBook
    markets: Mapping[str, Market]
    events: Sequence[PositionEvent]
    now: int | None
    historical: bool = False

    def category(self, condition_id: str) -> str:
        m = self.markets.get(condition_id)
        return category_for_tags(m.tags, self.categories) if m else "OTHER"


def _g5(ev: PositionEvent, market: Market | None, ctx: GateContext, now: int) -> Check:
    if market is None:
        return Check("G5", False, "UNKNOWN MARKET")
    if ctx.blocklist.blocked(event_slug=market.event_slug or ev.event_slug, slug=market.slug, volume=market.volume):
        return Check("G5", False, "BLOCKLISTED")
    if market.closed and not ctx.historical:
        return Check("G5", False, "MARKET CLOSED")
    if market.end_ts is None:
        return Check("G5", True, "NO END DATE")
    hours = (market.end_ts - now) / 3600
    return Check("G5", hours >= ctx.cfg.min_hours_to_end, f"{hours:.1f}H TO END")


def evaluate(ev: PositionEvent, ctx: GateContext, quote: FollowQuote | None) -> Evaluation:
    g = ctx.cfg
    market = ctx.markets.get(ev.condition_id)
    category = ctx.category(ev.condition_id)
    now = ev.first_ts if ctx.historical or ctx.now is None else ctx.now
    view = ctx.scores.view(ev.wallet, category)
    certified = view is not None and view.basis != "NONE"
    window = g.conflict_window_h * 3600

    def certified_buy_nearby(o: PositionEvent) -> bool:
        return (o.side == "BUY" and abs(o.first_ts - ev.first_ts) <= window
                and ctx.scores.certified(o.wallet, category))

    checks = [Check("G1", ev.side == "BUY", "BUY" if ev.side == "BUY" else "SELL · EXIT")]

    if certified:
        checks.append(Check("G2", True, f"CERTIFIED {view.basis} · EDGE {view.post_edge:+.3f}"))
    else:
        checks.append(Check("G2", False, "UNKNOWN WALLET" if view is None else "NOT CERTIFIED"))

    median = view.median_stake if view else math.nan
    multiple = ev.usdc / median if median and median > 0 else math.nan
    g3 = ev.usdc >= g.min_usdc and not math.isnan(multiple) and multiple >= g.conviction_k
    checks.append(Check("G3", g3, f"${ev.usdc:,.0f} · {multiple:.1f}× MEDIAN" if view else f"${ev.usdc:,.0f}"))

    checks.append(Check("G4", g.price_min <= ev.price <= g.price_max, f"PRICE {ev.price:.3f}"))
    checks.append(_g5(ev, market, ctx, now))

    net: float | None = None
    if not certified or market is None:
        checks.append(Check("G6", False, "N/A"))
    elif quote is None:
        checks.append(Check("G6", False, "NO BOOK"))
    elif not quote.complete or math.isnan(quote.vwap):
        checks.append(Check("G6", False, "BOOK TOO THIN"))
    else:
        net = view.post_edge - (quote.vwap - ev.price) - market.fee_per_share(quote.vwap)
        checks.append(Check("G6", net >= g.min_net_edge, f"FOLLOW {quote.vwap:.3f} · NET {net:+.3f}"))

    opposing = [o for o in ctx.events
                if o.condition_id == ev.condition_id and o.asset != ev.asset and certified_buy_nearby(o)]
    n_opp = len({o.wallet for o in opposing})
    checks.append(Check("G7", not opposing, "NO CONFLICT" if not opposing else f"CONFLICT · {n_opp} OPPOSING"))

    consensus = {o.wallet for o in ctx.events if o.asset == ev.asset and certified_buy_nearby(o)}
    if certified and ev.side == "BUY":
        consensus.add(ev.wallet)

    post = view.post_edge if certified else None
    fee = market.fee_per_share(ev.price) if market else None
    max_entry = ev.price + post - (fee or 0.0) - g.max_entry_margin if post is not None else None

    expired = not ctx.historical and (
        (market is not None and market.closed)
        or (ctx.now is not None and ctx.now - ev.last_ts > g.signal_lookback_h * 3600))
    if expired:
        status = "EXPIRED"
    elif ev.side != "BUY":
        status = "EXIT"
    elif all(c.passed for c in checks):
        status = "SIGNAL"
    elif certified and not checks[6].passed:
        status = "CONFLICT"
    else:
        status = "REJECTED"

    tier = None
    if status == "SIGNAL":
        strong = (net is not None and net >= g.tier_a_net_edge) or len(consensus) >= g.tier_a_consensus
        tier = "A" if strong else "B"
    return Evaluation(ev, category, tuple(checks), status, tier, post, net, max_entry, fee, quote,
                      tuple(sorted(consensus)))


def needs_book(e: Evaluation) -> bool:
    """True when fetching an order book could turn this evaluation into a signal."""
    failed = e.failed()
    return e.status == "REJECTED" and len(failed) == 1 and failed[0].code == "G6" and failed[0].detail == "NO BOOK"
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_book.py tests/test_gate.py -q` — Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: follow quotes via C++ book and the G1–G7 signal gate"
```

---

### Task 12: Walk-forward validation

**Files:**
- Create: `src/whalescan/validate.py`
- Test: `tests/test_validate.py`

**Interfaces:**
- Consumes: `score_wallets`, `ELIGIBLE_COLUMNS` (Task 10); `aggregate`, `evaluate`, `GateContext`, `ScoreBook` (Task 11); `FollowQuote` (Task 11).
- Produces: `Fold(cutoff: int, end: int)`; `make_folds(first_ts, last_ts, *, fold_days, min_history_days) -> list[Fold]`; `run_validation(eligible, flags, trades: list[Trade], markets: Mapping[str, Market], cfg: Config, blocklist: Blocklist, *, now: int) -> dict` with keys `generated_at, verdict, folds, groups, calibration, params, caveats`; `groups` has keys `A, B, SIGNALS, BASELINE`, each `{n, mean_ret, hit_rate, t_stat}`; `verdict ∈ {"INSUFFICIENT DATA", "EDGE CONFIRMED", "EDGE NOT CONFIRMED"}`.

- [ ] **Step 1: Write the failing tests**

`tests/test_validate.py`:
```python
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
    assert len(report["caveats"]) == 3


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
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_validate.py -q` — Expected: FAIL, module not found.

- [ ] **Step 3: Implement**

`src/whalescan/validate.py`:
```python
"""Walk-forward validation (spec §4.7): score on the past, test on the future, report honestly."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from whalescan.book import FollowQuote
from whalescan.classify import Blocklist
from whalescan.config import Config
from whalescan.gate import GateContext, ScoreBook, aggregate, evaluate
from whalescan.models import Market, Trade
from whalescan.scoring import score_wallets

DAY = 86400
CALIBRATION_BINS = (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)


@dataclass(frozen=True)
class Fold:
    cutoff: int
    end: int


def make_folds(first_ts: int, last_ts: int, *, fold_days: int, min_history_days: int) -> list[Fold]:
    folds = []
    step = fold_days * DAY
    t = first_ts + min_history_days * DAY
    while t + step <= last_ts:
        folds.append(Fold(t, t + step))
        t += step
    return folds


def _stats(rets: np.ndarray) -> dict[str, Any]:
    n = len(rets)
    if n == 0:
        return {"n": 0, "mean_ret": None, "hit_rate": None, "t_stat": None}
    mean = float(rets.mean())
    sd = float(rets.std(ddof=1)) if n > 1 else 0.0
    return {"n": n, "mean_ret": mean, "hit_rate": float((rets > 0).mean()),
            "t_stat": mean / (sd / math.sqrt(n)) if sd > 0 else None}


def run_validation(eligible: pd.DataFrame, flags: pd.Series, trades: list[Trade], markets: Mapping[str, Market],
                   cfg: Config, blocklist: Blocklist, *, now: int) -> dict[str, Any]:
    v, g = cfg.validation, cfg.gate
    rows: list[dict[str, Any]] = []
    fold_rows: list[dict[str, Any]] = []
    if not eligible.empty and trades:
        first = int(eligible["closed_ts"].min())
        last = min(max(t.ts for t in trades), now)
        for fold in make_folds(first, last, fold_days=v.fold_days, min_history_days=v.min_history_days):
            train = eligible[eligible["closed_ts"] < fold.cutoff]
            scores = score_wallets(train, flags, cfg.scoring, as_of=fold.cutoff, n_sims=v.n_sims)
            events = aggregate([t for t in trades if fold.cutoff <= t.ts < fold.end], g.aggregation_window_s)
            ctx = GateContext(cfg=g, categories=cfg.categories, blocklist=blocklist,
                              scores=ScoreBook(scores, g.fallback_max_cat_positions), markets=markets,
                              events=events, now=None, historical=True)
            fold_rets = []
            for ev in events:
                m = markets.get(ev.condition_id)
                winner = m.winner_index() if m else None
                if m is None or winner is None or ev.side != "BUY":
                    continue
                won = 1.0 if ev.outcome_index == winner else 0.0
                follow = min(0.99, ev.price + v.slippage)
                ret = won - follow - m.fee_per_share(follow)
                if ev.usdc >= g.min_usdc and g.price_min <= ev.price <= g.price_max:
                    rows.append({"group": "BASELINE", "ret": ret, "predicted": np.nan, "won": won})
                quote = FollowQuote(follow, True, ev.first_ts, None, None, None, ())
                e = evaluate(ev, ctx, quote)
                if e.status == "SIGNAL":
                    fold_rets.append(ret)
                    rows.append({"group": e.tier, "ret": ret, "won": won,
                                 "predicted": min(0.99, ev.price + (e.post_edge or 0.0))})
            fold_rows.append({"cutoff": fold.cutoff, "end": fold.end, "signals": len(fold_rets),
                              "mean_ret": float(np.mean(fold_rets)) if fold_rets else None})
    return _report(rows, fold_rows, cfg, now)


def _report(rows: list[dict[str, Any]], fold_rows: list[dict[str, Any]], cfg: Config, now: int) -> dict[str, Any]:
    v = cfg.validation
    df = pd.DataFrame(rows, columns=["group", "ret", "predicted", "won"])
    sig = df[df["group"].isin(["A", "B"])]
    groups = {
        "A": _stats(df.loc[df["group"] == "A", "ret"].to_numpy(float)),
        "B": _stats(df.loc[df["group"] == "B", "ret"].to_numpy(float)),
        "SIGNALS": _stats(sig["ret"].to_numpy(float)),
        "BASELINE": _stats(df.loc[df["group"] == "BASELINE", "ret"].to_numpy(float)),
    }
    calibration = []
    for lo, hi in zip(CALIBRATION_BINS[:-1], CALIBRATION_BINS[1:]):
        pred = sig["predicted"].astype(float)
        inside = sig[(pred >= lo) & ((pred < hi) if hi < 1.0 else (pred <= hi))]
        calibration.append({"lo": lo, "hi": hi, "n": len(inside),
                            "predicted": float(inside["predicted"].mean()) if len(inside) else None,
                            "realized": float(inside["won"].mean()) if len(inside) else None})
    s = groups["SIGNALS"]
    if s["n"] < v.min_signals:
        verdict = "INSUFFICIENT DATA"
    elif s["t_stat"] is not None and s["t_stat"] >= 2.0:
        verdict = "EDGE CONFIRMED"
    else:
        verdict = "EDGE NOT CONFIRMED"
    return {
        "generated_at": now,
        "verdict": verdict,
        "folds": fold_rows,
        "groups": groups,
        "calibration": calibration,
        "params": {"fold_days": v.fold_days, "min_history_days": v.min_history_days, "slippage": v.slippage,
                   "n_sims": v.n_sims},
        "caveats": [
            "Wallet universe is seeded from the current leaderboard, which leaks some future information into "
            "which wallets are studied.",
            "Positions exited before resolution are scored as if held to resolution from the average entry price.",
            f"Follow cost is modelled as whale price + {v.slippage:.3f}; historical order books were not recorded.",
        ],
    }
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_validate.py -q` — Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: walk-forward validation with tier, baseline and calibration report"
```

---
### Task 13: Snapshot writer, batch pipeline and CLI

**Files:**
- Create: `src/whalescan/snapshot.py`, `src/whalescan/batch.py`, `src/whalescan/cli.py`
- Test: `tests/test_batch.py`

**Interfaces:**
- Consumes: everything above. API client method names/signatures from Task 7 (the fakes in the test mirror them exactly).
- Produces:
  - `snapshot.py`: `clean(obj) -> JSON-safe obj` (NaN/inf → None, numpy → Python, dataclass → dict); `write_json_atomic(path: Path, obj) -> None`; `write_parquet_atomic(df, path: Path) -> None`; `evaluation_json(e: Evaluation, market: Market | None, names: Mapping[str, str], history: list[PricePoint] | None) -> dict`; `whales_json(scores, eligible, names, *, recent_n=12, watchlist_n=25) -> list[dict]`.
  - `batch.py`: `Apis(data, gamma, clob)` with `skipped() -> int`; `BatchReport` (fields `wallets_scanned, wallets_complete, tests, testable, certified_wallets, signals, contacts, api_errors, validation_ran, top`); `async run_batch(cfg, *, apis: Apis | None = None, now: int | None = None, skip_validation=False, force_validation=False, stop_after_scoring=False, publish=False) -> BatchReport`; `git_publish(snapshot_dir: Path, now: int) -> bool`.
  - `cli.py`: `main(argv: list[str] | None = None) -> int`; exit codes `0` ok, `2` locked, `3` blocked. Commands: `whalescan score [--max-wallets N]`, `whalescan batch [--skip-validation] [--force-validation] [--publish] [--max-wallets N]`, global `--config PATH`, `-v`.
  - Snapshot files in `cfg.paths.snapshot_dir`: `meta.json`, `signals.json`, `contacts.json`, `whales.json`, `validation.json` (written when validation runs). JSON shapes are exactly those built by `evaluation_json`, `whales_json`, `_meta_json`, `run_validation` below; Task 14's `types.ts` mirrors them.

- [ ] **Step 1: Write the failing tests**

`tests/test_batch.py`:
```python
import json
from dataclasses import replace

import numpy as np
import pytest

from whalescan import cli
from whalescan.api.data_api import PositionHistory, TradePage
from whalescan.api.http import BlockedError
from whalescan.batch import Apis, run_batch
from whalescan.config import PathsCfg, load_config
from whalescan.models import BookSnapshot, ClosedPosition, LeaderboardEntry, Market, PricePoint, Trade
from whalescan.snapshot import write_json_atomic
from whalescan.store import LockedError

NOW = 1_790_000_000
DAY = 86400


def config(tmp):
    return replace(load_config(), paths=PathsCfg(research_db=str(tmp / "research.duckdb"),
                                                 scores_parquet=str(tmp / "scores.parquet"),
                                                 snapshot_dir=str(tmp / "snapshot")))


class FakeData:
    def __init__(self, positions, trades, *, block=False):
        self.positions, self.rows, self.block, self.skipped = positions, trades, block, 0

    async def leaderboard(self, *, period, order_by, pages):
        if self.block:
            raise BlockedError("BLOCKED by bot protection at test")
        return [LeaderboardEntry(w, i + 1, 1e6, 1e5, f"name{i}") for i, w in enumerate(sorted(self.positions))]

    async def closed_positions(self, wallet, *, since_ts=None):
        rows = [p for p in self.positions.get(wallet, []) if since_ts is None or p.ts >= since_ts]
        return PositionHistory(rows, True)

    async def trades(self, *, user=None, min_usdc=None, since_ts=None):
        rows = [t for t in self.rows if (user is None or t.wallet == user)
                and (since_ts is None or t.ts >= since_ts) and (min_usdc is None or t.usdc >= min_usdc)]
        return TradePage(rows, True)


class FakeGamma:
    def __init__(self, markets):
        self.m, self.skipped = markets, 0

    async def markets(self, ids):
        return {i: self.m[i] for i in ids if i in self.m}


class FakeClob:
    skipped = 0

    async def book(self, token_id):
        return BookSnapshot(token_id, NOW * 1000, ((0.39, 5000.0),), ((0.41, 100000.0),))

    async def price_history(self, token_id, *, interval="1w", fidelity=60):
        return [PricePoint(NOW - 3600, 0.39), PricePoint(NOW, 0.40)]


def resolved(cid, winner):
    return Market(cid, f"Q {cid}", f"slug-{cid}", f"ev-{cid}", NOW - 10 * DAY, True, NOW - 10 * DAY,
                  (1.0, 0.0) if winner == 0 else (0.0, 1.0), (f"{cid}-y", f"{cid}-n"), False, 0.0, 1.0, 1e6,
                  ("Politics",))


def world(skilled=True, seed=5):
    """30 slightly-bad wallets, optionally one strongly skilled whale, and one live $20k trade."""
    rng = np.random.default_rng(seed)
    positions, markets = {}, {}
    wallets = [f"0xnull{k}" for k in range(30)] + (["0xwhale"] if skilled else [])
    for w in wallets:
        n, skill = (200, 0.3) if w == "0xwhale" else (60, -0.05)
        rows = []
        for i in range(n):
            p = float(rng.uniform(0.2, 0.8))
            won = rng.uniform() < min(max(p + skill, 0.01), 0.99)
            cid = f"{w}-c{i}"
            markets[cid] = resolved(cid, 0 if won else 1)
            rows.append(ClosedPosition(w, f"{cid}-y", cid, p, 2000.0, 0.0, 1.0 if won else 0.0, "Yes", 0, f"T{i}",
                                       f"ev-{cid}", NOW - 20 * DAY + i))
        positions[w] = rows
    markets["0xlive"] = Market("0xlive", "Will it happen?", "will-it-happen", "it-happens", NOW + 3 * DAY, False,
                               None, (0.4, 0.6), ("live-yes", "live-no"), False, 0.0, 1.0, 5e6, ("Politics",))
    trader = "0xwhale" if skilled else "0xnull0"
    trades = [Trade("0xt1", NOW - 3600, trader, "live-yes", "0xlive", "BUY", 0.40, 50_000.0, "it-happens",
                    "Will it happen?", "Yes", 0, None)]
    return positions, markets, trades


async def run(tmp, w, block=False, **kw):
    positions, markets, trades = w
    apis = Apis(FakeData(positions, trades, block=block), FakeGamma(markets), FakeClob())
    return await run_batch(config(tmp), apis=apis, now=NOW, **kw)


def read(tmp, name):
    def reject(c):
        raise AssertionError(f"non-JSON constant {c}")
    return json.loads((tmp / "snapshot" / name).read_text(), parse_constant=reject)


async def test_batch_produces_signal_for_certified_whale(tmp_path):
    report = await run(tmp_path, world(), skip_validation=True)
    assert report.certified_wallets == 1
    signals = read(tmp_path, "signals.json")
    assert [s["wallet"] for s in signals] == ["0xwhale"]
    s = signals[0]
    assert s["status"] == "SIGNAL" and s["tier"] == "A"
    assert s["quote"]["vwap"] == pytest.approx(0.41) and s["history"] and s["wallet_name"].startswith("name")
    assert {c["code"] for c in s["checks"]} == {f"G{i}" for i in range(1, 8)}
    whales = read(tmp_path, "whales.json")
    assert whales[0]["wallet"] == "0xwhale" and whales[0]["certified"] and whales[0]["recent"]
    meta = read(tmp_path, "meta.json")
    assert meta["counts"]["signals"] == 1 and meta["mode"] == "SNAPSHOT"
    assert (tmp_path / "scores.parquet").exists()
    assert not (tmp_path / "snapshot" / "validation.json").exists()


async def test_batch_with_no_certified_whales_writes_snapshot(tmp_path):
    report = await run(tmp_path, world(skilled=False))
    assert report.certified_wallets == 0 and report.validation_ran
    assert read(tmp_path, "signals.json") == []
    contacts = read(tmp_path, "contacts.json")
    assert len(contacts) == 1 and contacts[0]["status"] == "REJECTED"
    assert all(not w["certified"] for w in read(tmp_path, "whales.json"))
    assert read(tmp_path, "validation.json")["verdict"] == "INSUFFICIENT DATA"
    assert read(tmp_path, "meta.json")["counts"]["certified_wallets"] == 0


async def test_score_only_stops_before_snapshot(tmp_path):
    report = await run(tmp_path, world(), stop_after_scoring=True)
    assert report.certified_wallets == 1 and list(report.top.wallet.unique()) == ["0xwhale"]
    assert not (tmp_path / "snapshot").exists()


async def test_second_run_is_incremental_and_stable(tmp_path):
    await run(tmp_path, world(), skip_validation=True)
    await run(tmp_path, world(), skip_validation=True)
    assert [s["wallet"] for s in read(tmp_path, "signals.json")] == ["0xwhale"]


def test_json_has_no_nan(tmp_path):
    write_json_atomic(tmp_path / "x.json", {"a": float("nan"), "b": [np.float64("inf"), np.int64(3)], "c": np.bool_(True)})
    assert json.loads((tmp_path / "x.json").read_text()) == {"a": None, "b": [None, 3], "c": True}
    assert not list(tmp_path.glob("*.tmp"))


async def test_blocked_run_keeps_previous_snapshot(tmp_path):
    snap = tmp_path / "snapshot"
    snap.mkdir()
    (snap / "meta.json").write_text('{"old": true}')
    with pytest.raises(BlockedError):
        await run(tmp_path, world(), block=True)
    assert json.loads((snap / "meta.json").read_text()) == {"old": True}


def test_cli_blocked_exit_code_keeps_snapshot(monkeypatch, capsys):
    async def blocked(*args, **kwargs):
        raise BlockedError("BLOCKED by bot protection at test")

    monkeypatch.setattr(cli, "run_batch", blocked)
    assert cli.main(["batch"]) == 3
    assert "BLOCKED" in capsys.readouterr().err


def test_cli_locked_exit_code(monkeypatch, capsys):
    async def locked(*args, **kwargs):
        raise LockedError("held by another whalescan process")

    monkeypatch.setattr(cli, "run_batch", locked)
    assert cli.main(["score"]) == 2
    assert "LOCKED" in capsys.readouterr().err


def test_cli_max_wallets_override(monkeypatch):
    seen = {}

    async def fake(cfg, **kwargs):
        seen["max"] = cfg.universe.max_wallets
        seen["kwargs"] = kwargs
        from whalescan.batch import BatchReport
        return BatchReport()

    monkeypatch.setattr(cli, "run_batch", fake)
    assert cli.main(["batch", "--max-wallets", "50", "--skip-validation"]) == 0
    assert seen["max"] == 50 and seen["kwargs"]["skip_validation"] is True
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_batch.py -q` — Expected: FAIL, modules not found.

- [ ] **Step 3: Implement the snapshot writer**

`src/whalescan/snapshot.py`:
```python
"""Strict-JSON snapshot files read by the dashboard. Writes are atomic (tmp file + rename)."""

from __future__ import annotations

import dataclasses
import json
import math
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from whalescan.gate import Evaluation
from whalescan.models import Market, PricePoint


def clean(obj: Any) -> Any:
    if obj is None or isinstance(obj, str):
        return obj
    if isinstance(obj, (bool, np.bool_)):
        return bool(obj)
    if isinstance(obj, (int, np.integer)):
        return int(obj)
    if isinstance(obj, (float, np.floating)):
        f = float(obj)
        return f if math.isfinite(f) else None
    if obj is pd.NA or obj is pd.NaT:
        return None
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: clean(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, Mapping):
        return {str(k): clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, np.ndarray)):
        return [clean(v) for v in obj]
    raise TypeError(f"cannot serialise {type(obj).__name__}")


def write_json_atomic(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(clean(obj), allow_nan=False, separators=(",", ":")))
    os.replace(tmp, path)


def write_parquet_atomic(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    df.to_parquet(tmp, index=False)
    os.replace(tmp, path)


def evaluation_json(e: Evaluation, market: Market | None, names: Mapping[str, str],
                    history: list[PricePoint] | None) -> dict[str, Any]:
    ev, q = e.event, e.quote
    return {
        "id": ev.id,
        "status": e.status,
        "tier": e.tier,
        "category": e.category,
        "wallet": ev.wallet,
        "wallet_name": names.get(ev.wallet, ""),
        "question": market.question if market else ev.title,
        "market_slug": market.slug if market else "",
        "event_slug": (market.event_slug if market else "") or ev.event_slug,
        "end_ts": market.end_ts if market else None,
        "outcome": ev.outcome,
        "side": ev.side,
        "price": ev.price,
        "usdc": ev.usdc,
        "shares": ev.shares,
        "first_ts": ev.first_ts,
        "last_ts": ev.last_ts,
        "n_fills": ev.n_fills,
        "post_edge": e.post_edge,
        "net_edge": e.net_edge,
        "max_entry": e.max_entry,
        "fee": e.fee,
        "quote": None if q is None else {
            "vwap": q.vwap, "complete": q.complete, "book_as_of": q.book_as_of, "best_bid": q.best_bid,
            "best_ask": q.best_ask, "microprice": q.microprice, "levels": [list(level) for level in q.levels],
        },
        "consensus": list(e.consensus),
        "checks": [{"code": c.code, "passed": c.passed, "detail": c.detail} for c in e.checks],
        "history": None if history is None else [{"t": p.ts, "p": p.price} for p in history],
    }


def whales_json(scores: pd.DataFrame, eligible: pd.DataFrame, names: Mapping[str, str], *, recent_n: int = 12,
                watchlist_n: int = 25) -> list[dict[str, Any]]:
    """Certified wallets plus a watchlist of the most significant uncertified, unflagged wallets."""
    if scores.empty:
        return []
    overall = scores[scores["category"] == "ALL"].set_index("wallet")
    certified = set(scores.loc[scores["certified"].astype(bool), "wallet"])
    watch = [w for w in overall[overall["flags"] == ""].sort_values("p_value").index if w not in certified]
    out = []
    for wallet in sorted(certified) + watch[:watchlist_n]:
        cats = scores[scores["wallet"] == wallet].sort_values("p_value")
        best = cats[cats["certified"].astype(bool)].sort_values("post_edge", ascending=False)
        recent = eligible[eligible["wallet"] == wallet].sort_values("closed_ts", ascending=False).head(recent_n)
        row = overall.loc[wallet] if wallet in overall.index else None
        out.append({
            "wallet": wallet,
            "name": names.get(wallet, ""),
            "certified": wallet in certified,
            "median_stake": None if row is None else row["median_stake"],
            "flags": "" if row is None else row["flags"],
            "best_category": best["category"].iloc[0] if len(best) else None,
            "best_post_edge": float(best["post_edge"].iloc[0]) if len(best) else None,
            "categories": cats[["category", "n", "n_eff", "edge", "post_edge", "p_value", "bh_pass",
                                "certified"]].to_dict("records"),
            "recent": [{"title": r.title, "outcome": r.outcome, "price": r.p, "won": bool(r.y), "stake": r.stake,
                        "closed_ts": r.closed_ts} for r in recent.itertuples(index=False)],
        })
    return sorted(out, key=lambda d: (not d["certified"], -(d["best_post_edge"] or 0.0)))
```

- [ ] **Step 4: Implement the batch pipeline**

`src/whalescan/batch.py`:
```python
"""Snapshot pipeline (spec §3): universe → positions → markets → scores → signals → validation → JSON."""

from __future__ import annotations

import asyncio
import logging
import subprocess
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from whalescan import __version__
from whalescan.api.clob import ClobApi
from whalescan.api.data_api import DataApi
from whalescan.api.gamma import GammaApi
from whalescan.api.http import ApiError, BlockedError, HttpClient
from whalescan.book import follow_quote
from whalescan.classify import Blocklist, wallet_flags
from whalescan.config import ROOT, Config
from whalescan.gate import GateContext, ScoreBook, aggregate, evaluate, needs_book
from whalescan.scoring import prepare_positions, score_wallets
from whalescan.snapshot import evaluation_json, whales_json, write_json_atomic, write_parquet_atomic
from whalescan.store import Store, trades_from_frame
from whalescan.validate import run_validation

log = logging.getLogger(__name__)
MARKET_MAX_AGE_S = 3600
FLAG_COLUMNS = ["wallet", "condition_id", "outcome_index", "avg_price", "total_bought"]


@dataclass
class Apis:
    data: Any  # DataApi-compatible
    gamma: Any  # GammaApi-compatible
    clob: Any  # ClobApi-compatible

    def skipped(self) -> int:
        return self.data.skipped + self.gamma.skipped + self.clob.skipped


@dataclass
class BatchReport:
    wallets_scanned: int = 0
    wallets_complete: int = 0
    tests: int = 0
    testable: int = 0
    certified_wallets: int = 0
    signals: int = 0
    contacts: int = 0
    api_errors: int = 0
    validation_ran: bool = False
    top: pd.DataFrame | None = None


async def _guarded(coro: Any, what: str) -> Any:
    """Await an API call; log and swallow ordinary API errors, but let blocks propagate."""
    try:
        return await coro
    except BlockedError:
        raise
    except ApiError as e:
        log.warning("%s failed: %s", what, e)
        return None


async def discover_universe(apis: Apis, store: Store, cfg: Config, now: int) -> dict[str, str]:
    u = cfg.universe
    sources: dict[str, str] = {}
    names: dict[str, str] = {}
    for period in u.leaderboard_periods:
        for order in u.leaderboard_orders:
            for entry in await apis.data.leaderboard(period=period, order_by=order, pages=u.leaderboard_pages):
                sources.setdefault(entry.wallet, "leaderboard")
                names.setdefault(entry.wallet, entry.name)
    page = await apis.data.trades(min_usdc=u.large_trade_min_usdc, since_ts=now - u.large_trade_lookback_days * 86400)
    store.upsert_trades(page.trades)
    volume: dict[str, float] = {}
    for t in page.trades:
        volume[t.wallet] = volume.get(t.wallet, 0.0) + t.usdc
    for wallet, _ in sorted(volume.items(), key=lambda kv: -kv[1]):
        sources.setdefault(wallet, "large_trades")
    store.set_wallet_names(names)
    return dict(list(sources.items())[:u.max_wallets])


async def refresh_positions(apis: Apis, store: Store, wallets: Mapping[str, str], concurrency: int, now: int) -> int:
    state = store.wallet_state()
    sem = asyncio.Semaphore(concurrency)
    failures = 0

    async def one(wallet: str, source: str) -> None:
        nonlocal failures
        prev = state.get(wallet)
        since = prev.max_ts if prev and prev.complete and prev.max_ts is not None else None
        async with sem:
            hist = await _guarded(apis.data.closed_positions(wallet, since_ts=since), f"positions {wallet}")
        if hist is None:
            failures += 1
            return
        store.upsert_positions(hist.positions)
        store.record_wallet_fetch(wallet, fetched_at=now, complete=hist.complete, source=source)

    await asyncio.gather(*(one(w, s) for w, s in wallets.items()))
    return failures


async def refresh_markets(apis: Apis, store: Store, now: int) -> None:
    ids = store.condition_ids_needing_refresh(now, MARKET_MAX_AGE_S)
    if ids:
        log.info("fetching metadata for %d markets", len(ids))
        store.upsert_markets((await apis.gamma.markets(ids)).values(), now)


async def build_signals(apis: Apis, store: Store, cfg: Config, scores: pd.DataFrame, blocklist: Blocklist,
                        now: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    g = cfg.gate
    since = now - int(g.signal_lookback_h * 3600)
    book = ScoreBook(scores, g.fallback_max_cat_positions)
    sem = asyncio.Semaphore(cfg.http.concurrency)

    async def wallet_trades(wallet: str) -> None:
        async with sem:
            page = await _guarded(apis.data.trades(user=wallet, since_ts=since), f"trades {wallet}")
        if page is not None:
            store.upsert_trades(page.trades)

    await asyncio.gather(*(wallet_trades(w) for w in sorted(book.certified_wallets())))
    page = await _guarded(apis.data.trades(min_usdc=g.min_usdc, since_ts=since), "large trades")
    if page is not None:
        store.upsert_trades(page.trades)
    await refresh_markets(apis, store, now)

    events = aggregate(trades_from_frame(store.trades_frame(since_ts=since)), g.aggregation_window_s)
    markets = store.markets_by_id({e.condition_id for e in events})
    ctx = GateContext(cfg=g, categories=cfg.categories, blocklist=blocklist, scores=book, markets=markets,
                      events=events, now=now)
    evaluations = [evaluate(e, ctx, None) for e in events]
    for i, e in enumerate(evaluations):
        if needs_book(e):
            snap = await _guarded(apis.clob.book(e.event.asset), f"book {e.event.asset}")
            if snap is not None:
                evaluations[i] = evaluate(e.event, ctx, follow_quote(snap, g.follow_size_usdc))

    names = {w: s.name for w, s in store.wallet_state().items()}
    signals = []
    for e in sorted((e for e in evaluations if e.status == "SIGNAL"),
                    key=lambda e: (e.tier or "Z", -(e.net_edge or 0.0))):
        history = await _guarded(apis.clob.price_history(e.event.asset), f"history {e.event.asset}")
        signals.append(evaluation_json(e, markets.get(e.event.condition_id), names, history))
    contacts = [evaluation_json(e, markets.get(e.event.condition_id), names, None)
                for e in sorted(evaluations, key=lambda e: -e.event.last_ts)
                if e.status != "SIGNAL" and e.event.usdc >= g.min_usdc][:g.max_contacts]
    return signals, contacts


async def maybe_validate(apis: Apis, store: Store, cfg: Config, eligible: pd.DataFrame, flags: pd.Series,
                         scores: pd.DataFrame, blocklist: Blocklist, now: int, *, force: bool) -> dict[str, Any] | None:
    last = store.get_meta("validation_at")
    if not force and last and now - int(last) < cfg.validation.every_hours * 3600:
        return None
    testable = scores[(scores["category"] == "ALL") & (scores["n_eff"] >= cfg.scoring.min_n_eff)
                      & (scores["flags"] == "")].sort_values("n_eff", ascending=False)
    wallets = list(testable["wallet"].head(cfg.validation.max_wallets))
    sem = asyncio.Semaphore(cfg.http.concurrency)

    async def history(wallet: str) -> None:
        async with sem:
            page = await _guarded(apis.data.trades(user=wallet), f"history {wallet}")
        if page is not None:
            store.upsert_trades(page.trades)

    await asyncio.gather(*(history(w) for w in wallets))
    await refresh_markets(apis, store, now)
    trades = trades_from_frame(store.trades_frame(wallets=wallets))
    markets = store.markets_by_id({t.condition_id for t in trades})
    report = run_validation(eligible, flags, trades, markets, cfg, blocklist, now=now)
    store.set_meta("validation_at", str(now))
    return report


def _meta_json(cfg: Config, report: BatchReport, now: int, validation_at: str | None) -> dict[str, Any]:
    g = cfg.gate
    return {
        "generated_at": now,
        "version": __version__,
        "mode": "SNAPSHOT",
        "counts": {
            "wallets_scanned": report.wallets_scanned, "wallets_complete": report.wallets_complete,
            "tests": report.tests, "testable": report.testable, "certified_wallets": report.certified_wallets,
            "signals": report.signals, "contacts": report.contacts,
        },
        "params": {
            "bh_q": cfg.scoring.bh_q, "min_usdc": g.min_usdc, "conviction_k": g.conviction_k,
            "follow_size_usdc": g.follow_size_usdc, "min_net_edge": g.min_net_edge,
            "signal_lookback_h": g.signal_lookback_h,
        },
        "errors": {"api": report.api_errors},
        "validation_generated_at": int(validation_at) if validation_at else None,
    }


def git_publish(snapshot_dir: Path, now: int) -> bool:
    """Commit and push the snapshot directory. Returns False when nothing changed."""
    subprocess.run(["git", "add", str(snapshot_dir)], check=True, cwd=ROOT)
    if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ROOT).returncode == 0:
        return False
    stamp = datetime.fromtimestamp(now, UTC).strftime("%Y-%m-%dT%H:%MZ")
    subprocess.run(["git", "commit", "-m", f"snapshot: {stamp}"], check=True, cwd=ROOT)
    subprocess.run(["git", "push"], check=True, cwd=ROOT)
    return True


async def run_batch(cfg: Config, *, apis: Apis | None = None, now: int | None = None, skip_validation: bool = False,
                    force_validation: bool = False, stop_after_scoring: bool = False,
                    publish: bool = False) -> BatchReport:
    now = now or int(time.time())
    http: HttpClient | None = None
    if apis is None:
        http = HttpClient(user_agent=cfg.http.user_agent, rate_per_s=cfg.http.rate_per_s,
                          max_retries=cfg.http.max_retries)
        apis = Apis(DataApi(http), GammaApi(http), ClobApi(http))
    report = BatchReport()
    blocklist = Blocklist(cfg.blocklist)
    try:
        with Store(cfg.path(cfg.paths.research_db)) as store:
            wallets = await discover_universe(apis, store, cfg, now)
            report.wallets_scanned = len(wallets)
            failures = await refresh_positions(apis, store, wallets, cfg.http.concurrency, now)
            await refresh_markets(apis, store, now)

            frame = store.positions_frame()
            complete = frame[frame["complete"].astype(bool)]
            report.wallets_complete = int(complete["wallet"].nunique())
            eligible = prepare_positions(frame, cfg.scoring, cfg.categories, blocklist)
            flags = wallet_flags(complete[FLAG_COLUMNS], cfg.scoring)
            scores = score_wallets(eligible, flags, cfg.scoring, as_of=now)
            store.replace_scores(scores)
            write_parquet_atomic(scores, cfg.path(cfg.paths.scores_parquet))
            certified = scores[scores["certified"].astype(bool)]
            report.tests = len(scores)
            report.testable = int(((scores["n_eff"] >= cfg.scoring.min_n_eff) & (scores["flags"] == "")).sum())
            report.certified_wallets = int(certified["wallet"].nunique())
            report.top = certified.sort_values("post_edge", ascending=False).head(25)
            report.api_errors = failures + (http.errors if http else 0) + apis.skipped()
            log.info("scored %d tests, %d certified wallets", report.tests, report.certified_wallets)
            if stop_after_scoring:
                return report

            signals, contacts = await build_signals(apis, store, cfg, scores, blocklist, now)
            validation = None
            if not skip_validation:
                validation = await maybe_validate(apis, store, cfg, eligible, flags, scores, blocklist, now,
                                                  force=force_validation)
                report.validation_ran = validation is not None
            report.signals, report.contacts = len(signals), len(contacts)
            report.api_errors = failures + (http.errors if http else 0) + apis.skipped()

            names = {w: s.name for w, s in store.wallet_state().items()}
            out = cfg.path(cfg.paths.snapshot_dir)
            write_json_atomic(out / "signals.json", signals)
            write_json_atomic(out / "contacts.json", contacts)
            write_json_atomic(out / "whales.json", whales_json(scores, eligible, names))
            if validation is not None:
                write_json_atomic(out / "validation.json", validation)
            write_json_atomic(out / "meta.json", _meta_json(cfg, report, now, store.get_meta("validation_at")))
    finally:
        if http is not None:
            await http.aclose()
    if publish:
        git_publish(cfg.path(cfg.paths.snapshot_dir), now)
    return report
```

- [ ] **Step 5: Implement the CLI**

`src/whalescan/cli.py`:
```python
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
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    cfg = load_config(args.config)
    if args.max_wallets:
        cfg = replace(cfg, universe=replace(cfg.universe, max_wallets=args.max_wallets))
    try:
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
```

- [ ] **Step 6: Run the tests**

Run: `uv run pytest -q` — Expected: the whole suite passes.

- [ ] **Step 7: First real run (network, ~10–20 min)**

Run: `uv run whalescan batch --max-wallets 300 --force-validation`
Expected: log lines for universe, positions, markets, scoring; final summary line; `ls data/snapshot` shows `meta.json signals.json contacts.json whales.json validation.json`. Then `uv run python -c "import json; print(json.load(open('data/snapshot/meta.json'))['counts'])"` prints non-zero `wallets_scanned` and `tests`. Record the counts in the commit message body.

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "feat: batch snapshot pipeline, strict JSON writer and whalescan CLI"
```

---

### Task 14: Dashboard (snapshot mode)

**Files:**
- Create: `web/package.json`, `web/tsconfig.json`, `web/vite.config.ts`, `web/index.html`, `web/scripts/copy-data.mjs`
- Create: `web/src/main.tsx`, `web/src/App.tsx`, `web/src/styles.css`, `web/src/types.ts`, `web/src/format.ts`, `web/src/data.ts`
- Create: `web/src/components/{Panel,StatusBar,SignalsPanel,ContactsPanel,DossierPanel,BookPanel,PriceChart,ValidationPanel}.tsx`
- Test: `web/src/format.test.ts`, `web/src/data.test.ts`

**Interfaces:**
- Consumes: snapshot JSON (Task 13) from `./data/*.json` relative to the page.
- Produces: `npm run build` → static `web/dist/` with `web/dist/data/*.json` copied in.

- [ ] **Step 1: Create the project skeleton and install dependencies**

`web/package.json`:
```json
{
  "name": "whalescan-web",
  "private": true,
  "version": "0.1.0",
  "type": "module",
  "scripts": {
    "copy-data": "node scripts/copy-data.mjs",
    "predev": "npm run copy-data",
    "dev": "vite",
    "prebuild": "npm run copy-data",
    "build": "tsc --noEmit && vite build",
    "test": "vitest run"
  }
}
```

Run:
```bash
cd web
npm install react react-dom lightweight-charts @fontsource/ibm-plex-mono
npm install -D vite @vitejs/plugin-react typescript @types/react @types/react-dom vitest @types/node
cd ..
```
Expected: `package.json` gains `dependencies`/`devDependencies`; `package-lock.json` is created. Confirm `lightweight-charts` major version is 5 (`npm ls lightweight-charts`); the chart code below uses the v5 API (`addSeries(LineSeries)`, `createSeriesMarkers`).

`web/tsconfig.json`:
```json
{
  "compilerOptions": {
    "target": "ES2022",
    "lib": ["ES2022", "DOM", "DOM.Iterable"],
    "module": "ESNext",
    "moduleResolution": "bundler",
    "jsx": "react-jsx",
    "strict": true,
    "noUnusedLocals": true,
    "noUnusedParameters": true,
    "noFallthroughCasesInSwitch": true,
    "skipLibCheck": true,
    "isolatedModules": true,
    "noEmit": true,
    "types": ["vite/client"]
  },
  "include": ["src"]
}
```

`web/vite.config.ts`:
```ts
/// <reference types="vitest/config" />
import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

// base './' makes the build work under https://<user>.github.io/whalescan/
export default defineConfig({ base: './', plugins: [react()], test: { environment: 'node' } });
```

`web/index.html`:
```html
<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <meta name="color-scheme" content="dark" />
    <title>WHALESCAN</title>
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/main.tsx"></script>
  </body>
</html>
```

`web/scripts/copy-data.mjs`:
```js
// Copies ../data/snapshot into public/data so both `vite dev` and `vite build` serve it at ./data/.
import { cpSync, existsSync, mkdirSync, rmSync } from 'node:fs';

const src = new URL('../../data/snapshot/', import.meta.url);
const dst = new URL('../public/data/', import.meta.url);
rmSync(dst, { recursive: true, force: true });
mkdirSync(dst, { recursive: true });
if (existsSync(src)) {
  cpSync(src, dst, { recursive: true });
  console.log('copied data/snapshot -> web/public/data');
} else {
  console.warn('no data/snapshot yet — the dashboard will show NO SNAPSHOT DATA');
}
```

- [ ] **Step 2: Write the types (mirror of Task 13 JSON)**

`web/src/types.ts`:
```ts
export type Status = 'SIGNAL' | 'REJECTED' | 'EXIT' | 'CONFLICT' | 'EXPIRED';

export interface Check { code: string; passed: boolean; detail: string }

export interface Quote {
  vwap: number | null;
  complete: boolean;
  book_as_of: number;
  best_bid: number | null;
  best_ask: number | null;
  microprice: number | null;
  levels: [number, number][];
}

export interface Signal {
  id: string;
  status: Status;
  tier: 'A' | 'B' | null;
  category: string;
  wallet: string;
  wallet_name: string;
  question: string;
  market_slug: string;
  event_slug: string;
  end_ts: number | null;
  outcome: string;
  side: 'BUY' | 'SELL';
  price: number;
  usdc: number;
  shares: number;
  first_ts: number;
  last_ts: number;
  n_fills: number;
  post_edge: number | null;
  net_edge: number | null;
  max_entry: number | null;
  fee: number | null;
  quote: Quote | null;
  consensus: string[];
  checks: Check[];
  history: { t: number; p: number }[] | null;
}

export interface CategoryScore {
  category: string;
  n: number;
  n_eff: number;
  edge: number;
  post_edge: number;
  p_value: number;
  bh_pass: boolean;
  certified: boolean;
}

export interface Whale {
  wallet: string;
  name: string;
  certified: boolean;
  median_stake: number | null;
  flags: string;
  best_category: string | null;
  best_post_edge: number | null;
  categories: CategoryScore[];
  recent: { title: string; outcome: string; price: number; won: boolean; stake: number; closed_ts: number }[];
}

export interface GroupStats { n: number; mean_ret: number | null; hit_rate: number | null; t_stat: number | null }

export interface Validation {
  generated_at: number;
  verdict: 'INSUFFICIENT DATA' | 'EDGE CONFIRMED' | 'EDGE NOT CONFIRMED';
  folds: { cutoff: number; end: number; signals: number; mean_ret: number | null }[];
  groups: { A: GroupStats; B: GroupStats; SIGNALS: GroupStats; BASELINE: GroupStats };
  calibration: { lo: number; hi: number; n: number; predicted: number | null; realized: number | null }[];
  params: { fold_days: number; min_history_days: number; slippage: number; n_sims: number };
  caveats: string[];
}

export interface Meta {
  generated_at: number;
  version: string;
  mode: 'SNAPSHOT' | 'LIVE';
  counts: {
    wallets_scanned: number; wallets_complete: number; tests: number; testable: number;
    certified_wallets: number; signals: number; contacts: number;
  };
  params: { bh_q: number; min_usdc: number; conviction_k: number; follow_size_usdc: number; min_net_edge: number; signal_lookback_h: number };
  errors: { api: number };
  validation_generated_at: number | null;
}

export interface Snapshot { meta: Meta; signals: Signal[]; contacts: Signal[]; whales: Whale[]; validation: Validation | null }
```

- [ ] **Step 3: Write the failing tests**

`web/src/format.test.ts`:
```ts
import { describe, expect, it } from 'vitest';
import { fmtAge, fmtCents, fmtP, fmtPrice, fmtUsd, shortWallet, signalsEmptyMessage } from './format';
import type { Meta } from './types';

const meta = (certified: number): Meta => ({
  generated_at: 0, version: '0.1.0', mode: 'SNAPSHOT',
  counts: { wallets_scanned: 10, wallets_complete: 10, tests: 20, testable: 5, certified_wallets: certified, signals: 0, contacts: 0 },
  params: { bh_q: 0.1, min_usdc: 5000, conviction_k: 2, follow_size_usdc: 1000, min_net_edge: 0.02, signal_lookback_h: 24 },
  errors: { api: 0 }, validation_generated_at: null,
});

describe('format', () => {
  it('formats money, prices and edges', () => {
    expect(fmtUsd(20_000)).toBe('$20.0K');
    expect(fmtUsd(2_500_000)).toBe('$2.50M');
    expect(fmtUsd(null)).toBe('—');
    expect(fmtPrice(0.4)).toBe('0.400');
    expect(fmtCents(0.042)).toBe('+4.2¢');
    expect(fmtCents(-0.01)).toBe('−1.0¢');
    expect(fmtP(0.00001)).toBe('1.0E-5');
    expect(fmtP(0.034)).toBe('0.034');
  });

  it('formats ages like a HUD', () => {
    expect(fmtAge(42)).toBe('42S');
    expect(fmtAge(125)).toBe('2M');
    expect(fmtAge(3 * 3600 + 12 * 60)).toBe('3H12M');
    expect(fmtAge(5 * 86400)).toBe('5D');
  });

  it('shortens wallets', () => {
    expect(shortWallet('0x5268527977f700f9bf9b6d5cd843859e4e70135d')).toBe('0x5268…135d');
  });

  it('explains an empty signal board', () => {
    expect(signalsEmptyMessage(meta(0))).toMatch(/NO CERTIFIED WHALES YET/);
    expect(signalsEmptyMessage(meta(3))).toBe('NO SIGNALS · 3 CERTIFIED WHALES UNDER WATCH');
    expect(signalsEmptyMessage(meta(1))).toBe('NO SIGNALS · 1 CERTIFIED WHALE UNDER WATCH');
  });
});
```

`web/src/data.test.ts`:
```ts
import { describe, expect, it } from 'vitest';
import { loadSnapshot } from './data';

function fakeFetch(files: Record<string, unknown>): typeof fetch {
  return (async (input: RequestInfo | URL) => {
    const name = String(input).split('/').pop() ?? '';
    return name in files
      ? new Response(JSON.stringify(files[name]), { status: 200 })
      : new Response('', { status: 404 });
  }) as typeof fetch;
}

describe('loadSnapshot', () => {
  it('treats validation as optional', async () => {
    const snap = await loadSnapshot('./data', fakeFetch({
      'meta.json': { generated_at: 1 }, 'signals.json': [], 'contacts.json': [], 'whales.json': [],
    }));
    expect(snap.validation).toBeNull();
    expect(snap.meta.generated_at).toBe(1);
  });

  it('fails clearly when no snapshot exists', async () => {
    await expect(loadSnapshot('./data', fakeFetch({}))).rejects.toThrow(/NO SNAPSHOT DATA/);
  });

  it('surfaces server errors', async () => {
    const broken = (async () => new Response('', { status: 500 })) as typeof fetch;
    await expect(loadSnapshot('./data', broken)).rejects.toThrow(/HTTP 500/);
  });
});
```

- [ ] **Step 4: Run to verify failure**

Run: `cd web && npm test; cd ..` — Expected: FAIL, cannot resolve `./format` and `./data`.

- [ ] **Step 5: Implement formatting and data loading**

`web/src/format.ts`:
```ts
import type { Meta } from './types';

type Num = number | null | undefined;

export const fmtUsd = (n: Num): string =>
  n == null ? '—' : n >= 1e6 ? `$${(n / 1e6).toFixed(2)}M` : n >= 1e3 ? `$${(n / 1e3).toFixed(1)}K` : `$${n.toFixed(0)}`;

export const fmtPrice = (p: Num): string => (p == null ? '—' : p.toFixed(3));

export const fmtCents = (e: Num): string =>
  e == null ? '—' : `${e >= 0 ? '+' : '−'}${Math.abs(e * 100).toFixed(1)}¢`;

export const fmtP = (p: Num): string =>
  p == null ? '—' : p < 1e-3 ? p.toExponential(1).toUpperCase() : p.toFixed(3);

export const fmtPct = (x: Num): string => (x == null ? '—' : `${(x * 100).toFixed(0)}%`);

export function fmtAge(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  if (s < 60) return `${s}S`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}M`;
  const h = Math.floor(m / 60);
  if (h < 48) return `${h}H${String(m % 60).padStart(2, '0')}M`;
  return `${Math.floor(h / 24)}D`;
}

export const fmtUtc = (ts: number): string => `${new Date(ts * 1000).toISOString().slice(11, 16)}Z`;

export const fmtDate = (ts: number): string => new Date(ts * 1000).toISOString().slice(0, 10);

export const shortWallet = (w: string): string => `${w.slice(0, 6)}…${w.slice(-4)}`;

export function signalsEmptyMessage(meta: Meta): string {
  const c = meta.counts.certified_wallets;
  if (c === 0) return 'NO CERTIFIED WHALES YET — SCORING NEEDS MORE RESOLVED HISTORY';
  return `NO SIGNALS · ${c} CERTIFIED WHALE${c === 1 ? '' : 'S'} UNDER WATCH`;
}
```

`web/src/data.ts`:
```ts
import type { Meta, Signal, Snapshot, Validation, Whale } from './types';

async function getJson<T>(url: string, fetcher: typeof fetch): Promise<T | null> {
  const r = await fetcher(url, { cache: 'no-store' });
  if (r.status === 404) return null;
  if (!r.ok) throw new Error(`${url}: HTTP ${r.status}`);
  return (await r.json()) as T;
}

export async function loadSnapshot(base = './data', fetcher: typeof fetch = fetch): Promise<Snapshot> {
  const [meta, signals, contacts, whales, validation] = await Promise.all([
    getJson<Meta>(`${base}/meta.json`, fetcher),
    getJson<Signal[]>(`${base}/signals.json`, fetcher),
    getJson<Signal[]>(`${base}/contacts.json`, fetcher),
    getJson<Whale[]>(`${base}/whales.json`, fetcher),
    getJson<Validation>(`${base}/validation.json`, fetcher),
  ]);
  if (!meta) throw new Error('NO SNAPSHOT DATA — RUN `whalescan batch` FIRST');
  return { meta, signals: signals ?? [], contacts: contacts ?? [], whales: whales ?? [], validation };
}
```

- [ ] **Step 6: Run the unit tests**

Run: `cd web && npm test; cd ..` — Expected: 7 tests pass.

- [ ] **Step 7: Implement the styles**

`web/src/styles.css`:
```css
:root {
  --bg: #07090b;
  --panel: #0c1014;
  --line: #1c242c;
  --text: #c9d1d9;
  --dim: #6b7785;
  --amber: #ffb000;
  --green: #3ddc84;
  --red: #ff4d4d;
  --cyan: #4fc3f7;
  --font: 'IBM Plex Mono', ui-monospace, monospace;
  color-scheme: dark;
}

* { box-sizing: border-box; }
html, body, #root { height: 100%; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font: 400 12px/1.45 var(--font);
  font-variant-numeric: tabular-nums;
  letter-spacing: 0.02em;
  -webkit-font-smoothing: antialiased;
}
button, select { font: inherit; color: inherit; }
b { font-weight: 600; }
.dim { color: var(--dim); }
.green { color: var(--green); }
.amber { color: var(--amber); }
.red { color: var(--red); }
.cyan { color: var(--cyan); }
.right { margin-left: auto; }

.statusbar {
  display: flex; align-items: center; gap: 24px; height: 44px; padding: 0 16px;
  border-bottom: 1px solid var(--line); background: var(--panel);
  text-transform: uppercase; letter-spacing: 0.08em; white-space: nowrap; overflow-x: auto;
}
.brand { font-weight: 600; color: var(--text); }
.status-items { display: flex; gap: 20px; color: var(--dim); }
.status-items b { color: var(--text); }
.badge.ok { color: var(--green); }
.badge.warn { color: var(--amber); }

.grid {
  display: grid; gap: 1px; background: var(--line);
  grid-template-columns: minmax(320px, 1fr) minmax(0, 1.6fr) minmax(300px, 1fr);
  grid-template-rows: minmax(0, 1.25fr) minmax(0, 1fr);
  grid-template-areas: "signals book dossier" "signals contacts validation";
  height: calc(100vh - 44px);
}
.area-signals { grid-area: signals; }
.area-book { grid-area: book; }
.area-dossier { grid-area: dossier; }
.area-contacts { grid-area: contacts; }
.area-validation { grid-area: validation; }

@media (max-width: 1100px) {
  .grid {
    grid-template-columns: minmax(0, 1fr); grid-template-rows: none; height: auto;
    grid-template-areas: "signals" "book" "dossier" "contacts" "validation";
  }
  .panel { min-height: 320px; }
}

.panel { position: relative; display: flex; flex-direction: column; min-height: 0; background: var(--panel); }
.panel::before, .panel::after {
  content: ''; position: absolute; width: 8px; height: 8px; border-color: var(--dim); border-style: solid; pointer-events: none;
}
.panel::before { top: 4px; left: 4px; border-width: 1px 0 0 1px; }
.panel::after { bottom: 4px; right: 4px; border-width: 0 1px 1px 0; }
.panel-head {
  display: flex; align-items: center; gap: 10px; padding: 10px 16px; border-bottom: 1px solid var(--line);
  text-transform: uppercase; letter-spacing: 0.08em;
}
.panel-head h2 { margin: 0; font-size: 12px; font-weight: 600; }
.panel-code { color: var(--dim); }
.panel-right { margin-left: auto; color: var(--dim); }
.panel-body { flex: 1; min-height: 0; overflow: auto; padding: 12px 16px; }
.panel select { background: var(--bg); border: 1px solid var(--line); padding: 2px 6px; text-transform: uppercase; }
.empty { color: var(--dim); text-transform: uppercase; letter-spacing: 0.08em; margin: 24px 0; text-align: center; }

.signal-list { list-style: none; margin: 0; padding: 0; display: grid; gap: 8px; }
.signal {
  display: grid; gap: 6px; width: 100%; text-align: left; padding: 10px 12px; cursor: pointer;
  background: var(--bg); border: 1px solid var(--line); border-left: 3px solid var(--amber);
}
.signal.tier-A { border-left-color: var(--green); }
.signal:hover, .signal.selected { border-color: var(--cyan); }
.signal .row { display: flex; gap: 12px; align-items: baseline; text-transform: uppercase; }
.signal .question { font-weight: 500; color: var(--text); }
.tier { font-weight: 600; letter-spacing: 0.08em; }
.tier.tier-A { color: var(--green); }
.tier.tier-B { color: var(--amber); }
.kv { display: grid; grid-template-columns: repeat(4, auto); gap: 2px 12px; margin: 0; text-transform: uppercase; }
.kv dt { color: var(--dim); font-size: 10px; letter-spacing: 0.08em; }
.kv dd { margin: 0; grid-row: 2; }
.stale { color: var(--amber); font-size: 10px; letter-spacing: 0.08em; }

table { width: 100%; border-collapse: collapse; }
th {
  text-align: left; color: var(--dim); font-weight: 400; font-size: 10px; letter-spacing: 0.08em;
  text-transform: uppercase; padding: 4px 8px 6px 0; border-bottom: 1px solid var(--line); position: sticky; top: 0; background: var(--panel);
}
td { padding: 5px 8px 5px 0; border-bottom: 1px solid var(--line); vertical-align: top; }
td.num, th.num { text-align: right; }
.link { background: none; border: 0; padding: 0; color: var(--cyan); cursor: pointer; }
.truncate { max-width: 260px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.gate-fail { color: var(--dim); text-transform: uppercase; }

.chart { height: 240px; }
.ladder { display: grid; gap: 2px; margin-top: 12px; }
.ladder-row { display: grid; grid-template-columns: 64px 1fr 80px; gap: 8px; align-items: center; }
.ladder-bar { height: 10px; background: rgba(255, 77, 77, 0.35); }
.ladder-bar.filled { background: var(--red); }
.stats { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; margin: 12px 0; text-transform: uppercase; }
.stat { border: 1px solid var(--line); padding: 6px 8px; }
.stat span { display: block; color: var(--dim); font-size: 10px; letter-spacing: 0.08em; }

.verdict { padding: 8px 12px; margin-bottom: 12px; font-weight: 600; letter-spacing: 0.1em; border: 1px solid; }
.verdict.ok { color: var(--green); border-color: var(--green); }
.verdict.bad { color: var(--red); border-color: var(--red); }
.verdict.wait { color: var(--amber); border-color: var(--amber); }
.caveats { color: var(--dim); font-size: 11px; padding-left: 16px; }
.calibration { width: 100%; max-width: 260px; height: auto; display: block; margin: 12px 0; }

.boot { display: grid; place-items: center; height: 100%; letter-spacing: 0.2em; color: var(--dim); text-transform: uppercase; }
.boot .red { max-width: 560px; text-align: center; }
```

- [ ] **Step 8: Implement the components**

`web/src/components/Panel.tsx`:
```tsx
import type { ReactNode } from 'react';

interface Props { code: string; title: string; right?: ReactNode; className?: string; children: ReactNode }

export function Panel({ code, title, right, className = '', children }: Props) {
  return (
    <section className={`panel ${className}`}>
      <header className="panel-head">
        <span className="panel-code">{code}</span>
        <h2>{title}</h2>
        <div className="panel-right">{right}</div>
      </header>
      <div className="panel-body">{children}</div>
    </section>
  );
}
```

`web/src/components/StatusBar.tsx`:
```tsx
import { fmtAge, fmtUtc } from '../format';
import type { Meta } from '../types';

export function StatusBar({ meta, now }: { meta: Meta; now: number }) {
  const age = now - meta.generated_at;
  return (
    <header className="statusbar">
      <div className="brand">WHALESCAN <span className="dim">// POLYMARKET INTELLIGENCE</span></div>
      <div className="status-items">
        <span className={`badge ${age > 8 * 3600 ? 'warn' : 'ok'}`}>
          ● {meta.mode} · {fmtUtc(meta.generated_at)} · {fmtAge(age)} AGO
        </span>
        <span>SCANNED <b>{meta.counts.wallets_scanned}</b></span>
        <span>TESTABLE <b>{meta.counts.testable}</b></span>
        <span>CERTIFIED <b className="green">{meta.counts.certified_wallets}</b></span>
        <span>SIGNALS <b className="amber">{meta.counts.signals}</b></span>
        <span>FDR q=<b>{meta.params.bh_q}</b></span>
        {meta.errors.api > 0 && <span className="red">API ERR {meta.errors.api}</span>}
      </div>
    </header>
  );
}
```

`web/src/components/SignalsPanel.tsx`:
```tsx
import type { RefObject } from 'react';
import { fmtAge, fmtCents, fmtPrice, fmtUsd, signalsEmptyMessage } from '../format';
import type { Meta, Signal } from '../types';
import { Panel } from './Panel';

interface Props {
  signals: Signal[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  meta: Meta;
  now: number;
  categories: string[];
  category: string;
  onCategory: (c: string) => void;
  filterRef: RefObject<HTMLSelectElement | null>;
}

export function SignalsPanel({ signals, selectedId, onSelect, meta, now, categories, category, onCategory, filterRef }: Props) {
  const filter = (
    <select ref={filterRef} value={category} onChange={(e) => onCategory(e.target.value)} aria-label="Category filter">
      {['ALL', ...categories].map((c) => <option key={c}>{c}</option>)}
    </select>
  );
  return (
    <Panel code="01" title="SIGNALS" className="area-signals" right={filter}>
      {signals.length === 0 ? (
        <p className="empty">{signalsEmptyMessage(meta)}</p>
      ) : (
        <ul className="signal-list">
          {signals.map((s) => (
            <li key={s.id}>
              <SignalCard s={s} selected={s.id === selectedId} onSelect={() => onSelect(s.id)} now={now} />
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}

function SignalCard({ s, selected, onSelect, now }: { s: Signal; selected: boolean; onSelect: () => void; now: number }) {
  const bookAge = s.quote ? now - s.quote.book_as_of : null;
  return (
    <button className={`signal tier-${s.tier} ${selected ? 'selected' : ''}`} onClick={onSelect}>
      <div className="row">
        <span className={`tier tier-${s.tier}`}>TIER {s.tier}</span>
        <span className="dim">{s.category}</span>
        <span className="dim right">{fmtAge(now - s.last_ts)} AGO</span>
      </div>
      <div className="question">{s.question}</div>
      <div className="row">
        <span>BUY <b>{s.outcome.toUpperCase()}</b></span>
        <span>@ {fmtPrice(s.price)}</span>
        <span>{fmtUsd(s.usdc)}</span>
      </div>
      <dl className="kv">
        <dt>NET EDGE</dt><dd className="green">{fmtCents(s.net_edge)}</dd>
        <dt>MAX ENTRY</dt><dd className="amber">{fmtPrice(s.max_entry)}</dd>
        <dt>FOLLOW</dt><dd>{fmtPrice(s.quote?.vwap)}</dd>
        <dt>WHALES</dt><dd>{s.consensus.length}</dd>
      </dl>
      {bookAge != null && bookAge > 3600 && <div className="stale">BOOK {fmtAge(bookAge)} OLD — RE-CHECK PRICE BEFORE ENTRY</div>}
    </button>
  );
}
```

`web/src/components/ContactsPanel.tsx`:
```tsx
import { fmtAge, fmtPrice, fmtUsd, shortWallet } from '../format';
import type { Signal } from '../types';
import { Panel } from './Panel';

export function ContactsPanel({ contacts, now, onWallet }: { contacts: Signal[]; now: number; onWallet: (w: string) => void }) {
  return (
    <Panel code="04" title="CONTACTS" className="area-contacts" right={`${contacts.length} ≥ GATE FLOOR`}>
      {contacts.length === 0 ? (
        <p className="empty">NO LARGE POSITION EVENTS IN WINDOW</p>
      ) : (
        <table>
          <thead>
            <tr><th>AGE</th><th>WALLET</th><th>MARKET</th><th>SIDE</th><th className="num">SIZE</th><th className="num">PX</th><th>STATUS</th></tr>
          </thead>
          <tbody>
            {contacts.map((c) => {
              const fail = c.checks.find((k) => !k.passed);
              return (
                <tr key={c.id}>
                  <td className="dim">{fmtAge(now - c.last_ts)}</td>
                  <td><button className="link" onClick={() => onWallet(c.wallet)}>{c.wallet_name || shortWallet(c.wallet)}</button></td>
                  <td className="truncate" title={c.question}>{c.question}</td>
                  <td>{c.side} {c.outcome.toUpperCase()}</td>
                  <td className="num">{fmtUsd(c.usdc)}</td>
                  <td className="num">{fmtPrice(c.price)}</td>
                  <td className="gate-fail">
                    <span className={c.status === 'CONFLICT' ? 'red' : c.status === 'EXIT' ? 'cyan' : ''}>{c.status}</span>
                    {fail && c.status === 'REJECTED' && <> · {fail.code} {fail.detail}</>}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </Panel>
  );
}
```

`web/src/components/DossierPanel.tsx`:
```tsx
import { fmtCents, fmtDate, fmtP, fmtPrice, fmtUsd, shortWallet } from '../format';
import type { Whale } from '../types';
import { Panel } from './Panel';

export function DossierPanel({ whale, wallet }: { whale: Whale | null; wallet: string | null }) {
  if (!whale) {
    return (
      <Panel code="03" title="DOSSIER" className="area-dossier">
        <p className="empty">{wallet ? `NO DOSSIER FOR ${shortWallet(wallet)} — NOT IN SCORED SET` : 'SELECT A SIGNAL OR CONTACT'}</p>
      </Panel>
    );
  }
  const status = whale.certified ? <span className="green">CERTIFIED</span> : <span className="amber">WATCHLIST</span>;
  return (
    <Panel code="03" title="DOSSIER" className="area-dossier" right={status}>
      <div className="row">
        <b>{whale.name || shortWallet(whale.wallet)}</b>{' '}
        <a className="link" href={`https://polymarket.com/profile/${whale.wallet}`} target="_blank" rel="noreferrer">{shortWallet(whale.wallet)} ↗</a>
      </div>
      <div className="stats">
        <div className="stat"><span>BEST CAT</span>{whale.best_category ?? '—'}</div>
        <div className="stat"><span>POST EDGE</span>{fmtCents(whale.best_post_edge)}</div>
        <div className="stat"><span>MEDIAN BET</span>{fmtUsd(whale.median_stake)}</div>
        <div className="stat"><span>FLAGS</span>{whale.flags || 'NONE'}</div>
      </div>
      <table>
        <thead>
          <tr><th>CATEGORY</th><th className="num">N</th><th className="num">EDGE</th><th className="num">POST</th><th className="num">P</th><th>FDR</th></tr>
        </thead>
        <tbody>
          {whale.categories.map((c) => (
            <tr key={c.category}>
              <td>{c.category}</td>
              <td className="num">{c.n}</td>
              <td className="num">{fmtCents(c.edge)}</td>
              <td className="num">{fmtCents(c.post_edge)}</td>
              <td className="num">{fmtP(c.p_value)}</td>
              <td className={c.certified ? 'green' : c.bh_pass ? 'amber' : 'dim'}>{c.certified ? 'CERT' : c.bh_pass ? 'PASS' : '—'}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <h3 className="dim" style={{ fontSize: 10, letterSpacing: '0.08em', margin: '16px 0 4px' }}>RECENT RESOLVED</h3>
      <table>
        <tbody>
          {whale.recent.map((r, i) => (
            <tr key={i}>
              <td className={r.won ? 'green' : 'red'}>{r.won ? 'W' : 'L'}</td>
              <td className="truncate" title={r.title}>{r.title}</td>
              <td>{r.outcome.toUpperCase()}</td>
              <td className="num">{fmtPrice(r.price)}</td>
              <td className="num">{fmtUsd(r.stake)}</td>
              <td className="dim">{fmtDate(r.closed_ts)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </Panel>
  );
}
```

`web/src/components/PriceChart.tsx`:
```tsx
import { ColorType, LineSeries, LineStyle, createChart, createSeriesMarkers, type UTCTimestamp } from 'lightweight-charts';
import { useEffect, useMemo, useRef } from 'react';

interface Props { history: { t: number; p: number }[]; entryTs: number; entryPrice: number; maxEntry: number | null }

function dedupe(history: { t: number; p: number }[]) {
  const out: { time: UTCTimestamp; value: number }[] = [];
  let last = -Infinity;
  for (const h of [...history].sort((a, b) => a.t - b.t)) {
    if (h.t > last) {
      out.push({ time: h.t as UTCTimestamp, value: h.p });
      last = h.t;
    }
  }
  return out;
}

export function PriceChart({ history, entryTs, entryPrice, maxEntry }: Props) {
  const ref = useRef<HTMLDivElement>(null);
  const points = useMemo(() => dedupe(history), [history]);

  useEffect(() => {
    if (!ref.current || points.length < 2) return;
    const chart = createChart(ref.current, {
      autoSize: true,
      layout: { background: { type: ColorType.Solid, color: 'transparent' }, textColor: '#6B7785', fontFamily: "'IBM Plex Mono', monospace", fontSize: 11 },
      grid: { vertLines: { color: '#1C242C' }, horzLines: { color: '#1C242C' } },
      rightPriceScale: { borderColor: '#1C242C' },
      timeScale: { borderColor: '#1C242C', timeVisible: true },
      crosshair: { vertLine: { color: '#4FC3F7' }, horzLine: { color: '#4FC3F7' } },
    });
    const series = chart.addSeries(LineSeries, { color: '#4FC3F7', lineWidth: 2, priceFormat: { type: 'price', precision: 3, minMove: 0.001 } });
    series.setData(points);
    const nearest = points.reduce((best, p) => (Math.abs(p.time - entryTs) < Math.abs(best.time - entryTs) ? p : best), points[0]);
    createSeriesMarkers(series, [{ time: nearest.time, position: 'belowBar', color: '#FFB000', shape: 'arrowUp', text: `WHALE ${entryPrice.toFixed(3)}` }]);
    if (maxEntry != null) {
      series.createPriceLine({ price: maxEntry, color: '#3DDC84', lineStyle: LineStyle.Dashed, lineWidth: 1, axisLabelVisible: true, title: 'MAX ENTRY' });
    }
    chart.timeScale().fitContent();
    return () => chart.remove();
  }, [points, entryTs, entryPrice, maxEntry]);

  if (points.length < 2) return <p className="empty">NO PRICE HISTORY</p>;
  return <div className="chart" ref={ref} />;
}
```

`web/src/components/BookPanel.tsx`:
```tsx
import { useMemo } from 'react';
import { fmtAge, fmtPrice, fmtUsd } from '../format';
import type { Signal } from '../types';
import { Panel } from './Panel';
import { PriceChart } from './PriceChart';

export function BookPanel({ signal, now, followSize }: { signal: Signal | null; now: number; followSize: number }) {
  const history = useMemo(() => signal?.history ?? [], [signal]);
  if (!signal) {
    return (
      <Panel code="02" title="BOOK" className="area-book">
        <p className="empty">NO SIGNAL SELECTED</p>
      </Panel>
    );
  }
  const q = signal.quote;
  let cumulative = 0;
  const maxUsd = Math.max(1, ...(q?.levels ?? []).map(([p, s]) => p * s));
  return (
    <Panel code="02" title="BOOK" className="area-book" right={q ? `AS OF ${fmtAge(now - q.book_as_of)} AGO` : 'NO BOOK'}>
      <div className="question" style={{ marginBottom: 8 }}>{signal.question}</div>
      <PriceChart history={history} entryTs={signal.first_ts} entryPrice={signal.price} maxEntry={signal.max_entry} />
      <div className="stats">
        <div className="stat"><span>BID</span>{fmtPrice(q?.best_bid)}</div>
        <div className="stat"><span>ASK</span>{fmtPrice(q?.best_ask)}</div>
        <div className="stat"><span>MICRO</span>{fmtPrice(q?.microprice)}</div>
        <div className="stat"><span>FOLLOW {fmtUsd(followSize)}</span>{fmtPrice(q?.vwap)}</div>
      </div>
      <div className="ladder" aria-label="Ask ladder">
        {(q?.levels ?? []).map(([price, size]) => {
          const usd = price * size;
          const filled = cumulative < followSize;
          cumulative += usd;
          return (
            <div className="ladder-row" key={price}>
              <span className="red">{fmtPrice(price)}</span>
              <div className={`ladder-bar ${filled ? 'filled' : ''}`} style={{ width: `${(usd / maxUsd) * 100}%` }} />
              <span className="dim" style={{ textAlign: 'right' }}>{fmtUsd(usd)}</span>
            </div>
          );
        })}
      </div>
      <table style={{ marginTop: 12 }}>
        <tbody>
          {signal.checks.map((c) => (
            <tr key={c.code}>
              <td className={c.passed ? 'green' : 'red'}>{c.passed ? '■' : '□'} {c.code}</td>
              <td className="dim">{c.detail}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </Panel>
  );
}
```

`web/src/components/ValidationPanel.tsx`:
```tsx
import { fmtCents, fmtDate, fmtPct } from '../format';
import type { Validation } from '../types';
import { Panel } from './Panel';

function Calibration({ bins }: { bins: Validation['calibration'] }) {
  const s = 200;
  const pts = bins.filter((b) => b.n > 0 && b.predicted != null && b.realized != null);
  return (
    <svg className="calibration" viewBox={`0 0 ${s} ${s}`} role="img" aria-label="Calibration: predicted vs realized win rate">
      <rect x="0" y="0" width={s} height={s} fill="none" stroke="#1C242C" />
      <line x1="0" y1={s} x2={s} y2="0" stroke="#6B7785" strokeDasharray="4 4" />
      {pts.map((b) => (
        <circle key={b.lo} cx={b.predicted! * s} cy={s - b.realized! * s} r={3 + Math.min(8, Math.sqrt(b.n))} fill="#4FC3F7" fillOpacity="0.7" />
      ))}
      <text x="4" y="12" fill="#6B7785" fontSize="9">REALIZED</text>
      <text x={s - 4} y={s - 4} fill="#6B7785" fontSize="9" textAnchor="end">PREDICTED</text>
    </svg>
  );
}

export function ValidationPanel({ validation }: { validation: Validation | null }) {
  if (!validation) {
    return (
      <Panel code="05" title="VALIDATION" className="area-validation">
        <p className="empty">VALIDATION NOT RUN YET</p>
      </Panel>
    );
  }
  const cls = validation.verdict === 'EDGE CONFIRMED' ? 'ok' : validation.verdict === 'EDGE NOT CONFIRMED' ? 'bad' : 'wait';
  const rows = [['TIER A', validation.groups.A], ['TIER B', validation.groups.B], ['ALL SIGNALS', validation.groups.SIGNALS], ['BASELINE ≥ FLOOR', validation.groups.BASELINE]] as const;
  return (
    <Panel code="05" title="VALIDATION" className="area-validation" right={`WALK-FORWARD · ${fmtDate(validation.generated_at)}`}>
      <div className={`verdict ${cls}`}>{validation.verdict}</div>
      <table>
        <thead>
          <tr><th>GROUP</th><th className="num">N</th><th className="num">RET/$</th><th className="num">HIT</th><th className="num">T</th></tr>
        </thead>
        <tbody>
          {rows.map(([label, g]) => (
            <tr key={label}>
              <td>{label}</td>
              <td className="num">{g.n}</td>
              <td className={`num ${g.mean_ret != null && g.mean_ret > 0 ? 'green' : 'red'}`}>{fmtCents(g.mean_ret)}</td>
              <td className="num">{fmtPct(g.hit_rate)}</td>
              <td className="num">{g.t_stat == null ? '—' : g.t_stat.toFixed(2)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <Calibration bins={validation.calibration} />
      <ul className="caveats">
        {validation.caveats.map((c) => <li key={c}>{c}</li>)}
      </ul>
    </Panel>
  );
}
```

- [ ] **Step 9: Implement the app shell**

`web/src/App.tsx`:
```tsx
import { useEffect, useMemo, useRef, useState } from 'react';
import { BookPanel } from './components/BookPanel';
import { ContactsPanel } from './components/ContactsPanel';
import { DossierPanel } from './components/DossierPanel';
import { SignalsPanel } from './components/SignalsPanel';
import { StatusBar } from './components/StatusBar';
import { ValidationPanel } from './components/ValidationPanel';
import { loadSnapshot } from './data';
import type { Snapshot } from './types';

const nowSeconds = () => Math.floor(Date.now() / 1000);

export default function App() {
  const [snap, setSnap] = useState<Snapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [walletPick, setWalletPick] = useState<string | null>(null);
  const [category, setCategory] = useState('ALL');
  const [now, setNow] = useState(nowSeconds);
  const filterRef = useRef<HTMLSelectElement>(null);

  useEffect(() => {
    loadSnapshot().then(setSnap).catch((e: unknown) => setError(String(e instanceof Error ? e.message : e)));
    const t = setInterval(() => setNow(nowSeconds()), 1000);
    return () => clearInterval(t);
  }, []);

  const categories = useMemo(() => [...new Set((snap?.signals ?? []).map((s) => s.category))].sort(), [snap]);
  const signals = useMemo(
    () => (snap?.signals ?? []).filter((s) => category === 'ALL' || s.category === category),
    [snap, category],
  );
  const selected = signals.find((s) => s.id === selectedId) ?? signals[0] ?? null;
  const wallet = walletPick ?? selected?.wallet ?? null;
  const whale = snap?.whales.find((w) => w.wallet === wallet) ?? null;

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.target instanceof HTMLSelectElement) return;
      const i = selected ? signals.indexOf(selected) : -1;
      if (e.key === 'j' && i + 1 < signals.length) { setSelectedId(signals[i + 1].id); setWalletPick(null); }
      else if (e.key === 'k' && i > 0) { setSelectedId(signals[i - 1].id); setWalletPick(null); }
      else if (e.key === 'Enter' && selected) setWalletPick(selected.wallet);
      else if (e.key === '/') { e.preventDefault(); filterRef.current?.focus(); }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [signals, selected]);

  if (error) return <div className="boot"><div className="red">{error}</div></div>;
  if (!snap) return <div className="boot">ACQUIRING SNAPSHOT…</div>;

  return (
    <>
      <StatusBar meta={snap.meta} now={now} />
      <main className="grid">
        <SignalsPanel
          signals={signals} selectedId={selected?.id ?? null}
          onSelect={(id) => { setSelectedId(id); setWalletPick(null); }}
          meta={snap.meta} now={now} categories={categories} category={category} onCategory={setCategory} filterRef={filterRef}
        />
        <BookPanel signal={selected} now={now} followSize={snap.meta.params.follow_size_usdc} />
        <DossierPanel whale={whale} wallet={wallet} />
        <ContactsPanel contacts={snap.contacts} now={now} onWallet={setWalletPick} />
        <ValidationPanel validation={snap.validation} />
      </main>
    </>
  );
}
```

`web/src/main.tsx`:
```tsx
import '@fontsource/ibm-plex-mono/400.css';
import '@fontsource/ibm-plex-mono/500.css';
import '@fontsource/ibm-plex-mono/600.css';
import './styles.css';
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import App from './App';

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
```

- [ ] **Step 10: Typecheck, test, build**

Run: `cd web && npm test && npm run build; cd ..`
Expected: 7 tests pass; `tsc` reports no errors; `vite build` writes `web/dist/index.html`, assets, and `web/dist/data/*.json` (from Task 13 Step 7's snapshot).

- [ ] **Step 11: Look at it**

Run: `cd web && npm run dev` and open the printed URL (default `http://localhost:5173`).
Expected: dark HUD, IBM Plex Mono everywhere (confirm in devtools → Computed → font-family), status bar with counts matching `data/snapshot/meta.json`, five panels. If `signals.json` is empty the SIGNALS panel shows the `signalsEmptyMessage` text. Resize to 400px width: panels stack in one column with no horizontal page scroll. Stop the server with Ctrl-C.

- [ ] **Step 12: Commit**

```bash
git add -A
git commit -m "feat(web): military-style snapshot dashboard with signals, book, dossier, contacts and validation"
```

---

### Task 15: CI, scheduled snapshot + GitHub Pages, README

**Files:**
- Create: `.github/workflows/ci.yml`, `.github/workflows/snapshot.yml`, `README.md`

**Interfaces:**
- Consumes: `scripts/test-cpp.sh`, `uv run pytest`, `uv run whalescan batch`, `web` npm scripts.
- Produces: green CI on every push; a public dashboard at `https://<user>.github.io/whalescan/` refreshed every 6 hours.

- [ ] **Step 1: Write the CI workflow**

`.github/workflows/ci.yml`:
```yaml
name: ci
on:
  push:
  pull_request:
jobs:
  cpp:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: scripts/test-cpp.sh
  python:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v6
      - run: uv sync --locked
      - run: uv run pytest -q
  web:
    runs-on: ubuntu-latest
    defaults: { run: { working-directory: web } }
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with: { node-version: 22, cache: npm, cache-dependency-path: web/package-lock.json }
      - run: npm ci
      - run: npm test
      - run: npm run build
```

- [ ] **Step 2: Write the snapshot + Pages workflow**

`.github/workflows/snapshot.yml`:
```yaml
name: snapshot
on:
  schedule:
    - cron: "17 */6 * * *"
  workflow_dispatch:
    inputs:
      skip_batch:
        description: "Only rebuild and deploy the dashboard"
        type: boolean
        default: false
  push:
    branches: [main]
    paths: ["web/**"]
permissions:
  contents: write
  pages: write
  id-token: write
concurrency:
  group: snapshot
  cancel-in-progress: false
jobs:
  build:
    runs-on: ubuntu-latest
    timeout-minutes: 330
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v6
      - name: Restore research database
        if: github.event_name != 'push' && !inputs.skip_batch
        uses: actions/cache/restore@v4
        with:
          path: data/research.duckdb
          key: research-${{ github.run_id }}
          restore-keys: research-
      - name: Run batch
        if: github.event_name != 'push' && !inputs.skip_batch
        run: |
          uv sync --locked
          uv run whalescan batch
      - name: Save research database
        if: always() && github.event_name != 'push' && !inputs.skip_batch
        uses: actions/cache/save@v4
        with:
          path: data/research.duckdb
          key: research-${{ github.run_id }}
      - name: Commit snapshot
        if: github.event_name != 'push' && !inputs.skip_batch
        run: |
          git config user.name "whalescan-bot"
          git config user.email "whalescan-bot@users.noreply.github.com"
          git add data/snapshot
          git diff --cached --quiet || (git commit -m "snapshot: $(date -u +%FT%H:%MZ)" && git push)
      - uses: actions/setup-node@v4
        with: { node-version: 22, cache: npm, cache-dependency-path: web/package-lock.json }
      - name: Build dashboard
        working-directory: web
        run: npm ci && npm run build
      - uses: actions/upload-pages-artifact@v3
        with: { path: web/dist }
  deploy:
    needs: build
    runs-on: ubuntu-latest
    environment:
      name: github-pages
      url: ${{ steps.deployment.outputs.page_url }}
    steps:
      - id: deployment
        uses: actions/deploy-pages@v4
```

- [ ] **Step 3: Write the README**

`README.md`:
````markdown
# WHALESCAN

**Statistically certified Polymarket whale scanner.** Finds wallets whose forecasting skill survives a
Monte Carlo significance test *and* a false-discovery-rate correction, watches their trades, and surfaces
only the few that are still worth following after the real cost of entry. Humans decide; the tool never trades.

Live dashboard: `https://<your-user>.github.io/whalescan/` (refreshed every 6 hours by GitHub Actions, €0).

## How it works

```
leaderboard + large trades ──▶ wallet universe
closed positions (sorted by time, full history) ──▶ DuckDB
resolved markets only (exact 1/0) ──▶ edge = Σw(y − p)/Σw
C++ Monte Carlo: simulate "no skill" (y* ~ Bernoulli(p)) 100k× ──▶ p-value
Benjamini–Hochberg @ q=0.10 + empirical-Bayes shrinkage ──▶ certified whales
recent trades ──▶ position events ──▶ gates G1–G7 (incl. C++ order-book walk for cost-to-follow)
walk-forward backtest ──▶ EDGE CONFIRMED / NOT CONFIRMED, shown on the dashboard
```

| Gate | Check |
|---|---|
| G1 | whale is buying (sells are EXIT notices) |
| G2 | wallet certified in this category (or overall, when the category is thin) |
| G3 | ≥ $5,000 and ≥ 2× the wallet's median bet |
| G4 | price in [0.05, 0.95] |
| G5 | known, non-bot, liquid market with ≥ 2 h to resolution |
| G6 | edge after walking the live book for $1,000 and paying fees ≥ 2¢ |
| G7 | no certified whale on the other side |

## Run it

```bash
uv sync                                   # builds the C++ core via CMake + nanobind
uv run whalescan score --max-wallets 300  # certified whale table
uv run whalescan batch                    # full pipeline -> data/snapshot/*.json
cd web && npm install && npm run dev      # dashboard on http://localhost:5173
```

Tests: `uv run pytest` · `scripts/test-cpp.sh` · `cd web && npm test`.

If GitHub's runners are ever blocked by Polymarket, run `uv run whalescan batch --publish` locally: it
commits and pushes the snapshot, and the Pages workflow deploys it.

## Layout

`cpp/` C++20 core (Monte Carlo engine, L2 order book) · `src/whalescan/` Python pipeline ·
`web/` React/TypeScript dashboard · `config.toml` every threshold · `docs/` design spec and API notes.

## Honest caveats

- The wallet universe is seeded from the current leaderboard, which leaks some future information into selection.
- Positions exited before resolution are scored as if held.
- Snapshot signals can be hours old; the dashboard shows the order book's age. Always re-check the price.
- Check Polymarket's terms for your jurisdiction before trading.
````

- [ ] **Step 4: Fill in the Pages URL and verify locally**

Run: `sed -i '' "s/<your-user>/$(gh api user -q .login)/" README.md && grep -n "github.io" README.md`
Expected: the README shows the real `https://<login>.github.io/whalescan/` URL.

Run: `uv run pytest -q && scripts/test-cpp.sh && (cd web && npm test && npm run build)`
Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "ci: test workflow, 6-hourly snapshot + Pages deploy, README"
```

- [ ] **Step 6: Publish (ask the user first — outward-facing)**

```bash
git push
gh api -X POST "repos/{owner}/whalescan/pages" -f build_type=workflow
gh workflow run snapshot
gh run watch "$(gh run list --workflow snapshot -L1 --json databaseId -q '.[0].databaseId')"
gh api "repos/{owner}/whalescan/pages" -q .html_url
```
Expected: the `ci` run is green; the `snapshot` run completes (the first full run can take a few hours while the position cache fills); the printed Pages URL serves the dashboard. If the run fails with `BLOCKED`, run `uv run whalescan batch --publish` locally and then `gh workflow run snapshot -f skip_batch=true`.

---

## After this plan

Design decisions already made for Plan 2 (measured 2026-09-24 on the 200 most active tokens: ~1,100 WS
messages/s carrying ~2,200 book deltas/s; Python `json.loads` ≈ 3.3 µs per message, i.e. < 0.5% of one core):
- **Transport stays in Python** (`websockets` + asyncio). A C++ WebSocket client would add TLS, reconnect and
  build complexity to save a cost that is already negligible, and gating needs Python anyway.
- **Batch the C++ boundary:** one `apply_deltas(side[], price[], size[])` call per WS message instead of one call
  per delta.
- **Book storage:** replace `std::map` with a fixed array indexed by tick (prices live in [0, 1], so at most
  10,001 slots per side) plus cached best-bid/best-ask indices — O(1) updates, no allocation, no Abseil
  dependency. Only if profiling shows the book mattering; the `OrderBook` interface stays identical.
- **Off-grid prices:** `apply_delta` rejects a price that is not within 1e-9 of the 0.0001 grid (every
  Polymarket tick size, 0.1 down to 0.0001, is a multiple of it), and the stream layer resnapshots that token.

Write **Plan 2 — Live Station** against the code as it now exists: reconnecting WebSocket client (`stream/ws_base.py`), RTDS ingest, CLOB market WebSocket watch set feeding `whalecore.OrderBook` deltas with crossed-book resync, FastAPI + WebSocket push to the dashboard (`data.ts` gains live mode), STALE re-evaluation, `data/live.duckdb` + `scores.parquet` reload, and the end-of-project "under the hood" walkthrough.
