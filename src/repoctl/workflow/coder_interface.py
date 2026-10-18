from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ..scanner.core import DEFAULT_STATE_ROOT
from ..scanner.git_ops import ScanError, validate_git_worktree
from .architect_dispatches import ArchitectDispatchError, list_owner_dispatches
from ..web.repository_registry import RepositoryEntry, RepositoryRegistry, RepositoryRegistryError
from .lifecycle import (
    WorkflowRecordError,
    load_milestone_workflow,
    record_milestone_event,
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


CENTRAL_REPOSITORY_REGISTRY_PATH = Path(__file__).resolve().parents[3] / "ops" / "repositories.toml"


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


def _is_correction_mode(workflow: dict[str, Any]) -> bool:
    if workflow.get("phase") != "IN_PROGRESS":
        return False
    for event in reversed(workflow.get("event_history", [])):
        if event.get("event_type") == "correction_requested":
            return True
        if event.get("event_type") in {"implementation_started", "implementation_resumed"}:
            return False
    return False


def get_authorization_status(phase: str, is_correction: bool = False) -> dict[str, Any]:
    if phase == "PLANNING":
        return {
            "authorized": False,
            "can_preflight": False,
            "status": "NOT_AUTHORIZED",
            "message": "NO — milestone is in PLANNING; Product Owner prompt review and approval required before implementation.",
        }
    if phase == "READY":
        return {
            "authorized": False,
            "can_preflight": True,
            "status": "PREFLIGHT_ONLY",
            "message": "NO (preflight only) — prompt is READY; implementation begins only after explicit lifecycle start.",
        }
    if phase == "IN_PROGRESS":
        if is_correction:
            return {
                "authorized": True,
                "can_preflight": True,
                "status": "AUTHORIZED_CORRECTION",
                "message": "YES (correction mode) — work is limited to the requested bounded correction.",
            }
        return {
            "authorized": True,
            "can_preflight": True,
            "status": "AUTHORIZED",
            "message": "YES — implementation and validation are authorized.",
        }
    if phase == "WAITING":
        return {
            "authorized": False,
            "can_preflight": False,
            "status": "NOT_AUTHORIZED",
            "message": "NO — implementation is paused in WAITING; resolve recorded condition and Product Owner resume required.",
        }
    if phase == "REVIEW":
        return {
            "authorized": False,
            "can_preflight": False,
            "status": "NOT_AUTHORIZED",
            "message": "NO — implementation is under REVIEW; no new changes authorized unless Product Owner requests bounded correction.",
        }
    if phase == "CLOSED":
        return {
            "authorized": False,
            "can_preflight": False,
            "status": "NOT_AUTHORIZED",
            "message": "NO — milestone is CLOSED; no further implementation authorized under this milestone.",
        }
    return {
        "authorized": False,
        "can_preflight": False,
        "status": "UNKNOWN",
        "message": f"NO — unknown phase: {phase}",
    }


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


def get_coder_status(
    repository_path: str | Path | None = None,
    state_root: str | Path | None = None,
    milestone_root: str | None = None,
) -> dict[str, Any]:
    entry, _, candidate, workflow = _resolve_context_and_workflow(
        repository_path=repository_path,
        state_root=state_root,
        milestone_root=milestone_root,
    )

    phase = workflow["phase"]
    is_corr = _is_correction_mode(workflow)
    auth = get_authorization_status(phase, is_corr)

    binding_lock_ins = [
        event
        for event in workflow.get("event_history", [])
        if event.get("event_type") == "lock_in_approved" and event.get("binding") is True
    ]
    latest_lock_in = binding_lock_ins[-1] if binding_lock_ins else None

    prompt_path = entry.path / candidate["prompt_path"]
    prompt_size = prompt_path.stat().st_size if prompt_path.is_file() else 0

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
        "authorization": auth,
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
        },
    }


def get_coder_context(
    repository_path: str | Path | None = None,
    state_root: str | Path | None = None,
    milestone_root: str | None = None,
) -> dict[str, Any]:
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
    context_data["approved_architect_dispatches"] = [
        {
            "alias": f"A{index}",
            "dispatch_id": item["dispatch_id"],
            "type": item["type"],
            "milestone_id": item["milestone_id"],
            "approved_at": item["approved_at"],
            "related_event_id": item.get("related_event_id"),
            "body": item["body"],
        }
        for index, item in enumerate(applicable_dispatches, start=1)
    ]
    context_data["prompt"] = {
        "path": prompt_rel_path,
        "content": prompt_content,
        "size_bytes": len(prompt_content.encode("utf-8")),
    }
    return context_data


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
        f"Lifecycle:      {milestone['phase']}",
        f"Authorization:  {auth['message']}",
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
                    dispatch["body"],
                    "",
                ]
            )
    else:
        lines.append("None.")
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
) -> dict[str, Any]:
    if not summary or not summary.strip():
        raise CoderInterfaceError("Bug summary must not be empty.")
    clean_summary = summary.strip()

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


def submit_coder_test_result(
    summary: str,
    source: str,
    result: str,
    repository_path: str | Path | None = None,
    state_root: str | Path | None = None,
    milestone_root: str | None = None,
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


def submit_coder_closeout(
    summary: str | None = None,
    repository_path: str | Path | None = None,
    state_root: str | Path | None = None,
    milestone_root: str | None = None,
) -> dict[str, Any]:
    clean_summary = summary.strip() if summary and summary.strip() else "Closeout submitted for review"

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
