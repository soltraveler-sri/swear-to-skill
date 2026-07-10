"""Safe installation and removal of swear-to-skill's Claude Code hook."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import sysconfig
import tempfile
from typing import TextIO

from .paths import resolve_paths


HOOK_MARKER = "s2s hook"
SESSION_END_COMMAND = "s2s hook session-end"
SESSION_END_ENTRY = {"type": "command", "command": SESSION_END_COMMAND}
SKILL_NAME = "s2s"
SKILL_VERSION_RE = re.compile(r"^<!-- s2s-skill-version: (\d+) -->$", re.MULTILINE)
SESSION_START_COMMAND = "s2s hook session-start"
SESSION_START_ENTRY = {"type": "command", "command": SESSION_START_COMMAND}


class SettingsError(RuntimeError):
    """Raised when settings cannot be changed without risking user content."""


@dataclass(frozen=True)
class InitResult:
    """Observable changes made by one initialization."""

    settings_changed: bool
    config_created: bool
    skill_changed: bool


def default_settings_path() -> Path:
    """Return Claude Code's user settings path without touching it."""

    return Path.home() / ".claude" / "settings.json"


def default_skill_path(settings_path: Path | None = None) -> Path:
    """Return the companion skill target, keeping injected settings homes isolated."""

    settings = Path(settings_path) if settings_path is not None else default_settings_path()
    return settings.parent / "skills" / SKILL_NAME


def vendored_skill_dir() -> Path:
    """Locate the repository-owned companion skill source."""

    source_tree = Path(__file__).resolve().parents[2] / "skill" / SKILL_NAME
    if source_tree.is_dir():
        return source_tree
    return Path(sysconfig.get_path("data")) / "skill" / SKILL_NAME


def install_companion_skill(
    skill_path: Path | None = None,
    *,
    source_dir: Path | None = None,
) -> bool:
    """Install or upgrade the marker-owned skill without overwriting user content."""

    target = Path(skill_path) if skill_path is not None else default_skill_path()
    if target.name != SKILL_NAME:
        raise SettingsError(f"companion skill target must be named {SKILL_NAME!r}")
    source = Path(source_dir) if source_dir is not None else vendored_skill_dir()
    source_version = _skill_version(source / "SKILL.md", label="vendored")

    if target.exists():
        if not target.is_dir():
            raise SettingsError(f"companion skill target {target} is not a directory")
        installed_version = _skill_version(target / "SKILL.md", label="installed")
        if installed_version >= source_version:
            return False

    _atomic_replace_skill_directory(source, target)
    return True


def remove_companion_skill(skill_path: Path | None = None) -> bool:
    """Remove only an installed skill carrying this project's version marker."""

    target = Path(skill_path) if skill_path is not None else default_skill_path()
    if target.name != SKILL_NAME:
        raise SettingsError(f"companion skill target must be named {SKILL_NAME!r}")
    if not target.exists():
        return False
    if not target.is_dir():
        raise SettingsError(f"companion skill target {target} is not a directory")
    _skill_version(target / "SKILL.md", label="installed")
    shutil.rmtree(target)
    _fsync_directory(target.parent)
    return True


def merge_session_end_hook(settings_path: Path | None = None) -> bool:
    """Add the s2s SessionEnd command while preserving every other setting."""
    return merge_managed_hook(
        settings_path or default_settings_path(),
        event="SessionEnd",
        command=SESSION_END_COMMAND,
        marker=HOOK_MARKER,
    )


def merge_session_start_hook(settings_path: Path | None = None) -> bool:
    """Add the s2s SessionStart digest command through the managed-hook path."""
    return merge_managed_hook(
        settings_path or default_settings_path(),
        event="SessionStart",
        command=SESSION_START_COMMAND,
        marker=HOOK_MARKER,
    )


def remove_session_end_hook(settings_path: Path | None = None) -> bool:
    """Remove only commands carrying the s2s hook marker."""
    return remove_managed_hook(
        settings_path or default_settings_path(), event="SessionEnd", marker=HOOK_MARKER
    )


