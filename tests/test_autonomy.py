from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess

import pytest

from s2s import gate, pump
from s2s.config import (
    Autonomy,
    AutonomyState,
    Config,
    effective_autonomy_state,
    set_autonomy_state,
)
from s2s.gate import GateTargets, adjudicate_pending_autonomously, install
from s2s.ledger import Ledger, Proposal
from s2s.llm import LLMError


@pytest.fixture
def autonomy_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[Ledger, GateTargets]:
    home = tmp_path / "s2s-home"
    monkeypatch.setenv("S2S_HOME", str(home))
    targets = GateTargets(
        skills_dir=tmp_path / ".claude" / "skills",
        global_claude_md=tmp_path / ".claude" / "CLAUDE.md",
        settings_path=tmp_path / ".claude" / "settings.json",
        state_dir=home / "state",
        project_claude_md=tmp_path / "project" / "CLAUDE.md",
    )
    ledger = Ledger()
    yield ledger, targets
    ledger.close()


def _config(**changes: object) -> Config:
    return Config(autonomy=replace(Autonomy(), **changes))


def _draft(
    incident_id: int,
    remedy_type: str,
    confidence: float,
    *,
    action: str | None = None,
    name: str = "autonomy-check",
) -> str:
    content: dict[str, object]
    if remedy_type == "skill":
        content = {
            "name": name,
            "description": "Use when explicit constraints need a check.",
            "body_markdown": "# Check\n\nHonor the constraint.",
        }
    elif remedy_type == "hook":
        content = {"event": "PreToolUse", "command_sketch": "check-scope"}
    else:
        content = {"text": "Honor explicit constraints.", "target": "global"}
    payload: dict[str, object] = {
        "remedy_type": remedy_type,
        "routing_rationale": "Cheapest effective remedy.",
        "failure_statement": "The agent ignores explicit constraints.",
        "remedy_content": content,
        "evidence": [{"incident_id": incident_id, "quote": "You ignored it."}],
        "dedup": [],
        "confidence": confidence,
    }
    if action is not None:
        payload["action"] = action
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _pending(
    ledger: Ledger,
    remedy_type: str = "claude-md",
    *,
    confidence: float = 0.95,
    singleton: bool = False,
    revises: str | None = None,
    action: str | None = None,
    name: str = "autonomy-check",
) -> Proposal:
    sequence = int(ledger.connection.execute("SELECT COUNT(*) FROM proposal").fetchone()[0])
    incident_id = ledger.create_incident(
        source="claude-code",
        session_id=f"autonomy-{sequence}",
        project="project",
        message="You ignored it.",
    )
    ledger.transition_incident(incident_id, "triaged", reason="triaged")
    ledger.transition_incident(incident_id, "open", reason="accepted")
    ledger.transition_incident(incident_id, "promoted", reason="generalizable")
    proposal_id = ledger.create_proposal(
        remedy_type=remedy_type,
        drafted_content=_draft(
            incident_id, remedy_type, confidence, action=action, name=name
        ),
        evidence_incident_ids=[incident_id],
        dedup_verdict="[]",
        gate_status="pending",
        revises=revises,
        singleton=singleton,
    )
    ledger.transition_incident(incident_id, "in-proposal", reason="drafted")
    proposal = ledger.get_proposal(proposal_id)
    assert proposal is not None
    return proposal


@pytest.mark.parametrize(
    "remedy_type,confidence,singleton,installed",
    [
        ("claude-md", 0.8, False, True),
        ("claude-md", 0.799, False, False),
        ("skill", 0.9, False, True),
        ("skill", 0.899, False, False),
        ("claude-md", 0.899, True, False),
        ("claude-md", 0.9, True, True),
    ],
)
def test_confidence_bars_and_singleton_escalation(
    autonomy_env: tuple[Ledger, GateTargets],
    remedy_type: str,
    confidence: float,
    singleton: bool,
    installed: bool,
) -> None:
    ledger, targets = autonomy_env
    set_autonomy_state("autonomous")
    proposal = _pending(ledger, remedy_type, confidence=confidence, singleton=singleton)

    result = adjudicate_pending_autonomously(ledger, targets=targets)

    assert result.installed == int(installed)
    assert ledger.get_proposal(proposal.id).gate_status == ("installed" if installed else "pending")  # type: ignore[union-attr]
    if installed:
        remedy = ledger.remedy_for_proposal(proposal.id)
        assert remedy is not None and remedy.provenance == "auto"
        artifact = (
            targets.skills_dir / "autonomy-check" / "SKILL.md"
            if remedy_type == "skill"
            else targets.global_claude_md
        )
        assert "auto" in artifact.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "remedy_type,action",
    [
        ("hook", None),
        ("setting", None),
        ("claude-md", "retirement"),
        ("claude-md", "rollback"),
    ],
)
def test_high_blast_radius_actions_always_queue_for_human(
    autonomy_env: tuple[Ledger, GateTargets], remedy_type: str, action: str | None
) -> None:
    ledger, targets = autonomy_env
    set_autonomy_state("autonomous")
    proposal = _pending(ledger, remedy_type, action=action)

    result = adjudicate_pending_autonomously(ledger, targets=targets)

    assert result.queued_for_human == 1
    assert ledger.get_proposal(proposal.id).gate_status == "pending"  # type: ignore[union-attr]


