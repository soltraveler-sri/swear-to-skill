from __future__ import annotations

from pathlib import Path

import pytest

from s2s.cli import main
from s2s.ledger import Ledger
from s2s.meter import collect_dashboard_data, friendly_model_name, write_dashboard


@pytest.fixture
def ledger(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Ledger:
    monkeypatch.setenv("S2S_HOME", str(tmp_path / "s2s-home"))
    database = Ledger()
    yield database
    database.close()


def _record_detection(
    ledger: Ledger,
    *,
    source: str,
    session_id: str,
    project: str,
    occurred_at: str,
    message: str,
    label: str | None = None,
) -> None:
    incident_id = ledger.create_scanned_incident(
        source=source,
        session_id=session_id,
        project=project,
        occurred_at=occurred_at,
        message=message,
    )
    if label is not None:
        ledger.triage_incident(
            incident_id,
            label=label,
            one_liner="A fixture category for the meter.",
            severity="medium",
            confidence=0.8,
            context_pack_pointer="archive/fixture.context.json",
        )


def _seed_ledger(ledger: Ledger) -> None:
    ledger.upsert_session_stats(
        source="claude-code",
        session_id="one",
        project="alpha",
        dominant_model="claude-opus-4-8",
        direct_message_count=10,
        hit_count=2,
        first_timestamp="2026-01-05T09:00:00+00:00",
        last_timestamp="2026-01-05T10:00:00+00:00",
        scanned_at="2026-01-05T10:05:00+00:00",
    )
    _record_detection(
        ledger,
        source="claude-code",
        session_id="one",
        project="alpha",
        occurred_at="2026-01-05T09:30:00+00:00",
        message="That is not what I asked.",
        label="ignored-instruction",
    )
    _record_detection(
        ledger,
        source="claude-code",
        session_id="one",
        project="alpha",
        occurred_at="2026-01-05T09:45:00+00:00",
        message="This is still broken.",
    )
    ledger.upsert_session_stats(
        source="claude-code",
        session_id="two",
        project="alpha",
        dominant_model="claude-opus-4-8-20260101",
        direct_message_count=5,
        hit_count=1,
        first_timestamp="2026-01-12T09:00:00+00:00",
        last_timestamp="2026-01-12T10:00:00+00:00",
        scanned_at="2026-01-12T10:05:00+00:00",
    )
    _record_detection(
        ledger,
        source="claude-code",
        session_id="two",
        project="alpha",
        occurred_at="2026-01-12T09:30:00+00:00",
        message="Not working.",
        label="other",
    )
    ledger.upsert_session_stats(
        source="claude-code",
        session_id="three",
        project="beta",
        dominant_model="claude-sonnet-4-5",
        direct_message_count=4,
        hit_count=0,
        first_timestamp="2026-01-06T09:00:00+00:00",
        last_timestamp="2026-01-06T10:00:00+00:00",
        scanned_at="2026-01-06T10:05:00+00:00",
    )
    ledger.upsert_session_stats(
        source="codex",
        session_id="four",
        project="gamma",
        dominant_model="gpt-5",
        direct_message_count=3,
        hit_count=1,
        first_timestamp="2026-01-19T09:00:00+00:00",
        last_timestamp="2026-01-19T10:00:00+00:00",
        scanned_at="2026-01-19T10:05:00+00:00",
    )
    _record_detection(
        ledger,
        source="codex",
        session_id="four",
        project="gamma",
        occurred_at="2026-01-19T09:30:00+00:00",
        message="You invented an API.",
    )


def test_meter_aggregates_weekly_model_project_and_category_rates(ledger: Ledger) -> None:
    _seed_ledger(ledger)

    data = collect_dashboard_data(ledger)

    assert (data.session_count, data.direct_messages, data.hit_messages) == (4, 22, 4)
    assert [(week.label, week.direct_messages, week.hit_messages) for week in data.weeks] == [
        ("2026-W02", 14, 2),
        ("2026-W03", 5, 1),
        ("2026-W04", 3, 1),
    ]
    assert [(rate.label, rate.direct_messages, rate.hit_messages) for rate in data.models] == [
        ("Opus 4.8", 15, 3),
        ("Sonnet 4.5", 4, 0),
        ("Other", 3, 1),
    ]
    assert [(rate.label, rate.direct_messages, rate.hit_messages) for rate in data.projects] == [
        ("alpha", 15, 3),
        ("beta", 4, 0),
        ("gamma", 3, 1),
    ]
    assert data.categories == (
        ("Unclassified", 2),
        ("ignored-instruction", 1),
        ("other", 1),
    )
    assert data.date_start is not None and data.date_start.isoformat() == "2026-01-05"
    assert data.date_end is not None and data.date_end.isoformat() == "2026-01-19"


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("claude-opus-4-8", "Opus 4.8"),
        ("claude-sonnet-4-5-20260101", "Sonnet 4.5"),
        ("claude-haiku-3-5", "Haiku 3.5"),
        ("gpt-5", "Other"),
        (None, "Other"),
    ],
)
def test_friendly_model_name_folds_only_known_claude_ids(
    model: str | None, expected: str
) -> None:
    assert friendly_model_name(model) == expected


