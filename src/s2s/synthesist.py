"""Stage 4: cautious remedy routing over promoted failure-mode clusters."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
import json
from pathlib import Path
import re

from .config import load_config
from .ledger import Incident, Ledger
from .notify import emit
from .llm import call, estimate_and_confirm, load_prompt
from .taxonomy import list_labels
from .triager import context_for_incident


PROMPT_NAME = "synthesize"
PROMPT_VERSION = 1
SKILL_NAME_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
MANAGED_BLOCK_RE = re.compile(r"<!--\s*s2s:begin\s*-->(.*?)<!--\s*s2s:end\s*-->", re.DOTALL)


class SynthesisValidationError(ValueError):
    """Raised when a schema-valid Synthesist response violates Stage 4 laws."""


@dataclass(frozen=True)
class SynthesisResult:
    """The durable proposal IDs produced for one promoted label group."""

    label: str
    incident_ids: tuple[int, ...]
    proposal_ids: tuple[int, ...]


@dataclass(frozen=True)
class RemedySurface:
    """Bounded, non-sensitive digest of remedies that may overlap this proposal."""

    skills: tuple[tuple[str, str], ...]
    claude_md_blocks: tuple[tuple[str, str], ...]
    proposals: tuple[tuple[str, str], ...]

    @property
    def references(self) -> tuple[str, ...]:
        return tuple(reference for reference, _ in (*self.skills, *self.claude_md_blocks, *self.proposals))


def synthesize_pending(
    ledger: Ledger,
    *,
    assume_yes: bool = False,
    skills_dir: Path | None = None,
    global_claude_md_path: Path | None = None,
    project_claude_md_paths: Mapping[str, Path] | Iterable[Path] | None = None,
) -> list[SynthesisResult]:
    """Synthesize each promoted label group once, preserving all-or-nothing resume safety."""

    groups: dict[str, list[Incident]] = defaultdict(list)
    for incident in ledger.incidents_in_state("promoted"):
        if not incident.label:
            raise SynthesisValidationError(f"promoted incident {incident.id} has no taxonomy label")
        groups[incident.label].append(incident)
    if not groups:
        return []

    model = load_config().models.synthesize
    if not estimate_and_confirm(len(groups), model, assume_yes=assume_yes):
        return []
    template, schema = load_prompt(PROMPT_NAME, PROMPT_VERSION)
    default_project_paths = project_claude_md_paths
    if default_project_paths is None:
        default_project_paths = _incident_project_claude_md_paths(groups.values())
    results: list[SynthesisResult] = []
    for label, incidents in sorted(groups.items()):
        # Earlier groups in this same resumable run are pending remedies too, so
        # refresh the ledger portion before each independent synthesis call.
        surface = collect_remedy_surface(
            ledger,
            skills_dir=skills_dir,
            global_claude_md_path=global_claude_md_path,
            project_claude_md_paths=default_project_paths,
        )
        response = _call_validated(
            render_synthesis_prompt(template, label, incidents, surface),
            schema=schema,
            model=model,
            incident_ids={incident.id for incident in incidents},
            surface_references=set(surface.references),
        )
        normalized = _normalize_response(response, singleton=_singleton_group(ledger, incidents))
        proposal_ids = ledger.create_synthesis_proposals(
            normalized,
            promoted_incident_ids=[incident.id for incident in incidents],
        )
        results.append(
            SynthesisResult(label, tuple(incident.id for incident in incidents), tuple(proposal_ids))
        )
        if proposal_ids:
            # Emit only after the ledger commit so a notification never
            # claims work that was rolled back.
            emit("proposal_pending", proposal_count=len(proposal_ids))
    return results


def collect_remedy_surface(
    ledger: Ledger,
    *,
    skills_dir: Path | None = None,
    global_claude_md_path: Path | None = None,
    project_claude_md_paths: Mapping[str, Path] | Iterable[Path] | None = None,
) -> RemedySurface:
    """Read only digest-safe existing remedy metadata, with injectable paths for tests."""

    claude_home = Path.home() / ".claude"
    skills_root = skills_dir if skills_dir is not None else claude_home / "skills"
    global_path = global_claude_md_path if global_claude_md_path is not None else claude_home / "CLAUDE.md"
    skill_rows: list[tuple[str, str]] = []
    if skills_root.is_dir():
        for path in sorted(skills_root.glob("*/SKILL.md")):
            try:
                frontmatter, _ = parse_skill_markdown(path.read_text(encoding="utf-8"))
            except (OSError, SynthesisValidationError):
                continue
            skill_rows.append((f"skill:{path}", f"name={frontmatter['name']} | description={frontmatter['description']}"))

    paths = [global_path, *_project_paths(project_claude_md_paths)]
    block_rows: list[tuple[str, str]] = []
    seen_paths: set[Path] = set()
    for path in paths:
        if path in seen_paths or not path.is_file():
            continue
        seen_paths.add(path)
        try:
            blocks = MANAGED_BLOCK_RE.findall(path.read_text(encoding="utf-8"))
        except OSError:
            continue
        for index, block in enumerate(blocks, start=1):
            compact = block.strip()
            if compact:
                block_rows.append((f"claude-md:{path}#{index}", compact))
    return RemedySurface(tuple(skill_rows), tuple(block_rows), tuple(ledger.proposal_surface_rows()))


def render_synthesis_prompt(
    template: str,
    label: str,
    incidents: Sequence[Incident],
    surface: RemedySurface,
) -> str:
    """Render the complete evidence and every dedup target for one label group."""

    evidence = _bounded_evidence_packs(incidents, load_config().curator.context_char_budget)
    replacements = {
        "{{TAXONOMY_GIST}}": _taxonomy_gist(label),
        "{{EVIDENCE_PACKS}}": evidence,
        "{{SKILL_DIGESTS}}": _render_surface_rows(surface.skills),
        "{{CLAUDE_MD_DIGESTS}}": _render_surface_rows(surface.claude_md_blocks),
        "{{PROPOSAL_DIGESTS}}": _render_surface_rows(surface.proposals),
    }
    rendered = template
    for marker, value in replacements.items():
        rendered = rendered.replace(marker, value)
    if "{{" in rendered or "}}" in rendered:
        raise ValueError("synthesize prompt template contains an unknown placeholder")
    return rendered


def parse_skill_markdown(markdown: str) -> tuple[dict[str, str], str]:
    """Parse the small YAML frontmatter contract Claude Code uses for SKILL.md."""

    if not markdown.startswith("---\n"):
        raise SynthesisValidationError("SKILL.md must start with YAML frontmatter")
    closing = markdown.find("\n---\n", 4)
    if closing < 0:
        raise SynthesisValidationError("SKILL.md frontmatter must end with ---")
    raw_frontmatter = markdown[4:closing]
    body = markdown[closing + 5 :].strip()
    fields: dict[str, str] = {}
    for line in raw_frontmatter.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            raise SynthesisValidationError("SKILL.md frontmatter is not simple YAML")
        key, value = line.split(":", 1)
        key, value = key.strip(), value.strip()
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", key) or not value:
            raise SynthesisValidationError("SKILL.md frontmatter contains an invalid field")
        if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            value = value[1:-1]
        fields[key] = value
    name, description = fields.get("name", ""), fields.get("description", "")
    if not SKILL_NAME_RE.fullmatch(name):
        raise SynthesisValidationError("SKILL.md name must be kebab-case")
    if not description.strip():
        raise SynthesisValidationError("SKILL.md description must be non-empty")
    if not body:
        raise SynthesisValidationError("SKILL.md body must be non-empty")
    return {"name": name, "description": description}, body


def _call_validated(
    prompt: str,
    *,
    schema: dict[str, object],
    model: str,
    incident_ids: set[int],
    surface_references: set[str],
) -> dict[str, object]:
    """Retry once when code-only artifact and partition checks reject valid JSON."""

    retry_note = "\n\nYour prior draft failed local validation. Correct the SKILL.md/frontmatter, dedup, and evidence partition exactly."
    last_error: SynthesisValidationError | None = None
    for attempt in range(2):
        response = call(
            prompt if attempt == 0 else prompt + retry_note,
            schema=schema,
            model=model,
            stage="synthesize",
        )
        try:
            _validate_response(response, incident_ids=incident_ids, surface_references=surface_references)
        except SynthesisValidationError as error:
            last_error = error
            continue
        return response
    assert last_error is not None
    raise last_error


def _validate_response(
    response: Mapping[str, object],
    *,
    incident_ids: set[int],
    surface_references: set[str],
) -> None:
    proposals = response.get("split")
    if proposals is None:
        if _validate_proposal(response, incident_ids, surface_references) != incident_ids:
            raise SynthesisValidationError("proposal evidence must cover every promoted incident")
        return
    if not isinstance(proposals, list) or len(proposals) < 2:
        raise SynthesisValidationError("split must contain at least two proposals")
    seen: set[int] = set()
    for proposal in proposals:
        if not isinstance(proposal, Mapping):
            raise SynthesisValidationError("split proposals must be objects")
        ids = _validate_proposal(proposal, incident_ids, surface_references)
        if seen.intersection(ids):
            raise SynthesisValidationError("split evidence overlaps between proposals")
        seen.update(ids)
    if seen != incident_ids:
        raise SynthesisValidationError("split evidence must partition every promoted incident")


def _validate_proposal(
    proposal: Mapping[str, object], incident_ids: set[int], surface_references: set[str]
) -> set[int]:
    remedy_type = proposal.get("remedy_type")
    if remedy_type not in {"claude-md", "hook", "skill", "benchmark-only"}:
        raise SynthesisValidationError("proposal remedy_type is invalid")
    for key in ("routing_rationale", "failure_statement"):
        if not isinstance(proposal.get(key), str) or not str(proposal[key]).strip():
            raise SynthesisValidationError(f"proposal {key} must be non-empty")
    confidence = proposal.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        raise SynthesisValidationError("proposal confidence must be between 0 and 1")
    evidence = proposal.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        raise SynthesisValidationError("proposal requires evidence")
    ids: set[int] = set()
    for item in evidence:
        if not isinstance(item, Mapping):
            raise SynthesisValidationError("evidence entries must be objects")
        incident_id, quote = item.get("incident_id"), item.get("quote")
        if isinstance(incident_id, bool) or not isinstance(incident_id, int) or incident_id not in incident_ids:
            raise SynthesisValidationError("evidence must cite a promoted incident")
        if not isinstance(quote, str) or not quote.strip() or len(quote.splitlines()) > 2:
            raise SynthesisValidationError("evidence quotes must be non-empty and at most two lines")
        if incident_id in ids:
            raise SynthesisValidationError("proposal repeats an evidence incident")
        ids.add(incident_id)
    content = proposal.get("remedy_content")
    if not isinstance(content, Mapping):
        raise SynthesisValidationError("remedy_content must be an object")
    _validate_remedy_content(str(remedy_type), content)
    dedup = proposal.get("dedup")
    if not isinstance(dedup, list):
        raise SynthesisValidationError("dedup must be an array")
    targets: dict[str, str] = {}
    for item in dedup:
        if not isinstance(item, Mapping):
            raise SynthesisValidationError("dedup entries must be objects")
        existing, verdict, reason = item.get("existing"), item.get("verdict"), item.get("reason")
        if not isinstance(existing, str) or not isinstance(verdict, str) or not isinstance(reason, str) or not reason.strip():
            raise SynthesisValidationError("dedup entries require existing, verdict, and reason")
        if verdict not in {"clear", "overlap"} or existing in targets:
            raise SynthesisValidationError("dedup verdicts must be unique clear or overlap entries")
        targets[existing] = verdict
    if set(targets) != surface_references:
        raise SynthesisValidationError("dedup must explicitly cover every shown existing remedy")
    overlaps = {existing for existing, verdict in targets.items() if verdict == "overlap"}
    action = proposal.get("overlap_action")
    if overlaps:
        if not isinstance(action, Mapping) or action.get("revises") not in overlaps:
            raise SynthesisValidationError("overlap proposals must revise an overlapping existing remedy")
    elif action is not None:
        raise SynthesisValidationError("overlap_action is only valid when overlap exists")
    return ids


def _validate_remedy_content(remedy_type: str, content: Mapping[str, object]) -> None:
    if remedy_type == "claude-md":
        text, target = content.get("text"), content.get("target")
        if not isinstance(text, str) or not text.strip() or target not in {"global", "project"}:
            raise SynthesisValidationError("claude-md requires text and global/project target")
        if target == "project" and (not isinstance(content.get("project"), str) or not str(content["project"]).strip()):
            raise SynthesisValidationError("project claude-md requires project")
    elif remedy_type == "skill":
        name, description, body = content.get("name"), content.get("description"), content.get("body_markdown")
        if not all(isinstance(value, str) for value in (name, description, body)):
            raise SynthesisValidationError("skill requires name, description, and body_markdown")
        skill_markdown = f"---\nname: {name}\ndescription: {description}\n---\n{body}"
        _, parsed_body = parse_skill_markdown(skill_markdown)
        if not parsed_body:
            raise SynthesisValidationError("skill body must be non-empty")
        if not str(description).lstrip().lower().startswith("use when "):
            raise SynthesisValidationError("skill description must be a concrete 'Use when ...' trigger")
    elif remedy_type == "hook":
        if not all(isinstance(content.get(key), str) and str(content[key]).strip() for key in ("event", "command_sketch")):
            raise SynthesisValidationError("hook requires event and command_sketch")
    else:
        if not isinstance(content.get("note"), str) or not str(content["note"]).strip():
            raise SynthesisValidationError("benchmark-only requires note")


def _normalize_response(response: Mapping[str, object], *, singleton: bool) -> list[dict[str, object]]:
    raw_proposals = response.get("split")
    source = raw_proposals if isinstance(raw_proposals, list) else [response]
    normalized: list[dict[str, object]] = []
    for raw in source:
        assert isinstance(raw, Mapping)
        evidence = raw["evidence"]
        assert isinstance(evidence, list)
        dedup = raw["dedup"]
        assert isinstance(dedup, list)
        revisions = [item["existing"] for item in dedup if isinstance(item, Mapping) and item.get("verdict") == "overlap"]
        proposal = dict(raw)
        proposal.pop("split", None)
        proposal["singleton"] = singleton
        normalized.append(
            {
                "remedy_type": proposal["remedy_type"],
                "drafted_content": json.dumps(proposal, sort_keys=True, separators=(",", ":")),
                "evidence_incident_ids": [int(item["incident_id"]) for item in evidence if isinstance(item, Mapping)],
                "dedup_verdict": json.dumps(dedup, sort_keys=True, separators=(",", ":")),
                "revises": revisions[0] if revisions else None,
                "singleton": singleton,
            }
        )
    return normalized


def _render_evidence_pack(incident: Incident) -> str:
    pack, _ = context_for_incident(incident)
    return (
        f"BEGIN PROMOTED EVIDENCE incident_id={incident.id} project={incident.project}\n"
        f"One-liner: {incident.one_liner or '(missing)'}\n"
        f"Preceding request:\n{pack.preceding_request}\n\n"
        f"Agent activity digest:\n{pack.agent_activity_digest}\n\n"
        f"Frustrated message:\n{pack.frustrated_message}\n\n"
        f"Following exchange:\n{pack.following_exchange}\n"
        "END PROMOTED EVIDENCE"
    )


def _bounded_evidence_packs(incidents: Sequence[Incident], budget: int) -> str:
    """Keep one synthesis call bounded without dropping an incident's identity or quote."""

    packs = [_render_evidence_pack(incident) for incident in incidents]
    combined = "\n\n".join(packs)
    if len(combined) <= budget:
        return combined
    # Unlike Curator chunks, a synthesis call must retain its whole promotion group
    # to permit the one authorized split. Preserve every incident's one-liner and
    # complaint, then trim verbose surrounding transcript fields proportionally.
    per_pack = max(1, budget // len(incidents))
    return "\n\n".join(pack[:per_pack] + "\n[context truncated by synthesis budget]" for pack in packs)


def _taxonomy_gist(label: str) -> str:
    for entry in list_labels():
        if entry.label == label:
            return f"{entry.label}: {entry.gist}"
    return f"{label}: promoted taxonomy label"


def _render_surface_rows(rows: Sequence[tuple[str, str]]) -> str:
    return "\n".join(f"- {reference} | {digest}" for reference, digest in rows) or "- (none)"


def _project_paths(paths: Mapping[str, Path] | Iterable[Path] | None) -> list[Path]:
    if paths is None:
        return []
    if isinstance(paths, Mapping):
        return [Path(path) for path in paths.values()]
    return [Path(path) for path in paths]


def _incident_project_claude_md_paths(groups: Iterable[Sequence[Incident]]) -> list[Path]:
    """Resolve project-scoped rules from the archived evidence's actual working dirs."""

    paths: list[Path] = []
    for incidents in groups:
        for incident in incidents:
            pack, _ = context_for_incident(incident)
            if pack.cwd:
                paths.append(Path(pack.cwd) / "CLAUDE.md")
    return paths


def _singleton_group(ledger: Ledger, incidents: Sequence[Incident]) -> bool:
    return any(
        decision.singleton
        for incident in incidents
        for decision in ledger.curator_decisions(incident.id)
    )
