"""Offline aggregation and local HTML rendering for the frustration meter."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from html import escape
import json
from pathlib import Path
import re

from .ledger import INCIDENT_STATES, Ledger, SessionStats
from .paths import resolve_paths


MODEL_ID_RE = re.compile(
    r"^claude-(?P<family>opus|sonnet|haiku)-(?P<major>\d+)-(?P<minor>\d+)(?:[-_.].*)?$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Rate:
    """A numerator and denominator displayed as a percentage."""

    label: str
    direct_messages: int
    hit_messages: int

    @property
    def percentage(self) -> float:
        if self.direct_messages == 0:
            return 0.0
        return self.hit_messages / self.direct_messages * 100


@dataclass(frozen=True)
class WeeklyRate(Rate):
    """One ISO-week bucket, keyed by the Monday that starts it."""

    week_start: date


@dataclass(frozen=True)
class CostTotal:
    """Aggregate accounting for one LLM stage/model pairing."""

    stage: str
    model: str
    calls: int
    tokens: int
    cost_usd: float


@dataclass(frozen=True)
class RemedyOutcome:
    """The latest compact, per-remedy audit reading for status/dashboard surfaces."""

    remedy_id: int
    label: str
    installed_at: str
    pre_rate: float | None
    post_rate: float | None
    verdict: str | None


@dataclass(frozen=True)
class PendingProposal:
    """A compact, safe-to-display view of a human-review proposal."""

    proposal_id: int
    remedy_type: str
    confidence: float | None
    content: str


@dataclass(frozen=True)
class SkillUsageSummary:
    """Compact usage totals for one generated skill or the companion skill."""

    skill_name: str
    count: int
    last_used_at: str
    is_companion: bool


@dataclass(frozen=True)
class DashboardData:
    """All durable facts needed by the static dashboard and status command."""

    session_count: int
    direct_messages: int
    hit_messages: int
    weeks: tuple[WeeklyRate, ...]
    models: tuple[Rate, ...]
    sources: tuple[Rate, ...]
    projects: tuple[Rate, ...]
    categories: tuple[tuple[str, int], ...]
    incident_states: tuple[tuple[str, int], ...]
    queue_depth: int
    active_cluster_count: int
    singleton_ratio: float
    other_share: float
    date_start: date | None
    date_end: date | None
    last_scan: str | None
    cost_totals: tuple[CostTotal, ...]
    pending_proposals: tuple[PendingProposal, ...]
    remedy_outcomes: tuple[RemedyOutcome, ...]
    revision_proposal_count: int
    skill_usage: tuple[SkillUsageSummary, ...]

    @property
    def rate(self) -> float:
        if self.direct_messages == 0:
            return 0.0
        return self.hit_messages / self.direct_messages * 100


def friendly_model_name(model: str | None) -> str:
    """Fold known Claude model identifiers into compact dashboard labels."""

    if not model:
        return "Other"
    match = MODEL_ID_RE.fullmatch(model.strip())
    if match is None:
        return "Other"
    family = match.group("family").title()
    return f"{family} {match.group('major')}.{match.group('minor')}"


def collect_dashboard_data(ledger: Ledger) -> DashboardData:
    """Read the existing ledger into meter, health, and status aggregates.

    ``session_stats.hit_count`` is the scanner's per-message numerator: scanner
    records one detection per direct message even if multiple lexicon terms match.
    Incident rows are consulted as a defensive cap for a partially-written or
    manually-edited ledger, so the reported numerator can never exceed the
    detected records attached to a populated session.
    """

    stats = ledger.session_stats()
    incident_counts, categories, state_counts = _incident_aggregates(ledger)
    cost_totals = _cost_totals(ledger)
    remedy_outcomes = tuple(
        RemedyOutcome(
            remedy_id=remedy.id,
            label=", ".join(ledger.remedy_labels(remedy.id)) or "unlabelled",
            installed_at=remedy.installed_at,
            pre_rate=remedy.outcome_pre_rate,
            post_rate=remedy.outcome_post_rate,
            verdict=remedy.outcome_verdict,
        )
        for remedy in ledger.installed_remedies()
    )
    proposals = ledger.pending_proposals()
    pending_proposals = tuple(_pending_proposal_view(proposal) for proposal in proposals)
    revision_proposal_count = sum(proposal.proposal_kind == "revision" for proposal in proposals)
    usage_groups: dict[str, list[str]] = defaultdict(list)
    for usage in ledger.skill_usage():
        usage_groups[usage.skill_name].append(usage.used_at)
    skill_usage = tuple(
        SkillUsageSummary(
            skill_name=skill_name,
            count=len(timestamps),
            last_used_at=max(timestamps),
            is_companion=skill_name == "s2s",
        )
        for skill_name, timestamps in sorted(usage_groups.items())
    )
    weekly: dict[date, list[int]] = defaultdict(lambda: [0, 0])
    models: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    sources: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    projects: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    dates: list[date] = []
    scan_times: list[tuple[datetime, str]] = []

    total_messages = 0
    total_hits = 0
    for session in stats:
        messages = max(session.direct_message_count, 0)
        hits = _session_hit_messages(session, incident_counts)
        total_messages += messages
        total_hits += hits

        model = friendly_model_name(session.dominant_model)
        models[model][0] += messages
        models[model][1] += hits
        source_label = (
            "Codex" if session.source == "codex" else "Claude Code" if session.source == "claude-code" else session.source
        )
        sources[source_label][0] += messages
        sources[source_label][1] += hits
        projects[session.project][0] += messages
        projects[session.project][1] += hits

        first = _parse_timestamp(session.first_timestamp)
        last = _parse_timestamp(session.last_timestamp)
        if first is not None:
            week_start = first.date() - timedelta(days=_days_since_monday(first.date()))
            weekly[week_start][0] += messages
            weekly[week_start][1] += hits
            dates.append(first.date())
        if last is not None:
            dates.append(last.date())
        scanned = _parse_timestamp(session.scanned_at)
        if scanned is not None:
            scan_times.append((scanned, session.scanned_at))

    return DashboardData(
        session_count=len(stats),
        direct_messages=total_messages,
        hit_messages=total_hits,
        weeks=tuple(
            WeeklyRate(
                label=_iso_week_label(week_start),
                direct_messages=counts[0],
                hit_messages=counts[1],
                week_start=week_start,
            )
            for week_start, counts in sorted(weekly.items())
        ),
        models=_rates_from_counts(models),
        sources=_rates_from_counts(sources),
        projects=_rates_from_counts(projects),
        categories=tuple(sorted(categories.items(), key=lambda item: (-item[1], item[0]))),
        incident_states=tuple(
            (state, state_counts[state])
            for state in INCIDENT_STATES
            if state_counts[state]
        ),
        queue_depth=len(ledger.pending_queue_items()),
        active_cluster_count=ledger.active_cluster_count(),
        singleton_ratio=ledger.singleton_ratio(),
        other_share=ledger.other_share(),
        date_start=min(dates) if dates else None,
        date_end=max(dates) if dates else None,
        last_scan=max(scan_times)[1] if scan_times else None,
        cost_totals=cost_totals,
        pending_proposals=pending_proposals,
        remedy_outcomes=remedy_outcomes,
        revision_proposal_count=revision_proposal_count,
        skill_usage=skill_usage,
    )


def write_dashboard(ledger: Ledger, path: Path | None = None) -> Path:
    """Render the self-contained dashboard under the configured local home."""

    report_path = path or (resolve_paths().home / "reports" / "dashboard.html")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(render_dashboard(collect_dashboard_data(ledger)), encoding="utf-8")
    return report_path


def render_dashboard(data: DashboardData) -> str:
    """Return the complete local-only dashboard document without external assets."""

    meter_body = _render_empty_meter() if data.session_count == 0 else _render_meter(data)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>swear-to-skill · meter</title>
<style>
:root {{ color-scheme: dark; --ink:#eee8dc; --muted:#aaa294; --paper:#181715; --panel:#24221f; --line:#393630; --accent:#e6a54b; --signal:#dd7658; --mint:#7fb9a3; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--paper); color:var(--ink); font:15px/1.5 ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }}
main {{ max-width:1120px; margin:0 auto; padding:48px 24px 36px; }}
h1,h2,h3,p {{ margin-top:0; }}
h1 {{ margin-bottom:6px; font-family:ui-serif,Georgia,serif; font-size:clamp(2.2rem,6vw,4.5rem); font-weight:500; letter-spacing:-.055em; }}
h2 {{ margin-bottom:18px; font-size:1rem; letter-spacing:.08em; text-transform:uppercase; color:var(--muted); }}
h3 {{ margin-bottom:8px; font-size:1rem; }}
.eyebrow,.muted {{ color:var(--muted); }} .eyebrow {{ font-size:.78rem; letter-spacing:.12em; text-transform:uppercase; }}
.lede {{ max-width:58ch; color:var(--muted); }}
.section {{ margin-top:52px; }}
.grid {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:12px; }}
.health-grid {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:12px; }}
.card,.table-card,.placeholder {{ border:1px solid var(--line); border-radius:12px; background:var(--panel); }}
.card {{ padding:16px; }} .number {{ display:block; font-size:1.8rem; font-variant-numeric:tabular-nums; }} .label {{ color:var(--muted); font-size:.82rem; }}
.chart {{ margin-top:18px; padding:18px; border:1px solid var(--line); border-radius:12px; background:var(--panel); overflow-x:auto; }}
svg {{ display:block; min-width:560px; width:100%; height:auto; }} .axis {{ stroke:var(--line); stroke-width:1; }} .bar {{ fill:var(--accent); opacity:.85; }} .rate-line {{ fill:none; stroke:var(--signal); stroke-width:3; stroke-linecap:round; stroke-linejoin:round; }} .point {{ fill:var(--signal); }} .svg-label {{ fill:var(--muted); font-size:12px; }}
.legend {{ display:flex; flex-wrap:wrap; gap:14px; margin:13px 0 0; color:var(--muted); font-size:.84rem; }} .key {{ display:inline-block; width:10px; height:10px; margin-right:5px; border-radius:50%; background:var(--signal); }} .key.bar-key {{ border-radius:2px; background:var(--accent); }}
.split {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:12px; margin-top:12px; }}
.table-card {{ padding:16px; }} table {{ width:100%; border-collapse:collapse; font-variant-numeric:tabular-nums; }} th,td {{ padding:8px 0; border-bottom:1px solid var(--line); text-align:left; }} tr:last-child td {{ border-bottom:0; }} th {{ color:var(--muted); font-size:.76rem; font-weight:500; text-transform:uppercase; letter-spacing:.06em; }} td:last-child,th:last-child {{ text-align:right; }}
.empty {{ padding:34px 18px; border:1px dashed var(--line); border-radius:12px; color:var(--muted); }} .placeholder {{ padding:16px; color:var(--muted); }} .placeholder strong {{ color:var(--ink); }}
footer {{ margin-top:56px; padding-top:18px; border-top:1px solid var(--line); color:var(--muted); font-size:.84rem; }}
@media (max-width:680px) {{ main {{ padding:32px 16px; }} .grid,.health-grid,.split {{ grid-template-columns:1fr; }} }}
</style>
</head>
<body>
<main>
  <header>
    <div class="eyebrow">swear-to-skill / local meter</div>
    <h1>How often did it miss?</h1>
    <p class="lede">A quiet record of direct-message frustration signals across your archived coding-agent sessions.</p>
    <p class="muted">{_date_range(data)}</p>
  </header>
  <section class="section" aria-labelledby="meter-heading">
    <h2 id="meter-heading">Meter</h2>
    {meter_body}
  </section>
  <section class="section" aria-labelledby="health-heading">
    <h2 id="health-heading">Pipeline health</h2>
    {_render_health(data)}
  </section>
  <section class="section" aria-labelledby="usage-heading">
    <h2 id="usage-heading">Skill usage</h2>
    {_render_skill_usage(data.skill_usage)}
  </section>
  <footer>This file is local and private. Sharing a screenshot shares the dashboard’s content.</footer>
</main>
</body>
</html>
"""


