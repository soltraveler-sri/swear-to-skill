"""Validate the immutable synthetic golden corpus through production code.

This is deliberately an authoring guardrail, not eval logic.  It calls the real
adapters and scanner matcher so annotations cannot silently drift from parser
or lexicon behavior.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import sys
from typing import Any

ROOT = Path(__file__).resolve().parent / "corpus" / "v1"
SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
REQUIRED_INCIDENT_KEYS = {
    "source",
    "session",
    "uuid",
    "authentic",
    "cluster_id",
    "remedy_worthy",
    "expected_remedy_shape",
    "lexicon_hit_expected",
    "rationale",
}
VALID_SHAPES = {"claude-md", "hook", "skill", "benchmark-only", "none"}
PATH_RE = re.compile(r"(?<![A-Za-z0-9])/(?:[A-Za-z0-9._-]+/?)+")
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
HANDLE_RE = re.compile(r"(?<![A-Za-z0-9])@[A-Za-z][A-Za-z0-9_-]*")


class CorpusLintError(ValueError):
    """A corpus invariant that would invalidate a replay baseline."""


def _fail(message: str) -> None:
    raise CorpusLintError(message)


def _load_manifest(root: Path) -> dict[str, Any]:
    try:
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        _fail(f"cannot load manifest: {error}")
    if not isinstance(manifest, dict):
        _fail("manifest must be an object")
    return manifest


def _privacy_scan(root: Path) -> None:
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        if EMAIL_RE.search(text) or HANDLE_RE.search(text):
            _fail(f"privacy scan found email or handle in {path.relative_to(root)}")
        for match in PATH_RE.finditer(text):
            candidate = match.group(0).rstrip("/.,;:')\"]}")
            if not candidate.startswith("/work/fake-"):
                _fail(f"privacy scan found non-fake path {candidate!r} in {path.relative_to(root)}")


def lint_corpus(root: Path = ROOT) -> dict[str, dict[str, int]]:
    """Raise on invalid corpus content and return compact source statistics."""

    # Imports stay here so this script also works as ``python evals/lint_corpus.py``.
    if str(SOURCE_ROOT) not in sys.path:
        sys.path.insert(0, str(SOURCE_ROOT))
    from s2s.adapters import codex
    from s2s.adapters import claude_code
    from s2s.scanner import compile_patterns, load_lexicon, match_message

    root = Path(root)
    manifest = _load_manifest(root)
    if manifest.get("version") != "v1" or manifest.get("synthetic") is not True:
        _fail("manifest must identify synthetic v1")
    definitions = manifest.get("cluster_definitions")
    if not isinstance(definitions, dict) or not 4 <= len(definitions) <= 5:
        _fail("manifest needs four or five named cluster definitions")
    taxonomy = json.loads((SOURCE_ROOT / "s2s" / "lexicons" / "taxonomy.v1.json").read_text(encoding="utf-8"))
    canonical_labels = {
        entry.get("label")
        for entry in taxonomy.get("labels", [])
        if isinstance(entry, dict) and isinstance(entry.get("label"), str)
    }
    if not set(definitions).issubset(canonical_labels):
        _fail("named cluster definitions must use shipped taxonomy labels")
    if not isinstance(manifest.get("expected_convergence"), str):
        _fail("manifest needs expected_convergence text")
    incidents = manifest.get("incidents")
    if not isinstance(incidents, list) or not 35 <= len(incidents) <= 50:
        _fail("manifest needs 35-50 incidents")

    _privacy_scan(root)
    patterns = compile_patterns(load_lexicon())
    messages: dict[tuple[str, str, str], str] = {}
    session_counts = {"claude-code": 0, "codex": 0}
    for path in sorted((root / "claude").glob("*.jsonl")):
        session_counts["claude-code"] += 1
        metadata = claude_code.extract_session_metadata(path)
        if metadata.malformed_line_count:
            _fail(f"Claude transcript is malformed: {path.relative_to(root)}")
        reference = str(path.relative_to(root))
        for message in claude_code.extract_user_messages(path):
            if not message.uuid:
                _fail(f"Claude direct message has no uuid: {reference}")
            messages[("claude-code", reference, message.uuid)] = message.message
    for path in sorted((root / "codex" / "sessions").glob("*.jsonl")):
        session_counts["codex"] += 1
        metadata = codex.extract_session_metadata(path)
        if metadata.malformed_line_count:
            _fail(f"Codex rollout is malformed: {path.relative_to(root)}")
        reference = str(path.relative_to(root))
        for message in codex.extract_user_messages(path):
            if not message.uuid:
                _fail(f"Codex direct message has no uuid: {reference}")
            messages[("codex", reference, message.uuid)] = message.message
    if not 15 <= session_counts["claude-code"] <= 20 or not 3 <= session_counts["codex"] <= 5:
        _fail(f"unexpected source session counts: {session_counts}")

    history = codex.extract_user_messages(root / "codex" / "history.jsonl")
    rollout_texts = {(key[2].split(":", 1)[0], value) for key, value in messages.items() if key[0] == "codex"}
    if {(item.session_id, item.message) for item in history} != rollout_texts:
        _fail("Codex history.jsonl does not exactly mirror rollout user messages")

    seen: set[tuple[str, str, str]] = set()
    stats = {source: {"sessions": session_counts[source], "incidents": 0, "clusters": 0, "decoys": 0, "known_misses": 0} for source in session_counts}
    cluster_sources: dict[str, set[str]] = {cluster: set() for cluster in definitions}
    source_clusters: dict[str, set[str]] = {source: set() for source in session_counts}
    for index, incident in enumerate(incidents):
        if not isinstance(incident, dict) or REQUIRED_INCIDENT_KEYS - incident.keys():
            _fail(f"incident {index} lacks required fields")
        source = incident["source"]
        session = incident["session"]
        uuid = incident["uuid"]
        if source not in session_counts or not all(isinstance(value, str) for value in (session, uuid)):
            _fail(f"incident {index} has invalid source/session/uuid")
        key = (source, session, uuid)
        if key in seen or key not in messages:
            _fail(f"incident {index} does not resolve to a direct adapter message: {key}")
        seen.add(key)
        if not isinstance(incident["authentic"], bool) or not isinstance(incident["remedy_worthy"], bool):
            _fail(f"incident {index} authenticity and remedy flags must be booleans")
        if incident["expected_remedy_shape"] not in VALID_SHAPES:
            _fail(f"incident {index} has invalid remedy shape")
        cluster = incident["cluster_id"]
        if cluster not in definitions and cluster not in {"singleton", "decoy"}:
            _fail(f"incident {index} has unknown cluster {cluster!r}")
        if cluster == "decoy" and (incident["authentic"] or incident["remedy_worthy"] or incident["expected_remedy_shape"] != "none"):
            _fail(f"decoy {index} must be inauthentic and have no remedy")
        actual_hit = bool(match_message(messages[key], patterns))
        if actual_hit != incident["lexicon_hit_expected"]:
            _fail(f"incident {index} lexicon expectation disagrees with real scanner")
        source_stats = stats[source]
        source_stats["incidents"] += 1
        if cluster in definitions:
            source_clusters[source].add(cluster)
            cluster_sources[cluster].add(source)
        if cluster == "decoy":
            source_stats["decoys"] += 1
        if not incident["lexicon_hit_expected"]:
            source_stats["known_misses"] += 1

    if len(seen) != len(incidents) or set(messages).issuperset(seen) is False:
        _fail("manifest references must be unique direct messages")
    if any(not sources for sources in cluster_sources.values()):
        _fail("every named cluster needs an incident")
    fixtures = manifest.get("pre_existing_remedies")
    if not isinstance(fixtures, list) or len(fixtures) != 2:
        _fail("manifest needs two pre-existing remedy fixtures")
    fixture_ids = {item.get("id") for item in fixtures if isinstance(item, dict)}
    duplicate_ids = {item.get("pre_existing_remedy_id") for item in incidents if isinstance(item, dict) and "pre_existing_remedy_id" in item}
    if fixture_ids != duplicate_ids:
        _fail("every pre-existing remedy must have exactly one planted near duplicate")
    for fixture in fixtures:
        if not isinstance(fixture, dict) or not isinstance(fixture.get("path"), str) or not (root / fixture["path"]).is_file():
            _fail("pre-existing remedy fixture is missing")
        if not isinstance(fixture.get("near_duplicate_incidents"), list) or len(fixture["near_duplicate_incidents"]) != 1:
            _fail("each pre-existing remedy needs one near-duplicate annotation")
        if fixture["near_duplicate_incidents"][0] not in {item["uuid"] for item in incidents if isinstance(item, dict) and isinstance(item.get("uuid"), str)}:
            _fail("pre-existing remedy near duplicate does not name an incident")
    for source, clusters in source_clusters.items():
        stats[source]["clusters"] = len(clusters)
    return stats


def main() -> int:
    try:
        stats = lint_corpus()
    except CorpusLintError as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1
    print("PASS:", json.dumps(stats, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
