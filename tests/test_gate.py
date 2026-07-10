from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess

import pytest

from s2s import gate
from s2s.cli import main
from s2s.gate import GateTargets, HandEditedError, install, recover_pending_intents, rollback
from s2s.ledger import Ledger, Proposal


@pytest.fixture
def gate_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Ledger, GateTargets]:
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


def _draft(incident_id: int, remedy_type: str, *, text: str = "Honor explicit constraints.") -> str:
    content: dict[str, object] = {
        "skill": {
            "name": "instruction-check",
            "description": "Use when a task has explicit constraints before action.",
            "body_markdown": f"# Check\n\n{text}",
        },
        "claude-md": {"text": text, "target": "global"},
        "hook": {"event": "PreToolUse", "command_sketch": f"check-scope --rule {text!r}"},
        "benchmark-only": {"note": text},
    }[remedy_type]
    return json.dumps(
        {
            "remedy_type": remedy_type,
            "routing_rationale": "Cheapest effective remedy.",
            "failure_statement": "The agent ignores explicit constraints.",
            "remedy_content": content,
            "evidence": [{"incident_id": incident_id, "quote": "You ignored the constraint."}],
            "dedup": [],
            "confidence": 0.91,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _pending(
    ledger: Ledger,
    remedy_type: str,
    *,
    text: str = "Honor explicit constraints.",
    revises: str | None = None,
) -> Proposal:
    incident_id = ledger.create_incident(
        source="claude-code",
        session_id=f"gate-{remedy_type}-{ledger.pending_proposal_count()}",
        project="project",
        message="You ignored the constraint.",
    )
    ledger.transition_incident(incident_id, "triaged", reason="triaged")
    ledger.transition_incident(incident_id, "open", reason="accepted")
    ledger.transition_incident(incident_id, "promoted", reason="generalizable")
    proposal_id = ledger.create_proposal(
        remedy_type=remedy_type,
        drafted_content=_draft(incident_id, remedy_type, text=text),
        evidence_incident_ids=[incident_id],
        dedup_verdict="[]",
        gate_status="pending",
        revises=revises,
    )
    ledger.transition_incident(incident_id, "in-proposal", reason="drafted")
    proposal = ledger.get_proposal(proposal_id)
    assert proposal is not None
    return proposal


def _approve(ledger: Ledger, proposal: Proposal) -> Proposal:
    return ledger.approve_proposal(proposal.id)


def _git_messages(state_dir: Path) -> list[str]:
    result = subprocess.run(
        ["git", "log", "--format=%s"],
        cwd=state_dir,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.splitlines()


@pytest.mark.parametrize("remedy_type", ["skill", "claude-md", "hook", "benchmark-only"])
def test_round_trip_every_remedy_type(
    gate_env: tuple[Ledger, GateTargets], remedy_type: str
) -> None:
    ledger, targets = gate_env
    if remedy_type == "claude-md":
        targets.global_claude_md.parent.mkdir(parents=True)
        targets.global_claude_md.write_bytes(b"# User rules\nKeep this.\n")
        target = targets.global_claude_md
    elif remedy_type == "hook":
        targets.settings_path.parent.mkdir(parents=True)
        targets.settings_path.write_bytes(b'{"theme":"dark"}')
        target = targets.settings_path
    else:
        target = targets.skills_dir / "instruction-check" / "SKILL.md"
    original = target.read_bytes() if target.exists() else None
    proposal = _approve(ledger, _pending(ledger, remedy_type))

    result = install(proposal, targets=targets)

    remedy = ledger.get_remedy(result.remedy_id)
    assert remedy is not None and remedy.state == "installed"
    assert ledger.get_proposal(proposal.id).gate_status == "installed"  # type: ignore[union-attr]
    assert ledger.get_incident(proposal.evidence_incident_ids[0]).state == "remedied"  # type: ignore[union-attr]
    if remedy_type == "skill":
        assert f"# s2s:managed remedy={result.remedy_id}" in target.read_text()
    elif remedy_type == "claude-md":
        assert f"<!-- s2s:begin remedy={result.remedy_id} -->" in target.read_text()
    elif remedy_type == "hook":
        assert f"s2s:managed remedy={result.remedy_id}" in target.read_text()
    else:
        assert not target.exists()

    rollback(result.remedy_id, targets=targets)

    remedy = ledger.get_remedy(result.remedy_id)
    assert remedy is not None and remedy.state == "rolled-back"
    if original is None:
        assert not target.exists()
    else:
        assert target.read_bytes() == original
        assert len(list(target.parent.glob(f"{target.name}.s2s-backup-*"))) == 1
    messages = _git_messages(targets.state_dir)
    assert messages[0] == f"rollback remedy {result.remedy_id}"
    assert messages[-1] == f"intent: install remedy {result.remedy_id}"
    assert len(messages) == 3


def test_marker_excision_preserves_user_content_around_block(
    gate_env: tuple[Ledger, GateTargets]
) -> None:
    ledger, targets = gate_env
    path = targets.global_claude_md
    path.parent.mkdir(parents=True)
    path.write_bytes(b"original-before\n")
    result = install(_approve(ledger, _pending(ledger, "claude-md")), targets=targets)
    installed = path.read_bytes()
    path.write_bytes(b"user-prepend\n" + installed + b"user-append\n")

    rollback(result.remedy_id, targets=targets)

    assert path.read_bytes() == b"user-prepend\noriginal-before\nuser-append\n"


def test_hand_edited_block_requires_force(
    gate_env: tuple[Ledger, GateTargets]
) -> None:
    ledger, targets = gate_env
    path = targets.global_claude_md
    result = install(_approve(ledger, _pending(ledger, "claude-md")), targets=targets)
    path.write_text(path.read_text().replace("Honor explicit", "HAND EDITED"))

    with pytest.raises(HandEditedError, match="force=True"):
        rollback(result.remedy_id, targets=targets)
    assert "HAND EDITED" in path.read_text()

    rollback(result.remedy_id, force=True, targets=targets)
    assert not path.exists()


def test_skill_missing_marker_and_extra_file_require_force(
    gate_env: tuple[Ledger, GateTargets]
) -> None:
    ledger, targets = gate_env
    result = install(_approve(ledger, _pending(ledger, "skill")), targets=targets)
    skill_dir = targets.skills_dir / "instruction-check"
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(
        skill_file.read_text().replace(
            f"# s2s:managed remedy={result.remedy_id}\n", ""
        )
    )
    (skill_dir / "user-note.txt").write_text("hand edit", encoding="utf-8")

    with pytest.raises(HandEditedError, match="force=True"):
        rollback(result.remedy_id, targets=targets)
    assert skill_dir.exists()

    rollback(result.remedy_id, force=True, targets=targets)
    assert not skill_dir.exists()


def test_benchmark_only_never_touches_claude_surfaces(
    gate_env: tuple[Ledger, GateTargets]
) -> None:
    ledger, targets = gate_env
    result = install(_approve(ledger, _pending(ledger, "benchmark-only")), targets=targets)
    assert not targets.skills_dir.parent.exists()
    rollback(result.remedy_id, targets=targets)
    assert not targets.skills_dir.parent.exists()


def test_write_ahead_commit_precedes_target_mutation(
    gate_env: tuple[Ledger, GateTargets], monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, targets = gate_env
    proposal = _approve(ledger, _pending(ledger, "skill"))
    target = targets.skills_dir / "instruction-check" / "SKILL.md"

    def crash_after_intent(intent, received_targets):
        assert received_targets == targets
        assert not target.exists()
        assert _git_messages(targets.state_dir)[0].startswith("intent: install remedy ")
        raise RuntimeError("simulated crash")

    monkeypatch.setattr(gate, "_apply_install", crash_after_intent)
    with pytest.raises(RuntimeError, match="simulated crash"):
        install(proposal, targets=targets)
    assert recover_pending_intents(targets=targets)[0]["proposal"]["id"] == proposal.id


def test_direct_intent_is_recoverable_without_target_write(
    gate_env: tuple[Ledger, GateTargets]
) -> None:
    ledger, targets = gate_env
    proposal = _approve(ledger, _pending(ledger, "claude-md"))
    gate.write_install_intent(proposal, 77, targets=targets)
    assert [item["remedy_id"] for item in recover_pending_intents(targets=targets)] == [77]
    assert not targets.global_claude_md.exists()


def test_committed_install_resumes_ledger_finalization_without_second_target_write(
    gate_env: tuple[Ledger, GateTargets], monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, targets = gate_env
    proposal = _approve(ledger, _pending(ledger, "claude-md"))
    original_finalize = Ledger.mark_remedy_installed
    calls = 0

    def crash_once(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("crash after install commit")
        return original_finalize(self, *args, **kwargs)

    monkeypatch.setattr(Ledger, "mark_remedy_installed", crash_once)
    with pytest.raises(RuntimeError, match="after install commit"):
        install(proposal, targets=targets)
    installed_bytes = targets.global_claude_md.read_bytes()
    assert len(_git_messages(targets.state_dir)) == 2

    resumed = install(proposal, targets=targets)

    assert targets.global_claude_md.read_bytes() == installed_bytes
    assert len(_git_messages(targets.state_dir)) == 2
    assert ledger.get_remedy(resumed.remedy_id).state == "installed"  # type: ignore[union-attr]


def test_committed_rollback_resumes_ledger_finalization_without_second_action_commit(
    gate_env: tuple[Ledger, GateTargets], monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, targets = gate_env
    result = install(_approve(ledger, _pending(ledger, "claude-md")), targets=targets)
    original_finalize = Ledger.mark_remedy_rolled_back
    calls = 0

    def crash_once(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("crash after rollback target")
        return original_finalize(self, *args, **kwargs)

    monkeypatch.setattr(Ledger, "mark_remedy_rolled_back", crash_once)
    with pytest.raises(RuntimeError, match="after rollback target"):
        rollback(result.remedy_id, targets=targets)
    assert not targets.global_claude_md.exists()
    assert len(_git_messages(targets.state_dir)) == 3

    resumed = rollback(result.remedy_id, targets=targets)

    assert resumed.remedy_id == result.remedy_id
    assert len(_git_messages(targets.state_dir)) == 3
    assert ledger.get_remedy(result.remedy_id).state == "rolled-back"  # type: ignore[union-attr]


def test_revision_replaces_in_place_and_rolls_back_to_prior_artifact(
    gate_env: tuple[Ledger, GateTargets]
) -> None:
    ledger, targets = gate_env
    first_proposal = _approve(ledger, _pending(ledger, "claude-md", text="OLD RULE"))
    first = install(first_proposal, targets=targets)
    second_proposal = _approve(
        ledger,
        _pending(
            ledger,
            "claude-md",
            text="NEW RULE",
            revises=f"proposal #{first_proposal.id}",
        ),
    )

    second = install(second_proposal, targets=targets)

    content = targets.global_claude_md.read_text()
    assert "NEW RULE" in content and "OLD RULE" not in content
    assert content.count("s2s:begin") == 1
    assert ledger.get_remedy(second.remedy_id).revises_remedy_id == first.remedy_id  # type: ignore[union-attr]
    rollback(second.remedy_id, targets=targets)
    assert "OLD RULE" in targets.global_claude_md.read_text()
    assert targets.global_claude_md.read_text().count("s2s:begin") == 1


@pytest.mark.parametrize("remedy_type", ["skill", "hook"])
def test_skill_and_hook_revisions_replace_without_duplicates(
    gate_env: tuple[Ledger, GateTargets], remedy_type: str
) -> None:
    ledger, targets = gate_env
    if remedy_type == "hook":
        targets.settings_path.parent.mkdir(parents=True)
        targets.settings_path.write_text('{"theme":"dark"}', encoding="utf-8")
    first_proposal = _approve(ledger, _pending(ledger, remedy_type, text="OLD RULE"))
    first = install(first_proposal, targets=targets)
    second = install(
        _approve(
            ledger,
            _pending(
                ledger,
                remedy_type,
                text="NEW RULE",
                revises=f"proposal #{first_proposal.id}",
            ),
        ),
        targets=targets,
    )
    path = (
        targets.settings_path
        if remedy_type == "hook"
        else targets.skills_dir / "instruction-check" / "SKILL.md"
    )
    content = path.read_text()
    assert "NEW RULE" in content and "OLD RULE" not in content
    assert content.count(f"s2s:managed remedy={first.remedy_id}") == 1

    rollback(second.remedy_id, targets=targets)
    restored = path.read_text()
    assert "OLD RULE" in restored and "NEW RULE" not in restored
    assert restored.count(f"s2s:managed remedy={first.remedy_id}") == 1


def test_proposals_json_and_reject_reason(
    gate_env: tuple[Ledger, GateTargets], capsys: pytest.CaptureFixture[str]
) -> None:
    ledger, _ = gate_env
    proposal = _pending(ledger, "claude-md")
    ledger.close()

    assert main(["proposals", "--json"]) == 0
    rendered = json.loads(capsys.readouterr().out)
    assert rendered[0]["remedy_type"] == "claude-md"
    assert rendered[0]["evidence"][0]["quote"] == "You ignored the constraint."
    assert rendered[0]["confidence"] == 0.91

    assert main(["reject", str(proposal.id), "--reason", "too broad"]) == 0
    with Ledger() as reopened:
        rejected = reopened.get_proposal(proposal.id)
        assert rejected is not None
        assert rejected.gate_status == "rejected"
        assert rejected.rejection_reason == "too broad"


def test_approve_edit_runs_editor_and_installs_edited_content(
    gate_env: tuple[Ledger, GateTargets],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ledger, targets = gate_env
    proposal = _pending(ledger, "claude-md")
    ledger.close()
    editor = tmp_path / "append-editor"
    editor.write_text("#!/bin/sh\nprintf '\\nEDITED BY HUMAN' >> \"$1\"\n", encoding="utf-8")
    editor.chmod(editor.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("EDITOR", str(editor))
    monkeypatch.setattr(gate, "default_targets", lambda: targets)

    assert main(["approve", str(proposal.id), "--edit"]) == 0
    assert "EDITED BY HUMAN" in targets.global_claude_md.read_text()
    assert "installed remedy" in capsys.readouterr().out