def render_status(data: DashboardData, *, archived_sessions: int) -> str:
    """Return a compact text status summary for the terminal and companion skill."""

    incidents = ", ".join(f"{state}={count}" for state, count in data.incident_states)
    total_calls = sum(row.calls for row in data.cost_totals)
    total_cost = sum(row.cost_usd for row in data.cost_totals)
    cost_lines = [
        f"LLM costs: ${total_cost:.4f} across {total_calls} call(s)",
        *( 
            f"  {row.stage}/{row.model}: {row.calls} call(s), {row.tokens} token(s), ${row.cost_usd:.4f}"
            for row in data.cost_totals
        ),
    ]
    if not data.cost_totals:
        cost_lines.append("  no LLM runs recorded")
    outcome_counts = Counter(
        outcome.verdict for outcome in data.remedy_outcomes if outcome.verdict is not None
    )
    summary_parts = [
        f"{count} {verdict}"
        for verdict, count in sorted(outcome_counts.items())
        if verdict != "persisting" or data.revision_proposal_count == 0
    ]
    if data.revision_proposal_count:
        summary_parts.append(f"{data.revision_proposal_count} revision proposed")
    remedy_summary = ", ".join(summary_parts) or "not yet audited"
    remedy_lines = tuple(
        "remedy "
        f"#{outcome.remedy_id} [{outcome.label}]: {outcome.verdict or 'not audited'} "
        f"(pre {_audit_rate(outcome.pre_rate)}, post {_audit_rate(outcome.post_rate)})"
        for outcome in data.remedy_outcomes
    )
    usage_lines = tuple(
        "skill usage: "
        f"{usage.skill_name}{' (companion)' if usage.is_companion else ''} "
        f"used {usage.count}x (last {_usage_date(usage.last_used_at)})"
        for usage in data.skill_usage
    )
    return "\n".join(
        (
            "s2s status",
            f"sessions archived: {archived_sessions}",
            (
                "session stats: "
                f"{data.session_count} session(s), {data.direct_messages} direct message(s), "
                f"{data.hit_messages} detected message(s)"
            ),
            f"incidents: {incidents or 'none'}",
            "sources: " + (
                ", ".join(f"{rate.label}={_percentage(rate.percentage)}" for rate in data.sources)
                or "none"
            ),
            f"queue depth: {data.queue_depth}",
            f"singleton ratio: {_percentage(data.singleton_ratio * 100)}",
            f"other share: {_percentage(data.other_share * 100)}",
            f"last scan: {data.last_scan or 'none'}",
            *(usage_lines or ("skill usage: none",)),
            f"remedies installed: {len(data.remedy_outcomes)} ({remedy_summary})",
            *remedy_lines,
            *cost_lines,
        )
    )


