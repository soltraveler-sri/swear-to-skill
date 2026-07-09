"""Central, side-effect-free locations for swear-to-skill state."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class S2SPaths:
    """All filesystem locations owned by swear-to-skill.

    Resolution never creates directories. Callers that write state are responsible
    for creating the specific parent directories they need.
    """

    home: Path
    archive_dir: Path
    ledger_path: Path
    state_dir: Path
    config_path: Path
    status_file: Path


def s2s_home() -> Path:
    """Return the configured home, defaulting to ``~/.s2s``.

    ``S2S_HOME`` exists both for isolated tests and for users who choose a
    non-default local state location.
    """

    override = os.environ.get("S2S_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".s2s"


def resolve_paths() -> S2SPaths:
    """Resolve every owned path from the current ``S2S_HOME`` environment."""

    home = s2s_home()
    return S2SPaths(
        home=home,
        archive_dir=home / "archive",
        ledger_path=home / "ledger.db",
        state_dir=home / "state",
        config_path=home / "config.toml",
        status_file=home / "status.txt",
    )
