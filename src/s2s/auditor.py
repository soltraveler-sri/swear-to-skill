"""Stage 6 deterministic, per-cluster remedy outcome measurement.

The Auditor reads ledger facts and may queue a retirement proposal, but it never
touches a remedy target.  Approval remains the Gate's authority boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from .config import Auditor as AuditorConfig
from .config import load_config
from .ledger import Ledger, Remedy, SessionStats
from .timeutils import as_utc, parse_timestamp


@dataclass(frozen=True)
class AuditOutcome:
    """One remedy/cluster comparison, with denominators kept visible."""

    remedy_id: int
    label: str
    installed_at: str
    pre_incidents: int
    pre_sessions: int
    post_incidents: int
    post_sessions: int
    pre_rate: float | None
    post_rate: float | None
    verdict: str
    counter_evidence_ids: tuple[int, ...]
    proposal_id: int | None = None


def audit_remedies(
    ledger: Ledger,
    *,
    now: datetime | None = None,
    config: AuditorConfig | None = None,
) -> tuple[AuditOutcome, ...]:
    """Measure every installed remedy and queue only deterministic retirement actions.

    Comparisons are deliberately *per cluster*.  A global frustration-rate drop
    cannot establish that one specific remedy worked, so it is never consulted.
    """

    current = as_utc(now or datetime.now(timezone.utc))
    settings = config or load_config().auditor
    outcomes: list[AuditOutcome] = []
    for remedy in ledger.installed_remedies():
        usage_outcome = _unused_skill_outcome(ledger, remedy, current, settings)
        if usage_outcome is not None:
            # Put this first so the existing one-revision-per-remedy Synthesist
            # path prioritizes a concrete discovery failure over rate evidence.
            outcomes.append(usage_outcome)
        labels = ledger.remedy_labels(remedy.id)
        for label in labels:
            outcome = _measure(ledger, remedy, label, current, settings)
            ledger.record_remedy_outcome(
                remedy.id,
                verdict=outcome.verdict,
                pre_rate=outcome.pre_rate,
                post_rate=outcome.post_rate,
                assessed_at=current,
            )
            proposal_id = None
            if usage_outcome is None and outcome.verdict == "silent" and not ledger.audit_proposal_exists(
                remedy.id, "retirement"
            ):
                proposal_id = _queue_retirement(ledger, remedy, outcome)
                outcome = AuditOutcome(**{**outcome.__dict__, "proposal_id": proposal_id})
            outcomes.append(outcome)
    return tuple(outcomes)


def _unused_skill_outcome(
    ledger: Ledger,
    remedy: Remedy,
    now: datetime,
    config: AuditorConfig,
) -> AuditOutcome | None:
    """Return a Synthesist-compatible trigger-revision signal at both thresholds."""

    if remedy.artifact_type != "skill":
        return None
    installed = parse_timestamp(remedy.installed_at)
    if installed is None:
        return None
    age = now - installed
    if age < timedelta(days=max(0, config.min_usage_observation_days)):
        return None
    sessions = ledger.sessions_scanned_since(remedy.installed_at, before=now.isoformat())
    if sessions < max(0, config.min_sessions_scanned):
        return None
    original = ledger.get_proposal(remedy.proposal_id)
    if original is None or not original.evidence_incident_ids:
        return None
    skill = _installed_skill_identity(Path(remedy.artifact_path)) or _proposed_skill_identity(
        original.drafted_content
    )
    if skill is None:
        return None
    skill_name, description = skill
    if not skill_name.startswith("s2s-"):
        return None
    if ledger.skill_usage_count(
        skill_name, after=remedy.installed_at, before=now.isoformat()
    ):
        return None
    prompt_input = (
        f"Installed skill {skill_name!r}. Description: {description}. "
        f"It never fired during {sessions} sessions scanned since installation; "
        "revise its trigger description so the intended situations are discoverable."
    )
    return AuditOutcome(
        remedy_id=remedy.id,
        label=prompt_input,
        installed_at=remedy.installed_at,
        pre_incidents=0,
        pre_sessions=0,
        post_incidents=0,
        post_sessions=sessions,
        pre_rate=None,
        post_rate=0.0,
        verdict="persisting",
        counter_evidence_ids=original.evidence_incident_ids,
    )


def _installed_skill_identity(path: Path) -> tuple[str, str] | None:
    """Read only the installed skill's simple name/description frontmatter."""

    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    if not text.startswith("---\n"):
        return None
    closing = text.find("\n---\n", 4)
    if closing < 0:
        return None
    fields: dict[str, str] = {}
    for line in text[4:closing].splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        fields[key.strip()] = value
    name = fields.get("name", "")
    description = fields.get("description", "")
    return (name, description) if name and description else None


