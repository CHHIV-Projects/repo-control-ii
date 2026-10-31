from __future__ import annotations

import errno
import os
import re
import stat
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from ..scanner.git_ops import ScanError
from ..workflow.git_state import WorkflowGitStateError, inspect_git_state
from ..workflow.handoff_packets import (
    MAX_GIT_PATHS,
    MAX_PACKET_BYTES,
    MAX_SOURCE_BYTES,
    HandoffPacketError,
    _PRIVATE_KEY_MARKER,
    _open_conditions,
    _read_relative_source,
)
from ..workflow.lifecycle import WorkflowRecordError, load_milestone_workflow
from ..workflow.milestones import MilestoneArtifactError, load_milestone_selection
from ..workflow.authority import get_authorization_status, is_correction_mode
from ..workflow.context_verification import (
    build_authority_snapshot,
    build_verification_coverage,
    collect_verification_sources,
)
from .repository_registry import DOCUMENT_ROLES, RepositoryEntry

CONTRACT_VERSION = "1"
EVENT_LIMIT = 30
MAX_REPOSITORY_DOCUMENT_BYTES = MAX_SOURCE_BYTES
MAX_REPOSITORY_DOCUMENT_FILE_BYTES = 5_000_000
MAX_HISTORY_CANDIDATES = 2_000
MAX_HISTORY_SEARCH_FILES = 300
MAX_HISTORY_SEARCH_BYTES = 16_000_000
MAX_HISTORY_RESULTS = 20
DEFAULT_HISTORY_RESULTS = 10
MAX_HISTORY_QUERY_LENGTH = 256
MAX_HISTORY_DIRECTORIES = 2_000
MAX_HISTORY_DIRECTORY_ENTRIES = 50_000
MAX_HISTORY_RESPONSE_BYTES = 128_000
_MILESTONE_HISTORY_FILENAME = re.compile(
    r"^(M[0-9]{3}(?:\.[0-9]{3})+|R[0-9]{3}\.[0-9]{2})_"
    r"[a-z0-9]+(?:_[a-z0-9]+)*_(prompt|workflow|closeout)\.md$"
)
_ARCHIVE_MILESTONE_ID = re.compile(
    r"^((?:M[0-9]{3}(?:\.[0-9]{3})+|R[0-9]{3}\.[0-9]{2}|[0-9]{3}(?:\.[0-9]+)*))_"
)
_HISTORY_TOKEN = re.compile(r"[\w-]+", re.UNICODE)
_DOCUMENT_EXTENSIONS = frozenset({".md", ".txt", ".rst"})
_SENSITIVE_PATH_COMPONENTS = frozenset(
    {
        ".env",
        ".git",
        ".ssh",
        ".venv",
        "config",
        "configs",
        "configuration",
        "configurations",
        "credential",
        "credentials",
        "env",
        "private",
        "secret",
        "secrets",
        "venv",
    }
)
_HISTORICAL_PATH_COMPONENTS = frozenset({"archive", "archives", "historical", "history", "milestones"})


class ArchitectContractError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int) -> None:
        self.code = code
        self.message = message
        self.status_code = status_code
        super().__init__(message)


