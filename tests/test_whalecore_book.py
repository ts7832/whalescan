import math

import numpy as np
import pytest

import whalecore


def levels(*pairs):
    return np.array(pairs, dtype=np.float64).reshape(-1, 2)


def test_book_from_numpy_and_walk():
    b = whalecore.OrderBook()
    b.apply_snapshot(levels((0.40, 100.0)), levels((0.50, 100.0), (0.52, 200.0)))
    w = b.walk(whalecore.Side.ASK, 100.0)
    assert w.complete
    assert w.vwap == pytest.approx(100.0 / (100.0 + 50.0 / 0.52))
    assert b.best_bid() == pytest.approx(0.40)


def test_empty_book_returns_none_and_nan():
    b = whalecore.OrderBook()
    b.apply_snapshot(levels(), levels())
    assert b.best_ask() is None
    w = b.walk(whalecore.Side.ASK, 50.0)
    assert not w.complete and math.isnan(w.vwap)


def test_bad_price_raises_value_error():
    b = whalecore.OrderBook()
    with pytest.raises(ValueError):
        b.apply_delta(whalecore.Side.BID, 2.0, 1.0)
