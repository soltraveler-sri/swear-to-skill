"""Append-only SQLite ledger for swear-to-skill pipeline state.

The ledger deliberately stores only the small, durable facts that later pipeline
stages need.  Full context packs remain files in the archive; incidents keep only
an archive-relative pointer to them.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from .paths import resolve_paths


SCHEMA_VERSION = 9
BUSY_TIMEOUT_MS = 5_000
PROPOSAL_STATES = ("pending", "approved", "installed", "rejected")

INCIDENT_STATES = (
    "detected",
    "triaged",
    "dismissed-triage",
    "open",
    "promoted",
    "parked",
    "dismissed-reviewed",
    "in-proposal",
    "remedied",
)

# This is intentionally the complete state machine from NORTHSTAR.md §5.  States
# with an empty set are terminal; no caller may bypass an intermediate state.
INCIDENT_TRANSITIONS: dict[str, frozenset[str]] = {
    "detected": frozenset({"triaged"}),
    "triaged": frozenset({"dismissed-triage", "open"}),
    # Curator QC sampling may resurrect a triage dismissal (NORTHSTAR §4 Stage 3).
    "dismissed-triage": frozenset({"open"}),
    "open": frozenset({"promoted", "parked", "dismissed-reviewed"}),
    "promoted": frozenset({"in-proposal"}),
    "parked": frozenset({"open"}),
    "dismissed-reviewed": frozenset(),
    "in-proposal": frozenset({"remedied"}),
    "remedied": frozenset(),
}


class LedgerError(RuntimeError):
    """Base error for ledger operations."""


class SchemaVersionError(LedgerError):
    """Raised when a database was created by a newer version of s2s."""


class IncidentNotFoundError(LedgerError):
    """Raised when an operation refers to an unknown incident."""


class IllegalTransitionError(LedgerError):
    """Raised when an incident transition is outside the North Star state graph."""

    def __init__(self, incident_id: int, from_state: str, to_state: str) -> None:
        self.incident_id = incident_id
        self.from_state = from_state
        self.to_state = to_state
        super().__init__(
            f"incident {incident_id} cannot transition from {from_state!r} to {to_state!r}"
        )


@dataclass(frozen=True)
class Incident:
    """A durable, minimal record of one detected frustration."""

    id: int
    source: str
    session_id: str
    project: str
    occurred_at: str
    created_at: str
    message: str
    context_pack_pointer: str | None
    label: str | None
    one_liner: str | None
    severity: str | None
    confidence: float | None
    state: str


@dataclass(frozen=True)
class StateHistoryEntry:
    """One immutable state change, including the initial detection record."""

    id: int
    incident_id: int
    from_state: str | None
    to_state: str
    reason: str
    timestamp: str


@dataclass(frozen=True)
class QueueItem:
    """A transcript-processing work item that survives interrupted pumps."""

    id: int
    item_path: str
    source: str
    enqueued_at: str
    processed_at: str | None


@dataclass(frozen=True)
class SessionStats:
    """Deterministic Stage 1 totals for one transcript session."""

    source: str
    session_id: str
    project: str
    dominant_model: str | None
    direct_message_count: int
    hit_count: int
    first_timestamp: str | None
    last_timestamp: str | None
    scanned_at: str


@dataclass(frozen=True)
class SkillUsage:
    """One usage observation kept separate from frustration incidents."""

    id: int
    skill_name: str
    session_id: str
    source: str
    used_at: str

    @property
    def is_companion(self) -> bool:
        return self.skill_name == "s2s"


@dataclass(frozen=True)
class ClusterStats:
    """Derived statistics for an implicit cluster (all incidents sharing a label)."""

    label: str
    incident_count: int
    project_count: int
    first_seen: str
    last_seen: str
    remedy_ids: tuple[int, ...]


@dataclass(frozen=True)
class CuratorDecision:
    """One durable Curator judgment over an incident's full evidence."""

    id: int
    incident_id: int
    verdict: str
    reason: str
    previous_label: str | None
    reassign_label: str | None
    singleton: bool
    created_at: str


@dataclass(frozen=True)
class CuratorClusterDecision:
    """One durable Curator judgment over a label cluster."""

    id: int
    label: str
    verdict: str
    reason: str
    created_at: str


@dataclass(frozen=True)
class Proposal:
    """One Stage 5 proposal, including its gate audit fields."""

    id: int
    remedy_type: str
    drafted_content: str
    evidence_incident_ids: tuple[int, ...]
    dedup_verdict: str
    gate_status: str
    install_record_ref: str | None
    revises: str | None
    singleton: bool
    rejection_reason: str | None
    approved_at: str | None
    decided_at: str | None
    autonomy_decision: str | None
    autonomy_decided_at: str | None
    created_at: str
    proposal_kind: str
    target_remedy_id: int | None


@dataclass(frozen=True)
class Remedy:
    """One reserved, installed, or rolled-back filesystem remedy."""

    id: int
    artifact_type: str
    artifact_path: str
    artifact_digest: str | None
    proposal_id: int
    installed_at: str
    rollback_at: str | None
    state: str
    managed_remedy_id: int
    revises_remedy_id: int | None
    state_record_ref: str | None
    outcome_verdict: str | None
    outcome_pre_rate: float | None
    outcome_post_rate: float | None
    outcome_assessed_at: str | None
    provenance: str


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _timestamp(value: str | datetime | None) -> str:
    if value is None:
        return _utc_now()
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    return value


def _migration_1(connection: sqlite3.Connection) -> None:
    """Create the initial append-only ledger schema."""

    state_values = ", ".join(repr(state) for state in INCIDENT_STATES)
    connection.execute(
        f"""
        CREATE TABLE incident (
            id INTEGER PRIMARY KEY,
            source TEXT NOT NULL CHECK (source IN ('claude-code', 'codex')),
            session_id TEXT NOT NULL,
            project TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            message TEXT NOT NULL,
            context_pack_pointer TEXT,
            label TEXT,
            one_liner TEXT,
            severity TEXT,
            confidence REAL,
            state TEXT NOT NULL CHECK (state IN ({state_values}))
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE state_history (
            id INTEGER PRIMARY KEY,
            incident_id INTEGER NOT NULL REFERENCES incident(id) ON DELETE RESTRICT,
            from_state TEXT,
            to_state TEXT NOT NULL,
            reason TEXT NOT NULL,
            timestamp TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE proposal (
            id INTEGER PRIMARY KEY,
            remedy_type TEXT NOT NULL,
            drafted_content TEXT NOT NULL,
            evidence_incident_ids TEXT NOT NULL,
            dedup_verdict TEXT NOT NULL,
            gate_status TEXT NOT NULL,
            install_record_ref TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE proposal_evidence (
            proposal_id INTEGER NOT NULL REFERENCES proposal(id) ON DELETE RESTRICT,
            incident_id INTEGER NOT NULL REFERENCES incident(id) ON DELETE RESTRICT,
            PRIMARY KEY (proposal_id, incident_id)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE remedy (
            id INTEGER PRIMARY KEY,
            artifact_type TEXT NOT NULL,
            artifact_path TEXT NOT NULL,
            artifact_digest TEXT,
            proposal_id INTEGER NOT NULL REFERENCES proposal(id) ON DELETE RESTRICT,
            installed_at TEXT NOT NULL,
            rollback_at TEXT,
            rollback_metadata TEXT,
            outcome_incident_count_before INTEGER,
            outcome_incident_count_after INTEGER,
            outcome_session_count_before INTEGER,
            outcome_session_count_after INTEGER,
            outcome_measured_at TEXT
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE run_log (
            id INTEGER PRIMARY KEY,
            stage TEXT NOT NULL,
            model TEXT NOT NULL,
            tokens INTEGER NOT NULL,
            cost_usd REAL NOT NULL,
            duration_ms INTEGER NOT NULL,
            input_digest TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE queue (
            id INTEGER PRIMARY KEY,
            item_path TEXT NOT NULL,
            source TEXT NOT NULL CHECK (source IN ('claude-code', 'codex')),
            enqueued_at TEXT NOT NULL,
            processed_at TEXT
        )
        """
    )
    connection.execute("CREATE INDEX incident_state_idx ON incident(state)")
    connection.execute("CREATE INDEX incident_label_idx ON incident(label)")
    connection.execute(
        "CREATE INDEX state_history_incident_idx ON state_history(incident_id, id)"
    )
    connection.execute("CREATE INDEX proposal_evidence_incident_idx ON proposal_evidence(incident_id)")
    connection.execute("CREATE INDEX queue_pending_idx ON queue(processed_at, id)")


