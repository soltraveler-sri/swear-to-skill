from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path

import pytest

from s2s import gate
from s2s.auditor import audit_remedies
from s2s.cli import main
from s2s.config import Auditor
from s2s.curator import run_pass
from s2s.gate import GateTargets, install
from s2s.ledger import Ledger
from s2s.synthesist import collect_remedy_surface


UTC = timezone.utc


@pytest.fixture
def ledger(monkeypatch: pytest.MonkeyPatch, tmp_path) -> Ledger:
    monkeypatch.setenv("S2S_HOME", str(tmp_path / "s2s-home"))
    database = Ledger()
    yield database
    database.close()


def _incident(ledger: Ledger, stamp: str, message: str) -> int:
    incident_id = ledger.create_incident(
        source="claude-code",
        session_id=f"session-{stamp}-{message}",
        project="project",
        message=message,
        occurred_at=stamp,
    )
    ledger.triage_incident(
        incident_id,
        label="ignored-instruction",
        one_liner="Agent ignored an explicit instruction.",
        severity="2",
        confidence=0.9,
        context_pack_pointer="fixture/context.jsonl#message",
    )
    return incident_id


def _session(ledger: Ledger, stamp: str, name: str) -> None:
    ledger.upsert_session_stats(
        source="claude-code",
        session_id=f"stats-{name}",
        project="project",
        dominant_model=None,
        direct_message_count=5,
        hit_count=0,
        first_timestamp=stamp,
        last_timestamp=stamp,
        scanned_at=stamp,
    )


