from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import tempfile
import uuid
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from .document_library import (
    DocumentRecord,
    GitFileProvider,
    GitRepositoryContext,
    ProviderSnapshot,
    SourceEnumerationError,
)
from .information_store import (
    ImportSourceError,
    InformationStore,
    StoreIdentity,
    V1FixtureBundle,
    build_historical_closeout_path_reconciliation_receipt,
    build_historical_path_reconciliation_receipt,
    historical_prompt_path_alias,
    _parse_sidecar_bytes,
    _projection,
    _read_fixture_selection_snapshot,
    _validate_selection_root,
)
from .scanner.util import make_repository_id
from .scanner.core import state_artifact_lock
from .store_authority import load_store_authority
from .web.repository_registry import RepositoryEntry, RepositoryRegistry
from .workflow.architect_dispatches import ArchitectDispatchError, read_owner_dispatch_ledger_snapshot
from .workflow.lifecycle import WorkflowRecordError, _reduce_events


_CANONICAL_MILESTONE_ID = re.compile(r"(?:M[0-9]{3}(?:\.[0-9]{3})+|R[0-9]{3}\.[0-9]{2})\Z")


class MigrationRehearsalError(RuntimeError):
    pass


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _is_v1_workflow_record(record: DocumentRecord) -> bool:
    return (
        record.logical_type == "WORKFLOW"
        and record.milestone_id is not None
        and PurePosixPath(record.path).name.startswith(f"{record.milestone_id}_")
        and PurePosixPath(record.path).name.endswith("_workflow.md")
    )


@dataclass(frozen=True)
class RepositoryInventory:
    repository: RepositoryEntry
    current_milestone_id: str | None
    provider_snapshot: ProviderSnapshot
    dispatch_snapshot: dict[str, Any] | None = field(default=None, repr=False)
    exclusions: tuple[dict[str, str], ...] = ()
    warnings: tuple[str, ...] = ()

    def report(self) -> dict[str, Any]:
        documents = [
            _inventory_document(record, self.repository.repository_id)
            for record in self.provider_snapshot.records
        ]
        dispatch = self.dispatch_snapshot
        dispatch_source = None
        dispatch_records: list[dict[str, Any]] = []
        if dispatch is not None:
            ledger = dispatch["ledger"]
            dispatch_source = {
                "repository_id": dispatch["repository_id"],
                "source_path": dispatch["source_path"],
                "status": dispatch["status"],
                "source_sha256": dispatch["source_sha256"],
                "source_size_bytes": dispatch["source_size_bytes"],
                "source_owner": "central_operational",
                "dispatch_count": len(ledger["dispatches"]),
            }
            dispatch_records = [
                {
                    "dispatch_id": record["dispatch_id"],
                    "milestone_id": record["milestone_id"],
                    "type": record["type"],
                    "status": record["status"],
                    "related_event_id": record.get("related_event_id"),
                    "approved_milestone_id": record.get("approved_milestone_id"),
                    "provenance": "validated current ledger snapshot; prior decision history is not inferred",
                }
                for record in ledger["dispatches"]
            ]
        categories: dict[str, int] = {}
        workflow_sidecars = [
            item
            for item in documents
            if item["source_class"] == "v1_workflow_sidecar"
        ]
        for item in documents:
            category = item["source_class"]
            categories[category] = categories.get(category, 0) + 1
        if dispatch_source is not None and dispatch_source["status"] == "present":
            categories["central_dispatch_ledger"] = 1
        canonical = {
            "repository_id": self.repository.repository_id,
            "repository_key": self.repository.key,
            "repository_root": str(self.repository.path),
            "current_milestone_id": self.current_milestone_id,
            "provider_fingerprint": self.provider_snapshot.fingerprint,
            "documents": documents,
            "dispatch_source": dispatch_source,
        }
        return {
            "schema_version": 1,
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            **{
                key: canonical[key]
                for key in (
                    "repository_id",
                    "repository_key",
                    "repository_root",
                    "current_milestone_id",
                    "provider_fingerprint",
                )
            },
            "source_inventory_sha256": _sha256(_canonical_json(canonical)),
            "document_count": len(documents),
            "document_bytes": sum(item["size_bytes"] for item in documents),
            "provider_bytes_read": self.provider_snapshot.bytes_scanned,
            "category_counts": dict(sorted(categories.items())),
            "source_owner_counts": {
                "git_owned": len(documents),
                "central_operational": int(
                    dispatch_source is not None and dispatch_source["status"] == "present"
                ),
            },
            "workflow_sidecar_counts": {
                "total": len(workflow_sidecars),
                "with_events": sum(item["v1_lifecycle_events"] is True for item in workflow_sidecars),
                "without_events": sum(item["v1_lifecycle_events"] is False for item in workflow_sidecars),
                "unparsed_or_unknown": sum(item["v1_lifecycle_events"] is None for item in workflow_sidecars),
            },
            "allowlisted_history_document_count": categories.get("allowlisted_historical_document", 0),
            "noncanonical_milestone_ids_observed": sorted(
                {
                    item["milestone_id"]
                    for item in documents
                    if item["milestone_id"] is not None
                    and not _CANONICAL_MILESTONE_ID.fullmatch(item["milestone_id"])
                }
            ),
            "documents": documents,
            "dispatch_source": dispatch_source,
            "dispatch_records": dispatch_records,
            "exclusions": list(self.exclusions),
            "warnings": [*self.provider_snapshot.warnings, *self.warnings],
            "coverage_limits": [
                "Only repositories explicitly supplied to this inventory run are inspected.",
                "Git records represent the observed working tree, not historical committed blob contents.",
                "Committed history not exposed by the allowlisted document provider is not inventoried.",
                "Only document types accepted by the existing DocumentLibrary provider are included.",
                "The generated inventory JSON is outside the provider's allowed document roots/types and is not part of its own snapshot.",
            ],
        }

    @property
    def documents(self) -> tuple[DocumentRecord, ...]:
        return self.provider_snapshot.records


@dataclass(frozen=True)
class RegisteredRepositoryInventory:
    authorized_repository_keys: tuple[str, ...]
    inventories: tuple[RepositoryInventory, ...]
    exclusions: tuple[dict[str, str], ...] = ()

    def report(self) -> dict[str, Any]:
        repositories = [inventory.report() for inventory in self.inventories]
        category_counts: dict[str, int] = {}
        source_owner_counts = {"git_owned": 0, "central_operational": 0}
        sidecar_counts = {"total": 0, "with_events": 0, "without_events": 0, "unparsed_or_unknown": 0}
        for repository in repositories:
            for category, count in repository["category_counts"].items():
                category_counts[category] = category_counts.get(category, 0) + count
            for owner, count in repository["source_owner_counts"].items():
                source_owner_counts[owner] = source_owner_counts.get(owner, 0) + count
            for key in sidecar_counts:
                sidecar_counts[key] += repository["workflow_sidecar_counts"][key]
        return {
            "schema_version": 1,
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "authorized_repository_keys": list(self.authorized_repository_keys),
            "accessible_repository_count": len(repositories),
            "excluded_repository_count": len(self.exclusions),
            "repository_count": len(repositories) + len(self.exclusions),
            "document_count": sum(item["document_count"] for item in repositories),
            "document_bytes": sum(item["document_bytes"] for item in repositories),
            "provider_bytes_read": sum(item["provider_bytes_read"] for item in repositories),
            "category_counts": dict(sorted(category_counts.items())),
            "source_owner_counts": dict(sorted(source_owner_counts.items())),
            "workflow_sidecar_counts": sidecar_counts,
            "dispatch_record_count": sum(len(item["dispatch_records"]) for item in repositories),
            "source_inventory_sha256": _sha256(
                _canonical_json(
                    [
                        {
                            "repository_id": item["repository_id"],
                            "source_inventory_sha256": item["source_inventory_sha256"],
                        }
                        for item in repositories
                    ]
                )
            ),
            "repositories": repositories,
            "exclusions": list(self.exclusions),
            "coverage_limits": [
                "Only the explicitly authorized keys from the server-validated repository registry are inspected.",
                "Git records represent observed working-tree bytes, not historical committed blob contents.",
                "Committed history not exposed by the allowlisted document provider remains NOT VERIFIED.",
                "A repository excluded for a read or validation failure is NOT VERIFIED, not an empty corpus.",
            ],
        }


