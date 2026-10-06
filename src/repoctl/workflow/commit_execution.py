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
from .commit_plan import _derive_plan_id, _staged_delta
from .errors import WorkflowReasonError
from .git_state import WorkflowGitStateError, _GIT_OPERATION_MARKERS, inspect_git_state
from .status import _calculate_snapshot_candidate

EXECUTION_REQUIRED_FILES = {"execution.json", "execution.md"}
PLAN_REQUIRED_FILES = {"plan.json", "plan.md"}


def _run_git_text(repo_root: Path, args: list[str], *, check: bool = True, env: dict[str, str] | None = None) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env=env,
    )
    if check and proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace").strip()
        raise WorkflowReasonError("git_commit_failed", f"git command failed: {' '.join(args)}: {stderr}")
    return proc.stdout.decode("utf-8", errors="strict")


def _run_git_bytes(repo_root: Path, args: list[str]) -> bytes:
    proc = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace").strip()
        raise WorkflowReasonError("post_commit_verification_failed", f"git command failed: {' '.join(args)}: {stderr}")
    return proc.stdout


def _load_plan(plan_dir: Path, expected_repo_id: str, expected_repo_root: Path) -> dict[str, Any]:
    if not plan_dir.exists() or not plan_dir.is_dir():
        raise WorkflowReasonError("plan_not_found", f"commit plan not found: {plan_dir.name}")

    names = {child.name for child in plan_dir.iterdir()}
    if names != PLAN_REQUIRED_FILES:
        raise WorkflowReasonError("plan_integrity_failed", "plan artifact set is incomplete or unexpected")

    payload = json.loads((plan_dir / "plan.json").read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1:
        raise WorkflowReasonError("plan_integrity_failed", "unsupported plan schema version")

    plan_id = payload.get("plan_id")
    if not isinstance(plan_id, str):
        raise WorkflowReasonError("plan_integrity_failed", "plan id missing")

    base = dict(payload)
    base.pop("plan_id", None)
    recomputed = _derive_plan_id(base)
    if recomputed != plan_id:
        raise WorkflowReasonError("plan_integrity_failed", "plan id/content integrity mismatch")

    if payload.get("repository_id") != expected_repo_id or payload.get("repository_root") != str(expected_repo_root):
        raise WorkflowReasonError("repository_mismatch", "plan repository does not match selected repository")

    return payload


def _locate_plan_dir(root: Path, repository_id: str, plan_id: str) -> tuple[Path, str]:
    expected = root / repository_id / "workflow" / "commit_plans" / plan_id
    if expected.exists() and expected.is_dir():
        return expected, "selected"

    for repo_dir in root.iterdir():
        if not repo_dir.is_dir() or repo_dir.name == repository_id:
            continue
        candidate = repo_dir / "workflow" / "commit_plans" / plan_id
        if candidate.exists() and candidate.is_dir():
            return candidate, "other_repository"

    return expected, "missing"


def _find_executable_hooks(repo_root: Path) -> list[str]:
    hooks = ["pre-commit", "prepare-commit-msg", "commit-msg", "post-commit"]
    active: list[str] = []
    hooks_dir = Path(_run_git_text(repo_root, ["rev-parse", "--git-path", "hooks"]).strip())
    if not hooks_dir.is_absolute():
        hooks_dir = (repo_root / hooks_dir).resolve()

    for hook in hooks:
        hook_path = hooks_dir / hook
        if hook_path.exists() and os.access(hook_path, os.X_OK):
            active.append(hook)
    return active


def _enforce_hook_boundary(repo_root: Path) -> None:
    proc = subprocess.run(
        ["git", "-C", str(repo_root), "config", "--get", "core.hooksPath"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode == 0 and proc.stdout.decode("utf-8", errors="strict").strip():
        raise WorkflowReasonError("unsupported_git_hooks", "custom core.hooksPath is not supported for milestone commit execution")

    active_hooks = _find_executable_hooks(repo_root)
    if active_hooks:
        joined = ", ".join(active_hooks)
        raise WorkflowReasonError("unsupported_git_hooks", f"executable commit hooks present: {joined}")


def _enforce_git_identity(repo_root: Path) -> None:
    author = subprocess.run(
        ["git", "-C", str(repo_root), "var", "GIT_AUTHOR_IDENT"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    committer = subprocess.run(
        ["git", "-C", str(repo_root), "var", "GIT_COMMITTER_IDENT"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if author.returncode != 0 or committer.returncode != 0:
        raise WorkflowReasonError("git_identity_unavailable", "git author/committer identity is unavailable")


def _current_branch_name(git_state: dict[str, Any]) -> str:
    branch = git_state["branch"]
    if branch["state"] != "attached" or not branch["name"]:
        raise WorkflowReasonError("detached_head", "commit execution requires an attached branch")
    return branch["name"]


def _assert_precommit_state(plan: dict[str, Any], git_state: dict[str, Any], staged: dict[str, Any], repo_root: Path, repo_id: str, state_root: Path) -> None:
    branch_name = _current_branch_name(git_state)
    if plan["branch"]["name"] != branch_name:
        raise WorkflowReasonError("branch_changed", "branch changed since plan preparation")

    if plan["head_before"] != git_state["head"]:
        raise WorkflowReasonError("head_changed", "HEAD changed since plan preparation")

    if staged["staged_state_fingerprint"] != plan["staged_state_fingerprint"]:
        raise WorkflowReasonError("staged_state_changed", "staged index state changed since plan preparation")

    wt = git_state["working_tree"]
    if wt["unstaged"]["count"] > 0:
        raise WorkflowReasonError("unstaged_changes_present", "unstaged changes present before commit execution")
    if wt["untracked"]["count"] > 0:
        raise WorkflowReasonError("untracked_changes_present", "untracked changes present before commit execution")
    if wt["unmerged"]["count"] > 0:
        raise WorkflowReasonError("conflicts_present", "conflicts present before commit execution")
    if git_state["git_operation_in_progress"]:
        raise WorkflowReasonError("git_operation_in_progress", "active git operation present before commit execution")

    _candidate, matching_exists, matching_id = _calculate_snapshot_candidate(str(repo_root), repo_id, state_root)
    if not matching_exists or matching_id != plan["matching_snapshot_id"]:
        raise WorkflowReasonError("matching_snapshot_required", "exact matching immutable snapshot requirement is no longer satisfied")


def _parse_raw_records(raw: bytes) -> list[bytes]:
    chunks = [chunk for chunk in raw.split(b"\x00") if chunk]
    if len(chunks) % 2 != 0:
        raise WorkflowReasonError("post_commit_verification_failed", "invalid post-commit raw diff shape")

    records: list[bytes] = []
    for i in range(0, len(chunks), 2):
        records.append(chunks[i] + b"\t" + chunks[i + 1])
    # Canonical ordering must match prepare-time staged fingerprint semantics.
    # Sort by path bytes first, then metadata bytes for deterministic tie-breaking.
    records.sort(key=lambda item: (item.split(b"\t", 1)[1], item.split(b"\t", 1)[0]))
    return records


def _delta_fingerprint_from_records(records: list[bytes]) -> str:
    parts = bytearray(b"repoctl-staged-delta-v1\0")
    for record in records:
        parts.extend(record)
        parts.extend(b"\0")
    return hashlib.sha256(bytes(parts)).hexdigest()


def _verify_post_commit(
    repo_root: Path,
    plan: dict[str, Any],
    before_head: str,
    after_head: str,
    *,
    force_failure: bool,
) -> dict[str, Any]:
    if before_head == after_head:
        raise WorkflowReasonError("post_commit_verification_failed", "HEAD did not change after commit")

    parent = _run_git_text(repo_root, ["rev-parse", f"{after_head}^"]).strip()
    if parent != before_head:
        raise WorkflowReasonError("post_commit_verification_failed", "new commit parent does not match planned head")

    try:
        git_state = inspect_git_state(str(repo_root))
    except WorkflowGitStateError as exc:
        raise WorkflowReasonError("post_commit_verification_failed", str(exc)) from exc

    branch_name = _current_branch_name(git_state)
    if branch_name != plan["branch"]["name"]:
        raise WorkflowReasonError("post_commit_verification_failed", "branch changed during commit execution")

    raw = _run_git_bytes(repo_root, ["diff-tree", "--raw", "--no-renames", "--abbrev=40", "--no-commit-id", "-z", "-r", before_head, after_head])
    records = _parse_raw_records(raw)
    committed_delta_fingerprint = _delta_fingerprint_from_records(records)
    if committed_delta_fingerprint != plan["staged_delta_fingerprint"]:
        raise WorkflowReasonError("post_commit_verification_failed", "committed delta does not match approved staged fingerprint")

    wt = git_state["working_tree"]
    if wt["staged"]["count"] != 0:
        raise WorkflowReasonError("post_commit_verification_failed", "index still contains staged changes after commit")
    if wt["unstaged"]["count"] != 0 or wt["untracked"]["count"] != 0:
        raise WorkflowReasonError("post_commit_verification_failed", "worktree is not clean after commit")
    if wt["unmerged"]["count"] != 0:
        raise WorkflowReasonError("post_commit_verification_failed", "conflict state present after commit")
    if git_state["git_operation_in_progress"]:
        raise WorkflowReasonError("post_commit_verification_failed", "git operation is active after commit")

    if force_failure:
        raise WorkflowReasonError("post_commit_verification_failed", "controlled post-commit verification failure")

    return {
        "head_changed": True,
        "parent_matches_previous_head": True,
        "branch_unchanged": True,
        "committed_delta_matches_plan": True,
        "index_clean": True,
        "worktree_clean": True,
        "conflicts_present": False,
        "git_operation_in_progress": False,
        "committed_delta_fingerprint": committed_delta_fingerprint,
    }


def _derive_execution_id(base_payload: dict[str, Any]) -> str:
    canonical = json.dumps(base_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "commit-exec--" + hashlib.sha256(canonical).hexdigest()[:16]


def _build_execution_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Commit Execution Evidence",
        "",
        f"- Execution id: {payload['execution_id']}",
        f"- Plan id: {payload['plan_id']}",
        f"- Repository root: {payload['repository_root']}",
        f"- Repository id: {payload['repository_id']}",
        f"- Branch: {payload['branch']}",
        f"- HEAD before: {payload['head_before']}",
        f"- HEAD after: {payload['head_after']}",
        f"- Matching snapshot id: {payload['matching_snapshot_id']}",
        "",
        "## Commit Message",
        "",
        payload["commit_message"],
        "",
        "## Verification",
    ]

    for key, value in payload["verification"].items():
        lines.append(f"- {key}: {value}")

    return "\n".join(lines) + "\n"


def _build_audit_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Commit Audit Outcome",
        "",
        f"- Audit id: {payload['audit_id']}",
        f"- Execution id: {payload['execution_id']}",
        f"- Plan id: {payload['plan_id']}",
        f"- Mutation succeeded: {payload['mutation_succeeded']}",
        f"- Resulting HEAD: {payload['head_after']}",
        f"- Outcome: {payload['outcome']}",
        f"- Reason: {payload.get('reason', '')}",
        "",
    ]
    return "\n".join(lines)


def _derive_audit_id(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "commit-audit--" + hashlib.sha256(canonical).hexdigest()[:16]


def _publish_audit(audit_root: Path, payload: dict[str, Any]) -> str:
    audit_root.mkdir(parents=True, exist_ok=True)
    base = dict(payload)
    audit_id = _derive_audit_id(base)
    full = {**base, "audit_id": audit_id}
    final_dir = audit_root / audit_id
    temp_dir = Path(mkdtemp(prefix="commit-audit-tmp-", dir=str(audit_root)))
    try:
        write_json_deterministic(temp_dir / "audit.json", full)
        (temp_dir / "audit.md").write_text(_build_audit_markdown(full), encoding="utf-8", newline="\n")
        if final_dir.exists():
            if (final_dir / "audit.json").read_bytes() != (temp_dir / "audit.json").read_bytes():
                raise WorkflowReasonError("commit_succeeded_audit_failed", "existing audit content mismatch for identical audit id")
            shutil.rmtree(temp_dir)
            return audit_id
        temp_dir.rename(final_dir)
        return audit_id
    except Exception:
        if temp_dir.exists():
            shutil.rmtree(temp_dir)
        raise


def _verify_execution_dir(execution_dir: Path, expected_id: str) -> dict[str, Any]:
    names = {child.name for child in execution_dir.iterdir()}
    if names != EXECUTION_REQUIRED_FILES:
        raise WorkflowReasonError("commit_succeeded_audit_failed", "execution artifact set is incomplete or unexpected")

    payload = json.loads((execution_dir / "execution.json").read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1:
        raise WorkflowReasonError("commit_succeeded_audit_failed", "unsupported execution schema version")
    if payload.get("execution_id") != expected_id:
        raise WorkflowReasonError("commit_succeeded_audit_failed", "execution id mismatch")

    base = dict(payload)
    base.pop("execution_id", None)
    recomputed = _derive_execution_id(base)
    if recomputed != expected_id:
        raise WorkflowReasonError("commit_succeeded_audit_failed", "execution id/content integrity mismatch")

    return payload


def _publish_execution(execution_root: Path, payload: dict[str, Any], markdown: str, *, force_failure: bool) -> tuple[str, bool]:
    execution_root.mkdir(parents=True, exist_ok=True)
    execution_id = payload["execution_id"]
    final_dir = execution_root / execution_id
    temp_dir = Path(mkdtemp(prefix="commit-exec-tmp-", dir=str(execution_root)))
    try:
        write_json_deterministic(temp_dir / "execution.json", payload)
        (temp_dir / "execution.md").write_text(markdown, encoding="utf-8", newline="\n")

        if force_failure:
            raise WorkflowReasonError("commit_succeeded_audit_failed", "controlled execution audit failure")

        _verify_execution_dir(temp_dir, execution_id)

        if final_dir.exists():
            _verify_execution_dir(final_dir, execution_id)
            if (final_dir / "execution.json").read_bytes() != (temp_dir / "execution.json").read_bytes() or (
                final_dir / "execution.md"
            ).read_bytes() != (temp_dir / "execution.md").read_bytes():
                raise WorkflowReasonError("commit_succeeded_audit_failed", "existing execution content mismatch for identical execution id")
            shutil.rmtree(temp_dir)
            return str(final_dir), True

        temp_dir.rename(final_dir)
        return str(final_dir), False
    except Exception:
        if temp_dir.exists():
            shutil.rmtree(temp_dir)
        raise


def execute_prepared_commit(
    repository_path: str,
    plan_id: str,
    approve: bool,
    state_root: Path | None = None,
    *,
    _test_force_post_verify_failure: bool = False,
    _test_force_audit_failure: bool = False,
) -> dict[str, Any]:
    if not approve:
        raise WorkflowReasonError("approval_required", "explicit --approve is required for commit execution")

    root = (state_root or DEFAULT_STATE_ROOT).expanduser()
    root.mkdir(parents=True, exist_ok=True)

    try:
        git_state = inspect_git_state(repository_path)
    except WorkflowGitStateError as exc:
        raise WorkflowReasonError("staged_state_changed", str(exc)) from exc

    repo_root = Path(git_state["repository_root"])
    repo_id = make_repository_id(repo_root)
    plan_dir, location = _locate_plan_dir(root, repo_id, plan_id)
    if location == "other_repository":
        raise WorkflowReasonError("repository_mismatch", "plan repository does not match selected repository")
    if location == "missing":
        raise WorkflowReasonError("plan_not_found", f"commit plan not found: {plan_id}")
    plan = _load_plan(plan_dir, repo_id, repo_root)

    _enforce_hook_boundary(repo_root)
    _enforce_git_identity(repo_root)

    staged = _staged_delta(repo_root, git_state["head"])
    _assert_precommit_state(plan, git_state, staged, repo_root, repo_id, root)

    before_head = plan["head_before"]

    env = dict(os.environ)
    env["GIT_EDITOR"] = ":"
    proc = subprocess.run(
        ["git", "-C", str(repo_root), "commit", "--no-gpg-sign", "-m", plan["commit_message"]],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env=env,
    )
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace").strip()
        raise WorkflowReasonError("git_commit_failed", f"git commit failed: {stderr}")

    after_head = _run_git_text(repo_root, ["rev-parse", "HEAD"]).strip()

    mutation_payload = {
        "schema_version": 1,
        "plan_id": plan["plan_id"],
        "repository_id": repo_id,
        "repository_root": str(repo_root),
        "branch": plan["branch"]["name"],
        "head_before": before_head,
        "head_after": after_head,
        "commit_message": plan["commit_message"],
        "matching_snapshot_id": plan["matching_snapshot_id"],
        "staged_state_fingerprint": plan["staged_state_fingerprint"],
        "staged_delta_fingerprint": plan["staged_delta_fingerprint"],
        "staged_summary": plan["staged_summary"],
        "mutation_succeeded": True,
        "verification": {"status": "pending"},
        "remote_refresh_performed": False,
        "push_performed": False,
    }
    execution_id = _derive_execution_id(mutation_payload)
    execution_payload = {**mutation_payload, "execution_id": execution_id}
    execution_markdown = _build_execution_markdown(execution_payload)
    execution_root = root / repo_id / "workflow" / "commit_executions"
    try:
        execution_dir, reused_existing = _publish_execution(
            execution_root,
            execution_payload,
            execution_markdown,
            force_failure=False,
        )
    except WorkflowReasonError as exc:
        raise WorkflowReasonError(
            "commit_succeeded_audit_failed",
            f"commit succeeded but mutation evidence write failed; resulting commit id: {after_head}; detail: {exc.safe_message}",
            commit_id=after_head,
        ) from exc

    try:
        verification = _verify_post_commit(
            repo_root,
            plan,
            before_head,
            after_head,
            force_failure=_test_force_post_verify_failure,
        )
    except WorkflowReasonError as exc:
        try:
            _publish_audit(
                root / repo_id / "workflow" / "commit_audits",
                {
                    "execution_id": execution_id,
                    "plan_id": plan["plan_id"],
                    "repository_id": repo_id,
                    "repository_root": str(repo_root),
                    "head_before": before_head,
                    "head_after": after_head,
                    "mutation_succeeded": True,
                    "outcome": "mutation_succeeded_audit_failed",
                    "reason": exc.safe_message,
                },
            )
        except WorkflowReasonError as audit_exc:
            raise WorkflowReasonError(
                "commit_succeeded_audit_failed",
                f"commit succeeded but audit outcome write failed; resulting commit id: {after_head}; detail: {audit_exc.safe_message}",
                commit_id=after_head,
            ) from audit_exc
        raise WorkflowReasonError(
            "post_commit_verification_failed",
            f"post-commit verification failed after commit (old HEAD={before_head}, new HEAD={after_head}, execution={execution_id}): {exc.safe_message}",
            commit_id=after_head,
        ) from exc

    try:
        if _test_force_audit_failure:
            raise WorkflowReasonError("commit_succeeded_audit_failed", "controlled execution audit failure")
        _publish_audit(
            root / repo_id / "workflow" / "commit_audits",
            {
                "execution_id": execution_id,
                "plan_id": plan["plan_id"],
                "repository_id": repo_id,
                "repository_root": str(repo_root),
                "head_before": before_head,
                "head_after": after_head,
                "mutation_succeeded": True,
                "outcome": "verified",
                "reason": "post-commit verification passed",
            },
        )
    except WorkflowReasonError as exc:
        raise WorkflowReasonError(
            "commit_succeeded_audit_failed",
            f"commit succeeded but execution audit write failed; resulting commit id: {after_head}; detail: {exc.safe_message}",
            commit_id=after_head,
        ) from exc

    return {
        "plan_id": plan["plan_id"],
        "execution_id": execution_id,
        "repository_id": repo_id,
        "repository_root": str(repo_root),
        "branch": plan["branch"]["name"],
        "head_before": before_head,
        "head_after": after_head,
        "commit_message": plan["commit_message"],
        "execution_dir": execution_dir,
        "reused_existing": reused_existing,
    }