def count_archived_sessions() -> int:
    """Count archived transcript files without reading their private contents."""

    archive_dir = resolve_paths().archive_dir
    if not archive_dir.is_dir():
        return 0
    return sum(path.is_file() for path in archive_dir.rglob("*.jsonl"))


def _incident_aggregates(
    ledger: Ledger,
) -> tuple[Counter[tuple[str, str]], Counter[str], Counter[str]]:
    counts: Counter[tuple[str, str]] = Counter()
    categories: Counter[str] = Counter()
    states: Counter[str] = Counter()
    rows = ledger.connection.execute(
        "SELECT source, session_id, label, state FROM incident"
    ).fetchall()
    for row in rows:
        counts[(str(row["source"]), str(row["session_id"]))] += 1
        categories[str(row["label"]) if row["label"] is not None else "Unclassified"] += 1
        states[str(row["state"])] += 1
    return counts, categories, states


def _cost_totals(ledger: Ledger) -> tuple[CostTotal, ...]:
    rows = ledger.connection.execute(
        """
        SELECT stage, model, COUNT(*) AS calls,
               COALESCE(SUM(tokens), 0) AS tokens,
               COALESCE(SUM(cost_usd), 0.0) AS cost_usd
        FROM run_log
        GROUP BY stage, model
        ORDER BY stage, model
        """
    ).fetchall()
    return tuple(
        CostTotal(
            stage=str(row["stage"]),
            model=str(row["model"]),
            calls=int(row["calls"]),
            tokens=int(row["tokens"]),
            cost_usd=float(row["cost_usd"]),
        )
        for row in rows
    )


