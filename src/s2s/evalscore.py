"""Deterministic, ground-truth scorers for an :mod:`s2s.evalrun` record.

This module intentionally has no pipeline or LLM imports.  A score is a pure
function of a completed record and its corpus manifest, so failures remain
reproducible and every reported number can point back to its input facts.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
import json
from pathlib import Path
import tomllib
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_THRESHOLDS = PROJECT_ROOT / "evals" / "thresholds.toml"


class EvalScoreError(ValueError):
    """The record or manifest cannot support deterministic scoring."""


def load_thresholds(path: Path | None = None) -> dict[str, float]:
    """Load documented numeric threshold defaults, optionally overridden."""

    with (path or DEFAULT_THRESHOLDS).open("rb") as handle:
        document = tomllib.load(handle)
    values = document.get("thresholds")
    if not isinstance(values, dict) or not all(isinstance(v, (int, float)) for v in values.values()):
        raise EvalScoreError("thresholds.toml must contain a numeric [thresholds] table")
    return {str(key): float(value) for key, value in values.items()}


def score_record(
    record: Mapping[str, object], manifest: Mapping[str, object], *, thresholds: Mapping[str, float] | None = None
) -> dict[str, object]:
    """Return the complete machine-readable score card for one run record."""

    _require_sequence(manifest, "incidents")
    if record.get("mode") == "mock":
        return {
            "format_version": 1,
            "status": "withheld",
            "summary": "mode=mock: scores withheld",
            "metrics": [],
            # #43 appends judge metrics here without changing this schema.
            "judge": {"status": "pending", "metrics": []},
        }
    limits = dict(thresholds or load_thresholds())
    incidents = _incident_map(manifest)
    metrics: list[dict[str, object]] = []
    metrics.extend(_detection_metrics(record, manifest, incidents, limits))
    metrics.extend(_triage_metrics(record, manifest, incidents, limits))
    metrics.extend(_convergence_metrics(record, manifest, incidents, limits))
    metrics.extend(_spam_metrics(record, manifest, incidents, limits))
    metrics.extend(_stability_metrics(record, limits))
    metrics.extend(_invariant_metrics(record, limits))
    validate_evidence_pointers({"record": record, "manifest": manifest}, metrics)
    passed = all(bool(metric["pass"]) for metric in metrics)
    return {
        "format_version": 1,
        "status": "pass" if passed else "fail",
        "summary": "deterministic scores complete",
        "metrics": metrics,
        # Deliberate extension point owned by the judge issue.
        "judge": {"status": "pending", "metrics": []},
    }


def score_files(
    record_path: Path, manifest_path: Path, *, thresholds_path: Path | None = None
) -> dict[str, object]:
    """Score JSON inputs and atomically write ``scores.json`` beside the record."""

    record = _load_object(record_path)
    manifest = _load_object(manifest_path)
    scores = score_record(record, manifest, thresholds=load_thresholds(thresholds_path))
    output = record_path.with_name("scores.json")
    temporary = output.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(scores, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(output)
    return scores


def validate_evidence_pointers(inputs: Mapping[str, Mapping[str, object]], metrics: Sequence[Mapping[str, object]]) -> None:
    """Raise when a score's traceability pointer does not resolve to its input."""

    for metric in metrics:
        evidence = metric.get("evidence", [])
        if not isinstance(evidence, list):
            raise EvalScoreError(f"metric {metric.get('metric')!r} evidence is not a list")
        for pointer in evidence:
            if not isinstance(pointer, str) or ":" not in pointer:
                raise EvalScoreError(f"invalid evidence pointer {pointer!r}")
            root, path = pointer.split(":", 1)
            if root not in inputs or not path.startswith("/"):
                raise EvalScoreError(f"invalid evidence pointer {pointer!r}")
            _resolve_pointer(inputs[root], path)


