"""Command-line interface for swear-to-skill."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
import json
import os
import sys


COMMAND_ISSUES = {
    "scan": 5,
    "meter": 6,
    "status": 6,
    "triage": 8,
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

IMPLEMENTED_COMMANDS = ("init", "backfill", "review")


def build_parser() -> argparse.ArgumentParser:
    """Build the zero-dependency command parser."""
    parser = argparse.ArgumentParser(prog="s2s", description="swear-to-skill")
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    for command in (*IMPLEMENTED_COMMANDS, *COMMAND_ISSUES):
        subparsers.add_parser(command)

    subparsers.choices["schedule"].add_argument(
        "action", nargs="?", choices=("install", "remove", "status"), default="status"
    )
    subparsers.choices["autonomy"].add_argument(
        "mode", nargs="?", choices=("on", "off")
    )
    subparsers.choices["approve"].add_argument("proposal_id", type=int)
    subparsers.choices["approve"].add_argument("--edit", action="store_true")
    subparsers.choices["reject"].add_argument("proposal_id", type=int)
    subparsers.choices["reject"].add_argument("--reason")
    subparsers.choices["rollback"].add_argument("remedy_id", type=int)
    subparsers.choices["rollback"].add_argument("--force", action="store_true")
    subparsers.choices["proposals"].add_argument("--json", action="store_true")
    subparsers.choices["meter"].add_argument("--open", action="store_true")
    subparsers.choices["pump"].add_argument("--background", action="store_true")
    subparsers.choices["scan"].add_argument("--suggest-terms", action="store_true")
    subparsers.choices["triage"].add_argument("--limit", type=int)
    subparsers.choices["triage"].add_argument("--yes", action="store_true")
    subparsers.choices["review"].add_argument("--yes", action="store_true")
    subparsers.choices["init"].add_argument("--uninstall", action="store_true")

    hook_parser = subparsers.add_parser("hook")
    hook_subparsers = hook_parser.add_subparsers(dest="hook_event")
    hook_subparsers.add_parser("session-end")
    hook_subparsers.add_parser("session-start", help=argparse.SUPPRESS)

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
        created = sum(result.incidents_created for result in results)
        print(f"scanned {len(results)} transcript(s); {created} detection(s) recorded")
    return 0


def _run_meter(args: argparse.Namespace) -> int:
    """Regenerate the local dashboard and optionally open it in the default browser."""

    from .ledger import Ledger
    from .meter import write_dashboard

    with Ledger() as ledger:
        report_path = write_dashboard(ledger)
    report_uri = report_path.resolve().as_uri()
    if args.open:
        import webbrowser

        webbrowser.open(report_uri)
    print(report_uri)
    return 0


def _run_status() -> int:
    """Print current substrate and scan health without making any external request."""

    from .ledger import Ledger
    from .meter import collect_dashboard_data, count_archived_sessions, render_status

    with Ledger() as ledger:
        print(render_status(collect_dashboard_data(ledger), archived_sessions=count_archived_sessions()))
        print(f"pending proposals: {ledger.pending_proposal_count()}")
    return 0


def _run_triage(args: argparse.Namespace) -> int:
    from .ledger import Ledger
    from .triager import triage_pending

    with Ledger() as ledger:
        results = triage_pending(ledger, limit=args.limit, assume_yes=args.yes)
    print(f"triaged {len(results)} incident(s)")
    return 0


def _run_review(args: argparse.Namespace) -> int:
    from .curator import run_pass
    from .ledger import Ledger

    with Ledger() as ledger:
        result = run_pass(ledger, assume_yes=args.yes)
    if result.skipped:
        print("curator: no new, resurfaced, or QC evidence to review")
        return 0
    print(
        f"curator: {result.applied_incident_verdicts} incident verdict(s), "
        f"{result.applied_cluster_verdicts} cluster verdict(s), "
        f"{result.rejected_verdicts} rejected/skipped"
    )
    print(f"report: {result.report_path}")
    return 0


def _proposal_view(proposal: object) -> dict[str, object]:
    """Render the stable human/machine Gate view from one ledger proposal."""

    from .ledger import Proposal

    assert isinstance(proposal, Proposal)
    try:
        payload = json.loads(proposal.drafted_content)
    except json.JSONDecodeError:
        payload = {}
    payload = payload if isinstance(payload, dict) else {}
    return {
        "id": proposal.id,
        "remedy_type": proposal.remedy_type,
        "drafted_content": payload.get("remedy_content", proposal.drafted_content),
        "evidence": payload.get("evidence", []),
        "dedup_verdict": payload.get("dedup", proposal.dedup_verdict),
        "confidence": payload.get("confidence"),
        "revises": proposal.revises,
        "singleton": proposal.singleton,
        "proposal_kind": proposal.proposal_kind,
        "target_remedy_id": proposal.target_remedy_id,
    }


def _run_proposals(args: argparse.Namespace) -> int:
    from .ledger import Ledger

    with Ledger() as ledger:
        views = [_proposal_view(proposal) for proposal in ledger.pending_proposals()]
    if args.json:
        print(json.dumps(views, ensure_ascii=False, sort_keys=True))
        return 0
    if not views:
        print("No pending proposals.")
        return 0
    for view in views:
        print(f"proposal {view['id']} | {view['remedy_type']} | confidence={view['confidence']}")
        print(f"drafted content: {json.dumps(view['drafted_content'], ensure_ascii=False)}")
        evidence = view["evidence"]
        if isinstance(evidence, list):
            for item in evidence:
                if isinstance(item, dict):
                    print(f"evidence #{item.get('incident_id')}: {item.get('quote', '')}")
        print(f"dedup: {json.dumps(view['dedup_verdict'], ensure_ascii=False)}")
        if view["revises"]:
            print(f"revises: {view['revises']}")
    return 0


def _run_approve(args: argparse.Namespace) -> int:
    from .gate import GateError, edit_proposal_content, install, rollback
    from .ledger import Ledger, LedgerError

    try:
        with Ledger() as ledger:
            proposal = ledger.get_proposal(args.proposal_id)
            if proposal is None:
                raise LedgerError(f"proposal {args.proposal_id} does not exist")
            if proposal.gate_status == "pending":
                drafted_content = None
                if args.edit:
                    editor = os.environ.get("EDITOR")
                    if editor:
                        drafted_content = edit_proposal_content(proposal, editor)
                    else:
                        print("$EDITOR is unset; installing the unchanged draft.")
                proposal = ledger.approve_proposal(
                    args.proposal_id, drafted_content=drafted_content
                )
            elif proposal.gate_status != "approved":
                raise LedgerError(
                    f"proposal {proposal.id} is {proposal.gate_status!r}, not pending"
                )
        if proposal.proposal_kind == "retirement":
            if proposal.target_remedy_id is None:
                raise LedgerError(f"retirement proposal {proposal.id} has no target remedy")
            result = rollback(proposal.target_remedy_id)
            with Ledger() as ledger:
                ledger.mark_retirement_proposal_completed(
                    proposal.id,
                    rollback_record_ref=f"rollbacks/remedy-{result.remedy_id}.json",
                )
            print(f"retired remedy {result.remedy_id} from proposal {proposal.id}")
            return 0
        result = install(proposal)
    except (GateError, LedgerError, OSError) as error:
        print(f"approve failed: {error}", file=sys.stderr)
        return 1
    print(f"installed remedy {result.remedy_id} from proposal {result.proposal_id}")
    return 0


def _run_reject(args: argparse.Namespace) -> int:
    from .ledger import Ledger, LedgerError

    try:
        with Ledger() as ledger:
            ledger.reject_proposal(args.proposal_id, reason=args.reason)
    except LedgerError as error:
        print(f"reject failed: {error}", file=sys.stderr)
        return 1
    print(f"rejected proposal {args.proposal_id}")
    return 0


def _run_rollback(args: argparse.Namespace) -> int:
    from .gate import GateError, rollback
    from .ledger import LedgerError

    try:
        result = rollback(args.remedy_id, force=args.force)
    except (GateError, LedgerError, OSError) as error:
        print(f"rollback failed: {error}", file=sys.stderr)
        return 1
    print(f"rolled back remedy {result.remedy_id}")
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

    if args.command == "meter":
        return _run_meter(args)

    if args.command == "status":
        return _run_status()

    if args.command == "triage":
        return _run_triage(args)

    if args.command == "review":
        return _run_review(args)

    if args.command == "proposals":
        return _run_proposals(args)

    if args.command == "approve":
        return _run_approve(args)

    if args.command == "reject":
        return _run_reject(args)

    if args.command == "rollback":
        return _run_rollback(args)

    if args.command in {"run", "pump"}:
        from .pump import run_pump

        result = run_pump(background=getattr(args, "background", False))
        if result.background:
            print("pump: started in background")
        elif result.locked:
            print("pump: already running")
        else:
            print(
                f"pump: scanned {result.scanned}; triaged {result.triaged}; "
                f"curator calls {result.curator_calls}"
            )
        return 0

    if args.command == "schedule":
        from .pump import schedule

        installed, last_run = schedule(args.action)
        state = "installed" if installed else "not installed"
        suffix = f"; last pump {last_run}" if last_run else "; no pump has run yet"
        print(f"schedule: {state}{suffix}")
        return 0

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
        if args.hook_event == "session-start":
            from .notify import sessionstart_digest

            try:
                digest = sessionstart_digest()
                if digest:
                    print(digest)
            except BaseException:
                pass
            return 0
        parser.error("hook requires an event")

    issue = COMMAND_ISSUES[args.command]
    print(f"{args.command}: not implemented yet (issue #{issue})")
    return 0
