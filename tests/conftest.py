"""Shared zero-cost Claude CLI test harness."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import stat

import pytest


@dataclass(frozen=True)
class MockClaude:
    """A canned-response fake CLI with inspectable stdin and argv recordings."""

    queue_path: Path
    recording_path: Path

    def enqueue_response(self, payload: object) -> None:
        with self.queue_path.open("a", encoding="utf-8") as queue:
            queue.write(json.dumps(payload, separators=(",", ":")) + "\n")

    def invocations(self) -> list[dict[str, object]]:
        if not self.recording_path.exists():
            return []
        return [
            record
            for line in self.recording_path.read_text(encoding="utf-8").splitlines()
            if line and isinstance(record := json.loads(line), dict)
        ]


@dataclass(frozen=True)
class MockCodex:
    """A canned-response fake Codex CLI with inspectable stdin and argv."""

    queue_path: Path
    recording_path: Path

    def enqueue_response(self, payload: object) -> None:
        with self.queue_path.open("a", encoding="utf-8") as queue:
            queue.write(json.dumps(payload, separators=(",", ":")) + "\n")

    def invocations(self) -> list[dict[str, object]]:
        if not self.recording_path.exists():
            return []
        return [
            record
            for line in self.recording_path.read_text(encoding="utf-8").splitlines()
            if line and isinstance(record := json.loads(line), dict)
        ]


@pytest.fixture
def mock_claude(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> MockClaude:
    """Put a fake ``claude`` executable first on PATH and return its helpers."""

    bin_dir = tmp_path / "mock-bin"
    bin_dir.mkdir()
    executable = bin_dir / "claude"
    executable.write_text(Path(__file__).with_name("mockclaude.py").read_text(), encoding="utf-8")
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    queue_path = tmp_path / "claude-queue.jsonl"
    recording_path = tmp_path / "claude-recording.jsonl"
    queue_path.write_text("", encoding="utf-8")
    recording_path.write_text("", encoding="utf-8")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("S2S_MOCK_CLAUDE_QUEUE", str(queue_path))
    monkeypatch.setenv("S2S_MOCK_CLAUDE_RECORDING", str(recording_path))
    monkeypatch.setenv("S2S_HOME", str(tmp_path / "s2s-home"))
    return MockClaude(queue_path=queue_path, recording_path=recording_path)


@pytest.fixture
def mock_codex(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> MockCodex:
    """Put a fake ``codex`` executable first on PATH and return its helpers."""

    bin_dir = tmp_path / "mock-codex-bin"
    bin_dir.mkdir()
    executable = bin_dir / "codex"
    executable.write_text(Path(__file__).with_name("mockcodex.py").read_text(), encoding="utf-8")
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    queue_path = tmp_path / "codex-queue.jsonl"
    recording_path = tmp_path / "codex-recording.jsonl"
    queue_path.write_text("", encoding="utf-8")
    recording_path.write_text("", encoding="utf-8")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("S2S_MOCK_CODEX_QUEUE", str(queue_path))
    monkeypatch.setenv("S2S_MOCK_CODEX_RECORDING", str(recording_path))
    monkeypatch.setenv("S2S_HOME", str(tmp_path / "s2s-home"))
    return MockCodex(queue_path=queue_path, recording_path=recording_path)
