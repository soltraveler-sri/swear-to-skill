"""Stage 4: cautious remedy routing over promoted failure-mode clusters."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
import json
from pathlib import Path
import re

from .config import load_config
from .ledger import Incident, Ledger, Remedy
from .notify import emit
from .llm import MalformedOutputError, call, estimate_and_confirm, load_prompt
from .taxonomy import list_labels
from .triager import context_for_incident


PROMPT_NAME = "synthesize"
PROMPT_VERSION = 2
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
    error: str | None = None


@dataclass(frozen=True)
class RemedySurface:
    """Bounded, non-sensitive digest of remedies that may overlap this proposal."""

    skills: tuple[tuple[str, str], ...]
    claude_md_blocks: tuple[tuple[str, str], ...]
    proposals: tuple[tuple[str, str], ...]

    @property
    def reference_ids(self) -> dict[str, str]:
        """Stable prompt tokens mapped to their durable remedy references."""

        rows = (*self.skills, *self.claude_md_blocks, *self.proposals)
        return {f"R{index}": reference for index, (reference, _) in enumerate(rows, start=1)}

    @property
    def references(self) -> tuple[str, ...]:
        return tuple(self.reference_ids.values())


def synthesize_pending(
    ledger: Ledger,
    *,
    assume_yes: bool = False,
    skills_dir: Path | None = None,
    global_claude_md_path: Path | None = None,
    project_claude_md_paths: Mapping[str, Path] | Iterable[Path] | None = None,
    surface_reference_root: Path | None = None,
) -> list[SynthesisResult]:
    """Synthesize each promoted label group once, preserving all-or-nothing resume safety."""

    groups: dict[str, list[Incident]] = defaultdict(list)
    for incident in ledger.incidents_in_state("promoted"):
        if not incident.label:
            raise SynthesisValidationError(f"promoted incident {incident.id} has no taxonomy label")
        groups[incident.label].append(incident)
    if not groups:
        return []

    config = load_config()
    model = config.models.synthesize
    if not estimate_and_confirm(len(groups), model, assume_yes=assume_yes):
        return []
    template, schema = load_prompt(PROMPT_NAME, config.prompts.synthesize)
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
            reference_root=surface_reference_root,
        )
        try:
            response = _call_validated(
                render_synthesis_prompt(template, label, incidents, surface),
                schema=schema,
                model=model,
                effort=config.models.synthesize_effort,
                incident_ids={incident.id for incident in incidents},
                reference_ids=surface.reference_ids,
            )
        except (SynthesisValidationError, MalformedOutputError) as error:
            # A group whose response stays invalid after the corrective retry
            # is a data point (evals score it; the pump retries next cycle
            # since its incidents remain 'promoted') — never a reason to
            # abandon the remaining groups.
            results.append(
                SynthesisResult(
                    label,
                    tuple(incident.id for incident in incidents),
                    (),
                    error=f"{type(error).__name__}: {error}",
                )
            )
            continue
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


def synthesize_audit_revision(
    ledger: Ledger,
    *,
    remedy: Remedy,
    label: str,
    incidents: Sequence[Incident],
) -> int | None:
    """Draft one Gate-queued revision from incidents that survived a remedy.

    This deliberately reuses the normal Synthesist schema and remedy-surface
    machinery.  It does not install anything and it does not alter incident
    state: these are counter-evidence records, not a second Curator promotion.
    """

    if not incidents or ledger.audit_proposal_exists(remedy.id, "revision"):
        return None
    original = ledger.get_proposal(remedy.proposal_id)
    if original is None:
        raise SynthesisValidationError(f"remedy {remedy.id} has no source proposal")
    surface = collect_remedy_surface(ledger)
    config = load_config()
    template, schema = load_prompt(PROMPT_NAME, config.prompts.synthesize)
    prompt = render_synthesis_prompt(template, label, incidents, surface)
    prompt += (
        "\n\nAUDIT REVISION BRIEF\n"
        f"The installed remedy is proposal #{original.id}, remedy #{remedy.id}.\n"
        f"Original drafted remedy:\n{original.drafted_content}\n\n"
        "These incidents occurred DESPITE that remedy. Diagnose why the remedy text "
        "failed to prevent them: wrong trigger description, too vague, wrong remedy "
        "type, or another concrete mismatch. Draft a replacement on the same remedy "
        "surface/type, and mark the original proposal as an overlap/revision.\n"
    )
    response = _call_validated(
        prompt,
        schema=schema,
        model=config.models.synthesize,
        effort=config.models.synthesize_effort,
        incident_ids={incident.id for incident in incidents},
        reference_ids=surface.reference_ids,
    )
    if response.get("split") is not None:
        raise SynthesisValidationError("audit revision must produce one replacement proposal")
    normalized = _normalize_response(response, singleton=False)
    proposal = normalized[0]
    if proposal["remedy_type"] != remedy.artifact_type:
        raise SynthesisValidationError("audit revision must keep the installed remedy type")
    proposal["revises"] = f"proposal #{original.id}"
    return ledger.create_proposal(
        remedy_type=str(proposal["remedy_type"]),
        drafted_content=str(proposal["drafted_content"]),
        evidence_incident_ids=[int(item) for item in proposal["evidence_incident_ids"]],
        dedup_verdict=str(proposal["dedup_verdict"]),
        gate_status="pending",
        revises=str(proposal["revises"]),
        singleton=False,
        proposal_kind="revision",
        target_remedy_id=remedy.id,
    )


def collect_remedy_surface(
    ledger: Ledger,
    *,
    skills_dir: Path | None = None,
    global_claude_md_path: Path | None = None,
    project_claude_md_paths: Mapping[str, Path] | Iterable[Path] | None = None,
    reference_root: Path | None = None,
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
            skill_rows.append(
                (
                    f"skill:{_surface_path(path, reference_root)}",
                    f"name={frontmatter['name']} | description={frontmatter['description']}",
                )
            )

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
                block_rows.append(
                    (f"claude-md:{_surface_path(path, reference_root)}#{index}", compact)
                )
    return RemedySurface(tuple(skill_rows), tuple(block_rows), tuple(ledger.proposal_surface_rows()))


def render_synthesis_prompt(
    template: str,
    label: str,
    incidents: Sequence[Incident],
    surface: RemedySurface,
) -> str:
    """Render the complete evidence and every dedup target for one label group."""

    evidence = _bounded_evidence_packs(incidents, load_config().curator.context_char_budget)
    render_rows = _render_surface_rows if "{{REFERENCE_COUNT}}" in template else _render_legacy_surface_rows
    replacements = {
        "{{TAXONOMY_GIST}}": _taxonomy_gist(label),
        "{{EVIDENCE_PACKS}}": evidence,
        "{{REFERENCE_COUNT}}": str(len(surface.reference_ids)),
        "{{SKILL_DIGESTS}}": render_rows(surface.skills),
        "{{CLAUDE_MD_DIGESTS}}": render_rows(
            surface.claude_md_blocks, start_index=len(surface.skills) + 1
        ),
        "{{PROPOSAL_DIGESTS}}": render_rows(
            surface.proposals,
            start_index=len(surface.skills) + len(surface.claude_md_blocks) + 1,
        ),
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
    effort: str | None = None,
    incident_ids: set[int],
    reference_ids: Mapping[str, str],
) -> dict[str, object]:
    """Retry once when code-only artifact and partition checks reject valid JSON."""

    retry_note = "\n\nYour prior draft failed local validation. Correct the SKILL.md/frontmatter, dedup, and evidence partition exactly."
    last_error: SynthesisValidationError | None = None
    for attempt in range(2):
        invocation_prompt = prompt
        if attempt:
            assert last_error is not None
            invocation_prompt += retry_note + " Specific validation failure: " + str(last_error)
        response = call(
            invocation_prompt,
            schema=schema,
            model=model,
            stage="synthesize",
            effort=effort,
        )
        try:
            _validate_response(response, incident_ids=incident_ids, reference_ids=reference_ids)
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
    reference_ids: Mapping[str, str],
) -> None:
    proposals = response.get("split")
    if proposals is None:
        if _validate_proposal(response, incident_ids, reference_ids) != incident_ids:
            raise SynthesisValidationError("proposal evidence must cover every promoted incident")
        return
    if not isinstance(proposals, list) or len(proposals) < 2:
        raise SynthesisValidationError("split must contain at least two proposals")
    seen: set[int] = set()
    for proposal in proposals:
        if not isinstance(proposal, Mapping):
            raise SynthesisValidationError("split proposals must be objects")
        ids = _validate_proposal(proposal, incident_ids, reference_ids)
        if seen.intersection(ids):
            raise SynthesisValidationError("split evidence overlaps between proposals")
        seen.update(ids)
    if seen != incident_ids:
        raise SynthesisValidationError("split evidence must partition every promoted incident")


def _validate_proposal(
    proposal: Mapping[str, object], incident_ids: set[int], reference_ids: Mapping[str, str]
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
    normalized_evidence: list[Mapping[str, object]] = []
    for item in evidence:
        if not isinstance(item, Mapping):
            raise SynthesisValidationError("evidence entries must be objects")
        incident_id, quote = item.get("incident_id"), item.get("quote")
        if isinstance(incident_id, bool) or not isinstance(incident_id, int) or incident_id not in incident_ids:
            raise SynthesisValidationError("evidence must cite a promoted incident")
        if not isinstance(quote, str) or not quote.strip() or len(quote.splitlines()) > 2:
            raise SynthesisValidationError("evidence quotes must be non-empty and at most two lines")
        if incident_id in ids:
            # Real models occasionally cite the same incident twice. A repeat
            # is harmless redundancy, not a correctness problem — normalize by
            # keeping the first citation instead of failing the proposal.
            continue
        ids.add(incident_id)
        normalized_evidence.append(item)
    if isinstance(proposal, dict):
        proposal["evidence"] = normalized_evidence
    content = proposal.get("remedy_content")
    if not isinstance(content, Mapping):
        raise SynthesisValidationError("remedy_content must be an object")
    _validate_remedy_content(str(remedy_type), content)
    dedup = proposal.get("dedup")
    if not isinstance(dedup, list):
        raise SynthesisValidationError("dedup must be an array")
    reference_to_id = {reference: reference_id for reference_id, reference in reference_ids.items()}
    targets: dict[str, str] = {}
    for item in dedup:
        if not isinstance(item, Mapping):
            raise SynthesisValidationError("dedup entries must be objects")
        existing, verdict, reason = item.get("existing"), item.get("verdict"), item.get("reason")
        if not isinstance(existing, str) or not isinstance(verdict, str) or not isinstance(reason, str) or not reason.strip():
            raise SynthesisValidationError("dedup entries require existing, verdict, and reason")
        if verdict not in {"clear", "overlap"}:
            raise SynthesisValidationError("dedup verdicts must be unique clear or overlap entries")
        reference_id = existing if existing in reference_ids else reference_to_id.get(existing)
        if reference_id is None:
            raise SynthesisValidationError(f"dedup references unknown existing remedy: {existing}")
        if reference_id in targets:
            raise SynthesisValidationError("dedup verdicts must be unique clear or overlap entries")
        targets[reference_id] = verdict
    missing = [reference_id for reference_id in reference_ids if reference_id not in targets]
    if missing:
        details = ", ".join(
            f"{reference_id} ({reference_ids[reference_id]})" for reference_id in missing
        )
        raise SynthesisValidationError(f"dedup missing entries for: {details}")
    overlaps = {reference_id for reference_id, verdict in targets.items() if verdict == "overlap"}
    action = proposal.get("overlap_action")
    if overlaps:
        revises = action.get("revises") if isinstance(action, Mapping) else None
        revision_id = None
        if isinstance(revises, str):
            revision_id = revises if revises in reference_ids else reference_to_id.get(revises)
        if revision_id not in overlaps:
            raise SynthesisValidationError("overlap proposals must revise an overlapping existing remedy")
        if isinstance(proposal, dict):
            normalized_action = dict(action)
            normalized_action["revises"] = reference_ids[revision_id]
            proposal["overlap_action"] = normalized_action
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
        action = raw.get("overlap_action")
        revises = action.get("revises") if isinstance(action, Mapping) else None
        proposal = dict(raw)
        proposal.pop("split", None)
        proposal["singleton"] = singleton
        normalized.append(
            {
                "remedy_type": proposal["remedy_type"],
                "drafted_content": json.dumps(proposal, sort_keys=True, separators=(",", ":")),
                "evidence_incident_ids": [int(item["incident_id"]) for item in evidence if isinstance(item, Mapping)],
                "dedup_verdict": json.dumps(dedup, sort_keys=True, separators=(",", ":")),
                "revises": revises if isinstance(revises, str) else None,
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


def _render_surface_rows(
    rows: Sequence[tuple[str, str]], *, start_index: int = 1
) -> str:
    return (
        "\n".join(
            f"- [R{index}] {reference} | {digest}"
            for index, (reference, digest) in enumerate(rows, start=start_index)
        )
        or "- (none)"
    )


def _render_legacy_surface_rows(
    rows: Sequence[tuple[str, str]], *, start_index: int = 1
) -> str:
    """Keep published v1 arms byte-compatible while v2 owns stable prompt IDs."""

    del start_index
    return "\n".join(f"- {reference} | {digest}" for reference, digest in rows) or "- (none)"


def _project_paths(paths: Mapping[str, Path] | Iterable[Path] | None) -> list[Path]:
    if paths is None:
        return []
    if isinstance(paths, Mapping):
        return [Path(path) for path in paths.values()]
    return [Path(path) for path in paths]


def _surface_path(path: Path, reference_root: Path | None) -> str:
    """Render stable eval references while preserving production path identity."""

    if reference_root is None:
        return str(path)
    try:
        relative = path.resolve().relative_to(reference_root.resolve())
    except ValueError:
        return str(path)
    return f"$HOME/{relative.as_posix()}"


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
