"""Stage 3: capable batched judgment with O(new evidence) economics."""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import logging
from pathlib import Path
import random

from .config import Config, load_config
from .ledger import Incident, Ledger, LedgerError
from .llm import call, estimate_and_confirm, load_prompt
from .paths import resolve_paths
from .taxonomy import apply_merge, apply_new_label, label_names, list_labels
from .triager import context_for_incident


PROMPT_NAME = "curate"
PROMPT_VERSION = 1
GARDEN_PROMPT_NAME = "garden"
GARDEN_PROMPT_VERSION = 1
LAST_PASS_META_KEY = "curator.last_pass_at"
MEMBER_ONE_LINER_LIMIT = 180
PROPOSAL_TEXT_LIMIT = 180

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _PackEntry:
    incident: Incident
    origin: str
    text: str


@dataclass(frozen=True)
class _Chunk:
    entries: tuple[_PackEntry, ...]
    regular_labels: tuple[str, ...]


@dataclass(frozen=True)
class _ReportVerdict:
    kind: str
    subject: str
    verdict: str
    reason: str
    status: str


@dataclass(frozen=True)
class CuratorPassResult:
    """Observable result of one forced Curator pass attempt."""

    report_path: Path | None
    calls: int
    full_context_incident_ids: tuple[int, ...]
    applied_incident_verdicts: int
    applied_cluster_verdicts: int
    rejected_verdicts: int
    skipped: bool = False


def pass_due(ledger: Ledger, config: Config) -> bool:
    """Return whether accumulation or elapsed-time policy makes review due."""

    unreviewed = ledger.curator_unreviewed_incidents()
    qc_candidates = ledger.curator_qc_candidates()
    if not unreviewed and not qc_candidates:
        return False
    threshold = max(1, config.thresholds.curator_unreviewed_count)
    if len(unreviewed) >= threshold:
        return True

    max_age = timedelta(days=max(0, config.thresholds.curator_max_age_days))
    last_pass = _parse_timestamp(ledger.get_meta(LAST_PASS_META_KEY))
    now = datetime.now(timezone.utc)
    if last_pass is not None:
        return now - last_pass >= max_age

    # A fresh ledger has no pass timestamp yet. Treat the oldest waiting item as
    # the start of its first age window instead of running immediately at count 1.
    oldest = min(
        (_parse_timestamp(item.created_at) or now for item in (*unreviewed, *qc_candidates)),
        default=now,
    )
    return now - oldest >= max_age