def _session_hit_messages(
    session: SessionStats,
    incident_counts: Counter[tuple[str, str]],
) -> int:
    reported = min(max(session.hit_count, 0), max(session.direct_message_count, 0))
    incidents = incident_counts.get((session.source, session.session_id))
    # Every normal scanner run has one incident per matching direct message.  A
    # zero count can still occur in a pre-seeded/partially migrated fixture, where
    # ``session_stats`` remains the durable scanner total.
    if incidents:
        return min(reported, incidents)
    return reported


def _rates_from_counts(counts: dict[str, list[int]]) -> tuple[Rate, ...]:
    return tuple(
        Rate(label=label, direct_messages=values[0], hit_messages=values[1])
        for label, values in sorted(counts.items(), key=lambda item: (-item[1][0], item[0]))
    )


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _days_since_monday(value: date) -> int:
    return value.weekday()


def _iso_week_label(week_start: date) -> str:
    iso = week_start.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _percentage(value: float) -> str:
    return f"{value:.1f}%"


def _date_range(data: DashboardData) -> str:
    if data.date_start is None or data.date_end is None:
        return "Date range: no scanned sessions yet"
    return f"Date range: {data.date_start.isoformat()} – {data.date_end.isoformat()}"


def _render_empty_meter() -> str:
    return (
        '<div class="empty"><strong>no data yet — run: s2s backfill && s2s scan</strong>'
        "<br>Once scanned, this local report will show weekly rates, models, and projects.</div>"
    )


