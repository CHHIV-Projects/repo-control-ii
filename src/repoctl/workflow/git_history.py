from __future__ import annotations

import base64
import os
import re
from pathlib import Path
from typing import Any

from ..scanner.git_ops import ScanError, _run_git_bounded, validate_git_worktree

MAX_HISTORY_COMMITS = 30
MAX_FILE_HISTORY_COMMITS = 30
MAX_BRANCHES = 256
MAX_HISTORY_OUTPUT_BYTES = 1024 * 1024
MAX_BRANCH_OUTPUT_BYTES = 256 * 1024
MAX_COMMIT_DIFF_BYTES = 128 * 1024
MAX_BRANCH_DIFF_BYTES = 128 * 1024
MAX_GIT_STDERR_BYTES = 16 * 1024
GIT_COMMAND_TIMEOUT_SECONDS = 10
_OBJECT_ID = re.compile(r"^[0-9a-f]{40,64}$")


class GitHistoryError(RuntimeError):
    pass


def _run(
    repo_root: Path,
    args: list[str],
    *,
    output_limit: int = MAX_HISTORY_OUTPUT_BYTES,
    allow_nonzero: bool = False,
) -> tuple[bytes, bytes, int, bool]:
    try:
        stdout, stderr, returncode, truncated = _run_git_bounded(
            repo_root,
            args,
            stdout_limit=output_limit,
            stderr_limit=MAX_GIT_STDERR_BYTES,
            timeout_seconds=GIT_COMMAND_TIMEOUT_SECONDS,
        )
    except ScanError as exc:
        raise GitHistoryError(str(exc)) from exc
    if truncated:
        return stdout, stderr, returncode, True
    if returncode and not allow_nonzero:
        message = stderr.decode("utf-8", errors="replace").strip()
        raise GitHistoryError(f"Git {args[0]} failed: {message or 'no diagnostic supplied'}")
    return stdout, stderr, returncode, False


def _text(value: bytes) -> str:
    return value.decode("utf-8", errors="replace")


def _token(value: str) -> str:
    return base64.urlsafe_b64encode(os.fsencode(value)).decode("ascii").rstrip("=")


def _display_path(value: str) -> str:
    parts: list[str] = []
    for character in value:
        codepoint = ord(character)
        if 0xDC80 <= codepoint <= 0xDCFF:
            parts.append(f"\\x{codepoint - 0xDC00:02x}")
        elif character == "\\":
            parts.append("\\\\")
        elif codepoint < 0x20 or codepoint == 0x7F:
            parts.append(repr(character)[1:-1])
        else:
            parts.append(character)
    return "".join(parts)


def _parse_records(output: bytes, field_count: int) -> list[list[bytes]]:
    records = []
    for record in output.split(b"\x1e"):
        record = record.strip(b"\n")
        if not record:
            continue
        fields = record.split(b"\x00")
        if len(fields) != field_count:
            raise GitHistoryError("Git returned an invalid history record.")
        records.append(fields)
    return records


def _history_records(
    repo_root: Path,
    *,
    limit: int,
    path: str | None = None,
    follow: bool = False,
    revisions: list[str] | None = None,
) -> tuple[list[dict[str, str]], bool]:
    args = [
        "log",
        f"--max-count={limit}",
        "--date=iso-strict",
        "--format=%H%x00%h%x00%an%x00%aI%x00%cI%x00%P%x00%s%x00%D%x1e",
    ]
    if revisions:
        args.extend(revisions)
    if follow and path is not None:
        args.append("--follow")
    if path is not None:
        args.extend(["--", f":(literal){path}"])
    output, _stderr, _returncode, truncated = _run(
        repo_root,
        args,
        output_limit=MAX_HISTORY_OUTPUT_BYTES,
    )
    if truncated:
        return [], True
    result = []
    for fields in _parse_records(output, 8):
        full_oid, short_oid, author, authored, committed, parents, subject, decorations = map(_text, fields)
        parent_list = parents.split() if parents else []
        result.append(
            {
                "oid": full_oid,
                "short_oid": short_oid,
                "author": author,
                "author_date": authored,
                "commit_date": committed,
                "parents": parent_list,
                "subject": subject,
                "decorations": decorations,
                "merge": len(parent_list) > 1,
            }
        )
    return result, False


