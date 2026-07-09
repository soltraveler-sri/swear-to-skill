from __future__ import annotations

from datetime import datetime, timezone
from io import StringIO
import json
import os
from pathlib import Path

import pytest

from s2s import archiver, pump
from s2s.curator import CuratorPassResult
from s2s.ledger import Ledger


@pytest.fixture
def s2s_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path / "s2s-home"
    monkeypatch.setenv("S2S_HOME", str(home))
    return home


def _detected(ledger: Ledger, count: int) -> None:
    for index in range(count):
        ledger.create_incident(
            source="claude-code",
            session_id=f"session-{index}",
            project="project",
            message=f"message {index}",
            occurred_at=datetime.now(timezone.utc).isoformat(),
        )


def test_live_lock_exits_quietly_without_running_stages(s2s_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s2s_home.mkdir()
    (s2s_home / "pump.lock").write_text(f"{os.getpid()}\n", encoding="utf-8")
    monkeypatch.setattr(pump, "scan_pending_queue", lambda ledger: pytest.fail("scan must not run"))

    result = pump.run_pump()

    assert result.locked is True


def test_stale_lock_is_replaced_and_released(s2s_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s2s_home.mkdir()
    lock = s2s_home / "pump.lock"
    lock.write_text("-1\n", encoding="utf-8")
    monkeypatch.setattr(pump, "scan_pending_queue", lambda ledger: [])

    assert pump.run_pump().locked is False
    assert not lock.exists()


def test_below_threshold_does_not_invoke_llm_stages(
    s2s_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with Ledger() as ledger:
        _detected(ledger, 1)
    (s2s_home / "config.toml").write_text(
        "[thresholds]\ntriage_untriaged_count = 2\ncurator_unreviewed_count = 2\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(pump, "scan_pending_queue", lambda ledger: [])
    monkeypatch.setattr(pump, "triage_pending", lambda *args, **kwargs: pytest.fail("triage must not run"))
    monkeypatch.setattr(pump, "run_pass", lambda *args, **kwargs: pytest.fail("curator must not run"))

    result = pump.run_pump()

    assert result == pump.PumpResult()


def test_stage_order_cap_and_status_note(s2s_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with Ledger() as ledger:
        _detected(ledger, 3)
    (s2s_home / "config.toml").write_text(
        "[thresholds]\ntriage_untriaged_count = 1\ntriage_per_run_cap = 2\n"
        "curator_unreviewed_count = 1\n",
        encoding="utf-8",
    )
    calls: list[str] = []

    def fake_scan(ledger: Ledger):
        calls.append("scan")
        return []

    def fake_triage(ledger: Ledger, *, limit: int | None, assume_yes: bool):
        calls.append(f"triage:{limit}:{assume_yes}")
        for incident in ledger.untriaged_incidents()[: limit or 0]:
            ledger.triage_incident(
                incident.id,
                label="ignored-instruction",
                one_liner="Ignored an instruction.",
                severity="2",
                confidence=0.9,
                context_pack_pointer="archive/project/session.jsonl#id",
            )
        return [object(), object()]

    def fake_curate(ledger: Ledger, *, assume_yes: bool):
        calls.append(f"curator:{assume_yes}")
        return CuratorPassResult(None, 1, (), 0, 0, 0)

    monkeypatch.setattr(pump, "scan_pending_queue", fake_scan)
    monkeypatch.setattr(pump, "triage_pending", fake_triage)
    monkeypatch.setattr(pump, "run_pass", fake_curate)

    result = pump.run_pump()
    status = json.loads((s2s_home / "status.txt").read_text(encoding="utf-8"))

    assert calls == ["scan", "triage:2:True", "curator:False"]
    assert result == pump.PumpResult(triaged=2, curator_calls=1)
    assert status["untriaged"] == 1
    assert status["notes"] == ["1 triage items deferred by cost cap"]
    assert set(status) == {
        "generated_at", "queue_depth", "untriaged", "unreviewed", "proposals_pending",
        "last_scan", "last_triage", "last_curator_pass", "notes",
    }


def test_session_end_spawns_pump_without_running_pipeline(monkeypatch: pytest.MonkeyPatch, s2s_home: Path) -> None:
    spawned: list[bool] = []
    monkeypatch.setattr(archiver, "archive_transcript", lambda path: object())
    monkeypatch.setattr(archiver, "spawn_background_pump", lambda: spawned.append(True))

    assert archiver.handle_session_end(StringIO(json.dumps({"transcript_path": "/tmp/session.jsonl"}))) == 0
    assert spawned == [True]


def test_schedule_writes_golden_platform_files_and_removes_them(s2s_home: Path, tmp_path: Path) -> None:
    launchd = tmp_path / "LaunchAgents"
    assert pump.schedule("install", directory=launchd, system="Darwin") == (True, None)
    assert "<string>s2s</string><string>pump</string>" in (
        launchd / "com.s2s.pump.plist"
    ).read_text(encoding="utf-8")
    assert pump.schedule("remove", directory=launchd, system="Darwin") == (False, None)

    systemd = tmp_path / "systemd-user"
    assert pump.schedule("install", directory=systemd, system="Linux") == (True, None)
    assert "ExecStart=s2s pump" in (systemd / "s2s-pump.service").read_text(encoding="utf-8")
    assert "OnCalendar=daily" in (systemd / "s2s-pump.timer").read_text(encoding="utf-8")
