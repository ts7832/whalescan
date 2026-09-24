import math

from whalescan.models import BookSnapshot
from whalescan.stream.books import LiveBooks
from whalescan.stream.messages import PriceChanges


def snap(asset="7", bids=((0.40, 100.0),), asks=((0.42, 5000.0),), ts_ms=1_000):
    return BookSnapshot(asset, ts_ms, bids, asks)


def change(asset="7", side=0, price=0.41, size=50.0, best_bid=0.41, best_ask=0.42, ts_ms=2_000):
    return PriceChanges(ts_ms, (asset,), (side,), (price,), (size,), (best_bid,), (best_ask,))


def test_snapshot_then_consistent_change_keeps_book_in_sync():
    b = LiveBooks()
    b.on_snapshot(snap())
    assert b.on_changes(change()) == set()
    q = b.quote("7", 1000.0)
    assert q.best_bid == 0.41 and q.best_ask == 0.42 and q.complete and math.isclose(q.vwap, 0.42)
    assert q.book_as_of == 2  # seconds of the last applied message


def test_disagreeing_with_server_top_of_book_marks_desync():
    b = LiveBooks()
    b.on_snapshot(snap())
    assert b.on_changes(change(best_bid=0.43)) == {"7"}   # server says 0.43, we computed 0.41
    assert "7" in b.desynced and b.quote("7", 100.0) is None
    b.on_snapshot(snap())                                  # a fresh snapshot heals it
    assert "7" not in b.desynced and b.quote("7", 100.0) is not None


def test_off_grid_price_marks_desync_without_mutating():
    b = LiveBooks()
    b.on_snapshot(snap())
    assert b.on_changes(change(price=0.41005, best_bid=0.41005)) == {"7"}
    assert b.book("7").best_bid() == 0.40


def test_changes_for_unknown_assets_are_ignored():
    b = LiveBooks()
    assert b.on_changes(change(asset="nope")) == set()
    assert b.quote("nope", 100.0) is None


def test_mixed_assets_in_one_message():
    b = LiveBooks()
    b.on_snapshot(snap("a"))
    b.on_snapshot(snap("b"))
    msg = PriceChanges(3_000, ("a", "b"), (1, 1), (0.42, 0.45), (0.0, 10.0), (0.40, 0.40), (None, 0.42))
    assert b.on_changes(msg) == set()
    assert b.book("a").best_ask() is None and b.book("b").best_ask() == 0.42


def test_forget_drops_books():
    b = LiveBooks()
    b.on_snapshot(snap("a"))
    b.forget({"a"})
    assert b.book("a") is None
