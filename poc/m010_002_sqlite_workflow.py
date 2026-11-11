from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from repoctl.workflow import lifecycle


SCHEMA_VERSION = 1
BUSY_TIMEOUT_MS = 10_000
MAX_PAGE_SIZE = 100


class PocError(RuntimeError):
    pass


class IdentityConflict(PocError):
    pass


class PromptIdentityConflict(PocError):
    pass


class RevisionConflict(PocError):
    pass


class IdempotencyConflict(PocError):
    pass


class IntegrityFailure(PocError):
    pass


class CursorConflict(PocError):
    pass


@dataclass(frozen=True)
class WorkflowIdentity:
    repository_id: str
    milestone_id: str
    prompt_path: str
    prompt_sha256: str


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


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


class SQLiteWorkflowPoc:
    def __init__(
        self,
        path: str | Path,
        identity: WorkflowIdentity,
        *,
        initialize: bool = True,
    ) -> None:
        self.path = Path(path)
        self.identity = identity
        if initialize:
            self._initialize()
        else:
            self._verify_identity()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=BUSY_TIMEOUT_MS / 1000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            journal_mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            if journal_mode.lower() != "wal":
                raise PocError(f"SQLite refused WAL mode: {journal_mode!r}")

            schema_version = connection.execute("PRAGMA user_version").fetchone()[0]
            if schema_version not in {0, SCHEMA_VERSION}:
                raise PocError(f"Unsupported POC schema version: {schema_version}")
            if schema_version == 0:
                connection.executescript(
                    """
                    CREATE TABLE milestone_state (
                        repository_id TEXT NOT NULL,
                        milestone_id TEXT NOT NULL,
                        prompt_path TEXT NOT NULL,
                        prompt_sha256 TEXT NOT NULL,
                        revision INTEGER NOT NULL CHECK (revision >= 0),
                        event_count INTEGER NOT NULL CHECK (event_count >= 0),
                        projection_json TEXT NOT NULL,
                        PRIMARY KEY (repository_id, milestone_id)
                    );

                    CREATE TABLE transaction_receipt (
                        repository_id TEXT NOT NULL,
                        milestone_id TEXT NOT NULL,
                        transaction_id TEXT NOT NULL,
                        request_hash TEXT NOT NULL,
                        resulting_revision INTEGER NOT NULL,
                        result_json TEXT NOT NULL,
                        PRIMARY KEY (repository_id, milestone_id, transaction_id),
                        FOREIGN KEY (repository_id, milestone_id)
                            REFERENCES milestone_state (repository_id, milestone_id)
                    );

                    CREATE TABLE workflow_event (
                        repository_id TEXT NOT NULL,
                        milestone_id TEXT NOT NULL,
                        sequence INTEGER NOT NULL CHECK (sequence > 0),
                        event_id TEXT NOT NULL,
                        event_type TEXT NOT NULL,
                        event_timestamp TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        payload_sha256 TEXT NOT NULL,
                        transaction_id TEXT NOT NULL,
                        PRIMARY KEY (repository_id, milestone_id, sequence),
                        UNIQUE (repository_id, milestone_id, event_id),
                        FOREIGN KEY (repository_id, milestone_id)
                            REFERENCES milestone_state (repository_id, milestone_id),
                        FOREIGN KEY (repository_id, milestone_id, transaction_id)
                            REFERENCES transaction_receipt
                                (repository_id, milestone_id, transaction_id)
                    );

                    CREATE INDEX workflow_event_type_sequence
                        ON workflow_event
                            (repository_id, milestone_id, event_type, sequence);

                    CREATE TRIGGER workflow_event_no_update
                    BEFORE UPDATE ON workflow_event
                    BEGIN
                        SELECT RAISE(ABORT, 'workflow events are immutable');
                    END;

                    CREATE TRIGGER workflow_event_no_delete
                    BEFORE DELETE ON workflow_event
                    BEGIN
                        SELECT RAISE(ABORT, 'workflow events are immutable');
                    END;
                    """
                )
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

            projection = _projection([])
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT OR IGNORE INTO milestone_state
                    (repository_id, milestone_id, prompt_path, prompt_sha256,
                     revision, event_count, projection_json)
                VALUES (?, ?, ?, ?, 0, 0, ?)
                """,
                (
                    self.identity.repository_id,
                    self.identity.milestone_id,
                    self.identity.prompt_path,
                    self.identity.prompt_sha256,
                    _json_bytes(projection).decode("utf-8"),
                ),
            )
            row = self._milestone_row(connection)
            self._check_identity(row)
            connection.commit()
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def _milestone_row(self, connection: sqlite3.Connection) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT repository_id, milestone_id, prompt_path, prompt_sha256,
                   revision, event_count, projection_json
            FROM milestone_state
            WHERE repository_id = ? AND milestone_id = ?
            """,
            (self.identity.repository_id, self.identity.milestone_id),
        ).fetchone()
        if row is None:
            raise IdentityConflict("The disposable database has no matching milestone identity.")
        return row

    def _check_identity(self, row: sqlite3.Row) -> None:
        actual = (
            row["repository_id"],
            row["milestone_id"],
            row["prompt_path"],
            row["prompt_sha256"],
        )
        expected = (
            self.identity.repository_id,
            self.identity.milestone_id,
            self.identity.prompt_path,
            self.identity.prompt_sha256,
        )
        if actual != expected:
            raise IdentityConflict("The disposable database identity does not match the requested fixture.")

    def _verify_identity(self) -> None:
        connection = self._connect()
        try:
            schema_version = connection.execute("PRAGMA user_version").fetchone()[0]
            if schema_version != SCHEMA_VERSION:
                raise PocError(f"Unsupported POC schema version: {schema_version}")
            self._check_identity(self._milestone_row(connection))
        finally:
            connection.close()

    def _load_events(self, connection: sqlite3.Connection) -> list[dict[str, Any]]:
        rows = connection.execute(
            """
            SELECT sequence, event_id, event_type, event_timestamp,
                   payload_json, payload_sha256
            FROM workflow_event
            WHERE repository_id = ? AND milestone_id = ?
            ORDER BY sequence
            """,
            (self.identity.repository_id, self.identity.milestone_id),
        ).fetchall()
        events: list[dict[str, Any]] = []
        for expected_sequence, row in enumerate(rows, start=1):
            if row["sequence"] != expected_sequence:
                raise IntegrityFailure("Workflow event sequence is not contiguous.")
            payload = row["payload_json"].encode("utf-8")
            if hashlib.sha256(payload).hexdigest() != row["payload_sha256"]:
                raise IntegrityFailure(f"Payload hash mismatch at sequence {expected_sequence}.")
            try:
                event = json.loads(payload)
            except json.JSONDecodeError as exc:
                raise IntegrityFailure(
                    f"Malformed event payload at sequence {expected_sequence}."
                ) from exc
            if (
                not isinstance(event, dict)
                or event.get("event_id") != row["event_id"]
                or event.get("event_type") != row["event_type"]
                or event.get("timestamp") != row["event_timestamp"]
            ):
                raise IntegrityFailure(f"Event index mismatch at sequence {expected_sequence}.")
            events.append(event)
        return events

    def _validate_projection(
        self,
        row: sqlite3.Row,
        events: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if row["event_count"] != len(events):
            raise IntegrityFailure("Stored event count does not match event rows.")
        try:
            stored = json.loads(row["projection_json"])
        except json.JSONDecodeError as exc:
            raise IntegrityFailure("Stored workflow projection is malformed.") from exc
        replayed = _projection(events)
        if stored != replayed:
            raise IntegrityFailure("Stored projection does not match reducer replay.")
        return replayed

    def snapshot(self) -> dict[str, Any]:
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            row = self._milestone_row(connection)
            self._check_identity(row)
            events = self._load_events(connection)
            projection = self._validate_projection(row, events)
            result = {
                "revision": row["revision"],
                "event_count": row["event_count"],
                "projection": projection,
                "events": events,
            }
            connection.commit()
            return result
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def audit(self) -> dict[str, Any]:
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            integrity = [
                row[0] for row in connection.execute("PRAGMA integrity_check").fetchall()
            ]
            if integrity != ["ok"]:
                raise IntegrityFailure(f"SQLite integrity check failed: {integrity!r}")
            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise IntegrityFailure(f"SQLite foreign-key check failed: {len(violations)} violations.")

            row = self._milestone_row(connection)
            self._check_identity(row)
            events = self._load_events(connection)
            projection = self._validate_projection(row, events)
            receipt_count = connection.execute(
                """
                SELECT COUNT(*)
                FROM transaction_receipt
                WHERE repository_id = ? AND milestone_id = ?
                """,
                (self.identity.repository_id, self.identity.milestone_id),
            ).fetchone()[0]
            if receipt_count != row["revision"]:
                raise IntegrityFailure("Stored revision does not match committed transaction receipts.")
            result = {
                "revision": row["revision"],
                "event_count": row["event_count"],
                "projection": projection,
                "sqlite_integrity": "ok",
                "receipt_count": receipt_count,
                "payload_hashes_verified": len(events),
            }
            connection.commit()
            return result
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def _request_hash(
        self,
        expected_revision: int,
        prompt_sha256: str,
        payloads: list[bytes],
    ) -> str:
        request = {
            "repository_id": self.identity.repository_id,
            "milestone_id": self.identity.milestone_id,
            "prompt_path": self.identity.prompt_path,
            "prompt_sha256": prompt_sha256,
            "expected_revision": expected_revision,
            "events": [json.loads(payload) for payload in payloads],
        }
        return hashlib.sha256(_json_bytes(request)).hexdigest()

    def _write_projection(
        self,
        connection: sqlite3.Connection,
        expected_revision: int,
        new_revision: int,
        events: list[dict[str, Any]],
        projection: dict[str, Any],
    ) -> None:
        cursor = connection.execute(
            """
            UPDATE milestone_state
            SET revision = ?, event_count = ?, projection_json = ?
            WHERE repository_id = ? AND milestone_id = ? AND revision = ?
            """,
            (
                new_revision,
                len(events),
                _json_bytes(projection).decode("utf-8"),
                self.identity.repository_id,
                self.identity.milestone_id,
                expected_revision,
            ),
        )
        if cursor.rowcount != 1:
            raise RevisionConflict("Workflow revision changed before projection update.")

    def append(
        self,
        *,
        transaction_id: str,
        expected_revision: int,
        prompt_sha256: str,
        events: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if not transaction_id:
            raise ValueError("Transaction ID is required.")
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("Expected revision must be a non-negative integer.")
        if not events:
            raise ValueError("At least one workflow event is required.")
        if prompt_sha256 != self.identity.prompt_sha256:
            raise PromptIdentityConflict("Prompt identity changed before append.")

        payloads = [_json_bytes(event) for event in events]
        request_hash = self._request_hash(expected_revision, prompt_sha256, payloads)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            receipt = connection.execute(
                """
                SELECT request_hash, result_json
                FROM transaction_receipt
                WHERE repository_id = ? AND milestone_id = ? AND transaction_id = ?
                """,
                (
                    self.identity.repository_id,
                    self.identity.milestone_id,
                    transaction_id,
                ),
            ).fetchone()
            if receipt is not None:
                if receipt["request_hash"] != request_hash:
                    raise IdempotencyConflict(
                        "Transaction ID was already used with a different request."
                    )
                result = json.loads(receipt["result_json"])
                connection.commit()
                return result

            row = self._milestone_row(connection)
            self._check_identity(row)
            if row["revision"] != expected_revision:
                raise RevisionConflict("Expected workflow revision is stale.")

            previous_events = self._load_events(connection)
            previous_projection = self._validate_projection(row, previous_events)
            candidate_events = previous_events + events
            projection = _projection(candidate_events)
            first_sequence = len(previous_events) + 1
            new_revision = expected_revision + 1

            connection.execute(
                """
                INSERT INTO transaction_receipt
                    (repository_id, milestone_id, transaction_id, request_hash,
                     resulting_revision, result_json)
                VALUES (?, ?, ?, ?, ?, '{}')
                """,
                (
                    self.identity.repository_id,
                    self.identity.milestone_id,
                    transaction_id,
                    request_hash,
                    new_revision,
                ),
            )
            for offset, (event, payload) in enumerate(zip(events, payloads, strict=True)):
                connection.execute(
                    """
                    INSERT INTO workflow_event
                        (repository_id, milestone_id, sequence, event_id, event_type,
                         event_timestamp, payload_json, payload_sha256, transaction_id)
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
                        hashlib.sha256(payload).hexdigest(),
                        transaction_id,
                    ),
                )

            self._write_projection(
                connection,
                expected_revision,
                new_revision,
                candidate_events,
                projection,
            )
            result = {
                "transaction_id": transaction_id,
                "request_hash": request_hash,
                "revision": new_revision,
                "first_sequence": first_sequence,
                "last_sequence": first_sequence + len(events) - 1,
                "event_ids": [event["event_id"] for event in events],
            }
            connection.execute(
                """
                UPDATE transaction_receipt
                SET result_json = ?
                WHERE repository_id = ? AND milestone_id = ? AND transaction_id = ?
                """,
                (
                    _json_bytes(result).decode("utf-8"),
                    self.identity.repository_id,
                    self.identity.milestone_id,
                    transaction_id,
                ),
            )
            connection.commit()
            return result
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def page(
        self,
        *,
        snapshot_revision: int,
        snapshot_event_count: int,
        after_sequence: int = 0,
        limit: int = MAX_PAGE_SIZE,
    ) -> dict[str, Any]:
        if type(snapshot_revision) is not int or snapshot_revision < 0:
            raise ValueError("Snapshot revision must be a non-negative integer.")
        if type(snapshot_event_count) is not int or snapshot_event_count < 0:
            raise ValueError("Snapshot event count must be a non-negative integer.")
        if type(after_sequence) is not int or after_sequence < 0:
            raise ValueError("Page sequence must be a non-negative integer.")
        if type(limit) is not int or not 1 <= limit <= MAX_PAGE_SIZE:
            raise ValueError(f"Page size must be between 1 and {MAX_PAGE_SIZE}.")

        connection = self._connect()
        try:
            connection.execute("BEGIN")
            row = self._milestone_row(connection)
            self._check_identity(row)
            if snapshot_revision > row["revision"]:
                raise CursorConflict("Snapshot revision is ahead of the stored workflow.")
            if snapshot_revision == row["revision"]:
                boundary = row["event_count"]
            elif snapshot_revision == 0:
                boundary = 0
            else:
                receipt = connection.execute(
                    """
                    SELECT result_json
                    FROM transaction_receipt
                    WHERE repository_id = ? AND milestone_id = ?
                        AND resulting_revision = ?
                    """,
                    (
                        self.identity.repository_id,
                        self.identity.milestone_id,
                        snapshot_revision,
                    ),
                ).fetchone()
                if receipt is None:
                    raise CursorConflict("Snapshot revision is no longer available.")
                try:
                    boundary = json.loads(receipt["result_json"])["last_sequence"]
                except (KeyError, json.JSONDecodeError, TypeError) as exc:
                    raise IntegrityFailure("Snapshot transaction receipt is malformed.") from exc
                if type(boundary) is not int or boundary < 0:
                    raise IntegrityFailure("Snapshot transaction receipt has an invalid sequence boundary.")
            if snapshot_event_count != boundary:
                raise CursorConflict("Snapshot event count does not match its revision.")
            if after_sequence > boundary:
                raise CursorConflict("Page cursor is beyond the requested snapshot.")
            rows = connection.execute(
                """
                SELECT sequence, event_id, event_type, event_timestamp,
                       payload_json, payload_sha256
                FROM workflow_event
                WHERE repository_id = ? AND milestone_id = ?
                    AND sequence > ? AND sequence <= ?
                ORDER BY sequence
                LIMIT ?
                """,
                (
                    self.identity.repository_id,
                    self.identity.milestone_id,
                    after_sequence,
                    boundary,
                    limit,
                ),
            ).fetchall()
            events = self._decode_page(rows, after_sequence)
            result = {
                "revision": snapshot_revision,
                "snapshot_event_count": snapshot_event_count,
                "events": events,
                "next_sequence": events[-1]["sequence"] if events else after_sequence,
            }
            connection.commit()
            return result
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def _decode_page(
        self,
        rows: list[sqlite3.Row],
        after_sequence: int,
    ) -> list[dict[str, Any]]:
        events = []
        expected_sequence = after_sequence + 1
        for row in rows:
            if row["sequence"] != expected_sequence:
                raise IntegrityFailure("Page contains a sequence gap.")
            payload = row["payload_json"].encode("utf-8")
            if hashlib.sha256(payload).hexdigest() != row["payload_sha256"]:
                raise IntegrityFailure(f"Payload hash mismatch at sequence {expected_sequence}.")
            try:
                event = json.loads(payload)
            except json.JSONDecodeError as exc:
                raise IntegrityFailure(
                    f"Malformed event payload at sequence {expected_sequence}."
                ) from exc
            if (
                not isinstance(event, dict)
                or event.get("event_id") != row["event_id"]
                or event.get("event_type") != row["event_type"]
                or event.get("timestamp") != row["event_timestamp"]
            ):
                raise IntegrityFailure(f"Event index mismatch at sequence {expected_sequence}.")
            events.append({"sequence": row["sequence"], **event})
            expected_sequence += 1
        return events

    def settings(self) -> dict[str, Any]:
        connection = self._connect()
        try:
            return {
                "journal_mode": connection.execute("PRAGMA journal_mode").fetchone()[0],
                "synchronous": connection.execute("PRAGMA synchronous").fetchone()[0],
                "foreign_keys": connection.execute("PRAGMA foreign_keys").fetchone()[0],
                "busy_timeout_ms": connection.execute("PRAGMA busy_timeout").fetchone()[0],
                "user_version": connection.execute("PRAGMA user_version").fetchone()[0],
            }
        finally:
            connection.close()
