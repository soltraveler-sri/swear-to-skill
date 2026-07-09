from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from s2s.config import load_config
from s2s.curator import LAST_PASS_META_KEY, pass_due, run_pass
from s2s.ledger import Ledger
from s2s.llm import load_prompt


def _envelope(
    incident_verdicts: list[dict[str, object]],
    cluster_verdicts: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "result": {
            "incident_verdicts": incident_verdicts,
            "cluster_verdicts": cluster_verdicts or [],
        },
        "usage": {"input_tokens": 5, "output_tokens": 3},
    }


def _verdict(incident_id: int, verdict: str, reason: str = "Useful judgment.", **extra):
    return {"incident_id": incident_id, "verdict": verdict, "reason": reason, **extra}


def _add_triaged(
    ledger: Ledger,
    marker: str,
    *,
    label: str = "ignored-instruction",
    project: str = "project-a",
    dismissed: bool = False,
    index: int = 0,
) -> int:
    session = f"session-{marker.lower().replace(' ', '-')}-{index}"
    timestamp = f"2026-07-{index + 1:02d}T12:00:00+00:00"
    archive = Path(ledger.path).parent / "archive" / project / f"{session}.jsonl"
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.write_text(
        json.dumps(
            {
                "type": "user",
                "uuid": f"uuid-{marker}-{index}",
                "sessionId": session,
                "timestamp": timestamp,
                "cwd": f"/work/{project}",
                "message": {"role": "user", "content": marker},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    incident_id = ledger.create_incident(
        source="claude-code",
        session_id=session,
        project=project,
        message=marker,
        occurred_at=timestamp,
    )
    ledger.triage_incident(
        incident_id,
        label=label,
        one_liner=f"Generalized {marker}",
        severity="2",
        confidence=0.8,
        context_pack_pointer=f"archive/{project}/{session}.jsonl#uuid-{marker}-{index}",
        dismissed=dismissed,
    )
    return incident_id


@pytest.fixture
def ledger(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Ledger:
    monkeypatch.setenv("S2S_HOME", str(tmp_path / "s2s-home"))
    database = Ledger()
    yield database
    database.close()


def test_unchanged_second_pass_sends_zero_full_context_and_makes_zero_calls(
    ledger: Ledger, mock_claude
) -> None:
    incident_id = _add_triaged(ledger, "UNIQUE NEW EVIDENCE")
    mock_claude.enqueue_response(_envelope([_verdict(incident_id, "park")]))

    first = run_pass(ledger, assume_yes=True)
    second = run_pass(ledger, assume_yes=True)

    assert first.full_context_incident_ids == (incident_id,)
    assert second.skipped is True and second.calls == 0
    assert len(mock_claude.invocations()) == 1
    assert str(mock_claude.invocations()[0]["stdin"]).count("BEGIN FULL CONTEXT PACK") == 1


def test_new_arrival_resurfaces_only_its_parked_cluster(
    ledger: Ledger, mock_claude
) -> None:
    touched_old = _add_triaged(ledger, "TOUCHED PARKED", label="ignored-instruction")
    untouched_old = _add_triaged(ledger, "UNTOUCHED PARKED", label="tool-misuse", index=1)
    ledger.apply_curator_incident_verdict(touched_old, "park", reason="wait")
    ledger.apply_curator_incident_verdict(untouched_old, "park", reason="wait")
    arrival = _add_triaged(ledger, "NEW ARRIVAL", label="ignored-instruction", index=2)
    mock_claude.enqueue_response(
        _envelope([_verdict(arrival, "park"), _verdict(touched_old, "park")])
    )

    result = run_pass(ledger, assume_yes=True)

    assert set(result.full_context_incident_ids) == {arrival, touched_old}
    prompt = str(mock_claude.invocations()[0]["stdin"])
    assert f"BEGIN FULL CONTEXT PACK incident_id={touched_old}" in prompt
    assert f"BEGIN FULL CONTEXT PACK incident_id={arrival}" in prompt
    assert f"BEGIN FULL CONTEXT PACK incident_id={untouched_old}" not in prompt
    # The untouched cluster remains visible only through its one-line digest.
    assert "UNTOUCHED PARKED" in prompt
    assert ledger.get_incident(untouched_old).state == "parked"  # type: ignore[union-attr]


def test_singleton_promotion_records_provenance_and_report(
    ledger: Ledger, mock_claude
) -> None:
    incident_id = _add_triaged(ledger, "ONE OFF GENERAL LESSON")
    mock_claude.enqueue_response(_envelope([_verdict(incident_id, "promote")]))

    result = run_pass(ledger, assume_yes=True)

    assert ledger.get_incident(incident_id).state == "promoted"  # type: ignore[union-attr]
    assert ledger.curator_decisions(incident_id)[0].singleton is True
    assert result.report_path is not None and result.report_path.is_file()
    assert "singleton provenance" in result.report_path.read_text(encoding="utf-8")


def test_qc_resurrection_returns_triage_dismissal_to_open(
    ledger: Ledger, mock_claude
) -> None:
    incident_id = _add_triaged(ledger, "FALSE NEGATIVE", dismissed=True)
    mock_claude.enqueue_response(
        _envelope(
            [
                _verdict(
                    incident_id,
                    "resurrect",
                    reassign_label="shallow-investigation",
                )
            ]
        )
    )

    run_pass(ledger, assume_yes=True)

    incident = ledger.get_incident(incident_id)
    assert incident is not None and incident.state == "open"
    assert incident.label == "shallow-investigation"
    assert ledger.curator_decisions(incident_id)[0].verdict == "resurrect"
    assert run_pass(ledger, assume_yes=True).calls == 0
    assert len(mock_claude.invocations()) == 1


def test_context_governor_chunks_by_cluster_without_repeating_packs(
    ledger: Ledger, mock_claude
) -> None:
    first = _add_triaged(ledger, "CHUNK ALPHA", label="ignored-instruction")
    second = _add_triaged(ledger, "CHUNK BETA", label="tool-misuse", index=1)
    Path(ledger.path).parent.joinpath("config.toml").write_text(
        "[curator]\ncontext_char_budget = 1\nqc_sample_size = 0\n",
        encoding="utf-8",
    )
    mock_claude.enqueue_response(_envelope([_verdict(first, "park")]))
    mock_claude.enqueue_response(_envelope([_verdict(second, "park")]))

    result = run_pass(ledger, assume_yes=True)

    assert result.calls == 2
    prompts = [str(invocation["stdin"]) for invocation in mock_claude.invocations()]
    assert sum(prompt.count("BEGIN FULL CONTEXT PACK") for prompt in prompts) == 2
    assert sum("Frustrated message:\nCHUNK ALPHA" in prompt for prompt in prompts) == 1
    assert sum("Frustrated message:\nCHUNK BETA" in prompt for prompt in prompts) == 1
    assert all("LEDGER DIGEST" in prompt for prompt in prompts)


def test_fast_track_flag_is_computed_from_count_and_project_spread(
    ledger: Ledger, mock_claude
) -> None:
    ids = [
        _add_triaged(ledger, "FAST ONE", project="project-a", index=0),
        _add_triaged(ledger, "FAST TWO", project="project-a", index=1),
        _add_triaged(ledger, "FAST THREE", project="project-b", index=2),
    ]
    mock_claude.enqueue_response(_envelope([_verdict(item, "park") for item in ids]))

    run_pass(ledger, assume_yes=True)

    assert "FAST-TRACK: recommend synthesis" in str(mock_claude.invocations()[0]["stdin"])


def test_duplicate_incident_verdicts_are_rejected_without_illegal_transition(
    ledger: Ledger, mock_claude
) -> None:
    incident_id = _add_triaged(ledger, "CONFLICTING VERDICTS")
    mock_claude.enqueue_response(
        _envelope([_verdict(incident_id, "promote"), _verdict(incident_id, "park")])
    )

    result = run_pass(ledger, assume_yes=True)

    assert result.rejected_verdicts == 2
    assert ledger.get_incident(incident_id).state == "open"  # type: ignore[union-attr]
    assert ledger.curator_decisions(incident_id) == []


def test_cluster_synthesize_promotes_reviewed_open_members_and_unworthy_is_durable(
    ledger: Ledger, mock_claude
) -> None:
    reviewed_open = _add_triaged(ledger, "REASSIGNED REVIEWED", label="tool-misuse")
    ledger.apply_curator_incident_verdict(
        reviewed_open,
        "reassign",
        reason="better existing label",
        reassign_label="ignored-instruction",
    )
    arrival = _add_triaged(ledger, "CLUSTER ARRIVAL", label="ignored-instruction", index=1)
    mock_claude.enqueue_response(
        _envelope(
            [_verdict(arrival, "park")],
            [
                {
                    "label": "ignored-instruction",
                    "verdict": "synthesize",
                    "reason": "The reviewed evidence supports one remedy.",
                }
            ],
        )
    )

    result = run_pass(ledger, assume_yes=True)

    assert ledger.get_incident(reviewed_open).state == "promoted"  # type: ignore[union-attr]
    assert ledger.get_incident(arrival).state == "parked"  # type: ignore[union-attr]
    assert result.applied_cluster_verdicts == 1
    assert ledger.curator_cluster_decisions()[0].verdict == "synthesize"

    unworthy_arrival = _add_triaged(
        ledger, "UNWORTHY ARRIVAL", label="tool-misuse", index=2
    )
    mock_claude.enqueue_response(
        _envelope(
            [_verdict(unworthy_arrival, "park")],
            [
                {
                    "label": "tool-misuse",
                    "verdict": "unworthy",
                    "reason": "No instruction-level remedy is actionable.",
                }
            ],
        )
    )
    run_pass(ledger, assume_yes=True)
    assert ledger.curator_cluster_decisions()[-1].reason == (
        "No instruction-level remedy is actionable."
    )


def test_prompt_contains_northstar_asymmetry_instruction_verbatim() -> None:
    template, _ = load_prompt("curate", 1)
    required = (
        "Attaching two similar-but-not-identical items is a cheap, recoverable error; "
        "keeping them apart when related is silent and fatal. When uncertain, attach. "
        "To keep items apart you must state why one remedy could not cover both."
    )
    assert required in template


def test_pass_due_uses_count_or_persisted_elapsed_time(ledger: Ledger) -> None:
    _add_triaged(ledger, "WAITING")
    config = load_config()
    assert pass_due(ledger, config) is False
    ledger.set_meta(
        LAST_PASS_META_KEY,
        (datetime.now(timezone.utc) - timedelta(days=8)).isoformat(),
    )
    assert pass_due(ledger, config) is True
