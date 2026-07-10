from __future__ import annotations

from pathlib import Path

import pytest

from s2s.cli import COMMAND_ISSUES, build_parser, main


def test_all_issue_commands_have_left_the_cli_scaffold() -> None:
    assert set(COMMAND_ISSUES) == {
        "scan", "meter", "status", "triage", "run", "pump", "schedule",
        "proposals", "approve", "reject", "rollback", "log", "autonomy",
    }


def test_autonomy_commands_persist_override_without_rewriting_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("S2S_HOME", str(home))
    home.mkdir()
    config = home / "config.toml"
    original = "# keep me\n[autonomy]\nmode = 'review'\n"
    config.write_text(original, encoding="utf-8")

    assert main(["autonomy", "on"]) == 0
    assert "unattended pumps" in capsys.readouterr().out
    assert config.read_text(encoding="utf-8") == original
    assert main(["autonomy", "status"]) == 0
    assert "autonomy: autonomous" in capsys.readouterr().out
    assert main(["autonomy", "off"]) == 0
    assert "kill switch is active" in capsys.readouterr().out


def test_proposals_parser_accepts_json_output() -> None:
    args = build_parser().parse_args(["proposals", "--json"])
    assert args.command == "proposals" and args.json is True


def test_approve_parser_accepts_edit() -> None:
    args = build_parser().parse_args(["approve", "12", "--edit"])
    assert args.proposal_id == 12 and args.edit is True


def test_reject_parser_accepts_reason() -> None:
    args = build_parser().parse_args(["reject", "13", "--reason", "too broad"])
    assert args.proposal_id == 13 and args.reason == "too broad"


def test_rollback_parser_accepts_force() -> None:
    args = build_parser().parse_args(["rollback", "14", "--force"])
    assert args.remedy_id == 14 and args.force is True


def test_init_dispatch_uses_isolated_s2s_and_claude_homes(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("S2S_HOME", str(tmp_path / "s2s-home"))
    settings = tmp_path / ".claude" / "settings.json"
    monkeypatch.setattr("s2s.initcmd.default_settings_path", lambda: settings)

    assert main(["init"]) == 0
    assert settings.is_file()


def test_backfill_dispatch_uses_the_adapter_and_returns_success(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("S2S_HOME", str(tmp_path / "s2s-home"))
    projects = tmp_path / ".claude" / "projects"
    projects.mkdir(parents=True)
    monkeypatch.setattr("s2s.archiver.iter_sessions", lambda base_dir=None: iter(()))

    assert main(["backfill"]) == 0


def test_scan_command_end_to_end_reports_created_incidents(
    monkeypatch: pytest.MonkeyPatch, tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Regression: the scan CLI must survive a NON-empty queue (issue #5 wiring)."""
    from s2s.archiver import archive_transcript
    from s2s.cli import main

    monkeypatch.setenv("S2S_HOME", str(tmp_path / "home"))
    fixture = Path(__file__).parent / "fixtures" / "claude_code" / "synthetic-session.jsonl"
    source = tmp_path / "projects" / "-demo-project" / "session-1.jsonl"
    source.parent.mkdir(parents=True)
    source.write_text(fixture.read_text())
    archive_transcript(source)

    assert main(["scan"]) == 0
    out = capsys.readouterr().out
    assert "scanned 1 transcript(s)" in out