def _commit_detail(repo_root: Path, oid: str) -> dict[str, Any]:
    if not _OBJECT_ID.fullmatch(oid):
        raise ValueError("Commit selection must be a full hexadecimal object ID.")
    _check, check_stderr, code, truncated = _run(
        repo_root,
        ["cat-file", "-e", f"{oid}^{{commit}}"],
        output_limit=1024,
        allow_nonzero=True,
    )
    if truncated:
        raise GitHistoryError("Commit verification exceeded the configured output limit.")
    if code == 1:
        raise FileNotFoundError("The selected commit is not present in this repository.")
    if code != 0:
        message = check_stderr.decode("utf-8", errors="replace").strip()
        raise GitHistoryError(f"Git commit verification failed: {message or 'no diagnostic supplied'}")

    meta_args = [
        "show",
        "-s",
        "--format=%H%x00%h%x00%an%x00%aI%x00%cI%x00%P%x00%s",
        oid,
    ]
    metadata, _stderr, _code, truncated = _run(repo_root, meta_args, output_limit=16 * 1024)
    if truncated:
        raise GitHistoryError("Commit metadata exceeded the configured output limit.")
    fields = metadata.rstrip(b"\n").split(b"\x00")
    if len(fields) != 7:
        raise GitHistoryError("Git returned invalid commit metadata.")
    full_oid, short_oid, author, author_date, commit_date, parents, subject = map(_text, fields)
    parent_list = parents.split() if parents else []

    name_status_args = [
        "diff-tree",
        "--root",
        "-r",
        "--no-commit-id",
        "--name-status",
        "--no-renames",
        "-z",
    ]
    if parent_list:
        name_status_args = [
            "diff-tree",
            "-r",
            "--no-commit-id",
            "--name-status",
            "--no-renames",
            "-z",
            parent_list[0],
            oid,
            "--",
        ]
    else:
        name_status_args.append(oid)
    name_status, _stderr, _code, files_truncated = _run(
        repo_root,
        name_status_args,
        output_limit=MAX_HISTORY_OUTPUT_BYTES,
    )
    changed_files = _parse_name_status(name_status) if not files_truncated else []

    diff, _stderr, _code, diff_truncated = _run(
        repo_root,
        [
            "show",
            "--first-parent",
            "--format=",
            "--no-ext-diff",
            "--no-textconv",
            "--no-color",
            "--no-renames",
            "--unified=3",
            oid,
        ],
        output_limit=MAX_COMMIT_DIFF_BYTES,
    )
    try:
        diff_text = diff.decode("utf-8", errors="strict")
        diff_available = True
        diff_reason = ""
    except UnicodeDecodeError:
        diff_text = ""
        diff_available = False
        diff_reason = "Commit diff contains undecodable bytes."
    return {
        "oid": full_oid,
        "short_oid": short_oid,
        "author": author,
        "author_date": author_date,
        "commit_date": commit_date,
        "parents": parent_list,
        "subject": subject,
        "merge": len(parent_list) > 1,
        "comparison": (
            "Root commit vs empty tree."
            if not parent_list
            else "First parent vs this commit."
        ),
        "files": changed_files,
        "files_complete": not files_truncated,
        "diff": {
            "text": diff_text,
            "available": diff_available,
            "complete": not diff_truncated,
            "reason": diff_reason or (
                "Commit diff exceeded the configured output limit."
                if diff_truncated
                else ""
            ),
        },
    }


def _parse_name_status(output: bytes) -> list[dict[str, str]]:
    fields = output.split(b"\x00")
    if fields and fields[-1] == b"":
        fields.pop()
    result = []
    index = 0
    while index < len(fields):
        status = _text(fields[index])
        index += 1
        if index >= len(fields):
            raise GitHistoryError("Git returned incomplete changed-file status.")
        path = os.fsdecode(fields[index])
        index += 1
        result.append(
            {
                "status": status,
                "path": _display_path(path),
                "token": _token(path),
            }
        )
    return result


