import httpx
import pytest

from whalescan.api import data_api as data_api_module
from whalescan.api.clob import ClobApi
from whalescan.api.data_api import DataApi
from whalescan.api.gamma import GammaApi
from whalescan.api.http import ApiError, HttpClient


async def _noop(_):
    return None


def http(handler) -> HttpClient:
    return HttpClient(user_agent="t", rate_per_s=1e6, max_retries=0, transport=httpx.MockTransport(handler), sleep=_noop)


def pos(i: int, ts: int) -> dict:
    return {"proxy_wallet": "0xw", "token_id": str(i), "condition_id": f"0xc{i}", "avg_price": 0.5,
            "total_size": 10, "realized_pnl": 1, "current_price": 1, "outcome": "Yes", "outcome_index": 0,
            "title": "t", "event_slug": "e", "last_event_at": ts}


def trade(i: int, ts: int) -> dict:
    return {"proxy_wallet": "0xw", "side": "BUY", "token_id": str(i), "condition_id": "0xc", "size": 100,
            "price": 0.5, "timestamp": ts, "transaction_hash": f"0x{i}", "outcome_index": 0}


def env(rows: list, nxt: str | None) -> dict:
    """A Data API v2 response: rows in `data`, the next page's cursor in `pagination`."""
    return {"data": rows, "pagination": {"has_more": nxt is not None, "next_cursor": nxt}}


def page_no(request) -> int:
    """Test cursors are just the next page's number."""
    return int(request.url.params.get("cursor") or 0)


async def test_closed_positions_pages_by_cursor_in_timestamp_order():
    seen = []

    def handler(request):
        q = request.url.params
        assert "offset" not in q  # v2 answers 400 to any offset parameter
        seen.append((request.url.path, q["status"], q["sort_by"], q["sort_direction"], q.get("cursor")))
        n = page_no(request)
        rows = [pos(n * 50 + k, 1000 - n * 50 - k) for k in range(50 if n < 2 else 7)]
        return httpx.Response(200, json=env(rows, str(n + 1) if n < 2 else None))

    hist = await DataApi(http(handler)).closed_positions("0xw")
    assert hist.complete and not hist.truncated and len(hist.positions) == 107
    assert hist.positions[0].ts == 1000  # last_event_at is the position's time
    assert seen == [("/v2/positions", "CLOSED", "TIMESTAMP", "DESC", None),
                    ("/v2/positions", "CLOSED", "TIMESTAMP", "DESC", "1"),
                    ("/v2/positions", "CLOSED", "TIMESTAMP", "DESC", "2")]


async def test_closed_positions_error_object_raises_instead_of_truncating():
    # An error body must fail the fetch, so nothing partial is stored and the next run retries in full.
    def handler(request):
        if page_no(request) >= 1:
            return httpx.Response(200, json={"error": "temporary"})
        return httpx.Response(200, json=env([pos(k, 5000 - k) for k in range(50)], "1"))

    with pytest.raises(ApiError, match="error object"):
        await DataApi(http(handler)).closed_positions("0xw")


async def test_closed_positions_stops_at_local_row_cap(monkeypatch):
    # Sorted by TIMESTAMP, a capped history is the most recent window: unbiased with respect to outcomes,
    # so it stays usable (complete) but is marked truncated.
    monkeypatch.setattr(data_api_module, "MAX_OFFSET", 100)
    hist = await DataApi(http(lambda r: httpx.Response(200, json=env([pos(k, 9999) for k in range(50)],
                                                                      str(page_no(r) + 1))))).closed_positions("0xw")
    assert hist.complete and hist.truncated
    assert len(hist.positions) == 100


async def test_closed_positions_incremental_stops_at_since_ts():
    pages = {0: env([pos(k, 100 - k) for k in range(50)], "1"), 1: env([pos(50 + k, 50 - k) for k in range(50)], None)}
    hist = await DataApi(http(lambda r: httpx.Response(200, json=pages[page_no(r)]))).closed_positions("0xw", since_ts=80)
    assert hist.complete
    assert [p.ts for p in hist.positions] == list(range(100, 79, -1))


async def test_trades_filters_and_stops_at_since_ts():
    seen = {}

    def handler(request):
        seen.update(dict(request.url.params))
        return httpx.Response(200, json=env([trade(k, 1000 - k * 10) for k in range(500)], "1"))

    page = await DataApi(http(handler)).trades(min_usdc=5000, since_ts=900)
    assert page.complete
    assert min(t.ts for t in page.trades) >= 900
    assert seen["filter_type"] == "CASH" and seen["filter_amount"] == "5000" and seen["taker_only"] == "false"


async def test_trades_parse_v2_fields():
    page = await DataApi(http(lambda r: httpx.Response(200, json=env([trade(7, 123)], None)))).trades(market="0xc")
    t = page.trades[0]
    assert (t.tx_hash, t.ts, t.wallet, t.asset, t.condition_id, t.outcome_index, t.fee) == (
        "0x7", 123, "0xw", "7", "0xc", 0, None)


