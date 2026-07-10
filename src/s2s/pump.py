"""Durable opportunistic queue pump and optional local scheduling.

The status file is JSON for the SessionStart/notifier interface introduced in
issue #14.  Its stable shape is ``{generated_at, queue_depth, untriaged,
unreviewed, proposals_pending, last_scan, last_triage, last_curator_pass,
notes}``, where timestamps are ISO-8601 UTC strings or ``null`` and ``notes``
is a list of human-readable deferred-work messages.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import logging
import os
from pathlib import Path
import platform
import subprocess
import sys
from typing import Iterator

from .config import Config, effective_autonomy_state, load_config, set_autonomy_state
from .curator import LAST_PASS_META_KEY, pass_due, run_pass
from .gate import adjudicate_pending_autonomously, record_autonomy_pause
from .ledger import Ledger
from .llm import LLMError
from .paths import resolve_paths
from .scanner import scan_pending_queue
from .synthesist import synthesize_pending
from .triager import triage_pending


LOGGER = logging.getLogger(__name__)
LAST_SCAN_META_KEY = "pump.last_scan_at"
LAST_TRIAGE_META_KEY = "pump.last_triage_at"
LAST_PUMP_META_KEY = "pump.last_run_at"
LOCK_NAME = "pump.lock"
LOG_NAME = "pump.log"
LOG_MAX_BYTES = 1_000_000


@dataclass(frozen=True)
class PumpResult:
    """Small observable result used by the CLI and deterministic tests."""

    scanned: int = 0
    triaged: int = 0
    curator_calls: int = 0
    synthesized: int = 0
    auto_installed: int = 0
    auto_queued: int = 0
    autonomy_paused: bool = False
    locked: bool = False
    background: bool = False


def run_pump(*, background: bool = False) -> PumpResult:
    """Pump free scan work then threshold-gated LLM work, under one process lock."""

    if background:
        spawn_background_pump()
        return PumpResult(background=True)

    paths = resolve_paths()
    with _pump_lock(paths.home / LOCK_NAME) as acquired:
        if not acquired:
            return PumpResult(locked=True)

        notes: list[str] = []
        scanned = triaged = curator_calls = synthesized = auto_installed = auto_queued = 0
        autonomy_paused = False
        config = load_config()
        try:
            with Ledger() as ledger:
                scan_results = scan_pending_queue(ledger)
                scanned = len(scan_results)
                now = _utc_now()
                ledger.set_meta(LAST_SCAN_META_KEY, now)

                if _triage_due(ledger, config):
                    pending = len(ledger.untriaged_incidents())
                    cap = max(0, config.thresholds.triage_per_run_cap)
                    triage_results = triage_pending(ledger, limit=cap, assume_yes=True)
                    triaged = len(triage_results)
                    ledger.set_meta(LAST_TRIAGE_META_KEY, _utc_now())
                    deferred = max(0, pending - cap)
                    if deferred:
                        notes.append(f"{deferred} triage items deferred by cost cap")

                if pass_due(ledger, config):
                    curator_calls = run_pass(ledger, assume_yes=False).calls

                promoted_before = len(ledger.incidents_in_state("promoted"))
                if promoted_before:
                    synthesis_results = synthesize_pending(ledger, assume_yes=False)
                    synthesized = sum(len(result.proposal_ids) for result in synthesis_results)
                    if not synthesis_results and ledger.incidents_in_state("promoted"):
                        notes.append(
                            f"{promoted_before} promoted incidents deferred by synthesis cost confirmation"
                        )

                if effective_autonomy_state(config).enabled:
                    autonomy = adjudicate_pending_autonomously(ledger, config=config)
                    auto_installed = autonomy.installed
                    auto_queued = autonomy.queued_for_human
                    autonomy_paused = autonomy.paused
                    if auto_installed:
                        noun = "remedy" if auto_installed == 1 else "remedies"
                        notes.append(
                            f"autonomous action installed {auto_installed} {noun} since the last pump — "
                            "'s2s log' to inspect"
                        )
                    if auto_queued:
                        notes.append(
                            f"autonomous action queued {auto_queued} proposal(s) for human review"
                        )

                ledger.set_meta(LAST_PUMP_META_KEY, _utc_now())
        except LLMError as error:
            if not effective_autonomy_state(config).enabled:
                raise
            set_autonomy_state("paused", paused_reason=str(error))
            record_autonomy_pause(f"Claude call failed during unattended pump: {error}")
            autonomy_paused = True
            notes.append(
                "autonomy paused after a Claude call failed — run 's2s autonomy on' to resume"
            )
        finally:
            # A failed stage still leaves a current durable health snapshot;
            # queued items remain untouched for the next pump.
            _write_status(notes)
        return PumpResult(
            scanned=scanned,
            triaged=triaged,
            curator_calls=curator_calls,
            synthesized=synthesized,
            auto_installed=auto_installed,
            auto_queued=auto_queued,
            autonomy_paused=autonomy_paused,
        )


def spawn_background_pump() -> None:
    """Detach one foreground ``python -m s2s pump`` and append its bounded log."""

    paths = resolve_paths()
    log_dir = paths.home / "logs"
    log_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    log_path = log_dir / LOG_NAME
    if log_path.exists() and log_path.stat().st_size >= LOG_MAX_BYTES:
        rotated = log_path.with_name(f"{LOG_NAME}.1")
        os.replace(log_path, rotated)
    with log_path.open("a", encoding="utf-8") as log_file:
        subprocess.Popen(
            [sys.executable, "-m", "s2s", "pump"],
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=log_file,
            start_new_session=True,
            close_fds=True,
        )


def schedule(
    action: str, *, directory: Path | None = None, system: str | None = None
) -> tuple[bool, str | None]:
    """Install, remove, or inspect the additive platform-native pump schedule."""

    if action not in {"install", "remove", "status"}:
        raise ValueError(f"unknown schedule action: {action}")
    paths = _schedule_paths(system or platform.system(), directory=directory)
    if action == "install":
        for path, content in paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
    elif action == "remove":
        for path, _ in paths:
            path.unlink(missing_ok=True)
    installed = all(path.is_file() for path, _ in paths)
    with Ledger() as ledger:
        last_run = ledger.get_meta(LAST_PUMP_META_KEY)
    return installed, last_run


def _schedule_paths(system: str, *, directory: Path | None = None) -> tuple[tuple[Path, str], ...]:
    if system == "Darwin":
        root = directory or (Path.home() / "Library" / "LaunchAgents")
        path = root / "com.s2s.pump.plist"
        return ((path, _launchd_plist()),)
    if system == "Linux":
        root = directory or (Path.home() / ".config" / "systemd" / "user")
        return (
            (root / "s2s-pump.service", _systemd_service()),
            (root / "s2s-pump.timer", _systemd_timer()),
        )
    raise RuntimeError(f"s2s scheduling is unsupported on {system}")


def _launchd_plist() -> str:
    return """<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">
