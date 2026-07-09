from __future__ import annotations

import sqlite3

import pytest

from s2s.ledger import (
    IllegalTransitionError,
    Ledger,
    SCHEMA_VERSION,
    SchemaVersionError,
)
from s2s.paths import resolve_paths


@pytest.fixture
def ledger(monkeypatch: pytest.MonkeyPatch, tmp_path) -> Ledger:
    monkeypatch.setenv("S2S_HOME", str(tmp_path / "s2s-home"))
    database = Ledger()
    yield database
    database.close()


def create_open_incident(ledger: Ledger, *, label: str = "ignored-instruction") -> int:
    incident_id = ledger.create_incident(
        source="claude-code",
        session_id="session-1",
        project="project-a",
        message="That was not what I asked.",
        occurred_at="2026-07-09T12:00:00+00:00",
    )
    ledger.triage_incident(
        incident_id,
        label=label,
        one_liner="The agent ignored an explicit instruction.",
        severity="high",
        confidence=0.91,
        context_pack_pointer="archive/project-a/session-1.context.json",
    )
    return incident_id


def test_schema_is_created_at_resolved_s2s_home_and_is_versioned(ledger: Ledger) -> None:
    assert ledger.path == resolve_paths().ledger_path
    assert ledger.connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_legal_state_machine_transitions_append_history(ledger: Ledger) -> None:
    incident_id = create_open_incident(ledger)

    ledger.transition_incident(incident_id, "promoted", reason="generalizable lesson")
    ledger.transition_incident(incident_id, "in-proposal", reason="proposal drafted")
    ledger.transition_incident(incident_id, "remedied", reason="remedy installed")

    assert ledger.get_incident(incident_id).state == "remedied"  # type: ignore[union-attr]
    assert [entry.to_state for entry in ledger.state_history(incident_id)] == [
        "detected",
        "triaged",
        "open",
        "promoted",
        "in-proposal",
        "remedied",
    ]


def test_triage_dismissal_uses_its_required_intermediate_state(ledger: Ledger) -> None:
    incident_id = ledger.create_incident(
        source="claude-code",
        session_id="session-dismissed-at-triage",
        project="project-a",
        message="This is quoted text, not frustration.",
    )

    ledger.triage_incident(
        incident_id,
        label="other",
        one_liner="Quoted text was not an agent complaint.",
        severity="low",
        confidence=0.99,
        context_pack_pointer="archive/project-a/triage-dismissal.context.json",
        dismissed=True,
    )

    assert [entry.to_state for entry in ledger.state_history(incident_id)] == [
        "detected",
        "triaged",
        "dismissed-triage",
    ]


def test_illegal_state_machine_transition_is_rejected_without_history(ledger: Ledger) -> None:
    incident_id = ledger.create_incident(
        source="codex",
        session_id="session-2",
        project="project-b",
        message="Please read the spec first.",
    )

    with pytest.raises(IllegalTransitionError):
        ledger.transition_incident(incident_id, "open", reason="skipping triage")

    assert ledger.get_incident(incident_id).state == "detected"  # type: ignore[union-attr]
    assert [entry.to_state for entry in ledger.state_history(incident_id)] == ["detected"]


def test_parked_incidents_wake_when_their_cluster_receives_an_arrival(ledger: Ledger) -> None:
    incident_id = create_open_incident(ledger, label="shallow-investigation")
    ledger.transition_incident(incident_id, "parked", reason="wait for recurrence")

    assert ledger.wake_parked_incidents("shallow-investigation") == [incident_id]
    assert ledger.get_incident(incident_id).state == "open"  # type: ignore[union-attr]
    assert [entry.to_state for entry in ledger.state_history(incident_id)][-2:] == ["parked", "open"]


def test_queue_item_is_durable_across_a_crash_simulated_reopen(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("S2S_HOME", str(tmp_path / "s2s-home"))
    first_process = Ledger()
    item_id = first_process.enqueue_item("archive/project/session.jsonl", "claude-code")
    first_process.close()

    reopened = Ledger()
    try:
        pending = reopened.pending_queue_items()
        assert [(item.id, item.item_path, item.processed_at) for item in pending] == [
            (item_id, "archive/project/session.jsonl", None)
        ]
    finally:
        reopened.close()


def test_dismissing_an_incident_never_deletes_it(ledger: Ledger) -> None:
    incident_id = create_open_incident(ledger)
    ledger.transition_incident(incident_id, "dismissed-reviewed", reason="not actionable")

    dismissed = ledger.incidents_in_state("dismissed-reviewed")
    assert [incident.id for incident in dismissed] == [incident_id]
    assert ledger.get_incident(incident_id).message == "That was not what I asked."  # type: ignore[union-attr]


def test_wal_allows_a_reader_while_another_process_writes(ledger: Ledger) -> None:
    reader = sqlite3.connect(ledger.path)
    try:
        assert ledger.connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        reader.execute("BEGIN")
        assert reader.execute("SELECT COUNT(*) FROM queue").fetchone()[0] == 0
        ledger.enqueue_item("archive/project/concurrent.jsonl", "claude-code")
        # The read transaction stays usable while the writer commits its queue item.
        assert reader.execute("SELECT COUNT(*) FROM queue").fetchone()[0] == 0
        reader.execute("COMMIT")
    finally:
        reader.close()


def test_newer_schema_is_rejected_by_the_forward_only_migration_runner(tmp_path) -> None:
    database_path = tmp_path / "newer-ledger.db"
    connection = sqlite3.connect(database_path)
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    connection.close()

    with pytest.raises(SchemaVersionError):
        Ledger(database_path)


def test_cluster_helpers_and_metrics_are_derived_from_incidents(ledger: Ledger) -> None:
    first = create_open_incident(ledger, label="other")
    second = create_open_incident(ledger, label="ignored-instruction")
    ledger.create_incident(
        source="codex",
        session_id="session-3",
        project="project-b",
        message="You ignored my constraint again.",
        label="ignored-instruction",
        occurred_at="2026-07-10T12:00:00+00:00",
    )
    proposal_id = ledger.create_proposal(
        remedy_type="claude-md",
        drafted_content="Read explicit instructions before acting.",
        evidence_incident_ids=[second],
        dedup_verdict="no overlap",
        gate_status="approved",
    )
    remedy_id = ledger.create_remedy(
        artifact_type="claude-md",
        artifact_path="CLAUDE.md",
        proposal_id=proposal_id,
    )

    clusters = {cluster.label: cluster for cluster in ledger.cluster_stats()}
    assert clusters["ignored-instruction"].incident_count == 2
    assert clusters["ignored-instruction"].project_count == 2
    assert clusters["ignored-instruction"].remedy_ids == (remedy_id,)
    assert clusters["other"].incident_count == 1
    assert ledger.singleton_ratio() == 0.5
    assert ledger.other_share() == 1 / 3
    assert first in [incident.id for incident in ledger.unreviewed_incidents()]


def test_dismissed_triage_can_be_resurrected_by_curator(ledger: Ledger) -> None:
    """NORTHSTAR §4 Stage 3: Curator QC sampling may resurrect a triage dismissal."""
    incident_id = ledger.create_incident(
        source="claude-code",
        session_id="session-qc",
        project="project-a",
        message="ugh, forget it",
        occurred_at="2026-07-09T12:00:00+00:00",
    )
    ledger.transition_incident(incident_id, "triaged", reason="triage complete")
    ledger.transition_incident(incident_id, "dismissed-triage", reason="judged not authentic")
    ledger.transition_incident(incident_id, "open", reason="curator QC resurrection")
    assert ledger.get_incident(incident_id).state == "open"