def _source_class(record: DocumentRecord) -> str:
    path = PurePosixPath(record.path)
    if _is_v1_workflow_record(record):
        return "v1_workflow_sidecar"
    if record.authority_classification == "current_governing_document":
        return "current_governing_document"
    if path.parts[:2] == ("docs", "history"):
        return "allowlisted_historical_document"
    if record.logical_type == "PROMPT":
        return "canonical_milestone_prompt"
    if record.logical_type == "CLOSEOUT":
        return "canonical_milestone_closeout"
    return "allowlisted_milestone_document"


def _inventory_document(record: DocumentRecord, repository_id: str) -> dict[str, Any]:
    return {
        "repository_id": record.repository_id,
        "repository_key": record.repository_key,
        "source_path": record.path,
        "source_sha256": record.content_sha256,
        "size_bytes": len(record.content.encode("utf-8")),
        "milestone_id": record.milestone_id,
        "document_class": record.logical_type,
        "source_class": _source_class(record),
        "source_owner": "git_owned",
        "v1_lifecycle_events": record.version_provenance.get("v1_lifecycle_events"),
        "v1_event_count": record.version_provenance.get("v1_event_count"),
        "lifecycle_projection": record.version_provenance.get("lifecycle_projection"),
        "source_identity": record.source_identity,
        "document_id": record.document_id,
        "revision_id": record.revision_id,
        "authority_classification": record.authority_classification,
        "revision_status": record.revision_status,
        "approval_state": record.approval_state,
        "version_kind": record.version_kind,
        "source_provenance": {
            **record.version_provenance,
            "commit_membership": "not established",
            "repository_identity_verified_by": repository_id,
        },
    }


def _workflow_details(
    record: DocumentRecord,
    repository: RepositoryEntry,
    *,
    prompt_record: DocumentRecord | None = None,
) -> dict[str, Any] | None:
    if not _is_v1_workflow_record(record):
        return None
    if record.milestone_id is None:
        return {"error": "Milestone identity is unavailable; lifecycle events were not inferred."}
    workflow_name = PurePosixPath(record.path).name
    prompt_name = workflow_name.removesuffix("_workflow.md") + "_prompt.md"
    identity = StoreIdentity(
        repository_id=repository.repository_id,
        repository_key=repository.key,
        repository_root=str(repository.path),
        milestone_id=record.milestone_id,
        prompt_path=(PurePosixPath(record.path).parent / prompt_name).as_posix(),
        selection_id="inventory-read-only",
        selection_revision=0,
    )
    reconciliation_receipt = None
    historical_path = historical_prompt_path_alias(identity)
    if prompt_record is not None and historical_path is not None:
        reconciliation_receipt = build_historical_path_reconciliation_receipt(
            identity,
            original_declared_prompt_path=historical_path,
            workflow_path=record.path,
            prompt_path=prompt_record.path,
            prompt_bytes=prompt_record.content.encode("utf-8"),
            workflow_bytes=record.content.encode("utf-8"),
        )
    try:
        events = _parse_sidecar_bytes(
            record.content.encode("utf-8"),
            identity,
            reconciliation_receipt=reconciliation_receipt,
            prompt_path=prompt_record.path if prompt_record is not None else None,
            prompt_bytes=prompt_record.content.encode("utf-8") if prompt_record is not None else None,
            workflow_path=record.path if reconciliation_receipt is not None else None,
        )
        projection = _reduce_events(events)
    except (ImportSourceError, WorkflowRecordError) as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    details = {
        "v1_lifecycle_events": bool(events),
        "v1_event_count": len(events),
        "lifecycle_projection": {
            "phase": projection["phase"],
            "waiting_category": projection["waiting_category"],
            "event_count": len(events),
        },
    }
    if reconciliation_receipt is not None:
        details["historical_reconciliation"] = reconciliation_receipt
    return details


def inventory_selected_repository(
    repository: RepositoryEntry,
    *,
    current_milestone_id: str | None,
    dispatch_state_root: str | Path,
) -> RepositoryInventory:
    """Capture only the caller-selected registered repository and explicit dispatch source."""
    root = repository.path.resolve(strict=True)
    if root != repository.path or repository.repository_id != make_repository_id(root):
        raise MigrationRehearsalError("Selected repository identity is unresolved or does not match its registry root.")
    dispatch_snapshot = read_owner_dispatch_ledger_snapshot(
        root,
        dispatch_state_root,
        repository.repository_id,
    )
    if dispatch_snapshot.get("repository_id") != repository.repository_id:
        raise MigrationRehearsalError("Dispatch ledger snapshot belongs to a different repository.")

    provider = GitFileProvider(
        [GitRepositoryContext(repository, current_milestone_id=current_milestone_id)]
    )
    snapshot = provider.enumerate_records()
    warnings: list[str] = []
    records: list[DocumentRecord] = []
    prompt_records_by_milestone: dict[str, list[DocumentRecord]] = {}
    for candidate in snapshot.records:
        if (
            candidate.milestone_id is not None
            and _source_class(candidate) == "canonical_milestone_prompt"
        ):
            prompt_records_by_milestone.setdefault(candidate.milestone_id, []).append(candidate)
    for record in snapshot.records:
        prompt_candidates = prompt_records_by_milestone.get(record.milestone_id or "", [])
        prompt_record = prompt_candidates[0] if len(prompt_candidates) == 1 else None
        details = _workflow_details(record, repository, prompt_record=prompt_record)
        if details is None:
            records.append(record)
            continue
        if "error" in details:
            warnings.append(f"Workflow sidecar {record.path!r} could not be reduced: {details['error']}")
            records.append(record)
            continue
        provenance = dict(record.version_provenance)
        lifecycle_provenance: dict[str, Any] = {
            "v1_lifecycle_events": details["v1_lifecycle_events"],
            "v1_event_count": details["v1_event_count"],
            "lifecycle_projection": details["lifecycle_projection"],
        }
        if "historical_reconciliation" in details:
            lifecycle_provenance["historical_reconciliation"] = details[
                "historical_reconciliation"
            ]
        records.append(
            replace(
                record,
                version_provenance={**provenance, **lifecycle_provenance},
            )
        )
    snapshot = ProviderSnapshot(
        provider_id=snapshot.provider_id,
        records=tuple(records),
        fingerprint=snapshot.fingerprint,
        bytes_scanned=snapshot.bytes_scanned,
        warnings=snapshot.warnings,
    )
    exclusions = (
        {
            "source": "Git history beyond the current working tree",
            "reason": "The selected provider exposes allowlisted working-tree snapshots, not a complete historical Git object inventory.",
        },
    )
    return RepositoryInventory(
        repository=repository,
        current_milestone_id=current_milestone_id,
        provider_snapshot=snapshot,
        dispatch_snapshot=dispatch_snapshot,
        exclusions=exclusions,
        warnings=tuple(warnings),
    )