def _render_meter(data: DashboardData) -> str:
    cards = "\n".join(
        (
            _metric_card("Direct messages", str(data.direct_messages)),
            _metric_card("Detected messages", str(data.hit_messages)),
            _metric_card("Frustration rate", _percentage(data.rate)),
        )
    )
    return f"""
<div class="grid">{cards}</div>
<div class="chart">
  <h3>Weekly volume and frustration rate</h3>
  {_render_chart(data.weeks)}
  <div class="legend"><span><i class="key bar-key"></i>Direct-message volume</span><span><i class="key"></i>Frustration rate</span></div>
</div>
<div class="split">
  {_render_rates_table('By source', data.sources, 'source')}
  {_render_rates_table('By model', data.models, 'model')}
</div>
<div class="split">
  {_render_rates_table('By project', data.projects, 'project')}
  {_render_weekly_table(data.weeks)}
</div>
<div class="split">
  {_render_categories(data.categories)}
  <div class="placeholder"><strong>Cross-agent comparison</strong><br>Compare Claude Code and Codex rates above; each rate uses that source's own direct-message denominator.</div>
</div>
"""


def _metric_card(label: str, value: str) -> str:
    return f'<div class="card"><span class="number">{escape(value)}</span><span class="label">{escape(label)}</span></div>'


def _health_metric_card(label: str, value: str, interpretation: str) -> str:
    return (
        f'<div class="card"><span class="number">{escape(value)}</span>'
        f'<span class="label">{escape(label)}</span><p class="muted">{escape(interpretation)}</p></div>'
    )


def _singleton_interpretation(data: DashboardData) -> str:
    if data.singleton_ratio > 0.9 and data.active_cluster_count >= 10:
        return "fragmentation death pattern — the pipeline is not clustering; inspect taxonomy fit"
    return f"{data.active_cluster_count} active cluster(s); lower is healthier when incidents recur."


def _other_interpretation(data: DashboardData) -> str:
    if data.other_share > 0.3:
        return "taxonomy gap — gardening should be creating labels"
    return "The escape hatch is within the expected range."