@pytest.mark.parametrize("base_provenance,revision_installed", [("human", False), ("auto", True)])
def test_revision_only_auto_installs_over_an_auto_remedy(
    autonomy_env: tuple[Ledger, GateTargets],
    base_provenance: str,
    revision_installed: bool,
) -> None:
    ledger, targets = autonomy_env
    base = _pending(ledger)
    if base_provenance == "human":
        installed_base = install(ledger.approve_proposal(base.id), targets=targets)
    else:
        set_autonomy_state("autonomous")
        base_result = adjudicate_pending_autonomously(ledger, targets=targets)
        installed_base = ledger.remedy_for_proposal(base.id)
        assert base_result.installed == 1 and installed_base is not None
    revision = _pending(ledger, revises=f"proposal #{base.id}")
    set_autonomy_state("autonomous")

    result = adjudicate_pending_autonomously(ledger, targets=targets)

    assert result.installed == int(revision_installed)
    assert ledger.get_proposal(revision.id).gate_status == ("installed" if revision_installed else "pending")  # type: ignore[union-attr]
    assert installed_base is not None


def test_rolling_week_cap_uses_injected_clock_and_rolls_over(
    autonomy_env: tuple[Ledger, GateTargets]
) -> None:
    ledger, targets = autonomy_env
    set_autonomy_state("autonomous")
    config = _config(max_auto_remedies_per_week=1, max_active_auto_skills=10)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    first = _pending(ledger)
    assert adjudicate_pending_autonomously(
        ledger, targets=targets, config=config, clock=lambda: start
    ).installed == 1
    second = _pending(ledger)
    capped = adjudicate_pending_autonomously(
        ledger, targets=targets, config=config, clock=lambda: start + timedelta(days=6)
    )
    assert capped.installed == 0 and capped.queued_for_human == 1

    third = _pending(ledger)
    rolled = adjudicate_pending_autonomously(
        ledger, targets=targets, config=config, clock=lambda: start + timedelta(days=8)
    )
    assert rolled.installed == 1
    assert ledger.get_proposal(first.id).gate_status == "installed"  # type: ignore[union-attr]
    assert ledger.get_proposal(second.id).gate_status == "pending"  # type: ignore[union-attr]
    assert ledger.get_proposal(third.id).gate_status == "installed"  # type: ignore[union-attr]


def test_total_active_auto_remedy_cap(
    autonomy_env: tuple[Ledger, GateTargets]
) -> None:
    ledger, targets = autonomy_env
    set_autonomy_state("autonomous")
    config = _config(max_auto_remedies_per_week=99, max_active_auto_skills=1)
    _pending(ledger)
    assert adjudicate_pending_autonomously(ledger, targets=targets, config=config).installed == 1
    second = _pending(ledger)

    result = adjudicate_pending_autonomously(ledger, targets=targets, config=config)

    assert result.installed == 0 and result.decisions[0].action == "cap-hit"
    assert ledger.get_proposal(second.id).gate_status == "pending"  # type: ignore[union-attr]


