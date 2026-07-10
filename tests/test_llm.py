from __future__ import annotations

import os
from pathlib import Path
import re
import sys

import pytest

from s2s.ledger import Ledger
from s2s.llm import (
    CLAUDE_REQUIRED_FLAGS,
    ClaudeAuthError,
    ClaudeNotFoundError,
    LLMTimeout,
    MalformedOutputError,
    build_argv,
    call,
    estimate_and_confirm,
    load_prompt,
    run_batch,
    validate_schema,
)


ECHO_SCHEMA = {
    "type": "object",
    "required": ["message"],
    "properties": {"message": {"type": "string"}},
}


def _envelope(result: object, *, input_tokens: int = 3, output_tokens: int = 2) -> dict[str, object]:
    return {
        "result": result,
        "session_id": "mock-session",
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
        "total_cost_usd": 0.0125,
    }


def _run_log() -> list[object]:
    with Ledger() as ledger:
        return ledger.connection.execute("SELECT * FROM run_log ORDER BY id").fetchall()


def test_call_uses_required_flags_parses_a_string_result_and_logs(mock_claude) -> None:
    mock_claude.enqueue_response(_envelope('{"message":"hello"}'))

    assert call("echo hello", schema=ECHO_SCHEMA, model="haiku", stage="triage") == {
        "message": "hello"
    }

    invocation = mock_claude.invocations()
    assert len(invocation) == 1
    assert all(flag in invocation[0]["argv"] for flag in CLAUDE_REQUIRED_FLAGS)
    assert invocation[0]["stdin"] == "echo hello"
    run_log = _run_log()
    assert len(run_log) == 1
    assert (run_log[0]["stage"], run_log[0]["model"], run_log[0]["tokens"]) == (
        "triage",
        "haiku",
        5,
    )
    assert run_log[0]["cost_usd"] == pytest.approx(0.0125)
    assert len(run_log[0]["input_digest"]) == 16


def test_malformed_output_is_retried_once_with_a_corrective_suffix(mock_claude) -> None:
    mock_claude.enqueue_response({"stdout": "not json"})
    mock_claude.enqueue_response(_envelope({"message": "recovered"}))

    assert call("fix it", schema=ECHO_SCHEMA, model="haiku", stage="triage") == {
        "message": "recovered"
    }

    invocations = mock_claude.invocations()
    assert len(invocations) == 2
    assert invocations[0]["stdin"] == "fix it"
    assert "previous response was not valid" in str(invocations[1]["stdin"])
    assert len(_run_log()) == 2


def test_schema_invalid_output_is_retried_then_raises_typed_error(mock_claude) -> None:
    mock_claude.enqueue_response(_envelope({"message": 42}))
    mock_claude.enqueue_response(_envelope({"message": 42}))

    with pytest.raises(MalformedOutputError, match="does not satisfy"):
        call("must be text", schema=ECHO_SCHEMA, model="haiku", stage="triage")

    assert len(mock_claude.invocations()) == 2
    assert len(_run_log()) == 2


def test_timeout_is_fast_typed_and_logged(mock_claude) -> None:
    mock_claude.enqueue_response({"sleep_forever": True})

    with pytest.raises(LLMTimeout):
        call("wait", schema=ECHO_SCHEMA, model="haiku", stage="triage", timeout_s=0.2)

    assert len(mock_claude.invocations()) == 1
    assert len(_run_log()) == 1


