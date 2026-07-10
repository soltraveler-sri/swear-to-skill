"""Human-readable, offline reports for completed synthetic eval runs."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from html import escape
import json
from pathlib import Path
from typing import Literal

from .evalscore import DEFAULT_THRESHOLDS


MAX_QUOTE_CHARS = 280
HONEST_LIMITS = (
    "These results measure a synthetic corpus. They do not certify performance "
    "on any individual's real transcripts; live-mode scores can vary as model "
    "versions change. The corpus is a public target, so prompt overfitting is a "
    "known risk; use held-out, versioned corpora when comparing changes."
)


class EvalReportError(ValueError):
    """An eval artifact cannot be rendered into an honest report."""


@dataclass(frozen=True)
class ReportResult:
    """Paths and gate result emitted beside one eval record."""

    markdown_path: Path
    html_path: Path
    verdict: Literal["PASS", "FAIL", "PARTIAL", "WIRING CHECK ONLY"]


@dataclass(frozen=True)
class MatrixReportResult:
    """Self-contained comparative artifacts written at a matrix-run root."""

    markdown_path: Path
    html_path: Path


@dataclass(frozen=True)
class Excerpt:
    """One evidence-backed incident chain included in the report."""

    category: str
    incident_id: int | None
    evidence: tuple[str, ...]
    lines: tuple[tuple[str, str], ...]


def write_report(record_path: Path, *, thresholds_path: Path | None = None) -> ReportResult:
    """Write ``report.md`` and fully self-contained ``report.html`` beside a record."""

    record = _load_object(record_path)
    scores = _load_object(record_path.with_name("scores.json"))
    judge_path = record_path.with_name("judge-results.json")
    judge = _load_object(judge_path) if judge_path.exists() else {}
    threshold_file = (thresholds_path or DEFAULT_THRESHOLDS).resolve()
    override = thresholds_path is not None and threshold_file != DEFAULT_THRESHOLDS.resolve()
    verdict = greenlight_verdict(record, scores, judge)
    excerpts = pick_excerpts(record, scores, judge)
    spotlights = failure_spotlights(record, scores, judge)
    markdown = render_markdown(
        record, scores, judge, verdict, excerpts, spotlights, threshold_file, override
    )
    html = render_html(record, scores, judge, verdict, excerpts, spotlights, threshold_file, override)
    markdown_path = record_path.with_name("report.md")
    html_path = record_path.with_name("report.html")
    markdown_path.write_text(markdown, encoding="utf-8")
    html_path.write_text(html, encoding="utf-8")
    return ReportResult(markdown_path, html_path, verdict)


def write_matrix_report(
    root: Path,
    arms: Sequence[tuple[object, Path]],
    *,
    default_corpus: Path,
) -> MatrixReportResult:
    """Compare same-corpus arm records without hiding cost or judge differences."""

    if len(arms) < 2:
        raise EvalReportError("comparative report needs at least two arms")
    loaded = []
    for profile, record_path in arms:
        record = _load_object(record_path)
        scores = _load_object(record_path.with_name("scores.json"))
        judge_path = record_path.with_name("judge-results.json")
        judge = _load_object(judge_path) if judge_path.exists() else {}
        name = str(getattr(profile, "name", record.get("profile", "unknown")))
        loaded.append((name, profile, record, scores, judge))
    corpus_versions = {str(record.get("corpus_version")) for _, _, record, _, _ in loaded}
    if len(corpus_versions) != 1:
        raise EvalReportError("matrix arms did not use the same corpus version")
    markdown = _matrix_markdown(loaded, default_corpus=default_corpus)
    html = _matrix_html(markdown)
    markdown_path = root / "matrix-report.md"
    html_path = root / "matrix-report.html"
    markdown_path.write_text(markdown, encoding="utf-8")
    html_path.write_text(html, encoding="utf-8")
    return MatrixReportResult(markdown_path, html_path)


def _matrix_markdown(arms: Sequence[tuple[str, object, Mapping[str, object], Mapping[str, object], Mapping[str, object]]], *, default_corpus: Path) -> str:
    names = [name for name, *_ in arms]
    baseline = names[0]
    lines = ["# swear-to-skill A/B experiment matrix", "", f"Baseline arm: **{baseline}**", "", "## What changed", ""]
    for name, profile, _, _, _ in arms:
        lines.append(f"- **{name}** — {_profile_diff(profile, arms[0][1])}")
    lines.extend(["", "## Metrics", "", "| Metric | " + " | ".join(names) + " |", "| --- | " + " | ".join("---:" for _ in names) + " |"])
    metric_names = sorted({str(row.get("metric")) for _, _, _, scores, _ in arms for row in _rows(scores, "metrics")})
    for metric in metric_names:
        values = [_metric_value(scores, metric) for _, _, _, scores, _ in arms]
        lines.append("| " + metric + " | " + " | ".join(_with_delta(value, values[0]) for value in values) + " |")
    lines.extend(["", "## Cost, duration, and stability", "", "| Measure | " + " | ".join(names) + " |", "| --- | " + " | ".join("---:" for _ in names) + " |"])
    costs = [_run_cost(record) for _, _, record, _, _ in arms]
    durations = [_run_duration(record) for _, _, record, _, _ in arms]
    stability = [_stability(judge) for _, _, _, _, judge in arms]
    for label, values, suffix in (("Cost", costs, " USD"), ("Duration", durations, " ms"), ("Judge quality variance (lower is stabler)", stability, "")):
        lines.append("| " + label + " | " + " | ".join(_with_delta(value, values[0], suffix=suffix) for value in values) + " |")
    lines.extend(["", "## Judge disclosure", ""])
    for name, _, record, _, judge in arms:
        config = judge.get("judge_config", {}) if isinstance(judge, Mapping) else {}
        model = config.get("model", record.get("stage_config", {}).get("judge", {}).get("model", "unknown")) if isinstance(config, Mapping) else "unknown"
        prompt = config.get("prompt_version", "unknown") if isinstance(config, Mapping) else "unknown"
        lines.append(f"- **{name}**: judge model `{model}`, prompt `{prompt}`.")
    if all(_is_default_corpus(record, default_corpus) for _, _, record, _, _ in arms):
        lines.extend(["", "## Overfitting hazard", "", "All arms used the default public synthetic corpus. Treat prompt wins as provisional; rerun this matrix with `--corpus <held-out-version>` before drawing a product conclusion."])
    lines.extend(["", "## Honest limits", "", HONEST_LIMITS, ""])
    return "\n".join(lines)


def _profile_diff(profile: object, baseline: object) -> str:
    stages = ("triage", "curate", "synthesize", "judge")
    changes = []
    for stage in stages:
        current = getattr(profile, stage, None)
        first = getattr(baseline, stage, None)
        if current != first:
            changes.append(f"{stage}={getattr(current, 'model', '?')}/{getattr(current, 'prompt_version', '?')}/{getattr(current, 'effort', None) or 'default effort'}")
    return "; ".join(changes) if changes else "identical declared LLM settings"


def _metric_value(scores: Mapping[str, object], name: str) -> float:
    row = next((row for row in _rows(scores, "metrics") if row.get("metric") == name), None)
    return float(row.get("value", 0.0)) if row is not None else 0.0


def _run_cost(record: Mapping[str, object]) -> float:
    return sum(
        float(value)
        for row in _rows(record, "llm_run_log")
        if isinstance((value := row.get("cost_usd")), (int, float))
    )


def _run_duration(record: Mapping[str, object]) -> float:
    return sum(float(row.get("duration_ms", 0.0)) for row in _rows(record, "llm_run_log"))


def _stability(judge: Mapping[str, object]) -> float:
    metrics = judge.get("metrics", {})
    return float(metrics.get("quality_variance_mean", 0.0)) if isinstance(metrics, Mapping) else 0.0


def _with_delta(value: float, baseline: float, *, suffix: str = "") -> str:
    delta = value - baseline
    return f"{value:.3f}{suffix} ({delta:+.3f}{suffix})"


def _is_default_corpus(record: Mapping[str, object], default_corpus: Path) -> bool:
    # The v1 version is the public default; the resolved path remains available
    # to callers that use an alternate corpus with a coincidentally different ID.
    return str(record.get("corpus_version")) == default_corpus.name


def _matrix_html(markdown: str) -> str:
    """Keep the comparative HTML offline and readable without a renderer dependency."""

    return f"""<!doctype html><html><head><meta charset=\"utf-8\"><title>swear-to-skill A/B matrix</title><style>body{{max-width:980px;margin:40px auto;padding:0 20px;background:#181715;color:#eee8dc;font:15px/1.55 system-ui}}pre{{white-space:pre-wrap;background:#24221f;padding:24px;border-radius:10px}} </style></head><body><pre>{escape(markdown)}</pre></body></html>"""