def _draft(incident_id: int, text: str = "Honor explicit constraints.") -> str:
    return json.dumps(
        {
            "remedy_type": "claude-md",
            "routing_rationale": "Cheap standing rule.",
            "failure_statement": "The agent ignores explicit constraints.",
            "remedy_content": {"text": text, "target": "global"},
            "evidence": [{"incident_id": incident_id, "quote": "Ignored it."}],
            "dedup": [],
            "confidence": 0.9,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _installed_remedy(ledger: Ledger, *, installed_at: str) -> tuple[int, int]:
    original_id = _incident(ledger, "2025-12-01T12:00:00+00:00", "ORIGINAL")
    proposal_id = ledger.create_proposal(
        remedy_type="claude-md",
        drafted_content=_draft(original_id),
        evidence_incident_ids=[original_id],
        dedup_verdict="[]",
        gate_status="installed",
    )
    remedy_id = ledger.create_remedy(
        artifact_type="claude-md",
        artifact_path="/fake/CLAUDE.md",
        proposal_id=proposal_id,
        installed_at=installed_at,
    )
    return remedy_id, proposal_id


def test_per_cluster_rate_normalizes_by_active_sessions(ledger: Ledger) -> None:
    remedy_id, _ = _installed_remedy(ledger, installed_at="2026-02-01T00:00:00+00:00")
    for index in range(10):
        _session(ledger, f"2026-01-{10 + index:02d}T12:00:00+00:00", f"pre-{index}")
    for index in range(20):
        _session(ledger, f"2026-02-{1 + index:02d}T12:00:00+00:00", f"post-{index}")
    for index in range(5):
        _incident(ledger, f"2026-01-{10 + index:02d}T13:00:00+00:00", f"PRE-{index}")
    _incident(ledger, "2026-02-10T13:00:00+00:00", "POST")

    outcome = audit_remedies(
        ledger,
        now=datetime(2026, 3, 5, tzinfo=UTC),
        config=Auditor(min_post_install_sessions=20, min_post_install_days=30),
    )[0]

    assert outcome.remedy_id == remedy_id
    assert (outcome.pre_incidents, outcome.pre_sessions, outcome.pre_rate) == (6, 10, 0.6)
    assert (outcome.post_incidents, outcome.post_sessions, outcome.post_rate) == (1, 20, 0.05)
    assert outcome.verdict == "effective"


def test_installed_yesterday_is_honest_insufficient_data(ledger: Ledger) -> None:
    _installed_remedy(ledger, installed_at="2026-03-04T00:00:00+00:00")
    _session(ledger, "2026-03-04T12:00:00+00:00", "post")

    outcome = audit_remedies(
        ledger,
        now=datetime(2026, 3, 5, tzinfo=UTC),
        config=Auditor(min_post_install_sessions=20, min_post_install_days=30),
    )[0]

    assert outcome.verdict == "insufficient-data"
    assert ledger.pending_proposals() == []


def test_persisting_creates_synthesized_revision_with_counter_evidence(
    ledger: Ledger, mock_claude, monkeypatch: pytest.MonkeyPatch
) -> None:
    remedy_id, original_proposal = _installed_remedy(
        ledger, installed_at="2026-02-01T00:00:00+00:00"
    )
    for index in range(2):
        _session(ledger, f"2026-01-{10 + index:02d}T12:00:00+00:00", f"pre-{index}")
        _incident(ledger, f"2026-01-{10 + index:02d}T13:00:00+00:00", f"PRE-{index}")
    counter_ids = []
    for index in range(3):
        _session(ledger, f"2026-02-{10 + index:02d}T12:00:00+00:00", f"post-{index}")
        counter_ids.append(_incident(ledger, f"2026-02-{10 + index:02d}T13:00:00+00:00", f"POST-{index}"))
    for index in range(2):
        counter_ids.append(_incident(ledger, "2026-02-12T14:00:00+00:00", f"POST-EXTRA-{index}"))
    Path(os.environ["S2S_HOME"]).joinpath("config.toml").write_text(
        "[auditor]\nmin_post_install_sessions = 2\nmin_post_install_days = 30\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "s2s.synthesist._bounded_evidence_packs",
        lambda incidents, budget: "\n".join(f"COUNTER #{item.id}" for item in incidents),
    )
    surface = collect_remedy_surface(ledger)
    mock_claude.enqueue_response(
        {
            "result": {
                "remedy_type": "claude-md",
                "routing_rationale": "The first rule was too vague.",
                "failure_statement": "The agent still ignores explicit constraints.",
                "remedy_content": {"text": "List constraints before acting.", "target": "global"},
                "evidence": [
                    {"incident_id": item, "quote": f"Counter-evidence {item}."}
                    for item in counter_ids
                ],
                "dedup": [
                    {
                        "existing": reference,
                        "verdict": "overlap" if reference == f"proposal #{original_proposal}" else "clear",
                        "reason": "Replacement." if reference == f"proposal #{original_proposal}" else "Distinct surface.",
                    }
                    for reference in surface.references
                ],
                "overlap_action": {"revises": f"proposal #{original_proposal}"},
                "confidence": 0.9,
            },
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
    )
    for incident in ledger.audit_incidents("ignored-instruction"):
        ledger.apply_curator_incident_verdict(incident.id, "park", reason="already reviewed")

    result = run_pass(ledger, assume_yes=True)
    assert result.audit_outcomes and result.audit_outcomes[0].verdict == "persisting"

    proposals = ledger.pending_proposals()
    assert len(proposals) == 1
    revision = proposals[0]
    assert revision.proposal_kind == "revision" and revision.target_remedy_id == remedy_id
    assert revision.revises == f"proposal #{original_proposal}"
    assert revision.evidence_incident_ids == tuple(counter_ids)
    prompt = str(mock_claude.invocations()[-1]["stdin"])
    assert "occurred DESPITE that remedy" in prompt
    assert "wrong trigger description" in prompt


def test_silent_retirement_is_queued_and_visible_in_proposals_cli(
    ledger: Ledger, capsys: pytest.CaptureFixture[str]
) -> None:
    remedy_id, _ = _installed_remedy(ledger, installed_at="2026-01-01T00:00:00+00:00")
    _session(ledger, "2026-01-05T12:00:00+00:00", "pre")

    outcome = audit_remedies(
        ledger,
        now=datetime(2026, 5, 1, tzinfo=UTC),
        config=Auditor(silent_days=90),
    )[0]
    assert outcome.verdict == "silent" and outcome.proposal_id is not None
    ledger.close()

    assert main(["proposals", "--json"]) == 0
    rendered = json.loads(capsys.readouterr().out)
    assert rendered[0]["proposal_kind"] == "retirement"
    assert rendered[0]["target_remedy_id"] == remedy_id


def test_retirement_approval_rolls_back_through_gate(
    ledger: Ledger, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    incident_id = _incident(ledger, "2025-12-01T12:00:00+00:00", "ORIGINAL")
    proposal_id = ledger.create_proposal(
        remedy_type="claude-md",
        drafted_content=_draft(incident_id),
        evidence_incident_ids=[incident_id],
        dedup_verdict="[]",
        gate_status="pending",
    )
    original = ledger.approve_proposal(proposal_id)
    targets = GateTargets(
        skills_dir=tmp_path / ".claude" / "skills",
        global_claude_md=tmp_path / ".claude" / "CLAUDE.md",
        settings_path=tmp_path / ".claude" / "settings.json",
        state_dir=tmp_path / "s2s-home" / "state",
    )
    result = install(original, targets=targets)
    ledger.connection.execute(
        "UPDATE remedy SET installed_at = ? WHERE id = ?",
        ("2026-01-01T00:00:00+00:00", result.remedy_id),
    )
    ledger.connection.commit()
    audit_remedies(
        ledger,
        now=datetime(2026, 5, 1, tzinfo=UTC),
        config=Auditor(silent_days=90),
    )
    retirement = ledger.pending_proposals()[0]
    ledger.close()
    monkeypatch.setattr(gate, "default_targets", lambda: targets)

    assert main(["approve", str(retirement.id)]) == 0
    with Ledger() as reopened:
        assert reopened.get_remedy(result.remedy_id).state == "rolled-back"  # type: ignore[union-attr]
        assert reopened.get_proposal(retirement.id).gate_status == "installed"  # type: ignore[union-attr]
    assert not targets.global_claude_md.exists()


def test_auditor_has_no_gate_mutation_dependency() -> None:
    source = (Path(__file__).parents[1] / "src" / "s2s" / "auditor.py").read_text(encoding="utf-8")
    assert "from .gate" not in source and "gate.rollback" not in source and "gate.install" not in source
