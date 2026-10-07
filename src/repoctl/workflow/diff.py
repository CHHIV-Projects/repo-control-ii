from __future__ import annotations

import base64
import os
import re
import stat
from pathlib import Path
from typing import Any

from ..scanner.git_ops import ScanError, _run_git_bounded, validate_git_worktree
from .git_state import WorkflowGitStateError, inspect_git_state

MAX_STATUS_OUTPUT_BYTES = 1024 * 1024
MAX_DIFF_OUTPUT_BYTES = 128 * 1024
MAX_UNTRACKED_PREVIEW_BYTES = 64 * 1024
MAX_GIT_STDERR_BYTES = 16 * 1024
GIT_COMMAND_TIMEOUT_SECONDS = 10

_STATUS_DISPLAY_LABELS = {
    "Conflicted / unmerged": "Conflicts / unmerged",
    "Untracked": "New / untracked",
    "Staged + unstaged": "Staged + additional unstaged changes",
    "Staged": "Staged changes",
    "Unstaged": "Unstaged changes",
    "Changed state unavailable": "Changed state unavailable",
}
_WHITESPACE_WARNING = re.compile(
    rb":\d+: .*?(?:whitespace|tab in indent|indentation)"
)


class GitReviewError(RuntimeError):
    pass


def _path_token(path: str) -> str:
    return base64.urlsafe_b64encode(os.fsencode(path)).decode("ascii").rstrip("=")


def _display_path(path: str) -> str:
    rendered: list[str] = []
    for character in path:
        codepoint = ord(character)
        if 0xDC80 <= codepoint <= 0xDCFF:
            rendered.append(f"\\x{codepoint - 0xDC00:02x}")
        elif character == "\\":
            rendered.append("\\\\")
        elif codepoint < 0x20 or codepoint == 0x7F:
            rendered.append(repr(character)[1:-1])
        else:
            rendered.append(character)
    return "".join(rendered)


def _file_status(entry: dict[str, Any]) -> dict[str, Any]:
    if entry["kind"] == "unmerged":
        return {
            "label": "Conflicted / unmerged",
            "display_label": "Conflict / unmerged",
            "staged": False,
            "unstaged": False,
            "conflicted": True,
        }
    if entry["kind"] == "untracked":
        return {
            "label": "Untracked",
            "display_label": "New / untracked",
            "staged": False,
            "unstaged": False,
            "conflicted": False,
        }

    xy = entry.get("xy") or ".."
    staged = bool(xy[0] != ".")
    unstaged = bool(xy[1] != ".")
    if staged and unstaged:
        label = "Staged + unstaged"
    elif staged:
        label = "Staged"
    elif unstaged:
        label = "Unstaged"
    else:
        label = "Changed state unavailable"
    display_label = _STATUS_DISPLAY_LABELS[label]
    if entry["kind"] == "rename_or_copy":
        label = f"{label}; {entry['operation']}"
        display_label = f"{display_label}; {entry['operation']}"
    return {
        "label": label,
        "display_label": display_label,
        "staged": staged,
        "unstaged": unstaged,
        "conflicted": False,
    }


def _run_bounded_git(
    repo_root: Path,
    args: list[str],
    *,
    output_limit: int,
    allow_nonzero: bool = False,
) -> tuple[bytes, bytes, int, bool]:
    stdout, stderr, returncode, truncated = _run_git_bounded(
        repo_root,
        args,
        stdout_limit=output_limit,
        stderr_limit=MAX_GIT_STDERR_BYTES,
        timeout_seconds=GIT_COMMAND_TIMEOUT_SECONDS,
    )
    if not truncated and returncode != 0 and not allow_nonzero:
        message = stderr.decode("utf-8", errors="replace").strip()
        raise GitReviewError(f"Git command failed ({args[0]}): {message or 'no diagnostic supplied'}")
    return stdout, stderr, returncode, truncated


def _pathspecs(entry: dict[str, Any]) -> list[str]:
    paths = [entry["path"]]
    original_path = entry.get("original_path")
    if original_path and original_path not in paths:
        paths.append(original_path)
    return [f":(literal){path}" for path in paths]


def _diff_args(entry: dict[str, Any], *, staged: bool) -> list[str]:
    args = [
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        "--no-color",
        "--no-renames",
        "--unified=3",
    ]
    if staged:
        args.extend(["--cached", "HEAD"])
    args.append("--")
    args.extend(_pathspecs(entry))
    return args


