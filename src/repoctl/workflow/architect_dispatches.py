from __future__ import annotations

import json
import os
import re
import stat
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from ..scanner.core import state_artifact_lock
from ..scanner.util import make_repository_id
from .milestones import MilestoneArtifactError, _root_paths, discover_milestones

_SCHEMA_VERSION = 1
_MILESTONE_ID_RE = re.compile(r"(?:M[0-9]{3}(?:\.[0-9]{3})+|R[0-9]{3}\.[0-9]{2})\Z")
_SLUG_RE = re.compile(r"[a-z0-9]+(?:_[a-z0-9]+)*\Z")
_RELATED_ID_RE = re.compile(r"[0-9a-f]{32}\Z")
_STATUSES = {"PENDING", "APPROVED", "REJECTED"}
_TYPES = {"PROMPT", "INSTRUCTION", "RESPONSE"}
MAX_BODY_BYTES = 64 * 1024
MAX_TITLE_CHARS = 200
MAX_DISPATCHES_PER_REPOSITORY = 50
MAX_READ_LIMIT = 5
MAX_LEDGER_BYTES = 8 * 1024 * 1024


class ArchitectDispatchError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _validate_milestone_id(value: str) -> str:
    if not isinstance(value, str) or not _MILESTONE_ID_RE.fullmatch(value):
        raise ArchitectDispatchError("Target milestone ID must use a supported canonical form.")
    return value