def inventory_registered_repositories(
    registry: RepositoryRegistry,
    *,
    authorized_repository_keys: Sequence[str],
    current_milestone_ids: Mapping[str, str | None] | None,
    dispatch_state_root: str | Path,
) -> RegisteredRepositoryInventory:
    """Inventory an explicit set of already-registered roots without changing selection."""
    keys = tuple(authorized_repository_keys)
    if not keys or len(set(keys)) != len(keys):
        raise MigrationRehearsalError("Authorized repository keys must be a non-empty unique sequence.")
    configured = {entry.key: entry for entry in registry.entries}
    unknown = sorted(set(keys) - configured.keys())
    if unknown:
        raise MigrationRehearsalError(
            f"Repository keys are not in the validated registry: {', '.join(unknown)}."
        )
    milestones = dict(current_milestone_ids or {})
    if set(milestones) - set(keys):
        raise MigrationRehearsalError("Current milestone bindings may only name explicitly authorized repositories.")
    inventories: list[RepositoryInventory] = []
    exclusions: list[dict[str, str]] = []
    for key in keys:
        repository = configured[key]
        try:
            inventory = inventory_selected_repository(
                repository,
                current_milestone_id=milestones.get(key),
                dispatch_state_root=dispatch_state_root,
            )
        except (OSError, ArchitectDispatchError, MigrationRehearsalError, SourceEnumerationError) as exc:
            exclusions.append(
                {
                    "repository_key": key,
                    "repository_id": repository.repository_id,
                    "repository_root": str(repository.path),
                    "status": "NOT_VERIFIED",
                    "reason": f"{type(exc).__name__}: {exc}",
                }
            )
            continue
        inventories.append(inventory)
    return RegisteredRepositoryInventory(
        authorized_repository_keys=keys,
        inventories=tuple(inventories),
        exclusions=tuple(exclusions),
    )


def _find_one(
    records: tuple[DocumentRecord, ...],
    milestone_id: str,
    logical_type: str,
    milestone_root: str,
) -> DocumentRecord | None:
    root = milestone_root.rstrip("/") + "/"
    matches = [
        item
        for item in records
        if item.milestone_id == milestone_id
        and item.logical_type == logical_type
        and item.path.startswith(root)
        and (logical_type != "WORKFLOW" or _is_v1_workflow_record(item))
    ]
    if len(matches) > 1:
        raise MigrationRehearsalError(
            f"Milestone {milestone_id!r} has ambiguous {logical_type} source documents."
        )
    return matches[0] if matches else None


def _validate_rehearsal_root(
    value: str | Path, repository_root: Path, *, require_temporary: bool = True,
) -> Path:
    candidate = Path(value).expanduser()
    if candidate.is_symlink() or not candidate.is_dir():
        raise MigrationRehearsalError("Rehearsal destination must be an existing real directory.")
    root = candidate.resolve(strict=True)
    temp_root = Path(tempfile.gettempdir()).resolve(strict=True)
    if (
        (require_temporary and not root.is_relative_to(temp_root))
        or root == repository_root
        or root.is_relative_to(repository_root)
        or repository_root.is_relative_to(root)
    ):
        if require_temporary:
            raise MigrationRehearsalError("Rehearsal databases must remain in a separate disposable temp directory.")
        raise MigrationRehearsalError("Prepared databases must remain separate from every repository.")
    return root


@dataclass
class _CentralRehearsal:
    path: Path
    managed_prepared: bool = False
    store: InformationStore | None = None
    selection_revisions: dict[str, int] = field(default_factory=dict)

    def bind(self, identity: StoreIdentity) -> InformationStore:
        revision = self.selection_revisions.get(identity.repository_id, 0) + 1
        identity = replace(identity, selection_revision=revision)
        if self.store is None:
            if self.managed_prepared:
                store = InformationStore.initialize_managed_prepared(self.path, identity)
            else:
                store = InformationStore.initialize_disposable(
                    self.path,
                    identity,
                    allow_unsupported_runtime_for_fixture=True,
                )
        else:
            store = (
                self.store.register_import_scope(identity) if self.managed_prepared
                else self.store.register_disposable_scope(identity)
            )
        self.store = store
        self.selection_revisions[identity.repository_id] = revision
        return store