def _whitespace_warnings(repo_root: Path, entry: dict[str, Any], *, staged: bool) -> dict[str, Any]:
    args = ["diff", "--check", "--no-ext-diff", "--no-textconv", "--no-color"]
    if staged:
        args.extend(["--cached", "HEAD"])
    args.append("--")
    args.extend(_pathspecs(entry))

    stdout, stderr, returncode, truncated = _run_bounded_git(
        repo_root,
        args,
        output_limit=MAX_GIT_STDERR_BYTES,
        allow_nonzero=True,
    )
    diagnostics = stdout + stderr
    has_warning = bool(_WHITESPACE_WARNING.search(diagnostics))
    if truncated:
        return {"label": "Whitespace check output is incomplete", "details": "", "complete": False}
    if returncode != 0 and not has_warning:
        message = stderr.decode("utf-8", errors="replace").strip()
        raise GitReviewError(f"Git whitespace check failed: {message or 'no diagnostic supplied'}")
    if has_warning:
        return {
            "label": "Whitespace warning",
            "details": diagnostics.decode("utf-8", errors="replace").strip(),
            "complete": True,
        }
    return {"label": "No whitespace issue detected", "details": "", "complete": True}


def _tracked_diff(repo_root: Path, entry: dict[str, Any], *, staged: bool) -> dict[str, Any]:
    side = "Staged — index vs HEAD" if staged else "Unstaged — working tree vs index"
    stdout, _stderr, _returncode, truncated = _run_bounded_git(
        repo_root,
        _diff_args(entry, staged=staged),
        output_limit=MAX_DIFF_OUTPUT_BYTES,
    )
    if b"Binary files " in stdout or b"GIT binary patch" in stdout:
        evidence = {
            "side": side,
            "text": "",
            "available": False,
            "complete": not truncated,
            "reason": "Git reports binary content; a text patch is not available.",
            "truncated": truncated,
        }
    elif stdout:
        try:
            text = stdout.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            evidence = {
                "side": side,
                "text": "",
                "available": False,
                "complete": not truncated,
                "reason": (
                    "Diff exceeded the output limit and contains undecodable bytes; text evidence is unavailable."
                    if truncated
                    else "Diff contains undecodable bytes; text evidence is unavailable."
                ),
                "truncated": truncated,
            }
        else:
            evidence = {
                "side": side,
                "text": text,
                "available": True,
                "complete": not truncated,
                "reason": "Diff output exceeded the configured limit." if truncated else "",
                "truncated": truncated,
            }
    else:
        evidence = {
            "side": side,
            "text": "",
            "available": False,
            "complete": True,
            "reason": "Git reports this path as changed, but returned no ordinary text diff.",
            "truncated": False,
        }
    evidence["whitespace"] = _whitespace_warnings(repo_root, entry, staged=staged)
    return evidence


def _untracked_preview(repo_root: Path, path: str) -> dict[str, Any]:
    candidate = repo_root / path
    try:
        metadata = candidate.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            return {
                "available": False,
                "reason": "Symbolic-link preview is not supported.",
                "size_bytes": metadata.st_size,
            }
        resolved = candidate.resolve(strict=True)
        if not resolved.is_relative_to(repo_root):
            return {"available": False, "reason": "Path resolves outside the repository.", "size_bytes": None}
    except OSError:
        return {"available": False, "reason": "File is unavailable for preview.", "size_bytes": None}

    size_bytes = metadata.st_size
    if not stat.S_ISREG(metadata.st_mode):
        return {"available": False, "reason": "Only regular files can be previewed.", "size_bytes": size_bytes}

    try:
        fd = os.open(candidate, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode):
                return {"available": False, "reason": "Only regular files can be previewed.", "size_bytes": size_bytes}
            content = bytearray()
            while len(content) <= MAX_UNTRACKED_PREVIEW_BYTES:
                chunk = os.read(
                    fd,
                    min(16 * 1024, MAX_UNTRACKED_PREVIEW_BYTES + 1 - len(content)),
                )
                if not chunk:
                    break
                content.extend(chunk)
        finally:
            os.close(fd)
    except OSError:
        return {"available": False, "reason": "File could not be opened safely for preview.", "size_bytes": size_bytes}

    if b"\x00" in content:
        return {"available": False, "reason": "Binary content is not previewed.", "size_bytes": size_bytes}

    truncated = len(content) > MAX_UNTRACKED_PREVIEW_BYTES or size_bytes > MAX_UNTRACKED_PREVIEW_BYTES
    preview = bytes(content[:MAX_UNTRACKED_PREVIEW_BYTES])
    try:
        text = preview.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return {"available": False, "reason": "Content is not valid UTF-8 text.", "size_bytes": size_bytes}
    return {
        "available": True,
        "text": text,
        "size_bytes": size_bytes,
        "complete": not truncated,
        "reason": "Preview is truncated at the configured byte limit." if truncated else "",
    }


