from pathlib import Path

import pytest

from whalescan.config import ROOT, load_config


def test_loads_repo_config():
    cfg = load_config()
    assert cfg.scoring.bh_q == 0.10
    assert cfg.gate.min_usdc == 5000
    assert cfg.http.user_agent.startswith("whalescan/")
    assert set(cfg.categories.order) <= set(cfg.categories.tags)
    assert all(t == t.lower() for tags in cfg.categories.tags.values() for t in tags)


def test_relative_paths_resolve_from_repo_root(tmp_path):
    cfg = load_config()
    assert cfg.path("data/x.duckdb") == ROOT / "data/x.duckdb"
    assert cfg.path(str(tmp_path / "abs.duckdb")) == tmp_path / "abs.duckdb"


def test_missing_key_is_an_error(tmp_path):
    text = (ROOT / "config.toml").read_text().replace("bh_q = 0.10\n", "")
    p = tmp_path / "c.toml"
    p.write_text(text)
    with pytest.raises(ValueError, match="scoring.bh_q"):
        load_config(p)


def test_unknown_key_is_an_error(tmp_path):
    text = (ROOT / "config.toml").read_text().replace("bh_q = 0.10\n", "bh_q = 0.10\nbhq = 1\n")
    p = tmp_path / "c.toml"
    p.write_text(text)
    with pytest.raises(ValueError, match="bhq"):
        load_config(p)