<plist version=\"1.0\"><dict>
  <key>Label</key><string>com.s2s.pump</string>
  <key>ProgramArguments</key><array><string>s2s</string><string>pump</string></array>
  <key>StartInterval</key><integer>86400</integer>
</dict></plist>
"""


def _systemd_service() -> str:
    return """[Unit]
Description=swear-to-skill queue pump

[Service]
Type=oneshot
ExecStart=s2s pump
"""


def _systemd_timer() -> str:
    return """[Unit]
Description=Run swear-to-skill queue pump daily

[Timer]
OnCalendar=daily
Persistent=true

[Install]
WantedBy=timers.target
"""


def _triage_due(ledger: Ledger, config: Config) -> bool:
    pending = ledger.untriaged_incidents()
    if not pending:
        return False
    if len(pending) >= max(1, config.thresholds.triage_untriaged_count):
        return True
    now = datetime.now(timezone.utc)
    age = timedelta(hours=max(0, config.thresholds.triage_max_age_hours))
    last_run = _parse_timestamp(ledger.get_meta(LAST_TRIAGE_META_KEY))
    if last_run is not None:
        return now - last_run >= age
    oldest = min((_parse_timestamp(item.created_at) or now for item in pending), default=now)
    return now - oldest >= age


@contextmanager
def _pump_lock(path: Path) -> Iterator[bool]:
    """Acquire an O_EXCL PID lock, replacing only locks whose owner is dead."""

    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor: int | None = None
    for _ in range(2):
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            if _pid_is_alive(_read_lock_pid(path)):
                yield False
                return
            LOGGER.warning("Replacing stale pump lock %s", path)
            try:
                path.unlink()
            except FileNotFoundError:
                continue
        else:
            break
    if descriptor is None:
        yield False
        return
    try:
        os.write(descriptor, f"{os.getpid()}\n".encode())
        yield True
    finally:
        os.close(descriptor)
        path.unlink(missing_ok=True)


def _read_lock_pid(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _pid_is_alive(pid: int | None) -> bool:
    if pid is None or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _write_status(notes: list[str]) -> None:
    paths = resolve_paths()
    try:
        with Ledger() as ledger:
            payload = {
                "generated_at": _utc_now(),
                "queue_depth": len(ledger.pending_queue_items()),
                "untriaged": len(ledger.untriaged_incidents()),
                "unreviewed": len(ledger.curator_unreviewed_incidents()),
                "proposals_pending": ledger.pending_proposal_count(),
                "last_scan": ledger.get_meta(LAST_SCAN_META_KEY),
                "last_triage": ledger.get_meta(LAST_TRIAGE_META_KEY),
                "last_curator_pass": ledger.get_meta(LAST_PASS_META_KEY),
                "notes": notes,
            }
        paths.status_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        paths.status_file.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    except Exception:
        LOGGER.exception("Unable to write pump status file")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