def _migration_2(connection: sqlite3.Connection) -> None:
    """Add deterministic scanner totals and its transcript-level idempotency key."""

    connection.execute(
        """
        CREATE TABLE session_stats (
            source TEXT NOT NULL CHECK (source IN ('claude-code', 'codex')),
            session_id TEXT NOT NULL,
            project TEXT NOT NULL,
            dominant_model TEXT,
            direct_message_count INTEGER NOT NULL CHECK (direct_message_count >= 0),
            hit_count INTEGER NOT NULL CHECK (hit_count >= 0),
            first_timestamp TEXT,
            last_timestamp TEXT,
            scanned_at TEXT NOT NULL,
            PRIMARY KEY (source, session_id)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE scan_dedup (
            id INTEGER PRIMARY KEY,
            session_id TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            message TEXT NOT NULL,
            incident_id INTEGER NOT NULL UNIQUE REFERENCES incident(id) ON DELETE RESTRICT
        )
        """
    )


def _migration_3(connection: sqlite3.Connection) -> None:
    """Add Curator pass metadata, provenance, and append-only decisions."""

    connection.execute(
        """
        CREATE TABLE meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE curator_incident_decision (
            id INTEGER PRIMARY KEY,
            incident_id INTEGER NOT NULL REFERENCES incident(id) ON DELETE RESTRICT,
            verdict TEXT NOT NULL CHECK (
                verdict IN ('promote', 'park', 'dismiss', 'reassign', 'resurrect')
            ),
            reason TEXT NOT NULL,
            previous_label TEXT,
            reassign_label TEXT,
            singleton INTEGER NOT NULL DEFAULT 0 CHECK (singleton IN (0, 1)),
            created_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE curator_cluster_decision (
            id INTEGER PRIMARY KEY,
            label TEXT NOT NULL,
            verdict TEXT NOT NULL CHECK (verdict IN ('synthesize', 'hold', 'unworthy')),
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX curator_incident_decision_incident_idx
        ON curator_incident_decision (incident_id, id)
        """
    )
    connection.execute(
        """
        CREATE INDEX curator_cluster_decision_label_idx
        ON curator_cluster_decision (label, id)
        """
    )
    # A direct user message is the scanner unit of work.  This makes manual
    # re-scans and a retried queue item safe even after a process interruption,
    # without imposing scanner semantics on incidents written by later stages.
    connection.execute(
        """
        CREATE UNIQUE INDEX scan_dedup_key_idx
        ON scan_dedup (session_id, timestamp, message)
        """
    )


def _migration_4(connection: sqlite3.Connection) -> None:
    """Add Synthesist provenance for singleton and revision proposals."""

    connection.execute("ALTER TABLE proposal ADD COLUMN revises TEXT")
    connection.execute(
        "ALTER TABLE proposal ADD COLUMN singleton INTEGER NOT NULL DEFAULT 0 "
        "CHECK (singleton IN (0, 1))"
    )


def _migration_5(connection: sqlite3.Connection) -> None:
    """Add Gate decision audit fields and explicit remedy lifecycle metadata."""

    connection.execute("ALTER TABLE proposal ADD COLUMN rejection_reason TEXT")
    connection.execute("ALTER TABLE proposal ADD COLUMN approved_at TEXT")
    connection.execute("ALTER TABLE proposal ADD COLUMN decided_at TEXT")
    connection.execute(
        "ALTER TABLE remedy ADD COLUMN state TEXT NOT NULL DEFAULT 'installed' "
        "CHECK (state IN ('approved', 'installed', 'rolled-back'))"
    )
    connection.execute("ALTER TABLE remedy ADD COLUMN managed_remedy_id INTEGER")
    connection.execute(
        "ALTER TABLE remedy ADD COLUMN revises_remedy_id INTEGER REFERENCES remedy(id)"
    )
    connection.execute("ALTER TABLE remedy ADD COLUMN state_record_ref TEXT")
    connection.execute("UPDATE remedy SET managed_remedy_id = id")
    connection.execute("CREATE INDEX remedy_state_idx ON remedy(state, id)")


def _migration_6(connection: sqlite3.Connection) -> None:
    """Add audit proposal identity and the latest deterministic remedy outcome."""

    connection.execute(
        "ALTER TABLE proposal ADD COLUMN proposal_kind TEXT NOT NULL DEFAULT 'remedy' "
        "CHECK (proposal_kind IN ('remedy', 'revision', 'retirement'))"
    )
    connection.execute(
        "ALTER TABLE proposal ADD COLUMN target_remedy_id INTEGER REFERENCES remedy(id)"
    )
    connection.execute(
        "ALTER TABLE remedy ADD COLUMN outcome_verdict TEXT "
        "CHECK (outcome_verdict IN ('effective', 'persisting', 'silent', 'insufficient-data'))"
    )
    connection.execute("ALTER TABLE remedy ADD COLUMN outcome_pre_rate REAL")
    connection.execute("ALTER TABLE remedy ADD COLUMN outcome_post_rate REAL")
    connection.execute("ALTER TABLE remedy ADD COLUMN outcome_assessed_at TEXT")
    connection.execute(
        "CREATE INDEX proposal_audit_target_idx ON proposal(target_remedy_id, proposal_kind, gate_status)"
    )


def _migration_7(connection: sqlite3.Connection) -> None:
    """Tag review-approved and autonomous remedies without changing install semantics."""

    connection.execute(
        "ALTER TABLE remedy ADD COLUMN provenance TEXT NOT NULL DEFAULT 'human' "
        "CHECK (provenance IN ('human', 'auto'))"
    )
    connection.execute(
        "CREATE INDEX remedy_provenance_idx ON remedy(provenance, state, installed_at)"
    )
    connection.execute("ALTER TABLE proposal ADD COLUMN autonomy_decision TEXT")
    connection.execute("ALTER TABLE proposal ADD COLUMN autonomy_decided_at TEXT")


def _migration_8(connection: sqlite3.Connection) -> None:
    """Record the model transport and allow CLIs with no usage data to log nulls."""

    connection.execute("ALTER TABLE run_log RENAME TO run_log_v7")
    connection.execute(
        """
        CREATE TABLE run_log (
            id INTEGER PRIMARY KEY,
            stage TEXT NOT NULL,
            model TEXT NOT NULL,
            transport TEXT NOT NULL CHECK (transport IN ('claude', 'codex')),
            tokens INTEGER,
            cost_usd REAL,
            duration_ms INTEGER NOT NULL,
            input_digest TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        INSERT INTO run_log (
            id, stage, model, transport, tokens, cost_usd, duration_ms, input_digest, created_at
        )
        SELECT id, stage, model, 'claude', tokens, cost_usd, duration_ms, input_digest, created_at
        FROM run_log_v7
        """
    )
    connection.execute("DROP TABLE run_log_v7")


def _migration_9(connection: sqlite3.Connection) -> None:
    """Add isolated, idempotent mechanical skill-usage observations."""

    connection.execute(
        """
        CREATE TABLE skill_usage (
            id INTEGER PRIMARY KEY,
            skill_name TEXT NOT NULL,
            session_id TEXT NOT NULL,
            source TEXT NOT NULL CHECK (source IN ('claude-code', 'codex')),
            used_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE UNIQUE INDEX skill_usage_dedup_idx
        ON skill_usage (source, session_id, skill_name, used_at)
        """
    )
    connection.execute(
        "CREATE INDEX skill_usage_skill_time_idx ON skill_usage (skill_name, used_at)"
    )


MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {
    1: _migration_1,
    2: _migration_2,
    3: _migration_3,
    4: _migration_4,
    5: _migration_5,
    6: _migration_6,
    7: _migration_7,
    8: _migration_8,
    9: _migration_9,
}


def migrate(connection: sqlite3.Connection) -> None:
    """Apply migrations in order, refusing to open a newer database for writing."""

    current = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if current > SCHEMA_VERSION:
        raise SchemaVersionError(
            f"ledger schema version {current} is newer than supported version {SCHEMA_VERSION}"
        )

    for version in range(current + 1, SCHEMA_VERSION + 1):
        migration = MIGRATIONS.get(version)
        if migration is None:  # Defensive: a shipped version must have its migration.
            raise SchemaVersionError(f"missing migration for schema version {version}")
        connection.execute("BEGIN IMMEDIATE")
        try:
            migration(connection)
            connection.execute(f"PRAGMA user_version = {version}")
        except Exception:
            connection.rollback()
            raise
        else:
            connection.commit()