def rehearse_milestone_imports(
    inventory: RepositoryInventory,
    destination_root: str | Path,
    *,
    _central_rehearsal: _CentralRehearsal | None = None,
) -> dict[str, Any]:
    """Import canonical milestone source copies into one disposable store per milestone."""
    root = _validate_rehearsal_root(
        destination_root,
        inventory.repository.path,
        require_temporary=not (_central_rehearsal and _central_rehearsal.managed_prepared),
    )
    records = inventory.documents
    prompt_records = [
        item
        for item in records
        if item.logical_type == "PROMPT"
        and item.milestone_id is not None
        and _CANONICAL_MILESTONE_ID.fullmatch(item.milestone_id)
        and item.path.startswith(inventory.repository.milestone_root.rstrip("/") + "/")
    ]
    prompt_records.sort(key=lambda item: (item.milestone_id or "", item.path))
    exclusions: list[dict[str, str]] = list(inventory.exclusions)
    imported: list[dict[str, Any]] = []
    mismatches: list[dict[str, str]] = []
    source_owner_counts = {"git_owned": 0, "central_operational": 0}
    for record in records:
        if record.logical_type == "PROMPT":
            if (
                record.milestone_id is None
                or not _CANONICAL_MILESTONE_ID.fullmatch(record.milestone_id)
                or not record.path.startswith(inventory.repository.milestone_root.rstrip("/") + "/")
            ):
                exclusions.append(
                    {
                        "source": record.path,
                        "reason": "Legacy identifiers and historical prompt locations remain Git-owned evidence and are not coerced into current InformationStore authority.",
                    }
                )
            continue
        if record.logical_type == "CLOSEOUT" or _is_v1_workflow_record(record):
            continue
        exclusions.append(
            {
                "source": record.path,
                "reason": "Current governing and historical context remain Git-owned; they are inventoried and retrieved through DocumentLibrary, not migrated into workflow authority.",
            }
        )

    seen_milestones: set[str] = set()
    for prompt in prompt_records:
        milestone_id = prompt.milestone_id
        assert milestone_id is not None
        if milestone_id in seen_milestones:
            raise MigrationRehearsalError(f"Duplicate canonical prompt for milestone {milestone_id!r}.")
        seen_milestones.add(milestone_id)
        closeout = _find_one(
            records,
            milestone_id,
            "CLOSEOUT",
            inventory.repository.milestone_root,
        )
        workflow = _find_one(
            records,
            milestone_id,
            "WORKFLOW",
            inventory.repository.milestone_root,
        )
        identity = StoreIdentity(
            repository_id=inventory.repository.repository_id,
            repository_key=inventory.repository.key,
            repository_root=str(inventory.repository.path),
            milestone_id=milestone_id,
            prompt_path=prompt.path,
            selection_id=f"rehearsal:{_sha256(milestone_id.encode())[:24]}",
            selection_revision=0,
        )
        if _central_rehearsal is None:
            database_path = root / f"milestone-{_sha256(milestone_id.encode())[:24]}.sqlite3"
            if database_path.exists() or database_path.is_symlink():
                raise MigrationRehearsalError("Refusing to overwrite an existing rehearsal database.")
            store = InformationStore.initialize_disposable(
                database_path,
                identity,
                allow_unsupported_runtime_for_fixture=True,
            )
        else:
            store = _central_rehearsal.bind(identity)
            identity = store.identity
            database_path = store.path
        dispatch_snapshot = inventory.dispatch_snapshot
        dispatch_bytes = (
            dispatch_snapshot.get("source_bytes")
            if dispatch_snapshot is not None and dispatch_snapshot.get("status") == "present"
            else None
        )
        historical_path = historical_prompt_path_alias(identity)
        reconciliation_receipt = None
        if workflow is not None and historical_path is not None:
            reconciliation_receipt = build_historical_path_reconciliation_receipt(
                identity,
                original_declared_prompt_path=historical_path,
                workflow_path=workflow.path,
                prompt_path=prompt.path,
                prompt_bytes=prompt.content.encode("utf-8"),
                workflow_bytes=workflow.content.encode("utf-8"),
            )
        closeout_reconciliation_receipt = None
        if (
            closeout is not None
            and workflow is not None
            and reconciliation_receipt is not None
        ):
            closeout_reconciliation_receipt = build_historical_closeout_path_reconciliation_receipt(
                identity,
                prompt_reconciliation_receipt=reconciliation_receipt,
                prompt_path=prompt.path,
                prompt_bytes=prompt.content.encode("utf-8"),
                workflow_path=workflow.path,
                workflow_bytes=workflow.content.encode("utf-8"),
                closeout_path=closeout.path,
                closeout_bytes=closeout.content.encode("utf-8"),
                canonical_target_match_count=1,
            )
        bundle = V1FixtureBundle(
            prompt_path=prompt.path,
            closeout_path=closeout.path if closeout is not None else None,
            workflow_path=workflow.path if workflow is not None else None,
            prompt_bytes=prompt.content.encode("utf-8"),
            closeout_bytes=closeout.content.encode("utf-8") if closeout is not None else None,
            workflow_bytes=workflow.content.encode("utf-8") if workflow is not None else None,
            dispatch_path=(
                dispatch_snapshot["source_path"]
                if dispatch_bytes is not None and dispatch_snapshot is not None
                else None
            ),
            dispatch_bytes=dispatch_bytes,
            reconciliation_receipt=reconciliation_receipt,
            closeout_reconciliation_receipt=closeout_reconciliation_receipt,
        )
        try:
            first = store.import_v1_fixture(bundle)
        except ImportSourceError as exc:
            mismatches.append(
                {
                    "milestone_id": milestone_id,
                    "source_owner": "git_owned + central_operational",
                    "reason": f"{type(exc).__name__}: {exc}",
                }
            )
            exclusions.append(
                {
                    "source": milestone_id,
                    "reason": "Import was refused because exact source validation failed; no authority or document links were synthesized.",
                }
            )
            continue
        retry = store.import_v1_fixture(bundle)
        if first != retry:
            raise MigrationRehearsalError(f"Exact import retry was not idempotent for {milestone_id}.")
        source_rows = store.list_import_sources()
        expected_sources = {
            (item["source_kind"], item["source_path"]): item
            for item in first["source_manifest"]
        }
        if len(source_rows) != len(expected_sources):
            raise MigrationRehearsalError(f"Imported source count differs for milestone {milestone_id}.")
        for source in source_rows:
            key = (source["source_kind"], source["source_path"])
            expected = expected_sources.get(key)
            if (
                expected is None
                or source["source_sha256"] != expected["source_sha256"]
                or _sha256(store.read_import_source(source["source_id"]))
                != expected["source_sha256"]
            ):
                raise MigrationRehearsalError(f"Imported source bytes differ for {milestone_id}: {key}.")
        projection = store.snapshot()["projection"]
        imported_events = store.snapshot()["events"]
        reduced = _reduce_events(imported_events)
        if (
            any(projection.get(key) != value for key, value in reduced.items())
            or projection.get("event_count") != len(imported_events)
        ):
            raise MigrationRehearsalError(f"Imported lifecycle projection differs for {milestone_id}.")
        if workflow is not None:
            expected_events = _parse_sidecar_bytes(
                workflow.content.encode("utf-8"),
                identity,
                reconciliation_receipt=reconciliation_receipt,
                prompt_path=prompt.path,
                prompt_bytes=prompt.content.encode("utf-8"),
                workflow_path=workflow.path,
            )
            if imported_events != expected_events:
                raise MigrationRehearsalError(f"Imported workflow chronology differs for {milestone_id}.")
        elif imported_events:
            raise MigrationRehearsalError(f"Workflow events were invented for {milestone_id}.")
        prompt_revision = store.get_revision(first["prompt_document_id"], first["prompt_revision_id"])
        if (
            prompt_revision["source_path"] != prompt.path
            or prompt_revision["content"] != prompt.content
            or prompt_revision["content_sha256"] != prompt.content_sha256
        ):
            raise MigrationRehearsalError(f"Imported prompt revision differs for {milestone_id}.")
        if closeout is not None:
            closeout_revision = store.get_revision(
                first["closeout_document_id"],
                first["closeout_revision_id"],
            )
            if (
                closeout_revision["source_path"] != closeout.path
                or closeout_revision["content"] != closeout.content
                or closeout_revision["content_sha256"] != closeout.content_sha256
            ):
                raise MigrationRehearsalError(f"Imported closeout revision differs for {milestone_id}.")
        imported_source_paths = {prompt.path}
        if closeout is not None:
            imported_source_paths.add(closeout.path)
        if workflow is not None:
            imported_source_paths.add(workflow.path)
        unresolved_document_references: list[dict[str, Any]] = []
        qualified_reconciled_closeout_references: list[dict[str, Any]] = []
        for event in imported_events:
            evidence_path = event.get("evidence_path")
            if not isinstance(evidence_path, str) or evidence_path in imported_source_paths:
                continue
            if (
                closeout_reconciliation_receipt is not None
                and event["event_type"] == "closeout_submitted"
                and evidence_path
                == closeout_reconciliation_receipt["original_declared_closeout_path"]
            ):
                qualified_reconciled_closeout_references.append(
                    {
                        "event_id": event["event_id"],
                        "event_type": event["event_type"],
                        "original_evidence_path": evidence_path,
                        "mapped_target_closeout_path": closeout_reconciliation_receipt[
                            "mapped_target_closeout_path"
                        ],
                        "original_content_identity": "UNKNOWN/NOT VERIFIED",
                    }
                )
                continue
            unresolved_document_references.append(
                {
                    "event_id": event["event_id"],
                    "event_type": event["event_type"],
                    "evidence_path": evidence_path,
                }
            )
        dispatch_audit: list[dict[str, Any]] = []
        if dispatch_snapshot is not None and dispatch_snapshot.get("status") == "present":
            matching_dispatches = [
                item
                for item in dispatch_snapshot["ledger"]["dispatches"]
                if item["milestone_id"] == milestone_id
            ]
            for source_dispatch in matching_dispatches:
                imported_dispatch = store.get_dispatch(source_dispatch["dispatch_id"])
                history = store.list_dispatch_history(source_dispatch["dispatch_id"])
                if (
                    imported_dispatch["status"] != source_dispatch["status"]
                    or imported_dispatch["body"] != source_dispatch["body"]
                    or imported_dispatch["authorization_granted"] is not False
                    or len(history) != 1
                    or history[0]["record_kind"] != "LEGACY_SNAPSHOT"
                    or history[0]["po_confirmed"] is not None
                ):
                    raise MigrationRehearsalError(
                        f"Imported dispatch provenance differs for {milestone_id}: {source_dispatch['dispatch_id']}."
                    )
                dispatch_audit.append(
                    {
                        "dispatch_id": source_dispatch["dispatch_id"],
                        "status_snapshot": imported_dispatch["status"],
                        "authorization_granted": False,
                        "decision_history": "unavailable in v1 source",
                        "po_confirmation": "unknown",
                    }
                )
        if closeout is None:
            exclusions.append(
                {
                    "source": f"{milestone_id}:closeout",
                    "reason": "No closeout source file exists; none was synthesized.",
                }
            )
        if workflow is None:
            exclusions.append(
                {
                    "source": f"{milestone_id}:workflow",
                    "reason": "No v1 workflow sidecar exists; no lifecycle events or approval evidence were invented.",
                }
            )
        for item in first["source_manifest"]:
            source_owner_counts[item["source_owner"]] = source_owner_counts.get(item["source_owner"], 0) + 1
        imported.append(
            {
                "repository_id": identity.repository_id,
                "milestone_id": milestone_id,
                "prompt_path": prompt.path,
                "database_path": str(database_path),
                "import_receipt_sha256": first["import_receipt_sha256"],
                "source_count": len(first["source_manifest"]),
                "event_count": len(imported_events),
                "event_ids_in_order": [event["event_id"] for event in imported_events],
                "event_types_in_order": [event["event_type"] for event in imported_events],
                "event_json_sha256": _sha256(_canonical_json(imported_events)),
                "event_type_counts": {
                    event_type: sum(item["event_type"] == event_type for item in imported_events)
                    for event_type in sorted({item["event_type"] for item in imported_events})
                },
                "unresolved_document_references": unresolved_document_references,
                "qualified_reconciled_closeout_references": (
                    qualified_reconciled_closeout_references
                ),
                "historical_closeout_path_reconciliation": (
                    closeout_reconciliation_receipt
                ),
                "unverified_historical_origin_count": int(
                    closeout_reconciliation_receipt is not None
                ),
                "projection": projection,
                "linked_event_count": first["linked_event_count"],
                "approved_prompt_revision_id": first["approved_prompt_revision_id"],
                "approval_events_are_reported_source_claims": True,
                "dispatch_count": first["dispatch_count"],
                "dispatch_audit": dispatch_audit,
                "exact_retry": "PASS",
                "source_parity": "PASS",
                "workflow_parity": "PASS",
                "source_manifest": first["source_manifest"],
            }
        )

    dispatch_snapshot_audit: dict[str, Any] | None = None
    dispatch_snapshot = inventory.dispatch_snapshot
    if dispatch_snapshot is not None and dispatch_snapshot.get("status") == "present":
        if _central_rehearsal is not None:
            if (
                _central_rehearsal.store is None
                or _central_rehearsal.store.identity.repository_id != inventory.repository.repository_id
            ):
                mismatches.append({
                    "milestone_id": "(repository dispatch snapshot)",
                    "source_owner": "central_operational",
                    "reason": "No canonical milestone scope exists to bind the repository dispatch snapshot.",
                })
            else:
                try:
                    dispatch_snapshot_audit = _central_rehearsal.store.reconcile_disposable_dispatch_snapshot(
                        source_path=dispatch_snapshot["source_path"],
                        source_bytes=dispatch_snapshot["source_bytes"],
                    )
                    retry = _central_rehearsal.store.reconcile_disposable_dispatch_snapshot(
                        source_path=dispatch_snapshot["source_path"],
                        source_bytes=dispatch_snapshot["source_bytes"],
                    )
                    if retry != dispatch_snapshot_audit:
                        raise MigrationRehearsalError("Repository dispatch snapshot retry was not idempotent.")
                    dispatch_snapshot_audit["exact_retry"] = "PASS"
                except ImportSourceError as exc:
                    mismatches.append({
                        "milestone_id": "(repository dispatch snapshot)",
                        "source_owner": "central_operational",
                        "reason": f"{type(exc).__name__}: {exc}",
                    })
        else:
            imported_dispatch_ids = {
                item["dispatch_id"] for milestone in imported for item in milestone["dispatch_audit"]
            }
            for record in dispatch_snapshot["ledger"]["dispatches"]:
                if record["dispatch_id"] not in imported_dispatch_ids:
                    exclusions.append({
                        "source": f"dispatch:{record['dispatch_id']}",
                        "reason": "No imported milestone scope matches this dispatch in the per-milestone rehearsal.",
                    })

    imported_ids = {item["milestone_id"] for item in imported}
    for record in records:
        if (
            record.logical_type == "CLOSEOUT" or _is_v1_workflow_record(record)
        ) and (
            record.milestone_id not in imported_ids
            or not record.path.startswith(inventory.repository.milestone_root.rstrip("/") + "/")
        ):
            exclusions.append(
                {
                    "source": record.path,
                    "reason": "Historical or orphaned source remains Git-owned; it has no unique canonical prompt identity for an InformationStore import.",
                }
            )
    return {
        "schema_version": 1,
        "repository_id": inventory.repository.repository_id,
        "repository_key": inventory.repository.key,
        "inventory_sha256": inventory.report()["source_inventory_sha256"],
        "imported_milestone_count": len(imported),
        "imported_source_count": sum(item["source_count"] for item in imported),
        "imported_event_count": sum(item["event_count"] for item in imported),
        "import_refusal_count": len(mismatches),
        "qualified_closeout_reconciliation_count": sum(
            item["historical_closeout_path_reconciliation"] is not None
            for item in imported
        ),
        "unverified_historical_origin_count": sum(
            item["unverified_historical_origin_count"] for item in imported
        ),
        "qualified_reconciled_closeout_event_reference_count": sum(
            len(item["qualified_reconciled_closeout_references"]) for item in imported
        ),
        "dispatch_snapshot_audit": dispatch_snapshot_audit,
        "source_owner_counts": dict(sorted(source_owner_counts.items())),
        "mismatch_count": len(mismatches),
        "mismatches": mismatches,
        "imports": imported,
        "exclusions": exclusions,
        "source_owner_policy": {
            "git_owned": "Prompt, closeout, and workflow snapshots are exact v1 source copies; Git remains their operational owner.",
            "central_operational": "External dispatch ledger bytes are copied from the read-only validated snapshot; imported decisions do not grant authorization.",
        },
        "authority_changed": False,
    }


