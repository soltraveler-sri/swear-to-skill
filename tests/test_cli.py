from __future__ import annotations

from pathlib import Path

import pytest

from s2s.cli import COMMAND_ISSUES, main


@pytest.mark.parametrize(
    "command, issue",
    [(c, i) for c, i in COMMAND_ISSUES.items() if c not in {"scan", "meter", "status"}],
)
def test_each_stub_exits_successfully(command: str, issue: int, capsys: pytest.CaptureFixture[str]) -> None:
    assert main([command]) == 0
    assert capsys.readouterr().out == f"{command}: not implemented yet (issue #{issue})\n"


def test_stub_commands_accept_their_documented_scaffold_options(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["schedule", "install"]) == 0
    assert main(["autonomy", "on"]) == 0
    assert main(["approve", "proposal-1"]) == 0
    assert main(["pump", "--background"]) == 0
    assert "not implemented yet" in capsys.readouterr().out


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
