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


async def test_each_host_gets_its_own_rate_limit():
    now = {"t": 0.0}
    sleeps = []

    async def sleep(s):
        sleeps.append(s)
        now["t"] += s

    c = HttpClient(user_agent="t", rate_per_s=1.0, max_retries=0, sleep=sleep, clock=lambda: now["t"],
                   host_rates={"fast.test": 4.0},
                   transport=httpx.MockTransport(lambda r: httpx.Response(200, json=1)))
    for _ in range(4):
        await c.get_json("https://fast.test/x")   # burst 4 at 4/s: no waiting
    assert sleeps == []
    await c.get_json("https://slow.test/x")       # default bucket, burst 1: first call free
    await c.get_json("https://slow.test/x")       # second call waits 1 s at 1/s
    assert sleeps == [pytest.approx(1.0)]
