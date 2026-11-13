from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import tempfile
import uuid
from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Sequence

from .scanner.core import state_artifact_lock
from .store_authority import (
    StoreAuthorityConfiguration,
    StoreAuthorityError,
    load_store_authority,
    verify_store_registry,
)
from .sqlite_runtime import MINIMUM_SQLITE_VERSION, sqlite_runtime_status
from .workflow import architect_dispatches, lifecycle


APPLICATION_ID = 0x52434953
SCHEMA_VERSION = 3
SCHEMA_ID = "repoctl.information-store:v3"
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
_PUBLICATION_TRANSITIONS = {
    "accepted_not_published": {"accepted_not_published", "preview_ready", "failed", "drifted"},
    "preview_ready": {"preview_ready", "materialized_unstaged", "staged", "committed", "failed", "drifted"},
    "materialized_unstaged": {
        "materialized_unstaged", "staged", "committed", "failed", "drifted",
    },
    "staged": {"staged", "committed", "failed", "drifted"},
    "committed": {"committed"},
    "failed": {"failed", "materialized_unstaged", "staged", "committed", "drifted"},
    "drifted": {"drifted"},
}


class InformationStoreError(RuntimeError):
    pass


def dispatch_reconciliation_fingerprint(dispatch: Mapping[str, Any]) -> str:
    fields = (
        "dispatch_id",
        "milestone_id",
        "dispatch_type",
        "status",
        "body",
        "title",
        "approved_milestone_id",
        "related_event_id",
        "source_sha256",
        "created_at",
        "updated_at",
    )
    return _sha256(_json_text([dispatch[field] for field in fields]).encode("utf-8"))


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
    closeout_path: str | None
    workflow_path: str | None
    prompt_bytes: bytes
    closeout_bytes: bytes | None
    workflow_bytes: bytes | None
    dispatch_path: str | None = None
    dispatch_bytes: bytes | None = None
    reconciliation_receipt: Mapping[str, Any] | None = None
    closeout_reconciliation_receipt: Mapping[str, Any] | None = None


_HISTORICAL_PATH_RECONCILIATIONS: dict[str, dict[str, Any]] = {
    "M001.000": {
        "repository_id": "vocab-app--0d81efec80ef",
        "repository_key": "vocab-app",
        "repository_root": "/home/chuck/projects/vocab-app",
        "original_declared_prompt_path": (
            "milestones/M001.000_vocab_app_repo_control_onboarding_reconnaissance_prompt.md"
        ),
        "canonical_prompt_path": (
            "docs/milestones/M001.000_vocab_app_repo_control_onboarding_reconnaissance_prompt.md"
        ),
        "prompt_sha256": "1191b1965e26178a0a415aa83062353e8e678ed1a90243d121212c26ab9051cd",
        "workflow_sha256": "596bbc944a9bfe56e78739a5e82ec7680044e078bb510a159057e89c0f92f7ce",
        "original_declared_closeout_path": (
            "milestones/M001.000_vocab_app_repo_control_onboarding_reconnaissance_closeout.md"
        ),
        "canonical_closeout_path": (
            "docs/milestones/M001.000_vocab_app_repo_control_onboarding_reconnaissance_closeout.md"
        ),
        "closeout_sha256": "4baf6bdcf9cc4c881d486f27e920652450700941e3b07cdbf9c0d8cd34c7d53e",
        "closeout_submission_count": 1,
        "rationale": (
            "The registered Vocab App prompt has one canonical docs/milestones target; "
            "the unchanged historical sidecar records its former path without the docs/ prefix."
        ),
        "closeout_rationale": (
            "The Product Owner approved this exact current canonical closeout as the best-supported "
            "archival counterpart; the original sidecar-declared bytes and event-time revision are unavailable."
        ),
    },
    "M001.001": {
        "repository_id": "vocab-app--0d81efec80ef",
        "repository_key": "vocab-app",
        "repository_root": "/home/chuck/projects/vocab-app",
        "original_declared_prompt_path": (
            "milestones/M001.001_vocab_app_governing_structure_and_shared_skill_onboarding_prompt.md"
        ),
        "canonical_prompt_path": (
            "docs/milestones/M001.001_vocab_app_governing_structure_and_shared_skill_onboarding_prompt.md"
        ),
        "prompt_sha256": "e1747e874a70e72d8b528f0a42be46ad4ca78e4497197ff82f657d6cb4bb44c8",
        "workflow_sha256": "97aae7434c5a4001073b69e2e84d76e847591803bf7545a6724141629d695c84",
        "original_declared_closeout_path": (
            "milestones/M001.001_vocab_app_governing_structure_and_shared_skill_onboarding_closeout.md"
        ),
        "canonical_closeout_path": (
            "docs/milestones/M001.001_vocab_app_governing_structure_and_shared_skill_onboarding_closeout.md"
        ),
        "closeout_sha256": "bead14b3fe59dcdc6624879caadc45c2a9398bc5fbd85169721db793b62d2868",
        "closeout_submission_count": 3,
        "rationale": (
            "The registered Vocab App prompt has one canonical docs/milestones target; "
            "the unchanged historical sidecar records its former path without the docs/ prefix."
        ),
        "closeout_rationale": (
            "The Product Owner approved this exact current canonical closeout as the best-supported "
            "archival counterpart; the original sidecar-declared bytes and event-time revisions are unavailable."
        ),
    },
}


def historical_prompt_path_alias(identity: StoreIdentity) -> str | None:
    rule = _HISTORICAL_PATH_RECONCILIATIONS.get(identity.milestone_id)
    if (
        rule is None
        or identity.repository_id != rule["repository_id"]
        or identity.repository_key != rule["repository_key"]
        or identity.repository_root != rule["repository_root"]
    ):
        return None
    return rule["original_declared_prompt_path"]


def build_historical_path_reconciliation_receipt(
    identity: StoreIdentity,
    *,
    original_declared_prompt_path: str,
    workflow_path: str,
    prompt_path: str,
    prompt_bytes: bytes,
    workflow_bytes: bytes,
) -> dict[str, str] | None:
    rule = _HISTORICAL_PATH_RECONCILIATIONS.get(identity.milestone_id)
    if rule is None:
        return None
    expected_workflow_path = prompt_path.removesuffix("_prompt.md") + "_workflow.md"
    if (
        identity.repository_id != rule["repository_id"]
        or identity.repository_key != rule["repository_key"]
        or identity.repository_root != rule["repository_root"]
        or identity.prompt_path != rule["canonical_prompt_path"]
        or prompt_path != rule["canonical_prompt_path"]
        or workflow_path != expected_workflow_path
        or original_declared_prompt_path != rule["original_declared_prompt_path"]
        or _sha256(prompt_bytes) != rule["prompt_sha256"]
        or _sha256(workflow_bytes) != rule["workflow_sha256"]
    ):
        return None
    receipt = {
        "category": "historical_reconciled",
        "rule_id": f"vocab-app-{identity.milestone_id.lower()}-prompt-path-v1",
        "repository_id": identity.repository_id,
        "repository_key": identity.repository_key,
        "repository_root": identity.repository_root,
        "milestone_id": identity.milestone_id,
        "original_declared_prompt_path": original_declared_prompt_path,
        "mapped_target_prompt_path": prompt_path,
        "workflow_source_path": workflow_path,
        "prompt_sha256": rule["prompt_sha256"],
        "workflow_sha256": rule["workflow_sha256"],
        "rationale": rule["rationale"],
    }
    receipt["reconciliation_receipt_sha256"] = _sha256(_json_text(receipt).encode("utf-8"))
    return receipt


def _validate_reconciliation_receipt(
    receipt: Mapping[str, Any] | None,
    identity: StoreIdentity,
    *,
    prompt_path: str,
    prompt_bytes: bytes,
    workflow_path: str | None,
    workflow_bytes: bytes | None,
) -> dict[str, str] | None:
    if receipt is None:
        return None
    if workflow_path is None or workflow_bytes is None:
        raise ImportSourceError("Historical reconciliation requires its original workflow source.")
    original_path = receipt.get("original_declared_prompt_path")
    expected = build_historical_path_reconciliation_receipt(
        identity,
        original_declared_prompt_path=original_path if isinstance(original_path, str) else "",
        workflow_path=workflow_path,
        prompt_path=prompt_path,
        prompt_bytes=prompt_bytes,
        workflow_bytes=workflow_bytes,
    )
    if expected is None or dict(receipt) != expected:
        raise ImportSourceError("Historical reconciliation receipt does not match an allowed source identity.")
    return expected


def build_historical_closeout_path_reconciliation_receipt(
    identity: StoreIdentity,
    *,
    prompt_reconciliation_receipt: Mapping[str, Any],
    prompt_path: str,
    prompt_bytes: bytes,
    workflow_path: str,
    workflow_bytes: bytes,
    closeout_path: str,
    closeout_bytes: bytes,
    canonical_target_match_count: int,
) -> dict[str, str] | None:
    rule = _HISTORICAL_PATH_RECONCILIATIONS.get(identity.milestone_id)
    if rule is None or type(canonical_target_match_count) is not int or canonical_target_match_count != 1:
        return None
    if (
        identity.repository_id != rule["repository_id"]
        or identity.repository_key != rule["repository_key"]
        or identity.repository_root != rule["repository_root"]
        or identity.prompt_path != rule["canonical_prompt_path"]
        or prompt_path != rule["canonical_prompt_path"]
        or workflow_path != prompt_path.removesuffix("_prompt.md") + "_workflow.md"
        or closeout_path != rule["canonical_closeout_path"]
        or not isinstance(prompt_bytes, bytes)
        or not isinstance(workflow_bytes, bytes)
        or not isinstance(closeout_bytes, bytes)
        or _sha256(prompt_bytes) != rule["prompt_sha256"]
        or _sha256(workflow_bytes) != rule["workflow_sha256"]
        or _sha256(closeout_bytes) != rule["closeout_sha256"]
    ):
        return None
    try:
        validated_prompt_receipt = _validate_reconciliation_receipt(
            prompt_reconciliation_receipt,
            identity,
            prompt_path=prompt_path,
            prompt_bytes=prompt_bytes,
            workflow_path=workflow_path,
            workflow_bytes=workflow_bytes,
        )
        if validated_prompt_receipt is None:
            return None
        events = _parse_sidecar_bytes(
            workflow_bytes,
            identity,
            reconciliation_receipt=validated_prompt_receipt,
            prompt_path=prompt_path,
            prompt_bytes=prompt_bytes,
            workflow_path=workflow_path,
        )
    except ImportSourceError:
        return None
    closeout_events = [
        event for event in events
        if event["event_type"] == "closeout_submitted"
    ]
    if (
        len(closeout_events) != rule["closeout_submission_count"]
        or any(
            event.get("evidence_path") != rule["original_declared_closeout_path"]
            for event in closeout_events
        )
    ):
        return None
    receipt = {
        "category": "historical_reconciled_unverified_original",
        "rule_id": f"vocab-app-{identity.milestone_id.lower()}-closeout-path-v1",
        "repository_id": identity.repository_id,
        "repository_key": identity.repository_key,
        "repository_root": identity.repository_root,
        "milestone_id": identity.milestone_id,
        "prompt_source_path": prompt_path,
        "prompt_sha256": rule["prompt_sha256"],
        "prompt_source_bytes": str(len(prompt_bytes)),
        "workflow_source_path": workflow_path,
        "workflow_sha256": rule["workflow_sha256"],
        "workflow_source_bytes": str(len(workflow_bytes)),
        "original_declared_closeout_path": rule["original_declared_closeout_path"],
        "mapped_target_closeout_path": closeout_path,
        "mapped_closeout_sha256": rule["closeout_sha256"],
        "mapped_closeout_source_bytes": str(len(closeout_bytes)),
        "original_closeout_sha256": "UNKNOWN/NOT VERIFIED",
        "original_closeout_bytes": "UNKNOWN/NOT VERIFIED",
        "event_time_revision_identity": "UNKNOWN/NOT VERIFIED",
        "closeout_submission_count": str(len(closeout_events)),
        "canonical_target_match_count": "1",
        "target_selection": "Unique exact canonical source in the registered repository inventory.",
        "source_provenance": "Current working-tree source; original missing source bytes are not asserted identical.",
        "rationale": rule["closeout_rationale"],
    }
    receipt["reconciliation_receipt_sha256"] = _sha256(_json_text(receipt).encode("utf-8"))
    return receipt


