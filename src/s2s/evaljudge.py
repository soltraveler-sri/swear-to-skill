"""Versioned LLM judging for eval-run proposal artifacts.

The judge is intentionally a separate phase: pipeline output remains inspectable
on its own, while live and replay evals can attach model-graded evidence in a
separate ``judge-results.json`` file.  Configure ``[eval.judge]`` with a model
that differs from synthesis where possible; matching models are retained as an
explicit self-grading-bias warning rather than silently accepted.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
import json
from pathlib import Path
from statistics import fmean

from . import llm
from .config import load_config


REMEDY_PROMPT_NAME = "judge_remedy"
COUNTERFACTUAL_PROMPT_NAME = "judge_counterfactual"
PROMPT_VERSION = 1
RUBRIC_DIMENSIONS = (
    "actionability",
    "generality_without_vagueness",
    "trigger_quality",
    "leakage",
    "routing_fitness",
)


class JudgeError(RuntimeError):
    """A record cannot be judged safely or did not contain required artifacts."""


@dataclass(frozen=True)
class JudgeConfig:
    """Judge arm settings kept distinct from the pipeline-stage settings."""

    model: str = "sonnet"
    effort: str | None = None
    prompt_version: int = PROMPT_VERSION
    remedy_prompt_version: int | str | None = None
    counterfactual_prompt_version: int | str | None = None
    repeat: int = 1
    parallelism: int | None = None

    def __post_init__(self) -> None:
        if self.repeat < 1:
            raise ValueError("judge repeat must be at least 1")


def estimate_call_count(record: Mapping[str, object], *, repeat: int = 1, calibration_count: int | None = None) -> int:
    """Return calibration + quality + per-evidence counterfactual calls."""

    if repeat < 1:
        raise ValueError("judge repeat must be at least 1")
    proposals = _proposals(record)
    evidence_calls = sum(len(_evidence_ids(proposal)) for proposal in proposals)
    calibration = len(load_calibration()) if calibration_count is None else calibration_count
    return calibration + repeat * (len(proposals) + evidence_calls)


def run_judge(
    record: Mapping[str, object],
    output_path: Path,
    *,
    config: JudgeConfig | None = None,
) -> dict[str, object]:
    """Judge a completed live/replay record and atomically write its results."""

    if config is None:
        configured = load_config()
        config = JudgeConfig(
            model=configured.eval.judge.model,
            effort=configured.eval.judge.effort,
            prompt_version=configured.prompts.judge_remedy,
            counterfactual_prompt_version=configured.prompts.judge_counterfactual,
        )

    if record.get("mode") == "mock":
        result = _skipped_result(record, config)
        _write_results(output_path, result)
        return result
    if record.get("status") != "complete":
        raise JudgeError("only a completed eval record can be judged")

    remedy_version = config.remedy_prompt_version or config.prompt_version
    counter_version = config.counterfactual_prompt_version or config.prompt_version
    remedy_template, remedy_schema = llm.load_prompt(REMEDY_PROMPT_NAME, remedy_version)
    counter_template, counter_schema = llm.load_prompt(COUNTERFACTUAL_PROMPT_NAME, counter_version)
    proposals = _proposals(record)
    contexts = _contexts(record)
    calibration = _run_calibration(remedy_template, remedy_schema, config)
    self_grading = _self_grading_warning(record, config)

    judged: list[dict[str, object]] = []
    for proposal in proposals:
        artifact = proposal.get("artifact")
        if not isinstance(artifact, Mapping):
            raise JudgeError("proposal artifact must be an object for remedy judging")
        proposal_text = _json_text(artifact)
        quality_runs = llm.run_batch(
            range(config.repeat),
            lambda _: _judge_remedy(remedy_template, remedy_schema, proposal_text, config),
            parallelism=config.parallelism,
        )
        evidence = _evidence_ids(proposal)
        incidents: list[dict[str, object]] = []
        for incident_id in evidence:
            context = contexts.get(str(incident_id))
            if context is None:
                raise JudgeError(f"record lacks context pack for evidence incident {incident_id}")
            runs = llm.run_batch(
                range(config.repeat),
                lambda _: _judge_counterfactual(
                    counter_template, counter_schema, proposal_text, _json_text(context), config
                ),
                parallelism=config.parallelism,
            )
            incidents.append(_incident_result(incident_id, runs))
        judged.append(_proposal_result(proposal, quality_runs, incidents))

    calibration_status = calibration["check"]["status"]  # type: ignore[index]
    result: dict[str, object] = {
        "format_version": 1,
        "status": "complete" if calibration_status == "passed" else "failed",
        "mode": record.get("mode"),
        "record": "record.json",
        "judge_config": asdict(config),
        "prompts": {
            "remedy": f"{REMEDY_PROMPT_NAME}.v{str(remedy_version).removeprefix('v')}",
            "counterfactual": f"{COUNTERFACTUAL_PROMPT_NAME}.v{str(counter_version).removeprefix('v')}",
        },
        "self_grading_bias_warning": self_grading,
        "calibration": calibration,
        "proposals": judged,
        "metrics": _metrics(judged),
    }
    _write_results(output_path, result)
    if calibration_status != "passed":
        raise JudgeError("judge calibration ranking inversion detected")
    return result


def load_calibration(directory: Path | None = None) -> tuple[dict[str, object], ...]:
    """Load the hand-written calibration proposals and their expected bands."""

    root = directory or Path(__file__).resolve().parents[2] / "evals" / "corpus" / "v1" / "calibration"
    examples: list[dict[str, object]] = []
    for path in sorted(root.glob("*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise JudgeError(f"cannot load calibration example {path}: {error}") from error
        if not isinstance(value, dict) or not isinstance(value.get("id"), str):
            raise JudgeError(f"calibration example {path} must have an id")
        if value.get("expected_band") not in {"excellent", "mediocre", "bad"}:
            raise JudgeError(f"calibration example {path} has an invalid expected band")
        if not isinstance(value.get("proposal"), dict):
            raise JudgeError(f"calibration example {path} must contain a proposal object")
        examples.append(value)
    if len(examples) < 6:
        raise JudgeError("judge calibration requires at least six examples")
    return tuple(examples)


def calibration_check(scored: Sequence[Mapping[str, object]]) -> dict[str, object]:
    """Detect ranking inversions: every bad calibration must rank below excellent."""

    excellent = [float(item["mean_score"]) for item in scored if item.get("expected_band") == "excellent"]
    bad = [float(item["mean_score"]) for item in scored if item.get("expected_band") == "bad"]
    if not excellent or not bad:
        raise JudgeError("calibration requires both excellent and bad examples")
    inverted = max(bad) >= min(excellent)
    return {
        "status": "failed" if inverted else "passed",
        "inversion_detected": inverted,
        "excellent_min": min(excellent),
        "bad_max": max(bad),
    }


def score_variance(scores: Sequence[float]) -> float:
    """Population variance, suitable for reporting every observed repeat."""

    if not scores:
        return 0.0
    mean = fmean(scores)
    return fmean((score - mean) ** 2 for score in scores)


def _run_calibration(template: str, schema: Mapping[str, object], config: JudgeConfig) -> dict[str, object]:
    scored: list[dict[str, object]] = []
    for example in load_calibration():
        response = _judge_remedy(template, schema, _json_text(example["proposal"]), config)
        scored.append(
            {
                "id": example["id"],
                "expected_band": example["expected_band"],
                "mean_score": _rubric_mean(response),
                "rubric": response,
            }
        )
    return {"examples": scored, "check": calibration_check(scored)}


def _judge_remedy(template: str, schema: Mapping[str, object], proposal: str, config: JudgeConfig) -> dict[str, object]:
    return llm.call(
        _render(template, PROPOSAL=proposal), schema=schema, model=config.model,
        stage="judge", effort=config.effort,
    )


def _judge_counterfactual(template: str, schema: Mapping[str, object], proposal: str, context: str, config: JudgeConfig) -> dict[str, object]:
    return llm.call(
        _render(template, PROPOSAL=proposal, CONTEXT_PACK=context),
        schema=schema,
        model=config.model,
        stage="judge",
        effort=config.effort,
    )


def _proposal_result(proposal: Mapping[str, object], quality_runs: Sequence[Mapping[str, object]], incidents: Sequence[Mapping[str, object]]) -> dict[str, object]:
    run_means = [_rubric_mean(run) for run in quality_runs]
    prevented = [float(item["prevented_fraction"]) for item in incidents]
    partially = [float(item["partially_fraction"]) for item in incidents]
    return {
        "proposal_id": proposal.get("proposal_id"),
        "quality": {
            "runs": list(quality_runs),
            "mean_score": fmean(run_means),
            "variance": score_variance(run_means),
            "dimensions": {
                dimension: {
                    "mean_score": fmean([_dimension_score(run, dimension) for run in quality_runs]),
                    "variance": score_variance([_dimension_score(run, dimension) for run in quality_runs]),
                }
                for dimension in RUBRIC_DIMENSIONS
            },
        },
        "counterfactual": {
            "incidents": list(incidents),
            "prevented_fraction": fmean(prevented) if prevented else 0.0,
            "partially_fraction": fmean(partially) if partially else 0.0,
        },
    }


def _incident_result(incident_id: int, runs: Sequence[Mapping[str, object]]) -> dict[str, object]:
    verdicts = [str(run["verdict"]) for run in runs]
    return {
        "incident_id": incident_id,
        "runs": list(runs),
        "prevented_fraction": verdicts.count("prevented") / len(verdicts),
        "partially_fraction": verdicts.count("partially") / len(verdicts),
    }


def _metrics(proposals: Sequence[Mapping[str, object]]) -> dict[str, object]:
    quality = [float(item["quality"]["mean_score"]) for item in proposals]  # type: ignore[index]
    prevented = [float(item["counterfactual"]["prevented_fraction"]) for item in proposals]  # type: ignore[index]
    variance = [float(item["quality"]["variance"]) for item in proposals]  # type: ignore[index]
    return {
        "quality_mean": fmean(quality) if quality else 0.0,
        "quality_variance_mean": fmean(variance) if variance else 0.0,
        "prevented_fraction": fmean(prevented) if prevented else 0.0,
    }


def _rubric_mean(response: Mapping[str, object]) -> float:
    return fmean([_dimension_score(response, dimension) for dimension in RUBRIC_DIMENSIONS])


def _dimension_score(response: Mapping[str, object], dimension: str) -> float:
    value = response.get(dimension)
    if not isinstance(value, Mapping) or isinstance(value.get("score"), bool):
        raise JudgeError(f"judge response lacks valid {dimension} score")
    return float(value["score"])


def _proposals(record: Mapping[str, object]) -> tuple[Mapping[str, object], ...]:
    proposals = record.get("proposals")
    if not isinstance(proposals, list):
        raise JudgeError("record proposals must be a list")
    return tuple(item for item in proposals if isinstance(item, Mapping))


def _evidence_ids(proposal: Mapping[str, object]) -> tuple[int, ...]:
    values = proposal.get("evidence_incident_ids")
    if not isinstance(values, list) or any(isinstance(item, bool) or not isinstance(item, int) for item in values):
        raise JudgeError("proposal evidence_incident_ids must be integer list")
    return tuple(values)


def _contexts(record: Mapping[str, object]) -> dict[str, Mapping[str, object]]:
    raw = record.get("context_packs")
    if not isinstance(raw, Mapping):
        raise JudgeError("record must include context_packs for counterfactual judging")
    return {str(key): value for key, value in raw.items() if isinstance(value, Mapping)}


def _self_grading_warning(record: Mapping[str, object], config: JudgeConfig) -> str | None:
    stage = record.get("stage_config")
    synthesize = stage.get("synthesize") if isinstance(stage, Mapping) else None
    model = synthesize.get("model") if isinstance(synthesize, Mapping) else None
    if model == config.model:
        return "Judge model matches the synthesize model; quality scores may be self-grading biased."
    return None


def _skipped_result(record: Mapping[str, object], config: JudgeConfig) -> dict[str, object]:
    return {
        "format_version": 1,
        "status": "skipped",
        "mode": "mock",
        "reason": "mock mode uses canned pipeline responses; LLM judging is intentionally skipped",
        "judge_config": asdict(config),
        "metrics": {},
        "proposals": [],
    }


def _render(template: str, **values: str) -> str:
    rendered = template
    for name, value in values.items():
        rendered = rendered.replace("{{" + name + "}}", value)
    return rendered


def _json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def _write_results(path: Path, result: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)
