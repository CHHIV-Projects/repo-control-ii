from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..scanner.git_ops import ScanError, _run_git_bounded, validate_git_worktree
from ..scanner.util import make_repository_id
from .diff import GitReviewError, inspect_git_review
from .git_state import WorkflowGitStateError, inspect_git_state
from .git_history import GitHistoryError, inspect_branches

MAX_STATUS_BYTES = 1024 * 1024
MAX_INDEX_OUTPUT_BYTES = 16 * 1024 * 1024
MAX_ACTION_OUTPUT_BYTES = 64 * 1024
MAX_COMMIT_DIFF_BYTES = 512 * 1024
MAX_COMMIT_PATHS = 256
MAX_ACTION_PATHS = 256
MAX_ACTION_PATH_BYTES = 32 * 1024
MAX_FILE_HASH_BYTES = 256 * 1024 * 1024
ACTION_TTL_SECONDS = 300
MAX_PREPARED_ACTIONS = 64
_ACTIONS = {"stage", "unstage", "restore-unstaged", "restore-head", "commit"}
_BRANCH_REMOTE_ACTIONS = {
    "create-branch",
    "switch-branch",
    "delete-branch",
    "fetch",
    "push",
    "publish-branch",
    "fast-forward",
}
_REMOTE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_OBJECT_ID = re.compile(r"^[0-9a-f]{40,64}$")
_REMOTE_URL = re.compile(r"(?P<url>[A-Za-z][A-Za-z0-9+.-]*://[^\s'\"<>]+)")
_SCP_REMOTE = re.compile(r"(?P<credentials>[^/@:\s]+(?::[^/@\s]*)?@)(?P<host>[^:/\s]+):(?P<path>[^\s]+)")
MAX_REMOTE_OUTPUT_BYTES = 128 * 1024
MAX_REMOTE_RUNTIME_SECONDS = 60
_NO_FOLLOW = getattr(os, "O_NOFOLLOW", 0)


class GitActionError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int = 409) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(message)


@dataclass
class PreparedAction:
    token: str
    action: str
    repository_id: str
    repository_root: str
    created_at: float
    expires_at: float
    state: dict[str, Any]
    preview: dict[str, Any]


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _run_read(
    repo_root: Path,
    args: list[str],
    *,
    limit: int = MAX_INDEX_OUTPUT_BYTES,
    allow_nonzero: bool = False,
) -> tuple[bytes, bytes, int]:
    try:
        stdout, stderr, returncode, truncated = _run_git_bounded(
            repo_root,
            args,
            stdout_limit=limit,
            stderr_limit=16 * 1024,
            timeout_seconds=10,
        )
    except ScanError as exc:
        raise GitActionError("git_inspection_failed", str(exc), 503) from exc
    if truncated:
        raise GitActionError("git_evidence_incomplete", f"Git {args[0]} output exceeded its configured limit.", 503)
    if returncode and not allow_nonzero:
        message = stderr.decode("utf-8", errors="replace").strip()
        raise GitActionError("git_inspection_failed", f"Git {args[0]} failed: {message or 'no diagnostic supplied'}", 503)
    return stdout, stderr, returncode


def _run_mutation(
    repo_root: Path,
    args: list[str],
    *,
    timeout_seconds: float = 30,
    noninteractive_remote: bool = False,
) -> tuple[int, bytes, bytes]:
    env_overrides = None
    if noninteractive_remote:
        env_overrides = _noninteractive_remote_environment()
    try:
        stdout, stderr, returncode, truncated = _run_git_bounded(
            repo_root,
            args,
            stdout_limit=MAX_ACTION_OUTPUT_BYTES,
            stderr_limit=MAX_ACTION_OUTPUT_BYTES,
            timeout_seconds=timeout_seconds,
            env_overrides=env_overrides,
        )
    except ScanError as exc:
        raise GitActionError("git_action_execution_failed", str(exc), 503) from exc
    if truncated:
        raise GitActionError(
            "git_action_output_incomplete",
            "Git action output exceeded its configured limit; inspect Git Review before retrying.",
            503,
        )
    return returncode, stdout, stderr


def _state(repo_root: Path) -> dict[str, Any]:
    try:
        result = inspect_git_state(str(repo_root), max_status_output_bytes=MAX_STATUS_BYTES)
    except (ScanError, WorkflowGitStateError) as exc:
        raise GitActionError("git_state_unavailable", str(exc), 503) from exc
    if result["git_operation_in_progress"]:
        operations = ", ".join(result["git_operations"])
        raise GitActionError("git_operation_in_progress", f"Git operation in progress ({operations}); local actions are disabled.")
    if result["working_tree"]["unmerged"]["count"]:
        raise GitActionError("conflicts_present", "Unresolved conflicts are present; local actions are disabled.")
    if result["branch"]["state"] != "attached":
        raise GitActionError("detached_head", "Local actions are unavailable while HEAD is detached.")
    return result


def _status_entry(state: dict[str, Any], path: str) -> dict[str, Any] | None:
    matches = [
        entry
        for entry in state["working_tree"]["entries"]
        if entry["path"] == path or entry.get("original_path") == path
    ]
    if any(entry["kind"] == "rename_or_copy" for entry in matches):
        raise GitActionError("unsupported_rename", "Rename/copy paths are not supported by these local actions.")
    if len(matches) > 1:
        raise GitActionError("ambiguous_path", "The selected path has ambiguous Git status.")
    return matches[0] if matches else None


def _index_records(repo_root: Path, path: str | None = None) -> bytes:
    args = ["ls-files", "--stage", "-z"]
    if path is not None:
        args.extend(["--", f":(literal){path}"])
    output, _stderr, _code = _run_read(repo_root, args)
    return output


def _file_identity(repo_root: Path, path: str) -> dict[str, Any]:
    candidate = repo_root / path
    try:
        metadata = candidate.lstat()
    except FileNotFoundError:
        return {"state": "absent"}
    except OSError as exc:
        raise GitActionError("path_unavailable", f"Selected path cannot be inspected: {exc}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise GitActionError("unsupported_file_type", "Only ordinary regular files and tracked deletions are supported.")
    if metadata.st_size > MAX_FILE_HASH_BYTES:
        raise GitActionError("file_too_large", "Selected file exceeds the safe action identity size limit.")
    digest = hashlib.sha256()
    total = 0
    try:
        fd = os.open(candidate, os.O_RDONLY | _NO_FOLLOW)
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode):
                raise GitActionError("unsupported_file_type", "Selected path is not a regular file.")
            while True:
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_FILE_HASH_BYTES:
                    raise GitActionError("file_too_large", "Selected file exceeds the safe action identity size limit.")
                digest.update(chunk)
        finally:
            os.close(fd)
    except GitActionError:
        raise
    except OSError as exc:
        raise GitActionError("path_unavailable", f"Selected path cannot be read safely: {exc}") from exc
    return {
        "state": "present",
        "mode": stat.S_IMODE(metadata.st_mode),
        "size": total,
        "sha256": digest.hexdigest(),
    }


def _head_entry(repo_root: Path, path: str) -> bytes:
    output, _stderr, code = _run_read(
        repo_root,
        ["ls-tree", "-z", "HEAD", "--", f":(literal){path}"],
        allow_nonzero=True,
    )
    if code:
        raise GitActionError("head_path_unavailable", "Git could not inspect the selected path at HEAD.")
    return output


def _assert_no_custom_filter(repo_root: Path, path: str) -> None:
    output, _stderr, code = _run_read(
        repo_root,
        ["check-attr", "-z", "filter", "--", path],
    )
    if code:
        raise GitActionError("filter_inspection_failed", "Git could not inspect clean filters for the selected path.", 503)
    fields = output.split(b"\x00")
    if fields and fields[-1] == b"":
        fields.pop()
    if len(fields) != 3 or fields[1] != b"filter":
        raise GitActionError("filter_inspection_failed", "Git returned invalid clean-filter evidence.", 503)
    if fields[2] not in {b"unspecified", b"unset"}:
        raise GitActionError("unsupported_clean_filter", "A configured clean filter may alter staged content; this path is unsupported.")


def _capture_path(
    repo_root: Path,
    path: str,
    *,
    state: dict[str, Any] | None = None,
    index_state: str | None = None,
) -> dict[str, Any]:
    if state is None:
        state = _state(repo_root)
    entry = _status_entry(state, path)
    if entry is None:
        raise GitActionError("path_not_changed", "The selected path is no longer changed.")
    if entry["kind"] not in {"ordinary", "untracked"}:
        raise GitActionError("unsupported_path_state", "This path state is not supported by local actions.")
    if entry.get("submodule") not in {None, "N..."}:
        raise GitActionError("unsupported_submodule", "Submodule changes are not supported by local actions.")
    _assert_no_custom_filter(repo_root, path)
    return {
        "repository_id": make_repository_id(repo_root),
        "repository_root": str(repo_root),
        "head": state["head"],
        "branch": state["branch"]["name"],
        "status": {
            "kind": entry["kind"],
            "xy": entry.get("xy"),
            "path": entry["path"],
        },
        "index_state": index_state or _digest(_index_records(repo_root)),
        "index_path_records": _digest(_index_records(repo_root, path)),
        "worktree": _file_identity(repo_root, path),
        "head_path_records": _digest(_head_entry(repo_root, path)),
        "path": path,
    }


