from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Sequence

from .workflow import architect_dispatches, lifecycle


APPLICATION_ID = 0x52434953
SCHEMA_VERSION = 1
SCHEMA_ID = "repoctl.information-store:v1"
MINIMUM_SQLITE_VERSION = (3, 51, 3)
BUSY_TIMEOUT_MS = 250
MAX_EVENT_COUNT = 2000
MAX_EVENT_BATCH_BYTES = 5_000_000
MAX_EVENT_PAGE_SIZE = 100
MAX_DOCUMENT_LIST_SIZE = 100
MAX_DOCUMENT_REVISION_LIST_SIZE = 100
MAX_DOCUMENT_PAGE_BYTES = 64 * 1024
MAX_DOCUMENT_BYTES = 5_000_000
MAX_DISPATCH_COUNT = 500
_MILESTONE_ID_RE = re.compile(r"(?:M[0-9]{3}(?:\.[0-9]{3})+|R[0-9]{3}\.[0-9]{2})\Z")
_DOCUMENT_TYPES = {
    "PROMPT",
    "CLOSEOUT",
    "PROJECT_CONTEXT",
    "ARCHITECTURE",
    "WORKFLOW",
    "CODING_RULES",
    "PARKING_LOT",
    "PROCEDURE",
    "OTHER",
}
_PROTECTED_WORKFLOW_EVENTS = {
    "prompt_approved",
    "lock_in_approved",
    "acceptance_recorded",
    "milestone_closed",
    "milestone_reopened",
}
_WORKFLOW_LINK_ROLES = {"prompt", "closeout", "related"}
_DISPATCH_TYPES = {"PROMPT", "INSTRUCTION", "RESPONSE"}
_DISPATCH_STATUSES = {"PENDING", "APPROVED", "REJECTED"}
_PUBLICATION_STATUSES = {
    "accepted_not_published",
    "preview_ready",
    "materialized_unstaged",
    "staged",
    "committed",
    "failed",
    "drifted",
}


class InformationStoreError(RuntimeError):
    pass


class RuntimeGateError(InformationStoreError):
    pass


class SchemaMismatchError(InformationStoreError):
    pass


class IdentityMismatchError(InformationStoreError):
    pass


class StaleStateError(InformationStoreError):
    pass


class RevisionConflictError(InformationStoreError):
    pass


class IdempotencyConflictError(InformationStoreError):
    pass


class IntegrityFailureError(InformationStoreError):
    pass


class BusyStoreError(InformationStoreError):
    pass


class ImportSourceError(InformationStoreError):
    pass


class DispatchDecisionError(InformationStoreError):
    pass


@dataclass(frozen=True)
class StoreIdentity:
    repository_id: str
    repository_key: str
    repository_root: str
    milestone_id: str
    prompt_path: str
    selection_id: str
    selection_revision: int


@dataclass(frozen=True)
class DocumentRevisionInput:
    document_id: str
    revision_id: str
    logical_key: str
    logical_type: str
    source_path: str
    content: str
    parent_revision_id: str | None = None
    source_kind: str = "fixture"
    provenance: dict[str, Any] | None = None


@dataclass(frozen=True)
class WorkflowDocumentLink:
    event_id: str
    document_id: str
    revision_id: str
    content_sha256: str
    link_role: str
    evidence: str


@dataclass(frozen=True)
class V1FixtureBundle:
    prompt_path: str
    closeout_path: str
    workflow_path: str
    prompt_bytes: bytes
    closeout_bytes: bytes
    workflow_bytes: bytes
    dispatch_path: str | None = None
    dispatch_bytes: bytes | None = None


