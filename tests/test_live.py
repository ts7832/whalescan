import json
from dataclasses import replace

import pandas as pd
from fastapi.testclient import TestClient

from whalescan.config import PathsCfg, load_config
from whalescan.live import Hub, Station, create_app
from whalescan.models import BookSnapshot, Market
from whalescan.scoring import SCORE_COLUMNS

NOW = 1_790_000_000
MARKET = Market("0xc", "Who wins?", "who-wins", "us-election", NOW + 48 * 3600, False, None, (0.4, 0.6),
                ("yes", "no"), False, 0.0, 1.0, 1e6, ("Politics",))


def scores():
    row = {"n": 100, "n_eff": 100.0, "edge": 0.08, "sigma": 0.02, "post_edge": 0.06, "p_value": 0.001,
           "bh_pass": True, "certified": True, "flags": "", "median_stake": 2000.0, "as_of": NOW}
    return pd.DataFrame([dict(row, wallet="0xwhale", category="ALL"), dict(row, wallet="0xwhale", category="POLITICS")],
                        columns=SCORE_COLUMNS)


def config(tmp):
    base = load_config()
    base = replace(base, gate=replace(base.gate, skill_mode="certified"))
    return replace(base, paths=PathsCfg(research_db=str(tmp / "r.duckdb"), scores_parquet=str(tmp / "s.parquet"),
                                        snapshot_dir=str(tmp / "snap")))


class FakeGamma:
    skipped = 0

    def __init__(self):
        self.asked = []

    async def markets(self, ids):
        self.asked.append(set(ids))
        return {"0xc": MARKET} if "0xc" in ids else {}

    async def created_ts(self, wallet):
        return NOW - 86400 if wallet.startswith("0xfresh") else NOW - 400 * 86400


class FakeClob:
    skipped = 0

    def __init__(self):
        self.books = []

    async def book(self, token_id):
        self.books.append(token_id)
        return BookSnapshot(token_id, NOW * 1000, ((0.39, 5000.0),), ((0.41, 100_000.0),))


class FakeSocket:
    def __init__(self):
        self.reconnects = 0
        self.connects = 1
        self.messages = 0
        self.sent = []

    def reconnect_now(self):
        self.reconnects += 1

    async def send(self, msg):
        self.sent.append(json.loads(msg))
        return True


def rtds_frame(wallet="0xWHALE", usdc=10_000.0, price=0.40, tx="0xt1", side="BUY"):
    payload = {"proxyWallet": wallet, "side": side, "asset": "yes", "conditionId": "0xc", "size": usdc / price,
               "price": price, "timestamp": NOW - 5, "transactionHash": tx, "outcome": "Yes", "outcomeIndex": 0,
               "eventSlug": "us-election", "title": "Who wins?"}
    return json.dumps({"topic": "activity", "type": "trades", "timestamp": (NOW - 1) * 1000, "payload": payload})


def station(tmp, **kw):
    gamma, clob = FakeGamma(), FakeClob()
    s = Station(config(tmp), scores=scores(), gamma=gamma, clob=clob, clock=lambda: NOW, **kw)
    s.clob_socket = FakeSocket()
    return s, gamma, clob


async def test_trade_flows_to_signal_through_maintenance(tmp_path):
    s, gamma, clob = station(tmp_path)
    got = []
    s.hub.subscribe_callback(got.append)
    await s.handle_rtds(rtds_frame())
    assert s.feed_latency_ms == 1000
    await s.tick()                     # fetch unknown market, react to watch-set change
    assert gamma.asked == [{"0xc"}] and s.clob_socket.reconnects == 0
    assert s.clob_socket.sent == [{"assets_ids": ["yes"], "operation": "subscribe"}]
    # the server answers an incremental subscribe with the book snapshot:
    await s.handle_clob(json.dumps({"event_type": "book", "asset_id": "yes", "timestamp": str(NOW * 1000),
                                    "bids": [{"price": "0.39", "size": "5000"}], "asks": [{"price": "0.41", "size": "100000"}]}))
    types = [(m["type"], m["data"].get("status")) for m in got if m["type"] in ("contact", "signal")]
    assert ("signal", "SIGNAL") in types


async def test_clob_messages_update_books_and_resync_on_mismatch(tmp_path):
    s, gamma, clob = station(tmp_path)
    s.state.books.subscribe({"yes"})
    await s.handle_clob(json.dumps({"event_type": "book", "asset_id": "yes", "timestamp": str(NOW * 1000),
                                    "bids": [{"price": "0.39", "size": "10"}], "asks": [{"price": "0.41", "size": "10"}]}))
    await s.handle_clob(json.dumps({"event_type": "price_change", "market": "0xc", "timestamp": str(NOW * 1000 + 5),
                                    "price_changes": [{"asset_id": "yes", "price": "0.40", "size": "5", "side": "BUY",
                                                       "hash": "h", "best_bid": "0.38", "best_ask": "0.41"}]}))
    assert "yes" in s.state.books.desynced
    await s.tick()
    assert clob.books == ["yes"] and "yes" not in s.state.books.desynced


async def test_large_trades_are_persisted_for_restart(tmp_path):
    s, gamma, clob = station(tmp_path)
    await s.handle_rtds(rtds_frame(wallet="0xsomeone", usdc=2_000.0, tx="0xbig"))
    await s.handle_rtds(rtds_frame(wallet="0xsomeone", usdc=10.0, tx="0xtiny"))
    await s.tick()
    assert [t.tx_hash for t in s.store_trades()] == ["0xbig"]
    s.close()