def _validate_body(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ArchitectDispatchError("Dispatch body must not be empty.")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ArchitectDispatchError("Dispatch body contains invalid Unicode.") from exc
    if size > MAX_BODY_BYTES:
        raise ArchitectDispatchError(f"Dispatch body exceeds the {MAX_BODY_BYTES}-byte limit.")
    if "\x00" in value:
        raise ArchitectDispatchError("Dispatch body contains an unsupported NUL character.")
    if any(ord(character) < 32 and character not in "\t\n\r" for character in value):
        raise ArchitectDispatchError("Dispatch body contains unsupported control characters.")
    return value


def _slug(title: str) -> str:
    if not isinstance(title, str) or not title.strip() or len(title) > MAX_TITLE_CHARS:
        raise ArchitectDispatchError(f"Prompt title must be non-empty and at most {MAX_TITLE_CHARS} characters.")
    try:
        title.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ArchitectDispatchError("Prompt title contains invalid Unicode.") from exc
    if any(ord(character) < 32 or ord(character) == 127 for character in title):
        raise ArchitectDispatchError("Prompt title contains unsupported control characters.")
    slug = re.sub(r"[^a-z0-9]+", "_", title.strip().lower()).strip("_")
    if not slug or not _SLUG_RE.fullmatch(slug):
        raise ArchitectDispatchError("Prompt title cannot be converted to a canonical milestone filename.")
    return slug


def _state_file(repository_root: str | Path, state_root: str | Path, repository_id: str) -> Path:
    root = Path(repository_root).resolve(strict=True)
    external_root = Path(state_root).expanduser().resolve()
    if external_root == root or external_root.is_relative_to(root):
        raise ArchitectDispatchError("Architect dispatch state must be stored outside the repository.")
    if repository_id != make_repository_id(root):
        raise ArchitectDispatchError("Repository identity does not match the selected repository.")
    state_directory_candidate = external_root / repository_id
    if state_directory_candidate.is_symlink():
        raise ArchitectDispatchError("Architect dispatch state directory cannot be a symlink.")
    state_directory = state_directory_candidate.resolve()
    if not state_directory.is_relative_to(external_root):
        raise ArchitectDispatchError("Architect dispatch state directory resolves outside the configured state root.")
    if state_directory.is_relative_to(root):
        raise ArchitectDispatchError("Architect dispatch state must be stored outside the repository.")
    return state_directory / "architect_dispatches.json"


def _empty_ledger(repository_id: str) -> dict[str, Any]:
    return {"schema_version": _SCHEMA_VERSION, "repository_id": repository_id, "dispatches": []}


def _read_ledger(path: Path, repository_id: str) -> dict[str, Any]:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return _empty_ledger(repository_id)
    except OSError as exc:
        raise ArchitectDispatchError(f"Could not inspect Architect dispatch state: {exc}") from exc
    try:
        with os.fdopen(descriptor, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode):
                raise ArchitectDispatchError("Architect dispatch state must be a regular file.")
            if metadata.st_size > MAX_LEDGER_BYTES:
                raise ArchitectDispatchError("Architect dispatch state exceeds its safety limit.")
            raw = stream.read(MAX_LEDGER_BYTES + 1)
        if len(raw) > MAX_LEDGER_BYTES:
            raise ArchitectDispatchError("Architect dispatch state exceeds its safety limit.")
        if len(raw) != metadata.st_size:
            raise ArchitectDispatchError("Architect dispatch state changed while it was being read.")
        payload = json.loads(raw.decode("utf-8"))
    except ArchitectDispatchError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArchitectDispatchError(f"Could not read Architect dispatch state: {exc}") from exc
    return _validate_ledger(payload, repository_id)


def _validate_ledger(payload: Any, repository_id: str) -> dict[str, Any]:
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != _SCHEMA_VERSION
        or payload.get("repository_id") != repository_id
        or not isinstance(payload.get("dispatches"), list)
        or len(payload["dispatches"]) > MAX_DISPATCHES_PER_REPOSITORY
    ):
        raise ArchitectDispatchError("Architect dispatch state has an invalid or mismatched record shape.")
    seen_ids: set[str] = set()
    for item in payload["dispatches"]:
        try:
            if isinstance(item, dict) and isinstance(item.get("body"), str):
                _validate_body(item["body"])
        except ArchitectDispatchError as exc:
            raise ArchitectDispatchError("Architect dispatch state contains an invalid dispatch body.") from exc
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("dispatch_id"), str)
            or not _RELATED_ID_RE.fullmatch(item["dispatch_id"])
            or item["dispatch_id"] in seen_ids
            or item.get("repository_id") != repository_id
            or not isinstance(item.get("type"), str)
            or item["type"] not in _TYPES
            or not isinstance(item.get("status"), str)
            or item["status"] not in _STATUSES
            or not isinstance(item.get("body"), str)
            or len(item["body"].encode("utf-8")) > MAX_BODY_BYTES
            or not isinstance(item.get("milestone_id"), str)
            or not _MILESTONE_ID_RE.fullmatch(item["milestone_id"])
            or not isinstance(item.get("created_at"), str)
            or not isinstance(item.get("updated_at"), str)
        ):
            raise ArchitectDispatchError("Architect dispatch state contains an invalid dispatch record.")
        if item["type"] == "PROMPT" and (
            not isinstance(item.get("title"), str)
            or not item["title"].strip()
            or len(item["title"]) > MAX_TITLE_CHARS
            or not isinstance(item.get("slug"), str)
            or not _SLUG_RE.fullmatch(item["slug"])
            or not isinstance(item.get("canonical_destination"), str)
        ):
            raise ArchitectDispatchError("Architect dispatch state contains an invalid prompt record.")
        seen_ids.add(item["dispatch_id"])
    return payload


