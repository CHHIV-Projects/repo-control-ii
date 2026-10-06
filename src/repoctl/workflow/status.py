from __future__ import annotations

import json
import shutil
from pathlib import Path
from tempfile import mkdtemp
from typing import Any, Callable

from ..scanner.core import DEFAULT_STATE_ROOT, run_scan_with_artifacts
from ..scanner.util import make_repository_id, write_json_deterministic
from ..snapshot.manager import SCAN_ARTIFACTS, _verify_existing_snapshot, derive_snapshot_id
from .git_state import WorkflowGitStateError, inspect_git_state

REQUIRED_STATUS_KEYS = {
    "schema_version",
    "repository_id",
    "repository_root",
    "head",
    "branch",
    "upstream",
    "remote_refresh_performed",
    "working_tree",
    "workflow_state",
    "git_operation_in_progress",
    "git_operations",
    "mutation_preconditions",
    "current_snapshot_id_candidate",
    "matching_snapshot_exists",
    "matching_snapshot_id",
}


class WorkflowError(RuntimeError):
    pass


def _build_mutation_preconditions(status_payload: dict[str, Any]) -> dict[str, bool]:
    working_tree = status_payload["working_tree"]
    upstream = status_payload["upstream"]
    return {
        "branch_attached": status_payload["branch"]["state"] == "attached",
        "staged_changes_present": working_tree["staged"]["count"] > 0,
        "unstaged_changes_present": working_tree["unstaged"]["count"] > 0,
        "untracked_changes_present": working_tree["untracked"]["count"] > 0,
        "unmerged_entries_present": working_tree["unmerged"]["count"] > 0,
        "git_operation_in_progress": status_payload["git_operation_in_progress"],
        "upstream_configured": upstream["configured"],
        "upstream_divergence_available": upstream["divergence_state"] == "available",
    }


def _build_status_markdown(status_payload: dict[str, Any]) -> str:
    branch_name = status_payload["branch"]["name"] if status_payload["branch"]["state"] == "attached" else "(detached)"
    upstream_ref = status_payload["upstream"]["ref"] if status_payload["upstream"]["ref"] else "(none)"
    relation = status_payload["upstream"]["relation"] if status_payload["upstream"]["relation"] else "(unavailable)"
    ahead = status_payload["upstream"]["ahead"] if status_payload["upstream"]["ahead"] is not None else "(unavailable)"
    behind = status_payload["upstream"]["behind"] if status_payload["upstream"]["behind"] is not None else "(unavailable)"

    operations = status_payload["git_operations"]
    operation_text = ", ".join(operations) if operations else "none"

    wt = status_payload["working_tree"]
    preconditions = status_payload["mutation_preconditions"]

    lines = [
        "# Milestone Git Status",
        "",
        "## Repository",
        f"- Repository root: {status_payload['repository_root']}",
        f"- Repository id: {status_payload['repository_id']}",
        "",
        "## Branch and HEAD",
        f"- Branch state: {status_payload['branch']['state']}",
        f"- Branch name: {branch_name}",
        f"- HEAD: {status_payload['head']}",
        "",
        "## Upstream",
        f"- Upstream configured: {status_payload['upstream']['configured']}",
        f"- Upstream ref: {upstream_ref}",
        f"- Divergence state: {status_payload['upstream']['divergence_state']}",
        f"- Unavailable reason: {status_payload['upstream']['unavailable_reason']}",
        f"- Local upstream relation: {relation}",
        f"- Ahead: {ahead}",
        f"- Behind: {behind}",
        "- No Git fetch is performed.",
        "- Ahead/behind values describe locally available Git refs and may not reflect the current state of the remote server.",
        "",
        "## Working Tree",
        f"- Workflow state: {status_payload['workflow_state']}",
        f"- Staged count: {wt['staged']['count']}",
        f"- Unstaged count: {wt['unstaged']['count']}",
        f"- Untracked count: {wt['untracked']['count']}",
        f"- Unmerged count: {wt['unmerged']['count']}",
        f"- Staged paths: {', '.join(wt['staged']['paths']) if wt['staged']['paths'] else '(none)'}",
        f"- Unstaged paths: {', '.join(wt['unstaged']['paths']) if wt['unstaged']['paths'] else '(none)'}",
        f"- Untracked paths: {', '.join(wt['untracked']['paths']) if wt['untracked']['paths'] else '(none)'}",
        f"- Unmerged paths: {', '.join(wt['unmerged']['paths']) if wt['unmerged']['paths'] else '(none)'}",
        "",
        "## Active Git Operations",
        f"- Operation in progress: {status_payload['git_operation_in_progress']}",
        f"- Operations: {operation_text}",
        "",
        "## Mutation Preconditions",
        f"- branch_attached: {preconditions['branch_attached']}",
        f"- staged_changes_present: {preconditions['staged_changes_present']}",
        f"- unstaged_changes_present: {preconditions['unstaged_changes_present']}",
        f"- untracked_changes_present: {preconditions['untracked_changes_present']}",
        f"- unmerged_entries_present: {preconditions['unmerged_entries_present']}",
        f"- git_operation_in_progress: {preconditions['git_operation_in_progress']}",
        f"- upstream_configured: {preconditions['upstream_configured']}",
        f"- upstream_divergence_available: {preconditions['upstream_divergence_available']}",
        "",
        "## Repo Control Plane Evidence",
        f"- Current snapshot candidate: {status_payload['current_snapshot_id_candidate']}",
        f"- Matching snapshot exists: {status_payload['matching_snapshot_exists']}",
        f"- Matching snapshot id: {status_payload['matching_snapshot_id']}",
        "",
        "## Limitations",
        "- No Git fetch was performed.",
        "- Upstream divergence reflects locally available refs only and may be stale versus the remote server.",
        "- No Git mutation was performed.",
        "- No commit or push approval decision is made by this command.",
        "",
    ]
    return "\n".join(lines)


