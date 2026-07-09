"""Command-line interface for swear-to-skill."""

from __future__ import annotations

import argparse
from collections.abc import Sequence


COMMAND_ISSUES = {
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

IMPLEMENTED_COMMANDS = ("init", "backfill")


def build_parser() -> argparse.ArgumentParser:
    """Build the zero-dependency command parser."""
    parser = argparse.ArgumentParser(prog="s2s", description="swear-to-skill")
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    for command in (*IMPLEMENTED_COMMANDS, *COMMAND_ISSUES):
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
    subparsers.choices["scan"].add_argument("--suggest-terms", action="store_true")
    subparsers.choices["init"].add_argument("--uninstall", action="store_true")

    hook_parser = subparsers.add_parser("hook")
    hook_subparsers = hook_parser.add_subparsers(dest="hook_event")
    hook_subparsers.add_parser("session-end")

    return parser


def _run_scan(args: argparse.Namespace) -> int:
    from .ledger import Ledger
    from .scanner import scan_pending_queue, write_candidate_phrase_report

    if args.suggest_terms:
        report = write_candidate_phrase_report()
        print(
            f"candidate phrases written to {report.path} "
            f"({report.candidate_count} candidates from {report.messages_considered} messages)"
        )
        return 0

    with Ledger() as ledger:
        results = scan_pending_queue(ledger)
        hits = sum(result.incident_count for result in results)
        print(f"scanned {len(results)} transcript(s); {hits} detection(s) recorded")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch implemented commands and retain labelled future stubs."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    if args.command == "scan":
        return _run_scan(args)

    if args.command == "init":
        from .initcmd import run_init

        return run_init(uninstall_mode=args.uninstall)

    if args.command == "backfill":
        from .archiver import backfill

        result = backfill()
        return 1 if result.failed else 0

    if args.command == "hook":
        if args.hook_event == "session-end":
            from .archiver import handle_session_end

            return handle_session_end()
        parser.error("hook requires an event")

    issue = COMMAND_ISSUES[args.command]
    print(f"{args.command}: not implemented yet (issue #{issue})")
    return 0