def _render_chart(weeks: tuple[WeeklyRate, ...]) -> str:
    if not weeks:
        return '<div class="empty">No dated sessions are available for a weekly chart yet.</div>'
    width = 900
    height = 270
    left = 42
    right = 18
    top = 18
    bottom = 45
    plot_width = width - left - right
    plot_height = height - top - bottom
    baseline = top + plot_height
    max_messages = max(week.direct_messages for week in weeks) or 1
    slot = plot_width / len(weeks)
    bar_width = max(8, min(46, slot * 0.58))
    points: list[str] = []
    bars: list[str] = []
    labels: list[str] = []
    for index, week in enumerate(weeks):
        center = left + slot * (index + 0.5)
        bar_height = week.direct_messages / max_messages * plot_height
        x = center - bar_width / 2
        y = baseline - bar_height
        rate_y = baseline - week.percentage / 100 * plot_height
        bars.append(
            f'<rect class="bar" x="{x:.1f}" y="{y:.1f}" width="{bar_width:.1f}" '
            f'height="{bar_height:.1f}"><title>{escape(week.label)}: {week.direct_messages} messages, {_percentage(week.percentage)}</title></rect>'
        )
        points.append(f"{center:.1f},{rate_y:.1f}")
        labels.append(
            f'<text class="svg-label" x="{center:.1f}" y="{height - 16}" text-anchor="middle">{escape(week.label)}</text>'
        )
    circles = "".join(
        f'<circle class="point" cx="{point.split(",")[0]}" cy="{point.split(",")[1]}" r="3.5"></circle>'
        for point in points
    )
    return f"""<svg viewBox="0 0 {width} {height}" role="img" aria-label="Weekly direct-message volume and frustration rate">
  <line class="axis" x1="{left}" y1="{baseline}" x2="{width - right}" y2="{baseline}"></line>
  <text class="svg-label" x="0" y="{top + 4}">100%</text>
  <text class="svg-label" x="8" y="{baseline}">0%</text>
  {''.join(bars)}
  <polyline class="rate-line" points="{' '.join(points)}"></polyline>
  {circles}
  {''.join(labels)}
</svg>"""


def _render_rates_table(title: str, rates: tuple[Rate, ...], noun: str) -> str:
    rows = "".join(
        f"<tr><td>{escape(rate.label)}</td><td>{rate.direct_messages}</td><td>{rate.hit_messages}</td><td>{_percentage(rate.percentage)}</td></tr>"
        for rate in rates
    ) or f'<tr><td colspan="4" class="muted">No {escape(noun)} data yet.</td></tr>'
    return f"""<div class="table-card"><h3>{escape(title)}</h3><table>
<thead><tr><th>{escape(noun)}</th><th>Messages</th><th>Hits</th><th>Rate</th></tr></thead>
<tbody>{rows}</tbody></table></div>"""


def _render_categories(categories: tuple[tuple[str, int], ...]) -> str:
    rows = "".join(
        f"<tr><td>{escape(category)}</td><td>{count}</td></tr>" for category, count in categories[:8]
    ) or '<tr><td colspan="2" class="muted">No detected categories yet.</td></tr>'
    return f"""<div class="table-card"><h3>Top categories</h3><table>
<thead><tr><th>Category</th><th>Incidents</th></tr></thead><tbody>{rows}</tbody></table></div>"""


def _render_weekly_table(weeks: tuple[WeeklyRate, ...]) -> str:
    rows = "".join(
        f"<tr><td>{escape(week.label)}</td><td>{week.direct_messages}</td><td>{week.hit_messages}</td><td>{_percentage(week.percentage)}</td></tr>"
        for week in weeks
    ) or '<tr><td colspan="4" class="muted">No dated sessions yet.</td></tr>'
    return f"""<div class="table-card"><h3>Weekly detail</h3><table>
<thead><tr><th>Week</th><th>Messages</th><th>Hits</th><th>Rate</th></tr></thead><tbody>{rows}</tbody></table></div>"""


def _render_health(data: DashboardData) -> str:
    state_rows = "".join(
        f"<tr><td>{escape(state)}</td><td>{count}</td></tr>" for state, count in data.incident_states
    ) or '<tr><td colspan="2" class="muted">No incidents recorded.</td></tr>'
    return f"""
<div class="health-grid">
  {_metric_card('Queue depth', str(data.queue_depth))}
  {_health_metric_card('Singleton ratio', _percentage(data.singleton_ratio * 100), _singleton_interpretation(data))}
  {_health_metric_card('Other share', _percentage(data.other_share * 100), _other_interpretation(data))}
  <div class="table-card"><h3>Incidents by state</h3><table><thead><tr><th>State</th><th>Count</th></tr></thead><tbody>{state_rows}</tbody></table></div>
</div>
<div class="split">
  {_render_cost_log(data.cost_totals)}
  {_render_proposals_digest(data.pending_proposals)}
</div>
{_render_remedy_outcomes(data.remedy_outcomes)}
"""


