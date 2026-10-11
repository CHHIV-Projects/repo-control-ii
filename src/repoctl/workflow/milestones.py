from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from ..scanner.core import STATE_ARTIFACT_LOCK
from ..scanner.util import make_repository_id

_CANONICAL_FILENAME_RE = re.compile(
    r"^(M[0-9]{3}(?:\.[0-9]{3})+)_([a-z0-9]+(?:_[a-z0-9]+)*)_(prompt|closeout)\.md$"
)
_LEGACY_FILENAME_RE = re.compile(
    r"^(R[0-9]{3}\.[0-9]{2})_([a-z0-9]+(?:_[a-z0-9]+)*)_(prompt|closeout)\.md$"
)
_ARTIFACT_LIKE_RE = re.compile(
    r"^(?:[A-Za-z][A-Za-z0-9.]*|[0-9][A-Za-z0-9.]*)_.+_[a-z]+\.md$",
    re.IGNORECASE,
)
_POINTER_SCHEMA_VERSION = 1


class MilestoneArtifactError(RuntimeError):
    pass


def _root_paths(repository_root: Path, milestone_root: str) -> tuple[Path, Path]:
    repo = repository_root.resolve(strict=True)
    if not isinstance(milestone_root, str):
        raise MilestoneArtifactError("Configured milestone root must be a repository-relative path.")
    relative = PurePosixPath(milestone_root)
    if (
        not milestone_root
        or relative.is_absolute()
        or "\\" in milestone_root
        or any(part in {"", ".", ".."} for part in milestone_root.split("/"))
    ):
        raise MilestoneArtifactError("Configured milestone root must be a repository-relative path without traversal.")

    configured = repo.joinpath(*relative.parts)
    current = repo
    for part in relative.parts:
        current = current / part
        try:
            if current.is_symlink():
                raise MilestoneArtifactError("Configured milestone root cannot contain symlinked path components.")
        except OSError as exc:
            raise MilestoneArtifactError(f"Cannot inspect configured milestone root: {exc}") from exc

    resolved = configured.resolve(strict=False)
    if not resolved.is_relative_to(repo):
        raise MilestoneArtifactError("Configured milestone root resolves outside the selected repository.")
    return repo, resolved


def _contained_file(repository_root: Path, milestone_dir: Path, relative_path: str) -> Path:
    relative = PurePosixPath(relative_path)
    if relative.is_absolute() or not relative_path or "\\" in relative_path:
        raise MilestoneArtifactError("Milestone artifact path must be a repository-relative POSIX path.")
    if any(part in {"", ".", ".."} for part in relative.parts):
        raise MilestoneArtifactError("Milestone artifact path contains an invalid path component.")
    try:
        resolved = (repository_root / Path(*relative.parts)).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise MilestoneArtifactError("Milestone artifact is missing or cannot be resolved.") from exc
    if not resolved.is_relative_to(repository_root) or not resolved.is_relative_to(milestone_dir):
        raise MilestoneArtifactError("Milestone artifact resolves outside the configured milestone root.")
    if not resolved.is_file():
        raise MilestoneArtifactError("Milestone artifact is not a regular file.")
    return resolved


def _parse_filename(name: str) -> tuple[str, str, str] | None:
    match = _CANONICAL_FILENAME_RE.fullmatch(name) or _LEGACY_FILENAME_RE.fullmatch(name)
    if match is None:
        return None
    return match.group(1), match.group(2), match.group(3)


def _sort_key(candidate: dict[str, Any]) -> tuple[str, tuple[int, ...]]:
    milestone_id = candidate["milestone_id"]
    process_code = milestone_id[0]
    numeric_hierarchy = tuple(int(part) for part in milestone_id[1:].split("."))
    return process_code, tuple(-number for number in numeric_hierarchy) + (0,)


