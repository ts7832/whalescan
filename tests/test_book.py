import math

from whalescan.book import follow_quote
from whalescan.models import BookSnapshot


def test_follow_quote_walks_asks():
    snap = BookSnapshot("t", 1_790_000_000_500, bids=((0.39, 100.0),), asks=((0.42, 1000.0), (0.41, 10000.0)))
    q = follow_quote(snap, 500.0)
    assert q.complete and abs(q.vwap - 0.41) < 1e-12
    assert q.book_as_of == 1_790_000_000
    assert q.best_bid == 0.39 and q.best_ask == 0.41
    assert q.levels == ((0.41, 10000.0), (0.42, 1000.0))


def test_follow_quote_on_empty_book():
    q = follow_quote(BookSnapshot("t", 0, (), ()), 500.0)
    assert not q.complete and math.isnan(q.vwap) and q.best_ask is None and q.levels == ()
