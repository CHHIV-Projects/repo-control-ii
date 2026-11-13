from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any, Protocol

from ..scanner.core import DEFAULT_STATE_ROOT
from ..scanner.git_ops import ScanError, validate_git_worktree
from .architect_dispatches import ArchitectDispatchError, get_dispatch, list_owner_dispatches
from ..web.repository_registry import RepositoryEntry, RepositoryRegistry, RepositoryRegistryError
from .authority import get_authorization_status, is_correction_mode as _is_correction_mode
from .lifecycle import (
    WorkflowRecordError,
    load_milestone_workflow,
    record_milestone_event,
    workflow_runtime_capabilities,
)
from .context_verification import (
    VERIFICATION_RECORD_SCHEMA_VERSION,
    VERIFICATION_EVENT_TYPE,
    VERIFICATION_TRIGGERS,
    build_authority_snapshot,
    collect_verification_sources,
    make_verification_record,
    snapshot_digest,
    validate_verification_record,
)
from .runtime_compatibility import (
    PRODUCER_CONTRACT_ID,
    RuntimeCompatibilityError,
    require_event_runtime_compatibility,
)
from .milestones import (
    MilestoneArtifactError,
    load_milestone_selection,
)

GIT_MUTATION_POLICY_NOTICE = (
    "Coding agents must not stage, commit, push, tag, merge, rebase, reset, "
    "stash, clean, switch/checkout, create/delete branches, or mutate remotes. "
    "Stop after implementation, tests, closeout, and final read-only Git status."
)


class CoderInterfaceError(RuntimeError):
    pass


class _InformationStoreIdentity(Protocol):
    repository_id: str
    repository_key: str
    repository_root: str
    milestone_id: str
    prompt_path: str
    selection_id: str
    selection_revision: int


class InformationStoreCoderClients(Protocol):
    identity: _InformationStoreIdentity
    fixture_only: bool

    def coder_snapshot(self) -> dict[str, Any]: ...

    def runtime_binding(self) -> dict[str, Any]: ...

    def list_documents(self, *, after_order: int = 0) -> dict[str, Any]: ...

    def get_dispatch(self, dispatch_id: str) -> dict[str, Any]: ...

    def get_revision(self, document_id: str, revision_id: str) -> dict[str, Any]: ...

    def read_revision_page(
        self,
        document_id: str,
        revision_id: str,
        *,
        byte_offset: int = 0,
        max_bytes: int = 64 * 1024,
    ) -> dict[str, Any]: ...

    def read_selected_milestone_document_page(
        self,
        document_path: str,
        *,
        start_byte: int = 0,
        source_fingerprint: str | None = None,
    ) -> dict[str, Any]: ...

    def append_coder_event(
        self,
        *,
        event_type: str,
        summary: str,
        event_fields: dict[str, Any] | None = None,
        expected_store_revision: int | None = None,
        expected_prompt_revision_id: str | None = None,
        expected_guidance_sha256: str | None = None,
    ) -> dict[str, Any]: ...

    def submit_coder_closeout(
        self,
        *,
        summary: str,
        document_id: str,
        revision_id: str,
        content_sha256: str,
        expected_prompt_revision_id: str,
        expected_store_revision: int,
    ) -> dict[str, Any]: ...

    def submit_coder_closeout_draft(
        self,
        *,
        summary: str,
        content: str,
        content_sha256: str,
        expected_prompt_revision_id: str,
        expected_store_revision: int,
    ) -> dict[str, Any]: ...


CENTRAL_REPOSITORY_REGISTRY_PATH = Path(__file__).resolve().parents[3] / "ops" / "repositories.toml"
MAX_CODER_DISPATCH_BODY_BYTES = 64 * 1024


def resolve_repository_entry(
    repository_path: str | Path | None = None,
    milestone_root: str | None = None,
) -> RepositoryEntry:
    target = Path(repository_path).resolve() if repository_path else Path.cwd().resolve()
    try:
        root = validate_git_worktree(target)
    except ScanError as exc:
        raise CoderInterfaceError(f"Target is not a valid Git repository: {exc}") from exc

    if not CENTRAL_REPOSITORY_REGISTRY_PATH.is_file():
        raise CoderInterfaceError(
            f"Managed repository registry is unavailable: {CENTRAL_REPOSITORY_REGISTRY_PATH}"
        )
    try:
        registry = RepositoryRegistry.from_toml(CENTRAL_REPOSITORY_REGISTRY_PATH)
    except RepositoryRegistryError as exc:
        message = str(exc)
        if message.startswith("Cannot read repository registry"):
            raise CoderInterfaceError(f"Managed repository registry is unavailable: {message}") from exc
        raise CoderInterfaceError(f"Managed repository registry is invalid: {message}") from exc

    matches = [entry for entry in registry.entries if entry.path == root]
    if not matches:
        raise CoderInterfaceError(
            f"Repository is not managed by the central Repo Control registry: {root}"
        )
    if len(matches) != 1:
        raise CoderInterfaceError(
            f"Repository path is mapped by multiple central Repo Control entries: {root}"
        )

    entry = matches[0]
    configured_milestone_path = entry.path.joinpath(*Path(entry.milestone_root).parts)
    if not configured_milestone_path.is_dir():
        raise CoderInterfaceError(
            f"Configured milestone root is missing or is not a directory for managed repository "
            f"{entry.key!r}: {entry.milestone_root}"
        )
    if milestone_root is not None and milestone_root != entry.milestone_root:
        raise CoderInterfaceError(
            f"Milestone root is controlled by the central Repo Control registry for managed repository "
            f"{entry.key!r}: expected {entry.milestone_root!r}"
        )
    return entry


def _resolve_context_and_workflow(
    repository_path: str | Path | None = None,
    state_root: str | Path | None = None,
    milestone_root: str | None = None,
) -> tuple[RepositoryEntry, Path, dict[str, Any], dict[str, Any]]:
    entry = resolve_repository_entry(repository_path, milestone_root)
    effective_state_root = Path(state_root).expanduser().resolve() if state_root else DEFAULT_STATE_ROOT
    try:
        selection = load_milestone_selection(entry.path, effective_state_root, entry.milestone_root)
    except MilestoneArtifactError as exc:
        raise CoderInterfaceError(str(exc)) from exc

    if selection["status"] == "none":
        raise CoderInterfaceError(
            "No milestone is currently selected. The Product Owner must select an active milestone in Repo Control first."
        )
    if selection["status"] == "invalid":
        reason = selection.get("reason") or "The saved milestone selection is invalid."
        raise CoderInterfaceError(f"{reason} Select an active milestone in Repo Control first.")

    candidate = selection["selection"]
    try:
        workflow = load_milestone_workflow(entry.path, effective_state_root, entry.milestone_root, candidate)
    except WorkflowRecordError as exc:
        raise CoderInterfaceError(f"Could not load workflow for selected milestone: {exc}") from exc

    return entry, effective_state_root, candidate, workflow


def _validate_information_store_overrides(
    information_store_clients: InformationStoreCoderClients,
    repository_path: str | Path | None,
    state_root: str | Path | None,
    milestone_root: str | None,
) -> RepositoryEntry:
    identity = information_store_clients.identity
    if state_root is not None:
        binding = information_store_clients.runtime_binding()
        if (
            information_store_clients.fixture_only
            or binding.get("authority") != "sqlite_managed"
            or binding.get("authority_state_root") != str(Path(state_root).expanduser().resolve())
        ):
            raise CoderInterfaceError("A legacy state-root override is unavailable under InformationStore authority.")
    entry = resolve_repository_entry(repository_path or identity.repository_root, milestone_root)
    if (
        entry.repository_id != identity.repository_id
        or entry.key != identity.repository_key
        or entry.path != Path(identity.repository_root).expanduser().resolve()
    ):
        raise CoderInterfaceError("InformationStore identity differs from the central repository registry.")
    return entry