def run_pass(ledger: Ledger, *, assume_yes: bool = False) -> CuratorPassResult:
    """Assemble, call, apply, and audit one Curator pass.

    This is the force-now entrypoint used by ``s2s review``. It still becomes a
    zero-call no-op when no never-reviewed, event-resurfaced, or unsampled QC
    evidence exists.
    """

    config = load_config()
    new_incidents = ledger.curator_unreviewed_incidents()
    touched_labels = tuple(
        dict.fromkeys(incident.label for incident in new_incidents if incident.label)
    )
    parked = [
        incident
        for label in touched_labels
        for incident in ledger.parked_incidents(label)
    ]
    qc = _select_qc_sample(
        ledger.curator_qc_candidates(),
        max(0, config.curator.qc_sample_size),
        ledger.get_meta(LAST_PASS_META_KEY),
    )

    selected: list[tuple[Incident, str]] = []
    seen_ids: set[int] = set()
    for incident, origin in (
        *((incident, "new") for incident in new_incidents),
        *((incident, "parked") for incident in parked),
        *((incident, "qc") for incident in qc),
    ):
        if incident.id not in seen_ids:
            seen_ids.add(incident.id)
            selected.append((incident, origin))

    garden_initially_needed = gardening_needed(ledger)
    if not selected and not garden_initially_needed:
        return CuratorPassResult(None, 0, (), 0, 0, 0, skipped=True)

    entries = tuple(_build_pack_entry(incident, origin) for incident, origin in selected)
    digest = render_ledger_digest(ledger)
    chunks = _chunk_entries(entries, max(1, config.curator.context_char_budget)) if entries else ()
    model = config.models.curate
    planned_calls = len(chunks) + int(garden_initially_needed)
    if not estimate_and_confirm(planned_calls, model, assume_yes=assume_yes):
        return CuratorPassResult(
            None,
            0,
            tuple(entry.incident.id for entry in entries),
            0,
            0,
            0,
            skipped=True,
        )

    template, base_schema = load_prompt(PROMPT_NAME, PROMPT_VERSION) if chunks else ("", {})
    schema = curator_schema(base_schema) if chunks else {}
    records: list[_ReportVerdict] = []
    applied_incident = 0
    applied_cluster = 0
    rejected = 0

    for chunk in chunks:
        prompt = render_curator_prompt(template, digest, chunk)
        response = call(prompt, schema=schema, model=model, stage="curate")

        # Wake only after a successful structured response. A failed call leaves
        # parked evidence parked and eligible for the next arrival event.
        parked_labels = {
            entry.incident.label
            for entry in chunk.entries
            if entry.origin == "parked" and entry.incident.label
        }
        for label in sorted(parked_labels):
            ledger.wake_parked_incidents(label)

        chunk_records, incident_count, cluster_count, rejection_count = _apply_response(
            ledger, chunk, response
        )
        records.extend(chunk_records)
        applied_incident += incident_count
        applied_cluster += cluster_count
        rejected += rejection_count

    garden_records, garden_calls = _run_gardening(ledger, model=model)
    records.extend(garden_records)

    completed_at = datetime.now(timezone.utc)
    report_path = _write_report(completed_at, digest, entries, records)
    ledger.set_meta(LAST_PASS_META_KEY, completed_at.isoformat())
    return CuratorPassResult(
        report_path=report_path,
        calls=len(chunks) + garden_calls,
        full_context_incident_ids=tuple(entry.incident.id for entry in entries),
        applied_incident_verdicts=applied_incident,
        applied_cluster_verdicts=applied_cluster,
        rejected_verdicts=rejected,
    )


def curator_schema(base_schema: dict[str, object] | None = None) -> dict[str, object]:
    """Inject the immutable taxonomy into both reassign and cluster label enums."""

    if base_schema is None:
        _, base_schema = load_prompt(PROMPT_NAME, PROMPT_VERSION)
    schema = deepcopy(base_schema)
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        raise ValueError("curator schema must define properties")
    incident_items = _array_item_properties(properties, "incident_verdicts")
    cluster_items = _array_item_properties(properties, "cluster_verdicts")
    labels = list(label_names())
    reassign = incident_items.get("reassign_label")
    cluster_label = cluster_items.get("label")
    if not isinstance(reassign, dict) or not isinstance(cluster_label, dict):
        raise ValueError("curator schema must define reassign_label and cluster label")
    reassign["enum"] = labels
    cluster_label["enum"] = labels
    return schema


def garden_schema(base_schema: dict[str, object] | None = None) -> dict[str, object]:
    """Inject current mergeable labels into the gardening schema at call time."""

    if base_schema is None:
        _, base_schema = load_prompt(GARDEN_PROMPT_NAME, GARDEN_PROMPT_VERSION)
    schema = deepcopy(base_schema)
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        raise ValueError("garden schema must define properties")
    merges = _array_item_properties(properties, "merge_labels")
    labels = [name for name in label_names() if name != "other"]
    for name in ("survivor", "absorbed"):
        field = merges.get(name)
        if not isinstance(field, dict):
            raise ValueError(f"garden schema must define merge {name}")
        field["enum"] = labels
    return schema


def gardening_needed(ledger: Ledger) -> bool:
    """Return whether active escape-hatch evidence or fragmentation merits judgment."""

    return bool(ledger.other_nonterminal_incidents()) or _distribution_anomaly(ledger)


