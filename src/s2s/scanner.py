"""Deterministic, offline Stage 1 scanning over direct human transcript messages.

Regex matches are review leads only.  They never decide whether a user was truly
frustrated and never assign a failure-mode label; that is the later triage stage's
job.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
import csv
from dataclasses import dataclass
import json
from pathlib import Path
import re
import sqlite3
from typing import Any

from .adapters.claude_code import (
    UserMessage,
    extract_session_metadata,
    extract_user_messages,
    iter_archived_sessions,
)
from .ledger import Ledger
from .paths import resolve_paths


SOURCE = "claude-code"
LEXICONS_DIR = Path(__file__).with_name("lexicons")
EXCLUSION_KEYS = ("excluded_terms", "swear_index_excluded_terms")
TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9']*")

# Phrase mining intentionally removes connective language.  Candidate terms remain
# suggestions for the owner to review, never automatic lexicon mutations.
STOPWORDS = frozenset(
    {
        "a",
        "about",
        "again",
        "all",
        "also",
        "am",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "be",
        "because",
        "been",
        "but",
        "by",
        "can",
        "could",
        "did",
        "do",
        "does",
        "doing",
        "done",
        "for",
        "from",
        "get",
        "go",
        "had",
        "has",
        "have",
        "he",
        "her",
        "here",
        "him",
        "his",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "just",
        "like",
        "me",
        "more",
        "my",
        "no",
        "not",
        "now",
        "of",
        "on",
        "one",
        "or",
        "our",
        "out",
        "please",
        "so",
        "some",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "think",
        "this",
        "to",
        "up",
        "us",
        "use",
        "want",
        "was",
        "we",
        "what",
        "when",
        "where",
        "which",
        "with",
        "would",
        "you",
        "your",
    }
)


@dataclass(frozen=True)
class TermPattern:
    """One compiled review-lead term and the category metadata it carries."""

    category: str
    group: str
    weight: int
    term: str
    excluded: bool
    pattern: re.Pattern[str]


@dataclass(frozen=True)
class TermHit:
    """All occurrences of one matching lexicon term within one message."""

    category: str
    group: str
    weight: int
    term: str
    excluded: bool
    count: int
    first_start: int
    first_end: int


@dataclass(frozen=True)
class Detection:
    """In-memory hit detail for a newly or previously detected message."""

    message: UserMessage
    hits: tuple[TermHit, ...]
    incident_id: int | None


@dataclass(frozen=True)
class ScanResult:
    """One transcript's deterministic scan outcome and denominator totals."""

    transcript_path: Path
    session_id: str
    project: str
    total_direct_messages: int
    hit_count: int
    incidents_created: int
    duplicates_skipped: int
    detections: tuple[Detection, ...]


@dataclass(frozen=True)
class CandidatePhrase:
    """A phrase that is disproportionately common in matched short messages."""

    phrase: str
    matched_messages: int
    all_messages: int
    specificity: float


@dataclass(frozen=True)
class CandidatePhraseReport:
    """The local, inspectable result of candidate-phrase mining."""

    path: Path
    messages_considered: int
    matched_messages: int
    candidate_count: int


def term_to_regex(term: str) -> str:
    """Compile one literal lexicon term using tolerant whitespace and safe boundaries."""

    normalized = term.strip()
    if not normalized:
        raise ValueError("lexicon terms cannot be empty")
    escaped = re.escape(normalized)
    escaped = re.sub(r"\\\s+", r"\\s+", escaped)
    if normalized[0].isalnum():
        escaped = r"(?<![A-Za-z0-9_])" + escaped
    if normalized[-1].isalnum():
        escaped += r"(?![A-Za-z0-9_])"
    return escaped


def load_lexicon(user_lexicons_dir: Path | None = None) -> dict[str, Any]:
    """Merge bundled seed JSONs with user additions from ``S2S_HOME/lexicons``.

    A user's category contributes terms to the matching seed category and may
    replace its category metadata (group, signal, weight, or description).  Root
    exclusions are additive so an owner can suppress headline treatment of an
    ambiguous phrase without losing it as a review lead.
    """

    lexicon: dict[str, Any] = {
        "version": 1,
        "categories": {},
        "excluded_terms": [],
        "swear_index_excluded_terms": [],
    }
    for path in _json_files(LEXICONS_DIR):
        if path.name.startswith("taxonomy."):
            continue
        _merge_lexicon(lexicon, _read_lexicon_file(path), path)

    override_dir = user_lexicons_dir
    if override_dir is None:
        override_dir = resolve_paths().home / "lexicons"
    for path in _json_files(override_dir):
        _merge_lexicon(lexicon, _read_lexicon_file(path), path)
    return lexicon


