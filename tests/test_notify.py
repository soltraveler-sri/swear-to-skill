from __future__ import annotations

from datetime import datetime, timedelta, timezone
import socketserver
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread

import pytest

from s2s import notify
from s2s.cli import main
from s2s.initcmd import initialize, uninstall


def _status(home, *, generated_at: str | None = None, pending: int = 0, notes=None) -> None:
    home.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "queue_depth": 0,
        "untriaged": 0,
        "unreviewed": 0,
        "proposals_pending": pending,
        "last_scan": None,
        "last_triage": None,
        "last_curator_pass": None,
        "notes": [] if notes is None else notes,
    }
    (home / "status.txt").write_text(json.dumps(payload), encoding="utf-8")


def test_sessionstart_digest_reads_status_only_and_is_silent_for_empty_stale_missing_corrupt(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("S2S_HOME", str(tmp_path))
    _status(tmp_path, pending=2)
    assert notify.sessionstart_digest() == (
        "s2s: 2 remedy proposals awaiting review — 's2s proposals' or '/s2s'"
    )

    _status(tmp_path)
    assert notify.sessionstart_digest() == ""
    _status(
        tmp_path,
        generated_at=(datetime.now(timezone.utc) - timedelta(days=8)).isoformat(),
        pending=2,
    )
    assert notify.sessionstart_digest() == ""
    (tmp_path / "status.txt").unlink()
    assert notify.sessionstart_digest() == ""
    (tmp_path / "status.txt").write_text("not json", encoding="utf-8")
    assert notify.sessionstart_digest() == ""

    class ExplodingLedger:
        def __init__(self, *args, **kwargs):
            raise AssertionError("SessionStart must not open the ledger")

    monkeypatch.setattr("s2s.ledger.Ledger", ExplodingLedger)
    _status(tmp_path, pending=1)
    assert notify.sessionstart_digest().startswith("s2s: 1 remedy proposal")


def test_sessionstart_autonomous_note_is_one_line(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("S2S_HOME", str(tmp_path))
    _status(tmp_path, notes=["autonomous action installed remedy #7"])
    result = notify.sessionstart_digest()
    assert result.startswith("s2s: autonomous action:")
    assert "\n" not in result


def test_sessionstart_hook_always_succeeds(monkeypatch: pytest.MonkeyPatch, tmp_path, capsys) -> None:
    monkeypatch.setenv("S2S_HOME", str(tmp_path))
    assert main(["hook", "session-start"]) == 0
    assert capsys.readouterr().out == ""


def test_init_round_trip_installs_both_hooks(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("S2S_HOME", str(tmp_path / "s2s-home"))
    settings = tmp_path / ".claude" / "settings.json"
    initialize(settings)
    document = json.loads(settings.read_text())
    commands = {
        entry["command"]
        for event in ("SessionStart", "SessionEnd")
        for group in document["hooks"][event]
        for entry in group["hooks"]
    }
    assert commands == {"s2s hook session-start", "s2s hook session-end"}
    assert uninstall(settings) is True
    assert json.loads(settings.read_text()) == {}


def test_webhook_payload_shape_and_failure_swallowing(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("S2S_HOME", str(tmp_path))
    monkeypatch.setattr(notify, "WEBHOOK_TIMEOUT_S", 0.25)
    (tmp_path / "config.toml").write_text(
        '[notifications]\ndesktop = false\nwebhook_url = "http://127.0.0.1:0/notify"\n'
        'events = ["proposal_pending"]\n',
        encoding="utf-8",
    )
    received: list[dict[str, object]] = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def do_POST(self):  # noqa: N802
            length = int(self.headers["Content-Length"])
            received.append(json.loads(self.rfile.read(length)))
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *_args):
            return

    class QuietBindServer(ThreadingHTTPServer):
        def server_bind(self):
            # HTTPServer.server_bind calls socket.getfqdn(), a reverse-DNS
            # lookup that can stall ~35s on some resolvers. Tests skip it.
            socketserver.TCPServer.server_bind(self)
            self.server_name = "localhost"
            self.server_port = self.socket.getsockname()[1]

    server = QuietBindServer(("127.0.0.1", 0), Handler)
    server_url = f"http://127.0.0.1:{server.server_port}/notify"
    server_thread = Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    try:
        (tmp_path / "config.toml").write_text(
            f'[notifications]\nwebhook_url = "{server_url}"\nevents = ["proposal_pending"]\n',
            encoding="utf-8",
        )
        notify.emit("proposal_pending", proposal_count=2, proposal_id=9, title="Rule")
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join()

    assert received == [
        {
            "text": "s2s: 2 remedy proposals awaiting review",
            "event": "proposal_pending",
            "proposal_count": 2,
            "proposal_id": 9,
            "title": "Rule",
        }
    ]

    (tmp_path / "config.toml").write_text(
        '[notifications]\nwebhook_url = "http://127.0.0.1:1/notify"\nevents = ["proposal_pending"]\n',
        encoding="utf-8",
    )
    notify.emit("proposal_pending", proposal_count=1)
    assert (tmp_path / "logs" / "notify.log").is_file()


def test_desktop_argv_and_metadata_truncation(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("S2S_HOME", str(tmp_path))
    calls = []
    monkeypatch.setattr(notify.sys, "platform", "darwin")
    monkeypatch.setattr(notify.subprocess, "run", lambda *args, **kwargs: calls.append((args, kwargs)))
    notify._send_desktop("hello")
    assert calls[0][0][0] == ["osascript", "-e", 'display notification "hello" with title "swear-to-skill"']

    (tmp_path / "config.toml").write_text(
        '[notifications]\ndesktop = false\nevents = ["proposal_pending"]\n',
        encoding="utf-8",
    )
    notify.emit("proposal_pending", title="x" * 250)
    assert "truncated notification field 'title'" in (tmp_path / "logs" / "notify.log").read_text()