def _capture_paths(repo_root: Path, paths: list[str]) -> dict[str, Any]:
    if not paths:
        raise GitActionError("path_required", "Select at least one changed path for this action.", 400)
    if len(paths) > MAX_ACTION_PATHS or sum(len(os.fsencode(path)) for path in paths) > MAX_ACTION_PATH_BYTES:
        raise GitActionError("path_set_too_large", "The selected path set exceeds the safe action limit.")
    if len(set(paths)) != len(paths):
        raise GitActionError("duplicate_path", "The selected path set contains duplicate paths.", 400)
    ordered_paths = sorted(paths, key=os.fsencode)
    state = _state(repo_root)
    index_state = _digest(_index_records(repo_root))
    captured = [
        _capture_path(repo_root, path, state=state, index_state=index_state)
        for path in ordered_paths
    ]
    baseline = captured[0]
    for item in captured[1:]:
        for key in ("repository_id", "repository_root", "head", "branch", "index_state"):
            if item[key] != baseline[key]:
                raise GitActionError("stale_prepared_action", "Repository state changed while preparing the selected path set.")
    return {
        "repository_id": baseline["repository_id"],
        "repository_root": baseline["repository_root"],
        "head": baseline["head"],
        "branch": baseline["branch"],
        "index_state": baseline["index_state"],
        "paths": captured,
    }


def _capture_commit(repo_root: Path) -> dict[str, Any]:
    state = _state(repo_root)
    if state["working_tree"]["staged"]["count"] == 0:
        raise GitActionError("nothing_staged", "There are no explicitly staged changes to commit.")
    raw_index = _index_records(repo_root)
    staged_raw, _stderr, _code = _run_read(
        repo_root,
        ["diff-index", "--cached", "--raw", "--no-renames", "--abbrev=40", "-z", "HEAD", "--"],
    )
    patch, _stderr, _code = _run_read(
        repo_root,
        ["diff", "--cached", "HEAD", "--no-ext-diff", "--no-textconv", "--no-color", "--no-renames", "--unified=3", "--"],
        limit=MAX_COMMIT_DIFF_BYTES,
    )
    if b"Binary files " in patch or b"GIT binary patch" in patch:
        raise GitActionError("binary_commit_evidence", "Staged binary changes cannot be fully reviewed by this text-only commit screen.")
    try:
        patch_text = patch.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise GitActionError("commit_evidence_unavailable", "Staged diff contains undecodable bytes; commit is unavailable.") from exc
    entries = state["working_tree"]["entries"]
    staged_paths = []
    remaining_paths = []
    for entry in entries:
        xy = entry.get("xy") or ""
        if entry["kind"] == "unmerged":
            raise GitActionError("conflicts_present", "Unresolved conflicts are present; commit is unavailable.")
        if xy and xy[0] != ".":
            staged_paths.append({"path": entry["path"], "xy": xy, "kind": entry["kind"]})
        if entry["kind"] == "untracked" or (xy and xy[1] != "."):
            remaining_paths.append({"path": entry["path"], "kind": entry["kind"], "xy": xy})
    if len(staged_paths) > MAX_COMMIT_PATHS or len(remaining_paths) > MAX_COMMIT_PATHS:
        raise GitActionError("commit_inventory_too_large", "Changed-file inventory exceeds the commit review limit.")
    return {
        "repository_id": make_repository_id(repo_root),
        "repository_root": str(repo_root),
        "head": state["head"],
        "branch": state["branch"]["name"],
        "index": _digest(raw_index),
        "staged_raw": _digest(staged_raw),
        "staged_paths": staged_paths,
        "remaining_paths": remaining_paths,
        "patch": patch_text,
        "patch_digest": _digest(patch),
        "git_operations": state["git_operations"],
    }


def _assert_hooks_supported(repo_root: Path) -> None:
    output, _stderr, code = _run_read(repo_root, ["config", "--get", "core.hooksPath"], allow_nonzero=True)
    if code == 0 and output.strip():
        raise GitActionError("unsupported_hooks", "A configured custom core.hooksPath is present; commit is unavailable.")
    hooks_path, _stderr, code = _run_read(repo_root, ["rev-parse", "--git-path", "hooks"])
    if code:
        raise GitActionError("unsupported_hooks", "Git hooks directory cannot be determined.")
    hooks_dir = Path(os.fsdecode(hooks_path).strip())
    if not hooks_dir.is_absolute():
        hooks_dir = repo_root / hooks_dir
    active = []
    for name in ("pre-commit", "prepare-commit-msg", "commit-msg", "post-commit"):
        candidate = hooks_dir / name
        try:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                active.append(name)
        except OSError as exc:
            raise GitActionError("unsupported_hooks", f"Git hook {name} cannot be inspected: {exc}") from exc
    if active:
        raise GitActionError("unsupported_hooks", f"Executable commit hooks are present: {', '.join(active)}.")


def _safe_review(repo_root: Path, path_token: str) -> tuple[dict[str, Any], str]:
    try:
        review = inspect_git_review(str(repo_root), selected_file=path_token)
    except (GitReviewError, FileNotFoundError, ValueError) as exc:
        raise GitActionError("review_evidence_unavailable", str(exc), 409) from exc
    selected = review.get("selected")
    if not selected:
        raise GitActionError("path_not_found", "Select a changed path before preparing an action.", 404)
    return review, _raw_path_from_token(repo_root, path_token)


def _raw_selected_path(repo_root: Path, path_token: str) -> str:
    state = _state(repo_root)
    matches = [entry for entry in state["working_tree"]["entries"] if _path_token(entry["path"]) == path_token]
    if len(matches) != 1:
        raise GitActionError("path_not_found", "The selected changed path is no longer available.", 404)
    return matches[0]["path"]


def _raw_path_from_token(repo_root: Path, path_token: str) -> str:
    return _raw_selected_path(repo_root, path_token)


def _path_preview(repo_root: Path, action: str, path: str) -> dict[str, Any]:
    state = _state(repo_root)
    entry = _status_entry(state, path)
    if entry is None:
        raise GitActionError("path_not_changed", "The selected path is no longer changed.")
    if action == "restore-head" and not _head_entry(repo_root, path):
        raise GitActionError("head_path_missing", "The selected path does not exist at HEAD; restore-to-HEAD is unavailable.")
    xy = entry.get("xy") or ""
    if action == "stage":
        if entry["kind"] == "untracked":
            preview = inspect_git_review(str(repo_root), selected_file=_path_token(path))["selected"]
            evidence = preview["preview"]
            return {
                "title": "Stage selected path",
                "summary": "This will add the selected untracked file to the index. It will not stage other paths.",
                "layers": ["index"],
                "path": path,
                "evidence": evidence,
            }
        review, _display = _safe_review(repo_root, _path_token(path))
        selected = review["selected"]
        return {
            "title": "Stage selected path",
            "summary": "This will update the index for this path from its current worktree content. Existing staged content on this path will be replaced by the selected current version.",
            "layers": ["index"],
            "path": path,
            "diffs": selected["diffs"],
        }
    review, _display = _safe_review(repo_root, _path_token(path))
    selected = review["selected"]
    key = "unstage" if action == "unstage" else "restore-unstaged" if action == "restore-unstaged" else "restore-head"
    item = next((candidate for candidate in selected["action_previews"] if candidate["key"] == key), None)
    if item is None or not item.get("available") or not item.get("complete"):
        raise GitActionError("preview_unavailable", "Complete supported preview evidence is required for this action.")
    return {
        "title": item["title"],
        "summary": item["comparison"],
        "layers": item["affected_layers"],
        "path": path,
        "preview": item,
    }


def _path_token(path: str) -> str:
    return base64.urlsafe_b64encode(os.fsencode(path)).decode("ascii").rstrip("=")


def _commit_message(message: str | None) -> str:
    if message is None or not message.strip():
        raise GitActionError("invalid_commit_message", "Commit message must not be empty.", 400)
    if "\x00" in message or "\n" in message or "\r" in message:
        raise GitActionError("invalid_commit_message", "Use a single-line commit subject without control characters.", 400)
    if len(message) > 500:
        raise GitActionError("invalid_commit_message", "Commit subject exceeds 500 characters.", 400)
    if any(ord(character) < 32 or ord(character) == 127 for character in message):
        raise GitActionError("invalid_commit_message", "Commit subject contains unsupported control characters.", 400)
    return message


def _sanitize_url(value: str) -> str:
    try:
        parts = urlsplit(value)
    except ValueError:
        return "Configured remote URL (format unsupported; credentials hidden)"
    if not parts.scheme or not parts.netloc:
        scp = _SCP_REMOTE.fullmatch(value)
        if scp:
            return f"{scp.group('host')}:{scp.group('path')}"
        return value
    host = parts.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        port = f":{parts.port}" if parts.port is not None else ""
    except ValueError:
        port = ""
    return f"{parts.scheme}://{host}{port}{parts.path}"


def _sanitize_diagnostic(value: str) -> str:
    def redact(match: re.Match[str]) -> str:
        return _sanitize_url(match.group("url").rstrip(".,:;"))

    sanitized = _REMOTE_URL.sub(redact, value)
    return _SCP_REMOTE.sub(lambda match: f"{match.group('host')}:{match.group('path')}", sanitized).strip()


def _push_failure_message(diagnostic: str) -> str | None:
    lowered = diagnostic.casefold()
    non_fast_forward = (
        "non-fast-forward" in lowered
        or "fetch first" in lowered
        or "failed to push some refs" in lowered
        or "updates were rejected" in lowered
    )
    if not non_fast_forward:
        return None
    return (
        "Push was rejected because the remote contains commits not present locally. "
        "Fetch and inspect the divergence. Automatic merge/rebase resolution is outside "
        "Repo Control's current supported workflow."
    )


def _noninteractive_remote_environment() -> dict[str, str]:
    environment = {
        "GIT_ASKPASS": "/bin/false",
        "SSH_ASKPASS": "/bin/false",
    }
    ssh_command = os.environ.get("GIT_SSH_COMMAND")
    if ssh_command:
        environment["GIT_SSH_COMMAND"] = f"{ssh_command} -oBatchMode=yes"
    elif not os.environ.get("GIT_SSH"):
        environment["GIT_SSH_COMMAND"] = "ssh -oBatchMode=yes"
    return environment


