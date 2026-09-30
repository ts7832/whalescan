import gzip
import json

from whalescan.archive import archive_round, read_archive
from whalescan.models import Trade
from whalescan.store import Store

NOW = 1_790_000_000  # 2026-09-21 14:13:20 UTC


def fill(tx, ts, usdc=2000.0, wallet="0xw"):
    return Trade(tx, ts, wallet, "tok", "0xc", "BUY", 0.5, usdc / 0.5, "ev", "q", "Yes", 0, None)


def test_a_round_writes_one_new_immutable_file_named_by_its_utc_time(tmp_path):
    path = archive_round(tmp_path, [fill("a", NOW - 60), fill("b", NOW - 30)], NOW)
    assert path == tmp_path / "fills" / "2026-09-21" / "141320.jsonl.gz"
    rows = [json.loads(line) for line in gzip.open(path, "rt")]
    assert [r["tx_hash"] for r in rows] == ["a", "b"] and rows[0]["usdc"] == 2000.0


def test_nothing_new_writes_nothing(tmp_path):
    assert archive_round(tmp_path, [], NOW) is None
    assert not (tmp_path / "fills").exists()


def test_the_archive_reads_back_every_round_in_order(tmp_path):
    archive_round(tmp_path, [fill("a", NOW - 60)], NOW)
    archive_round(tmp_path, [fill("b", NOW + 840)], NOW + 900)
    assert [t.tx_hash for t in read_archive(tmp_path)] == ["a", "b"]


def test_store_reports_only_fills_it_has_not_seen(tmp_path):
    with Store(tmp_path / "s.duckdb") as s:
        s.upsert_trades([fill("a", NOW)])
        new = s.new_trades([fill("a", NOW), fill("b", NOW), fill("a", NOW, usdc=3000.0)])  # same tx, other fill
        assert [(t.tx_hash, t.usdc) for t in new] == [("b", 2000.0), ("a", 3000.0)]


def test_reading_the_archive_drops_fills_archived_twice(tmp_path):
    # after a lost sweep cache the first round re-archives the last 24 h: readers must never double-count
    archive_round(tmp_path, [fill("a", NOW - 60), fill("b", NOW - 30)], NOW)
    archive_round(tmp_path, [fill("a", NOW - 60), fill("c", NOW + 10)], NOW + 900)
    assert [t.tx_hash for t in read_archive(tmp_path)] == ["a", "b", "c"]
