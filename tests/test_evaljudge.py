from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping

import pytest

from s2s import evaljudge, llm


def _rubric(score: int) -> dict[str, object]:
    return {
        dimension: {"score": score, "justification": "This is a concise evidence-based justification."}
        for dimension in evaljudge.RUBRIC_DIMENSIONS
    }


def _record(*, mode: str = "live", synthesize_model: str = "sonnet") -> dict[str, object]:
    return {
        "status": "complete",
        "mode": mode,
        "stage_config": {"synthesize": {"model": synthesize_model}},
        "proposals": [
            {
                "proposal_id": 7,
                "evidence_incident_ids": [11],
                "artifact": {
                    "remedy_type": "claude-md",
                    "failure_statement": "Explicit scope was not retained.",
                    "remedy_content": {"text": "Preserve explicit user constraints before acting."},
                },
            }
        ],
        "context_packs": {
            "11": {
                "preceding_request": "Change one file only.",
                "agent_activity_digest": "Changed unrelated files.",
                "frustrated_message": "I asked for one file.",
                "following_exchange": "",
                "metadata": {"session_id": "synthetic"},
            }
        },
    }


def _provider(request: llm.LLMRequest) -> Mapping[str, object]:
    if "INCIDENT CONTEXT PACK:" in request.prompt:
        result: object = {"verdict": "prevented", "reasoning": "The rule preserves the stated scope."}
    else:
        result = _rubric(1 if "Be better and avoid mistakes." in request.prompt or "aurora-branch-fix" in request.prompt else 4)
    return {"result": result, "usage": {}, "total_cost_usd": 0.0}


def test_remedy_rubric_schema_requires_every_dimension() -> None:
    _, schema = llm.load_prompt("judge_remedy", 1)
    malformed = _rubric(4)
    malformed.pop("leakage")

    with pytest.raises(llm.SchemaValidationError, match="leakage"):
        llm.validate_schema(malformed, schema)


def test_calibration_inversion_detection_fails() -> None:
    result = evaljudge.calibration_check(
        (
            {"expected_band": "excellent", "mean_score": 3.0},
            {"expected_band": "excellent", "mean_score": 4.0},
            {"expected_band": "bad", "mean_score": 3.0},
            {"expected_band": "bad", "mean_score": 1.0},
        )
    )

    assert result == {
        "status": "failed",
        "inversion_detected": True,
        "excellent_min": 3.0,
        "bad_max": 3.0,
    }


def test_calibration_inversion_fails_judge_execution(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("S2S_HOME", str(tmp_path / "s2s-home"))
    path = tmp_path / "judge-results.json"

    def inverted(request: llm.LLMRequest) -> Mapping[str, object]:
        result: object = (
            {"verdict": "no", "reasoning": "This response only supports the inversion fixture."}
            if "INCIDENT CONTEXT PACK:" in request.prompt
            else _rubric(3)
        )
        return {"result": result, "usage": {}, "total_cost_usd": 0.0}

    with llm.using_response_provider(inverted), pytest.raises(
        evaljudge.JudgeError, match="ranking inversion"
    ):
        evaljudge.run_judge(_record(), path, config=evaljudge.JudgeConfig(parallelism=1))

    assert json.loads(path.read_text(encoding="utf-8"))["status"] == "failed"


def test_repeat_variance_uses_population_variance() -> None:
    assert evaljudge.score_variance([1.0, 3.0]) == pytest.approx(1.0)
    assert evaljudge.score_variance([]) == 0.0


def test_mock_mode_is_skipped_and_writes_results_file(tmp_path: Path) -> None:
    path = tmp_path / "judge-results.json"
    result = evaljudge.run_judge(_record(mode="mock"), path)

    assert result["status"] == "skipped"
    assert json.loads(path.read_text(encoding="utf-8"))["reason"].startswith("mock mode")


def test_judge_results_include_warning_variance_and_expected_shape(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "judge-results.json"
    monkeypatch.setenv("S2S_HOME", str(tmp_path / "s2s-home"))
    with llm.using_response_provider(_provider):
        result = evaljudge.run_judge(
            _record(),
            path,
            config=evaljudge.JudgeConfig(model="sonnet", repeat=2, parallelism=1),
        )

    assert result["self_grading_bias_warning"] is not None
    assert result["calibration"]["check"]["status"] == "passed"  # type: ignore[index]
    assert result["proposals"][0]["quality"]["variance"] == 0.0  # type: ignore[index]
    assert result["proposals"][0]["counterfactual"]["prevented_fraction"] == 1.0  # type: ignore[index]
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert set(saved) >= {"calibration", "judge_config", "metrics", "proposals", "self_grading_bias_warning"}