def _validate_status_payload(payload: dict[str, Any]) -> None:
    missing = REQUIRED_STATUS_KEYS - set(payload)
    if missing:
        missing_str = ", ".join(sorted(missing))
        raise WorkflowError(f"workflow status payload missing required keys: {missing_str}")


def _publish_status_pair(
    workflow_root: Path,
    status_payload: dict[str, Any],
    status_markdown: str,
    fail_hook: Callable[[str], None] | None = None,
) -> dict[str, str]:
    workflow_root.mkdir(parents=True, exist_ok=True)
    staging_dir = Path(mkdtemp(prefix="workflow-status-tmp-", dir=str(workflow_root)))

    final_json = workflow_root / "status.json"
    final_md = workflow_root / "status.md"

    backup_json_bytes = final_json.read_bytes() if final_json.exists() else None
    backup_md_bytes = final_md.read_bytes() if final_md.exists() else None

    try:
        staged_json = staging_dir / "status.json"
        staged_md = staging_dir / "status.md"
        write_json_deterministic(staged_json, status_payload)
        staged_md.write_text(status_markdown, encoding="utf-8", newline="\n")

        json.loads(staged_json.read_text(encoding="utf-8"))
        if not staged_md.read_text(encoding="utf-8"):
            raise WorkflowError("status markdown rendering produced empty content")

        staged_json.replace(final_json)
        if fail_hook:
            fail_hook("after_status_json_replace")
        staged_md.replace(final_md)
        if fail_hook:
            fail_hook("after_status_md_replace")
    except Exception:
        if backup_json_bytes is not None and backup_md_bytes is not None:
            final_json.write_bytes(backup_json_bytes)
            final_md.write_bytes(backup_md_bytes)
        elif backup_json_bytes is not None:
            final_json.write_bytes(backup_json_bytes)
            if final_md.exists():
                final_md.unlink()
        elif backup_md_bytes is not None:
            final_md.write_bytes(backup_md_bytes)
            if final_json.exists():
                final_json.unlink()
        else:
            if final_json.exists():
                final_json.unlink()
            if final_md.exists():
                final_md.unlink()
        raise
    finally:
        if staging_dir.exists():
            shutil.rmtree(staging_dir)

    return {
        "status_json": str(final_json),
        "status_md": str(final_md),
    }


def _calculate_snapshot_candidate(
    repository_path: str,
    repository_id: str,
    state_root: Path,
) -> tuple[str, bool, str | None]:
    scan_result = run_scan_with_artifacts(repository_path, state_root=state_root)
    scan_output = Path(scan_result["output_dir"])
    artifact_bytes = {name: (scan_output / name).read_bytes() for name in SCAN_ARTIFACTS}
    candidate_id = derive_snapshot_id(artifact_bytes)

    snapshot_dir = state_root / repository_id / "snapshots" / candidate_id
    if not snapshot_dir.exists():
        return candidate_id, False, None

    try:
        _verify_existing_snapshot(snapshot_dir, repository_id)
    except Exception as exc:
        raise WorkflowError(f"matching snapshot integrity failure: {exc}") from exc

    return candidate_id, True, candidate_id


def generate_milestone_status(repository_path: str, state_root: Path | None = None) -> dict[str, Any]:
    root = (state_root or DEFAULT_STATE_ROOT).expanduser()
    root.mkdir(parents=True, exist_ok=True)

    try:
        git_state = inspect_git_state(repository_path)
    except WorkflowGitStateError as exc:
        raise WorkflowError(str(exc)) from exc

    repository_root = Path(git_state["repository_root"])
    repository_id = make_repository_id(repository_root)

    candidate_id, matching_exists, matching_id = _calculate_snapshot_candidate(
        repository_path=str(repository_root),
        repository_id=repository_id,
        state_root=root,
    )

    status_payload: dict[str, Any] = {
        "schema_version": 1,
        "repository_id": repository_id,
        "repository_root": str(repository_root),
        "head": git_state["head"],
        "branch": git_state["branch"],
        "upstream": git_state["upstream"],
        "remote_refresh_performed": False,
        "working_tree": git_state["working_tree"],
        "workflow_state": git_state["workflow_state"],
        "git_operation_in_progress": git_state["git_operation_in_progress"],
        "git_operations": git_state["git_operations"],
        "current_snapshot_id_candidate": candidate_id,
        "matching_snapshot_exists": matching_exists,
        "matching_snapshot_id": matching_id,
    }
    status_payload["mutation_preconditions"] = _build_mutation_preconditions(status_payload)

    _validate_status_payload(status_payload)
    status_markdown = _build_status_markdown(status_payload)

    workflow_root = root / repository_id / "workflow"
    publish_result = _publish_status_pair(workflow_root, status_payload, status_markdown)

    return {
        "repository_root": str(repository_root),
        "repository_id": repository_id,
        "workflow_state": status_payload["workflow_state"],
        "head": status_payload["head"],
        "branch": status_payload["branch"],
        "upstream": status_payload["upstream"],
        "working_tree": status_payload["working_tree"],
        "git_operation_in_progress": status_payload["git_operation_in_progress"],
        "git_operations": status_payload["git_operations"],
        "remote_refresh_performed": status_payload["remote_refresh_performed"],
        "current_snapshot_id_candidate": status_payload["current_snapshot_id_candidate"],
        "matching_snapshot_exists": status_payload["matching_snapshot_exists"],
        "matching_snapshot_id": status_payload["matching_snapshot_id"],
        "status_json": publish_result["status_json"],
        "status_md": publish_result["status_md"],
    }