def test_kill_switch_is_rechecked_mid_queue(
    autonomy_env: tuple[Ledger, GateTargets], monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, targets = autonomy_env
    _pending(ledger)
    second = _pending(ledger)
    calls = 0

    def state(_config: Config | None = None) -> AutonomyState:
        nonlocal calls
        calls += 1
        return AutonomyState("autonomous" if calls <= 3 else "review", "test")

    monkeypatch.setattr(gate, "effective_autonomy_state", state)
    result = adjudicate_pending_autonomously(ledger, targets=targets)

    assert result.installed == 1 and result.paused is True
    assert ledger.get_proposal(second.id).gate_status == "pending"  # type: ignore[union-attr]


def test_every_decision_is_git_logged_and_notified(
    autonomy_env: tuple[Ledger, GateTargets], monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, targets = autonomy_env
    set_autonomy_state("autonomous")
    events: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(gate.notify, "emit", lambda event, **fields: events.append((event, fields)))
    _pending(ledger, confidence=0.95)
    _pending(ledger, confidence=0.1)
    _pending(ledger, confidence=0.95)
    config = _config(max_auto_remedies_per_week=1)

    result = adjudicate_pending_autonomously(ledger, targets=targets, config=config)

    assert [item.action for item in result.decisions] == [
        "installed", "fallback-to-human", "cap-hit"
    ]
    log = (targets.state_dir / "autonomy-log.md").read_text(encoding="utf-8")
    assert log.count("\naction=") == 0
    assert log.count("| action=") == 3
    assert all(event == "autonomous_action" for event, _ in events)
    assert all(fields["provenance"] == "auto" for _, fields in events)
    messages = subprocess.run(
        ["git", "log", "--format=%s"],
        cwd=targets.state_dir,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    assert sum(message.startswith("autonomy:") for message in messages) == 3


def test_chaos_twenty_high_confidence_proposals_install_exactly_three(
    autonomy_env: tuple[Ledger, GateTargets], monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, targets = autonomy_env
    set_autonomy_state("autonomous")
    events: list[str] = []
    monkeypatch.setattr(gate.notify, "emit", lambda event, **fields: events.append(event))
    for _ in range(20):
        _pending(ledger, confidence=0.99)

    result = adjudicate_pending_autonomously(ledger, targets=targets)

    assert result.installed == 3
    assert result.queued_for_human == 17
    assert ledger.pending_proposal_count() == 17
    assert [decision.action for decision in result.decisions].count("cap-hit") == 17
    assert len(gate.read_autonomy_log(limit=100, targets=targets)) == 20
    assert events == ["autonomous_action"] * 20


def test_unattended_llm_failure_pauses_and_writes_status_note(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("S2S_HOME", str(home))
    set_autonomy_state("autonomous")
    with Ledger() as ledger:
        ledger.create_incident(
            source="claude-code", session_id="failure", project="project", message="bad"
        )
    events: list[str] = []
    monkeypatch.setattr(gate.notify, "emit", lambda event, **fields: events.append(event))
    monkeypatch.setattr(pump, "scan_pending_queue", lambda ledger: [])
    monkeypatch.setattr(pump, "_triage_due", lambda ledger, config: True)
    monkeypatch.setattr(
        pump,
        "triage_pending",
        lambda *args, **kwargs: (_ for _ in ()).throw(LLMError("boom")),
    )

    result = pump.run_pump()

    assert result.autonomy_paused is True
    state = effective_autonomy_state()
    assert state.paused is True and state.paused_reason == "boom"
    status = json.loads((home / "status.txt").read_text(encoding="utf-8"))
    assert "autonomy paused" in status["notes"][0]
    assert "action=pause" in (home / "state" / "autonomy-log.md").read_text(encoding="utf-8")
    assert events == ["autonomous_action"]


def test_pump_runs_synthesis_for_promoted_incidents_in_review_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("S2S_HOME", str(home))
    with Ledger() as ledger:
        proposal = _pending(ledger)
        ledger.connection.execute("DELETE FROM proposal_evidence WHERE proposal_id = ?", (proposal.id,))
        ledger.connection.execute("DELETE FROM proposal WHERE id = ?", (proposal.id,))
        ledger.connection.execute(
            "UPDATE incident SET state = 'promoted' WHERE id = ?",
            (proposal.evidence_incident_ids[0],),
        )
    calls: list[int] = []
    monkeypatch.setattr(pump, "scan_pending_queue", lambda ledger: [])
    monkeypatch.setattr(
        pump,
        "synthesize_pending",
        lambda ledger, assume_yes: calls.append(len(ledger.incidents_in_state("promoted"))) or [],
    )

    pump.run_pump()

    assert calls == [1]