def _render_remedy_outcomes(outcomes: tuple[RemedyOutcome, ...]) -> str:
    rows = "".join(
        "<tr>"
        f"<td>#{outcome.remedy_id} · {escape(outcome.label)}</td>"
        f"<td>{escape(outcome.installed_at[:10])}</td>"
        f"<td>{_audit_rate(outcome.pre_rate)}</td>"
        f"<td>{_audit_rate(outcome.post_rate)}</td>"
        f"<td>{escape(outcome.verdict or 'not audited')}</td>"
        "</tr>"
        for outcome in outcomes
    ) or '<tr><td colspan="5" class="muted">No installed remedies yet.</td></tr>'
    return f"""<div id="remedy-outcomes" class="table-card" style="margin-top:12px">
<h3>Remedy outcomes</h3><table><thead><tr><th>Remedy</th><th>Installed</th><th>Pre rate</th><th>Post rate</th><th>Verdict</th></tr></thead>
<tbody>{rows}</tbody></table></div>"""


def _render_skill_usage(usages: tuple[SkillUsageSummary, ...]) -> str:
    rows = "".join(
        "<tr>"
        f"<td>{escape(usage.skill_name)}</td>"
        f"<td>{'companion' if usage.is_companion else 'generated'}</td>"
        f"<td>{usage.count}</td>"
        f"<td>{escape(_usage_date(usage.last_used_at))}</td>"
        "</tr>"
        for usage in usages
    ) or '<tr><td colspan="4" class="muted">No s2s skill usage recorded yet.</td></tr>'
    return f'''<div id="skill-usage" class="table-card"><table>
<thead><tr><th>Skill</th><th>Kind</th><th>Uses</th><th>Last used</th></tr></thead>
<tbody>{rows}</tbody></table></div>'''


def _usage_date(value: str) -> str:
    parsed = _parse_timestamp(value)
    return parsed.date().isoformat() if parsed is not None else (value[:10] or "unknown")


def _audit_rate(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3f}/session"


def _render_cost_log(cost_totals: tuple[CostTotal, ...]) -> str:
    rows = "".join(
        (
            f"<tr><td>{escape(cost.stage)}</td><td>{escape(cost.model)}</td>"
            f"<td>{cost.calls}</td><td>{cost.tokens}</td><td>${cost.cost_usd:.4f}</td></tr>"
        )
        for cost in cost_totals
    ) or '<tr><td colspan="5" class="muted">No LLM runs recorded yet.</td></tr>'
    return f'''<div id="cost-log" class="table-card"><h3>Cost log</h3><table>
<thead><tr><th>Stage</th><th>Model</th><th>Calls</th><th>Tokens</th><th>Cost</th></tr></thead>
<tbody>{rows}</tbody></table></div>'''


def _pending_proposal_view(proposal: object) -> PendingProposal:
    """Extract a bounded display summary without exposing raw evidence packs."""

    from .ledger import Proposal

    assert isinstance(proposal, Proposal)
    try:
        payload = json.loads(proposal.drafted_content)
    except json.JSONDecodeError:
        payload = {}
    payload = payload if isinstance(payload, dict) else {}
    content = payload.get("remedy_content", proposal.drafted_content)
    if isinstance(content, dict):
        content = content.get("text") or content.get("note") or content.get("command_sketch") or ""
    confidence = payload.get("confidence")
    return PendingProposal(
        proposal_id=proposal.id,
        remedy_type=proposal.remedy_type,
        confidence=float(confidence) if isinstance(confidence, int | float) else None,
        content=_one_line(str(content), limit=180),
    )


def _render_proposals_digest(proposals: tuple[PendingProposal, ...]) -> str:
    if not proposals:
        body = 'No pending proposals. Run <code>s2s status</code> to check the pipeline.'
    else:
        rows = "".join(
            "<li>"
            f"<strong>#{proposal.proposal_id} · {escape(proposal.remedy_type)}</strong>"
            f"{(' · confidence ' + format(proposal.confidence, '.2f')) if proposal.confidence is not None else ''}"
            f"<br>{escape(proposal.content)}"
            "</li>"
            for proposal in proposals
        )
        body = f"<ul>{rows}</ul><p>Review with <code>s2s proposals</code> or <code>/s2s</code>.</p>"
    return f'<div id="proposals-digest" class="placeholder"><strong>Proposals digest</strong><br>{body}</div>'


def _one_line(value: str, *, limit: int) -> str:
    return " ".join(value.split())[:limit]
