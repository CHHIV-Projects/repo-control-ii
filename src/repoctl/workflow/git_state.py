from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from ..scanner.git_ops import ScanError, get_branch, get_head_commit, get_working_tree_with_branch, validate_git_worktree
from ..scanner.util import encode_path_order

_GIT_OPERATION_ORDER = ["merge", "rebase", "cherry_pick", "revert", "bisect"]
_GIT_OPERATION_MARKERS = {
    "merge": ["MERGE_HEAD"],
    "rebase": ["rebase-merge", "rebase-apply", "REBASE_HEAD"],
    "cherry_pick": ["CHERRY_PICK_HEAD"],
    "revert": ["REVERT_HEAD"],
    "bisect": ["BISECT_LOG", "BISECT_START"],
}


class WorkflowGitStateError(RuntimeError):
    pass


def _run_git(repo_root: Path, args: list[str]) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace").strip()
        raise WorkflowGitStateError(f"git command failed: {' '.join(args)}: {stderr}")
    return proc.stdout.decode("utf-8", errors="strict")


def _git_path_exists(repo_root: Path, marker: str) -> bool:
    resolved = _run_git(repo_root, ["rev-parse", "--git-path", marker]).strip()
    path = Path(resolved)
    if not path.is_absolute():
        path = (repo_root / path).resolve()
    return path.exists()


def _derive_operations(repo_root: Path) -> list[str]:
    operations: list[str] = []
    for operation in _GIT_OPERATION_ORDER:
        markers = _GIT_OPERATION_MARKERS[operation]
        if any(_git_path_exists(repo_root, marker) for marker in markers):
            operations.append(operation)
    return operations


def _parse_branch_headers(headers: list[str]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for header in headers:
        if not header.startswith("# "):
            continue
        line = header[2:]
        if line.startswith("branch.oid "):
            parsed["branch.oid"] = line[len("branch.oid ") :]
            continue
        if line.startswith("branch.head "):
            parsed["branch.head"] = line[len("branch.head ") :]
            continue
        if line.startswith("branch.upstream "):
            parsed["branch.upstream"] = line[len("branch.upstream ") :]
            continue
        if line.startswith("branch.ab "):
            parsed["branch.ab"] = line[len("branch.ab ") :]
            continue
    return parsed


def _classify_working_tree(entries: list[dict[str, Any]]) -> dict[str, Any]:
    staged_paths: list[str] = []
    unstaged_paths: list[str] = []
    untracked_paths: list[str] = []
    unmerged_paths: list[str] = []

    for entry in entries:
        kind = entry["kind"]
        path = entry["path"]

        if kind == "untracked":
            untracked_paths.append(path)
            continue

        if kind == "unmerged":
            unmerged_paths.append(path)
            continue

        xy = entry.get("xy") or ""
        if len(xy) >= 1 and xy[0] != ".":
            staged_paths.append(path)
        if len(xy) >= 2 and xy[1] != ".":
            unstaged_paths.append(path)

    staged_paths = sorted(set(staged_paths), key=encode_path_order)
    unstaged_paths = sorted(set(unstaged_paths), key=encode_path_order)
    untracked_paths = sorted(set(untracked_paths), key=encode_path_order)
    unmerged_paths = sorted(set(unmerged_paths), key=encode_path_order)

    staged_present = bool(staged_paths)
    unstaged_or_untracked_present = bool(unstaged_paths or untracked_paths)
    unmerged_present = bool(unmerged_paths)

    if unmerged_present:
        workflow_state = "conflicted"
    elif staged_present and not unstaged_or_untracked_present:
        workflow_state = "staged_only"
    elif staged_present and unstaged_or_untracked_present:
        workflow_state = "staged_and_unstaged"
    elif not staged_present and unstaged_or_untracked_present:
        workflow_state = "unstaged_only"
    else:
        workflow_state = "clean"

    return {
        "is_clean": workflow_state == "clean",
        "entries": entries,
        "staged": {"count": len(staged_paths), "paths": staged_paths},
        "unstaged": {"count": len(unstaged_paths), "paths": unstaged_paths},
        "untracked": {"count": len(untracked_paths), "paths": untracked_paths},
        "unmerged": {"count": len(unmerged_paths), "paths": unmerged_paths},
        "workflow_state": workflow_state,
    }


def _derive_upstream(parsed_headers: dict[str, str]) -> dict[str, Any]:
    upstream_ref = parsed_headers.get("branch.upstream")
    if not upstream_ref:
        return {
            "configured": False,
            "ref": None,
            "divergence_state": "unavailable",
            "unavailable_reason": "upstream_not_configured",
            "relation": None,
            "ahead": None,
            "behind": None,
        }

    ab = parsed_headers.get("branch.ab")
    if not ab:
        return {
            "configured": True,
            "ref": upstream_ref,
            "divergence_state": "unavailable",
            "unavailable_reason": "upstream_ref_unavailable",
            "relation": None,
            "ahead": None,
            "behind": None,
        }

    parts = ab.split(" ")
    if len(parts) != 2 or not parts[0].startswith("+") or not parts[1].startswith("-"):
        raise WorkflowGitStateError(f"invalid branch.ab header: {ab}")

    ahead = int(parts[0][1:])
    behind = int(parts[1][1:])
    if ahead == 0 and behind == 0:
        relation = "equal"
    elif ahead > 0 and behind == 0:
        relation = "ahead"
    elif ahead == 0 and behind > 0:
        relation = "behind"
    else:
        relation = "diverged"

    return {
        "configured": True,
        "ref": upstream_ref,
        "divergence_state": "available",
        "unavailable_reason": None,
        "relation": relation,
        "ahead": ahead,
        "behind": behind,
    }


def inspect_git_state(repository_path: str) -> dict[str, Any]:
    target_path = Path(repository_path).expanduser().resolve()
    repo_root = validate_git_worktree(target_path)

    try:
        status = get_working_tree_with_branch(repo_root)
    except ScanError as exc:
        msg = str(exc)
        if msg.startswith("unsupported porcelain v2 record type: "):
            prefix = msg.split(": ", 1)[1][:1]
            raise WorkflowGitStateError(
                f"git status error [unsupported_porcelain_v2_record]: unsupported porcelain-v2 record prefix: {prefix}"
            ) from exc
        raise WorkflowGitStateError(str(exc)) from exc

    parsed_headers = _parse_branch_headers(status.get("headers", []))
    branch = get_branch(repo_root)
    head = get_head_commit(repo_root)

    classified = _classify_working_tree(status["entries"])
    operations = _derive_operations(repo_root)
    upstream = _derive_upstream(parsed_headers)

    return {
        "repository_root": str(repo_root),
        "head": head,
        "branch": branch,
        "upstream": upstream,
        "remote_refresh_performed": False,
        "working_tree": {
            "is_clean": classified["is_clean"],
            "staged": classified["staged"],
            "unstaged": classified["unstaged"],
            "untracked": classified["untracked"],
            "unmerged": classified["unmerged"],
            "entries": status["entries"],
        },
        "workflow_state": classified["workflow_state"],
        "git_operation_in_progress": len(operations) > 0,
        "git_operations": operations,
    }
