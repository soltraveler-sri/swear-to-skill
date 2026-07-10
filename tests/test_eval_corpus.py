from __future__ import annotations

import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals.corpus_tools import claude_session_skeleton
from evals.lint_corpus import lint_corpus
from s2s.adapters.claude_code import extract_user_messages


def _markdown_table(markdown: str, heading: str) -> list[dict[str, str]]:
    section = markdown.split(heading, 1)[1].split("\n## ", 1)[0]
    source_lines = section.splitlines()
    start = next(index for index, line in enumerate(source_lines) if line.startswith("|"))
    lines: list[str] = []
    for line in source_lines[start:]:
        if not line.startswith("|"):
            break
        lines.append(line)
    headers = [cell.strip() for cell in lines[0].strip("|").split("|")]
    return [
        dict(zip(headers, (cell.strip() for cell in line.strip("|").split("|")), strict=True))
        for line in lines[2:]
    ]


def test_golden_corpus_lints_through_real_adapters_and_scanner() -> None:
    stats = lint_corpus()

    assert stats == {
        "claude-code": {"sessions": 16, "incidents": 31, "clusters": 5, "decoys": 8, "known_misses": 2},
        "codex": {"sessions": 4, "incidents": 7, "clusters": 2, "decoys": 2, "known_misses": 0},
    }


def test_authoring_helper_round_trips_through_real_claude_adapter(tmp_path: Path) -> None:
    records = claude_session_skeleton(
        "generated-fake-session",
        "/work/fake-generated",
        ["Please inspect the fake parser.", "This is not what I asked."],
    )
    transcript = tmp_path / "generated.jsonl"
    transcript.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")

    messages = extract_user_messages(transcript)

    assert [message.uuid for message in messages] == ["generated-fake-session-u-01", "generated-fake-session-u-02"]
    assert [message.message for message in messages] == ["Please inspect the fake parser.", "This is not what I asked."]


def test_measured_performance_headlines_match_results_reference() -> None:
    root = Path(__file__).resolve().parents[1]
    readme_rows = _markdown_table((root / "README.md").read_text(encoding="utf-8"), "## Measured performance")
    results_rows = _markdown_table((root / "evals" / "RESULTS.md").read_text(encoding="utf-8"), "## Full results")
    result_by_model = {row["Triage model"]: row for row in results_rows}

    for readme_row in readme_rows:
        result_row = result_by_model[readme_row["Triage model"]]
        assert readme_row["Authenticity precision"] == f"{float(result_row['Authenticity precision']):.2f}"
        assert readme_row["Authenticity recall"] == f"{float(result_row['Authenticity recall']):.2f}"
        assert readme_row["Label agreement"] == f"{float(result_row['Label agreement']):.2f}"
        assert readme_row["Convergence"] == f"{float(result_row['Convergence']):.1f}"
