"""TOML-backed configuration with North Star defaults."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import tomllib

from .paths import resolve_paths


@dataclass(frozen=True)
class Thresholds:
    """Queue-pump limits from North Star §9.1."""

    triage_untriaged_count: int = 10
    triage_max_age_hours: int = 24
    triage_per_run_cap: int = 25
    curator_unreviewed_count: int = 10
    curator_max_age_days: int = 7


@dataclass(frozen=True)
class Autonomy:
    """Default human gate and autonomous-install caps from North Star §8."""

    mode: str = "review"
    max_auto_remedies_per_week: int = 3
    max_active_auto_skills: int = 15
    claude_md_confidence_bar: float = 0.8
    skill_confidence_bar: float = 0.9


@dataclass(frozen=True)
class AutonomyState:
    """Effective Gate policy after the local kill-switch override is applied."""

    mode: str
    source: str
    paused_reason: str | None = None
    updated_at: str | None = None

    @property
    def enabled(self) -> bool:
        return self.mode == "autonomous"

    @property
    def paused(self) -> bool:
        return self.mode == "paused"


@dataclass(frozen=True)
class Notifications:
    """Layered notification defaults from North Star §9.2."""

    session_start_digest: bool = True
    desktop: bool = False
    webhook_url: str = ""
    events: tuple[str, ...] = ("proposal_pending", "autonomous_action")


@dataclass(frozen=True)
class Models:
    """LLM policy defaults from North Star §§4 and 7."""

    # Measured on the golden corpus: Sonnet is safer for remedy evidence;
    # Haiku remains the frugal option.
    triage: str = "sonnet"
    curate: str = "sonnet"
    synthesize: str = "sonnet"
    parallelism: int = 2
    triage_effort: str | None = None
    curate_effort: str | None = None
    synthesize_effort: str | None = None


@dataclass(frozen=True)
class Prompts:
    """Published prompt versions used by each judgment surface."""

    triage: str = "v2"
    curate: str = "v2"
    garden: str = "v1"
    synthesize: str = "v2"
    judge_remedy: str = "v1"
    judge_counterfactual: str = "v1"


@dataclass(frozen=True)
class Costs:
    """User-confirmation policy for bulk LLM work."""

    confirm_threshold_usd: float = 1.0


@dataclass(frozen=True)
class Curator:
    """Curator evidence sampling and per-call context governor."""

    qc_sample_size: int = 5
    context_char_budget: int = 60_000


@dataclass(frozen=True)
class Sources:
    """Transcript sources enabled for scheduled and historical scans."""

    codex: bool = False


@dataclass(frozen=True)
class Auditor:
    """Evidence thresholds for deterministic remedy outcome measurement."""

    min_post_install_sessions: int = 20
    min_post_install_days: int = 30
    min_usage_observation_days: int = 30
    min_sessions_scanned: int = 20
    silent_days: int = 90
    meaningful_drop_fraction: float = 0.20


@dataclass(frozen=True)
class Visibility:
    """Human-facing catalog controls from North Star §15.7."""

    library: bool = True
    attribution: bool = True


@dataclass(frozen=True)
class EvalSettings:
    """Eval transport default; ``auto`` prefers replay only when cache exists."""

    mode: str = "auto"
    judge: EvalJudgeSettings = field(default_factory=lambda: EvalJudgeSettings())


@dataclass(frozen=True)
class EvalJudgeSettings:
    """Independent model arm for eval-only remedy judging."""

    model: str = "sonnet"
    effort: str | None = None


@dataclass(frozen=True)
class Config:
    """Complete configuration available before any optional features exist."""

    thresholds: Thresholds = Thresholds()
    autonomy: Autonomy = Autonomy()
    notifications: Notifications = Notifications()
    models: Models = Models()
    prompts: Prompts = Prompts()
    costs: Costs = Costs()
    curator: Curator = Curator()
    sources: Sources = Sources()
    auditor: Auditor = Auditor()
    visibility: Visibility = Visibility()
    eval: EvalSettings = EvalSettings()


def _section(document: dict[str, object], name: str) -> dict[str, object]:
    value = document.get(name, {})
    return value if isinstance(value, dict) else {}


def _int(section: dict[str, object], name: str, default: int) -> int:
    value = section.get(name, default)
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _str(section: dict[str, object], name: str, default: str) -> str:
    value = section.get(name, default)
    return value if isinstance(value, str) else default


def _optional_str(section: dict[str, object], name: str, default: str | None) -> str | None:
    value = section.get(name, default)
    return value if value is None or isinstance(value, str) else default


def _bool(section: dict[str, object], name: str, default: bool) -> bool:
    value = section.get(name, default)
    return value if isinstance(value, bool) else default


def _float(section: dict[str, object], name: str, default: float) -> float:
    value = section.get(name, default)
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else default


def _strings(section: dict[str, object], name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    value = section.get(name, default)
    if not isinstance(value, list):
        return default
    return tuple(item for item in value if isinstance(item, str))


def load_config(config_path: Path | None = None) -> Config:
    """Load a config file, returning pure documented defaults when it is absent."""

    path = config_path or resolve_paths().config_path
    if not path.exists():
        return Config()

    with path.open("rb") as config_file:
        document = tomllib.load(config_file)

    thresholds = _section(document, "thresholds")
    autonomy = _section(document, "autonomy")
    notifications = _section(document, "notifications")
    models = _section(document, "models")
    prompts = _section(document, "prompts")
    costs = _section(document, "costs")
    curator = _section(document, "curator")
    sources = _section(document, "sources")
    auditor = _section(document, "auditor")
    visibility = _section(document, "visibility")
    eval_section = _section(document, "eval")
    eval_judge = _section(eval_section, "judge")
    defaults = Config()
    # Codex is opt-out when its normal rollout root exists; otherwise preserve a
    # quiet default for machines that have never used Codex.
    codex_default = (Path.home() / ".codex" / "sessions").is_dir()

    return Config(
        thresholds=Thresholds(
            triage_untriaged_count=_int(
                thresholds,
                "triage_untriaged_count",
                defaults.thresholds.triage_untriaged_count,
            ),
            triage_max_age_hours=_int(
                thresholds,
                "triage_max_age_hours",
                defaults.thresholds.triage_max_age_hours,
            ),
            triage_per_run_cap=_int(
                thresholds,
                "triage_per_run_cap",
                defaults.thresholds.triage_per_run_cap,
            ),
            curator_unreviewed_count=_int(
                thresholds,
                "curator_unreviewed_count",
                defaults.thresholds.curator_unreviewed_count,
            ),
            curator_max_age_days=_int(
                thresholds,
                "curator_max_age_days",
                defaults.thresholds.curator_max_age_days,
            ),
        ),
        autonomy=Autonomy(
            mode=_str(autonomy, "mode", defaults.autonomy.mode),
            max_auto_remedies_per_week=_int(
                autonomy,
                "max_auto_remedies_per_week",
                defaults.autonomy.max_auto_remedies_per_week,
            ),
            max_active_auto_skills=_int(
                autonomy,
                "max_active_auto_skills",
                defaults.autonomy.max_active_auto_skills,
            ),
            claude_md_confidence_bar=_float(
                autonomy,
                "claude_md_confidence_bar",
                defaults.autonomy.claude_md_confidence_bar,
            ),
            skill_confidence_bar=_float(
                autonomy,
                "skill_confidence_bar",
                defaults.autonomy.skill_confidence_bar,
            ),
        ),
        notifications=Notifications(
            session_start_digest=_bool(
                notifications,
                "session_start_digest",
                defaults.notifications.session_start_digest,
            ),
            desktop=_bool(notifications, "desktop", defaults.notifications.desktop),
            webhook_url=_str(
                notifications,
                "webhook_url",
                defaults.notifications.webhook_url,
            ),
            events=_strings(
                notifications,
                "events",
                defaults.notifications.events,
            ),
        ),
        models=Models(
            triage=_str(models, "triage", defaults.models.triage),
            curate=_str(models, "curate", defaults.models.curate),
            synthesize=_str(models, "synthesize", defaults.models.synthesize),
            parallelism=_int(models, "parallelism", defaults.models.parallelism),
            triage_effort=_optional_str(models, "triage_effort", defaults.models.triage_effort),
            curate_effort=_optional_str(models, "curate_effort", defaults.models.curate_effort),
            synthesize_effort=_optional_str(models, "synthesize_effort", defaults.models.synthesize_effort),
        ),
        prompts=Prompts(
            triage=_str(prompts, "triage", defaults.prompts.triage),
            curate=_str(prompts, "curate", defaults.prompts.curate),
            garden=_str(prompts, "garden", defaults.prompts.garden),
            synthesize=_str(prompts, "synthesize", defaults.prompts.synthesize),
            judge_remedy=_str(prompts, "judge_remedy", defaults.prompts.judge_remedy),
            judge_counterfactual=_str(prompts, "judge_counterfactual", defaults.prompts.judge_counterfactual),
        ),
        costs=Costs(
            confirm_threshold_usd=_float(
                costs,
                "confirm_threshold_usd",
                defaults.costs.confirm_threshold_usd,
            )
        ),
        curator=Curator(
            qc_sample_size=_int(
                curator, "qc_sample_size", defaults.curator.qc_sample_size
            ),
            context_char_budget=_int(
                curator,
                "context_char_budget",
                defaults.curator.context_char_budget,
            ),
        ),
        sources=Sources(codex=_bool(sources, "codex", codex_default)),
        auditor=Auditor(
            min_post_install_sessions=_int(
                auditor,
                "min_post_install_sessions",
                defaults.auditor.min_post_install_sessions,
            ),
            min_post_install_days=_int(
                auditor,
                "min_post_install_days",
                defaults.auditor.min_post_install_days,
            ),
            min_usage_observation_days=_int(
                auditor,
                "min_usage_observation_days",
                defaults.auditor.min_usage_observation_days,
            ),
            min_sessions_scanned=_int(
                auditor,
                "min_sessions_scanned",
                defaults.auditor.min_sessions_scanned,
            ),
            silent_days=_int(auditor, "silent_days", defaults.auditor.silent_days),
            meaningful_drop_fraction=_float(
                auditor,
                "meaningful_drop_fraction",
                defaults.auditor.meaningful_drop_fraction,
            ),
        ),
        visibility=Visibility(
            library=_bool(visibility, "library", defaults.visibility.library),
            attribution=_bool(visibility, "attribution", defaults.visibility.attribution),
        ),
        eval=EvalSettings(
            mode=(
                configured_mode
                if (configured_mode := _str(eval_section, "mode", defaults.eval.mode))
                in {"auto", "mock", "replay"}
                else defaults.eval.mode
            ),
            judge=EvalJudgeSettings(
                model=_str(eval_judge, "model", defaults.eval.judge.model),
                effort=(
                    configured_effort
                    if (configured_effort := eval_judge.get("effort")) is None
                    or isinstance(configured_effort, str)
                    else defaults.eval.judge.effort
                ),
            ),
        ),
    )


AUTONOMY_STATE_NAME = "autonomy-state.json"
AUTONOMY_MODES = frozenset({"review", "autonomous", "paused"})


def autonomy_state_path() -> Path:
    """Return the comment-preserving override used by ``s2s autonomy``."""

    return resolve_paths().home / AUTONOMY_STATE_NAME


def effective_autonomy_state(config: Config | None = None) -> AutonomyState:
    """Resolve the atomic local override before the user's TOML default.

    The override intentionally lives outside ``config.toml``: rewriting TOML with
    stdlib ``tomllib`` would destroy comments.  Removing the override restores the
    configured ``[autonomy].mode`` value.
    """

    path = autonomy_state_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        payload = None
    if isinstance(payload, dict) and payload.get("mode") in AUTONOMY_MODES:
        return AutonomyState(
            mode=str(payload["mode"]),
            source="override",
            paused_reason=(
                str(payload["paused_reason"])
                if isinstance(payload.get("paused_reason"), str)
                else None
            ),
            updated_at=(
                str(payload["updated_at"])
                if isinstance(payload.get("updated_at"), str)
                else None
            ),
        )
    configured = config or load_config()
    mode = configured.autonomy.mode
    if mode not in {"review", "autonomous"}:
        mode = "review"
    return AutonomyState(mode=mode, source="config")


def set_autonomy_state(
    mode: str,
    *,
    paused_reason: str | None = None,
    timestamp: datetime | None = None,
) -> AutonomyState:
    """Atomically set the instant local policy override."""

    if mode not in AUTONOMY_MODES:
        raise ValueError(f"unknown autonomy mode: {mode}")
    now = timestamp or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    payload = {
        "mode": mode,
        "paused_reason": paused_reason if mode == "paused" else None,
        "updated_at": now.astimezone(timezone.utc).isoformat(),
    }
    path = autonomy_state_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, raw_temp = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temp = Path(raw_temp)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(payload, output, sort_keys=True, separators=(",", ":"))
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
    return AutonomyState(
        mode=mode,
        source="override",
        paused_reason=payload["paused_reason"],
        updated_at=str(payload["updated_at"]),
    )