def rehearse_registered_repository_imports(
    inventory: RegisteredRepositoryInventory,
    destination_root: str | Path,
    *,
    central_database: bool = False,
    _central_rehearsal: _CentralRehearsal | None = None,
) -> dict[str, Any]:
    """Rehearse authorized repositories in disposable per-milestone or central databases."""
    if type(central_database) is not bool:
        raise ValueError("central_database must be a boolean.")
    if not inventory.inventories:
        raise MigrationRehearsalError("No accessible authorized repository inventory is available to import.")
    repositories = [item.repository for item in inventory.inventories]
    first_root = repositories[0].path
    managed_prepared = _central_rehearsal is not None and _central_rehearsal.managed_prepared
    root = _validate_rehearsal_root(destination_root, first_root, require_temporary=not managed_prepared)
    for repository in repositories[1:]:
        if (
            root == repository.path
            or root.is_relative_to(repository.path)
            or repository.path.is_relative_to(root)
        ):
            raise MigrationRehearsalError("Rehearsal databases must remain outside every registered repository.")
    central = _central_rehearsal
    if central_database and central is None:
        central_path = root / "central-rehearsal.sqlite3"
        if central_path.exists() or central_path.is_symlink():
            raise MigrationRehearsalError("Refusing to overwrite an existing central rehearsal database.")
        central = _CentralRehearsal(central_path)
    reports: list[dict[str, Any]] = []
    for repository_inventory in inventory.inventories:
        repository = repository_inventory.repository
        repository_destination = root / _sha256(repository.repository_id.encode("utf-8"))[:24]
        repository_destination.mkdir(mode=0o700, exist_ok=False)
        reports.append(
            {
                **repository_inventory.report(),
                "rehearsal": rehearse_milestone_imports(
                    repository_inventory,
                    repository_destination,
                    _central_rehearsal=central,
                ),
            }
        )
    return {
        "schema_version": 1,
        "authorized_repository_keys": list(inventory.authorized_repository_keys),
        "accessible_repository_count": len(inventory.inventories),
        "excluded_repository_count": len(inventory.exclusions),
        "repositories": reports,
        "exclusions": list(inventory.exclusions),
        "document_count": sum(item["document_count"] for item in reports),
        "document_bytes": sum(item["document_bytes"] for item in reports),
        "imported_milestone_count": sum(
            item["rehearsal"]["imported_milestone_count"] for item in reports
        ),
        "imported_source_count": sum(
            item["rehearsal"]["imported_source_count"] for item in reports
        ),
        "imported_event_count": sum(
            item["rehearsal"]["imported_event_count"] for item in reports
        ),
        "dispatch_record_count": sum(len(item["dispatch_records"]) for item in reports),
        "central_imported_dispatch_count": (
            sum(
                (item["rehearsal"]["dispatch_snapshot_audit"] or {}).get("source_count", 0)
                for item in reports
            ) if central_database else None
        ),
        "mismatch_count": sum(item["rehearsal"]["mismatch_count"] for item in reports),
        "import_refusal_count": sum(
            item["rehearsal"]["import_refusal_count"] for item in reports
        ),
        "qualified_closeout_reconciliation_count": sum(
            item["rehearsal"]["qualified_closeout_reconciliation_count"]
            for item in reports
        ),
        "unverified_historical_origin_count": sum(
            item["rehearsal"]["unverified_historical_origin_count"]
            for item in reports
        ),
        "qualified_reconciled_closeout_event_reference_count": sum(
            item["rehearsal"]["qualified_reconciled_closeout_event_reference_count"]
            for item in reports
        ),
        "authority_changed": False,
        "storage_layout": (
            "single_managed_prepared_database" if managed_prepared
            else "single_disposable_database" if central_database else "per_milestone_databases"
        ),
        "disposable_databases_created_under_temporary_root": not managed_prepared,
    }