async def test_leaderboard_pages():
    def handler(request):
        n = page_no(request)
        assert request.url.path == "/v2/leaderboard" and request.url.params["time_period"] == "ALL"
        rows = [{"rank": n * 50 + k + 1, "user_id": f"0x{n * 50 + k}", "volume": 1, "pnl": 1, "user_name": "u"}
                for k in range(50)]
        return httpx.Response(200, json=env(rows, "1" if n == 0 else None))

    board = await DataApi(http(handler)).leaderboard(period="ALL", order_by="PNL", pages=5)
    assert len(board) == 100 and board[0].rank == 1 and board[0].wallet == "0x0"


async def test_gamma_queries_closed_first_then_open_for_the_rest():
    calls = []

    def handler(request):
        ids = request.url.params.get_list("condition_ids")
        closed = request.url.params["closed"]
        calls.append((len(ids), closed, request.url.params["include_tag"], request.url.params["limit"]))
        rows = [{"conditionId": i.upper(), "closed": closed == "true", "outcomePrices": "[\"1\",\"0\"]"}
                for i in ids if (int(i[2:]) % 2 == 0) == (closed == "true")]
        return httpx.Response(200, json=rows)

    ids = [f"0x{i}" for i in range(250)]
    markets = await GammaApi(http(handler)).markets(ids + ["0X1"])
    assert len(markets) == 250
    assert markets["0x2"].closed and not markets["0x3"].closed
    # 250 ids -> closed pass in chunks of 100; the 125 odd (open) ids left -> open pass in chunks of 100.
    # Chunks within a pass run concurrently, so only the pass order is fixed.
    assert sorted(calls[:3]) == [(50, "true", "true", "100"), (100, "true", "true", "100"),
                                 (100, "true", "true", "100")]
    assert sorted(calls[3:]) == [(25, "false", "true", "100"), (100, "false", "true", "100")]


async def test_gamma_fetches_chunks_concurrently_and_logs_progress(caplog):
    # 1.16M markets fetched one request at a time is what ran the daily job past GitHub's time limit.
    import asyncio
    import logging

    state = {"in_flight": 0, "peak": 0}

    async def handler(request):
        state["in_flight"] += 1
        state["peak"] = max(state["peak"], state["in_flight"])
        await asyncio.sleep(0.005)
        state["in_flight"] -= 1
        ids = request.url.params.get_list("condition_ids")
        closed = request.url.params["closed"] == "true"
        return httpx.Response(200, json=[{"conditionId": i, "closed": True, "outcomePrices": "[\"1\",\"0\"]"}
                                         for i in ids] if closed else [])

    caplog.set_level(logging.INFO, logger="whalescan.api.gamma")
    markets = await GammaApi(http(handler)).markets([f"0x{i}" for i in range(3000)])
    assert len(markets) == 3000
    assert state["peak"] > 1
    assert "market metadata: 30/30 chunks" in caplog.text


async def test_gamma_skips_malformed_rows():
    api = GammaApi(http(lambda r: httpx.Response(200, json=[{"question": "no id"}])))
    assert await api.markets(["0x1"]) == {}
    assert api.skipped == 2  # one malformed row from the closed pass, one from the open pass


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


async def test_redeemable_positions_returns_only_resolved_rows_across_pages():
    seen = []

    def handler(request):
        q = request.url.params
        seen.append((request.url.path, q["user"], q["status"], q.get("cursor")))
        n = page_no(request)
        rows = [dict(pos(n * 500 + k, 0), redeemable=(k % 2 == 0)) for k in range(500 if n == 0 else 3)]
        return httpx.Response(200, json=env(rows, "1" if n == 0 else None))

    hist = await DataApi(http(handler)).redeemable_positions("0xw")
    assert hist.complete
    assert len(hist.positions) == 250 + 2
    assert all(p.ts == 0 for p in hist.positions)
    assert seen == [("/v2/positions", "0xw", "REDEEMABLE", None), ("/v2/positions", "0xw", "REDEEMABLE", "1")]


async def test_capped_redeemable_positions_are_incomplete(monkeypatch):
    # No known outcome-neutral order, so a capped fetch is not a trustworthy window.
    monkeypatch.setattr(data_api_module, "MAX_OFFSET", 1000)
    hist = await DataApi(http(lambda r: httpx.Response(200, json=env(
        [dict(pos(k, 0), redeemable=True) for k in range(500)], str(page_no(r) + 1))))).redeemable_positions("0xw")
    assert not hist.complete


async def test_gamma_failed_chunk_does_not_lose_the_others():
    def handler(request):
        ids = request.url.params.get_list("condition_ids")
        if "0x0" in ids and request.url.params["closed"] == "true":
            return httpx.Response(500)
        if request.url.params["closed"] == "false":
            return httpx.Response(200, json={"error": "not a list"})
        return httpx.Response(200, json=[{"conditionId": i, "closed": True, "outcomePrices": "[\"1\",\"0\"]"} for i in ids])

    api = GammaApi(http(handler))
    markets = await api.markets([f"0x{i}" for i in range(150)])
    assert len(markets) == 50 and "0x0" not in markets and "0x99" in markets  # ids sort as text
    assert api.skipped >= 1


