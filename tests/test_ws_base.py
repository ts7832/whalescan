import asyncio

import pytest
from websockets.asyncio.server import serve

from whalescan.stream.ws_base import ReconnectingWS


class Server:
    """Local WS server whose behaviour per connection is scripted by `handler(ws, n)`."""

    def __init__(self, handler):
        self.handler, self.connections, self.received = handler, 0, []

    async def _handle(self, ws):
        self.connections += 1
        n = self.connections
        await self.handler(ws, n, self)

    async def __aenter__(self):
        self._srv = await serve(self._handle, "127.0.0.1", 0)
        self.url = f"ws://127.0.0.1:{self._srv.sockets[0].getsockname()[1]}"
        return self

    async def __aexit__(self, *exc):
        self._srv.close()
        await self._srv.wait_closed()


async def recv_all(ws, server):
    async for m in ws:
        server.received.append(m)


def client(url, got, statuses, **kw):
    async def on_message(raw):
        got.append(raw)

    return ReconnectingWS("test", url, subscribe=lambda: ["SUB"], on_message=on_message,
                          on_status=lambda s, info: statuses.append(s), rng=lambda: 0.5,
                          backoff_min_s=0.01, backoff_max_s=0.05, **kw)


async def wait_for(cond, timeout=3.0):
    t = asyncio.get_running_loop().time()
    while not cond():
        if asyncio.get_running_loop().time() - t > timeout:
            raise AssertionError("condition not met")
        await asyncio.sleep(0.01)


async def test_subscribes_and_delivers_messages():
    async def handler(ws, n, srv):
        srv.received.append(await ws.recv())
        await ws.send("a")
        await ws.send("b")
        await recv_all(ws, srv)

    got, statuses = [], []
    async with Server(handler) as srv:
        c = client(srv.url, got, statuses)
        task = asyncio.create_task(c.run())
        await wait_for(lambda: got == ["a", "b"])
        assert srv.received[0] == "SUB" and "connected" in statuses and c.messages == 2
        c.stop()
        await asyncio.wait_for(task, 2)


async def test_reconnects_and_resubscribes_after_drop():
    async def handler(ws, n, srv):
        srv.received.append(await ws.recv())
        await ws.send(f"hello{n}")
        if n == 1:
            await ws.close()
        else:
            await recv_all(ws, srv)

    got, statuses = [], []
    async with Server(handler) as srv:
        c = client(srv.url, got, statuses)
        task = asyncio.create_task(c.run())
        await wait_for(lambda: got == ["hello1", "hello2"])
        assert srv.received.count("SUB") == 2 and "disconnected" in statuses
        c.stop()
        await asyncio.wait_for(task, 2)


async def test_silent_connection_is_treated_as_dead():
    async def handler(ws, n, srv):
        await ws.recv()
        if n == 1:
            await asyncio.sleep(5)  # alive at TCP level but says nothing
        await ws.send("fresh")
        await recv_all(ws, srv)

    got, statuses = [], []
    async with Server(handler) as srv:
        c = client(srv.url, got, statuses, dead_after_s=0.3)
        task = asyncio.create_task(c.run())
        await wait_for(lambda: got == ["fresh"])
        assert "silent" in statuses and srv.connections == 2
        c.stop()
        await asyncio.wait_for(task, 2)


async def test_backoff_grows_and_is_capped_when_server_is_down():
    delays = []

    async def fake_sleep(s):
        delays.append(s)
        if len(delays) >= 5:
            c.stop()

    c = ReconnectingWS("down", "ws://127.0.0.1:9", subscribe=lambda: [], on_message=lambda raw: None,
                       rng=lambda: 0.5, backoff_min_s=1.0, backoff_max_s=4.0, sleep=fake_sleep)
    await asyncio.wait_for(c.run(), 5)
    assert delays == [1.0, 2.0, 4.0, 4.0, 4.0]  # jitter factor 0.5 + rng() == 1.0


async def test_app_level_ping_is_sent_periodically():
    async def handler(ws, n, srv):
        await recv_all(ws, srv)

    got, statuses = [], []
    async with Server(handler) as srv:
        c = client(srv.url, got, statuses, app_ping="PING", ping_interval_s=0.05)
        task = asyncio.create_task(c.run())
        await wait_for(lambda: srv.received.count("PING") >= 2)
        c.stop()
        await asyncio.wait_for(task, 2)


async def test_reconnect_now_forces_a_fresh_subscription():
    subs = iter([["SUB1"], ["SUB2"]])

    async def handler(ws, n, srv):
        await recv_all(ws, srv)

    got, statuses = [], []
    async with Server(handler) as srv:
        async def on_message(raw):
            got.append(raw)

        c = ReconnectingWS("t", srv.url, subscribe=lambda: next(subs), on_message=on_message, rng=lambda: 0.5,
                           backoff_min_s=0.01)
        task = asyncio.create_task(c.run())
        await wait_for(lambda: "SUB1" in srv.received)
        c.reconnect_now()
        await wait_for(lambda: "SUB2" in srv.received)
        c.stop()
        await asyncio.wait_for(task, 2)


def test_rejects_bad_backoff():
    with pytest.raises(ValueError):
        ReconnectingWS("x", "ws://x", subscribe=list, on_message=print, backoff_min_s=0)


async def test_handler_exception_does_not_kill_the_connection():
    async def handler(ws, n, srv):
        await ws.recv()
        await ws.send("boom")
        await ws.send("ok")
        await recv_all(ws, srv)

    got = []

    async def on_message(raw):
        if raw == "boom":
            raise RuntimeError("bug in handler")
        got.append(raw)

    async with Server(handler) as srv:
        c = ReconnectingWS("t", srv.url, subscribe=lambda: ["SUB"], on_message=on_message, rng=lambda: 0.5,
                           backoff_min_s=0.01)
        task = asyncio.create_task(c.run())
        await wait_for(lambda: got == ["ok"])
        assert srv.connections == 1
        c.stop()
        await asyncio.wait_for(task, 2)


async def test_backoff_is_not_reset_by_connections_that_die_before_any_message():
    delays = []

    async def handler(ws, n, srv):
        await ws.close()   # accepts the handshake, then drops immediately

    async with Server(handler) as srv:
        async def fake_sleep(s):
            delays.append(s)
            if len(delays) >= 3:
                c.stop()

        c = ReconnectingWS("t", srv.url, subscribe=lambda: [], on_message=lambda raw: None, rng=lambda: 0.5,
                           backoff_min_s=1.0, backoff_max_s=8.0, sleep=fake_sleep)
        await asyncio.wait_for(c.run(), 5)
    assert delays == [1.0, 2.0, 4.0]