def _validate_historical_closeout_path_reconciliation_receipt(
    receipt: Mapping[str, Any] | None,
    identity: StoreIdentity,
    *,
    prompt_reconciliation_receipt: Mapping[str, Any] | None,
    prompt_path: str,
    prompt_bytes: bytes,
    workflow_path: str | None,
    workflow_bytes: bytes | None,
    closeout_path: str | None,
    closeout_bytes: bytes | None,
) -> dict[str, str] | None:
    if receipt is None:
        return None
    if (
        prompt_reconciliation_receipt is None
        or workflow_path is None
        or workflow_bytes is None
        or closeout_path is None
        or closeout_bytes is None
    ):
        raise ImportSourceError("Historical closeout reconciliation requires its exact prompt, workflow, and closeout sources.")
    target_match_count = 1 if receipt.get("canonical_target_match_count") == "1" else 0
    expected = build_historical_closeout_path_reconciliation_receipt(
        identity,
        prompt_reconciliation_receipt=prompt_reconciliation_receipt,
        prompt_path=prompt_path,
        prompt_bytes=prompt_bytes,
        workflow_path=workflow_path,
        workflow_bytes=workflow_bytes,
        closeout_path=closeout_path,
        closeout_bytes=closeout_bytes,
        canonical_target_match_count=target_match_count,
    )
    if expected is None or dict(receipt) != expected:
        raise ImportSourceError("Historical closeout reconciliation receipt does not match an approved source identity.")
    return expected


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
        FOREIGN KEY (repository_id) REFERENCES repository(repository_id)
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
        request_id TEXT NOT NULL,
        request_hash TEXT NOT NULL,
        document_id TEXT NOT NULL,
        revision_id TEXT NOT NULL,
        content_sha256 TEXT NOT NULL,
        target_path TEXT NOT NULL,
        target_branch TEXT NOT NULL,
        target_head TEXT NOT NULL,
        worktree_fingerprint TEXT NOT NULL,
        index_fingerprint TEXT NOT NULL,
        preview_fingerprint TEXT NOT NULL,
        git_observation_json TEXT NOT NULL,
        status TEXT NOT NULL CHECK (
            status IN (
                'accepted_not_published', 'preview_ready', 'materialized_unstaged',
                'staged', 'committed', 'failed', 'drifted'
            )
        ),
        related_commit_id TEXT,
        failure_reason TEXT,
        confirmed_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (repository_id, publication_id),
        UNIQUE (repository_id, milestone_id, request_id),
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
_DOCUMENT_PUBLICATION_COLUMNS = {
    "repository_id",
    "milestone_id",
    "publication_id",
    "request_id",
    "request_hash",
    "document_id",
    "revision_id",
    "content_sha256",
    "target_path",
    "target_branch",
    "target_head",
    "worktree_fingerprint",
    "index_fingerprint",
    "preview_fingerprint",
    "git_observation_json",
    "status",
    "related_commit_id",
    "failure_reason",
    "confirmed_at",
    "created_at",
    "updated_at",
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


def _validate_dispatch_source_path(value: str, identity: StoreIdentity) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("Dispatch source path must be a non-empty absolute path.")
    path = Path(value).expanduser()
    if (
        not path.is_absolute()
        or path.name != "architect_dispatches.json"
        or path.parent.name != identity.repository_id
        or path.is_symlink()
    ):
        raise ImportSourceError("Dispatch source path must identify the selected repository's external ledger.")
    resolved = path.resolve(strict=False)
    repository_root = Path(identity.repository_root).expanduser().resolve(strict=False)
    if resolved.is_relative_to(repository_root):
        raise ImportSourceError("Central dispatch source path must remain outside the repository.")
    return str(resolved)


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


def _validate_selection_root(value: str | Path, repository_root: str, *, fixture: bool) -> Path:
    candidate = Path(value).expanduser()
    if candidate.is_symlink() or not candidate.is_dir():
        raise ValueError("Selection state must be an existing real directory.")
    root = candidate.resolve(strict=True)
    temporary_root = Path(tempfile.gettempdir()).resolve(strict=True)
    repository = Path(repository_root).resolve(strict=False)
    if (
        root == repository
        or root.is_relative_to(repository)
        or repository.is_relative_to(root)
    ):
        raise ValueError("Selection state must be separate from the repository.")
    if fixture and (root == temporary_root or not root.is_relative_to(temporary_root)):
        raise ValueError("Fixture selection state must be under a separate temporary directory.")
    return root


def _validate_fixture_selection_root(value: str | Path, repository_root: str) -> Path:
    return _validate_selection_root(value, repository_root, fixture=True)


def _read_fixture_selection_snapshot(state_root: Path, repository_id: str) -> tuple[dict[str, Any], str]:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", repository_id):
        raise IdentityMismatchError("Repository ID cannot be used as a selection path component.")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptors: list[int] = []
    try:
        descriptors.append(os.open(state_root, directory_flags))
        descriptors.append(os.open(repository_id, directory_flags, dir_fd=descriptors[-1]))
        descriptors.append(os.open("milestones", directory_flags, dir_fd=descriptors[-1]))
        descriptor = os.open("selection.json", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=descriptors[-1])
        with os.fdopen(descriptor, "rb") as source:
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > 16_384:
                raise StaleStateError("External fixture selection is not a bounded regular file.")
            content = source.read(16_385)
            after = os.fstat(source.fileno())
            named = os.stat("selection.json", dir_fd=descriptors[-1], follow_symlinks=False)
        if (
            len(content) > 16_384
            or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            or (before.st_dev, before.st_ino) != (named.st_dev, named.st_ino)
        ):
            raise StaleStateError("External fixture selection changed while being read.")
        try:
            payload = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StaleStateError("External fixture selection is malformed.") from exc
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise StaleStateError("External fixture selection has an unsupported record shape.")
        return payload, _sha256(content)
    except OSError as exc:
        raise StaleStateError(f"External fixture selection is unavailable or unsafe: {exc}") from exc
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _read_fixture_selection(state_root: Path, repository_id: str) -> dict[str, Any]:
    return _read_fixture_selection_snapshot(state_root, repository_id)[0]


def _write_fixture_selection(state_root: Path, repository_id: str, payload: Mapping[str, Any]) -> None:
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptors: list[int] = []
    temporary_name = f".selection-{uuid.uuid4().hex}.tmp"
    created = False
    try:
        descriptors.append(os.open(state_root, directory_flags))
        descriptors.append(os.open(repository_id, directory_flags, dir_fd=descriptors[-1]))
        descriptors.append(os.open("milestones", directory_flags, dir_fd=descriptors[-1]))
        descriptor = os.open(
            temporary_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600, dir_fd=descriptors[-1],
        )
        created = True
        with os.fdopen(descriptor, "wb") as target:
            target.write(_json_text(dict(payload), sort_keys=True).encode("utf-8") + b"\n")
            target.flush()
            os.fsync(target.fileno())
        os.replace(
            temporary_name, "selection.json",
            src_dir_fd=descriptors[-1], dst_dir_fd=descriptors[-1],
        )
        created = False
        os.fsync(descriptors[-1])
    except OSError as exc:
        raise StaleStateError(f"External fixture selection could not be written safely: {exc}") from exc
    finally:
        if created and descriptors:
            os.unlink(temporary_name, dir_fd=descriptors[-1])
        for descriptor in reversed(descriptors):
            os.close(descriptor)


class _SelectionLockedConnection(sqlite3.Connection):
    def bind_selection_guard(
        self,
        guard: AbstractContextManager[None] | None,
        validate: Callable[[], None],
        validate_marker: Callable[[sqlite3.Connection], None],
        managed: bool,
    ) -> None:
        self._selection_guard = guard
        self._validate_selection = validate
        self._validate_marker = validate_marker
        self._managed = managed
        self._committed_changes = self.total_changes

    def validate_authority(self, *, selection: bool = True) -> None:
        if selection:
            self._validate_selection()
        self._validate_marker(self)

    def mark_managed_write(self) -> None:
        if self._managed and self.in_transaction and self.total_changes > self._committed_changes:
            self.execute(
                "UPDATE store_metadata SET value = '1' WHERE key = 'authority_first_write' AND value = '0'"
            )

    def commit(self) -> None:
        self.validate_authority()
        self.mark_managed_write()
        super().commit()
        self._committed_changes = self.total_changes

    def rollback(self) -> None:
        super().rollback()
        self._committed_changes = self.total_changes

    def close(self) -> None:
        try:
            super().close()
        finally:
            guard = getattr(self, "_selection_guard", None)
            if guard is not None:
                self._selection_guard = None
                guard.__exit__(None, None, None)


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


def _parse_sidecar_bytes(
    source: bytes,
    identity: StoreIdentity,
    *,
    reconciliation_receipt: Mapping[str, Any] | None = None,
    prompt_path: str | None = None,
    prompt_bytes: bytes | None = None,
    workflow_path: str | None = None,
) -> list[dict[str, Any]]:
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
    if not isinstance(ledger, dict):
        raise ImportSourceError("Workflow sidecar identity, schema, or event collection is invalid.")
    declared_prompt_path = ledger.get("prompt_path")
    accepted_prompt_path = identity.prompt_path
    if reconciliation_receipt is not None:
        expected_receipt = _validate_reconciliation_receipt(
            reconciliation_receipt,
            identity,
            prompt_path=prompt_path if prompt_path is not None else identity.prompt_path,
            prompt_bytes=prompt_bytes if prompt_bytes is not None else b"",
            workflow_path=workflow_path,
            workflow_bytes=source,
        )
        if (
            expected_receipt is None
            or declared_prompt_path != expected_receipt["original_declared_prompt_path"]
        ):
            raise ImportSourceError("Historical reconciliation receipt does not match the sidecar declaration.")
        accepted_prompt_path = expected_receipt["original_declared_prompt_path"]
    if (
        ledger.get("schema_version") != lifecycle.WORKFLOW_LEDGER_SCHEMA_VERSION
        or ledger.get("repository_id") != identity.repository_id
        or ledger.get("milestone_id") != identity.milestone_id
        or declared_prompt_path != accepted_prompt_path
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
    """SQLite store with distinct disposable, prepared and managed authority bindings."""

    def __init__(
        self,
        path: Path,
        identity: StoreIdentity,
        runtime_status: dict[str, Any],
        selection_state_root: Path | None = None,
        allow_unsupported_runtime_for_fixture: bool = False,
        creation_mode: str = "disposable_fixture",
        authority_configuration: StoreAuthorityConfiguration | None = None,
    ) -> None:
        self.path = path
        self.identity = identity
        self.runtime_status = runtime_status
        self.selection_state_root = selection_state_root
        self.allow_unsupported_runtime_for_fixture = allow_unsupported_runtime_for_fixture
        self.creation_mode = creation_mode
        self.authority_configuration = authority_configuration
        if creation_mode not in {"disposable_fixture", "managed_prepared"}:
            raise ValueError("Unsupported InformationStore creation mode.")
        if authority_configuration is not None and (
            creation_mode != "managed_prepared"
            or selection_state_root != authority_configuration.state_root
            or path != authority_configuration.database_path
            or allow_unsupported_runtime_for_fixture
        ):
            raise IdentityMismatchError("Managed authority configuration does not match the store binding.")
        metadata = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(metadata.st_mode):
            raise IdentityMismatchError("The InformationStore path is not a regular file.")
        self._database_file_identity = (metadata.st_dev, metadata.st_ino)

    def database_identity(self) -> str:
        try:
            metadata = self.path.stat(follow_symlinks=False)
        except OSError as exc:
            raise IdentityMismatchError("The bound InformationStore file is unavailable.") from exc
        if (
            not stat.S_ISREG(metadata.st_mode)
            or (metadata.st_dev, metadata.st_ino) != self._database_file_identity
        ):
            raise IdentityMismatchError("The bound InformationStore file was replaced; reopen and reverify it.")
        return _sha256(
            f"{self.path}:{metadata.st_dev}:{metadata.st_ino}".encode("utf-8")
        )

    @classmethod
    def initialize_disposable(
        cls,
        path: str | Path,
        identity: StoreIdentity,
        *,
        allow_unsupported_runtime_for_fixture: bool = False,
    ) -> InformationStore:
        return cls._initialize(
            path, identity, creation_mode="disposable_fixture",
            allow_unsupported_runtime_for_fixture=allow_unsupported_runtime_for_fixture,
        )

    @classmethod
    def initialize_managed_prepared(cls, path: str | Path, identity: StoreIdentity) -> InformationStore:
        return cls._initialize(path, identity, creation_mode="managed_prepared")

    @classmethod
    def _initialize(
        cls,
        path: str | Path,
        identity: StoreIdentity,
        *,
        creation_mode: str,
        allow_unsupported_runtime_for_fixture: bool = False,
    ) -> InformationStore:
        if creation_mode != "disposable_fixture" and allow_unsupported_runtime_for_fixture:
            raise RuntimeGateError("Unsupported-runtime permission is restricted to disposable fixtures.")
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
                        ("creation_mode", creation_mode),
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
                cls._verify_schema(connection, creation_mode=creation_mode)
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
        return cls(
            db_path, identity, status,
            allow_unsupported_runtime_for_fixture=allow_unsupported_runtime_for_fixture,
            creation_mode=creation_mode,
        )

    @classmethod
    def open_disposable(
        cls,
        path: str | Path,
        identity: StoreIdentity,
        *,
        allow_unsupported_runtime_for_fixture: bool = False,
        selection_state_root: str | Path | None = None,
    ) -> InformationStore:
        return cls._open(
            path, identity, creation_mode="disposable_fixture",
            allow_unsupported_runtime_for_fixture=allow_unsupported_runtime_for_fixture,
            selection_state_root=selection_state_root,
        )

    @classmethod
    def open_managed_prepared(cls, path: str | Path, identity: StoreIdentity) -> InformationStore:
        return cls._open(path, identity, creation_mode="managed_prepared")

    @classmethod
    def _open(
        cls,
        path: str | Path,
        identity: StoreIdentity,
        *,
        creation_mode: str,
        allow_unsupported_runtime_for_fixture: bool = False,
        selection_state_root: str | Path | None = None,
        authority_configuration: StoreAuthorityConfiguration | None = None,
    ) -> InformationStore:
        if creation_mode != "disposable_fixture" and allow_unsupported_runtime_for_fixture:
            raise RuntimeGateError("Unsupported-runtime permission is restricted to disposable fixtures.")
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
        selection_root = (
            _validate_selection_root(
                selection_state_root, identity.repository_root, fixture=creation_mode == "disposable_fixture",
            )
            if selection_state_root is not None else None
        )
        store = cls(
            db_path, identity, status, selection_root,
            allow_unsupported_runtime_for_fixture=allow_unsupported_runtime_for_fixture,
            creation_mode=creation_mode,
            authority_configuration=authority_configuration,
        )
        connection = store._connection(read_only=True)
        try:
            store._verify_schema(connection, creation_mode=creation_mode)
            if connection.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal":
                raise SchemaMismatchError("Disposable store is not in WAL mode.")
            store._verify_identity(connection, identity)
        finally:
            connection.close()
        return store

    @classmethod
    def open_managed_from_external_selection(
        cls,
        configuration: StoreAuthorityConfiguration,
        *,
        repository_id: str,
        repository_key: str,
        repository_root: str,
    ) -> InformationStore:
        state_root = _validate_selection_root(configuration.state_root, repository_root, fixture=False)
        with state_artifact_lock(state_root):
            try:
                if load_store_authority(state_root) != configuration:
                    raise IdentityMismatchError("The managed authority configuration changed or is absent.")
                verify_store_registry(configuration)
            except StoreAuthorityError as exc:
                raise IdentityMismatchError(str(exc)) from exc
            selection = _read_fixture_selection(state_root, repository_id)
            identity = StoreIdentity(
                repository_id=repository_id,
                repository_key=repository_key,
                repository_root=repository_root,
                milestone_id=selection.get("milestone_id"),
                prompt_path=selection.get("prompt_path"),
                selection_id=selection.get("selection_id"),
                selection_revision=selection.get("selection_revision"),
            )
            _validate_store_identity(identity)
            if selection.get("repository_id") != repository_id:
                raise IdentityMismatchError("External selection belongs to a different repository.")
            return cls._open(
                configuration.database_path, identity, creation_mode="managed_prepared",
                selection_state_root=state_root, authority_configuration=configuration,
            )

    @classmethod
    def open_disposable_from_external_selection(
        cls,
        path: str | Path,
        *,
        repository_id: str,
        repository_key: str,
        repository_root: str,
        selection_state_root: str | Path,
        allow_unsupported_runtime_for_fixture: bool = False,
    ) -> InformationStore:
        state_root = _validate_fixture_selection_root(selection_state_root, repository_root)
        with state_artifact_lock(state_root):
            selection = _read_fixture_selection(state_root, repository_id)
            identity = StoreIdentity(
                repository_id=repository_id,
                repository_key=repository_key,
                repository_root=repository_root,
                milestone_id=selection.get("milestone_id"),
                prompt_path=selection.get("prompt_path"),
                selection_id=selection.get("selection_id"),
                selection_revision=selection.get("selection_revision"),
            )
            _validate_store_identity(identity)
            if selection.get("repository_id") != repository_id:
                raise IdentityMismatchError("External selection belongs to a different repository.")
            return cls.open_disposable(
                path,
                identity,
                allow_unsupported_runtime_for_fixture=allow_unsupported_runtime_for_fixture,
                selection_state_root=state_root,
            )

    @classmethod
    def bind_disposable_v1_selection(
        cls,
        path: str | Path,
        identity: StoreIdentity,
        *,
        selection_state_root: str | Path,
        expected_v1_sha256: str,
        fixture_cutover_confirmed: bool,
        allow_unsupported_runtime_for_fixture: bool = False,
    ) -> InformationStore:
        if fixture_cutover_confirmed is not True:
            raise InformationStoreError("Binding a v1 selection requires explicit disposable-fixture confirmation.")
        return cls._bind_v1_selection(
            path, identity, selection_state_root=selection_state_root, expected_v1_sha256=expected_v1_sha256,
            creation_mode="disposable_fixture",
            allow_unsupported_runtime_for_fixture=allow_unsupported_runtime_for_fixture,
        )

    @classmethod
    def bind_prepared_v1_selection(
        cls,
        path: str | Path,
        identity: StoreIdentity,
        *,
        selection_state_root: str | Path,
        expected_v1_sha256: str,
        cutover_confirmed: bool,
    ) -> InformationStore:
        if cutover_confirmed is not True:
            raise InformationStoreError("Binding a v1 selection requires explicit Product Owner confirmation.")
        return cls._bind_v1_selection(
            path, identity, selection_state_root=selection_state_root, expected_v1_sha256=expected_v1_sha256,
            creation_mode="managed_prepared",
        )

    @classmethod
    def _bind_v1_selection(
        cls,
        path: str | Path,
        identity: StoreIdentity,
        *,
        selection_state_root: str | Path,
        expected_v1_sha256: str,
        creation_mode: str,
        allow_unsupported_runtime_for_fixture: bool = False,
    ) -> InformationStore:
        if not isinstance(expected_v1_sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", expected_v1_sha256):
            raise ValueError("The expected v1 selection hash must be a lowercase SHA-256 digest.")
        state_root = _validate_selection_root(
            selection_state_root, identity.repository_root, fixture=creation_mode == "disposable_fixture",
        )
        store = cls._open(
            path, identity, creation_mode=creation_mode,
            allow_unsupported_runtime_for_fixture=allow_unsupported_runtime_for_fixture,
        )
        with state_artifact_lock(state_root):
            if creation_mode == "managed_prepared":
                try:
                    if load_store_authority(state_root) is not None:
                        raise InformationStoreError("A managed authority is already selected; offline binding is disabled.")
                except StoreAuthorityError as exc:
                    raise InformationStoreError(str(exc)) from exc
            candidates = store.list_disposable_milestones()["candidates"]
            if not any(
                item["milestone_id"] == identity.milestone_id and item["prompt_path"] == identity.prompt_path
                for item in candidates
            ):
                raise IdentityMismatchError("The selected fixture has no unique stored prompt revision.")
            selection, source_hash = _read_fixture_selection_snapshot(state_root, identity.repository_id)
            expected = {
                "schema_version": 1,
                "repository_id": identity.repository_id,
                "milestone_id": identity.milestone_id,
                "prompt_path": identity.prompt_path,
            }
            if any(selection.get(key) != value for key, value in expected.items()):
                raise IdentityMismatchError("The v1 selection does not match the imported repository and milestone.")
            if set(selection) == set(expected):
                if source_hash != expected_v1_sha256:
                    raise StaleStateError("The v1 selection bytes changed since the quiesced inventory.")
                selection.update(
                    selection_id=identity.selection_id,
                    selection_revision=identity.selection_revision,
                    v1_selection_sha256=expected_v1_sha256,
                )
                store.database_identity()
                _write_fixture_selection(state_root, identity.repository_id, selection)
            elif (
                set(selection) != {*expected, "selection_id", "selection_revision", "v1_selection_sha256"}
                or selection.get("selection_id") != identity.selection_id
                or selection.get("selection_revision") != identity.selection_revision
                or selection.get("v1_selection_sha256") != expected_v1_sha256
            ):
                raise StaleStateError("The external selection is already bound to different fixture evidence.")
            store.database_identity()
            return cls._open(
                path, identity,
                creation_mode=creation_mode,
                allow_unsupported_runtime_for_fixture=allow_unsupported_runtime_for_fixture,
                selection_state_root=state_root,
            )

    @classmethod
    def recover_disposable_selection_mirror(
        cls,
        path: str | Path,
        *,
        expected_external: StoreIdentity,
        expected_mirror: StoreIdentity,
        selection_state_root: str | Path,
        fixture_recovery_confirmed: bool,
        allow_unsupported_runtime_for_fixture: bool = False,
    ) -> InformationStore:
        if fixture_recovery_confirmed is not True:
            raise InformationStoreError("Selection recovery requires explicit disposable-fixture confirmation.")
        return cls._recover_selection_mirror(
            path,
            expected_external=expected_external,
            expected_mirror=expected_mirror,
            selection_state_root=selection_state_root,
            allow_unsupported_runtime_for_fixture=allow_unsupported_runtime_for_fixture,
        )

    @classmethod
    def recover_managed_selection_mirror(
        cls,
        configuration: StoreAuthorityConfiguration,
        *,
        expected_external: StoreIdentity,
        expected_mirror: StoreIdentity,
        po_recovery_confirmed: bool,
    ) -> InformationStore:
        if po_recovery_confirmed is not True:
            raise InformationStoreError("Selection recovery requires explicit Product Owner confirmation.")
        return cls._recover_selection_mirror(
            configuration.database_path,
            expected_external=expected_external,
            expected_mirror=expected_mirror,
            selection_state_root=configuration.state_root,
            authority_configuration=configuration,
        )

    @classmethod
    def preview_managed_selection_recovery(
        cls,
        configuration: StoreAuthorityConfiguration,
        *,
        repository_id: str,
        repository_key: str,
        repository_root: str,
    ) -> dict[str, Any]:
        from .store_authority import authority_binding_digest

        status = sqlite_runtime_status()
        if not status["production_eligible"]:
            raise RuntimeGateError("Managed selection recovery requires an eligible linked SQLite runtime.")
        state_root = _validate_selection_root(configuration.state_root, repository_root, fixture=False)
        path = _validate_database_path(configuration.database_path, repository_root)
        with state_artifact_lock(state_root):
            selection = _read_fixture_selection(state_root, repository_id)
            external = StoreIdentity(
                repository_id=repository_id,
                repository_key=repository_key,
                repository_root=repository_root,
                milestone_id=selection.get("milestone_id"),
                prompt_path=selection.get("prompt_path"),
                selection_id=selection.get("selection_id"),
                selection_revision=selection.get("selection_revision"),
            )
            _validate_store_identity(external)
            store = cls(path, external, status, state_root, creation_mode="managed_prepared",
                        authority_configuration=configuration)
            store._verify_bound_selection()
            connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, isolation_level=None)
            connection.row_factory = sqlite3.Row
            try:
                cls._verify_schema(connection, creation_mode="managed_prepared")
                store._verify_authority_marker(connection)
                repository = connection.execute(
                    "SELECT repository_key, repository_root FROM repository WHERE repository_id = ?",
                    (repository_id,),
                ).fetchone()
                row = connection.execute(
                    """SELECT s.milestone_id, m.prompt_path, s.selection_id, s.selection_revision
                       FROM repository_selection s JOIN milestone m
                         ON m.repository_id = s.repository_id AND m.milestone_id = s.milestone_id
                       WHERE s.repository_id = ?""",
                    (repository_id,),
                ).fetchone()
                if repository is None or tuple(repository) != (repository_key, repository_root) or row is None:
                    raise IdentityMismatchError("Recovery repository does not match the database.")
                mirror = StoreIdentity(
                    repository_id=repository_id,
                    repository_key=repository_key,
                    repository_root=repository_root,
                    milestone_id=row["milestone_id"],
                    prompt_path=row["prompt_path"],
                    selection_id=row["selection_id"],
                    selection_revision=row["selection_revision"],
                )
                cls._verify_identity(connection, mirror)
                if (
                    external.selection_revision != mirror.selection_revision + 1
                    or external.selection_id == mirror.selection_id
                    or external.milestone_id == mirror.milestone_id
                ):
                    raise IdentityMismatchError("No exact adjacent interrupted selection is available to recover.")
                target = connection.execute(
                    """SELECT count(*) FROM milestone m JOIN document d
                       ON d.repository_id = m.repository_id AND d.milestone_id = m.milestone_id
                        AND d.logical_type = 'PROMPT' AND d.source_path = m.prompt_path
                       WHERE m.repository_id = ? AND m.milestone_id = ? AND m.prompt_path = ?
                         AND (d.draft_revision_id IS NOT NULL OR d.approved_revision_id IS NOT NULL)""",
                    (repository_id, external.milestone_id, external.prompt_path),
                ).fetchone()[0]
                if target != 1:
                    raise IdentityMismatchError("Recovery target has no unique stored prompt revision.")
            finally:
                connection.close()
            plan = {
                "authority_configuration_sha256": configuration.configuration_sha256,
                "database_identity": store.database_identity(),
                "external": asdict(external),
                "mirror": asdict(mirror),
            }
            plan["plan_sha256"] = authority_binding_digest(plan)
            return plan

    @classmethod
    def _recover_selection_mirror(
        cls,
        path: str | Path,
        *,
        expected_external: StoreIdentity,
        expected_mirror: StoreIdentity,
        selection_state_root: str | Path,
        allow_unsupported_runtime_for_fixture: bool = False,
        authority_configuration: StoreAuthorityConfiguration | None = None,
    ) -> InformationStore:
        _validate_store_identity(expected_external)
        _validate_store_identity(expected_mirror)
        if (
            (expected_external.repository_id, expected_external.repository_key, expected_external.repository_root)
            != (expected_mirror.repository_id, expected_mirror.repository_key, expected_mirror.repository_root)
            or expected_external.selection_revision != expected_mirror.selection_revision + 1
            or expected_external.selection_id == expected_mirror.selection_id
            or expected_external.milestone_id == expected_mirror.milestone_id
        ):
            raise IdentityMismatchError("Recovery requires the two exact adjacent selection snapshots.")
        db_path = _validate_database_path(path, expected_external.repository_root)
        creation_mode = "managed_prepared" if authority_configuration is not None else "disposable_fixture"
        state_root = _validate_selection_root(
            selection_state_root, expected_external.repository_root, fixture=authority_configuration is None,
        )
        status = sqlite_runtime_status()
        if not status["production_eligible"] and not allow_unsupported_runtime_for_fixture:
            raise RuntimeGateError("Selection recovery requires an eligible runtime or explicit fixture permission.")
        if not db_path.is_file():
            raise SchemaMismatchError("Disposable database does not exist; recovery never creates it.")
        store = cls(
            db_path, expected_external, status, state_root,
            allow_unsupported_runtime_for_fixture=allow_unsupported_runtime_for_fixture,
            creation_mode=creation_mode,
            authority_configuration=authority_configuration,
        )
        with state_artifact_lock(state_root):
            selection = _read_fixture_selection(state_root, expected_external.repository_id)
            store._verify_bound_selection()
            connection = sqlite3.connect(
                f"{db_path.as_uri()}?mode=rw", uri=True,
                timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None,
            )
            try:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA foreign_keys = ON")
                connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
                connection.execute("PRAGMA synchronous = FULL")
                cls._verify_schema(connection, creation_mode=creation_mode)
                store._verify_authority_marker(connection)
                if connection.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal":
                    raise SchemaMismatchError("Disposable store is not in WAL mode.")
                cls._begin_immediate(connection)
                cls._verify_identity(connection, expected_mirror)
                targets = connection.execute(
                    """
                    SELECT d.draft_revision_id, d.approved_revision_id
                    FROM milestone AS m JOIN document AS d
                      ON d.repository_id = m.repository_id AND d.milestone_id = m.milestone_id
                     AND d.logical_type = 'PROMPT' AND d.source_path = m.prompt_path
                    WHERE m.repository_id = ? AND m.milestone_id = ? AND m.prompt_path = ?
                    """,
                    (
                        expected_external.repository_id,
                        expected_external.milestone_id,
                        expected_external.prompt_path,
                    ),
                ).fetchall()
                if len(targets) != 1 or not (targets[0]["draft_revision_id"] or targets[0]["approved_revision_id"]):
                    raise IdentityMismatchError("Recovery target has no unique stored prompt revision.")
                connection.execute(
                    """
                    UPDATE repository_selection
                    SET milestone_id = ?, selection_id = ?, selection_revision = ?
                    WHERE repository_id = ?
                    """,
                    (
                        expected_external.milestone_id,
                        expected_external.selection_id,
                        expected_external.selection_revision,
                        expected_external.repository_id,
                    ),
                )
                if authority_configuration is not None:
                    connection.execute(
                        "UPDATE store_metadata SET value = '1' WHERE key = 'authority_first_write'"
                    )
                store.database_identity()
                if _read_fixture_selection(state_root, expected_external.repository_id) != selection:
                    raise StaleStateError("External fixture selection changed during recovery.")
                store._verify_authority_configuration()
                connection.commit()
            except Exception:
                if connection.in_transaction:
                    connection.rollback()
                raise
            finally:
                connection.close()
            verified = store._connection(read_only=True)
            try:
                cls._verify_identity(verified, expected_external)
            finally:
                verified.close()
        return store

    def switch_disposable_selection(
        self,
        milestone_id: str,
        *,
        expected_selection_id: str,
        expected_selection_revision: int,
    ) -> InformationStore:
        if self.creation_mode != "disposable_fixture":
            raise InformationStoreError("Disposable selection switching requires a fixture store.")
        return self.switch_selection(
            milestone_id,
            expected_selection_id=expected_selection_id,
            expected_selection_revision=expected_selection_revision,
        )

    def switch_selection(
        self,
        milestone_id: str,
        *,
        expected_selection_id: str,
        expected_selection_revision: int,
    ) -> InformationStore:
        if self.selection_state_root is None:
            raise InformationStoreError("Selection changes require an external selection binding.")
        if self.creation_mode == "managed_prepared" and self.authority_configuration is None:
            raise InformationStoreError("Prepared stores cannot switch selections before authority activation.")
        if not isinstance(milestone_id, str) or not _MILESTONE_ID_RE.fullmatch(milestone_id):
            raise ValueError("Select a supported canonical milestone ID.")
        if (
            expected_selection_id != self.identity.selection_id
            or expected_selection_revision != self.identity.selection_revision
        ):
            raise StaleStateError("The submitted selection snapshot is stale.")
        with state_artifact_lock(self.selection_state_root):
            connection = self._connection()
            try:
                if self.authority_configuration is not None and milestone_id != self.identity.milestone_id:
                    self._begin_immediate(connection)
                    connection.execute(
                        "UPDATE store_metadata SET value = '1' WHERE key = 'authority_first_write'"
                    )
                    connection.commit()
                self._begin_immediate(connection)
                self._verify_identity(connection, self.identity)
                if milestone_id == self.identity.milestone_id:
                    connection.commit()
                    return self
                target = connection.execute(
                    """
                    SELECT m.prompt_path, d.draft_revision_id, d.approved_revision_id
                    FROM milestone AS m
                    JOIN document AS d ON d.repository_id = m.repository_id
                      AND d.milestone_id = m.milestone_id AND d.logical_type = 'PROMPT'
                      AND d.source_path = m.prompt_path
                    WHERE m.repository_id = ? AND m.milestone_id = ?
                    """,
                    (self.identity.repository_id, milestone_id),
                ).fetchall()
                if len(target) != 1 or not (target[0]["draft_revision_id"] or target[0]["approved_revision_id"]):
                    raise IdentityMismatchError("The target has no unique stored prompt revision.")
                next_identity = StoreIdentity(
                    repository_id=self.identity.repository_id,
                    repository_key=self.identity.repository_key,
                    repository_root=self.identity.repository_root,
                    milestone_id=milestone_id,
                    prompt_path=target[0]["prompt_path"],
                    selection_id=uuid.uuid4().hex,
                    selection_revision=self.identity.selection_revision + 1,
                )
                selection = _read_fixture_selection(self.selection_state_root, self.identity.repository_id)
                self._verify_bound_selection()
                selection.update(
                    milestone_id=next_identity.milestone_id,
                    prompt_path=next_identity.prompt_path,
                    selection_id=next_identity.selection_id,
                    selection_revision=next_identity.selection_revision,
                )
                connection.execute(
                    """
                    UPDATE repository_selection
                    SET milestone_id = ?, selection_id = ?, selection_revision = ?
                    WHERE repository_id = ?
                    """,
                    (
                        next_identity.milestone_id,
                        next_identity.selection_id,
                        next_identity.selection_revision,
                        next_identity.repository_id,
                    ),
                )
                _write_fixture_selection(self.selection_state_root, self.identity.repository_id, selection)
                self.database_identity()
                if _read_fixture_selection(self.selection_state_root, self.identity.repository_id) != selection:
                    raise StaleStateError("External fixture selection changed during selection publication.")
                self._verify_authority_configuration()
                if isinstance(connection, _SelectionLockedConnection):
                    connection.validate_authority(selection=False)
                    connection.mark_managed_write()
                sqlite3.Connection.commit(connection)
            except Exception:
                if connection.in_transaction:
                    connection.rollback()
                raise
            finally:
                connection.close()
            bound = InformationStore(
                self.path, next_identity, self.runtime_status, self.selection_state_root,
                allow_unsupported_runtime_for_fixture=self.allow_unsupported_runtime_for_fixture,
                creation_mode=self.creation_mode,
                authority_configuration=self.authority_configuration,
            )
            if bound._database_file_identity != self._database_file_identity:
                raise IdentityMismatchError("The disposable store was replaced during selection publication.")
            verified = bound._connection(read_only=True)
            try:
                bound._verify_identity(verified, next_identity)
            finally:
                verified.close()
            return bound

    def register_disposable_scope(self, identity: StoreIdentity) -> InformationStore:
        if self.creation_mode != "disposable_fixture":
            raise InformationStoreError("Disposable scope registration requires a fixture store.")
        return self.register_import_scope(identity)

    def register_import_scope(self, identity: StoreIdentity) -> InformationStore:
        """Register and bind one scope during a quiesced offline import."""
        if self.selection_state_root is not None:
            raise InformationStoreError("Offline scope registration cannot change an active external selection.")
        _validate_store_identity(identity)
        _validate_database_path(self.path, identity.repository_root)
        connection = self._connection()
        try:
            self._begin_immediate(connection)
            self._verify_identity(connection, self.identity)
            conflicts = connection.execute(
                """
                SELECT repository_id, repository_key, repository_root FROM repository
                WHERE repository_id = ? OR repository_key = ? OR repository_root = ?
                """,
                (identity.repository_id, identity.repository_key, identity.repository_root),
            ).fetchall()
            expected_repository = (identity.repository_id, identity.repository_key, identity.repository_root)
            if any(tuple(row) != expected_repository for row in conflicts):
                raise IdentityMismatchError("Repository ID, key, or root conflicts with an existing store scope.")
            if not conflicts:
                connection.execute(
                    "INSERT INTO repository(repository_id, repository_key, repository_root) VALUES (?, ?, ?)",
                    expected_repository,
                )
            milestone = connection.execute(
                "SELECT prompt_path FROM milestone WHERE repository_id = ? AND milestone_id = ?",
                (identity.repository_id, identity.milestone_id),
            ).fetchone()
            if milestone is not None and milestone["prompt_path"] != identity.prompt_path:
                raise IdentityMismatchError("Registered milestone has a different prompt path.")
            if milestone is None:
                connection.execute(
                    "INSERT INTO milestone(repository_id, milestone_id, prompt_path) VALUES (?, ?, ?)",
                    (identity.repository_id, identity.milestone_id, identity.prompt_path),
                )
                connection.execute(
                    """
                    INSERT INTO workflow_state
                        (repository_id, milestone_id, store_revision, event_count, projection_json)
                    VALUES (?, ?, 0, 0, ?)
                    """,
                    (identity.repository_id, identity.milestone_id, _json_text(_projection([]))),
                )
            selection = connection.execute(
                """
                SELECT milestone_id, selection_id, selection_revision
                FROM repository_selection WHERE repository_id = ?
                """,
                (identity.repository_id,),
            ).fetchone()
            new_selection = (identity.milestone_id, identity.selection_id, identity.selection_revision)
            if selection is None:
                connection.execute(
                    """
                    INSERT INTO repository_selection
                        (repository_id, milestone_id, selection_id, selection_revision)
                    VALUES (?, ?, ?, ?)
                    """,
                    (identity.repository_id, *new_selection),
                )
            elif tuple(selection) != new_selection:
                if identity.selection_revision <= selection["selection_revision"]:
                    raise StaleStateError("A changed disposable selection requires a newer revision.")
                connection.execute(
                    """
                    UPDATE repository_selection
                    SET milestone_id = ?, selection_id = ?, selection_revision = ?
                    WHERE repository_id = ?
                    """,
                    (*new_selection, identity.repository_id),
                )
            connection.commit()
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()
        self.database_identity()
        bound = InformationStore(
            self.path, identity, self.runtime_status,
            allow_unsupported_runtime_for_fixture=self.allow_unsupported_runtime_for_fixture,
            creation_mode=self.creation_mode,
        )
        if bound._database_file_identity != self._database_file_identity:
            raise IdentityMismatchError("The disposable store was replaced during scope registration.")
        return bound

    @staticmethod
    def _verify_schema(connection: sqlite3.Connection, *, creation_mode: str = "disposable_fixture") -> None:
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
            raise SchemaMismatchError(
                f"SQLite information-store table set does not match schema version {SCHEMA_VERSION}."
            )
        publication_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(document_publication)")
        }
        if publication_columns != _DOCUMENT_PUBLICATION_COLUMNS:
            raise SchemaMismatchError("SQLite publication record columns do not match the current schema.")
        triggers = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_schema WHERE type = 'trigger'")
        }
        if triggers != _EXPECTED_TRIGGERS:
            raise SchemaMismatchError(
                f"SQLite information-store trigger set does not match schema version {SCHEMA_VERSION}."
            )
        metadata = dict(connection.execute("SELECT key, value FROM store_metadata").fetchall())
        base_metadata = {"schema_id": SCHEMA_ID, "creation_mode": creation_mode}
        authority_keys = {
            "authority_generation", "authority_configuration_sha256", "authority_state_root",
            "authority_first_write",
        }
        if (
            any(metadata.get(key) != value for key, value in base_metadata.items())
            or set(metadata) - set(base_metadata) not in (set(), authority_keys)
            or (creation_mode != "managed_prepared" and set(metadata) != set(base_metadata))
        ):
            raise SchemaMismatchError(f"SQLite database is not an initialized {creation_mode} store.")

    def _verify_authority_marker(self, connection: sqlite3.Connection) -> None:
        metadata = dict(connection.execute(
            "SELECT key, value FROM store_metadata WHERE key LIKE 'authority_%'"
        ).fetchall())
        if self.creation_mode != "managed_prepared":
            return
        if self.authority_configuration is None:
            if metadata:
                raise StaleStateError("The prepared store has been activated; offline access is disabled.")
            return
        configuration = self.authority_configuration
        if (
            metadata.get("authority_generation") != configuration.generation
            or metadata.get("authority_configuration_sha256") != configuration.configuration_sha256
            or metadata.get("authority_state_root") != str(configuration.state_root)
            or metadata.get("authority_first_write") not in {"0", "1"}
        ):
            raise StaleStateError("The store activation marker does not match the selected authority.")

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
        guard = state_artifact_lock(self.selection_state_root) if self.selection_state_root is not None else None
        if guard is not None:
            guard.__enter__()
        connection: sqlite3.Connection | None = None
        try:
            self._verify_bound_selection()
            mode = "ro" if read_only else "rw"
            uri = f"{self.path.as_uri()}?mode={mode}"
            locked = sqlite3.connect(
                uri, uri=True, timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None,
                factory=_SelectionLockedConnection,
            )
            locked.bind_selection_guard(
                guard, self._verify_bound_selection, self._verify_authority_marker,
                self.authority_configuration is not None,
            )
            guard = None
            connection = locked
            self.database_identity()
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
            if not read_only:
                connection.execute("PRAGMA synchronous = FULL")
            self._verify_schema(connection, creation_mode=self.creation_mode)
            self._verify_authority_marker(connection)
            return connection
        except Exception:
            if connection is not None:
                connection.close()
            elif guard is not None:
                guard.__exit__(None, None, None)
            raise

    def _verify_authority_configuration(self) -> None:
        if self.authority_configuration is not None:
            try:
                if load_store_authority(self.selection_state_root) != self.authority_configuration:
                    raise StaleStateError("Managed authority configuration changed or is absent.")
                verify_store_registry(self.authority_configuration)
            except StoreAuthorityError as exc:
                raise StaleStateError(str(exc)) from exc

    def _verify_bound_selection(self) -> None:
        self.database_identity()
        self._verify_authority_configuration()
        if self.selection_state_root is None:
            return
        selection = _read_fixture_selection(self.selection_state_root, self.identity.repository_id)
        expected = {
            "repository_id": self.identity.repository_id,
            "milestone_id": self.identity.milestone_id,
            "prompt_path": self.identity.prompt_path,
            "selection_id": self.identity.selection_id,
            "selection_revision": self.identity.selection_revision,
        }
        if any(selection.get(key) != value for key, value in expected.items()):
            raise StaleStateError("External fixture selection changed or does not match the database binding.")

    def list_disposable_milestones(self) -> dict[str, Any]:
        connection = self._connection(read_only=True)
        try:
            connection.execute("BEGIN")
            self._verify_identity(connection, self.identity)
            milestones = connection.execute(
                """
                SELECT milestone_id, prompt_path FROM milestone
                WHERE repository_id = ? ORDER BY milestone_id
                """,
                (self.identity.repository_id,),
            ).fetchall()
            candidates: list[dict[str, Any]] = []
            issues: list[str] = []
            for milestone in milestones:
                prompts = connection.execute(
                    """
                    SELECT draft_revision_id, approved_revision_id FROM document
                    WHERE repository_id = ? AND milestone_id = ?
                      AND logical_type = 'PROMPT' AND source_path = ?
                    """,
                    (self.identity.repository_id, milestone["milestone_id"], milestone["prompt_path"]),
                ).fetchall()
                if len(prompts) != 1 or not (prompts[0]["draft_revision_id"] or prompts[0]["approved_revision_id"]):
                    issues.append(f"{milestone['milestone_id']}: no unique stored prompt revision.")
                    continue
                closeout_path = milestone["prompt_path"].removesuffix("_prompt.md") + "_closeout.md"
                closeout_count = connection.execute(
                    """
                    SELECT COUNT(*) FROM document
                    WHERE repository_id = ? AND milestone_id = ?
                      AND logical_type = 'CLOSEOUT' AND source_path = ?
                    """,
                    (self.identity.repository_id, milestone["milestone_id"], closeout_path),
                ).fetchone()[0]
                if closeout_count > 1:
                    issues.append(f"{milestone['milestone_id']}: ambiguous stored closeout documents.")
                    continue
                candidates.append({
                    "milestone_id": milestone["milestone_id"],
                    "prompt_path": milestone["prompt_path"],
                    "title_slug": PurePosixPath(milestone["prompt_path"]).name.split("_", 1)[-1].removesuffix("_prompt.md"),
                    "closeout_exists": closeout_count == 1,
                    "closeout_path": closeout_path if closeout_count == 1 else None,
                })
            connection.commit()
            return {"candidates": candidates, "issues": issues}
        finally:
            connection.close()

    @staticmethod
    def _begin_immediate(connection: sqlite3.Connection) -> None:
        try:
            connection.execute("BEGIN IMMEDIATE")
            if isinstance(connection, _SelectionLockedConnection):
                connection.validate_authority()
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

    def _approved_guidance(self, connection: sqlite3.Connection) -> list[dict[str, Any]]:
        rows = connection.execute(
            """
            SELECT d.dispatch_id, d.milestone_id, d.dispatch_type, d.status, d.body, d.title,
                   d.approved_milestone_id, d.related_event_id, d.source_sha256, d.payload_json,
                   d.created_at, d.updated_at,
                   EXISTS (
                       SELECT 1 FROM dispatch_history AS h
                       WHERE h.repository_id = d.repository_id AND h.dispatch_id = d.dispatch_id
                         AND h.record_kind = 'DECISION' AND h.to_status = 'APPROVED'
                         AND h.po_confirmed = 1
                   ) AS decision_confirmed
            FROM architect_dispatch AS d
            WHERE d.repository_id = ? AND d.milestone_id = ?
              AND d.status = 'APPROVED'
              AND d.dispatch_type IN ('INSTRUCTION', 'RESPONSE')
              AND d.approved_milestone_id = d.milestone_id
            ORDER BY d.created_at, d.dispatch_id
            LIMIT ?
            """,
            (self.identity.repository_id, self.identity.milestone_id, MAX_DISPATCH_COUNT + 1),
        ).fetchall()
        if len(rows) > MAX_DISPATCH_COUNT:
            raise DispatchDecisionError("Approved Architect guidance exceeds the bounded list size.")
        result = []
        for row in rows:
            dispatch = dict(row)
            dispatch["decision_confirmed"] = bool(dispatch["decision_confirmed"])
            dispatch["payload"] = json.loads(dispatch.pop("payload_json"))
            dispatch["authorization_granted"] = False
            result.append(dispatch)
        return result

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
        *,
        milestone_id: str | None = None,
    ) -> None:
        target_milestone_id = self.identity.milestone_id if milestone_id is None else milestone_id
        doc = connection.execute(
            """
            SELECT document_id, logical_type, source_path, draft_revision_id
            FROM document
            WHERE repository_id = ? AND milestone_id = ? AND logical_key = ?
            """,
            (self.identity.repository_id, target_milestone_id, item["logical_key"]),
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
                    target_milestone_id,
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
                target_milestone_id,
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
                target_milestone_id,
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

    def _confirmed_po_action(self, fixture: bool, managed: bool) -> bool:
        if type(fixture) is not bool or type(managed) is not bool:
            raise ValueError("Product Owner confirmation flags must be explicit booleans.")
        if fixture and self.creation_mode != "disposable_fixture":
            raise InformationStoreError("Fixture confirmation cannot authorize a managed store.")
        if managed and (self.creation_mode != "managed_prepared" or self.authority_configuration is None):
            raise InformationStoreError("Managed confirmation requires an active managed authority binding.")
        return fixture or managed

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
        trusted_managed_po_confirmation: bool = False,
        approved_prompt_revision_id: str | None = None,
        expected_guidance_sha256: str | None = None,
        expected_document_drafts: Mapping[str, str] | None = None,
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
        po_confirmed = self._confirmed_po_action(
            trusted_fixture_po_confirmation, trusted_managed_po_confirmation,
        )
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
        if expected_guidance_sha256 is not None:
            request_body["expected_guidance_sha256"] = expected_guidance_sha256
        if trusted_managed_po_confirmation:
            request_body["trusted_managed_po_confirmation"] = True
        if expected_document_drafts is not None:
            request_body["expected_document_drafts"] = dict(expected_document_drafts)
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
            if expected_guidance_sha256 is not None:
                current_guidance = self._approved_guidance(connection)
                if _sha256(_json_text(current_guidance).encode("utf-8")) != expected_guidance_sha256:
                    raise StaleStateError("Approved Architect guidance changed; refresh and retry.")
            if expected_document_drafts is not None:
                for document_id, revision_id in expected_document_drafts.items():
                    row = connection.execute(
                        """
                        SELECT draft_revision_id FROM document
                        WHERE repository_id = ? AND milestone_id = ? AND document_id = ?
                        """,
                        (self.identity.repository_id, self.identity.milestone_id, document_id),
                    ).fetchone()
                    if row is None or row["draft_revision_id"] != revision_id:
                        raise StaleStateError("A selected document draft changed; refresh and retry.")
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
                    and not po_confirmed
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
                if link.link_role == "prompt" and linked_event["event_type"] != "prompt_approved":
                    raise IdentityMismatchError("Prompt approval links must attach to a prompt_approved event.")
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
                    SELECT logical_type, source_path, draft_revision_id FROM document
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
                    if prompt_doc["draft_revision_id"] != approved_prompt_revision_id:
                        raise StaleStateError("The selected prompt draft changed before approval.")
                if logical_type != "PROMPT" or source_path != self.identity.prompt_path:
                    raise IdentityMismatchError("Approved revision is not the selected milestone prompt.")
                if not po_confirmed:
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
                    VALUES (?, ?, ?, ?, ?, 'APPROVED', ?, ?, 1, ?, ?, ?)
                    """,
                    (
                        uuid.uuid4().hex,
                        self.identity.repository_id,
                        self.identity.milestone_id,
                        prompt_link["document_id"],
                        approved_prompt_revision_id,
                        decision_event["actor_role"],
                        decision_event["actor_attribution"],
                        "trusted_fixture" if self.creation_mode == "disposable_fixture" else "po_browser",
                        decision_event["timestamp"],
                        _json_text({
                            "event_id": decision_event["event_id"],
                            "fixture_confirmation" if self.creation_mode == "disposable_fixture"
                            else "browser_confirmation": True,
                        }),
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
                    VALUES (?, ?, ?, ?, ?, 'APPROVED', ?, ?, 1, ?, ?, ?)
                    """,
                    (
                        uuid.uuid4().hex,
                        self.identity.repository_id,
                        self.identity.milestone_id,
                        closeout_link["document_id"],
                        closeout_link["revision_id"],
                        event["actor_role"],
                        event["actor_attribution"],
                        "trusted_fixture" if self.creation_mode == "disposable_fixture" else "po_browser",
                        event["timestamp"],
                        _json_text({
                            "event_id": event["event_id"],
                            "fixture_confirmation" if self.creation_mode == "disposable_fixture"
                            else "browser_confirmation": True,
                        }),
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
        trusted_managed_po_confirmation: bool = False,
        expected_guidance_sha256: str | None = None,
        expected_document_drafts: Mapping[str, str] | None = None,
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
            trusted_managed_po_confirmation=trusted_managed_po_confirmation,
            expected_guidance_sha256=expected_guidance_sha256,
            expected_document_drafts=expected_document_drafts,
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
        trusted_fixture_po_confirmation: bool = False,
        trusted_managed_po_confirmation: bool = False,
        following_events: Sequence[dict[str, Any]] = (),
        expected_guidance_sha256: str | None = None,
    ) -> dict[str, Any]:
        link = WorkflowDocumentLink(
            event_id=prompt_approved_event.get("event_id", ""),
            document_id=document_id,
            revision_id=revision_id,
            content_sha256=content_sha256,
            link_role="prompt",
            evidence="Exact prompt revision selected by explicit Product Owner confirmation.",
        )
        return self._append_workflow_batch(
            request_id=request_id,
            expected_repository_id=self.identity.repository_id,
            expected_milestone_id=self.identity.milestone_id,
            expected_selection_id=self.identity.selection_id,
            expected_selection_revision=self.identity.selection_revision,
            expected_prompt_revision_id=None,
            expected_store_revision=expected_store_revision,
            events=[prompt_approved_event, *following_events],
            links=[link],
            trusted_fixture_po_confirmation=trusted_fixture_po_confirmation,
            trusted_managed_po_confirmation=trusted_managed_po_confirmation,
            approved_prompt_revision_id=revision_id,
            expected_guidance_sha256=expected_guidance_sha256,
        )

    def confirm_imported_prompt_revision(
        self,
        *,
        request_id: str,
        expected_store_revision: int,
        document_id: str,
        revision_id: str,
        content_sha256: str,
        trusted_fixture_po_confirmation: bool = False,
        trusted_managed_po_confirmation: bool = False,
    ) -> dict[str, Any]:
        if not self._confirmed_po_action(trusted_fixture_po_confirmation, trusted_managed_po_confirmation):
            raise InformationStoreError("An imported prompt requires explicit PO confirmation.")
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", request_id):
            raise ValueError("Prompt confirmation request ID must be a simple bounded identifier.")
        if type(expected_store_revision) is not int or expected_store_revision < 0:
            raise ValueError("Expected workflow revision must be a non-negative integer.")
        if not all(isinstance(value, str) and value for value in (document_id, revision_id)):
            raise ValueError("Exact prompt document and revision IDs are required.")
        if not isinstance(content_sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", content_sha256):
            raise ValueError("Prompt confirmation requires a lowercase SHA-256 digest.")
        request = {
            "repository_id": self.identity.repository_id,
            "milestone_id": self.identity.milestone_id,
            "selection_id": self.identity.selection_id,
            "selection_revision": self.identity.selection_revision,
            "expected_store_revision": expected_store_revision,
            "document_id": document_id,
            "revision_id": revision_id,
            "content_sha256": content_sha256,
        }
        request_hash = _sha256(_json_text(request).encode("utf-8"))
        connection = self._connection()
        try:
            self._begin_immediate(connection)
            self._verify_identity(connection, self.identity)
            prior = connection.execute(
                """
                SELECT request_hash, result_json FROM transaction_receipt
                WHERE repository_id = ? AND milestone_id = ? AND request_id = ?
                """,
                (self.identity.repository_id, self.identity.milestone_id, request_id),
            ).fetchone()
            if prior is not None:
                if prior["request_hash"] != request_hash:
                    raise IdempotencyConflictError("Prompt confirmation request conflicts with its earlier receipt.")
                result = json.loads(prior["result_json"])
                connection.commit()
                return result
            state = connection.execute(
                """
                SELECT store_revision, event_count, projection_json
                FROM workflow_state WHERE repository_id = ? AND milestone_id = ?
                """,
                (self.identity.repository_id, self.identity.milestone_id),
            ).fetchone()
            events = self._load_events(connection)
            projection = self._validate_projection(state, events)
            if state["store_revision"] != expected_store_revision:
                raise StaleStateError("Workflow changed before prompt confirmation; review and retry.")
            if projection["phase"] == "PLANNING":
                raise InformationStoreError("A planning prompt must use the normal Approve & Start action.")
            row = connection.execute(
                """
                SELECT d.document_id, d.logical_type, d.source_path, d.draft_revision_id,
                       d.approved_revision_id, r.content, r.content_sha256, r.source_kind,
                       m.prompt_revision_id
                FROM document d
                JOIN document_revision r ON r.document_id = d.document_id
                JOIN milestone m ON m.repository_id = d.repository_id AND m.milestone_id = d.milestone_id
                WHERE d.repository_id = ? AND d.milestone_id = ?
                  AND d.document_id = ? AND r.revision_id = ?
                """,
                (self.identity.repository_id, self.identity.milestone_id, document_id, revision_id),
            ).fetchone()
            if (
                row is None
                or row["logical_type"] != "PROMPT"
                or row["source_path"] != self.identity.prompt_path
                or row["draft_revision_id"] != revision_id
                or row["approved_revision_id"] is not None
                or row["prompt_revision_id"] is not None
                or row["source_kind"] != "v1_import"
                or row["content_sha256"] != content_sha256
                or _sha256(row["content"].encode("utf-8")) != content_sha256
            ):
                raise RevisionConflictError("The reviewed revision is not the unique unconfirmed imported prompt.")
            sources = connection.execute(
                """
                SELECT source_id, source_sha256, source_bytes FROM import_source
                WHERE repository_id = ? AND milestone_id = ? AND source_kind = 'prompt' AND source_path = ?
                """,
                (self.identity.repository_id, self.identity.milestone_id, self.identity.prompt_path),
            ).fetchall()
            if (
                len(sources) != 1
                or sources[0]["source_sha256"] != content_sha256
                or bytes(sources[0]["source_bytes"]) != row["content"].encode("utf-8")
            ):
                raise ImportSourceError("The selected imported prompt no longer matches its exact source copy.")
            decision_id = uuid.uuid4().hex
            result = {
                **request,
                "decision_id": decision_id,
                "import_source_id": sources[0]["source_id"],
                "confirmation": (
                    "new_explicit_fixture_po_decision"
                    if self.creation_mode == "disposable_fixture" else "new_explicit_browser_po_decision"
                ),
                "historical_event_time_revision": "UNKNOWN/NOT VERIFIED",
                "lifecycle_changed": False,
            }
            connection.execute(
                """
                INSERT INTO document_revision_decision
                    (decision_id, repository_id, milestone_id, document_id, revision_id,
                     decision_type, actor_role, actor_attribution, po_confirmed, source_kind,
                     decided_at, payload_json)
                VALUES (?, ?, ?, ?, ?, 'APPROVED', 'product_owner', ?, 1, ?, ?, ?)
                """,
                (
                    decision_id, self.identity.repository_id, self.identity.milestone_id,
                    document_id, revision_id,
                    (
                        "Explicit new fixture PO confirmation; identity and historical revision are not authenticated."
                        if self.creation_mode == "disposable_fixture"
                        else "Explicit new PO browser confirmation; identity and historical revision are not authenticated."
                    ),
                    "fixture_reconfirmation" if self.creation_mode == "disposable_fixture" else "po_browser_reconfirmation",
                    _now(), _json_text(result),
                ),
            )
            connection.execute(
                "UPDATE document SET approved_revision_id = ? WHERE document_id = ?",
                (revision_id, document_id),
            )
            connection.execute(
                "UPDATE milestone SET prompt_revision_id = ? WHERE repository_id = ? AND milestone_id = ?",
                (revision_id, self.identity.repository_id, self.identity.milestone_id),
            )
            connection.execute(
                """
                INSERT INTO transaction_receipt
                    (repository_id, milestone_id, request_id, request_hash, resulting_revision, result_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self.identity.repository_id, self.identity.milestone_id, request_id,
                    request_hash, state["store_revision"], _json_text(result), _now(),
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

    def submit_closeout_revision(
        self,
        *,
        request_id: str,
        expected_prompt_revision_id: str,
        expected_store_revision: int,
        revision: DocumentRevisionInput,
        closeout_submitted_event: dict[str, Any],
        expected_guidance_sha256: str | None = None,
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
            expected_guidance_sha256=expected_guidance_sha256,
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
            guidance = self._approved_guidance(connection)
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
                "confirmed_guidance": [item for item in guidance if item["decision_confirmed"]],
                "unverified_guidance": [item for item in guidance if not item["decision_confirmed"]],
                "guidance_sha256": _sha256(_json_text(guidance).encode("utf-8")),
                "runtime": dict(self.runtime_status),
                "production_eligible": self.runtime_status["production_eligible"],
                "authority": (
                    "sqlite_managed"
                    if self.authority_configuration is not None
                    else "managed_prepared"
                    if self.creation_mode == "managed_prepared"
                    else "disposable_fixture_only"
                ),
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

    def list_repository_revisions(self) -> list[dict[str, Any]]:
        """Read all immutable revisions for the bound repository in one snapshot."""
        connection = self._connection(read_only=True)
        try:
            connection.execute("BEGIN")
            self._verify_identity(connection, self.identity)
            rows = connection.execute(
                """
                SELECT d.milestone_id, d.document_id, d.logical_key, d.logical_type, d.source_path,
                       d.draft_revision_id, d.approved_revision_id,
                       r.revision_id, r.parent_revision_id, r.content, r.content_sha256,
                       r.source_kind, r.provenance_json, r.created_at
                FROM document AS d
                JOIN document_revision AS r
                  ON r.document_id = d.document_id
                 AND r.repository_id = d.repository_id AND r.milestone_id = d.milestone_id
                WHERE d.repository_id = ?
                ORDER BY d.document_order, r.revision_order
                """,
                (self.identity.repository_id,),
            ).fetchall()
            revisions = []
            for row in rows:
                revision = dict(row)
                if _sha256(revision["content"].encode("utf-8")) != revision["content_sha256"]:
                    raise IntegrityFailureError("Stored document content hash does not match its revision.")
                revision["provenance"] = json.loads(revision.pop("provenance_json"))
                revision["status"] = (
                    "approved" if revision["revision_id"] == revision["approved_revision_id"]
                    else "draft" if revision["revision_id"] == revision["draft_revision_id"]
                    else "historical"
                )
                revisions.append(revision)
            connection.commit()
            return revisions
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

    def list_workflow_document_links(
        self,
        document_id: str,
        revision_id: str,
    ) -> list[dict[str, Any]]:
        if not isinstance(document_id, str) or not document_id:
            raise ValueError("Document ID is required.")
        if not isinstance(revision_id, str) or not revision_id:
            raise ValueError("Revision ID is required.")
        connection = self._connection(read_only=True)
        try:
            self._verify_identity(connection, self.identity)
            rows = connection.execute(
                """
                SELECT link.event_id, link.document_id, link.revision_id,
                       link.content_sha256, link.link_role, link.evidence,
                       revision.content_sha256 AS revision_sha256
                FROM workflow_document_link AS link
                JOIN document_revision AS revision
                  ON revision.repository_id = link.repository_id
                 AND revision.milestone_id = link.milestone_id
                 AND revision.document_id = link.document_id
                 AND revision.revision_id = link.revision_id
                WHERE link.repository_id = ? AND link.milestone_id = ?
                  AND link.document_id = ? AND link.revision_id = ?
                ORDER BY link.event_id, link.link_role
                """,
                (
                    self.identity.repository_id,
                    self.identity.milestone_id,
                    document_id,
                    revision_id,
                ),
            ).fetchall()
            revision = connection.execute(
                """
                SELECT content_sha256 FROM document_revision
                WHERE repository_id = ? AND milestone_id = ?
                  AND document_id = ? AND revision_id = ?
                """,
                (
                    self.identity.repository_id,
                    self.identity.milestone_id,
                    document_id,
                    revision_id,
                ),
            ).fetchone()
            if revision is None:
                raise RevisionConflictError("Exact document revision was not found.")
            if any(row["content_sha256"] != row["revision_sha256"] for row in rows):
                raise IntegrityFailureError("Workflow document link hash does not match its exact revision.")
            return [
                {key: row[key] for key in ("event_id", "document_id", "revision_id", "content_sha256", "link_role", "evidence")}
                for row in rows
            ]
        finally:
            connection.close()

    def get_event_document_link(self, event_id: str, link_role: str) -> dict[str, Any]:
        if not isinstance(event_id, str) or not event_id or link_role not in _WORKFLOW_LINK_ROLES:
            raise ValueError("An event ID and supported document-link role are required.")
        connection = self._connection(read_only=True)
        try:
            self._verify_identity(connection, self.identity)
            row = connection.execute(
                """
                SELECT l.event_id, l.document_id, l.revision_id, l.content_sha256, l.link_role,
                       l.evidence, r.content_sha256 AS revision_sha256
                FROM workflow_document_link AS l
                JOIN document_revision AS r
                  ON r.repository_id = l.repository_id AND r.milestone_id = l.milestone_id
                 AND r.document_id = l.document_id AND r.revision_id = l.revision_id
                WHERE l.repository_id = ? AND l.milestone_id = ?
                  AND l.event_id = ? AND l.link_role = ?
                """,
                (self.identity.repository_id, self.identity.milestone_id, event_id, link_role),
            ).fetchone()
            if row is None:
                raise RevisionConflictError("The event has no exact linked document revision.")
            if row["content_sha256"] != row["revision_sha256"]:
                raise IntegrityFailureError("Event document link hash does not match its exact revision.")
            return {
                key: row[key]
                for key in ("event_id", "document_id", "revision_id", "content_sha256", "link_role", "evidence")
            }
        finally:
            connection.close()

    def get_closeout_publication_evidence(
        self,
        document_id: str,
        revision_id: str,
    ) -> dict[str, Any]:
        connection = self._connection(read_only=True)
        try:
            connection.execute("BEGIN")
            self._verify_identity(connection, self.identity)
            revision_row = connection.execute(
                """
                SELECT d.document_id, d.logical_key, d.logical_type, d.source_path,
                       r.revision_id, r.content, r.content_sha256
                FROM document_revision r
                JOIN document d ON d.document_id = r.document_id
                WHERE r.repository_id = ? AND r.milestone_id = ?
                    AND r.document_id = ? AND r.revision_id = ?
                """,
                (self.identity.repository_id, self.identity.milestone_id, document_id, revision_id),
            ).fetchone()
            if revision_row is None:
                raise RevisionConflictError("Exact closeout revision was not found.")
            revision = dict(revision_row)
            if _sha256(revision["content"].encode("utf-8")) != revision["content_sha256"]:
                raise IntegrityFailureError("Stored closeout content hash does not match its revision.")

            state = connection.execute(
                """
                SELECT store_revision, event_count, projection_json
                FROM workflow_state WHERE repository_id = ? AND milestone_id = ?
                """,
                (self.identity.repository_id, self.identity.milestone_id),
            ).fetchone()
            events = self._load_events(connection)
            projection = self._validate_projection(state, events)
            links = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT event_id, document_id, revision_id, content_sha256, link_role
                    FROM workflow_document_link
                    WHERE repository_id = ? AND milestone_id = ?
                        AND document_id = ? AND revision_id = ? AND content_sha256 = ?
                    ORDER BY event_id, link_role
                    """,
                    (
                        self.identity.repository_id,
                        self.identity.milestone_id,
                        document_id,
                        revision_id,
                        revision["content_sha256"],
                    ),
                )
            ]
            decisions = [
                {
                    **{
                        key: row[key]
                        for key in ("decision_type", "actor_role", "po_confirmed", "source_kind", "decided_at")
                    },
                    "payload": json.loads(row["payload_json"]),
                }
                for row in connection.execute(
                    """
                    SELECT decision_type, actor_role, po_confirmed, source_kind, decided_at, payload_json
                    FROM document_revision_decision
                    WHERE repository_id = ? AND milestone_id = ?
                        AND document_id = ? AND revision_id = ?
                    ORDER BY decided_at, decision_id
                    """,
                    (self.identity.repository_id, self.identity.milestone_id, document_id, revision_id),
                )
            ]
            result = {
                "repository_id": self.identity.repository_id,
                "milestone_id": self.identity.milestone_id,
                "store_revision": state["store_revision"],
                "projection": projection,
                "events": events,
                "revision": revision,
                "links": links,
                "decisions": decisions,
            }
            connection.commit()
            return result
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def get_document_publication(
        self,
        *,
        publication_id: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any] | None:
        if (publication_id is None) == (request_id is None):
            raise ValueError("Supply exactly one publication ID or request ID.")
        column, value = (
            ("publication_id", publication_id)
            if publication_id is not None
            else ("request_id", request_id)
        )
        if not isinstance(value, str) or not value:
            raise ValueError("Publication lookup ID must be non-empty text.")
        connection = self._connection(read_only=True)
        try:
            self._verify_identity(connection, self.identity)
            row = connection.execute(
                f"""
                SELECT * FROM document_publication
                WHERE repository_id = ? AND milestone_id = ? AND {column} = ?
                """,
                (self.identity.repository_id, self.identity.milestone_id, value),
            ).fetchone()
            return self._publication_row(row) if row is not None else None
        finally:
            connection.close()

    def create_document_publication(
        self,
        *,
        publication_id: str,
        request_id: str,
        request_hash: str,
        document_id: str,
        revision_id: str,
        content_sha256: str,
        target_path: str,
        target_branch: str,
        target_head: str,
        worktree_fingerprint: str,
        index_fingerprint: str,
        preview_fingerprint: str,
        git_observation: dict[str, Any],
    ) -> dict[str, Any]:
        for label, value in (
            ("publication ID", publication_id),
            ("request ID", request_id),
            ("document ID", document_id),
            ("revision ID", revision_id),
            ("target branch", target_branch),
        ):
            if not isinstance(value, str) or not value or len(value) > 256:
                raise ValueError(f"{label} must be non-empty text of at most 256 characters.")
        _validate_relative_path(target_path, "Publication target path")
        for label, value in (
            ("request hash", request_hash),
            ("content hash", content_sha256),
            ("worktree fingerprint", worktree_fingerprint),
            ("index fingerprint", index_fingerprint),
            ("preview fingerprint", preview_fingerprint),
        ):
            if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
                raise ValueError(f"{label} must be a lowercase SHA-256 digest.")
        if not isinstance(target_head, str) or not re.fullmatch(r"(?:[a-f0-9]{40}|[a-f0-9]{64})", target_head):
            raise ValueError("Target HEAD must be a full Git object ID.")
        if not isinstance(git_observation, dict):
            raise ValueError("Git observation must be a JSON object.")

        connection = self._connection()
        try:
            self._begin_immediate(connection)
            self._verify_identity(connection, self.identity)
            existing = connection.execute(
                """
                SELECT * FROM document_publication
                WHERE repository_id = ? AND milestone_id = ? AND request_id = ?
                """,
                (self.identity.repository_id, self.identity.milestone_id, request_id),
            ).fetchone()
            if existing is not None:
                if existing["request_hash"] != request_hash:
                    raise IdempotencyConflictError(
                        "Publication request ID was already used with different inputs."
                    )
                connection.commit()
                return self._publication_row(existing)

            revision = connection.execute(
                """
                SELECT d.logical_type, d.source_path
                FROM document_revision r JOIN document d ON d.document_id = r.document_id
                WHERE r.repository_id = ? AND r.milestone_id = ?
                    AND r.document_id = ? AND r.revision_id = ? AND r.content_sha256 = ?
                """,
                (
                    self.identity.repository_id,
                    self.identity.milestone_id,
                    document_id,
                    revision_id,
                    content_sha256,
                ),
            ).fetchone()
            if revision is None:
                raise RevisionConflictError("Publication must reference an exact stored revision and hash.")
            if revision["logical_type"] != "CLOSEOUT" or revision["source_path"] != target_path:
                raise IdentityMismatchError("Publication target must match the exact closeout revision path.")

            timestamp = _now()
            connection.execute(
                """
                INSERT INTO document_publication
                    (repository_id, milestone_id, publication_id, request_id, request_hash,
                     document_id, revision_id, content_sha256, target_path, target_branch, target_head,
                     worktree_fingerprint, index_fingerprint, preview_fingerprint,
                     git_observation_json, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'accepted_not_published', ?, ?)
                """,
                (
                    self.identity.repository_id,
                    self.identity.milestone_id,
                    publication_id,
                    request_id,
                    request_hash,
                    document_id,
                    revision_id,
                    content_sha256,
                    target_path,
                    target_branch,
                    target_head,
                    worktree_fingerprint,
                    index_fingerprint,
                    preview_fingerprint,
                    _json_text(git_observation),
                    timestamp,
                    timestamp,
                ),
            )
            row = connection.execute(
                """
                SELECT * FROM document_publication
                WHERE repository_id = ? AND publication_id = ?
                """,
                (self.identity.repository_id, publication_id),
            ).fetchone()
            connection.commit()
            return self._publication_row(row)
        except sqlite3.IntegrityError as exc:
            if connection.in_transaction:
                connection.rollback()
            if "unique constraint failed" in str(exc).lower():
                raise IdempotencyConflictError("Publication operation identity is already in use.") from exc
            raise IntegrityFailureError("Publication record failed SQLite integrity validation.") from exc
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _publication_row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["git_observation"] = json.loads(result.pop("git_observation_json"))
        return result

    def update_document_publication(
        self,
        publication_id: str,
        *,
        expected_status: str,
        status: str,
        target_branch: str,
        target_head: str,
        worktree_fingerprint: str,
        index_fingerprint: str,
        preview_fingerprint: str,
        git_observation: dict[str, Any],
        related_commit_id: str | None = None,
        failure_reason: str | None = None,
        confirm: bool = False,
    ) -> dict[str, Any]:
        if expected_status not in _PUBLICATION_STATUSES or status not in _PUBLICATION_STATUSES:
            raise ValueError("Publication status is unsupported.")
        if status not in _PUBLICATION_TRANSITIONS[expected_status]:
            raise ValueError(f"Publication cannot transition from {expected_status} to {status}.")
        if not isinstance(publication_id, str) or not publication_id:
            raise ValueError("Publication ID is required.")
        if not isinstance(target_branch, str) or not target_branch:
            raise ValueError("Target branch is required.")
        if not isinstance(target_head, str) or not re.fullmatch(r"(?:[a-f0-9]{40}|[a-f0-9]{64})", target_head):
            raise ValueError("Target HEAD must be a full Git object ID.")
        for label, value in (
            ("worktree fingerprint", worktree_fingerprint),
            ("index fingerprint", index_fingerprint),
            ("preview fingerprint", preview_fingerprint),
        ):
            if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
                raise ValueError(f"{label} must be a lowercase SHA-256 digest.")
        if not isinstance(git_observation, dict):
            raise ValueError("Git observation must be a JSON object.")
        if related_commit_id is not None and not re.fullmatch(
            r"(?:[a-f0-9]{40}|[a-f0-9]{64})", related_commit_id
        ):
            raise ValueError("Related commit ID must be a full Git object ID.")
        if status == "committed" and related_commit_id is None:
            raise ValueError("Committed publication requires a verified related commit ID.")
        if type(confirm) is not bool:
            raise ValueError("Publication confirmation must be an explicit boolean.")
        if confirm and expected_status != "preview_ready":
            raise ValueError("PO confirmation is valid only for a preview-ready publication.")
        if failure_reason is not None and (
            not isinstance(failure_reason, str) or len(failure_reason) > 1000
        ):
            raise ValueError("Failure reason must be text of at most 1000 characters.")

        connection = self._connection()
        try:
            self._begin_immediate(connection)
            row = connection.execute(
                """
                SELECT * FROM document_publication
                WHERE repository_id = ? AND milestone_id = ? AND publication_id = ?
                """,
                (self.identity.repository_id, self.identity.milestone_id, publication_id),
            ).fetchone()
            if row is None:
                raise RevisionConflictError("Publication operation was not found.")
            if row["status"] != expected_status:
                raise StaleStateError("Publication status changed before the update.")
            commit_id = related_commit_id if related_commit_id is not None else row["related_commit_id"]
            confirmed_at = row["confirmed_at"] or (_now() if confirm else None)
            cursor = connection.execute(
                """
                UPDATE document_publication
                SET status = ?, target_branch = ?, target_head = ?,
                    worktree_fingerprint = ?, index_fingerprint = ?, preview_fingerprint = ?,
                    git_observation_json = ?, related_commit_id = ?, failure_reason = ?,
                    confirmed_at = ?, updated_at = ?
                WHERE repository_id = ? AND milestone_id = ? AND publication_id = ? AND status = ?
                """,
                (
                    status,
                    target_branch,
                    target_head,
                    worktree_fingerprint,
                    index_fingerprint,
                    preview_fingerprint,
                    _json_text(git_observation),
                    commit_id,
                    failure_reason,
                    confirmed_at,
                    _now(),
                    self.identity.repository_id,
                    self.identity.milestone_id,
                    publication_id,
                    expected_status,
                ),
            )
            if cursor.rowcount != 1:
                raise StaleStateError("Publication status changed during the update.")
            updated = connection.execute(
                """
                SELECT * FROM document_publication
                WHERE repository_id = ? AND milestone_id = ? AND publication_id = ?
                """,
                (self.identity.repository_id, self.identity.milestone_id, publication_id),
            ).fetchone()
            connection.commit()
            return self._publication_row(updated)
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

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
        if not isinstance(bundle.prompt_bytes, bytes):
            raise ImportSourceError("Prompt fixture source must be caller-supplied bytes.")
        if (bundle.closeout_path is None) != (bundle.closeout_bytes is None):
            raise ImportSourceError("Closeout path and bytes must either both be supplied or both be absent.")
        if (bundle.workflow_path is None) != (bundle.workflow_bytes is None):
            raise ImportSourceError("Workflow path and bytes must either both be supplied or both be absent.")
        closeout_path = (
            _validate_relative_path(bundle.closeout_path, "Closeout source path")
            if bundle.closeout_path is not None
            else None
        )
        workflow_path = (
            _validate_relative_path(bundle.workflow_path, "Workflow source path")
            if bundle.workflow_path is not None
            else None
        )
        try:
            prompt_text = bundle.prompt_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ImportSourceError("Prompt fixture source is not valid UTF-8.") from exc
        closeout_text = None
        if bundle.closeout_bytes is not None:
            try:
                closeout_text = bundle.closeout_bytes.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ImportSourceError("Closeout fixture source is not valid UTF-8.") from exc
        reconciliation_receipt = _validate_reconciliation_receipt(
            bundle.reconciliation_receipt,
            self.identity,
            prompt_path=bundle.prompt_path,
            prompt_bytes=bundle.prompt_bytes,
            workflow_path=bundle.workflow_path,
            workflow_bytes=bundle.workflow_bytes,
        )
        closeout_reconciliation_receipt = _validate_historical_closeout_path_reconciliation_receipt(
            bundle.closeout_reconciliation_receipt,
            self.identity,
            prompt_reconciliation_receipt=reconciliation_receipt,
            prompt_path=bundle.prompt_path,
            prompt_bytes=bundle.prompt_bytes,
            workflow_path=bundle.workflow_path,
            workflow_bytes=bundle.workflow_bytes,
            closeout_path=closeout_path,
            closeout_bytes=bundle.closeout_bytes,
        )
        events = (
            _parse_sidecar_bytes(
                bundle.workflow_bytes,
                self.identity,
                reconciliation_receipt=reconciliation_receipt,
                prompt_path=bundle.prompt_path,
                prompt_bytes=bundle.prompt_bytes,
                workflow_path=bundle.workflow_path,
            )
            if bundle.workflow_bytes is not None
            else []
        )
        historical_evidence_path = (
            closeout_reconciliation_receipt["original_declared_closeout_path"]
            if closeout_reconciliation_receipt is not None
            else closeout_path
        )
        for event in events:
            if (
                closeout_path is not None
                and event["event_type"] == "closeout_submitted"
                and event.get("evidence_path") != historical_evidence_path
            ):
                raise ImportSourceError("Workflow closeout path does not match the caller-supplied closeout source.")
        dispatch_ledger: dict[str, Any] | None = None
        if bundle.dispatch_bytes is not None:
            if not isinstance(bundle.dispatch_path, str):
                raise ImportSourceError("Dispatch bytes require the exact external ledger source path.")
            dispatch_path = _validate_dispatch_source_path(bundle.dispatch_path, self.identity)
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
        else:
            dispatch_path = None

        prompt_hash = _sha256(bundle.prompt_bytes)
        prompt_doc_id = _sha256(
            _json_text([self.identity.repository_id, self.identity.milestone_id, prompt_path]).encode("utf-8")
        )[:32]
        prompt_revision_id = _sha256(f"{prompt_doc_id}:{prompt_hash}".encode("utf-8"))[:32]
        closeout_hash = _sha256(bundle.closeout_bytes) if bundle.closeout_bytes is not None else None
        closeout_doc_id = (
            _sha256(
                _json_text([self.identity.repository_id, self.identity.milestone_id, closeout_path]).encode(
                    "utf-8"
                )
            )[:32]
            if closeout_path is not None
            else None
        )
        closeout_revision_id = (
            _sha256(f"{closeout_doc_id}:{closeout_hash}".encode("utf-8"))
            if closeout_doc_id is not None and closeout_hash is not None
            else None
        )
        source_snapshots = [("prompt", prompt_path, bundle.prompt_bytes)]
        if closeout_path is not None and bundle.closeout_bytes is not None:
            source_snapshots.append(("closeout", closeout_path, bundle.closeout_bytes))
        if workflow_path is not None and bundle.workflow_bytes is not None:
            source_snapshots.append(("workflow", workflow_path, bundle.workflow_bytes))
        dispatch_hash = None
        if bundle.dispatch_bytes is not None and bundle.dispatch_path is not None:
            dispatch_hash = _sha256(bundle.dispatch_bytes)
            source_snapshots.append(("dispatch", dispatch_path, bundle.dispatch_bytes))
        matching_dispatches = (
            [
                item
                for item in dispatch_ledger["dispatches"]
                if item["milestone_id"] == self.identity.milestone_id
            ]
            if dispatch_ledger is not None
            else []
        )
        if len(matching_dispatches) > MAX_DISPATCH_COUNT:
            raise ImportSourceError(f"Fixture dispatch count exceeds {MAX_DISPATCH_COUNT}.")
        source_manifest = [
            {
                "source_kind": kind,
                "source_path": path,
                "source_sha256": _sha256(source_bytes),
                "source_bytes": len(source_bytes),
                "source_owner": "central_operational" if kind == "dispatch" else "git_owned",
            }
            for kind, path, source_bytes in source_snapshots
        ]
        receipt_payload = {
            "repository_id": self.identity.repository_id,
            "milestone_id": self.identity.milestone_id,
            "sources": source_manifest,
            "event_ids": [event["event_id"] for event in events],
            "event_count": len(events),
            "dispatch_count": len(matching_dispatches),
        }
        if reconciliation_receipt is not None:
            receipt_payload["historical_reconciliation"] = reconciliation_receipt
        if closeout_reconciliation_receipt is not None:
            receipt_payload["historical_closeout_reconciliation"] = closeout_reconciliation_receipt
        receipt_sha256 = _sha256(
            _json_text(
                receipt_payload
            ).encode("utf-8")
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
            "imported_event_count": len(events),
            "linked_event_count": 0,
            "dispatch_count": len(matching_dispatches),
            "transaction_boundaries": "not_invented",
            "approved_prompt_revision_id": None,
            "source_manifest": source_manifest,
            "import_receipt_sha256": receipt_sha256,
            "historical_reconciliation": reconciliation_receipt,
            "historical_closeout_reconciliation": closeout_reconciliation_receipt,
        }
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
                stored_sources = [
                    (row["source_kind"], row["source_path"], row["source_sha256"], bytes(row["source_bytes"]))
                    for row in connection.execute(
                        """
                        SELECT source_kind, source_path, source_sha256, source_bytes
                        FROM import_source
                        WHERE repository_id = ? AND milestone_id = ?
                        ORDER BY source_kind, source_path
                        """,
                        (self.identity.repository_id, self.identity.milestone_id),
                    )
                ]
                expected_sources = sorted(
                    (kind, path, _sha256(source_bytes), source_bytes)
                    for kind, path, source_bytes in source_snapshots
                )
                if sorted(stored_sources) != expected_sources:
                    raise ImportSourceError("Existing import source snapshot differs from the requested retry.")
                expected_documents = [
                    (prompt_doc_id, prompt_revision_id, prompt_path, "PROMPT", prompt_text, prompt_hash)
                ]
                if (
                    closeout_doc_id is not None
                    and closeout_revision_id is not None
                    and closeout_path is not None
                    and closeout_text is not None
                    and closeout_hash is not None
                ):
                    expected_documents.append(
                        (
                            closeout_doc_id,
                            closeout_revision_id,
                            closeout_path,
                            "CLOSEOUT",
                            closeout_text,
                            closeout_hash,
                        )
                    )
                actual_document_count = connection.execute(
                    "SELECT COUNT(*) FROM document WHERE repository_id = ? AND milestone_id = ?",
                    (self.identity.repository_id, self.identity.milestone_id),
                ).fetchone()[0]
                actual_revision_count = connection.execute(
                    "SELECT COUNT(*) FROM document_revision WHERE repository_id = ? AND milestone_id = ?",
                    (self.identity.repository_id, self.identity.milestone_id),
                ).fetchone()[0]
                if actual_document_count != len(expected_documents) or actual_revision_count != len(expected_documents):
                    raise ImportSourceError("Existing import document set differs from the requested retry.")
                for document_id, revision_id, path, logical_type, content, content_hash in expected_documents:
                    row = connection.execute(
                        """
                        SELECT d.document_id, d.logical_key, d.logical_type, d.source_path,
                               r.revision_id, r.content, r.content_sha256
                        FROM document d JOIN document_revision r ON r.document_id = d.document_id
                        WHERE d.repository_id = ? AND d.milestone_id = ? AND d.logical_key = ?
                        """,
                        (self.identity.repository_id, self.identity.milestone_id, path),
                    ).fetchone()
                    if row is None or tuple(row) != (
                        document_id,
                        path,
                        logical_type,
                        path,
                        revision_id,
                        content,
                        content_hash,
                    ):
                        raise ImportSourceError("Existing imported revision differs from the requested retry.")
                    if (
                        closeout_reconciliation_receipt is not None
                        and logical_type == "CLOSEOUT"
                    ):
                        provenance_row = connection.execute(
                            """
                            SELECT provenance_json FROM document_revision
                            WHERE repository_id = ? AND milestone_id = ?
                                AND document_id = ? AND revision_id = ?
                            """,
                            (
                                self.identity.repository_id,
                                self.identity.milestone_id,
                                document_id,
                                revision_id,
                            ),
                        ).fetchone()
                        if (
                            provenance_row is None
                            or json.loads(provenance_row["provenance_json"]).get(
                                "historical_closeout_path_reconciliation"
                            ) != closeout_reconciliation_receipt
                        ):
                            raise ImportSourceError(
                                "Existing closeout reconciliation receipt differs from the requested retry."
                            )
                stored_events = self._load_events(connection)
                state = connection.execute(
                    """
                    SELECT store_revision, event_count, projection_json
                    FROM workflow_state WHERE repository_id = ? AND milestone_id = ?
                    """,
                    (self.identity.repository_id, self.identity.milestone_id),
                ).fetchone()
                self._validate_projection(state, stored_events)
                if stored_events != events:
                    raise ImportSourceError("Existing imported workflow events differ from the requested retry.")
                connection.commit()
                return result

            if self.authority_configuration is not None:
                raise ImportSourceError("New legacy imports require a quiesced, inactive prepared store.")
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
            prompt_item = self._revision_data(prompt_revision)
            self._insert_revision(connection, prompt_item)
            if (
                closeout_doc_id is not None
                and closeout_revision_id is not None
                and closeout_path is not None
                and closeout_text is not None
                and closeout_hash is not None
            ):
                closeout_revision = DocumentRevisionInput(
                    document_id=closeout_doc_id,
                    revision_id=closeout_revision_id,
                    logical_key=closeout_path,
                    logical_type="CLOSEOUT",
                    source_path=closeout_path,
                    content=closeout_text,
                    source_kind="v1_import",
                    provenance={
                        "source_path": closeout_path,
                        "source_sha256": closeout_hash,
                        **(
                            {
                                "historical_closeout_path_reconciliation": (
                                    closeout_reconciliation_receipt
                                )
                            }
                            if closeout_reconciliation_receipt is not None
                            else {}
                        ),
                    },
                )
                self._insert_revision(connection, self._revision_data(closeout_revision))
            for source_kind, source_path, source_bytes in source_snapshots:
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
                for record in matching_dispatches:
                    self._insert_legacy_dispatch(connection, record, dispatch_hash)

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

    def _insert_legacy_dispatch(
        self,
        connection: sqlite3.Connection,
        record: Mapping[str, Any],
        source_hash: str | None,
    ) -> None:
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
                self.identity.repository_id, record["dispatch_id"], record["milestone_id"],
                record["type"], record["status"], record["body"], record.get("title"),
                record.get("approved_milestone_id"), record.get("related_event_id"),
                source_hash, raw, record["created_at"], record["updated_at"],
            ),
        )
        connection.execute(
            """
            INSERT INTO dispatch_history
                (repository_id, dispatch_id, sequence, history_id, record_kind, from_status,
                 to_status, actor_role, actor_attribution, po_confirmed, related_event_id,
                 recorded_at, source_kind, payload_json)
            VALUES (?, ?, 1, ?, 'LEGACY_SNAPSHOT', NULL, ?, 'repo_control', ?,
                    NULL, ?, ?, 'v1_import', ?)
            """,
            (
                self.identity.repository_id, record["dispatch_id"], uuid.uuid4().hex,
                record["status"],
                "Imported current ledger snapshot; actor and historical decision are not authenticated.",
                record.get("related_event_id"), record["updated_at"], raw,
            ),
        )

    def reconcile_disposable_dispatch_snapshot(
        self,
        *,
        source_path: str,
        source_bytes: bytes,
    ) -> dict[str, Any]:
        """Complete a quiesced repository-wide import without inventing decisions."""
        if self.authority_configuration is not None:
            raise ImportSourceError("Legacy dispatch import requires a quiesced, inactive prepared store.")
        path = _validate_dispatch_source_path(source_path, self.identity)
        if not isinstance(source_bytes, bytes) or len(source_bytes) > architect_dispatches.MAX_LEDGER_BYTES:
            raise ImportSourceError("Dispatch snapshot must be bounded source bytes.")
        try:
            ledger = architect_dispatches._validate_ledger(
                json.loads(source_bytes.decode("utf-8")), self.identity.repository_id,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, architect_dispatches.ArchitectDispatchError) as exc:
            raise ImportSourceError(f"Dispatch fixture source is malformed: {exc}") from exc
        source_hash = _sha256(source_bytes)
        request = {
            "repository_id": self.identity.repository_id,
            "source_path": path,
            "source_sha256": source_hash,
            "dispatch_ids": [item["dispatch_id"] for item in ledger["dispatches"]],
        }
        request_hash = _sha256(_json_text(request).encode("utf-8"))
        request_id = f"dispatch-snapshot:{request_hash}"
        connection = self._connection()
        try:
            self._begin_immediate(connection)
            self._verify_identity(connection, self.identity)
            stored = {
                row["dispatch_id"]: row
                for row in connection.execute(
                    """
                    SELECT dispatch_id, milestone_id, dispatch_type, status, body, title,
                           approved_milestone_id, related_event_id, source_sha256,
                           payload_json, created_at, updated_at
                    FROM architect_dispatch WHERE repository_id = ?
                    """,
                    (self.identity.repository_id,),
                ).fetchall()
            }
            expected_ids = set(request["dispatch_ids"])
            if set(stored) - expected_ids:
                raise ImportSourceError("Store has dispatches absent from the quiesced source snapshot.")
            receipt = connection.execute(
                """
                SELECT request_hash, result_json FROM transaction_receipt
                WHERE repository_id = ? AND milestone_id = ? AND request_id = ?
                """,
                (self.identity.repository_id, self.identity.milestone_id, request_id),
            ).fetchone()
            inserted = 0
            for record in ledger["dispatches"]:
                raw = _json_text(record)
                existing = stored.get(record["dispatch_id"])
                if existing is not None:
                    history = connection.execute(
                        """
                        SELECT record_kind, source_kind, po_confirmed, to_status, payload_json
                        FROM dispatch_history WHERE repository_id = ? AND dispatch_id = ?
                        ORDER BY sequence
                        """,
                        (self.identity.repository_id, record["dispatch_id"]),
                    ).fetchall()
                    if (
                        existing["source_sha256"] != source_hash
                        or existing["payload_json"] != raw
                        or existing["milestone_id"] != record["milestone_id"]
                        or existing["dispatch_type"] != record["type"]
                        or existing["status"] != record["status"]
                        or existing["body"] != record["body"]
                        or existing["title"] != record.get("title")
                        or existing["approved_milestone_id"] != record.get("approved_milestone_id")
                        or existing["related_event_id"] != record.get("related_event_id")
                        or existing["created_at"] != record["created_at"]
                        or existing["updated_at"] != record["updated_at"]
                        or len(history) != 1
                        or history[0]["record_kind"] != "LEGACY_SNAPSHOT"
                        or history[0]["source_kind"] != "v1_import"
                        or history[0]["po_confirmed"] is not None
                        or history[0]["to_status"] != record["status"]
                        or history[0]["payload_json"] != raw
                    ):
                        raise ImportSourceError("Stored dispatch differs from its unmodified source snapshot.")
                    continue
                if receipt is not None:
                    raise ImportSourceError("An imported dispatch is missing from an existing snapshot receipt.")
                self._insert_legacy_dispatch(connection, record, source_hash)
                inserted += 1
            source_id = _source_record_id(
                self.identity.repository_id, self.identity.milestone_id, "dispatch", path, source_hash,
            )
            source = connection.execute(
                "SELECT source_bytes FROM import_source WHERE source_id = ?", (source_id,),
            ).fetchone()
            if source is None:
                if receipt is not None:
                    raise ImportSourceError("Dispatch snapshot source is missing from its import receipt.")
                connection.execute(
                    """
                    INSERT INTO import_source
                        (source_id, repository_id, milestone_id, source_kind, source_path,
                         source_sha256, source_bytes, imported_at)
                    VALUES (?, ?, ?, 'dispatch', ?, ?, ?, ?)
                    """,
                    (
                        source_id, self.identity.repository_id, self.identity.milestone_id,
                        path, source_hash, source_bytes, _now(),
                    ),
                )
            elif bytes(source["source_bytes"]) != source_bytes:
                raise ImportSourceError("Stored dispatch source bytes differ from the requested snapshot.")
            if receipt is not None:
                if receipt["request_hash"] != request_hash:
                    raise IdempotencyConflictError("Dispatch snapshot receipt conflicts with the requested source.")
                result = json.loads(receipt["result_json"])
            else:
                result = {
                    "repository_id": self.identity.repository_id,
                    "source_sha256": source_hash,
                    "source_count": len(ledger["dispatches"]),
                    "imported_missing_count": inserted,
                    "receipt_sha256": request_hash,
                    "decision_history": "unavailable in v1 source",
                }
                revision = connection.execute(
                    "SELECT store_revision FROM workflow_state WHERE repository_id = ? AND milestone_id = ?",
                    (self.identity.repository_id, self.identity.milestone_id),
                ).fetchone()["store_revision"]
                connection.execute(
                    """
                    INSERT INTO transaction_receipt
                        (repository_id, milestone_id, request_id, request_hash,
                         resulting_revision, result_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        self.identity.repository_id, self.identity.milestone_id, request_id,
                        request_hash, revision, _json_text(result), _now(),
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
        milestone_id: str | None = None,
        milestone_root: str | None = None,
        created_at: str | None = None,
        title: str | None = None,
        related_event_id: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(dispatch_id, str) or not re.fullmatch(r"[a-f0-9]{32}", dispatch_id):
            raise ValueError("Dispatch ID must be 32 lowercase hexadecimal characters.")
        if dispatch_type not in _DISPATCH_TYPES:
            raise ValueError("Unsupported Architect dispatch type.")
        target_milestone_id = milestone_id or self.identity.milestone_id
        if not isinstance(target_milestone_id, str) or not _MILESTONE_ID_RE.fullmatch(target_milestone_id):
            raise ValueError("Dispatch target must be a canonical milestone ID.")
        if dispatch_type != "PROMPT" and target_milestone_id != self.identity.milestone_id:
            raise IdentityMismatchError("Guidance dispatch must target the selected milestone.")
        if dispatch_type == "PROMPT" and title is None:
            raise ValueError("A PROMPT proposal requires a title.")
        try:
            architect_dispatches._validate_body(body)
            slug = architect_dispatches._slug(title) if dispatch_type == "PROMPT" else None
        except architect_dispatches.ArchitectDispatchError as exc:
            raise ValueError(str(exc)) from exc
        if dispatch_type != "PROMPT" and title is not None:
            raise ValueError("Only PROMPT dispatches may include a title.")
        if dispatch_type == "PROMPT" and related_event_id is not None:
            raise ValueError("A PROMPT proposal cannot refer to an existing milestone event.")
        timestamp = _validate_timestamp(created_at or _now())
        if related_event_id is not None:
            if not self._event_exists(related_event_id):
                raise DispatchDecisionError("Related workflow event does not exist in this milestone.")
        payload = {
            "dispatch_id": dispatch_id,
            "repository_id": self.identity.repository_id,
            "milestone_id": target_milestone_id,
            "type": dispatch_type,
            "status": "PENDING",
            "body": body,
            "title": title,
            "created_at": timestamp,
            "updated_at": timestamp,
            "related_event_id": related_event_id,
        }
        if dispatch_type == "PROMPT":
            root = str(PurePosixPath(self.identity.prompt_path).parent) if milestone_root is None else milestone_root
            _validate_relative_path(root, "Milestone root")
            destination = str(PurePosixPath(root) / f"{target_milestone_id}_{slug}_prompt.md")
            _validate_relative_path(destination, "Prompt destination")
            payload.update(slug=slug, title=title.strip(), canonical_destination=destination)
        raw = _json_text(payload)
        connection = self._connection()
        try:
            self._begin_immediate(connection)
            self._verify_identity(connection, self.identity)
            dispatch_count = connection.execute(
                "SELECT COUNT(*) FROM architect_dispatch WHERE repository_id = ?",
                (self.identity.repository_id,),
            ).fetchone()[0]
            if dispatch_count >= MAX_DISPATCH_COUNT:
                raise DispatchDecisionError("The repository dispatch limit was reached; no proposal was recorded.")
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
                    target_milestone_id,
                    dispatch_type,
                    body,
                    title.strip() if title is not None else None,
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
                        'Dispatch proposal; actor identity is not authenticated.', NULL, ?, ?, ?, ?)
                """,
                (
                    self.identity.repository_id,
                    dispatch_id,
                    uuid.uuid4().hex,
                    related_event_id,
                    timestamp,
                    "fixture" if self.creation_mode == "disposable_fixture" else "architect_submission",
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

    def _prompt_dispatch_revision(
        self,
        dispatch_id: str,
        dispatch: sqlite3.Row,
        milestone_root: str | None,
    ) -> DocumentRevisionInput:
        payload = json.loads(dispatch["payload_json"])
        title = dispatch["title"]
        if not isinstance(payload, dict) or not isinstance(title, str):
            raise DispatchDecisionError("Prompt proposal has an invalid stored identity.")
        try:
            slug = architect_dispatches._slug(title)
            architect_dispatches._validate_body(dispatch["body"])
        except architect_dispatches.ArchitectDispatchError as exc:
            raise DispatchDecisionError(str(exc)) from exc
        root = str(PurePosixPath(self.identity.prompt_path).parent) if milestone_root is None else milestone_root
        _validate_relative_path(root, "Milestone root")
        destination = str(PurePosixPath(root) / f"{dispatch['milestone_id']}_{slug}_prompt.md")
        if (
            payload.get("dispatch_id") != dispatch_id
            or payload.get("repository_id") != self.identity.repository_id
            or payload.get("milestone_id") != dispatch["milestone_id"]
            or payload.get("type") != "PROMPT"
            or payload.get("body") != dispatch["body"]
            or payload.get("title") != title
            or payload.get("slug") != slug
            or payload.get("canonical_destination") != destination
        ):
            raise IdentityMismatchError("Prompt proposal no longer matches its canonical destination or content.")
        body = dispatch["body"]
        return DocumentRevisionInput(
            document_id=f"dispatch-prompt:{dispatch_id}",
            revision_id=f"dispatch-prompt-revision:{dispatch_id}",
            logical_key=destination,
            logical_type="PROMPT",
            source_path=destination,
            content=body if body.endswith("\n") else body + "\n",
            source_kind="architect_dispatch",
            provenance={
                "dispatch_id": dispatch_id,
                "fixture_po_decision" if self.creation_mode == "disposable_fixture" else "po_browser_decision": True,
            },
        )

    def record_dispatch_decision(
        self,
        *,
        dispatch_id: str,
        history_id: str,
        status: str,
        actor_role: str,
        actor_attribution: str,
        trusted_fixture_po_confirmation: bool = False,
        trusted_managed_po_confirmation: bool = False,
        approved_milestone_id: str | None = None,
        related_event_id: str | None = None,
        recorded_at: str | None = None,
        milestone_root: str | None = None,
        expected_fingerprint: str | None = None,
    ) -> dict[str, Any]:
        if status not in {"APPROVED", "REJECTED"}:
            raise ValueError("Dispatch decision must be APPROVED or REJECTED.")
        if not isinstance(history_id, str) or not re.fullmatch(r"[a-f0-9]{32}", history_id):
            raise ValueError("Dispatch decision ID must be 32 lowercase hexadecimal characters.")
        if actor_role != "product_owner" or not actor_attribution:
            raise DispatchDecisionError("A dispatch decision requires reported Product Owner context.")
        if not self._confirmed_po_action(trusted_fixture_po_confirmation, trusted_managed_po_confirmation):
            raise DispatchDecisionError(
                "Dispatch approval/rejection requires explicit trusted fixture or managed PO confirmation."
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
            dispatch = connection.execute(
                """
                SELECT dispatch_id, dispatch_type, status, milestone_id, body, title,
                       approved_milestone_id, related_event_id, source_sha256, payload_json,
                       created_at, updated_at
                FROM architect_dispatch
                WHERE repository_id = ? AND dispatch_id = ?
                """,
                (self.identity.repository_id, dispatch_id),
            ).fetchone()
            if dispatch is None or (
                dispatch["dispatch_type"] != "PROMPT"
                and dispatch["milestone_id"] != self.identity.milestone_id
            ):
                raise DispatchDecisionError("Dispatch was not found for the selected fixture repository or milestone.")
            prompt_item = None
            if status == "APPROVED" and dispatch["dispatch_type"] == "PROMPT":
                prompt_item = self._revision_data(
                    self._prompt_dispatch_revision(dispatch_id, dispatch, milestone_root)
                )
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
            if expected_fingerprint is not None:
                decision_payload["expected_fingerprint"] = expected_fingerprint
            if milestone_root is not None:
                decision_payload["milestone_root"] = milestone_root
            if prompt_item is not None:
                decision_payload["prompt_binding"] = {
                    key: prompt_item[key]
                    for key in ("document_id", "revision_id", "source_path", "content_sha256")
                }
            decision_json = _json_text(decision_payload)
            if existing_history is not None:
                if existing_history["payload_json"] != decision_json:
                    raise IdempotencyConflictError("Dispatch history ID was reused with different decision content.")
                connection.commit()
                return self.get_dispatch(dispatch_id)
            if dispatch["status"] != "PENDING":
                raise DispatchDecisionError("Only a PENDING dispatch may receive a new fixture decision.")
            if expected_fingerprint is not None and (
                not isinstance(expected_fingerprint, str)
                or not re.fullmatch(r"[a-f0-9]{64}", expected_fingerprint)
                or dispatch_reconciliation_fingerprint(dict(dispatch)) != expected_fingerprint
            ):
                raise StaleStateError("The dispatch changed since the fixture decision was reviewed.")
            if status == "APPROVED":
                if dispatch["dispatch_type"] == "PROMPT" and approved_milestone_id is not None:
                    raise DispatchDecisionError("A prompt dispatch decision does not approve the milestone workflow.")
                if dispatch["dispatch_type"] != "PROMPT" and approved_milestone_id != self.identity.milestone_id:
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
            if prompt_item is not None:
                existing = connection.execute(
                    "SELECT 1 FROM milestone WHERE repository_id = ? AND milestone_id = ?",
                    (self.identity.repository_id, dispatch["milestone_id"]),
                ).fetchone()
                if existing is not None:
                    raise DispatchDecisionError("A prompt for this milestone already exists; no revision was created.")
                connection.execute(
                    "INSERT INTO milestone(repository_id, milestone_id, prompt_path) VALUES (?, ?, ?)",
                    (self.identity.repository_id, dispatch["milestone_id"], prompt_item["source_path"]),
                )
                connection.execute(
                    """
                    INSERT INTO workflow_state
                        (repository_id, milestone_id, store_revision, event_count, projection_json)
                    VALUES (?, ?, 0, 0, ?)
                    """,
                    (self.identity.repository_id, dispatch["milestone_id"], _json_text(_projection([]))),
                )
                self._insert_revision(connection, prompt_item, milestone_id=dispatch["milestone_id"])
                current_payload.update(
                    prompt_path=prompt_item["source_path"],
                    prompt_document_id=prompt_item["document_id"],
                    prompt_revision_id=prompt_item["revision_id"],
                    prompt_sha256=prompt_item["content_sha256"],
                )
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
                VALUES (?, ?, ?, ?, 'DECISION', 'PENDING', ?, ?, ?, 1, ?, ?, ?, ?)
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
                    "trusted_fixture" if self.creation_mode == "disposable_fixture" else "po_browser",
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

    def reconcile_imported_guidance(
        self,
        *,
        dispatch_id: str,
        history_id: str,
        status: str,
        expected_fingerprint: str,
        trusted_fixture_po_confirmation: bool = False,
        trusted_managed_po_confirmation: bool = False,
    ) -> dict[str, Any]:
        if status not in {"APPROVED", "REJECTED"}:
            raise ValueError("Reconciliation must explicitly approve or reject the imported guidance.")
        if not isinstance(history_id, str) or not re.fullmatch(r"[a-f0-9]{32}", history_id):
            raise ValueError("Reconciliation history ID must be 32 lowercase hexadecimal characters.")
        if not isinstance(expected_fingerprint, str) or not re.fullmatch(r"[a-f0-9]{64}", expected_fingerprint):
            raise ValueError("The reviewed guidance fingerprint must be a lowercase SHA-256 digest.")
        if not self._confirmed_po_action(trusted_fixture_po_confirmation, trusted_managed_po_confirmation):
            raise DispatchDecisionError("Imported guidance reconciliation requires explicit PO confirmation.")
        decision = {
            "dispatch_id": dispatch_id,
            "status": status,
            "reviewed_fingerprint": expected_fingerprint,
            "po_confirmed": True,
            "historical_approval": "UNKNOWN/NOT VERIFIED",
        }
        decision_json = _json_text(decision)
        connection = self._connection()
        try:
            self._begin_immediate(connection)
            self._verify_identity(connection, self.identity)
            prior = connection.execute(
                "SELECT payload_json FROM dispatch_history WHERE repository_id = ? AND history_id = ?",
                (self.identity.repository_id, history_id),
            ).fetchone()
            if prior is not None:
                if prior["payload_json"] != decision_json:
                    raise IdempotencyConflictError("Reconciliation history ID was reused with different content.")
                connection.commit()
                return self.get_dispatch(dispatch_id)
            row = connection.execute(
                """
                SELECT dispatch_id, milestone_id, dispatch_type, status, body, title,
                       approved_milestone_id, related_event_id, source_sha256, payload_json,
                       created_at, updated_at
                FROM architect_dispatch WHERE repository_id = ? AND dispatch_id = ?
                """,
                (self.identity.repository_id, dispatch_id),
            ).fetchone()
            if row is None:
                raise DispatchDecisionError("Imported guidance was not found.")
            dispatch = dict(row)
            if (
                dispatch["milestone_id"] != self.identity.milestone_id
                or dispatch["approved_milestone_id"] != self.identity.milestone_id
                or dispatch["dispatch_type"] not in {"INSTRUCTION", "RESPONSE"}
                or dispatch["status"] != "APPROVED"
                or not dispatch["source_sha256"]
            ):
                raise DispatchDecisionError("Only applicable imported approved guidance can be reconciled.")
            history = connection.execute(
                """
                SELECT record_kind, source_kind, po_confirmed FROM dispatch_history
                WHERE repository_id = ? AND dispatch_id = ? ORDER BY sequence
                """,
                (self.identity.repository_id, dispatch_id),
            ).fetchall()
            if (
                len(history) != 1
                or history[0]["record_kind"] != "LEGACY_SNAPSHOT"
                or history[0]["source_kind"] != "v1_import"
                or history[0]["po_confirmed"] is not None
            ):
                raise DispatchDecisionError("Guidance is not an unreconciled imported status snapshot.")
            if dispatch_reconciliation_fingerprint(dispatch) != expected_fingerprint:
                raise StaleStateError("The imported guidance changed since it was reviewed.")
            timestamp = _now()
            current_payload = json.loads(dispatch["payload_json"])
            current_payload.update(status=status, updated_at=timestamp)
            connection.execute(
                """
                UPDATE architect_dispatch SET status = ?, updated_at = ?, payload_json = ?
                WHERE repository_id = ? AND dispatch_id = ?
                """,
                (status, timestamp, _json_text(current_payload), self.identity.repository_id, dispatch_id),
            )
            connection.execute(
                """
                INSERT INTO dispatch_history
                    (repository_id, dispatch_id, sequence, history_id, record_kind, from_status,
                     to_status, actor_role, actor_attribution, po_confirmed, related_event_id,
                     recorded_at, source_kind, payload_json)
                VALUES (?, ?, 2, ?, 'DECISION', 'APPROVED', ?, 'product_owner', ?, 1, ?, ?, ?, ?)
                """,
                (
                    self.identity.repository_id,
                    dispatch_id,
                    history_id,
                    status,
                    (
                        "Explicit fixture PO decision now; the historical approval remains unverified."
                        if self.creation_mode == "disposable_fixture"
                        else "Explicit PO browser decision now; the historical approval remains unverified."
                    ),
                    dispatch["related_event_id"],
                    timestamp,
                    (
                        "trusted_fixture_reconciliation"
                        if self.creation_mode == "disposable_fixture" else "po_browser_reconciliation"
                    ),
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

    def list_dispatches(
        self,
        *,
        milestone_id: str | None = None,
        status: str | None = None,
        limit: int = MAX_DISPATCH_COUNT,
    ) -> list[dict[str, Any]]:
        if milestone_id is not None and (
            not isinstance(milestone_id, str) or not _MILESTONE_ID_RE.fullmatch(milestone_id)
        ):
            raise ValueError("Dispatch filter must be a canonical milestone ID.")
        if status is not None and status not in _DISPATCH_STATUSES:
            raise ValueError("Unsupported Architect dispatch status.")
        if type(limit) is not int or not 1 <= limit <= MAX_DISPATCH_COUNT:
            raise ValueError(f"Dispatch limit must be between 1 and {MAX_DISPATCH_COUNT}.")
        connection = self._connection(read_only=True)
        try:
            self._verify_identity(connection, self.identity)
            rows = connection.execute(
                """
                SELECT dispatch_id, milestone_id, dispatch_type, status, body, title,
                       approved_milestone_id, related_event_id, source_sha256, payload_json,
                       created_at, updated_at
                FROM architect_dispatch
                WHERE repository_id = ? AND (? IS NULL OR milestone_id = ?)
                    AND (? IS NULL OR status = ?)
                ORDER BY created_at DESC, dispatch_id DESC
                LIMIT ?
                """,
                (
                    self.identity.repository_id,
                    milestone_id,
                    milestone_id,
                    status,
                    status,
                    limit + 1,
                ),
            ).fetchall()
            if len(rows) > limit:
                raise DispatchDecisionError(
                    f"Dispatch list exceeds the bounded {limit}-record page."
                )
            result = []
            for row in rows:
                dispatch = dict(row)
                dispatch["payload"] = json.loads(dispatch.pop("payload_json"))
                dispatch["authorization_granted"] = False
                result.append(dispatch)
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
