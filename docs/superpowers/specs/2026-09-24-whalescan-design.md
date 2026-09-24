# WHALESCAN — Design Spec

**Date:** 2026-09-24 · **Status:** Approved design, pending spec review
**One line:** A zero-cost Polymarket intelligence station that finds wallets with *statistically proven* forecasting skill, watches them live, and surfaces only the few trades worth a human's attention.

---

## Part 0 — Under the hood (primer)

Read this first. Every later section assumes these ideas.

### 0.1 REST vs WebSockets

**REST** is request → response. Your program opens an HTTPS connection, asks `GET /trades?limit=100`, the server answers with JSON, and the exchange is over. It is perfect for "give me the history of wallet X" but terrible for "tell me the instant a whale trades" — you would have to ask again and again (*polling*), wasting requests and always being late by up to one polling interval.

**A WebSocket** starts life as an ordinary HTTP request carrying the header `Upgrade: websocket`. If the server agrees (status `101 Switching Protocols`), the same TCP connection stops being HTTP and becomes a **persistent, two-way pipe** of small *frames*. Either side may send at any time. We send one message ("subscribe me to all trades"), then the server *pushes* every trade to us within milliseconds of it happening. Consequences we must engineer for:

- **The connection can silently die** (Wi-Fi blip, laptop sleep, server restart). We detect this with *heartbeats* (ping/pong frames; if no pong in N seconds, the pipe is dead) and **reconnect with exponential backoff** (wait 1s, 2s, 4s … capped at 60s, plus random jitter so thousands of clients don't reconnect in lockstep).
- **Messages missed while disconnected are gone.** For trades we backfill the gap over REST. For order books (below) we throw the local book away and request a fresh snapshot.
- **Back-pressure:** if we process slower than messages arrive, a queue grows forever. The reader task only parses and enqueues; heavy work happens elsewhere, and the queue is bounded.

We use WebSockets **twice**: Polymarket → our station (ingest), and our station → the browser dashboard (so the UI updates the instant a whale trades, without the browser polling).

### 0.2 How Polymarket works

- Each **market** is a yes/no question (`Will X happen?`). Each outcome is an ERC-1155 **token** on the Polygon blockchain, identified by a huge integer `asset`/`token_id`. The two tokens of a market share a `conditionId`.
- At resolution the winning token pays **$1.00 USDC**, the loser **$0**. Therefore **a price of 0.37 is the market's implied probability of 37%**. Buying YES at 0.37 and winning returns 0.63 profit per share.
- Trading happens on a **CLOB** (central limit order book), operated off-chain and settled on-chain. Every trade is public and tied to a **proxy wallet** address — which is exactly why whale tracking is possible.
- Markets are grouped into **events** (e.g., one NFL game event contains moneyline, spread, totals markets). Events carry **tags** (`Sports`, `NFL`, `Politics`…) that we use for categories.
- Some markets charge **taker fees** (`feesEnabled`); the fee must be subtracted from any edge.

### 0.3 Order books, microprice and cost-to-follow

An order book is two sorted ladders: **bids** (people wanting to buy, best = highest price) and **asks** (wanting to sell, best = lowest). The gap between best bid and best ask is the **spread**.

- **Mid price** = (best bid + best ask) / 2.
- **Microprice** weights the mid toward the side with *less* resting size, since that side is more likely to be eaten next: `micro = (bid·askSize + ask·bidSize) / (bidSize + askSize)`. It is a better short-term fair value than mid.
- **Cost-to-follow:** if you want to buy $1,000 of YES *now*, you don't get the best ask — you "walk the book", taking the best level, then the next, until $1,000 is filled. The **VWAP** (volume-weighted average price) of that walk is your real entry price. If a whale bought at 0.40 and your walk VWAP is 0.46, six cents of their edge is already gone.

Polymarket's market WebSocket sends one full **`book` snapshot** and then **`price_change` deltas** ("level 0.22 on the BUY side now has size 948.5"; size 0 means the level is removed). Rebuilding the book = apply snapshot, then apply every delta in order. That is a classic C++ problem: we want sorted maps with fast insert/erase/best-level access, thousands of updates per second, zero garbage-collector pauses.

### 0.4 C++ from Python: nanobind

**nanobind** (successor of pybind11, by the same author) compiles a C++ library into a Python *extension module* (`whalecore.cpython-312-darwin.so`). You write C++ classes/functions, declare in `bindings.cpp` which ones Python can see, and then in Python `import whalecore` and call them like any Python code. Numpy arrays pass to C++ **without copying** (nanobind's `ndarray` view). **scikit-build-core** makes `uv pip install .` run CMake and the C++ compiler automatically, so the build is one command locally and in CI.

Rule of thumb for what goes in C++: tight loops over millions of numbers, or per-message work in a hot stream. Everything else (HTTP, JSON, orchestration) stays in Python where it is faster to write.

### 0.5 Is a whale skilled or lucky? (the statistics)

**Edge.** For a resolved position bought at average price `p` with outcome `y` (1 = won, 0 = lost), the *excess* result is `y − p`. A wallet buying at 0.30 things that happen 45% of the time earns +0.15 per dollar of shares on average. A wallet's edge is the stake-weighted average of `y − p`.

**The null hypothesis.** "No skill" means the market price was right: each bet wins with probability exactly `p`. Under that assumption we can **simulate** the wallet's history: for every position draw `y* ~ Bernoulli(p)`, compute the edge. Repeat 100,000 times. The **p-value** is the fraction of simulated lucky worlds that did *as well or better* than reality. p = 0.001 means "luck alone produces a record this good 1 time in 1,000." This is a **Monte Carlo permutation-style test** and it handles weird price mixes exactly, with no normal-distribution approximation. It is ~10⁵ simulations × hundreds of positions × thousands of wallets ≈ 10⁹–10¹⁰ random draws → **C++, multithreaded**.

**Multiple testing.** Test 3,000 wallets at p < 0.05 and ~150 will "pass" by pure luck. This is the #1 source of fake signals in every whale tracker. **Benjamini–Hochberg (BH)** fixes it: sort p-values ascending, find the largest rank `k` with `p_(k) ≤ (k/m)·q`, certify the first `k`. With `q = 0.10`, at most ~10% of certified whales are expected to be flukes. Only certified whales can create signals.

**Shrinkage (empirical Bayes).** A wallet with 8 bets and +20c edge is less trustworthy than one with 400 bets and +6c. We assume true edges across wallets are spread as `N(0, τ²)` and estimate τ² from the whole population. Each wallet's measured edge `S` has sampling variance `σ² = Σw²p(1−p)/(Σw)²`. The best estimate of its true edge is `S · τ² / (τ² + σ²)` — noisy wallets are pulled hard toward zero, well-measured wallets keep their edge. This *posterior edge* is what we use to price signals.

### 0.6 Walk-forward validation

Any scoring rule looks brilliant on the data it was built from. **Walk-forward** avoids fooling ourselves: pick a cutoff date T, score wallets using only positions resolved before T, then measure how their trades *opened after T* performed — including realistic slippage. Slide T forward and repeat. If the edge survives out-of-sample, it is real (or at least not overfit). If not, the dashboard says so.

### 0.7 DuckDB

An embedded analytical database: a single file, no server, SQL that runs columnar and vectorized (very fast group-bys over millions of trades), and it reads/writes pandas DataFrames and Parquet directly. Think "SQLite for analytics".

### 0.8 GitHub Actions & Pages (the €0 cloud)

**Actions** runs jobs on GitHub's machines on events (push) or a cron schedule, free for public repos. **Pages** serves static files (HTML/JS/JSON) at `https://<user>.github.io/whalescan`. Our cron job recomputes scores every 6 hours and redeploys the dashboard with fresh JSON — a live-looking public site with no server.

---

## Part 1 — Goals and non-goals

**Goals**
1. Identify Polymarket wallets with statistically significant, category-specific forecasting skill (FDR-controlled).
2. Watch all Polymarket trades in real time and flag trades by certified whales that are *still worth following after costs*.
3. Keep the signal board sparse: a typical day should produce a handful of signals, not hundreds.
4. Show proof: out-of-sample validation displayed on the dashboard, including when it fails.
5. Run at €0: live mode on the user's Mac, public snapshot mode on GitHub Actions + Pages.
6. Use C++ where it earns its place (order book engine, Monte Carlo engine).

**Non-goals (v1)**
- No automatic order placement, no wallet keys, no trading API auth. Humans decide.
- No Kalshi (US-only KYC).
- No always-on server; live mode runs only while the station is running locally.
- No user accounts / multi-user features.

---

## Part 2 — Data sources (all verified working 2026-09-24, no API key)

| Source | Endpoint | Used for |
|---|---|---|
| Data API — trades | `GET https://data-api.polymarket.com/trades?limit&offset&user&filterType=CASH&filterAmount` | backfill, whale discovery, wallet trade history (entry times) |
| Data API — closed positions | `GET https://data-api.polymarket.com/closed-positions?user=&limit&offset&sortBy=TIMESTAMP&sortDirection=DESC` | resolved positions: `avgPrice`, `totalBought`, `realizedPnl`, `curPrice` (1/0 at resolution), `conditionId`, `outcome`, `timestamp`. **Default sort is `realizedPnl` descending** (verified: page 1 of a top wallet = 100% winners, ascending sort = 100% losers). We always sort by `TIMESTAMP` and paginate to exhaustion; a truncated fetch is recorded as `incomplete` and the wallet is not testable — otherwise scoring suffers survivorship bias. |
| Data API — leaderboard | `GET https://data-api.polymarket.com/v1/leaderboard?limit&...` | wallet universe seeding |
| Gamma — markets/events | `GET https://gamma-api.polymarket.com/markets`, `/events?slug=` | market metadata, `endDate`, `feesEnabled`, `clobTokenIds`, event `tags` |
| CLOB — book | `GET https://clob.polymarket.com/book?token_id=` | order-book snapshot (snapshot mode, resync) |
| RTDS WebSocket | `wss://ws-live-data.polymarket.com`, send `{"action":"subscribe","subscriptions":[{"topic":"activity","type":"trades"}]}` | live firehose of every trade (wallet, side, price, size, asset, eventSlug, fee) |
| CLOB market WebSocket | `wss://ws-subscriptions-clob.polymarket.com/ws/market`, send `{"assets_ids":[...],"type":"market"}` | `book` snapshots + `price_change` deltas for watched tokens |

Exact pagination limits, rate limits, and the fee-rate formula are confirmed during implementation (task 1 of the plan) and recorded in `docs/polymarket-api-notes.md`.

---

## Part 3 — Architecture

```
                         ┌──────────────── GitHub (free) ─────────────────┐
                         │  Actions cron (6h): batch.py → C++ MC → JSON   │
                         │  Pages: web/ build + data/snapshot/*.json      │
                         └────────────────────────▲───────────────────────┘
                                                  │ git push (snapshot)
 Polymarket                       Your Mac (live mode)
 ─────────                        ───────────────────────────────────────────
 RTDS WS (all trades) ──▶ stream/rtds.py ──▶ queue ──▶ gate.py ──▶ live.py ──WS──▶ Browser (web/)
 CLOB WS (books)      ──▶ stream/clob_ws.py ──▶ whalecore.OrderBook (C++)  ▲
 REST APIs            ──▶ api/*.py ──▶ store.py (DuckDB) ──▶ scoring.py ──┘
                                                         └▶ whalecore.mc (C++)
```

Two run modes share one codebase:

- **Live (`whalescan live`)** — async Python process on the Mac. Ingests RTDS + CLOB WebSockets, keeps C++ books for watched tokens, gates trades against the latest wallet scores, serves the dashboard on `http://localhost:8765` and pushes events over `ws://localhost:8765/ws`.
- **Snapshot (`whalescan batch`)** — run by GitHub Actions every 6h (and runnable locally). Refreshes the wallet universe and scores, evaluates the last 6h of large trades through the same gate using REST book snapshots for cost-to-follow, runs validation, writes `data/snapshot/*.json`, and the workflow deploys `web/` + JSON to Pages.

The web app detects its mode: if `ws://localhost:8765/ws` answers it is live; otherwise it loads `./data/*.json` and shows a `SNAPSHOT · <timestamp>` badge.

### 3.1 Repository layout

```
whalescan/
├── pyproject.toml            # uv + scikit-build-core + nanobind; Python ≥3.12
├── CMakeLists.txt            # builds whalecore extension + Catch2 tests
├── config.toml               # all thresholds (gate, scoring, blocklist)
├── cpp/
│   ├── include/whalecore/orderbook.hpp
│   ├── include/whalecore/montecarlo.hpp
│   ├── src/orderbook.cpp
│   ├── src/montecarlo.cpp
│   ├── src/bindings.cpp
│   └── tests/test_orderbook.cpp, test_montecarlo.cpp
├── src/whalescan/
│   ├── config.py             # typed config loader
│   ├── models.py             # dataclasses: Trade, Position, WalletScore, Signal, BookView
│   ├── api/ratelimit.py      # async token bucket + retry/backoff
│   ├── api/data_api.py       # trades, closed-positions, leaderboard
│   ├── api/gamma.py          # markets, events, tags (cached)
│   ├── api/clob.py           # REST book snapshot
│   ├── stream/ws_base.py     # reconnecting WS client: heartbeat, backoff, jitter
│   ├── stream/rtds.py        # trade firehose → Trade
│   ├── stream/clob_ws.py     # book/price_change → whalecore.OrderBook
│   ├── store.py              # DuckDB schema + upserts (dedupe by tx hash)
│   ├── classify.py           # categories, blocklist, market-maker/farmer detection
│   ├── scoring.py            # edge, shrinkage, MC call, BH certification
│   ├── gate.py               # position-event aggregation + signal gate + tiers
│   ├── validate.py           # walk-forward backtest
│   ├── live.py               # asyncio station + FastAPI/WS server
│   ├── batch.py              # snapshot pipeline
│   └── cli.py                # `whalescan live|batch|score|validate`
├── tests/                    # pytest (unit + recorded-fixture integration)
├── web/                      # Vite + React + TypeScript dashboard
├── data/snapshot/            # committed JSON outputs (small)
└── .github/workflows/ci.yml, snapshot.yml
```

---

## Part 4 — Components

### 4.1 `whalecore` (C++20)

**`OrderBook`** — one per token.
- Storage: `std::map<Price, Size, std::greater<>>` for bids, `std::map<Price, Size>` for asks. Prices are integer **ticks** (price × 10,000 → `int32`) to avoid floating-point keys; sizes are `double`.
- API (exposed to Python):
  - `apply_snapshot(bids: ndarray[n,2], asks: ndarray[n,2])`
  - `apply_delta(side: Side, price: float, size: float)` — size 0 erases the level
  - `best_bid() / best_ask() / mid() / microprice() / spread()`
  - `depth(side, ticks_from_best) -> float` (USDC resting within N ticks)
  - `walk(side, notional_usdc) -> WalkResult{vwap, filled_usdc, levels_consumed, worst_price}`
  - `imbalance(levels=5) -> float` in [−1, 1]
- Invariant check: after deltas, if `best_bid >= best_ask` the book is **crossed** → Python triggers a REST resnapshot.

**`skill_mc`** — Monte Carlo significance.
- Input: `prices: ndarray[float64]`, `outcomes: ndarray[uint8]`, `weights: ndarray[float64]`, `n_sims`, `seed`.
- Output: `{edge, p_value, null_mean, null_sd}` where `edge = Σw(y−p)/Σw`, `p_value = (1 + #{S* ≥ S}) / (1 + n_sims)`.
- `skill_mc_batch(...)` takes many wallets in CSR form (offsets array) and distributes wallets across `std::thread`s (`hardware_concurrency`). RNG: per-thread `std::mt19937_64` seeded from `seed ^ wallet_index` → deterministic results regardless of thread count.
- Fast path: `uniform < p` Bernoulli draws; the inner loop is branch-light and auto-vectorizes with `-O3`.

### 4.2 Ingestion (`api/`, `stream/`)

- `api/*` use `httpx.AsyncClient` (HTTP/2, keep-alive) behind a shared **token-bucket limiter** (default 5 req/s, configurable) and retry with exponential backoff on 429/5xx. Responses are parsed into `models.py` dataclasses at the boundary; nothing downstream sees raw JSON.
- `stream/ws_base.py`: `ReconnectingWS(url, subscribe_msg, on_message)` — heartbeat every 10s (ping/pong via `websockets` library), declare dead after 30s silence, backoff 1→60s with jitter, re-subscribe on reconnect, emits `connected/disconnected/latency` status events for the UI status bar.
- `rtds.py`: parses trade payloads → `Trade`, pushes into a bounded `asyncio.Queue(maxsize=50_000)`. On reconnect, backfills the gap from REST `/trades` for trades ≥ `min_backfill_usdc`.
- `clob_ws.py`: manages the **watch set** of tokens (tokens touched by certified whales in the last 24h, plus tokens with open signals; capped at 200). Resubscribes when the set changes. Routes `book` → `apply_snapshot`, `price_change` → `apply_delta`, crossed book → REST resnapshot.

### 4.3 Storage (`store.py`, DuckDB, gitignored)

DuckDB allows only one writing process per file, so each process owns its own file: `data/research.duckdb` is written only by `score` / `batch` / `validate`, and `data/live.duckdb` only by `live`. Scoring publishes its results by writing `data/scores.parquet` to a temp file and atomically renaming it; the live station reloads that parquet when its modification time changes. Every command takes an advisory lock file (`data/<name>.lock`) and exits with a clear message if another process holds it, instead of surfacing a raw DuckDB lock error.

Tables: `trades(tx_hash PK, ts, wallet, asset, condition_id, side, price, size, usdc, event_slug, fee)`, `positions(wallet, asset PK-pair, avg_price, total_bought, realized_pnl, outcome, resolved_ts, category)`, `markets(condition_id PK, question, event_slug, end_date, fees_enabled, category, tags)`, `wallet_scores(wallet, category, as_of, n, n_eff, edge, post_edge, p_value, certified, flags)`, `signals(id PK, ts, ...)`. Upserts are idempotent (dedupe by `tx_hash` / natural keys), so re-running any step is safe.

### 4.4 Classification (`classify.py`)

- **Category** from event tags → one of `POLITICS, GEOPOLITICS, SPORTS, CRYPTO, ECONOMY, TECH, CULTURE, OTHER` via an ordered mapping in `config.toml`.
- **Blocklist** (never scored, never signalled): short-horizon crypto up/down markets (event slug regex `-updown-\d+m-` and similar), markets with < `min_market_volume`.
- **Wallet flags** (computed from history; flagged wallets are not certifiable):
  - `MARKET_MAKER`: held both outcomes of the same market in > 30% of markets, or buy/sell volume ratio within 0.8–1.25 across > 50 markets.
  - `FARMER`: > 60% of stake entered at price ≥ 0.95.
  - `LOTTERY`: > 60% of stake entered at price ≤ 0.05.

### 4.5 Scoring (`scoring.py`)

1. **Universe**: union of leaderboard wallets (several windows) and every wallet with a ≥ $5,000 trade in the last 30 days; capped at `max_wallets` (default 3,000, ordered by volume).
2. **Positions**: fetch *all* closed positions per wallet (see Part 2 sort caveat). A position counts only if its market is resolved in Gamma (`closed = true` and `outcomePrices` exactly `["1","0"]` or `["0","1"]`), which gives `y ∈ {0, 1}`. Markets resolved fractionally (50/50 splits, disputed/voided) are **discarded** and counted in `wallet_scores.flags` — the no-skill null `y* ~ Bernoulli(p)` cannot produce fractional outcomes, so mixing them in would bias the test. Positions in markets that are still open (the wallet sold early) are excluded. Keep entry `avgPrice ∈ [0.03, 0.97]`, non-blocklisted markets. Stake `w = totalBought · avgPrice`, **winsorized** at the wallet's 95th percentile so one giant bet can't dominate.
3. **Per (wallet, category)** and per (wallet, ALL): run `skill_mc_batch` (100k sims). Require `n_eff = (Σw)²/Σw² ≥ 20` to be testable.
4. **Shrinkage**: estimate τ² by method of moments across testable wallets (`τ² = max(0, var(S) − mean(σ²))`), compute `post_edge`.
5. **Certification**: BH at `q = 0.10` over all testable (wallet, category) tests jointly; certified ∧ `post_edge ≥ 0.03` ∧ no flags.
6. **Limitation (documented, v1)**: positions exited before resolution are scored on outcome from the average entry price; early-exit skill is not credited separately.

### 4.6 Signal gate (`gate.py`)

Raw trades → **position events**: fills by the same (wallet, asset, side) within 10 minutes are merged into one event with VWAP price and total USDC.

A position event becomes a **signal** only if **all** pass (defaults in `config.toml`):

**G6 in snapshot mode.** The question G6 answers is *"what would it cost to follow this trade now?"*, so the current book is the correct input (not the book at whale-trade time). In snapshot mode "now" is the batch run time, so every snapshot signal carries `book_as_of` and the UI shows its age (`BOOK 3H12M OLD`). Signals whose market has since resolved or whose position event is older than 24h are marked `EXPIRED`. Historical evaluation (validation, 4.7) never uses a current book — it uses the slippage model.

| # | Check | Default |
|---|---|---|
| G1 | side is BUY (SELL events → `EXIT` entries in the contact log) | — |
| G2 | wallet certified in the event's category, **or** certified overall with < 10 category positions and non-negative category edge | q = 0.10 |
| G3 | size ≥ absolute floor **and** ≥ k × wallet's median stake | $5,000; k = 2 |
| G4 | entry price within band | [0.05, 0.95] |
| G5 | market not blocklisted; ≥ 2h to `endDate` | — |
| G6 | cost-to-follow: walk the ask side for `follow_size`; `net_edge = post_edge − (follow_vwap − whale_price) − fee ≥ min_net_edge` | $1,000; 0.02 |
| G7 | no certified whale holds an opposing position event in the same market within 24h (else `CONFLICT`, suppressed, logged) | — |

**Output per signal:** market, outcome, whale(s), whale price, current microprice, follow VWAP, `net_edge`, **max entry price** = `whale_price + post_edge − fee − 0.01`, reasons list (each gate with its value), tier.

**Tiers:** `A` — net_edge ≥ 0.05, or ≥ 2 certified whales same side within 24h (consensus). `B` — everything else that passed. The SIGNALS panel shows A and B; everything that failed a gate stays in CONTACTS with the failing gate labelled. Re-evaluation: an open signal whose follow VWAP rises above max entry is marked `STALE`.

### 4.7 Validation (`validate.py`)

- Cutoffs every 14 days over the available history (≥ 6 folds when data allows).
- For each fold: score using positions resolved before T; collect certified wallets' BUY position events opened after T and resolved; apply gates G1–G5 and G7 historically (G6 approximated with a slippage model: `+max(0.01, 0.5·spread_estimate)`); record `y − follow_price − fee`.
- Report per tier and baseline ("follow every ≥ $5k trade"): n, mean return per $, hit rate, t-stat, and a **calibration table** (predicted win prob = whale_price + post_edge, bucketed, vs realized frequency).
- Written to `data/snapshot/validation.json`; shown verbatim on the dashboard, including a red `EDGE NOT CONFIRMED` banner if tier A mean return's t-stat < 2.

### 4.8 Live station (`live.py`)

`asyncio` process running: RTDS reader, CLOB WS reader, gate worker, periodic score refresh (reloads `wallet_scores` from DuckDB every 30 min; full re-score is `whalescan score`, run manually or nightly), and a **FastAPI** app:
- `GET /` → built dashboard (`web/dist`)
- `GET /api/state` → current signals, recent contacts, watched books summary, status
- `WS /ws` → pushes `contact`, `signal`, `signal_update`, `book` (throttled to 4 Hz per token), `status` messages as JSON `{type, data}`
- `GET /api/wallet/{addr}` → dossier data

### 4.9 Dashboard (`web/`)

- **Stack:** Vite + React + TypeScript; TradingView `lightweight-charts` for price/depth; no UI kit (custom CSS for full control of the look).
- **Typography:** **IBM Plex Mono** everywhere (self-hosted via `@fontsource/ibm-plex-mono`, weights 400/500/600), uppercase labels with +0.08em letter-spacing, tabular numerals.
- **Palette:** background `#07090B`, panels `#0C1014`, hairlines `#1C242C`, text `#C9D1D9`, dim `#6B7785`, amber `#FFB000` (attention/tier B), green `#3DDC84` (tier A/connected), red `#FF4D4D` (conflict/stale/disconnected), cyan `#4FC3F7` (data highlights). Dark only.
- **Layout:** top status bar (`WHALESCAN // LINK: LIVE ● · FEED LAT 142MS · SCORES 14:02Z · CERTIFIED 37`), then a panel grid:
  - **SIGNALS** — tier A/B cards with reasons, max entry, live net edge
  - **CONTACTS** — scrolling live feed of ≥ $5k position events, each tagged with the gate that stopped it
  - **DOSSIER** — selected wallet: per-category edge, p-value, n, calibration chart, recent positions
  - **BOOK** — ladder + depth chart for the selected market, with the follow-walk overlaid
  - **VALIDATION** — fold results table, tier vs baseline, calibration chart
- Keyboard: `J/K` move through signals, `Enter` opens dossier, `/` filters by category.

---

## Part 5 — Error handling

| Failure | Handling |
|---|---|
| WS disconnect / silent death | heartbeat timeout → reconnect with backoff+jitter; UI status turns red; REST backfill of trades on reconnect |
| Missed book deltas / crossed book | discard book, REST resnapshot, resubscribe |
| HTTP 429 / 5xx | token bucket + exponential retry (max 5); batch job logs and skips a wallet after exhausting retries rather than failing the run |
| Blocked by Cloudflare / 403 challenge (e.g. GitHub runner IPs) | detected by status + HTML body; job fails loudly with `BLOCKED` in the log and keeps the previous snapshot. Fallback (still €0): run `whalescan batch --publish` locally, which commits and pushes the snapshot; no paid proxy |
| Schema drift in API JSON | boundary parsers raise `ParseError` with the raw payload logged; message skipped, counter shown in status bar |
| Missing market metadata | fetch Gamma on demand, cache; category `OTHER` if tags unavailable |
| Batch job partial failure | writes snapshot only if scoring completed; otherwise keeps previous snapshot and the workflow fails visibly |
| Queue overflow | drop oldest *sub-threshold* trades first; count drops in status bar |

---

## Part 6 — Testing

- **C++ (Catch2, via CMake FetchContent):** order book snapshot/delta/erase, crossed detection, `walk` across multiple levels and partial fills, microprice formula; MC edge value exact, p-value ≈ 1 for a losing wallet, calibration test — for 2,000 simulated *no-skill* wallets p-values are ~uniform (fraction < 0.1 within [0.08, 0.12]), determinism across thread counts.
- **Python (pytest):** parsers against recorded real JSON fixtures in `tests/fixtures/`; closed-positions pagination fetches every page with `sortBy=TIMESTAMP` and marks truncated histories `incomplete`; fractional/unresolved markets are excluded; BH against a hand-computed example; shrinkage math; classifier flags; every gate G1–G7 with pass/fail cases; position-event aggregation windows; `ReconnectingWS` against a local test WS server that drops connections; end-to-end `batch` on fixtures producing snapshot JSON.
- **Web:** TypeScript typecheck + a Vitest test for the mode detection and message reducer.
- **CI (`ci.yml`)** on every push: build C++ + run Catch2, `uv sync` + pytest, `npm ci && npm run build && npm test`.
- **`snapshot.yml`:** cron `0 */6 * * *` + manual dispatch; runs `whalescan batch`, commits `data/snapshot/`, builds web, deploys Pages. Uses `actions/cache` for the raw position cache so each run only fetches deltas.

---

## Part 7 — Build order (each milestone demo-able)

1. **Foundation:** repo scaffold, **day-one Actions smoke test** (a workflow that calls each REST endpoint from a GitHub runner and fails on 403/challenge, so an IP block is discovered before anything depends on Actions), API notes, `whalecore` MC + tests, API clients, DuckDB store, classification, scoring + BH. → `whalescan score` prints the certified whale table.
2. **Public snapshot:** batch pipeline, gate (with REST book for G6), validation, dashboard in snapshot mode, Actions + Pages. → public URL for the Junction application.
3. **Live station:** reconnecting WS, RTDS ingest, FastAPI/WS server, dashboard live mode.
4. **Books:** `whalecore.OrderBook`, CLOB WS watch set, live cost-to-follow, BOOK panel, STALE re-evaluation.
5. **Polish:** README with architecture diagram, screenshots, and the end-of-project "under the hood" walkthrough.

---

## Part 8 — Risks and open points

- **Edge may not exist out-of-sample.** Mitigation: the validation panel reports honestly; the project is still valuable as a rigorous negative result.
- **API limits / undocumented changes.** Mitigation: fixtures + boundary parsers + rate limiter; confirmed in plan task 1.
- **GitHub Actions runtime for 3,000 wallets.** Mitigation: incremental cache (only new closed positions), `max_wallets` configurable.
- **Legal:** the user must check Polymarket's terms for Finland before trading. The tool never trades.
