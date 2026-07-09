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


SCHEMA_VERSION = 2
BUSY_TIMEOUT_MS = 5_000

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
class ClusterStats:
    """Derived statistics for an implicit cluster (all incidents sharing a label)."""

    label: str
    incident_count: int
    project_count: int
    first_seen: str
    last_seen: str
    remedy_ids: tuple[int, ...]


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
    # A direct user message is the scanner unit of work.  This makes manual
    # re-scans and a retried queue item safe even after a process interruption,
    # without imposing scanner semantics on incidents written by later stages.
    connection.execute(
        """
        CREATE UNIQUE INDEX scan_dedup_key_idx
        ON scan_dedup (session_id, timestamp, message)
        """
    )


MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {
    1: _migration_1,
    2: _migration_2,
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
            outcome_reason = "triage dismissed" if dismissed else "triage accepted"
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

    def parked_incidents(self, label: str) -> list[Incident]:
        """Return parked incidents for one label, including all durable metadata."""

        rows = self.connection.execute(
            "SELECT * FROM incident WHERE state = 'parked' AND label = ? ORDER BY occurred_at, id",
            (label,),
        ).fetchall()
        return [self._incident_from_row(row) for row in rows]

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

    def log_run(
        self,
        *,
        stage: str,
        model: str,
        tokens: int,
        cost_usd: float,
        duration_ms: int,
        input_digest: str,
        created_at: str | datetime | None = None,
    ) -> int:
        """Append accounting for one LLM call."""

        with self._write_transaction():
            cursor = self.connection.execute(
                """
                INSERT INTO run_log (stage, model, tokens, cost_usd, duration_ms, input_digest, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (stage, model, tokens, cost_usd, duration_ms, input_digest, _timestamp(created_at)),
            )
            return int(cursor.lastrowid)

    def create_proposal(
        self,
        *,
        remedy_type: str,
        drafted_content: str,
        evidence_incident_ids: Sequence[int],
        dedup_verdict: str,
        gate_status: str,
        install_record_ref: str | None = None,
        created_at: str | datetime | None = None,
    ) -> int:
        """Append a proposal and immutable normalized links to its evidence incidents."""

        evidence_ids = list(dict.fromkeys(evidence_incident_ids))
        if not evidence_ids:
            raise LedgerError("a proposal requires at least one evidence incident")
        with self._write_transaction():
            cursor = self.connection.execute(
                """
                INSERT INTO proposal (
                    remedy_type, drafted_content, evidence_incident_ids, dedup_verdict,
                    gate_status, install_record_ref, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    remedy_type,
                    drafted_content,
                    json.dumps(evidence_ids, separators=(",", ":")),
                    dedup_verdict,
                    gate_status,
                    install_record_ref,
                    _timestamp(created_at),
                ),
            )
            proposal_id = int(cursor.lastrowid)
            self.connection.executemany(
                "INSERT INTO proposal_evidence (proposal_id, incident_id) VALUES (?, ?)",
                [(proposal_id, incident_id) for incident_id in evidence_ids],
            )
        return proposal_id

    def create_remedy(
        self,
        *,
        artifact_type: str,
        artifact_path: str,
        proposal_id: int,
        artifact_digest: str | None = None,
        installed_at: str | datetime | None = None,
    ) -> int:
        """Append an installed-remedy record linked back to its proposal provenance."""

        with self._write_transaction():
            cursor = self.connection.execute(
                """
                INSERT INTO remedy (artifact_type, artifact_path, artifact_digest, proposal_id, installed_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (artifact_type, artifact_path, artifact_digest, proposal_id, _timestamp(installed_at)),
            )
            return int(cursor.lastrowid)

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
        if labelled_count == 0:
            return 0.0
        return int(row["other_count"]) / labelled_count

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
