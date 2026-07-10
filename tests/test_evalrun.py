from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
from typing import Mapping

import pytest

from s2s import evalrun, llm
from s2s.cli import build_parser


CORPUS = Path(__file__).parents[1] / "evals" / "corpus" / "v1"


def _config(tmp_path: Path, **changes: object) -> evalrun.EvalRunConfig:
    values: dict[str, object] = {
        "corpus": CORPUS,
        "mode": "mock",
        "replay_root": tmp_path / "replays",
        "output_root": tmp_path / "runs",
    }
    values.update(changes)
    return evalrun.EvalRunConfig(**values)  # type: ignore[arg-type]


def _record(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _without_timestamps(value: object) -> object:
    if isinstance(value, list):
        return [_without_timestamps(item) for item in value]
    if isinstance(value, dict):
        return {
            key: _without_timestamps(item)
            for key, item in value.items()
            if key not in {"started_at", "completed_at", "created_at"}
        }
    return value


def _mock_provider() -> evalrun.MockProvider:
    manifest = evalrun._load_manifest(CORPUS)
    return evalrun.MockProvider(evalrun._load_annotations(CORPUS, manifest))


def test_cli_exposes_all_eval_controls() -> None:
    args = build_parser().parse_args(
        [
            "eval",
            "--mode",
            "live",
            "--record",
            "--yes",
            "--quick",
            "--stages",
            "scan,triage",
            "--keep",
            "--repeat",
            "3",
            "--corpus",
            str(CORPUS),
        ]
    )
    assert (args.mode, args.record, args.yes, args.quick, args.stages, args.keep, args.repeat) == (
        "live",
        True,
        True,
        True,
        "scan,triage",
        True,
        3,
    )
    assert args.corpus == CORPUS


def test_sandbox_canary_and_child_environment_audit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    real_home = tmp_path / "real-home-shaped"
    sentinel = real_home / ".claude" / "sentinel.txt"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_text("do not touch", encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: real_home)
    observed: list[tuple[str, str, Path]] = []
    canned = _mock_provider()

    def audit_provider(request: llm.LLMRequest) -> Mapping[str, object]:
        observed.append(
            (request.env["HOME"], request.env["S2S_HOME"], Path.home())
        )
        return canned(request)

    result = evalrun.run_eval(
        _config(
            tmp_path,
            mode="live",
            assume_yes=True,
            stages=("scan", "triage"),
        ),
        live_provider=audit_provider,
    )

    assert result.triaged == 36 and observed
    assert all(home == str(path_home) for home, _, path_home in observed)
    assert all(s2s_home.startswith(home.removesuffix("/home")) for home, s2s_home, _ in observed)
    assert all(home != str(real_home) for home, _, _ in observed)
    assert sentinel.read_text(encoding="utf-8") == "do not touch"
    assert not (real_home / ".s2s").exists()


def test_record_then_two_replays_are_deterministic_modulo_timestamps(tmp_path: Path) -> None:
    base = _config(
        tmp_path,
        mode="live",
        record=True,
        assume_yes=True,
    )
    recorded = evalrun.run_eval(base, live_provider=_mock_provider())
    replay_one = evalrun.run_eval(
        replace(base, mode="replay", record=False, output_root=tmp_path / "replay-one")
    )
    replay_two = evalrun.run_eval(
        replace(base, mode="replay", record=False, output_root=tmp_path / "replay-two")
    )

    assert recorded.proposals == replay_one.proposals == replay_two.proposals == 6
    # Two identical gardening requests intentionally share one content-addressed key.
    assert len(list((tmp_path / "replays" / "v1" / "default").glob("*.json"))) == 83
    assert _without_timestamps(_record(replay_one.record_path)) == _without_timestamps(
        _record(replay_two.record_path)
    )


def test_replay_cache_miss_is_typed_actionable_and_never_calls_live(tmp_path: Path) -> None:
    called = False

    def forbidden(_: llm.LLMRequest) -> Mapping[str, object]:
        nonlocal called
        called = True
        raise AssertionError("replay must never fall back to live")

    with pytest.raises(evalrun.ReplayCacheMissError) as error:
        evalrun.run_eval(
            _config(tmp_path, mode="replay", stages=("scan", "triage")),
            live_provider=forbidden,
        )

    assert called is False
    assert "(triage," in str(error.value)
    assert evalrun.REPLAY_COMMAND in str(error.value)


def test_live_mode_requires_per_run_confirmation_or_yes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class NonInteractive:
        def isatty(self) -> bool:
            return False

    monkeypatch.setattr(evalrun.sys, "stdin", NonInteractive())
    with pytest.raises(evalrun.EvalConfirmationDeclined, match="not confirmed"):
        evalrun.run_eval(
            _config(tmp_path, mode="live", stages=("scan", "triage")),
            live_provider=lambda request: pytest.fail("unconfirmed live transport ran"),
        )

    assert not (tmp_path / "runs").exists()


def test_partial_stages_stop_before_curator_and_synthesist(tmp_path: Path) -> None:
    result = evalrun.run_eval(
        _config(tmp_path, stages=evalrun.parse_stages("scan,triage"))
    )
    record = _record(result.record_path)

    assert result.detections == result.triaged == 36
    assert record["stages_requested"] == ["scan", "triage"]
    assert record["curator"] == {
        "passes": [],
        "incident_verdicts": [],
        "cluster_verdicts": [],
    }
    assert record["proposals"] == []
    assert len(record["llm_run_log"]) == 36  # type: ignore[arg-type]
    assert set(record["timings"]) == {"archive", "scan", "triage"}  # type: ignore[arg-type]


def test_keep_preserves_and_default_teardown_removes_sandbox(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    created = iter((tmp_path / "removed", tmp_path / "preserved"))

    def fake_mkdtemp(*, prefix: str) -> str:
        path = next(created)
        path.mkdir()
        return str(path)

    monkeypatch.setattr(evalrun.tempfile, "mkdtemp", fake_mkdtemp)
    removed = evalrun.run_eval(_config(tmp_path, stages=("scan",)))
    kept = evalrun.run_eval(
        _config(tmp_path, stages=("scan",), keep=True, output_root=tmp_path / "kept-runs")
    )

    assert removed.sandbox_path is None and not (tmp_path / "removed").exists()
    assert kept.sandbox_path == tmp_path / "preserved"
    assert (tmp_path / "preserved" / "home" / ".claude" / "projects").is_dir()


def test_estimate_math_and_quick_subset_are_stable() -> None:
    manifest = evalrun._load_manifest(CORPUS)
    first = evalrun.quick_subset(manifest)
    second = evalrun.quick_subset(manifest)

    assert first == second == (
        "c01-u1",
        "c02-u1",
        "c03-u1",
        "c04-u1",
        "c05-u1",
        "c02-u2",
        "c03-u2",
        "c05-u2",
    )
    assert evalrun.estimate_call_counts(manifest) == evalrun.EvalCallEstimate(36, 4, 7, 38)
    assert evalrun.estimate_call_counts(manifest, first) == evalrun.EvalCallEstimate(7, 1, 2, 13)


def test_quick_run_copies_only_the_documented_subset(tmp_path: Path) -> None:
    result = evalrun.run_eval(_config(tmp_path, quick=True, stages=("scan", "triage")))
    record = _record(result.record_path)

    assert result.detections == result.triaged == 7
    assert {item["corpus_incident_id"] for item in record["detections"]} == {  # type: ignore[index]
        "c01-u1",
        "c03-u1",
        "c04-u1",
        "c05-u1",
        "c02-u2",
        "c03-u2",
        "c05-u2",
    }
    assert record["fed_annotations"] == list(evalrun.quick_subset(evalrun._load_manifest(CORPUS)))
    assert record["fed_annotation_projects"]["c02-u1"] == "aurora"


def test_eval_stage_models_are_honored_and_record_is_scorer_ready(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        triage=evalrun.EvalStageConfig("custom-haiku", effort="reserved", prompt_version=1),
        curate=evalrun.EvalStageConfig("custom-sonnet"),
        synthesize=evalrun.EvalStageConfig("custom-synth-sonnet"),
    )
    result = evalrun.run_eval(config)
    record = _record(result.record_path)
    models = {(item["stage"], item["model"]) for item in record["llm_run_log"]}  # type: ignore[index]

    assert models == {
        ("triage", "custom-haiku"),
        ("curate", "custom-sonnet"),
        ("synthesize", "custom-synth-sonnet"),
    }
    assert record["stage_config"] == {
        "triage": {"model": "custom-haiku", "effort": "reserved", "prompt_version": 1},
        "curate": {"model": "custom-sonnet", "effort": None, "prompt_version": 1},
        "synthesize": {"model": "custom-synth-sonnet", "effort": None, "prompt_version": 1},
        "judge": {"model": "sonnet", "effort": None, "prompt_version": 1},
    }
    assert set(record) >= {
        "corpus_version",
        "mode",
        "profile",
        "git_describe",
        "archive",
        "detections",
        "triage_verdicts",
        "curator",
        "proposals",
        "llm_run_log",
        "timings",
    }
    assert all("artifact" in item for item in record["proposals"])  # type: ignore[union-attr]


def test_auto_mode_prefers_mock_until_replays_exist(tmp_path: Path) -> None:
    replay_dir = tmp_path / "replays" / "v1" / "default"
    assert evalrun._resolve_mode("auto", replay_dir) == "mock"
    replay_dir.mkdir(parents=True)
    (replay_dir / "one.json").write_text("{}", encoding="utf-8")
    assert evalrun._resolve_mode("auto", replay_dir) == "replay"


def test_repeat_and_tranche_cycles_emit_scorer_inputs(tmp_path: Path) -> None:
    result = evalrun.run_eval(_config(tmp_path, repeat=2, cycles=3))
    record = _record(result.record_path)

    assert len(record["repeats"]) == 2  # type: ignore[arg-type]
    assert len(record["cycles"]) == 3  # type: ignore[arg-type]
    assert len({item["corpus_incident_id"] for item in record["detections"]}) == 36  # type: ignore[index]
    assert all(item["idempotent"] for item in record["cycles"])  # type: ignore[index]
    assert all(item["state_machine_integrity"] for item in record["cycles"])  # type: ignore[index]
    assert all(item["on_new_economics"] for item in record["cycles"])  # type: ignore[index]
