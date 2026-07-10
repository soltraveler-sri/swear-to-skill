from __future__ import annotations

import json
from pathlib import Path

import pytest

from s2s import evalrun
from s2s.evalscore import EvalScoreError, load_thresholds, score_files, score_record, validate_evidence_pointers


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


def _subset_record(manifest: dict[str, object], fed_ids: list[str]) -> dict[str, object]:
    incidents = {str(item["uuid"]): item for item in manifest["incidents"]}  # type: ignore[index]
    detections = []
    triage = []
    context_packs: dict[str, object] = {}
    projects = {"c01-u1": "aurora", "c02-u1": "aurora", "c03-u1": "aurora", "c04-u1": "cinder", "c05-u1": "cinder"}
    for incident_id, corpus_id in enumerate(fed_ids, start=1):
        truth = incidents[corpus_id]
        if truth["lexicon_hit_expected"] is not True:
            continue
        detections.append({"incident_id": incident_id, "corpus_incident_id": corpus_id})
        triage.append(
            {
                "incident_id": incident_id,
                "corpus_incident_id": corpus_id,
                "authentic": truth["authentic"],
                "label": truth["cluster_id"] if truth["cluster_id"] not in {"singleton", "decoy"} else "other",
            }
        )
        if corpus_id in projects:
            context_packs[str(incident_id)] = {"metadata": {"project": projects[corpus_id]}}
    return {
        "mode": "replay",
        "fed_annotations": fed_ids,
        "fed_annotation_projects": {
            corpus_id: projects[corpus_id] for corpus_id in fed_ids if corpus_id in projects
        },
        "detections": detections,
        "triage_verdicts": triage,
        "context_packs": context_packs,
        "curator": {"cluster_verdicts": [{"label": "ignored-instruction", "verdict": "synthesize"}]},
        "proposals": [],
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


def test_quick_subset_scores_only_fed_annotations() -> None:
    manifest = _manifest()
    record = _subset_record(manifest, list(evalrun.quick_subset(manifest)))
    metrics = {item["metric"]: item for item in score_record(record, manifest)["metrics"]}

    assert metrics["detection_recall"].get("found") == metrics["detection_recall"].get("expected") == 7
    assert metrics["lexicon_gap"]["value"] == 0.125
    assert metrics["convergence"]["value"] == 1.0
    assert metrics["recurrence_fast_track_recall"]["value"] == 1.0
    assert metrics["dedup_catch_rate"]["expected"] == 0
    assert metrics["dedup_catch_rate"]["excluded"] is True
    assert metrics["dedup_catch_rate"]["note"] == "no near-duplicate annotations were fed"


def test_small_sample_flags_preserve_the_numeric_verdict() -> None:
    manifest = _manifest()
    metrics = {
        item["metric"]: item
        for item in score_record(_subset_record(manifest, list(evalrun.quick_subset(manifest))), manifest)["metrics"]
    }

    assert metrics["exact_label_agreement"]["flag"] == "small-sample"
    assert metrics["exact_label_agreement"]["pass"] is True
    assert metrics["recurrence_fast_track_recall"]["flag"] == "small-sample"
    assert metrics["recurrence_fast_track_recall"]["pass"] is True


def test_dedup_with_no_fed_cases_is_excluded_from_the_gate() -> None:
    manifest = _manifest()
    limits = {
        **load_thresholds(),
        "lexicon_gap": 1.0,
        "singleton_ratio_max": 1.0,
        "other_share_max": 1.0,
        "claude_md_share": 0.0,
    }
    scores = score_record(_subset_record(manifest, list(evalrun.quick_subset(manifest))), manifest, thresholds=limits)
    dedup = next(item for item in scores["metrics"] if item["metric"] == "dedup_catch_rate")

    assert dedup["pass"] is False
    assert dedup["excluded"] is True
    assert scores["status"] == "pass"


def test_label_agreement_uses_labelled_members_not_all_fed_members() -> None:
    manifest = _manifest()
    record = _subset_record(manifest, list(evalrun.quick_subset(manifest)))
    for verdict in record["triage_verdicts"]:  # type: ignore[index]
        if verdict["corpus_incident_id"] == "c05-u1":
            verdict["label"] = "scope-deviation"
    metrics = {item["metric"]: item for item in score_record(record, manifest)["metrics"]}

    assert metrics["label_agreement"]["value"] == 0.75
    assert metrics["exact_label_agreement"]["value"] == 1.0


def test_clean_post_curation_unification_converges_despite_low_label_share() -> None:
    manifest = _manifest()
    record = _subset_record(manifest, ["c01-u1", "c03-u1", "c04-u1"])
    for label, verdict in zip(("alpha", "beta", "gamma"), record["triage_verdicts"], strict=True):  # type: ignore[index]
        verdict["label"] = label
    record["proposals"] = [{"proposal_id": 7, "evidence_incident_ids": [1, 2, 3]}]

    convergence = next(item for item in score_record(record, manifest)["metrics"] if item["metric"] == "convergence")
    cluster = convergence["clusters"][0]

    assert cluster["triage_label_share"] == 1 / 3
    assert cluster["post_curation_unified"] is True
    assert cluster["post_curation_clean"] is True
    assert cluster["converged"] is True


def test_contaminated_post_curation_unification_does_not_converge() -> None:
    manifest = _manifest()
    record = _subset_record(manifest, ["c01-u1", "c03-u1", "c04-u1", "c03-u2"])
    for label, verdict in zip(("alpha", "beta", "gamma"), record["triage_verdicts"][:3], strict=True):  # type: ignore[index]
        verdict["label"] = label
    record["proposals"] = [{"proposal_id": 8, "evidence_incident_ids": [1, 2, 3, 4]}]

    convergence = next(item for item in score_record(record, manifest)["metrics"] if item["metric"] == "convergence")
    cluster = convergence["clusters"][0]

    assert cluster["post_curation_unified"] is True
    assert cluster["post_curation_clean"] is False
    assert cluster["contaminating_incident_ids"] == [4]
    assert cluster["converged"] is False


def test_spam_precision_is_graded_by_clean_citations() -> None:
    manifest = _manifest()
    record = _subset_record(manifest, ["c01-u1", "c03-u1", "c03-u2"])
    record["proposals"] = [{"proposal_id": 9, "evidence_incident_ids": [1, 2, 3]}]

    metrics = {item["metric"]: item for item in score_record(record, manifest)["metrics"]}

    assert metrics["spam_clean"]["value"] == 0.0
    assert metrics["spam_clean"]["pass"] is False
    assert metrics["spam_precision"]["value"] == 0.666667
    assert metrics["spam_precision"]["clean_citations"] == 2
    assert metrics["spam_precision"]["citations"] == 3
    assert metrics["spam_precision"]["clean_proposals"] == 0
    assert metrics["spam_precision"]["proposals"] == 1


def test_subset_excludes_one_member_clusters_with_traceable_note() -> None:
    manifest = _manifest()
    record = _subset_record(manifest, ["c01-u1", "c06-u1", "c02-u2"])
    convergence = next(item for item in score_record(record, manifest)["metrics"] if item["metric"] == "convergence")

    assert convergence["clusters"] == []
    assert convergence["excluded_clusters"] == [
        {"cluster": "ignored-instruction", "fed_members": 1},
        {"cluster": "premature-completion-claim", "fed_members": 1},
    ]
    assert convergence["flag"] == "small-sample"
    assert any(pointer.startswith("manifest:/incidents/") for pointer in convergence["evidence"])


def test_full_corpus_provenance_preserves_pinned_scores() -> None:
    manifest = _manifest()
    record = _minimal_record(manifest)
    baseline = score_record(record, manifest)
    record["fed_annotations"] = [item["uuid"] for item in manifest["incidents"]]  # type: ignore[index]

    assert score_record(record, manifest) == baseline
    metrics = {item["metric"]: item["value"] for item in baseline["metrics"]}
    assert metrics == {
        "detection_recall": 0.277778,
        "lexicon_gap": 0.052632,
        "scaffold_filter_precision": 1.0,
        "authenticity_precision": 1.0,
        "authenticity_recall": 1.0,
        "label_agreement": 0.36,
        "exact_label_agreement": 0.4,
        "convergence": 0.4,
        "singleton_ratio": 0.333333,
        "recurrence_fast_track_recall": 0.4,
        "other_share": 0.1,
        "spam_clean": 1.0,
        "spam_precision": 1.0,
        "remedies_per_authentic_incident": 0.074074,
        "dedup_catch_rate": 1.0,
        "claude_md_dominance": 1.0,
        "stability_label_flip_rate": 0.0,
        "stability_authenticity_flip_rate": 0.0,
        "stability_schema_retry_rate": 0.0,
        "pipeline_invariants": 1.0,
    }


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
