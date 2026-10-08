from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..scanner.git_ops import ScanError, _run_git_bounded, validate_git_worktree
from ..scanner.util import make_repository_id
from .diff import GitReviewError, inspect_git_review
from .git_state import WorkflowGitStateError, inspect_git_state

MAX_STATUS_BYTES = 1024 * 1024
MAX_INDEX_OUTPUT_BYTES = 16 * 1024 * 1024
MAX_ACTION_OUTPUT_BYTES = 64 * 1024
MAX_COMMIT_DIFF_BYTES = 512 * 1024
MAX_COMMIT_PATHS = 256
MAX_FILE_HASH_BYTES = 256 * 1024 * 1024
ACTION_TTL_SECONDS = 300
MAX_PREPARED_ACTIONS = 64
_ACTIONS = {"stage", "unstage", "restore-unstaged", "restore-head", "commit"}
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


def _run_mutation(repo_root: Path, args: list[str]) -> tuple[int, bytes, bytes]:
    try:
        stdout, stderr, returncode, truncated = _run_git_bounded(
            repo_root,
            args,
            stdout_limit=MAX_ACTION_OUTPUT_BYTES,
            stderr_limit=MAX_ACTION_OUTPUT_BYTES,
            timeout_seconds=30,
        )
    except ScanError as exc:
        raise GitActionError("git_action_execution_failed", str(exc), 503) from exc
    if truncated:
        return returncode, stdout, stderr
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


def _capture_path(repo_root: Path, path: str) -> dict[str, Any]:
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
        "index_state": _digest(_index_records(repo_root)),
        "index_path_records": _digest(_index_records(repo_root, path)),
        "worktree": _file_identity(repo_root, path),
        "head_path_records": _digest(_head_entry(repo_root, path)),
        "path": path,
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


