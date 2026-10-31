from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


VERIFICATION_EVENT_TYPE = "context_verification_recorded"
VERIFICATION_TRIGGERS = {
    "NEW",
    "HANDOFF",
    "AUTHORITY_TRANSITION",
    "RECOVERY",
}
MAX_VERIFICATION_RECORD_BYTES = 16_000
AUDIT_RECORD_LIMIT = 5
_SENSITIVE_PATH_PARTS = {
    ".aws",
    ".env",
    ".gnupg",
    ".ssh",
    "authorized_keys",
    "id_ed25519",
    "id_rsa",
}


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def snapshot_digest(snapshot: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(snapshot).encode("utf-8")).hexdigest()


def _redacted_path(path: str) -> str:
    parts = path.replace("\\", "/").split("/")
    lowered = [part.lower() for part in parts]
    if any(
        part in _SENSITIVE_PATH_PARTS
        or part.startswith(".env.")
        or any(word in part for word in ("credential", "password", "private", "secret", "token"))
        or part.endswith((".cer", ".crt", ".der", ".jks", ".key", ".p12", ".p8", ".pem", ".pfx"))
        for part in lowered
    ):
        return "<redacted-sensitive-path>"
    return path[:512]


def normalize_procedure_snapshot(
    *,
    canonical_release: str,
    manifest_sha256: str,
    installed_release: str | None,
    status: str,
    lock_sha256: str | None,
    overlay_path: str | None,
    overlay_valid: bool,
    overlay_sha256: str | None,
) -> dict[str, Any]:
    return {
        "inspection_status": "available",
        "canonical_release": canonical_release,
        "manifest_sha256": manifest_sha256,
        "installed_release": installed_release,
        "status": status,
        "lock_sha256": lock_sha256,
        "overlay_path": overlay_path,
        "overlay_valid": overlay_valid,
        "overlay_sha256": overlay_sha256,
    }


def normalize_approved_dispatches(
    dispatches: list[dict[str, Any]],
    milestone_id: str,
) -> list[dict[str, Any]]:
    return [
        {
            "dispatch_id": item["dispatch_id"],
            "type": item["type"],
            "milestone_id": item["milestone_id"],
            "approved_at": item["approved_at"],
            "related_event_id": item.get("related_event_id"),
            "body_sha256": hashlib.sha256(item["body"].encode("utf-8")).hexdigest(),
        }
        for item in reversed(dispatches)
        if item["status"] == "APPROVED"
        and item["type"] in {"INSTRUCTION", "RESPONSE"}
        and item.get("approved_milestone_id") == milestone_id
    ]


def collect_verification_sources(
    repository: Any,
    candidate: dict[str, Any],
    state_root: str | Path,
) -> dict[str, Any]:
    try:
        from ..procedure_governance import ProcedureGovernanceError, inspect_procedure, load_bundle

        bundle = load_bundle()
        procedure_status = inspect_procedure(repository, bundle)
        overlay = procedure_status.get("overlay", {})
        overlay_path = overlay.get("path")
        overlay_hash = None
        if overlay.get("valid") and isinstance(overlay_path, str):
            overlay_file = (Path(repository.path) / overlay_path).resolve(strict=True)
            if overlay_file.is_relative_to(Path(repository.path)) and overlay_file.is_file():
                overlay_hash = hashlib.sha256(overlay_file.read_bytes()).hexdigest()
        procedure = normalize_procedure_snapshot(
            canonical_release=bundle.release,
            manifest_sha256=bundle.manifest_sha256,
            installed_release=procedure_status.get("current_release"),
            status=procedure_status["status"],
            lock_sha256=procedure_status.get("lock_sha256"),
            overlay_path=overlay_path,
            overlay_valid=overlay.get("valid") is True,
            overlay_sha256=overlay_hash,
        )
    except (OSError, UnicodeError, ProcedureGovernanceError) as exc:
        procedure = {
            "inspection_status": "unavailable",
            "failure_type": type(exc).__name__,
        }

    from .git_state import WorkflowGitStateError, inspect_git_state

    try:
        git_state = inspect_git_state(str(repository.path))
    except WorkflowGitStateError as exc:
        git_state = None
        git_failure_type = type(exc).__name__
    else:
        git_failure_type = None

    from .architect_dispatches import ArchitectDispatchError, list_owner_dispatches

    try:
        dispatches = list_owner_dispatches(
            repository.path,
            state_root,
            repository.repository_id,
        )
        approved_dispatches = normalize_approved_dispatches(
            dispatches,
            candidate["milestone_id"],
        )
    except ArchitectDispatchError as exc:
        approved_dispatches = None
        dispatch_failure_type = type(exc).__name__
    else:
        dispatch_failure_type = None

    return {
        "procedure": procedure,
        "git_state": git_state,
        "git_failure_type": git_failure_type,
        "approved_dispatches": approved_dispatches,
        "dispatch_failure_type": dispatch_failure_type,
    }