def render_garden_prompt(template: str, ledger: Ledger) -> str:
    """Render the complete active ``other`` set and distribution health facts."""

    other = ledger.other_nonterminal_incidents()
    other_lines = [
        f"- incident_id={incident.id} | one_liner={_truncate(incident.one_liner or incident.message, MEMBER_ONE_LINER_LIMIT)}"
        for incident in other
    ] or ["- (none)"]
    stats = ledger.cluster_stats()
    total = sum(item.incident_count for item in stats)
    distribution = [
        f"- {item.label}: count={item.incident_count} share={item.incident_count / total:.1%}"
        for item in stats
    ] if total else ["- (no labelled incidents)"]
    distribution.extend(
        (
            f"- active_cluster_count={ledger.active_cluster_count()}",
            f"- singleton_ratio={ledger.singleton_ratio():.1%}",
            f"- other_share_open={ledger.other_share():.1%}",
        )
    )
    rendered = template.replace("{{OTHER_INCIDENTS}}", "\n".join(other_lines))
    rendered = rendered.replace("{{LABEL_DISTRIBUTION}}", "\n".join(distribution))
    if "{{" in rendered or "}}" in rendered:
        raise ValueError("garden prompt template contains an unknown placeholder")
    return rendered


def render_curator_prompt(template: str, digest: str, chunk: _Chunk) -> str:
    """Render one chunk while repeating the digest and never repeating a pack."""

    chunk_labels = "\n".join(f"- {label}" for label in chunk.regular_labels) or "(none; QC-only chunk)"
    replacements = {
        "{{TAXONOMY_MENU}}": "\n".join(
            f"- {entry.label}: {entry.gist}" for entry in list_labels()
        ),
        "{{CHUNK_LABELS}}": chunk_labels,
        "{{LEDGER_DIGEST}}": digest,
        "{{FULL_CONTEXT_PACKS}}": "\n\n".join(entry.text for entry in chunk.entries),
    }
    rendered = template
    for marker, value in replacements.items():
        rendered = rendered.replace(marker, value)
    if "{{" in rendered or "}}" in rendered:
        raise ValueError("curator prompt template contains an unknown placeholder")
    return rendered


def render_ledger_digest(ledger: Ledger) -> str:
    """Render one bounded line per active cluster, proposal, and remedy."""

    members_by_label: dict[str, list[Incident]] = {}
    for incident in ledger.active_cluster_members():
        if incident.label:
            members_by_label.setdefault(incident.label, []).append(incident)

    lines = ["CLUSTERS:"]
    if not members_by_label:
        lines.append("- (none)")
    for label, members in sorted(members_by_label.items()):
        projects = len({member.project for member in members})
        states = ",".join(
            f"{state}:{count}" for state, count in sorted(Counter(member.state for member in members).items())
        )
        one_liners = "; ".join(
            f"#{member.id} {_truncate(member.one_liner or member.message, MEMBER_ONE_LINER_LIMIT)}"
            for member in members
        )
        fast_track = (
            " | FAST-TRACK: recommend synthesis"
            if len(members) >= 3 and projects >= 2
            else ""
        )
        lines.append(
            f"- {label} | count={len(members)} | projects={projects} | states={states}"
            f"{fast_track} | members={one_liners}"
        )

    lines.append("PROPOSALS:")
    proposal_rows = ledger.proposal_digest_rows()
    if not proposal_rows:
        lines.append("- (none)")
    for proposal_id, remedy_type, gate_status, content in proposal_rows:
        lines.append(
            f"- proposal #{proposal_id} | type={remedy_type} | status={gate_status} | "
            f"{_truncate(content, PROPOSAL_TEXT_LIMIT)}"
        )

    lines.append("REMEDIES:")
    remedy_rows = ledger.remedy_digest_rows()
    if not remedy_rows:
        lines.append("- (none)")
    for remedy_id, artifact_type, artifact_path, rollback_at in remedy_rows:
        status = "rolled-back" if rollback_at else "installed"
        lines.append(
            f"- remedy #{remedy_id} | type={artifact_type} | status={status} | path={artifact_path}"
        )
    return "\n".join(lines)


