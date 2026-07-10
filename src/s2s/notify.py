"""Local-first notification surfaces for the human Gate.

SessionStart reads only the pump's small status file. Event notifications are
opt-in and deliberately carry metadata, never transcript content.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import subprocess
import sys
import urllib.request

from .config import load_config
from .paths import resolve_paths


STATUS_MAX_AGE_SECONDS = 7 * 24 * 60 * 60
MAX_FIELD_CHARS = 200
NOTIFY_LOG_NAME = "notify.log"
_SAFE_FIELD_SUFFIXES = ("_count", "_id", "_title")
_SAFE_FIELD_NAMES = {
    "action",
    "confidence",
    "count",
    "evidence_ids",
    "id",
    "provenance",
    "reason",
    "remedy_type",
    "title",
}


def sessionstart_digest() -> str:
    """Return one short status-file-only line, or ``''`` when there is none."""

    try:
        payload = json.loads(resolve_paths().status_file.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return ""
        generated_at = _parse_timestamp(payload.get("generated_at"))
        if generated_at is None:
            return ""
        if (datetime.now(timezone.utc) - generated_at).total_seconds() > STATUS_MAX_AGE_SECONDS:
            return ""

        pending = payload.get("proposals_pending", 0)
        if not isinstance(pending, int) or isinstance(pending, bool) or pending < 0:
            pending = 0
        autonomous_note = _autonomous_note(payload.get("notes"))
        if pending == 0 and autonomous_note is None:
            return ""

        parts: list[str] = []
        if pending:
            noun = "proposal" if pending == 1 else "proposals"
            parts.append(
                f"s2s: {pending} remedy {noun} awaiting review — "
                "'s2s proposals' or '/s2s'"
            )
        if autonomous_note is not None:
            suffix = f"autonomous action: {autonomous_note}"
            if parts:
                return _one_line(f"{parts[0]} — {suffix}")
            return _one_line(f"s2s: {suffix}")
        return _one_line(parts[0])
    except (OSError, UnicodeError, TypeError, ValueError, json.JSONDecodeError):
        return ""


def emit(event: str, **fields: object) -> None:
    """Dispatch one metadata-only event to enabled, fail-soft notifiers."""

    try:
        config = load_config()
        notifications = config.notifications
        if event not in notifications.events:
            return
        safe_fields = _safe_fields(fields)
        event_name = _bounded_string(str(event), "event")
        text = _event_text(event_name, safe_fields)
        payload: dict[str, object] = {"text": text, "event": event_name, **safe_fields}
        _assert_payload_bounds(payload)

        if notifications.desktop:
            _send_desktop(text)
        if notifications.webhook_url:
            _send_webhook(notifications.webhook_url, payload)
    except Exception as error:
        _log_failure("notification dispatch failed", error)


def _send_desktop(text: str) -> None:
    """Send a platform-native desktop notification without invoking a shell."""

    try:
        if sys.platform == "darwin":
            script = f'display notification {_apple_quote(text)} with title "swear-to-skill"'
            subprocess.run(["osascript", "-e", script], check=True)
        elif sys.platform.startswith("linux"):
            subprocess.run(["notify-send", "swear-to-skill", text], check=True)
    except Exception as error:
        _log_failure("desktop notification failed", error)


WEBHOOK_TIMEOUT_S = 5.0


def _send_webhook(url: str, payload: dict[str, object], *, timeout_s: float | None = None) -> None:
    """POST a JSON webhook, retrying one time and swallowing every failure."""

    timeout = WEBHOOK_TIMEOUT_S if timeout_s is None else timeout_s
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    for attempt in range(2):
        try:
            with urllib.request.urlopen(request, timeout=timeout):
                pass
            return
        except Exception as error:
            if attempt == 1:
                _log_failure("webhook notification failed", error)


def _safe_fields(fields: dict[str, object]) -> dict[str, object]:
    safe: dict[str, object] = {}
    for key, value in fields.items():
        if not _is_safe_field_name(key):
            _log_failure(f"dropped non-metadata notification field {key!r}")
            continue
        if isinstance(value, str):
            safe[key] = _bounded_string(value, key)
        elif isinstance(value, (int, float, bool)) or value is None:
            safe[key] = value
        else:
            _log_failure(f"dropped non-scalar notification field {key!r}")
    return safe


def _is_safe_field_name(name: str) -> bool:
    return name in _SAFE_FIELD_NAMES or name.endswith(_SAFE_FIELD_SUFFIXES)


def _bounded_string(value: str, field: str) -> str:
    if len(value) <= MAX_FIELD_CHARS:
        return value
    _log_failure(f"truncated notification field {field!r} from {len(value)} characters")
    return value[:MAX_FIELD_CHARS]


def _assert_payload_bounds(payload: dict[str, object]) -> None:
    """Keep the metadata boundary executable and easy to test."""

    assert all(not isinstance(value, str) or len(value) <= MAX_FIELD_CHARS for value in payload.values())


def _event_text(event: str, fields: dict[str, object]) -> str:
    if event == "proposal_pending":
        count = fields.get("proposal_count", 1)
        noun = "proposal" if count == 1 else "proposals"
        return f"s2s: {count} remedy {noun} awaiting review"
    if event == "autonomous_action":
        action = fields.get("action", "completed")
        proposal_id = fields.get("proposal_id")
        remedy_id = fields.get("remedy_id")
        subject = (
            f"remedy {remedy_id}"
            if remedy_id is not None
            else f"proposal {proposal_id}"
            if proposal_id is not None
            else "pump"
        )
        return _one_line(f"s2s: autonomous action {action}: {subject}")
    return _one_line(f"s2s: {event}")


def _autonomous_note(notes: object) -> str | None:
    if not isinstance(notes, list):
        return None
    for note in notes:
        if isinstance(note, str) and "autonomous" in note.lower() and "action" in note.lower():
            return _one_line(note, limit=120)
    return None


def _one_line(value: str, *, limit: int = MAX_FIELD_CHARS) -> str:
    compact = " ".join(value.split())
    return compact if len(compact) <= limit else compact[:limit]


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _apple_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _log_failure(message: str, error: object | None = None) -> None:
    try:
        log_path = resolve_paths().home / "logs" / NOTIFY_LOG_NAME
        log_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        suffix = f": {error}" if error is not None else ""
        with log_path.open("a", encoding="utf-8") as log_file:
            log_file.write(f"{message}{suffix}\n")
    except Exception:
        pass
