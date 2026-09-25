import json
import logging

from whalescan.ledger import Ledger

DAY = 86400
NOW = 1_790_000_000


def call(id_="0xc:yes:BUY:100:INSIDER", call_ts=NOW, schedule=None, kind="INSIDER"):
    schedule = schedule if schedule is not None else [{"day": 7, "due_ts": call_ts + 7 * DAY},
                                                       {"day": 14, "due_ts": call_ts + 14 * DAY}]
    return {"id": id_, "kind": kind, "call_ts": call_ts, "schedule": schedule, "wallet": "0xw",
            "condition_id": "0xc", "entry_cost": 0.42}


def mark(call_id="0xc:yes:BUY:100:INSIDER", type_="CHECKPOINT", day=7, at=NOW + 7 * DAY, return_pct=0.1):
    return {"call_id": call_id, "type": type_, "day": day, "at": at, "return_pct": return_pct}


def test_append_and_reload_calls_and_marks(tmp_path):
    led = Ledger(tmp_path)
    led.append_call(call())
    led.append_mark(mark())
    reloaded = Ledger(tmp_path)
    assert [c["id"] for c in reloaded.calls()] == ["0xc:yes:BUY:100:INSIDER"]
    assert [m["type"] for m in reloaded.marks()] == ["CHECKPOINT"]


def test_has_call_prevents_double_logging():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        led = Ledger(d)
        assert not led.has_call("0xc:yes:BUY:100:INSIDER")
        led.append_call(call())
        assert led.has_call("0xc:yes:BUY:100:INSIDER")
        assert not led.has_call("some-other-id")


def test_corrupt_line_is_skipped_not_fatal(tmp_path):
    (tmp_path / "calls.jsonl").write_text(json.dumps(call()) + "\n" + "{not json\n" + json.dumps(call("0xd:yes:BUY:1:INSIDER")) + "\n")
    led = Ledger(tmp_path)
    assert len(led.calls()) == 2


def test_open_calls_excludes_settled(tmp_path):
    led = Ledger(tmp_path)
    led.append_call(call("a"))
    led.append_call(call("b"))
    led.append_mark(mark("a", type_="SETTLEMENT", day=None, return_pct=1.0))
    assert [c["id"] for c in led.open_calls()] == ["b"]


def test_due_checkpoints_returns_each_due_day_once(tmp_path):
    led = Ledger(tmp_path)
    led.append_call(call("a", call_ts=NOW))
    due = led.due_checkpoints(now=NOW + 8 * DAY)
    assert [(c["id"], day) for c, day, due_ts in due] == [("a", 7)]


def test_due_checkpoints_catches_up_after_a_late_sweep(tmp_path):
    # a sweep that runs once after 20 days must return BOTH the day-7 and day-14 checkpoints,
    # each exactly once, so a late sweep never silently skips one.
    led = Ledger(tmp_path)
    led.append_call(call("a", call_ts=NOW))
    due = led.due_checkpoints(now=NOW + 20 * DAY)
    assert sorted(day for _c, day, _ts in due) == [7, 14]


def test_due_checkpoints_skips_days_already_marked(tmp_path):
    led = Ledger(tmp_path)
    led.append_call(call("a", call_ts=NOW))
    led.append_mark(mark("a", day=7, at=NOW + 7 * DAY))
    due = led.due_checkpoints(now=NOW + 20 * DAY)
    assert [day for _c, day, _ts in due] == [14]


def test_settled_call_has_no_due_checkpoints(tmp_path):
    led = Ledger(tmp_path)
    led.append_call(call("a", call_ts=NOW))
    led.append_mark(mark("a", type_="SETTLEMENT", day=None, return_pct=1.0))
    assert led.due_checkpoints(now=NOW + 100 * DAY) == []


def test_latest_mark_is_the_most_recent_by_time(tmp_path):
    led = Ledger(tmp_path)
    led.append_call(call("a", call_ts=NOW))
    assert led.latest_mark("a") is None
    led.append_mark(mark("a", day=7, at=NOW + 7 * DAY, return_pct=0.1))
    led.append_mark(mark("a", day=14, at=NOW + 14 * DAY, return_pct=0.2))
    assert led.latest_mark("a")["return_pct"] == 0.2


def test_corrupt_line_is_logged(tmp_path, caplog):
    (tmp_path / "calls.jsonl").write_text("{not json\n")
    with caplog.at_level(logging.WARNING):
        Ledger(tmp_path).calls()
    assert "calls.jsonl" in caplog.text
