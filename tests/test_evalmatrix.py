from __future__ import annotations

from pathlib import Path
from typing import Mapping

import pytest

from s2s import evalrun, llm


CORPUS = Path(__file__).parents[1] / "evals" / "corpus" / "v1"


def _profile(name: str, *, triage_version: int | str = 1, synth_model: str = "sonnet") -> evalrun.EvalProfile:
    stage = evalrun.EvalStageConfig
    return evalrun.EvalProfile(
        name,
        stage("haiku", prompt_version=triage_version),
        stage("sonnet"),
        stage(synth_model),
        stage("sonnet"),
        Path(f"{name}.toml"),
    )


def _provider() -> evalrun.MockProvider:
    manifest = evalrun._load_manifest(CORPUS)
    return evalrun.MockProvider(evalrun._load_annotations(CORPUS, manifest))


def test_profile_rejects_unknown_controls_and_stage_keys(tmp_path: Path) -> None:
    (tmp_path / "bad.toml").write_text("[thresholds]\nanything = 1\n", encoding="utf-8")
    with pytest.raises(evalrun.EvalRunError, match="forbidden"):
        evalrun.load_profile("bad", root=tmp_path)
    (tmp_path / "bad.toml").write_text(
        "[triage]\nmodel='haiku'\nprompt_version='v1'\nunknown=true\n"
        "[curate]\nmodel='sonnet'\nprompt_version='v1'\n"
        "[synthesize]\nmodel='sonnet'\nprompt_version='v1'\n"
        "[judge]\nmodel='sonnet'\nprompt_version='v1'\n",
        encoding="utf-8",
    )
    with pytest.raises(evalrun.EvalRunError, match="unknown key"):
        evalrun.load_profile("bad", root=tmp_path)


def test_matrix_pins_v2_prompt_and_writes_comparative_artifacts(tmp_path: Path) -> None:
    prompts: list[str] = []
    canned = _provider()

    def recorder(request: llm.LLMRequest) -> Mapping[str, object]:
        prompts.append(request.prompt)
        return canned(request)

    result = evalrun.run_matrix(
        evalrun.EvalRunConfig(corpus=CORPUS, mode="live", assume_yes=True, output_root=tmp_path),
        (_profile("baseline", triage_version="v2"), _profile("v1-arm", triage_version="v1", synth_model="opus")),
        live_provider=recorder,
    )
    markdown = result.report_markdown_path.read_text(encoding="utf-8")
    triage_prompts = [prompt for prompt in prompts if "one_liner" in prompt]
    # Version threading proven with the real prompt files: the default (v2)
    # carries the authenticity checklist; the arm pinned to v1 must not.
    assert any("AUTHENTICITY CHECKLIST" in prompt for prompt in triage_prompts)
    assert any("AUTHENTICITY CHECKLIST" not in prompt for prompt in triage_prompts)
    assert result.report_html_path.is_file()
    assert "| detection_recall |" in markdown
    assert "Overfitting hazard" in markdown
    assert "synthesize=opus" in markdown
    assert all(result.root in arm.record_path.parents for arm in result.arms)
    assert all(arm.sandbox_path is not None and result.root in arm.sandbox_path.parents for arm in result.arms)


def test_matrix_live_estimate_is_the_sum_of_each_arm(tmp_path: Path) -> None:
    config = evalrun.EvalRunConfig(corpus=CORPUS, mode="live", output_root=tmp_path)
    profiles = (_profile("one"), _profile("two", synth_model="opus"))
    counts = evalrun.estimate_call_counts(evalrun._load_manifest(CORPUS))
    expected = sum(
        count * llm.ESTIMATED_CALL_COST_USD.get(model, llm.ESTIMATED_CALL_COST_USD["sonnet"])
        for count, model in (
            (counts.triage, "haiku"), (counts.curate, "sonnet"), (counts.synthesize, "sonnet"), (counts.judge, "sonnet"),
            (counts.triage, "haiku"), (counts.curate, "sonnet"), (counts.synthesize, "opus"), (counts.judge, "sonnet"),
        )
    )
    assert evalrun._matrix_live_estimate(config, profiles) == expected


def test_matrix_replay_miss_names_the_profile_fill_command(tmp_path: Path) -> None:
    with pytest.raises(evalrun.ReplayCacheMissError, match=r"--matrix baseline"):
        evalrun.run_matrix(
            evalrun.EvalRunConfig(corpus=CORPUS, mode="replay", output_root=tmp_path),
            (_profile("baseline"), _profile("other")),
        )