def discover_milestones(
    repository_root: str | Path,
    milestone_root: str = "docs/milestones",
) -> dict[str, Any]:
    repo = Path(repository_root).resolve(strict=True)
    try:
        repo, milestone_dir = _root_paths(repo, milestone_root)
    except MilestoneArtifactError as exc:
        return {"candidates": [], "issues": [str(exc)]}
    configured = repo.joinpath(*PurePosixPath(milestone_root).parts)
    try:
        configured.lstat()
    except FileNotFoundError:
        return {"candidates": [], "issues": []}
    except OSError as exc:
        raise MilestoneArtifactError(f"Cannot inspect configured milestone root: {exc}") from exc
    if configured.is_symlink():
        return {
            "candidates": [],
            "issues": ["Configured milestone root is a symlink and is not accepted."],
        }
    if not milestone_dir.is_dir():
        return {
            "candidates": [],
            "issues": ["Configured milestone root is not a directory contained in the selected repository."],
        }

    issues: list[str] = []
    prompt_records: list[dict[str, str]] = []
    closeout_records: list[dict[str, str]] = []
    for current_name, directories, filenames in os.walk(milestone_dir, followlinks=False):
        current = Path(current_name)
        retained_directories: list[str] = []
        for directory in sorted(directories, key=os.fsencode):
            directory_path = current / directory
            if directory_path.is_symlink():
                issues.append(
                    f"Milestone subdirectory symlink is not traversed: "
                    f"{directory_path.relative_to(repo).as_posix()}"
                )
            else:
                retained_directories.append(directory)
        directories[:] = retained_directories

        for filename in sorted(filenames, key=os.fsencode):
            if not _ARTIFACT_LIKE_RE.fullmatch(filename):
                continue
            path = current / filename
            relative_path = path.relative_to(repo).as_posix()
            if path.is_symlink():
                artifact_type = (_parse_filename(filename) or (None, None, ""))[2]
                label = "closeout" if artifact_type == "closeout" or filename.lower().endswith("_closeout.md") else "prompt"
                issues.append(f"Milestone {label} symlinks are not selectable: {relative_path}")
                continue
            parsed = _parse_filename(filename)
            if parsed is None:
                issues.append(f"Invalid milestone prompt or closeout filename: {relative_path}")
                continue
            milestone_id, slug, artifact_type = parsed
            try:
                _contained_file(repo, milestone_dir, relative_path)
            except MilestoneArtifactError as exc:
                issues.append(f"Invalid milestone artifact {relative_path}: {exc}")
                continue
            record = {
                "milestone_id": milestone_id,
                "title_slug": slug,
                "artifact_type": artifact_type,
                "path": relative_path,
            }
            (prompt_records if artifact_type == "prompt" else closeout_records).append(record)

    prompt_counts: dict[str, int] = {}
    for record in prompt_records:
        prompt_counts[record["milestone_id"]] = prompt_counts.get(record["milestone_id"], 0) + 1

    candidates: list[dict[str, Any]] = []
    prompt_pairs = {(record["milestone_id"], record["title_slug"]) for record in prompt_records}
    closeouts_by_pair: dict[tuple[str, str], list[dict[str, str]]] = {}
    closeouts_by_id: dict[str, list[dict[str, str]]] = {}
    for record in closeout_records:
        key = (record["milestone_id"], record["title_slug"])
        closeouts_by_pair.setdefault(key, []).append(record)
        closeouts_by_id.setdefault(record["milestone_id"], []).append(record)
        if key not in prompt_pairs:
            issues.append(f"Orphan or unmatched milestone closeout: {record['path']}")

    for prompt in prompt_records:
        milestone_id = prompt["milestone_id"]
        if prompt_counts[milestone_id] > 1:
            if not any(f"Ambiguous milestone ID {milestone_id}:" in issue for issue in issues):
                issues.append(f"Ambiguous milestone ID {milestone_id}: more than one valid prompt filename.")
            continue
        pair = (milestone_id, prompt["title_slug"])
        matching_closeouts = closeouts_by_pair.get(pair, [])
        milestone_closeouts = closeouts_by_id.get(milestone_id, [])
        if len(milestone_closeouts) > 1:
            issues.append(
                f"Ambiguous closeouts for milestone {milestone_id}: multiple closeouts use this ID."
            )
            closeout_exists = False
            closeout_path = None
        elif matching_closeouts:
            closeout_exists = True
            closeout_path = matching_closeouts[0]["path"]
        else:
            closeout_exists = False
            closeout_path = None
        candidates.append(
            {
                "milestone_id": milestone_id,
                "prompt_path": prompt["path"],
                "title_slug": prompt["title_slug"],
                "closeout_exists": closeout_exists,
                "closeout_path": closeout_path,
            }
        )

    candidates.sort(key=_sort_key)
    return {"candidates": candidates, "issues": issues}


