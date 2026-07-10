"""Durable, content-addressed transcript archiving for Stage 0."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
import traceback
from typing import Literal, TextIO

from .adapters.claude_code import iter_sessions
from .config import load_config
from .ledger import Ledger
from .paths import resolve_paths
from .pump import spawn_background_pump


LOGGER = logging.getLogger(__name__)
COPY_CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True)
class ArchiveResult:
    """The observable outcome of one archive attempt."""

    source_path: Path
    archive_path: Path
    status: Literal["new", "updated", "skipped"]

    @property
    def enqueued(self) -> bool:
        """Whether this attempt appended a queue item."""

        return self.status != "skipped"


@dataclass(frozen=True)
class BackfillResult:
    """Streaming backfill counters suitable for CLI reporting and tests."""

    found: int
    new: int
    skipped: int
    failed: int = 0


def archive_transcript(path: Path | str, *, source: str = "claude-code") -> ArchiveResult:
    """Atomically archive one transcript and enqueue newly archived content.

    Equality is established by size plus a streaming SHA-256 digest. If an
    existing archive differs, the new content wins. A queue failure restores the
    previous archive (or removes a newly created one) before the error escapes.
    """

    source_path = Path(path).expanduser()
    if not source_path.is_file():
        raise FileNotFoundError(f"transcript is not a readable file: {source_path}")

    if source not in {"claude-code", "codex"}:
        raise ValueError(f"unsupported transcript source: {source}")
    project_slug = "codex" if source == "codex" else source_path.parent.name
    session_id = source_path.stem
    if not project_slug or not session_id:
        raise ValueError(f"cannot derive project and session names from {source_path}")

    paths = resolve_paths()
    destination = paths.archive_dir / project_slug / f"{session_id}.jsonl"
    _mkdir_private(paths.home)
    _mkdir_private(paths.archive_dir)
    _mkdir_private(destination.parent)

    with _destination_lock(destination):
        return _archive_to_destination(source_path, destination, source_name=source)


def _archive_to_destination(source: Path, destination: Path, *, source_name: str = "claude-code") -> ArchiveResult:
    """Perform one archive attempt while the destination lock is held."""

    if destination.exists():
        if not destination.is_file():
            raise IsADirectoryError(f"archive destination is not a file: {destination}")
        if _same_content(source, destination):
            return ArchiveResult(source, destination, "skipped")
        status: Literal["new", "updated"] = "updated"
    else:
        status = "new"

    staged_path = _copy_to_staged_file(source, destination.parent, destination.name)
    rollback_path: Path | None = None
    installed = False
    try:
        if status == "updated":
            rollback_path = _hardlink_for_rollback(destination)
        os.replace(staged_path, destination)
        installed = True
        _fsync_directory(destination.parent)

        try:
            with Ledger() as ledger:
                ledger.enqueue_item(str(destination), source_name)
        except BaseException:
            _restore_archive_after_queue_failure(destination, rollback_path, status)
            installed = False
            raise

        if status == "updated":
            LOGGER.warning(
                "Re-archived changed transcript %s to %s (last-write-wins)",
                source,
                destination,
            )
        return ArchiveResult(source, destination, status)
    finally:
        staged_path.unlink(missing_ok=True)
        if rollback_path is not None:
            rollback_path.unlink(missing_ok=True)
        if installed:
            _fsync_directory(destination.parent)


def backfill(
    base_dir: Path | None = None,
    *,
    output: TextIO | None = None,
    error_output: TextIO | None = None,
) -> BackfillResult:
    """Archive historical Claude Code sessions and enabled Codex rollouts.

    Passing ``base_dir`` remains the focused Claude fixture/API path.  The normal
    CLI path discovers Codex too when its source switch is enabled.
    """

    output = sys.stdout if output is None else output
    error_output = sys.stderr if error_output is None else error_output
    found = new = skipped = failed = 0

    discovered: list[tuple[Path, str]] = [(path, "claude-code") for path in iter_sessions(base_dir)]

    for transcript_path, source_name in discovered:
        found += 1
        try:
            result = archive_transcript(transcript_path, source=source_name)
        except Exception as error:
            failed += 1
            print(f"backfill: failed {transcript_path}: {error}", file=error_output)
        else:
            if result.status == "skipped":
                skipped += 1
            else:
                new += 1

        if found % 100 == 0:
            print(
                f"Backfill progress: found={found} new={new} "
                f"skipped={skipped} failed={failed}",
                file=output,
            )

    if base_dir is None and load_config().sources.codex:
        # Codex's history index is the detection source; it joins and archives
        # only matched rollouts, rather than parsing every full transcript first.
        from .scanner import scan_codex_history

        try:
            with Ledger() as ledger:
                codex_hits = scan_codex_history(ledger)
        except Exception as error:
            failed += 1
            print(f"backfill: failed Codex history scan: {error}", file=error_output)
        else:
            found += codex_hits
            new += codex_hits

    result = BackfillResult(found=found, new=new, skipped=skipped, failed=failed)
    print(
        f"Backfill complete: found={result.found} new={result.new} "
        f"skipped={result.skipped} failed={result.failed}",
        file=output,
    )
    return result


def handle_session_end(payload_stream: TextIO | None = None) -> int:
    """Archive a SessionEnd payload while guaranteeing a successful hook exit."""

    try:
        payload = json.load(sys.stdin if payload_stream is None else payload_stream)
        if not isinstance(payload, dict):
            raise ValueError("SessionEnd hook payload must be a JSON object")
        transcript_path = payload.get("transcript_path")
        if not isinstance(transcript_path, str) or not transcript_path.strip():
            raise ValueError("SessionEnd hook payload is missing transcript_path")
        archive_transcript(transcript_path)
        # The hook is deliberately limited to durable archive work plus this
        # detached spawn.  The pump owns scan/LLM work outside hook latency.
        spawn_background_pump()
    except BaseException as error:
        _log_hook_failure(error)
    return 0


def _same_content(left: Path, right: Path) -> bool:
    if left.stat().st_size != right.stat().st_size:
        return False
    return _sha256(left) == _sha256(right)


def _sha256(path: Path) -> bytes:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(COPY_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.digest()


def _copy_to_staged_file(source: Path, directory: Path, destination_name: str) -> Path:
    descriptor, raw_path = tempfile.mkstemp(
        prefix=f".{destination_name}.", suffix=".tmp", dir=directory
    )
    staged_path = Path(raw_path)
    try:
        with os.fdopen(descriptor, "wb") as staged, source.open("rb") as transcript:
            for chunk in iter(lambda: transcript.read(COPY_CHUNK_SIZE), b""):
                staged.write(chunk)
            staged.flush()
            os.fsync(staged.fileno())
        return staged_path
    except BaseException:
        staged_path.unlink(missing_ok=True)
        raise


def _hardlink_for_rollback(destination: Path) -> Path:
    descriptor, raw_path = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".rollback", dir=destination.parent
    )
    os.close(descriptor)
    rollback_path = Path(raw_path)
    rollback_path.unlink()
    try:
        os.link(destination, rollback_path)
    except BaseException:
        rollback_path.unlink(missing_ok=True)
        raise
    return rollback_path


def _restore_archive_after_queue_failure(
    destination: Path,
    rollback_path: Path | None,
    status: Literal["new", "updated"],
) -> None:
    try:
        if status == "updated" and rollback_path is not None:
            os.replace(rollback_path, destination)
        else:
            destination.unlink(missing_ok=True)
        _fsync_directory(destination.parent)
    except OSError:
        LOGGER.exception("Unable to roll back archive after queue insertion failed")


def _mkdir_private(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)


@contextmanager
def _destination_lock(destination: Path) -> Iterator[None]:
    lock_path = destination.with_name(f".{destination.name}.lock")
    descriptor = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _log_hook_failure(error: BaseException) -> None:
    """Best-effort logging that is itself forbidden from breaking the hook."""

    try:
        home = resolve_paths().home
        _mkdir_private(home)
        log_dir = home / "logs"
        _mkdir_private(log_dir)
        log_path = log_dir / "hook.log"
        timestamp = datetime.now(timezone.utc).isoformat()
        detail = "".join(traceback.format_exception(type(error), error, error.__traceback__))
        descriptor = os.open(log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8") as log_file:
            log_file.write(f"[{timestamp}] SessionEnd hook failed\n{detail}")
            log_file.flush()
            os.fsync(log_file.fileno())
    except BaseException:
        pass