def test_http_state_and_websocket_push(tmp_path):
    hub = Hub()
    snapshot = {"meta": {"mode": "LIVE"}, "signals": [], "contacts": [], "whales": [], "validation": None}
    app = create_app(lambda: snapshot, hub, static_dir=None)
    with TestClient(app) as c:
        assert c.get("/api/state").json()["meta"]["mode"] == "LIVE"
        with c.websocket_connect("/ws") as ws:
            assert ws.receive_json() == {"type": "state", "data": snapshot}
            hub.broadcast({"type": "contact", "data": {"id": "x"}})
            assert ws.receive_json() == {"type": "contact", "data": {"id": "x"}}




def signal_frame(tx="0xt1"):
    return rtds_frame(tx=tx)


async def _open_signal(s):
    await s.handle_rtds(signal_frame())
    await s.tick()          # market fetched; watch set applied via an incremental subscribe
    await s.handle_clob(json.dumps({"event_type": "book", "asset_id": "yes", "timestamp": str(NOW * 1000),
                                    "bids": [{"price": "0.39", "size": "5000"}], "asks": [{"price": "0.41", "size": "100000"}]}))


async def test_book_pushes_are_coalesced_and_the_last_state_is_flushed(tmp_path):
    s, gamma, clob = station(tmp_path)
    got = []
    s.hub.subscribe_callback(got.append)
    await _open_signal(s)
    for px in ("0.395", "0.396"):   # a burst inside one throttle window (bids stay below the 0.41 ask)
        await s.handle_clob(json.dumps({"event_type": "price_change", "market": "0xc", "timestamp": str(NOW * 1000 + 9),
                                        "price_changes": [{"asset_id": "yes", "price": px, "size": "10", "side": "BUY",
                                                           "hash": "h", "best_bid": px, "best_ask": "0.41"}]}))
    got.clear()
    s.flush_books()
    books = [m for m in got if m["type"] == "book"]
    assert len(books) == 1 and books[0]["data"]["quote"]["best_bid"] == 0.396


async def test_watch_changes_are_incremental_subscribe_and_unsubscribe(tmp_path):
    s, gamma, clob = station(tmp_path)
    await s.handle_rtds(signal_frame())
    await s.tick()
    await s.handle_rtds(rtds_frame(tx="0xt2", wallet="0xwhale", price=0.4).replace('"asset": "yes"', '"asset": "other"'))
    await s.tick()
    assert s.clob_socket.sent[-1] == {"assets_ids": ["other"], "operation": "subscribe"}
    assert "other" in s.state.books.subscribed and s.clob_socket.reconnects == 0
    s.clock = lambda: NOW + 25 * 3600          # everything expires -> unsubscribe both
    await s.tick()
    assert s.clob_socket.sent[-1] == {"assets_ids": ["other", "yes"], "operation": "unsubscribe"}
    assert s.state.books.subscribed == set()


async def test_clob_link_down_disables_quotes(tmp_path):
    s, gamma, clob = station(tmp_path)
    await _open_signal(s)
    s._on_status("clob")("backoff", {})
    assert s.state.books.quote("yes", 100.0) is None


def test_slow_client_gets_a_fresh_state_instead_of_gaps():
    hub = Hub(max_queue=2, state_provider=lambda: {"fresh": True})
    q = hub.add()
    for i in range(5):
        hub.broadcast({"i": i})
    items = [q.get_nowait() for _ in range(q.qsize())]
    assert items[-1] == {"type": "state", "data": {"fresh": True}}


def test_websocket_rejects_foreign_origins():
    import pytest
    from starlette.websockets import WebSocketDisconnect

    app = create_app(lambda: {"meta": {}}, Hub(), static_dir=None)
    with TestClient(app) as c:
        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect("/ws", headers={"origin": "https://evil.example"}) as ws:
                ws.receive_json()
        with c.websocket_connect("/ws", headers={"origin": "http://testserver"}) as ws:
            assert ws.receive_json()["type"] == "state"


class FakeData:
    skipped = 0

    def __init__(self):
        self.calls = []

    async def markets_traded(self, wallet):
        return 1

    async def trades(self, *, user=None, min_usdc=None, since_ts=None):
        from whalescan.api.data_api import TradePage
        from whalescan.models import Trade
        self.calls.append(user)
        rows = [Trade("0xw1", NOW - 100, "0xwhale", "yes", "0xc", "BUY", 0.40, 25_000.0, "us-election", "Who wins?",
                      "Yes", 0, None)] if user in (None, "0xwhale") else []
        return TradePage(rows, True)


async def test_warm_up_replays_recent_trades_from_rest_and_disk(tmp_path):
    gamma, clob, data = FakeGamma(), FakeClob(), FakeData()
    s = Station(config(tmp_path), scores=scores(), gamma=gamma, clob=clob, data=data, clock=lambda: NOW)
    s.clob_socket = FakeSocket()
    await s.warm_up()
    assert set(data.calls) == {None, "0xwhale"}
    assert len(s.state.events) == 1                      # the same fill from both sources counts once
    assert s.watch == ["yes"] and s.clob_socket.sent[-1]["operation"] == "subscribe"
    s.close()


async def test_station_fetches_profiles_for_fresh_big_news_bets(tmp_path):
    s, gamma, clob = station(tmp_path)
    s.data = FakeData()
    got = []
    s.hub.subscribe_callback(got.append)
    await s.handle_rtds(rtds_frame(wallet="0xFRESH9", usdc=30_000.0, tx="0xf"))
    await s.tick()      # market + profile fetched, book subscribed
    await s.handle_clob(json.dumps({"event_type": "book", "asset_id": "yes", "timestamp": str(NOW * 1000),
                                    "bids": [{"price": "0.39", "size": "5000"}], "asks": [{"price": "0.41", "size": "100000"}]}))
    assert ("signal", "INSIDER") in [(m["type"], m["data"].get("status")) for m in got]
    assert s.meta()["counts"]["insiders"] == 1
