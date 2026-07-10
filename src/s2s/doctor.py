"""Read-only discovery checks for installed swear-to-skill remedies."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Iterator, Mapping

from . import llm
from .gate import GateTargets, default_targets
from .synthesist import SynthesisValidationError, parse_skill_markdown


MANAGED_SKILL_PREFIX = "s2s-"
MANAGED_MARKER = "# s2s:managed remedy="
CLAUDE_BEGIN_RE = re.compile(r"<!--\s*s2s:begin remedy=(\d+)\s*-->")
CLAUDE_END_RE = re.compile(r"<!--\s*s2s:end remedy=(\d+)\s*-->")
LIVE_SKILLS_SCHEMA: dict[str, object] = {
    "type": "object",
    "required": ["skills"],
    "properties": {"skills": {"type": "array", "items": {"type": "string"}}},
}
LIVE_PROMPT = (
    "List every currently available skill whose name begins with 's2s-'. "
    "Return only their exact names in the skills array. Do not infer names."
)


@dataclass(frozen=True)
class DoctorCheck:
    """One named, operator-readable health check."""

    name: str
    passed: bool
    message: str
    severity: str = "error"


@dataclass(frozen=True)
class DoctorReport:
    """The complete result of one static, optionally live, discovery check."""

    checks: tuple[DoctorCheck, ...]
    installed_skills: tuple[str, ...]

    @property
    def healthy(self) -> bool:
        return all(check.passed for check in self.checks)

    @property
    def exit_code(self) -> int:
        return 0 if self.healthy else 1


def build_live_probe_argv(schema: Mapping[str, object] | str, model: str = "haiku") -> list[str]:
    """Build the discovery-aware argv for the one opt-in live probe.

    Pipeline calls normally disable slash-command resolution so local skills cannot
    affect their structured work. This probe is specifically checking discovery, so
    it intentionally omits that flag and retains session-persistence suppression.
    """

    schema_text = (
        schema
        if isinstance(schema, str)
        else json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
    )
    return [
        *llm.CLAUDE_BASE_ARGS,
        "--json-schema",
        schema_text,
        "--model",
        model,
        "--no-session-persistence",
    ]


@contextmanager
def _live_probe_argv() -> Iterator[None]:
    """Route one ``llm.call`` through the discovery-aware argv builder.

    ``llm.call`` owns accounting, error handling, and the sole process boundary.
    It has no per-call argv hook, so this tightly scoped substitution preserves that
    boundary while allowing this one probe to leave skills visible.
    """

    original = llm.build_argv

    def discovery_argv(
        schema: Mapping[str, object] | str, model: str, *, effort: str | None = None
    ) -> list[str]:
        del effort
        return build_live_probe_argv(schema, model)

    llm.build_argv = discovery_argv
    try:
        yield
    finally:
        llm.build_argv = original


def run_doctor(
    *, targets: GateTargets | None = None, live: bool = False
) -> DoctorReport:
    """Check generated remedy discovery surfaces without modifying them."""

    resolved = targets or default_targets()
    checks, installed = _static_checks(resolved)
    if live:
        checks.append(_live_check(installed))
    return DoctorReport(tuple(checks), tuple(sorted(installed)))


def render_report(report: DoctorReport) -> str:
    """Render checks grouped into problems and successful checks for the terminal."""

    problems = [check for check in report.checks if not check.passed]
    passed = [check for check in report.checks if check.passed]
    lines: list[str] = []
    if problems:
        lines.append("PROBLEMS")
        lines.extend(f"  FAIL {check.name}: {check.message}" for check in problems)
    if passed:
        if lines:
            lines.append("")
        lines.append("HEALTHY")
        lines.extend(f"  PASS {check.name}: {check.message}" for check in passed)
    if not lines:
        lines.append("HEALTHY\n  PASS doctor: no installed s2s skills to check")
    return "\n".join(lines)


def _static_checks(targets: GateTargets) -> tuple[list[DoctorCheck], set[str]]:
    checks: list[DoctorCheck] = []
    installed: set[str] = set()
    parsed_names: dict[str, list[str]] = {}
    skills_dir = targets.skills_dir
    entries = sorted(skills_dir.iterdir()) if skills_dir.is_dir() else []

    for entry in entries:
        if not entry.name.startswith(MANAGED_SKILL_PREFIX):
            continue
        installed.add(entry.name)
        if not entry.is_dir():
            checks.append(
                DoctorCheck("skill layout", False, f"{entry.name} is not a directory")
            )
            continue
        expected = entry / "SKILL.md"
        nested = sorted(
            path.relative_to(entry).as_posix()
            for path in entry.rglob("SKILL.md")
            if path != expected
        )
        renamed = sorted(
            path.relative_to(entry).as_posix()
            for path in entry.glob("*.md")
            if path.name != "SKILL.md"
        )
        anomalies: list[str] = []
        if not expected.is_file():
            anomalies.append("missing SKILL.md")
        if nested:
            anomalies.append(f"nested SKILL.md ({', '.join(nested)})")
        if renamed:
            anomalies.append(f"renamed markdown ({', '.join(renamed)})")
        checks.append(
            DoctorCheck(
                "skill layout",
                not anomalies,
                f"{entry.name}: exact skills/{entry.name}/SKILL.md"
                if not anomalies
                else f"{entry.name}: {'; '.join(anomalies)}",
            )
        )
        if not expected.is_file():
            continue
        try:
            content = expected.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            checks.append(DoctorCheck("frontmatter", False, f"{entry.name}: {error}"))
            checks.append(DoctorCheck("managed marker", False, f"{entry.name}: cannot read SKILL.md"))
            continue
        checks.append(
            DoctorCheck(
                "managed marker",
                MANAGED_MARKER in content,
                f"{entry.name}: managed marker present"
                if MANAGED_MARKER in content
                else f"{entry.name}: missing {MANAGED_MARKER!r}",
            )
        )
        try:
            fields, _ = parse_skill_markdown(content)
        except SynthesisValidationError as error:
            checks.append(DoctorCheck("frontmatter", False, f"{entry.name}: {error}"))
            continue
        name = fields["name"]
        parsed_names.setdefault(name, []).append(entry.name)
        checks.append(
            DoctorCheck(
                "frontmatter",
                name == entry.name and bool(fields["description"].strip()),
                f"{entry.name}: name and non-empty description are valid"
                if name == entry.name and fields["description"].strip()
                else f"{entry.name}: frontmatter name is {name!r}, expected {entry.name!r}",
            )
        )

    # User-owned skills are not required to carry s2s markers, but a duplicate
    # frontmatter name still makes discovery ambiguous for a managed remedy.
    for entry in entries:
        if entry.name.startswith(MANAGED_SKILL_PREFIX) or not entry.is_dir():
            continue
        skill_file = entry / "SKILL.md"
        if not skill_file.is_file():
            continue
        try:
            fields, _ = parse_skill_markdown(skill_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, SynthesisValidationError):
            continue
        parsed_names.setdefault(fields["name"], []).append(entry.name)

    duplicates = {
        name: locations for name, locations in parsed_names.items() if len(locations) > 1
    }
    checks.append(
        DoctorCheck(
            "duplicate skill names",
            not duplicates,
            "no duplicate names across the skills directory"
            if not duplicates
            else "; ".join(
                f"{name}: {', '.join(locations)}" for name, locations in sorted(duplicates.items())
            ),
        )
    )
    checks.extend(_claude_marker_checks(targets))
    return checks, installed


def _claude_marker_checks(targets: GateTargets) -> list[DoctorCheck]:
    paths = [targets.global_claude_md]
    if targets.project_claude_md is not None:
        paths.append(targets.project_claude_md)
    if targets.project_claude_md_paths:
        paths.extend(Path(path) for path in targets.project_claude_md_paths.values())
    results: list[DoctorCheck] = []
    seen: set[Path] = set()
    for path in paths:
        path = Path(path)
        if path in seen:
            continue
        seen.add(path)
        if not path.exists():
            results.append(DoctorCheck("CLAUDE.md remedy markers", True, f"{path}: no managed blocks"))
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            results.append(DoctorCheck("CLAUDE.md remedy markers", False, f"{path}: {error}"))
            continue
        begins = CLAUDE_BEGIN_RE.findall(content)
        ends = CLAUDE_END_RE.findall(content)
        invalid = sorted(
            remedy_id
            for remedy_id in set(begins) | set(ends)
            if begins.count(remedy_id) != 1 or ends.count(remedy_id) != 1
        )
        results.append(
            DoctorCheck(
                "CLAUDE.md remedy markers",
                not invalid,
                f"{path}: {len(begins)} managed block(s) intact"
                if not invalid
                else f"{path}: unpaired or duplicated remedy marker(s): {', '.join(invalid)}",
            )
        )
    return results


def _live_check(installed: set[str]) -> DoctorCheck:
    try:
        with _live_probe_argv():
            response = llm.call(
                LIVE_PROMPT,
                schema=LIVE_SKILLS_SCHEMA,
                model="haiku",
                stage="doctor-live",
            )
        reported = response["skills"]
        assert isinstance(reported, list)
        visible = {name for name in reported if isinstance(name, str) and name.startswith(MANAGED_SKILL_PREFIX)}
    except (llm.LLMError, OSError, ValueError, AssertionError) as error:
        return DoctorCheck("live discovery", False, f"probe failed: {error}")

    found = sorted(installed & visible)
    missing = sorted(installed - visible)
    unexpected = sorted(visible - installed)
    message = f"found: {', '.join(found) or 'none'}; missing: {', '.join(missing) or 'none'}"
    if unexpected:
        message += f"; unexpected: {', '.join(unexpected)}"
    return DoctorCheck("live discovery", not missing and not unexpected, message)
