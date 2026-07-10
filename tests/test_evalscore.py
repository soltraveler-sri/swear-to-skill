from __future__ import annotations

import json
from pathlib import Path

import pytest

from s2s import evalrun
from s2s.evalscore import EvalScoreError, score_files, score_record, validate_evidence_pointers


CORPUS = Path(__file__).parents[1] / "evals" / "corpus" / "v1"


def _manifest() -> dict[str, object]:
    return json.loads((CORPUS / "manifest.json").read_text(encoding="utf-8"))


def _minimal_record(manifest: dict[str, object]) -> dict[str, object]:
    incidents = manifest["incidents"]
    assert isinstance(incidents, list)
    selected = [item for item in incidents if item["uuid"] in {"c01-u1", "c03-u1", "c04-u1", "c05-u1", "c06-u1", "c07-u1", "c08-u1", "codex-d01:1", "codex-d02:1", "c03-u2"}]
    detections = []
    triage = []
    for index, item in enumerate(selected, start=1):
        detections.append({"incident_id": index, "corpus_incident_id": item["uuid"]})
        triage.append({"incident_id": index, "corpus_incident_id": item["uuid"], "authentic": item["authentic"], "label": item["cluster_id"] if item["cluster_id"] not in {"decoy", "singleton"} else "other"})
    return {
        "mode": "replay",
        "detections": detections,
        "triage_verdicts": triage,
        "curator": {"cluster_verdicts": [{"label": "ignored-instruction", "verdict": "synthesize"}, {"label": "premature-completion-claim", "verdict": "synthesize"}]},
        "proposals": [{"proposal_id": 1, "remedy_type": "claude-md", "evidence_incident_ids": [1, 2, 3, 4], "dedup_verdict": []}, {"proposal_id": 2, "remedy_type": "claude-md", "evidence_incident_ids": [5, 6, 7, 8, 9], "dedup_verdict": [{"verdict": "clear"}]}],
        "llm_run_log": [],
    }


def test_hand_built_record_has_exact_deterministic_scores() -> None:
    scores = score_record(_minimal_record(_manifest()), _manifest())
    metrics = {item["metric"]: item for item in scores["metrics"]}

    assert metrics["detection_recall"]["value"] == 0.277778
    assert metrics["scaffold_filter_precision"]["value"] == 1.0
    assert metrics["authenticity_precision"]["value"] == 1.0
    assert metrics["authenticity_recall"]["value"] == 1.0
    assert metrics["convergence"]["clusters"]
    assert all("scattered_members" in item for item in metrics["convergence"]["clusters"])


def test_threshold_failure_and_evidence_integrity() -> None:
    manifest = _manifest()
    record = _minimal_record(manifest)
    scores = score_record(record, manifest, thresholds={**__import__("s2s.evalscore", fromlist=["load_thresholds"]).load_thresholds(), "detection_recall": 1.0})
    detection = next(item for item in scores["metrics"] if item["metric"] == "detection_recall")
    assert detection["pass"] is False
    validate_evidence_pointers({"record": record, "manifest": manifest}, scores["metrics"])
    broken = dict(detection)
    broken["evidence"] = ["record:/does-not-exist"]
    with pytest.raises(EvalScoreError, match="does not resolve"):
        validate_evidence_pointers({"record": record, "manifest": manifest}, [broken])


def test_mock_run_writes_withheld_scores(tmp_path: Path) -> None:
    result = evalrun.run_eval(
        evalrun.EvalRunConfig(corpus=CORPUS, mode="mock", output_root=tmp_path / "runs")
    )
    scores = score_files(result.record_path, CORPUS / "manifest.json")

    assert scores == {
        "format_version": 1,
        "status": "withheld",
        "summary": "mode=mock: scores withheld",
        "metrics": [],
        "judge": {"status": "pending", "metrics": []},
    }
    assert result.record_path.with_name("scores.json").is_file()
