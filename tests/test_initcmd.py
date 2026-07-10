from __future__ import annotations

import json

import pytest

from s2s import initcmd
from s2s.initcmd import (
    SESSION_END_COMMAND,
    SKILL_VERSION_RE,
    initialize,
    merge_session_end_hook,
    run_init,
    uninstall,
)


def _write_settings(path, document) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    original = json.dumps(document, separators=(",", ":"), ensure_ascii=False).encode()
    path.write_bytes(original)
    return original


def test_settings_merge_preserves_existing_user_hooks_and_creates_one_backup(tmp_path) -> None:
    settings = tmp_path / ".claude" / "settings.json"
    user_hook = {"type": "command", "command": "notify-session-end"}
    document = {
        "theme": "dark",
        "hooks": {
            "PreToolUse": [{"matcher": "Bash", "hooks": [user_hook]}],
            "SessionEnd": [{"matcher": "clear", "hooks": [user_hook]}],
        },
    }
    original = _write_settings(settings, document)

    assert merge_session_end_hook(settings) is True

    merged = json.loads(settings.read_text())
    assert merged["theme"] == "dark"
    assert merged["hooks"]["PreToolUse"] == document["hooks"]["PreToolUse"]
    assert merged["hooks"]["SessionEnd"][0] == document["hooks"]["SessionEnd"][0]
    assert merged["hooks"]["SessionEnd"][-1] == {
        "hooks": [{"type": "command", "command": SESSION_END_COMMAND}]
    }
    backups = list(settings.parent.glob("settings.json.s2s-backup-*"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == original


def test_init_is_byte_idempotent_and_installs_default_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    s2s_home = tmp_path / "s2s-home"
    settings = tmp_path / ".claude" / "settings.json"
    monkeypatch.setenv("S2S_HOME", str(s2s_home))

    first = initialize(settings)
    settings_after_first = settings.read_bytes()
    config_after_first = (s2s_home / "config.toml").read_bytes()
    second = initialize(settings)

    assert first.settings_changed is True
    assert first.config_created is True
    assert second.settings_changed is False
    assert second.config_created is False
    assert settings.read_bytes() == settings_after_first
    assert (s2s_home / "config.toml").read_bytes() == config_after_first
    assert (s2s_home / "archive").is_dir()
    assert (s2s_home / "state").is_dir()
    assert (s2s_home / "logs").is_dir()
    companion = settings.parent / "skills" / "s2s" / "SKILL.md"
    assert companion.is_file()
    assert "name: s2s" in companion.read_text()


def test_companion_skill_install_upgrade_and_uninstall_round_trip(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("S2S_HOME", str(tmp_path / "s2s-home"))
    settings = tmp_path / ".claude" / "settings.json"
    skill = settings.parent / "skills" / "s2s"

    first = initialize(settings)
    installed = skill / "SKILL.md"
    assert first.skill_changed is True
    assert int(SKILL_VERSION_RE.search(installed.read_text()).group(1)) == 1

    installed.write_text(installed.read_text().replace("<!-- s2s-skill-version: 1 -->", "<!-- s2s-skill-version: 0 -->"))
    upgraded = initialize(settings)
    assert upgraded.skill_changed is True
    assert int(SKILL_VERSION_RE.search(installed.read_text()).group(1)) == 1

    assert uninstall(settings) is True
    assert not skill.exists()
    assert uninstall(settings) is False


def test_companion_skill_refuses_to_replace_or_remove_unmarked_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("S2S_HOME", str(tmp_path / "s2s-home"))
    settings = tmp_path / ".claude" / "settings.json"
    skill = settings.parent / "skills" / "s2s"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: s2s\n---\nuser-owned\n")

    with pytest.raises(initcmd.SettingsError, match="version marker"):
        initialize(settings)
    with pytest.raises(initcmd.SettingsError, match="version marker"):
        uninstall(settings)


def test_uninstall_removes_only_s2s_entries(tmp_path) -> None:
    settings = tmp_path / ".claude" / "settings.json"
    user_hook = {"type": "command", "command": "keep-me"}
    document = {
        "permissions": {"allow": ["Read"]},
        "hooks": {
            "SessionEnd": [
                {"matcher": "clear", "hooks": [user_hook]},
                {
                    "hooks": [
                        {"type": "command", "command": SESSION_END_COMMAND},
                        {"type": "command", "command": "also-keep-me"},
                    ]
                },
            ],
            "Stop": [{"hooks": [user_hook]}],
        },
    }
    _write_settings(settings, document)

    assert uninstall(settings) is True

    remaining = json.loads(settings.read_text())
    assert remaining["permissions"] == document["permissions"]
    assert remaining["hooks"]["Stop"] == document["hooks"]["Stop"]
    assert remaining["hooks"]["SessionEnd"] == [
        {"matcher": "clear", "hooks": [user_hook]},
        {"hooks": [{"type": "command", "command": "also-keep-me"}]},
    ]


def test_uninstall_prunes_structures_created_by_init(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("S2S_HOME", str(tmp_path / "s2s-home"))
    settings = tmp_path / ".claude" / "settings.json"
    initialize(settings)

    assert uninstall(settings) is True
    assert json.loads(settings.read_text()) == {}
    assert uninstall(settings) is False


def test_uninstall_prunes_an_empty_s2s_matcher_group(tmp_path) -> None:
    settings = tmp_path / ".claude" / "settings.json"
    _write_settings(
        settings,
        {
            "hooks": {
                "SessionEnd": [
                    {
                        "matcher": "",
                        "hooks": [{"type": "command", "command": SESSION_END_COMMAND}],
                    }
                ]
            }
        },
    )

    assert uninstall(settings) is True
    assert json.loads(settings.read_text()) == {}


def test_unparseable_settings_are_refused_and_left_byte_identical(
    monkeypatch: pytest.MonkeyPatch, tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("S2S_HOME", str(tmp_path / "s2s-home"))
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    original = b'{"hooks": broken\n'
    settings.write_bytes(original)

    assert run_init(settings_path=settings) == 1

    assert settings.read_bytes() == original
    assert not list(settings.parent.glob("settings.json.s2s-backup-*"))
    assert "not valid JSON" in capsys.readouterr().err


def test_invalid_hook_shape_is_refused_instead_of_clobbered(tmp_path) -> None:
    settings = tmp_path / ".claude" / "settings.json"
    original = _write_settings(settings, {"hooks": {"SessionEnd": "future-shape"}})

    assert run_init(settings_path=settings) == 1
    assert settings.read_bytes() == original


def test_atomic_replace_failure_leaves_original_settings_untouched(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    settings = tmp_path / ".claude" / "settings.json"
    original = _write_settings(settings, {"theme": "dark"})

    def fail_replace(source, destination):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(initcmd.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated replace failure"):
        merge_session_end_hook(settings)

    assert settings.read_bytes() == original
    assert not list(settings.parent.glob(".settings.json.*.tmp"))