def test_missing_binary_is_actionable_and_still_logged(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("S2S_HOME", str(tmp_path / "s2s-home"))
    monkeypatch.setenv("PATH", "")

    with pytest.raises(ClaudeNotFoundError, match="install claude and check PATH"):
        call("missing", schema=ECHO_SCHEMA, model="haiku", stage="triage")

    assert len(_run_log()) == 1


def test_auth_failure_is_typed_and_logged(mock_claude) -> None:
    mock_claude.enqueue_response({"returncode": 1, "stderr": "not logged in; run claude login"})

    with pytest.raises(ClaudeAuthError, match="claude login"):
        call("auth", schema=ECHO_SCHEMA, model="haiku", stage="triage")

    assert len(_run_log()) == 1


def test_prompt_loader_validator_batch_and_estimate_helpers(
    mock_claude, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    text, schema = load_prompt("echo", 1)
    assert "supplied message" in text
    validate_schema({"message": "valid"}, schema)
    assert run_batch([1, 2, 3], lambda item: item * 2, parallelism=2) == [2, 4, 6]

    class NonInteractive:
        def isatty(self) -> bool:
            return False

    monkeypatch.setattr(sys, "stdin", NonInteractive())
    assert estimate_and_confirm(34, "sonnet") is False
    assert estimate_and_confirm(34, "sonnet", assume_yes=True) is True
    assert "Estimated LLM cost" in capsys.readouterr().out


def test_build_argv_keeps_required_flags_together_for_every_call() -> None:
    argv = build_argv(ECHO_SCHEMA, "haiku")
    assert argv[:4] == ["claude", "-p", "--output-format", "json"]
    assert tuple(argv[-2:]) == CLAUDE_REQUIRED_FLAGS
    assert "--json-schema" in argv and "--model" in argv


def test_only_llm_module_may_construct_a_claude_subprocess() -> None:
    source_dir = Path(__file__).parents[1] / "src" / "s2s"
    prohibited = re.compile(r"(?:subprocess[\\s\\S]{0,400}claude|claude[\\s\\S]{0,400}subprocess)")
    offenders = [
        path.name
        for path in source_dir.glob("*.py")
        if path.name != "llm.py" and prohibited.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


@pytest.mark.real_llm
@pytest.mark.skipif(os.environ.get("S2S_REAL_LLM") != "1", reason="set S2S_REAL_LLM=1 to opt in")
def test_real_claude_smoke_is_explicitly_opt_in(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("S2S_HOME", str(tmp_path / "home"))
    text, schema = load_prompt("echo", 1)
    response = call(
        f"{text}\n\nMessage: smoke",
        schema=schema,
        model="haiku",
        stage="real-llm-smoke",
        timeout_s=120,
    )
    assert isinstance(response["message"], str)


def test_transport_uses_real_user_home_and_private_neutral_cwd(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Sandbox HOME overrides steer pipeline writes, never claude credentials.

    Also: the neutral cwd must be per-process and 0o700 (a predictable shared
    path would allow local CLAUDE.md prompt-injection into pipeline calls).
    """
    import os as _os
    import pwd as _pwd
    import stat as _stat

    from s2s import llm

    captured: dict[str, object] = {}

    def fake_run(argv, **kwargs):
        captured["env"] = kwargs["env"]
        captured["cwd"] = kwargs["cwd"]

        class Done:
            returncode = 0
            stdout = '{"result": {"ok": true}, "usage": {}, "total_cost_usd": 0}'
            stderr = ""

        return Done()

    monkeypatch.setattr(llm.subprocess, "run", fake_run)
    request = llm.LLMRequest(
        argv=("claude",),
        prompt="p",
        schema={"type": "object"},
        model="haiku",
        stage="test",
        timeout_s=5,
        env={
            "HOME": str(tmp_path / "fake-home"),
            "CLAUDE_CONFIG_DIR": str(tmp_path / "fake-home" / ".claude"),
            "PATH": _os.environ.get("PATH", ""),
        },
    )
    llm.default_response_provider(request)

    real_home = _pwd.getpwuid(_os.getuid()).pw_dir
    assert captured["env"]["HOME"] == real_home
    # A sandboxed CLAUDE_CONFIG_DIR must never reach the transport (it
    # relocates claude's credentials); the process-start value wins.
    assert "CLAUDE_CONFIG_DIR" not in captured["env"] or captured["env"][
        "CLAUDE_CONFIG_DIR"
    ] == llm._PROCESS_START_CLAUDE_CONFIG_DIR
    mode = _os.stat(captured["cwd"]).st_mode
    assert _stat.S_IMODE(mode) == 0o700
    assert "s2s-neutral-cwd-" in str(captured["cwd"])