def _metric(name: str, value: float, threshold: float, op: str, evidence: list[str], **details: object) -> dict[str, object]:
    passed = value >= threshold if op == ">=" else value <= threshold
    return {"metric": name, "value": round(value, 6), "threshold": threshold, "op": op, "pass": passed, "evidence": evidence, **details}


def _detection_metrics(record: Mapping[str, object], manifest: Mapping[str, object], incidents: Mapping[str, Mapping[str, object]], limits: Mapping[str, float]) -> list[dict[str, object]]:
    detections = _records(record, "detections")
    detected_ids = {str(item.get("corpus_incident_id")) for item in detections if item.get("corpus_incident_id") is not None}
    expected = [item for item in _incidents(manifest) if item.get("lexicon_hit_expected") is True]
    found = [item for item in expected if str(item["uuid"]) in detected_ids]
    known_misses = [item for item in _incidents(manifest) if item.get("lexicon_hit_expected") is False]
    detection_refs = _record_refs("detections", detections)
    expected_refs = _manifest_refs("incidents", _incidents(manifest), lambda item: item.get("lexicon_hit_expected") is True)
    scaffold = [item for item in detections if item.get("corpus_incident_id") not in incidents]
    return [
        _metric("detection_recall", _ratio(len(found), len(expected)), limits["detection_recall"], ">=", detection_refs + expected_refs, found=len(found), expected=len(expected)),
        _metric("lexicon_gap", _ratio(len(known_misses), len(_incidents(manifest))), 1.0, "<=", _manifest_refs("incidents", _incidents(manifest), lambda item: item.get("lexicon_hit_expected") is False), known_misses=[item["uuid"] for item in known_misses]),
        _metric("scaffold_filter_precision", _ratio(len(detections) - len(scaffold), len(detections)), limits["scaffold_filter_precision"], ">=", detection_refs, scaffold_detection_indexes=[index for index, item in enumerate(detections) if item.get("corpus_incident_id") not in incidents]),
    ]


def _triage_metrics(record: Mapping[str, object], manifest: Mapping[str, object], incidents: Mapping[str, Mapping[str, object]], limits: Mapping[str, float]) -> list[dict[str, object]]:
    verdicts = _records(record, "triage_verdicts")
    pairs = [(index, item, incidents[str(item["corpus_incident_id"])]) for index, item in enumerate(verdicts) if str(item.get("corpus_incident_id")) in incidents]
    tp = sum(item.get("authentic") is True and truth.get("authentic") is True for _, item, truth in pairs)
    predicted = sum(item.get("authentic") is True for _, item, _ in pairs)
    actual = sum(truth.get("authentic") is True for _, _, truth in pairs)
    named = [item for item in _incidents(manifest) if str(item.get("cluster_id")) not in {"singleton", "decoy"}]
    labels = {str(item.get("corpus_incident_id")): item.get("label") for item in verdicts}
    per_cluster = []
    for cluster in sorted({str(item["cluster_id"]) for item in named}):
        members = [item for item in named if item["cluster_id"] == cluster]
        assigned = [str(labels.get(str(item["uuid"]))) for item in members if labels.get(str(item["uuid"]))]
        modal, count = _modal(assigned)
        per_cluster.append({"cluster": cluster, "modal_label": modal, "agreement": _ratio(count, len(members)), "exact": modal == cluster})
    return [
        _metric("authenticity_precision", _ratio(tp, predicted), limits["authenticity_precision"], ">=", _record_refs("triage_verdicts", verdicts), true_positives=tp, predicted_authentic=predicted),
        _metric("authenticity_recall", _ratio(tp, actual), limits["authenticity_recall"], ">=", _record_refs("triage_verdicts", verdicts), true_positives=tp, authentic=actual),
        _metric("label_agreement", _mean([float(item["agreement"]) for item in per_cluster]), limits["label_agreement"], ">=", _record_refs("triage_verdicts", verdicts) + _manifest_refs("incidents", _incidents(manifest), lambda item: str(item.get("cluster_id")) not in {"singleton", "decoy"}), clusters=per_cluster),
        _metric("exact_label_agreement", _ratio(sum(bool(item["exact"]) for item in per_cluster), len(per_cluster)), limits["exact_label_agreement"], ">=", _record_refs("triage_verdicts", verdicts), clusters=per_cluster),
    ]