def _run_remote_read(
    repo_root: Path,
    args: list[str],
    *,
    limit: int = MAX_REMOTE_OUTPUT_BYTES,
    allow_nonzero: bool = False,
) -> tuple[bytes, bytes, int]:
    try:
        stdout, stderr, returncode, truncated = _run_git_bounded(
            repo_root,
            args,
            stdout_limit=limit,
            stderr_limit=MAX_ACTION_OUTPUT_BYTES,
            timeout_seconds=MAX_REMOTE_RUNTIME_SECONDS,
            env_overrides=_noninteractive_remote_environment(),
        )
    except ScanError as exc:
        raise GitActionError("remote_inspection_failed", _sanitize_diagnostic(str(exc)), 503) from exc
    if truncated:
        raise GitActionError("remote_evidence_incomplete", f"Git {args[0]} output exceeded its configured limit.", 503)
    if returncode and not allow_nonzero:
        message = _sanitize_diagnostic(stderr.decode("utf-8", errors="replace"))
        raise GitActionError(
            "remote_inspection_failed",
            f"Git {args[0]} failed: {message or 'no diagnostic supplied'}",
            503,
        )
    return stdout, stderr, returncode


def _operation_state(repo_root: Path) -> dict[str, Any]:
    try:
        state = inspect_git_state(str(repo_root), max_status_output_bytes=MAX_STATUS_BYTES)
    except (ScanError, WorkflowGitStateError) as exc:
        raise GitActionError("git_state_unavailable", str(exc), 503) from exc
    if state["git_operation_in_progress"]:
        operations = ", ".join(state["git_operations"])
        raise GitActionError("git_operation_in_progress", f"Git operation in progress ({operations}); branch and remote actions are disabled.")
    if state["working_tree"]["unmerged"]["count"]:
        raise GitActionError("conflicts_present", "Unresolved conflicts are present; branch and remote actions are disabled.")
    return state


def _branch_list(repo_root: Path) -> list[dict[str, Any]]:
    try:
        return inspect_branches(str(repo_root))["branches"]
    except (GitHistoryError, ScanError) as exc:
        raise GitActionError("branch_inventory_unavailable", str(exc), 503) from exc


def _branch_by_token(repo_root: Path, token: str | None) -> dict[str, Any]:
    if not token:
        raise GitActionError("branch_required", "Select a local branch from the current branch inventory.", 400)
    branch = next((item for item in _branch_list(repo_root) if item["token"] == token), None)
    if branch is None:
        raise GitActionError("branch_not_found", "Selected local branch is no longer available; review the branch inventory again.", 404)
    return branch


def _remote_name_is_supported(name: str) -> bool:
    return bool(_REMOTE_NAME.fullmatch(name))


def _remote_inventory(repo_root: Path) -> list[dict[str, Any]]:
    output, _stderr, _code = _run_read(repo_root, ["remote"], limit=MAX_REMOTE_OUTPUT_BYTES)
    try:
        names = output.decode("utf-8", errors="strict").splitlines()
    except UnicodeDecodeError as exc:
        raise GitActionError("remote_inventory_unavailable", "Configured remote names are not valid UTF-8.", 503) from exc
    if len(names) > 64 or any(not _remote_name_is_supported(name) for name in names):
        raise GitActionError("remote_inventory_unsupported", "A configured remote name is unsupported; no remote action is offered.")
    if len(names) != len(set(names)):
        raise GitActionError("remote_inventory_invalid", "Git returned duplicate configured remote names.", 503)
    remotes = []
    for name in names:
        remotes.append(
            {
                "name": name,
                "token": base64.urlsafe_b64encode(name.encode("utf-8")).decode("ascii").rstrip("="),
            }
        )
    return remotes


def _remote_urls(repo_root: Path, remote: str, *, push: bool = False) -> list[str]:
    if not _remote_name_is_supported(remote):
        raise GitActionError("remote_invalid", "Configured remote name is unsupported.")
    args = ["remote", "get-url"]
    if push:
        args.append("--push")
    args.extend(["--all", remote])
    try:
        output, _stderr, _code = _run_read(repo_root, args, limit=MAX_REMOTE_OUTPUT_BYTES)
    except GitActionError as exc:
        raise GitActionError(exc.code, _sanitize_diagnostic(str(exc)), exc.status_code) from exc
    try:
        urls = [line for line in output.decode("utf-8", errors="strict").splitlines() if line]
    except UnicodeDecodeError as exc:
        raise GitActionError("remote_url_unavailable", "A configured remote URL cannot be safely displayed.", 503) from exc
    if not urls:
        raise GitActionError("remote_url_unavailable", f"Configured remote {remote} has no usable URL.")
    return urls


def _remote_config_values(repo_root: Path, remote: str, key: str) -> list[str]:
    output, _stderr, code = _run_read(
        repo_root,
        ["config", "--null", "--get-all", f"remote.{remote}.{key}"],
        limit=MAX_REMOTE_OUTPUT_BYTES,
        allow_nonzero=True,
    )
    if code == 1:
        return []
    if code != 0:
        raise GitActionError("remote_configuration_unavailable", f"Could not inspect remote {remote} configuration.", 503)
    fields = output.split(b"\x00")
    if fields and fields[-1] == b"":
        fields.pop()
    try:
        return [field.decode("utf-8", errors="strict") for field in fields]
    except UnicodeDecodeError as exc:
        raise GitActionError("remote_configuration_unsupported", "Remote configuration contains unsupported bytes.") from exc


def _assert_safe_fetch_refspecs(repo_root: Path, remote: str) -> list[str]:
    refspecs = _remote_config_values(repo_root, remote, "fetch")
    if not refspecs:
        raise GitActionError("unsafe_fetch_configuration", f"Remote {remote} has no configured fetch refspec.")
    for configured in refspecs:
        refspec = configured[1:] if configured.startswith("+") else configured
        if refspec.startswith("^") or refspec.count(":") != 1:
            raise GitActionError("unsafe_fetch_configuration", f"Remote {remote} has a fetch refspec outside remote-tracking scope.")
        source, destination = refspec.split(":", 1)
        expected_destination = f"refs/remotes/{remote}/"
        if source == "refs/heads/*" and destination == f"{expected_destination}*":
            continue
        source_prefix = "refs/heads/"
        if (
            not source.startswith(source_prefix)
            or not destination.startswith(expected_destination)
            or "*" in source
            or "*" in destination
            or source.removeprefix(source_prefix) != destination.removeprefix(expected_destination)
            or any(character in refspec for character in ("?", "[", "]", "\n", "\r"))
        ):
            raise GitActionError("unsafe_fetch_configuration", f"Remote {remote} fetch refspec could update refs outside its remote-tracking namespace.")
        _run_read(
            repo_root,
            ["check-ref-format", source],
            limit=256,
        )
    return refspecs


def _remote_mirror_enabled(repo_root: Path, remote: str) -> bool:
    output, _stderr, code = _run_read(
        repo_root,
        ["config", "--bool", "--get", f"remote.{remote}.mirror"],
        limit=128,
        allow_nonzero=True,
    )
    if code == 1:
        return False
    if code != 0:
        raise GitActionError("remote_configuration_unavailable", f"Could not inspect remote {remote} mirror configuration.", 503)
    value = output.decode("ascii", errors="strict").strip()
    if value not in {"true", "false"}:
        raise GitActionError("remote_configuration_invalid", f"Remote {remote} mirror setting is invalid.", 503)
    return value == "true"


def _remote_by_token(repo_root: Path, token: str | None, *, select_default: bool) -> dict[str, Any]:
    remotes = _remote_inventory(repo_root)
    if token:
        selected = next((item for item in remotes if item["token"] == token), None)
        if selected is None:
            raise GitActionError("remote_not_found", "Selected remote is no longer configured; review the configured remote list.", 404)
        return selected
    if not select_default:
        raise GitActionError("remote_required", "Select a configured remote.", 400)
    origin = next((item for item in remotes if item["name"] == "origin"), None)
    if origin:
        return origin
    if len(remotes) == 1:
        return remotes[0]
    if not remotes:
        raise GitActionError("remote_unavailable", "No configured remote is available.")
    raise GitActionError("remote_selection_required", "Select a configured remote; no default is safe when multiple non-origin remotes exist.", 400)


def _remote_facts(repo_root: Path, remote: str) -> dict[str, Any]:
    fetch_urls = _remote_urls(repo_root, remote)
    push_urls = _remote_urls(repo_root, remote, push=True)
    return {
        "name": remote,
        "fetch_urls": fetch_urls,
        "push_urls": push_urls,
        "fetch_refspecs": _remote_config_values(repo_root, remote, "fetch"),
        "mirror": _remote_mirror_enabled(repo_root, remote),
        "display_fetch_urls": [_sanitize_url(url) for url in fetch_urls],
        "display_push_urls": [_sanitize_url(url) for url in push_urls],
    }


def _assert_branch_remote_hooks_supported(repo_root: Path, action: str) -> None:
    custom_path, _stderr, code = _run_read(
        repo_root,
        ["config", "--get", "core.hooksPath"],
        limit=4096,
        allow_nonzero=True,
    )
    if code == 0 and custom_path.strip():
        raise GitActionError("unsupported_hooks", "A configured custom core.hooksPath is present; branch and remote actions are unavailable.")
    if code not in {0, 1}:
        raise GitActionError("unsupported_hooks", "Git hook configuration cannot be inspected.")
    hooks_path, _stderr, code = _run_read(repo_root, ["rev-parse", "--git-path", "hooks"], limit=4096)
    if code:
        raise GitActionError("unsupported_hooks", "Git hooks directory cannot be determined.")
    hooks_dir = Path(os.fsdecode(hooks_path).strip())
    if not hooks_dir.is_absolute():
        hooks_dir = repo_root / hooks_dir
    hooks_by_action = {
        "create-branch": ("reference-transaction",),
        "switch-branch": ("post-checkout", "reference-transaction"),
        "delete-branch": ("reference-transaction",),
        "fetch": ("post-fetch", "reference-transaction"),
        "push": ("pre-push", "reference-transaction"),
        "publish-branch": ("pre-push", "reference-transaction"),
        "fast-forward": ("post-merge", "reference-transaction"),
    }
    active = []
    for name in hooks_by_action[action]:
        try:
            if (hooks_dir / name).is_file() and os.access(hooks_dir / name, os.X_OK):
                active.append(name)
        except OSError as exc:
            raise GitActionError("unsupported_hooks", f"Git hook {name} cannot be inspected: {exc}") from exc
    if active:
        raise GitActionError("unsupported_hooks", f"Executable Git hooks are present: {', '.join(active)}.")


