from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from tempfile import mkdtemp
from typing import Any

from ..scanner.core import DEFAULT_STATE_ROOT
from ..scanner.util import make_repository_id, write_json_deterministic
from .errors import WorkflowReasonError
from .git_state import WorkflowGitStateError, inspect_git_state
from .stage_plan import (
    STAGE_PLAN_REQUIRED_FILES,
    _derive_stage_plan_id,
    _enumerate_stage_candidates,
    _parse_diff_raw_output,
    _raw_record_bytes,
    _run_git_bytes,
)

STAGE_EXECUTION_REQUIRED_FILES = {"execution.json", "execution.md"}


def _locate_stage_plan_dir(root: Path, repository_id: str, plan_id: str) -> tuple[Path, str]:
    expected = root / repository_id / "workflow" / "stage_plans" / plan_id
    if expected.exists() and expected.is_dir():
        return expected, "selected"

    for repo_dir in root.iterdir():
        if not repo_dir.is_dir() or repo_dir.name == repository_id:
            continue
        candidate = repo_dir / "workflow" / "stage_plans" / plan_id
        if candidate.exists() and candidate.is_dir():
            return candidate, "other_repository"
    return expected, "missing"


def _load_stage_plan(plan_dir: Path, expected_repo_id: str, expected_repo_root: Path) -> dict[str, Any]:
    if not plan_dir.exists() or not plan_dir.is_dir():
        raise WorkflowReasonError("plan_not_found", f"stage plan not found: {plan_dir.name}")

    names = {child.name for child in plan_dir.iterdir()}
    if names != STAGE_PLAN_REQUIRED_FILES:
        raise WorkflowReasonError("plan_integrity_failed", "stage plan artifact set is incomplete or unexpected")

    payload = json.loads((plan_dir / "plan.json").read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1:
        raise WorkflowReasonError("plan_integrity_failed", "unsupported stage plan schema version")
    plan_id = payload.get("plan_id")
    if not isinstance(plan_id, str):
        raise WorkflowReasonError("plan_integrity_failed", "stage plan id missing")

    base = dict(payload)
    base.pop("plan_id", None)
    recomputed = _derive_stage_plan_id(base)
    if recomputed != plan_id:
        raise WorkflowReasonError("plan_integrity_failed", "stage plan id/content integrity mismatch")
    if payload.get("repository_id") != expected_repo_id or payload.get("repository_root") != str(expected_repo_root):
        raise WorkflowReasonError("repository_mismatch", "stage plan repository does not match selected repository")
    return payload


def _current_staged_raw(repo_root: Path) -> bytes:
    return _run_git_bytes(
        repo_root,
        ["diff-index", "--cached", "--raw", "--no-renames", "--abbrev=40", "-z", "HEAD", "--"],
        error_code="post_stage_verification_failed",
        error_prefix="unable to inspect staged index state",
    )


def _current_state_summary(repo_root: Path) -> str:
    try:
        git_state = inspect_git_state(str(repo_root))
    except WorkflowGitStateError as exc:
        return f"unable to inspect current state: {exc}"

    wt = git_state["working_tree"]
    return (
        f"workflow_state={git_state['workflow_state']}, staged={wt['staged']['count']}, "
        f"unstaged={wt['unstaged']['count']}, untracked={wt['untracked']['count']}, unmerged={wt['unmerged']['count']}"
    )


def _check_index_lock(repo_root: Path) -> None:
    lock_path_text = _run_git_bytes(
        repo_root,
        ["rev-parse", "--git-path", "index.lock"],
        error_code="git_index_locked",
        error_prefix="unable to resolve git index lock path",
    ).decode("utf-8", errors="strict").strip()
    lock_path = Path(lock_path_text)
    if not lock_path.is_absolute():
        lock_path = (repo_root / lock_path).resolve()
    if lock_path.exists():
        raise WorkflowReasonError("git_index_locked", "git index is locked by another process")


def _assert_prestage_state(plan: dict[str, Any], git_state: dict[str, Any], current_candidates: dict[str, Any]) -> None:
    branch = git_state["branch"]
    wt = git_state["working_tree"]
    if branch["state"] != "attached":
        raise WorkflowReasonError("detached_head", "stage execution requires an attached branch")
    if plan["branch"]["name"] != branch["name"]:
        raise WorkflowReasonError("branch_changed", "branch changed since stage plan preparation")
    if plan["head"] != git_state["head"]:
        raise WorkflowReasonError("head_changed", "HEAD changed since stage plan preparation")
    if wt["staged"]["count"] > 0:
        raise WorkflowReasonError("staged_changes_present", "existing staged changes present before stage execution")
    if wt["unmerged"]["count"] > 0:
        raise WorkflowReasonError("conflicts_present", "conflicts present before stage execution")
    if git_state["git_operation_in_progress"]:
        raise WorkflowReasonError("git_operation_in_progress", "active git operation present before stage execution")
    if current_candidates["stage_candidate_fingerprint"] != plan["stage_candidate_fingerprint"]:
        raise WorkflowReasonError("worktree_state_changed", "worktree candidate state changed since stage plan preparation")


def _run_git_add(repo_root: Path, args: list[str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", str(repo_root), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def _derive_execution_id(base_payload: dict[str, Any]) -> str:
    canonical = json.dumps(base_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "stage-exec--" + hashlib.sha256(canonical).hexdigest()[:16]


def _build_execution_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Stage Execution Evidence",
        "",
        f"- Execution id: {payload['execution_id']}",
        f"- Plan id: {payload['plan_id']}",
        f"- Repository root: {payload['repository_root']}",
        f"- Repository id: {payload['repository_id']}",
        f"- Branch: {payload['branch']}",
        f"- HEAD before: {payload['head_before']}",
        f"- HEAD after: {payload['head_after']}",
        f"- Resulting workflow state: {payload['resulting_workflow_state']}",
        "",
        "## Staged Summary",
    ]
    for item in payload["staged_summary"]:
        lines.append(f"- {item['status']} {item['path']}")
    return "\n".join(lines) + "\n"


def _verify_existing_stage_execution(execution_dir: Path, expected_id: str) -> dict[str, Any]:
    names = {child.name for child in execution_dir.iterdir()}
    if names != STAGE_EXECUTION_REQUIRED_FILES:
        raise WorkflowReasonError("stage_succeeded_audit_failed", "stage execution artifact set is incomplete or unexpected")

    payload = json.loads((execution_dir / "execution.json").read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1:
        raise WorkflowReasonError("stage_succeeded_audit_failed", "unsupported stage execution schema version")
    if payload.get("execution_id") != expected_id:
        raise WorkflowReasonError("stage_succeeded_audit_failed", "stage execution id mismatch")
    base = dict(payload)
    base.pop("execution_id", None)
    recomputed = _derive_execution_id(base)
    if recomputed != expected_id:
        raise WorkflowReasonError("stage_succeeded_audit_failed", "stage execution id/content integrity mismatch")
    return payload


def _publish_stage_execution(execution_root: Path, payload: dict[str, Any], markdown: str, *, force_failure: bool) -> tuple[str, bool]:
    execution_root.mkdir(parents=True, exist_ok=True)
    execution_id = payload["execution_id"]
    final_dir = execution_root / execution_id
    temp_dir = Path(mkdtemp(prefix="stage-exec-tmp-", dir=str(execution_root)))
    try:
        write_json_deterministic(temp_dir / "execution.json", payload)
        (temp_dir / "execution.md").write_text(markdown, encoding="utf-8", newline="\n")
        if force_failure:
            raise WorkflowReasonError("stage_succeeded_audit_failed", "controlled stage execution audit failure")
        _verify_existing_stage_execution(temp_dir, execution_id)

        if final_dir.exists():
            _verify_existing_stage_execution(final_dir, execution_id)
            if (final_dir / "execution.json").read_bytes() != (temp_dir / "execution.json").read_bytes() or (
                final_dir / "execution.md"
            ).read_bytes() != (temp_dir / "execution.md").read_bytes():
                raise WorkflowReasonError("stage_succeeded_audit_failed", "existing stage execution content mismatch for identical execution id")
            shutil.rmtree(temp_dir)
            return str(final_dir), True

        temp_dir.rename(final_dir)
        return str(final_dir), False
    except Exception:
        if temp_dir.exists():
            shutil.rmtree(temp_dir)
        raise


def _verify_post_stage(
    repo_root: Path,
    plan: dict[str, Any],
    before_head: str,
    *,
    force_failure: bool,
) -> dict[str, Any]:
    try:
        git_state = inspect_git_state(str(repo_root))
    except WorkflowGitStateError as exc:
        raise WorkflowReasonError("post_stage_verification_failed", str(exc)) from exc

    if git_state["head"] != before_head:
        raise WorkflowReasonError("post_stage_verification_failed", "HEAD changed during stage execution")
    branch = git_state["branch"]
    if branch["state"] != "attached" or branch["name"] != plan["branch"]["name"]:
        raise WorkflowReasonError("post_stage_verification_failed", "branch changed during stage execution")

    staged_raw = _current_staged_raw(repo_root)
    if not staged_raw:
        raise WorkflowReasonError("post_stage_verification_failed", "no staged changes present after stage execution")
    parsed_records = _parse_diff_raw_output(staged_raw)
    actual_raw_records = sorted(
        [
            (
                f":{record['old_mode']} {record['new_mode']} {record['old_oid']} {record['new_oid']} {record['status']}"
                .encode("utf-8")
                + b"\t"
                + record["path"].encode("utf-8")
            )
            for record in parsed_records
        ]
    )
    expected_raw_records = sorted([_raw_record_bytes(record) for record in plan["candidate_records"]])
    if actual_raw_records != expected_raw_records:
        raise WorkflowReasonError("post_stage_verification_failed", "staged delta does not match approved plan")
    parts = bytearray(b"repoctl-staged-delta-v1\0")
    for record in actual_raw_records:
        parts.extend(record)
        parts.extend(b"\0")
    staged_fingerprint = hashlib.sha256(bytes(parts)).hexdigest()

    wt = git_state["working_tree"]
    if wt["staged"]["count"] != plan["candidate_record_count"]:
        raise WorkflowReasonError("post_stage_verification_failed", "staged path count does not match approved plan")
    if wt["unstaged"]["count"] != 0:
        raise WorkflowReasonError("post_stage_verification_failed", "unstaged changes remain after stage execution")
    if wt["untracked"]["count"] != 0:
        raise WorkflowReasonError("post_stage_verification_failed", "untracked files remain after stage execution")
    if wt["unmerged"]["count"] != 0:
        raise WorkflowReasonError("post_stage_verification_failed", "conflicts present after stage execution")
    if git_state["git_operation_in_progress"]:
        raise WorkflowReasonError("post_stage_verification_failed", "git operation active after stage execution")
    if git_state["workflow_state"] != "staged_only":
        raise WorkflowReasonError("post_stage_verification_failed", "resulting workflow state is not staged_only")

    actual_staged_paths = sorted(wt["staged"]["paths"], key=lambda p: p.encode("utf-8"))
    expected_paths = sorted([record["path"] for record in plan["candidate_records"]], key=lambda p: p.encode("utf-8"))
    if actual_staged_paths != expected_paths:
        raise WorkflowReasonError("post_stage_verification_failed", "actual staged path set does not match approved plan")

    if force_failure:
        raise WorkflowReasonError("post_stage_verification_failed", "controlled post-stage verification failure")

    return {
        "head_unchanged": True,
        "branch_unchanged": True,
        "staged_changes_present": True,
        "staged_path_set_matches_plan": True,
        "staged_object_identities_match_plan": True,
        "staged_modes_match_plan": True,
        "no_unstaged_changes_remain": True,
        "no_untracked_changes_remain": True,
        "no_conflicts": True,
        "no_git_operation": True,
        "workflow_state": git_state["workflow_state"],
        "resulting_staged_delta_fingerprint": plan["expected_staged_delta_fingerprint"],
    }


def execute_prepared_stage(
    repository_path: str,
    plan_id: str,
    approve: bool,
    state_root: Path | None = None,
    *,
    _test_force_post_verify_failure: bool = False,
    _test_force_audit_failure: bool = False,
) -> dict[str, Any]:
    if not approve:
        raise WorkflowReasonError("approval_required", "explicit --approve is required for stage execution")

    root = (state_root or DEFAULT_STATE_ROOT).expanduser()
    root.mkdir(parents=True, exist_ok=True)

    try:
        git_state = inspect_git_state(repository_path)
    except WorkflowGitStateError as exc:
        raise WorkflowReasonError("worktree_state_changed", str(exc)) from exc

    repo_root = Path(git_state["repository_root"])
    repo_id = make_repository_id(repo_root)
    plan_dir, location = _locate_stage_plan_dir(root, repo_id, plan_id)
    if location == "other_repository":
        raise WorkflowReasonError("repository_mismatch", "stage plan repository does not match selected repository")
    if location == "missing":
        raise WorkflowReasonError("plan_not_found", f"stage plan not found: {plan_id}")
    plan = _load_stage_plan(plan_dir, repo_id, repo_root)

    _check_index_lock(repo_root)
    current_candidates = _enumerate_stage_candidates(repo_root, git_state)
    _assert_prestage_state(plan, git_state, current_candidates)

    before_head = git_state["head"]
    before_staged_raw = _current_staged_raw(repo_root)

    tracked_paths = [record["path"] for record in plan["candidate_records"] if record["source"] == "tracked"]
    untracked_paths = [record["path"] for record in plan["candidate_records"] if record["source"] == "untracked"]

    staged_any = False
    for args in (
        ["add", "--update", "--", *tracked_paths] if tracked_paths else None,
        ["add", "--", *untracked_paths] if untracked_paths else None,
    ):
        if args is None:
            continue
        proc = _run_git_add(repo_root, args)
        if proc.returncode != 0:
            after_failure_raw = _current_staged_raw(repo_root)
            stderr = proc.stderr.decode("utf-8", errors="replace").strip()
            if after_failure_raw != before_staged_raw:
                raise WorkflowReasonError(
                    "git_stage_failed_after_mutation",
                    f"git add failed after index mutation: {stderr}; current state: {_current_state_summary(repo_root)}",
                )
            raise WorkflowReasonError("git_stage_failed", f"git add failed without index mutation: {stderr}")
        staged_any = True

    if not staged_any:
        raise WorkflowReasonError("git_stage_failed", "no stage command was executed")

    try:
        verification = _verify_post_stage(
            repo_root,
            plan,
            before_head,
            force_failure=_test_force_post_verify_failure,
        )
    except WorkflowReasonError as exc:
        raise WorkflowReasonError(
            "post_stage_verification_failed",
            f"post-stage verification failed after staging; current state: {_current_state_summary(repo_root)}; detail: {exc.safe_message}",
        ) from exc

    base_payload = {
        "schema_version": 1,
        "plan_id": plan["plan_id"],
        "repository_id": repo_id,
        "repository_root": str(repo_root),
        "branch": plan["branch"]["name"],
        "head_before": before_head,
        "head_after": before_head,
        "candidate_fingerprint": plan["stage_candidate_fingerprint"],
        "resulting_staged_fingerprint": verification["resulting_staged_delta_fingerprint"],
        "staged_summary": plan["candidate_summary"],
        "verification": verification,
        "resulting_workflow_state": verification["workflow_state"],
        "remote_refresh_performed": False,
        "commit_performed": False,
        "push_performed": False,
    }
    execution_id = _derive_execution_id(base_payload)
    execution_payload = dict(base_payload)
    execution_payload["execution_id"] = execution_id
    markdown = _build_execution_markdown(execution_payload)

    execution_root = root / repo_id / "workflow" / "stage_executions"
    try:
        execution_dir, reused_existing = _publish_stage_execution(
            execution_root,
            execution_payload,
            markdown,
            force_failure=_test_force_audit_failure,
        )
    except WorkflowReasonError as exc:
        raise WorkflowReasonError(
            "stage_succeeded_audit_failed",
            f"staging succeeded but execution audit write failed; current state: {_current_state_summary(repo_root)}; detail: {exc.safe_message}",
        ) from exc

    return {
        "plan_id": plan["plan_id"],
        "execution_id": execution_id,
        "repository_id": repo_id,
        "repository_root": str(repo_root),
        "branch": plan["branch"]["name"],
        "head_before": before_head,
        "head_after": before_head,
        "candidate_record_count": plan["candidate_record_count"],
        "staged_summary": plan["candidate_summary"],
        "execution_dir": execution_dir,
        "reused_existing": reused_existing,
    }