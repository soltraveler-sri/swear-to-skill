"""Command-line interface for swear-to-skill.

Issue #1 intentionally exposes only ownership-labelled command stubs. Each
subcommand has a later issue responsible for its behavior.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence


COMMAND_ISSUES = {
    "init": 4,
    "backfill": 4,
    "scan": 5,
    "meter": 6,
    "status": 6,
    "triage": 8,
    "review": 9,
    "run": 11,
    "pump": 11,
    "schedule": 11,
    "proposals": 13,
    "approve": 13,
    "reject": 13,
    "rollback": 13,
    "log": 17,
    "autonomy": 17,
}


def build_parser() -> argparse.ArgumentParser:
    """Build the zero-dependency command parser."""
    parser = argparse.ArgumentParser(prog="s2s", description="swear-to-skill")
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    for command in COMMAND_ISSUES:
        subparsers.add_parser(command)

    subparsers.choices["schedule"].add_argument(
        "action", nargs="?", choices=("install", "remove")
    )
    subparsers.choices["autonomy"].add_argument(
        "mode", nargs="?", choices=("on", "off")
    )
    for command in ("approve", "reject", "rollback"):
        subparsers.choices[command].add_argument("remedy_id", nargs="?")
    subparsers.choices["meter"].add_argument("--open", action="store_true")
    subparsers.choices["pump"].add_argument("--background", action="store_true")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run a command stub and return a shell-compatible exit status."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    issue = COMMAND_ISSUES[args.command]
    print(f"{args.command}: not implemented yet (issue #{issue})")
    return 0
