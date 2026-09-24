import json
from pathlib import Path

from whalescan.models import BookSnapshot
from whalescan.stream.messages import PriceChanges, parse_clob, parse_rtds

REAL = Path(__file__).parent / "fixtures" / "real"


def test_real_rtds_frames_parse_to_trades_with_send_time():
    for frame in json.loads((REAL / "rtds_trades.json").read_text()):
        trade, sent_ms = parse_rtds(json.dumps(frame))
        assert trade.wallet == trade.wallet.lower() and trade.side in ("BUY", "SELL")
        assert sent_ms == frame["timestamp"] and trade.ts == frame["payload"]["timestamp"]


def test_rtds_keepalives_and_other_topics_are_ignored():
    assert parse_rtds("") is None
    assert parse_rtds("   ") is None
    assert parse_rtds("not json") is None
    assert parse_rtds(json.dumps({"topic": "comments", "type": "x", "payload": {}})) is None
    assert parse_rtds(json.dumps({"topic": "activity", "type": "trades", "payload": {"junk": 1}})) is None


def test_real_clob_messages_parse_to_books_and_changes():
    msgs = json.loads((REAL / "clob_ws.json").read_text())
    out = [x for m in msgs for x in parse_clob(json.dumps(m))]
    books = [x for x in out if isinstance(x, BookSnapshot)]
    changes = [x for x in out if isinstance(x, PriceChanges)]
    assert books and changes
    c = changes[0]
    assert len(c.assets) == len(c.sides) == len(c.prices) == len(c.sizes) == len(c.best_bids) == len(c.best_asks)
    assert set(c.sides) <= {0, 1}


def test_clob_list_payloads_and_unknown_events():
    book = {"event_type": "book", "asset_id": "7", "timestamp": "5", "bids": [{"price": "0.4", "size": "1"}], "asks": []}
    change = {"event_type": "price_change", "market": "m", "timestamp": "6", "price_changes": [
        {"asset_id": "7", "price": "0.41", "size": "0", "side": "BUY", "hash": "h", "best_bid": "0.4", "best_ask": "0"}]}
    out = parse_clob(json.dumps([book, change, {"event_type": "last_trade_price"}]))
    assert isinstance(out[0], BookSnapshot) and out[0].bids == ((0.4, 1.0),)
    assert isinstance(out[1], PriceChanges) and out[1].sides == (0,) and out[1].best_asks == (None,)
    assert parse_clob("PONG") == [] and parse_clob("") == []