def _resolve_commit(repo_root: Path, ref: str) -> str | None:
    output, _stderr, code = _run_read(
        repo_root,
        ["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
        limit=256,
        allow_nonzero=True,
    )
    if code == 1:
        return None
    if code != 0:
        raise GitActionError("git_ref_inspection_failed", f"Git could not inspect {ref}.", 503)
    oid = output.decode("ascii", errors="strict").strip()
    if not _OBJECT_ID.fullmatch(oid):
        raise GitActionError("git_ref_inspection_failed", "Git returned an invalid object ID.", 503)
    return oid


def _current_ref(state: dict[str, Any]) -> str | None:
    name = state["branch"]["name"]
    return f"refs/heads/{name}" if name else None


def _branch_name(repo_root: Path, value: str | None) -> str:
    name = value or ""
    if not name or len(os.fsencode(name)) > 255 or any(ord(char) < 32 or ord(char) == 127 for char in name):
        raise GitActionError("invalid_branch_name", "Enter a valid branch name of at most 255 bytes.", 400)
    output, _stderr, code = _run_read(
        repo_root,
        ["check-ref-format", "--branch", name],
        limit=1024,
        allow_nonzero=True,
    )
    if code != 0 or output.decode("utf-8", errors="replace").strip() != name or name.startswith("-"):
        raise GitActionError("invalid_branch_name", "Git rejected the branch name; enter one literal local branch name.", 400)
    if name == "main":
        raise GitActionError("protected_branch", "The main branch name cannot be created through this action.")
    return name


def _index_fingerprint(repo_root: Path) -> str:
    return _digest(_index_records(repo_root))


def _ref_counts(repo_root: Path, left: str, right: str) -> tuple[int, int]:
    output, _stderr, _code = _run_read(
        repo_root,
        ["rev-list", "--left-right", "--count", f"{left}...{right}"],
        limit=256,
    )
    try:
        first, second = output.decode("ascii").split()
        return int(first), int(second)
    except (UnicodeDecodeError, ValueError) as exc:
        raise GitActionError("branch_comparison_unavailable", "Git returned invalid branch ancestry counts.", 503) from exc


def _is_ancestor(repo_root: Path, candidate: str, reference: str) -> bool:
    _output, stderr, code = _run_read(
        repo_root,
        ["merge-base", "--is-ancestor", candidate, reference],
        limit=256,
        allow_nonzero=True,
    )
    if code == 0:
        return True
    if code == 1:
        return False
    message = _sanitize_diagnostic(stderr.decode("utf-8", errors="replace"))
    raise GitActionError("branch_comparison_unavailable", f"Git ancestry check failed: {message or 'no diagnostic supplied'}", 503)


def _upstream_parts(repo_root: Path, upstream: str) -> tuple[str, str]:
    prefix = "refs/remotes/"
    if not upstream.startswith(prefix):
        raise GitActionError("unsupported_upstream", "Only a configured remote-tracking upstream is supported.")
    remainder = upstream[len(prefix) :]
    remote, separator, branch = remainder.partition("/")
    if not separator or not branch or not _remote_name_is_supported(remote):
        raise GitActionError("unsupported_upstream", "Configured upstream does not identify a supported remote branch.")
    if branch.startswith("-") or any(ord(char) < 32 or ord(char) == 127 for char in branch):
        raise GitActionError("unsupported_upstream", "Configured upstream branch is not supported.")
    return remote, branch


def _remote_tracking_refs(repo_root: Path, remote: str) -> list[tuple[str, str]]:
    output, _stderr, _code = _run_read(
        repo_root,
        ["for-each-ref", "--format=%(refname)%00%(objectname)", f"refs/remotes/{remote}/"],
        limit=MAX_REMOTE_OUTPUT_BYTES,
    )
    refs: list[tuple[str, str]] = []
    for line in output.splitlines():
        fields = line.split(b"\x00")
        if len(fields) != 2:
            raise GitActionError("remote_tracking_state_invalid", "Git returned invalid remote-tracking-ref evidence.", 503)
        ref, oid = (field.decode("utf-8", errors="strict") for field in fields)
        if not ref.startswith(f"refs/remotes/{remote}/") or not _OBJECT_ID.fullmatch(oid):
            raise GitActionError("remote_tracking_state_invalid", "Git returned invalid remote-tracking-ref evidence.", 503)
        refs.append((ref, oid))
    return sorted(refs)


def _ls_remote_tip(repo_root: Path, remote: str, branch_ref: str) -> str | None:
    output, stderr, code = _run_remote_read(
        repo_root,
        ["ls-remote", "--heads", "--", remote, branch_ref],
        limit=16 * 1024,
        allow_nonzero=True,
    )
    if code:
        diagnostic = _sanitize_diagnostic(stderr.decode("utf-8", errors="replace"))
        raise GitActionError(
            "remote_inspection_failed",
            f"Could not query the configured remote branch: {diagnostic or 'remote command failed'}",
            503,
        )
    lines = output.splitlines()
    if not lines:
        return None
    if len(lines) != 1:
        raise GitActionError("remote_branch_ambiguous", "Remote returned multiple matching branch refs; operation is refused.")
    fields = lines[0].split(b"\t")
    try:
        returned_ref = fields[1].decode("utf-8", errors="strict") if len(fields) == 2 else ""
        oid = fields[0].decode("ascii", errors="strict") if len(fields) == 2 else ""
    except UnicodeDecodeError as exc:
        raise GitActionError("remote_branch_invalid", "Remote returned undecodable branch evidence.", 503) from exc
    if len(fields) != 2 or returned_ref != branch_ref:
        raise GitActionError("remote_branch_ambiguous", "Remote returned an unexpected branch ref; operation is refused.")
    if not _OBJECT_ID.fullmatch(oid):
        raise GitActionError("remote_branch_invalid", "Remote returned an invalid branch object ID.", 503)
    return oid


def _common_operation_state(repo_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    state = _operation_state(repo_root)
    common = {
        "head": state["head"],
        "branch": state["branch"]["name"],
        "status": state["working_tree"]["entries"],
        "index": _index_fingerprint(repo_root),
    }
    return state, common


def _capture_branch_remote_action(
    repo_root: Path,
    action: str,
    *,
    branch_token: str | None = None,
    branch_name: str | None = None,
    remote_token: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    _assert_branch_remote_hooks_supported(repo_root, action)
    state, common = _common_operation_state(repo_root)
    current_ref = _current_ref(state)
    current_branch = state["branch"]["name"] or f"(detached at {state['head'][:12]})"
    branches = _branch_list(repo_root)
    preview: dict[str, Any] = {
        "action": action,
        "repository_root": str(repo_root),
        "branch": current_branch,
        "head": state["head"],
        "warning": "",
    }

    if action == "create-branch":
        name = _branch_name(repo_root, branch_name)
        if any(branch["name"] == name for branch in branches):
            raise GitActionError("branch_exists", f"Local branch {name} already exists.")
        facts = {**common, "name": name}
        preview.update(
            {
                "title": "Create local branch",
                "branch_name": name,
                "source": state["head"],
                "source_branch": current_branch,
                "remote_impact": "None. Creation does not switch branches or configure an upstream.",
            }
        )
        return facts, preview

    if action == "switch-branch":
        if not state["working_tree"]["is_clean"]:
            raise GitActionError("dirty_worktree", "Switching requires a completely clean working tree, including index and untracked paths.")
        target = _branch_by_token(repo_root, branch_token)
        if target["ref"] == current_ref:
            raise GitActionError("branch_already_current", "The selected local branch is already current.")
        facts = {
            **common,
            "branch_token": branch_token,
            "target_ref": target["ref"],
            "target_name": target["name"],
            "target_oid": target["oid"],
        }
        preview.update(
            {
                "title": "Switch local branch",
                "target_branch": target["name"],
                "target_head": target["oid"],
                "worktree": "Clean; switching is permitted.",
                "warning": "Only an existing local branch is selected. No branch will be created.",
            }
        )
        return facts, preview

    if action == "delete-branch":
        target = _branch_by_token(repo_root, branch_token)
        main = next((branch for branch in branches if branch["ref"] == "refs/heads/main"), None)
        if target["ref"] == current_ref:
            raise GitActionError("current_branch_delete", "The current local branch cannot be deleted.")
        if target["ref"] == "refs/heads/main":
            raise GitActionError("protected_branch", "The main branch cannot be deleted.")
        if main is None or not target["main_relation"].get("fully_merged"):
            raise GitActionError("branch_not_merged", "Git does not prove this branch fully merged into local main.")
        facts = {
            **common,
            "branch_token": branch_token,
            "target_ref": target["ref"],
            "target_name": target["name"],
            "target_oid": target["oid"],
            "main_oid": main["oid"],
            "upstream": target["upstream"],
        }
        preview.update(
            {
                "title": "Delete merged local branch",
                "target_branch": target["name"],
                "target_head": target["oid"],
                "main_head": main["oid"],
                "fully_merged": True,
                "upstream": target["upstream"] or "None",
                "remote_impact": "None. This deletes only the local branch; any remote branch remains.",
                "warning": "Git will use normal safe-delete semantics. No force-delete option is used.",
            }
        )
        return facts, preview

    if action == "fetch":
        remote = _remote_by_token(repo_root, remote_token, select_default=True)
        remote_state = _remote_facts(repo_root, remote["name"])
        refspecs = _assert_safe_fetch_refspecs(repo_root, remote["name"])
        if remote_state["mirror"]:
            raise GitActionError("unsafe_fetch_configuration", f"Remote {remote['name']} is configured as a mirror; fetch is unavailable.")
        tracking_refs = _remote_tracking_refs(repo_root, remote["name"])
        facts = {
            **common,
            "remote_token": remote["token"],
            "remote": remote["name"],
            "remote_urls": remote_state["fetch_urls"],
            "push_urls": remote_state["push_urls"],
            "fetch_refspecs": refspecs,
            "tracking_refs": tracking_refs,
        }
        preview.update(
            {
                "title": "Fetch configured remote",
                "remote": remote["name"],
                "remote_urls": remote_state["display_fetch_urls"],
                "remote_effect": "Network access will occur. Local remote-tracking refs may change; local branch, index, and worktree must not move.",
                "warning": "Fetch refreshes local remote-tracking knowledge only; no merge, rebase, prune, or local branch update is performed.",
            }
        )
        return facts, preview

    if action in {"push", "publish-branch", "fast-forward"}:
        if current_ref is None:
            raise GitActionError("detached_head", "This operation requires HEAD attached to a local branch.")
        current = next((branch for branch in branches if branch["ref"] == current_ref), None)
        if current is None:
            raise GitActionError("branch_inventory_unavailable", "Current local branch is absent from Git's branch inventory.", 503)
        if action == "publish-branch":
            if current["upstream"]:
                raise GitActionError("upstream_already_configured", "This local branch already has an upstream; use Push instead.")
            remote = _remote_by_token(repo_root, remote_token, select_default=True)
            remote_state = _remote_facts(repo_root, remote["name"])
            if remote_state["mirror"]:
                raise GitActionError("unsafe_push_configuration", f"Remote {remote['name']} is configured as a mirror; first push is unavailable.")
            if len(remote_state["push_urls"]) != 1:
                raise GitActionError("ambiguous_push_url", "First push requires exactly one configured push URL.")
            target_ref = f"refs/heads/{current['name']}"
            remote_tip = _ls_remote_tip(repo_root, remote_state["push_urls"][0], target_ref)
            if remote_tip is not None:
                raise GitActionError("remote_branch_exists", f"Remote branch {remote['name']}/{current['name']} already exists; first push is refused.")
            facts = {
                **common,
                "remote_token": remote["token"],
                "branch_ref": current_ref,
                "branch_name": current["name"],
                "remote": remote["name"],
                "remote_urls": remote_state["fetch_urls"],
                "push_urls": remote_state["push_urls"],
                "target_ref": target_ref,
                "remote_tip": None,
            }
            preview.update(
                {
                    "title": "Publish branch and set upstream",
                    "branch_name": current["name"],
                    "remote": remote["name"],
                    "remote_branch": target_ref.removeprefix("refs/heads/"),
                    "remote_urls": remote_state["display_push_urls"],
                    "remote_tip": "Confirmed absent at the configured push URL during preparation",
                    "committed_only": "Only committed history is pushed. Uncommitted changes are not included.",
                    "remote_impact": "Creates the same-name remote branch and configures it as this local branch's upstream.",
                    "warning": "If the same-name remote branch exists when revalidated, this action is refused.",
                }
            )
            return facts, preview

        if action == "push":
            upstream = current["upstream"]
            if not upstream:
                raise GitActionError("upstream_required", "This branch has no upstream; use first-push publishing.")
            remote_name, remote_branch = _upstream_parts(repo_root, upstream)
            remote = _remote_by_token(
                repo_root,
                next((item["token"] for item in _remote_inventory(repo_root) if item["name"] == remote_name), None),
                select_default=False,
            )
            remote_state = _remote_facts(repo_root, remote_name)
            if remote_state["mirror"]:
                raise GitActionError("unsafe_push_configuration", f"Remote {remote_name} is configured as a mirror; push is unavailable.")
            if len(remote_state["push_urls"]) != 1:
                raise GitActionError("ambiguous_push_url", "Push requires exactly one configured push URL.")
            tracking_oid = _resolve_commit(repo_root, upstream)
            if tracking_oid is None:
                raise GitActionError("upstream_unavailable", "Configured upstream tracking ref is unavailable locally; fetch and review again.")
            behind, ahead = _ref_counts(repo_root, upstream, current_ref)
            if ahead == 0:
                raise GitActionError("nothing_to_push", "The local branch has no commits ahead of its local upstream tracking ref.")
            target_ref = f"refs/heads/{remote_branch}"
            facts = {
                **common,
                "remote_token": remote["token"],
                "branch_ref": current_ref,
                "branch_name": current["name"],
                "upstream": upstream,
                "tracking_oid": tracking_oid,
                "ahead": ahead,
                "behind": behind,
                "remote": remote_name,
                "remote_urls": remote_state["fetch_urls"],
                "push_urls": remote_state["push_urls"],
                "target_ref": target_ref,
            }
            preview.update(
                {
                    "title": "Push current branch",
                    "branch_name": current["name"],
                    "remote": remote_name,
                    "remote_branch": remote_branch,
                    "remote_urls": remote_state["display_push_urls"],
                    "tracking_oid": tracking_oid,
                    "ahead": ahead,
                    "behind": behind,
                    "committed_only": "Only committed history is pushed. Uncommitted changes are not included.",
                    "remote_freshness": "Ahead/behind is based on the local remote-tracking ref and may be stale. The remote enforces non-fast-forward safety; force is never used.",
                    "warning": "A remote non-fast-forward rejection is surfaced without retry or force.",
                }
            )
            return facts, preview

        upstream = current["upstream"]
        if not upstream:
            raise GitActionError("upstream_required", "Fast-forward update requires a configured upstream.")
        tracking_oid = _resolve_commit(repo_root, upstream)
        if tracking_oid is None:
            raise GitActionError("upstream_unavailable", "Configured upstream tracking ref is unavailable locally; fetch first.")
        if not state["working_tree"]["is_clean"]:
            raise GitActionError("dirty_worktree", "Fast-forward update requires a completely clean working tree.")
        ahead, behind = _ref_counts(repo_root, current_ref, upstream)
        if ahead != 0 or behind < 1 or not _is_ancestor(repo_root, current_ref, upstream):
            raise GitActionError("not_fast_forwardable", "Only a clean behind-only branch can be advanced; diverged or ahead branches are refused.")
        facts = {
            **common,
            "branch_ref": current_ref,
            "branch_name": current["name"],
            "upstream": upstream,
            "upstream_oid": tracking_oid,
            "ahead": ahead,
            "behind": behind,
        }
        preview.update(
            {
                "title": "Fast-forward current branch",
                "branch_name": current["name"],
                "upstream": upstream,
                "upstream_oid": tracking_oid,
                "ahead": ahead,
                "behind": behind,
                "worktree": "Clean",
                "remote_effect": "No network access. This uses the local upstream tracking ref, which may be stale until Fetch.",
                "warning": "Fast-forward only. No merge commit, merge, rebase, reset, or implicit fetch is used.",
            }
        )
        return facts, preview

    raise GitActionError("invalid_action", "Unsupported branch or remote action.", 400)


class PreparedGitActions:
    def __init__(self) -> None:
        self._actions: dict[str, PreparedAction] = {}
        self._lock = threading.RLock()

    def invalidate_all(self) -> None:
        with self._lock:
            self._actions.clear()

    def _expire(self, now: float) -> None:
        expired = [token for token, item in self._actions.items() if item.expires_at <= now]
        for token in expired:
            self._actions.pop(token, None)
        if len(self._actions) > MAX_PREPARED_ACTIONS:
            oldest = sorted(self._actions, key=lambda token: self._actions[token].created_at)
            for token in oldest[: len(self._actions) - MAX_PREPARED_ACTIONS]:
                self._actions.pop(token, None)

    def options(self, repository_path: str, path_token: str) -> dict[str, Any]:
        return self.options_for_tokens(repository_path, [path_token])[path_token]

    def options_for_tokens(self, repository_path: str, path_tokens: list[str]) -> dict[str, dict[str, Any]]:
        repo_root = validate_git_worktree(Path(repository_path).expanduser().resolve())
        try:
            state = _state(repo_root)
        except GitActionError as exc:
            return {
                path_token: {"actions": [], "blocked_reason": str(exc), "unavailable_actions": []}
                for path_token in path_tokens
            }
        entries_by_token = {_path_token(entry["path"]): entry for entry in state["working_tree"]["entries"]}
        results = {}
        for path_token in path_tokens:
            entry = entries_by_token.get(path_token)
            if entry is None:
                results[path_token] = {
                    "actions": [],
                    "blocked_reason": "The selected path is no longer changed.",
                    "unavailable_actions": [],
                }
                continue
            results[path_token] = self._options_for_entry(repo_root, state, entry)
        return results

    def _options_for_entry(self, repo_root: Path, state: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
        path = entry["path"]
        try:
            entry = _status_entry(state, path)
            if entry is None:
                return {"actions": [], "blocked_reason": "The selected path is no longer changed.", "unavailable_actions": []}
            if entry["kind"] not in {"ordinary", "untracked"} or entry.get("submodule") not in {None, "N..."}:
                return {"actions": [], "blocked_reason": "This changed path state is unsupported for local actions.", "unavailable_actions": []}
            _file_identity(repo_root, path)
            _assert_no_custom_filter(repo_root, path)
        except GitActionError as exc:
            return {"actions": [], "blocked_reason": str(exc), "unavailable_actions": []}
        xy = entry.get("xy") or ""
        actions = []
        unavailable_actions = []
        if entry["kind"] == "untracked":
            actions.append("stage")
        elif len(xy) > 1 and xy[1] != ".":
            actions.append("stage")
        if xy and xy[0] != ".":
            actions.append("unstage")
        if entry["kind"] == "ordinary" and len(xy) >= 2 and (xy[0] != "." or xy[1] != "."):
            try:
                review = inspect_git_review(str(repo_root), selected_file=_path_token(path))
                previews = {item["key"]: item for item in review["selected"]["action_previews"]}
                possible = []
                if xy[1] != ".":
                    possible.append("restore-unstaged")
                if xy[0] != "." or xy[1] != ".":
                    possible.append("restore-head")
                for action in possible:
                    item = previews.get(action)
                    if item and item.get("available") and item.get("complete"):
                        actions.append(action)
                    elif action == "restore-head" or len(xy) > 1 and xy[1] != ".":
                        reason = item.get("reason") if item else None
                        unavailable_actions.append(
                            {"action": action, "reason": reason or "Complete supported consequence evidence is unavailable."}
                        )
            except (GitReviewError, FileNotFoundError, ValueError) as exc:
                for action in ("restore-unstaged", "restore-head"):
                    unavailable_actions.append({"action": action, "reason": str(exc)})
        return {"actions": actions, "blocked_reason": None, "unavailable_actions": unavailable_actions}

    def can_commit(self, repository_path: str) -> dict[str, Any]:
        repo_root = validate_git_worktree(Path(repository_path).expanduser().resolve())
        try:
            snapshot = _capture_commit(repo_root)
        except GitActionError as exc:
            return {"available": False, "reason": str(exc)}
        return {
            "available": True,
            "staged_paths": snapshot["staged_paths"],
            "remaining_paths": snapshot["remaining_paths"],
            "diff": snapshot["patch"],
            "head": snapshot["head"],
            "branch": snapshot["branch"],
        }

    def branch_controls(self, repository_path: str, branch_token: str | None) -> dict[str, Any]:
        repo_root = validate_git_worktree(Path(repository_path).expanduser().resolve())
        result: dict[str, Any] = {
            "create_available": False,
            "create_reason": None,
            "selected": None,
        }
        try:
            state = _operation_state(repo_root)
            _assert_branch_remote_hooks_supported(repo_root, "create-branch")
            result["create_available"] = True
            result["current_branch"] = state["branch"]["name"] or f"(detached at {state['head'][:12]})"
            result["head"] = state["head"]
        except GitActionError as exc:
            result["create_reason"] = str(exc)
            return result
        if branch_token:
            try:
                branch = _branch_by_token(repo_root, branch_token)
                selected = {
                    "name": branch["name"],
                    "current": branch["current"],
                    "switch_available": False,
                    "switch_reason": None,
                    "delete_available": False,
                    "delete_reason": None,
                }
                for action, enabled_key, reason_key in (
                    ("switch-branch", "switch_available", "switch_reason"),
                    ("delete-branch", "delete_available", "delete_reason"),
                ):
                    try:
                        _capture_branch_remote_action(repo_root, action, branch_token=branch_token)
                        selected[enabled_key] = True
                    except GitActionError as exc:
                        selected[reason_key] = str(exc)
                result["selected"] = selected
            except GitActionError:
                result["selected"] = None
        return result

    def remote_controls(self, repository_path: str) -> dict[str, Any]:
        repo_root = validate_git_worktree(Path(repository_path).expanduser().resolve())
        remotes: list[dict[str, Any]] = []
        error = None
        try:
            for remote in _remote_inventory(repo_root):
                facts = _remote_facts(repo_root, remote["name"])
                fetch_reason = None
                publish_reason = None
                try:
                    if facts["mirror"]:
                        raise GitActionError("unsafe_fetch_configuration", "Remote is configured as a mirror.")
                    _assert_safe_fetch_refspecs(repo_root, remote["name"])
                except GitActionError as exc:
                    fetch_reason = str(exc)
                if facts["mirror"]:
                    publish_reason = "Mirror remotes cannot be pushed through Repo Control."
                elif len(facts["push_urls"]) != 1:
                    publish_reason = "First push requires exactly one configured push URL."
                remotes.append(
                    {
                        **remote,
                        "fetch_urls": facts["display_fetch_urls"],
                        "push_urls": facts["display_push_urls"],
                        "fetch_eligible": fetch_reason is None,
                        "fetch_reason": fetch_reason,
                        "publish_eligible": publish_reason is None,
                        "publish_reason": publish_reason,
                    }
                )
        except GitActionError as exc:
            error = str(exc)
        origin = next((remote for remote in remotes if remote["name"] == "origin"), None)
        default_fetch = (
            origin if origin and origin["fetch_eligible"]
            else remotes[0] if origin is None and len(remotes) == 1 and remotes[0]["fetch_eligible"]
            else None
        )
        default_publish = (
            origin if origin and origin["publish_eligible"]
            else remotes[0] if origin is None and len(remotes) == 1 and remotes[0]["publish_eligible"]
            else None
        )
        result: dict[str, Any] = {
            "remotes": remotes,
            "default_fetch_remote_token": default_fetch["token"] if default_fetch else "",
            "default_publish_remote_token": default_publish["token"] if default_publish else "",
            "selection_required": len(remotes) > 1 and origin is None,
            "error": error,
            "fetch_available": any(remote["fetch_eligible"] for remote in remotes) and error is None,
            "fetch_reason": error or ("No configured remote is available." if not remotes else None),
            "publish_available": False,
            "publish_reason": None,
            "push_available": False,
            "push_reason": None,
            "fast_forward_available": False,
            "fast_forward_reason": None,
        }
        if result["fetch_available"]:
            try:
                _assert_branch_remote_hooks_supported(repo_root, "fetch")
            except GitActionError as exc:
                result["fetch_available"] = False
                result["fetch_reason"] = str(exc)
        try:
            state = _operation_state(repo_root)
            current_ref = _current_ref(state)
            result["current_branch"] = state["branch"]["name"] or f"(detached at {state['head'][:12]})"
            result["head"] = state["head"]
            if current_ref is None:
                raise GitActionError("detached_head", "Remote branch actions require HEAD attached to a local branch.")
            current = next((item for item in _branch_list(repo_root) if item["ref"] == current_ref), None)
            if current is None:
                raise GitActionError("branch_inventory_unavailable", "Current branch is unavailable.")
            if current["upstream"]:
                try:
                    _capture_branch_remote_action(repo_root, "push")
                    result["push_available"] = True
                except GitActionError as exc:
                    result["push_reason"] = str(exc)
                try:
                    _capture_branch_remote_action(repo_root, "fast-forward")
                    result["fast_forward_available"] = True
                except GitActionError as exc:
                    result["fast_forward_reason"] = str(exc)
                result["publish_reason"] = "This branch already has an upstream; use Push."
            else:
                result["fast_forward_reason"] = "This branch has no configured upstream."
                if any(remote["publish_eligible"] for remote in remotes):
                    try:
                        _assert_branch_remote_hooks_supported(repo_root, "publish-branch")
                        result["publish_available"] = True
                    except GitActionError as exc:
                        result["publish_reason"] = str(exc)
                elif remotes:
                    result["publish_reason"] = "No configured remote has a supported push URL."
                else:
                    result["publish_reason"] = "No configured remote is available."
        except GitActionError as exc:
            result["fetch_available"] = False
            result["fetch_reason"] = str(exc)
            result["publish_reason"] = result["push_reason"] = result["fast_forward_reason"] = str(exc)
        return result

    def prepare(
        self,
        repository_path: str,
        action: str,
        *,
        path_token: str | None = None,
        path_tokens: list[str] | None = None,
        message: str | None = None,
        branch_token: str | None = None,
        branch_name: str | None = None,
        remote_token: str | None = None,
    ) -> tuple[str, dict[str, Any]]:
        if action not in _ACTIONS | _BRANCH_REMOTE_ACTIONS:
            raise GitActionError("invalid_action", "Unsupported local Git action.", 400)
        repo_root = validate_git_worktree(Path(repository_path).expanduser().resolve())
        if action in _BRANCH_REMOTE_ACTIONS:
            state, preview = _capture_branch_remote_action(
                repo_root,
                action,
                branch_token=branch_token,
                branch_name=branch_name,
                remote_token=remote_token,
            )
            if action == "fetch":
                preview["remote_before"] = dict(state["tracking_refs"])
            if action == "publish-branch":
                preview["remote_absent"] = True
            now = time.monotonic()
            token = secrets.token_urlsafe(32)
            prepared = PreparedAction(
                token=token,
                action=action,
                repository_id=make_repository_id(repo_root),
                repository_root=str(repo_root),
                created_at=now,
                expires_at=now + ACTION_TTL_SECONDS,
                state=state,
                preview=preview,
            )
            with self._lock:
                self._expire(now)
                self._actions[token] = prepared
            return token, preview
        if action == "commit":
            commit_message = _commit_message(message)
            state = _capture_commit(repo_root)
            _assert_hooks_supported(repo_root)
            preview = {
                "action": action,
                "title": "Commit explicitly staged changes",
                "repository_root": str(repo_root),
                "branch": state["branch"],
                "head": state["head"],
                "message": commit_message,
                "staged_paths": state["staged_paths"],
                "remaining_paths": state["remaining_paths"],
                "diff": state["patch"],
                "warning": "Only the explicitly staged content shown here will be committed. Unstaged and untracked paths remain outside the commit.",
            }
        else:
            submitted_tokens = path_tokens if path_tokens is not None else ([path_token] if path_token else [])
            if not submitted_tokens:
                raise GitActionError("path_required", "Select at least one changed path for this action.", 400)
            if len(submitted_tokens) > MAX_ACTION_PATHS:
                raise GitActionError("path_set_too_large", "The selected path set exceeds the safe action limit.")
            if len(set(submitted_tokens)) != len(submitted_tokens):
                raise GitActionError("duplicate_path", "The selected path set contains duplicate paths.", 400)
            inventory = _state(repo_root)["working_tree"]["entries"]
            paths_by_token = {_path_token(entry["path"]): entry["path"] for entry in inventory}
            try:
                paths = sorted((paths_by_token[token] for token in submitted_tokens), key=os.fsencode)
            except KeyError as exc:
                raise GitActionError("path_not_found", "A selected changed path is no longer available.", 404) from exc
            if sum(len(os.fsencode(path)) for path in paths) > MAX_ACTION_PATH_BYTES:
                raise GitActionError("path_set_too_large", "The selected path set exceeds the safe action limit.")
            state = _capture_paths(repo_root, paths)
            options_by_token = self.options_for_tokens(
                str(repo_root),
                [_path_token(path) for path in paths],
            )
            path_previews = []
            for path in paths:
                options = options_by_token[_path_token(path)]
                if action not in options["actions"]:
                    reason = options.get("blocked_reason") or next(
                        (item["reason"] for item in options.get("unavailable_actions", []) if item["action"] == action),
                        "At least one selected path does not support this action.",
                    )
                    raise GitActionError("action_unavailable", f"{path}: {reason}")
                preview_data = _path_preview(repo_root, action, path)
                path_previews.append(
                    {
                        "path": path,
                        "summary": preview_data["summary"],
                        "layers": preview_data["layers"],
                        "details": preview_data,
                    }
                )
            if _capture_paths(repo_root, paths) != state:
                raise GitActionError(
                    "stale_prepared_action",
                    "Repository state changed while preparing the selected path set; review current evidence again.",
                )
            action_titles = {
                "stage": "Stage selected paths",
                "unstage": "Unstage selected paths",
                "restore-unstaged": "Restore unstaged changes",
                "restore-head": "Restore selected paths to HEAD",
            }
            warning = {
                "restore-unstaged": "UNSTAGED WORKTREE CONTENT FOR ALL SELECTED PATHS WILL BE DISCARDED. This discards unstaged worktree content for every selected path.",
                "restore-head": "STAGED AND UNSTAGED CHANGES FOR THE SELECTED PATHS MAY BE DISCARDED. Both layers will be replaced from the current HEAD version for every selected path.",
                "unstage": "INDEX WILL CHANGE. WORKTREE CONTENT WILL NOT BE REPLACED.",
            }.get(action, "")
            preview = {
                "action": action,
                "title": action_titles[action],
                "repository_root": str(repo_root),
                "branch": state["branch"],
                "head": state["head"],
                "paths": path_previews,
                "warning": warning,
            }
            if len(path_previews) == 1:
                preview.update(path_previews[0])
        now = time.monotonic()
        token = secrets.token_urlsafe(32)
        prepared = PreparedAction(
            token=token,
            action=action,
            repository_id=make_repository_id(repo_root),
            repository_root=str(repo_root),
            created_at=now,
            expires_at=now + ACTION_TTL_SECONDS,
            state=state,
            preview=preview,
        )
        with self._lock:
            self._expire(now)
            self._actions[token] = prepared
        return token, preview

    def cancel(self, token: str) -> None:
        with self._lock:
            self._actions.pop(token, None)

    def confirm(self, repository_path: str, action: str, token: str) -> dict[str, Any]:
        repo_root = validate_git_worktree(Path(repository_path).expanduser().resolve())
        with self._lock:
            now = time.monotonic()
            self._expire(now)
            prepared = self._actions.get(token)
            if prepared is None:
                raise GitActionError("prepared_action_unavailable", "Prepared action is unknown, expired, or already used.")
            if prepared.action != action:
                raise GitActionError("prepared_action_mismatch", "Prepared confirmation is bound to a different action.", 400)
            if prepared.repository_root != str(repo_root) or prepared.repository_id != make_repository_id(repo_root):
                self._actions.pop(token, None)
                raise GitActionError("repository_mismatch", "Prepared action belongs to a different repository.")
            self._actions.pop(token, None)
            if prepared.expires_at <= now:
                raise GitActionError("prepared_action_expired", "Prepared action expired; review current Git state again.")
            if action in _BRANCH_REMOTE_ACTIONS:
                current, _preview = _capture_branch_remote_action(
                    repo_root,
                    action,
                    branch_token=prepared.state.get("branch_token"),
                    branch_name=prepared.state.get("name"),
                    remote_token=prepared.state.get("remote_token"),
                )
                if current != prepared.state:
                    raise GitActionError(
                        "stale_prepared_action",
                        "Prepared branch or remote action is stale. Repository, branch, remote, or tracking state changed; review again.",
                    )
                return self._execute_branch_remote(repo_root, prepared)
            current = _capture_commit(repo_root) if action == "commit" else _capture_paths(
                repo_root,
                [item["path"] for item in prepared.state["paths"]],
            )
            if current != prepared.state:
                raise GitActionError("stale_prepared_action", "Prepared action is stale. Repository state changed; review current evidence again.")
            if action == "commit":
                _assert_hooks_supported(repo_root)
                self._execute_commit(repo_root, prepared)
                return self._verify_commit(repo_root, prepared)
            self._execute_paths(repo_root, prepared)
            return self._verify_paths(repo_root, prepared)

    def _execute_branch_remote(self, repo_root: Path, prepared: PreparedAction) -> dict[str, Any]:
        state = prepared.state
        action = prepared.action
        remote_action = action in {"fetch", "push", "publish-branch"}
        if action == "create-branch":
            args = ["branch", "--", state["name"]]
        elif action == "switch-branch":
            args = ["switch", "--no-guess", "--", state["target_name"]]
        elif action == "delete-branch":
            args = ["branch", "-d", "--", state["target_name"]]
        elif action == "fetch":
            args = ["fetch", "--no-prune", "--no-tags", "--recurse-submodules=no", state["remote"]]
        elif action == "push":
            args = [
                "push",
                "--no-follow-tags",
                "--recurse-submodules=no",
                state["remote"],
                f"{state['branch_ref']}:{state['target_ref']}",
            ]
        elif action == "publish-branch":
            args = [
                "push",
                "--set-upstream",
                "--no-follow-tags",
                "--recurse-submodules=no",
                state["remote"],
                f"{state['branch_ref']}:{state['target_ref']}",
            ]
        elif action == "fast-forward":
            args = ["merge", "--ff-only", state["upstream_oid"]]
        else:
            raise GitActionError("invalid_action", "Unsupported branch or remote action.", 400)

        if action in {"create-branch", "switch-branch", "delete-branch", "fast-forward"}:
            self._check_index_lock(repo_root)
        try:
            returncode, _stdout, stderr = _run_mutation(
                repo_root,
                args,
                timeout_seconds=MAX_REMOTE_RUNTIME_SECONDS if remote_action else 30,
                noninteractive_remote=remote_action,
            )
        except GitActionError as exc:
            if remote_action:
                raise GitActionError(
                    "remote_action_execution_failed",
                    f"Remote operation did not complete with a verified result: {_sanitize_diagnostic(str(exc))}",
                    503,
                ) from exc
            raise
        if returncode != 0:
            message = stderr.decode("utf-8", errors="replace").strip()
            message = _sanitize_diagnostic(message)
            if action in {"push", "publish-branch"}:
                safe_push_failure = _push_failure_message(message)
                if safe_push_failure:
                    raise GitActionError("remote_action_execution_failed", safe_push_failure, 503)
            code = "remote_action_execution_failed" if remote_action else "git_branch_action_failed"
            label = "Remote" if remote_action else "Git branch action"
            raise GitActionError(code, f"{label} failed: {message or 'no diagnostic supplied'}.", 503)
        return self._verify_branch_remote(repo_root, prepared)

    def _verify_branch_remote(self, repo_root: Path, prepared: PreparedAction) -> dict[str, Any]:
        action = prepared.action
        before = prepared.state
        state = _operation_state(repo_root)
        common_after = {
            "head": state["head"],
            "branch": state["branch"]["name"],
            "status": state["working_tree"]["entries"],
            "index": _index_fingerprint(repo_root),
        }

        if action == "create-branch":
            created = _resolve_commit(repo_root, f"refs/heads/{before['name']}")
            if created != before["head"] or common_after != {
                "head": before["head"],
                "branch": before["branch"],
                "status": before["status"],
                "index": before["index"],
            }:
                raise GitActionError("post_action_verification_failed", "Branch creation did not preserve the reviewed HEAD, current branch, index, and worktree status.", 503)
            return {
                "action": action,
                "message": f"Created local branch {before['name']} at {created[:12]}; current branch did not change.",
                "branch": common_after["branch"],
                "head": common_after["head"],
            }

        if action == "switch-branch":
            if (
                common_after["branch"] != before["target_name"]
                or common_after["head"] != before["target_oid"]
                or common_after["status"]
                or state["git_operation_in_progress"]
                or not state["working_tree"]["is_clean"]
            ):
                raise GitActionError("post_action_verification_failed", "Branch switch did not reach the reviewed target in a clean worktree.", 503)
            return {
                "action": action,
                "message": f"Switched to {before['target_name']} at {common_after['head'][:12]}; working tree is clean.",
                "branch": common_after["branch"],
                "head": common_after["head"],
            }

        if action == "delete-branch":
            if _resolve_commit(repo_root, before["target_ref"]) is not None:
                raise GitActionError("post_action_verification_failed", "Git returned but the local branch still exists.", 503)
            if common_after != {
                "head": before["head"],
                "branch": before["branch"],
                "status": before["status"],
                "index": before["index"],
            }:
                raise GitActionError("post_action_verification_failed", "Local branch deletion changed the current branch, HEAD, index, or worktree status.", 503)
            return {
                "action": action,
                "message": f"Deleted local branch {before['target_name']}. Any remote branch was not targeted.",
                "branch": common_after["branch"],
                "head": common_after["head"],
            }

        if action == "fetch":
            expected_common = {
                "head": before["head"],
                "branch": before["branch"],
                "status": before["status"],
                "index": before["index"],
            }
            if common_after != expected_common:
                raise GitActionError("post_action_verification_failed", "Fetch completed but current branch, HEAD, index, or worktree status changed.", 503)
            remote_now = _remote_facts(repo_root, before["remote"])
            if (
                remote_now["fetch_urls"] != before["remote_urls"]
                or remote_now["push_urls"] != before["push_urls"]
                or _assert_safe_fetch_refspecs(repo_root, before["remote"]) != before["fetch_refspecs"]
            ):
                raise GitActionError("post_action_verification_failed", "Remote configuration changed during fetch; inspect repository state.", 503)
            after_refs = _remote_tracking_refs(repo_root, before["remote"])
            old_refs = dict(before["tracking_refs"])
            new_refs = dict(after_refs)
            changed = [
                {"ref": ref, "old": old_refs.get(ref), "new": new_refs.get(ref)}
                for ref in sorted(set(old_refs) | set(new_refs))
                if old_refs.get(ref) != new_refs.get(ref)
            ]
            summary = [f"{item['ref']}: {item['old'] or '(absent)'} -> {item['new'] or '(absent)'}" for item in changed]
            message = "Fetch completed. Local branch, index, and worktree did not move."
            if summary:
                message += " Updated local tracking refs: " + "; ".join(summary)
            else:
                message += " No remote-tracking ref changed."
            return {"action": action, "message": message, "branch": common_after["branch"], "head": common_after["head"], "refs_changed": changed}

        if action in {"push", "publish-branch"}:
            expected_common = {
                "head": before["head"],
                "branch": before["branch"],
                "status": before["status"],
                "index": before["index"],
            }
            if common_after != expected_common:
                raise GitActionError("post_action_verification_failed", "Push completed but current branch, HEAD, index, or worktree status changed.", 503)
            remote_now = _remote_facts(repo_root, before["remote"])
            if remote_now["fetch_urls"] != before["remote_urls"] or remote_now["push_urls"] != before["push_urls"]:
                raise GitActionError("remote_verification_failed", "Push command succeeded, but remote configuration changed and the remote tip could not be safely verified.", 503)
            if action == "publish-branch":
                branch = next((item for item in _branch_list(repo_root) if item["ref"] == before["branch_ref"]), None)
                if branch is None or branch["upstream"] != f"refs/remotes/{before['remote']}/{before['branch_name']}":
                    raise GitActionError("post_action_verification_failed", "Push completed but the expected same-name upstream was not configured.", 503)
            try:
                remote_tip = _ls_remote_tip(repo_root, before["push_urls"][0], before["target_ref"])
            except GitActionError as exc:
                raise GitActionError(
                    "remote_verification_failed",
                    f"Push command succeeded, but actual remote-tip verification was unavailable: {_sanitize_diagnostic(str(exc))}",
                    503,
                ) from exc
            if remote_tip != before["head"]:
                raise GitActionError(
                    "remote_verification_failed",
                    f"Push command succeeded, but remote tip {remote_tip or '(absent)'} does not match local HEAD {before['head']}.",
                    503,
                )
            return {
                "action": action,
                "message": f"Push command succeeded. Actual remote {before['remote']}/{before['target_ref'].removeprefix('refs/heads/')} tip verified at {remote_tip[:12]}. Only committed history was pushed.",
                "branch": common_after["branch"],
                "head": common_after["head"],
                "remote_tip": remote_tip,
                "remote_verified": True,
            }

        if action == "fast-forward":
            if (
                common_after["branch"] != before["branch_name"]
                or common_after["head"] != before["upstream_oid"]
                or common_after["status"]
                or not state["working_tree"]["is_clean"]
                or state["git_operation_in_progress"]
            ):
                raise GitActionError("post_action_verification_failed", "Fast-forward did not reach the reviewed upstream tip with the same branch and a clean worktree.", 503)
            return {
                "action": action,
                "message": f"Fast-forwarded {before['branch_name']} to local upstream tip {before['upstream_oid'][:12]}; no merge commit was created.",
                "branch": common_after["branch"],
                "head": common_after["head"],
            }
        raise GitActionError("invalid_action", "Unsupported branch or remote action.", 400)

    def _execute_paths(self, repo_root: Path, prepared: PreparedAction) -> None:
        pathspecs = [f":(literal){item['path']}" for item in prepared.state["paths"]]
        if prepared.action == "stage":
            args = ["add", "--", *pathspecs]
        elif prepared.action == "unstage":
            args = ["restore", "--staged", "--", *pathspecs]
        elif prepared.action == "restore-unstaged":
            args = ["restore", "--worktree", "--", *pathspecs]
        elif prepared.action == "restore-head":
            args = ["restore", "--source=HEAD", "--staged", "--worktree", "--", *pathspecs]
        else:
            raise GitActionError("invalid_action", "Unsupported local Git action.", 400)
        self._check_index_lock(repo_root)
        returncode, _stdout, stderr = _run_mutation(repo_root, args)
        if returncode != 0:
            message = stderr.decode("utf-8", errors="replace").strip()
            raise GitActionError("git_action_execution_failed", f"Git action failed: {message or 'no diagnostic supplied'}.")

    def _execute_commit(self, repo_root: Path, prepared: PreparedAction) -> None:
        self._check_index_lock(repo_root)
        returncode, _stdout, stderr = _run_mutation(repo_root, ["commit", "-m", prepared.preview["message"]])
        if returncode != 0:
            message = stderr.decode("utf-8", errors="replace").strip()
            raise GitActionError("git_commit_failed", f"Git commit failed: {message or 'no diagnostic supplied'}.")

    def _check_index_lock(self, repo_root: Path) -> None:
        output, _stderr, _code = _run_read(repo_root, ["rev-parse", "--git-path", "index.lock"], limit=1024)
        lock = Path(os.fsdecode(output).strip())
        if not lock.is_absolute():
            lock = repo_root / lock
        if lock.exists():
            raise GitActionError("git_index_locked", "Git index is locked by another process.")

    def _verify_paths(self, repo_root: Path, prepared: PreparedAction) -> dict[str, Any]:
        state = _state(repo_root)
        for prepared_path in prepared.state["paths"]:
            path = prepared_path["path"]
            entry = _status_entry(state, path)
            xy = (entry.get("xy") or "") if entry else ""
            if prepared.action == "stage":
                if entry is None or entry["kind"] == "untracked" or len(xy) < 2 or xy[0] == "." or xy[1] != ".":
                    raise GitActionError("post_action_verification_failed", f"Stage command returned but {path} is not verified as fully staged.", 503)
            elif prepared.action == "unstage":
                if entry is not None and xy and xy[0] != ".":
                    raise GitActionError("post_action_verification_failed", f"Unstage command returned but {path} remains staged.", 503)
                if _file_identity(repo_root, path) != prepared_path["worktree"]:
                    raise GitActionError("post_action_verification_failed", f"Unstage command changed the worktree identity for {path}.", 503)
            elif prepared.action == "restore-unstaged":
                if entry is not None and len(xy) > 1 and xy[1] != ".":
                    raise GitActionError("post_action_verification_failed", f"Restore command returned but unstaged changes remain for {path}.", 503)
                if _digest(_index_records(repo_root, path)) != prepared_path["index_path_records"]:
                    raise GitActionError("post_action_verification_failed", f"Restore command changed the index state for {path}.", 503)
            else:
                if entry is not None:
                    raise GitActionError("post_action_verification_failed", f"Restore-to-HEAD returned but {path} is still changed.", 503)
                if state["head"] != prepared.state["head"]:
                    raise GitActionError("post_action_verification_failed", "HEAD changed during restore-to-HEAD verification.", 503)
        count = len(prepared.state["paths"])
        result = {
            "stage": f"{count} selected path(s) are now staged; other paths were not selected.",
            "unstage": f"{count} selected path(s) are no longer staged; worktree content remains unchanged.",
            "restore-unstaged": f"Unstaged changes are no longer present for all {count} selected path(s); index state is unchanged.",
            "restore-head": f"All {count} selected path(s) now match HEAD in both index and worktree.",
        }[prepared.action]
        return {
            "action": prepared.action,
            "message": result,
            "paths": [item["path"] for item in prepared.state["paths"]],
            "head": state["head"],
            "branch": state["branch"]["name"],
        }

    def _verify_commit(self, repo_root: Path, prepared: PreparedAction) -> dict[str, Any]:
        before = prepared.state["head"]
        state = _state(repo_root)
        after = state["head"]
        if after == before:
            raise GitActionError("post_commit_verification_failed", "Commit command returned but HEAD did not advance.", 503)
        subject, _stderr, _code = _run_read(repo_root, ["show", "-s", "--format=%s", after], limit=4096)
        if os.fsdecode(subject).rstrip("\n") != prepared.preview["message"]:
            raise GitActionError("post_commit_verification_failed", "New HEAD subject does not match the reviewed commit message.", 503)
        if state["working_tree"]["staged"]["count"]:
            raise GitActionError("post_commit_verification_failed", "Commit was created but staged changes remain.", 503)
        return {
            "action": "commit",
            "message": f"Created {after[:12]} {prepared.preview['message']}.",
            "head": after,
            "branch": state["branch"]["name"],
            "remaining_unstaged": state["working_tree"]["unstaged"]["paths"],
            "remaining_untracked": state["working_tree"]["untracked"]["paths"],
        }


def inspect_action_options(repository_path: str, path_token: str) -> dict[str, Any]:
    return PreparedGitActions().options(repository_path, path_token)
