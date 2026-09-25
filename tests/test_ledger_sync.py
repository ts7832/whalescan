"""scripts/ledger-sync.sh against a local bare repo — no network, no touching the real project's git config."""

import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ledger-sync.sh"


def run(mode: str, repo_dir: Path, work_dir: Path, remote_url: str) -> subprocess.CompletedProcess:
    env = {"LEDGER_REPO_URL": remote_url, "LEDGER_DIR": str(work_dir), "PATH": "/usr/bin:/bin:/usr/local/bin"}
    return subprocess.run(["bash", str(SCRIPT), mode], cwd=repo_dir, env=env, capture_output=True, text=True)


@pytest.fixture
def bare_remote(tmp_path):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "ledger", str(remote)], check=True)
    return f"file://{remote}"


def test_first_push_creates_the_branch_with_the_calls_file(tmp_path, bare_remote):
    work = tmp_path / "work"
    work.mkdir()
    (work / "calls.jsonl").write_text('{"id":"a"}\n')
    result = run("push", tmp_path, work, bare_remote)
    assert result.returncode == 0, result.stderr

    check = tmp_path / "check"
    subprocess.run(["git", "clone", "-q", "-b", "ledger", bare_remote, str(check)], check=True)
    assert (check / "calls.jsonl").read_text() == '{"id":"a"}\n'


def test_push_with_no_changes_does_nothing_and_still_succeeds(tmp_path, bare_remote):
    work = tmp_path / "work"
    work.mkdir()
    (work / "calls.jsonl").write_text('{"id":"a"}\n')
    run("push", tmp_path, work, bare_remote)
    before = subprocess.run(["git", "-C", str(work), "rev-parse", "HEAD"], capture_output=True, text=True).stdout

    result = run("push", tmp_path, work, bare_remote)
    assert result.returncode == 0, result.stderr
    after = subprocess.run(["git", "-C", str(work), "rev-parse", "HEAD"], capture_output=True, text=True).stdout
    assert before == after  # no empty commit was made


def test_pull_brings_down_a_change_pushed_from_elsewhere(tmp_path, bare_remote):
    work_a = tmp_path / "a"
    work_a.mkdir()
    (work_a / "calls.jsonl").write_text('{"id":"a"}\n')
    run("push", tmp_path, work_a, bare_remote)

    (work_a / "calls.jsonl").write_text('{"id":"a"}\n{"id":"b"}\n')
    run("push", tmp_path, work_a, bare_remote)

    work_b = tmp_path / "b"
    result = run("pull", tmp_path, work_b, bare_remote)
    assert result.returncode == 0, result.stderr
    assert (work_b / "calls.jsonl").read_text() == '{"id":"a"}\n{"id":"b"}\n'


def test_history_accumulates_across_pushes_never_force_pushed(tmp_path, bare_remote):
    work = tmp_path / "work"
    work.mkdir()
    for i in range(3):
        (work / "calls.jsonl").write_text(f'{{"id":"{i}"}}\n')
        result = run("push", tmp_path, work, bare_remote)
        assert result.returncode == 0, result.stderr

    log = subprocess.run(["git", "-C", str(work), "log", "--oneline"], capture_output=True, text=True).stdout
    assert len(log.strip().splitlines()) == 3  # three real commits, none rewritten away


def test_concurrent_push_rebases_instead_of_losing_the_other_writer(tmp_path, bare_remote):
    # writer A pushes first
    work_a = tmp_path / "a"
    work_a.mkdir()
    (work_a / "calls.jsonl").write_text('{"id":"a"}\n')
    run("push", tmp_path, work_a, bare_remote)

    # writer B started from the same state, pulls, then also has local work to push
    work_b = tmp_path / "b"
    run("pull", tmp_path, work_b, bare_remote)
    (work_b / "marks.jsonl").write_text('{"call_id":"a"}\n')
    result = run("push", tmp_path, work_b, bare_remote)
    assert result.returncode == 0, result.stderr

    check = tmp_path / "check"
    subprocess.run(["git", "clone", "-q", "-b", "ledger", bare_remote, str(check)], check=True)
    assert (check / "calls.jsonl").read_text() == '{"id":"a"}\n'
    assert (check / "marks.jsonl").read_text() == '{"call_id":"a"}\n'