_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE store_metadata (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE repository (
        repository_id TEXT PRIMARY KEY,
        repository_key TEXT NOT NULL,
        repository_root TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE milestone (
        repository_id TEXT NOT NULL,
        milestone_id TEXT NOT NULL,
        prompt_path TEXT NOT NULL,
        prompt_revision_id TEXT,
        PRIMARY KEY (repository_id, milestone_id),
        FOREIGN KEY (repository_id) REFERENCES repository(repository_id)
    )
    """,
    """
    CREATE TABLE repository_selection (
        repository_id TEXT PRIMARY KEY,
        milestone_id TEXT NOT NULL,
        selection_id TEXT NOT NULL,
        selection_revision INTEGER NOT NULL CHECK (selection_revision >= 0),
        FOREIGN KEY (repository_id, milestone_id)
            REFERENCES milestone(repository_id, milestone_id)
    )
    """,
    """
    CREATE TABLE workflow_state (
        repository_id TEXT NOT NULL,
        milestone_id TEXT NOT NULL,
        store_revision INTEGER NOT NULL CHECK (store_revision >= 0),
        event_count INTEGER NOT NULL CHECK (event_count >= 0),
        projection_json TEXT NOT NULL,
        PRIMARY KEY (repository_id, milestone_id),
        FOREIGN KEY (repository_id, milestone_id)
            REFERENCES milestone(repository_id, milestone_id)
    )
    """,
    """
    CREATE TABLE document (
        document_order INTEGER PRIMARY KEY AUTOINCREMENT,
        document_id TEXT NOT NULL UNIQUE,
        repository_id TEXT NOT NULL,
        milestone_id TEXT NOT NULL,
        logical_key TEXT NOT NULL,
        logical_type TEXT NOT NULL,
        source_path TEXT NOT NULL,
        draft_revision_id TEXT,
        approved_revision_id TEXT,
        UNIQUE (repository_id, milestone_id, logical_key),
        FOREIGN KEY (repository_id, milestone_id)
            REFERENCES milestone(repository_id, milestone_id),
        FOREIGN KEY (document_id, draft_revision_id)
            REFERENCES document_revision(document_id, revision_id),
        FOREIGN KEY (document_id, approved_revision_id)
            REFERENCES document_revision(document_id, revision_id)
    )
    """,
    """
    CREATE TABLE document_revision (
        revision_order INTEGER PRIMARY KEY AUTOINCREMENT,
        revision_id TEXT NOT NULL UNIQUE,
        document_id TEXT NOT NULL,
        repository_id TEXT NOT NULL,
        milestone_id TEXT NOT NULL,
        parent_revision_id TEXT,
        content TEXT NOT NULL,
        content_sha256 TEXT NOT NULL,
        source_kind TEXT NOT NULL,
        provenance_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE (document_id, revision_id),
        UNIQUE (document_id, revision_id, content_sha256),
        UNIQUE (repository_id, milestone_id, revision_id, content_sha256),
        FOREIGN KEY (document_id) REFERENCES document(document_id),
        FOREIGN KEY (document_id, parent_revision_id)
            REFERENCES document_revision(document_id, revision_id),
        FOREIGN KEY (repository_id, milestone_id)
            REFERENCES milestone(repository_id, milestone_id)
    )
    """,
    """
    CREATE TABLE document_revision_decision (
        decision_id TEXT PRIMARY KEY,
        repository_id TEXT NOT NULL,
        milestone_id TEXT NOT NULL,
        document_id TEXT NOT NULL,
        revision_id TEXT NOT NULL,
        decision_type TEXT NOT NULL CHECK (decision_type IN ('APPROVED', 'REJECTED', 'IMPORTED_SNAPSHOT')),
        actor_role TEXT NOT NULL,
        actor_attribution TEXT NOT NULL,
        po_confirmed INTEGER,
        source_kind TEXT NOT NULL,
        decided_at TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        FOREIGN KEY (document_id, revision_id)
            REFERENCES document_revision(document_id, revision_id),
        FOREIGN KEY (repository_id, milestone_id)
            REFERENCES milestone(repository_id, milestone_id)
    )
    """,
    """
    CREATE TABLE workflow_event (
        repository_id TEXT NOT NULL,
        milestone_id TEXT NOT NULL,
        sequence INTEGER NOT NULL CHECK (sequence > 0),
        event_id TEXT NOT NULL,
        event_type TEXT NOT NULL,
        event_timestamp TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        payload_sha256 TEXT NOT NULL,
        request_id TEXT,
        PRIMARY KEY (repository_id, milestone_id, sequence),
        UNIQUE (repository_id, milestone_id, event_id),
        FOREIGN KEY (repository_id, milestone_id)
            REFERENCES milestone(repository_id, milestone_id)
    )
    """,
    """
    CREATE INDEX workflow_event_type_sequence
        ON workflow_event(repository_id, milestone_id, event_type, sequence)
    """,
    """
    CREATE TABLE workflow_document_link (
        repository_id TEXT NOT NULL,
        milestone_id TEXT NOT NULL,
        event_id TEXT NOT NULL,
        document_id TEXT NOT NULL,
        revision_id TEXT NOT NULL,
        content_sha256 TEXT NOT NULL,
        link_role TEXT NOT NULL CHECK (link_role IN ('prompt', 'closeout', 'related')),
        evidence TEXT NOT NULL,
        PRIMARY KEY (repository_id, milestone_id, event_id, link_role),
        FOREIGN KEY (repository_id, milestone_id, event_id)
            REFERENCES workflow_event(repository_id, milestone_id, event_id),
        FOREIGN KEY (repository_id, milestone_id, revision_id, content_sha256)
            REFERENCES document_revision(repository_id, milestone_id, revision_id, content_sha256)
    )
    """,
    """
    CREATE TABLE transaction_receipt (
        repository_id TEXT NOT NULL,
        milestone_id TEXT NOT NULL,
        request_id TEXT NOT NULL,
        request_hash TEXT NOT NULL,
        resulting_revision INTEGER NOT NULL,
        result_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (repository_id, milestone_id, request_id),
        FOREIGN KEY (repository_id, milestone_id)
            REFERENCES milestone(repository_id, milestone_id)
    )
    """,
    """
    CREATE TABLE architect_dispatch (
        repository_id TEXT NOT NULL,
        dispatch_id TEXT NOT NULL,
        milestone_id TEXT NOT NULL,
        dispatch_type TEXT NOT NULL CHECK (dispatch_type IN ('PROMPT', 'INSTRUCTION', 'RESPONSE')),
        status TEXT NOT NULL CHECK (status IN ('PENDING', 'APPROVED', 'REJECTED')),
        body TEXT NOT NULL,
        title TEXT,
        approved_milestone_id TEXT,
        related_event_id TEXT,
        source_sha256 TEXT,
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (repository_id, dispatch_id),
        FOREIGN KEY (repository_id, milestone_id)
            REFERENCES milestone(repository_id, milestone_id)
    )
    """,
    """
    CREATE TABLE dispatch_history (
        repository_id TEXT NOT NULL,
        dispatch_id TEXT NOT NULL,
        sequence INTEGER NOT NULL CHECK (sequence > 0),
        history_id TEXT NOT NULL,
        record_kind TEXT NOT NULL CHECK (record_kind IN ('CREATED', 'DECISION', 'LEGACY_SNAPSHOT')),
        from_status TEXT,
        to_status TEXT NOT NULL CHECK (to_status IN ('PENDING', 'APPROVED', 'REJECTED')),
        actor_role TEXT NOT NULL,
        actor_attribution TEXT NOT NULL,
        po_confirmed INTEGER,
        related_event_id TEXT,
        recorded_at TEXT NOT NULL,
        source_kind TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        PRIMARY KEY (repository_id, dispatch_id, sequence),
        UNIQUE (repository_id, history_id),
        FOREIGN KEY (repository_id, dispatch_id)
            REFERENCES architect_dispatch(repository_id, dispatch_id)
    )
    """,
    """
    CREATE TABLE import_source (
        source_id TEXT PRIMARY KEY,
        repository_id TEXT NOT NULL,
        milestone_id TEXT NOT NULL,
        source_kind TEXT NOT NULL,
        source_path TEXT NOT NULL,
        source_sha256 TEXT NOT NULL,
        source_bytes BLOB NOT NULL,
        imported_at TEXT NOT NULL,
        UNIQUE (repository_id, milestone_id, source_kind, source_path, source_sha256),
        FOREIGN KEY (repository_id, milestone_id)
            REFERENCES milestone(repository_id, milestone_id)
    )
    """,
    """
    CREATE TABLE document_publication (
        repository_id TEXT NOT NULL,
        milestone_id TEXT NOT NULL,
        publication_id TEXT NOT NULL,
        document_id TEXT NOT NULL,
        revision_id TEXT NOT NULL,
        content_sha256 TEXT NOT NULL,
        status TEXT NOT NULL CHECK (
            status IN (
                'accepted_not_published', 'preview_ready', 'materialized_unstaged',
                'staged', 'committed', 'failed', 'drifted'
            )
        ),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (repository_id, publication_id),
        FOREIGN KEY (repository_id, milestone_id, revision_id, content_sha256)
            REFERENCES document_revision(repository_id, milestone_id, revision_id, content_sha256)
    )
    """,
    """
    CREATE TRIGGER document_revision_no_update
    BEFORE UPDATE ON document_revision
    BEGIN SELECT RAISE(ABORT, 'document revisions are immutable'); END
    """,
    """
    CREATE TRIGGER document_revision_no_delete
    BEFORE DELETE ON document_revision
    BEGIN SELECT RAISE(ABORT, 'document revisions are immutable'); END
    """,
    """
    CREATE TRIGGER workflow_event_no_update
    BEFORE UPDATE ON workflow_event
    BEGIN SELECT RAISE(ABORT, 'workflow events are immutable'); END
    """,
    """
    CREATE TRIGGER workflow_event_no_delete
    BEFORE DELETE ON workflow_event
    BEGIN SELECT RAISE(ABORT, 'workflow events are immutable'); END
    """,
    """
    CREATE TRIGGER workflow_document_link_no_update
    BEFORE UPDATE ON workflow_document_link
    BEGIN SELECT RAISE(ABORT, 'workflow document links are immutable'); END
    """,
    """
    CREATE TRIGGER workflow_document_link_no_delete
    BEFORE DELETE ON workflow_document_link
    BEGIN SELECT RAISE(ABORT, 'workflow document links are immutable'); END
    """,
    """
    CREATE TRIGGER document_revision_decision_no_update
    BEFORE UPDATE ON document_revision_decision
    BEGIN SELECT RAISE(ABORT, 'document revision decisions are immutable'); END
    """,
    """
    CREATE TRIGGER document_revision_decision_no_delete
    BEFORE DELETE ON document_revision_decision
    BEGIN SELECT RAISE(ABORT, 'document revision decisions are immutable'); END
    """,
    """
    CREATE TRIGGER transaction_receipt_no_update
    BEFORE UPDATE ON transaction_receipt
    BEGIN SELECT RAISE(ABORT, 'transaction receipts are immutable'); END
    """,
    """
    CREATE TRIGGER transaction_receipt_no_delete
    BEFORE DELETE ON transaction_receipt
    BEGIN SELECT RAISE(ABORT, 'transaction receipts are immutable'); END
    """,
    """
    CREATE TRIGGER dispatch_history_no_update
    BEFORE UPDATE ON dispatch_history
    BEGIN SELECT RAISE(ABORT, 'dispatch history is immutable'); END
    """,
    """
    CREATE TRIGGER dispatch_history_no_delete
    BEFORE DELETE ON dispatch_history
    BEGIN SELECT RAISE(ABORT, 'dispatch history is immutable'); END
    """,
    """
    CREATE TRIGGER import_source_no_update
    BEFORE UPDATE ON import_source
    BEGIN SELECT RAISE(ABORT, 'import sources are immutable'); END
    """,
    """
    CREATE TRIGGER import_source_no_delete
    BEFORE DELETE ON import_source
    BEGIN SELECT RAISE(ABORT, 'import sources are immutable'); END
    """,
    """
    CREATE TRIGGER dispatch_no_delete
    BEFORE DELETE ON architect_dispatch
    BEGIN SELECT RAISE(ABORT, 'dispatch identities are retained'); END
    """,
)

_EXPECTED_TABLES = {
    "store_metadata",
    "repository",
    "milestone",
    "repository_selection",
    "workflow_state",
    "document",
    "document_revision",
    "document_revision_decision",
    "workflow_event",
    "workflow_document_link",
    "transaction_receipt",
    "architect_dispatch",
    "dispatch_history",
    "import_source",
    "document_publication",
}
_EXPECTED_TRIGGERS = {
    "document_revision_no_update",
    "document_revision_no_delete",
    "workflow_event_no_update",
    "workflow_event_no_delete",
    "workflow_document_link_no_update",
    "workflow_document_link_no_delete",
    "document_revision_decision_no_update",
    "document_revision_decision_no_delete",
    "transaction_receipt_no_update",
    "transaction_receipt_no_delete",
    "dispatch_history_no_update",
    "dispatch_history_no_delete",
    "import_source_no_update",
    "import_source_no_delete",
    "dispatch_no_delete",
}


def sqlite_runtime_status(
    version_info: Sequence[int] | None = None,
    *,
    fixed_backport_evidence: str | None = None,
) -> dict[str, Any]:
    reported = tuple(sqlite3.sqlite_version_info if version_info is None else version_info)
    if len(reported) != 3 or any(type(part) is not int or part < 0 for part in reported):
        raise ValueError("SQLite version must contain three non-negative integers.")
    eligible_by_version = reported >= MINIMUM_SQLITE_VERSION
    has_backport_evidence = isinstance(fixed_backport_evidence, str) and bool(fixed_backport_evidence.strip())
    return {
        "sqlite_version_info": reported,
        "sqlite_version": ".".join(str(part) for part in reported),
        "minimum_required": MINIMUM_SQLITE_VERSION,
        "production_eligible": eligible_by_version or has_backport_evidence,
        "eligibility_basis": (
            "minimum_version"
            if eligible_by_version
            else "explicit_fixed_backport_evidence"
            if has_backport_evidence
            else "below_minimum"
        ),
        "fixed_backport_evidence": fixed_backport_evidence.strip() if has_backport_evidence else None,
    }


def _json_text(value: Any, *, sort_keys: bool = True) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=sort_keys,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _validate_timestamp(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("A timestamp is required.")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("Timestamp must be ISO-8601 text with a UTC offset.") from exc
    if parsed.utcoffset() is None:
        raise ValueError("Timestamp must include a UTC offset.")
    return value


def _validate_relative_path(value: str, label: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise ValueError(f"{label} must be a non-empty POSIX relative path.")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"{label} must be a contained POSIX relative path.")
    return value


def _validate_store_identity(identity: StoreIdentity) -> None:
    for label, value in (
        ("repository ID", identity.repository_id),
        ("repository key", identity.repository_key),
        ("repository root", identity.repository_root),
        ("milestone ID", identity.milestone_id),
        ("selection ID", identity.selection_id),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{label} is required.")
    if not Path(identity.repository_root).is_absolute():
        raise ValueError("Repository root must be absolute.")
    if not _MILESTONE_ID_RE.fullmatch(identity.milestone_id):
        raise ValueError("Milestone ID is not in a supported canonical form.")
    _validate_relative_path(identity.prompt_path, "Prompt path")
    if type(identity.selection_revision) is not int or identity.selection_revision < 0:
        raise ValueError("Selection revision must be a non-negative integer.")


def _validate_database_path(value: str | Path, repository_root: str) -> Path:
    if str(value) == ":memory:":
        raise ValueError("Fixture databases require an explicit disposable filesystem path for WAL testing.")
    candidate = Path(value).expanduser()
    if candidate.is_symlink():
        raise ValueError("Disposable database path cannot be a symlink.")
    resolved = candidate.resolve(strict=False)
    root = Path(repository_root).expanduser().resolve(strict=False)
    if resolved == root or resolved.is_relative_to(root):
        raise ValueError("Disposable database must be outside the repository root.")
    if not resolved.parent.is_dir():
        raise ValueError("Disposable database parent directory must already exist.")
    if resolved.exists() and not resolved.is_file():
        raise ValueError("Disposable database path must be a regular file.")
    return resolved


def _projection(events: list[dict[str, Any]]) -> dict[str, Any]:
    state = lifecycle._reduce_events(events)
    return {
        "phase": state["phase"],
        "waiting_category": state["waiting_category"],
        "waiting_reason": state["waiting_reason"],
        "open_question_ids": sorted(lifecycle._open_question_ids(events)),
        "open_bug_ids": sorted(lifecycle._open_bug_ids(events)),
        "unfixed_bug_ids": sorted(lifecycle._unfixed_bug_ids(events)),
        "event_count": len(events),
    }


def _source_record_id(repository_id: str, milestone_id: str, kind: str, path: str, source_hash: str) -> str:
    return _sha256(_json_text([repository_id, milestone_id, kind, path, source_hash]).encode("utf-8"))


def _parse_sidecar_bytes(source: bytes, identity: StoreIdentity) -> list[dict[str, Any]]:
    try:
        text = source.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ImportSourceError("Workflow sidecar is not valid UTF-8.") from exc
    marker = "<!-- repoctl-workflow-ledger:v1 -->"
    if text.count(marker) != 1:
        raise ImportSourceError("Workflow sidecar must contain exactly one v1 ledger marker.")
    match = re.search(r"(?m)^(`{3,})json[ \t]*\n(.*?)\n\1[ \t]*$", text[text.index(marker) + len(marker) :], re.DOTALL)
    if match is None:
        raise ImportSourceError("Workflow sidecar has no valid JSON ledger block.")
    try:
        ledger = json.loads(match.group(2))
    except json.JSONDecodeError as exc:
        raise ImportSourceError(f"Workflow sidecar ledger JSON is malformed: {exc}") from exc
    if (
        not isinstance(ledger, dict)
        or ledger.get("schema_version") != lifecycle.WORKFLOW_LEDGER_SCHEMA_VERSION
        or ledger.get("repository_id") != identity.repository_id
        or ledger.get("milestone_id") != identity.milestone_id
        or ledger.get("prompt_path") != identity.prompt_path
        or not isinstance(ledger.get("events"), list)
        or len(ledger["events"]) > MAX_EVENT_COUNT
    ):
        raise ImportSourceError("Workflow sidecar identity, schema, or event collection is invalid.")
    events = ledger["events"]
    try:
        lifecycle._reduce_events(events)
    except lifecycle.WorkflowRecordError as exc:
        raise ImportSourceError(f"Workflow sidecar event history is invalid: {exc}") from exc
    return events


class InformationStore:
    """Explicit-path, disposable SQLite contract store; it is not a runtime authority."""

    def __init__(
        self,
        path: Path,
        identity: StoreIdentity,
        runtime_status: dict[str, Any],
    ) -> None:
        self.path = path
        self.identity = identity
        self.runtime_status = runtime_status

    @classmethod
    def initialize_disposable(
        cls,
        path: str | Path,
        identity: StoreIdentity,
        *,
        allow_unsupported_runtime_for_fixture: bool = False,
    ) -> InformationStore:
        _validate_store_identity(identity)
        db_path = _validate_database_path(path, identity.repository_root)
        status = sqlite_runtime_status()
        if not status["production_eligible"] and not allow_unsupported_runtime_for_fixture:
            raise RuntimeGateError(
                f"Linked SQLite {status['sqlite_version']} is below the production floor "
                f"{'.'.join(map(str, MINIMUM_SQLITE_VERSION))}; explicit disposable-fixture permission is required."
            )
        connection = sqlite3.connect(
            str(db_path),
            timeout=BUSY_TIMEOUT_MS / 1000,
            isolation_level=None,
        )
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
            app_id = connection.execute("PRAGMA application_id").fetchone()[0]
            user_version = connection.execute("PRAGMA user_version").fetchone()[0]
            objects = connection.execute(
                "SELECT type, name FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'"
            ).fetchall()
            if app_id == 0 and user_version == 0 and not objects:
                journal_mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
                if str(journal_mode).lower() != "wal":
                    raise InformationStoreError(f"SQLite refused WAL mode: {journal_mode!r}.")
                connection.execute("PRAGMA synchronous = FULL")
                connection.execute("BEGIN IMMEDIATE")
                for statement in _SCHEMA_STATEMENTS:
                    connection.execute(statement)
                connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                connection.executemany(
                    "INSERT INTO store_metadata(key, value) VALUES (?, ?)",
                    (
                        ("schema_id", SCHEMA_ID),
                        ("creation_mode", "disposable_fixture"),
                    ),
                )
                connection.execute(
                    "INSERT INTO repository(repository_id, repository_key, repository_root) VALUES (?, ?, ?)",
                    (identity.repository_id, identity.repository_key, identity.repository_root),
                )
                connection.execute(
                    "INSERT INTO milestone(repository_id, milestone_id, prompt_path) VALUES (?, ?, ?)",
                    (identity.repository_id, identity.milestone_id, identity.prompt_path),
                )
                connection.execute(
                    """
                    INSERT INTO repository_selection
                        (repository_id, milestone_id, selection_id, selection_revision)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        identity.repository_id,
                        identity.milestone_id,
                        identity.selection_id,
                        identity.selection_revision,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO workflow_state
                        (repository_id, milestone_id, store_revision, event_count, projection_json)
                    VALUES (?, ?, 0, 0, ?)
                    """,
                    (
                        identity.repository_id,
                        identity.milestone_id,
                        _json_text(_projection([])),
                    ),
                )
                connection.commit()
            else:
                cls._verify_schema(connection)
                journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
                if str(journal_mode).lower() != "wal":
                    raise SchemaMismatchError("Disposable store is not in WAL mode.")
                cls._verify_identity(connection, identity)
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()
        return cls(db_path, identity, status)

    @classmethod
    def open_disposable(
        cls,
        path: str | Path,
        identity: StoreIdentity,
        *,
        allow_unsupported_runtime_for_fixture: bool = False,
    ) -> InformationStore:
        _validate_store_identity(identity)
        db_path = _validate_database_path(path, identity.repository_root)
        status = sqlite_runtime_status()
        if not status["production_eligible"] and not allow_unsupported_runtime_for_fixture:
            raise RuntimeGateError(
                f"Linked SQLite {status['sqlite_version']} is below the production floor "
                f"{'.'.join(map(str, MINIMUM_SQLITE_VERSION))}; explicit disposable-fixture permission is required."
            )
        if not db_path.is_file():
            raise SchemaMismatchError("Disposable database does not exist; open never creates a database.")
        store = cls(db_path, identity, status)
        with store._connection(read_only=True) as connection:
            store._verify_schema(connection)
            if connection.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal":
                raise SchemaMismatchError("Disposable store is not in WAL mode.")
            store._verify_identity(connection, identity)
        return store

    @staticmethod
    def _verify_schema(connection: sqlite3.Connection) -> None:
        app_id = connection.execute("PRAGMA application_id").fetchone()[0]
        user_version = connection.execute("PRAGMA user_version").fetchone()[0]
        if app_id != APPLICATION_ID:
            raise SchemaMismatchError(f"Unexpected SQLite application_id: {app_id}.")
        if user_version != SCHEMA_VERSION:
            raise SchemaMismatchError(f"Unsupported SQLite user_version: {user_version}.")
        tables = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_schema WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")
        }
        if tables != _EXPECTED_TABLES:
            raise SchemaMismatchError("SQLite information-store table set does not match schema version 1.")
        triggers = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_schema WHERE type = 'trigger'")
        }
        if triggers != _EXPECTED_TRIGGERS:
            raise SchemaMismatchError("SQLite information-store trigger set does not match schema version 1.")
        metadata = dict(connection.execute("SELECT key, value FROM store_metadata").fetchall())
        if metadata != {"schema_id": SCHEMA_ID, "creation_mode": "disposable_fixture"}:
            raise SchemaMismatchError("SQLite database is not an initialized disposable fixture store.")

    @staticmethod
    def _verify_identity(connection: sqlite3.Connection, identity: StoreIdentity) -> None:
        repository = connection.execute(
            """
            SELECT repository_id, repository_key, repository_root FROM repository
            WHERE repository_id = ?
            """,
            (identity.repository_id,),
        ).fetchone()
        if repository is None or tuple(repository) != (
            identity.repository_id,
            identity.repository_key,
            identity.repository_root,
        ):
            raise IdentityMismatchError("Repository identity does not match the disposable store.")
        milestone = connection.execute(
            "SELECT prompt_path FROM milestone WHERE repository_id = ? AND milestone_id = ?",
            (identity.repository_id, identity.milestone_id),
        ).fetchone()
        if milestone is None or milestone["prompt_path"] != identity.prompt_path:
            raise IdentityMismatchError("Milestone or prompt path does not match the disposable store.")
        selection = connection.execute(
            """
            SELECT milestone_id, selection_id, selection_revision
            FROM repository_selection WHERE repository_id = ?
            """,
            (identity.repository_id,),
        ).fetchone()
        if (
            selection is None
            or selection["milestone_id"] != identity.milestone_id
            or selection["selection_id"] != identity.selection_id
            or selection["selection_revision"] != identity.selection_revision
        ):
            raise IdentityMismatchError("Selection snapshot does not match the disposable store.")

    def _connection(self, *, read_only: bool = False) -> sqlite3.Connection:
        mode = "ro" if read_only else "rw"
        uri = f"{self.path.as_uri()}?mode={mode}"
        connection = sqlite3.connect(
            uri,
            uri=True,
            timeout=BUSY_TIMEOUT_MS / 1000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        if not read_only:
            connection.execute("PRAGMA synchronous = FULL")
        self._verify_schema(connection)
        return connection

    @staticmethod
    def _begin_immediate(connection: sqlite3.Connection) -> None:
        try:
            connection.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            message = str(exc).lower()
            if "locked" in message or "busy" in message:
                raise BusyStoreError(
                    f"SQLite writer remained busy past the {BUSY_TIMEOUT_MS} ms fixture timeout."
                ) from exc
            raise

    def _assert_identity(
        self,
        connection: sqlite3.Connection,
        *,
        expected_repository_id: str,
        expected_milestone_id: str,
        expected_selection_id: str,
        expected_selection_revision: int,
        expected_prompt_revision_id: str | None,
        expected_store_revision: int | None = None,
    ) -> sqlite3.Row:
        if (
            expected_repository_id != self.identity.repository_id
            or expected_milestone_id != self.identity.milestone_id
        ):
            raise IdentityMismatchError("Request repository or milestone identity is stale.")
        selection = connection.execute(
            """
            SELECT milestone_id, selection_id, selection_revision
            FROM repository_selection WHERE repository_id = ?
            """,
            (self.identity.repository_id,),
        ).fetchone()
        if (
            selection is None
            or selection["milestone_id"] != expected_milestone_id
            or selection["selection_id"] != expected_selection_id
            or selection["selection_revision"] != expected_selection_revision
        ):
            raise StaleStateError("Repository selection changed before the fixture operation.")
        milestone = connection.execute(
            """
            SELECT prompt_path, prompt_revision_id FROM milestone
            WHERE repository_id = ? AND milestone_id = ?
            """,
            (self.identity.repository_id, self.identity.milestone_id),
        ).fetchone()
        if milestone is None or milestone["prompt_path"] != self.identity.prompt_path:
            raise IdentityMismatchError("Milestone prompt identity is missing or mismatched.")
        if milestone["prompt_revision_id"] != expected_prompt_revision_id:
            raise StaleStateError("Expected prompt revision is stale.")
        state = connection.execute(
            """
            SELECT store_revision, event_count, projection_json FROM workflow_state
            WHERE repository_id = ? AND milestone_id = ?
            """,
            (self.identity.repository_id, self.identity.milestone_id),
        ).fetchone()
        if state is None:
            raise IntegrityFailureError("Workflow projection row is missing.")
        if expected_store_revision is not None and state["store_revision"] != expected_store_revision:
            raise StaleStateError("Expected workflow store revision is stale.")
        return state

    def _load_events(self, connection: sqlite3.Connection) -> list[dict[str, Any]]:
        rows = connection.execute(
            """
            SELECT sequence, event_id, event_type, event_timestamp, payload_json, payload_sha256
            FROM workflow_event
            WHERE repository_id = ? AND milestone_id = ?
            ORDER BY sequence
            """,
            (self.identity.repository_id, self.identity.milestone_id),
        ).fetchall()
        events: list[dict[str, Any]] = []
        for expected_sequence, row in enumerate(rows, start=1):
            payload = row["payload_json"].encode("utf-8")
            if row["sequence"] != expected_sequence or _sha256(payload) != row["payload_sha256"]:
                raise IntegrityFailureError(f"Workflow event sequence/hash mismatch at {expected_sequence}.")
            try:
                event = json.loads(payload)
            except json.JSONDecodeError as exc:
                raise IntegrityFailureError(f"Malformed workflow event at sequence {expected_sequence}.") from exc
            if (
                not isinstance(event, dict)
                or event.get("event_id") != row["event_id"]
                or event.get("event_type") != row["event_type"]
                or event.get("timestamp") != row["event_timestamp"]
            ):
                raise IntegrityFailureError(f"Workflow event index mismatch at sequence {expected_sequence}.")
            events.append(event)
        return events

    def _validate_projection(
        self,
        state: sqlite3.Row,
        events: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if state["event_count"] != len(events) or state["store_revision"] != len(events):
            raise IntegrityFailureError("Workflow store revision/event count does not match event rows.")
        try:
            stored = json.loads(state["projection_json"])
        except json.JSONDecodeError as exc:
            raise IntegrityFailureError("Stored workflow projection is malformed.") from exc
        replayed = _projection(events)
        if stored != replayed:
            raise IntegrityFailureError("Stored workflow projection differs from lifecycle reducer replay.")
        return replayed

    @staticmethod
    def _revision_data(revision: DocumentRevisionInput) -> dict[str, Any]:
        if not all(
            isinstance(value, str) and value
            for value in (revision.document_id, revision.revision_id, revision.logical_key, revision.source_path)
        ):
            raise ValueError("Document, revision, logical key, and source path are required.")
        if len(revision.document_id) > 128 or len(revision.revision_id) > 128:
            raise ValueError("Document or revision identifier exceeds 128 characters.")
        if revision.logical_type not in _DOCUMENT_TYPES:
            raise ValueError(f"Unsupported logical document type: {revision.logical_type!r}.")
        _validate_relative_path(revision.source_path, "Document source path")
        if not isinstance(revision.content, str):
            raise ValueError("Document content must be Unicode text.")
        try:
            content_bytes = revision.content.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("Document content contains invalid Unicode.") from exc
        if len(content_bytes) > MAX_DOCUMENT_BYTES:
            raise ValueError(f"Document exceeds the {MAX_DOCUMENT_BYTES}-byte fixture limit.")
        if revision.parent_revision_id is not None and (
            not isinstance(revision.parent_revision_id, str) or not revision.parent_revision_id
        ):
            raise ValueError("Parent revision identifier must be non-empty text or None.")
        if not isinstance(revision.source_kind, str) or not revision.source_kind:
            raise ValueError("Document source kind is required.")
        provenance = revision.provenance or {}
        if not isinstance(provenance, dict):
            raise ValueError("Document provenance must be a JSON object.")
        provenance_json = _json_text(provenance)
        return {
            "document_id": revision.document_id,
            "revision_id": revision.revision_id,
            "logical_key": revision.logical_key,
            "logical_type": revision.logical_type,
            "source_path": revision.source_path,
            "parent_revision_id": revision.parent_revision_id,
            "content": revision.content,
            "content_sha256": _sha256(content_bytes),
            "source_kind": revision.source_kind,
            "provenance_json": provenance_json,
            "created_at": _now(),
        }

    def _insert_revision(
        self,
        connection: sqlite3.Connection,
        item: dict[str, Any],
    ) -> None:
        doc = connection.execute(
            """
            SELECT document_id, logical_type, source_path, draft_revision_id
            FROM document
            WHERE repository_id = ? AND milestone_id = ? AND logical_key = ?
            """,
            (self.identity.repository_id, self.identity.milestone_id, item["logical_key"]),
        ).fetchone()
        if doc is None:
            if item["parent_revision_id"] is not None:
                raise RevisionConflictError("A first document revision cannot name a parent.")
            connection.execute(
                """
                INSERT INTO document
                    (document_id, repository_id, milestone_id, logical_key, logical_type, source_path)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    item["document_id"],
                    self.identity.repository_id,
                    self.identity.milestone_id,
                    item["logical_key"],
                    item["logical_type"],
                    item["source_path"],
                ),
            )
        else:
            if (
                doc["document_id"] != item["document_id"]
                or doc["logical_type"] != item["logical_type"]
                or doc["source_path"] != item["source_path"]
            ):
                raise IdentityMismatchError("Document logical identity changed.")
            if doc["draft_revision_id"] != item["parent_revision_id"]:
                raise RevisionConflictError("Document parent revision is stale.")
        if item["parent_revision_id"] is not None:
            parent = connection.execute(
                "SELECT 1 FROM document_revision WHERE document_id = ? AND revision_id = ?",
                (item["document_id"], item["parent_revision_id"]),
            ).fetchone()
            if parent is None:
                raise RevisionConflictError("Parent revision does not belong to this document.")
        connection.execute(
            """
            INSERT INTO document_revision
                (revision_id, document_id, repository_id, milestone_id, parent_revision_id,
                 content, content_sha256, source_kind, provenance_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item["revision_id"],
                item["document_id"],
                self.identity.repository_id,
                self.identity.milestone_id,
                item["parent_revision_id"],
                item["content"],
                item["content_sha256"],
                item["source_kind"],
                item["provenance_json"],
                item["created_at"],
            ),
        )
        connection.execute(
            """
            UPDATE document SET draft_revision_id = ?
            WHERE document_id = ? AND repository_id = ? AND milestone_id = ?
            """,
            (
                item["revision_id"],
                item["document_id"],
                self.identity.repository_id,
                self.identity.milestone_id,
            ),
        )

    def create_immutable_revision(self, revision: DocumentRevisionInput) -> dict[str, Any]:
        item = self._revision_data(revision)
        connection = self._connection()
        try:
            self._begin_immediate(connection)
            self._verify_identity(connection, self.identity)
            self._insert_revision(connection, item)
            connection.commit()
            return {
                "document_id": item["document_id"],
                "revision_id": item["revision_id"],
                "content_sha256": item["content_sha256"],
                "source_kind": item["source_kind"],
            }
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def _revision_hash(self, connection: sqlite3.Connection, document_id: str, revision_id: str) -> str:
        row = connection.execute(
            """
            SELECT content_sha256 FROM document_revision
            WHERE repository_id = ? AND milestone_id = ? AND document_id = ? AND revision_id = ?
            """,
            (self.identity.repository_id, self.identity.milestone_id, document_id, revision_id),
        ).fetchone()
        if row is None:
            raise RevisionConflictError("Exact document revision was not found.")
        return row["content_sha256"]

    def _append_workflow_batch(
        self,
        *,
        request_id: str,
        expected_repository_id: str,
        expected_milestone_id: str,
        expected_selection_id: str,
        expected_selection_revision: int,
        expected_prompt_revision_id: str | None,
        expected_store_revision: int,
        events: Sequence[dict[str, Any]],
        new_revisions: Sequence[DocumentRevisionInput] = (),
        links: Sequence[WorkflowDocumentLink] = (),
        trusted_fixture_po_confirmation: bool = False,
        approved_prompt_revision_id: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(request_id, str) or not request_id or len(request_id) > 128:
            raise ValueError("Request ID must be non-empty text of at most 128 characters.")
        if not isinstance(events, Sequence) or isinstance(events, (str, bytes)) or not events:
            raise ValueError("At least one workflow event is required.")
        if len(events) > MAX_EVENT_COUNT:
            raise ValueError(f"Workflow batch exceeds the {MAX_EVENT_COUNT}-event fixture limit.")
        if len(new_revisions) > 100 or len(links) > MAX_EVENT_COUNT:
            raise ValueError("Workflow batch exceeds the bounded revision/link limit.")
        event_bytes = sum(len(_json_text(event).encode("utf-8")) for event in events)
        if event_bytes > MAX_EVENT_BATCH_BYTES:
            raise ValueError(f"Workflow batch exceeds the {MAX_EVENT_BATCH_BYTES}-byte fixture limit.")
        revision_items = [self._revision_data(item) for item in new_revisions]
        revision_by_id = {item["revision_id"]: item for item in revision_items}
        if len(revision_by_id) != len(revision_items):
            raise RevisionConflictError("Workflow batch contains duplicate new revision IDs.")
        if not isinstance(trusted_fixture_po_confirmation, bool):
            raise ValueError("Fixture PO confirmation must be an explicit boolean.")
        request_body = {
            "request_id": request_id,
            "repository_id": expected_repository_id,
            "milestone_id": expected_milestone_id,
            "selection_id": expected_selection_id,
            "selection_revision": expected_selection_revision,
            "prompt_revision_id": expected_prompt_revision_id,
            "store_revision": expected_store_revision,
            "events": list(events),
            "new_revisions": [
                {
                    **{key: item[key] for key in (
                        "document_id", "revision_id", "logical_key", "logical_type", "source_path",
                        "parent_revision_id", "content_sha256", "source_kind", "provenance_json",
                    )}
                }
                for item in revision_items
            ],
            "links": [link.__dict__ for link in links],
            "trusted_fixture_po_confirmation": trusted_fixture_po_confirmation,
            "approved_prompt_revision_id": approved_prompt_revision_id,
        }
        request_hash = _sha256(_json_text(request_body).encode("utf-8"))
        connection = self._connection()
        try:
            self._begin_immediate(connection)
            receipt = connection.execute(
                """
                SELECT request_hash, result_json FROM transaction_receipt
                WHERE repository_id = ? AND milestone_id = ? AND request_id = ?
                """,
                (self.identity.repository_id, self.identity.milestone_id, request_id),
            ).fetchone()
            if receipt is not None:
                if receipt["request_hash"] != request_hash:
                    raise IdempotencyConflictError("Request ID was already used with different content.")
                result = json.loads(receipt["result_json"])
                connection.commit()
                return result

            state = self._assert_identity(
                connection,
                expected_repository_id=expected_repository_id,
                expected_milestone_id=expected_milestone_id,
                expected_selection_id=expected_selection_id,
                expected_selection_revision=expected_selection_revision,
                expected_prompt_revision_id=expected_prompt_revision_id,
                expected_store_revision=expected_store_revision,
            )
            previous_events = self._load_events(connection)
            self._validate_projection(state, previous_events)
            candidate_events = previous_events + list(events)
            try:
                projection = _projection(candidate_events)
            except lifecycle.WorkflowRecordError:
                raise
            for event in events:
                if (
                    isinstance(event, dict)
                    and event.get("event_type") in _PROTECTED_WORKFLOW_EVENTS
                    and event.get("po_approved") is True
                    and not trusted_fixture_po_confirmation
                ):
                    raise InformationStoreError(
                        "Protected workflow events require explicit trusted fixture PO confirmation."
                    )

            event_ids = {event.get("event_id") for event in events if isinstance(event, dict)}
            link_specs: list[dict[str, Any]] = []
            seen_links: set[tuple[str, str]] = set()
            for link in links:
                if link.link_role not in _WORKFLOW_LINK_ROLES or not link.evidence:
                    raise ValueError("Workflow links require a supported role and evidence.")
                if link.event_id not in event_ids:
                    raise IntegrityFailureError("Workflow links may only attach to events in this batch.")
                if (link.event_id, link.link_role) in seen_links:
                    raise IntegrityFailureError("Workflow batch contains duplicate document-link roles.")
                seen_links.add((link.event_id, link.link_role))
                new_item = revision_by_id.get(link.revision_id)
                if new_item is not None:
                    if new_item["document_id"] != link.document_id:
                        raise IdentityMismatchError("New document link has a mismatched document identity.")
                    actual_hash = new_item["content_sha256"]
                    logical_type = new_item["logical_type"]
                    source_path = new_item["source_path"]
                else:
                    actual_hash = self._revision_hash(connection, link.document_id, link.revision_id)
                    revision_row = connection.execute(
                        """
                        SELECT d.logical_type, d.source_path
                        FROM document_revision r JOIN document d ON d.document_id = r.document_id
                        WHERE r.repository_id = ? AND r.milestone_id = ?
                            AND r.document_id = ? AND r.revision_id = ?
                        """,
                        (
                            self.identity.repository_id,
                            self.identity.milestone_id,
                            link.document_id,
                            link.revision_id,
                        ),
                    ).fetchone()
                    if revision_row is None:
                        raise RevisionConflictError("Linked document revision disappeared.")
                    logical_type = revision_row["logical_type"]
                    source_path = revision_row["source_path"]
                if actual_hash != link.content_sha256:
                    raise IntegrityFailureError("Workflow link content hash does not match exact revision.")
                if link.link_role == "prompt" and logical_type != "PROMPT":
                    raise IdentityMismatchError("Prompt links must target a PROMPT revision.")
                if link.link_role == "closeout" and logical_type != "CLOSEOUT":
                    raise IdentityMismatchError("Closeout links must target a CLOSEOUT revision.")
                linked_event = next(event for event in events if event["event_id"] == link.event_id)
                if (
                    linked_event["event_type"] == "closeout_submitted"
                    and linked_event.get("evidence_path") != source_path
                ):
                    raise IdentityMismatchError("Closeout event path does not match its exact linked revision.")
                link_specs.append(
                    {
                        "event_id": link.event_id,
                        "document_id": link.document_id,
                        "revision_id": link.revision_id,
                        "content_sha256": link.content_sha256,
                        "link_role": link.link_role,
                        "evidence": link.evidence,
                    }
                )
            links_by_event_role = {(item["event_id"], item["link_role"]) for item in link_specs}
            for event in events:
                event_type = event.get("event_type") if isinstance(event, dict) else None
                required_role = (
                    "prompt"
                    if event_type == "prompt_approved"
                    else "closeout"
                    if event_type in {"closeout_submitted", "acceptance_recorded"}
                    else None
                )
                if required_role and (event["event_id"], required_role) not in links_by_event_role:
                    raise IntegrityFailureError(f"{event_type} requires an exact {required_role} revision link.")

            if approved_prompt_revision_id is not None:
                prompt_link = next(
                    (
                        item
                        for item in link_specs
                        if item["link_role"] == "prompt"
                        and item["revision_id"] == approved_prompt_revision_id
                    ),
                    None,
                )
                if prompt_link is None:
                    raise IntegrityFailureError("Approved prompt revision must be linked to prompt_approved.")
                prompt_doc = connection.execute(
                    """
                    SELECT logical_type, source_path FROM document
                    WHERE document_id = ? AND repository_id = ? AND milestone_id = ?
                    """,
                    (prompt_link["document_id"], self.identity.repository_id, self.identity.milestone_id),
                ).fetchone()
                if prompt_doc is None:
                    new_prompt = revision_by_id.get(approved_prompt_revision_id)
                    logical_type = new_prompt["logical_type"] if new_prompt else None
                    source_path = new_prompt["source_path"] if new_prompt else None
                else:
                    logical_type = prompt_doc["logical_type"]
                    source_path = prompt_doc["source_path"]
                if logical_type != "PROMPT" or source_path != self.identity.prompt_path:
                    raise IdentityMismatchError("Approved revision is not the selected milestone prompt.")
                if not trusted_fixture_po_confirmation:
                    raise InformationStoreError(
                        "Selecting an approved prompt requires explicit trusted fixture PO confirmation."
                    )
            elif any(
                isinstance(event, dict) and event.get("event_type") == "prompt_approved"
                for event in events
            ):
                raise IntegrityFailureError("prompt_approved must select and link an exact prompt revision.")

            for item in revision_items:
                self._insert_revision(connection, item)

            first_sequence = len(previous_events) + 1
            for offset, event in enumerate(events):
                payload = _json_text(event).encode("utf-8")
                connection.execute(
                    """
                    INSERT INTO workflow_event
                        (repository_id, milestone_id, sequence, event_id, event_type,
                         event_timestamp, payload_json, payload_sha256, request_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        self.identity.repository_id,
                        self.identity.milestone_id,
                        first_sequence + offset,
                        event["event_id"],
                        event["event_type"],
                        event["timestamp"],
                        payload.decode("utf-8"),
                        _sha256(payload),
                        request_id,
                    ),
                )
            for link in link_specs:
                connection.execute(
                    """
                    INSERT INTO workflow_document_link
                        (repository_id, milestone_id, event_id, document_id, revision_id,
                         content_sha256, link_role, evidence)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        self.identity.repository_id,
                        self.identity.milestone_id,
                        link["event_id"],
                        link["document_id"],
                        link["revision_id"],
                        link["content_sha256"],
                        link["link_role"],
                        link["evidence"],
                    ),
                )

            resulting_revision = len(candidate_events)
            cursor = self._write_workflow_state(
                connection,
                expected_store_revision=expected_store_revision,
                resulting_revision=resulting_revision,
                event_count=len(candidate_events),
                projection_json=_json_text(projection),
            )
            if cursor.rowcount != 1:
                raise StaleStateError("Workflow projection changed during append.")

            if approved_prompt_revision_id is not None:
                prompt_link = next(item for item in link_specs if item["revision_id"] == approved_prompt_revision_id)
                connection.execute(
                    "UPDATE document SET approved_revision_id = ? WHERE document_id = ?",
                    (approved_prompt_revision_id, prompt_link["document_id"]),
                )
                connection.execute(
                    """
                    UPDATE milestone SET prompt_revision_id = ?
                    WHERE repository_id = ? AND milestone_id = ?
                    """,
                    (
                        approved_prompt_revision_id,
                        self.identity.repository_id,
                        self.identity.milestone_id,
                    ),
                )
                decision_event = next(event for event in events if event["event_type"] == "prompt_approved")
                connection.execute(
                    """
                    INSERT INTO document_revision_decision
                        (decision_id, repository_id, milestone_id, document_id, revision_id,
                         decision_type, actor_role, actor_attribution, po_confirmed, source_kind,
                         decided_at, payload_json)
                    VALUES (?, ?, ?, ?, ?, 'APPROVED', ?, ?, 1, 'trusted_fixture', ?, ?)
                    """,
                    (
                        uuid.uuid4().hex,
                        self.identity.repository_id,
                        self.identity.milestone_id,
                        prompt_link["document_id"],
                        approved_prompt_revision_id,
                        decision_event["actor_role"],
                        decision_event["actor_attribution"],
                        decision_event["timestamp"],
                        _json_text({"event_id": decision_event["event_id"], "fixture_confirmation": True}),
                    ),
                )

            for event in events:
                if event["event_type"] != "acceptance_recorded":
                    continue
                closeout_link = next(
                    item
                    for item in link_specs
                    if item["event_id"] == event["event_id"] and item["link_role"] == "closeout"
                )
                connection.execute(
                    """
                    INSERT INTO document_revision_decision
                        (decision_id, repository_id, milestone_id, document_id, revision_id,
                         decision_type, actor_role, actor_attribution, po_confirmed, source_kind,
                         decided_at, payload_json)
                    VALUES (?, ?, ?, ?, ?, 'APPROVED', ?, ?, 1, 'trusted_fixture', ?, ?)
                    """,
                    (
                        uuid.uuid4().hex,
                        self.identity.repository_id,
                        self.identity.milestone_id,
                        closeout_link["document_id"],
                        closeout_link["revision_id"],
                        event["actor_role"],
                        event["actor_attribution"],
                        event["timestamp"],
                        _json_text({"event_id": event["event_id"], "fixture_confirmation": True}),
                    ),
                )

            result = {
                "request_id": request_id,
                "request_hash": request_hash,
                "store_revision": resulting_revision,
                "first_sequence": first_sequence,
                "last_sequence": resulting_revision,
                "event_ids": [event["event_id"] for event in events],
                "document_revision_ids": [item["revision_id"] for item in revision_items],
                "linked_revision_ids": [item["revision_id"] for item in link_specs],
            }
            connection.execute(
                """
                INSERT INTO transaction_receipt
                    (repository_id, milestone_id, request_id, request_hash,
                     resulting_revision, result_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self.identity.repository_id,
                    self.identity.milestone_id,
                    request_id,
                    request_hash,
                    resulting_revision,
                    _json_text(result),
                    _now(),
                ),
            )
            connection.commit()
            return result
        except sqlite3.OperationalError as exc:
            if connection.in_transaction:
                connection.rollback()
            message = str(exc).lower()
            if "locked" in message or "busy" in message:
                raise BusyStoreError(
                    f"SQLite writer remained busy past the {BUSY_TIMEOUT_MS} ms fixture timeout."
                ) from exc
            raise
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def append_workflow_batch(
        self,
        *,
        request_id: str,
        expected_repository_id: str,
        expected_milestone_id: str,
        expected_selection_id: str,
        expected_selection_revision: int,
        expected_prompt_revision_id: str | None,
        expected_store_revision: int,
        events: Sequence[dict[str, Any]],
        new_revisions: Sequence[DocumentRevisionInput] = (),
        links: Sequence[WorkflowDocumentLink] = (),
        trusted_fixture_po_confirmation: bool = False,
    ) -> dict[str, Any]:
        return self._append_workflow_batch(
            request_id=request_id,
            expected_repository_id=expected_repository_id,
            expected_milestone_id=expected_milestone_id,
            expected_selection_id=expected_selection_id,
            expected_selection_revision=expected_selection_revision,
            expected_prompt_revision_id=expected_prompt_revision_id,
            expected_store_revision=expected_store_revision,
            events=events,
            new_revisions=new_revisions,
            links=links,
            trusted_fixture_po_confirmation=trusted_fixture_po_confirmation,
        )

    def _write_workflow_state(
        self,
        connection: sqlite3.Connection,
        *,
        expected_store_revision: int,
        resulting_revision: int,
        event_count: int,
        projection_json: str,
    ) -> sqlite3.Cursor:
        return connection.execute(
            """
            UPDATE workflow_state
            SET store_revision = ?, event_count = ?, projection_json = ?
            WHERE repository_id = ? AND milestone_id = ? AND store_revision = ?
            """,
            (
                resulting_revision,
                event_count,
                projection_json,
                self.identity.repository_id,
                self.identity.milestone_id,
                expected_store_revision,
            ),
        )

    def select_approved_prompt_revision(
        self,
        *,
        request_id: str,
        expected_store_revision: int,
        document_id: str,
        revision_id: str,
        content_sha256: str,
        prompt_approved_event: dict[str, Any],
        trusted_fixture_po_confirmation: bool,
    ) -> dict[str, Any]:
        link = WorkflowDocumentLink(
            event_id=prompt_approved_event.get("event_id", ""),
            document_id=document_id,
            revision_id=revision_id,
            content_sha256=content_sha256,
            link_role="prompt",
            evidence="Exact fixture prompt revision selected by explicit trusted fixture confirmation.",
        )
        return self._append_workflow_batch(
            request_id=request_id,
            expected_repository_id=self.identity.repository_id,
            expected_milestone_id=self.identity.milestone_id,
            expected_selection_id=self.identity.selection_id,
            expected_selection_revision=self.identity.selection_revision,
            expected_prompt_revision_id=None,
            expected_store_revision=expected_store_revision,
            events=[prompt_approved_event],
            links=[link],
            trusted_fixture_po_confirmation=trusted_fixture_po_confirmation,
            approved_prompt_revision_id=revision_id,
        )

    def submit_closeout_revision(
        self,
        *,
        request_id: str,
        expected_prompt_revision_id: str,
        expected_store_revision: int,
        revision: DocumentRevisionInput,
        closeout_submitted_event: dict[str, Any],
    ) -> dict[str, Any]:
        item = self._revision_data(revision)
        link = WorkflowDocumentLink(
            event_id=closeout_submitted_event.get("event_id", ""),
            document_id=revision.document_id,
            revision_id=revision.revision_id,
            content_sha256=item["content_sha256"],
            link_role="closeout",
            evidence="Exact closeout revision submitted in this transaction.",
        )
        if item["logical_type"] != "CLOSEOUT":
            raise IdentityMismatchError("Closeout submission requires a CLOSEOUT document revision.")
        return self._append_workflow_batch(
            request_id=request_id,
            expected_repository_id=self.identity.repository_id,
            expected_milestone_id=self.identity.milestone_id,
            expected_selection_id=self.identity.selection_id,
            expected_selection_revision=self.identity.selection_revision,
            expected_prompt_revision_id=expected_prompt_revision_id,
            expected_store_revision=expected_store_revision,
            events=[closeout_submitted_event],
            new_revisions=[revision],
            links=[link],
        )

    def snapshot(self) -> dict[str, Any]:
        connection = self._connection(read_only=True)
        try:
            connection.execute("BEGIN")
            self._verify_identity(connection, self.identity)
            state = connection.execute(
                """
                SELECT store_revision, event_count, projection_json
                FROM workflow_state WHERE repository_id = ? AND milestone_id = ?
                """,
                (self.identity.repository_id, self.identity.milestone_id),
            ).fetchone()
            events = self._load_events(connection)
            projection = self._validate_projection(state, events)
            result = {
                "repository_id": self.identity.repository_id,
                "milestone_id": self.identity.milestone_id,
                "selection_id": self.identity.selection_id,
                "selection_revision": self.identity.selection_revision,
                "prompt_revision_id": connection.execute(
                    """
                    SELECT prompt_revision_id FROM milestone
                    WHERE repository_id = ? AND milestone_id = ?
                    """,
                    (self.identity.repository_id, self.identity.milestone_id),
                ).fetchone()["prompt_revision_id"],
                "store_revision": state["store_revision"],
                "event_count": state["event_count"],
                "projection": projection,
                "events": events,
                "runtime": dict(self.runtime_status),
                "production_eligible": self.runtime_status["production_eligible"],
                "authority": "disposable_fixture_only",
            }
            connection.commit()
            return result
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def page_events(
        self,
        *,
        snapshot_revision: int,
        after_sequence: int = 0,
        limit: int = MAX_EVENT_PAGE_SIZE,
    ) -> dict[str, Any]:
        if type(snapshot_revision) is not int or snapshot_revision < 0:
            raise ValueError("Snapshot revision must be a non-negative integer.")
        if type(after_sequence) is not int or after_sequence < 0:
            raise ValueError("Page sequence must be a non-negative integer.")
        if type(limit) is not int or not 1 <= limit <= MAX_EVENT_PAGE_SIZE:
            raise ValueError(f"Page size must be between 1 and {MAX_EVENT_PAGE_SIZE}.")
        connection = self._connection(read_only=True)
        try:
            connection.execute("BEGIN")
            self._verify_identity(connection, self.identity)
            state = connection.execute(
                """
                SELECT store_revision FROM workflow_state
                WHERE repository_id = ? AND milestone_id = ?
                """,
                (self.identity.repository_id, self.identity.milestone_id),
            ).fetchone()
            if state is None or snapshot_revision > state["store_revision"]:
                raise StaleStateError("Requested snapshot revision is unavailable.")
            if after_sequence > snapshot_revision:
                raise StaleStateError("Event cursor is beyond the requested snapshot.")
            rows = connection.execute(
                """
                SELECT sequence, event_id, event_type, event_timestamp, payload_json, payload_sha256
                FROM workflow_event
                WHERE repository_id = ? AND milestone_id = ?
                    AND sequence > ? AND sequence <= ?
                ORDER BY sequence LIMIT ?
                """,
                (
                    self.identity.repository_id,
                    self.identity.milestone_id,
                    after_sequence,
                    snapshot_revision,
                    limit,
                ),
            ).fetchall()
            page: list[dict[str, Any]] = []
            expected = after_sequence + 1
            for row in rows:
                raw = row["payload_json"].encode("utf-8")
                if row["sequence"] != expected or _sha256(raw) != row["payload_sha256"]:
                    raise IntegrityFailureError(f"Event page integrity failure at sequence {expected}.")
                event = json.loads(raw)
                if (
                    not isinstance(event, dict)
                    or event.get("event_id") != row["event_id"]
                    or event.get("event_type") != row["event_type"]
                    or event.get("timestamp") != row["event_timestamp"]
                ):
                    raise IntegrityFailureError(f"Event page index mismatch at sequence {expected}.")
                page.append({"sequence": row["sequence"], **event})
                expected += 1
            result = {
                "snapshot_revision": snapshot_revision,
                "events": page,
                "next_sequence": page[-1]["sequence"] if page else after_sequence,
            }
            connection.commit()
            return result
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def list_documents(
        self,
        *,
        after_order: int = 0,
        limit: int = MAX_DOCUMENT_LIST_SIZE,
    ) -> dict[str, Any]:
        if type(after_order) is not int or after_order < 0:
            raise ValueError("Document cursor must be a non-negative integer.")
        if type(limit) is not int or not 1 <= limit <= MAX_DOCUMENT_LIST_SIZE:
            raise ValueError(f"Document page size must be between 1 and {MAX_DOCUMENT_LIST_SIZE}.")
        connection = self._connection(read_only=True)
        try:
            self._verify_identity(connection, self.identity)
            rows = connection.execute(
                """
                SELECT document_order, document_id, logical_key, logical_type, source_path,
                       draft_revision_id, approved_revision_id
                FROM document
                WHERE repository_id = ? AND milestone_id = ? AND document_order > ?
                ORDER BY document_order LIMIT ?
                """,
                (
                    self.identity.repository_id,
                    self.identity.milestone_id,
                    after_order,
                    limit + 1,
                ),
            ).fetchall()
            has_more = len(rows) > limit
            selected = rows[:limit]
            return {
                "documents": [dict(row) for row in selected],
                "next_cursor": selected[-1]["document_order"] if has_more and selected else None,
            }
        finally:
            connection.close()

    def list_revisions(
        self,
        document_id: str,
        *,
        after_order: int = 0,
        limit: int = MAX_DOCUMENT_REVISION_LIST_SIZE,
    ) -> dict[str, Any]:
        if not isinstance(document_id, str) or not document_id:
            raise ValueError("Document ID is required.")
        if type(after_order) is not int or after_order < 0:
            raise ValueError("Revision cursor must be a non-negative integer.")
        if type(limit) is not int or not 1 <= limit <= MAX_DOCUMENT_REVISION_LIST_SIZE:
            raise ValueError(
                f"Revision page size must be between 1 and {MAX_DOCUMENT_REVISION_LIST_SIZE}."
            )
        connection = self._connection(read_only=True)
        try:
            self._verify_identity(connection, self.identity)
            document = connection.execute(
                """
                SELECT draft_revision_id, approved_revision_id
                FROM document
                WHERE repository_id = ? AND milestone_id = ? AND document_id = ?
                """,
                (self.identity.repository_id, self.identity.milestone_id, document_id),
            ).fetchone()
            if document is None:
                raise RevisionConflictError("Document was not found.")
            rows = connection.execute(
                """
                SELECT revision_order, revision_id, parent_revision_id, content_sha256,
                       source_kind, provenance_json, created_at
                FROM document_revision
                WHERE repository_id = ? AND milestone_id = ? AND document_id = ?
                    AND revision_order > ?
                ORDER BY revision_order
                LIMIT ?
                """,
                (
                    self.identity.repository_id,
                    self.identity.milestone_id,
                    document_id,
                    after_order,
                    limit + 1,
                ),
            ).fetchall()
            has_more = len(rows) > limit
            selected = rows[:limit]
            revisions = []
            for row in selected:
                status = (
                    "approved"
                    if row["revision_id"] == document["approved_revision_id"]
                    else "draft"
                    if row["revision_id"] == document["draft_revision_id"]
                    else "historical"
                )
                revisions.append(
                    {
                        **{
                            key: row[key]
                            for key in (
                                "revision_order",
                                "revision_id",
                                "parent_revision_id",
                                "content_sha256",
                                "source_kind",
                                "created_at",
                            )
                        },
                        "provenance": json.loads(row["provenance_json"]),
                        "status": status,
                    }
                )
            return {
                "document_id": document_id,
                "revisions": revisions,
                "next_cursor": selected[-1]["revision_order"] if has_more and selected else None,
            }
        finally:
            connection.close()

    def get_revision(self, document_id: str, revision_id: str) -> dict[str, Any]:
        connection = self._connection(read_only=True)
        try:
            self._verify_identity(connection, self.identity)
            row = connection.execute(
                """
                SELECT d.document_id, d.logical_key, d.logical_type, d.source_path,
                       d.draft_revision_id, d.approved_revision_id,
                       r.revision_id, r.parent_revision_id, r.content, r.content_sha256,
                       r.source_kind, r.provenance_json, r.created_at
                FROM document_revision r
                JOIN document d ON d.document_id = r.document_id
                WHERE r.repository_id = ? AND r.milestone_id = ?
                    AND r.document_id = ? AND r.revision_id = ?
                """,
                (self.identity.repository_id, self.identity.milestone_id, document_id, revision_id),
            ).fetchone()
            if row is None:
                raise RevisionConflictError("Exact document revision was not found.")
            result = dict(row)
            if _sha256(result["content"].encode("utf-8")) != result["content_sha256"]:
                raise IntegrityFailureError("Stored document content hash does not match its revision.")
            result["provenance"] = json.loads(result.pop("provenance_json"))
            return result
        finally:
            connection.close()

    def get_revision_by_hash(self, document_id: str, content_sha256: str) -> dict[str, Any]:
        connection = self._connection(read_only=True)
        try:
            self._verify_identity(connection, self.identity)
            row = connection.execute(
                """
                SELECT revision_id FROM document_revision
                WHERE repository_id = ? AND milestone_id = ? AND document_id = ?
                    AND content_sha256 = ?
                ORDER BY revision_order DESC LIMIT 1
                """,
                (self.identity.repository_id, self.identity.milestone_id, document_id, content_sha256),
            ).fetchone()
            if row is None:
                raise RevisionConflictError("Document revision hash was not found.")
        finally:
            connection.close()
        return self.get_revision(document_id, row["revision_id"])

    def read_revision_page(
        self,
        document_id: str,
        revision_id: str,
        *,
        byte_offset: int = 0,
        max_bytes: int = MAX_DOCUMENT_PAGE_BYTES,
    ) -> dict[str, Any]:
        if type(byte_offset) is not int or byte_offset < 0:
            raise ValueError("Document byte offset must be a non-negative integer.")
        if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_DOCUMENT_PAGE_BYTES:
            raise ValueError(f"Document page size must be between 1 and {MAX_DOCUMENT_PAGE_BYTES} bytes.")
        revision = self.get_revision(document_id, revision_id)
        content_bytes = revision["content"].encode("utf-8")
        if byte_offset > len(content_bytes):
            raise ValueError("Document byte offset is beyond the exact revision.")
        try:
            content_bytes[:byte_offset].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("Document byte offset splits a UTF-8 code point.") from exc
        end = min(byte_offset + max_bytes, len(content_bytes))
        while end > byte_offset:
            try:
                content = content_bytes[byte_offset:end].decode("utf-8")
                break
            except UnicodeDecodeError:
                end -= 1
        else:
            if byte_offset < len(content_bytes):
                raise ValueError("Page size is too small to contain the next UTF-8 code point.")
            content = ""
        return {
            "repository_id": self.identity.repository_id,
            "milestone_id": self.identity.milestone_id,
            "document_id": document_id,
            "revision_id": revision_id,
            "content_sha256": revision["content_sha256"],
            "byte_offset": byte_offset,
            "next_byte_offset": end,
            "content": content,
            "complete": end == len(content_bytes),
        }

    def import_v1_fixture(self, bundle: V1FixtureBundle) -> dict[str, Any]:
        if bundle.prompt_path != self.identity.prompt_path:
            raise ImportSourceError("Imported prompt path does not match the fixture milestone identity.")
        prompt_path = _validate_relative_path(bundle.prompt_path, "Prompt source path")
        closeout_path = _validate_relative_path(bundle.closeout_path, "Closeout source path")
        workflow_path = _validate_relative_path(bundle.workflow_path, "Workflow source path")
        if not all(isinstance(item, bytes) for item in (bundle.prompt_bytes, bundle.closeout_bytes, bundle.workflow_bytes)):
            raise ImportSourceError("Prompt, closeout, and workflow fixture sources must be caller-supplied bytes.")
        try:
            prompt_text = bundle.prompt_bytes.decode("utf-8")
            closeout_text = bundle.closeout_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ImportSourceError("Prompt or closeout fixture source is not valid UTF-8.") from exc
        events = _parse_sidecar_bytes(bundle.workflow_bytes, self.identity)
        for event in events:
            if event["event_type"] == "closeout_submitted" and event.get("evidence_path") != closeout_path:
                raise ImportSourceError("Workflow closeout path does not match the caller-supplied closeout source.")
        dispatch_ledger: dict[str, Any] | None = None
        if bundle.dispatch_bytes is not None:
            if not isinstance(bundle.dispatch_path, str):
                raise ImportSourceError("Dispatch bytes require a caller-supplied relative source path.")
            _validate_relative_path(bundle.dispatch_path, "Dispatch source path")
            try:
                dispatch_payload = json.loads(bundle.dispatch_bytes.decode("utf-8"))
                dispatch_ledger = architect_dispatches._validate_ledger(
                    dispatch_payload,
                    self.identity.repository_id,
                )
            except (UnicodeDecodeError, json.JSONDecodeError, architect_dispatches.ArchitectDispatchError) as exc:
                raise ImportSourceError(f"Dispatch fixture source is malformed: {exc}") from exc
        elif bundle.dispatch_path is not None:
            raise ImportSourceError("Dispatch source path was supplied without dispatch bytes.")

        prompt_hash = _sha256(bundle.prompt_bytes)
        closeout_hash = _sha256(bundle.closeout_bytes)
        prompt_doc_id = _sha256(
            _json_text([self.identity.repository_id, self.identity.milestone_id, prompt_path]).encode("utf-8")
        )[:32]
        closeout_doc_id = _sha256(
            _json_text([self.identity.repository_id, self.identity.milestone_id, closeout_path]).encode("utf-8")
        )[:32]
        prompt_revision_id = _sha256(f"{prompt_doc_id}:{prompt_hash}".encode("utf-8"))[:32]
        closeout_revision_id = _sha256(f"{closeout_doc_id}:{closeout_hash}".encode("utf-8"))[:32]
        connection = self._connection()
        try:
            self._begin_immediate(connection)
            self._verify_identity(connection, self.identity)
            existing_counts = connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM document WHERE repository_id = ? AND milestone_id = ?) AS documents,
                    (SELECT COUNT(*) FROM workflow_event WHERE repository_id = ? AND milestone_id = ?) AS events,
                    (SELECT COUNT(*) FROM import_source WHERE repository_id = ? AND milestone_id = ?) AS sources
                """,
                (
                    self.identity.repository_id,
                    self.identity.milestone_id,
                    self.identity.repository_id,
                    self.identity.milestone_id,
                    self.identity.repository_id,
                    self.identity.milestone_id,
                ),
            ).fetchone()
            if any(existing_counts[key] for key in ("documents", "events", "sources")):
                raise ImportSourceError("V1 fixture import requires an empty disposable milestone store.")

            prompt_revision = DocumentRevisionInput(
                document_id=prompt_doc_id,
                revision_id=prompt_revision_id,
                logical_key=prompt_path,
                logical_type="PROMPT",
                source_path=prompt_path,
                content=prompt_text,
                source_kind="v1_import",
                provenance={"source_path": prompt_path, "source_sha256": prompt_hash},
            )
            closeout_revision = DocumentRevisionInput(
                document_id=closeout_doc_id,
                revision_id=closeout_revision_id,
                logical_key=closeout_path,
                logical_type="CLOSEOUT",
                source_path=closeout_path,
                content=closeout_text,
                source_kind="v1_import",
                provenance={"source_path": closeout_path, "source_sha256": closeout_hash},
            )
            prompt_item = self._revision_data(prompt_revision)
            closeout_item = self._revision_data(closeout_revision)
            self._insert_revision(connection, prompt_item)
            self._insert_revision(connection, closeout_item)
            for source_kind, source_path, source_bytes in (
                ("prompt", prompt_path, bundle.prompt_bytes),
                ("closeout", closeout_path, bundle.closeout_bytes),
                ("workflow", workflow_path, bundle.workflow_bytes),
            ):
                source_hash = _sha256(source_bytes)
                connection.execute(
                    """
                    INSERT INTO import_source
                        (source_id, repository_id, milestone_id, source_kind, source_path,
                         source_sha256, source_bytes, imported_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        _source_record_id(
                            self.identity.repository_id,
                            self.identity.milestone_id,
                            source_kind,
                            source_path,
                            source_hash,
                        ),
                        self.identity.repository_id,
                        self.identity.milestone_id,
                        source_kind,
                        source_path,
                        source_hash,
                        source_bytes,
                        _now(),
                    ),
                )
            if bundle.dispatch_bytes is not None and bundle.dispatch_path is not None:
                dispatch_hash = _sha256(bundle.dispatch_bytes)
                connection.execute(
                    """
                    INSERT INTO import_source
                        (source_id, repository_id, milestone_id, source_kind, source_path,
                         source_sha256, source_bytes, imported_at)
                    VALUES (?, ?, ?, 'dispatch', ?, ?, ?, ?)
                    """,
                    (
                        _source_record_id(
                            self.identity.repository_id,
                            self.identity.milestone_id,
                            "dispatch",
                            bundle.dispatch_path,
                            dispatch_hash,
                        ),
                        self.identity.repository_id,
                        self.identity.milestone_id,
                        bundle.dispatch_path,
                        dispatch_hash,
                        bundle.dispatch_bytes,
                        _now(),
                    ),
                )

            for sequence, event in enumerate(events, start=1):
                payload = _json_text(event).encode("utf-8")
                connection.execute(
                    """
                    INSERT INTO workflow_event
                        (repository_id, milestone_id, sequence, event_id, event_type,
                         event_timestamp, payload_json, payload_sha256, request_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
                    """,
                    (
                        self.identity.repository_id,
                        self.identity.milestone_id,
                        sequence,
                        event["event_id"],
                        event["event_type"],
                        event["timestamp"],
                        payload.decode("utf-8"),
                        _sha256(payload),
                    ),
                )
            projection = _projection(events)
            event_count = len(events)
            connection.execute(
                """
                UPDATE workflow_state
                SET store_revision = ?, event_count = ?, projection_json = ?
                WHERE repository_id = ? AND milestone_id = ?
                """,
                (
                    event_count,
                    event_count,
                    _json_text(projection),
                    self.identity.repository_id,
                    self.identity.milestone_id,
                ),
            )
            if dispatch_ledger is not None:
                matching_dispatches = [
                    item
                    for item in dispatch_ledger["dispatches"]
                    if item["milestone_id"] == self.identity.milestone_id
                ]
                if len(matching_dispatches) > MAX_DISPATCH_COUNT:
                    raise ImportSourceError(f"Fixture dispatch count exceeds {MAX_DISPATCH_COUNT}.")
                for record in matching_dispatches:
                    raw = _json_text(record)
                    connection.execute(
                        """
                        INSERT INTO architect_dispatch
                            (repository_id, dispatch_id, milestone_id, dispatch_type, status, body,
                             title, approved_milestone_id, related_event_id, source_sha256,
                             payload_json, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            self.identity.repository_id,
                            record["dispatch_id"],
                            record["milestone_id"],
                            record["type"],
                            record["status"],
                            record["body"],
                            record.get("title"),
                            record.get("approved_milestone_id"),
                            record.get("related_event_id"),
                            dispatch_hash,
                            raw,
                            record["created_at"],
                            record["updated_at"],
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO dispatch_history
                            (repository_id, dispatch_id, sequence, history_id, record_kind,
                             from_status, to_status, actor_role, actor_attribution, po_confirmed,
                             related_event_id, recorded_at, source_kind, payload_json)
                        VALUES (?, ?, 1, ?, 'LEGACY_SNAPSHOT', NULL, ?, 'repo_control', ?,
                                NULL, ?, ?, 'v1_import', ?)
                        """,
                        (
                            self.identity.repository_id,
                            record["dispatch_id"],
                            uuid.uuid4().hex,
                            record["status"],
                            "Imported current ledger snapshot; actor and historical decision are not authenticated.",
                            record.get("related_event_id"),
                            record["updated_at"],
                            raw,
                        ),
                    )

            result = {
                "repository_id": self.identity.repository_id,
                "milestone_id": self.identity.milestone_id,
                "prompt_document_id": prompt_doc_id,
                "prompt_revision_id": prompt_revision_id,
                "prompt_sha256": prompt_hash,
                "closeout_document_id": closeout_doc_id,
                "closeout_revision_id": closeout_revision_id,
                "closeout_sha256": closeout_hash,
                "imported_event_count": event_count,
                "linked_event_count": 0,
                "dispatch_count": len(matching_dispatches) if dispatch_ledger is not None else 0,
                "transaction_boundaries": "not_invented",
                "approved_prompt_revision_id": None,
            }
            connection.commit()
            return result
        except sqlite3.OperationalError as exc:
            if connection.in_transaction:
                connection.rollback()
            message = str(exc).lower()
            if "locked" in message or "busy" in message:
                raise BusyStoreError(
                    f"SQLite writer remained busy past the {BUSY_TIMEOUT_MS} ms fixture timeout."
                ) from exc
            raise
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def list_import_sources(self) -> list[dict[str, Any]]:
        connection = self._connection(read_only=True)
        try:
            self._verify_identity(connection, self.identity)
            rows = connection.execute(
                """
                SELECT source_id, source_kind, source_path, source_sha256, length(source_bytes) AS source_bytes
                FROM import_source
                WHERE repository_id = ? AND milestone_id = ?
                ORDER BY source_kind, source_path
                """,
                (self.identity.repository_id, self.identity.milestone_id),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()

    def read_import_source(self, source_id: str) -> bytes:
        connection = self._connection(read_only=True)
        try:
            self._verify_identity(connection, self.identity)
            row = connection.execute(
                """
                SELECT source_bytes, source_sha256 FROM import_source
                WHERE repository_id = ? AND milestone_id = ? AND source_id = ?
                """,
                (self.identity.repository_id, self.identity.milestone_id, source_id),
            ).fetchone()
            if row is None:
                raise ImportSourceError("Imported source record was not found.")
            raw = bytes(row["source_bytes"])
            if _sha256(raw) != row["source_sha256"]:
                raise IntegrityFailureError("Imported source bytes do not match their recorded hash.")
            return raw
        finally:
            connection.close()

    def create_pending_dispatch(
        self,
        *,
        dispatch_id: str,
        dispatch_type: str,
        body: str,
        created_at: str | None = None,
        title: str | None = None,
        related_event_id: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(dispatch_id, str) or not re.fullmatch(r"[a-f0-9]{32}", dispatch_id):
            raise ValueError("Dispatch ID must be 32 lowercase hexadecimal characters.")
        if dispatch_type not in _DISPATCH_TYPES:
            raise ValueError("Unsupported Architect dispatch type.")
        if not isinstance(body, str) or not body.strip() or len(body.encode("utf-8")) > architect_dispatches.MAX_BODY_BYTES:
            raise ValueError("Dispatch body is empty or exceeds the supported size.")
        if "\x00" in body or any(ord(char) < 32 and char not in "\t\n\r" for char in body):
            raise ValueError("Dispatch body contains unsupported control characters.")
        if title is not None and (not isinstance(title, str) or not title.strip() or len(title) > architect_dispatches.MAX_TITLE_CHARS):
            raise ValueError("Dispatch title is empty or exceeds the supported size.")
        timestamp = _validate_timestamp(created_at or _now())
        if related_event_id is not None:
            if not self._event_exists(related_event_id):
                raise DispatchDecisionError("Related workflow event does not exist in this milestone.")
        payload = {
            "dispatch_id": dispatch_id,
            "repository_id": self.identity.repository_id,
            "milestone_id": self.identity.milestone_id,
            "type": dispatch_type,
            "status": "PENDING",
            "body": body,
            "title": title,
            "created_at": timestamp,
            "updated_at": timestamp,
            "related_event_id": related_event_id,
        }
        raw = _json_text(payload)
        connection = self._connection()
        try:
            self._begin_immediate(connection)
            self._verify_identity(connection, self.identity)
            if related_event_id is not None and not connection.execute(
                """
                SELECT 1 FROM workflow_event
                WHERE repository_id = ? AND milestone_id = ? AND event_id = ?
                """,
                (self.identity.repository_id, self.identity.milestone_id, related_event_id),
            ).fetchone():
                raise DispatchDecisionError("Related workflow event does not exist in this milestone.")
            connection.execute(
                """
                INSERT INTO architect_dispatch
                    (repository_id, dispatch_id, milestone_id, dispatch_type, status, body, title,
                     approved_milestone_id, related_event_id, source_sha256, payload_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'PENDING', ?, ?, NULL, ?, NULL, ?, ?, ?)
                """,
                (
                    self.identity.repository_id,
                    dispatch_id,
                    self.identity.milestone_id,
                    dispatch_type,
                    body,
                    title,
                    related_event_id,
                    raw,
                    timestamp,
                    timestamp,
                ),
            )
            connection.execute(
                """
                INSERT INTO dispatch_history
                    (repository_id, dispatch_id, sequence, history_id, record_kind, from_status,
                     to_status, actor_role, actor_attribution, po_confirmed, related_event_id,
                     recorded_at, source_kind, payload_json)
                VALUES (?, ?, 1, ?, 'CREATED', NULL, 'PENDING', 'architect',
                        'Dispatch proposal; actor identity is not authenticated.', NULL, ?, ?, 'fixture', ?)
                """,
                (
                    self.identity.repository_id,
                    dispatch_id,
                    uuid.uuid4().hex,
                    related_event_id,
                    timestamp,
                    raw,
                ),
            )
            connection.commit()
            return self.get_dispatch(dispatch_id)
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def _event_exists(self, event_id: str) -> bool:
        connection = self._connection(read_only=True)
        try:
            return connection.execute(
                """
                SELECT 1 FROM workflow_event
                WHERE repository_id = ? AND milestone_id = ? AND event_id = ?
                """,
                (self.identity.repository_id, self.identity.milestone_id, event_id),
            ).fetchone() is not None
        finally:
            connection.close()

    def record_dispatch_decision(
        self,
        *,
        dispatch_id: str,
        history_id: str,
        status: str,
        actor_role: str,
        actor_attribution: str,
        trusted_fixture_po_confirmation: bool = False,
        approved_milestone_id: str | None = None,
        related_event_id: str | None = None,
        recorded_at: str | None = None,
    ) -> dict[str, Any]:
        if status not in {"APPROVED", "REJECTED"}:
            raise ValueError("Dispatch decision must be APPROVED or REJECTED.")
        if not isinstance(history_id, str) or not re.fullmatch(r"[a-f0-9]{32}", history_id):
            raise ValueError("Dispatch decision ID must be 32 lowercase hexadecimal characters.")
        if actor_role != "product_owner" or not actor_attribution:
            raise DispatchDecisionError("A fixture dispatch decision requires reported Product Owner context.")
        if not trusted_fixture_po_confirmation:
            raise DispatchDecisionError(
                "Dispatch approval/rejection requires explicit trusted fixture PO confirmation."
            )
        timestamp = _validate_timestamp(recorded_at or _now())
        if related_event_id is not None and not self._event_exists(related_event_id):
            raise DispatchDecisionError("Related workflow event does not exist in this milestone.")
        connection = self._connection()
        try:
            self._begin_immediate(connection)
            self._verify_identity(connection, self.identity)
            existing_history = connection.execute(
                """
                SELECT to_status, payload_json FROM dispatch_history
                WHERE repository_id = ? AND history_id = ?
                """,
                (self.identity.repository_id, history_id),
            ).fetchone()
            decision_payload = {
                "dispatch_id": dispatch_id,
                "status": status,
                "actor_role": actor_role,
                "actor_attribution": actor_attribution,
                "po_confirmed": True,
                "approved_milestone_id": approved_milestone_id,
                "related_event_id": related_event_id,
                "recorded_at": timestamp,
            }
            decision_json = _json_text(decision_payload)
            if existing_history is not None:
                if existing_history["payload_json"] != decision_json:
                    raise IdempotencyConflictError("Dispatch history ID was reused with different decision content.")
                connection.commit()
                return self.get_dispatch(dispatch_id)
            dispatch = connection.execute(
                """
                SELECT dispatch_type, status, milestone_id, payload_json, updated_at
                FROM architect_dispatch
                WHERE repository_id = ? AND dispatch_id = ?
                """,
                (self.identity.repository_id, dispatch_id),
            ).fetchone()
            if dispatch is None or dispatch["milestone_id"] != self.identity.milestone_id:
                raise DispatchDecisionError("Dispatch was not found for the selected fixture milestone.")
            if dispatch["status"] != "PENDING":
                raise DispatchDecisionError("Only a PENDING dispatch may receive a new fixture decision.")
            if status == "APPROVED" and dispatch["dispatch_type"] != "PROMPT":
                if approved_milestone_id != self.identity.milestone_id:
                    raise DispatchDecisionError("Approved guidance must bind to the selected milestone.")
            if related_event_id is not None and not connection.execute(
                """
                SELECT 1 FROM workflow_event
                WHERE repository_id = ? AND milestone_id = ? AND event_id = ?
                """,
                (self.identity.repository_id, self.identity.milestone_id, related_event_id),
            ).fetchone():
                raise DispatchDecisionError("Related workflow event does not exist in this milestone.")
            current_payload = json.loads(dispatch["payload_json"])
            current_payload.update(
                status=status,
                updated_at=timestamp,
                approved_milestone_id=approved_milestone_id,
                related_event_id=related_event_id,
            )
            sequence = connection.execute(
                """
                SELECT COALESCE(MAX(sequence), 0) + 1 FROM dispatch_history
                WHERE repository_id = ? AND dispatch_id = ?
                """,
                (self.identity.repository_id, dispatch_id),
            ).fetchone()[0]
            connection.execute(
                """
                UPDATE architect_dispatch
                SET status = ?, approved_milestone_id = ?, related_event_id = ?,
                    updated_at = ?, payload_json = ?
                WHERE repository_id = ? AND dispatch_id = ? AND status = 'PENDING'
                """,
                (
                    status,
                    approved_milestone_id,
                    related_event_id,
                    timestamp,
                    _json_text(current_payload),
                    self.identity.repository_id,
                    dispatch_id,
                ),
            )
            connection.execute(
                """
                INSERT INTO dispatch_history
                    (repository_id, dispatch_id, sequence, history_id, record_kind,
                     from_status, to_status, actor_role, actor_attribution, po_confirmed,
                     related_event_id, recorded_at, source_kind, payload_json)
                VALUES (?, ?, ?, ?, 'DECISION', 'PENDING', ?, ?, ?, 1, ?, ?, 'trusted_fixture', ?)
                """,
                (
                    self.identity.repository_id,
                    dispatch_id,
                    sequence,
                    history_id,
                    status,
                    actor_role,
                    actor_attribution,
                    related_event_id,
                    timestamp,
                    decision_json,
                ),
            )
            connection.commit()
            return self.get_dispatch(dispatch_id)
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def get_dispatch(self, dispatch_id: str) -> dict[str, Any]:
        connection = self._connection(read_only=True)
        try:
            self._verify_identity(connection, self.identity)
            row = connection.execute(
                """
                SELECT dispatch_id, milestone_id, dispatch_type, status, body, title,
                       approved_milestone_id, related_event_id, source_sha256, payload_json,
                       created_at, updated_at
                FROM architect_dispatch
                WHERE repository_id = ? AND dispatch_id = ?
                """,
                (self.identity.repository_id, dispatch_id),
            ).fetchone()
            if row is None:
                raise DispatchDecisionError("Dispatch was not found.")
            result = dict(row)
            result["payload"] = json.loads(result.pop("payload_json"))
            result["authorization_granted"] = False
            return result
        finally:
            connection.close()

    def list_dispatch_history(self, dispatch_id: str) -> list[dict[str, Any]]:
        connection = self._connection(read_only=True)
        try:
            self._verify_identity(connection, self.identity)
            rows = connection.execute(
                """
                SELECT sequence, history_id, record_kind, from_status, to_status, actor_role,
                       actor_attribution, po_confirmed, related_event_id, recorded_at,
                       source_kind, payload_json
                FROM dispatch_history
                WHERE repository_id = ? AND dispatch_id = ?
                ORDER BY sequence
                """,
                (self.identity.repository_id, dispatch_id),
            ).fetchall()
            return [
                {**dict(row), "payload": json.loads(row["payload_json"])}
                for row in rows
            ]
        finally:
            connection.close()