def _run_gardening(ledger: Ledger, *, model: str) -> tuple[list[_ReportVerdict], int]:
    """Apply the capable-model's strictly limited taxonomy maintenance authority."""

    if not gardening_needed(ledger):
        return [], 0
    template, base_schema = load_prompt(GARDEN_PROMPT_NAME, GARDEN_PROMPT_VERSION)
    response = call(
        render_garden_prompt(template, ledger),
        schema=garden_schema(base_schema),
        model=model,
        stage="curate",
    )
    records: list[_ReportVerdict] = []
    other_ids = {incident.id for incident in ledger.other_nonterminal_incidents()}
    raw_merges = response.get("merge_labels", [])
    assert isinstance(raw_merges, list)
    for raw_merge in raw_merges:
        if not isinstance(raw_merge, dict):
            records.append(_ReportVerdict("garden merge", "unknown", "merge", "invalid response", "rejected"))
            continue
        survivor = raw_merge.get("survivor")
        absorbed = raw_merge.get("absorbed")
        reason = raw_merge.get("reason")
        subject = f"{absorbed} -> {survivor}"
        if not all(isinstance(value, str) and value.strip() for value in (survivor, absorbed, reason)):
            records.append(_ReportVerdict("garden merge", subject, "merge", str(reason), "rejected: invalid fields"))
            continue
        try:
            apply_merge(survivor=survivor, absorbed=absorbed, reason=reason)
            changed = ledger.relabel_label(absorbed, survivor)
        except (ValueError, FileExistsError, LedgerError) as error:
            records.append(_ReportVerdict("garden merge", subject, "merge", reason, f"rejected: {error}"))
            continue
        records.append(
            _ReportVerdict("garden merge", subject, "merge", reason, f"applied: relabelled {changed} incident(s)")
        )

    proposal = response.get("propose_label")
    if proposal is None:
        return records, 1
    if not isinstance(proposal, dict):
        records.append(_ReportVerdict("garden label", "unknown", "add", "invalid response", "rejected"))
        return records, 1
    name = proposal.get("name")
    gist = proposal.get("gist")
    examples = proposal.get("examples")
    evidence = proposal.get("evidence_incident_ids")
    subject = str(name)
    if (
        not isinstance(name, str)
        or not isinstance(gist, str)
        or not isinstance(examples, list)
        or not isinstance(evidence, list)
    ):
        records.append(_ReportVerdict("garden label", subject, "add", str(gist), "rejected: invalid fields"))
        return records, 1
    if len(evidence) < 3 or len(set(evidence)) != len(evidence) or not all(
        isinstance(item, int) and not isinstance(item, bool) and item in other_ids for item in evidence
    ):
        records.append(
            _ReportVerdict(
                "garden label",
                subject,
                "add",
                gist,
                "rejected: requires three unique active other evidence incident IDs",
            )
        )
        return records, 1
    try:
        apply_new_label(
            name=name,
            gist=gist,
            examples=examples,
            evidence_incident_ids=evidence,
        )
        changed = ledger.relabel_incidents(evidence, name)
    except (ValueError, FileExistsError, LedgerError) as error:
        records.append(_ReportVerdict("garden label", subject, "add", gist, f"rejected: {error}"))
        return records, 1
    records.append(
        _ReportVerdict(
            "garden label", subject, "add", gist, f"applied: relabelled {changed} evidence incident(s)"
        )
    )
    return records, 1


def _distribution_anomaly(ledger: Ledger) -> bool:
    return (
        ledger.active_cluster_count() >= 10 and ledger.singleton_ratio() > 0.9
    ) or ledger.other_share() > 0.3


