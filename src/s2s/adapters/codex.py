"""Defensive Codex transcript adapter.

Codex has no SessionEnd hook, so its logs are discovered by ``backfill`` and the
periodic pump.  ``history.jsonl`` is the cheap detection index; rollouts are read
only when context or session metadata is needed.  Both formats are undocumented,
therefore every reader is deliberately tolerant of bad and unknown JSONL lines.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import logging
from pathlib import Path
import sqlite3
from typing import Any, Iterator

from .claude_code import ContextPack, ContextPackMetadata, SessionMetadata, UserMessage


LOGGER = logging.getLogger(__name__)

# Generated prompt scaffolding, not human-authored requests.  Keep this explicit:
# a new Codex wrapper should be added here only after it is observed and reviewed.
SCAFFOLD_PREFIXES = (
    "# AGENTS.md instructions",
    "<INSTRUCTIONS>",
    "<environment_context>",
    "<permissions instructions>",
    "<skill>",
    "<turn_aborted>",
    "Automation:",
)


@dataclass(frozen=True)
class _Line:
    data: dict[str, Any]
    position: int


def codex_home(root: Path | None = None) -> Path:
    """Return an injectable Codex root without creating it."""

    return Path(root) if root is not None else Path.home() / ".codex"


def history_path(root: Path | None = None) -> Path:
    return codex_home(root) / "history.jsonl"


def sessions_dir(root: Path | None = None) -> Path:
    return codex_home(root) / "sessions"


def iter_sessions(root: Path | None = None) -> Iterator[Path]:
    """Yield live and archived rollout files in deterministic order."""

    home = codex_home(root)
    candidates: list[Path] = []
    for directory in (home / "sessions", home / "archived_sessions"):
        if directory.is_dir():
            candidates.extend(path for path in directory.rglob("*.jsonl") if path.is_file())
    yield from sorted(set(candidates))


def iter_archived_sessions(base_dir: Path | None = None) -> Iterator[Path]:
    root = Path(base_dir) if base_dir is not None else codex_home() / "archive"
    if root.is_dir():
        yield from sorted(path for path in root.rglob("*.jsonl") if path.is_file())


def extract_user_messages(path: Path, *, root: Path | None = None) -> list[UserMessage]:
    """Extract direct requests from a history index or a full Codex rollout."""

    source = Path(path)
    if source.name == "history.jsonl":
        return _history_messages(source)
    return _rollout_messages(source)


def extract_session_metadata(path: Path, *, root: Path | None = None) -> SessionMetadata:
    """Return rollout aggregates, looking up model metadata opportunistically."""

    rollout = Path(path)
    malformed = [0]
    lines = list(_iter_json_lines(rollout, malformed))
    meta = _session_meta(lines)
    session_id = _string(meta.get("thread_id")) or _rollout_id(rollout)
    cwd = _string(meta.get("cwd"))
    project = _project(cwd)
    messages = _rollout_messages_from_lines(lines, rollout, meta)
    timestamps = [timestamp for timestamp in (_timestamp(line.data) for line in lines) if timestamp]
    first, last = _timestamp_bounds(timestamps)
    assistant_count = sum(1 for line in lines if _is_assistant_record(line.data))
    user_count = sum(1 for line in lines if _is_user_record(line.data))
    return SessionMetadata(
        session_id=session_id,
        project=project,
        dominant_model=_model_for_rollout(rollout, codex_home(root)),
        direct_message_count=len(messages),
        user_message_count=user_count,
        assistant_message_count=assistant_count,
        first_timestamp=first,
        last_timestamp=last,
        duration_seconds=_duration_seconds(first, last),
        malformed_line_count=malformed[0],
    )


def session_metadata(path: Path, *, root: Path | None = None) -> SessionMetadata:
    return extract_session_metadata(path, root=root)


def build_context_pack(path: Path, target_uuid: str, max_chars: int = 16_000) -> ContextPack:
    """Build the standard four-section context pack from a Codex rollout."""

    rollout = Path(path)
    lines = list(_iter_json_lines(rollout))
    meta = _session_meta(lines)
    session_id = _string(meta.get("thread_id")) or _rollout_id(rollout)
    cwd = _string(meta.get("cwd"))
    project = _project(cwd)
    messages = _rollout_messages_from_lines(lines, rollout, meta)
    target = next((message for message in messages if message.uuid == target_uuid), None)
    metadata = ContextPackMetadata(
        session_id=session_id,
        project=project,
        cwd=cwd,
        git_branch=None,
        timestamp=target.timestamp if target is not None else None,
    )
    if target is None:
        LOGGER.warning("Codex context target %s was not found in %s", target_uuid, rollout)
        return ContextPack("", "", "", "", metadata)

    target_position = int(target.uuid.rsplit(":", 1)[-1])
    preceding = [message for message in messages if int(message.uuid.rsplit(":", 1)[-1]) < target_position]
    preceding_request = preceding[-1].message if preceding else ""
    activity = _activity_digest(
        [line for line in lines if _between(line.position, preceding[-1].uuid if preceding else None, target.uuid)]
    )
    following = _following_exchange(lines, target_position, rollout, meta)
    bounded = _truncate_oldest_first((preceding_request, activity, target.message, following), max_chars)
    return ContextPack(*bounded, metadata)


def find_rollout(session_id: str, root: Path | None = None) -> Path | None:
    """Locate a rollout by its thread id without trusting filename conventions."""

    for rollout in iter_sessions(root):
        lines = list(_iter_json_lines(rollout))
        if _string(_session_meta(lines).get("thread_id")) == session_id:
            return rollout
    return None


def find_message_uuid(path: Path, *, session_id: str, message: str, occurred_at: str) -> str | None:
    """Resolve a durable scanner row back to its rollout-local message id."""

    messages = _rollout_messages(Path(path))
    exact = [item for item in messages if item.session_id == session_id and item.message == message]
    if occurred_at:
        timestamp_match = next((item for item in exact if item.timestamp == occurred_at), None)
        if timestamp_match is not None:
            return timestamp_match.uuid
    return exact[0].uuid if exact else None


def _history_messages(path: Path) -> list[UserMessage]:
    messages: list[UserMessage] = []
    seen: set[tuple[str, str | None, str]] = set()
    for line in _iter_json_lines(path):
        session_id = _string(line.data.get("session_id"))
        text = _string(line.data.get("text"))
        if not session_id or not text or _is_scaffold(text):
            continue
        timestamp = _string(line.data.get("ts"))
        key = (session_id, timestamp, text)
        if key in seen:
            continue
        seen.add(key)
        messages.append(UserMessage(text, f"{session_id}:{line.position}", timestamp, session_id, "codex", None, None, None))
    return messages


def _rollout_messages(path: Path) -> list[UserMessage]:
    lines = list(_iter_json_lines(path))
    # Rollouts are tool-event heavy.  This cheap raw-line test avoids decoding
    # unrelated records when callers need only scanner candidates; context and
    # metadata deliberately retain the complete parse above.
    candidates = list(_iter_message_candidate_lines(path))
    return _rollout_messages_from_lines(candidates, path, _session_meta(lines))


def _rollout_messages_from_lines(lines: list[_Line], path: Path, meta: dict[str, Any]) -> list[UserMessage]:
    if _is_subagent(meta.get("source")):
        return []
    session_id = _string(meta.get("thread_id")) or _rollout_id(path)
    cwd = _string(meta.get("cwd"))
    project = _project(cwd)
    primary: list[UserMessage] = []
    fallback: list[UserMessage] = []
    for line in lines:
        text = _event_user_text(line.data)
        if text is not None and not _is_scaffold(text):
            primary.append(UserMessage(text, f"{session_id}:{line.position}", _timestamp(line.data), session_id, project, cwd, None, None))
            continue
        text = _response_user_text(line.data)
        if text is not None and not _is_scaffold(text):
            fallback.append(UserMessage(text, f"{session_id}:{line.position}", _timestamp(line.data), session_id, project, cwd, None, None))
    return _dedupe(primary if primary else fallback)


def _iter_json_lines(path: Path, malformed: list[int] | None = None) -> Iterator[_Line]:
    try:
        with Path(path).open(encoding="utf-8") as handle:
            for position, raw in enumerate(handle):
                if not raw.strip():
                    continue
                # Most rollout lines lack user messages. Avoid JSON decoding in
                # the history fast path only after this safe substring test.
                try:
                    value = json.loads(raw)
                except json.JSONDecodeError:
                    if malformed is not None:
                        malformed[0] += 1
                    LOGGER.warning("Skipping malformed Codex JSONL line %s:%d", path, position + 1)
                    continue
                if isinstance(value, dict):
                    yield _Line(value, position)
    except OSError as error:
        LOGGER.warning("Unable to read Codex transcript %s: %s", path, error)


def _iter_message_candidate_lines(path: Path) -> Iterator[_Line]:
    """Decode only lines which can carry either supported user-message shape."""

    try:
        with Path(path).open(encoding="utf-8") as handle:
            for position, raw in enumerate(handle):
                if '"user_message"' not in raw and '"response_item"' not in raw:
                    continue
                try:
                    value = json.loads(raw)
                except json.JSONDecodeError:
                    LOGGER.warning("Skipping malformed Codex JSONL line %s:%d", path, position + 1)
                    continue
                if isinstance(value, dict):
                    yield _Line(value, position)
    except OSError as error:
        LOGGER.warning("Unable to read Codex transcript %s: %s", path, error)


def _event_user_text(data: dict[str, Any]) -> str | None:
    if data.get("type") != "event_msg":
        return None
    payload = _mapping(data.get("payload"))
    if payload is None or payload.get("type") != "user_message":
        return None
    return _text(payload.get("message"))


def _response_user_text(data: dict[str, Any]) -> str | None:
    if data.get("type") != "response_item":
        return None
    payload = _mapping(data.get("payload"))
    if payload is None or payload.get("type") != "message" or payload.get("role") != "user":
        return None
    return _text(payload.get("content"))


def _following_exchange(lines: list[_Line], target_position: int, path: Path, meta: dict[str, Any]) -> str:
    messages = _rollout_messages_from_lines(lines, path, meta)
    rendered: list[str] = []
    for line in lines:
        if line.position <= target_position:
            continue
        user = next((item for item in messages if item.uuid and item.uuid.endswith(f":{line.position}")), None)
        if user is not None:
            rendered.append(f"User: {user.message}")
            continue
        activity = _activity_line(line.data)
        if activity:
            rendered.append(activity)
        elif _is_assistant_record(line.data):
            text = _assistant_text(line.data)
            if text:
                rendered.append(f"Assistant: {text}")
    return "\n".join(rendered)


def _activity_digest(lines: list[_Line]) -> str:
    return "\n".join(line for parsed in lines if (line := _activity_line(parsed.data)))


def _activity_line(data: dict[str, Any]) -> str:
    payload = _mapping(data.get("payload")) or data
    kind = _string(payload.get("type")) or _string(data.get("type")) or ""
    if kind in {"function_call", "tool_call", "custom_tool_call", "function_call_output"}:
        name = _string(payload.get("name")) or _string(payload.get("call_id")) or "tool"
        return f"Tool: {name}"
    return ""


def _assistant_text(data: dict[str, Any]) -> str:
    payload = _mapping(data.get("payload"))
    if payload is None:
        return ""
    if payload.get("role") != "assistant":
        return ""
    return _text(payload.get("content")) or ""


def _is_assistant_record(data: dict[str, Any]) -> bool:
    payload = _mapping(data.get("payload"))
    return bool(payload and payload.get("role") == "assistant")


def _is_user_record(data: dict[str, Any]) -> bool:
    return _event_user_text(data) is not None or _response_user_text(data) is not None


def _session_meta(lines: list[_Line]) -> dict[str, Any]:
    for line in lines:
        if line.data.get("type") == "session_meta":
            payload = _mapping(line.data.get("payload"))
            return payload if payload is not None else line.data
    return {}


def _model_for_rollout(rollout: Path, root: Path) -> str:
    database = root / "state_5.sqlite"
    if not database.is_file():
        return "unknown"
    try:
        with sqlite3.connect(database) as connection:
            rows = connection.execute("SELECT rollout_path, model FROM threads").fetchall()
    except (sqlite3.DatabaseError, OSError):
        return "unknown"
    rollout_text = str(rollout)
    for saved_path, model in rows:
        if isinstance(saved_path, str) and (saved_path == rollout_text or Path(saved_path).name == rollout.name):
            return model if isinstance(model, str) and model else "unknown"
    return "unknown"


def _is_subagent(value: object) -> bool:
    if isinstance(value, dict):
        return any("subagent" in str(key).casefold() or _is_subagent(item) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return any(_is_subagent(item) for item in value)
    return isinstance(value, str) and "subagent" in value.casefold()


def _text(value: object) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return _text(value.get("text")) or _text(value.get("content"))
    if isinstance(value, list):
        parts = [_text(item) for item in value]
        joined = "\n".join(part for part in parts if part)
        return joined or None
    return None


def _mapping(value: object) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None


def _string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _timestamp(data: dict[str, Any]) -> str | None:
    return _string(data.get("timestamp")) or _string(data.get("ts"))


def _project(cwd: str | None) -> str:
    return Path(cwd).name if cwd else "codex"


def _rollout_id(path: Path) -> str:
    return path.stem.removeprefix("rollout-")


def _is_scaffold(text: str) -> bool:
    return text.lstrip().startswith(SCAFFOLD_PREFIXES)


def _dedupe(messages: list[UserMessage]) -> list[UserMessage]:
    result: list[UserMessage] = []
    seen: set[tuple[str, str | None, str]] = set()
    for message in messages:
        key = (message.session_id, message.timestamp, message.message)
        if key not in seen:
            seen.add(key)
            result.append(message)
    return result


def _between(position: int, previous_uuid: str | None, target_uuid: str) -> bool:
    start = int(previous_uuid.rsplit(":", 1)[-1]) if previous_uuid else -1
    end = int(target_uuid.rsplit(":", 1)[-1])
    return start < position < end


def _truncate_oldest_first(sections: tuple[str, str, str, str], max_chars: int) -> tuple[str, str, str, str]:
    remaining = max(0, max_chars)
    result = list(sections)
    total = sum(len(item) for item in result)
    for index in (0, 1, 3, 2):
        if total <= remaining:
            break
        remove = min(len(result[index]), total - remaining)
        result[index] = result[index][remove:]
        total -= remove
    return tuple(result)  # type: ignore[return-value]


def _timestamp_bounds(values: list[str]) -> tuple[str | None, str | None]:
    return (min(values), max(values)) if values else (None, None)


def _duration_seconds(first: str | None, last: str | None) -> float | None:
    if not first or not last:
        return None
    try:
        return max(0.0, (datetime.fromisoformat(last.replace("Z", "+00:00")) - datetime.fromisoformat(first.replace("Z", "+00:00"))).total_seconds())
    except ValueError:
        return None