def _source_bytes(repository_root: Path, relative_path: str) -> tuple[str, int | None]:
    relative = PurePosixPath(relative_path)
    if (
        not relative_path
        or relative.is_absolute()
        or "\\" in relative_path
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        return "unsafe", None
    current = repository_root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return "unsafe", None
    try:
        resolved = repository_root.joinpath(*relative.parts).resolve(strict=True)
        if not resolved.is_relative_to(repository_root) or not resolved.is_file():
            return "unsafe", None
        return "present", resolved.stat().st_size
    except FileNotFoundError:
        return "missing", None
    except (OSError, RuntimeError):
        return "unreadable", None


def _event_view(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_id": event["event_id"],
        "timestamp": event["timestamp"],
        "event_type": event["event_type"],
        "reported_actor_role": event["actor_role"],
        "actor_authenticated": False,
        "summary": event["summary"],
        "binding": event.get("binding") is True and event["event_type"] == "lock_in_approved",
        "related_event_id": event.get("related_event_id"),
        "evidence_source": event.get("evidence_source"),
        "result": event.get("result"),
        "evidence_path": event.get("evidence_path"),
        "evidence_status": "reported" if event["event_type"] == "test_result_recorded" else None,
    }


def _bounded_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    selected = events[-EVENT_LIMIT:]
    return {
        "total_count": len(events),
        "returned_count": len(selected),
        "omitted_count": len(events) - len(selected),
        "events": [_event_view(event) for event in selected],
    }


def _get_selection(repository: RepositoryEntry, state_root: str | Path) -> dict[str, Any]:
    try:
        selection = load_milestone_selection(repository.path, state_root, repository.milestone_root)
    except (MilestoneArtifactError, OSError) as exc:
        raise ArchitectContractError("milestone_inventory_failed", str(exc), 503) from exc
    if selection["status"] == "invalid":
        message = selection.get("reason") or "The saved milestone selection is invalid."
        code = "missing_prompt" if "prompt" in message.lower() else "stale_milestone_pointer"
        status_code = 404 if code == "missing_prompt" else 409
        raise ArchitectContractError(code, message, status_code)
    return selection


def _load_workflow(
    repository: RepositoryEntry,
    state_root: str | Path,
    candidate: dict[str, Any],
) -> dict[str, Any]:
    prompt = PurePosixPath(candidate["prompt_path"])
    sidecar_path = prompt.with_name(prompt.name.removesuffix("_prompt.md") + "_workflow.md").as_posix()
    sidecar = repository.path.joinpath(*PurePosixPath(sidecar_path).parts)
    if sidecar.exists() or sidecar.is_symlink():
        try:
            _read_relative_source(repository.path, sidecar_path, max_bytes=5_000_000)
        except HandoffPacketError as exc:
            code = "source_oversized" if "exceeds" in str(exc) else "unsafe_workflow_sidecar"
            status_code = 413 if code == "source_oversized" else 422
            raise ArchitectContractError(code, str(exc), status_code) from exc
    try:
        return load_milestone_workflow(
            repository.path,
            state_root,
            repository.milestone_root,
            candidate,
        )
    except (MilestoneArtifactError, WorkflowRecordError, OSError) as exc:
        if "references a missing or mismatched closeout artifact" in str(exc):
            raise ArchitectContractError(
                "required_closeout_missing",
                "The workflow ledger records a submitted closeout, but the discovered closeout is missing or mismatched.",
                409,
            ) from exc
        raise ArchitectContractError("malformed_workflow_sidecar", str(exc), 422) from exc


def _base(
    repository: RepositoryEntry,
    registry_generation: int,
    state_root: str | Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    selection = _get_selection(repository, state_root)
    selected = selection.get("selection") if selection["status"] == "selected" else None
    envelope = {
        "contract_version": CONTRACT_VERSION,
        "repository": {
            "key": repository.key,
            "name": repository.name,
            "path": str(repository.path),
            "repository_id": repository.repository_id,
            "registry_generation": registry_generation,
        },
        "selection": {
            "status": selection["status"],
            "milestone_id": selected["milestone_id"] if selected else None,
            "prompt_path": selected["prompt_path"] if selected else None,
        },
    }
    return envelope, selected


def _milestone_view(
    repository: RepositoryEntry,
    state_root: str | Path,
    candidate: dict[str, Any],
    workflow: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if workflow is None:
        workflow = _load_workflow(repository, state_root, candidate)
    events = workflow["event_history"]
    sidecar_path = workflow["sidecar_path"]
    sidecar_status, sidecar_size = _source_bytes(repository.path, sidecar_path)
    if sidecar_status == "unsafe" or sidecar_status == "unreadable":
        raise ArchitectContractError(
            "unsafe_workflow_sidecar",
            f"Workflow sidecar {sidecar_path} is not a safe readable repository file.",
            422,
        )
    latest_lock_in = next(
        (
            event
            for event in reversed(events)
            if event["event_type"] == "lock_in_approved" and event.get("binding") is True
        ),
        None,
    )
    closeout = _closeout_metadata(repository, candidate, workflow["phase"])
    prompt_status, prompt_size = _source_bytes(repository.path, candidate["prompt_path"])
    if prompt_status != "present":
        raise ArchitectContractError(
            "missing_prompt" if prompt_status == "missing" else "unsafe_prompt",
            f"Selected prompt {candidate['prompt_path']} is {prompt_status}.",
            404 if prompt_status == "missing" else 422,
        )
    return {
        "id": candidate["milestone_id"],
        "phase": workflow["phase"],
        "waiting_reason": workflow.get("waiting_reason"),
        "waiting_category": workflow.get("waiting_category"),
        "prompt": {
            "path": candidate["prompt_path"],
            "status": prompt_status,
            "size_bytes": prompt_size,
            "source_type": "milestone_prompt",
        },
        "closeout": closeout,
        "workflow": {
            "path": sidecar_path,
            "status": "present" if sidecar_status == "present" else "absent",
            "size_bytes": sidecar_size,
            "source_type": "workflow_sidecar",
        },
        "event_count": workflow["event_count"],
        "latest_binding_lock_in": _event_view(latest_lock_in) if latest_lock_in else None,
        "open_conditions": _bounded_events(_open_conditions(workflow)),
        "lifecycle_phase": workflow["phase"],
        "next_authorized_action": workflow["next_action"],
        "lifecycle_source": {
            "source_type": "workflow_sidecar" if sidecar_status == "present" else "derived_empty_workflow",
            "path": sidecar_path,
        },
    }


def _closeout_metadata(
    repository: RepositoryEntry,
    candidate: dict[str, Any],
    phase: str,
) -> dict[str, Any]:
    closeout_path = candidate.get("closeout_path")
    if not closeout_path:
        expected_path = candidate["prompt_path"].removesuffix("_prompt.md") + "_closeout.md"
        source_status, size = _source_bytes(repository.path, expected_path)
        state = {
            "unsafe": "unsafe",
            "unreadable": "unreadable",
            "present": "ambiguous",
        }.get(source_status, "absent")
        required = phase == "REVIEW"
        return {
            "status": "required_missing" if required else state,
            "path": expected_path,
            "size_bytes": size,
            "source_status": source_status,
            "required_in_review": required,
            "lifecycle_phase": phase,
            "content": None,
            "source_type": "milestone_closeout",
        }
    status, size = _source_bytes(repository.path, closeout_path)
    if status != "present":
        raise ArchitectContractError(
            "unsafe_closeout",
            f"Configured closeout {closeout_path} is {status}.",
            422 if status in {"unsafe", "unreadable"} else 404,
        )
    return {
        "status": "present",
        "path": closeout_path,
        "size_bytes": size,
        "required_in_review": True,
        "lifecycle_phase": phase,
        "content": None,
        "source_type": "milestone_closeout",
    }


def _read_content(repository: RepositoryEntry, relative_path: str, source_type: str) -> dict[str, Any]:
    status, size = _source_bytes(repository.path, relative_path)
    if status != "present":
        code = "missing_source" if status == "missing" else "unsafe_source"
        http_status = 404 if status == "missing" else 422
        raise ArchitectContractError(code, f"Source {relative_path} is {status}.", http_status)
    if size is not None and size > MAX_SOURCE_BYTES:
        raise ArchitectContractError(
            "source_oversized",
            f"Source {relative_path} exceeds the {MAX_SOURCE_BYTES}-byte content limit; no partial content is returned.",
            413,
        )
    try:
        content = _read_relative_source(repository.path, relative_path)
    except HandoffPacketError as exc:
        code = "unsafe_source"
        if "private-key material" in str(exc):
            code = "unsafe_source"
        raise ArchitectContractError(code, str(exc), 422) from exc
    return {
        "source_type": source_type,
        "source_path": relative_path,
        "size_bytes": len(content.encode("utf-8")),
        "truncated": False,
        "content": content,
    }


def _repository_document_path(value: str | None, *, allow_historical: bool = False) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 512
        or "\\" in value
        or "\x00" in value
        or (len(value) > 1 and value[0].isalpha() and value[1] == ":")
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ArchitectContractError("invalid_document_path", "A valid repository-relative document path is required.", 400)
    path = PurePosixPath(value)
    parts = value.split("/")
    if path.is_absolute() or any(part in {"", ".", ".."} for part in parts):
        raise ArchitectContractError("invalid_document_path", "The document path must be repository-relative and traversal-free.", 400)
    lowered_parts = {part.lower() for part in parts}
    if lowered_parts & _SENSITIVE_PATH_COMPONENTS or any(
        part.startswith(".env.") or part.endswith((".pem", ".key", ".p12", ".pfx"))
        for part in lowered_parts
    ):
        raise ArchitectContractError(
            "document_path_not_allowed",
            "The requested path is outside the documentation allowlist.",
            403,
        )
    if not allow_historical and lowered_parts & _HISTORICAL_PATH_COMPONENTS:
        raise ArchitectContractError(
            "historical_document_not_supported",
            "Historical artifact discovery and retrieval are reserved for the separate historical capability.",
            403,
        )

    allowed_root = (
        parts[0] == "docs"
        or parts[0] in {".github", ".repo-control"}
        or (parts[0] == ".agents" and len(parts) == 4 and parts[1] == "skills" and parts[3] == "SKILL.md")
        or value == "README.md"
    )
    if not allowed_root and not allow_historical:
        raise ArchitectContractError(
            "document_path_not_allowed",
            "The requested path is outside the documentation allowlist.",
            403,
        )
    if value != "README.md" and Path(parts[-1]).suffix.lower() not in _DOCUMENT_EXTENSIONS:
        raise ArchitectContractError(
            "unsupported_document_type",
            "Only Markdown, plain-text, and reStructuredText files are supported.",
            415,
        )
    return value


def _read_repository_document(
    repository: RepositoryEntry,
    relative_path: str,
    *,
    allow_historical: bool = False,
) -> dict[str, Any]:
    relative_path = _repository_document_path(relative_path, allow_historical=allow_historical)
    parts = PurePosixPath(relative_path).parts
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        root_fd = os.open(repository.path, directory_flags)
    except OSError as exc:
        raise ArchitectContractError(
            "repository_document_unreadable",
            "The selected repository cannot be safely read.",
            422,
        ) from exc
    parent_fd = root_fd
    opened_directories: list[int] = []
    source_fd: int | None = None
    try:
        for part in parts[:-1]:
            parent_fd = os.open(part, directory_flags, dir_fd=parent_fd)
            opened_directories.append(parent_fd)
        try:
            source_fd = os.open(
                parts[-1],
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=parent_fd,
            )
        except FileNotFoundError as exc:
            raise ArchitectContractError(
                "missing_repository_document",
                "The requested document does not exist.",
                404,
            ) from exc
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise ArchitectContractError(
                    "unsafe_document_path",
                    "The requested path contains a symlink or non-directory component.",
                    422,
                ) from exc
            raise ArchitectContractError(
                "repository_document_unreadable",
                "The requested document cannot be safely opened.",
                422,
            ) from exc

        before = os.fstat(source_fd)
        if not stat.S_ISREG(before.st_mode):
            raise ArchitectContractError("unsafe_document_path", "The requested path is not a regular file.", 422)
        if before.st_size > MAX_REPOSITORY_DOCUMENT_FILE_BYTES:
            raise ArchitectContractError(
                "repository_document_oversized",
                f"The requested file exceeds the {MAX_REPOSITORY_DOCUMENT_FILE_BYTES}-byte source limit.",
                413,
            )

        read_limit = min(before.st_size, MAX_REPOSITORY_DOCUMENT_BYTES)
        raw = bytearray()
        while len(raw) < read_limit:
            chunk = os.read(source_fd, min(64 * 1024, read_limit - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)

        after = os.fstat(source_fd)
        try:
            current_path = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
        except OSError as exc:
            raise ArchitectContractError(
                "repository_document_changed",
                "The document changed or disappeared during the read.",
                409,
            ) from exc
        if (
            (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
            or (after.st_dev, after.st_ino) != (current_path.st_dev, current_path.st_ino)
            or len(raw) != read_limit
        ):
            raise ArchitectContractError(
                "repository_document_changed",
                "The document changed or disappeared during the read.",
                409,
            )

        truncated = before.st_size > MAX_REPOSITORY_DOCUMENT_BYTES
        visible = bytes(raw)
        try:
            content = visible.decode("utf-8")
        except UnicodeDecodeError as exc:
            if not truncated or exc.reason != "unexpected end of data" or exc.end != len(visible):
                raise ArchitectContractError(
                    "invalid_document_encoding",
                    "The document is not valid UTF-8 text.",
                    422,
                ) from exc
            visible = visible[: exc.start]
            try:
                content = visible.decode("utf-8")
            except UnicodeDecodeError as nested:
                raise ArchitectContractError(
                    "invalid_document_encoding",
                    "The document is not valid UTF-8 text.",
                    422,
                ) from nested
        if any(
            (ord(character) < 32 and character not in "\t\n\r") or ord(character) == 127
            for character in content
        ):
            raise ArchitectContractError(
                "invalid_document_encoding",
                "The document contains unsupported binary control characters.",
                422,
            )
        if _PRIVATE_KEY_MARKER.search(content):
            raise ArchitectContractError(
                "unsafe_document_content",
                "The requested document contains private-key material.",
                422,
            )
        return {
            "source_type": "repository_document",
            "path": relative_path,
            "file_size_bytes": before.st_size,
            "bytes_returned": len(content.encode("utf-8")),
            "character_count": len(content),
            "truncated": truncated,
            "working_tree_source": "current_working_tree",
            "git_state": "not_collected",
            "observed_at": datetime.now(UTC).isoformat(),
            "content": content,
        }
    except ArchitectContractError:
        raise
    except FileNotFoundError as exc:
        raise ArchitectContractError(
            "missing_repository_document",
            "The requested document does not exist.",
            404,
        ) from exc
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise ArchitectContractError(
                "unsafe_document_path",
                "The requested path contains a symlink or non-directory component.",
                422,
            ) from exc
        raise ArchitectContractError(
            "repository_document_unreadable",
            "The requested document cannot be safely read.",
            422,
        ) from exc
    finally:
        if source_fd is not None:
            os.close(source_fd)
        for directory_fd in reversed(opened_directories):
            os.close(directory_fd)
        os.close(root_fd)


def _historical_path_info(
    repository: RepositoryEntry,
    value: str,
    selected_milestone_id: str | None,
) -> dict[str, Any]:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 512
        or "\\" in value
        or "\x00" in value
        or (len(value) > 1 and value[0].isalpha() and value[1] == ":")
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ArchitectContractError(
            "invalid_history_path",
            "A valid repository-relative historical artifact path is required.",
            400,
        )
    path = PurePosixPath(value)
    parts = value.split("/")
    if path.is_absolute() or any(part in {"", ".", ".."} for part in parts):
        raise ArchitectContractError(
            "invalid_history_path",
            "The historical artifact path must be repository-relative and traversal-free.",
            400,
        )
    lowered_parts = {part.lower() for part in parts}
    if lowered_parts & _SENSITIVE_PATH_COMPONENTS or any(
        part.startswith(".env.") or part.endswith((".pem", ".key", ".p12", ".pfx"))
        for part in lowered_parts
    ):
        raise ArchitectContractError(
            "historical_path_not_allowed",
            "The requested path is outside the historical documentation corpus.",
            403,
        )
    if Path(parts[-1]).suffix.lower() not in _DOCUMENT_EXTENSIONS:
        raise ArchitectContractError(
            "unsupported_historical_document_type",
            "Historical artifacts must be Markdown, plain text, or reStructuredText.",
            415,
        )

    milestone_parts = PurePosixPath(repository.milestone_root).parts
    if len(parts) > len(milestone_parts) and tuple(parts[: len(milestone_parts)]) == milestone_parts:
        match = _MILESTONE_HISTORY_FILENAME.fullmatch(parts[-1])
        if match is None:
            raise ArchitectContractError(
                "historical_path_not_allowed",
                "Only canonical milestone prompts, workflow sidecars, and closeouts are historical artifacts.",
                403,
            )
        milestone_id, suffix = match.groups()
        return {
            "path": value,
            "artifact_type": f"milestone_{suffix}",
            "category": "milestone",
            "historical_root": repository.milestone_root,
            "milestone_id": milestone_id,
            "title": Path(parts[-1]).stem,
            "is_current": milestone_id == selected_milestone_id,
        }

    if len(parts) < 3 or tuple(parts[:2]) != ("docs", "history"):
        raise ArchitectContractError(
            "historical_path_not_allowed",
            "The requested path is outside the approved historical roots.",
            403,
        )
    relative_archive_parts = parts[2:]
    parent_parts = tuple(part.lower() for part in relative_archive_parts[:-1])
    filename = parts[-1]
    suffix = Path(filename).stem.rsplit("_", 1)[-1].lower()
    if "context" in parent_parts:
        artifact_type = "archived_context"
    elif "planning" in parent_parts:
        artifact_type = "archived_planning"
    elif "milestones" in parent_parts and suffix in {"prompt", "workflow", "closeout"}:
        artifact_type = f"milestone_{suffix}"
    else:
        artifact_type = "historical_document"
    id_match = _ARCHIVE_MILESTONE_ID.match(filename)
    return {
        "path": value,
        "artifact_type": artifact_type,
        "category": "archive",
        "historical_root": "docs/history",
        "milestone_id": id_match.group(1) if id_match else None,
        "title": Path(filename).stem,
        "is_current": False,
    }


def _historical_roots(repository: RepositoryEntry) -> list[tuple[str, Path]]:
    root = repository.path.resolve(strict=True)
    configured_roots = [repository.milestone_root, "docs/history"]
    roots: list[tuple[str, Path]] = []
    seen: set[tuple[str, ...]] = set()
    for relative_root in configured_roots:
        parts = PurePosixPath(relative_root).parts
        if not parts or PurePosixPath(relative_root).is_absolute() or "\\" in relative_root:
            raise ArchitectContractError(
                "unsafe_historical_root",
                "A configured historical root is not a safe repository-relative path.",
                422,
            )
        if parts in seen:
            continue
        seen.add(parts)
        current = root
        missing = False
        try:
            for part in parts:
                current = current / part
                try:
                    current.lstat()
                except FileNotFoundError:
                    missing = True
                    break
                if current.is_symlink():
                    raise ArchitectContractError(
                        "unsafe_historical_root",
                        "A historical root contains a symlink and cannot be traversed.",
                        422,
                    )
            if missing:
                continue
            resolved = current.resolve(strict=True)
            if not resolved.is_relative_to(root) or not resolved.is_dir():
                raise ArchitectContractError(
                    "unsafe_historical_root",
                    "A historical root is not a directory contained in the selected repository.",
                    422,
                )
            roots.append((PurePosixPath(*parts).as_posix(), resolved))
        except OSError as exc:
            raise ArchitectContractError(
                "historical_discovery_failed",
                "A historical root cannot be safely inspected.",
                503,
            ) from exc
    return roots


def _open_repository_directory(repository: RepositoryEntry, relative_path: str) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        root_fd = os.open(repository.path, flags)
    except OSError as exc:
        raise ArchitectContractError(
            "historical_discovery_failed",
            "The selected repository cannot be safely traversed.",
            503,
        ) from exc
    current_fd = root_fd
    try:
        for part in PurePosixPath(relative_path).parts:
            next_fd = os.open(part, flags, dir_fd=current_fd)
            if current_fd != root_fd:
                os.close(current_fd)
            current_fd = next_fd
        if current_fd == root_fd:
            return os.dup(root_fd)
        result_fd = current_fd
        current_fd = root_fd
        return result_fd
    except OSError as exc:
        if current_fd != root_fd:
            os.close(current_fd)
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise ArchitectContractError(
                "unsafe_historical_root",
                "A historical directory contains a symlink or non-directory component.",
                422,
            ) from exc
        raise ArchitectContractError(
            "historical_discovery_failed",
            "A historical directory cannot be safely opened.",
            503,
        ) from exc
    finally:
        os.close(root_fd)


def _discover_historical_candidates(
    repository: RepositoryEntry,
    selected_milestone_id: str | None,
) -> tuple[list[dict[str, Any]], bool, int]:
    candidates: list[dict[str, Any]] = []
    visited_directories = 0
    visited_entries = 0
    skipped_count = 0
    limited = False
    for root_relative, root_path in _historical_roots(repository):
        pending = [root_relative]
        while pending:
            relative_directory = pending.pop()
            visited_directories += 1
            if visited_directories > MAX_HISTORY_DIRECTORIES:
                return candidates, True, skipped_count
            try:
                directory_fd = _open_repository_directory(repository, relative_directory)
            except OSError as exc:
                raise ArchitectContractError(
                    "historical_discovery_failed",
                    "A historical directory cannot be safely inspected.",
                    503,
                ) from exc
            try:
                try:
                    with os.scandir(os.dup(directory_fd)) as iterator:
                        entries = []
                        for entry in iterator:
                            if visited_entries >= MAX_HISTORY_DIRECTORY_ENTRIES:
                                limited = True
                                break
                            visited_entries += 1
                            entries.append(entry)
                except OSError as exc:
                    raise ArchitectContractError(
                        "historical_discovery_failed",
                        "A historical directory cannot be safely inspected.",
                        503,
                    ) from exc
            finally:
                if directory_fd is not None:
                    os.close(directory_fd)
            if limited and visited_entries >= MAX_HISTORY_DIRECTORY_ENTRIES:
                candidates.sort(key=lambda item: item["path"])
                return candidates, True, skipped_count
            entries.sort(key=lambda entry: os.fsencode(entry.name))
            child_directories: list[str] = []
            for entry in entries:
                try:
                    if entry.is_symlink():
                        skipped_count += 1
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        child_directories.append(
                            PurePosixPath(relative_directory, entry.name).as_posix()
                        )
                        continue
                    file_stat = entry.stat(follow_symlinks=False)
                    if not stat.S_ISREG(file_stat.st_mode):
                        skipped_count += 1
                        continue
                    relative_path = PurePosixPath(relative_directory, entry.name).as_posix()
                    if Path(entry.name).suffix.lower() not in _DOCUMENT_EXTENSIONS:
                        continue
                    info = _historical_path_info(repository, relative_path, selected_milestone_id)
                except ArchitectContractError as exc:
                    if exc.code in {"historical_path_not_allowed", "unsupported_historical_document_type"}:
                        continue
                    raise
                except OSError:
                    skipped_count += 1
                    continue
                if info["is_current"]:
                    continue
                candidates.append(
                    {
                        **info,
                        "source_type": "historical_evidence",
                        "repository_key": repository.key,
                        "repository_id": repository.repository_id,
                        "file_size_bytes": file_stat.st_size,
                        "authority_classification": "historical_evidence",
                        "authority_note": (
                            "Historical planning evidence only; not current governing or lifecycle authority."
                        ),
                        "is_current_authority": False,
                        "working_tree_source": "current_working_tree",
                    }
                )
                if len(candidates) > MAX_HISTORY_CANDIDATES:
                    candidates.pop()
                    limited = True
                    candidates.sort(key=lambda item: item["path"])
                    return candidates, limited, skipped_count
            pending.extend(reversed(sorted(child_directories)))
    candidates.sort(key=lambda item: item["path"])
    return candidates, limited, skipped_count


def _history_query_tokens(query: str) -> list[str]:
    tokens = _HISTORY_TOKEN.findall(query.casefold())
    if not tokens:
        raise ArchitectContractError(
            "invalid_history_query",
            "A historical search query must contain at least one letter or number.",
            400,
        )
    return tokens


def _search_historical_candidates(
    repository: RepositoryEntry,
    candidates: list[dict[str, Any]],
    *,
    query: str | None,
    milestone_id: str | None,
    artifact_type: str | None,
    category: str | None,
    limit: int,
    scan_limited: bool,
    skipped_count: int,
) -> dict[str, Any]:
    filtered = [
        item
        for item in candidates
        if (milestone_id is None or item["milestone_id"] == milestone_id)
        and (artifact_type is None or item["artifact_type"] == artifact_type)
        and (category is None or item["category"] == category)
    ]
    tokens = _history_query_tokens(query) if query is not None else []
    matches: list[dict[str, Any]] = []
    files_scanned = 0
    bytes_scanned = 0
    limited = scan_limited
    if query is None:
        matches = filtered
    else:
        for item in filtered:
            if files_scanned >= MAX_HISTORY_SEARCH_FILES:
                limited = True
                break
            expected_bytes = min(item["file_size_bytes"], MAX_REPOSITORY_DOCUMENT_BYTES)
            if expected_bytes > MAX_HISTORY_SEARCH_BYTES - bytes_scanned:
                limited = True
                break
            try:
                document = _read_repository_document(
                    repository,
                    item["path"],
                    allow_historical=True,
                )
            except ArchitectContractError as exc:
                if exc.code in {
                    "repository_document_oversized",
                    "invalid_document_encoding",
                    "unsafe_document_content",
                }:
                    skipped_count += 1
                    limited = True
                    continue
                raise
            files_scanned += 1
            bytes_scanned += document["bytes_returned"]
            content = document["content"].casefold()
            searchable = f"{item['path']} {item['title']} {item['milestone_id'] or ''} {content}".casefold()
            if not all(token in searchable for token in tokens):
                continue
            occurrences = sum(searchable.count(token) for token in tokens)
            path_match = query.casefold() in item["path"].casefold()
            milestone_match = item["milestone_id"] is not None and query.casefold() == item["milestone_id"].casefold()
            matches.append(
                {
                    **item,
                    "match_metadata": {
                        "matched_terms": tokens,
                        "occurrence_count": occurrences,
                        "path_match": path_match,
                    },
                    "_ranking": (
                        -int(path_match),
                        -int(milestone_match),
                        -occurrences,
                        item["path"],
                    ),
                }
            )
            if document["truncated"]:
                limited = True
        matches.sort(key=lambda item: item["_ranking"])
        for item in matches:
            item.pop("_ranking", None)
    total_count = len(matches)
    results = matches[:limit]
    response = {
        "query": query,
        "filters": {
            "milestone_id": milestone_id,
            "artifact_type": artifact_type,
            "category": category,
        },
        "results": results,
        "returned_count": len(results),
        "total_count": total_count,
        "omitted_count": max(0, total_count - len(results)),
        "total_count_complete": not limited,
        "limited": limited,
        "truncated": limited or total_count > len(results),
        "candidates_considered": len(filtered),
        "files_scanned": files_scanned,
        "bytes_scanned": bytes_scanned,
        "skipped_count": skipped_count,
    }
    while results and response_size_bytes(response) > MAX_HISTORY_RESPONSE_BYTES:
        results.pop()
        response["returned_count"] = len(results)
        response["omitted_count"] = total_count - len(results)
        response["truncated"] = True
    if response_size_bytes(response) > MAX_HISTORY_RESPONSE_BYTES:
        raise ArchitectContractError(
            "historical_response_too_large",
            "The bounded historical search response exceeds its response limit.",
            413,
        )
    return response


def _history_filters(
    query: str | None,
    milestone_id: str | None,
    artifact_type: str | None,
    category: str | None,
    limit: int | None,
) -> int:
    if query is not None and len(query) > MAX_HISTORY_QUERY_LENGTH:
        raise ArchitectContractError(
            "invalid_history_query",
            f"Historical search query must be at most {MAX_HISTORY_QUERY_LENGTH} characters.",
            400,
        )
    if query is not None:
        _history_query_tokens(query)
    if milestone_id is not None and not re.fullmatch(
        r"(?:M[0-9]{3}(?:\.[0-9]{3})+|R[0-9]{3}\.[0-9]{2}|[0-9]{3}(?:\.[0-9]+)*)",
        milestone_id,
    ):
        raise ArchitectContractError(
            "invalid_milestone_filter",
            "The historical milestone filter is not a supported milestone identifier.",
            400,
        )
    valid_types = {
        "milestone_prompt",
        "milestone_workflow",
        "milestone_closeout",
        "archived_context",
        "archived_planning",
        "historical_document",
    }
    if artifact_type is not None and artifact_type not in valid_types:
        raise ArchitectContractError(
            "unsupported_historical_artifact_type",
            "The requested historical artifact type is not supported.",
            400,
        )
    if category is not None and category not in {"milestone", "archive"}:
        raise ArchitectContractError(
            "unsupported_historical_category",
            "The requested historical category must be 'milestone' or 'archive'.",
            400,
        )
    result_limit = DEFAULT_HISTORY_RESULTS if limit is None else limit
    if not isinstance(result_limit, int) or isinstance(result_limit, bool) or not 1 <= result_limit <= MAX_HISTORY_RESULTS:
        raise ArchitectContractError(
            "invalid_history_limit",
            f"Historical result limit must be between 1 and {MAX_HISTORY_RESULTS}.",
            400,
        )
    if query is None and not (
        milestone_id is not None or (category is not None and artifact_type is not None)
    ):
        raise ArchitectContractError(
            "historical_inventory_too_broad",
            "An inventory request requires a milestone ID or both category and artifact type filters.",
            400,
        )
    return result_limit


def _search_historical(
    repository: RepositoryEntry,
    state_root: str | Path,
    *,
    query: str | None,
    milestone_id: str | None,
    artifact_type: str | None,
    category: str | None,
    limit: int | None,
) -> dict[str, Any]:
    result_limit = _history_filters(query, milestone_id, artifact_type, category, limit)
    selection = _get_selection(repository, state_root)
    selected = selection.get("selection") if selection["status"] == "selected" else None
    selected_id = selected.get("milestone_id") if selected else None
    candidates, limited, skipped_count = _discover_historical_candidates(repository, selected_id)
    return _search_historical_candidates(
        repository,
        candidates,
        query=query,
        milestone_id=milestone_id,
        artifact_type=artifact_type,
        category=category,
        limit=result_limit,
        scan_limited=limited,
        skipped_count=skipped_count,
    )


def _read_historical_document(
    repository: RepositoryEntry,
    state_root: str | Path,
    relative_path: str | None,
) -> dict[str, Any]:
    selection = _get_selection(repository, state_root)
    selected = selection.get("selection") if selection["status"] == "selected" else None
    selected_id = selected.get("milestone_id") if selected else None
    if not isinstance(relative_path, str):
        raise ArchitectContractError(
            "invalid_history_path",
            "A repository-relative historical artifact path is required.",
            400,
        )
    info = _historical_path_info(repository, relative_path, selected_id)
    if info["is_current"]:
        raise ArchitectContractError(
            "current_artifact_not_historical",
            "Artifacts belonging to the selected milestone must be read through current milestone tools.",
            409,
        )
    try:
        document = _read_repository_document(repository, relative_path, allow_historical=True)
    except ArchitectContractError as exc:
        mapped_codes = {
            "missing_repository_document": ("historical_artifact_missing", 404),
            "unsafe_document_path": ("unsafe_historical_path", 422),
            "repository_document_oversized": ("historical_artifact_oversized", 413),
            "invalid_document_encoding": ("invalid_historical_encoding", 422),
            "unsafe_document_content": ("unsafe_historical_content", 422),
            "repository_document_changed": ("historical_artifact_changed", 409),
            "repository_document_unreadable": ("historical_artifact_unreadable", 422),
        }
        if exc.code not in mapped_codes:
            raise
        code, status_code = mapped_codes[exc.code]
        raise ArchitectContractError(code, str(exc), status_code) from exc
    return {
        "source_type": "historical_evidence",
        "artifact_type": info["artifact_type"],
        "category": info["category"],
        "historical_root": info["historical_root"],
        "milestone_id": info["milestone_id"],
        "title": info["title"],
        "path": document["path"],
        "repository_key": repository.key,
        "repository_id": repository.repository_id,
        "file_size_bytes": document["file_size_bytes"],
        "bytes_returned": document["bytes_returned"],
        "character_count": document["character_count"],
        "truncated": document["truncated"],
        "working_tree_source": "current_working_tree",
        "authority_classification": "historical_evidence",
        "authority_note": "Historical planning evidence only; not current governing or lifecycle authority.",
        "is_current_authority": False,
        "observed_at": document["observed_at"],
        "content": document["content"],
    }


def _git_view(repository: RepositoryEntry) -> dict[str, Any]:
    try:
        state = inspect_git_state(str(repository.path))
    except (ScanError, WorkflowGitStateError, OSError) as exc:
        raise ArchitectContractError("git_inspection_failed", str(exc), 503) from exc

    def paths_view(category: str) -> dict[str, Any]:
        item = state["working_tree"][category]
        paths = item["paths"]
        selected = paths[:MAX_GIT_PATHS]
        return {
            "total_count": item["count"],
            "returned_count": len(selected),
            "omitted_count": item["count"] - len(selected),
            "paths": selected,
        }

    branch = state["branch"]
    upstream = state["upstream"]
    return {
        "source": {
            "source_type": "local_git_state",
            "repository_path": str(repository.path),
            "observed_at": datetime.now(UTC).isoformat(),
        },
        "branch": branch.get("name") if branch.get("state") == "attached" else None,
        "head": state["head"],
        "upstream": {
            **upstream,
            "freshness": "local_tracking_refs_only",
            "remote_refresh_performed": False,
        },
        "working_tree": {
            "classification": state["workflow_state"],
            "is_clean": state["working_tree"]["is_clean"],
            "staged": paths_view("staged"),
            "unstaged": paths_view("unstaged"),
            "untracked": paths_view("untracked"),
            "unmerged": paths_view("unmerged"),
        },
        "git_operations": state["git_operations"],
        "git_operation_in_progress": state["git_operation_in_progress"],
    }


def _document_inventory(repository: RepositoryEntry) -> list[dict[str, Any]]:
    documents = []
    for role in sorted(DOCUMENT_ROLES):
        relative_path = repository.documents.get(role)
        if not relative_path:
            documents.append(
                {
                    "role": role,
                    "configured": False,
                    "path": None,
                    "status": "not_configured",
                    "size_bytes": None,
                    "source_type": "governing_document",
                }
            )
            continue
        status, size = _source_bytes(repository.path, relative_path)
        if status == "present" and size is not None and size > MAX_SOURCE_BYTES:
            status = "oversized"
        documents.append(
            {
                "role": role,
                "configured": True,
                "path": relative_path,
                "status": status,
                "size_bytes": size,
                "source_type": "governing_document",
            }
        )
    return documents


def _selected_response(
    resource: str,
    repository: RepositoryEntry,
    state_root: str | Path,
    candidate: dict[str, Any],
) -> dict[str, Any]:
    if resource == "prompt":
        return _read_content(repository, candidate["prompt_path"], "milestone_prompt")

    if resource == "closeout":
        try:
            workflow = _load_workflow(repository, state_root, candidate)
        except ArchitectContractError as exc:
            if exc.code != "required_closeout_missing":
                raise
            expected_path = candidate["prompt_path"].removesuffix("_prompt.md") + "_closeout.md"
            return {
                "status": "required_missing",
                "path": expected_path,
                "size_bytes": None,
                "required_in_review": True,
                "lifecycle_status": "invalid",
                "lifecycle_phase": None,
                "contract_error": {"code": exc.code, "message": str(exc)},
                "content": None,
                "source_type": "milestone_closeout",
            }
        closeout = _closeout_metadata(repository, candidate, workflow["phase"])
        if closeout["status"] == "present":
            closeout["content"] = _read_content(
                repository,
                closeout["path"],
                "milestone_closeout",
            )
        return closeout

    if resource in {"milestone", "workflow", "evidence", "review"}:
        workflow = _load_workflow(repository, state_root, candidate)
        milestone = _milestone_view(repository, state_root, candidate, workflow)
        events = workflow["event_history"]
        if resource == "milestone":
            return milestone
        if resource == "workflow":
            return {
                "source": milestone["lifecycle_source"],
                "phase": milestone["phase"],
                "waiting_reason": milestone["waiting_reason"],
                "waiting_category": milestone["waiting_category"],
                "event_count": len(events),
                "events": _bounded_events(events),
                "binding_lock_ins": _bounded_events(
                    [
                        event for event in events
                        if event["event_type"] == "lock_in_approved" and event.get("binding") is True
                    ]
                ),
                "open_conditions": milestone["open_conditions"],
                "next_authorized_action": milestone["next_authorized_action"],
            }
        if resource == "evidence":
            test_events = [
                event for event in events
                if event["event_type"] == "test_result_recorded"
            ]
            evidence = _bounded_events(test_events)
            return {
                "source": milestone["lifecycle_source"],
                "reported_vs_machine_observed": "reported",
                **evidence,
            }
        git_state = _git_view(repository)
        verification_snapshot = None
        verification_sources = collect_verification_sources(
            repository,
            candidate,
            state_root,
        )
        procedure = verification_sources["procedure"]
        raw_git_state = verification_sources["git_state"]
        approved_dispatches = verification_sources["approved_dispatches"]
        authorization = get_authorization_status(
            workflow["phase"],
            is_correction_mode(workflow),
        )
        if (
            raw_git_state is not None
            and verification_sources["dispatch_failure_type"] is None
            and procedure["inspection_status"] == "available"
        ):
            verification_snapshot = build_authority_snapshot(
                repository={
                    "key": repository.key,
                    "repository_id": repository.repository_id,
                    "path": str(repository.path),
                },
                candidate=candidate,
                workflow=workflow,
                authorization=authorization,
                procedure=procedure,
                git_state=raw_git_state,
                approved_dispatches=approved_dispatches,
            )
        verification_coverage = build_verification_coverage(
            workflow["event_history"],
            verification_snapshot,
        )
        return {
            "repository": {
                "key": repository.key,
                "name": repository.name,
                "path": str(repository.path),
            },
            "milestone": milestone,
            "binding_lock_ins": _bounded_events(
                [
                    event for event in events
                    if event["event_type"] == "lock_in_approved" and event.get("binding") is True
                ]
            ),
            "test_evidence": {
                "reported_vs_machine_observed": "reported",
                **_bounded_events([
                    event for event in events
                    if event["event_type"] == "test_result_recorded"
                ]),
            },
            "git": git_state,
            "context_verification_coverage": verification_coverage,
            "governing_documents": _document_inventory(repository),
            "source_references": [
                {"source_type": "milestone_prompt", "path": candidate["prompt_path"]},
                {"source_type": "workflow_sidecar", "path": milestone["workflow"]["path"]},
                *(
                    [{"source_type": "milestone_closeout", "path": milestone["closeout"]["path"]}]
                    if milestone["closeout"]["status"] == "present"
                    else []
                ),
            ],
        }
    raise ArchitectContractError("unsupported_resource", "Unsupported Architect contract resource.", 404)


def build_architect_response(
    *,
    repository: RepositoryEntry,
    registry_generation: int,
    state_root: str | Path,
    resource: str,
    role: str | None = None,
    document_path: str | None = None,
    history_query: str | None = None,
    history_milestone_id: str | None = None,
    history_artifact_type: str | None = None,
    history_category: str | None = None,
    history_limit: int | None = None,
    expected_repository_key: str | None = None,
    expected_generation: int | None = None,
    expected_milestone_id: str | None = None,
) -> dict[str, Any]:
    if resource in {"repository_document", "history_search", "history_document"}:
        if not expected_repository_key or expected_generation is None:
            raise ArchitectContractError(
                "invalid_precondition",
                "This repository-scoped read requires expected_repository_key and expected_generation.",
                400,
            )
    if expected_repository_key is not None and expected_repository_key != repository.key:
        raise ArchitectContractError(
            "stale_repository_selection",
            "The active repository differs from the expected repository key.",
            409,
        )
    if expected_generation is not None and expected_generation != registry_generation:
        raise ArchitectContractError(
            "stale_repository_generation",
            "The repository registry generation changed; refresh status before retrying.",
            409,
        )
    if resource == "repository_document":
        document_path = _repository_document_path(document_path)
    if resource in {"repository_document", "history_search", "history_document"}:
        envelope = {
            "contract_version": CONTRACT_VERSION,
            "repository": {
                "key": repository.key,
                "name": repository.name,
                "path": str(repository.path),
                "repository_id": repository.repository_id,
                "registry_generation": registry_generation,
            },
            "selection": {
                "status": "not_required" if resource == "repository_document" else "not_required_for_history",
                "milestone_id": None,
                "prompt_path": None,
            },
        }
        candidate = None
    else:
        envelope, candidate = _base(repository, registry_generation, state_root)
    if expected_milestone_id is not None and (
        candidate is None or candidate["milestone_id"] != expected_milestone_id
    ):
        raise ArchitectContractError(
            "stale_milestone_selection",
            "The selected milestone differs from the expected milestone ID.",
            409,
        )

    if resource == "history_search":
        envelope["data"] = _search_historical(
            repository,
            state_root,
            query=history_query,
            milestone_id=history_milestone_id,
            artifact_type=history_artifact_type,
            category=history_category,
            limit=history_limit,
        )
    elif resource == "history_document":
        envelope["data"] = _read_historical_document(repository, state_root, document_path)
    elif resource == "status":
        data: dict[str, Any] = {
            "service": "repo-control",
            "status": "available",
            "milestone_selected": candidate is not None,
        }
        if candidate:
            data["milestone"] = _milestone_view(repository, state_root, candidate)
        envelope["data"] = data
    elif resource == "documents":
        inventory = _document_inventory(repository)
        if role is not None:
            if role not in DOCUMENT_ROLES:
                raise ArchitectContractError("unsupported_document_role", "Unsupported governing-document role.", 404)
            configured_path = repository.documents.get(role)
            if not configured_path:
                raise ArchitectContractError(
                    "document_role_not_configured",
                    f"Governing-document role {role!r} is not configured for this repository.",
                    404,
                )
            envelope["data"] = next(item for item in inventory if item["role"] == role)
            envelope["data"]["content"] = _read_content(repository, configured_path, "governing_document")
        else:
            envelope["data"] = {
                "source_type": "repository_registry",
                "documents": inventory,
            }
    elif resource == "repository_document":
        envelope["data"] = {
            "repository": {
                "key": repository.key,
                "repository_id": repository.repository_id,
            },
            **_read_repository_document(repository, document_path),
        }
    elif resource == "git":
        envelope["data"] = _git_view(repository)
    else:
        if resource not in {"milestone", "prompt", "workflow", "closeout", "evidence", "review"}:
            raise ArchitectContractError("unsupported_resource", "Unsupported Architect contract resource.", 404)
        if candidate is None:
            raise ArchitectContractError(
                "no_selected_milestone",
                "No milestone is selected for the active repository.",
                409,
            )
        envelope["data"] = _selected_response(resource, repository, state_root, candidate)

    envelope["source_scope"] = {
        "repository_key": repository.key,
        "milestone_id": candidate["milestone_id"] if candidate else None,
    }
    return envelope


def response_size_bytes(payload: dict[str, Any]) -> int:
    import json

    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8"))


def maximum_response_bytes() -> int:
    return MAX_PACKET_BYTES