def _pointer_path(state_root: Path, repository_id: str) -> Path:
    return state_root / repository_id / "milestones" / "selection.json"


def load_milestone_selection(
    repository_root: str | Path,
    state_root: str | Path,
    milestone_root: str = "docs/milestones",
) -> dict[str, Any]:
    with STATE_ARTIFACT_LOCK:
        root = Path(repository_root).resolve(strict=True)
        repository_id = make_repository_id(root)
        path = _pointer_path(Path(state_root).expanduser(), repository_id)
        try:
            path.lstat()
        except FileNotFoundError:
            return {"status": "none", "selection": None, "reason": None}
        except OSError:
            return {
                "status": "invalid",
                "selection": None,
                "reason": "The saved milestone selection cannot be inspected; select a milestone again.",
            }
        if path.is_symlink() or not path.is_file():
            return {
                "status": "invalid",
                "selection": None,
                "reason": "The saved milestone selection is not a regular operational pointer; select a milestone again.",
            }
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return {
                "status": "invalid",
                "selection": None,
                "reason": "The saved milestone selection is unreadable or malformed; select a milestone again.",
            }
        if not isinstance(payload, dict):
            return {
                "status": "invalid",
                "selection": None,
                "reason": "The saved milestone selection has an invalid record shape; select a milestone again.",
            }
        milestone_id = payload.get("milestone_id")
        prompt_path = payload.get("prompt_path")
        if (
            payload.get("schema_version") != _POINTER_SCHEMA_VERSION
            or payload.get("repository_id") != repository_id
            or not isinstance(milestone_id, str)
            or not isinstance(prompt_path, str)
        ):
            return {
                "status": "invalid",
                "selection": None,
                "reason": "The saved milestone selection has invalid or mismatched metadata; select a milestone again.",
            }
        try:
            _, milestone_dir = _root_paths(root, milestone_root)
            _contained_file(root, milestone_dir, prompt_path)
        except MilestoneArtifactError:
            return {
                "status": "invalid",
                "selection": None,
                "reason": "The saved milestone prompt is missing or outside this repository; select a milestone again.",
            }
        inventory = discover_milestones(root, milestone_root)
        match = next(
            (
                candidate
                for candidate in inventory["candidates"]
                if candidate["milestone_id"] == milestone_id and candidate["prompt_path"] == prompt_path
            ),
            None,
        )
        if match is None:
            return {
                "status": "invalid",
                "selection": None,
                "reason": "The saved milestone prompt is no longer a valid candidate; select a milestone again.",
            }
        return {"status": "selected", "selection": match, "reason": None}


def save_milestone_selection(
    repository_root: str | Path,
    state_root: str | Path,
    milestone_id: str,
    milestone_root: str = "docs/milestones",
) -> dict[str, Any]:
    root = Path(repository_root).resolve(strict=True)
    external_state_root = Path(state_root).expanduser().resolve()
    if external_state_root == root or external_state_root.is_relative_to(root):
        raise MilestoneArtifactError("Milestone selection state must be stored outside the selected repository.")
    if not isinstance(milestone_id, str):
        raise MilestoneArtifactError("Select a valid milestone ID from the discovered candidates.")
    inventory = discover_milestones(root, milestone_root)
    candidate = next((item for item in inventory["candidates"] if item["milestone_id"] == milestone_id), None)
    if candidate is None:
        raise MilestoneArtifactError("Select a current milestone candidate from this repository.")

    repository_id = make_repository_id(root)
    target = _pointer_path(external_state_root, repository_id)
    payload = {
        "schema_version": _POINTER_SCHEMA_VERSION,
        "repository_id": repository_id,
        "milestone_id": milestone_id,
        "prompt_path": candidate["prompt_path"],
    }
    with STATE_ARTIFACT_LOCK:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise MilestoneArtifactError(f"Could not create milestone selection state directory: {exc}") from exc
        fd, temporary_name = tempfile.mkstemp(prefix=".selection-", suffix=".tmp", dir=target.parent)
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(payload, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, target)
        except OSError as exc:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise MilestoneArtifactError(f"Could not persist milestone selection: {exc}") from exc
    return candidate
