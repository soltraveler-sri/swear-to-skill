from __future__ import annotations

import json
from pathlib import Path

import pytest

from s2s.ledger import Ledger
from s2s.scanner import (
    LEXICONS_DIR,
    compile_patterns,
    load_lexicon,
    match_message,
    mine_candidate_phrases,
    scan_pending_queue,
    scan_transcript,
    write_candidate_phrase_report,
)


def _lexicon(*, terms: list[str], exclusions: list[str] | None = None) -> dict:
    return {
        "categories": {
            "test": {
                "group": "review-lead",
                "weight": 3,
                "terms": terms,
            }
        },
        "excluded_terms": exclusions or [],
    }


def _write_transcript(path: Path, messages: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    records = [
        {
            "type": "user",
            "uuid": f"user-{index}",
            "sessionId": "session-scanner",
            "timestamp": f"2026-07-09T12:00:0{index}+00:00",
            "cwd": "/work/project-a",
            "message": {"role": "user", "content": message},
        }
        for index, message in enumerate(messages)
    ]
    records.append(
        {
            "type": "assistant",
            "sessionId": "session-scanner",
            "timestamp": "2026-07-09T12:01:00+00:00",
            "message": {"role": "assistant", "model": "claude-sonnet", "content": "Done."},
        }
    )
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")


@pytest.fixture
def ledger(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Ledger:
    monkeypatch.setenv("S2S_HOME", str(tmp_path / "s2s-home"))
    database = Ledger()
    yield database
    database.close()


def test_literal_boundaries_and_multiword_whitespace_are_compiled_safely() -> None:
    patterns = compile_patterns(_lexicon(terms=["ass", "not working"]))

    hits = match_message("Assess this first; it is not\n\tworking.", patterns)

    assert [(hit.term, hit.count) for hit in hits] == [("not working", 1)]


def test_excluded_terms_remain_review_leads() -> None:
    patterns = compile_patterns(_lexicon(terms=["ambiguous phrase"], exclusions=["ambiguous phrase"]))

    hits = match_message("This ambiguous phrase needs review.", patterns)

    assert len(hits) == 1
    assert hits[0].excluded is True
    assert hits[0].group == "review-lead"
    assert hits[0].weight == 3


def test_user_lexicon_additions_merge_over_seed_categories(tmp_path: Path) -> None:
    overrides = tmp_path / "lexicons"
    overrides.mkdir()
    (overrides / "personal.json").write_text(
        json.dumps(
            {
                "categories": {
                    "agent_callout": {"weight": 7, "terms": ["please comply"]},
                    "personal": {"group": "personal", "weight": 2, "terms": ["my phrase"]},
                },
                "excluded_terms": ["please comply"],
            }
        ),
        encoding="utf-8",
    )

    lexicon = load_lexicon(overrides)
    patterns = compile_patterns(lexicon)
    terms = {(pattern.category, pattern.term): pattern for pattern in patterns}

    assert ("agent_callout", "not what I asked") in terms
    assert terms[("agent_callout", "please comply")].weight == 7
    assert terms[("agent_callout", "please comply")].excluded is True
    assert ("personal", "my phrase") in terms


def test_scanning_is_idempotent_and_persists_meter_denominator(
    ledger: Ledger, tmp_path: Path
) -> None:
    transcript = tmp_path / "project-a" / "session-scanner.jsonl"
    _write_transcript(
        transcript,
        ["Please explain the implementation.", "That is not working at all."],
    )
    lexicon = _lexicon(terms=["not working"])

    first = scan_transcript(transcript, ledger, lexicon=lexicon)
    second = scan_transcript(transcript, ledger, lexicon=lexicon)

    assert (first.total_direct_messages, first.hit_count, first.incidents_created) == (2, 1, 1)
    assert (second.incidents_created, second.duplicates_skipped) == (0, 1)
    incidents = ledger.untriaged_incidents()
    assert len(incidents) == 1
    assert incidents[0].label is None
    assert incidents[0].one_liner is None
    stats = ledger.session_stats("session-scanner")
    assert len(stats) == 1
    assert (stats[0].direct_message_count, stats[0].hit_count) == (2, 1)
    assert stats[0].dominant_model == "claude-sonnet"


def test_pending_queue_items_are_marked_only_after_scanning(ledger: Ledger, tmp_path: Path) -> None:
    transcript = tmp_path / "project-a" / "queue-session.jsonl"
    _write_transcript(transcript, ["This is bad."])
    ledger.enqueue_item(str(transcript), "claude-code")

    results = scan_pending_queue(ledger)

    assert len(results) == 1
    assert results[0].incidents_created == 1
    assert ledger.pending_queue_items() == []


def test_every_lexicon_declares_its_origin() -> None:
    vendored_origin = {
        "project": "petergpt/codex-swear-meter",
        "license": "MIT",
        "copyright": "Copyright (c) 2026 Peter",
        "url": "https://github.com/petergpt/codex-swear-meter",
        "note": "adapted",
    }
    original_origin = {
        "project": "swear-to-skill",
        "license": "MIT",
        "copyright": "Copyright (c) 2026 swear-to-skill contributors",
        "note": "original",
    }
    # Every seed lexicon is either adapted from the vendored upstream project or
    # original to this repo; either way it must say so honestly.
    expected_origin_by_name = {
        "negative_terms.json": vendored_origin,
        "spice_terms.json": vendored_origin,
        "positive_signal_terms.json": original_origin,
    }

    files = sorted(
        path
        for path in LEXICONS_DIR.iterdir()
        if path.is_file() and not path.name.startswith("taxonomy.")
    )
    assert files
    assert {path.name for path in files} == set(expected_origin_by_name)
    assert all(path.suffix == ".json" for path in files)
    assert all(
        json.loads(path.read_text(encoding="utf-8")).get("_origin") == expected_origin_by_name[path.name]
        for path in files
    )


def test_candidate_phrase_mining_is_specific_and_never_auto_applies(tmp_path: Path) -> None:
    candidates = mine_candidate_phrases(
        [
            ("Widget regression keeps recurring", True),
            ("Widget regression keeps recurring", True),
            ("Widget regression keeps recurring", True),
            ("Widget regression appears in a neutral note", False),
        ]
    )
    report = write_candidate_phrase_report(
        [],
        output_path=tmp_path / "candidate_phrases.csv",
        lexicon=_lexicon(terms=["regression"]),
    )

    widget_regression = next(candidate for candidate in candidates if candidate.phrase == "widget regression")
    assert (widget_regression.matched_messages, widget_regression.all_messages) == (3, 4)
    assert widget_regression.specificity == 0.75
    assert report.path.read_text(encoding="utf-8") == "phrase,matched_messages,all_messages,specificity\n"