def compile_patterns(lexicon: dict[str, Any]) -> list[TermPattern]:
    """Return stable, case-insensitive literal patterns for every valid term."""

    exclusions = _excluded_terms(lexicon)
    patterns: list[TermPattern] = []
    categories = lexicon.get("categories", {})
    if not isinstance(categories, dict):
        raise ValueError("lexicon categories must be an object")
    for category, raw_spec in categories.items():
        if not isinstance(category, str) or not isinstance(raw_spec, dict):
            continue
        terms = raw_spec.get("terms", [])
        if not isinstance(terms, list):
            continue
        group = raw_spec.get("group") or raw_spec.get("signal") or category
        try:
            weight = int(raw_spec.get("weight", 1))
        except (TypeError, ValueError):
            weight = 1
        for raw_term in terms:
            if not isinstance(raw_term, str) or not raw_term.strip():
                continue
            term = raw_term.strip()
            patterns.append(
                TermPattern(
                    category=category,
                    group=str(group),
                    weight=weight,
                    term=term,
                    excluded=term.casefold() in exclusions,
                    pattern=re.compile(term_to_regex(term), re.IGNORECASE),
                )
            )
    return patterns


def match_message(message: str, patterns: Sequence[TermPattern]) -> list[TermHit]:
    """Return category-aware term hits in source order for one direct user message."""

    hits: list[TermHit] = []
    for pattern in patterns:
        matches = list(pattern.pattern.finditer(message))
        if not matches:
            continue
        hits.append(
            TermHit(
                category=pattern.category,
                group=pattern.group,
                weight=pattern.weight,
                term=pattern.term,
                excluded=pattern.excluded,
                count=len(matches),
                first_start=matches[0].start(),
                first_end=matches[0].end(),
            )
        )
    return sorted(hits, key=lambda hit: (hit.first_start, hit.category, hit.term))


def scan_transcript(
    path: Path,
    ledger: Ledger,
    *,
    lexicon: dict[str, Any] | None = None,
) -> ScanResult:
    """Scan a Claude Code transcript and persist label-free detection leads.

    The durable incident is deliberately minimal.  Detailed term hits are held in
    the returned result for the immediate caller, while future triage derives its
    context from the archived source.  Re-scans are harmless: the ledger's unique
    key and this preflight check identify the same direct user message.
    """

    transcript_path = Path(path)
    if not transcript_path.is_file():
        raise FileNotFoundError(f"transcript does not exist: {transcript_path}")

    active_lexicon = lexicon if lexicon is not None else load_lexicon()
    patterns = compile_patterns(active_lexicon)
    metadata = extract_session_metadata(transcript_path)
    detections: list[Detection] = []
    incidents_created = 0
    duplicates_skipped = 0

    for user_message in extract_user_messages(transcript_path):
        hits = tuple(match_message(user_message.message, patterns))
        if not hits:
            continue
        occurred_at = user_message.timestamp or ""
        incident_id: int | None = None
        if ledger.has_incident_scan_key(
            session_id=user_message.session_id,
            occurred_at=occurred_at,
            message=user_message.message,
        ):
            duplicates_skipped += 1
        else:
            try:
                incident_id = ledger.create_scanned_incident(
                    source=SOURCE,
                    session_id=user_message.session_id,
                    project=user_message.project,
                    message=user_message.message,
                    occurred_at=occurred_at,
                )
                incidents_created += 1
            except sqlite3.IntegrityError:
                # The database constraint remains the concurrency-safe authority;
                # a second scanner may have created this exact incident after the
                # preflight read.
                if not ledger.has_incident_scan_key(
                    session_id=user_message.session_id,
                    occurred_at=occurred_at,
                    message=user_message.message,
                ):
                    raise
                duplicates_skipped += 1
        detections.append(Detection(message=user_message, hits=hits, incident_id=incident_id))

    ledger.upsert_session_stats(
        source=SOURCE,
        session_id=metadata.session_id,
        project=metadata.project,
        dominant_model=metadata.dominant_model,
        direct_message_count=metadata.direct_message_count,
        hit_count=len(detections),
        first_timestamp=metadata.first_timestamp,
        last_timestamp=metadata.last_timestamp,
    )
    return ScanResult(
        transcript_path=transcript_path,
        session_id=metadata.session_id,
        project=metadata.project,
        total_direct_messages=metadata.direct_message_count,
        hit_count=len(detections),
        incidents_created=incidents_created,
        duplicates_skipped=duplicates_skipped,
        detections=tuple(detections),
    )


def scan_pending_queue(ledger: Ledger) -> list[ScanResult]:
    """Process queued Claude Code transcripts, marking each only after a scan succeeds."""

    results: list[ScanResult] = []
    for item in ledger.pending_queue_items():
        if item.source != SOURCE:
            continue
        result = scan_transcript(Path(item.item_path), ledger)
        ledger.mark_queue_item_processed(item.id)
        results.append(result)
    return results


