"""Shared UTC timestamp helpers for the pipeline's ISO-8601 ledger fields."""

from __future__ import annotations

from datetime import datetime, timezone


def as_utc(value: datetime) -> datetime:
    """Coerce a naive datetime to UTC and convert an aware one into UTC."""

    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def parse_timestamp(value: object) -> datetime | None:
    """Parse one ISO-8601 string (``Z`` accepted) into an aware UTC datetime.

    Timestamps come from ledger rows, status files, and transcripts, so a
    missing, non-string, or malformed value simply returns ``None`` rather
    than interrupting the caller.
    """

    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return as_utc(parsed)


def utc_now_iso() -> str:
    """Return the current UTC time in the ledger's ISO-8601 spelling."""

    return datetime.now(timezone.utc).isoformat()
