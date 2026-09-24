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


async def test_closed_positions_error_object_with_200_marks_incomplete():
    hist = await DataApi(http(lambda r: httpx.Response(200, json={"error": "nope"}))).closed_positions("0xw")
    assert not hist.complete and hist.positions == []


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
