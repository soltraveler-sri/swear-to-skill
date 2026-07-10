"""Golden-artifact coverage for the greenlight report."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from s2s import evalreport


FIXTURES = Path(__file__).parent / "fixtures" / "evalreport"


def _render(case: str, tmp_path: Path) -> evalreport.ReportResult:
    source = FIXTURES / case
    for name in ("record.json", "scores.json", "judge-results.json"):
        (tmp_path / name).write_text((source / name).read_text(encoding="utf-8"), encoding="utf-8")
    return evalreport.write_report(tmp_path / "record.json")


@pytest.mark.parametrize(
    ("case", "verdict", "marker"),
    [
        ("green", "PASS", "GREENLIGHT: PASS"),
        ("failing", "FAIL", "GREENLIGHT: FAIL"),
        ("partial", "PARTIAL", "GREENLIGHT: PARTIAL"),
        ("mock", "WIRING CHECK ONLY", "WIRING CHECK ONLY — not an evaluation"),
    ],
)
def test_fixture_artifacts_render_golden_verdicts(case: str, verdict: str, marker: str, tmp_path: Path) -> None:
    report = _render(case, tmp_path)
    markdown = report.markdown_path.read_text(encoding="utf-8")
    html = report.html_path.read_text(encoding="utf-8")

    assert report.verdict == verdict
    assert marker in markdown and marker in html
    assert "Honest limits" in markdown and "Honest limits" in html
    assert "http://" not in html and "https://" not in html
    if case == "mock":
        assert "0.950" not in markdown and "0.950" not in html
    if case == "failing":
        assert "Failure spotlights" in markdown and "Failure spotlight" in markdown


def test_excerpt_picker_covers_every_available_category() -> None:
    case = FIXTURES / "failing"
    record = json.loads((case / "record.json").read_text(encoding="utf-8"))
    scores = json.loads((case / "scores.json").read_text(encoding="utf-8"))
    judge = json.loads((case / "judge-results.json").read_text(encoding="utf-8"))

    categories = {excerpt.category for excerpt in evalreport.pick_excerpts(record, scores, judge)}

    assert {
        "Converged-cluster member",
        "Promoted singleton",
        "Correctly-dropped decoy",
        "Dedup catch",
        "Worst failure",
    } <= categories


def test_quote_cap_and_missing_evidence_are_honest(tmp_path: Path) -> None:
    case = FIXTURES / "green"
    record = json.loads((case / "record.json").read_text(encoding="utf-8"))
    scores = json.loads((case / "scores.json").read_text(encoding="utf-8"))
    judge = json.loads((case / "judge-results.json").read_text(encoding="utf-8"))
    record["context_packs"]["1"]["frustrated_message"] = "x" * (evalreport.MAX_QUOTE_CHARS * 2)
    del record["context_packs"]["2"]
    excerpts = evalreport.pick_excerpts(record, scores, judge)
    rendered = evalreport.render_markdown(
        record, scores, judge, "PASS", excerpts, (), Path("thresholds.toml"), False
    )

    assert "x" * evalreport.MAX_QUOTE_CHARS not in rendered
    assert "context_packs pointer is unavailable" in rendered


@pytest.mark.parametrize(
    ("verdict", "code"),
    [("PASS", 0), ("FAIL", 1), ("PARTIAL", 2), ("WIRING CHECK ONLY", 0)],
)
def test_exit_code_mapping(verdict: str, code: int) -> None:
    assert evalreport.exit_code_for_verdict(verdict) == code