def remove_session_start_hook(settings_path: Path | None = None) -> bool:
    """Remove only the s2s SessionStart hook."""
    return remove_managed_hook(
        settings_path or default_settings_path(), event="SessionStart", marker=HOOK_MARKER
    )


def merge_managed_hook(
    settings_path: Path,
    *,
    event: str,
    command: str,
    marker: str,
) -> bool:
    """Add one recognizably tagged command hook using init's safe merge path."""

    if not event.strip() or not command.strip() or not marker.strip():
        raise SettingsError("managed hooks require an event, command, and marker")
    path = Path(settings_path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with _settings_update_lock(path):
        document, original = _read_settings(path)
        hooks, groups = _validated_hook_event(document, event)
        if any(
            _entry_has_marker(entry, marker)
            for group in groups
            for entry in group["hooks"]
        ):
            return False
        groups.append({"hooks": [{"type": "command", "command": command}]})
        hooks[event] = groups
        document["hooks"] = hooks
        _atomic_write_settings(path, document, original)
        return True


def remove_managed_hook(settings_path: Path, *, event: str, marker: str) -> bool:
    """Remove exactly hook entries containing ``marker``, preserving all others."""

    path = Path(settings_path)
    if not path.exists():
        return False
    with _settings_update_lock(path):
        document, original = _read_settings(path)
        hooks, groups = _validated_hook_event(document, event)
        changed = False
        retained_groups: list[dict[str, object]] = []
        for group in groups:
            entries = group["hooks"]
            retained_entries = [
                entry for entry in entries if not _entry_has_marker(entry, marker)
            ]
            if len(retained_entries) == len(entries):
                retained_groups.append(group)
                continue
            changed = True
            if retained_entries:
                retained_group = dict(group)
                retained_group["hooks"] = retained_entries
                retained_groups.append(retained_group)
            elif not set(group).issubset({"hooks", "matcher"}):
                retained_group = dict(group)
                retained_group["hooks"] = []
                retained_groups.append(retained_group)
        if not changed:
            return False
        if retained_groups:
            hooks[event] = retained_groups
        else:
            hooks.pop(event, None)
        if hooks:
            document["hooks"] = hooks
        else:
            document.pop("hooks", None)
        _atomic_write_settings(path, document, original)
        return True


def replace_managed_hook(
    settings_path: Path,
    *,
    old_event: str,
    new_event: str,
    command: str,
    marker: str,
) -> None:
    """Replace one tagged hook in a single locked, atomic settings mutation."""

    path = Path(settings_path)
    if not path.exists():
        raise SettingsError(f"managed hook settings file {path} no longer exists")
    with _settings_update_lock(path):
        document, original = _read_settings(path)
        hooks, old_groups = _validated_hook_event(document, old_event)
        found = 0
        retained_groups: list[dict[str, object]] = []
        for group in old_groups:
            entries = group["hooks"]
            retained_entries = []
            for entry in entries:
                if _entry_has_marker(entry, marker):
                    found += 1
                else:
                    retained_entries.append(entry)
            if retained_entries:
                retained_group = dict(group)
                retained_group["hooks"] = retained_entries
                retained_groups.append(retained_group)
            elif not set(group).issubset({"hooks", "matcher"}):
                retained_group = dict(group)
                retained_group["hooks"] = []
                retained_groups.append(retained_group)
        if found != 1:
            raise SettingsError(
                f"expected exactly one managed hook containing {marker!r}; found {found}"
            )
        if retained_groups:
            hooks[old_event] = retained_groups
        else:
            hooks.pop(old_event, None)
        if new_event == old_event:
            new_groups = retained_groups
        else:
            hooks_for_new, new_groups = _validated_hook_event(document, new_event)
            hooks = hooks_for_new
        new_groups.append({"hooks": [{"type": "command", "command": command}]})
        hooks[new_event] = new_groups
        document["hooks"] = hooks
        _atomic_write_settings(path, document, original)


def initialize(
    settings_path: Path | None = None,
    *,
    config_template_path: Path | None = None,
    skill_path: Path | None = None,
    skill_source_dir: Path | None = None,
) -> InitResult:
    """Create private s2s state, install defaults, and register SessionEnd."""

    paths = resolve_paths()
    for directory in (
        paths.home,
        paths.archive_dir,
        paths.state_dir,
        paths.home / "logs",
    ):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)

    config_created = _install_default_config(paths.config_path, config_template_path)
    end_changed = merge_session_end_hook(settings_path)
    start_changed = merge_session_start_hook(settings_path)
    settings_changed = end_changed or start_changed
    resolved_skill_path = (
        Path(skill_path) if skill_path is not None else default_skill_path(settings_path)
    )
    skill_changed = install_companion_skill(resolved_skill_path, source_dir=skill_source_dir)
    return InitResult(
        settings_changed=settings_changed,
        config_created=config_created,
        skill_changed=skill_changed,
    )