def greenlight_verdict(
    record: Mapping[str, object], scores: Mapping[str, object], judge: Mapping[str, object]
) -> Literal["PASS", "FAIL", "PARTIAL", "WIRING CHECK ONLY"]:
    """Map available artifacts to the CI gate while keeping mock runs non-verdicts."""

    if record.get("mode") == "mock" or scores.get("status") == "withheld":
        return "WIRING CHECK ONLY"
    if record.get("status") != "complete" or not _full_pipeline_requested(record):
        return "PARTIAL"
    if scores.get("status") == "fail" or judge.get("status") == "failed":
        return "FAIL"
    if scores.get("status") == "pass" and judge.get("status") == "complete":
        return "PASS"
    return "PARTIAL"


def exit_code_for_verdict(verdict: str) -> int:
    """Return the documented shell exit code for a rendered report verdict."""

    return {"PASS": 0, "FAIL": 1, "PARTIAL": 2, "WIRING CHECK ONLY": 0}[verdict]


def pick_excerpts(
    record: Mapping[str, object], scores: Mapping[str, object], judge: Mapping[str, object]
) -> tuple[Excerpt, ...]:
    """Choose three to five categories when their record evidence exists."""

    triage = _rows(record, "triage_verdicts")
    curations = _rows(record, "curator/incident_verdicts")
    clusters = _rows(record, "curator/cluster_verdicts")
    proposals = _rows(record, "proposals")
    by_incident = _by_int(triage, "incident_id")
    selected: list[tuple[str, int]] = []

    synthesized = {str(row.get("label")) for row in clusters if row.get("verdict") == "synthesize"}
    converged = next(
        (row for row in triage if str(row.get("label")) in synthesized and _int(row.get("incident_id")) is not None),
        None,
    )
    if converged is not None:
        selected.append(("Converged-cluster member", int(converged["incident_id"])))

    singleton = next(
        (row for row in curations if row.get("verdict") == "promote" and row.get("singleton") is True),
        None,
    )
    if singleton is not None and _int(singleton.get("incident_id")) is not None:
        selected.append(("Promoted singleton", int(singleton["incident_id"])))

    dropped = next(
        (
            row
            for row in curations
            if row.get("verdict") == "dismiss"
            and _int(row.get("incident_id")) in by_incident
            and by_incident[int(row["incident_id"])].get("authentic") is False
        ),
        None,
    )
    if dropped is not None:
        selected.append(("Correctly-dropped decoy", int(dropped["incident_id"])))

    dedup = next(
        (
            proposal
            for proposal in proposals
            if proposal.get("revises") is not None
            or any(str(item.get("verdict")) in {"revision", "revise"} for item in _rows_from(proposal.get("dedup_verdict")))
        ),
        None,
    )
    if dedup is not None:
        ids = _int_list(dedup.get("evidence_incident_ids"))
        if ids:
            selected.append(("Dedup catch", ids[0]))

    worst = _worst_failure_incident(record, scores)
    if worst is not None:
        selected.append(("Worst failure", worst))

    # A report is most useful with several readable chains. Fill only from real
    # triage evidence, never invented summaries, and keep every incident unique.
    used = {incident_id for _, incident_id in selected}
    for row in triage:
        incident_id = _int(row.get("incident_id"))
        if incident_id is not None and incident_id not in used:
            selected.append(("Representative incident", incident_id))
            used.add(incident_id)
        if len(selected) >= 5:
            break
    return tuple(_excerpt(record, judge, category, incident_id) for category, incident_id in selected[:5])