def test_dashboard_is_self_contained_and_contains_fixture_values(ledger: Ledger) -> None:
    _seed_ledger(ledger)

    report = write_dashboard(ledger)
    document = report.read_text(encoding="utf-8")

    assert report.name == "dashboard.html"
    assert "2026-W02" in document
    assert "22" in document
    assert "18.2%" in document
    assert "Opus 4.8" in document
    assert 'id="cost-log"' in document
    assert 'id="proposals-digest"' in document
    assert 'id="remedy-outcomes"' in document
    assert "http://" not in document
    assert "https://" not in document


def test_dashboard_empty_state_is_friendly_and_writes_without_data(ledger: Ledger) -> None:
    report = write_dashboard(ledger)

    assert "no data yet — run: s2s backfill && s2s scan" in report.read_text(encoding="utf-8")


def test_dashboard_renders_a_single_dated_session(ledger: Ledger) -> None:
    ledger.upsert_session_stats(
        source="claude-code",
        session_id="only-session",
        project="solo",
        dominant_model="claude-haiku-3-5",
        direct_message_count=1,
        hit_count=1,
        first_timestamp="2026-02-02T09:00:00+00:00",
        last_timestamp="2026-02-02T09:01:00+00:00",
        scanned_at="2026-02-02T09:02:00+00:00",
    )
    _record_detection(
        ledger,
        source="claude-code",
        session_id="only-session",
        project="solo",
        occurred_at="2026-02-02T09:00:00+00:00",
        message="This is not right.",
    )

    document = write_dashboard(ledger).read_text(encoding="utf-8")

    assert "2026-W06" in document
    assert "Haiku 3.5" in document
    assert "100.0%" in document
    assert "polyline" in document


def test_meter_open_uses_webbrowser_and_still_prints_the_local_uri(
    ledger: Ledger, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    opened: list[str] = []
    monkeypatch.setattr("webbrowser.open", opened.append)

    assert main(["meter", "--open"]) == 0

    uri = capsys.readouterr().out.strip()
    assert uri.startswith("file://")
    assert opened == [uri]


def test_meter_and_status_cli_report_local_paths_and_seeded_summary(
    ledger: Ledger, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_ledger(ledger)
    archive = ledger.path.parent / "archive" / "alpha" / "one.jsonl"
    archive.parent.mkdir(parents=True)
    archive.write_text("{}\n", encoding="utf-8")

    assert main(["meter"]) == 0
    meter_output = capsys.readouterr().out.strip()
    assert meter_output.startswith("file://")
    assert meter_output.endswith("/reports/dashboard.html")

    assert main(["status"]) == 0
    status_output = capsys.readouterr().out
    assert "sessions archived: 1" in status_output
    assert "session stats: 4 session(s), 22 direct message(s), 4 detected message(s)" in status_output
    assert "incidents: detected=2, open=2" in status_output
    assert "queue depth: 0" in status_output
    assert "singleton ratio: 100.0%" in status_output
    assert "other share: 50.0%" in status_output
    assert "last scan: 2026-01-19T10:05:00+00:00" in status_output