def prepare_managed_repository_imports(
    registry: RepositoryRegistry,
    destination_root: str | Path,
    *,
    state_root: str | Path,
    writers_quiesced: bool,
) -> dict[str, Any]:
    if writers_quiesced is not True:
        raise MigrationRehearsalError("Managed import requires explicitly quiesced v1 writers.")
    if not registry.entries:
        raise MigrationRehearsalError("Managed import requires at least one registered repository.")
    root = _validate_selection_root(state_root, str(registry.entries[0].path), fixture=False)
    destination = _validate_rehearsal_root(destination_root, registry.entries[0].path, require_temporary=False)
    database_path = destination / "managed-prepared.sqlite3"
    if database_path.exists() or database_path.is_symlink():
        raise MigrationRehearsalError("Refusing to overwrite an existing prepared database.")
    keys = tuple(entry.key for entry in registry.entries)
    with state_artifact_lock(root):
        if load_store_authority(root) is not None:
            raise MigrationRehearsalError("Managed import is offline-only while the legacy authority is selected.")
        pointers: dict[str, tuple[dict[str, Any], str]] = {}
        for entry in registry.entries:
            _validate_selection_root(root, str(entry.path), fixture=False)
            _validate_rehearsal_root(destination, entry.path, require_temporary=False)
            pointer, digest = _read_fixture_selection_snapshot(root, entry.repository_id)
            if (
                set(pointer) != {"schema_version", "repository_id", "milestone_id", "prompt_path"}
                or pointer["repository_id"] != entry.repository_id
                or not isinstance(pointer["milestone_id"], str)
                or not isinstance(pointer["prompt_path"], str)
            ):
                raise MigrationRehearsalError("Every registered repository requires an exact unbound v1 selection.")
            pointers[entry.key] = (pointer, digest)
        current_milestones = {key: pointer["milestone_id"] for key, (pointer, _) in pointers.items()}
        inventory = inventory_registered_repositories(
            registry,
            authorized_repository_keys=keys,
            current_milestone_ids=current_milestones,
            dispatch_state_root=root,
        )
        if inventory.exclusions or len(inventory.inventories) != len(registry.entries):
            raise MigrationRehearsalError(f"Registered source inventory is incomplete: {inventory.exclusions}")
        source_hash = inventory.report()["source_inventory_sha256"]
        central = _CentralRehearsal(database_path, managed_prepared=True)
        report = rehearse_registered_repository_imports(
            inventory, destination, central_database=True, _central_rehearsal=central,
        )
        if report["mismatch_count"] or report["import_refusal_count"] or central.store is None:
            raise MigrationRehearsalError(
                f"Prepared import has mismatches or refusals; inspect the quarantined database at {database_path}."
            )
        refreshed = inventory_registered_repositories(
            registry,
            authorized_repository_keys=keys,
            current_milestone_ids=current_milestones,
            dispatch_state_root=root,
        )
        if refreshed.exclusions or refreshed.report()["source_inventory_sha256"] != source_hash:
            raise MigrationRehearsalError("V1 source inventory changed during import; the database is not activatable.")
        bindings: list[dict[str, Any]] = []
        for item in inventory.inventories:
            entry = item.repository
            pointer, pointer_hash = pointers[entry.key]
            candidates = [
                record for record in item.documents
                if record.logical_type == "PROMPT"
                and record.milestone_id == pointer["milestone_id"]
                and record.path == pointer["prompt_path"]
            ]
            if len(candidates) != 1:
                raise MigrationRehearsalError(f"Selected prompt for {entry.key!r} is not uniquely imported.")
            revision = central.selection_revisions[entry.repository_id] + 1
            identity = StoreIdentity(
                repository_id=entry.repository_id,
                repository_key=entry.key,
                repository_root=str(entry.path),
                milestone_id=pointer["milestone_id"],
                prompt_path=pointer["prompt_path"],
                selection_id=uuid.uuid4().hex,
                selection_revision=revision,
            )
            central.store = central.store.register_import_scope(identity)
            central.selection_revisions[entry.repository_id] = revision
            current_pointer, current_hash = _read_fixture_selection_snapshot(root, entry.repository_id)
            if current_pointer != pointer or current_hash != pointer_hash:
                raise MigrationRehearsalError("A v1 selection changed during import; the database is not activatable.")
            bindings.append({**asdict(identity), "v1_selection_sha256": pointer_hash})
        report["source_inventory_sha256"] = source_hash
        report["prepared_database_path"] = str(database_path)
        report["prepared_selection_bindings"] = bindings
        report["external_selections_changed"] = False
        return report


