from __future__ import annotations

from pathlib import Path

from s2s.adapters.claude_code import (
    build_context_pack,
    extract_session_metadata,
    extract_user_messages,
    iter_archived_sessions,
    iter_sessions,
)


FIXTURE = Path(__file__).parent / "fixtures" / "claude_code" / "synthetic-session.jsonl"


def test_extract_user_messages_keeps_only_direct_human_content(caplog) -> None:
    records = extract_user_messages(FIXTURE)

    assert [record.message for record in records] == [
        "Please add a defensive transcript parser.",
        "This is not what I asked; please fix it.",
        "Thanks, that resolves it.",
    ]
    assert records[0].uuid == "u-request"
    assert records[0].session_id == "session-synthetic-001"
    assert records[0].project == "claude_code"
    assert records[0].cwd == "/work/demo"
    assert records[0].git_branch == "feat/example"
    assert records[0].cli_version == "2.1.4"
    assert "Skipping malformed Claude Code JSONL line" in caplog.text


def test_context_pack_uses_parent_chain_and_never_includes_raw_tool_output() -> None:
    pack = build_context_pack(FIXTURE, "u-frustrated")

    assert pack.preceding_request == "Please add a defensive transcript parser."
    assert "Tool: Read — src/s2s/paths.py" in pack.agent_activity_digest
    assert pack.frustrated_message == "This is not what I asked; please fix it."
    assert "Tool: Write — src/s2s/adapters/claude_code.py" in pack.following_exchange
    assert "I corrected the parser and added tests." in pack.following_exchange
    assert "Thanks, that resolves it." in pack.following_exchange
    assert "SYNTHETIC_TOOL_OUTPUT_MUST_NOT_APPEAR" not in pack.following_exchange
    assert pack.session_id == "session-synthetic-001"
    assert pack.project == "claude_code"
    assert pack.cwd == "/work/demo"
    assert pack.git_branch == "feat/example"
    assert pack.timestamp == "2026-07-09T10:02:20Z"


def test_context_pack_truncation_sacrifices_frustrated_message_last() -> None:
    pack = build_context_pack(FIXTURE, "u-frustrated", max_chars=100)

    assert pack.text_length <= 100
    assert pack.preceding_request == ""
    assert pack.agent_activity_digest == ""
    # The incident message is the anchor of the pack: it must survive while any
    # other section still holds content, and is truncated only as a last resort.
    assert pack.frustrated_message == "This is not what I asked; please fix it."
    assert len(pack.following_exchange) <= 100 - len(pack.frustrated_message)


def test_session_metadata_counts_messages_models_duration_and_bad_lines() -> None:
    metadata = extract_session_metadata(FIXTURE)

    assert metadata.session_id == "session-synthetic-001"
    assert metadata.project == "claude_code"
    assert metadata.dominant_model == "claude-sonnet-test"
    assert metadata.direct_message_count == 3
    assert metadata.user_message_count == 9
    assert metadata.assistant_message_count == 2
    assert metadata.first_timestamp == "2026-07-09T10:00:00Z"
    assert metadata.last_timestamp == "2026-07-09T10:04:00Z"
    assert metadata.duration_seconds == 240.0
    assert metadata.malformed_line_count == 1


def test_session_discovery_accepts_explicit_project_and_archive_roots(tmp_path) -> None:
    projects = tmp_path / "projects"
    archived = tmp_path / "archive"
    project_transcript = projects / "demo-project" / "session.jsonl"
    archived_transcript = archived / "demo-project" / "session.jsonl"
    project_transcript.parent.mkdir(parents=True)
    archived_transcript.parent.mkdir(parents=True)
    project_transcript.write_text(FIXTURE.read_text())
    archived_transcript.write_text(FIXTURE.read_text())

    assert list(iter_sessions(projects)) == [project_transcript]
    assert list(iter_archived_sessions(archived)) == [archived_transcript]
    assert extract_user_messages(project_transcript) == extract_user_messages(archived_transcript)
