# WHALESCAN

**Statistically certified Polymarket whale scanner.** Finds wallets whose forecasting skill survives a
Monte Carlo significance test *and* a false-discovery-rate correction, watches their trades, and surfaces
only the few that are still worth following after the real cost of entry. Humans decide; the tool never trades.

Live dashboard: `https://ts7832.github.io/whalescan/` (refreshed every 6 hours by GitHub Actions, €0).

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
