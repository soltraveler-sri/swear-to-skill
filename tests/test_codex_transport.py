from __future__ import annotations

import os

import pytest

from s2s.ledger import Ledger
from s2s.llm import (
    CODEX_FLAT_CALL_ESTIMATE_USD,
    CodexNotFoundError,
    LLMRequest,
    build_argv,
    call,
    default_response_provider,
    estimated_call_cost,
)


ECHO_SCHEMA = {
    "type": "object",
    "required": ["message"],
    "properties": {"message": {"type": "string"}},
}


def _run_log() -> list[object]:
    with Ledger() as ledger:
        return ledger.connection.execute("SELECT * FROM run_log ORDER BY id").fetchall()


def test_codex_prefix_routes_strips_model_and_uses_ephemeral_structured_exec(mock_codex) -> None:
    mock_codex.enqueue_response({"result": {"message": "hello"}})

    assert call(
        "echo hello",
        schema=ECHO_SCHEMA,
        model="codex:gpt-5.6-luna",
        stage="triage",
    ) == {"message": "hello"}

    invocation = mock_codex.invocations()
    assert len(invocation) == 1
    argv = invocation[0]["argv"]
    assert argv[:3] == ["--ask-for-approval", "never", "exec"]
    assert argv[argv.index("--model") + 1] == "gpt-5.6-luna"
    assert "codex:gpt-5.6-luna" not in argv
    assert "--json" in argv and "--output-schema" in argv
    assert invocation[0]["stdin"] == "echo hello"

    # Default codex exec writes ~/.codex/sessions rollouts; this flag is the
    # strongest discovered guard against the scanner ingesting transport calls.
    assert "--ephemeral" in argv

    row = _run_log()[0]
    assert (row["transport"], row["model"]) == ("codex", "codex:gpt-5.6-luna")
    assert row["tokens"] is None and row["cost_usd"] is None


def test_missing_codex_is_typed_actionable_and_strictly_opt_in(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("S2S_HOME", str(tmp_path / "s2s-home"))
    monkeypatch.setenv("PATH", "")

    with pytest.raises(CodexNotFoundError, match="install the OpenAI Codex CLI.*codex login"):
        call(
            "missing",
            schema=ECHO_SCHEMA,
            model="codex:gpt-5.6-luna",
            stage="triage",
        )

    row = _run_log()[0]
    assert row["transport"] == "codex"
    assert row["tokens"] is None and row["cost_usd"] is None


def test_codex_estimate_uses_documented_flat_per_call_guess() -> None:
    assert estimated_call_cost("codex:gpt-5.6-luna") == CODEX_FLAT_CALL_ESTIMATE_USD


def test_default_live_provider_routes_codex_requests_for_eval_wrappers(mock_codex) -> None:
    """Eval live/record mode installs this provider instead of leaving it unset."""

    mock_codex.enqueue_response({"result": {"message": "wrapped"}})
    request = LLMRequest(
        argv=tuple(build_argv(ECHO_SCHEMA, "codex:gpt-5.6-luna")),
        prompt="echo wrapped",
        schema=ECHO_SCHEMA,
        model="codex:gpt-5.6-luna",
        stage="triage",
        timeout_s=5,
        env=dict(os.environ),
    )

    assert default_response_provider(request) == {"result": {"message": "wrapped"}}
    assert mock_codex.invocations()[0]["stdin"] == "echo wrapped"
