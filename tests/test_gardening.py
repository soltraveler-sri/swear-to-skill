from __future__ import annotations

import json
from pathlib import Path

import pytest

from s2s.curator import run_pass
from s2s.ledger import Ledger
from s2s.llm import load_prompt
from s2s.taxonomy import label_names, load_current_taxonomy
from s2s.triager import triage_schema


def _envelope(result: dict[str, object]) -> dict[str, object]:
    return {"result": result, "usage": {"input_tokens": 5, "output_tokens": 3}}


def _add_open(ledger: Ledger, marker: str, *, label: str, index: int) -> int:
    session = f"garden-{index}"
    timestamp = f"2026-07-{index + 1:02d}T12:00:00+00:00"
    archive = Path(ledger.path).parent / "archive" / "project-a" / f"{session}.jsonl"
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.write_text(
        json.dumps(
            {
                "type": "user",
                "uuid": f"garden-{index}",
                "sessionId": session,
                "timestamp": timestamp,
                "cwd": "/work/project-a",
                "message": {"role": "user", "content": marker},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    incident_id = ledger.create_incident(
        source="claude-code",
        session_id=session,
        project="project-a",
        message=marker,
        occurred_at=timestamp,
    )
    ledger.triage_incident(
        incident_id,
        label=label,
        one_liner=f"Generalized {marker}",
        severity="2",
        confidence=0.8,
        context_pack_pointer=f"archive/project-a/{session}.jsonl#garden-{index}",
    )
    return incident_id


@pytest.fixture
def ledger(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Ledger:
    monkeypatch.setenv("S2S_HOME", str(tmp_path / "s2s-home"))
    database = Ledger()
    yield database
    database.close()


def test_new_label_gardening_bumps_user_taxonomy_relabels_and_updates_triage_menu(
    ledger: Ledger, mock_claude
) -> None:
    ids = [_add_open(ledger, f"UNIQUE OTHER {index}", label="other", index=index) for index in range(3)]
    mock_claude.enqueue_response(
        _envelope(
            {
                "incident_verdicts": [
                    {"incident_id": item, "verdict": "park", "reason": "Wait for taxonomy gardening."}
                    for item in ids
                ],
                "cluster_verdicts": [],
            }
        )
    )
    mock_claude.enqueue_response(
        _envelope(
            {
                "propose_label": {
                    "name": "missing-verification-context",
                    "gist": "failed to inspect context needed to verify a change",
                    "examples": ["Check the relevant context first.", "You skipped the verification context."],
                    "evidence_incident_ids": ids,
                },
                "merge_labels": [],
            }
        )
    )

    result = run_pass(ledger, assume_yes=True)

    taxonomy_path = Path(ledger.path).parent / "taxonomy" / "taxonomy.v2.json"
    assert result.calls == 2
    assert taxonomy_path.is_file()
    assert [ledger.get_incident(item).label for item in ids] == ["missing-verification-context"] * 3  # type: ignore[union-attr]
    assert "missing-verification-context" in triage_schema()["properties"]["label"]["enum"]  # type: ignore[index]
    assert result.report_path is not None
    assert "garden label `missing-verification-context`" in result.report_path.read_text(encoding="utf-8")


def test_merge_gardening_relabels_ledger_and_records_rename_map(ledger: Ledger, mock_claude) -> None:
    absorbed_ids = [_add_open(ledger, f"TOOL DUPLICATE {index}", label="tool-misuse", index=index) for index in range(2)]
    survivor_id = _add_open(ledger, "INSTRUCTION DUPLICATE", label="ignored-instruction", index=2)
    other_id = _add_open(ledger, "GARDEN TRIGGER", label="other", index=3)
    all_ids = [*absorbed_ids, survivor_id, other_id]
    mock_claude.enqueue_response(
        _envelope(
            {
                "incident_verdicts": [
                    {"incident_id": item, "verdict": "park", "reason": "Wait."} for item in all_ids
                ],
                "cluster_verdicts": [],
            }
        )
    )
    mock_claude.enqueue_response(
        _envelope(
            {
                "propose_label": None,
                "merge_labels": [
                    {
                        "survivor": "ignored-instruction",
                        "absorbed": "tool-misuse",
                        "reason": "One standing instruction can cover both failures.",
                    }
                ],
            }
        )
    )

    run_pass(ledger, assume_yes=True)

    document = json.loads((Path(ledger.path).parent / "taxonomy" / "taxonomy.v2.json").read_text())
    assert document["history"][-1]["rename_map"] == {"tool-misuse": "ignored-instruction"}
    assert "tool-misuse" not in label_names()
    assert [ledger.get_incident(item).label for item in absorbed_ids] == ["ignored-instruction"] * 2  # type: ignore[union-attr]


def test_gardening_rejects_a_new_label_with_fewer_than_three_evidence_ids(ledger: Ledger, mock_claude) -> None:
    ids = [_add_open(ledger, f"TOO FEW {index}", label="other", index=index) for index in range(3)]
    mock_claude.enqueue_response(
        _envelope(
            {
                "incident_verdicts": [
                    {"incident_id": item, "verdict": "park", "reason": "Wait."} for item in ids
                ],
                "cluster_verdicts": [],
            }
        )
    )
    mock_claude.enqueue_response(
        _envelope(
            {
                "propose_label": {
                    "name": "unsupported-pattern",
                    "gist": "not enough evidence",
                    "examples": ["One", "Two"],
                    "evidence_incident_ids": ids[:2],
                },
                "merge_labels": [],
            }
        )
    )

    result = run_pass(ledger, assume_yes=True)

    assert not (Path(ledger.path).parent / "taxonomy").exists()
    assert [ledger.get_incident(item).label for item in ids] == ["other"] * 3  # type: ignore[union-attr]
    assert result.report_path is not None
    assert "requires three unique active other evidence incident IDs" in result.report_path.read_text(encoding="utf-8")


def test_zero_other_and_no_anomaly_does_not_make_a_gardening_call(ledger: Ledger, mock_claude) -> None:
    incident_id = _add_open(ledger, "NO GARDEN", label="ignored-instruction", index=0)
    mock_claude.enqueue_response(
        _envelope(
            {
                "incident_verdicts": [
                    {"incident_id": incident_id, "verdict": "park", "reason": "Wait."}
                ],
                "cluster_verdicts": [],
            }
        )
    )

    result = run_pass(ledger, assume_yes=True)

    assert result.calls == 1
    assert len(mock_claude.invocations()) == 1
    assert "Taxonomy Gardener" not in str(mock_claude.invocations()[0]["stdin"])


def test_user_taxonomy_version_has_precedence_over_vendored_seed(ledger: Ledger) -> None:
    user_taxonomy = Path(ledger.path).parent / "taxonomy" / "taxonomy.v1.json"
    user_taxonomy.parent.mkdir(parents=True)
    document = json.loads((Path(__file__).parents[1] / "src" / "s2s" / "lexicons" / "taxonomy.v1.json").read_text())
    document["labels"][0]["gist"] = "user-state taxonomy wins"
    user_taxonomy.write_text(json.dumps(document), encoding="utf-8")

    taxonomy = load_current_taxonomy()

    assert taxonomy.version == 1
    assert taxonomy.labels[0].gist == "user-state taxonomy wins"


def test_garden_prompt_carries_the_northstar_asymmetry_instruction_verbatim() -> None:
    template, _ = load_prompt("garden", 1)
    assert (
        "Attaching two similar-but-not-identical items is a cheap, recoverable error; "
        "keeping them apart when related is silent and fatal. When uncertain, attach. "
        "To keep items apart you must state why one remedy could not cover both."
    ) in template