def uninstall(settings_path: Path | None = None, *, skill_path: Path | None = None) -> bool:
    """Unregister s2s without deleting archives, the ledger, or user config."""

    resolved_skill_path = (
        Path(skill_path) if skill_path is not None else default_skill_path(settings_path)
    )
    skill_removed = remove_companion_skill(resolved_skill_path)
    end_removed = remove_session_end_hook(settings_path)
    start_removed = remove_session_start_hook(settings_path)
    return end_removed or start_removed or skill_removed


def run_init(
    *,
    uninstall_mode: bool = False,
    settings_path: Path | None = None,
    output: TextIO | None = None,
    error_output: TextIO | None = None,
) -> int:
    """CLI-facing init runner with clear, non-traceback refusal messages."""

    output = sys.stdout if output is None else output
    error_output = sys.stderr if error_output is None else error_output
    try:
        if uninstall_mode:
            changed = uninstall(settings_path)
            print(
                "Removed s2s SessionStart and SessionEnd hooks."
                if changed
                else "s2s SessionStart and SessionEnd hooks not installed.",
                file=output,
            )
        else:
            result = initialize(settings_path)
            print(
                "s2s initialized (hooks + /s2s skill)."
                if result.settings_changed or result.config_created or result.skill_changed
                else "s2s already initialized; no changes made.",
                file=output,
            )
    except SettingsError as error:
        print(f"s2s init refused to modify Claude Code settings: {error}", file=error_output)
        return 1
    except OSError as error:
        print(f"s2s init failed: {error}", file=error_output)
        return 1
    return 0


def _read_settings(path: Path) -> tuple[dict[str, object], bytes | None]:
    if not path.exists():
        return {}, None
    try:
        original = path.read_bytes()
        document = json.loads(original)
    except json.JSONDecodeError as error:
        raise SettingsError(
            f"{path} is not valid JSON ({error.msg} at line {error.lineno}, "
            f"column {error.colno}); file left unchanged"
        ) from error
    except (OSError, UnicodeError) as error:
        raise SettingsError(f"cannot read {path}: {error}; file left unchanged") from error
    if not isinstance(document, dict):
        raise SettingsError(f"{path} must contain a JSON object; file left unchanged")
    return document, original


def _skill_version(path: Path, *, label: str) -> int:
    """Return the one required ownership marker from a skill file."""

    try:
        content = path.read_text(encoding="utf-8")
    except OSError as error:
        raise SettingsError(f"cannot read {label} companion skill {path}: {error}") from error
    matches = SKILL_VERSION_RE.findall(content)
    if len(matches) != 1:
        raise SettingsError(
            f"{label} companion skill {path} lacks exactly one s2s version marker"
        )
    return int(matches[0])


