from __future__ import annotations

from s2s.config import load_config
from s2s.paths import resolve_paths


def test_paths_are_fully_scoped_to_s2s_home(monkeypatch, tmp_path) -> None:
    home = tmp_path / "s2s-state"
    monkeypatch.setenv("S2S_HOME", str(home))

    paths = resolve_paths()

    assert paths.home == home
    assert paths.archive_dir == home / "archive"
    assert paths.ledger_path == home / "ledger.db"
    assert paths.state_dir == home / "state"
    assert paths.config_path == home / "config.toml"
    assert paths.status_file == home / "status.txt"
    assert not home.exists()


def test_missing_config_returns_documented_defaults(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("S2S_HOME", str(tmp_path))

    config = load_config()

    assert config.thresholds.triage_untriaged_count == 10
    assert config.thresholds.triage_max_age_hours == 24
    assert config.thresholds.triage_per_run_cap == 25
    assert config.thresholds.curator_unreviewed_count == 10
    assert config.thresholds.curator_max_age_days == 7
    assert config.autonomy.mode == "review"
    assert config.autonomy.max_auto_remedies_per_week == 3
    assert config.autonomy.max_active_auto_skills == 15
    assert config.notifications.session_start_digest is True
    assert config.notifications.desktop is False
    assert config.notifications.webhook_url == ""
    assert config.notifications.events == ("proposal_pending", "autonomous_action")
    assert config.models.triage == "haiku"
    assert config.models.curate == "sonnet"
    assert config.models.synthesize == "sonnet"
    assert config.models.parallelism == 2
    assert config.prompts.curate == "v2"
    assert config.prompts.synthesize == "v2"
    assert config.costs.confirm_threshold_usd == 1.0
    assert config.curator.qc_sample_size == 5
    assert config.curator.context_char_budget == 60_000


def test_config_is_loaded_from_the_overridden_s2s_home(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("S2S_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text(
        "[thresholds]\ncurator_unreviewed_count = 4\n"
        "[autonomy]\nmode = 'autonomous'\n"
        "[notifications]\ndesktop = true\n"
        "[models]\ntriage = 'custom-haiku'\nparallelism = 3\n"
        "[costs]\nconfirm_threshold_usd = 2.5\n"
    )

    config = load_config()

    assert config.thresholds.curator_unreviewed_count == 4
    assert config.thresholds.triage_per_run_cap == 25
    assert config.autonomy.mode == "autonomous"
    assert config.notifications.desktop is True
    assert config.models.triage == "custom-haiku"
    assert config.models.curate == "sonnet"
    assert config.models.parallelism == 3
    assert config.costs.confirm_threshold_usd == 2.5
