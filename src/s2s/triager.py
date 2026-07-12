"""Stage 2: one-incident closed-set classification over archived transcripts."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

from .adapters.claude_code import (
    ContextPack,
    build_context_pack,
    extract_session_metadata,
    extract_user_messages,
)
from .adapters import codex
from .config import load_config
from .ledger import Incident, Ledger
from .llm import call, estimate_and_confirm, load_prompt
from .paths import resolve_paths
from .taxonomy import label_names, list_labels


PROMPT_NAME = "triage"
PROMPT_VERSION = 2


class ContextResolutionError(RuntimeError):
    """Raised when a durable incident cannot be located in its archived source."""


@dataclass(frozen=True)
class TriageResult:
    """The committed outcome for one independently triaged incident."""

    incident_id: int
    state: str


def triage_pending(ledger: Ledger, *, limit: int | None = None, assume_yes: bool = False) -> list[TriageResult]:
    """Triage queued detections sequentially so each commit is independently resumable."""

    if limit is not None and limit < 0:
        raise ValueError("limit cannot be negative")
    incidents = ledger.untriaged_incidents()
    if limit is not None:
        incidents = incidents[:limit]
    if not incidents:
        return []

    config = load_config()
    model = config.models.triage
    if not estimate_and_confirm(len(incidents), model, assume_yes=assume_yes):
        return []

    template, base_schema = load_prompt(PROMPT_NAME, config.prompts.triage)
    schema = triage_schema(base_schema)
    outcomes: list[TriageResult] = []
    for incident in incidents:
        context_pack, pointer = context_for_incident(incident)
        response = call(
            render_triage_prompt(template, context_pack),
            schema=schema,
            model=model,
            stage="triage",
            effort=config.models.triage_effort,
        )
        authentic = response["authentic"]
        reason = response["reason"]
        label = response["label"]
        one_liner = response["one_liner"]
        severity = response["severity"]
        confidence = response["confidence"]
        if not isinstance(authentic, bool) or not all(
            isinstance(value, str) and value.strip() for value in (reason, label, one_liner)
        ) or isinstance(severity, bool) or not isinstance(severity, int) or isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            raise ValueError("validated triage response had unexpected Python types")
        ledger.triage_incident(
            incident.id,
            label=label,
            one_liner=one_liner,
            severity=str(severity),
            confidence=float(confidence),
            context_pack_pointer=pointer,
            dismissed=not authentic,
            reason=reason,
        )
        outcomes.append(TriageResult(incident.id, "dismissed-triage" if not authentic else "open"))
    return outcomes


def triage_schema(base_schema: dict[str, object] | None = None) -> dict[str, object]:
    """Inject the current taxonomy into the label enum at call time."""

    if base_schema is None:
        _, base_schema = load_prompt(PROMPT_NAME, load_config().prompts.triage)
    schema = deepcopy(base_schema)
    properties = schema.get("properties")
    if not isinstance(properties, dict) or not isinstance(properties.get("label"), dict):
        raise ValueError("triage schema must define a label property")
    properties["label"]["enum"] = list(label_names())
    return schema


def render_triage_prompt(template: str, context_pack: ContextPack) -> str:
    """Render only the supplied incident's pack and the fixed taxonomy guidance."""

    replacements = {
        "{{TAXONOMY_MENU}}": _render_taxonomy_menu(),
        "{{PRECEDING_REQUEST}}": context_pack.preceding_request,
        "{{AGENT_ACTIVITY_DIGEST}}": context_pack.agent_activity_digest,
        "{{FRUSTRATED_MESSAGE}}": context_pack.frustrated_message,
        "{{FOLLOWING_EXCHANGE}}": context_pack.following_exchange,
    }
    rendered = template
    for marker, value in replacements.items():
        rendered = rendered.replace(marker, value)
    if "{{" in rendered or "}}" in rendered:
        raise ValueError("triage prompt template contains an unknown placeholder")
    return rendered


def _render_taxonomy_menu() -> str:
    lines: list[str] = []
    for entry in list_labels():
        examples = "; ".join(entry.examples)
        lines.append(
            f"- {entry.label}: {entry.gist}. Examples: {examples}. "
            f"Counter-example: {entry.counter_example}."
        )
    return "\n".join(lines)


def context_for_incident(incident: Incident) -> tuple[ContextPack, str]:
    """Locate the exact archived Claude Code user record for a durable incident.

    Later judgment stages reuse this resolver so archive-path and UUID matching
    semantics remain centralized in the transcript-owning stage.
    """

    if incident.source not in {"claude-code", "codex"}:
        raise ContextResolutionError(f"no Stage 2 context adapter is available for {incident.source!r}")
    paths = resolve_paths()
    if incident.source == "codex":
        transcript = _archived_codex_transcript_for(paths.archive_dir, incident)
        target_uuid = codex.find_message_uuid(
            transcript,
            session_id=incident.session_id,
            message=incident.message,
            occurred_at=incident.occurred_at,
        )
        if target_uuid is None:
            raise ContextResolutionError(
                f"could not find incident {incident.id} in archived Codex rollout {transcript}"
            )
        context_pack = codex.build_context_pack(transcript, target_uuid)
    else:
        transcript = _archived_transcript_for(paths.archive_dir, incident)
        target_uuid = _incident_uuid(transcript, incident)
        context_pack = build_context_pack(transcript, target_uuid)
    try:
        archive_pointer = transcript.relative_to(paths.home)
    except ValueError as error:
        raise ContextResolutionError(f"archived transcript is outside S2S_HOME: {transcript}") from error
    return context_pack, f"{archive_pointer}#{target_uuid}"


def _archived_transcript_for(archive_dir: Path, incident: Incident) -> Path:
    """Locate the archived transcript holding this incident's session.

    Claude Code names transcripts by session UUID, so the direct path almost
    always exists — but the archive keys by source filename, which is not
    guaranteed to match the transcript's internal sessionId (e.g. copied or
    renamed files). Fall back to matching on the internal session id.
    """

    direct = archive_dir / incident.project / f"{incident.session_id}.jsonl"
    if direct.is_file():
        return direct
    project_dir = archive_dir / incident.project
    if project_dir.is_dir():
        for candidate in sorted(project_dir.glob("*.jsonl")):
            metadata = extract_session_metadata(candidate)
            if metadata.session_id == incident.session_id:
                return candidate
    raise ContextResolutionError(
        f"no archived transcript for session {incident.session_id!r} under {project_dir}"
    )


def _archived_codex_transcript_for(archive_dir: Path, incident: Incident) -> Path:
    """Find a Codex rollout by internal thread id inside its durable archive."""

    project_dir = archive_dir / "codex"
    if project_dir.is_dir():
        for candidate in sorted(project_dir.glob("*.jsonl")):
            if codex.extract_session_metadata(candidate).session_id == incident.session_id:
                return candidate
    raise ContextResolutionError(
        f"no archived Codex rollout for session {incident.session_id!r} under {project_dir}"
    )


def _incident_uuid(transcript: Path, incident: Incident) -> str:
    if not transcript.is_file():
        raise ContextResolutionError(f"archived transcript is missing: {transcript}")
    for message in extract_user_messages(transcript):
        if (
            message.uuid
            and message.session_id == incident.session_id
            and message.message == incident.message
            and (not incident.occurred_at or message.timestamp == incident.occurred_at)
        ):
            return message.uuid
    raise ContextResolutionError(
        f"could not find incident {incident.id} in archived transcript {transcript}"
    )
