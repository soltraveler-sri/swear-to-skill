from __future__ import annotations

from pathlib import Path

import pytest

from s2s.cli import main
from s2s.doctor import build_live_probe_argv, render_report, run_doctor
from s2s.gate import GateTargets


def _targets(tmp_path: Path) -> GateTargets:
    claude = tmp_path / ".claude"
    return GateTargets(
        skills_dir=claude / "skills",
        global_claude_md=claude / "CLAUDE.md",
        settings_path=claude / "settings.json",
        state_dir=tmp_path / "s2s-home",
    )


def _skill(targets: GateTargets, name: str = "s2s-check") -> Path:
    path = targets.skills_dir / name / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        "---\n"
        "# s2s:managed remedy=7\n"
        f"name: {name}\n"
        "description: Check a generated remedy.\n"
        "---\n"
        "Use this skill.\n",
        encoding="utf-8",
    )
    return path


def test_doctor_healthy_fake_home_and_cli_exit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys) -> None:
    targets = _targets(tmp_path)
    _skill(targets)
    targets.global_claude_md.parent.mkdir(parents=True, exist_ok=True)
    targets.global_claude_md.write_text(
        "<!-- s2s:begin remedy=4 -->\nRule\n<!-- s2s:end remedy=4 -->\n", encoding="utf-8"
    )

    report = run_doctor(targets=targets)
    assert report.healthy and report.exit_code == 0
    assert "HEALTHY" in render_report(report)
    monkeypatch.setattr("s2s.doctor.default_targets", lambda: targets)
    assert main(["doctor"]) == 0
    assert "PASS skill layout" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda path: path.unlink(), "missing SKILL.md"),
        (lambda path: path.write_text("---\nname: s2s-check\n---\nbody\n"), "frontmatter"),
        (lambda path: path.write_text(path.read_text().replace("# s2s:managed remedy=7\n", "")), "managed marker"),
    ],
)
def test_doctor_reports_broken_skill_variants(tmp_path: Path, mutate, expected: str) -> None:
    targets = _targets(tmp_path)
    path = _skill(targets)
    mutate(path)

    report = run_doctor(targets=targets)
    assert not report.healthy and report.exit_code == 1
    assert expected in render_report(report)


def test_doctor_flags_nested_and_renamed_skill_paths(tmp_path: Path) -> None:
    targets = _targets(tmp_path)
    path = _skill(targets)
    nested = path.parent / "nested" / "SKILL.md"
    nested.parent.mkdir()
    nested.write_text(path.read_text(), encoding="utf-8")
    (path.parent / "RENAMED.md").write_text("oops", encoding="utf-8")

    report = run_doctor(targets=targets)
    assert not report.healthy
    assert "nested SKILL.md" in render_report(report)
    assert "renamed markdown" in render_report(report)


def test_doctor_flags_unpaired_claude_markers_and_duplicate_names(tmp_path: Path) -> None:
    targets = _targets(tmp_path)
    _skill(targets, "s2s-one")
    duplicate = _skill(targets, "s2s-two")
    duplicate.write_text(duplicate.read_text().replace("name: s2s-two", "name: s2s-one"), encoding="utf-8")
    targets.global_claude_md.parent.mkdir(parents=True, exist_ok=True)
    targets.global_claude_md.write_text("<!-- s2s:begin remedy=9 -->\n", encoding="utf-8")

    report = run_doctor(targets=targets)
    text = render_report(report)
    assert not report.healthy
    assert "duplicate skill names" in text
    assert "unpaired or duplicated" in text


def test_live_probe_uses_one_call_and_keeps_skills_visible(mock_claude, tmp_path: Path) -> None:
    targets = _targets(tmp_path)
    _skill(targets)
    mock_claude.enqueue_response(
        {
            "result": {"skills": ["s2s-check"]},
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "total_cost_usd": 0,
        }
    )

    report = run_doctor(targets=targets, live=True)
    assert report.healthy
    assert len(mock_claude.invocations()) == 1
    argv = mock_claude.invocations()[0]["argv"]
    assert "--disable-slash-commands" not in argv
    assert "--no-session-persistence" in argv
    assert "found: s2s-check; missing: none" in render_report(report)
    assert "--disable-slash-commands" not in build_live_probe_argv({"type": "object"})


def test_live_probe_reports_missing_skill(tmp_path: Path, mock_claude) -> None:
    targets = _targets(tmp_path)
    _skill(targets)
    mock_claude.enqueue_response({"result": {"skills": []}, "usage": {}, "total_cost_usd": 0})

    report = run_doctor(targets=targets, live=True)
    assert report.exit_code == 1
    assert "missing: s2s-check" in render_report(report)