def _build_pack_entry(incident: Incident, origin: str) -> _PackEntry:
    pack, _ = context_for_incident(incident)
    label = incident.label or "other"
    text = (
        f"BEGIN FULL CONTEXT PACK incident_id={incident.id} label={label} origin={origin}\n"
        f"Preceding request:\n{pack.preceding_request}\n\n"
        f"Agent activity digest:\n{pack.agent_activity_digest}\n\n"
        f"Frustrated message:\n{pack.frustrated_message}\n\n"
        f"Following exchange:\n{pack.following_exchange}\n"
        f"END FULL CONTEXT PACK incident_id={incident.id}"
    )
    return _PackEntry(incident=incident, origin=origin, text=text)


def _chunk_entries(entries: tuple[_PackEntry, ...], budget: int) -> tuple[_Chunk, ...]:
    groups: dict[str, list[_PackEntry]] = {}
    for entry in entries:
        groups.setdefault(entry.incident.label or "other", []).append(entry)

    if sum(len(entry.text) for entry in entries) <= budget:
        return (_make_chunk(entries),)

    chunks: list[_Chunk] = []
    current: list[_PackEntry] = []
    current_size = 0
    for group_entries in groups.values():
        group_size = sum(len(entry.text) for entry in group_entries)
        if current and current_size + group_size > budget:
            chunks.append(_make_chunk(tuple(current)))
            current = []
            current_size = 0
        current.extend(group_entries)
        current_size += group_size
    if current:
        chunks.append(_make_chunk(tuple(current)))
    return tuple(chunks)


def _make_chunk(entries: tuple[_PackEntry, ...]) -> _Chunk:
    labels = tuple(
        dict.fromkeys(
            entry.incident.label or "other" for entry in entries if entry.origin != "qc"
        )
    )
    return _Chunk(entries=entries, regular_labels=labels)


