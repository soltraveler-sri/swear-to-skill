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


@dataclass(frozen=True)
class Config:
    """Complete configuration available before any optional features exist."""

    thresholds: Thresholds = Thresholds()
    autonomy: Autonomy = Autonomy()
    notifications: Notifications = Notifications()


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
        ),
    )
