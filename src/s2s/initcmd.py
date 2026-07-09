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
import stat
import sys
import tempfile
from typing import TextIO

from .paths import resolve_paths


HOOK_MARKER = "s2s hook"
SESSION_END_COMMAND = "s2s hook session-end"
SESSION_END_ENTRY = {"type": "command", "command": SESSION_END_COMMAND}


class SettingsError(RuntimeError):
    """Raised when settings cannot be changed without risking user content."""


@dataclass(frozen=True)
class InitResult:
    """Observable changes made by one initialization."""

    settings_changed: bool
    config_created: bool


def default_settings_path() -> Path:
    """Return Claude Code's user settings path without touching it."""

    return Path.home() / ".claude" / "settings.json"


def merge_session_end_hook(settings_path: Path | None = None) -> bool:
    """Add the s2s SessionEnd command while preserving every other setting."""

    path = Path(settings_path) if settings_path is not None else default_settings_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with _settings_update_lock(path):
        document, original = _read_settings(path)
        hooks, session_end = _validated_session_end(document)

        if any(_is_s2s_hook(entry) for group in session_end for entry in group["hooks"]):
            return False

        session_end.append({"hooks": [dict(SESSION_END_ENTRY)]})
        hooks["SessionEnd"] = session_end
        document["hooks"] = hooks
        _atomic_write_settings(path, document, original)
        return True


def remove_session_end_hook(settings_path: Path | None = None) -> bool:
    """Remove only commands carrying the s2s hook marker."""

    path = Path(settings_path) if settings_path is not None else default_settings_path()
    if not path.exists():
        return False

    with _settings_update_lock(path):
        if not path.exists():
            return False
        document, original = _read_settings(path)
        hooks, session_end = _validated_session_end(document)
        if not session_end:
            return False

        changed = False
        retained_groups: list[dict[str, object]] = []
        for group in session_end:
            entries = group["hooks"]
            retained_entries = [entry for entry in entries if not _is_s2s_hook(entry)]
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
            hooks["SessionEnd"] = retained_groups
        else:
            hooks.pop("SessionEnd", None)
        if hooks:
            document["hooks"] = hooks
        else:
            document.pop("hooks", None)

        _atomic_write_settings(path, document, original)
        return True


def initialize(
    settings_path: Path | None = None,
    *,
    config_template_path: Path | None = None,
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
    settings_changed = merge_session_end_hook(settings_path)
    return InitResult(settings_changed=settings_changed, config_created=config_created)


def uninstall(settings_path: Path | None = None) -> bool:
    """Unregister s2s without deleting archives, the ledger, or user config."""

    return remove_session_end_hook(settings_path)


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
                "Removed s2s SessionEnd hook." if changed else "s2s SessionEnd hook not installed.",
                file=output,
            )
        else:
            result = initialize(settings_path)
            print(
                "s2s initialized."
                if result.settings_changed or result.config_created
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


def _is_s2s_hook(entry: dict[str, object]) -> bool:
    command = entry.get("command")
    return isinstance(command, str) and HOOK_MARKER in command


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