def _apply_response(
    ledger: Ledger, chunk: _Chunk, response: dict[str, object]
) -> tuple[list[_ReportVerdict], int, int, int]:
    records: list[_ReportVerdict] = []
    applied_incident = 0
    applied_cluster = 0
    rejected = 0
    entries_by_id = {entry.incident.id: entry for entry in chunk.entries}
    raw_incident_verdicts = response.get("incident_verdicts", [])
    raw_cluster_verdicts = response.get("cluster_verdicts", [])
    assert isinstance(raw_incident_verdicts, list)
    assert isinstance(raw_cluster_verdicts, list)

    incident_ids = [
        item.get("incident_id")
        for item in raw_incident_verdicts
        if isinstance(item, dict) and isinstance(item.get("incident_id"), int)
    ]
    duplicate_ids = {item_id for item_id, count in Counter(incident_ids).items() if count > 1}
    applied_ids: set[int] = set()
    rejected_entry_ids: set[int] = set()
    for raw in raw_incident_verdicts:
        assert isinstance(raw, dict)
        incident_id = raw.get("incident_id")
        verdict = raw.get("verdict")
        reason = raw.get("reason")
        reassign_label = raw.get("reassign_label")
        subject = str(incident_id)
        if not isinstance(incident_id, int) or incident_id not in entries_by_id:
            logger.warning("Curator returned unknown incident_id %r; skipping", incident_id)
            records.append(
                _ReportVerdict("incident", subject, str(verdict), str(reason), "skipped: unknown id")
            )
            rejected += 1
            continue
        if incident_id in duplicate_ids:
            records.append(
                _ReportVerdict("incident", subject, str(verdict), str(reason), "rejected: duplicate id")
            )
            rejected_entry_ids.add(incident_id)
            rejected += 1
            continue
        entry = entries_by_id[incident_id]
        semantic_error = _incident_semantic_error(entry, verdict, reason, reassign_label)
        if semantic_error:
            records.append(
                _ReportVerdict("incident", subject, str(verdict), str(reason), f"rejected: {semantic_error}")
            )
            rejected_entry_ids.add(incident_id)
            rejected += 1
            continue
        assert isinstance(verdict, str) and isinstance(reason, str)
        assert reassign_label is None or isinstance(reassign_label, str)
        singleton = verdict == "promote" and _cluster_size(ledger, entry.incident.label) == 1
        try:
            ledger.apply_curator_incident_verdict(
                incident_id,
                verdict,
                reason=reason,
                reassign_label=reassign_label,
                singleton=singleton,
            )
        except LedgerError as error:
            records.append(
                _ReportVerdict("incident", subject, verdict, reason, f"rejected: {error}")
            )
            rejected_entry_ids.add(incident_id)
            rejected += 1
            continue
        applied_ids.add(incident_id)
        applied_incident += 1
        suffix = " (singleton provenance)" if singleton else ""
        records.append(_ReportVerdict("incident", subject, verdict, reason, f"applied{suffix}"))

    for incident_id, entry in entries_by_id.items():
        if incident_id not in incident_ids:
            records.append(
                _ReportVerdict(
                    "incident",
                    str(incident_id),
                    "missing",
                    "The model omitted this required full-context verdict.",
                    "rejected: missing verdict",
                )
            )
            rejected_entry_ids.add(incident_id)
            rejected += 1

    cluster_labels = [
        item.get("label")
        for item in raw_cluster_verdicts
        if isinstance(item, dict) and isinstance(item.get("label"), str)
    ]
    duplicate_labels = {
        label for label, count in Counter(cluster_labels).items() if count > 1
    }
    for raw in raw_cluster_verdicts:
        assert isinstance(raw, dict)
        label = raw.get("label")
        verdict = raw.get("verdict")
        reason = raw.get("reason")
        if not isinstance(label, str) or label not in chunk.regular_labels:
            records.append(
                _ReportVerdict("cluster", str(label), str(verdict), str(reason), "skipped: digest-only or unknown label")
            )
            rejected += 1
            continue
        if label in duplicate_labels:
            records.append(
                _ReportVerdict("cluster", label, str(verdict), str(reason), "rejected: duplicate label")
            )
            rejected += 1
            continue
        if not isinstance(reason, str) or not reason.strip():
            records.append(
                _ReportVerdict("cluster", label, str(verdict), str(reason), "rejected: empty reason")
            )
            rejected += 1
            continue
        assert isinstance(verdict, str)
        try:
            if verdict == "synthesize":
                open_members = [
                    incident
                    for incident in ledger.incidents_in_state("open")
                    if incident.label == label and incident.id not in rejected_entry_ids
                ]
                cluster_size = _cluster_size(ledger, label)
                for incident in open_members:
                    ledger.apply_curator_incident_verdict(
                        incident.id,
                        "promote",
                        reason=f"cluster synthesize: {reason}",
                        singleton=cluster_size == 1,
                    )
                    applied_ids.add(incident.id)
                    applied_incident += 1
                    records.append(
                        _ReportVerdict(
                            "incident",
                            str(incident.id),
                            "promote",
                            f"cluster synthesize: {reason}",
                            "applied via cluster",
                        )
                    )
            ledger.record_curator_cluster_verdict(label, verdict, reason=reason)
        except LedgerError as error:
            records.append(
                _ReportVerdict("cluster", label, verdict, reason, f"rejected: {error}")
            )
            rejected += 1
            continue
        applied_cluster += 1
        records.append(_ReportVerdict("cluster", label, verdict, reason, "applied"))

    # A resurfaced parked incident omitted or rejected by the model returns to
    # parked, so it cannot become a silent reviewed-open orphan after this event.
    for entry in chunk.entries:
        if entry.origin != "parked" or entry.incident.id in applied_ids:
            continue
        current = ledger.get_incident(entry.incident.id)
        if current is not None and current.state == "open":
            ledger.transition_incident(
                current.id,
                "parked",
                reason="Curator did not return a valid verdict for resurfaced evidence",
            )
    return records, applied_incident, applied_cluster, rejected