class PreparedGitActions:
    def __init__(self) -> None:
        self._actions: dict[str, PreparedAction] = {}
        self._lock = threading.RLock()

    def _expire(self, now: float) -> None:
        expired = [token for token, item in self._actions.items() if item.expires_at <= now]
        for token in expired:
            self._actions.pop(token, None)
        if len(self._actions) > MAX_PREPARED_ACTIONS:
            oldest = sorted(self._actions, key=lambda token: self._actions[token].created_at)
            for token in oldest[: len(self._actions) - MAX_PREPARED_ACTIONS]:
                self._actions.pop(token, None)

    def options(self, repository_path: str, path_token: str) -> dict[str, Any]:
        repo_root = validate_git_worktree(Path(repository_path).expanduser().resolve())
        path = _raw_selected_path(repo_root, path_token)
        state = _state(repo_root)
        entry = _status_entry(state, path)
        if entry is None:
            return {"actions": [], "blocked_reason": "The selected path is no longer changed.", "unavailable_actions": []}
        xy = entry.get("xy") or ""
        actions = []
        unavailable_actions = []
        if entry["kind"] == "ordinary" and entry.get("submodule", "N...") == "N...":
            try:
                _file_identity(repo_root, path)
                if entry["kind"] == "untracked" or len(xy) > 1 and xy[1] != ".":
                    actions.append("stage")
                if xy and xy[0] != ".":
                    actions.append("unstage")
                if xy and xy[1] != ".":
                    try:
                        _path_preview(repo_root, "restore-unstaged", path)
                        actions.append("restore-unstaged")
                    except GitActionError as exc:
                        unavailable_actions.append({"action": "restore-unstaged", "reason": str(exc)})
                if entry["kind"] == "ordinary":
                    try:
                        _path_preview(repo_root, "restore-head", path)
                        actions.append("restore-head")
                    except GitActionError as exc:
                        unavailable_actions.append({"action": "restore-head", "reason": str(exc)})
            except GitActionError as exc:
                return {"actions": [], "blocked_reason": str(exc), "unavailable_actions": []}
        elif entry["kind"] == "untracked":
            try:
                _file_identity(repo_root, path)
                actions.append("stage")
            except GitActionError as exc:
                return {"actions": [], "blocked_reason": str(exc), "unavailable_actions": []}
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

    def prepare(
        self,
        repository_path: str,
        action: str,
        *,
        path_token: str | None = None,
        message: str | None = None,
    ) -> tuple[str, dict[str, Any]]:
        if action not in _ACTIONS:
            raise GitActionError("invalid_action", "Unsupported local Git action.", 400)
        repo_root = validate_git_worktree(Path(repository_path).expanduser().resolve())
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
            if not path_token:
                raise GitActionError("path_required", "Select one changed path for this action.", 400)
            path = _raw_selected_path(repo_root, path_token)
            state = _capture_path(repo_root, path)
            options = self.options(str(repo_root), path_token)["actions"]
            if action not in options:
                raise GitActionError("action_unavailable", "This action is unsupported for the selected path state.")
            preview_data = _path_preview(repo_root, action, path)
            preview = {
                "action": action,
                "title": preview_data["title"],
                "repository_root": str(repo_root),
                "branch": state["branch"],
                "head": state["head"],
                "path": preview_data["path"],
                "summary": preview_data["summary"],
                "layers": preview_data["layers"],
                "details": preview_data,
                "warning": (
                    "This action discards unstaged worktree content for this path."
                    if action == "restore-unstaged"
                    else "This action replaces both index and worktree state for this path with the current HEAD version."
                    if action == "restore-head"
                    else ""
                ),
            }
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
            current = _capture_commit(repo_root) if action == "commit" else _capture_path(repo_root, prepared.state["path"])
            if current != prepared.state:
                raise GitActionError("stale_prepared_action", "Prepared action is stale. Repository state changed; review current evidence again.")
            if action == "commit":
                _assert_hooks_supported(repo_root)
                self._execute_commit(repo_root, prepared)
                return self._verify_commit(repo_root, prepared)
            self._execute_path(repo_root, prepared)
            return self._verify_path(repo_root, prepared)

    def _execute_path(self, repo_root: Path, prepared: PreparedAction) -> None:
        path = prepared.state["path"]
        literal_path = f":(literal){path}"
        if prepared.action == "stage":
            args = ["add", "--", literal_path]
        elif prepared.action == "unstage":
            args = ["restore", "--staged", "--", literal_path]
        elif prepared.action == "restore-unstaged":
            args = ["restore", "--worktree", "--", literal_path]
        elif prepared.action == "restore-head":
            args = ["restore", "--source=HEAD", "--staged", "--worktree", "--", literal_path]
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

    def _verify_path(self, repo_root: Path, prepared: PreparedAction) -> dict[str, Any]:
        path = prepared.state["path"]
        state = _state(repo_root)
        entry = _status_entry(state, path)
        if prepared.action == "stage":
            xy = (entry.get("xy") or "") if entry else ""
            if entry is None or entry["kind"] == "untracked" or len(xy) < 2 or xy[0] == ".":
                raise GitActionError("post_action_verification_failed", "Stage command returned but the selected path is not verified as staged.", 503)
            if xy[1] != ".":
                raise GitActionError("post_action_verification_failed", "Stage command returned but unstaged changes remain for the selected path.", 503)
            result = "Path is now staged; other paths were not selected."
        elif prepared.action == "unstage":
            xy = (entry.get("xy") or "") if entry else ""
            if entry is not None and xy and xy[0] != ".":
                raise GitActionError("post_action_verification_failed", "Unstage command returned but the selected path remains staged.", 503)
            if _file_identity(repo_root, path) != prepared.state["worktree"]:
                raise GitActionError("post_action_verification_failed", "Unstage command returned but worktree identity changed.", 503)
            result = "Path is no longer staged; worktree content remains unchanged."
        elif prepared.action == "restore-unstaged":
            if entry is not None and (entry.get("xy") or "")[1] != ".":
                raise GitActionError("post_action_verification_failed", "Restore command returned but unstaged changes remain for the selected path.", 503)
            if _digest(_index_records(repo_root, path)) != prepared.state["index_path_records"]:
                raise GitActionError("post_action_verification_failed", "Restore command changed the selected index state.", 503)
            result = "Unstaged changes for this path are no longer present; index state is unchanged."
        else:
            if entry is not None:
                raise GitActionError("post_action_verification_failed", "Restore-to-HEAD returned but the selected path is still changed.", 503)
            if state["head"] != prepared.state["head"]:
                raise GitActionError("post_action_verification_failed", "HEAD changed during restore-to-HEAD verification.", 503)
            result = "Selected path now matches HEAD in both index and worktree."
        return {
            "action": prepared.action,
            "message": result,
            "path": path,
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