def _preserve_dispatch_ledger(source_directory: Path, destination_directory: Path, repository_id: str) -> None:
    source = source_directory / "architect_dispatches.json"
    destination = destination_directory / "architect_dispatches.json"
    try:
        descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise ArchitectDispatchError(f"Could not inspect Architect dispatch state for scan preservation: {exc}") from exc

    try:
        with os.fdopen(descriptor, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode):
                raise ArchitectDispatchError("Architect dispatch state must be a regular file.")
            if metadata.st_size > MAX_LEDGER_BYTES:
                raise ArchitectDispatchError("Architect dispatch state exceeds its safety limit.")
            ledger_bytes = stream.read(MAX_LEDGER_BYTES + 1)
        if len(ledger_bytes) > MAX_LEDGER_BYTES or len(ledger_bytes) != metadata.st_size:
            raise ArchitectDispatchError("Architect dispatch state changed or exceeded its safety limit during scan.")
        try:
            payload = json.loads(ledger_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ArchitectDispatchError(f"Could not read Architect dispatch state for scan preservation: {exc}") from exc
        _validate_ledger(payload, repository_id)

        destination_fd = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            stat.S_IMODE(metadata.st_mode),
        )
        try:
            with os.fdopen(destination_fd, "wb") as stream:
                stream.write(ledger_bytes)
                stream.flush()
                os.fsync(stream.fileno())
                os.fchmod(stream.fileno(), stat.S_IMODE(metadata.st_mode))
            os.utime(
                destination,
                ns=(metadata.st_atime_ns, metadata.st_mtime_ns),
                follow_symlinks=False,
            )
        except OSError:
            destination.unlink(missing_ok=True)
            raise
        if destination.read_bytes() != ledger_bytes:
            destination.unlink(missing_ok=True)
            raise ArchitectDispatchError("Preserved Architect dispatch state did not match the original bytes.")
        _read_ledger(destination, repository_id)
    except ArchitectDispatchError:
        raise
    except OSError as exc:
        raise ArchitectDispatchError(f"Could not preserve Architect dispatch state during scan: {exc}") from exc


def _write_ledger(path: Path, payload: dict[str, Any]) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    if len(encoded.encode("utf-8")) > MAX_LEDGER_BYTES:
        raise ArchitectDispatchError("Architect dispatch state exceeds its safety limit.")
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.parent.is_symlink() or not path.parent.resolve(strict=True).is_relative_to(path.parent.parent.resolve(strict=True)):
            raise ArchitectDispatchError("Architect dispatch state directory is unsafe.")
        fd, temporary_name = tempfile.mkstemp(prefix=".architect-dispatch-", suffix=".tmp", dir=path.parent)
    except ArchitectDispatchError:
        raise
    except OSError as exc:
        raise ArchitectDispatchError(f"Could not prepare Architect dispatch state: {exc}") from exc
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    except OSError as exc:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError as cleanup_error:
            raise ArchitectDispatchError(
                f"Could not persist Architect dispatch state ({exc}); temporary state cleanup also failed: {cleanup_error}"
            ) from cleanup_error
        raise ArchitectDispatchError(f"Could not persist Architect dispatch state: {exc}") from exc


def _new_dispatch(
    repository_root: str | Path,
    state_root: str | Path,
    repository_id: str,
    *,
    dispatch_type: str,
    milestone_id: str,
    body: str,
    title: str | None = None,
    related_event_id: str | None = None,
    milestone_root: str = "docs/milestones",
) -> dict[str, Any]:
    if dispatch_type not in _TYPES:
        raise ArchitectDispatchError("Unsupported Architect dispatch type.")
    target_id = _validate_milestone_id(milestone_id)
    clean_body = _validate_body(body)
    record: dict[str, Any] = {
        "dispatch_id": uuid.uuid4().hex,
        "repository_id": repository_id,
        "milestone_id": target_id,
        "type": dispatch_type,
        "status": "PENDING",
        "source": "architect_mcp",
        "created_at": _now(),
        "updated_at": _now(),
        "body": clean_body,
    }
    if dispatch_type == "PROMPT":
        record["slug"] = _slug(title)
        record["title"] = title.strip()
        try:
            _, resolved_milestone_root = _root_paths(Path(repository_root), milestone_root)
        except MilestoneArtifactError as exc:
            raise ArchitectDispatchError(str(exc)) from exc
        filename = f"{target_id}_{record['slug']}_prompt.md"
        record["canonical_destination"] = (
            resolved_milestone_root.relative_to(Path(repository_root).resolve(strict=True)).as_posix()
            + "/"
            + filename
        )
    elif title is not None:
        raise ArchitectDispatchError("Only PROMPT dispatches may include a title.")
    if related_event_id is not None:
        if not isinstance(related_event_id, str) or not _RELATED_ID_RE.fullmatch(related_event_id):
            raise ArchitectDispatchError("Related workflow event ID must be a 32-character hexadecimal ID.")
        record["related_event_id"] = related_event_id
    _state_file(repository_root, state_root, repository_id)
    return record