def _incident_semantic_error(
    entry: _PackEntry, verdict: object, reason: object, reassign_label: object
) -> str | None:
    if not isinstance(reason, str) or not reason.strip():
        return "empty reason"
    if not isinstance(verdict, str):
        return "invalid verdict"
    if entry.origin == "qc":
        if verdict not in {"dismiss", "resurrect"}:
            return "QC permits only dismiss or resurrect"
    elif verdict not in {"promote", "park", "dismiss", "reassign"}:
        return "regular review cannot resurrect"
    if verdict == "reassign":
        if not isinstance(reassign_label, str) or reassign_label not in label_names():
            return "reassign requires an existing taxonomy label"
    elif verdict == "resurrect":
        if reassign_label is not None and (
            not isinstance(reassign_label, str) or reassign_label not in label_names()
        ):
            return "resurrection reassign target is not an existing taxonomy label"
    elif reassign_label is not None:
        return f"{verdict} cannot also reassign"
    return None


def _select_qc_sample(
    candidates: list[Incident], sample_size: int, last_pass: str | None
) -> list[Incident]:
    if sample_size <= 0 or not candidates:
        return []
    recent_pool = candidates[: max(sample_size, sample_size * 4)]
    seed_material = f"{last_pass or 'first'}|" + ",".join(
        str(incident.id) for incident in recent_pool
    )
    seed = int.from_bytes(sha256(seed_material.encode("utf-8")).digest()[:8], "big")
    selected = random.Random(seed).sample(recent_pool, min(sample_size, len(recent_pool)))
    return sorted(selected, key=lambda incident: (incident.occurred_at, incident.id))


def _cluster_size(ledger: Ledger, label: str | None) -> int:
    if label is None:
        return 0
    row = ledger.connection.execute(
        "SELECT COUNT(*) AS count FROM incident WHERE label = ?", (label,)
    ).fetchone()
    return int(row["count"])


def _array_item_properties(
    properties: dict[str, object], name: str
) -> dict[str, object]:
    array = properties.get(name)
    if not isinstance(array, dict) or not isinstance(array.get("items"), dict):
        raise ValueError(f"curator schema must define {name} array items")
    item_properties = array["items"].get("properties")
    if not isinstance(item_properties, dict):
        raise ValueError(f"curator schema must define {name} item properties")
    return item_properties


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _truncate(value: str, limit: int) -> str:
    compact = " ".join(value.split())
    if len(compact) <= limit:
        return compact
    return compact[: max(0, limit - 1)].rstrip() + "…"


def _write_report(
    completed_at: datetime,
    digest: str,
    entries: tuple[_PackEntry, ...],
    records: list[_ReportVerdict],
) -> Path:
    reports_dir = resolve_paths().home / "reports" / "curator"
    reports_dir.mkdir(parents=True, exist_ok=True)
    timestamp = completed_at.strftime("%Y%m%dT%H%M%S.%fZ")
    path = reports_dir / f"{timestamp}.md"
    lines = [
        "# Curator pass report",
        "",
        f"- Completed: {completed_at.isoformat()}",
        f"- Full context incidents: {', '.join(str(entry.incident.id) for entry in entries)}",
        f"- Verdict records: {len(records)}",
        "",
        "## Verdicts",
        "",
    ]
    if not records:
        lines.append("- (none)")
    for record in records:
        reason = " ".join(record.reason.split())
        lines.append(
            f"- {record.kind} `{record.subject}` — **{record.verdict}** — "
            f"{reason} _[{record.status}]_"
        )
    lines.extend(["", "## Ledger digest shown", "", "```text", digest, "```", ""])
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
