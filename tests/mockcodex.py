#!/usr/bin/env python3
"""Queue-driven stand-in for ``codex exec`` used only by the test suite."""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import sys
import time


def _locked_append(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handle.write(value + "\n")
        handle.flush()
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _next_response(path: Path) -> object:
    with path.open("r+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        lines = handle.read().splitlines()
        if not lines:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            raise RuntimeError("mock Codex response queue is empty")
        handle.seek(0)
        handle.truncate()
        if len(lines) > 1:
            handle.write("\n".join(lines[1:]) + "\n")
        handle.flush()
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return json.loads(lines[0])


def _option_value(name: str) -> str | None:
    try:
        return sys.argv[sys.argv.index(name) + 1]
    except (ValueError, IndexError):
        return None


def main() -> int:
    queue_path = Path(os.environ["S2S_MOCK_CODEX_QUEUE"])
    recording_path = Path(os.environ["S2S_MOCK_CODEX_RECORDING"])
    _locked_append(
        recording_path,
        json.dumps({"argv": sys.argv[1:], "stdin": sys.stdin.read()}, separators=(",", ":")),
    )
    try:
        response = _next_response(queue_path)
    except Exception as error:
        print(str(error), file=sys.stderr)
        return 2

    if not isinstance(response, dict):
        print(str(response), end="")
        return 0
    if response.get("sleep_forever"):
        while True:
            time.sleep(60)

    output_path = _option_value("--output-last-message")
    if output_path and "result" in response:
        result = response["result"]
        rendered = result if isinstance(result, str) else json.dumps(result, separators=(",", ":"))
        Path(output_path).write_text(rendered, encoding="utf-8")

    stdout = response.get("stdout", '{"type":"turn.completed"}\n')
    stderr = response.get("stderr", "")
    print(str(stdout), end="")
    print(str(stderr), end="", file=sys.stderr)
    return int(response.get("returncode", response.get("exit_code", 0)))


if __name__ == "__main__":
    raise SystemExit(main())
