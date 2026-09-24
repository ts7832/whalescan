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
    return replace(base, paths=PathsCfg(research_db=str(tmp / "r.duckdb"), scores_parquet=str(tmp / "s.parquet"),
                                        snapshot_dir=str(tmp / "snap")))


class FakeGamma:
    skipped = 0

    def __init__(self):
        self.asked = []

    async def markets(self, ids):
        self.asked.append(set(ids))
        return {"0xc": MARKET} if "0xc" in ids else {}


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

    def reconnect_now(self):
        self.reconnects += 1


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
    await s.tick()                     # fetch unknown market, react to watch-set change, snapshot the new book
    assert gamma.asked == [{"0xc"}] and s.clob_socket.reconnects == 1 and clob.books == ["yes"]
    types = [(m["type"], m["data"].get("status")) for m in got if m["type"] in ("contact", "signal")]
    assert ("signal", "SIGNAL") in types


async def test_clob_messages_update_books_and_resync_on_mismatch(tmp_path):
    s, gamma, clob = station(tmp_path)
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


def test_hub_drops_messages_for_a_client_that_cannot_keep_up():
    hub = Hub(max_queue=2)
    q = hub.add()
    for i in range(5):
        hub.broadcast({"i": i})
    assert q.qsize() == 2 and hub.dropped == 3