def submit_dispatch(
    repository_root: str | Path,
    state_root: str | Path,
    repository_id: str,
    *,
    dispatch_type: str,
    milestone_id: str,
    body: str,
    title: str | None = None,
    related_event_id: str | None = None,
    milestone_root: str = "docs/milestones",
) -> dict[str, Any]:
    record = _new_dispatch(
        repository_root,
        state_root,
        repository_id,
        dispatch_type=dispatch_type,
        milestone_id=milestone_id,
        body=body,
        title=title,
        related_event_id=related_event_id,
        milestone_root=milestone_root,
    )
    path = _state_file(repository_root, state_root, repository_id)
    with state_artifact_lock(state_root):
        ledger = _read_ledger(path, repository_id)
        if len(ledger["dispatches"]) >= MAX_DISPATCHES_PER_REPOSITORY:
            raise ArchitectDispatchError(
                f"Repository dispatch limit reached ({MAX_DISPATCHES_PER_REPOSITORY}); no pending dispatch was discarded."
            )
        ledger["dispatches"].append(record)
        _write_ledger(path, ledger)
    return dict(record)


def list_dispatches(
    repository_root: str | Path,
    state_root: str | Path,
    repository_id: str,
    *,
    milestone_id: str | None = None,
    status: str | None = None,
    limit: int = MAX_READ_LIMIT,
) -> list[dict[str, Any]]:
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_READ_LIMIT:
        raise ArchitectDispatchError(f"Dispatch read limit must be between 1 and {MAX_READ_LIMIT}.")
    if status is not None and status not in _STATUSES:
        raise ArchitectDispatchError(f"Dispatch status must be one of {sorted(_STATUSES)}.")
    if milestone_id is not None:
        _validate_milestone_id(milestone_id)
    path = _state_file(repository_root, state_root, repository_id)
    with state_artifact_lock(state_root):
        ledger = _read_ledger(path, repository_id)
    records = [
        item
        for item in ledger["dispatches"]
        if (milestone_id is None or item["milestone_id"] == milestone_id)
        and (status is None or item["status"] == status)
    ]
    return [dict(item) for item in reversed(records[-limit:])]


