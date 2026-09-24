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
    assert hist.complete and not hist.truncated and len(hist.positions) == 107
    assert seen == [("TIMESTAMP", "DESC", 0, 50), ("TIMESTAMP", "DESC", 50, 50), ("TIMESTAMP", "DESC", 100, 50)]


async def test_closed_positions_offset_cap_keeps_recent_time_window():
    # Sorted by TIMESTAMP, a history cut off at the API's depth limit is the most recent window:
    # unbiased with respect to outcomes, so it stays usable (complete) but is marked truncated.
    def handler(request):
        if int(request.url.params["offset"]) >= 100:
            return httpx.Response(400, json={"error": "max historical trades offset of 10000 exceeded"})
        return httpx.Response(200, json=[pos(k, 5000 - k) for k in range(50)])

    hist = await DataApi(http(handler)).closed_positions("0xw")
    assert hist.complete and hist.truncated
    assert len(hist.positions) == 100


async def test_closed_positions_refused_on_first_page_is_incomplete():
    hist = await DataApi(http(lambda r: httpx.Response(
        400, json={"error": "max historical trades offset of 10000 exceeded"}))).closed_positions("0xw")
    assert not hist.complete and not hist.truncated and hist.positions == []


async def test_closed_positions_error_object_raises_instead_of_truncating():
    # Only the explicit offset-cap 400 means "history ends here". Any other error body must fail the
    # fetch, so nothing partial is stored and the next run retries in full.
    def handler(request):
        if int(request.url.params["offset"]) >= 50:
            return httpx.Response(200, json={"error": "temporary"})
        return httpx.Response(200, json=[pos(k, 5000 - k) for k in range(50)])

    with pytest.raises(ApiError, match="error object"):
        await DataApi(http(handler)).closed_positions("0xw")


async def test_closed_positions_stops_at_local_offset_cap(monkeypatch):
    monkeypatch.setattr(data_api_module, "MAX_OFFSET", 100)
    hist = await DataApi(http(lambda r: httpx.Response(200, json=[pos(k, 9999) for k in range(50)]))).closed_positions("0xw")
    assert hist.complete and hist.truncated
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
    # 250 ids -> closed pass in chunks of 100; the 125 odd (open) ids left -> open pass in chunks of 100
    assert calls == [(100, "true", "true", "100"), (100, "true", "true", "100"), (50, "true", "true", "100"),
                     (100, "false", "true", "100"), (25, "false", "true", "100")]


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
        seen.append((request.url.path, q["user"], q["sizeThreshold"], int(q["offset"])))
        off = int(q["offset"])
        n = 500 if off == 0 else 3
        rows = [dict(pos(off + k, 0), redeemable=(k % 2 == 0), curPrice=0 if k % 2 == 0 else 0.4) for k in range(n)]
        return httpx.Response(200, json=rows)

    hist = await DataApi(http(handler)).redeemable_positions("0xw")
    assert hist.complete
    assert len(hist.positions) == 250 + 2
    assert all(p.ts == 0 for p in hist.positions)
    assert seen == [("/positions", "0xw", "0", 0), ("/positions", "0xw", "0", 500)]


async def test_truncated_redeemable_positions_are_incomplete():
    # /positions has no known outcome-neutral order, so a depth-capped fetch is not a trustworthy window.
    def handler(request):
        if int(request.url.params["offset"]) >= 500:
            return httpx.Response(400, json={"error": "max historical trades offset of 10000 exceeded"})
        return httpx.Response(200, json=[dict(pos(k, 0), redeemable=True, curPrice=0) for k in range(500)])

    hist = await DataApi(http(handler)).redeemable_positions("0xw")
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
        assert request.url.path == "/traded" and request.url.params["user"] == "0xw"
        return httpx.Response(200, json={"user": "0xw", "traded": 3})

    from whalescan.api.profiles import fetch_profile
    h = http(handler)
    p = await fetch_profile(GammaApi(h), DataApi(h), "0xW", now=1_790_000_000)
    assert p.wallet == "0xw" and p.markets_traded == 3 and p.fetched_at == 1_790_000_000
    assert p.created_ts == 1790157600


async def test_wallet_profile_tolerates_unknown_accounts():
    def handler(request):
        if request.url.path == "/public-profile":
            return httpx.Response(404, json={"error": "profile not found"})
        return httpx.Response(200, json={"user": "0xw", "traded": 0})

    from whalescan.api.profiles import fetch_profile
    h = http(handler)
    p = await fetch_profile(GammaApi(h), DataApi(h), "0xw", now=5)
    assert p.created_ts is None and p.markets_traded == 0