async def test_wallet_profile_combines_creation_time_and_markets_traded():
    def handler(request):
        if request.url.path == "/public-profile":
            assert request.url.params["address"] == "0xw"
            return httpx.Response(200, json={"createdAt": "2026-09-23T10:00:00.5Z", "proxyWallet": "0xw"})
        assert request.url.path == "/v2/user-stats" and request.url.params["user"] == "0xw"
        return httpx.Response(200, json={"data": {"proxy_wallet": "0xw", "trades": 3}})

    from whalescan.api.profiles import fetch_profile
    h = http(handler)
    p = await fetch_profile(GammaApi(h), DataApi(h), "0xW", now=1_790_000_000)
    assert p.wallet == "0xw" and p.markets_traded == 3 and p.fetched_at == 1_790_000_000
    assert p.created_ts == 1790157600


async def test_wallet_profile_tolerates_unknown_accounts():
    def handler(request):
        if request.url.path == "/public-profile":
            return httpx.Response(404, json={"error": "profile not found"})
        return httpx.Response(200, json={"data": {"proxy_wallet": "0xw", "trades": 0}})

    from whalescan.api.profiles import fetch_profile
    h = http(handler)
    p = await fetch_profile(GammaApi(h), DataApi(h), "0xw", now=5)
    assert p.created_ts is None and p.markets_traded == 0


async def test_gamma_block_propagates_as_itself_through_concurrent_chunks():
    from whalescan.api.http import BlockedError

    def handler(request):
        if "0x5" in request.url.params.get_list("condition_ids"):
            return httpx.Response(403, headers={"cf-mitigated": "challenge"}, text="blocked")
        return httpx.Response(200, json=[])

    with pytest.raises(BlockedError):
        await GammaApi(http(handler)).markets([f"0x{i}" for i in range(1000)])


async def test_trades_can_be_read_per_market_with_the_cash_filter():
    seen = []

    def handler(request):
        q = request.url.params
        assert "market" not in q  # v2 renamed it to `condition` and answers 400 to the old name
        seen.append((q.get("condition"), q.get("filter_amount"), q.get("user")))
        n = page_no(request)
        rows = [trade(n * 500 + k, 1000 - n * 500 - k) for k in range(500 if n == 0 else 3)]
        return httpx.Response(200, json=env(rows, "1" if n == 0 else None))

    page = await DataApi(http(handler)).trades(market="0xc", min_usdc=1000)
    assert page.complete and len(page.trades) == 503
    assert seen[0] == ("0xc", "1000", None)


async def test_gamma_lists_markets_by_end_date_window_across_keyset_pages():
    # Plain /markets refuses offsets beyond ~2,000 (HTTP 422) and a single week can hold more markets than that,
    # so listing uses /markets/keyset and follows `after_cursor` until the API returns no cursor.
    seen = []

    def handler(request):
        q = request.url.params
        assert request.url.path == "/markets/keyset" and "offset" not in q
        seen.append((q["closed"], q["end_date_min"], q["end_date_max"], q["volume_num_min"], q.get("after_cursor")))
        page = {None: 0, "c1": 1, "c2": 2}[q.get("after_cursor")]
        n = 100 if page < 2 else 17
        rows = [{"conditionId": f"0x{page}-{k}", "closed": True, "outcomePrices": "[\"1\",\"0\"]",
                 "tags": [{"label": "Politics"}]} for k in range(n)]
        return httpx.Response(200, json={"markets": rows, "next_cursor": {0: "c1", 1: "c2", 2: None}[page]})

    ms = await GammaApi(http(handler)).listed_markets(closed=True, end_min_ts=1_780_000_000, end_max_ts=1_790_000_000,
                                                      min_volume=10_000)
    assert len(ms) == 217 and ms[0].tags == ("Politics",)
    assert [s[4] for s in seen] == [None, "c1", "c2"]
    assert seen[0][:4] == ("true", "2026-05-28T20:26:40Z", "2026-09-21T14:13:20Z", "10000")


async def test_gamma_listing_stops_if_the_cursor_does_not_advance():
    # a cursor the API ignores would otherwise return the first page forever
    def handler(request):
        return httpx.Response(200, json={"markets": [{"conditionId": "0x1", "closed": True,
                                                      "outcomePrices": "[\"1\",\"0\"]"}] * 100,
                                         "next_cursor": "same"})

    ms = await GammaApi(http(handler)).listed_markets(closed=True, end_min_ts=0, end_max_ts=1, min_volume=0)
    assert len(ms) <= 200


async def test_clob_price_window_asks_for_minute_prices_between_two_times():
    def handler(request):
        q = request.url.params
        assert (q["market"], q["startTs"], q["endTs"], q["fidelity"]) == ("tok", "100", "4000", "1")
        return httpx.Response(200, json={"history": [{"t": 160, "p": 0.41}, {"t": 220, "p": 0.43}]})

    pts = await ClobApi(http(handler)).price_window("tok", 100, 4000)
    assert [(p.ts, p.price) for p in pts] == [(160, 0.41), (220, 0.43)]
