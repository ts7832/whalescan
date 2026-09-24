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
- `GET /positions?user&sizeThreshold=0&limit(max 500)&offset` — current positions. **Resolved losers usually stay
  here forever** (`redeemable: true`, `curPrice: 0`): redeeming a loser pays $0, so nobody does, and they never reach
  `/closed-positions`. Verified 2026-09-24: a top wallet showed a 97% win rate in `/closed-positions` and had
  287 unredeemed losers ($6.2M staked) here. Scoring must union both endpoints. Rows have no close timestamp.
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
