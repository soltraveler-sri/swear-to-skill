"""Small schema-faithful builders for synthetic corpus authors.

The helpers deliberately emit only fabricated Claude Code-shaped records.  They
are not transcript converters: real transcript material must never enter evals.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable


def claude_session_skeleton(
    session_id: str,
    cwd: str,
    messages: Iterable[str],
    *,
    start: datetime | None = None,
    branch: str = "feat/fake-eval",
) -> list[dict[str, object]]:
    """Return a direct-user/assistant chain accepted by the real adapter.

    UUIDs and parent UUIDs are deterministic from ``session_id``.  ``cwd`` is
    intentionally explicit so callers keep the corpus privacy-safe.
    """

    if not session_id or not cwd.startswith("/work/fake-"):
        raise ValueError("synthetic sessions require an id and a /work/fake-* cwd")
    clock = start or datetime(2026, 7, 9, 12, tzinfo=timezone.utc)
    records: list[dict[str, object]] = []
    parent: str | None = None
    for index, text in enumerate(messages, start=1):
        user_uuid = f"{session_id}-u-{index:02d}"
        timestamp = (clock + timedelta(minutes=(index - 1) * 2)).isoformat().replace("+00:00", "Z")
        records.append(
            {
                "type": "user",
                "uuid": user_uuid,
                "parentUuid": parent,
                "timestamp": timestamp,
                "sessionId": session_id,
                "cwd": cwd,
                "gitBranch": branch,
                "version": "synthetic-1.0",
                "message": {"role": "user", "content": text},
            }
        )
        assistant_uuid = f"{session_id}-a-{index:02d}"
        records.append(
            {
                "type": "assistant",
                "uuid": assistant_uuid,
                "parentUuid": user_uuid,
                "timestamp": (clock + timedelta(minutes=(index - 1) * 2 + 1)).isoformat().replace("+00:00", "Z"),
                "sessionId": session_id,
                "cwd": cwd,
                "gitBranch": branch,
                "message": {
                    "role": "assistant",
                    "model": "synthetic-claude",
                    "content": [{"type": "text", "text": "Synthetic acknowledgement."}],
                },
            }
        )
        parent = assistant_uuid
    return records