def _atomic_replace_skill_directory(source: Path, target: Path) -> None:
    """Stage a complete skill copy, then swap it while retaining a recovery path."""

    if not source.is_dir():
        raise SettingsError(f"vendored companion skill directory {source} is missing")
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    backup: Path | None = None
    with tempfile.TemporaryDirectory(prefix=f".{SKILL_NAME}.stage-", dir=target.parent) as raw_stage:
        staged = Path(raw_stage) / SKILL_NAME
        try:
            shutil.copytree(source, staged)
        except OSError as error:
            raise SettingsError(f"cannot stage companion skill from {source}: {error}") from error
        _skill_version(staged / "SKILL.md", label="staged")

        if target.exists():
            descriptor, raw_backup = tempfile.mkstemp(
                prefix=f".{SKILL_NAME}.backup-", dir=target.parent
            )
            os.close(descriptor)
            backup = Path(raw_backup)
            backup.unlink()
            os.replace(target, backup)
        try:
            os.replace(staged, target)
        except OSError as error:
            if backup is not None:
                os.replace(backup, target)
            raise SettingsError(f"cannot install companion skill at {target}: {error}") from error
        else:
            if backup is not None:
                shutil.rmtree(backup)
            _fsync_directory(target.parent)


def _validated_session_end(
    document: dict[str, object],
) -> tuple[dict[str, object], list[dict[str, object]]]:
    raw_hooks = document.get("hooks")
    if raw_hooks is None:
        hooks: dict[str, object] = {}
    elif isinstance(raw_hooks, dict):
        hooks = raw_hooks
    else:
        raise SettingsError("settings key 'hooks' must be a JSON object; file left unchanged")

    raw_session_end = hooks.get("SessionEnd")
    if raw_session_end is None:
        return hooks, []
    if not isinstance(raw_session_end, list):
        raise SettingsError(
            "settings key 'hooks.SessionEnd' must be a JSON array; file left unchanged"
        )

    groups: list[dict[str, object]] = []
    for group_index, raw_group in enumerate(raw_session_end):
        if not isinstance(raw_group, dict):
            raise SettingsError(
                f"hooks.SessionEnd[{group_index}] must be a JSON object; file left unchanged"
            )
        raw_entries = raw_group.get("hooks")
        if not isinstance(raw_entries, list):
            raise SettingsError(
                f"hooks.SessionEnd[{group_index}].hooks must be a JSON array; "
                "file left unchanged"
            )
        entries: list[dict[str, object]] = []
        for entry_index, entry in enumerate(raw_entries):
            if not isinstance(entry, dict):
                raise SettingsError(
                    f"hooks.SessionEnd[{group_index}].hooks[{entry_index}] must be "
                    "a JSON object; file left unchanged"
                )
            entries.append(entry)
        raw_group["hooks"] = entries
        groups.append(raw_group)

    return hooks, groups


def _validated_hook_event(
    document: dict[str, object], event: str
) -> tuple[dict[str, object], list[dict[str, object]]]:
    """Validate one arbitrary hook event through the same strict shape checks."""

    raw_hooks = document.get("hooks")
    if raw_hooks is None:
        hooks: dict[str, object] = {}
    elif isinstance(raw_hooks, dict):
        hooks = raw_hooks
    else:
        raise SettingsError("settings key 'hooks' must be a JSON object; file left unchanged")
    raw_groups = hooks.get(event)
    if raw_groups is None:
        return hooks, []
    if not isinstance(raw_groups, list):
        raise SettingsError(
            f"settings key 'hooks.{event}' must be a JSON array; file left unchanged"
        )
    groups: list[dict[str, object]] = []
    for group_index, raw_group in enumerate(raw_groups):
        if not isinstance(raw_group, dict):
            raise SettingsError(
                f"hooks.{event}[{group_index}] must be a JSON object; file left unchanged"
            )
        raw_entries = raw_group.get("hooks")
        if not isinstance(raw_entries, list):
            raise SettingsError(
                f"hooks.{event}[{group_index}].hooks must be a JSON array; file left unchanged"
            )
        entries: list[dict[str, object]] = []
        for entry_index, entry in enumerate(raw_entries):
            if not isinstance(entry, dict):
                raise SettingsError(
                    f"hooks.{event}[{group_index}].hooks[{entry_index}] must be "
                    "a JSON object; file left unchanged"
                )
            entries.append(entry)
        raw_group["hooks"] = entries
        groups.append(raw_group)
    return hooks, groups


