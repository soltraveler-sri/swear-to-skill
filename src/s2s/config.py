"""TOML-backed configuration with North Star defaults."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
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

    triage: str = "haiku"
    curate: str = "sonnet"
    synthesize: str = "sonnet"
    parallelism: int = 2


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
class Config:
    """Complete configuration available before any optional features exist."""

    thresholds: Thresholds = Thresholds()
    autonomy: Autonomy = Autonomy()
    notifications: Notifications = Notifications()
    models: Models = Models()
    costs: Costs = Costs()
    curator: Curator = Curator()


def _section(document: dict[str, object], name: str) -> dict[str, object]:
    value = document.get(name, {})
    return value if isinstance(value, dict) else {}


def _int(section: dict[str, object], name: str, default: int) -> int:
    value = section.get(name, default)
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _str(section: dict[str, object], name: str, default: str) -> str:
    value = section.get(name, default)
    return value if isinstance(value, str) else default


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
    costs = _section(document, "costs")
    curator = _section(document, "curator")
    defaults = Config()

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
    )