def inspect_history(repository_path: str, selected_commit: str | None = None) -> dict[str, Any]:
    try:
        repo_root = validate_git_worktree(Path(repository_path).expanduser().resolve())
    except ScanError as exc:
        raise GitHistoryError(str(exc)) from exc
    commits, truncated = _history_records(repo_root, limit=MAX_HISTORY_COMMITS)
    detail = _commit_detail(repo_root, selected_commit) if selected_commit else None
    return {
        "commits": commits,
        "complete": not truncated,
        "selected": detail,
        "limit": MAX_HISTORY_COMMITS,
        "repository_root": str(repo_root),
    }


def inspect_file_history(repository_path: str, file_token: str) -> dict[str, Any]:
    try:
        repo_root = validate_git_worktree(Path(repository_path).expanduser().resolve())
    except ScanError as exc:
        raise GitHistoryError(str(exc)) from exc
    tracked_output, _stderr, _code, truncated = _run(
        repo_root,
        ["ls-files", "-z"],
        output_limit=MAX_HISTORY_OUTPUT_BYTES,
    )
    if truncated:
        raise GitHistoryError("Tracked-file inventory exceeded the configured output limit.")
    tracked = {os.fsdecode(path) for path in tracked_output.split(b"\x00") if path}
    _head, _stderr, head_code, head_truncated = _run(
        repo_root,
        ["rev-parse", "--verify", "--quiet", "HEAD^{commit}"],
        output_limit=256,
        allow_nonzero=True,
    )
    if head_truncated:
        raise GitHistoryError("HEAD verification exceeded its output limit.")
    if head_code not in {0, 1}:
        raise GitHistoryError("Git could not determine the current HEAD commit.")
    if head_code == 0:
        committed_output, _stderr, _code, committed_truncated = _run(
            repo_root,
            ["ls-tree", "-r", "--name-only", "-z", "HEAD"],
            output_limit=MAX_HISTORY_OUTPUT_BYTES,
        )
        if committed_truncated:
            raise GitHistoryError("Committed-file inventory exceeded the configured output limit.")
        tracked.update(os.fsdecode(path) for path in committed_output.split(b"\x00") if path)
    selected = next((path for path in tracked if _token(path) == file_token), None)
    if selected is None:
        raise FileNotFoundError("Selected file is not currently tracked.")
    commits, history_truncated = _history_records(
        repo_root,
        limit=MAX_FILE_HISTORY_COMMITS,
        path=selected,
        follow=True,
    )
    return {
        "path": _display_path(selected),
        "token": file_token,
        "commits": commits,
        "complete": not history_truncated,
        "limit": MAX_FILE_HISTORY_COMMITS,
        "repository_root": str(repo_root),
    }


def _ref_token(ref: str) -> str:
    return base64.urlsafe_b64encode(ref.encode("utf-8")).decode("ascii").rstrip("=")


def _branch_inventory(repo_root: Path) -> list[dict[str, Any]]:
    output, _stderr, _code, truncated = _run(
        repo_root,
        [
            "for-each-ref",
            "--format=%(refname)%00%(objectname)%00%(subject)%00%(committerdate:iso-strict)%00%(upstream)",
            "refs/heads/",
        ],
        output_limit=MAX_BRANCH_OUTPUT_BYTES,
    )
    if truncated:
        raise GitHistoryError("Local branch inventory exceeded the configured output limit.")
    branches = []
    for record in output.splitlines():
        fields = record.split(b"\x00")
        if len(fields) != 5:
            raise GitHistoryError("Git returned an invalid local branch record.")
        ref, oid, subject, committed, upstream = map(_text, fields)
        branches.append(
            {
                "ref": ref,
                "name": ref.removeprefix("refs/heads/"),
                "token": _ref_token(ref),
                "oid": oid,
                "short_oid": oid[:12],
                "subject": subject,
                "commit_date": committed,
                "upstream": upstream,
            }
        )
        if len(branches) > MAX_BRANCHES:
            raise GitHistoryError("Local branch inventory exceeds the configured branch count limit.")
    return branches


