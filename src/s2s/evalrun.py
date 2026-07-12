"""Sandboxed three-mode evaluation runner over the synthetic golden corpus."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, redirect_stdout
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from hashlib import sha256
from io import StringIO
import json
import math
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import tomllib
from time import perf_counter
from typing import Literal
from unittest.mock import patch

from . import llm
from . import config as _config_module
from . import evaljudge
from .adapters import claude_code, codex
from .archiver import archive_transcript, backfill
from .config import load_config
from .curator import render_ledger_digest, run_pass
from .ledger import Ledger, Proposal
from .scanner import ScanResult, scan_pending_queue
from .synthesist import synthesize_pending
from .timeutils import utc_now_iso
from .triager import context_for_incident, triage_pending


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CORPUS = PROJECT_ROOT / "evals" / "corpus" / "v1"
DEFAULT_REPLAY_ROOT = PROJECT_ROOT / "evals" / "replays"
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "evalruns"
STAGE_ORDER = ("scan", "triage", "curate", "synthesize")
CURATOR_ESTIMATE_CHUNK = 10
PROFILE_PLACEHOLDER = "default"
REPLAY_COMMAND = "s2s eval --mode live --record"
PROFILE_ROOT = PROJECT_ROOT / "evals" / "profiles"


class EvalRunError(RuntimeError):
    """Base error for a safe eval that could not complete."""


class ReplayCacheMissError(EvalRunError):
    """A replay key was absent; live fallback is intentionally forbidden."""

    def __init__(self, key: ReplayKey, *, fill_command: str = REPLAY_COMMAND) -> None:
        self.key = key
        super().__init__(
            f"replay cache miss for key {key}; run `{fill_command}` to fill it"
        )


class ReplayCollisionError(EvalRunError):
    """A truncated digest resolved to a different recorded request."""


class EvalConfirmationDeclined(EvalRunError):
    """The explicit live-spend confirmation was not granted."""


@dataclass(frozen=True)
class EvalStageConfig:
    """One profile-ready stage arm; effort is reserved until pipeline support lands."""

    model: str
    effort: str | None = None
    prompt_version: int | str = 1


@dataclass(frozen=True)
class EvalProfile:
    """A strict, named arm whose only mutable surfaces are LLM settings."""

    name: str
    triage: EvalStageConfig
    curate: EvalStageConfig
    synthesize: EvalStageConfig
    judge: EvalStageConfig
    source: Path


@dataclass(frozen=True)
class MatrixRunResult:
    """Artifacts produced by one sequential, same-corpus experiment."""

    root: Path
    arms: tuple[EvalRunResult, ...]
    report_markdown_path: Path
    report_html_path: Path


@dataclass(frozen=True)
class EvalRunConfig:
    """Complete runner configuration, including future profile dimensions."""

    corpus: Path = DEFAULT_CORPUS
    mode: Literal["mock", "replay", "live"] | None = None
    record: bool = False
    assume_yes: bool = False
    quick: bool = False
    stages: tuple[str, ...] = STAGE_ORDER
    keep: bool = False
    profile: str = PROFILE_PLACEHOLDER
    # Repeat runs use fresh sandboxes and identical corpus inputs.  ``cycles``
    # reserves the pump-tranche record shape; one is the normal single pump.
    repeat: int = 1
    cycles: int = 1
    triage: EvalStageConfig = EvalStageConfig("haiku")
    curate: EvalStageConfig = EvalStageConfig("sonnet")
    synthesize: EvalStageConfig = EvalStageConfig("sonnet")
    judge: EvalStageConfig = EvalStageConfig("sonnet")
    judge_repeat: int = 1
    replay_root: Path = DEFAULT_REPLAY_ROOT
    output_root: Path = DEFAULT_OUTPUT_ROOT
    sandbox_root: Path | None = None
    live_estimate_handled: bool = False

    @classmethod
    def production_defaults(cls, **overrides: object) -> EvalRunConfig:
        """Build a profile shell whose model choices match production config."""

        loaded = load_config()
        models = loaded.models
        values: dict[str, object] = {
            "triage": EvalStageConfig(models.triage, models.triage_effort, loaded.prompts.triage),
            "curate": EvalStageConfig(models.curate, models.curate_effort, loaded.prompts.curate),
            "synthesize": EvalStageConfig(models.synthesize, models.synthesize_effort, loaded.prompts.synthesize),
            "judge": EvalStageConfig(loaded.eval.judge.model, effort=loaded.eval.judge.effort, prompt_version=loaded.prompts.judge_remedy),
        }
        values.update(overrides)
        return cls(**values)  # type: ignore[arg-type]

    def stage(self, name: str) -> EvalStageConfig:
        if name not in {"triage", "curate", "synthesize", "judge"}:
            raise ValueError(f"unknown LLM eval stage {name!r}")
        return getattr(self, name)


@dataclass(frozen=True)
class EvalCallEstimate:
    triage: int
    curate: int
    synthesize: int
    judge: int = 0

    @property
    def total(self) -> int:
        return self.triage + self.curate + self.synthesize + self.judge


@dataclass(frozen=True)
class ReplayKey:
    stage: str
    input_digest: str
    model: str

    @classmethod
    def from_request(cls, request: llm.LLMRequest) -> ReplayKey:
        return cls(request.stage, request.input_digest, request.model)

    def __str__(self) -> str:
        return f"({self.stage}, {self.input_digest}, {self.model})"


@dataclass(frozen=True)
class EvalRunResult:
    record_path: Path
    judge_results_path: Path
    mode: str
    sandbox_path: Path | None
    detections: int
    triaged: int
    proposals: int


@dataclass(frozen=True)
class EvalRescoreResult:
    """Artifacts refreshed from an already-completed eval record."""

    record_path: Path
    scores_path: Path
    report_markdown_path: Path
    report_html_path: Path
    verdict: str


def load_profile(name: str, *, root: Path = PROFILE_ROOT) -> EvalProfile:
    """Load one named profile and reject every non-comparison control surface."""

    if not re.fullmatch(r"[a-z][a-z0-9-]*", name):
        raise EvalRunError(f"invalid profile name {name!r}")
    path = root / f"{name}.toml"
    try:
        with path.open("rb") as file:
            document = tomllib.load(file)
    except FileNotFoundError as error:
        raise EvalRunError(f"unknown eval profile {name!r}: {path}") from error
    except tomllib.TOMLDecodeError as error:
        raise EvalRunError(f"invalid eval profile {path}: {error}") from error
    if not isinstance(document, dict):
        raise EvalRunError(f"eval profile {path} must be a TOML table")
    allowed = {"profile", "triage", "curate", "synthesize", "judge"}
    unknown = sorted(set(document) - allowed)
    if unknown:
        raise EvalRunError(
            f"eval profile {path} has forbidden key(s): {', '.join(unknown)}; "
            "profiles may only vary declared LLM stages"
        )
    profile_meta = document.get("profile", {})
    if not isinstance(profile_meta, dict) or set(profile_meta) - {"description"}:
        raise EvalRunError(f"eval profile {path} [profile] accepts only description")
    stages = {stage: _profile_stage(path, stage, document.get(stage)) for stage in ("triage", "curate", "synthesize", "judge")}
    return EvalProfile(name, stages["triage"], stages["curate"], stages["synthesize"], stages["judge"], path)


def _profile_stage(path: Path, stage: str, value: object) -> EvalStageConfig:
    if not isinstance(value, dict):
        raise EvalRunError(f"eval profile {path} must define [{stage}]")
    unknown = sorted(set(value) - {"model", "effort", "prompt_version"})
    if unknown:
        raise EvalRunError(f"eval profile {path} [{stage}] has unknown key(s): {', '.join(unknown)}")
    model = value.get("model")
    effort = value.get("effort")
    prompt_version = value.get("prompt_version")
    if not isinstance(model, str) or not model.strip():
        raise EvalRunError(f"eval profile {path} [{stage}].model must be a non-empty string")
    if effort is not None and (not isinstance(effort, str) or not effort.strip()):
        raise EvalRunError(f"eval profile {path} [{stage}].effort must be a non-empty string or absent")
    if not isinstance(prompt_version, (str, int)) or isinstance(prompt_version, bool):
        raise EvalRunError(f"eval profile {path} [{stage}].prompt_version must be a version string or positive integer")
    try:
        # Validate the spelling now. Assets are loaded again at the stage boundary.
        version = str(prompt_version)
        if version.startswith("v"):
            version = version[1:]
        if not version.isdigit() or int(version) < 1:
            raise ValueError
    except ValueError as error:
        raise EvalRunError(f"eval profile {path} [{stage}].prompt_version is invalid") from error
    return EvalStageConfig(model.strip(), effort, prompt_version)


def parse_matrix_profiles(value: str) -> tuple[str, ...]:
    """Parse the CLI's ordered, baseline-first matrix arm list."""

    names = tuple(item.strip() for item in value.split(",") if item.strip())
    if len(names) < 2:
        raise ValueError("--matrix requires at least two comma-separated profiles")
    if len(set(names)) != len(names):
        raise ValueError("--matrix profile names must be unique")
    return names