def failure_spotlights(
    record: Mapping[str, object], scores: Mapping[str, object], judge: Mapping[str, object]
) -> tuple[tuple[Mapping[str, object], Excerpt | None], ...]:
    """Attach a concrete traceable excerpt to each below-threshold metric."""

    failures = [row for row in _rows(scores, "metrics") if row.get("pass") is False]
    return tuple(
        (metric, _excerpt(record, judge, "Failure spotlight", incident_id) if incident_id is not None else None)
        for metric in failures
        for incident_id in [_incident_for_metric(record, metric)]
    )


def render_markdown(
    record: Mapping[str, object],
    scores: Mapping[str, object],
    judge: Mapping[str, object],
    verdict: str,
    excerpts: Sequence[Excerpt],
    spotlights: Sequence[tuple[Mapping[str, object], Excerpt | None]],
    thresholds_file: Path,
    override: bool,
) -> str:
    """Return a concise plain Markdown counterpart to the local HTML report."""

    lines = ["# swear-to-skill greenlight report", "", _markdown_banner(verdict), "", "## Run", ""]
    lines.extend(_metadata_lines(record, judge, thresholds_file, override))
    if verdict == "WIRING CHECK ONLY":
        lines.extend(["", "## Scores", "", "Scores withheld: this mock run checks wiring only; it is not an evaluation."])
    else:
        lines.extend(["", "## Thresholds", "", "| Metric | Value | Threshold | Verdict |", "| --- | ---: | ---: | --- |"])
        for metric in _rows(scores, "metrics"):
            lines.append(
                f"| {metric.get('metric', 'unknown')} | {_number(metric.get('value'))} | "
                f"{metric.get('op', '')} {_number(metric.get('threshold'))} | "
                f"{_metric_verdict(metric)} |"
            )
        lines.extend(_markdown_convergence_components(scores))
    lines.extend(_markdown_excerpts("Narrative excerpts", excerpts))
    if spotlights:
        lines.extend(["", "## Failure spotlights"])
        for metric, excerpt in spotlights:
            lines.extend(["", f"### {metric.get('metric', 'unknown')}"])
            if excerpt is None:
                lines.append("No concrete incident pointer was available; no excerpt was fabricated.")
            else:
                lines.extend(_excerpt_markdown(excerpt))
    lines.extend(["", "## Honest limits", "", HONEST_LIMITS, ""])
    return "\n".join(lines)


