from __future__ import annotations

import json
from pathlib import Path

import pytest

from s2s.ledger import Ledger
from s2s.synthesist import (
    collect_remedy_surface,
    parse_skill_markdown,
    synthesize_pending,
)


def _envelope(result: dict[str, object]) -> dict[str, object]:
    return {"result": result, "usage": {"input_tokens": 5, "output_tokens": 3}}


def _promoted(ledger: Ledger, marker: str, *, index: int = 0, singleton: bool = False) -> int:
    session = f"synthesis-{index}"
    timestamp = f"2026-07-{index + 1:02d}T12:00:00+00:00"
    archive = Path(ledger.path).parent / "archive" / "project-a" / f"{session}.jsonl"
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.write_text(
        json.dumps(
            {
                "type": "user",
                "uuid": f"uuid-{index}",
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
        label="ignored-instruction",
        one_liner="The agent ignored an explicit instruction.",
        severity="2",
        confidence=0.9,
        context_pack_pointer=f"archive/project-a/{session}.jsonl#uuid-{index}",
    )
    ledger.apply_curator_incident_verdict(
        incident_id, "promote", reason="general lesson", singleton=singleton
    )
    return incident_id


@pytest.fixture
def ledger(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Ledger:
    monkeypatch.setenv("S2S_HOME", str(tmp_path / "s2s-home"))
    database = Ledger()
    yield database
    database.close()


def _proposal(incident_id: int, remedy_type: str, *, dedup: list[dict[str, str]] | None = None) -> dict[str, object]:
    content: dict[str, object] = {
        "claude-md": {"text": "Honor explicit instructions before acting.", "target": "global"},
        "hook": {"event": "PreToolUse", "command_sketch": "check-scope.sh"},
        "skill": {
            "name": "instruction-check",
            "description": "Use when a task has explicit constraints before taking action.",
            "body_markdown": "# Instruction check\n\nRead and restate constraints before acting.",
        },
        "benchmark-only": {"note": "Track this model-level miss in the meter only."},
    }[remedy_type]
    proposal: dict[str, object] = {
        "remedy_type": remedy_type,
        "routing_rationale": "This is the cheapest remedy that can address the failure.",
        "failure_statement": "The agent fails to follow explicit constraints.",
        "remedy_content": content,
        "evidence": [{"incident_id": incident_id, "quote": "You ignored the constraint."}],
        "dedup": dedup or [],
        "confidence": 0.8,
    }
    return proposal


def _synthesize(ledger: Ledger, **kwargs):
    root = Path(ledger.path).parent / "injected-remedy-surface"
    kwargs.setdefault("skills_dir", root / "skills")
    kwargs.setdefault("global_claude_md_path", root / "CLAUDE.md")
    return synthesize_pending(
        ledger,
        assume_yes=True,
        **kwargs,
    )


@pytest.mark.parametrize("remedy_type", ["claude-md", "hook", "skill", "benchmark-only"])
def test_routing_paths_store_pending_proposals(
    ledger: Ledger, mock_claude, remedy_type: str
) -> None:
    incident_id = _promoted(ledger, f"MISSED {remedy_type}", singleton=True)
    mock_claude.enqueue_response(_envelope(_proposal(incident_id, remedy_type)))

    results = _synthesize(ledger)

    assert results[0].proposal_ids
    row = ledger.connection.execute("SELECT * FROM proposal").fetchone()
    assert row["remedy_type"] == remedy_type
    assert row["gate_status"] == "pending"
    assert ledger.get_incident(incident_id).state == "in-proposal"  # type: ignore[union-attr]
    if remedy_type == "benchmark-only":
        assert ledger.connection.execute("SELECT COUNT(*) FROM remedy").fetchone()[0] == 0


def test_overlap_is_linked_revision_not_sibling(ledger: Ledger, mock_claude) -> None:
    existing = ledger.create_proposal(
        remedy_type="claude-md",
        drafted_content="Existing rule",
        evidence_incident_ids=[_promoted(ledger, "OLD", index=0)],
        dedup_verdict="[]",
        gate_status="pending",
    )
    # The existing proposal's old promoted incident is not part of this run.
    ledger.transition_incident(1, "in-proposal", reason="old proposal")
    incident_id = _promoted(ledger, "NEW", index=1)
    dedup = [{"existing": "R1", "verdict": "overlap", "reason": "Same rule."}]
    response = _proposal(incident_id, "claude-md", dedup=dedup)
    response["overlap_action"] = {"revises": "R1"}
    mock_claude.enqueue_response(_envelope(response))

    _synthesize(ledger)

    row = ledger.connection.execute("SELECT revises FROM proposal WHERE id != ?", (existing,)).fetchone()
    assert row["revises"] == f"proposal #{existing}"


def test_split_creates_partitioned_proposals_and_rejects_orphans(ledger: Ledger, mock_claude) -> None:
    first = _promoted(ledger, "FIRST", index=0)
    second = _promoted(ledger, "SECOND", index=1)
    first_proposal = _proposal(first, "claude-md")
    second_proposal = _proposal(second, "hook")
    split_response = dict(first_proposal)
    split_response["split"] = [first_proposal, second_proposal]
    mock_claude.enqueue_response(_envelope(split_response))

    result = _synthesize(ledger)

    assert len(result[0].proposal_ids) == 2
    evidence = ledger.connection.execute("SELECT incident_id FROM proposal_evidence ORDER BY incident_id").fetchall()
    assert [row["incident_id"] for row in evidence] == [first, second]

    ledger.connection.execute("UPDATE proposal SET gate_status = 'rejected'")
    third = _promoted(ledger, "THIRD", index=2)
    fourth = _promoted(ledger, "FOURTH", index=3)
    orphan_response = _proposal(third, "claude-md")
    orphan_response["split"] = [_proposal(third, "claude-md"), _proposal(third, "hook")]
    mock_claude.enqueue_response(_envelope(orphan_response))
    mock_claude.enqueue_response(_envelope(orphan_response))
    orphan_results = _synthesize(ledger)
    assert orphan_results and orphan_results[-1].error is not None
    assert "split evidence" in orphan_results[-1].error
    assert orphan_results[-1].proposal_ids == ()
    assert ledger.get_incident(third).state == "promoted"  # type: ignore[union-attr]
    assert ledger.get_incident(fourth).state == "promoted"  # type: ignore[union-attr]


def test_skill_frontmatter_validation_retries_then_preserves_promoted_on_failure(
    ledger: Ledger, mock_claude
) -> None:
    incident_id = _promoted(ledger, "SKILL", singleton=True)
    invalid = _proposal(incident_id, "skill")
    invalid["remedy_content"] = {"name": "Not Kebab", "description": "Use when constraints matter.", "body_markdown": "Body"}
    mock_claude.enqueue_response(_envelope(invalid))
    mock_claude.enqueue_response(_envelope(invalid))

    failed_results = _synthesize(ledger)
    assert failed_results and failed_results[-1].error is not None
    assert "kebab-case" in failed_results[-1].error
    assert failed_results[-1].proposal_ids == ()
    assert ledger.get_incident(incident_id).state == "promoted"  # type: ignore[union-attr]
    assert len(mock_claude.invocations()) == 2
    retry_prompt = str(mock_claude.invocations()[1]["stdin"])
    assert "Specific validation failure: SKILL.md name must be kebab-case" in retry_prompt
    frontmatter, body = parse_skill_markdown(
        "---\nname: instruction-check\ndescription: Use when constraints must be checked.\n---\n# Check\n"
    )
    assert frontmatter["name"] == "instruction-check" and body == "# Check"


def test_prompt_contains_injectable_existing_remedy_digests(ledger: Ledger, mock_claude, tmp_path: Path) -> None:
    incident_id = _promoted(ledger, "DIGEST", singleton=True)
    skills = tmp_path / "skills"
    skill_file = skills / "existing" / "SKILL.md"
    skill_file.parent.mkdir(parents=True)
    skill_file.write_text(
        "---\nname: existing-rule\ndescription: Use when an existing remedy applies.\n---\nBody\n",
        encoding="utf-8",
    )
    claude_md = tmp_path / "CLAUDE.md"
    claude_md.write_text("outside\n<!-- s2s:begin -->\nmanaged rule\n<!-- s2s:end -->\n", encoding="utf-8")
    existing = ledger.create_proposal(
        remedy_type="hook", drafted_content="Prior hook", evidence_incident_ids=[incident_id], dedup_verdict="[]", gate_status="pending"
    )
    # Existing evidence can be promoted in this isolated fixture; the proposal itself is a digest target.
    dedup = [
        {"existing": "R1", "verdict": "clear", "reason": "Different."},
        {"existing": "R2", "verdict": "clear", "reason": "Different."},
        {"existing": "R3", "verdict": "clear", "reason": "Different."},
    ]
    mock_claude.enqueue_response(_envelope(_proposal(incident_id, "benchmark-only", dedup=dedup)))

    _synthesize(ledger, skills_dir=skills, global_claude_md_path=claude_md)

    prompt = str(mock_claude.invocations()[0]["stdin"])
    assert "There are 3 existing-remedy digests." in prompt
    assert f"[R1] skill:{skill_file}" in prompt
    assert f"[R2] claude-md:{claude_md}#1" in prompt
    assert f"[R3] proposal #{existing}" in prompt
    assert "name=existing-rule | description=Use when an existing remedy applies." in prompt
    assert "managed rule" in prompt and "Prior hook" in prompt


def test_skill_surface_dedup_normalizes_generated_prefix(ledger: Ledger, tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    skill_file = skills / "s2s-scope-check" / "SKILL.md"
    skill_file.parent.mkdir(parents=True)
    skill_file.write_text(
        "---\nname: s2s-scope-check\ndescription: Use when scope needs checking.\n---\n# Scope\n",
        encoding="utf-8",
    )

    surface = collect_remedy_surface(ledger, skills_dir=skills)

    assert surface.skills[0][1].startswith("name=scope-check |")


@pytest.mark.parametrize("use_reference_id", [True, False])
def test_dedup_accepts_reference_id_and_full_reference(
    ledger: Ledger, mock_claude, tmp_path: Path, use_reference_id: bool
) -> None:
    incident_id = _promoted(ledger, f"DEDUP-{use_reference_id}", singleton=True)
    skills = tmp_path / "skills"
    skill_file = skills / "existing" / "SKILL.md"
    skill_file.parent.mkdir(parents=True)
    skill_file.write_text(
        "---\nname: existing-rule\ndescription: Use when an existing remedy applies.\n---\nBody\n",
        encoding="utf-8",
    )
    existing = "R1" if use_reference_id else f"skill:{skill_file}"
    response = _proposal(
        incident_id,
        "benchmark-only",
        dedup=[{"existing": existing, "verdict": "clear", "reason": "Different."}],
    )
    mock_claude.enqueue_response(_envelope(response))

    result = _synthesize(ledger, skills_dir=skills)

    assert result[0].proposal_ids


def test_v2_coverage_failure_names_missing_reference_ids(
    ledger: Ledger, mock_claude, tmp_path: Path
) -> None:
    incident_id = _promoted(ledger, "MISSING DEDUP", singleton=True)
    skills = tmp_path / "skills"
    skill_file = skills / "existing" / "SKILL.md"
    skill_file.parent.mkdir(parents=True)
    skill_file.write_text(
        "---\nname: existing-rule\ndescription: Use when an existing remedy applies.\n---\nBody\n",
        encoding="utf-8",
    )
    claude_md = tmp_path / "CLAUDE.md"
    claude_md.write_text(
        "<!-- s2s:begin -->\nmanaged rule\n<!-- s2s:end -->\n", encoding="utf-8"
    )
    incomplete = _proposal(
        incident_id,
        "benchmark-only",
        dedup=[{"existing": "R1", "verdict": "clear", "reason": "Different."}],
    )
    mock_claude.enqueue_response(_envelope(incomplete))
    mock_claude.enqueue_response(_envelope(incomplete))

    result = _synthesize(
        ledger, skills_dir=skills, global_claude_md_path=claude_md
    )

    assert result[0].proposal_ids == ()
    assert result[0].error is not None
    assert f"dedup missing entries for: R2 (claude-md:{claude_md}#1)" in result[0].error


def test_singleton_provenance_is_copied_to_proposal(ledger: Ledger, mock_claude) -> None:
    incident_id = _promoted(ledger, "ONE", singleton=True)
    mock_claude.enqueue_response(_envelope(_proposal(incident_id, "claude-md")))

    _synthesize(ledger)

    row = ledger.connection.execute("SELECT singleton, drafted_content FROM proposal").fetchone()
    assert row["singleton"] == 1
    assert json.loads(row["drafted_content"])["singleton"] is True


def test_duplicate_evidence_citations_are_normalized_not_fatal(
    ledger: Ledger, mock_claude
) -> None:
    """Live finding: real models sometimes cite an incident twice; keep first."""
    incident_id = _promoted(ledger, "dup-evidence")
    proposal = _proposal(incident_id, "claude-md")
    proposal["evidence"] = [
        {"incident_id": incident_id, "quote": "This is not what I asked."},
        {"incident_id": incident_id, "quote": "This is not what I asked."},
    ]
    mock_claude.enqueue_response(_envelope(proposal))
    results = _synthesize(ledger)
    assert len(results) == 1 and results[0].proposal_ids
    stored = ledger.get_proposal(results[0].proposal_ids[0])
    assert stored is not None
