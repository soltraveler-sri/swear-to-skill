from __future__ import annotations

from pathlib import Path
import re

from s2s.cli import build_parser


SKILL_PATH = Path(__file__).parents[1] / "skill" / "s2s" / "SKILL.md"
LAW = (
    "Never run s2s approve without the user having explicitly approved that specific "
    "proposal in this conversation. Summarizing is not approval. If ambiguous, ask."
)


def _frontmatter(content: str) -> dict[str, str]:
    match = re.match(r"\A---\n(.*?)\n---\n", content, flags=re.DOTALL)
    assert match, "SKILL.md must begin with YAML frontmatter"
    fields: dict[str, str] = {}
    for line in match.group(1).splitlines():
        key, separator, value = line.partition(":")
        assert separator and key and value.strip(), f"invalid frontmatter line: {line!r}"
        fields[key] = value.strip().strip("\"'")
    return fields


def test_companion_skill_frontmatter_and_human_gate_law() -> None:
    content = SKILL_PATH.read_text(encoding="utf-8")
    frontmatter = _frontmatter(content)

    assert frontmatter["name"] == "s2s"
    assert "review s2s proposals" in frontmatter["description"]
    assert "swear meter" in frontmatter["description"]
    assert "Bash(s2s" in frontmatter["allowed-tools"]
    assert "<!-- s2s-skill-version: 1 -->" in content
    assert LAW in content


def test_every_s2s_command_documented_by_the_skill_exists_in_the_cli() -> None:
    content = SKILL_PATH.read_text(encoding="utf-8")
    body = content.split("---\n", maxsplit=2)[2]
    documented_commands = set(re.findall(r"(?<![\w/])s2s\s+([a-z][a-z-]*)", body))
    registered_commands = set(build_parser()._subparsers._group_actions[0].choices)

    assert documented_commands <= registered_commands
