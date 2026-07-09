"""Read-only access to the current closed-set triage taxonomy."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path


TAXONOMY_PATH = Path(__file__).with_name("lexicons") / "taxonomy.v1.json"


@dataclass(frozen=True)
class TaxonomyLabel:
    """One canonical label and the guidance shown to the Triager."""

    label: str
    gist: str
    examples: tuple[str, ...]
    counter_example: str


@dataclass(frozen=True)
class Taxonomy:
    """The current immutable taxonomy data; gardening belongs to a later stage."""

    version: int
    labels: tuple[TaxonomyLabel, ...]


def load_current_taxonomy() -> Taxonomy:
    """Load the bundled current taxonomy without modifying it."""

    document = json.loads(TAXONOMY_PATH.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or not isinstance(document.get("version"), int):
        raise ValueError("taxonomy must contain an integer version")
    raw_labels = document.get("labels")
    if not isinstance(raw_labels, list):
        raise ValueError("taxonomy labels must be an array")
    labels: list[TaxonomyLabel] = []
    for raw in raw_labels:
        if not isinstance(raw, dict):
            raise ValueError("taxonomy label entries must be objects")
        label, gist, examples, counter_example = (
            raw.get("label"), raw.get("gist"), raw.get("examples"), raw.get("counter_example")
        )
        if (
            not isinstance(label, str)
            or not label
            or not isinstance(gist, str)
            or not gist
            or not isinstance(examples, list)
            or not 2 <= len(examples) <= 3
            or not all(isinstance(example, str) and example for example in examples)
            or not isinstance(counter_example, str)
            or not counter_example
        ):
            raise ValueError("taxonomy labels require label, gist, 2-3 examples, and counter_example")
        labels.append(TaxonomyLabel(label, gist, tuple(examples), counter_example))
    if len({entry.label for entry in labels}) != len(labels) or "other" not in {entry.label for entry in labels}:
        raise ValueError("taxonomy labels must be unique and include other")
    return Taxonomy(version=document["version"], labels=tuple(labels))


def list_labels() -> tuple[TaxonomyLabel, ...]:
    """Return the current fixed label menu, including the legal ``other`` choice."""

    return load_current_taxonomy().labels


def label_names() -> tuple[str, ...]:
    """Return the exact values permitted by the dynamic triage schema."""

    return tuple(entry.label for entry in list_labels())