def list_outstanding_guidance(
    repository_root: str | Path,
    state_root: str | Path,
    repository_id: str,
    milestone_id: str,
    closeout_timestamp: str,
) -> list[dict[str, Any]]:
    try:
        latest_closeout_at = datetime.fromisoformat(closeout_timestamp)
    except (TypeError, ValueError) as exc:
        raise ArchitectDispatchError("The latest closeout has an invalid timestamp.") from exc
    if latest_closeout_at.utcoffset() is None:
        raise ArchitectDispatchError("The latest closeout timestamp has no UTC offset.")

    outstanding: list[tuple[datetime, dict[str, Any]]] = []
    for dispatch in list_dispatches(
        repository_root,
        state_root,
        repository_id,
        milestone_id=milestone_id,
        status="APPROVED",
    ):
        if (
            dispatch.get("repository_id") != repository_id
            or dispatch.get("status") != "APPROVED"
            or dispatch.get("milestone_id") != milestone_id
            or dispatch.get("approved_milestone_id") != milestone_id
            or dispatch.get("type") not in {"INSTRUCTION", "RESPONSE"}
        ):
            continue
        try:
            approved_at = datetime.fromisoformat(dispatch["approved_at"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ArchitectDispatchError(
                f"Approved dispatch {dispatch['dispatch_id']} has no valid approval timestamp."
            ) from exc
        if approved_at.utcoffset() is None:
            raise ArchitectDispatchError(
                f"Approved dispatch {dispatch['dispatch_id']} has no UTC offset."
            )
        if approved_at > latest_closeout_at:
            outstanding.append((approved_at, dispatch))
    return [dict(record) for _, record in sorted(outstanding, key=lambda item: item[0])]


def list_owner_dispatches(
    repository_root: str | Path,
    state_root: str | Path,
    repository_id: str,
) -> list[dict[str, Any]]:
    path = _state_file(repository_root, state_root, repository_id)
    with state_artifact_lock(state_root):
        ledger = _read_ledger(path, repository_id)
    return [dict(item) for item in reversed(ledger["dispatches"])]


def get_dispatch(
    repository_root: str | Path,
    state_root: str | Path,
    repository_id: str,
    dispatch_id: str,
) -> dict[str, Any]:
    if not isinstance(dispatch_id, str) or not _RELATED_ID_RE.fullmatch(dispatch_id):
        raise ArchitectDispatchError("Dispatch ID must be a 32-character hexadecimal ID.")
    path = _state_file(repository_root, state_root, repository_id)
    with state_artifact_lock(state_root):
        ledger = _read_ledger(path, repository_id)
    dispatch = next((item for item in ledger["dispatches"] if item["dispatch_id"] == dispatch_id), None)
    if dispatch is None:
        raise ArchitectDispatchError("Architect dispatch was not found for this repository.")
    return dict(dispatch)


def update_dispatch_status(
    repository_root: str | Path,
    state_root: str | Path,
    repository_id: str,
    dispatch_id: str,
    status: str,
    *,
    milestone_root: str,
    active_milestone_id: str | None,
) -> dict[str, Any]:
    if status not in {"APPROVED", "REJECTED"}:
        raise ArchitectDispatchError("Dispatch can only be approved or rejected.")
    path = _state_file(repository_root, state_root, repository_id)
    with state_artifact_lock(state_root):
        ledger = _read_ledger(path, repository_id)
        record = next((item for item in ledger["dispatches"] if item["dispatch_id"] == dispatch_id), None)
        if record is None:
            raise ArchitectDispatchError("Architect dispatch was not found for this repository.")
        if record["status"] == status:
            if status == "APPROVED" and record["type"] == "PROMPT":
                target_rel = record.get("prompt_path")
                if not isinstance(target_rel, str):
                    raise ArchitectDispatchError("Approved prompt dispatch is missing its materialized path.")
                if target_rel != record.get("canonical_destination"):
                    raise ArchitectDispatchError("Approved prompt path does not match its canonical destination.")
                root, milestone_dir = _root_paths(Path(repository_root), milestone_root)
                target = root / Path(*PurePosixPath(target_rel).parts)
                try:
                    resolved_target = target.resolve(strict=True)
                except (OSError, RuntimeError) as exc:
                    raise ArchitectDispatchError("Approved prompt artifact is missing or cannot be resolved.") from exc
                if not resolved_target.is_relative_to(milestone_dir):
                    raise ArchitectDispatchError("Materialized prompt is outside the configured milestone root.")
            return dict(record)
        if record["status"] != "PENDING":
            raise ArchitectDispatchError(f"Cannot change a {record['status']} dispatch to {status}.")
        created_prompt: Path | None = None
        created_prompt_identity: tuple[int, int] | None = None
        if status == "APPROVED" and record["type"] != "PROMPT" and active_milestone_id != record["milestone_id"]:
            raise ArchitectDispatchError("Active milestone changed; refresh and review this dispatch again.")
        if status == "APPROVED" and record["type"] == "PROMPT":
            created_prompt, created_prompt_identity = _materialize_prompt(repository_root, milestone_root, record)
            record["prompt_path"] = record["canonical_destination"]
        record["status"] = status
        record["updated_at"] = _now()
        record[f"{status.lower()}_at"] = record["updated_at"]
        if status == "APPROVED" and record["type"] != "PROMPT":
            record["approved_milestone_id"] = active_milestone_id
        ledger["dispatches"] = [
            record if item["dispatch_id"] == dispatch_id else item for item in ledger["dispatches"]
        ]
        try:
            _write_ledger(path, ledger)
        except ArchitectDispatchError:
            if created_prompt is not None and created_prompt_identity is not None:
                try:
                    _remove_created_prompt(created_prompt, created_prompt_identity)
                except ArchitectDispatchError as cleanup_error:
                    raise ArchitectDispatchError(
                        f"Dispatch state could not be saved and the newly created prompt could not be removed: {cleanup_error}"
                    ) from cleanup_error
            raise
        return dict(record)


def _materialize_prompt(
    repository_root: str | Path,
    milestone_root: str,
    record: dict[str, Any],
) -> tuple[Path, tuple[int, int]]:
    root = Path(repository_root).resolve(strict=True)
    try:
        _, milestone_dir = _root_paths(root, milestone_root)
    except MilestoneArtifactError as exc:
        raise ArchitectDispatchError(str(exc)) from exc
    target = milestone_dir / f"{record['milestone_id']}_{record['slug']}_prompt.md"
    destination = target.relative_to(root).as_posix()
    if record.get("canonical_destination") != destination:
        raise ArchitectDispatchError("Prompt destination no longer matches its canonical milestone root.")
    inventory = discover_milestones(root, milestone_root)
    collision_prefix = f"{record['milestone_id']}_"
    if any(
        candidate["milestone_id"] == record["milestone_id"]
        for candidate in inventory["candidates"]
    ) or any(collision_prefix in issue for issue in inventory["issues"]):
        raise ArchitectDispatchError("A prompt for this milestone ID already exists; no file was overwritten.")
    if target.exists() or target.is_symlink():
        raise ArchitectDispatchError("The canonical prompt destination already exists; no file was overwritten.")
    try:
        milestone_dir.mkdir(parents=True, exist_ok=True)
        resolved_dir = milestone_dir.resolve(strict=True)
        if not resolved_dir.is_relative_to(root):
            raise ArchitectDispatchError("Configured milestone root resolves outside the repository.")
        current = root
        relative_dir = resolved_dir.relative_to(root)
        for part in relative_dir.parts:
            current = current / part
            if current.is_symlink():
                raise ArchitectDispatchError("Configured milestone root cannot contain symlinked path components.")
        fd, temporary_name = tempfile.mkstemp(prefix=".architect-prompt-", suffix=".tmp", dir=resolved_dir)
    except ArchitectDispatchError:
        raise
    except OSError as exc:
        raise ArchitectDispatchError(f"Could not prepare canonical prompt destination: {exc}") from exc
    temporary_path = Path(temporary_name)
    target = resolved_dir / target.name
    created_identity: tuple[int, int] | None = None
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(record["body"])
            if not record["body"].endswith("\n"):
                stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary_stat = temporary_path.stat()
        created_identity = (temporary_stat.st_dev, temporary_stat.st_ino)
        os.link(temporary_path, target)
        target_stat = target.lstat()
        if (target_stat.st_dev, target_stat.st_ino) != created_identity:
            raise ArchitectDispatchError("Prompt destination changed during atomic creation.")
        temporary_path.unlink()
        directory_fd = os.open(resolved_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return target, created_identity
    except FileExistsError as exc:
        raise ArchitectDispatchError("The canonical prompt destination already exists; no file was overwritten.") from exc
    except ArchitectDispatchError:
        raise
    except OSError as exc:
        if created_identity is not None:
            try:
                _remove_created_prompt(target, created_identity)
            except ArchitectDispatchError as cleanup_error:
                raise ArchitectDispatchError(
                    f"Could not atomically create canonical milestone prompt ({exc}); cleanup failed: {cleanup_error}"
                ) from cleanup_error
        raise ArchitectDispatchError(f"Could not atomically create canonical milestone prompt: {exc}") from exc
    finally:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError as exc:
            raise ArchitectDispatchError(f"Could not remove temporary prompt file: {exc}") from exc


def _remove_created_prompt(path: Path, identity: tuple[int, int]) -> None:
    try:
        current = path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise ArchitectDispatchError(f"Could not inspect newly created prompt during cleanup: {exc}") from exc
    if (current.st_dev, current.st_ino) != identity:
        raise ArchitectDispatchError("Prompt destination changed before cleanup; it was not removed.")
    try:
        path.unlink()
    except OSError as exc:
        raise ArchitectDispatchError(f"Could not remove newly created prompt: {exc}") from exc
