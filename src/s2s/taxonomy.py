"""Versioned, user-state taxonomy lifecycle for closed-set triage."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any

from .paths import resolve_paths


TAXONOMY_PATH = Path(__file__).with_name("lexicons") / "taxonomy.v1.json"
_VERSIONED_NAME = re.compile(r"^taxonomy\.v(?P<version>[1-9][0-9]*)\.json$")
_LABEL_NAME = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")


@dataclass(frozen=True)
class TaxonomyLabel:
    """One canonical label and the guidance shown to the Triager."""

    label: str
    gist: str
    examples: tuple[str, ...]
    counter_example: str


@dataclass(frozen=True)
class Taxonomy:
    """The current closed-set taxonomy, selected from user state when present."""

    version: int
    labels: tuple[TaxonomyLabel, ...]


def taxonomy_dir() -> Path:
    """Return the user-owned directory that holds gardened taxonomy versions."""

    return resolve_paths().home / "taxonomy"


def load_current_taxonomy() -> Taxonomy:
    """Load the highest user-state version, falling back to the vendored seed."""

    document = _load_current_document()
    return _taxonomy_from_document(document)


def list_labels() -> tuple[TaxonomyLabel, ...]:
    """Return the current fixed label menu, including the legal ``other`` choice."""

    return load_current_taxonomy().labels


def label_names() -> tuple[str, ...]:
    """Return the exact values permitted by the dynamic triage schema."""

    return tuple(entry.label for entry in list_labels())


def apply_new_label(
    *,
    name: str,
    gist: str,
    examples: list[str] | tuple[str, ...],
    evidence_incident_ids: list[int] | tuple[int, ...],
) -> Taxonomy:
    """Append one capable-model-approved label as a new user-state version."""

    if not _LABEL_NAME.fullmatch(name):
        raise ValueError("taxonomy label names must be kebab-case")
    if not isinstance(gist, str) or not gist.strip():
        raise ValueError("taxonomy labels require a non-empty gist")
    if not isinstance(examples, (list, tuple)) or not 2 <= len(examples) <= 3 or not all(
        isinstance(example, str) and example.strip() for example in examples
    ):
        raise ValueError("taxonomy labels require 2-3 non-empty examples")
    evidence = _validated_evidence_ids(evidence_incident_ids)

    document = _load_current_document()
    existing = {str(item.get("label")) for item in _raw_labels(document)}
    if name in existing:
        raise ValueError(f"taxonomy label {name!r} already exists")
    if name == "other":
        raise ValueError("the other escape hatch cannot be proposed as a new label")

    raw_labels = _raw_labels(document)
    other_index = next(index for index, item in enumerate(raw_labels) if item["label"] == "other")
    raw_labels.insert(
        other_index,
        {
            "label": name,
            "gist": gist.strip(),
            "examples": [example.strip() for example in examples],
            "counter_example": "A supplied canonical label plainly fits better.",
        },
    )
    return _write_next_version(
        document,
        raw_labels,
        {
            "action": "add-label",
            "label": name,
            "evidence_incident_ids": evidence,
        },
    )


def apply_merge(*, survivor: str, absorbed: str, reason: str) -> Taxonomy:
    """Merge two existing canonical labels and retain the auditable rename map."""

    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("a taxonomy merge requires a non-empty reason")
    document = _load_current_document()
    raw_labels = _raw_labels(document)
    labels = {str(item["label"]) for item in raw_labels}
    if survivor == absorbed:
        raise ValueError("a taxonomy merge requires distinct labels")
    if survivor not in labels or absorbed not in labels:
        raise ValueError("taxonomy merges require existing labels")
    if "other" in {survivor, absorbed}:
        raise ValueError("the other escape hatch cannot be merged")
    remaining = [item for item in raw_labels if item["label"] != absorbed]
    return _write_next_version(
        document,
        remaining,
        {
            "action": "merge-labels",
            "survivor": survivor,
            "absorbed": absorbed,
            "reason": reason.strip(),
            "rename_map": {absorbed: survivor},
        },
    )


def _load_current_document() -> dict[str, Any]:
    candidates: list[tuple[int, Path]] = []
    directory = taxonomy_dir()
    if directory.is_dir():
        for path in directory.iterdir():
            match = _VERSIONED_NAME.fullmatch(path.name)
            if match is not None and path.is_file():
                candidates.append((int(match.group("version")), path))
    if candidates:
        _, path = max(candidates, key=lambda item: item[0])
        return _load_document(path)
    return _load_document(TAXONOMY_PATH)


def _load_document(path: Path) -> dict[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("taxonomy must be a JSON object")
    _taxonomy_from_document(document)
    return document


def _taxonomy_from_document(document: dict[str, Any]) -> Taxonomy:
    version = document.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise ValueError("taxonomy must contain a positive integer version")
    labels: list[TaxonomyLabel] = []
    for raw in _raw_labels(document):
        label, gist, examples, counter_example = (
            raw.get("label"), raw.get("gist"), raw.get("examples"), raw.get("counter_example")
        )
        if (
            not isinstance(label, str)
            or not _LABEL_NAME.fullmatch(label)
            or not isinstance(gist, str)
            or not gist.strip()
            or not isinstance(examples, list)
            or not 2 <= len(examples) <= 3
            or not all(isinstance(example, str) and example.strip() for example in examples)
            or not isinstance(counter_example, str)
            or not counter_example.strip()
        ):
            raise ValueError("taxonomy labels require kebab-case label, gist, 2-3 examples, and counter_example")
        labels.append(TaxonomyLabel(label, gist, tuple(examples), counter_example))
    names = {entry.label for entry in labels}
    if len(names) != len(labels) or "other" not in names:
        raise ValueError("taxonomy labels must be unique and include other")
    history = document.get("history", [])
    if not isinstance(history, list):
        raise ValueError("taxonomy history must be an array")
    return Taxonomy(version=version, labels=tuple(labels))


def _raw_labels(document: dict[str, Any]) -> list[dict[str, Any]]:
    raw_labels = document.get("labels")
    if not isinstance(raw_labels, list) or not all(isinstance(item, dict) for item in raw_labels):
        raise ValueError("taxonomy labels must be an array of objects")
    # JSON round-tripping preserves an immutable historical document while making
    # the next-version edits independent from the loaded object.
    return json.loads(json.dumps(raw_labels))


def _validated_evidence_ids(ids: list[int] | tuple[int, ...]) -> list[int]:
    if not isinstance(ids, (list, tuple)) or len(ids) < 3:
        raise ValueError("a new label requires at least three evidence incident IDs")
    if not all(isinstance(item, int) and not isinstance(item, bool) and item > 0 for item in ids):
        raise ValueError("evidence incident IDs must be positive integers")
    if len(set(ids)) != len(ids):
        raise ValueError("evidence incident IDs must be unique")
    return list(ids)


def _write_next_version(
    current: dict[str, Any], labels: list[dict[str, Any]], entry: dict[str, Any]
) -> Taxonomy:
    version = int(current["version"]) + 1
    changed_at = datetime.now(timezone.utc).isoformat()
    history = json.loads(json.dumps(current.get("history", [])))
    history.append({"version": version, "changed_at": changed_at, **entry})
    document = {"version": version, "changed_at": changed_at, "labels": labels, "history": history}
    _taxonomy_from_document(document)
    directory = taxonomy_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"taxonomy.v{version}.json"
    if path.exists():
        raise FileExistsError(f"refusing to overwrite existing taxonomy version {version}")
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)
    return _taxonomy_from_document(document)