def _is_s2s_hook(entry: dict[str, object]) -> bool:
    command = entry.get("command")
    return isinstance(command, str) and HOOK_MARKER in command


def _entry_has_marker(entry: dict[str, object], marker: str) -> bool:
    command = entry.get("command")
    return isinstance(command, str) and marker in command


def atomic_write_bytes(path: Path, content: bytes, original: bytes | None) -> None:
    """Atomically replace a user text file with race checks and first-touch backup."""

    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, raw_temp_path = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temp_path = Path(raw_temp_path)
    try:
        if original is not None and path.exists():
            os.fchmod(descriptor, stat.S_IMODE(path.stat().st_mode))
        output = os.fdopen(descriptor, "wb")
        descriptor = -1
        with output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        if _current_bytes(path) != original:
            raise SettingsError(f"{path} changed concurrently; retry; file left unchanged")
        if original is not None:
            _create_first_backup(path, original)
        if _current_bytes(path) != original:
            raise SettingsError(f"{path} changed concurrently; retry; file left unchanged")
        os.replace(temp_path, path)
        _fsync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temp_path.unlink(missing_ok=True)


def _atomic_write_settings(
    path: Path,
    document: dict[str, object],
    original: bytes | None,
) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    rendered = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    descriptor, raw_temp_path = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temp_path = Path(raw_temp_path)
    try:
        if original is not None:
            try:
                existing_mode = stat.S_IMODE(path.stat().st_mode)
            except OSError as error:
                raise SettingsError(f"cannot stat {path}: {error}; file left unchanged") from error
            os.fchmod(descriptor, existing_mode)
        settings_file = os.fdopen(descriptor, "wb")
        descriptor = -1
        with settings_file:
            settings_file.write(rendered)
            settings_file.flush()
            os.fsync(settings_file.fileno())

        if _current_bytes(path) != original:
            raise SettingsError(f"{path} changed during initialization; retry; file left unchanged")
        if original is not None:
            _create_first_backup(path, original)
        if _current_bytes(path) != original:
            raise SettingsError(f"{path} changed during initialization; retry; file left unchanged")

        os.replace(temp_path, path)
        _fsync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temp_path.unlink(missing_ok=True)


def _current_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise SettingsError(f"cannot re-read {path}: {error}; file left unchanged") from error


def _create_first_backup(path: Path, original: bytes) -> None:
    backup_prefix = f"{path.name}.s2s-backup-"
    if any(path.parent.glob(f"{backup_prefix}*")):
        return

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_path = path.with_name(f"{backup_prefix}{timestamp}")
    try:
        descriptor = os.open(backup_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as backup_file:
            backup_file.write(original)
            backup_file.flush()
            os.fsync(backup_file.fileno())
        _fsync_directory(path.parent)
    except OSError as error:
        backup_path.unlink(missing_ok=True)
        raise SettingsError(f"cannot create settings backup {backup_path}: {error}") from error


def _install_default_config(destination: Path, template_path: Path | None) -> bool:
    if destination.exists():
        return False

    source = Path(template_path) if template_path is not None else _default_config_template()
    try:
        content = source.read_bytes()
    except OSError as error:
        raise OSError(f"cannot read default config template {source}: {error}") from error

    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, raw_temp_path = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    temp_path = Path(raw_temp_path)
    try:
        with os.fdopen(descriptor, "wb") as config_file:
            config_file.write(content)
            config_file.flush()
            os.fsync(config_file.fileno())
        try:
            os.link(temp_path, destination)
        except FileExistsError:
            return False
        _fsync_directory(destination.parent)
        return True
    finally:
        temp_path.unlink(missing_ok=True)


def _default_config_template() -> Path:
    source_tree_template = Path(__file__).resolve().parents[2] / "config.example.toml"
    if source_tree_template.is_file():
        return source_tree_template
    return Path(__file__).with_name("config.example.toml")


@contextmanager
def _settings_update_lock(path: Path) -> Iterator[None]:
    """Serialize s2s writers without leaving a lock file in ~/.claude."""

    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)
