from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from ..scanner.git_ops import ScanError
from ..workflow.git_state import WorkflowGitStateError, inspect_git_state
from ..workflow.handoff_packets import (
    MAX_GIT_PATHS,
    MAX_PACKET_BYTES,
    MAX_SOURCE_BYTES,
    HandoffPacketError,
    _open_conditions,
    _read_relative_source,
)
from ..workflow.lifecycle import WorkflowRecordError, load_milestone_workflow
from ..workflow.milestones import MilestoneArtifactError, load_milestone_selection
from .repository_registry import DOCUMENT_ROLES, RepositoryEntry

CONTRACT_VERSION = "1"
EVENT_LIMIT = 30


class ArchitectContractError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int) -> None:
        self.code = code
        self.message = message
        self.status_code = status_code
        super().__init__(message)


def _source_bytes(repository_root: Path, relative_path: str) -> tuple[str, int | None]:
    relative = PurePosixPath(relative_path)
    if (
        not relative_path
        or relative.is_absolute()
        or "\\" in relative_path
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        return "unsafe", None
    current = repository_root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return "unsafe", None
    try:
        resolved = repository_root.joinpath(*relative.parts).resolve(strict=True)
        if not resolved.is_relative_to(repository_root) or not resolved.is_file():
            return "unsafe", None
        return "present", resolved.stat().st_size
    except FileNotFoundError:
        return "missing", None
    except (OSError, RuntimeError):
        return "unreadable", None


def _event_view(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_id": event["event_id"],
        "timestamp": event["timestamp"],
        "event_type": event["event_type"],
        "reported_actor_role": event["actor_role"],
        "actor_authenticated": False,
        "summary": event["summary"],
        "binding": event.get("binding") is True and event["event_type"] == "lock_in_approved",
        "related_event_id": event.get("related_event_id"),
        "evidence_source": event.get("evidence_source"),
        "result": event.get("result"),
        "evidence_path": event.get("evidence_path"),
        "evidence_status": "reported" if event["event_type"] == "test_result_recorded" else None,
    }


def _bounded_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    selected = events[-EVENT_LIMIT:]
    return {
        "total_count": len(events),
        "returned_count": len(selected),
        "omitted_count": len(events) - len(selected),
        "events": [_event_view(event) for event in selected],
    }


def _get_selection(repository: RepositoryEntry, state_root: str | Path) -> dict[str, Any]:
    try:
        selection = load_milestone_selection(repository.path, state_root, repository.milestone_root)
    except (MilestoneArtifactError, OSError) as exc:
        raise ArchitectContractError("milestone_inventory_failed", str(exc), 503) from exc
    if selection["status"] == "invalid":
        message = selection.get("reason") or "The saved milestone selection is invalid."
        code = "missing_prompt" if "prompt" in message.lower() else "stale_milestone_pointer"
        status_code = 404 if code == "missing_prompt" else 409
        raise ArchitectContractError(code, message, status_code)
    return selection


def _load_workflow(
    repository: RepositoryEntry,
    state_root: str | Path,
    candidate: dict[str, Any],
) -> dict[str, Any]:
    prompt = PurePosixPath(candidate["prompt_path"])
    sidecar_path = prompt.with_name(prompt.name.removesuffix("_prompt.md") + "_workflow.md").as_posix()
    sidecar = repository.path.joinpath(*PurePosixPath(sidecar_path).parts)
    if sidecar.exists() or sidecar.is_symlink():
        try:
            _read_relative_source(repository.path, sidecar_path, max_bytes=5_000_000)
        except HandoffPacketError as exc:
            code = "source_oversized" if "exceeds" in str(exc) else "unsafe_workflow_sidecar"
            status_code = 413 if code == "source_oversized" else 422
            raise ArchitectContractError(code, str(exc), status_code) from exc
    try:
        return load_milestone_workflow(
            repository.path,
            state_root,
            repository.milestone_root,
            candidate,
        )
    except (MilestoneArtifactError, WorkflowRecordError, OSError) as exc:
        if "references a missing or mismatched closeout artifact" in str(exc):
            raise ArchitectContractError(
                "required_closeout_missing",
                "The workflow ledger records a submitted closeout, but the discovered closeout is missing or mismatched.",
                409,
            ) from exc
        raise ArchitectContractError("malformed_workflow_sidecar", str(exc), 422) from exc


def _base(
    repository: RepositoryEntry,
    registry_generation: int,
    state_root: str | Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    selection = _get_selection(repository, state_root)
    selected = selection.get("selection") if selection["status"] == "selected" else None
    envelope = {
        "contract_version": CONTRACT_VERSION,
        "repository": {
            "key": repository.key,
            "name": repository.name,
            "path": str(repository.path),
            "repository_id": repository.repository_id,
            "registry_generation": registry_generation,
        },
        "selection": {
            "status": selection["status"],
            "milestone_id": selected["milestone_id"] if selected else None,
            "prompt_path": selected["prompt_path"] if selected else None,
        },
    }
    return envelope, selected


def _milestone_view(
    repository: RepositoryEntry,
    state_root: str | Path,
    candidate: dict[str, Any],
    workflow: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if workflow is None:
        workflow = _load_workflow(repository, state_root, candidate)
    events = workflow["event_history"]
    sidecar_path = workflow["sidecar_path"]
    sidecar_status, sidecar_size = _source_bytes(repository.path, sidecar_path)
    if sidecar_status == "unsafe" or sidecar_status == "unreadable":
        raise ArchitectContractError(
            "unsafe_workflow_sidecar",
            f"Workflow sidecar {sidecar_path} is not a safe readable repository file.",
            422,
        )
    latest_lock_in = next(
        (
            event
            for event in reversed(events)
            if event["event_type"] == "lock_in_approved" and event.get("binding") is True
        ),
        None,
    )
    closeout = _closeout_metadata(repository, candidate, workflow["phase"])
    prompt_status, prompt_size = _source_bytes(repository.path, candidate["prompt_path"])
    if prompt_status != "present":
        raise ArchitectContractError(
            "missing_prompt" if prompt_status == "missing" else "unsafe_prompt",
            f"Selected prompt {candidate['prompt_path']} is {prompt_status}.",
            404 if prompt_status == "missing" else 422,
        )
    return {
        "id": candidate["milestone_id"],
        "phase": workflow["phase"],
        "waiting_reason": workflow.get("waiting_reason"),
        "waiting_category": workflow.get("waiting_category"),
        "prompt": {
            "path": candidate["prompt_path"],
            "status": prompt_status,
            "size_bytes": prompt_size,
            "source_type": "milestone_prompt",
        },
        "closeout": closeout,
        "workflow": {
            "path": sidecar_path,
            "status": "present" if sidecar_status == "present" else "absent",
            "size_bytes": sidecar_size,
            "source_type": "workflow_sidecar",
        },
        "event_count": workflow["event_count"],
        "latest_binding_lock_in": _event_view(latest_lock_in) if latest_lock_in else None,
        "open_conditions": _bounded_events(_open_conditions(workflow)),
        "lifecycle_phase": workflow["phase"],
        "next_authorized_action": workflow["next_action"],
        "lifecycle_source": {
            "source_type": "workflow_sidecar" if sidecar_status == "present" else "derived_empty_workflow",
            "path": sidecar_path,
        },
    }


def _closeout_metadata(
    repository: RepositoryEntry,
    candidate: dict[str, Any],
    phase: str,
) -> dict[str, Any]:
    closeout_path = candidate.get("closeout_path")
    if not closeout_path:
        expected_path = candidate["prompt_path"].removesuffix("_prompt.md") + "_closeout.md"
        source_status, size = _source_bytes(repository.path, expected_path)
        state = {
            "unsafe": "unsafe",
            "unreadable": "unreadable",
            "present": "ambiguous",
        }.get(source_status, "absent")
        required = phase == "REVIEW"
        return {
            "status": "required_missing" if required else state,
            "path": expected_path,
            "size_bytes": size,
            "source_status": source_status,
            "required_in_review": required,
            "lifecycle_phase": phase,
            "content": None,
            "source_type": "milestone_closeout",
        }
    status, size = _source_bytes(repository.path, closeout_path)
    if status != "present":
        raise ArchitectContractError(
            "unsafe_closeout",
            f"Configured closeout {closeout_path} is {status}.",
            422 if status in {"unsafe", "unreadable"} else 404,
        )
    return {
        "status": "present",
        "path": closeout_path,
        "size_bytes": size,
        "required_in_review": True,
        "lifecycle_phase": phase,
        "content": None,
        "source_type": "milestone_closeout",
    }


def _read_content(repository: RepositoryEntry, relative_path: str, source_type: str) -> dict[str, Any]:
    status, size = _source_bytes(repository.path, relative_path)
    if status != "present":
        code = "missing_source" if status == "missing" else "unsafe_source"
        http_status = 404 if status == "missing" else 422
        raise ArchitectContractError(code, f"Source {relative_path} is {status}.", http_status)
    if size is not None and size > MAX_SOURCE_BYTES:
        raise ArchitectContractError(
            "source_oversized",
            f"Source {relative_path} exceeds the {MAX_SOURCE_BYTES}-byte content limit; no partial content is returned.",
            413,
        )
    try:
        content = _read_relative_source(repository.path, relative_path)
    except HandoffPacketError as exc:
        code = "unsafe_source"
        if "private-key material" in str(exc):
            code = "unsafe_source"
        raise ArchitectContractError(code, str(exc), 422) from exc
    return {
        "source_type": source_type,
        "source_path": relative_path,
        "size_bytes": len(content.encode("utf-8")),
        "truncated": False,
        "content": content,
    }


def _git_view(repository: RepositoryEntry) -> dict[str, Any]:
    try:
        state = inspect_git_state(str(repository.path))
    except (ScanError, WorkflowGitStateError, OSError) as exc:
        raise ArchitectContractError("git_inspection_failed", str(exc), 503) from exc

    def paths_view(category: str) -> dict[str, Any]:
        item = state["working_tree"][category]
        paths = item["paths"]
        selected = paths[:MAX_GIT_PATHS]
        return {
            "total_count": item["count"],
            "returned_count": len(selected),
            "omitted_count": item["count"] - len(selected),
            "paths": selected,
        }

    branch = state["branch"]
    upstream = state["upstream"]
    return {
        "source": {
            "source_type": "local_git_state",
            "repository_path": str(repository.path),
            "observed_at": datetime.now(UTC).isoformat(),
        },
        "branch": branch.get("name") if branch.get("state") == "attached" else None,
        "head": state["head"],
        "upstream": {
            **upstream,
            "freshness": "local_tracking_refs_only",
            "remote_refresh_performed": False,
        },
        "working_tree": {
            "classification": state["workflow_state"],
            "is_clean": state["working_tree"]["is_clean"],
            "staged": paths_view("staged"),
            "unstaged": paths_view("unstaged"),
            "untracked": paths_view("untracked"),
            "unmerged": paths_view("unmerged"),
        },
        "git_operations": state["git_operations"],
        "git_operation_in_progress": state["git_operation_in_progress"],
    }


def _document_inventory(repository: RepositoryEntry) -> list[dict[str, Any]]:
    documents = []
    for role in sorted(DOCUMENT_ROLES):
        relative_path = repository.documents.get(role)
        if not relative_path:
            documents.append(
                {
                    "role": role,
                    "configured": False,
                    "path": None,
                    "status": "not_configured",
                    "size_bytes": None,
                    "source_type": "governing_document",
                }
            )
            continue
        status, size = _source_bytes(repository.path, relative_path)
        if status == "present" and size is not None and size > MAX_SOURCE_BYTES:
            status = "oversized"
        documents.append(
            {
                "role": role,
                "configured": True,
                "path": relative_path,
                "status": status,
                "size_bytes": size,
                "source_type": "governing_document",
            }
        )
    return documents


def _selected_response(
    resource: str,
    repository: RepositoryEntry,
    state_root: str | Path,
    candidate: dict[str, Any],
) -> dict[str, Any]:
    if resource == "prompt":
        return _read_content(repository, candidate["prompt_path"], "milestone_prompt")

    if resource == "closeout":
        try:
            workflow = _load_workflow(repository, state_root, candidate)
        except ArchitectContractError as exc:
            if exc.code != "required_closeout_missing":
                raise
            expected_path = candidate["prompt_path"].removesuffix("_prompt.md") + "_closeout.md"
            return {
                "status": "required_missing",
                "path": expected_path,
                "size_bytes": None,
                "required_in_review": True,
                "lifecycle_status": "invalid",
                "lifecycle_phase": None,
                "contract_error": {"code": exc.code, "message": str(exc)},
                "content": None,
                "source_type": "milestone_closeout",
            }
        closeout = _closeout_metadata(repository, candidate, workflow["phase"])
        if closeout["status"] == "present":
            closeout["content"] = _read_content(
                repository,
                closeout["path"],
                "milestone_closeout",
            )
        return closeout

    if resource in {"milestone", "workflow", "evidence", "review"}:
        workflow = _load_workflow(repository, state_root, candidate)
        milestone = _milestone_view(repository, state_root, candidate, workflow)
        events = workflow["event_history"]
        if resource == "milestone":
            return milestone
        if resource == "workflow":
            return {
                "source": milestone["lifecycle_source"],
                "phase": milestone["phase"],
                "waiting_reason": milestone["waiting_reason"],
                "waiting_category": milestone["waiting_category"],
                "event_count": len(events),
                "events": _bounded_events(events),
                "binding_lock_ins": _bounded_events(
                    [
                        event for event in events
                        if event["event_type"] == "lock_in_approved" and event.get("binding") is True
                    ]
                ),
                "open_conditions": milestone["open_conditions"],
                "next_authorized_action": milestone["next_authorized_action"],
            }
        if resource == "evidence":
            test_events = [
                event for event in events
                if event["event_type"] == "test_result_recorded"
            ]
            evidence = _bounded_events(test_events)
            return {
                "source": milestone["lifecycle_source"],
                "reported_vs_machine_observed": "reported",
                **evidence,
            }
        git_state = _git_view(repository)
        return {
            "repository": {
                "key": repository.key,
                "name": repository.name,
                "path": str(repository.path),
            },
            "milestone": milestone,
            "binding_lock_ins": _bounded_events(
                [
                    event for event in events
                    if event["event_type"] == "lock_in_approved" and event.get("binding") is True
                ]
            ),
            "test_evidence": {
                "reported_vs_machine_observed": "reported",
                **_bounded_events([
                    event for event in events
                    if event["event_type"] == "test_result_recorded"
                ]),
            },
            "git": git_state,
            "governing_documents": _document_inventory(repository),
            "source_references": [
                {"source_type": "milestone_prompt", "path": candidate["prompt_path"]},
                {"source_type": "workflow_sidecar", "path": milestone["workflow"]["path"]},
                *(
                    [{"source_type": "milestone_closeout", "path": milestone["closeout"]["path"]}]
                    if milestone["closeout"]["status"] == "present"
                    else []
                ),
            ],
        }
    raise ArchitectContractError("unsupported_resource", "Unsupported Architect contract resource.", 404)


def build_architect_response(
    *,
    repository: RepositoryEntry,
    registry_generation: int,
    state_root: str | Path,
    resource: str,
    role: str | None = None,
    expected_repository_key: str | None = None,
    expected_generation: int | None = None,
    expected_milestone_id: str | None = None,
) -> dict[str, Any]:
    if expected_repository_key is not None and expected_repository_key != repository.key:
        raise ArchitectContractError(
            "stale_repository_selection",
            "The active repository differs from the expected repository key.",
            409,
        )
    if expected_generation is not None and expected_generation != registry_generation:
        raise ArchitectContractError(
            "stale_repository_generation",
            "The repository registry generation changed; refresh status before retrying.",
            409,
        )
    envelope, candidate = _base(repository, registry_generation, state_root)
    if expected_milestone_id is not None and (
        candidate is None or candidate["milestone_id"] != expected_milestone_id
    ):
        raise ArchitectContractError(
            "stale_milestone_selection",
            "The selected milestone differs from the expected milestone ID.",
            409,
        )

    if resource == "status":
        data: dict[str, Any] = {
            "service": "repo-control",
            "status": "available",
            "milestone_selected": candidate is not None,
        }
        if candidate:
            data["milestone"] = _milestone_view(repository, state_root, candidate)
        envelope["data"] = data
    elif resource == "documents":
        inventory = _document_inventory(repository)
        if role is not None:
            if role not in DOCUMENT_ROLES:
                raise ArchitectContractError("unsupported_document_role", "Unsupported governing-document role.", 404)
            configured_path = repository.documents.get(role)
            if not configured_path:
                raise ArchitectContractError(
                    "document_role_not_configured",
                    f"Governing-document role {role!r} is not configured for this repository.",
                    404,
                )
            envelope["data"] = next(item for item in inventory if item["role"] == role)
            envelope["data"]["content"] = _read_content(repository, configured_path, "governing_document")
        else:
            envelope["data"] = {
                "source_type": "repository_registry",
                "documents": inventory,
            }
    elif resource == "git":
        envelope["data"] = _git_view(repository)
    else:
        if resource not in {"milestone", "prompt", "workflow", "closeout", "evidence", "review"}:
            raise ArchitectContractError("unsupported_resource", "Unsupported Architect contract resource.", 404)
        if candidate is None:
            raise ArchitectContractError(
                "no_selected_milestone",
                "No milestone is selected for the active repository.",
                409,
            )
        envelope["data"] = _selected_response(resource, repository, state_root, candidate)

    envelope["source_scope"] = {
        "repository_key": repository.key,
        "milestone_id": candidate["milestone_id"] if candidate else None,
    }
    return envelope


def response_size_bytes(payload: dict[str, Any]) -> int:
    import json

    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8"))


def maximum_response_bytes() -> int:
    return MAX_PACKET_BYTES