def _rev_count(repo_root: Path, left: str, right: str) -> tuple[int, int]:
    output, _stderr, _code, truncated = _run(
        repo_root,
        ["rev-list", "--left-right", "--count", f"{left}...{right}"],
        output_limit=256,
    )
    if truncated:
        raise GitHistoryError("Branch ancestry count response exceeded its limit.")
    try:
        left_count, right_count = output.decode("ascii").split()
        return int(left_count), int(right_count)
    except (UnicodeDecodeError, ValueError) as exc:
        raise GitHistoryError("Git returned invalid branch ancestry counts.") from exc


def _ancestor(repo_root: Path, candidate: str, reference: str) -> bool:
    _output, stderr, code, truncated = _run(
        repo_root,
        ["merge-base", "--is-ancestor", candidate, reference],
        output_limit=256,
        allow_nonzero=True,
    )
    if truncated:
        raise GitHistoryError("Branch ancestry response exceeded its limit.")
    if code == 0:
        return True
    if code == 1:
        return False
    message = stderr.decode("utf-8", errors="replace").strip()
    raise GitHistoryError(f"Git ancestry check failed: {message or 'no diagnostic supplied'}")


def _branch_metrics(
    repo_root: Path,
    branch: dict[str, Any],
    main_oid: str | None,
    current_ref: str,
) -> dict[str, Any]:
    branch_ref = branch["ref"]
    if main_oid is None:
        main_relation: dict[str, Any] = {
            "state": "unavailable",
            "label": "Local main branch is unavailable",
        }
    elif branch_ref == "refs/heads/main":
        main_relation: dict[str, Any] = {
            "state": "reference",
            "label": "Reference branch (main)",
            "ahead": 0,
            "behind": 0,
            "fully_merged": True,
        }
    else:
        behind, ahead = _rev_count(repo_root, "refs/heads/main", branch_ref)
        main_relation = {
            "state": "available",
            "ahead": ahead,
            "behind": behind,
            "fully_merged": _ancestor(repo_root, branch_ref, "refs/heads/main"),
        }
    upstream_relation: dict[str, Any]
    upstream = branch["upstream"]
    if not upstream:
        upstream_relation = {"state": "not_configured", "label": "No upstream configured"}
    else:
        verify, _stderr, code, verify_truncated = _run(
            repo_root,
            ["rev-parse", "--verify", "--quiet", f"{upstream}^{{commit}}"],
            output_limit=256,
            allow_nonzero=True,
        )
        if code != 0 or verify_truncated:
            if code not in {0, 1} and not verify_truncated:
                message = _stderr.decode("utf-8", errors="replace").strip()
                raise GitHistoryError(f"Git upstream verification failed: {message or 'no diagnostic supplied'}")
            upstream_relation = {
                "state": "unavailable",
                "ref": upstream,
                "label": "Configured upstream ref is unavailable locally",
            }
        else:
            behind, ahead = _rev_count(repo_root, upstream, branch_ref)
            upstream_relation = {
                "state": "available",
                "ref": upstream,
                "ahead": ahead,
                "behind": behind,
                "label": f"{ahead} ahead / {behind} behind {upstream} (local tracking ref)",
            }
    branch.update(
        {
            "current": branch_ref == current_ref,
            "main_relation": main_relation,
            "upstream_relation": upstream_relation,
            "main_oid": main_oid,
        }
    )
    return branch


