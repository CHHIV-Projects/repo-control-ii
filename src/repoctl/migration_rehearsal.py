from __future__ import annotations

import hashlib
import json
import re
import tempfile
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from .document_library import (
    DocumentRecord,
    GitFileProvider,
    GitRepositoryContext,
    ProviderSnapshot,
)
from .information_store import (
    ImportSourceError,
    InformationStore,
    StoreIdentity,
    V1FixtureBundle,
    _parse_sidecar_bytes,
)
from .scanner.util import make_repository_id
from .web.repository_registry import RepositoryEntry
from .workflow.architect_dispatches import read_owner_dispatch_ledger_snapshot
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
                "Only the selected registry repository is inventoried; other configured repositories are not inspected.",
                "Git records represent the observed working tree, not historical committed blob contents.",
                "Committed history not exposed by the allowlisted document provider is not inventoried.",
                "Only document types accepted by the existing DocumentLibrary provider are included.",
                "The generated inventory JSON is outside the provider's allowed document roots/types and is not part of its own snapshot.",
            ],
        }

    @property
    def documents(self) -> tuple[DocumentRecord, ...]:
        return self.provider_snapshot.records


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


def _workflow_details(record: DocumentRecord, repository: RepositoryEntry) -> dict[str, Any] | None:
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
    try:
        events = _parse_sidecar_bytes(record.content.encode("utf-8"), identity)
        projection = _reduce_events(events)
    except (ImportSourceError, WorkflowRecordError) as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    return {
        "v1_lifecycle_events": bool(events),
        "v1_event_count": len(events),
        "lifecycle_projection": {
            "phase": projection["phase"],
            "waiting_category": projection["waiting_category"],
            "event_count": len(events),
        },
    }


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
    for record in snapshot.records:
        details = _workflow_details(record, repository)
        if details is None:
            records.append(record)
            continue
        if "error" in details:
            warnings.append(f"Workflow sidecar {record.path!r} could not be reduced: {details['error']}")
            records.append(record)
            continue
        provenance = dict(record.version_provenance)
        records.append(
            replace(
                record,
                version_provenance={
                    **provenance,
                    "v1_lifecycle_events": details["v1_lifecycle_events"],
                    "v1_event_count": details["v1_event_count"],
                    "lifecycle_projection": details["lifecycle_projection"],
                },
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
            "source": "other configured repositories",
            "reason": "Not inspected; M010.008 grants no authority to inventory another repository.",
        },
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


def _validate_rehearsal_root(value: str | Path, repository_root: Path) -> Path:
    candidate = Path(value).expanduser()
    if candidate.is_symlink() or not candidate.is_dir():
        raise MigrationRehearsalError("Rehearsal destination must be an existing real directory.")
    root = candidate.resolve(strict=True)
    temp_root = Path(tempfile.gettempdir()).resolve(strict=True)
    if (
        not root.is_relative_to(temp_root)
        or root == repository_root
        or root.is_relative_to(repository_root)
        or repository_root.is_relative_to(root)
    ):
        raise MigrationRehearsalError("Rehearsal databases must remain in a separate disposable temp directory.")
    return root


def rehearse_milestone_imports(
    inventory: RepositoryInventory,
    destination_root: str | Path,
) -> dict[str, Any]:
    """Import canonical milestone source copies into one disposable store per milestone."""
    root = _validate_rehearsal_root(destination_root, inventory.repository.path)
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
        database_path = root / f"milestone-{_sha256(milestone_id.encode())[:24]}.sqlite3"
        if database_path.exists() or database_path.is_symlink():
            raise MigrationRehearsalError("Refusing to overwrite an existing rehearsal database.")
        store = InformationStore.initialize_disposable(
            database_path,
            identity,
            allow_unsupported_runtime_for_fixture=True,
        )
        dispatch_snapshot = inventory.dispatch_snapshot
        dispatch_bytes = (
            dispatch_snapshot.get("source_bytes")
            if dispatch_snapshot is not None and dispatch_snapshot.get("status") == "present"
            else None
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
            expected_events = _parse_sidecar_bytes(workflow.content.encode("utf-8"), identity)
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
        unresolved_document_references = [
            {
                "event_id": event["event_id"],
                "event_type": event["event_type"],
                "evidence_path": event["evidence_path"],
            }
            for event in imported_events
            if isinstance(event.get("evidence_path"), str)
            and event["evidence_path"] not in imported_source_paths
        ]
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