def mine_candidate_phrases(records: Iterable[tuple[str, bool]]) -> list[CandidatePhrase]:
    """Rank one-, two-, and three-grams by their matched-message specificity."""

    all_counts: Counter[str] = Counter()
    matched_counts: Counter[str] = Counter()
    for message, is_match in records:
        if len(message) > 1_000:
            continue
        tokens = [
            token.casefold().strip("'")
            for token in TOKEN_RE.findall(message)
            if len(token.strip("'")) > 2 and token.casefold().strip("'") not in STOPWORDS
        ]
        phrases = set(_iter_ngrams(tokens, 1)) | set(_iter_ngrams(tokens, 2)) | set(_iter_ngrams(tokens, 3))
        all_counts.update(phrases)
        if is_match:
            matched_counts.update(phrases)

    candidates = [
        CandidatePhrase(
            phrase=phrase,
            matched_messages=matched_count,
            all_messages=all_counts[phrase],
            specificity=round(matched_count / all_counts[phrase], 4),
        )
        for phrase, matched_count in matched_counts.items()
        if matched_count >= 3 and all_counts[phrase]
    ]
    return sorted(
        candidates,
        key=lambda candidate: (
            -candidate.specificity,
            -candidate.matched_messages,
            candidate.phrase,
        ),
    )


def write_candidate_phrase_report(
    transcript_paths: Iterable[Path] | None = None,
    *,
    output_path: Path | None = None,
    lexicon: dict[str, Any] | None = None,
    limit: int = 200,
) -> CandidatePhraseReport:
    """Write local candidate suggestions from archived direct user messages.

    Supplying paths makes this fixture-friendly; omitting them scans the owned
    archive.  Suggestions are never applied to the user lexicon.
    """

    active_lexicon = lexicon if lexicon is not None else load_lexicon()
    patterns = compile_patterns(active_lexicon)
    paths = iter_archived_sessions() if transcript_paths is None else transcript_paths
    seen: set[tuple[str, str | None, str]] = set()
    records: list[tuple[str, bool]] = []
    matched_messages = 0
    for transcript_path in paths:
        for user_message in extract_user_messages(Path(transcript_path)):
            identity = (user_message.session_id, user_message.timestamp, user_message.message)
            if identity in seen:
                continue
            seen.add(identity)
            is_match = bool(match_message(user_message.message, patterns))
            records.append((user_message.message, is_match))
            matched_messages += int(is_match)

    candidates = mine_candidate_phrases(records)
    report_path = output_path or (resolve_paths().home / "reports" / "candidate_phrases.csv")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with report_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=("phrase", "matched_messages", "all_messages", "specificity"),
        )
        writer.writeheader()
        writer.writerows(
            {
                "phrase": candidate.phrase,
                "matched_messages": candidate.matched_messages,
                "all_messages": candidate.all_messages,
                "specificity": candidate.specificity,
            }
            for candidate in candidates[:limit]
        )
    return CandidatePhraseReport(
        path=report_path,
        messages_considered=len(records),
        matched_messages=matched_messages,
        candidate_count=min(len(candidates), limit),
    )


def _json_files(directory: Path) -> list[Path]:
    return sorted(path for path in directory.glob("*.json") if path.is_file()) if directory.is_dir() else []


def _read_lexicon_file(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"unable to read lexicon {path}: {error}") from error
    if not isinstance(document, dict):
        raise ValueError(f"lexicon {path} must contain a JSON object")
    return document


def _merge_lexicon(target: dict[str, Any], incoming: dict[str, Any], path: Path) -> None:
    categories = incoming.get("categories", {})
    if not isinstance(categories, dict):
        raise ValueError(f"lexicon {path} categories must be an object")
    target_categories = target["categories"]
    for name, raw_spec in categories.items():
        if not isinstance(name, str) or not isinstance(raw_spec, dict):
            continue
        existing = target_categories.get(name, {})
        merged = dict(existing) if isinstance(existing, dict) else {}
        existing_terms = merged.get("terms", [])
        incoming_terms = raw_spec.get("terms", [])
        if not isinstance(existing_terms, list):
            existing_terms = []
        if not isinstance(incoming_terms, list):
            raise ValueError(f"lexicon {path} category {name!r} terms must be a list")
        merged.update({key: value for key, value in raw_spec.items() if key != "terms"})
        merged["terms"] = _unique_terms([*existing_terms, *incoming_terms])
        target_categories[name] = merged

    for key in EXCLUSION_KEYS:
        raw_terms = incoming.get(key, [])
        if not isinstance(raw_terms, list):
            raise ValueError(f"lexicon {path} {key} must be a list")
        target[key] = _unique_terms([*target[key], *raw_terms])


def _unique_terms(terms: Iterable[object]) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for raw_term in terms:
        if not isinstance(raw_term, str) or not raw_term.strip():
            continue
        term = raw_term.strip()
        key = term.casefold()
        if key not in seen:
            seen.add(key)
            unique.append(term)
    return unique


def _excluded_terms(lexicon: dict[str, Any]) -> set[str]:
    return {
        term.casefold()
        for key in EXCLUSION_KEYS
        for term in lexicon.get(key, [])
        if isinstance(term, str) and term.strip()
    }


def _iter_ngrams(tokens: Sequence[str], size: int) -> Iterable[str]:
    for start in range(0, len(tokens) - size + 1):
        yield " ".join(tokens[start : start + size])
