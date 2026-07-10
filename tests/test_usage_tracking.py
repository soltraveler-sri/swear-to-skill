from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from s2s.adapters import codex
from s2s.adapters.claude_code import extract_skill_usages
from s2s.auditor import audit_remedies
from s2s.config import Auditor
from s2s.curator import run_pass
from s2s.ledger import Ledger
from s2s.meter import collect_dashboard_data, render_dashboard, render_status
from s2s.scanner import scan_transcript
from s2s.synthesist import collect_remedy_surface


UTC = timezone.utc


@pytest.fixture
def ledger(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Ledger:
    monkeypatch.setenv("S2S_HOME", str(tmp_path / "s2s-home"))
    database = Ledger()
    yield database
    database.close()


def _write_claude_usage_transcript(path: Path) -> None:
    records = [
        {
            "type": "user",
            "uuid": "slash",
            "sessionId": "usage-session",
            "timestamp": "2026-07-09T10:00:00+00:00",
            "message": {
                "role": "user",
                "content": "This is broken <command-name>/s2s-scope-check</command-name>",
            },
        },
        {
            "type": "user",
            "uuid": "tool",
            "sessionId": "usage-session",
            "timestamp": "2026-07-09T10:01:00+00:00",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "content": [{"type": "text", "text": "Launching skill: s2s-verify-first"}],
                    }
                ],
            },
        },
        {
            "type": "user",
            "uuid": "foreign-slash",
            "sessionId": "usage-session",
            "timestamp": "2026-07-09T10:02:00+00:00",
            "message": {"role": "user", "content": "<command-name>/deploy</command-name>"},
        },
        {
            "type": "user",
            "uuid": "foreign-tool",
            "sessionId": "usage-session",
            "timestamp": "2026-07-09T10:03:00+00:00",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "content": "Launching skill: summarize"}],
            },
        },
    ]
    path.parent.mkdir(parents=True)
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")


def test_both_claude_invocation_shapes_are_isolated_idempotent_usage(
    ledger: Ledger, tmp_path: Path
) -> None:
    transcript = tmp_path / "project" / "usage.jsonl"
    _write_claude_usage_transcript(transcript)

    assert [usage.skill_name for usage in extract_skill_usages(transcript)] == [
        "s2s-scope-check",
        "s2s-verify-first",
    ]
    lexicon = {"categories": {"test": {"terms": ["broken", "Launching skill"]}}}
    scan_transcript(transcript, ledger, lexicon=lexicon)
    scan_transcript(transcript, ledger, lexicon=lexicon)

    assert [usage.skill_name for usage in ledger.skill_usage()] == [
        "s2s-scope-check",
        "s2s-verify-first",
    ]
    assert ledger.connection.execute("SELECT COUNT(*) FROM incident").fetchone()[0] == 0
    assert ledger.connection.execute("SELECT COUNT(*) FROM state_history").fetchone()[0] == 0


def test_codex_adapter_detects_both_usage_shapes_and_ignores_foreign_skills(tmp_path: Path) -> None:
    rollout = tmp_path / "rollout-usage.jsonl"
    records = [
        {"type": "session_meta", "payload": {"thread_id": "codex-usage", "cwd": "/work/demo"}},
        {
            "type": "event_msg",
            "timestamp": "2026-07-09T11:00:00+00:00",
            "payload": {
                "type": "user_message",
                "message": "<command-name>/s2s</command-name>",
            },
        },
        {
            "type": "response_item",
            "timestamp": "2026-07-09T11:01:00+00:00",
            "payload": {
                "type": "custom_tool_call_output",
                "output": "Launching skill: s2s-codex-check",
            },
        },
        {
            "type": "response_item",
            "timestamp": "2026-07-09T11:02:00+00:00",
            "payload": {"type": "function_call_output", "output": "Launching skill: deploy"},
        },
    ]
    rollout.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")

    usages = codex.extract_skill_usages(rollout)
    assert [(usage.skill_name, usage.is_companion) for usage in usages] == [
        ("s2s", True),
        ("s2s-codex-check", False),
    ]


def _installed_skill(ledger: Ledger, path: Path, *, installed_at: str) -> tuple[int, int, int]:
    incident_id = ledger.create_incident(
        source="claude-code",
        session_id="original-skill-session",
        project="project",
        message="The trigger was missed.",
        occurred_at="2026-04-01T00:00:00+00:00",
    )
    ledger.triage_incident(
        incident_id,
        label="ignored-instruction",
        one_liner="The agent missed the intended situation.",
        severity="2",
        confidence=0.9,
        context_pack_pointer="fixture/original.jsonl#message",
    )
    proposal_id = ledger.create_proposal(
        remedy_type="skill",
        drafted_content=json.dumps(
            {
                "remedy_type": "skill",
                "remedy_content": {
                    "name": "scope-check",
                    "description": "Use when a task names strict file boundaries.",
                    "body_markdown": "# Scope check\n",
                },
                "evidence": [{"incident_id": incident_id, "quote": "Missed it."}],
                "dedup": [],
                "confidence": 0.9,
            }
        ),
        evidence_incident_ids=[incident_id],
        dedup_verdict="[]",
        gate_status="installed",
    )
    path.parent.mkdir(parents=True)
    path.write_text(
        "---\nname: s2s-scope-check\n"
        "description: Use when a task names strict file boundaries.\n---\n# Scope check\n",
        encoding="utf-8",
    )
    remedy_id = ledger.create_remedy(
        artifact_type="skill",
        artifact_path=str(path),
        proposal_id=proposal_id,
        installed_at=installed_at,
    )
    ledger.transition_incident(incident_id, "promoted", reason="fixture promotion")
    ledger.transition_incident(incident_id, "in-proposal", reason="fixture proposal")
    ledger.transition_incident(incident_id, "remedied", reason="fixture install")
    return remedy_id, proposal_id, incident_id