def _proposed_skill_identity(drafted_content: str) -> tuple[str, str] | None:
    """Fall back to proposal metadata when an installed artifact is unreadable."""

    try:
        payload = json.loads(drafted_content)
    except json.JSONDecodeError:
        return None
    content = payload.get("remedy_content") if isinstance(payload, dict) else None
    if not isinstance(content, dict):
        return None
    name = content.get("name")
    description = content.get("description")
    if not isinstance(name, str) or not isinstance(description, str) or not name or not description:
        return None
    return (name if name.startswith("s2s-") else f"s2s-{name}", description)


def _measure(
    ledger: Ledger,
    remedy: Remedy,
    label: str,
    now: datetime,
    config: AuditorConfig,
) -> AuditOutcome:
    installed = parse_timestamp(remedy.installed_at)
    if installed is None:
        # Install timestamps are ledger-owned, but preserve uncertainty if a user
        # has manually damaged one rather than inventing a result.
        installed = now
    sessions = ledger.session_stats()
    pre_sessions = _sessions_in_window(sessions, before=installed)
    post_sessions = _sessions_in_window(sessions, after=installed, before=now)
    pre_incidents = ledger.audit_incidents(label, before=installed.isoformat())
    post_incidents = ledger.audit_incidents(
        label, after=installed.isoformat(), before=now.isoformat()
    )
    pre_rate = _rate(len(pre_incidents), len(pre_sessions))
    post_rate = _rate(len(post_incidents), len(post_sessions))
    age = now - installed

    if age >= timedelta(days=max(0, config.silent_days)) and not post_incidents:
        verdict = "silent"
    elif (
        len(post_sessions) < max(0, config.min_post_install_sessions)
        and age < timedelta(days=max(0, config.min_post_install_days))
    ):
        verdict = "insufficient-data"
    elif pre_rate is None or post_rate is None or pre_rate == 0:
        verdict = "insufficient-data"
    elif post_rate <= pre_rate * (1 - _bounded_fraction(config.meaningful_drop_fraction)):
        verdict = "effective"
    else:
        verdict = "persisting"

    return AuditOutcome(
        remedy_id=remedy.id,
        label=label,
        installed_at=remedy.installed_at,
        pre_incidents=len(pre_incidents),
        pre_sessions=len(pre_sessions),
        post_incidents=len(post_incidents),
        post_sessions=len(post_sessions),
        pre_rate=pre_rate,
        post_rate=post_rate,
        verdict=verdict,
        counter_evidence_ids=tuple(incident.id for incident in post_incidents),
    )


def _queue_retirement(ledger: Ledger, remedy: Remedy, outcome: AuditOutcome) -> int:
    """Queue a human-gated archival candidate using original provenance as evidence."""

    original = ledger.get_proposal(remedy.proposal_id)
    if original is None:
        raise RuntimeError(f"remedy {remedy.id} has no provenance proposal")
    payload = {
        "remedy_type": "retirement",
        "action": "retire",
        "target_remedy_id": remedy.id,
        "failure_statement": (
            f"Cluster {outcome.label!r} has no incidents for the configured silent horizon."
        ),
        "evidence": [
            {"incident_id": incident_id, "quote": "Original remedy provenance."}
            for incident_id in original.evidence_incident_ids
        ],
        "confidence": None,
    }
    return ledger.create_proposal(
        remedy_type="retirement",
        drafted_content=json.dumps(payload, sort_keys=True, separators=(",", ":")),
        evidence_incident_ids=original.evidence_incident_ids,
        dedup_verdict="audit: silent cluster retirement candidate",
        gate_status="pending",
        proposal_kind="retirement",
        target_remedy_id=remedy.id,
    )


def _sessions_in_window(
    sessions: list[SessionStats],
    *,
    after: datetime | None = None,
    before: datetime | None = None,
) -> list[SessionStats]:
    """Count active sessions, not raw messages, as the outcome denominator."""

    selected: list[SessionStats] = []
    for session in sessions:
        timestamp = parse_timestamp(session.last_timestamp or session.first_timestamp)
        if timestamp is None or session.direct_message_count <= 0:
            continue
        if after is not None and timestamp < after:
            continue
        if before is not None and timestamp >= before:
            continue
        selected.append(session)
    return selected


def _rate(incidents: int, sessions: int) -> float | None:
    return incidents / sessions if sessions else None


def _bounded_fraction(value: float) -> float:
    return min(1.0, max(0.0, value))