def _convergence_metrics(record: Mapping[str, object], manifest: Mapping[str, object], incidents: Mapping[str, Mapping[str, object]], limits: Mapping[str, float]) -> list[dict[str, object]]:
    verdicts = _records(record, "triage_verdicts")
    curator = record.get("curator") if isinstance(record.get("curator"), dict) else {}
    cluster_verdicts = curator.get("cluster_verdicts", []) if isinstance(curator, dict) else []
    curated_labels = {str(item.get("label")): str(item.get("verdict")) for item in cluster_verdicts if isinstance(item, dict)}
    triage = {str(item.get("corpus_incident_id")): item for item in verdicts}
    named = [item for item in _incidents(manifest) if str(item.get("cluster_id")) not in {"singleton", "decoy"}]
    breakdown = []
    for cluster in sorted({str(item["cluster_id"]) for item in named}):
        members = [item for item in named if item["cluster_id"] == cluster]
        assignments = [str(triage[str(item["uuid"])].get("label")) for item in members if str(item["uuid"]) in triage]
        modal, count = _modal(assignments)
        agreement = _ratio(count, len(members))
        scattered = [str(item["uuid"]) for item in members if str(triage.get(str(item["uuid"]), {}).get("label")) != modal]
        surfaced = bool(modal and curated_labels.get(modal) == "synthesize")
        breakdown.append({"cluster": cluster, "modal_label": modal, "members": [item["uuid"] for item in members], "scattered_members": scattered, "agreement": agreement, "surfaced_as_one_cluster": surfaced, "converged": agreement >= limits["label_agreement"] and surfaced})
    convergence = _ratio(sum(bool(item["converged"]) for item in breakdown), len(breakdown))
    labelled = [item for item in verdicts if item.get("label")]
    label_counts = Counter(str(item.get("label")) for item in labelled)
    singleton_ratio = _ratio(sum(count == 1 for count in label_counts.values()), len(label_counts))
    other_share = _ratio(label_counts.get("other", 0), len(labelled))
    expected_fast = [item for item in breakdown if len(item["members"]) >= 3]
    fast = [item for item in expected_fast if item["surfaced_as_one_cluster"]]
    refs = _record_refs("triage_verdicts", verdicts) + _record_refs("curator/cluster_verdicts", cluster_verdicts if isinstance(cluster_verdicts, list) else [])
    return [
        _metric("convergence", convergence, limits["convergence"], ">=", refs, clusters=breakdown),
        _metric("singleton_ratio", singleton_ratio, limits["singleton_ratio_max"], "<=", _record_refs("triage_verdicts", verdicts), expected_singletons=sum(item.get("cluster_id") == "singleton" for item in _incidents(manifest))),
        _metric("recurrence_fast_track_recall", _ratio(len(fast), len(expected_fast)), limits["fast_track_recall"], ">=", refs, fast_tracked=[item["cluster"] for item in fast]),
        _metric("other_share", other_share, limits["other_share_max"], "<=", _record_refs("triage_verdicts", verdicts)),
    ]