def inspect_branches(
    repository_path: str,
    selected_branch: str | None = None,
) -> dict[str, Any]:
    try:
        repo_root = validate_git_worktree(Path(repository_path).expanduser().resolve())
    except ScanError as exc:
        raise GitHistoryError(str(exc)) from exc
    main, _stderr, main_code, main_truncated = _run(
        repo_root,
        ["rev-parse", "--verify", "--quiet", "refs/heads/main^{commit}"],
        output_limit=256,
        allow_nonzero=True,
    )
    if main_truncated:
        raise GitHistoryError("Local main reference response exceeded its output limit.")
    if main_code not in {0, 1}:
        raise GitHistoryError("Git could not determine whether local main exists.")
    main_available = main_code == 0
    main_oid = main.decode("ascii", errors="replace").strip() if main_available else None
    if main_oid is not None and not _OBJECT_ID.fullmatch(main_oid):
        raise GitHistoryError("Git returned an invalid local main object ID.")
    current, _stderr, current_code, current_truncated = _run(
        repo_root,
        ["symbolic-ref", "--quiet", "HEAD"],
        output_limit=1024,
        allow_nonzero=True,
    )
    if current_truncated:
        raise GitHistoryError("Current branch reference response exceeded its output limit.")
    if current_code not in {0, 1}:
        raise GitHistoryError("Git could not determine the current branch reference.")
    current_ref = current.decode("utf-8", errors="strict").strip() if current_code == 0 else ""
    branches = _branch_inventory(repo_root)
    for branch in branches:
        _branch_metrics(repo_root, branch, main_oid, current_ref)

    selected = None
    if selected_branch:
        selected = next((branch for branch in branches if branch["token"] == selected_branch), None)
        if selected is None:
            raise FileNotFoundError("Selected local branch is no longer available.")
        if main_available:
            base, _stderr, code, truncated = _run(
                repo_root,
                ["merge-base", "refs/heads/main", selected["ref"]],
                output_limit=256,
                allow_nonzero=True,
            )
            if truncated or code != 0:
                selected["comparison"] = {
                    "available": False,
                    "reason": "A common ancestor with main could not be established.",
                    "files": [],
                    "diff": "",
                    "complete": not truncated,
                }
            else:
                base_oid = base.decode("ascii", errors="replace").strip()
                file_output, _stderr, _code, file_truncated = _run(
                    repo_root,
                    [
                        "diff",
                        "--name-status",
                        "-z",
                        "--no-renames",
                        base_oid,
                        selected["ref"],
                        "--",
                    ],
                    output_limit=MAX_BRANCH_DIFF_BYTES,
                )
                diff_output, _stderr, _code, diff_truncated = _run(
                    repo_root,
                    [
                        "diff",
                        "--no-ext-diff",
                        "--no-textconv",
                        "--no-color",
                        "--no-renames",
                        "--unified=3",
                        base_oid,
                        selected["ref"],
                        "--",
                    ],
                    output_limit=MAX_BRANCH_DIFF_BYTES,
                )
                try:
                    diff_text = diff_output.decode("utf-8", errors="strict")
                    diff_available = True
                    reason = ""
                except UnicodeDecodeError:
                    diff_text = ""
                    diff_available = False
                    reason = "Branch comparison contains undecodable bytes."
                selected["comparison"] = {
                    "available": True,
                    "basis": f"Common ancestor {base_oid[:12]} (main...{selected['name']}); committed branch changes since merge-base.",
                    "files": _parse_name_status(file_output) if not file_truncated else [],
                    "files_complete": not file_truncated,
                    "diff": diff_text,
                    "diff_available": diff_available,
                    "reason": reason or (
                        "Branch diff exceeded the configured output limit."
                        if diff_truncated
                        else ""
                    ),
                    "complete": not diff_truncated,
                    "truncated": diff_truncated,
                }
        else:
            selected["comparison"] = {
                "available": False,
                "reason": "Local main branch is unavailable.",
                "files": [],
                "diff": "",
                "complete": True,
            }
        unique_commits, unique_truncated = (
            _history_records(
                repo_root,
                limit=10,
                revisions=[f"refs/heads/main..{selected['ref']}"],
            )
            if main_available
            else ([], False)
        )
        selected["recent_commits"] = unique_commits
        selected["recent_commits_complete"] = not unique_truncated
        main_only_commits, main_only_truncated = (
            _history_records(
                repo_root,
                limit=10,
                revisions=[f"{selected['ref']}..refs/heads/main"],
            )
            if main_available
            else ([], False)
        )
        selected["main_only_commits"] = main_only_commits
        selected["main_only_commits_complete"] = not main_only_truncated

    return {
        "repository_root": str(repo_root),
        "main_available": main_available,
        "main_oid": main_oid,
        "head_state": "attached" if current_code == 0 else "detached",
        "branches": branches,
        "selected": selected,
        "branch_limit": MAX_BRANCHES,
        "freshness": "Upstream comparisons use local refs only; no fetch was performed and remote-tracking refs may be stale.",
    }
