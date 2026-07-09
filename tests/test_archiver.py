from __future__ import annotations

from io import StringIO
import json

import pytest

from s2s import archiver
from s2s.archiver import archive_transcript, backfill
from s2s.cli import main
from s2s.ledger import Ledger


def _transcript(tmp_path, project: str = "demo-project", session: str = "session-1"):
    path = tmp_path / ".claude" / "projects" / project / f"{session}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"type":"user","message":{"role":"user","content":"hello"}}\n')
    return path


def test_archive_is_content_idempotent_and_changed_content_requeues(
    monkeypatch: pytest.MonkeyPatch, tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    s2s_home = tmp_path / "s2s-home"
    monkeypatch.setenv("S2S_HOME", str(s2s_home))
    source = _transcript(tmp_path)

    first = archive_transcript(source)
    second = archive_transcript(source)
    source.write_text(source.read_text().replace("hello", "hullo"))
    third = archive_transcript(source)

    assert (first.status, second.status, third.status) == ("new", "skipped", "updated")
    assert first.archive_path == s2s_home / "archive" / "demo-project" / "session-1.jsonl"
    assert first.archive_path.read_bytes() == source.read_bytes()
    assert "last-write-wins" in caplog.text
    with Ledger() as ledger:
        pending = ledger.pending_queue_items()
    assert [item.item_path for item in pending] == [
        str(first.archive_path),
        str(first.archive_path),
    ]
    assert all(item.source == "claude-code" for item in pending)


def test_queue_failure_rolls_back_archive(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("S2S_HOME", str(tmp_path / "s2s-home"))
    source = _transcript(tmp_path)
    archived = archive_transcript(source).archive_path
    original = archived.read_bytes()
    source.write_text(source.read_text() + '{"changed":true}\n')

    def fail_enqueue(*args, **kwargs):
        raise RuntimeError("queue unavailable")

    monkeypatch.setattr(archiver.Ledger, "enqueue_item", fail_enqueue)
    with pytest.raises(RuntimeError, match="queue unavailable"):
        archive_transcript(source)

    assert archived.read_bytes() == original


def test_hook_payload_archives_and_enqueues(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    s2s_home = tmp_path / "s2s-home"
    monkeypatch.setenv("S2S_HOME", str(s2s_home))
    source = _transcript(tmp_path)
    monkeypatch.setattr(
        "sys.stdin", StringIO(json.dumps({"transcript_path": str(source), "reason": "clear"}))
    )

    assert main(["hook", "session-end"]) == 0

    archived = s2s_home / "archive" / "demo-project" / "session-1.jsonl"
    assert archived.read_bytes() == source.read_bytes()
    with Ledger() as ledger:
        assert [item.item_path for item in ledger.pending_queue_items()] == [str(archived)]


@pytest.mark.parametrize("payload", ["not json", "{}"])
def test_hook_failure_always_exits_zero_and_logs(
    monkeypatch: pytest.MonkeyPatch, tmp_path, payload: str
) -> None:
    s2s_home = tmp_path / "s2s-home"
    monkeypatch.setenv("S2S_HOME", str(s2s_home))
    monkeypatch.setattr("sys.stdin", StringIO(payload))

    assert main(["hook", "session-end"]) == 0

    log = s2s_home / "logs" / "hook.log"
    assert log.is_file()
    assert "SessionEnd hook failed" in log.read_text()


def test_hook_archive_failure_also_exits_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    s2s_home = tmp_path / "s2s-home"
    monkeypatch.setenv("S2S_HOME", str(s2s_home))
    payload = {"transcript_path": str(tmp_path / "missing.jsonl")}

    assert archiver.handle_session_end(StringIO(json.dumps(payload))) == 0
    assert "transcript is not a readable file" in (s2s_home / "logs" / "hook.log").read_text()


def test_backfill_is_rerunnable_without_duplicate_queue_items(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("S2S_HOME", str(tmp_path / "s2s-home"))
    projects = tmp_path / ".claude" / "projects"
    _transcript(tmp_path, "a-project", "a-session")
    _transcript(tmp_path, "b-project", "b-session")

    first_output = StringIO()
    second_output = StringIO()
    first = backfill(projects, output=first_output)
    second = backfill(projects, output=second_output)

    assert first == archiver.BackfillResult(found=2, new=2, skipped=0)
    assert second == archiver.BackfillResult(found=2, new=0, skipped=2)
    assert "found=2 new=0 skipped=2 failed=0" in second_output.getvalue()
    with Ledger() as ledger:
        assert len(ledger.pending_queue_items()) == 2


def test_backfill_prints_progress_every_hundred_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("S2S_HOME", str(tmp_path / "s2s-home"))
    projects = tmp_path / ".claude" / "projects"
    for index in range(100):
        _transcript(tmp_path, "bulk", f"session-{index:03}")
    output = StringIO()

    result = backfill(projects, output=output)

    assert result.found == 100
    assert "Backfill progress: found=100" in output.getvalue()
