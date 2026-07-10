from __future__ import annotations

import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals.corpus_tools import claude_session_skeleton
from evals.lint_corpus import lint_corpus
from s2s.adapters.claude_code import extract_user_messages


def test_golden_corpus_lints_through_real_adapters_and_scanner() -> None:
    stats = lint_corpus()

    assert stats == {
        "claude-code": {"sessions": 16, "incidents": 31, "clusters": 5, "decoys": 8, "known_misses": 2},
        "codex": {"sessions": 4, "incidents": 7, "clusters": 2, "decoys": 2, "known_misses": 0},
    }


def test_authoring_helper_round_trips_through_real_claude_adapter(tmp_path: Path) -> None:
    records = claude_session_skeleton(
        "generated-fake-session",
        "/work/fake-generated",
        ["Please inspect the fake parser.", "This is not what I asked."],
    )
    transcript = tmp_path / "generated.jsonl"
    transcript.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")

    messages = extract_user_messages(transcript)

    assert [message.uuid for message in messages] == ["generated-fake-session-u-01", "generated-fake-session-u-02"]
    assert [message.message for message in messages] == ["Please inspect the fake parser.", "This is not what I asked."]
