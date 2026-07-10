"""Stage 6 deterministic, per-cluster remedy outcome measurement.

The Auditor reads ledger facts and may queue a retirement proposal, but it never
touches a remedy target.  Approval remains the Gate's authority boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json

from .config import Auditor as AuditorConfig
from .config import load_config
from .ledger import Ledger, Remedy, SessionStats


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

    current = _as_utc(now or datetime.now(timezone.utc))
    settings = config or load_config().auditor
    outcomes: list[AuditOutcome] = []
    for remedy in ledger.installed_remedies():
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
            if outcome.verdict == "silent" and not ledger.audit_proposal_exists(
                remedy.id, "retirement"
            ):
                proposal_id = _queue_retirement(ledger, remedy, outcome)
                outcome = AuditOutcome(**{**outcome.__dict__, "proposal_id": proposal_id})
            outcomes.append(outcome)
    return tuple(outcomes)


def _measure(
    ledger: Ledger,
    remedy: Remedy,
    label: str,
    now: datetime,
    config: AuditorConfig,
) -> AuditOutcome:
    installed = _parse_timestamp(remedy.installed_at)
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
        timestamp = _parse_timestamp(session.last_timestamp or session.first_timestamp)
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


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _as_utc(parsed)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