def _information_store_coder_context(
    information_store_clients: InformationStoreCoderClients,
    repository_path: str | Path | None = None,
    state_root: str | Path | None = None,
    milestone_root: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    from .lifecycle import _open_bug_ids, _open_question_ids
    from ..procedure_release import inspect_procedure_release_preparation

    entry = _validate_information_store_overrides(
        information_store_clients, repository_path, state_root, milestone_root
    )
    context = information_store_clients.coder_snapshot()
    identity = information_store_clients.identity
    snapshot = context["snapshot"]
    events = snapshot["events"]
    phase = snapshot["projection"]["phase"]
    closeout_path = identity.prompt_path.removesuffix("_prompt.md") + "_closeout.md"
    documents: list[dict[str, Any]] = []
    cursor = 0
    while True:
        page = information_store_clients.list_documents(after_order=cursor)
        documents.extend(page["documents"])
        if page["next_cursor"] is None:
            break
        cursor = page["next_cursor"]
    closeouts = [
        item
        for item in documents
        if item["logical_type"] == "CLOSEOUT" and item["source_path"] == closeout_path
    ]
    if len(closeouts) > 1:
        raise CoderInterfaceError("InformationStore contains ambiguous closeout revisions for the selected milestone.")
    authorization = get_authorization_status(
        phase,
        _is_correction_mode({"phase": phase, "event_history": events}),
    )
    unverified_guidance = context["unverified_approved_architect_dispatches"]
    if unverified_guidance and authorization["authorized"]:
        authorization = {
            "authorized": False,
            "can_preflight": False,
            "status": "UNVERIFIED_GUIDANCE",
            "message": (
                "NO — imported approved guidance lacks explicit decision evidence; "
                "Product Owner reconciliation is required."
            ),
        }
    workflow = {
        "phase": phase,
        "waiting_category": snapshot["projection"]["waiting_category"],
        "waiting_reason": snapshot["projection"]["waiting_reason"],
        "event_history": events,
        "event_count": len(events),
        "prompt_identity": (
            f"{context['prompt']['document_id']}:{context['prompt']['revision_id']}:"
            f"{context['prompt']['content_sha256']}"
        ),
    }
    candidate = {
        "milestone_id": identity.milestone_id,
        "prompt_path": identity.prompt_path,
        "closeout_exists": bool(closeouts),
        "closeout_path": closeout_path if closeouts else None,
    }
    release_preparation = inspect_procedure_release_preparation(
        entry.path,
        candidate,
        workflow,
        repository_key=entry.key,
        repository_id=entry.repository_id,
        prompt_content=context["prompt"]["content"].encode("utf-8"),
    )
    question_ids = _open_question_ids(events)
    bug_ids = _open_bug_ids(events)
    latest_lock_in = next(
        (
            event
            for event in reversed(events)
            if event.get("event_type") == "lock_in_approved" and event.get("binding") is True
        ),
        None,
    )
    status = {
        "repository": {
            "name": entry.name,
            "path": str(entry.path),
            "key": entry.key,
            "repository_id": entry.repository_id,
            "milestone_root": entry.milestone_root,
            "documents": dict(entry.documents),
        },
        "selection": {
            "milestone_id": identity.milestone_id,
            "prompt_path": identity.prompt_path,
            "selection_id": identity.selection_id,
            "selection_revision": identity.selection_revision,
        },
        "milestone": {
            "id": identity.milestone_id,
            "phase": phase,
            "closeout_exists": bool(closeouts),
            "closeout_path": closeout_path if closeouts else None,
        },
        "authorization": authorization,
        "procedure_release_preparation": release_preparation,
        "local_runtime_capabilities": {
            "producer_contract_id": PRODUCER_CONTRACT_ID,
            "workflow": {
                **workflow_runtime_capabilities(),
                "context_verification_record_schema_version": VERIFICATION_RECORD_SCHEMA_VERSION,
            },
        },
        "workflow": {
            "phase": phase,
            "waiting_category": snapshot["projection"]["waiting_category"],
            "waiting_reason": snapshot["projection"]["waiting_reason"],
            "next_action": authorization["message"],
            "open_questions": [event for event in events if event["event_id"] in question_ids],
            "open_bugs": [event for event in events if event["event_id"] in bug_ids],
            "latest_binding_lock_in": latest_lock_in,
            "recent_events": events[-5:],
            "event_count": len(events),
        },
        "standing_references": {
            "coding_agent_rules": entry.documents.get("coding_agent_rules"),
            "project_workflow": entry.documents.get("workflow"),
            "project_architecture": entry.documents.get("architecture"),
            "project_context": entry.documents.get("context"),
            "canonical_parking_lot": entry.documents.get("parking_lot"),
            "git_mutation_policy": GIT_MUTATION_POLICY_NOTICE,
        },
        "prompt": {
            "path": identity.prompt_path,
            "size_bytes": len(context["prompt"]["content"].encode("utf-8")),
            "identity": workflow["prompt_identity"],
        },
        "information_store_authority": snapshot["authority"],
        "unverified_approved_architect_dispatches": [
            item["dispatch_id"] for item in unverified_guidance
        ],
    }
    return status, context


def get_coder_status(
    repository_path: str | Path | None = None,
    state_root: str | Path | None = None,
    milestone_root: str | None = None,
    information_store_clients: InformationStoreCoderClients | None = None,
) -> dict[str, Any]:
    if information_store_clients is not None:
        status_data, _ = _information_store_coder_context(
            information_store_clients, repository_path, state_root, milestone_root
        )
        return status_data
    entry, _, candidate, workflow = _resolve_context_and_workflow(
        repository_path=repository_path,
        state_root=state_root,
        milestone_root=milestone_root,
    )

    phase = workflow["phase"]
    is_corr = _is_correction_mode(workflow)
    auth = get_authorization_status(phase, is_corr)
    from ..procedure_release import inspect_procedure_release_preparation

    release_preparation = inspect_procedure_release_preparation(
        entry.path,
        candidate,
        workflow,
        repository_key=entry.key,
        repository_id=entry.repository_id,
    )
    local_runtime_capabilities = {
        "producer_contract_id": PRODUCER_CONTRACT_ID,
        "workflow": {
            **workflow_runtime_capabilities(),
            "context_verification_record_schema_version": VERIFICATION_RECORD_SCHEMA_VERSION,
        },
    }

    binding_lock_ins = [
        event
        for event in workflow.get("event_history", [])
        if event.get("event_type") == "lock_in_approved" and event.get("binding") is True
    ]
    latest_lock_in = binding_lock_ins[-1] if binding_lock_ins else None

    prompt_path = entry.path / candidate["prompt_path"]
    prompt_size = prompt_path.stat().st_size if prompt_path.is_file() else 0
    from ..web.architect_contract import (
        MAX_MILESTONE_DOCUMENT_PAGE_BYTES,
        MAX_REPOSITORY_DOCUMENT_FILE_BYTES,
    )

    standing_references = {
        "coding_agent_rules": entry.documents.get("coding_agent_rules"),
        "project_workflow": entry.documents.get("workflow"),
        "project_architecture": entry.documents.get("architecture"),
        "project_context": entry.documents.get("context"),
        "canonical_parking_lot": entry.documents.get("parking_lot"),
        "git_mutation_policy": GIT_MUTATION_POLICY_NOTICE,
    }

    return {
        "repository": {
            "name": entry.name,
            "path": str(entry.path),
            "key": entry.key,
            "repository_id": entry.repository_id,
            "milestone_root": entry.milestone_root,
            "documents": dict(entry.documents),
        },
        "selection": {
            "milestone_id": candidate["milestone_id"],
            "prompt_path": candidate["prompt_path"],
        },
        "milestone": {
            "id": candidate["milestone_id"],
            "phase": phase,
            "closeout_exists": candidate["closeout_exists"],
            "closeout_path": candidate.get("closeout_path"),
        },
        "milestone_document_paging": {
            "command": "repoctl coder-read-milestone-document",
            "page_limit_bytes": MAX_MILESTONE_DOCUMENT_PAGE_BYTES,
            "maximum_file_bytes": MAX_REPOSITORY_DOCUMENT_FILE_BYTES,
            "continuation_requires_source_fingerprint": True,
            "evidence_paths_must_be_named_in_active_prompt": True,
        },
        "authorization": auth,
        "procedure_release_preparation": release_preparation,
        "local_runtime_capabilities": local_runtime_capabilities,
        "workflow": {
            "phase": phase,
            "waiting_category": workflow.get("waiting_category"),
            "waiting_reason": workflow.get("waiting_reason"),
            "next_action": workflow.get("next_action"),
            "open_questions": workflow.get("open_questions", []),
            "open_bugs": workflow.get("open_bugs", []),
            "latest_binding_lock_in": latest_lock_in,
            "recent_events": workflow.get("event_history", [])[-5:],
            "event_count": workflow.get("event_count", 0),
        },
        "standing_references": standing_references,
        "prompt": {
            "path": candidate["prompt_path"],
            "size_bytes": prompt_size,
            "identity": workflow["prompt_identity"],
        },
    }


def resolve_coder_release_preparation_context(
    repository_path: str | Path | None = None,
    state_root: str | Path | None = None,
    milestone_root: str | None = None,
) -> tuple[RepositoryEntry, dict[str, Any], dict[str, Any]]:
    entry, _, candidate, workflow = _resolve_context_and_workflow(
        repository_path=repository_path,
        state_root=state_root,
        milestone_root=milestone_root,
    )
    return entry, candidate, workflow


def get_coder_context(
    repository_path: str | Path | None = None,
    state_root: str | Path | None = None,
    milestone_root: str | None = None,
    information_store_clients: InformationStoreCoderClients | None = None,
) -> dict[str, Any]:
    if information_store_clients is not None:
        status_data, context = _information_store_coder_context(
            information_store_clients, repository_path, state_root, milestone_root
        )
        applicable_dispatches = context["approved_architect_dispatches"]
        context_dispatches = []
        included_body_bytes = 0
        for index, item in enumerate(applicable_dispatches, start=1):
            body_bytes = len(item["body"].encode("utf-8"))
            include_body = included_body_bytes + body_bytes <= MAX_CODER_DISPATCH_BODY_BYTES
            dispatch = {
                "alias": f"A{index}",
                "dispatch_id": item["dispatch_id"],
                "type": item["dispatch_type"],
                "milestone_id": item["milestone_id"],
                "approved_at": item["updated_at"],
                "related_event_id": item.get("related_event_id"),
                "body_included": include_body,
            }
            if include_body:
                dispatch["body"] = item["body"]
                included_body_bytes += body_bytes
            context_dispatches.append(dispatch)
        return {
            **status_data,
            "approved_architect_dispatches": context_dispatches,
            "prompt": {
                **status_data["prompt"],
                "content": context["prompt"]["content"],
            },
        }
    status_data = get_coder_status(
        repository_path=repository_path,
        state_root=state_root,
        milestone_root=milestone_root,
    )
    repo_path = Path(status_data["repository"]["path"])
    prompt_rel_path = status_data["prompt"]["path"]
    prompt_full_path = repo_path / prompt_rel_path

    if prompt_full_path.is_file():
        prompt_content = prompt_full_path.read_text(encoding="utf-8")
    else:
        prompt_content = f"Error: prompt file not found at {prompt_rel_path}"

    context_data = dict(status_data)
    effective_state_root = Path(state_root).expanduser().resolve() if state_root else DEFAULT_STATE_ROOT
    milestone_id = status_data["milestone"]["id"]
    try:
        dispatches = list_owner_dispatches(
            repo_path,
            effective_state_root,
            status_data["repository"]["repository_id"],
        )
    except ArchitectDispatchError as exc:
        raise CoderInterfaceError(f"Could not load approved Architect dispatches: {exc}") from exc
    applicable_dispatches = [
        item
        for item in reversed(dispatches)
        if item["status"] == "APPROVED"
        and item["type"] in {"INSTRUCTION", "RESPONSE"}
        and item.get("approved_milestone_id") == milestone_id
    ]
    context_dispatches = []
    included_body_bytes = 0
    for index, item in enumerate(applicable_dispatches, start=1):
        body_bytes = len(item["body"].encode("utf-8"))
        include_body = included_body_bytes + body_bytes <= MAX_CODER_DISPATCH_BODY_BYTES
        dispatch = {
            "alias": f"A{index}",
            "dispatch_id": item["dispatch_id"],
            "type": item["type"],
            "milestone_id": item["milestone_id"],
            "approved_at": item["approved_at"],
            "related_event_id": item.get("related_event_id"),
            "body_included": include_body,
        }
        if include_body:
            dispatch["body"] = item["body"]
            included_body_bytes += body_bytes
        context_dispatches.append(dispatch)
    context_data["approved_architect_dispatches"] = context_dispatches
    context_data["prompt"] = {
        "path": prompt_rel_path,
        "content": prompt_content,
        "size_bytes": len(prompt_content.encode("utf-8")),
        "identity": status_data["prompt"]["identity"],
    }
    return context_data


def read_coder_dispatch_body(
    dispatch_id: str,
    repository_path: str | Path | None = None,
    state_root: str | Path | None = None,
    milestone_root: str | None = None,
    information_store_clients: InformationStoreCoderClients | None = None,
) -> dict[str, Any]:
    if information_store_clients is not None:
        _validate_information_store_overrides(
            information_store_clients, repository_path, state_root, milestone_root
        )
        identity = information_store_clients.identity
        snapshot = information_store_clients.coder_snapshot()["snapshot"]
        dispatch = information_store_clients.get_dispatch(dispatch_id)
        if (
            dispatch["status"] != "APPROVED"
            or dispatch["dispatch_type"] not in {"INSTRUCTION", "RESPONSE"}
            or dispatch.get("approved_milestone_id") != identity.milestone_id
            or dispatch["milestone_id"] != identity.milestone_id
        ):
            raise CoderInterfaceError(
                "The requested dispatch is not approved for the currently selected milestone."
            )
        return {
            "dispatch_id": dispatch["dispatch_id"],
            "milestone_id": identity.milestone_id,
            "type": dispatch["dispatch_type"],
            "approved_at": dispatch["updated_at"],
            "body": dispatch["body"],
            "store_revision": snapshot["store_revision"],
        }
    status_data = get_coder_status(
        repository_path=repository_path,
        state_root=state_root,
        milestone_root=milestone_root,
    )
    repository = status_data["repository"]
    milestone_id = status_data["milestone"]["id"]
    effective_state_root = Path(state_root).expanduser().resolve() if state_root else DEFAULT_STATE_ROOT
    try:
        dispatch = get_dispatch(
            repository["path"],
            effective_state_root,
            repository["repository_id"],
            dispatch_id,
        )
    except ArchitectDispatchError as exc:
        raise CoderInterfaceError(f"Could not read approved Architect dispatch: {exc}") from exc
    if (
        dispatch["status"] != "APPROVED"
        or dispatch["type"] not in {"INSTRUCTION", "RESPONSE"}
        or dispatch.get("approved_milestone_id") != milestone_id
    ):
        raise CoderInterfaceError(
            "The requested dispatch is not approved for the currently selected milestone."
        )
    return {
        "dispatch_id": dispatch["dispatch_id"],
        "milestone_id": milestone_id,
        "type": dispatch["type"],
        "approved_at": dispatch["approved_at"],
        "body": dispatch["body"],
    }


def read_coder_milestone_document_page(
    document_path: str,
    *,
    expected_milestone_id: str,
    start_byte: int = 0,
    source_fingerprint: str | None = None,
    repository_path: str | Path | None = None,
    state_root: str | Path | None = None,
    milestone_root: str | None = None,
    information_store_clients: InformationStoreCoderClients | None = None,
) -> dict[str, Any]:
    if information_store_clients is not None:
        _validate_information_store_overrides(
            information_store_clients, repository_path, state_root, milestone_root
        )
        from ..information_store import InformationStoreError
        from ..web.architect_contract import ArchitectContractError
        from .information_store_fixture import FixtureIntegrationError

        if information_store_clients.identity.milestone_id != expected_milestone_id:
            raise CoderInterfaceError(
                "The selected milestone changed; refresh Coder context before reading another page."
            )
        try:
            selected = information_store_clients.coder_snapshot()
            page = information_store_clients.read_selected_milestone_document_page(
                document_path,
                start_byte=start_byte,
                source_fingerprint=source_fingerprint,
            )
            current = information_store_clients.coder_snapshot()
            if (
                current["snapshot"]["store_revision"] != selected["snapshot"]["store_revision"]
                or current["snapshot"]["guidance_sha256"] != selected["snapshot"]["guidance_sha256"]
                or current["prompt"]["revision_id"] != selected["prompt"]["revision_id"]
            ):
                raise CoderInterfaceError("Store authority changed during document reading; refresh and retry.")
            return page
        except ArchitectContractError as exc:
            raise CoderInterfaceError(f"Could not read selected milestone document ({exc.code}): {exc}") from exc
        except (InformationStoreError, FixtureIntegrationError) as exc:
            raise CoderInterfaceError(f"Could not read selected milestone document from InformationStore: {exc}") from exc

    entry, _effective_state_root, candidate, _workflow = _resolve_context_and_workflow(
        repository_path=repository_path,
        state_root=state_root,
        milestone_root=milestone_root,
    )
    if candidate["milestone_id"] != expected_milestone_id:
        raise CoderInterfaceError(
            "The selected milestone changed; refresh Coder context before reading another page."
        )
    from ..web.architect_contract import (
        ArchitectContractError,
        read_selected_milestone_document_page,
    )

    try:
        return read_selected_milestone_document_page(
            entry,
            candidate,
            document_path,
            start_byte=start_byte,
            source_fingerprint=source_fingerprint,
        )
    except ArchitectContractError as exc:
        raise CoderInterfaceError(
            f"Could not read selected milestone document ({exc.code}): {exc}"
        ) from exc


def format_coder_status_text(data: dict[str, Any]) -> str:
    repo = data["repository"]
    milestone = data["milestone"]
    auth = data["authorization"]
    wf = data["workflow"]
    prompt = data["prompt"]

    waiting_text = "None (active)"
    if wf.get("waiting_category") or wf.get("waiting_reason"):
        waiting_text = f"[{wf.get('waiting_category', 'general')}] {wf.get('waiting_reason')}"

    lock_in_text = "None"
    latest_lock_in = wf.get("latest_binding_lock_in")
    if latest_lock_in:
        lock_in_text = f"{latest_lock_in.get('event_id')}: {latest_lock_in.get('summary')}"

    closeout_text = "Absent"
    if milestone.get("closeout_exists"):
        closeout_text = f"Present ({milestone.get('closeout_path')})"

    lines = [
        "=" * 80,
        "REPO CONTROL — CODER STATUS",
        "=" * 80,
        f"Repository:     {repo['name']} ({repo['path']})",
        f"Managed Key:    {repo['key']}",
        f"Repository ID:  {repo['repository_id']}",
        f"Milestone Root: {repo['milestone_root']}",
        f"Selected ID:    {milestone['id']}",
        f"Prompt Path:    {prompt['path']} ({prompt.get('size_bytes', 0)} bytes)",
        (
            "Document Paging: "
            f"{data.get('milestone_document_paging', {}).get('command', 'unavailable')} "
            f"--path <path> --expected-milestone-id {milestone['id']} "
            "[--start-byte <next_offset> --source-fingerprint <fingerprint>]"
        ),
        f"Lifecycle:      {milestone['phase']}",
        f"Authorization:  {auth['message']}",
        f"Procedure Release: {data.get('procedure_release_preparation', {}).get('status', 'UNKNOWN')}",
        "Local Workflow Schema: "
        + json.dumps(data.get("local_runtime_capabilities", {}).get("workflow", {}), sort_keys=True),
        (
            "Procedure Release Diagnostic: "
            + data.get("procedure_release_preparation", {}).get("reason", "No additional diagnostic.")
        ),
        f"Next Action:    {wf.get('next_action')}",
        f"Closeout:       {closeout_text}",
        f"Waiting State:  {waiting_text}",
        f"Latest Lock-In: {lock_in_text}",
        f"Open Questions: {len(wf.get('open_questions', []))}",
        f"Open Bugs:      {len(wf.get('open_bugs', []))}",
        f"Event Count:    {wf.get('event_count', 0)}",
        "=" * 80,
    ]
    return "\n".join(lines)


def format_coder_context_text(data: dict[str, Any]) -> str:
    status_header = format_coder_status_text(data)
    refs = data.get("standing_references", {})
    prompt_content = data.get("prompt", {}).get("content", "")
    dispatches = data.get("approved_architect_dispatches", [])

    lines = [
        status_header,
        "",
        "-" * 80,
        "STANDING DOCUMENT REFERENCES & RULES",
        "-" * 80,
        f"- Coding Agent Rules:  {refs.get('coding_agent_rules')}",
        f"- Project Context:     {refs.get('project_context')}",
        f"- Project Workflow:    {refs.get('project_workflow')}",
        f"- Architecture:        {refs.get('project_architecture')}",
        f"- Canonical Parking Lot: {refs.get('canonical_parking_lot')}",
        "",
        "Git Mutation Policy:",
        refs.get("git_mutation_policy", GIT_MUTATION_POLICY_NOTICE),
        "",
        "-" * 80,
        "APPROVED ARCHITECT DISPATCHES",
        "-" * 80,
    ]
    if dispatches:
        for dispatch in dispatches:
            lines.extend(
                [
                    f"{dispatch['alias']} — {dispatch['milestone_id']}",
                    f"Type: {dispatch['type']}",
                    f"Approved: {dispatch['approved_at']}",
                ]
            )
            if dispatch.get("body_included", "body" in dispatch):
                lines.extend([dispatch["body"], ""])
            else:
                lines.extend(
                    [
                        f"Body not embedded to keep Coder context within "
                        f"{MAX_CODER_DISPATCH_BODY_BYTES} bytes.",
                        "Read the complete instruction with: repoctl coder-read-dispatch "
                        f"--dispatch-id {dispatch['dispatch_id']}",
                        "",
                    ]
                )
    else:
        lines.append("None.")
    release_preparation = data.get("procedure_release_preparation", {})
    if release_preparation.get("authorized"):
        lines.extend(
            [
                "",
                "-" * 80,
                "RELEASE-PREPARATION SCOPE RESTRICTION",
                "-" * 80,
                "Only the explicitly authorized shared-procedure release may be prepared. "
                "Do not perform generic milestone work or use generated instructions as authority.",
                "Authorized canonical source paths: "
                + ", ".join(release_preparation.get("authorized_sources", [])),
                "Use coder-procedure-release-preview before coder-procedure-release-publish. "
                "Procedure Apply remains unavailable; independently safe transaction Recover remains available.",
            ]
        )
        if release_preparation.get("candidate_validation_status"):
            lines.append(
                "Candidate validation: "
                f"{release_preparation['candidate_validation_status']}"
            )
        if release_preparation.get("candidate_validation_error"):
            lines.append(
                "Candidate validation error: "
                f"{release_preparation['candidate_validation_error']}"
            )
    elif release_preparation.get("status") == "RELEASE_PUBLISHED":
        lines.extend(
            [
                "",
                "-" * 80,
                "RELEASE-PREPARATION SCOPE RESTRICTION",
                "-" * 80,
                "The approved canonical release is published. Do not edit additional canonical sources "
                "or perform generic milestone work; finish validation and closeout only.",
            ]
        )
    lines.extend(
        [
            "",
            "-" * 80,
            "AUTHORITATIVE PROMPT CONTENT",
            "-" * 80,
            prompt_content,
            "=" * 80,
        ]
    )
    return "\n".join(lines)


def submit_coder_question(
    summary: str,
    blocking: bool = False,
    category: str | None = None,
    repository_path: str | Path | None = None,
    state_root: str | Path | None = None,
    milestone_root: str | None = None,
    information_store_clients: InformationStoreCoderClients | None = None,
) -> dict[str, Any]:
    if not summary or not summary.strip():
        raise CoderInterfaceError("Question summary must not be empty.")
    clean_summary = summary.strip()

    valid_categories = {"clarification", "decision", "blocker", "other"}
    if category is not None and category not in valid_categories:
        raise CoderInterfaceError(
            f"Invalid category {category!r}. Must be one of: {sorted(valid_categories)}"
        )

    if blocking:
        waiting_category = category or "blocker"
        waiting_reason = clean_summary
    else:
        waiting_category = None
        waiting_reason = None

    if information_store_clients is not None:
        _validate_information_store_overrides(
            information_store_clients, repository_path, state_root, milestone_root
        )
        event_fields = (
            {"waiting_category": waiting_category, "waiting_reason": waiting_reason}
            if blocking
            else {}
        )
        result = information_store_clients.append_coder_event(
            event_type="question_submitted",
            summary=clean_summary,
            event_fields=event_fields,
        )
        event = result["event"]
        return {
            "event_id": event["event_id"],
            "milestone_id": information_store_clients.identity.milestone_id,
            "phase_before": event["phase_before"],
            "phase_after": event["phase_after"],
            "blocking": blocking,
            "waiting_category": waiting_category,
            "waiting_reason": waiting_reason,
            "summary": clean_summary,
            "next_action": get_authorization_status(event["phase_after"])["message"],
            "transaction_receipt": result["receipt"],
        }

    entry, effective_state_root, candidate, _ = _resolve_context_and_workflow(
        repository_path=repository_path,
        state_root=state_root,
        milestone_root=milestone_root,
    )

    try:
        updated_workflow = record_milestone_event(
            repository_root=entry.path,
            state_root=effective_state_root,
            milestone_root=entry.milestone_root,
            candidate=candidate,
            event_type="question_submitted",
            actor_role="coder",
            summary=clean_summary,
            waiting_category=waiting_category,
            waiting_reason=waiting_reason,
            po_approved=False,
        )
    except WorkflowRecordError as exc:
        raise CoderInterfaceError(str(exc)) from exc

    recorded_event = updated_workflow["event_history"][-1]
    return {
        "event_id": recorded_event["event_id"],
        "milestone_id": candidate["milestone_id"],
        "phase_before": recorded_event["phase_before"],
        "phase_after": recorded_event["phase_after"],
        "blocking": blocking,
        "waiting_category": waiting_category,
        "waiting_reason": waiting_reason,
        "summary": clean_summary,
        "next_action": updated_workflow["next_action"],
    }


def submit_coder_bug(
    summary: str,
    repository_path: str | Path | None = None,
    state_root: str | Path | None = None,
    milestone_root: str | None = None,
    information_store_clients: InformationStoreCoderClients | None = None,
) -> dict[str, Any]:
    if not summary or not summary.strip():
        raise CoderInterfaceError("Bug summary must not be empty.")
    clean_summary = summary.strip()

    if information_store_clients is not None:
        _validate_information_store_overrides(
            information_store_clients, repository_path, state_root, milestone_root
        )
        result = information_store_clients.append_coder_event(
            event_type="bug_discovered",
            summary=clean_summary,
        )
        event = result["event"]
        return {
            "event_id": event["event_id"],
            "milestone_id": information_store_clients.identity.milestone_id,
            "phase": event["phase_after"],
            "summary": clean_summary,
            "next_action": get_authorization_status(event["phase_after"])["message"],
            "transaction_receipt": result["receipt"],
        }

    entry, effective_state_root, candidate, _ = _resolve_context_and_workflow(
        repository_path=repository_path,
        state_root=state_root,
        milestone_root=milestone_root,
    )

    try:
        updated_workflow = record_milestone_event(
            repository_root=entry.path,
            state_root=effective_state_root,
            milestone_root=entry.milestone_root,
            candidate=candidate,
            event_type="bug_discovered",
            actor_role="coder",
            summary=clean_summary,
            po_approved=False,
        )
    except WorkflowRecordError as exc:
        raise CoderInterfaceError(str(exc)) from exc

    recorded_event = updated_workflow["event_history"][-1]
    return {
        "event_id": recorded_event["event_id"],
        "milestone_id": candidate["milestone_id"],
        "phase": recorded_event["phase_after"],
        "summary": clean_summary,
        "next_action": updated_workflow["next_action"],
    }


def submit_coder_bug_fix(
    bug_event_id: str,
    summary: str,
    repository_path: str | Path | None = None,
    state_root: str | Path | None = None,
    milestone_root: str | None = None,
    information_store_clients: InformationStoreCoderClients | None = None,
) -> dict[str, Any]:
    if not isinstance(bug_event_id, str) or not bug_event_id:
        raise CoderInterfaceError("Bug event ID must not be empty.")
    if not summary or not summary.strip():
        raise CoderInterfaceError("Bug fix summary must not be empty.")
    clean_summary = summary.strip()
    if information_store_clients is not None:
        _validate_information_store_overrides(
            information_store_clients, repository_path, state_root, milestone_root
        )
        result = information_store_clients.append_coder_event(
            event_type="bug_fixed",
            summary=clean_summary,
            event_fields={"related_event_id": bug_event_id},
        )
        event = result["event"]
        return {
            "event_id": event["event_id"],
            "bug_event_id": bug_event_id,
            "milestone_id": information_store_clients.identity.milestone_id,
            "phase": event["phase_after"],
            "summary": clean_summary,
            "next_action": get_authorization_status(event["phase_after"])["message"],
            "transaction_receipt": result["receipt"],
        }
    entry, effective_state_root, candidate, workflow = _resolve_context_and_workflow(
        repository_path=repository_path,
        state_root=state_root,
        milestone_root=milestone_root,
    )
    fixable_ids = {
        item.get("event_id")
        for item in workflow.get("fixable_bugs", [])
        if isinstance(item, dict)
    }
    if bug_event_id not in fixable_ids:
        raise CoderInterfaceError("Bug event ID is not a classified, unfixed in-scope bug.")
    try:
        updated_workflow = record_milestone_event(
            repository_root=entry.path,
            state_root=effective_state_root,
            milestone_root=entry.milestone_root,
            candidate=candidate,
            event_type="bug_fixed",
            actor_role="coder",
            summary=clean_summary,
            related_event_id=bug_event_id,
            po_approved=False,
        )
    except WorkflowRecordError as exc:
        raise CoderInterfaceError(str(exc)) from exc
    recorded_event = updated_workflow["event_history"][-1]
    return {
        "event_id": recorded_event["event_id"],
        "bug_event_id": bug_event_id,
        "milestone_id": candidate["milestone_id"],
        "phase": recorded_event["phase_after"],
        "summary": clean_summary,
        "next_action": updated_workflow["next_action"],
    }


def submit_coder_test_result(
    summary: str,
    source: str,
    result: str,
    repository_path: str | Path | None = None,
    state_root: str | Path | None = None,
    milestone_root: str | None = None,
    information_store_clients: InformationStoreCoderClients | None = None,
) -> dict[str, Any]:
    if not summary or not summary.strip():
        raise CoderInterfaceError("Test result summary must not be empty.")
    if not source or not source.strip():
        raise CoderInterfaceError("Evidence source must not be empty.")
    if not result or not result.strip():
        raise CoderInterfaceError("Test outcome result must not be empty.")
    clean_summary = summary.strip()
    clean_source = source.strip()
    clean_result = result.strip()

    if information_store_clients is not None:
        _validate_information_store_overrides(
            information_store_clients, repository_path, state_root, milestone_root
        )
        result_record = information_store_clients.append_coder_event(
            event_type="test_result_recorded",
            summary=clean_summary,
            event_fields={"evidence_source": clean_source, "result": clean_result},
        )
        event = result_record["event"]
        return {
            "event_id": event["event_id"],
            "milestone_id": information_store_clients.identity.milestone_id,
            "phase": event["phase_after"],
            "evidence_source": clean_source,
            "result": clean_result,
            "summary": clean_summary,
            "next_action": get_authorization_status(event["phase_after"])["message"],
            "transaction_receipt": result_record["receipt"],
        }

    entry, effective_state_root, candidate, _ = _resolve_context_and_workflow(
        repository_path=repository_path,
        state_root=state_root,
        milestone_root=milestone_root,
    )

    try:
        updated_workflow = record_milestone_event(
            repository_root=entry.path,
            state_root=effective_state_root,
            milestone_root=entry.milestone_root,
            candidate=candidate,
            event_type="test_result_recorded",
            actor_role="coder",
            summary=clean_summary,
            evidence_source=clean_source,
            result=clean_result,
            po_approved=False,
        )
    except WorkflowRecordError as exc:
        raise CoderInterfaceError(str(exc)) from exc

    recorded_event = updated_workflow["event_history"][-1]
    return {
        "event_id": recorded_event["event_id"],
        "milestone_id": candidate["milestone_id"],
        "phase": recorded_event["phase_after"],
        "evidence_source": clean_source,
        "result": clean_result,
        "summary": clean_summary,
        "next_action": updated_workflow["next_action"],
    }


def _submit_information_store_verification(
    information_store_clients: InformationStoreCoderClients,
    *,
    trigger: str,
    git_boundary_acknowledged: bool,
    expected_repository_key: str,
    expected_repository_id: str,
    expected_repository_root: str | Path,
    expected_milestone_id: str,
    repository_path: str | Path | None,
    state_root: str | Path | None,
    milestone_root: str | None,
    runtime_url: str | None,
    expected_selection_id: str | None,
    expected_selection_revision: int | None,
    expected_prompt_document_id: str | None,
    expected_prompt_revision_id: str | None,
    expected_prompt_sha256: str | None,
    allow_materialized_prompt: bool,
) -> dict[str, Any]:
    from ..information_store import InformationStoreError
    from .approved_prompt_preflight import (
        PromptPreflightError,
        PromptPreflightObservation,
        PromptRevisionBinding,
        validate_fixture_prompt_preflight,
        validate_managed_prompt_preflight,
    )
    from .lifecycle import _open_bug_ids, _open_question_ids

    entry = _validate_information_store_overrides(
        information_store_clients, repository_path, state_root, milestone_root
    )
    identity = information_store_clients.identity
    if (
        entry.key != expected_repository_key
        or entry.repository_id != expected_repository_id
        or entry.path != Path(expected_repository_root).expanduser().resolve()
        or identity.milestone_id != expected_milestone_id
    ):
        raise CoderInterfaceError("Selected repository or milestone differs from the expected identity.")

    status, context = _information_store_coder_context(
        information_store_clients, repository_path, state_root, milestone_root
    )
    store_snapshot = context["snapshot"]
    prompt = context["prompt"]
    expected_values = (
        ("selection ID", expected_selection_id, identity.selection_id),
        ("selection revision", expected_selection_revision, identity.selection_revision),
        ("prompt document ID", expected_prompt_document_id, prompt["document_id"]),
        ("prompt revision ID", expected_prompt_revision_id, prompt["revision_id"]),
        ("prompt SHA-256", expected_prompt_sha256, prompt["content_sha256"]),
    )
    for label, expected, actual in expected_values:
        if expected is not None and expected != actual:
            raise CoderInterfaceError(f"Selected {label} differs from the expected binding.")
    if store_snapshot["prompt_revision_id"] != prompt["revision_id"]:
        raise CoderInterfaceError("Selected prompt revision changed during verification.")

    binding = information_store_clients.runtime_binding()
    if (
        binding["repository_id"] != identity.repository_id
        or binding["repository_key"] != identity.repository_key
        or binding["repository_root"] != identity.repository_root
        or binding["milestone_id"] != identity.milestone_id
        or binding["prompt_path"] != identity.prompt_path
        or binding["selection_id"] != identity.selection_id
        or binding["selection_revision"] != identity.selection_revision
        or binding["prompt_revision_id"] != prompt["revision_id"]
        or binding["prompt_sha256"] != prompt["content_sha256"]
    ):
        raise CoderInterfaceError("Store authority changed during verification; refresh and retry.")

    events = store_snapshot["events"]
    question_ids = _open_question_ids(events)
    bug_ids = _open_bug_ids(events)
    workflow = {
        "phase": store_snapshot["projection"]["phase"],
        "waiting_category": store_snapshot["projection"]["waiting_category"],
        "waiting_reason": store_snapshot["projection"]["waiting_reason"],
        "event_history": events,
        "event_count": len(events),
        "open_questions": [event for event in events if event["event_id"] in question_ids],
        "open_bugs": [event for event in events if event["event_id"] in bug_ids],
        "prompt_identity": prompt["content_sha256"],
        "sidecar_path": None,
    }
    candidate = {
        "milestone_id": identity.milestone_id,
        "prompt_path": identity.prompt_path,
        "closeout_exists": status["milestone"]["closeout_exists"],
        "closeout_path": status["milestone"]["closeout_path"],
    }
    approved_dispatches = [
        {
            "dispatch_id": item["dispatch_id"],
            "type": item["dispatch_type"],
            "milestone_id": item["milestone_id"],
            "approved_at": item["updated_at"],
            "related_event_id": item.get("related_event_id"),
            "body_sha256": hashlib.sha256(item["body"].encode("utf-8")).hexdigest(),
        }
        for item in reversed(context["approved_architect_dispatches"])
    ]
    sources = collect_verification_sources(
        entry,
        candidate,
        workflow,
        DEFAULT_STATE_ROOT,
        prompt_content=prompt["content"].encode("utf-8"),
        approved_dispatches_override=approved_dispatches,
    )
    git_state = sources["git_state"]
    preflight = None
    if allow_materialized_prompt and git_state is None:
        raise CoderInterfaceError("Materialized-prompt preflight requires an available Git observation.")
    if git_state is not None and (status["authorization"]["authorized"] or allow_materialized_prompt):
        if git_state.get("git_operation_in_progress"):
            raise CoderInterfaceError("Materialized-prompt preflight cannot run during a Git operation.")
        prompt_binding = PromptRevisionBinding(
            repository_key=identity.repository_key,
            repository_id=identity.repository_id,
            repository_root=identity.repository_root,
            milestone_id=identity.milestone_id,
            prompt_path=identity.prompt_path,
            document_id=prompt["document_id"],
            revision_id=prompt["revision_id"],
            content_sha256=prompt["content_sha256"],
            selection_id=identity.selection_id,
            selection_revision=identity.selection_revision,
        )
        dirty_entries = []
        for item in git_state.get("working_tree", {}).get("entries", []):
            path = item.get("path")
            kind = item.get("kind")
            xy = item.get("xy") or ""
            status_name = (
                "conflicted" if kind == "unmerged" else
                "untracked" if kind == "untracked" else
                "staged" if xy[:1] not in {"", "."} else
                "modified"
            )
            dirty = {"path": path, "status": status_name}
            if path == identity.prompt_path and allow_materialized_prompt:
                worktree_prompt = entry.path / identity.prompt_path
                try:
                    resolved = worktree_prompt.resolve(strict=True)
                    if not resolved.is_relative_to(entry.path) or not resolved.is_file():
                        raise CoderInterfaceError("Materialized prompt is not a contained regular file.")
                    if any(
                        (entry.path.joinpath(*Path(identity.prompt_path).parts[:index])).is_symlink()
                        for index in range(1, len(Path(identity.prompt_path).parts) + 1)
                    ):
                        raise CoderInterfaceError("Materialized prompt cannot contain symlinked path components.")
                    content = resolved.read_bytes()
                except OSError as exc:
                    raise CoderInterfaceError(f"Cannot read the materialized prompt: {exc}") from exc
                if len(content) > 2 * 1024 * 1024:
                    raise CoderInterfaceError("Materialized prompt exceeds the supported identity size.")
                dirty["content_sha256"] = hashlib.sha256(content).hexdigest()
            dirty_entries.append(dirty)
        try:
            observation = PromptPreflightObservation(
                binding=prompt_binding,
                approval_status="APPROVED",
                lifecycle_phase=workflow["phase"],
                lifecycle_authorized=status["authorization"]["authorized"],
                source_mode=(
                    "database_revision_materialized" if allow_materialized_prompt else "database_revision"
                ),
            )
            if information_store_clients.fixture_only:
                preflight = validate_fixture_prompt_preflight(
                    prompt_binding, observation, dirty_entries, fixture_only=True,
                )
            else:
                preflight = validate_managed_prompt_preflight(
                    prompt_binding, observation, dirty_entries,
                    managed_authority=binding["authority"] == "sqlite_managed",
                )
        except PromptPreflightError as exc:
            raise CoderInterfaceError(f"Materialized-prompt preflight failed: {exc}") from exc

    authorization = status["authorization"]
    procedure = sources["procedure"]
    snapshot = build_authority_snapshot(
        repository={"key": entry.key, "repository_id": entry.repository_id, "path": str(entry.path)},
        candidate=candidate,
        workflow=workflow,
        authorization=authorization,
        procedure=procedure,
        git_state=git_state,
        approved_dispatches=approved_dispatches,
    )
    snapshot["information_store"] = {
        **binding,
        "prompt_document_id": prompt["document_id"],
        "guidance_sha256": store_snapshot["guidance_sha256"],
        "materialized_prompt_preflight": preflight,
    }
    if sources["git_failure_type"] is not None:
        snapshot["git"]["failure_type"] = sources["git_failure_type"]
    unverified_guidance = context["unverified_approved_architect_dispatches"]
    if unverified_guidance:
        snapshot["approved_architect_dispatches"] = {
            "inspection_status": "unavailable",
            "failure_type": "unverified_imported_approval",
            "unverified_count": len(unverified_guidance),
        }
    if not authorization["authorized"]:
        result = "BLOCKED"
    elif (
        sources["release_preparation"].get("authorized")
        and git_state is not None
        and not unverified_guidance
        and git_boundary_acknowledged
    ):
        result = "RELEASE_PREPARATION"
    elif (
        sources["release_preparation"].get("status") == "RELEASE_PUBLISHED"
        and git_state is not None
        and not unverified_guidance
        and git_boundary_acknowledged
    ):
        result = "RELEASE_PUBLISHED"
    elif (
        procedure.get("inspection_status") != "available"
        or procedure.get("status") != "CURRENT"
        or not procedure.get("overlay_valid")
        or git_state is None
        or unverified_guidance
        or not git_boundary_acknowledged
    ):
        result = "PARTIAL_UNVERIFIED"
    else:
        result = "VERIFIED"
    try:
        runtime = require_event_runtime_compatibility(
            VERIFICATION_EVENT_TYPE,
            runtime_url,
            expected_information_store_binding=binding,
        )
    except RuntimeCompatibilityError as exc:
        raise CoderInterfaceError(f"{exc.code}: {exc}") from exc

    current_digest = snapshot_digest(snapshot)
    latest = next(
        (event for event in reversed(events) if event["event_type"] == VERIFICATION_EVENT_TYPE),
        None,
    )
    if latest is not None:
        prior = latest.get("context_verification", {})
        if (
            latest is events[-1]
            and validate_verification_record(prior)
            and prior.get("trigger") == trigger
            and prior.get("authority_snapshot_sha256") == current_digest
            and prior.get("git_boundary_acknowledged") is git_boundary_acknowledged
            and prior.get("runtime_compatibility") == runtime
        ):
            current = information_store_clients.coder_snapshot()["snapshot"]
            if (
                current["store_revision"] != store_snapshot["store_revision"]
                or current["guidance_sha256"] != store_snapshot["guidance_sha256"]
                or information_store_clients.runtime_binding() != binding
            ):
                raise CoderInterfaceError("Store authority changed during verification; refresh and retry.")
            return {
                "event_id": latest["event_id"],
                "milestone_id": identity.milestone_id,
                "repository_key": entry.key,
                "result": prior["result"],
                "trigger": trigger,
                "authority_snapshot_sha256": current_digest,
                "authority_snapshot": prior["authority_snapshot"],
                "idempotent": True,
                "phase": workflow["phase"],
                "session_identity": prior["session_identity"],
                "provider_delivery": prior["provider_delivery"],
                "runtime_compatibility": prior["runtime_compatibility"],
            }

    verification = make_verification_record(
        trigger=trigger,
        snapshot=snapshot,
        result=result,
        git_boundary_acknowledged=git_boundary_acknowledged,
        runtime_compatibility=runtime,
    )
    try:
        recorded = information_store_clients.append_coder_event(
            event_type=VERIFICATION_EVENT_TYPE,
            summary=f"Context verification: {result} ({trigger})",
            event_fields={"context_verification": verification},
            expected_store_revision=store_snapshot["store_revision"],
            expected_prompt_revision_id=prompt["revision_id"],
            expected_guidance_sha256=store_snapshot["guidance_sha256"],
        )
    except (InformationStoreError, WorkflowRecordError) as exc:
        raise CoderInterfaceError(str(exc)) from exc
    return {
        "event_id": recorded["event"]["event_id"],
        "milestone_id": identity.milestone_id,
        "repository_key": entry.key,
        "result": result,
        "trigger": trigger,
        "authority_snapshot_sha256": current_digest,
        "authority_snapshot": snapshot,
        "idempotent": False,
        "phase": workflow["phase"],
        "authorization": authorization,
        "procedure_status": procedure.get("status", "UNAVAILABLE"),
        "release_preparation": sources["release_preparation"],
        "runtime_compatibility": runtime,
        "git_state": snapshot["git"],
        "session_identity": verification["session_identity"],
        "provider_delivery": verification["provider_delivery"],
    }


def submit_coder_context_verification(
    trigger: str,
    *,
    git_boundary_acknowledged: bool = False,
    expected_repository_key: str,
    expected_repository_id: str,
    expected_repository_root: str | Path,
    expected_milestone_id: str,
    repository_path: str | Path | None = None,
    state_root: str | Path | None = None,
    milestone_root: str | None = None,
    runtime_url: str | None = None,
    information_store_clients: InformationStoreCoderClients | None = None,
    expected_selection_id: str | None = None,
    expected_selection_revision: int | None = None,
    expected_prompt_document_id: str | None = None,
    expected_prompt_revision_id: str | None = None,
    expected_prompt_sha256: str | None = None,
    allow_materialized_prompt: bool = False,
) -> dict[str, Any]:
    if not isinstance(trigger, str) or trigger not in VERIFICATION_TRIGGERS:
        raise CoderInterfaceError("Choose NEW, HANDOFF, AUTHORITY_TRANSITION, or RECOVERY as the trigger.")
    if (
        not isinstance(expected_repository_key, str)
        or not expected_repository_key.strip()
        or not isinstance(expected_repository_id, str)
        or not expected_repository_id.strip()
        or not isinstance(expected_repository_root, (str, Path))
        or not str(expected_repository_root).strip()
        or not isinstance(expected_milestone_id, str)
        or not expected_milestone_id.strip()
    ):
        raise CoderInterfaceError("Expected repository key and milestone ID are required.")

    if information_store_clients is not None:
        return _submit_information_store_verification(
            information_store_clients,
            trigger=trigger,
            git_boundary_acknowledged=git_boundary_acknowledged,
            expected_repository_key=expected_repository_key,
            expected_repository_id=expected_repository_id,
            expected_repository_root=expected_repository_root,
            expected_milestone_id=expected_milestone_id,
            repository_path=repository_path,
            state_root=state_root,
            milestone_root=milestone_root,
            runtime_url=runtime_url,
            expected_selection_id=expected_selection_id,
            expected_selection_revision=expected_selection_revision,
            expected_prompt_document_id=expected_prompt_document_id,
            expected_prompt_revision_id=expected_prompt_revision_id,
            expected_prompt_sha256=expected_prompt_sha256,
            allow_materialized_prompt=allow_materialized_prompt,
        )
    if any(
        value is not None
        for value in (
            expected_selection_id, expected_selection_revision, expected_prompt_document_id,
            expected_prompt_revision_id, expected_prompt_sha256,
        )
    ) or allow_materialized_prompt:
        raise CoderInterfaceError("InformationStore verification options require an injected store authority.")

    entry, effective_state_root, candidate, workflow = _resolve_context_and_workflow(
        repository_path=repository_path,
        state_root=state_root,
        milestone_root=milestone_root,
    )
    if entry.key != expected_repository_key:
        raise CoderInterfaceError(
            f"Selected repository key {entry.key!r} does not match expected key {expected_repository_key!r}."
        )
    if entry.repository_id != expected_repository_id:
        raise CoderInterfaceError(
            f"Selected repository ID {entry.repository_id!r} does not match expected ID "
            f"{expected_repository_id!r}."
        )
    expected_root = Path(expected_repository_root).expanduser().resolve()
    if entry.path != expected_root:
        raise CoderInterfaceError(
            f"Selected repository root {str(entry.path)!r} does not match expected root "
            f"{str(expected_root)!r}."
        )
    if candidate["milestone_id"] != expected_milestone_id:
        raise CoderInterfaceError(
            f"Selected milestone {candidate['milestone_id']!r} does not match expected milestone "
            f"{expected_milestone_id!r}."
        )

    sources = collect_verification_sources(entry, candidate, workflow, effective_state_root)
    procedure = sources["procedure"]
    git_state = sources["git_state"]
    approved_dispatches = sources["approved_dispatches"]

    authorization = get_authorization_status(
        workflow["phase"],
        _is_correction_mode(workflow),
    )
    snapshot = build_authority_snapshot(
        repository={
            "key": entry.key,
            "repository_id": entry.repository_id,
            "path": str(entry.path),
        },
        candidate=candidate,
        workflow=workflow,
        authorization=authorization,
        procedure=procedure,
        git_state=git_state,
        approved_dispatches=approved_dispatches,
    )
    if sources["git_failure_type"] is not None:
        snapshot["git"]["failure_type"] = sources["git_failure_type"]
    if sources["dispatch_failure_type"] is not None:
        snapshot["approved_architect_dispatches"]["failure_type"] = sources["dispatch_failure_type"]
    if not authorization["authorized"]:
        result = "BLOCKED"
    elif (
        sources["release_preparation"].get("authorized")
        and git_state is not None
        and approved_dispatches is not None
        and git_boundary_acknowledged
    ):
        result = "RELEASE_PREPARATION"
    elif (
        sources["release_preparation"].get("status") == "RELEASE_PUBLISHED"
        and git_state is not None
        and approved_dispatches is not None
        and git_boundary_acknowledged
    ):
        result = "RELEASE_PUBLISHED"
    elif (
        procedure.get("inspection_status") != "available"
        or procedure.get("status") != "CURRENT"
        or not procedure.get("overlay_valid")
        or git_state is None
        or approved_dispatches is None
        or not git_boundary_acknowledged
    ):
        result = "PARTIAL_UNVERIFIED"
    else:
        result = "VERIFIED"
    try:
        runtime_compatibility = require_event_runtime_compatibility(
            VERIFICATION_EVENT_TYPE,
            runtime_url,
        )
    except RuntimeCompatibilityError as exc:
        raise CoderInterfaceError(f"{exc.code}: {exc}") from exc

    current_digest = snapshot_digest(snapshot)
    latest_verification = next(
        (
            event for event in reversed(workflow["event_history"])
            if event["event_type"] == VERIFICATION_EVENT_TYPE
        ),
        None,
    )
    if latest_verification is not None:
        prior = latest_verification.get("context_verification", {})
        if (
            latest_verification is workflow["event_history"][-1]
            and validate_verification_record(prior)
            and prior.get("trigger") == trigger
            and prior.get("authority_snapshot_sha256") == current_digest
            and prior.get("git_boundary_acknowledged") is git_boundary_acknowledged
            and prior.get("runtime_compatibility") == runtime_compatibility
        ):
            return {
                "event_id": latest_verification["event_id"],
                "milestone_id": candidate["milestone_id"],
                "repository_key": entry.key,
                "result": prior.get("result"),
                "trigger": trigger,
                "authority_snapshot_sha256": current_digest,
                "authority_snapshot": prior["authority_snapshot"],
                "idempotent": True,
                "phase": workflow["phase"],
                "session_identity": prior["session_identity"],
                "provider_delivery": prior["provider_delivery"],
                "runtime_compatibility": prior["runtime_compatibility"],
            }

    verification = make_verification_record(
        trigger=trigger,
        snapshot=snapshot,
        result=result,
        git_boundary_acknowledged=git_boundary_acknowledged,
        runtime_compatibility=runtime_compatibility,
    )

    try:
        updated_workflow = record_milestone_event(
            repository_root=entry.path,
            state_root=effective_state_root,
            milestone_root=entry.milestone_root,
            candidate=candidate,
            event_type=VERIFICATION_EVENT_TYPE,
            actor_role="coder",
            summary=f"Context verification: {result} ({trigger})",
            context_verification=verification,
            expected_phase=workflow["phase"],
            expected_event_count=workflow["event_count"],
            expected_prompt_identity=workflow["prompt_identity"],
        )
    except WorkflowRecordError as exc:
        raise CoderInterfaceError(str(exc)) from exc

    recorded_event = updated_workflow["event_history"][-1]
    return {
        "event_id": recorded_event["event_id"],
        "milestone_id": candidate["milestone_id"],
        "repository_key": entry.key,
        "result": result,
        "trigger": trigger,
        "authority_snapshot_sha256": current_digest,
        "authority_snapshot": snapshot,
        "idempotent": False,
        "phase": updated_workflow["phase"],
        "authorization": authorization,
        "procedure_status": procedure.get("status", "UNAVAILABLE"),
        "release_preparation": sources["release_preparation"],
        "runtime_compatibility": verification["runtime_compatibility"],
        "git_state": snapshot["git"],
        "session_identity": verification["session_identity"],
        "provider_delivery": verification["provider_delivery"],
    }


def submit_coder_closeout(
    summary: str | None = None,
    repository_path: str | Path | None = None,
    state_root: str | Path | None = None,
    milestone_root: str | None = None,
    information_store_clients: InformationStoreCoderClients | None = None,
    document_id: str | None = None,
    revision_id: str | None = None,
    content_sha256: str | None = None,
    draft_file: str | None = None,
) -> dict[str, Any]:
    clean_summary = summary.strip() if summary and summary.strip() else "Closeout submitted for review"

    if information_store_clients is not None:
        status_data, context = _information_store_coder_context(
            information_store_clients, repository_path, state_root, milestone_root
        )
        identity = information_store_clients.identity
        if not status_data["authorization"]["authorized"]:
            raise CoderInterfaceError(status_data["authorization"]["message"])
        if not isinstance(content_sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", content_sha256):
            raise CoderInterfaceError("InformationStore closeout requires an exact lowercase SHA-256 hash.")
        if status_data["procedure_release_preparation"].get("authorized"):
            raise CoderInterfaceError("Closeout is unavailable while a shared-procedure release is being prepared.")
        if draft_file is not None:
            from ..information_store import MAX_DOCUMENT_BYTES
            from ..web.architect_contract import ArchitectContractError, _read_repository_document

            expected_path = identity.prompt_path.removesuffix("_prompt.md") + "_closeout.md"
            if draft_file != expected_path or document_id is not None or revision_id is not None:
                raise CoderInterfaceError(
                    "Draft import requires only the selected canonical closeout path and its exact hash."
                )
            entry = _validate_information_store_overrides(
                information_store_clients, repository_path, state_root, milestone_root
            )
            try:
                source = _read_repository_document(
                    entry,
                    draft_file,
                    selected_milestone_prompt_path=identity.prompt_path,
                    max_bytes=MAX_DOCUMENT_BYTES,
                )
            except ArchitectContractError as exc:
                raise CoderInterfaceError(f"Cannot safely read the explicit closeout draft: {exc}") from exc
            if source["truncated"] or hashlib.sha256(source["content"].encode("utf-8")).hexdigest() != content_sha256:
                raise CoderInterfaceError("Closeout draft bytes do not match the explicitly supplied hash.")
            submitted = information_store_clients.submit_coder_closeout_draft(
                summary=clean_summary,
                content=source["content"],
                content_sha256=content_sha256,
                expected_prompt_revision_id=context["prompt"]["revision_id"],
                expected_store_revision=context["snapshot"]["store_revision"],
            )
            document_id = submitted["document_id"]
            revision_id = submitted["revision_id"]
        else:
            if not isinstance(document_id, str) or not document_id or not isinstance(revision_id, str) or not revision_id:
                raise CoderInterfaceError(
                    "InformationStore closeout requires an exact document ID, revision ID, and hash."
                )
            submitted = information_store_clients.submit_coder_closeout(
                summary=clean_summary,
                document_id=document_id,
                revision_id=revision_id,
                content_sha256=content_sha256,
                expected_prompt_revision_id=context["prompt"]["revision_id"],
                expected_store_revision=context["snapshot"]["store_revision"],
            )
        event = submitted["event"]
        return {
            "event_id": event["event_id"],
            "milestone_id": identity.milestone_id,
            "phase_before": event["phase_before"],
            "phase_after": event["phase_after"],
            "evidence_path": event["evidence_path"],
            "summary": clean_summary,
            "next_action": get_authorization_status(event["phase_after"], False)["message"],
            "document_id": document_id,
            "revision_id": revision_id,
            "content_sha256": content_sha256,
            "transaction_receipt": submitted["receipt"],
        }
    if any(value is not None for value in (document_id, revision_id, content_sha256, draft_file)):
        raise CoderInterfaceError("Exact revision options require InformationStore authority.")

    entry, effective_state_root, candidate, workflow = _resolve_context_and_workflow(
        repository_path=repository_path,
        state_root=state_root,
        milestone_root=milestone_root,
    )
    from ..procedure_release import inspect_procedure_release_preparation

    release_preparation = inspect_procedure_release_preparation(
        entry.path,
        candidate,
        workflow,
        repository_key=entry.key,
        repository_id=entry.repository_id,
    )
    if release_preparation.get("authorized"):
        raise CoderInterfaceError(
            "Closeout is unavailable while an authorized shared-procedure release is being prepared. "
            "Validate and publish the candidate bundle first, then recheck canonical integrity."
        )

    try:
        updated_workflow = record_milestone_event(
            repository_root=entry.path,
            state_root=effective_state_root,
            milestone_root=entry.milestone_root,
            candidate=candidate,
            event_type="closeout_submitted",
            actor_role="coder",
            summary=clean_summary,
            po_approved=False,
        )
    except WorkflowRecordError as exc:
        raise CoderInterfaceError(str(exc)) from exc

    recorded_event = updated_workflow["event_history"][-1]
    return {
        "event_id": recorded_event["event_id"],
        "milestone_id": candidate["milestone_id"],
        "phase_before": recorded_event["phase_before"],
        "phase_after": recorded_event["phase_after"],
        "evidence_path": recorded_event.get("evidence_path"),
        "summary": clean_summary,
        "next_action": updated_workflow["next_action"],
    }