@dataclass(frozen=True)
class _Sandbox:
    root: Path
    home: Path
    s2s_home: Path
    projects_dir: Path
    codex_home: Path
    skills_dir: Path
    global_claude_md: Path


@dataclass(frozen=True)
class _Annotation:
    data: Mapping[str, object]
    message: str
    project: str

    @property
    def corpus_id(self) -> str:
        return str(self.data["uuid"])


class ReplayProvider:
    """Serve only exact recorded envelopes from one corpus/profile cache."""

    deterministic = True

    def __init__(self, directory: Path, *, fill_command: str = REPLAY_COMMAND) -> None:
        self.directory = directory
        self.fill_command = fill_command

    def __call__(self, request: llm.LLMRequest) -> Mapping[str, object]:
        key = ReplayKey.from_request(request)
        path = _replay_path(self.directory, key)
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise ReplayCacheMissError(key, fill_command=self.fill_command) from error
        except (OSError, json.JSONDecodeError) as error:
            raise EvalRunError(f"cannot read replay {path}: {error}") from error
        if not isinstance(document, dict) or document.get("key") != asdict(key):
            raise EvalRunError(f"replay file does not match key {key}: {path}")
        request_record = document.get("request")
        if not isinstance(request_record, dict) or request_record.get("prompt") != request.prompt:
            raise ReplayCollisionError(
                f"replay digest collision for key {key}; recorded prompt differs"
            )
        response = document.get("response")
        if not isinstance(response, dict):
            raise EvalRunError(f"replay response is not an object: {path}")
        return response