def _spam_metrics(record: Mapping[str, object], manifest: Mapping[str, object], incidents: Mapping[str, Mapping[str, object]], limits: Mapping[str, float]) -> list[dict[str, object]]:
    proposals = _records(record, "proposals")
    proposal_ids = {int(value) for proposal in proposals for value in proposal.get("evidence_incident_ids", []) if isinstance(value, int)}
    by_uuid = {
        str(item.get("corpus_incident_id")): item
        for item in _records(record, "triage_verdicts")
        if isinstance(item.get("incident_id"), int) and item.get("corpus_incident_id") is not None
    }
    bad = [item for item in _incidents(manifest) if not item.get("authentic") or not item.get("remedy_worthy")]
    bad_ids = {int(by_uuid[str(item["uuid"])]["incident_id"]) for item in bad if str(item["uuid"]) in by_uuid}
    clean = not (proposal_ids & bad_ids)
    authentic_worthy = [item for item in _incidents(manifest) if item.get("authentic") and item.get("remedy_worthy")]
    near = [item for item in _incidents(manifest) if item.get("pre_existing_remedy_id")]
    dedup_hits = 0
    for item in near:
        row = by_uuid.get(str(item["uuid"]))
        if row and int(row["incident_id"]) not in proposal_ids:
            dedup_hits += 1
            continue
        matching = [proposal for proposal in proposals if row and int(row["incident_id"]) in proposal.get("evidence_incident_ids", [])]
        if matching and any(str(proposal.get("revises") or "") == str(item.get("pre_existing_remedy_id")) or _has_dedup_action(proposal) for proposal in matching):
            dedup_hits += 1
    expected_shapes = Counter(str(item.get("expected_remedy_shape")) for item in authentic_worthy)
    routed = Counter(str(proposal.get("remedy_type")) for proposal in proposals)
    confusion = {shape: {actual: count for actual, count in routed.items()} for shape in sorted(expected_shapes)}
    refs = _record_refs("proposals", proposals) + _record_refs("triage_verdicts", list(by_uuid.values()))
    return [
        _metric("spam_precision", 1.0 if clean else 0.0, limits["spam_precision"], ">=", refs, prohibited_proposal_incident_ids=sorted(proposal_ids & bad_ids)),
        _metric("remedies_per_authentic_incident", _ratio(len(proposals), len(authentic_worthy)), 1.0, "<=", _record_refs("proposals", proposals), proposals=len(proposals), authentic_worthy=len(authentic_worthy)),
        _metric("dedup_catch_rate", _ratio(dedup_hits, len(near)), limits["dedup_catch_rate"], ">=", refs, caught=dedup_hits, expected=len(near)),
        _metric("claude_md_dominance", _ratio(routed.get("claude-md", 0), len(proposals)), limits["claude_md_share"], ">=", _record_refs("proposals", proposals), routing_confusion=confusion, expected_shapes=dict(expected_shapes), actual_shapes=dict(routed)),
    ]


def _stability_metrics(record: Mapping[str, object], limits: Mapping[str, float]) -> list[dict[str, object]]:
    repeats = record.get("repeats")
    if not isinstance(repeats, list) or len(repeats) < 2:
        return [_metric(name, 0.0, limits[key], "<=", ["record:/mode"], unavailable=True) for name, key in (("stability_label_flip_rate", "stability_label_flip_rate_max"), ("stability_authenticity_flip_rate", "stability_authenticity_flip_rate_max"), ("stability_schema_retry_rate", "stability_schema_retry_rate_max"))]
    runs = [item for item in repeats if isinstance(item, dict)]
    label_flips = _flip_rate(runs, "label")
    authenticity_flips = _flip_rate(runs, "authentic")
    retry_by_stage: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for run in runs:
        validation = run.get("schema_validation", {})
        if not isinstance(validation, dict):
            continue
        requests = validation.get("requests", {})
        retries = validation.get("retries", {})
        if not isinstance(requests, dict) or not isinstance(retries, dict):
            continue
        for stage, count in requests.items():
            if isinstance(count, int):
                retry_by_stage[str(stage)][1] += count
        for stage, count in retries.items():
            if isinstance(count, int):
                retry_by_stage[str(stage)][0] += count
    per_stage = {
        stage: _ratio(values[0], values[1]) for stage, values in sorted(retry_by_stage.items())
    }
    return [
        _metric("stability_label_flip_rate", label_flips, limits["stability_label_flip_rate_max"], "<=", [f"record:/repeats/{i}" for i in range(len(runs))]),
        _metric("stability_authenticity_flip_rate", authenticity_flips, limits["stability_authenticity_flip_rate_max"], "<=", [f"record:/repeats/{i}" for i in range(len(runs))]),
        _metric("stability_schema_retry_rate", max(per_stage.values(), default=0.0), limits["stability_schema_retry_rate_max"], "<=", [f"record:/repeats/{i}" for i in range(len(runs))], per_stage=per_stage),
    ]