def verify_prepared_source_parity(
    registry: RepositoryRegistry,
    state_root: str | Path,
    current_milestone_ids: Mapping[str, str],
    connection: sqlite3.Connection,
) -> dict[str, Any]:
    keys = tuple(entry.key for entry in registry.entries)
    inventory = inventory_registered_repositories(
        registry,
        authorized_repository_keys=keys,
        current_milestone_ids=current_milestone_ids,
        dispatch_state_root=state_root,
    )
    if inventory.exclusions or len(inventory.inventories) != len(registry.entries):
        raise MigrationRehearsalError(f"Registered source inventory is incomplete: {inventory.exclusions}")
    for table in ("document_revision_decision", "workflow_document_link", "document_publication"):
        if connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]:
            raise MigrationRehearsalError("Prepared import contains unverified decisions, links, or publications.")
    expected_sources: dict[tuple[str, str, str, str], bytes] = {}
    expected_receipts: dict[str, dict[str, Any]] = {}
    for item in inventory.inventories:
        entry = item.repository
        prompts = [
            record for record in item.documents
            if record.logical_type == "PROMPT"
            and record.milestone_id is not None
            and _CANONICAL_MILESTONE_ID.fullmatch(record.milestone_id)
            and record.path.startswith(entry.milestone_root.rstrip("/") + "/")
        ]
        seen: set[str] = set()
        for prompt in prompts:
            milestone_id = prompt.milestone_id
            assert milestone_id is not None
            if milestone_id in seen:
                raise MigrationRehearsalError(f"Duplicate canonical prompt for {entry.key}/{milestone_id}.")
            seen.add(milestone_id)
            workflow = _find_one(item.documents, milestone_id, "WORKFLOW", entry.milestone_root)
            closeout = _find_one(item.documents, milestone_id, "CLOSEOUT", entry.milestone_root)
            identity = StoreIdentity(
                repository_id=entry.repository_id, repository_key=entry.key, repository_root=str(entry.path),
                milestone_id=milestone_id, prompt_path=prompt.path, selection_id="parity", selection_revision=0,
            )
            for kind, record in (("prompt", prompt), ("workflow", workflow), ("closeout", closeout)):
                if record is not None:
                    expected_sources[(entry.repository_id, milestone_id, kind, record.path)] = record.content.encode("utf-8")
            dispatch = item.dispatch_snapshot
            if dispatch is not None and dispatch["status"] == "present":
                expected_sources[(entry.repository_id, milestone_id, "dispatch", dispatch["source_path"])] = (
                    dispatch["source_bytes"]
                )
            receipt = None
            alias = historical_prompt_path_alias(identity)
            if workflow is not None and alias is not None:
                receipt = build_historical_path_reconciliation_receipt(
                    identity,
                    original_declared_prompt_path=alias,
                    workflow_path=workflow.path,
                    prompt_path=prompt.path,
                    prompt_bytes=prompt.content.encode("utf-8"),
                    workflow_bytes=workflow.content.encode("utf-8"),
                )
            closeout_receipt = None
            if closeout is not None and workflow is not None and receipt is not None:
                closeout_receipt = build_historical_closeout_path_reconciliation_receipt(
                    identity,
                    prompt_reconciliation_receipt=receipt,
                    prompt_path=prompt.path,
                    prompt_bytes=prompt.content.encode("utf-8"),
                    workflow_path=workflow.path,
                    workflow_bytes=workflow.content.encode("utf-8"),
                    closeout_path=closeout.path,
                    closeout_bytes=closeout.content.encode("utf-8"),
                    canonical_target_match_count=1,
                )
            events = (
                _parse_sidecar_bytes(
                    workflow.content.encode("utf-8"), identity, reconciliation_receipt=receipt,
                    prompt_path=prompt.path, prompt_bytes=prompt.content.encode("utf-8"), workflow_path=workflow.path,
                )
                if workflow is not None else []
            )
            event_rows = connection.execute(
                """SELECT event_id, event_type, event_timestamp, request_id, payload_json, payload_sha256
                   FROM workflow_event
                   WHERE repository_id = ? AND milestone_id = ? ORDER BY sequence""",
                (entry.repository_id, milestone_id),
            ).fetchall()
            stored_events = [json.loads(row["payload_json"]) for row in event_rows]
            state = connection.execute(
                """SELECT store_revision, event_count, projection_json FROM workflow_state
                   WHERE repository_id = ? AND milestone_id = ?""",
                (entry.repository_id, milestone_id),
            ).fetchone()
            if (
                stored_events != events
                or any(_sha256(row["payload_json"].encode("utf-8")) != row["payload_sha256"] for row in event_rows)
                or any(
                    row["event_id"] != event["event_id"]
                    or row["event_type"] != event["event_type"]
                    or row["event_timestamp"] != event["timestamp"]
                    or row["request_id"] is not None
                    for row, event in zip(event_rows, events)
                )
                or state is None or state["event_count"] != len(events)
                or state["store_revision"] != len(events)
                or json.loads(state["projection_json"]) != _projection(events)
            ):
                raise MigrationRehearsalError(f"Workflow parity failed for {entry.key}/{milestone_id}.")
            milestone = connection.execute(
                "SELECT prompt_path, prompt_revision_id FROM milestone WHERE repository_id = ? AND milestone_id = ?",
                (entry.repository_id, milestone_id),
            ).fetchone()
            if milestone is None or milestone["prompt_path"] != prompt.path or milestone["prompt_revision_id"] is not None:
                raise MigrationRehearsalError(f"Prepared prompt approval is not unverified for {entry.key}/{milestone_id}.")
            expected_document_count = 1 + (closeout is not None)
            for table in ("document", "document_revision"):
                count = connection.execute(
                    f"SELECT count(*) FROM {table} WHERE repository_id = ? AND milestone_id = ?",
                    (entry.repository_id, milestone_id),
                ).fetchone()[0]
                if count != expected_document_count:
                    raise MigrationRehearsalError(f"Imported document set differs for {entry.key}/{milestone_id}.")
            for record in (prompt, closeout):
                if record is None:
                    continue
                revision = connection.execute(
                    """SELECT d.logical_key, d.approved_revision_id, r.content, r.content_sha256,
                              r.source_kind, r.parent_revision_id, r.provenance_json
                       FROM document d
                       JOIN document_revision r ON r.document_id = d.document_id
                         AND r.revision_id = d.draft_revision_id
                       WHERE d.repository_id = ? AND d.milestone_id = ? AND d.source_path = ?
                         AND d.logical_type = ?""",
                    (entry.repository_id, milestone_id, record.path, record.logical_type),
                ).fetchall()
                provenance: dict[str, Any] = {"source_path": record.path, "source_sha256": record.content_sha256}
                if record.logical_type == "CLOSEOUT" and closeout_receipt is not None:
                    provenance["historical_closeout_path_reconciliation"] = closeout_receipt
                if (
                    len(revision) != 1 or revision[0]["content"] != record.content
                    or revision[0]["content_sha256"] != record.content_sha256
                    or revision[0]["logical_key"] != record.path
                    or revision[0]["approved_revision_id"] is not None
                    or revision[0]["source_kind"] != "v1_import"
                    or revision[0]["parent_revision_id"] is not None
                    or json.loads(revision[0]["provenance_json"]) != provenance
                ):
                    raise MigrationRehearsalError(f"Document parity failed for {entry.key}/{record.path}.")
        ledger = item.dispatch_snapshot
        expected_dispatches = (
            {record["dispatch_id"]: record for record in ledger["ledger"]["dispatches"]}
            if ledger is not None and ledger["status"] == "present" else {}
        )
        if ledger is not None and ledger["status"] == "present":
            request = {
                "repository_id": entry.repository_id,
                "source_path": ledger["source_path"],
                "source_sha256": ledger["source_sha256"],
                "dispatch_ids": [record["dispatch_id"] for record in ledger["ledger"]["dispatches"]],
            }
            request_hash = _sha256(_canonical_json(request))
            expected_receipts[entry.repository_id] = {
                "request_id": f"dispatch-snapshot:{request_hash}",
                "request_hash": request_hash,
                "source_sha256": ledger["source_sha256"],
                "source_count": len(expected_dispatches),
            }
        stored_dispatches = {
            row["dispatch_id"]: row for row in connection.execute(
                """SELECT dispatch_id, milestone_id, dispatch_type, status, body, title, approved_milestone_id,
                          related_event_id, source_sha256, payload_json, created_at, updated_at
                   FROM architect_dispatch WHERE repository_id = ?""",
                (entry.repository_id,),
            )
        }
        if set(stored_dispatches) != set(expected_dispatches):
            raise MigrationRehearsalError(f"Dispatch parity failed for {entry.key}.")
        for dispatch_id, expected in expected_dispatches.items():
            row = stored_dispatches[dispatch_id]
            history = connection.execute(
                """SELECT sequence, record_kind, from_status, to_status, actor_role, po_confirmed,
                          related_event_id, recorded_at, source_kind, payload_json
                   FROM dispatch_history WHERE repository_id = ? AND dispatch_id = ? ORDER BY sequence""",
                (entry.repository_id, dispatch_id),
            ).fetchall()
            if (
                json.loads(row["payload_json"]) != expected
                or row["milestone_id"] != expected["milestone_id"]
                or row["dispatch_type"] != expected["type"]
                or row["status"] != expected["status"]
                or row["body"] != expected["body"]
                or row["title"] != expected.get("title")
                or row["approved_milestone_id"] != expected.get("approved_milestone_id")
                or row["related_event_id"] != expected.get("related_event_id")
                or row["source_sha256"] != ledger["source_sha256"]
                or row["created_at"] != expected["created_at"]
                or row["updated_at"] != expected["updated_at"]
                or len(history) != 1
                or history[0]["sequence"] != 1
                or history[0]["record_kind"] != "LEGACY_SNAPSHOT"
                or history[0]["from_status"] is not None
                or history[0]["to_status"] != expected["status"]
                or history[0]["actor_role"] != "repo_control"
                or history[0]["po_confirmed"] is not None
                or history[0]["related_event_id"] != expected.get("related_event_id")
                or history[0]["recorded_at"] != expected["updated_at"]
                or history[0]["source_kind"] != "v1_import"
                or json.loads(history[0]["payload_json"]) != expected
            ):
                raise MigrationRehearsalError(f"Dispatch provenance parity failed for {entry.key}/{dispatch_id}.")
    receipt_rows = connection.execute(
        """SELECT r.repository_id, r.request_id, r.request_hash, r.resulting_revision, r.result_json,
                  w.store_revision
           FROM transaction_receipt r
           JOIN workflow_state w ON w.repository_id = r.repository_id AND w.milestone_id = r.milestone_id"""
    ).fetchall()
    if len(receipt_rows) != len(expected_receipts):
        raise MigrationRehearsalError("Prepared import transaction receipts do not match the source inventory.")
    for row in receipt_rows:
        expected = expected_receipts.get(row["repository_id"])
        try:
            result = json.loads(row["result_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise MigrationRehearsalError("Prepared dispatch import receipt is malformed.") from exc
        if (
            expected is None
            or not isinstance(result, dict)
            or row["request_id"] != expected["request_id"]
            or row["request_hash"] != expected["request_hash"]
            or row["resulting_revision"] != row["store_revision"]
            or set(result) != {
                "repository_id", "source_sha256", "source_count", "imported_missing_count",
                "receipt_sha256", "decision_history",
            }
            or result["repository_id"] != row["repository_id"]
            or result["source_sha256"] != expected["source_sha256"]
            or result["source_count"] != expected["source_count"]
            or type(result["imported_missing_count"]) is not int
            or not 0 <= result["imported_missing_count"] <= expected["source_count"]
            or result["receipt_sha256"] != expected["request_hash"]
            or result["decision_history"] != "unavailable in v1 source"
        ):
            raise MigrationRehearsalError("Prepared dispatch import receipt does not match its source snapshot.")
    source_rows = connection.execute(
        """SELECT repository_id, milestone_id, source_kind, source_path, source_sha256, source_bytes
           FROM import_source"""
    ).fetchall()
    actual_sources = {
        (row["repository_id"], row["milestone_id"], row["source_kind"], row["source_path"]):
        (row["source_sha256"], bytes(row["source_bytes"]))
        for row in source_rows
    }
    if len(actual_sources) != len(source_rows) or set(actual_sources) != set(expected_sources) or any(
        actual_sources[key] != (_sha256(content), content) for key, content in expected_sources.items()
    ):
        raise MigrationRehearsalError("Prepared import sources do not exactly match the current quiesced inventory.")
    return {
        "source_inventory_sha256": inventory.report()["source_inventory_sha256"],
        "source_count": len(expected_sources),
    }