def _file_metadata(repo_root: Path, path: str) -> dict[str, Any]:
    try:
        metadata = (repo_root / path).lstat()
    except OSError:
        return {"size_bytes": None, "file_type": "unavailable"}
    if stat.S_ISLNK(metadata.st_mode):
        file_type = "symbolic link"
    elif stat.S_ISREG(metadata.st_mode):
        file_type = "regular file"
    else:
        file_type = "unsupported file type"
    return {"size_bytes": metadata.st_size, "file_type": file_type}


def inspect_git_review(repository_path: str, selected_file: str | None = None) -> dict[str, Any]:
    target = Path(repository_path).expanduser().resolve()
    try:
        repo_root = validate_git_worktree(target)
        git_state = inspect_git_state(
            str(repo_root),
            max_status_output_bytes=MAX_STATUS_OUTPUT_BYTES,
        )
    except (ScanError, WorkflowGitStateError) as exc:
        raise GitReviewError(str(exc)) from exc

    files: list[dict[str, Any]] = []
    entries_by_token: dict[str, dict[str, Any]] = {}
    for entry in git_state["working_tree"]["entries"]:
        token = _path_token(entry["path"])
        status = _file_status(entry)
        item = {
            "token": token,
            "path": entry["path"],
            "display_path": _display_path(entry["path"]),
            "original_path": entry.get("original_path"),
            "display_original_path": _display_path(entry["original_path"]) if entry.get("original_path") else None,
            "kind": entry["kind"],
            "xy": entry.get("xy") or ("??" if entry["kind"] == "untracked" else None),
            **status,
        }
        files.append(item)
        entries_by_token[token] = entry
    group_order = [
        "Conflicted / unmerged",
        "Untracked",
        "Staged + unstaged",
        "Staged",
        "Unstaged",
        "Changed state unavailable",
    ]
    grouped_files: dict[str, list[dict[str, Any]]] = {label: [] for label in group_order}
    for item in files:
        group_label = item["label"].split("; ", 1)[0]
        grouped_files.setdefault(group_label, []).append(item)
    groups = [
        {
            "label": _STATUS_DISPLAY_LABELS.get(label, label),
            "files": grouped_files[label],
        }
        for label in group_order
        if grouped_files[label]
    ]
    groups.extend(
        {"label": label, "files": grouped}
        for label, grouped in grouped_files.items()
        if label not in group_order and grouped
    )

    selected = None
    if selected_file is not None:
        entry = entries_by_token.get(selected_file)
        if entry is None:
            raise FileNotFoundError("The selected changed file is no longer present in current Git status.")
        status = _file_status(entry)
        path = entry["path"]
        selected = {
            "token": selected_file,
            "display_path": _display_path(path),
            "kind": entry["kind"],
            "status": status["label"],
            "display_status": status["display_label"],
            "xy": entry.get("xy") or ("??" if entry["kind"] == "untracked" else None),
            "metadata": _file_metadata(repo_root, path),
            "diffs": [],
            "preview": None,
            "conflict_note": None,
        }
        if status["conflicted"]:
            selected["conflict_note"] = (
                "Git reports this path as unmerged. Ordinary staged/unstaged diff evidence "
                "may be incomplete; conflict resolution is not available here."
            )
        elif entry["kind"] == "untracked":
            selected["preview"] = _untracked_preview(repo_root, path)
        else:
            if status["staged"]:
                selected["diffs"].append(_tracked_diff(repo_root, entry, staged=True))
            if status["unstaged"]:
                selected["diffs"].append(_tracked_diff(repo_root, entry, staged=False))

    upstream = git_state["upstream"]
    workflow_state_labels = {
        "clean": "Clean",
        "staged_only": "Staged changes",
        "unstaged_only": "Unstaged changes",
        "staged_and_unstaged": "Staged and unstaged changes",
        "conflicted": "Conflicts present",
    }
    return {
        "repository_root": str(repo_root),
        "repository_name": repo_root.name,
        "head": git_state["head"],
        "branch": git_state["branch"],
        "workflow_state": git_state["workflow_state"],
        "workflow_state_label": workflow_state_labels.get(git_state["workflow_state"], "Unknown"),
        "git_operations": git_state["git_operations"],
        "working_tree": git_state["working_tree"],
        "upstream": upstream,
        "upstream_freshness": (
            "Ahead/behind uses local tracking refs only; no remote refresh was performed."
        ),
        "files": files,
        "groups": groups,
        "selected": selected,
        "bounds": {
            "status_bytes": MAX_STATUS_OUTPUT_BYTES,
            "diff_bytes": MAX_DIFF_OUTPUT_BYTES,
            "untracked_preview_bytes": MAX_UNTRACKED_PREVIEW_BYTES,
        },
    }