def _invariant_metrics(record: Mapping[str, object], limits: Mapping[str, float]) -> list[dict[str, object]]:
    cycles = record.get("cycles")
    if not isinstance(cycles, list) or not cycles:
        return [_metric("pipeline_invariants", 1.0, limits["invariants"], ">=", ["record:/mode"], unavailable=True)]
    valid = all(bool(item.get("idempotent")) and bool(item.get("state_machine_integrity")) and bool(item.get("on_new_economics")) for item in cycles if isinstance(item, dict))
    return [_metric("pipeline_invariants", 1.0 if valid else 0.0, limits["invariants"], ">=", [f"record:/cycles/{i}" for i in range(len(cycles))], cycles=cycles)]


def _flip_rate(runs: Sequence[Mapping[str, object]], key: str) -> float:
    rows = [{str(item.get("corpus_incident_id")): item.get(key) for item in _records(run, "triage_verdicts")} for run in runs]
    common = set.intersection(*(set(row) for row in rows)) if rows else set()
    comparisons = [(identifier, [row[identifier] for row in rows]) for identifier in common]
    return _ratio(sum(len(set(values)) > 1 for _, values in comparisons), len(comparisons))


def _has_dedup_action(proposal: Mapping[str, object]) -> bool:
    values = proposal.get("dedup_verdict", [])
    return isinstance(values, list) and any(isinstance(item, dict) and item.get("verdict") in {"revision", "revise", "clear"} for item in values)


def _records(record: Mapping[str, object], key: str) -> list[dict[str, Any]]:
    current: object = record
    for part in key.split("/"):
        current = current.get(part, []) if isinstance(current, dict) else []
    return [item for item in current if isinstance(item, dict)] if isinstance(current, list) else []


def _incidents(manifest: Mapping[str, object]) -> list[dict[str, Any]]:
    return _records(manifest, "incidents")


def _incident_map(manifest: Mapping[str, object]) -> dict[str, Mapping[str, object]]:
    return {str(item["uuid"]): item for item in _incidents(manifest)}


def _record_refs(key: str, values: Sequence[object]) -> list[str]:
    return [f"record:/{key}/{index}" for index in range(len(values))]


def _manifest_refs(key: str, values: Sequence[Mapping[str, object]], predicate: Any) -> list[str]:
    return [f"manifest:/{key}/{index}" for index, item in enumerate(values) if predicate(item)]


def _modal(values: Sequence[str]) -> tuple[str | None, int]:
    if not values:
        return None, 0
    counts = Counter(values)
    value, count = min(counts.items(), key=lambda item: (-item[1], item[0]))
    return value, count


def _ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _load_object(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise EvalScoreError(f"cannot read {path}: {error}") from error
    if not isinstance(value, dict):
        raise EvalScoreError(f"{path} must contain a JSON object")
    return value


def _require_sequence(value: Mapping[str, object], key: str) -> None:
    if not isinstance(value.get(key), list):
        raise EvalScoreError(f"manifest missing {key!r} list")


def _resolve_pointer(value: object, pointer: str) -> object:
    current = value
    for part in pointer.removeprefix("/").split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        elif isinstance(current, dict) and part in current:
            current = current[part]
        else:
            raise EvalScoreError(f"evidence pointer does not resolve: {pointer}")
    return current
