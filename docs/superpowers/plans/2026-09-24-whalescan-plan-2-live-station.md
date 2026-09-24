# WHALESCAN Plan 2 — Live Station

**Goal:** `whalescan live` — a local process that streams every Polymarket trade and the order books of markets
certified whales touch, gates trades the moment they happen, and pushes contacts/signals/book updates to the
dashboard over a WebSocket.

**Spec:** `docs/superpowers/specs/2026-09-24-whalescan-design.md` §3, §4.2, §4.6 (STALE), §4.8, §4.9, Part 5.
**Builds on:** Plan 1 as merged (`whalecore`, `gate.py`, `book.py`, `store.py`, API clients, snapshot JSON shapes).
**Execution:** autonomous overnight run authorised by the user ("just do it whatever is best"); TDD per task,
one whole-branch review at the end.

## Measured facts this plan relies on (2026-09-24)

- RTDS `wss://ws-live-data.polymarket.com`, subscribe `{"action":"subscribe","subscriptions":[{"topic":"activity","type":"trades"}]}`.
  Envelope `{connection_id, topic, type, timestamp(ms), payload}`; `payload` has exactly the REST trade fields
  (`proxyWallet, side, asset, conditionId, size, price, timestamp(s), transactionHash, outcome, outcomeIndex, …, fee`),
  so `parse_trade(payload)` is reused. Blank keep-alive frames occur. Fixture: `tests/fixtures/real/rtds_trades.json`.
- CLOB market WS `wss://ws-subscriptions-clob.polymarket.com/ws/market`, send `{"assets_ids":[...],"type":"market"}`.
  Messages (single object or list): `book {asset_id, bids, asks, timestamp, hash, tick_size, …}` (same keys as REST
  `/book`, so `parse_book` is reused), `price_change {market, timestamp, price_changes:[{asset_id, price, size,
  side BUY|SELL, hash, best_bid, best_ask}]}`, `last_trade_price`. Fixture: `tests/fixtures/real/clob_ws.json`.
- Load: top-200 tokens ≈ 1,100 msgs/s ≈ 2,200 deltas/s; Python parse ≈ 3.3 µs/msg → transport stays in Python.

## Design decisions

1. **Integrity check from the feed itself.** Every `price_change` carries the server's `best_bid/best_ask` after the
   change. After applying a message's deltas, the C++ book's best levels must match (within 1e-9); a mismatch or a
   crossed book marks the token *desynced* and triggers a REST `/book` resnapshot. Stronger than crossed-only.
2. **One C++ call per message:** `OrderBook.apply_deltas(sides: u8[n], prices: f64[n], sizes: f64[n])`.
   Off-grid prices (not within 1e-6 ticks of the 0.0001 grid) raise `ValueError` → resnapshot.
3. **Watch set** = assets of BUY position events by certified wallets in the last 24h ∪ assets with open signals,
   capped at 200 (open signals first). Changes are applied with incremental `{"assets_ids":[…],"operation":
   "subscribe"|"unsubscribe"}` messages on the open socket (verified live 2026-09-24); a reconnect re-sends the full set.
4. **Station state is pure and testable.** `LiveState` owns aggregation, gating, watch set, STALE re-evaluation and
   message production; it never touches the network. The runner (`live.py`) wires sockets, REST, the store and the
   HTTP server around it.
5. **Scores** come from `data/scores.parquet` (written atomically by `whalescan score/batch`), reloaded when its mtime
   changes. Market metadata: Gamma on demand with an in-memory cache; unknown market ⇒ evaluate now (G5 fails
   UNKNOWN MARKET), fetch, re-evaluate.
6. **Persistence:** `data/live.duckdb` (its own lock) stores every trade ≥ $1,000 and every certified-wallet trade,
   so a restart warms up from disk + a 24 h REST backfill.
7. **Server:** FastAPI + uvicorn on `127.0.0.1:8765`: `GET /api/state`, `WS /ws`, `GET /` = built dashboard.
   Book pushes throttled to 4 Hz per token.
8. **Dashboard:** live mode when served by the station (`/api/state` answers); a reducer applies
   `contact | signal | signal_update | book | status` messages; the status bar shows LINK LIVE/DOWN and feed latency.

## Tasks

| # | Deliverable | Tests (written first) |
|---|---|---|
| 1 | C++ `apply_deltas` + off-grid rejection; bindings | Catch2: batch equals sequential, off-grid throws; pytest via numpy |
| 2 | `stream/messages.py`: `parse_rtds(msg) -> (Trade, sent_ms) | None`, `parse_clob(msg) -> list[BookSnapshot | PriceChanges]` | real fixtures parse; keep-alives/garbage → skipped |
| 3 | `stream/ws_base.py` `ReconnectingWS` (heartbeat, dead-after silence, backoff+jitter, resubscribe, status callbacks, stop) | local websockets server: receives, reconnects after drop, reconnects after silence, backoff grows, stop() ends cleanly |
| 4 | `stream/books.py` `LiveBooks` (apply snapshot/changes, integrity check → desync set, quote()) | mismatch → desync; snapshot clears desync; quote uses C++ walk |
| 5 | `live_state.py` `LiveState` (on_trade, on_book, watch set, STALE, messages, reload scores) | trade → contact; certified trade + book → signal; book moves above max entry → STALE; watch set cap/ordering; SELL → EXIT |
| 6 | `live.py` runner + FastAPI server + `whalescan live` CLI | `/api/state` + `/ws` broadcast via TestClient with a fake feed; CLI wiring |
| 7 | Web live mode (reducer, connection, status bar LINK/LAT) | vitest reducer tests; build |
| 8 | Real run ≥ 5 min against Polymarket; README live section | manual: log excerpt recorded in ledger |