class RecordingProvider:
    """Tee a supplied live transport into stable replay request/response files."""

    deterministic = False

    def __init__(self, directory: Path, upstream: llm.ResponseProvider) -> None:
        self.directory = directory
        self.upstream = upstream

    def __call__(self, request: llm.LLMRequest) -> Mapping[str, object]:
        response = dict(self.upstream(request))
        key = ReplayKey.from_request(request)
        path = _replay_path(self.directory, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        document = {
            "format_version": 1,
            "key": asdict(key),
            "request": {
                "stage": request.stage,
                "model": request.model,
                "prompt": request.prompt,
                "schema": request.schema,
            },
            "response": response,
        }
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise EvalRunError(f"cannot validate existing replay {path}: {error}") from error
            existing_request = existing.get("request") if isinstance(existing, dict) else None
            if not isinstance(existing_request, dict) or existing_request.get("prompt") != request.prompt:
                raise ReplayCollisionError(f"refusing to overwrite colliding replay key {key}")
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(_stable_json(document, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
        return response


class MockProvider:
    """Manifest-derived canned judgments for a free wiring-only run."""

    deterministic = True

    def __init__(self, annotations: Sequence[_Annotation]) -> None:
        self.annotations = tuple(annotations)

    def __call__(self, request: llm.LLMRequest) -> Mapping[str, object]:
        if request.stage == "triage":
            result = self._triage(request.prompt)
        elif request.stage == "curate" and "BEGIN FULL CONTEXT PACK" in request.prompt:
            result = self._curate(request.prompt)
        elif request.stage == "curate":
            result = {"propose_label": None, "merge_labels": []}
        elif request.stage == "synthesize":
            result = self._synthesize(request.prompt)
        elif request.stage == "judge":
            result = self._judge(request.prompt)
        else:
            raise EvalRunError(f"mock provider has no canned stage {request.stage!r}")
        return {
            "result": result,
            "usage": {"input_tokens": 0, "output_tokens": 0},
            "total_cost_usd": 0.0,
        }

    def _judge(self, prompt: str) -> dict[str, object]:
        if "INCIDENT CONTEXT PACK:" in prompt:
            return {"verdict": "prevented", "reasoning": "The proposed rule directly addresses the recorded failure."}
        low_quality = "Be better and avoid mistakes." in prompt or "aurora-branch-fix" in prompt
        score = 1 if low_quality else 4
        justification = "The remedy is concrete and appropriately scoped." if not low_quality else "The remedy is vague or tied to evidence-specific details."
        return {
            dimension: {"score": score, "justification": justification}
            for dimension in evaljudge.RUBRIC_DIMENSIONS
        }

    def _annotation_for_message(self, message: str) -> _Annotation:
        exact = next((item for item in self.annotations if item.message == message.strip()), None)
        if exact is not None:
            return exact
        contained = [item for item in self.annotations if item.message and item.message in message]
        if len(contained) == 1:
            return contained[0]
        raise EvalRunError("mock provider could not resolve a manifest incident from the prompt")

    def _triage(self, prompt: str) -> dict[str, object]:
        message = _between(prompt, "Frustrated message:\n", "\n\nFollowing exchange:")
        annotation = self._annotation_for_message(message)
        cluster = str(annotation.data["cluster_id"])
        label = cluster if cluster not in {"singleton", "decoy"} else "other"
        authentic = bool(annotation.data["authentic"])
        return {
            "authentic": authentic,
            "reason": str(annotation.data["rationale"]),
            "label": label,
            "one_liner": (
                f"Agent failure matching {label}."
                if authentic
                else "Quoted, playful, or third-party frustration."
            ),
            "severity": 2 if authentic else 1,
            "confidence": 0.99,
        }

    def _curate(self, prompt: str) -> dict[str, object]:
        verdicts: list[dict[str, object]] = []
        pack_pattern = re.compile(
            r"BEGIN FULL CONTEXT PACK incident_id=(\d+) label=([^ ]+)[^\n]*? origin=([^\n]+)\n"
            r"(.*?)END FULL CONTEXT PACK incident_id=\1",
            re.DOTALL,
        )
        for match in pack_pattern.finditer(prompt):
            incident_id, origin, pack = int(match.group(1)), match.group(3), match.group(4)
            message = _between(pack, "Frustrated message:\n", "\n\nFollowing exchange:")
            annotation = self._annotation_for_message(message)
            if origin == "qc":
                verdict = "resurrect" if annotation.data["authentic"] else "dismiss"
            elif annotation.data["remedy_worthy"]:
                verdict = "promote"
            else:
                verdict = "dismiss"
            verdicts.append(
                {
                    "incident_id": incident_id,
                    "verdict": verdict,
                    "reason": str(annotation.data["rationale"]),
                }
            )
        labels_text = _between(prompt, "CHUNK CLUSTERS:\n", "\n\nLEDGER DIGEST")
        labels = [line[2:] for line in labels_text.splitlines() if line.startswith("- ")]
        return {
            "incident_verdicts": verdicts,
            "cluster_verdicts": [
                {"label": label, "verdict": "synthesize", "reason": "Manifest wiring fixture."}
                for label in labels
            ],
        }

    def _synthesize(self, prompt: str) -> dict[str, object]:
        incident_ids = [
            int(value)
            for value in re.findall(r"BEGIN PROMOTED EVIDENCE incident_id=(\d+)", prompt)
        ]
        evidence: list[dict[str, object]] = []
        shapes: list[str] = []
        for block in re.findall(
            r"BEGIN PROMOTED EVIDENCE incident_id=\d+.*?END PROMOTED EVIDENCE",
            prompt,
            re.DOTALL,
        ):
            message = _between(block, "Frustrated message:\n", "\n\nFollowing exchange:")
            annotation = self._annotation_for_message(message)
            shapes.append(str(annotation.data["expected_remedy_shape"]))
            evidence.append(
                {
                    "incident_id": int(re.search(r"incident_id=(\d+)", block).group(1)),  # type: ignore[union-attr]
                    "quote": " ".join(message.split())[:180],
                }
            )
        if not evidence or {int(item["incident_id"]) for item in evidence} != set(incident_ids):
            raise EvalRunError("mock synthesist could not recover every promoted incident")
        remedy_type = "claude-md"
        if shapes and all(shape == "hook" for shape in shapes):
            remedy_type = "hook"
        elif shapes and all(shape == "benchmark-only" for shape in shapes):
            remedy_type = "benchmark-only"
        elif shapes and all(shape == "skill" for shape in shapes):
            remedy_type = "skill"
        content: dict[str, object]
        if remedy_type == "hook":
            content = {"event": "PreToolUse", "command_sketch": "verify the declared constraint"}
        elif remedy_type == "skill":
            content = {
                "name": "verify-before-claim",
                "description": "Use when reporting a change as complete.",
                "body_markdown": "Run the declared verification and report its result.",
            }
        elif remedy_type == "benchmark-only":
            content = {"note": "Keep this model-level miss as benchmark evidence."}
        else:
            content = {
                "text": "Follow the explicit user constraint and verify it before reporting completion.",
                "target": "global",
            }
        references = re.findall(
            r"^- ((?:skill:|claude-md:|proposal #)[^|]+?) \| ", prompt, re.MULTILINE
        )
        return {
            "remedy_type": remedy_type,
            "routing_rationale": "Cheapest manifest-derived wiring remedy.",
            "failure_statement": "An explicit workflow requirement was not reliably followed.",
            "remedy_content": content,
            "evidence": evidence,
            "dedup": [
                {"existing": reference, "verdict": "clear", "reason": "Wiring fixture is distinct."}
                for reference in references
            ],
            "confidence": 0.95,
        }


class _RetryTrackingProvider:
    """Count validation corrective calls without changing pipeline APIs."""

    def __init__(self, upstream: llm.ResponseProvider) -> None:
        self.upstream = upstream
        self.deterministic = bool(getattr(upstream, "deterministic", False))
        self.requests: Counter[str] = Counter()
        self.retries: Counter[str] = Counter()

    def __call__(self, request: llm.LLMRequest) -> Mapping[str, object]:
        self.requests[request.stage] += 1
        if request.prompt.endswith(llm.CORRECTIVE_SUFFIX) or "Your prior draft failed local validation." in request.prompt:
            self.retries[request.stage] += 1
        return self.upstream(request)


def parse_stages(value: str | Sequence[str] | None) -> tuple[str, ...]:
    """Parse a comma list and require an ordered pipeline prefix."""

    if value is None:
        return STAGE_ORDER
    raw = value.split(",") if isinstance(value, str) else list(value)
    stages = tuple(item.strip() for item in raw if item.strip())
    if not stages or stages != STAGE_ORDER[: len(stages)]:
        expected = ", ".join(",".join(STAGE_ORDER[:index]) for index in range(1, 5))
        raise ValueError(f"--stages must be an ordered pipeline prefix: {expected}")
    return stages


def quick_subset(manifest: Mapping[str, object]) -> tuple[str, ...]:
    """Select first named cluster, first worthy singleton, and first two decoys."""

    definitions = manifest.get("cluster_definitions")
    incidents = manifest.get("incidents")
    if not isinstance(definitions, dict) or not definitions or not isinstance(incidents, list):
        raise EvalRunError("manifest cannot produce the deterministic quick subset")
    first_cluster = next(iter(definitions))
    cluster = [item for item in incidents if isinstance(item, dict) and item.get("cluster_id") == first_cluster]
    singletons = [
        item
        for item in incidents
        if isinstance(item, dict)
        and item.get("cluster_id") == "singleton"
        and item.get("remedy_worthy") is True
    ]
    decoys = [item for item in incidents if isinstance(item, dict) and item.get("cluster_id") == "decoy"]
    selected = [*cluster, *singletons[:1], *decoys[:2]]
    if not cluster or not singletons or len(decoys) < 2:
        raise EvalRunError("manifest lacks the documented quick-subset cells")
    return tuple(str(item["uuid"]) for item in selected)


def estimate_call_counts(
    manifest: Mapping[str, object],
    selected_ids: Sequence[str] | None = None,
    *,
    curator_chunk: int = CURATOR_ESTIMATE_CHUNK,
    judge_repeat: int = 1,
) -> EvalCallEstimate:
    """Estimate triage detections, Curator chunks, and cluster/singleton synthesis."""

    incidents = manifest.get("incidents")
    if not isinstance(incidents, list) or curator_chunk < 1 or judge_repeat < 1:
        raise ValueError("estimate requires manifest incidents and a positive curator chunk")
    selected = set(selected_ids) if selected_ids is not None else None
    rows = [
        item
        for item in incidents
        if isinstance(item, dict) and (selected is None or str(item.get("uuid")) in selected)
    ]
    detections = [item for item in rows if item.get("lexicon_hit_expected") is True]
    named_clusters = {
        str(item["cluster_id"])
        for item in detections
        if item.get("authentic") is True
        and item.get("remedy_worthy") is True
        and item.get("cluster_id") not in {"singleton", "decoy"}
    }
    worthy_singletons = sum(
        item.get("cluster_id") == "singleton"
        and item.get("authentic") is True
        and item.get("remedy_worthy") is True
        for item in detections
    )
    synthesized_evidence = sum(
        item.get("authentic") is True
        and item.get("remedy_worthy") is True
        and item.get("cluster_id") != "decoy"
        for item in detections
    )
    judge_proposals = len(named_clusters) + worthy_singletons
    calibration_count = len(evaljudge.load_calibration())
    return EvalCallEstimate(
        triage=len(detections),
        curate=math.ceil(len(detections) / curator_chunk) if detections else 0,
        synthesize=judge_proposals,
        judge=calibration_count + judge_repeat * (judge_proposals + synthesized_evidence),
    )


def run_eval(
    config: EvalRunConfig,
    *,
    live_provider: llm.ResponseProvider | None = None,
) -> EvalRunResult:
    """Run an eval, adding repeat snapshots for stochastic-stage stability."""

    if config.repeat < 1 or config.cycles < 1:
        raise EvalRunError("--repeat and --cycles must both be at least 1")
    # A replay is deterministic by contract, so repeating it would only spend
    # time and manufacture a stability claim rather than measure one.
    if config.repeat == 1:
        return _run_eval_once(config, live_provider=live_provider)

    primary = _run_eval_once(replace(config, repeat=1), live_provider=live_provider)
    primary_record = _read_record(primary.record_path)
    mode = str(primary_record.get("mode"))
    if mode == "replay":
        primary_record["repeats"] = []
        primary_record["repeat_exempt"] = "replay is deterministic"
    else:
        repeats = [_repeat_snapshot(primary_record)]
        for _ in range(1, config.repeat):
            result = _run_eval_once(replace(config, repeat=1, keep=False), live_provider=live_provider)
            repeats.append(_repeat_snapshot(_read_record(result.record_path)))
        primary_record["repeats"] = repeats
    _write_record(primary.record_path, primary_record)
    return primary


def rescore_run(
    run_dir: Path,
    *,
    corpus: Path = DEFAULT_CORPUS,
    thresholds_path: Path | None = None,
) -> EvalRescoreResult:
    """Refresh scores and report without rerunning an eval pipeline or judge.

    The caller supplies an existing run directory, whose ``record.json`` and
    ``judge-results.json`` remain the only run facts used by this operation.
    """

    record_path = Path(run_dir) / "record.json"
    judge_path = record_path.with_name("judge-results.json")
    if not record_path.is_file():
        raise EvalRunError(f"rescore requires an existing record.json: {record_path}")
    if not judge_path.is_file():
        raise EvalRunError(f"rescore requires an existing judge-results.json: {judge_path}")
    manifest_path = Path(corpus) / "manifest.json"
    if not manifest_path.is_file():
        raise EvalRunError(f"rescore requires corpus manifest: {manifest_path}")

    from .evalreport import write_report
    from .evalscore import score_files

    score_files(record_path, manifest_path, thresholds_path=thresholds_path)
    report = write_report(record_path, thresholds_path=thresholds_path)
    return EvalRescoreResult(
        record_path=record_path,
        scores_path=record_path.with_name("scores.json"),
        report_markdown_path=report.markdown_path,
        report_html_path=report.html_path,
        verdict=report.verdict,
    )


def run_matrix(
    config: EvalRunConfig,
    profiles: Sequence[EvalProfile],
    *,
    live_provider: llm.ResponseProvider | None = None,
    thresholds_path: Path | None = None,
) -> MatrixRunResult:
    """Run named arms sequentially over one forced corpus and compare the artifacts."""

    if len(profiles) < 2:
        raise EvalRunError("a matrix requires at least two profiles")
    names = [profile.name for profile in profiles]
    if len(set(names)) != len(names):
        raise EvalRunError("matrix profiles must be unique")
    corpus = Path(config.corpus).resolve()
    _load_manifest(corpus)  # fail before creating any arm directory
    matrix_root = Path(config.output_root) / f"matrix-{_run_id()}"
    matrix_root.mkdir(parents=True, exist_ok=False)

    if config.mode == "live":
        estimate = _matrix_live_estimate(config, profiles)
        print(f"Estimated matrix LLM cost: ${estimate:.2f} across {len(profiles)} arms")
        if not config.assume_yes and not _confirm_live_run():
            raise EvalConfirmationDeclined("live matrix was not confirmed; no sandbox pipeline was run")

    results: list[EvalRunResult] = []
    for profile in profiles:
        arm_root = matrix_root / "arms" / profile.name
        arm = replace(
            config,
            corpus=corpus,
            profile=profile.name,
            triage=profile.triage,
            curate=profile.curate,
            synthesize=profile.synthesize,
            judge=profile.judge,
            output_root=arm_root,
            sandbox_root=arm_root / "sandbox",
            keep=True,
            live_estimate_handled=config.mode == "live",
        )
        results.append(run_eval(arm, live_provider=live_provider))

    from .evalreport import write_matrix_report
    from .evalscore import score_files

    for result in results:
        score_files(result.record_path, corpus / "manifest.json", thresholds_path=thresholds_path)

    report = write_matrix_report(
        matrix_root,
        [(profile, result.record_path) for profile, result in zip(profiles, results, strict=True)],
        default_corpus=DEFAULT_CORPUS,
    )
    return MatrixRunResult(matrix_root, tuple(results), report.markdown_path, report.html_path)


def _matrix_live_estimate(config: EvalRunConfig, profiles: Sequence[EvalProfile]) -> float:
    manifest = _load_manifest(Path(config.corpus).resolve())
    selected = quick_subset(manifest) if config.quick else None
    counts = estimate_call_counts(manifest, selected, judge_repeat=config.judge_repeat)
    stage_counts = {"triage": counts.triage, "curate": counts.curate, "synthesize": counts.synthesize, "judge": counts.judge}
    return sum(
        stage_counts[stage] * llm.estimated_call_cost(profile_stage.model)
        for profile in profiles
        for stage, profile_stage in (("triage", profile.triage), ("curate", profile.curate), ("synthesize", profile.synthesize), ("judge", profile.judge))
        if stage in config.stages or stage == "judge"
    )


def _run_eval_once(
    config: EvalRunConfig,
    *,
    live_provider: llm.ResponseProvider | None = None,
) -> EvalRunResult:
    """Run the real pipeline inside an isolated filesystem and response transport."""

    corpus = Path(config.corpus).resolve()
    manifest = _load_manifest(corpus)
    stages = parse_stages(config.stages)
    corpus_version = str(manifest["version"])
    replay_dir = Path(config.replay_root) / corpus_version / _safe_component(config.profile)
    configured_mode = config.mode or load_config().eval.mode
    mode = _resolve_mode(configured_mode, replay_dir)
    if config.record and mode != "live":
        raise EvalRunError("--record is valid only with --mode live")
    selected_ids = quick_subset(manifest) if config.quick else None
    cycle_tranches = _tranches(manifest, selected_ids, config.cycles)
    if mode == "live" and not config.live_estimate_handled:
        estimate = estimate_call_counts(manifest, selected_ids, judge_repeat=config.judge_repeat)
        has_calls = False
        for stage_name, calls in (
            ("triage", estimate.triage),
            ("curate", estimate.curate),
            ("synthesize", estimate.synthesize),
            ("judge", estimate.judge if "synthesize" in stages else len(evaljudge.load_calibration())),
        ):
            if (stage_name != "judge" and stage_name not in stages) or not calls:
                continue
            has_calls = True
            # Reuse the production estimator for each configured model, but keep
            # the trust-run decision to one explicit per-run confirmation below.
            llm.estimate_and_confirm(
                calls, config.stage(stage_name).model, assume_yes=True
            )
        if has_calls and not config.assume_yes and not _confirm_live_run():
            raise EvalConfirmationDeclined(
                "live eval was not confirmed; no sandbox pipeline was run"
            )

    annotations = _load_annotations(corpus, manifest)
    fed_annotations = tuple(
        item for item in annotations if selected_ids is None or item.corpus_id in selected_ids
    )
    output_dir = Path(config.output_root) / _run_id()
    output_dir.mkdir(parents=True, exist_ok=False)
    record_path = output_dir / "record.json"
    started_at = utc_now_iso()
    record: dict[str, object] = {
        "format_version": 1,
        "corpus_version": corpus_version,
        "mode": mode,
        "profile": config.profile,
        "git_describe": _git_describe(),
        "quick": config.quick,
        # Scoring must use the same annotation universe that the sandbox was
        # given.  Keep ids in the durable artifact rather than inferring them
        # later from detections (which deliberately omit lexicon misses).
        "fed_annotations": [item.corpus_id for item in fed_annotations],
        "fed_annotation_projects": {
            item.corpus_id: _annotation_project(item) for item in fed_annotations
        },
        "stages_requested": list(stages),
        "stage_config": {
            name: asdict(config.stage(name)) for name in ("triage", "curate", "synthesize", "judge")
        },
        "started_at": started_at,
        "completed_at": None,
        "status": "running",
        "sandbox": {
            "home": "$EVAL_ROOT/home",
            "s2s_home": "$EVAL_ROOT/s2s-home",
            "corpus_copied": True,
            "autonomy": "review",
        },
        "archive": {},
        "detections": [],
        "triage_verdicts": [],
        "curator": {"passes": [], "incident_verdicts": [], "cluster_verdicts": []},
        "proposals": [],
        "context_packs": {},
        "llm_run_log": [],
        "schema_validation": {"requests": {}, "retries": {}},
        "timings": {},
    }
    sandbox_path: Path | None = None
    try:
        with _sandbox(corpus, manifest, cycle_tranches[0], config, keep=config.keep) as sandbox:
            sandbox_path = sandbox.root if config.keep else None
            provider: llm.ResponseProvider | None
            if mode == "mock":
                provider = MockProvider(fed_annotations)
            elif mode == "replay":
                fill = f"{REPLAY_COMMAND} --corpus {corpus} --matrix {config.profile}"
                provider = ReplayProvider(replay_dir, fill_command=fill)
            elif config.record:
                provider = RecordingProvider(
                    replay_dir, live_provider or llm.default_response_provider
                )
            else:
                provider = live_provider
            tracked_provider = _RetryTrackingProvider(provider or llm.default_response_provider)
            with llm.using_response_provider(tracked_provider):
                # Stage APIs retain their normal estimate/status output. The eval
                # CLI owns one concise summary (and live already estimated above),
                # so suppress duplicate internal chatter without changing calls.
                with redirect_stdout(StringIO()):
                    if config.cycles == 1:
                        _drive_pipeline(
                            sandbox,
                            stages,
                            fed_annotations,
                            record,
                            deterministic=mode in {"mock", "replay"},
                        )
                    else:
                        _drive_cycles(
                            sandbox,
                            corpus,
                            manifest,
                            cycle_tranches,
                            config,
                            stages,
                            fed_annotations,
                            record,
                            deterministic=mode in {"mock", "replay"},
                        )
                    # The pipeline artifact is complete before the independent
                    # judge phase consumes it; a judge failure still marks the
                    # enclosing eval record failed in the outer handler.
                    record["status"] = "complete"
                    judge_results_path = output_dir / "judge-results.json"
                    judge_result = evaljudge.run_judge(
                        record,
                        judge_results_path,
                        config=evaljudge.JudgeConfig(
                            model=config.judge.model,
                            effort=config.judge.effort,
                            prompt_version=config.judge.prompt_version,
                            repeat=config.judge_repeat,
                        ),
                    )
                    record["judge_results"] = {
                        "path": judge_results_path.name,
                        "status": judge_result["status"],
                    }
                    _capture_run_log(record)
            record["schema_validation"] = {
                "requests": dict(tracked_provider.requests),
                "retries": dict(tracked_provider.retries),
            }
            record["status"] = "complete"
            record["completed_at"] = utc_now_iso()
            _write_record(record_path, record)
    except BaseException as error:
        record["status"] = "failed"
        record["completed_at"] = utc_now_iso()
        record["error"] = {"type": type(error).__name__, "message": str(error)}
        _write_record(record_path, record)
        raise

    detections = record["detections"]
    triage = record["triage_verdicts"]
    proposals = record["proposals"]
    assert isinstance(detections, list) and isinstance(triage, list) and isinstance(proposals, list)
    return EvalRunResult(
        record_path=record_path,
        judge_results_path=output_dir / "judge-results.json",
        mode=mode,
        sandbox_path=sandbox_path,
        detections=len(detections),
        triaged=len(triage),
        proposals=len(proposals),
    )


def _tranches(
    manifest: Mapping[str, object], selected_ids: Sequence[str] | None, cycles: int
) -> list[tuple[str, ...]]:
    """Partition the selected corpus deterministically for pump-cycle evals."""

    wanted = set(selected_ids) if selected_ids is not None else None
    rows = [
        item for item in manifest["incidents"]
        if isinstance(item, dict) and (wanted is None or str(item["uuid"]) in wanted)
    ]  # type: ignore[index]
    ids = [str(item["uuid"]) for item in rows]
    if cycles == 1:
        return [tuple(ids)]
    # A transcript is the archive/scanner unit.  Splitting one across tranches
    # would deliberately rewrite an archived source and turn this eval into a
    # test of that rewrite path rather than an O(new) pump cycle.
    by_session: dict[str, list[str]] = {}
    for item in rows:
        by_session.setdefault(str(item["session"]), []).append(str(item["uuid"]))
    target = max(1, math.ceil(len(ids) / cycles))
    tranches: list[list[str]] = [[]]
    for members in by_session.values():
        if tranches[-1] and len(tranches[-1]) + len(members) > target:
            tranches.append([])
        tranches[-1].extend(members)
    while len(tranches) > cycles:
        tranches[-2].extend(tranches.pop())
    return [tuple(tranche) for tranche in tranches]


def _cycle_record() -> dict[str, object]:
    return {
        "archive": {},
        "detections": [],
        "triage_verdicts": [],
        "curator": {"passes": [], "incident_verdicts": [], "cluster_verdicts": []},
        "proposals": [],
        "llm_run_log": [],
        "timings": {},
    }


def _drive_cycles(
    sandbox: _Sandbox,
    corpus: Path,
    manifest: Mapping[str, object],
    tranches: Sequence[Sequence[str]],
    config: EvalRunConfig,
    stages: tuple[str, ...],
    annotations: Sequence[_Annotation],
    record: dict[str, object],
    *,
    deterministic: bool,
) -> None:
    """Feed growing transcript tranches through one durable sandbox ledger."""

    all_ids: list[str] = []
    all_detections: list[object] = []
    all_triage: list[object] = []
    reports: list[dict[str, object]] = []
    seen_incident_ids: set[object] = set()
    seen_corpus_ids: set[object] = set()
    for index, tranche in enumerate(tranches):
        all_ids.extend(tranche)
        if index:
            # Updating the archived source re-enqueues only changed sessions;
            # scanner-level keys retain old incidents and admit only new ones.
            _fabricate_sandbox(sandbox, corpus, manifest, tuple(all_ids), config)
        cycle = _cycle_record()
        _drive_pipeline(sandbox, stages, annotations, cycle, deterministic=deterministic)
        detections = cycle["detections"]
        triage = cycle["triage_verdicts"]
        run_log = cycle["llm_run_log"]
        assert isinstance(detections, list) and isinstance(triage, list) and isinstance(run_log, list)
        duplicate_corpus_ids = [
            item.get("corpus_incident_id")
            for item in detections
            if isinstance(item, dict)
            and item.get("corpus_incident_id") is not None
            and item.get("corpus_incident_id") in seen_corpus_ids
        ]
        new_detections = [
            item
            for item in detections
            if isinstance(item, dict)
            and item.get("incident_id") not in seen_incident_ids
            and (
                item.get("corpus_incident_id") is None
                or item.get("corpus_incident_id") not in seen_corpus_ids
            )
        ]
        seen_incident_ids.update(
            item.get("incident_id") for item in new_detections if isinstance(item, dict)
        )
        seen_corpus_ids.update(
            item.get("corpus_incident_id")
            for item in new_detections
            if isinstance(item, dict) and item.get("corpus_incident_id") is not None
        )
        all_detections.extend(new_detections)
        all_triage.extend(triage)
        curator = cycle["curator"]
        assert isinstance(curator, dict)
        passes = curator.get("passes", [])
        curator_packs = sum(1 for item in passes if isinstance(item, dict))
        reports.append(
            {
                "cycle": index + 1,
                "tranche_incident_ids": list(tranche),
                "new_incidents": len(new_detections),
                "curator_packs": curator_packs,
                "idempotent": not duplicate_corpus_ids,
                "duplicate_corpus_incident_ids": duplicate_corpus_ids,
                "state_machine_integrity": all(
                    isinstance(item, dict)
                    and item.get("state") in {
                        "detected", "triaged", "dismissed-triage", "open", "promoted",
                        "parked", "dismissed-reviewed", "in-proposal", "remedied",
                    }
                    for item in triage
                ),
                # The record exposes the actual calls; an empty tranche must
                # never trigger curation, and a non-empty one has a finite,
                # input-bounded pack count rather than a corpus replay.
                "on_new_economics": (not new_detections and curator_packs == 0)
                or (bool(new_detections) and curator_packs <= len(new_detections)),
            }
        )
        record["archive"] = cycle["archive"]
        record["curator"] = cycle["curator"]
        record["proposals"] = cycle["proposals"]
        record["llm_run_log"] = cycle["llm_run_log"]
        record["timings"] = cycle["timings"]
    record["detections"] = all_detections
    record["triage_verdicts"] = all_triage
    record["cycles"] = reports


def _drive_pipeline(
    sandbox: _Sandbox,
    stages: tuple[str, ...],
    annotations: Sequence[_Annotation],
    record: dict[str, object],
    *,
    deterministic: bool,
) -> None:
    timings = record["timings"]
    assert isinstance(timings, dict)
    annotation_by_message = {item.message: item for item in annotations}

    with _timed(timings, "archive", deterministic=deterministic):
        output, errors = StringIO(), StringIO()
        claude_result = backfill(sandbox.projects_dir, output=output, error_output=errors)
        codex_results = [
            archive_transcript(path, source="codex")
            for path in codex.iter_sessions(sandbox.codex_home)
        ]
        record["archive"] = {
            "input_digest": _tree_digest(sandbox.projects_dir, sandbox.codex_home),
            "claude": asdict(claude_result),
            "codex": {
                "found": len(codex_results),
                "new": sum(item.status != "skipped" for item in codex_results),
                "skipped": sum(item.status == "skipped" for item in codex_results),
                "failed": 0,
            },
        }

    scan_results: list[ScanResult] = []
    if "scan" in stages:
        with _timed(timings, "scan", deterministic=deterministic):
            with Ledger() as ledger:
                scan_results = scan_pending_queue(ledger)
            detections: list[dict[str, object]] = []
            for result in scan_results:
                for detection in result.detections:
                    annotation = annotation_by_message.get(detection.message.message)
                    detections.append(
                        {
                            "incident_id": detection.incident_id,
                            "corpus_incident_id": annotation.corpus_id if annotation else None,
                            "source": "codex" if "codex" in result.transcript_path.parts else "claude-code",
                            "session_id": detection.message.session_id,
                            "message_digest": _digest(detection.message.message),
                            "matched_terms": [
                                {
                                    "term": hit.term,
                                    "category": hit.category,
                                    "group": hit.group,
                                    "count": hit.count,
                                }
                                for hit in detection.hits
                            ],
                        }
                    )
            record["detections"] = detections

    if "triage" in stages:
        with _timed(timings, "triage", deterministic=deterministic):
            with Ledger() as ledger:
                input_rows = [
                    {"incident_id": item.id, "message_digest": _digest(item.message)}
                    for item in ledger.untriaged_incidents()
                ]
                outcomes = triage_pending(ledger, assume_yes=True)
                verdicts = []
                for outcome in outcomes:
                    incident = ledger.get_incident(outcome.incident_id)
                    assert incident is not None
                    annotation = annotation_by_message.get(incident.message)
                    history = ledger.state_history(incident.id)
                    verdicts.append(
                        {
                            "incident_id": incident.id,
                            "corpus_incident_id": annotation.corpus_id if annotation else None,
                            "authentic": incident.state != "dismissed-triage",
                            "state": incident.state,
                            "label": incident.label,
                            "one_liner": incident.one_liner,
                            "severity": incident.severity,
                            "confidence": incident.confidence,
                            "reason": history[-1].reason,
                        }
                    )
                record["triage_verdicts"] = verdicts
                timings["triage"]["input_digest"] = _digest_json(input_rows)  # type: ignore[index]

    if "curate" in stages:
        with _timed(timings, "curate", deterministic=deterministic):
            passes: list[dict[str, object]] = []
            with Ledger() as ledger:
                while ledger.curator_unreviewed_incidents() or ledger.curator_qc_candidates():
                    pending_before = len(ledger.curator_unreviewed_incidents()) + len(
                        ledger.curator_qc_candidates()
                    )
                    digest = render_ledger_digest(ledger)
                    result = run_pass(ledger, assume_yes=True)
                    pending_after = len(ledger.curator_unreviewed_incidents()) + len(
                        ledger.curator_qc_candidates()
                    )
                    if pending_after >= pending_before and not (
                        result.applied_incident_verdicts or result.applied_cluster_verdicts
                    ):
                        raise EvalRunError(
                            "curator pass made no progress; aborting instead of looping "
                            f"(pending {pending_before} -> {pending_after})"
                        )
                    passes.append(
                        {
                            "ledger_digest": digest,
                            "ledger_digest_sha256": _digest(digest),
                            "calls": result.calls,
                            "full_context_incident_ids": list(result.full_context_incident_ids),
                            "applied_incident_verdicts": result.applied_incident_verdicts,
                            "applied_cluster_verdicts": result.applied_cluster_verdicts,
                            "rejected_verdicts": result.rejected_verdicts,
                            "skipped": result.skipped,
                        }
                    )
                    if result.skipped:
                        break
                curator = record["curator"]
                assert isinstance(curator, dict)
                curator["passes"] = passes
                curator["incident_verdicts"] = [
                    {
                        "incident_id": item.incident_id,
                        "verdict": item.verdict,
                        "reason": item.reason,
                        "previous_label": item.previous_label,
                        "reassign_label": item.reassign_label,
                        "singleton": item.singleton,
                    }
                    for item in ledger.curator_decisions()
                ]
                curator["cluster_verdicts"] = [
                    {"label": item.label, "verdict": item.verdict, "reason": item.reason}
                    for item in ledger.curator_cluster_decisions()
                ]
                timings["curate"]["input_digest"] = _digest_json(  # type: ignore[index]
                    [item["ledger_digest_sha256"] for item in passes]
                )

    if "synthesize" in stages:
        with _timed(timings, "synthesize", deterministic=deterministic):
            with Ledger() as ledger:
                promoted = [item.id for item in ledger.incidents_in_state("promoted")]
                synthesize_pending(
                    ledger,
                    assume_yes=True,
                    skills_dir=sandbox.skills_dir,
                    global_claude_md_path=sandbox.global_claude_md,
                    project_claude_md_paths=(),
                    surface_reference_root=sandbox.home,
                )
                rows = ledger.connection.execute("SELECT id FROM proposal ORDER BY id").fetchall()
                proposals = [ledger.get_proposal(int(row["id"])) for row in rows]
                record["proposals"] = [_proposal_record(item) for item in proposals if item]
                context_packs: dict[str, object] = {}
                for proposal in proposals:
                    if proposal is None:
                        continue
                    for incident_id in proposal.evidence_incident_ids:
                        incident = ledger.get_incident(incident_id)
                        if incident is None:
                            raise EvalRunError(f"proposal references missing incident {incident_id}")
                        pack, _ = context_for_incident(incident)
                        context_packs[str(incident_id)] = _context_pack_record(pack)
                record["context_packs"] = context_packs
                timings["synthesize"]["input_digest"] = _digest_json(promoted)  # type: ignore[index]

    _capture_run_log(record)


def _capture_run_log(record: dict[str, object]) -> None:
    """Snapshot all pipeline and judge calls after the active phase completes."""

    with Ledger() as ledger:
        rows = ledger.connection.execute("SELECT * FROM run_log ORDER BY id").fetchall()
        record["llm_run_log"] = [
            {
                "id": int(row["id"]),
                "stage": str(row["stage"]),
                "model": str(row["model"]),
                "transport": str(row["transport"]),
                "tokens": int(row["tokens"]) if row["tokens"] is not None else None,
                "cost_usd": float(row["cost_usd"]) if row["cost_usd"] is not None else None,
                "duration_ms": int(row["duration_ms"]),
                "input_digest": str(row["input_digest"]),
                "created_at": str(row["created_at"]),
            }
            for row in rows
        ]


@contextmanager
def _sandbox(
    corpus: Path,
    manifest: Mapping[str, object],
    selected_ids: Sequence[str] | None,
    config: EvalRunConfig,
    *,
    keep: bool,
) -> Iterator[_Sandbox]:
    if config.sandbox_root is None:
        root = Path(tempfile.mkdtemp(prefix="s2s-eval-"))
    else:
        root = Path(config.sandbox_root)
        root.mkdir(parents=True, exist_ok=False)
    home = root / "home"
    s2s_home = root / "s2s-home"
    projects = home / ".claude" / "projects"
    codex_home = home / ".codex"
    skills = home / ".claude" / "skills"
    global_md = home / ".claude" / "CLAUDE.md"
    sandbox = _Sandbox(root, home, s2s_home, projects, codex_home, skills, global_md)
    try:
        _fabricate_sandbox(sandbox, corpus, manifest, selected_ids, config)
        old_environment = {
            name: os.environ.get(name)
            for name in ("HOME", "S2S_HOME", "CLAUDE_CONFIG_DIR")
        }
        os.environ["HOME"] = str(home)
        os.environ["S2S_HOME"] = str(s2s_home)
        os.environ["CLAUDE_CONFIG_DIR"] = str(home / ".claude")
        # Binding Path.home as well as HOME closes the seam on platforms or tests
        # whose home resolver is cached/monkeypatched.
        with patch.object(Path, "home", lambda: home):
            yield sandbox
    finally:
        if "old_environment" in locals():
            for name, value in old_environment.items():
                _restore_env(name, value)
        if not keep:
            shutil.rmtree(root, ignore_errors=True)


def _fabricate_sandbox(
    sandbox: _Sandbox,
    corpus: Path,
    manifest: Mapping[str, object],
    selected_ids: Sequence[str] | None,
    config: EvalRunConfig,
) -> None:
    sandbox.projects_dir.mkdir(parents=True, exist_ok=True)
    sandbox.codex_home.joinpath("sessions").mkdir(parents=True, exist_ok=True)
    sandbox.skills_dir.mkdir(parents=True, exist_ok=True)
    sandbox.s2s_home.mkdir(parents=True, exist_ok=True)
    selected = set(selected_ids) if selected_ids is not None else None
    incidents = [item for item in manifest["incidents"] if isinstance(item, dict)]  # type: ignore[index]
    selected_sessions = {
        str(item["session"])
        for item in incidents
        if selected is None or str(item["uuid"]) in selected
    }
    for source in sorted((corpus / "claude").glob("*.jsonl")):
        session = f"claude/{source.name}"
        if session not in selected_sessions:
            continue
        project = source.stem.split("-", 1)[0]
        destination = sandbox.projects_dir / project / source.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        _copy_filtered_claude(source, destination, incidents, selected)
    codex_sessions = corpus / "codex" / "sessions"
    for source in sorted(codex_sessions.glob("*.jsonl")):
        session = f"codex/sessions/{source.name}"
        if session in selected_sessions:
            shutil.copy2(source, sandbox.codex_home / "sessions" / source.name)
    if selected is None and (corpus / "codex" / "history.jsonl").is_file():
        shutil.copy2(corpus / "codex" / "history.jsonl", sandbox.codex_home / "history.jsonl")

    fixture_rows = manifest.get("pre_existing_remedies", [])
    for fixture in fixture_rows if isinstance(fixture_rows, list) else []:
        if not isinstance(fixture, dict):
            continue
        source = corpus / str(fixture["path"])
        if fixture.get("shape") == "claude-md":
            content = source.read_text(encoding="utf-8").strip()
            sandbox.global_claude_md.write_text(
                f"<!-- s2s:begin -->\n{content}\n<!-- s2s:end -->\n", encoding="utf-8"
            )
        elif fixture.get("shape") == "skill":
            skill_dir = sandbox.skills_dir / str(fixture["id"])
            skill_dir.mkdir(parents=True, exist_ok=True)
            content = source.read_text(encoding="utf-8").strip()
            if not content.startswith("---\n"):
                content = (
                    f"---\nname: {fixture['id']}\n"
                    "description: Use when a completion claim requires verification.\n"
                    f"---\n{content}\n"
                )
            (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
    (sandbox.home / ".claude" / "settings.json").write_text("{}\n", encoding="utf-8")
    sandbox.s2s_home.joinpath("config.toml").write_text(
        "\n".join(
            (
                "[autonomy]",
                'mode = "review"',
                "[sources]",
                "codex = true",
                "[models]",
                f'triage = "{config.triage.model}"',
                f'curate = "{config.curate.model}"',
                f'synthesize = "{config.synthesize.model}"',
                _toml_optional_line("triage_effort", config.triage.effort),
                _toml_optional_line("curate_effort", config.curate.effort),
                _toml_optional_line("synthesize_effort", config.synthesize.effort),
                "parallelism = 1",
                "[prompts]",
                f'triage = {json.dumps(str(config.triage.prompt_version))}',
                f'curate = {json.dumps(str(config.curate.prompt_version))}',
                # Gardening is part of the curate pass but has its own prompt
                # lineage; pinning it to curate's version breaks the moment the
                # versions diverge (live finding: curate v2 exists, garden v2
                # does not, and the miss only fires when gardening runs).
                f'garden = {json.dumps(str(_config_module.Config().prompts.garden))}',
                f'synthesize = {json.dumps(str(config.synthesize.prompt_version))}',
                f'judge_remedy = {json.dumps(str(config.judge.prompt_version))}',
                f'judge_counterfactual = {json.dumps(str(config.judge.prompt_version))}',
                "",
            )
        ),
        encoding="utf-8",
    )


def _toml_optional_line(name: str, value: str | None) -> str:
    """Emit a TOML comment rather than an invalid null when an option is absent."""

    return f"{name} = {json.dumps(value)}" if value is not None else f"# {name} is unset"


def _copy_filtered_claude(
    source: Path,
    destination: Path,
    incidents: Sequence[Mapping[str, object]],
    selected: set[str] | None,
) -> None:
    if selected is None:
        shutil.copy2(source, destination)
        return
    annotated = {
        str(item["uuid"])
        for item in incidents
        if str(item.get("session")) == f"claude/{source.name}"
    }
    lines: list[str] = []
    for line in source.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            lines.append(line)
            continue
        if not isinstance(item, dict):
            lines.append(line)
            continue
        uuid = item.get("uuid") if isinstance(item, dict) else None
        if item.get("type") == "user" and uuid in annotated and uuid not in selected:
            continue
        lines.append(line)
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _load_manifest(corpus: Path) -> dict[str, object]:
    try:
        manifest = json.loads((corpus / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise EvalRunError(f"cannot load corpus manifest at {corpus}: {error}") from error
    if not isinstance(manifest, dict) or not isinstance(manifest.get("version"), str):
        raise EvalRunError("corpus manifest must be an object with a version")
    if manifest.get("synthetic") is not True or not isinstance(manifest.get("incidents"), list):
        raise EvalRunError("eval corpus must be explicitly synthetic and annotated")
    return manifest


def _load_annotations(
    corpus: Path, manifest: Mapping[str, object]
) -> tuple[_Annotation, ...]:
    messages: dict[tuple[str, str], tuple[str, str]] = {}
    sessions = {str(item["session"]) for item in manifest["incidents"] if isinstance(item, dict)}  # type: ignore[index]
    for session in sessions:
        path = corpus / session
        source = "codex" if session.startswith("codex/") else "claude-code"
        extracted = (
            codex.extract_user_messages(path)
            if source == "codex"
            else claude_code.extract_user_messages(path)
        )
        for item in extracted:
            if item.uuid:
                messages[(source, item.uuid)] = (item.message, item.project)
    annotations: list[_Annotation] = []
    for item in manifest["incidents"]:  # type: ignore[index]
        if not isinstance(item, dict):
            continue
        key = (str(item["source"]), str(item["uuid"]))
        if key not in messages:
            raise EvalRunError(f"manifest incident does not resolve through its adapter: {key}")
        message, project = messages[key]
        annotations.append(_Annotation(item, message, project))
    return tuple(annotations)


def _annotation_project(annotation: _Annotation) -> str:
    """Match the project identity the sandbox archive assigns to an annotation."""

    if annotation.data.get("source") == "claude-code":
        return Path(str(annotation.data["session"])).stem.split("-", 1)[0]
    return annotation.project


@contextmanager
def _timed(
    timings: dict[str, object], name: str, *, deterministic: bool
) -> Iterator[None]:
    started_at = utc_now_iso()
    started = perf_counter()
    entry: dict[str, object] = {"started_at": started_at}
    timings[name] = entry
    try:
        yield
    finally:
        entry["completed_at"] = utc_now_iso()
        entry["duration_ms"] = (
            0 if deterministic else round((perf_counter() - started) * 1000)
        )


def _proposal_record(proposal: Proposal) -> dict[str, object]:
    try:
        artifact = json.loads(proposal.drafted_content)
    except json.JSONDecodeError:
        artifact = proposal.drafted_content
    return {
        "proposal_id": proposal.id,
        "remedy_type": proposal.remedy_type,
        "evidence_incident_ids": list(proposal.evidence_incident_ids),
        "dedup_verdict": json.loads(proposal.dedup_verdict),
        "gate_status": proposal.gate_status,
        "revises": proposal.revises,
        "singleton": proposal.singleton,
        "proposal_kind": proposal.proposal_kind,
        "artifact": artifact,
    }


def _context_pack_record(pack: object) -> dict[str, object]:
    """Serialize the standard bounded pack so counterfactual judging is replayable."""

    for field in (
        "preceding_request",
        "agent_activity_digest",
        "frustrated_message",
        "following_exchange",
        "metadata",
    ):
        if not hasattr(pack, field):
            raise EvalRunError("context resolver returned an invalid context pack")
    metadata = getattr(pack, "metadata")
    return {
        "preceding_request": getattr(pack, "preceding_request"),
        "agent_activity_digest": getattr(pack, "agent_activity_digest"),
        "frustrated_message": getattr(pack, "frustrated_message"),
        "following_exchange": getattr(pack, "following_exchange"),
        "metadata": asdict(metadata),
    }


def _resolve_mode(configured: str, replay_dir: Path) -> str:
    if configured == "auto":
        return "replay" if any(replay_dir.glob("*.json")) else "mock"
    if configured not in {"mock", "replay", "live"}:
        raise EvalRunError(f"unknown eval mode {configured!r}")
    return configured


def _confirm_live_run() -> bool:
    """Require an affirmative trust-run decision even below normal cost threshold."""

    if not sys.stdin.isatty():
        print("Live eval requires confirmation; re-run with --yes to proceed.")
        return False
    return input("Run live eval with real Claude calls? [y/N] ").strip().lower() in {
        "y",
        "yes",
    }


def _replay_path(directory: Path, key: ReplayKey) -> Path:
    return directory / (
        f"{_safe_component(key.stage)}--{key.input_digest}--{_safe_component(key.model)}.json"
    )


def _safe_component(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-.")
    if not normalized:
        raise EvalRunError(f"unsafe empty path component from {value!r}")
    return normalized


def _between(value: str, start: str, end: str) -> str:
    try:
        return value.split(start, 1)[1].split(end, 1)[0].strip()
    except IndexError as error:
        raise EvalRunError(f"mock prompt lacks expected marker {start!r}") from error


def _tree_digest(*roots: Path) -> str:
    digest = sha256()
    for root in roots:
        for path in sorted(root.rglob("*")):
            if path.is_file():
                digest.update(str(path.relative_to(root)).encode())
                digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


def _digest(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()[:16]


def _digest_json(value: object) -> str:
    return _digest(_stable_json(value))


def _stable_json(value: object, *, indent: int | None = None) -> str:
    return json.dumps(value, ensure_ascii=False, indent=indent, sort_keys=True, separators=None if indent else (",", ":"))


def _write_record(path: Path, record: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(_stable_json(record, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _read_record(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):  # pragma: no cover - protected by _write_record
        raise EvalRunError(f"eval record is not an object: {path}")
    return value


def _repeat_snapshot(record: Mapping[str, object]) -> dict[str, object]:
    """Keep exactly the stochastic-stage facts the stability scorer compares."""

    return {
        "triage_verdicts": record.get("triage_verdicts", []),
        "curator": record.get("curator", {}),
        "llm_run_log": record.get("llm_run_log", []),
        "schema_validation": record.get("schema_validation", {}),
    }


def _run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def _restore_env(name: str, value: str | None) -> None:
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value


def _git_describe() -> str:
    """Read branch/commit identity without invoking git or mutating its metadata."""

    dotgit = PROJECT_ROOT / ".git"
    try:
        if dotgit.is_file():
            pointer = dotgit.read_text(encoding="utf-8").strip()
            gitdir = (PROJECT_ROOT / pointer.removeprefix("gitdir: ")).resolve()
        else:
            gitdir = dotgit
        head = (gitdir / "HEAD").read_text(encoding="utf-8").strip()
        if head.startswith("ref: "):
            ref = head[5:]
            ref_path = gitdir / ref
            commit = ref_path.read_text(encoding="utf-8").strip() if ref_path.is_file() else ""
            if not commit:
                packed = gitdir / "packed-refs"
                if packed.is_file():
                    for line in packed.read_text(encoding="utf-8").splitlines():
                        if line.endswith(f" {ref}"):
                            commit = line.split(" ", 1)[0]
                            break
            return f"{ref.removeprefix('refs/heads/')}@{commit[:12] or 'unknown'}"
        return head[:12]
    except OSError:
        return "unknown"