def render_html(
    record: Mapping[str, object],
    scores: Mapping[str, object],
    judge: Mapping[str, object],
    verdict: str,
    excerpts: Sequence[Excerpt],
    spotlights: Sequence[tuple[Mapping[str, object], Excerpt | None]],
    thresholds_file: Path,
    override: bool,
) -> str:
    """Return a complete offline document with no external assets or references."""

    score_section = _html_scores(scores, verdict)
    convergence_section = _html_convergence_components(scores, verdict)
    excerpt_section = "".join(_html_excerpt(excerpt) for excerpt in excerpts) or "<p class=muted>No traceable excerpts were available.</p>"
    spotlight_section = ""
    if spotlights:
        blocks = []
        for metric, excerpt in spotlights:
            title = escape(str(metric.get("metric", "unknown")))
            body = _html_excerpt(excerpt) if excerpt else "<p class=note>No concrete incident pointer was available; no excerpt was fabricated.</p>"
            blocks.append(f"<article class=spotlight><h3>{title}</h3>{body}</article>")
        spotlight_section = f"<section><h2>Failure spotlights</h2>{''.join(blocks)}</section>"
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>swear-to-skill · greenlight report</title>
<style>
:root {{ color-scheme:dark; --ink:#eee8dc; --muted:#aaa294; --paper:#181715; --panel:#24221f; --line:#393630; --pass:#7fb9a3; --fail:#dd7658; --partial:#e6a54b; }}
* {{ box-sizing:border-box }} body {{ margin:0; background:var(--paper); color:var(--ink); font:15px/1.5 ui-sans-serif,system-ui,sans-serif }} main {{ max-width:1040px; margin:auto; padding:46px 24px }} h1 {{ font:500 clamp(2rem,5vw,4rem)/1.05 ui-serif,Georgia,serif; letter-spacing:-.05em; margin:4px 0 12px }} h2 {{ margin:46px 0 15px; font-size:.8rem; letter-spacing:.12em; text-transform:uppercase; color:var(--muted) }} h3 {{ margin:0 0 10px; font-size:1rem }} .eyebrow,.muted,.evidence,.note {{ color:var(--muted) }} .eyebrow {{ font-size:.76rem; letter-spacing:.12em; text-transform:uppercase }} .banner {{ padding:17px 19px; border:1px solid var(--line); border-left-width:6px; border-radius:10px; font-weight:700; letter-spacing:.04em }} .pass {{ border-left-color:var(--pass) }} .fail {{ border-left-color:var(--fail) }} .partial,.wiring {{ border-left-color:var(--partial) }} .card,.spotlight {{ padding:18px; margin:12px 0; border:1px solid var(--line); border-radius:10px; background:var(--panel) }} .chain {{ display:grid; gap:9px }} .chain dt {{ color:var(--muted); font-size:.75rem; text-transform:uppercase; letter-spacing:.08em }} .chain dd {{ margin:0 }} q {{ color:var(--ink) }} table {{ width:100%; border-collapse:collapse; font-variant-numeric:tabular-nums }} th,td {{ padding:9px 7px; border-bottom:1px solid var(--line); text-align:left }} th {{ color:var(--muted); font-size:.75rem; text-transform:uppercase; letter-spacing:.06em }} td:nth-child(n+2),th:nth-child(n+2) {{ text-align:right }} .metric-pass {{ color:var(--pass) }} .metric-fail {{ color:var(--fail) }} .metric-info {{ color:var(--partial) }} footer {{ margin-top:50px; padding-top:18px; border-top:1px solid var(--line); color:var(--muted); font-size:.9rem }} @media(max-width:640px) {{ main {{ padding:30px 16px }} table {{ font-size:.82rem }} }}
</style>
</head>
<body><main>
<header><div class="eyebrow">swear-to-skill / synthetic eval</div><h1>Greenlight report</h1><div class="banner {_banner_class(verdict)}">{escape(_banner_text(verdict))}</div><p class="muted">{escape(_metadata_summary(record, judge, thresholds_file, override))}</p></header>
<section><h2>Run metadata</h2><div class="card">{_html_metadata(record, judge, thresholds_file, override)}</div></section>
{score_section}
{convergence_section}
<section><h2>Narrative excerpts</h2>{excerpt_section}</section>
{spotlight_section}
<footer><strong>Honest limits.</strong> {escape(HONEST_LIMITS)}</footer>
</main></body></html>
"""


def _excerpt(record: Mapping[str, object], judge: Mapping[str, object], category: str, incident_id: int) -> Excerpt:
    contexts = record.get("context_packs")
    context = contexts.get(str(incident_id)) if isinstance(contexts, Mapping) else None
    triage = next((row for row in _rows(record, "triage_verdicts") if _int(row.get("incident_id")) == incident_id), None)
    curated = next((row for row in _rows(record, "curator/incident_verdicts") if _int(row.get("incident_id")) == incident_id), None)
    proposal = next((row for row in _rows(record, "proposals") if incident_id in _int_list(row.get("evidence_incident_ids"))), None)
    lines: list[tuple[str, str]] = []
    evidence: list[str] = []
    if isinstance(context, Mapping):
        evidence.append(f"record:/context_packs/{incident_id}")
        trigger = context.get("frustrated_message")
        if isinstance(trigger, str) and trigger:
            lines.append(("Trigger found", _quote(trigger)))
        else:
            lines.append(("Trigger found", "Omitted: context pack has no quoted trigger."))
        summary = _context_summary(context)
        if summary:
            lines.append(("Context extracted", summary))
    else:
        lines.append(("Trigger and context", "Omitted: record:/context_packs pointer is unavailable."))
    if triage is not None:
        index = _rows(record, "triage_verdicts").index(triage)
        evidence.append(f"record:/triage_verdicts/{index}")
        lines.append(("Triage", _triage_text(triage)))
    else:
        lines.append(("Triage", "Omitted: no traceable triage verdict."))
    if curated is not None:
        index = _rows(record, "curator/incident_verdicts").index(curated)
        evidence.append(f"record:/curator/incident_verdicts/{index}")
        lines.append(("Curation", _curation_text(curated)))
    else:
        lines.append(("Curation", "Omitted: no traceable curation outcome."))
    if proposal is not None:
        index = _rows(record, "proposals").index(proposal)
        evidence.append(f"record:/proposals/{index}")
        lines.append(("Remedy", _remedy_text(proposal)))
        judge_line = _judge_text(judge, _int(proposal.get("proposal_id")), incident_id)
        if judge_line:
            evidence.append(f"judge-results:/proposals/{_judge_proposal_index(judge, _int(proposal.get('proposal_id')))}")
            lines.append(("Judge", judge_line))
        elif judge.get("status") == "skipped":
            lines.append(("Judge", "Omitted: judge is intentionally skipped for mock mode."))
        else:
            lines.append(("Judge", "Omitted: no traceable judge verdict for this evidence."))
    else:
        lines.append(("Remedy and judge", "Omitted: no proposal evidence pointer includes this incident."))
    return Excerpt(category, incident_id, tuple(evidence), tuple(lines))


def _incident_for_metric(record: Mapping[str, object], metric: Mapping[str, object]) -> int | None:
    for pointer in metric.get("evidence", []) if isinstance(metric.get("evidence"), list) else []:
        if not isinstance(pointer, str) or not pointer.startswith("record:"):
            continue
        resolved = _resolve_record_pointer(record, pointer.removeprefix("record:"))
        if isinstance(resolved, Mapping):
            incident_id = _int(resolved.get("incident_id"))
            if incident_id is not None:
                return incident_id
            ids = _int_list(resolved.get("evidence_incident_ids"))
            if ids:
                return ids[0]
    for key in ("prohibited_proposal_incident_ids", "scaffold_detection_indexes"):
        ids = _int_list(metric.get(key))
        if ids:
            return ids[0]
    return None


def _worst_failure_incident(record: Mapping[str, object], scores: Mapping[str, object]) -> int | None:
    failure = next((metric for metric in _rows(scores, "metrics") if metric.get("pass") is False), None)
    return _incident_for_metric(record, failure) if failure is not None else None


def _full_pipeline_requested(record: Mapping[str, object]) -> bool:
    requested = record.get("stages_requested")
    return isinstance(requested, list) and {"scan", "triage", "curate", "synthesize"}.issubset(set(requested))


def _metadata_lines(record: Mapping[str, object], judge: Mapping[str, object], thresholds_file: Path, override: bool) -> list[str]:
    models = _models(record)
    return [
        f"- Mode: {record.get('mode', 'unknown')}",
        f"- Corpus: {record.get('corpus_version', 'unknown')}",
        f"- Profile: {record.get('profile', 'default')}",
        f"- Models: {models or 'not recorded'}",
        f"- Cost: {_cost(record)}",
        f"- Duration: {_duration(record)}",
        f"- Repeat-N: {_repeat_count(record)}",
        f"- Thresholds: {thresholds_file} ({'user override' if override else 'defaults'})",
        f"- Judge: {judge.get('status', 'not available')}",
    ]


def _html_metadata(record: Mapping[str, object], judge: Mapping[str, object], thresholds_file: Path, override: bool) -> str:
    return "<br>".join(escape(line.removeprefix("- ")) for line in _metadata_lines(record, judge, thresholds_file, override))


def _metadata_summary(record: Mapping[str, object], judge: Mapping[str, object], thresholds_file: Path, override: bool) -> str:
    return f"mode={record.get('mode', 'unknown')} · corpus={record.get('corpus_version', 'unknown')} · profile={record.get('profile', 'default')} · thresholds={'override' if override else 'defaults'}"


def _html_scores(scores: Mapping[str, object], verdict: str) -> str:
    if verdict == "WIRING CHECK ONLY":
        return "<section><h2>Scores</h2><div class=card><strong>Scores withheld.</strong> This mock run checks wiring only; it is not an evaluation.</div></section>"
    rows = "".join(
        f"<tr><td>{escape(str(metric.get('metric', 'unknown')))}</td><td>{escape(_number(metric.get('value')))}</td><td>{escape(str(metric.get('op', '')))} {escape(_number(metric.get('threshold')))}</td><td class={_metric_class(metric)}>{_metric_verdict(metric)}</td></tr>"
        for metric in _rows(scores, "metrics")
    )
    return f"<section><h2>Thresholds</h2><div class=card><table><thead><tr><th>Metric</th><th>Value</th><th>Threshold</th><th>Verdict</th></tr></thead><tbody>{rows}</tbody></table></div></section>"


def _convergence_clusters(scores: Mapping[str, object]) -> list[Mapping[str, object]]:
    metric = next((row for row in _rows(scores, "metrics") if row.get("metric") == "convergence"), None)
    clusters = metric.get("clusters") if isinstance(metric, Mapping) else None
    return [item for item in clusters if isinstance(item, Mapping)] if isinstance(clusters, list) else []


def _markdown_convergence_components(scores: Mapping[str, object]) -> list[str]:
    clusters = _convergence_clusters(scores)
    if not any("post_curation_unified" in cluster for cluster in clusters):
        return []
    lines = ["", "## Convergence composition", "", "| Cluster | Triage-label share | Post-curation unification | Verdict |", "| --- | ---: | --- | --- |"]
    for cluster in clusters:
        unified = bool(cluster.get("post_curation_unified"))
        clean = bool(cluster.get("post_curation_clean"))
        group = cluster.get("promotion_group_id")
        contamination = ", ".join(str(value) for value in _int_list(cluster.get("contaminating_incident_ids")))
        cleanliness = "clean" if clean else f"contaminated: {contamination or 'unknown'}"
        unification = "not unified" if not unified else f"proposal {group}; {cleanliness}"
        lines.append(
            f"| {cluster.get('cluster', 'unknown')} | {_number(cluster.get('triage_label_share'))} | "
            f"{unification} | {'converged' if cluster.get('converged') else 'not converged'} |"
        )
    return lines


def _html_convergence_components(scores: Mapping[str, object], verdict: str) -> str:
    if verdict == "WIRING CHECK ONLY":
        return ""
    clusters = _convergence_clusters(scores)
    if not any("post_curation_unified" in cluster for cluster in clusters):
        return ""
    rows = []
    for cluster in clusters:
        unified = bool(cluster.get("post_curation_unified"))
        clean = bool(cluster.get("post_curation_clean"))
        group = cluster.get("promotion_group_id")
        contamination = ", ".join(str(value) for value in _int_list(cluster.get("contaminating_incident_ids")))
        cleanliness = "clean" if clean else f"contaminated: {contamination or 'unknown'}"
        unification = "not unified" if not unified else f"proposal {group}; {cleanliness}"
        rows.append(
            "<tr>"
            f"<td>{escape(str(cluster.get('cluster', 'unknown')))}</td>"
            f"<td>{escape(_number(cluster.get('triage_label_share')))}</td>"
            f"<td>{escape(unification)}</td>"
            f"<td>{'converged' if cluster.get('converged') else 'not converged'}</td>"
            "</tr>"
        )
    return "<section><h2>Convergence composition</h2><div class=card><table><thead><tr><th>Cluster</th><th>Triage-label share</th><th>Post-curation unification</th><th>Verdict</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table></div></section>"


def _metric_verdict(metric: Mapping[str, object]) -> str:
    if metric.get("excluded") is True:
        return f"INFO · {metric.get('note', 'excluded')}"
    if metric.get("flag") == "small-sample":
        return "INFO · small-sample"
    return "PASS" if metric.get("pass") else "FAIL"


def _metric_class(metric: Mapping[str, object]) -> str:
    if metric.get("excluded") is True:
        return "metric-info"
    if metric.get("flag") == "small-sample":
        return "metric-info"
    return "metric-pass" if metric.get("pass") else "metric-fail"


def _markdown_excerpts(title: str, excerpts: Sequence[Excerpt]) -> list[str]:
    lines = ["", f"## {title}"]
    for excerpt in excerpts:
        lines.extend(["", f"### {excerpt.category}"])
        lines.extend(_excerpt_markdown(excerpt))
    return lines


def _excerpt_markdown(excerpt: Excerpt) -> list[str]:
    lines = [f"- {label}: {value}" for label, value in excerpt.lines]
    evidence = ", ".join(excerpt.evidence) or "none"
    lines.append(f"- Evidence: {evidence}")
    return lines


def _html_excerpt(excerpt: Excerpt) -> str:
    chain = "".join(f"<dt>{escape(label)}</dt><dd>{escape(value)}</dd>" for label, value in excerpt.lines)
    evidence = " · ".join(excerpt.evidence) or "none"
    return f"<article class=card><h3>{escape(excerpt.category)}</h3><dl class=chain>{chain}</dl><p class=evidence>Evidence: {escape(evidence)}</p></article>"


def _markdown_banner(verdict: str) -> str:
    return f"> **{_banner_text(verdict)}**"


def _banner_text(verdict: str) -> str:
    return "WIRING CHECK ONLY — not an evaluation" if verdict == "WIRING CHECK ONLY" else f"GREENLIGHT: {verdict}"


def _banner_class(verdict: str) -> str:
    return {"PASS": "pass", "FAIL": "fail", "PARTIAL": "partial", "WIRING CHECK ONLY": "wiring"}[verdict]


def _triage_text(row: Mapping[str, object]) -> str:
    confidence = _number(row.get("confidence"))
    return f"{'authentic' if row.get('authentic') is True else 'not authentic'}; label={row.get('label', 'unlabelled')}; confidence={confidence}. {_quote(str(row.get('one_liner') or row.get('reason') or ''))}"


def _curation_text(row: Mapping[str, object]) -> str:
    return f"{row.get('verdict', 'unknown')}. {_quote(str(row.get('reason') or ''))}"


def _remedy_text(proposal: Mapping[str, object]) -> str:
    artifact = proposal.get("artifact")
    content = artifact.get("remedy_content") if isinstance(artifact, Mapping) else None
    if isinstance(content, Mapping):
        text = content.get("text") or content.get("content") or json.dumps(content, ensure_ascii=False, sort_keys=True)
    else:
        text = content or (artifact.get("failure_statement") if isinstance(artifact, Mapping) else None)
    return _quote(str(text or "No remedy text was recorded."))


def _judge_text(judge: Mapping[str, object], proposal_id: int | None, incident_id: int) -> str | None:
    proposal = next((row for row in _rows(judge, "proposals") if _int(row.get("proposal_id")) == proposal_id), None)
    if proposal is None:
        return None
    counterfactual = next((row for row in _rows(proposal, "counterfactual/incidents") if _int(row.get("incident_id")) == incident_id), None)
    if counterfactual is None:
        return None
    runs = _rows(counterfactual, "runs")
    if not runs:
        return None
    run = runs[0]
    verdict = run.get("verdict", "unknown")
    justification = run.get("reasoning") or run.get("justification") or ""
    return f"{verdict}. {_quote(str(justification))}"


def _judge_proposal_index(judge: Mapping[str, object], proposal_id: int | None) -> int:
    for index, row in enumerate(_rows(judge, "proposals")):
        if _int(row.get("proposal_id")) == proposal_id:
            return index
    return 0


def _context_summary(context: Mapping[str, object]) -> str:
    values = [context.get(key) for key in ("preceding_request", "following_exchange", "agent_activity_digest")]
    text = next((str(value).replace("\n", " ") for value in values if isinstance(value, str) and value.strip()), "")
    return _quote(text) if text else "Omitted: context pack has no surrounding exchange."


def _quote(value: str) -> str:
    normalized = " ".join(value.split())
    if len(normalized) > MAX_QUOTE_CHARS:
        normalized = normalized[: MAX_QUOTE_CHARS - 1].rstrip() + "…"
    return f"“{normalized}”" if normalized else ""


def _models(record: Mapping[str, object]) -> str:
    config = record.get("stage_config")
    if not isinstance(config, Mapping):
        return ""
    return ", ".join(f"{stage}={value.get('model')}" for stage, value in sorted(config.items()) if isinstance(value, Mapping) and value.get("model"))


def _cost(record: Mapping[str, object]) -> str:
    total = sum(float(row.get("total_cost_usd", 0.0)) for row in _rows(record, "llm_run_log") if isinstance(row.get("total_cost_usd", 0.0), (int, float)))
    return f"${total:.4f}"


def _duration(record: Mapping[str, object]) -> str:
    try:
        start = datetime.fromisoformat(str(record.get("started_at")))
        end = datetime.fromisoformat(str(record.get("completed_at")))
        return f"{max(0.0, (end - start).total_seconds()):.2f}s"
    except (TypeError, ValueError):
        return "not recorded"


def _repeat_count(record: Mapping[str, object]) -> int:
    repeats = record.get("repeats")
    return len(repeats) if isinstance(repeats, list) and repeats else 1


def _rows(value: Mapping[str, object], path: str) -> list[dict[str, object]]:
    current: object = value
    for key in path.split("/"):
        current = current.get(key, []) if isinstance(current, Mapping) else []
    return [dict(row) for row in current if isinstance(row, Mapping)] if isinstance(current, list) else []


def _rows_from(value: object) -> list[Mapping[str, object]]:
    return [row for row in value if isinstance(row, Mapping)] if isinstance(value, list) else []


def _by_int(rows: Sequence[Mapping[str, object]], key: str) -> dict[int, Mapping[str, object]]:
    return {number: row for row in rows if (number := _int(row.get(key))) is not None}


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _int_list(value: object) -> list[int]:
    return [item for item in value if _int(item) is not None] if isinstance(value, list) else []


def _number(value: object) -> str:
    return f"{float(value):.3f}" if isinstance(value, (int, float)) and not isinstance(value, bool) else "—"


def _resolve_record_pointer(record: Mapping[str, object], pointer: str) -> object:
    current: object = record
    for part in pointer.removeprefix("/").split("/"):
        if isinstance(current, Mapping):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return None
    return current


def _load_object(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise EvalReportError(f"cannot read {path}: {error}") from error
    if not isinstance(value, dict):
        raise EvalReportError(f"{path} must contain a JSON object")
    return value
