import numpy as np
import pytest

import whalecore


def test_skill_mc_from_numpy():
    r = whalecore.skill_mc(np.array([0.2, 0.4]), np.array([1, 1], dtype=np.uint8), np.array([1.0, 3.0]), n_sims=500, seed=1)
    assert r.edge == pytest.approx(0.65)
    assert 0 < r.p_value <= 1
    assert r.n_eff == pytest.approx(1.6)


def test_batch_returns_one_result_per_record():
    p = np.full(10, 0.5)
    y = np.array([1, 0] * 5, dtype=np.uint8)
    w = np.ones(10)
    offsets = np.array([0, 4, 10], dtype=np.int64)
    res = whalecore.skill_mc_batch(p, y, w, offsets, n_sims=200, seed=3, n_threads=2)
    assert len(res) == 2
    assert res[0].edge == pytest.approx(0.0)


def test_batch_with_no_records():
    empty = np.array([], dtype=np.float64)
    res = whalecore.skill_mc_batch(empty, np.array([], dtype=np.uint8), empty, np.array([0], dtype=np.int64), n_sims=10)
    assert res == []


def test_invalid_input_raises_value_error():
    with pytest.raises(ValueError, match="prices must be in"):
        whalecore.skill_mc(np.array([1.5]), np.array([1], dtype=np.uint8), np.array([1.0]), n_sims=10)