def _scanned_sessions(ledger: Ledger, count: int) -> None:
    for index in range(count):
        stamp = f"2026-06-{index + 1:02d}T12:00:00+00:00"
        ledger.upsert_session_stats(
            source="claude-code",
            session_id=f"usage-observation-{index}",
            project="project",
            dominant_model=None,
            direct_message_count=1,
            hit_count=0,
            first_timestamp=stamp,
            last_timestamp=stamp,
            scanned_at=stamp,
        )


def test_unused_skill_audit_requires_both_observation_thresholds(
    ledger: Ledger, tmp_path: Path
) -> None:
    _installed_skill(
        ledger, tmp_path / "skills" / "s2s-scope-check" / "SKILL.md", installed_at="2026-05-01T00:00:00+00:00"
    )
    _scanned_sessions(ledger, 19)
    settings = Auditor(min_usage_observation_days=30, min_sessions_scanned=20)

    old_enough = audit_remedies(ledger, now=datetime(2026, 7, 5, tzinfo=UTC), config=settings)
    assert not any("never fired" in outcome.label for outcome in old_enough)

    _scanned_sessions(ledger, 20)
    too_young = audit_remedies(ledger, now=datetime(2026, 5, 30, tzinfo=UTC), config=settings)
    assert not any("never fired" in outcome.label for outcome in too_young)


def test_zero_usage_never_queues_retirement(
    ledger: Ledger, tmp_path: Path
) -> None:
    _installed_skill(
        ledger,
        tmp_path / "skills" / "s2s-scope-check" / "SKILL.md",
        installed_at="2026-05-01T00:00:00+00:00",
    )
    _scanned_sessions(ledger, 20)

    outcomes = audit_remedies(
        ledger,
        now=datetime(2026, 9, 1, tzinfo=UTC),
        config=Auditor(
            min_usage_observation_days=30,
            min_sessions_scanned=20,
            silent_days=90,
        ),
    )

    assert any("never fired" in outcome.label for outcome in outcomes)
    assert not any(proposal.proposal_kind == "retirement" for proposal in ledger.pending_proposals())


def test_unused_skill_routes_description_revision_through_synthesist(
    ledger: Ledger, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mock_claude
) -> None:
    remedy_id, original_proposal, incident_id = _installed_skill(
        ledger, tmp_path / "skills" / "s2s-scope-check" / "SKILL.md", installed_at="2026-05-01T00:00:00+00:00"
    )
    _scanned_sessions(ledger, 20)
    (Path(ledger.path).parent / "config.toml").write_text(
        "[auditor]\nmin_usage_observation_days = 30\nmin_sessions_scanned = 20\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "s2s.synthesist._bounded_evidence_packs",
        lambda incidents, budget: "\n".join(f"ORIGINAL #{item.id}" for item in incidents),
    )
    surface = collect_remedy_surface(ledger)
    mock_claude.enqueue_response(
        {
            "result": {
                "remedy_type": "skill",
                "routing_rationale": "The old trigger was undiscoverable.",
                "failure_statement": "The installed skill never fired.",
                "remedy_content": {
                    "name": "scope-check",
                    "description": "Use when file boundaries or explicit scope constraints are named.",
                    "body_markdown": "# Scope check\nConfirm the allowed files.",
                },
                "evidence": [{"incident_id": incident_id, "quote": "Original provenance."}],
                "dedup": [
                    {
                        "existing": reference,
                        "verdict": "overlap" if reference == f"proposal #{original_proposal}" else "clear",
                        "reason": "Replacement." if reference == f"proposal #{original_proposal}" else "Distinct.",
                    }
                    for reference in surface.references
                ],
                "overlap_action": {"revises": f"proposal #{original_proposal}"},
                "confidence": 0.9,
            },
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
    )

    result = run_pass(ledger, assume_yes=True)

    revision = ledger.pending_proposals()[0]
    assert revision.proposal_kind == "revision" and revision.target_remedy_id == remedy_id
    prompt = str(mock_claude.invocations()[-1]["stdin"])
    assert "Use when a task names strict file boundaries." in prompt
    assert "It never fired during 20 sessions scanned" in prompt
    assert result.calls == 1


def test_status_and_dashboard_render_usage_with_companion_flag(ledger: Ledger) -> None:
    for index, stamp in enumerate(
        (
            "2026-07-07T09:00:00+00:00",
            "2026-07-08T09:00:00+00:00",
            "2026-07-09T09:00:00+00:00",
        )
    ):
        assert ledger.record_skill_usage(
            skill_name="s2s-foo",
            session_id=f"session-{index}",
            source="claude-code",
            used_at=stamp,
        )
    ledger.record_skill_usage(
        skill_name="s2s",
        session_id="companion-session",
        source="codex",
        used_at="2026-07-09T10:00:00+00:00",
    )

    data = collect_dashboard_data(ledger)
    status = render_status(data, archived_sessions=0)
    dashboard = render_dashboard(data)

    assert "skill usage: s2s-foo used 3x (last 2026-07-09)" in status
    assert "skill usage: s2s (companion) used 1x (last 2026-07-09)" in status
    assert 'id="skill-usage"' in dashboard
    assert "s2s-foo" in dashboard and "generated" in dashboard and "companion" in dashboard
