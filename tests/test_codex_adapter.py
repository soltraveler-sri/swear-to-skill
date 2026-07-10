from __future__ import annotations

import json
from pathlib import Path

import pytest

from s2s.adapters import codex
from s2s.ledger import Ledger
from s2s.scanner import scan_codex_history, scan_pending_queue
from s2s.triager import context_for_incident


def _rollout(path: Path, *, thread: str = "thread-fixture", fallback: bool = False, subagent: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    records: list[object] = [
        {"type": "session_meta", "payload": {"thread_id": thread, "cwd": "/work/demo", "source": {"subagent": True} if subagent else {}}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": "Please inspect the parser."}, "timestamp": "2026-07-09T10:00:00Z"},
        {"type": "response_item", "payload": {"type": "function_call", "name": "Read"}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": "This is not what I asked."}, "timestamp": "2026-07-09T10:01:00Z"},
        {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"text": "I corrected it."}]}},
    ]
    if fallback:
        records = [
            records[0],
            {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"text": "Older prompt."}]}, "timestamp": "2026-07-09T10:00:00Z"},
            {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"text": "This is not working."}]}, "timestamp": "2026-07-09T10:01:00Z"},
        ]
    path.write_text("{malformed\n" + "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    return path


def test_history_fast_path_and_rollout_context_are_defensive(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    root = tmp_path / ".codex"
    history = root / "history.jsonl"
    history.parent.mkdir()
    history.write_text(
        "{bad\n"
        + json.dumps({"session_id": "thread-fixture", "ts": "2026-07-09T10:01:00Z", "text": "This is not what I asked."}) + "\n"
        + json.dumps({"session_id": "thread-fixture", "ts": "2026-07-09T10:02:00Z", "text": "# AGENTS.md instructions\nignored"}) + "\n",
        encoding="utf-8",
    )
    rollout = _rollout(root / "sessions" / "2026" / "07" / "09" / "rollout-fixture.jsonl")

    messages = codex.extract_user_messages(history)
    assert [message.message for message in messages] == ["This is not what I asked."]
    assert codex.find_rollout("thread-fixture", root) == rollout
    rollout_messages = codex.extract_user_messages(rollout)
    pack = codex.build_context_pack(rollout, rollout_messages[1].uuid or "")
    assert pack.preceding_request == "Please inspect the parser."
    assert pack.agent_activity_digest == "Tool: Read"
    assert pack.frustrated_message == "This is not what I asked."
    assert "I corrected it." in pack.following_exchange
    assert "Skipping malformed Codex JSONL line" in caplog.text


def test_dual_path_subagent_and_missing_database_behaviour(tmp_path: Path) -> None:
    root = tmp_path / ".codex"
    fallback = _rollout(root / "sessions" / "rollout-old.jsonl", fallback=True)
    subagent = _rollout(root / "archived_sessions" / "rollout-subagent.jsonl", thread="subagent-thread", subagent=True)

    assert [item.message for item in codex.extract_user_messages(fallback)] == ["Older prompt.", "This is not working."]
    assert codex.extract_user_messages(subagent) == []
    metadata = codex.extract_session_metadata(fallback, root=root)
    assert metadata.dominant_model == "unknown"
    assert metadata.malformed_line_count == 1


def test_codex_history_scan_archives_and_triage_context_resolves(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / ".codex"
    history = root / "history.jsonl"
    history.parent.mkdir()
    history.write_text(json.dumps({"session_id": "thread-fixture", "ts": "2026-07-09T10:01:00Z", "text": "This is not what I asked."}) + "\n", encoding="utf-8")
    _rollout(root / "sessions" / "2026" / "07" / "09" / "rollout-fixture.jsonl")
    monkeypatch.setenv("S2S_HOME", str(tmp_path / "s2s"))
    (tmp_path / "s2s").mkdir()
    (tmp_path / "s2s" / "config.toml").write_text("[sources]\ncodex = true\n", encoding="utf-8")
    lexicon = {"categories": {"test": {"group": "test", "weight": 1, "terms": ["not what I asked"]}}}

    with Ledger() as ledger:
        assert scan_codex_history(ledger, root=root, lexicon=lexicon) == 1
        # The archive queue can be scanned again without changing the incident.
        scan_pending_queue(ledger)
        incident = ledger.untriaged_incidents()[0]
        assert incident.source == "codex"
        pack, pointer = context_for_incident(incident)

    assert pack.frustrated_message == "This is not what I asked."
    assert pointer.startswith("archive/codex/")