def normalize_git_snapshot(
    git_state: dict[str, Any] | None,
    excluded_paths: set[str] | None = None,
) -> dict[str, Any]:
    if git_state is None:
        return {"inspection_status": "unavailable"}
    working_tree = git_state.get("working_tree", {})
    entries = working_tree.get("entries", [])
    excluded = excluded_paths or set()
    raw_entries = sorted(
        (
            {
                "kind": item.get("kind"),
                "xy": item.get("xy"),
                "path": item.get("path", ""),
            }
            for item in entries
            if isinstance(item, dict)
            and item.get("path") not in excluded
        ),
        key=lambda item: (item["path"], item["kind"] or "", item["xy"] or ""),
    )
    safe_entries = [
        {
            "kind": item["kind"],
            "xy": item["xy"],
            "path": _redacted_path(item["path"]),
        }
        for item in raw_entries
    ]
    kinds = {item["kind"] for item in safe_entries}
    staged = any(
        item["kind"] not in {"untracked", "unmerged"}
        and isinstance(item["xy"], str)
        and item["xy"][:1] not in {"", "."}
        for item in safe_entries
    )
    unstaged = any(
        item["kind"] == "untracked"
        or item["kind"] == "unmerged"
        or (
            isinstance(item["xy"], str)
            and len(item["xy"]) > 1
            and item["xy"][1] != "."
        )
        for item in safe_entries
    )
    if "unmerged" in kinds:
        working_tree_state = "conflicted"
    elif staged and unstaged:
        working_tree_state = "staged_and_unstaged"
    elif staged:
        working_tree_state = "staged_only"
    elif unstaged:
        working_tree_state = "unstaged_only"
    else:
        working_tree_state = "clean"
    serialized_entries = _canonical_json(safe_entries).encode("utf-8")
    return {
        "inspection_status": "available",
        "repository_root": git_state.get("repository_root"),
        "branch": git_state.get("branch"),
        "head": git_state.get("head"),
        "working_tree_state": working_tree_state,
        "is_clean": working_tree_state == "clean",
        "dirty_entry_count": len(safe_entries),
        "dirty_entries_sha256": hashlib.sha256(serialized_entries).hexdigest(),
        "dirty_entries": [
            {
                "kind": item["kind"],
                "xy": item["xy"],
                "path": item["path"],
            }
            for item in safe_entries[:10]
        ],
        "omitted_dirty_entry_count": max(0, len(safe_entries) - 10),
        "excluded_workflow_sidecar": sorted(excluded),
        "git_operation_in_progress": git_state.get("git_operation_in_progress"),
        "git_operations": git_state.get("git_operations", []),
    }