def connect_ledger(path: Path | str | None = None) -> sqlite3.Connection:
    """Open and migrate a ledger connection with WAL and a bounded lock wait."""

    database_path = Path(path) if path is not None else resolve_paths().ledger_path
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        connection.execute("PRAGMA journal_mode = WAL")
        migrate(connection)
    except Exception:
        connection.close()
        raise
    return connection


class Ledger:
    """High-level append-only operations over a single s2s ledger database."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else resolve_paths().ledger_path
        self.connection = connect_ledger(self.path)

    def close(self) -> None:
        """Close the owned SQLite connection."""

        self.connection.close()

    def __enter__(self) -> Ledger:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    @contextmanager
    def _write_transaction(self) -> Iterator[None]:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def create_incident(
        self,
        *,
        source: str,
        session_id: str,
        project: str,
        message: str,
        occurred_at: str | datetime | None = None,
        context_pack_pointer: str | None = None,
        label: str | None = None,
        one_liner: str | None = None,
        severity: str | None = None,
        confidence: float | None = None,
        created_at: str | datetime | None = None,
    ) -> int:
        """Record a newly detected incident and its initial audit-history entry."""

        occurred = _timestamp(occurred_at)
        created = _timestamp(created_at)
        with self._write_transaction():
            cursor = self.connection.execute(
                """
                INSERT INTO incident (
                    source, session_id, project, occurred_at, created_at, message,
                    context_pack_pointer, label, one_liner, severity, confidence, state
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'detected')
                """,
                (
                    source,
                    session_id,
                    project,
                    occurred,
                    created,
                    message,
                    context_pack_pointer,
                    label,
                    one_liner,
                    severity,
                    confidence,
                ),
            )
            incident_id = int(cursor.lastrowid)
            self.connection.execute(
                """
                INSERT INTO state_history (incident_id, from_state, to_state, reason, timestamp)
                VALUES (?, NULL, 'detected', ?, ?)
                """,
                (incident_id, "incident detected", created),
            )
        return incident_id

    def create_scanned_incident(
        self,
        *,
        source: str,
        session_id: str,
        project: str,
        message: str,
        occurred_at: str,
        created_at: str | datetime | None = None,
    ) -> int:
        """Create one Stage 1 incident and atomically reserve its scan identity."""

        created = _timestamp(created_at)
        with self._write_transaction():
            cursor = self.connection.execute(
                """
                INSERT INTO incident (
                    source, session_id, project, occurred_at, created_at, message,
                    context_pack_pointer, label, one_liner, severity, confidence, state
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, 'detected')
                """,
                (source, session_id, project, occurred_at, created, message),
            )
            incident_id = int(cursor.lastrowid)
            self.connection.execute(
                """
                INSERT INTO state_history (incident_id, from_state, to_state, reason, timestamp)
                VALUES (?, NULL, 'detected', ?, ?)
                """,
                (incident_id, "incident detected", created),
            )
            self.connection.execute(
                """
                INSERT INTO scan_dedup (session_id, timestamp, message, incident_id)
                VALUES (?, ?, ?, ?)
                """,
                (session_id, occurred_at, message, incident_id),
            )
        return incident_id

    def transition_incident(
        self,
        incident_id: int,
        to_state: str,
        *,
        reason: str,
        timestamp: str | datetime | None = None,
    ) -> None:
        """Apply one legal state transition and append its immutable history record."""

        with self._write_transaction():
            self._transition_in_transaction(incident_id, to_state, reason, timestamp)

    # A short alias keeps stage-writer call sites readable without weakening checks.
    transition = transition_incident

    def _transition_in_transaction(
        self,
        incident_id: int,
        to_state: str,
        reason: str,
        timestamp: str | datetime | None,
    ) -> None:
        row = self.connection.execute(
            "SELECT state FROM incident WHERE id = ?", (incident_id,)
        ).fetchone()
        if row is None:
            raise IncidentNotFoundError(f"incident {incident_id} does not exist")

        from_state = str(row["state"])
        if to_state not in INCIDENT_TRANSITIONS[from_state]:
            raise IllegalTransitionError(incident_id, from_state, to_state)
        if not reason.strip():
            raise LedgerError("a state transition requires a non-empty reason")

        changed_at = _timestamp(timestamp)
        self.connection.execute(
            "UPDATE incident SET state = ? WHERE id = ?", (to_state, incident_id)
        )
        self.connection.execute(
            """
            INSERT INTO state_history (incident_id, from_state, to_state, reason, timestamp)
            VALUES (?, ?, ?, ?, ?)
            """,
            (incident_id, from_state, to_state, reason, changed_at),
        )

    def triage_incident(
        self,
        incident_id: int,
        *,
        label: str,
        one_liner: str,
        severity: str,
        confidence: float,
        context_pack_pointer: str,
        dismissed: bool = False,
        reason: str = "triage completed",
        timestamp: str | datetime | None = None,
    ) -> None:
        """Write triage fields, then move through ``triaged`` to its triage outcome."""

        if not label.strip() or not one_liner.strip() or not context_pack_pointer.strip():
            raise LedgerError("triage requires a label, one-liner, and context-pack pointer")
        with self._write_transaction():
            result = self.connection.execute(
                """
                UPDATE incident
                SET label = ?, one_liner = ?, severity = ?, confidence = ?, context_pack_pointer = ?
                WHERE id = ?
                """,
                (label, one_liner, severity, confidence, context_pack_pointer, incident_id),
            )
            if result.rowcount != 1:
                raise IncidentNotFoundError(f"incident {incident_id} does not exist")
            self._transition_in_transaction(incident_id, "triaged", reason, timestamp)
            outcome = "dismissed-triage" if dismissed else "open"
            outcome_reason = reason if dismissed else "triage accepted"
            self._transition_in_transaction(incident_id, outcome, outcome_reason, timestamp)

    def wake_parked_incidents(
        self,
        label: str,
        *,
        reason: str = "new arrival in cluster",
        timestamp: str | datetime | None = None,
    ) -> list[int]:
        """Wake every parked incident in a touched label cluster back to ``open``."""

        with self._write_transaction():
            incident_ids = [
                int(row["id"])
                for row in self.connection.execute(
                    "SELECT id FROM incident WHERE state = 'parked' AND label = ? ORDER BY id",
                    (label,),
                )
            ]
            for incident_id in incident_ids:
                self._transition_in_transaction(incident_id, "open", reason, timestamp)
        return incident_ids

    def get_incident(self, incident_id: int) -> Incident | None:
        """Return an incident without hiding dismissed or terminal states."""

        row = self.connection.execute("SELECT * FROM incident WHERE id = ?", (incident_id,)).fetchone()
        return self._incident_from_row(row) if row is not None else None

    def has_incident_scan_key(self, *, session_id: str, occurred_at: str, message: str) -> bool:
        """Return whether a scanner detection already owns this message identity."""

        row = self.connection.execute(
            """
            SELECT 1 FROM scan_dedup
            WHERE session_id = ? AND timestamp = ? AND message = ?
            LIMIT 1
            """,
            (session_id, occurred_at, message),
        ).fetchone()
        return row is not None

    def untriaged_incidents(self) -> list[Incident]:
        """Return detected incidents awaiting the triager."""

        return self.incidents_in_state("detected")

    def unreviewed_incidents(self) -> list[Incident]:
        """Return open incidents awaiting the Curator's one full-context review."""

        return self.incidents_in_state("open")

    def curator_unreviewed_incidents(self) -> list[Incident]:
        """Return open incidents not yet given any full-context Curator verdict.

        QC resurrection is itself a full-context Curator judgment, so it counts for
        the pay-once invariant. The resurrected incident remains visible in cluster
        digests and can move forward through a later cluster verdict. Previously
        parked incidents are surfaced only by the next-arrival event path.
        """

        rows = self.connection.execute(
            """
            SELECT incident.*
            FROM incident
            WHERE incident.state = 'open'
              AND NOT EXISTS (
                  SELECT 1
                  FROM curator_incident_decision
                  WHERE curator_incident_decision.incident_id = incident.id
              )
            ORDER BY incident.occurred_at, incident.id
            """
        ).fetchall()
        return [self._incident_from_row(row) for row in rows]

    def curator_qc_candidates(self) -> list[Incident]:
        """Return recent triage dismissals never before sampled by the Curator."""

        rows = self.connection.execute(
            """
            SELECT incident.*
            FROM incident
            WHERE incident.state = 'dismissed-triage'
              AND NOT EXISTS (
                  SELECT 1
                  FROM curator_incident_decision
                  WHERE curator_incident_decision.incident_id = incident.id
              )
            ORDER BY incident.occurred_at DESC, incident.id DESC
            """
        ).fetchall()
        return [self._incident_from_row(row) for row in rows]

    def parked_incidents(self, label: str) -> list[Incident]:
        """Return parked incidents for one label, including all durable metadata."""

        rows = self.connection.execute(
            "SELECT * FROM incident WHERE state = 'parked' AND label = ? ORDER BY occurred_at, id",
            (label,),
        ).fetchall()
        return [self._incident_from_row(row) for row in rows]

    def active_cluster_members(self) -> list[Incident]:
        """Return labelled members that still participate in an active cluster."""

        rows = self.connection.execute(
            """
            SELECT * FROM incident
            WHERE label IS NOT NULL
              AND state IN ('open', 'parked', 'promoted', 'in-proposal', 'remedied')
            ORDER BY label, occurred_at, id
            """
        ).fetchall()
        return [self._incident_from_row(row) for row in rows]

    def other_nonterminal_incidents(self) -> list[Incident]:
        """Return the gardener's complete active ``other`` evidence set."""

        rows = self.connection.execute(
            """
            SELECT * FROM incident
            WHERE label = 'other'
              AND state NOT IN ('dismissed-triage', 'dismissed-reviewed', 'remedied')
            ORDER BY occurred_at, id
            """
        ).fetchall()
        return [self._incident_from_row(row) for row in rows]

    def relabel_incidents(self, incident_ids: list[int] | tuple[int, ...], label: str) -> int:
        """Bulk-update labels without changing the incident state machine history."""

        ids = tuple(incident_ids)
        if not ids or not label.strip():
            raise LedgerError("relabeling requires incident IDs and a non-empty label")
        if len(set(ids)) != len(ids) or not all(isinstance(item, int) and item > 0 for item in ids):
            raise LedgerError("relabeling requires unique positive incident IDs")
        marks = ", ".join("?" for _ in ids)
        with self._write_transaction():
            result = self.connection.execute(
                f"UPDATE incident SET label = ? WHERE id IN ({marks})", (label, *ids)
            )
            if result.rowcount != len(ids):
                raise IncidentNotFoundError("one or more incidents do not exist")
            return result.rowcount

    def relabel_label(self, absorbed: str, survivor: str) -> int:
        """Merge an entire label cluster without altering incident states/history."""

        if not absorbed.strip() or not survivor.strip():
            raise LedgerError("label merges require non-empty labels")
        with self._write_transaction():
            result = self.connection.execute(
                "UPDATE incident SET label = ? WHERE label = ?", (survivor, absorbed)
            )
            return result.rowcount

    def proposal_digest_rows(self) -> list[tuple[int, str, str, str]]:
        """Return the compact proposal fields needed by the Curator ledger digest."""

        rows = self.connection.execute(
            """
            SELECT id, remedy_type, gate_status, drafted_content
            FROM proposal
            ORDER BY id
            """
        ).fetchall()
        return [
            (int(row["id"]), str(row["remedy_type"]), str(row["gate_status"]), str(row["drafted_content"]))
            for row in rows
        ]

    def pending_proposal_count(self) -> int:
        """Return proposals awaiting the Stage 5 human gate."""

        return int(
            self.connection.execute(
                "SELECT COUNT(*) FROM proposal WHERE gate_status = 'pending'"
            ).fetchone()[0]
        )

    def pending_proposals(self) -> list[Proposal]:
        """Return proposals awaiting a Gate decision in stable FIFO order."""

        rows = self.connection.execute(
            "SELECT * FROM proposal WHERE gate_status = 'pending' ORDER BY id"
        ).fetchall()
        return [self._proposal_from_row(row) for row in rows]

    def autonomy_pending_proposals(self) -> list[Proposal]:
        """Return pending proposals not already handed to the human queue by autonomy."""

        rows = self.connection.execute(
            "SELECT * FROM proposal "
            "WHERE gate_status = 'pending' AND autonomy_decision IS NULL ORDER BY id"
        ).fetchall()
        return [self._proposal_from_row(row) for row in rows]

    def mark_proposal_autonomy_deferred(
        self,
        proposal_id: int,
        decision: str,
        *,
        timestamp: str | datetime | None = None,
    ) -> None:
        """Permanently hand one guarded proposal to review without rejecting it."""

        if decision not in {"fallback-to-human", "cap-hit"}:
            raise LedgerError(f"unknown autonomy deferral {decision!r}")
        with self._write_transaction():
            result = self.connection.execute(
                "UPDATE proposal SET autonomy_decision = ?, autonomy_decided_at = ? "
                "WHERE id = ? AND gate_status = 'pending' AND autonomy_decision IS NULL",
                (decision, _timestamp(timestamp), proposal_id),
            )
            if result.rowcount != 1:
                raise LedgerError(
                    f"proposal {proposal_id} is not awaiting an autonomous decision"
                )

    def get_proposal(self, proposal_id: int) -> Proposal | None:
        """Return one proposal, including rejected and installed audit records."""

        row = self.connection.execute(
            "SELECT * FROM proposal WHERE id = ?", (proposal_id,)
        ).fetchone()
        return self._proposal_from_row(row) if row is not None else None

    def approve_proposal(
        self,
        proposal_id: int,
        *,
        drafted_content: str | None = None,
        timestamp: str | datetime | None = None,
    ) -> Proposal:
        """Move a pending proposal to approved, optionally storing a human edit."""

        changed_at = _timestamp(timestamp)
        with self._write_transaction():
            row = self.connection.execute(
                "SELECT gate_status FROM proposal WHERE id = ?", (proposal_id,)
            ).fetchone()
            if row is None:
                raise LedgerError(f"proposal {proposal_id} does not exist")
            if row["gate_status"] != "pending":
                raise LedgerError(
                    f"proposal {proposal_id} is {row['gate_status']!r}, not pending"
                )
            if drafted_content is None:
                result = self.connection.execute(
                    """
                    UPDATE proposal
                    SET gate_status = 'approved', approved_at = ?, decided_at = ?
                    WHERE id = ?
                    """,
                    (changed_at, changed_at, proposal_id),
                )
            else:
                result = self.connection.execute(
                    """
                    UPDATE proposal
                    SET drafted_content = ?, gate_status = 'approved',
                        approved_at = ?, decided_at = ?
                    WHERE id = ?
                    """,
                    (drafted_content, changed_at, changed_at, proposal_id),
                )
            assert result.rowcount == 1
        proposal = self.get_proposal(proposal_id)
        assert proposal is not None
        return proposal

    def reject_proposal(
        self,
        proposal_id: int,
        *,
        reason: str | None = None,
        timestamp: str | datetime | None = None,
    ) -> None:
        """Reject one pending proposal and retain the user's optional reason."""

        with self._write_transaction():
            result = self.connection.execute(
                """
                UPDATE proposal
                SET gate_status = 'rejected', rejection_reason = ?, decided_at = ?
                WHERE id = ? AND gate_status = 'pending'
                """,
                (reason, _timestamp(timestamp), proposal_id),
            )
            if result.rowcount != 1:
                row = self.connection.execute(
                    "SELECT gate_status FROM proposal WHERE id = ?", (proposal_id,)
                ).fetchone()
                if row is None:
                    raise LedgerError(f"proposal {proposal_id} does not exist")
                raise LedgerError(
                    f"proposal {proposal_id} is {row['gate_status']!r}, not pending"
                )

    def proposal_surface_rows(self) -> list[tuple[str, str]]:
        """Return compact references for pending and installed proposal deduplication."""

        rows = self.connection.execute(
            """
            SELECT proposal.id, proposal.remedy_type, proposal.gate_status,
                   proposal.drafted_content, proposal.revises,
                   EXISTS(SELECT 1 FROM remedy WHERE remedy.proposal_id = proposal.id) AS installed
            FROM proposal
            WHERE proposal.gate_status = 'pending'
               OR EXISTS(SELECT 1 FROM remedy WHERE remedy.proposal_id = proposal.id)
            ORDER BY proposal.id
            """
        ).fetchall()
        return [
            (
                f"proposal #{int(row['id'])}",
                " | ".join(
                    part
                    for part in (
                        f"type={row['remedy_type']}",
                        f"status={row['gate_status']}",
                        "installed" if row["installed"] else None,
                        f"revises={row['revises']}" if row["revises"] else None,
                        str(row["drafted_content"])[:500],
                    )
                    if part
                ),
            )
            for row in rows
        ]

    def remedy_digest_rows(self) -> list[tuple[int, str, str, str | None]]:
        """Return the compact installed/rolled-back remedy fields for the digest."""

        rows = self.connection.execute(
            """
            SELECT id, artifact_type, artifact_path, rollback_at
            FROM remedy
            ORDER BY id
            """
        ).fetchall()
        return [
            (
                int(row["id"]),
                str(row["artifact_type"]),
                str(row["artifact_path"]),
                row["rollback_at"],
            )
            for row in rows
        ]

    def incidents_in_state(self, state: str) -> list[Incident]:
        """Return all incidents in one state, oldest first."""

        if state not in INCIDENT_TRANSITIONS:
            raise LedgerError(f"unknown incident state {state!r}")
        rows = self.connection.execute(
            "SELECT * FROM incident WHERE state = ? ORDER BY occurred_at, id", (state,)
        ).fetchall()
        return [self._incident_from_row(row) for row in rows]

    def state_history(self, incident_id: int) -> list[StateHistoryEntry]:
        """Return the complete append-only audit trail for an incident."""

        rows = self.connection.execute(
            "SELECT * FROM state_history WHERE incident_id = ? ORDER BY id", (incident_id,)
        ).fetchall()
        return [
            StateHistoryEntry(
                id=int(row["id"]),
                incident_id=int(row["incident_id"]),
                from_state=row["from_state"],
                to_state=str(row["to_state"]),
                reason=str(row["reason"]),
                timestamp=str(row["timestamp"]),
            )
            for row in rows
        ]

    def enqueue_item(
        self,
        item_path: str,
        source: str,
        *,
        enqueued_at: str | datetime | None = None,
    ) -> int:
        """Append a transcript work item; processing later only fills ``processed_at``."""

        with self._write_transaction():
            cursor = self.connection.execute(
                "INSERT INTO queue (item_path, source, enqueued_at) VALUES (?, ?, ?)",
                (item_path, source, _timestamp(enqueued_at)),
            )
            return int(cursor.lastrowid)

    enqueue = enqueue_item

    def pending_queue_items(self) -> list[QueueItem]:
        """Return unprocessed queue work in durable FIFO order."""

        rows = self.connection.execute(
            "SELECT * FROM queue WHERE processed_at IS NULL ORDER BY id"
        ).fetchall()
        return [self._queue_item_from_row(row) for row in rows]

    unprocessed_queue_items = pending_queue_items

    def mark_queue_item_processed(
        self, item_id: int, *, processed_at: str | datetime | None = None
    ) -> None:
        """Mark work complete without deleting the queue record."""

        with self._write_transaction():
            result = self.connection.execute(
                "UPDATE queue SET processed_at = ? WHERE id = ? AND processed_at IS NULL",
                (_timestamp(processed_at), item_id),
            )
            if result.rowcount != 1:
                row = self.connection.execute("SELECT id FROM queue WHERE id = ?", (item_id,)).fetchone()
                if row is None:
                    raise LedgerError(f"queue item {item_id} does not exist")
                raise LedgerError(f"queue item {item_id} is already marked processed")

    def upsert_session_stats(
        self,
        *,
        source: str,
        session_id: str,
        project: str,
        dominant_model: str | None,
        direct_message_count: int,
        hit_count: int,
        first_timestamp: str | None,
        last_timestamp: str | None,
        scanned_at: str | datetime | None = None,
    ) -> None:
        """Store the latest complete deterministic totals for one session."""

        if direct_message_count < 0 or hit_count < 0:
            raise LedgerError("session statistics counts cannot be negative")
        with self._write_transaction():
            self.connection.execute(
                """
                INSERT INTO session_stats (
                    source, session_id, project, dominant_model, direct_message_count,
                    hit_count, first_timestamp, last_timestamp, scanned_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source, session_id) DO UPDATE SET
                    project = excluded.project,
                    dominant_model = excluded.dominant_model,
                    direct_message_count = excluded.direct_message_count,
                    hit_count = excluded.hit_count,
                    first_timestamp = excluded.first_timestamp,
                    last_timestamp = excluded.last_timestamp,
                    scanned_at = excluded.scanned_at
                """,
                (
                    source,
                    session_id,
                    project,
                    dominant_model,
                    direct_message_count,
                    hit_count,
                    first_timestamp,
                    last_timestamp,
                    _timestamp(scanned_at),
                ),
            )

    record_session_stats = upsert_session_stats

    def session_stats(self, session_id: str | None = None) -> list[SessionStats]:
        """Return deterministic scan totals, optionally for one source session id."""

        if session_id is None:
            rows = self.connection.execute(
                "SELECT * FROM session_stats ORDER BY first_timestamp, source, session_id"
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM session_stats WHERE session_id = ? ORDER BY source", (session_id,)
            ).fetchall()
        return [self._session_stats_from_row(row) for row in rows]

    def record_skill_usage(
        self,
        *,
        skill_name: str,
        session_id: str,
        source: str,
        used_at: str,
    ) -> bool:
        """Record one mechanical invocation, returning false for a rescan duplicate."""

        if source not in {"claude-code", "codex"}:
            raise LedgerError(f"unknown skill usage source {source!r}")
        if not skill_name or not session_id:
            raise LedgerError("skill usage requires a skill name and session id")
        with self._write_transaction():
            cursor = self.connection.execute(
                """
                INSERT INTO skill_usage (skill_name, session_id, source, used_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(source, session_id, skill_name, used_at) DO NOTHING
                """,
                (skill_name, session_id, source, used_at),
            )
        return cursor.rowcount == 1

    def skill_usage(
        self,
        skill_name: str | None = None,
        *,
        after: str | None = None,
        before: str | None = None,
    ) -> list[SkillUsage]:
        """Query usage observations without consulting or joining incidents."""

        clauses: list[str] = []
        values: list[object] = []
        if skill_name is not None:
            clauses.append("skill_name = ?")
            values.append(skill_name)
        if after is not None:
            clauses.append("used_at >= ?")
            values.append(after)
        if before is not None:
            clauses.append("used_at < ?")
            values.append(before)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.connection.execute(
            f"SELECT * FROM skill_usage{where} ORDER BY used_at, id", values
        ).fetchall()
        return [
            SkillUsage(
                id=int(row["id"]),
                skill_name=str(row["skill_name"]),
                session_id=str(row["session_id"]),
                source=str(row["source"]),
                used_at=str(row["used_at"]),
            )
            for row in rows
        ]

    def skill_usage_count(
        self,
        skill_name: str,
        *,
        after: str | None = None,
        before: str | None = None,
    ) -> int:
        """Count one skill's observations in a half-open time window."""

        clauses = ["skill_name = ?"]
        values: list[object] = [skill_name]
        if after is not None:
            clauses.append("used_at >= ?")
            values.append(after)
        if before is not None:
            clauses.append("used_at < ?")
            values.append(before)
        row = self.connection.execute(
            f"SELECT COUNT(*) AS total FROM skill_usage WHERE {' AND '.join(clauses)}",
            values,
        ).fetchone()
        return int(row["total"])

    def sessions_scanned_since(self, since: str, *, before: str | None = None) -> int:
        """Count durable session scans completed in a half-open observation window."""

        clauses = ["scanned_at >= ?"]
        values: list[object] = [since]
        if before is not None:
            clauses.append("scanned_at < ?")
            values.append(before)
        row = self.connection.execute(
            f"SELECT COUNT(*) AS total FROM session_stats WHERE {' AND '.join(clauses)}",
            values,
        ).fetchone()
        return int(row["total"])

    def log_run(
        self,
        *,
        stage: str,
        model: str,
        tokens: int | None,
        cost_usd: float | None,
        duration_ms: int,
        input_digest: str,
        transport: str = "claude",
        created_at: str | datetime | None = None,
    ) -> int:
        """Append accounting for one LLM call."""

        with self._write_transaction():
            cursor = self.connection.execute(
                """
                INSERT INTO run_log (
                    stage, model, transport, tokens, cost_usd, duration_ms, input_digest, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    stage,
                    model,
                    transport,
                    tokens,
                    cost_usd,
                    duration_ms,
                    input_digest,
                    _timestamp(created_at),
                ),
            )
            return int(cursor.lastrowid)

    def get_meta(self, key: str) -> str | None:
        """Read one small durable pipeline metadata value."""

        row = self.connection.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row is not None else None

    def set_meta(self, key: str, value: str) -> None:
        """Atomically create or replace one small pipeline metadata value."""

        if not key.strip():
            raise LedgerError("meta key cannot be empty")
        with self._write_transaction():
            self.connection.execute(
                """
                INSERT INTO meta (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )

    def apply_curator_incident_verdict(
        self,
        incident_id: int,
        verdict: str,
        *,
        reason: str,
        reassign_label: str | None = None,
        singleton: bool = False,
        timestamp: str | datetime | None = None,
    ) -> None:
        """Apply and record one semantically valid Curator verdict atomically."""

        if verdict not in {"promote", "park", "dismiss", "reassign", "resurrect"}:
            raise LedgerError(f"unknown Curator incident verdict {verdict!r}")
        if not reason.strip():
            raise LedgerError("a Curator verdict requires a non-empty reason")
        if verdict == "reassign" and not (reassign_label and reassign_label.strip()):
            raise LedgerError("reassign verdict requires reassign_label")
        if verdict not in {"reassign", "resurrect"} and reassign_label is not None:
            raise LedgerError(f"{verdict} verdict cannot also reassign a label")
        if singleton and verdict != "promote":
            raise LedgerError("singleton provenance is only valid for promotion")

        changed_at = _timestamp(timestamp)
        with self._write_transaction():
            row = self.connection.execute(
                "SELECT state, label FROM incident WHERE id = ?", (incident_id,)
            ).fetchone()
            if row is None:
                raise IncidentNotFoundError(f"incident {incident_id} does not exist")
            state = str(row["state"])
            previous_label = row["label"]

            if state == "open":
                if verdict == "promote":
                    self._transition_in_transaction(incident_id, "promoted", reason, changed_at)
                elif verdict == "park":
                    self._transition_in_transaction(incident_id, "parked", reason, changed_at)
                elif verdict == "dismiss":
                    self._transition_in_transaction(
                        incident_id, "dismissed-reviewed", reason, changed_at
                    )
                elif verdict == "reassign":
                    self.connection.execute(
                        "UPDATE incident SET label = ? WHERE id = ?",
                        (reassign_label, incident_id),
                    )
                else:
                    raise IllegalTransitionError(incident_id, state, "open")
            elif state == "dismissed-triage":
                if verdict == "resurrect":
                    if reassign_label is not None:
                        self.connection.execute(
                            "UPDATE incident SET label = ? WHERE id = ?",
                            (reassign_label, incident_id),
                        )
                    self._transition_in_transaction(incident_id, "open", reason, changed_at)
                elif verdict != "dismiss":
                    target = {
                        "promote": "promoted",
                        "park": "parked",
                        "reassign": state,
                    }.get(verdict, state)
                    raise IllegalTransitionError(incident_id, state, target)
                # ``dismiss`` upholds the existing triage dismissal without a state change.
            else:
                target = {
                    "promote": "promoted",
                    "park": "parked",
                    "dismiss": "dismissed-reviewed",
                    "reassign": state,
                    "resurrect": "open",
                }[verdict]
                raise IllegalTransitionError(incident_id, state, target)

            self.connection.execute(
                """
                INSERT INTO curator_incident_decision (
                    incident_id, verdict, reason, previous_label, reassign_label,
                    singleton, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    incident_id,
                    verdict,
                    reason,
                    previous_label,
                    reassign_label,
                    int(singleton),
                    changed_at,
                ),
            )

    def record_curator_cluster_verdict(
        self,
        label: str,
        verdict: str,
        *,
        reason: str,
        timestamp: str | datetime | None = None,
    ) -> None:
        """Append a cluster judgment, including durable unworthy/hold reasons."""

        if verdict not in {"synthesize", "hold", "unworthy"}:
            raise LedgerError(f"unknown Curator cluster verdict {verdict!r}")
        if not label.strip() or not reason.strip():
            raise LedgerError("a cluster verdict requires a label and reason")
        with self._write_transaction():
            self.connection.execute(
                """
                INSERT INTO curator_cluster_decision (label, verdict, reason, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (label, verdict, reason, _timestamp(timestamp)),
            )

    def curator_decisions(self, incident_id: int | None = None) -> list[CuratorDecision]:
        """Return append-only incident decisions, optionally for one incident."""

        if incident_id is None:
            rows = self.connection.execute(
                "SELECT * FROM curator_incident_decision ORDER BY id"
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM curator_incident_decision WHERE incident_id = ? ORDER BY id",
                (incident_id,),
            ).fetchall()
        return [
            CuratorDecision(
                id=int(row["id"]),
                incident_id=int(row["incident_id"]),
                verdict=str(row["verdict"]),
                reason=str(row["reason"]),
                previous_label=row["previous_label"],
                reassign_label=row["reassign_label"],
                singleton=bool(row["singleton"]),
                created_at=str(row["created_at"]),
            )
            for row in rows
        ]

    def curator_cluster_decisions(self) -> list[CuratorClusterDecision]:
        """Return all append-only cluster decisions."""

        rows = self.connection.execute(
            "SELECT * FROM curator_cluster_decision ORDER BY id"
        ).fetchall()
        return [
            CuratorClusterDecision(
                id=int(row["id"]),
                label=str(row["label"]),
                verdict=str(row["verdict"]),
                reason=str(row["reason"]),
                created_at=str(row["created_at"]),
            )
            for row in rows
        ]

    def create_proposal(
        self,
        *,
        remedy_type: str,
        drafted_content: str,
        evidence_incident_ids: Sequence[int],
        dedup_verdict: str,
        gate_status: str,
        install_record_ref: str | None = None,
        revises: str | None = None,
        singleton: bool = False,
        proposal_kind: str = "remedy",
        target_remedy_id: int | None = None,
        created_at: str | datetime | None = None,
    ) -> int:
        """Append a proposal and immutable normalized links to its evidence incidents."""

        evidence_ids = list(dict.fromkeys(evidence_incident_ids))
        if not evidence_ids:
            raise LedgerError("a proposal requires at least one evidence incident")
        if gate_status not in PROPOSAL_STATES:
            raise LedgerError(f"unknown proposal gate status {gate_status!r}")
        if proposal_kind not in {"remedy", "revision", "retirement"}:
            raise LedgerError(f"unknown proposal kind {proposal_kind!r}")
        if proposal_kind in {"revision", "retirement"} and target_remedy_id is None:
            raise LedgerError(f"{proposal_kind} proposals require a target remedy")
        with self._write_transaction():
            cursor = self.connection.execute(
                """
                INSERT INTO proposal (
                    remedy_type, drafted_content, evidence_incident_ids, dedup_verdict,
                    gate_status, install_record_ref, revises, singleton, created_at,
                    proposal_kind, target_remedy_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    remedy_type,
                    drafted_content,
                    json.dumps(evidence_ids, separators=(",", ":")),
                    dedup_verdict,
                    gate_status,
                    install_record_ref,
                    revises,
                    int(singleton),
                    _timestamp(created_at),
                    proposal_kind,
                    target_remedy_id,
                ),
            )
            proposal_id = int(cursor.lastrowid)
            self.connection.executemany(
                "INSERT INTO proposal_evidence (proposal_id, incident_id) VALUES (?, ?)",
                [(proposal_id, incident_id) for incident_id in evidence_ids],
            )
        return proposal_id

    def create_synthesis_proposals(
        self,
        proposals: Sequence[dict[str, object]],
        *,
        promoted_incident_ids: Sequence[int],
    ) -> list[int]:
        """Atomically store a validated synthesis result and consume its promotion group.

        Callers must fully validate split partitions before this method.  The state
        changes are deliberately in the same transaction as proposal insertion: an
        interrupted run remains entirely ``promoted`` and can be resumed safely.
        """

        promoted = list(dict.fromkeys(promoted_incident_ids))
        if not promoted:
            raise LedgerError("a synthesis group requires promoted incidents")
        if not proposals:
            raise LedgerError("synthesis requires at least one proposal")
        proposal_ids: list[int] = []
        with self._write_transaction():
            rows = self.connection.execute(
                f"SELECT id, state FROM incident WHERE id IN ({', '.join('?' for _ in promoted)})",
                promoted,
            ).fetchall()
            if len(rows) != len(promoted) or any(row["state"] != "promoted" for row in rows):
                raise LedgerError("synthesis incidents must all still be promoted")
            for proposal in proposals:
                evidence = proposal["evidence_incident_ids"]
                if not isinstance(evidence, list) or not evidence:
                    raise LedgerError("a synthesis proposal requires evidence")
                cursor = self.connection.execute(
                    """
                    INSERT INTO proposal (
                        remedy_type, drafted_content, evidence_incident_ids, dedup_verdict,
                        gate_status, install_record_ref, revises, singleton, created_at
                    ) VALUES (?, ?, ?, ?, 'pending', NULL, ?, ?, ?)
                    """,
                    (
                        proposal["remedy_type"],
                        proposal["drafted_content"],
                        json.dumps(evidence, separators=(",", ":")),
                        proposal["dedup_verdict"],
                        proposal.get("revises"),
                        int(bool(proposal.get("singleton", False))),
                        _utc_now(),
                    ),
                )
                proposal_id = int(cursor.lastrowid)
                proposal_ids.append(proposal_id)
                self.connection.executemany(
                    "INSERT INTO proposal_evidence (proposal_id, incident_id) VALUES (?, ?)",
                    [(proposal_id, incident_id) for incident_id in evidence],
                )
            for incident_id in promoted:
                self._transition_in_transaction(
                    incident_id,
                    "in-proposal",
                    "synthesis proposal drafted",
                    None,
                )
        return proposal_ids

    def create_remedy(
        self,
        *,
        artifact_type: str,
        artifact_path: str,
        proposal_id: int,
        artifact_digest: str | None = None,
        installed_at: str | datetime | None = None,
        state: str = "installed",
        managed_remedy_id: int | None = None,
        revises_remedy_id: int | None = None,
        state_record_ref: str | None = None,
        provenance: str = "human",
    ) -> int:
        """Append an installed-remedy record linked back to its proposal provenance."""

        with self._write_transaction():
            if state not in {"approved", "installed", "rolled-back"}:
                raise LedgerError(f"unknown remedy state {state!r}")
            if provenance not in {"human", "auto"}:
                raise LedgerError(f"unknown remedy provenance {provenance!r}")
            cursor = self.connection.execute(
                """
                INSERT INTO remedy (
                    artifact_type, artifact_path, artifact_digest, proposal_id,
                    installed_at, state, managed_remedy_id, revises_remedy_id,
                    state_record_ref, provenance
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact_type,
                    artifact_path,
                    artifact_digest,
                    proposal_id,
                    _timestamp(installed_at),
                    state,
                    managed_remedy_id,
                    revises_remedy_id,
                    state_record_ref,
                    provenance,
                ),
            )
            remedy_id = int(cursor.lastrowid)
            if managed_remedy_id is None:
                self.connection.execute(
                    "UPDATE remedy SET managed_remedy_id = ? WHERE id = ?",
                    (remedy_id, remedy_id),
                )
            return remedy_id

    def get_remedy(self, remedy_id: int) -> Remedy | None:
        """Return one remedy lifecycle record."""

        row = self.connection.execute(
            "SELECT * FROM remedy WHERE id = ?", (remedy_id,)
        ).fetchone()
        return self._remedy_from_row(row) if row is not None else None

    def installed_remedies(self) -> list[Remedy]:
        """Return remedies still installed and therefore eligible for audit."""

        rows = self.connection.execute(
            "SELECT * FROM remedy WHERE state = 'installed' ORDER BY id"
        ).fetchall()
        return [self._remedy_from_row(row) for row in rows]

    def remedy_labels(self, remedy_id: int) -> tuple[str, ...]:
        """Return the original evidence clusters for one remedy, without global mixing."""

        rows = self.connection.execute(
            """
            SELECT DISTINCT incident.label
            FROM remedy
            JOIN proposal_evidence ON proposal_evidence.proposal_id = remedy.proposal_id
            JOIN incident ON incident.id = proposal_evidence.incident_id
            WHERE remedy.id = ? AND incident.label IS NOT NULL
            ORDER BY incident.label
            """,
            (remedy_id,),
        ).fetchall()
        return tuple(str(row["label"]) for row in rows)

    def audit_incidents(self, label: str, *, after: str | None = None, before: str | None = None) -> list[Incident]:
        """Read one cluster's incidents in a half-open time window for outcome math."""

        clauses = ["label = ?"]
        values: list[object] = [label]
        if after is not None:
            clauses.append("occurred_at >= ?")
            values.append(after)
        if before is not None:
            clauses.append("occurred_at < ?")
            values.append(before)
        rows = self.connection.execute(
            f"SELECT * FROM incident WHERE {' AND '.join(clauses)} ORDER BY occurred_at, id",
            values,
        ).fetchall()
        return [self._incident_from_row(row) for row in rows]

    def record_remedy_outcome(
        self,
        remedy_id: int,
        *,
        verdict: str,
        pre_rate: float | None,
        post_rate: float | None,
        assessed_at: str | datetime | None = None,
    ) -> None:
        """Store the latest audit summary; raw evidence remains queryable in the ledger."""

        if verdict not in {"effective", "persisting", "silent", "insufficient-data"}:
            raise LedgerError(f"unknown remedy outcome verdict {verdict!r}")
        with self._write_transaction():
            result = self.connection.execute(
                """
                UPDATE remedy
                SET outcome_verdict = ?, outcome_pre_rate = ?, outcome_post_rate = ?,
                    outcome_assessed_at = ?
                WHERE id = ?
                """,
                (verdict, pre_rate, post_rate, _timestamp(assessed_at), remedy_id),
            )
            if result.rowcount != 1:
                raise LedgerError(f"remedy {remedy_id} does not exist")

    def audit_proposal_exists(self, remedy_id: int, proposal_kind: str) -> bool:
        """Avoid re-queueing the same outstanding audit action on every pass."""

        row = self.connection.execute(
            """
            SELECT 1 FROM proposal
            WHERE target_remedy_id = ? AND proposal_kind = ?
              AND gate_status IN ('pending', 'approved')
            LIMIT 1
            """,
            (remedy_id, proposal_kind),
        ).fetchone()
        return row is not None

    def remedy_for_proposal(self, proposal_id: int) -> Remedy | None:
        """Return the newest remedy reserved for a proposal, if any."""

        row = self.connection.execute(
            "SELECT * FROM remedy WHERE proposal_id = ? ORDER BY id DESC LIMIT 1",
            (proposal_id,),
        ).fetchone()
        return self._remedy_from_row(row) if row is not None else None

    def mark_remedy_installed(
        self,
        remedy_id: int,
        *,
        artifact_digest: str,
        state_record_ref: str,
        timestamp: str | datetime | None = None,
    ) -> None:
        """Finalize a reserved remedy and remediate all of its proposal evidence."""

        changed_at = _timestamp(timestamp)
        with self._write_transaction():
            row = self.connection.execute(
                "SELECT proposal_id, state FROM remedy WHERE id = ?", (remedy_id,)
            ).fetchone()
            if row is None:
                raise LedgerError(f"remedy {remedy_id} does not exist")
            if row["state"] != "approved":
                raise LedgerError(f"remedy {remedy_id} is {row['state']!r}, not approved")
            proposal_id = int(row["proposal_id"])
            self.connection.execute(
                """
                UPDATE remedy
                SET state = 'installed', artifact_digest = ?, state_record_ref = ?,
                    installed_at = ?
                WHERE id = ?
                """,
                (artifact_digest, state_record_ref, changed_at, remedy_id),
            )
            self.connection.execute(
                """
                UPDATE proposal
                SET gate_status = 'installed', install_record_ref = ?
                WHERE id = ? AND gate_status = 'approved'
                """,
                (state_record_ref, proposal_id),
            )
            incident_rows = self.connection.execute(
                """
                SELECT incident.id, incident.state
                FROM incident
                JOIN proposal_evidence ON proposal_evidence.incident_id = incident.id
                WHERE proposal_evidence.proposal_id = ?
                ORDER BY incident.id
                """,
                (proposal_id,),
            ).fetchall()
            for incident in incident_rows:
                if incident["state"] == "in-proposal":
                    self._transition_in_transaction(
                        int(incident["id"]),
                        "remedied",
                        f"remedy {remedy_id} installed",
                        changed_at,
                    )

    def mark_remedy_rolled_back(
        self,
        remedy_id: int,
        *,
        rollback_metadata: str,
        timestamp: str | datetime | None = None,
    ) -> None:
        """Move an installed remedy to its terminal rolled-back state."""

        changed_at = _timestamp(timestamp)
        with self._write_transaction():
            result = self.connection.execute(
                """
                UPDATE remedy
                SET state = 'rolled-back', rollback_at = ?, rollback_metadata = ?
                WHERE id = ? AND state = 'installed'
                """,
                (changed_at, rollback_metadata, remedy_id),
            )
            if result.rowcount != 1:
                row = self.connection.execute(
                    "SELECT state FROM remedy WHERE id = ?", (remedy_id,)
                ).fetchone()
                if row is None:
                    raise LedgerError(f"remedy {remedy_id} does not exist")
                raise LedgerError(f"remedy {remedy_id} is {row['state']!r}, not installed")

    def mark_retirement_proposal_completed(
        self, proposal_id: int, *, rollback_record_ref: str
    ) -> None:
        """Finalize a Gate-approved retirement after its rollback succeeds."""

        with self._write_transaction():
            result = self.connection.execute(
                """
                UPDATE proposal
                SET gate_status = 'installed', install_record_ref = ?
                WHERE id = ? AND proposal_kind = 'retirement' AND gate_status = 'approved'
                """,
                (rollback_record_ref, proposal_id),
            )
            if result.rowcount != 1:
                raise LedgerError(f"retirement proposal {proposal_id} is not approved")

    def cluster_stats(self) -> list[ClusterStats]:
        """Derive cluster counts, project spread, time range, and linked remedies by label."""

        rows = self.connection.execute(
            """
            SELECT
                label,
                COUNT(*) AS incident_count,
                COUNT(DISTINCT project) AS project_count,
                MIN(occurred_at) AS first_seen,
                MAX(occurred_at) AS last_seen
            FROM incident
            WHERE label IS NOT NULL
            GROUP BY label
            ORDER BY label
            """
        ).fetchall()
        remedy_rows = self.connection.execute(
            """
            SELECT DISTINCT incident.label, remedy.id AS remedy_id
            FROM incident
            JOIN proposal_evidence ON proposal_evidence.incident_id = incident.id
            JOIN remedy ON remedy.proposal_id = proposal_evidence.proposal_id
            WHERE incident.label IS NOT NULL
            ORDER BY incident.label, remedy.id
            """
        ).fetchall()
        remedies_by_label: dict[str, list[int]] = {}
        for row in remedy_rows:
            remedies_by_label.setdefault(str(row["label"]), []).append(int(row["remedy_id"]))

        return [
            ClusterStats(
                label=str(row["label"]),
                incident_count=int(row["incident_count"]),
                project_count=int(row["project_count"]),
                first_seen=str(row["first_seen"]),
                last_seen=str(row["last_seen"]),
                remedy_ids=tuple(remedies_by_label.get(str(row["label"]), [])),
            )
            for row in rows
        ]

    clusters = cluster_stats

    def singleton_ratio(self) -> float:
        """Return the fraction of labelled clusters containing exactly one incident."""

        clusters = self.cluster_stats()
        if not clusters:
            return 0.0
        return sum(cluster.incident_count == 1 for cluster in clusters) / len(clusters)

    def active_cluster_count(self) -> int:
        """Return the number of labelled clusters participating in the pipeline."""

        return len(self.cluster_stats())

    def other_share(self) -> float:
        """Return the fraction of labelled incidents assigned to the ``other`` escape hatch."""

        row = self.connection.execute(
            """
            SELECT COUNT(*) AS labelled_count,
                   SUM(CASE WHEN label = 'other' THEN 1 ELSE 0 END) AS other_count
            FROM incident
            WHERE label IS NOT NULL
            """
        ).fetchone()
        labelled_count = int(row["labelled_count"])
        other_count = int(row["other_count"] or 0)
        return other_count / labelled_count if labelled_count else 0.0

    @staticmethod
    def _incident_from_row(row: sqlite3.Row) -> Incident:
        confidence = row["confidence"]
        return Incident(
            id=int(row["id"]),
            source=str(row["source"]),
            session_id=str(row["session_id"]),
            project=str(row["project"]),
            occurred_at=str(row["occurred_at"]),
            created_at=str(row["created_at"]),
            message=str(row["message"]),
            context_pack_pointer=row["context_pack_pointer"],
            label=row["label"],
            one_liner=row["one_liner"],
            severity=row["severity"],
            confidence=float(confidence) if confidence is not None else None,
            state=str(row["state"]),
        )

    @staticmethod
    def _proposal_from_row(row: sqlite3.Row) -> Proposal:
        return Proposal(
            id=int(row["id"]),
            remedy_type=str(row["remedy_type"]),
            drafted_content=str(row["drafted_content"]),
            evidence_incident_ids=tuple(int(item) for item in json.loads(row["evidence_incident_ids"])),
            dedup_verdict=str(row["dedup_verdict"]),
            gate_status=str(row["gate_status"]),
            install_record_ref=row["install_record_ref"],
            revises=row["revises"],
            singleton=bool(row["singleton"]),
            rejection_reason=row["rejection_reason"],
            approved_at=row["approved_at"],
            decided_at=row["decided_at"],
            autonomy_decision=row["autonomy_decision"],
            autonomy_decided_at=row["autonomy_decided_at"],
            created_at=str(row["created_at"]),
            proposal_kind=str(row["proposal_kind"]),
            target_remedy_id=(
                int(row["target_remedy_id"])
                if row["target_remedy_id"] is not None
                else None
            ),
        )

    @staticmethod
    def _remedy_from_row(row: sqlite3.Row) -> Remedy:
        managed_id = row["managed_remedy_id"]
        return Remedy(
            id=int(row["id"]),
            artifact_type=str(row["artifact_type"]),
            artifact_path=str(row["artifact_path"]),
            artifact_digest=row["artifact_digest"],
            proposal_id=int(row["proposal_id"]),
            installed_at=str(row["installed_at"]),
            rollback_at=row["rollback_at"],
            state=str(row["state"]),
            managed_remedy_id=int(managed_id) if managed_id is not None else int(row["id"]),
            revises_remedy_id=(
                int(row["revises_remedy_id"])
                if row["revises_remedy_id"] is not None
                else None
            ),
            state_record_ref=row["state_record_ref"],
            outcome_verdict=row["outcome_verdict"],
            outcome_pre_rate=(
                float(row["outcome_pre_rate"])
                if row["outcome_pre_rate"] is not None
                else None
            ),
            outcome_post_rate=(
                float(row["outcome_post_rate"])
                if row["outcome_post_rate"] is not None
                else None
            ),
            outcome_assessed_at=row["outcome_assessed_at"],
            provenance=str(row["provenance"]),
        )

    @staticmethod
    def _queue_item_from_row(row: sqlite3.Row) -> QueueItem:
        return QueueItem(
            id=int(row["id"]),
            item_path=str(row["item_path"]),
            source=str(row["source"]),
            enqueued_at=str(row["enqueued_at"]),
            processed_at=row["processed_at"],
        )

    @staticmethod
    def _session_stats_from_row(row: sqlite3.Row) -> SessionStats:
        return SessionStats(
            source=str(row["source"]),
            session_id=str(row["session_id"]),
            project=str(row["project"]),
            dominant_model=row["dominant_model"],
            direct_message_count=int(row["direct_message_count"]),
            hit_count=int(row["hit_count"]),
            first_timestamp=row["first_timestamp"],
            last_timestamp=row["last_timestamp"],
            scanned_at=str(row["scanned_at"]),
        )


def open_ledger(path: Path | str | None = None) -> Ledger:
    """Create a :class:`Ledger` at ``resolve_paths().ledger_path`` by default."""

    return Ledger(path)
