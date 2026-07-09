from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

from s2s.adapters.claude_code import build_context_pack
from s2s.ledger import Ledger
from s2s.llm import ClaudeProcessError, MalformedOutputError, load_prompt
from s2s.taxonomy import label_names, list_labels
from s2s.triager import render_triage_prompt, triage_pending, triage_schema


def _envelope(result: object) -> dict[str, object]:
    return {"result": result, "usage": {"input_tokens": 3, "output_tokens": 2}}


def _response(
    *,
    authentic: bool = True,
    label: str = "ignored-instruction",
    one_liner: str = "The agent did not follow an explicit instruction.",
) -> dict[str, object]:
    return _envelope(
        {
            "authentic": authentic,
            "reason": "The message addresses the agent response.",
            "label": label,
            "one_liner": one_liner,
            "severity": 2,
            "confidence": 0.8,
        }
    )


def _archive_and_detect(ledger: Ledger, messages: list[str], *, session: str = "session-1") -> list[int]:
    """Write a minimal archived transcript and matching detected incidents."""

    archive = Path(ledger.path).parent / "archive" / "project-a" / f"{session}.jsonl"
    archive.parent.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, object]] = []
    incident_ids: list[int] = []
    for index, message in enumerate(messages):
        timestamp = f"2026-07-09T12:00:0{index}Z"
        records.append(
            {
                "type": "user",
                "uuid": f"incident-{index}",
                "sessionId": session,
                "timestamp": timestamp,
                "cwd": "/work/project-a",
                "message": {"role": "user", "content": message},
            }
        )
        incident_ids.append(
            ledger.create_incident(
                source="claude-code",
                session_id=session,
                project="project-a",
                message=message,
                occurred_at=timestamp,
            )
        )
    archive.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    return incident_ids


@pytest.fixture
def ledger(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Ledger:
    monkeypatch.setenv("S2S_HOME", str(tmp_path / "s2s-home"))
    database = Ledger()
    yield database
    database.close()


def test_taxonomy_is_the_fixed_sixteen_label_menu() -> None:
    assert label_names() == (
        "premature-completion-claim",
        "unverified-change",
        "ignored-instruction",
        "forgotten-context",
        "scope-deviation",
        "hallucinated-interface",
        "destructive-action",
        "repeated-after-correction",
        "shallow-investigation",
        "misread-request",
        "overengineering",
        "environment-mismanagement",
        "house-rules-violation",
        "tool-misuse",
        "communication-failure",
        "other",
    )
    assert all(2 <= len(entry.examples) <= 3 for entry in list_labels())


def test_triage_happy_path_keeps_both_outcomes_and_context_pointer(
    ledger: Ledger, mock_claude
) -> None:
    open_id, dismissed_id = _archive_and_detect(
        ledger, ["You ignored my instruction.", "I am quoting a playful complaint."],
    )
    mock_claude.enqueue_response(_response())
    mock_claude.enqueue_response(_response(authentic=False, label="other"))

    results = triage_pending(ledger, assume_yes=True)

    assert [(result.incident_id, result.state) for result in results] == [
        (open_id, "open"),
        (dismissed_id, "dismissed-triage"),
    ]
    accepted = ledger.get_incident(open_id)
    dismissed = ledger.get_incident(dismissed_id)
    assert accepted is not None and accepted.state == "open"
    assert accepted.label == "ignored-instruction"
    assert accepted.severity == "2"
    assert accepted.context_pack_pointer == "archive/project-a/session-1.jsonl#incident-0"
    assert dismissed is not None and dismissed.state == "dismissed-triage"
    assert ledger.state_history(dismissed_id)[-1].reason == "The message addresses the agent response."


def test_invented_label_retries_then_raises_and_leaves_incident_detected(
    ledger: Ledger, mock_claude
) -> None:
    (incident_id,) = _archive_and_detect(ledger, ["This request was ignored."])
    invalid = _response(label="new-bucket")
    mock_claude.enqueue_response(invalid)
    mock_claude.enqueue_response(invalid)

    with pytest.raises(MalformedOutputError, match="schema enum"):
        triage_pending(ledger, assume_yes=True)

    assert len(mock_claude.invocations()) == 2
    assert ledger.get_incident(incident_id).state == "detected"  # type: ignore[union-attr]


def test_mid_batch_failure_leaves_only_the_in_flight_incident_and_later_work_queued(
    ledger: Ledger, mock_claude
) -> None:
    first_id, second_id, third_id = _archive_and_detect(
        ledger, ["First request ignored.", "Second request ignored.", "Third request ignored."],
    )
    mock_claude.enqueue_response(_response())
    mock_claude.enqueue_response({"returncode": 2, "stderr": "simulated second-call failure"})

    with pytest.raises(ClaudeProcessError, match="simulated second-call failure"):
        triage_pending(ledger, assume_yes=True)

    assert ledger.get_incident(first_id).state == "open"  # type: ignore[union-attr]
    assert ledger.get_incident(second_id).state == "detected"  # type: ignore[union-attr]
    assert ledger.get_incident(third_id).state == "detected"  # type: ignore[union-attr]

    mock_claude.enqueue_response(_response())
    mock_claude.enqueue_response(_response())
    assert [result.incident_id for result in triage_pending(ledger, assume_yes=True)] == [second_id, third_id]
    assert ledger.untriaged_incidents() == []


def test_each_rendered_prompt_contains_only_its_own_incident_context(
    ledger: Ledger, mock_claude
) -> None:
    _archive_and_detect(ledger, ["UNIQUE INCIDENT ALPHA", "UNIQUE INCIDENT BETA"])
    mock_claude.enqueue_response(_response())
    mock_claude.enqueue_response(_response())

    triage_pending(ledger, assume_yes=True)

    prompts = [str(invocation["stdin"]) for invocation in mock_claude.invocations()]
    assert "UNIQUE INCIDENT ALPHA" in prompts[0]
    assert "UNIQUE INCIDENT BETA" not in prompts[0]
    assert "UNIQUE INCIDENT BETA" in prompts[1]
    assert "UNIQUE INCIDENT ALPHA" not in prompts[1]


def test_rendered_prompt_never_asks_the_forbidden_structural_questions(ledger: Ledger) -> None:
    _archive_and_detect(ledger, ["One incident only."])
    template, _ = load_prompt("triage", 1)
    pack = build_context_pack(
        Path(ledger.path).parent / "archive" / "project-a" / "session-1.jsonl", "incident-0"
    )
    prompt = render_triage_prompt(template, pack).casefold()

    assert all(forbidden not in prompt for forbidden in ("similar to", "same as", "compare"))


def test_dynamic_schema_uses_the_current_taxonomy_menu() -> None:
    schema = triage_schema()
    properties = schema["properties"]
    assert isinstance(properties, dict)
    assert properties["label"] == {"type": "string", "enum": list(label_names())}


def test_estimate_refusal_in_noninteractive_bulk_run_leaves_work_queued(
    ledger: Ledger, mock_claude, monkeypatch: pytest.MonkeyPatch
) -> None:
    (incident_id,) = _archive_and_detect(ledger, ["Please follow my instruction."])
    config = Path(ledger.path).parent / "config.toml"
    config.write_text("[costs]\nconfirm_threshold_usd = 0.001\n", encoding="utf-8")

    class NonInteractive:
        def isatty(self) -> bool:
            return False

    monkeypatch.setattr(sys, "stdin", NonInteractive())
    assert triage_pending(ledger) == []
    assert mock_claude.invocations() == []
    assert ledger.get_incident(incident_id).state == "detected"  # type: ignore[union-attr]