def build_authority_snapshot(
    *,
    repository: dict[str, Any],
    candidate: dict[str, Any],
    workflow: dict[str, Any],
    authorization: dict[str, Any],
    procedure: dict[str, Any],
    git_state: dict[str, Any] | None,
    approved_dispatches: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    questions = sorted(
        event["event_id"]
        for event in workflow.get("open_questions", [])
        if isinstance(event.get("event_id"), str)
    )
    bugs = sorted(
        event["event_id"]
        for event in workflow.get("open_bugs", [])
        if isinstance(event.get("event_id"), str)
    )
    approved_lock_ins = [
        {
            "event_id": event["event_id"],
            "summary": event["summary"],
            "timestamp": event["timestamp"],
        }
        for event in workflow.get("event_history", [])
        if event.get("event_type") == "lock_in_approved" and event.get("binding") is True
    ]
    test_evidence = [
        {
            "event_id": event["event_id"],
            "timestamp": event["timestamp"],
            "evidence_source": event.get("evidence_source"),
            "result": event.get("result"),
        }
        for event in workflow.get("event_history", [])
        if event.get("event_type") == "test_result_recorded"
    ]
    closeout_submissions = [
        {
            "event_id": event["event_id"],
            "timestamp": event["timestamp"],
            "evidence_path": event.get("evidence_path"),
        }
        for event in workflow.get("event_history", [])
        if event.get("event_type") == "closeout_submitted"
    ]
    dispatch_snapshot = (
        {
            "inspection_status": "available",
            "count": len(approved_dispatches),
            "sha256": snapshot_digest(approved_dispatches),
        }
        if approved_dispatches is not None
        else {"inspection_status": "unavailable"}
    )
    prompt_hash = workflow.get("prompt_identity")
    return {
        "repository": {
            "key": repository["key"],
            "id": repository["repository_id"],
            "root": repository["path"],
        },
        "milestone": {
            "id": candidate["milestone_id"],
            "prompt_path": candidate["prompt_path"],
            "prompt_sha256": prompt_hash,
            "lifecycle": workflow["phase"],
            "authorization": {
                "authorized": authorization["authorized"],
                "status": authorization["status"],
            },
            "open_conditions": {
                "blocking": workflow["phase"] == "WAITING",
                "waiting_category": workflow.get("waiting_category"),
                "open_question_count": len(questions),
                "open_question_ids": questions[:20],
                "open_question_ids_sha256": snapshot_digest(questions),
                "omitted_open_question_count": max(0, len(questions) - 20),
                "open_bug_count": len(bugs),
                "open_bug_ids": bugs[:20],
                "open_bug_ids_sha256": snapshot_digest(bugs),
                "omitted_open_bug_count": max(0, len(bugs) - 20),
            },
            "approved_lock_ins": {
                "count": len(approved_lock_ins),
                "sha256": snapshot_digest(approved_lock_ins),
            },
            "closeout": {
                "exists": candidate.get("closeout_exists") is True,
                "path": candidate.get("closeout_path"),
                "submission_count": len(closeout_submissions),
                "submissions_sha256": snapshot_digest(closeout_submissions),
            },
        },
        "workflow_evidence": {
            "test_evidence_provenance": "agent_reported",
            "test_result_count": len(test_evidence),
            "recent_test_event_ids": [item["event_id"] for item in test_evidence[-5:]],
            "test_evidence_sha256": snapshot_digest(test_evidence),
        },
        "approved_architect_dispatches": dispatch_snapshot,
        "procedure": procedure,
        "git": normalize_git_snapshot(
            git_state,
            {workflow["sidecar_path"]} if workflow.get("sidecar_path") else set(),
        ),
    }


def make_verification_record(
    *,
    trigger: str,
    snapshot: dict[str, Any],
    result: str,
    git_boundary_acknowledged: bool,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "trigger": trigger,
        "trigger_provenance": "agent_reported",
        "session_identity": "unverified",
        "lookup_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "lookup_source": "repoctl coder-verify",
        "result": result,
        "evidence_provenance": "repo_control_observed_local_state",
        "evidence_strength": "live_local_snapshot",
        "provider_delivery": "unverified",
        "git_boundary_acknowledged": git_boundary_acknowledged,
        "authority_snapshot_sha256": snapshot_digest(snapshot),
        "authority_snapshot": snapshot,
    }


def validate_verification_record(record: Any) -> bool:
    if not isinstance(record, dict):
        return False
    required = {
        "schema_version",
        "trigger",
        "trigger_provenance",
        "session_identity",
        "lookup_at",
        "lookup_source",
        "result",
        "evidence_provenance",
        "evidence_strength",
        "provider_delivery",
        "git_boundary_acknowledged",
        "authority_snapshot_sha256",
        "authority_snapshot",
    }
    if set(record) != required:
        return False
    if (
        type(record["schema_version"]) is not int
        or record["schema_version"] != 1
        or not isinstance(record["trigger"], str)
        or record["trigger"] not in VERIFICATION_TRIGGERS
        or record["trigger_provenance"] != "agent_reported"
        or record["session_identity"] != "unverified"
        or record["lookup_source"] != "repoctl coder-verify"
        or not isinstance(record["result"], str)
        or record["result"] not in {"VERIFIED", "BLOCKED", "PARTIAL_UNVERIFIED"}
        or record["evidence_provenance"] != "repo_control_observed_local_state"
        or record["evidence_strength"] != "live_local_snapshot"
        or record["provider_delivery"] != "unverified"
        or not isinstance(record["git_boundary_acknowledged"], bool)
        or not isinstance(record["authority_snapshot"], dict)
    ):
        return False
    if not isinstance(record["lookup_at"], str):
        return False
    try:
        parsed_time = datetime.fromisoformat(record["lookup_at"])
        encoded = _canonical_json(record).encode("utf-8")
    except (TypeError, ValueError):
        return False
    if parsed_time.utcoffset() is None or len(encoded) > MAX_VERIFICATION_RECORD_BYTES:
        return False
    snapshot = record["authority_snapshot"]
    required_snapshot_sections = {
        "repository",
        "milestone",
        "approved_architect_dispatches",
        "procedure",
        "git",
        "workflow_evidence",
    }
    if not required_snapshot_sections.issubset(snapshot):
        return False
    milestone = snapshot.get("milestone")
    repository = snapshot.get("repository")
    if (
        not isinstance(repository, dict)
        or any(not isinstance(repository.get(key), str) for key in ("key", "id", "root"))
        or not isinstance(milestone, dict)
        or not isinstance(milestone.get("id"), str)
        or not isinstance(milestone.get("prompt_path"), str)
        or not isinstance(milestone.get("prompt_sha256"), str)
        or not isinstance(milestone.get("lifecycle"), str)
        or not isinstance(milestone.get("authorization"), dict)
        or not isinstance(milestone.get("closeout"), dict)
        or not isinstance(milestone["authorization"].get("authorized"), bool)
        or not isinstance(snapshot.get("workflow_evidence"), dict)
        or not isinstance(snapshot.get("procedure"), dict)
        or not isinstance(snapshot.get("git"), dict)
        or not isinstance(snapshot.get("approved_architect_dispatches"), dict)
    ):
        return False
    if len(milestone["prompt_sha256"]) != 64 or any(
        character not in "0123456789abcdef"
        for character in milestone["prompt_sha256"]
    ):
        return False
    procedure = snapshot["procedure"]
    git = snapshot["git"]
    dispatches = snapshot["approved_architect_dispatches"]
    if record["result"] == "VERIFIED" and not (
        milestone["authorization"]["authorized"] is True
        and procedure.get("inspection_status") == "available"
        and procedure.get("status") == "CURRENT"
        and procedure.get("overlay_valid") is True
        and git.get("inspection_status") == "available"
        and dispatches.get("inspection_status") == "available"
        and record["git_boundary_acknowledged"] is True
    ):
        return False
    if record["result"] == "BLOCKED" and milestone["authorization"]["authorized"] is not False:
        return False
    if record["result"] == "PARTIAL_UNVERIFIED" and milestone["authorization"]["authorized"] is not True:
        return False
    if (
        not isinstance(record["authority_snapshot_sha256"], str)
        or record["authority_snapshot_sha256"] != snapshot_digest(snapshot)
    ):
        return False
    return True


def build_verification_coverage(
    events: list[dict[str, Any]],
    current_snapshot: dict[str, Any] | None,
) -> dict[str, Any]:
    verification_events = [
        event for event in events
        if event.get("event_type") == VERIFICATION_EVENT_TYPE
    ]
    records: list[dict[str, Any]] = []
    valid_count = 0
    invalid_count = 0
    current_count = 0
    stale_count = 0
    unverifiable_count = 0
    trigger_counts: dict[str, int] = {}
    for event in verification_events:
        record = event.get("context_verification")
        if (
            not validate_verification_record(record)
            or event.get("actor_role") != "coder"
            or event.get("po_approved") is not False
            or event.get("phase_before") != event.get("phase_after")
        ):
            invalid_count += 1
            freshness = "INVALID"
        else:
            valid_count += 1
            trigger_counts[record["trigger"]] = trigger_counts.get(record["trigger"], 0) + 1
            if current_snapshot is None:
                unverifiable_count += 1
                freshness = "UNVERIFIABLE"
            elif record["authority_snapshot_sha256"] == snapshot_digest(current_snapshot):
                current_count += 1
                freshness = "CURRENT"
            else:
                stale_count += 1
                freshness = "STALE"
        if len(records) == AUDIT_RECORD_LIMIT:
            records.pop(0)
        records.append(
            {
                "event_id": event.get("event_id"),
                "timestamp": event.get("timestamp"),
                "freshness": freshness,
                "record": record if freshness != "INVALID" else None,
            }
        )
    latest_freshness = records[-1]["freshness"] if records else "MISSING"
    status = {
        "MISSING": "MISSING",
        "CURRENT": "CURRENT",
        "STALE": "STALE",
        "UNVERIFIABLE": "UNVERIFIABLE",
        "INVALID": "INVALID",
    }[latest_freshness]
    return {
        "status": status,
        "observed_agent_reported_trigger_count": len(verification_events),
        "valid_record_count": valid_count,
        "invalid_record_count": invalid_count,
        "current_record_count": current_count,
        "stale_record_count": stale_count,
        "unverifiable_record_count": unverifiable_count,
        "observed_trigger_counts": trigger_counts,
        "returned_record_count": len(records),
        "omitted_record_count": max(0, len(verification_events) - len(records)),
        "records": records,
        "trigger_provenance": "agent_reported",
        "snapshot_provenance": "repo_control_observed_local_state",
        "provider_delivery": "unverified",
        "coverage_limitation": (
            "Only recorded agent-reported triggers are covered; session starts, identity, and host-hook "
            "delivery are not observable here, so missing records do not prove that no context started. "
            "Identical consecutive trigger/snapshot retries are deduplicated and are not a per-session count."
        ),
    }
