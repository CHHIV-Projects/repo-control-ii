from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import secrets
import stat
import time
import uuid
from typing import Any, Callable

from .procedure_governance import (
    ProcedureBundle,
    ReconciliationPlan,
    _render_artifacts,
    _render_lock,
    _sha256,
    _status_for,
    load_bundle,
    preview_reconciliation,
)
from .scanner.core import DEFAULT_STATE_ROOT, state_artifact_lock
from .web.repository_registry import RepositoryEntry

SCHEMA_VERSION = 1
FIXED_TARGETS = (
    ".github/copilot-instructions.md",
    ".repo-control/workflow_core.md",
    ".agents/skills/repo-control-implementation/SKILL.md",
    ".agents/skills/repo-control-blocker-bug-escalation/SKILL.md",
    ".agents/skills/repo-control-validation-closeout/SKILL.md",
)
MAX_TARGET_BYTES = 1024 * 1024
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_JOURNAL_BYTES = 256 * 1024
MAX_HISTORY = 10
CONFIRMATION_TTL_SECONDS = 300
_ACTIVE_STATES = {"PREPARING", "PREPARED", "APPLYING", "RECOVERY_REQUIRED"}
_TERMINAL_STATES = {"VERIFIED", "RECOVERED"}


class ProcedureTransactionError(RuntimeError):
    pass


def _safe_permissions(mode: int, gid: int) -> bool:
    return not (mode & 0o002) and (not mode & 0o020 or gid == os.getegid())


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _digest_json(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _stable_parent(state_root: str | Path) -> Path:
    configured = Path(state_root).expanduser().resolve(strict=False)
    parent = configured.parent
    try:
        metadata = parent.stat()
    except OSError as exc:
        raise ProcedureTransactionError(f"Transaction storage parent is unavailable: {exc}") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o022
    ):
        raise ProcedureTransactionError(
            "Transaction storage parent must be an owned, non-symlink directory not writable by group or others."
        )
    return parent


def _checked_directory(path: Path, *, create: bool) -> Path:
    created = False
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        if not create:
            raise
        try:
            path.mkdir(mode=0o700)
            created = True
        except FileExistsError:
            pass
        if created:
            os.chmod(path, 0o700, follow_symlinks=False)
            try:
                _fsync_directory(path.parent)
            except OSError as exc:
                raise ProcedureTransactionError(
                    f"Cannot durably create transaction directory {path.name}: {exc}"
                ) from exc
        metadata = path.lstat()
    except OSError as exc:
        raise ProcedureTransactionError(f"Cannot inspect transaction directory: {exc}") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise ProcedureTransactionError(f"Transaction directory has unsafe type, owner, or permissions: {path.name}")
    return path


def _repository_store(state_root: str | Path, repository_id: str, *, create: bool) -> Path:
    if not repository_id or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for character in repository_id):
        raise ProcedureTransactionError("Repository identity is not safe for transaction storage.")
    parent = _stable_parent(state_root)
    root = parent / f".{Path(state_root).expanduser().name}-procedure-transactions"
    if create:
        _checked_directory(root, create=True)
    elif not root.exists() and not root.is_symlink():
        return root / repository_id
    else:
        _checked_directory(root, create=False)
    repository_store = root / repository_id
    if create:
        _checked_directory(repository_store, create=True)
    elif not repository_store.exists() and not repository_store.is_symlink():
        return repository_store
    else:
        _checked_directory(repository_store, create=False)
    for child in repository_store.iterdir():
        if child.name not in {"transactions", "confirmations"}:
            raise ProcedureTransactionError("Transaction storage contains an unknown repository-state entry.")
    return repository_store


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short write while persisting procedure transaction data")
        view = view[written:]


def _atomic_write(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    if len(data) > MAX_JOURNAL_BYTES:
        raise ProcedureTransactionError("Procedure transaction journal exceeds its size limit.")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        mode,
    )
    try:
        _write_all(descriptor, data)
        os.fsync(descriptor)
        os.fchmod(descriptor, mode)
    except Exception:
        os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise
    os.close(descriptor)
    try:
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise ProcedureTransactionError(f"Cannot persist procedure transaction journal: {exc}") from exc


def _write_image(path: Path, data: bytes) -> None:
    if len(data) > MAX_TARGET_BYTES:
        raise ProcedureTransactionError(f"Generated target exceeds the {MAX_TARGET_BYTES}-byte transaction limit.")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        _write_all(descriptor, data)
        os.fsync(descriptor)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
            raise ProcedureTransactionError("Transaction image has unsafe type or ownership.")
    finally:
        os.close(descriptor)


def _read_regular(path: Path, limit: int, *, required: bool = True) -> bytes | None:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        if required:
            raise ProcedureTransactionError(f"Required transaction file is missing: {path.name}")
        return None
    except OSError as exc:
        raise ProcedureTransactionError(f"Cannot safely open transaction file {path.name}: {exc}") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o077
            or metadata.st_size > limit
        ):
            raise ProcedureTransactionError(f"Transaction file has unsafe type, owner, permissions, or size: {path.name}")
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > limit or len(data) != metadata.st_size:
            raise ProcedureTransactionError(f"Transaction file changed while being read: {path.name}")
        return data
    finally:
        os.close(descriptor)


def _read_journal(operation_directory: Path, repository: RepositoryEntry) -> dict[str, Any]:
    raw = _read_regular(operation_directory / "journal.json", MAX_JOURNAL_BYTES)
    try:
        journal = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProcedureTransactionError(f"Procedure transaction journal is malformed: {exc}") from exc
    if (
        not isinstance(journal, dict)
        or journal.get("schema_version") != SCHEMA_VERSION
        or journal.get("operation_id") != operation_directory.name
        or journal.get("repository_id") != repository.repository_id
        or journal.get("repository_key") != repository.key
        or journal.get("repository_path") != str(repository.path)
        or journal.get("state") not in _ACTIVE_STATES | _TERMINAL_STATES
        or not isinstance(journal.get("targets"), list)
        or not isinstance(journal.get("directories"), list)
        or not isinstance(journal.get("release"), str)
        or not isinstance(journal.get("manifest_sha256"), str)
        or not isinstance(journal.get("source_hashes"), dict)
        or not isinstance(journal.get("created_at"), str)
        or not isinstance(journal.get("updated_at"), str)
        or not isinstance(journal.get("root_identity"), dict)
        or not isinstance(journal.get("plan_sha256"), str)
    ):
        raise ProcedureTransactionError("Procedure transaction journal identity or schema is invalid.")
    if (
        set(journal["root_identity"]) != {"device", "inode", "uid", "gid", "mode"}
        or any(type(value) is not int or value < 0 for value in journal["root_identity"].values())
        or journal["root_identity"]["mode"] > 0o777
        or not _safe_permissions(journal["root_identity"]["mode"], journal["root_identity"]["gid"])
        or len(journal["plan_sha256"]) != 64
        or any(character not in "0123456789abcdef" for character in journal["plan_sha256"])
    ):
        raise ProcedureTransactionError("Procedure transaction journal root or plan identity is invalid.")
    target_paths = [item.get("path") for item in journal["targets"] if isinstance(item, dict)]
    if target_paths != [*FIXED_TARGETS, ".repo-control/shared-procedure.lock.toml"]:
        raise ProcedureTransactionError("Procedure transaction journal contains an unauthorized target set.")
    expected_directories = set()
    for relative in [*FIXED_TARGETS, ".repo-control/shared-procedure.lock.toml"]:
        parts: list[str] = []
        for part in PurePosixPath(relative).parts[:-1]:
            parts.append(part)
            expected_directories.add("/".join(parts))
    expected_directory_order = sorted(expected_directories, key=lambda value: (value.count("/"), value))
    if [item.get("path") for item in journal["directories"] if isinstance(item, dict)] != expected_directory_order:
        raise ProcedureTransactionError("Procedure transaction journal contains an unauthorized directory set.")
    if any(
        not isinstance(item, dict)
        or item.get("index") != index
        or item.get("is_lock") != (index == len(FIXED_TARGETS))
        or type(item.get("before_present")) is not bool
        or item.get("after_present") is not True
        or not isinstance(item.get("after_sha256"), str)
        or len(item["after_sha256"]) != 64
        or any(character not in "0123456789abcdef" for character in item["after_sha256"])
        or (item["before_present"] and item.get("before_file") != f"before-{index}.bin")
        or (not item["before_present"] and item.get("before_file") is not None)
        or item.get("after_file") != f"after-{index}.bin"
        or (item["before_present"] and type(item.get("before_mode")) is not int)
        or (item["before_present"] and type(item.get("before_gid")) is not int)
        or (
            item["before_present"]
            and (
                not isinstance(item.get("before_sha256"), str)
                or len(item["before_sha256"]) != 64
                or any(character not in "0123456789abcdef" for character in item["before_sha256"])
                or item.get("before_sha256_image") != item["before_sha256"]
                or item["before_mode"] < 0
                or item["before_mode"] > 0o777
                or not _safe_permissions(item["before_mode"], item["before_gid"])
                or item["before_gid"] < 0
            )
        )
        or (
            not item["before_present"]
            and any(item.get(field) is not None for field in ("before_sha256", "before_mode", "before_gid", "before_sha256_image"))
        )
        or type(item.get("after_mode")) is not int
        or type(item.get("after_gid")) is not int
        or item["after_mode"] < 0
        or item["after_mode"] > 0o777
        or not _safe_permissions(item["after_mode"], item["after_gid"])
        or item["after_gid"] < 0
        for index, item in enumerate(journal["targets"])
    ):
        raise ProcedureTransactionError("Procedure transaction journal contains invalid target metadata.")
    if any(
        not isinstance(item, dict)
        or type(item.get("present")) is not bool
        or type(item.get("created")) is not bool
        or (
            (item["present"] or item["created"])
            and any(type(item.get(field)) is not int or item[field] < 0 for field in ("device", "inode", "uid", "gid", "mode"))
        )
        or (
            not item["present"]
            and not item["created"]
            and any(item.get(field) is not None for field in ("device", "inode", "uid", "gid", "mode"))
        )
        or ((item["present"] or item["created"]) and item["mode"] > 0o777)
        or (
            (item["present"] or item["created"])
            and not _safe_permissions(item["mode"], item["gid"])
        )
        for item in journal["directories"]
    ):
        raise ProcedureTransactionError("Procedure transaction journal contains invalid directory metadata.")
    allowed = {"journal.json"}
    for item in journal["targets"]:
        for name in (item.get("before_file"), item.get("after_file")):
            if name is not None:
                allowed.add(name)
    for child in operation_directory.iterdir():
        metadata = child.lstat()
        journal_temp = child.name.startswith(".journal.json.") and child.name.endswith(".tmp")
        size_limit = MAX_JOURNAL_BYTES if child.name == "journal.json" or journal_temp else MAX_TARGET_BYTES
        if (
            (child.name not in allowed and not journal_temp)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o077
            or metadata.st_size > size_limit
        ):
            raise ProcedureTransactionError("Transaction directory contains unknown or unsafe data.")
    return journal


def _write_journal(operation_directory: Path, journal: dict[str, Any]) -> None:
    journal["updated_at"] = _now()
    encoded = json.dumps(journal, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    _atomic_write(operation_directory / "journal.json", encoded)


def _target_paths(bundle: ProcedureBundle) -> tuple[str, ...]:
    _validate_bundle_targets(bundle)
    return (*FIXED_TARGETS, bundle.lock_path)


def _root_identity(repository: RepositoryEntry) -> dict[str, int]:
    try:
        metadata = repository.path.lstat()
    except OSError as exc:
        raise ProcedureTransactionError(f"Managed repository path is unavailable: {exc}") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or repository.path.resolve(strict=True) != repository.path
        or not _safe_permissions(stat.S_IMODE(metadata.st_mode), metadata.st_gid)
    ):
        raise ProcedureTransactionError("Managed repository root is symlinked, non-directory, or no longer canonical.")
    return {
        "device": metadata.st_dev,
        "inode": metadata.st_ino,
        "uid": metadata.st_uid,
        "gid": metadata.st_gid,
        "mode": stat.S_IMODE(metadata.st_mode),
    }


def _assert_root_identity(repository: RepositoryEntry, expected: dict[str, int]) -> None:
    if _root_identity(repository) != expected:
        raise ProcedureTransactionError("Managed repository root changed after transaction review.")


def _open_parent(
    repository: RepositoryEntry,
    relative: str,
    *,
    create: bool = False,
    directory_states: list[dict[str, Any]] | None = None,
    on_created: Callable[[], None] | None = None,
) -> tuple[int | None, str]:
    parts = PurePosixPath(relative).parts
    if not parts or PurePosixPath(relative).is_absolute() or any(part in {"", ".", ".."} for part in parts):
        raise ProcedureTransactionError("Transaction target path is not repository-relative.")
    _root_identity(repository)
    descriptor = os.open(repository.path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    current_relative: list[str] = []
    try:
        for part in parts[:-1]:
            current_relative.append(part)
            child_relative = "/".join(current_relative)
            try:
                child = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=descriptor,
                )
            except FileNotFoundError:
                if not create:
                    os.close(descriptor)
                    return None, parts[-1]
                os.mkdir(part, mode=0o755, dir_fd=descriptor)
                os.fsync(descriptor)
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                if directory_states is not None:
                    existing = next((item for item in directory_states if item["path"] == child_relative), None)
                    if existing is None or existing["present"]:
                        raise ProcedureTransactionError("Transaction attempted to create an unplanned parent directory.")
                    existing["created"] = True
                    metadata = os.fstat(child)
                    existing["device"] = metadata.st_dev
                    existing["inode"] = metadata.st_ino
                    existing["uid"] = metadata.st_uid
                    existing["gid"] = metadata.st_gid
                    existing["mode"] = stat.S_IMODE(metadata.st_mode)
                    if on_created is not None:
                        on_created()
            except OSError as exc:
                raise ProcedureTransactionError(f"Unsafe target parent {child_relative}: {exc}") from exc
            child_metadata = os.fstat(child)
            if child_metadata.st_uid != os.geteuid() or not _safe_permissions(
                stat.S_IMODE(child_metadata.st_mode),
                child_metadata.st_gid,
            ):
                os.close(child)
                raise ProcedureTransactionError(f"Unsafe target parent ownership or permissions: {child_relative}")
            os.close(descriptor)
            descriptor = child
        return descriptor, parts[-1]
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


def _target_state(repository: RepositoryEntry, relative: str) -> dict[str, Any]:
    descriptor, name = _open_parent(repository, relative)
    if descriptor is None:
        return {"present": False, "sha256": None, "mode": None, "gid": None}
    try:
        try:
            file_descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=descriptor)
        except FileNotFoundError:
            return {"present": False, "sha256": None, "mode": None, "gid": None}
        except OSError as exc:
            raise ProcedureTransactionError(f"Cannot safely inspect target {relative}: {exc}") from exc
        try:
            metadata = os.fstat(file_descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise ProcedureTransactionError(f"Transaction target is not a regular file: {relative}")
            if metadata.st_uid != os.geteuid():
                raise ProcedureTransactionError(f"Transaction target is not owned by the service user: {relative}")
            if (
                not _safe_permissions(stat.S_IMODE(metadata.st_mode), metadata.st_gid)
                or metadata.st_mode & 0o7000
            ):
                raise ProcedureTransactionError(f"Transaction target has unsafe permissions: {relative}")
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(file_descriptor, 64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_TARGET_BYTES:
                    raise ProcedureTransactionError(f"Transaction target exceeds its size limit: {relative}")
                chunks.append(chunk)
            data = b"".join(chunks)
            if len(data) != metadata.st_size:
                raise ProcedureTransactionError(f"Transaction target changed while being read: {relative}")
            return {
                "present": True,
                "sha256": _sha256(data),
                "mode": stat.S_IMODE(metadata.st_mode),
                "gid": metadata.st_gid,
            }
        finally:
            os.close(file_descriptor)
    finally:
        os.close(descriptor)


def _assert_parent_identity(repository: RepositoryEntry, relative: str, descriptor: int) -> None:
    current_descriptor, _ = _open_parent(repository, relative)
    if current_descriptor is None:
        raise ProcedureTransactionError(f"Target parent disappeared during replacement: {relative}")
    try:
        original = os.fstat(descriptor)
        current = os.fstat(current_descriptor)
        if (original.st_dev, original.st_ino) != (current.st_dev, current.st_ino):
            raise ProcedureTransactionError(f"Target parent changed during replacement: {relative}")
    finally:
        os.close(current_descriptor)


def _directory_states(repository: RepositoryEntry, targets: tuple[str, ...]) -> list[dict[str, Any]]:
    directories: set[str] = set()
    for relative in targets:
        current: list[str] = []
        for part in PurePosixPath(relative).parts[:-1]:
            current.append(part)
            directories.add("/".join(current))
    result = []
    for relative in sorted(directories, key=lambda value: (value.count("/"), value)):
        descriptor, _ = _open_parent(repository, f"{relative}/.repoctl-probe")
        if descriptor is None:
            result.append({"path": relative, "present": False, "device": None, "inode": None, "created": False})
            continue
        try:
            metadata = os.fstat(descriptor)
            if (
                metadata.st_uid != os.geteuid()
                or not stat.S_ISDIR(metadata.st_mode)
                or not _safe_permissions(stat.S_IMODE(metadata.st_mode), metadata.st_gid)
            ):
                raise ProcedureTransactionError(f"Unsafe target parent directory: {relative}")
            result.append(
                {
                    "path": relative,
                    "present": True,
                    "device": metadata.st_dev,
                    "inode": metadata.st_ino,
                    "uid": metadata.st_uid,
                    "gid": metadata.st_gid,
                    "mode": stat.S_IMODE(metadata.st_mode),
                    "created": False,
                }
            )
        finally:
            os.close(descriptor)
    return result


def _assert_directory_states(
    repository: RepositoryEntry,
    states: list[dict[str, Any]],
    *,
    permit_created: bool,
) -> None:
    for item in states:
        descriptor, _ = _open_parent(repository, f"{item['path']}/.repoctl-probe")
        if descriptor is None:
            if item["present"] and not (permit_created and item.get("created")):
                raise ProcedureTransactionError(f"Target parent disappeared after review: {item['path']}")
            if not item["present"] and item.get("created"):
                raise ProcedureTransactionError(f"Transaction-created parent disappeared: {item['path']}")
            continue
        try:
            metadata = os.fstat(descriptor)
            if not _safe_permissions(stat.S_IMODE(metadata.st_mode), metadata.st_gid):
                raise ProcedureTransactionError(f"Target parent permissions are unsafe: {item['path']}")
            if item["present"] and (metadata.st_dev, metadata.st_ino) != (item["device"], item["inode"]):
                raise ProcedureTransactionError(f"Target parent changed after review: {item['path']}")
            if item["present"] and (
                metadata.st_uid,
                metadata.st_gid,
                stat.S_IMODE(metadata.st_mode),
            ) != (item["uid"], item["gid"], item["mode"]):
                raise ProcedureTransactionError(f"Target parent permissions changed after review: {item['path']}")
            if not item["present"] and item.get("created") and (
                metadata.st_dev,
                metadata.st_ino,
            ) != (item["device"], item["inode"]):
                raise ProcedureTransactionError(f"Transaction-created parent changed: {item['path']}")
            if not item["present"] and item.get("created") and (
                metadata.st_uid,
                metadata.st_gid,
                stat.S_IMODE(metadata.st_mode),
            ) != (item["uid"], item["gid"], item["mode"]):
                raise ProcedureTransactionError(f"Transaction-created parent permissions changed: {item['path']}")
            if not item["present"] and not (permit_created and item.get("created")):
                raise ProcedureTransactionError(f"Target parent appeared after review: {item['path']}")
        finally:
            os.close(descriptor)


def _plan_digest(entry: RepositoryEntry, bundle: ProcedureBundle, plan: ReconciliationPlan) -> str:
    return _digest_json(
        {
            "repository_key": entry.key,
            "repository_id": entry.repository_id,
            "repository_path": str(entry.path),
            "release": bundle.release,
            "manifest_sha256": bundle.manifest_sha256,
            "source_hashes": bundle.source_hashes,
            "root_identity": _root_identity(entry),
            "target_states": [(path, _target_state(entry, path)) for path in _target_paths(bundle)],
            "directory_states": _directory_states(entry, _target_paths(bundle)),
            "snapshots": list(plan.snapshots),
            "files": [(path, _sha256(data)) for path, data in plan.files],
            "lock_sha256": _sha256(plan.lock_bytes),
            "changes": list(plan.changes),
            "blockers": list(plan.blockers),
        }
    )


def procedure_plan_digest(entry: RepositoryEntry, bundle: ProcedureBundle, plan: ReconciliationPlan) -> str:
    _validate_bundle_targets(bundle)
    return _plan_digest(entry, bundle, plan)


def _validate_bundle_targets(bundle: ProcedureBundle) -> None:
    if tuple(sorted(artifact.target for artifact in bundle.artifacts)) != tuple(sorted(FIXED_TARGETS)):
        raise ProcedureTransactionError("Canonical manifest differs from the fixed five generated procedure targets.")
    if bundle.lock_path != ".repo-control/shared-procedure.lock.toml":
        raise ProcedureTransactionError("Canonical manifest lock path differs from the approved fixed target.")


def _token_directory(store: Path, *, create: bool) -> Path:
    directory = store / "confirmations"
    if create:
        _checked_directory(directory, create=True)
    elif directory.exists() or directory.is_symlink():
        _checked_directory(directory, create=False)
    return directory


def _issue_token(
    store: Path,
    *,
    purpose: str,
    binding: dict[str, Any],
) -> dict[str, Any]:
    directory = _token_directory(store, create=True)
    token_id = uuid.uuid4().hex
    token = f"{token_id}.{secrets.token_urlsafe(32)}"
    expires_at = int(time.time()) + CONFIRMATION_TTL_SECONDS
    record = {
        "schema_version": SCHEMA_VERSION,
        "token_id": token_id,
        "token_sha256": _sha256(token.encode("ascii")),
        "purpose": purpose,
        "expires_at": expires_at,
        "binding": binding,
    }
    _atomic_write(
        directory / f"{token_id}.json",
        json.dumps(record, sort_keys=True, separators=(",", ":")).encode("ascii"),
    )
    _fsync_directory(directory)
    return {"token": token, "expires_at": datetime.fromtimestamp(expires_at, UTC).isoformat()}


def _token_id(token: str) -> str:
    if not isinstance(token, str) or len(token) > 162:
        raise ProcedureTransactionError("Confirmation token is malformed or expired.")
    try:
        token_id, secret = token.split(".", 1)
    except (AttributeError, ValueError) as exc:
        raise ProcedureTransactionError("Confirmation token is malformed or expired.") from exc
    if (
        len(token_id) != 32
        or any(character not in "0123456789abcdef" for character in token_id)
        or not 32 <= len(secret) <= 128
        or any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for character in secret)
    ):
        raise ProcedureTransactionError("Confirmation token is malformed or expired.")
    return token_id


def _prune_expired_tokens(store: Path) -> None:
    directory = _token_directory(store, create=True)
    tokens = list(directory.iterdir())
    for path in tokens:
        if path.is_symlink() or not path.is_file() or not path.name.endswith(".json"):
            raise ProcedureTransactionError("Confirmation storage contains an unknown or unsafe entry.")
        raw = _read_regular(path, MAX_JOURNAL_BYTES)
        try:
            record = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProcedureTransactionError("Confirmation storage contains a malformed record.") from exc
        if (
            not isinstance(record, dict)
            or type(record.get("expires_at")) is not int
            or not isinstance(record.get("token_id"), str)
            or path.name != f"{record['token_id']}.json"
        ):
            raise ProcedureTransactionError("Confirmation storage contains an invalid record.")
        if record["expires_at"] < int(time.time()):
            path.unlink()
    remaining = list(directory.iterdir())
    if len(remaining) >= 32:
        raise ProcedureTransactionError("Too many outstanding confirmations; wait for expiry and retry.")
    if len(remaining) != len(tokens):
        _fsync_directory(directory)


def _consume_token(
    store: Path,
    token: str,
    *,
    purpose: str,
    expected_binding: dict[str, Any],
) -> None:
    token_id = _token_id(token)
    directory = _token_directory(store, create=False)
    record_path = directory / f"{token_id}.json"
    raw = _read_regular(record_path, MAX_JOURNAL_BYTES, required=False)
    if raw is None:
        raise ProcedureTransactionError("Confirmation token is unknown, expired, or already used.")
    try:
        record = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProcedureTransactionError("Confirmation token record is malformed; no action was taken.") from exc
    if (
        not isinstance(record, dict)
        or record.get("schema_version") != SCHEMA_VERSION
        or record.get("token_id") != token_id
        or record.get("purpose") != purpose
        or type(record.get("expires_at")) is not int
        or record.get("expires_at", 0) < int(time.time())
        or record.get("binding") != expected_binding
        or not isinstance(record.get("token_sha256"), str)
        or not secrets.compare_digest(record["token_sha256"], _sha256(token.encode("ascii")))
    ):
        raise ProcedureTransactionError("Confirmation token is expired or no longer matches reviewed state.")
    record_path.unlink()
    _fsync_directory(directory)


def _invalidate_apply_tokens(store: Path, entry: RepositoryEntry) -> None:
    directory = _token_directory(store, create=False)
    if not directory.exists():
        return
    removed = False
    for token_path in directory.iterdir():
        raw = _read_regular(token_path, MAX_JOURNAL_BYTES)
        try:
            record = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProcedureTransactionError("Confirmation storage contains a malformed record.") from exc
        binding = record.get("binding") if isinstance(record, dict) else None
        if (
            not isinstance(record, dict)
            or not isinstance(binding, dict)
            or token_path.name != f"{record.get('token_id')}.json"
        ):
            raise ProcedureTransactionError("Confirmation storage contains an invalid record.")
        if (
            record.get("purpose") == "apply"
            and binding.get("repository_key") == entry.key
            and binding.get("repository_id") == entry.repository_id
            and binding.get("repository_path") == str(entry.path)
        ):
            token_path.unlink()
            removed = True
    if removed:
        _fsync_directory(directory)


def _transaction_directories(store: Path, *, create: bool) -> Path:
    directory = store / "transactions"
    if create:
        _checked_directory(directory, create=True)
    elif directory.exists() or directory.is_symlink():
        _checked_directory(directory, create=False)
    return directory


def _active_operations(store: Path, entry: RepositoryEntry) -> tuple[list[tuple[Path, dict[str, Any]]], list[str]]:
    directory = _transaction_directories(store, create=False)
    if not directory.exists():
        return [], []
    active: list[tuple[Path, dict[str, Any]]] = []
    errors: list[str] = []
    for child in sorted(directory.iterdir(), key=lambda path: path.name):
        try:
            if (
                not child.name
                or len(child.name) != 32
                or any(character not in "0123456789abcdef" for character in child.name)
            ):
                raise ProcedureTransactionError("Transaction store contains an unknown operation entry.")
            _checked_directory(child, create=False)
            journal = _read_journal(child, entry)
            if journal["state"] in _ACTIVE_STATES:
                active.append((child, journal))
        except (OSError, ProcedureTransactionError) as exc:
            errors.append(str(exc))
    return active, errors


def _image_bytes(operation_directory: Path, record: dict[str, Any], name: str) -> bytes | None:
    if not record.get(name + "_present"):
        return None
    image_name = record.get(name + "_file")
    if not isinstance(image_name, str) or image_name != f"{name}-{record['index']}.bin":
        raise ProcedureTransactionError("Transaction journal contains an invalid image filename.")
    data = _read_regular(operation_directory / image_name, MAX_TARGET_BYTES)
    if _sha256(data) != record.get(name + "_sha256"):
        raise ProcedureTransactionError(f"Transaction image integrity check failed: {image_name}")
    return data


def _verify_images(operation_directory: Path, journal: dict[str, Any]) -> None:
    total = 0
    for item in journal["targets"]:
        before = _image_bytes(operation_directory, item, "before")
        after = _image_bytes(operation_directory, item, "after")
        total += len(before or b"") + len(after or b"")
    if total > MAX_IMAGE_BYTES:
        raise ProcedureTransactionError("Transaction images exceed their aggregate size limit.")


def _target_before_state(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "present": item["before_present"],
        "sha256": item["before_sha256"] if item["before_present"] else None,
        "mode": item["before_mode"] if item["before_present"] else None,
        "gid": item["before_gid"] if item["before_present"] else None,
    }


def _target_after_state(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "present": True,
        "sha256": item["after_sha256"],
        "mode": item["after_mode"],
        "gid": item["after_gid"],
    }


def _state_matches(current: dict[str, Any], candidate: dict[str, Any]) -> bool:
    return current == candidate


def _temp_name(operation_id: str, index: int) -> str:
    return f".repoctl-{operation_id}-{index}.tmp"


def _write_target(
    repository: RepositoryEntry,
    relative: str,
    content: bytes,
    expected_current: dict[str, Any],
    mode: int,
    gid: int,
    temporary_name: str,
    *,
    directory_states: list[dict[str, Any]],
    on_directory_created: Callable[[], None] | None = None,
) -> None:
    _assert_directory_states(repository, directory_states, permit_created=True)
    descriptor, name = _open_parent(
        repository,
        relative,
        create=True,
        directory_states=directory_states,
        on_created=on_directory_created,
    )
    if descriptor is None:
        raise ProcedureTransactionError(f"Cannot prepare target parent for {relative}.")
    try:
        _assert_parent_identity(repository, relative, descriptor)
        _assert_directory_states(repository, directory_states, permit_created=True)
        current = _target_state(repository, relative)
        if not _state_matches(current, expected_current):
            raise ProcedureTransactionError(f"Target changed immediately before replacement: {relative}")
        temp_fd = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=descriptor,
        )
        try:
            _write_all(temp_fd, content)
            os.fsync(temp_fd)
            if os.fstat(temp_fd).st_gid != gid:
                os.fchown(temp_fd, -1, gid)
            os.fchmod(temp_fd, mode)
        except Exception:
            os.close(temp_fd)
            raise
        os.close(temp_fd)
        # Recheck the target after staging the complete durable replacement.
        _assert_parent_identity(repository, relative, descriptor)
        _assert_directory_states(repository, directory_states, permit_created=True)
        current = _target_state(repository, relative)
        if not _state_matches(current, expected_current):
            os.unlink(temporary_name, dir_fd=descriptor)
            os.fsync(descriptor)
            raise ProcedureTransactionError(f"Target changed during replacement preparation: {relative}")
        os.replace(temporary_name, name, src_dir_fd=descriptor, dst_dir_fd=descriptor)
        os.fsync(descriptor)
        _assert_parent_identity(repository, relative, descriptor)
    finally:
        os.close(descriptor)


def _unlink_target(repository: RepositoryEntry, relative: str, expected_current: dict[str, Any]) -> None:
    descriptor, name = _open_parent(repository, relative)
    if descriptor is None:
        return
    try:
        current = _target_state(repository, relative)
        if not _state_matches(current, expected_current):
            raise ProcedureTransactionError(f"Target changed before removal: {relative}")
        os.unlink(name, dir_fd=descriptor)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _remove_transaction_temp(
    repository: RepositoryEntry,
    operation_directory: Path,
    journal: dict[str, Any],
    item: dict[str, Any],
) -> None:
    descriptor, _ = _open_parent(repository, item["path"])
    if descriptor is None:
        return
    temp_name = _temp_name(journal["operation_id"], item["index"])
    try:
        try:
            temp_fd = os.open(temp_name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=descriptor)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise ProcedureTransactionError(f"Unsafe transaction temporary file for {item['path']}: {exc}") from exc
        try:
            metadata = os.fstat(temp_fd)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or metadata.st_size > MAX_TARGET_BYTES
            ):
                raise ProcedureTransactionError(f"Transaction temporary file has unsafe type: {item['path']}")
            chunks = []
            while True:
                chunk = os.read(temp_fd, 64 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            temporary_bytes = b"".join(chunks)
            if len(temporary_bytes) != metadata.st_size or len(temporary_bytes) > MAX_TARGET_BYTES:
                raise ProcedureTransactionError(f"Transaction temporary file changed while being read: {item['path']}")
        finally:
            os.close(temp_fd)
        known_images = [_image_bytes(operation_directory, item, "after")]
        before_bytes = _image_bytes(operation_directory, item, "before")
        if before_bytes is not None:
            known_images.append(before_bytes)
        if not any(
            len(temporary_bytes) <= len(image) and image.startswith(temporary_bytes)
            for image in known_images
        ):
            raise ProcedureTransactionError(f"Transaction temporary file is not a known image prefix: {item['path']}")
        os.unlink(temp_name, dir_fd=descriptor)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _remove_created_directories(repository: RepositoryEntry, states: list[dict[str, Any]]) -> None:
    for item in sorted(states, key=lambda value: value["path"].count("/"), reverse=True):
        if item["present"] or not item.get("created"):
            continue
        parts = PurePosixPath(item["path"]).parts
        parent_relative = "/".join(parts[:-1])
        if not parent_relative:
            parent_fd = os.open(repository.path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        else:
            parent_fd, _ = _open_parent(repository, f"{parent_relative}/.repoctl-probe")
            if parent_fd is None:
                continue
        try:
            try:
                os.rmdir(parts[-1], dir_fd=parent_fd)
                os.fsync(parent_fd)
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise ProcedureTransactionError(
                    f"Cannot remove transaction-created empty directory {item['path']}: {exc}"
                ) from exc
        finally:
            os.close(parent_fd)


def _restore_original(
    repository: RepositoryEntry,
    operation_directory: Path,
    journal: dict[str, Any],
) -> None:
    if journal["state"] == "PREPARING":
        _assert_root_identity(repository, journal["root_identity"])
        _assert_directory_states(repository, journal["directories"], permit_created=False)
        for item in journal["targets"]:
            if not _state_matches(_target_state(repository, item["path"]), _target_before_state(item)):
                raise ProcedureTransactionError(
                    f"Preparation recovery is blocked because {item['path']} changed before Apply began."
                )
        return
    _verify_images(operation_directory, journal)
    for item in journal["targets"]:
        current = _target_state(repository, item["path"])
        before = _target_before_state(item)
        after = _target_after_state(item)
        if not _state_matches(current, before) and not _state_matches(current, after):
            raise ProcedureTransactionError(
                f"Recovery is blocked: {item['path']} matches neither its original nor intended image."
            )
    _assert_directory_states(repository, journal["directories"], permit_created=True)
    for item in journal["targets"]:
        _remove_transaction_temp(repository, operation_directory, journal, item)
    # Restore generated targets first and the provenance lock last.
    ordered = [item for item in journal["targets"] if not item["is_lock"]]
    ordered.extend(item for item in journal["targets"] if item["is_lock"])
    for item in ordered:
        before_state = _target_before_state(item)
        current = _target_state(repository, item["path"])
        if _state_matches(current, before_state):
            continue
        if not item["before_present"]:
            _unlink_target(repository, item["path"], current)
            continue
        original = _image_bytes(operation_directory, item, "before")
        _write_target(
            repository,
            item["path"],
            original,
            current,
            item["before_mode"],
            item["before_gid"],
            _temp_name(journal["operation_id"], item["index"]),
            directory_states=journal["directories"],
            on_directory_created=lambda: _write_journal(operation_directory, journal),
        )
    _remove_created_directories(repository, journal["directories"])
    for item in journal["targets"]:
        if not _state_matches(_target_state(repository, item["path"]), _target_before_state(item)):
            raise ProcedureTransactionError(f"Recovery verification failed for {item['path']}.")


def _prune_terminal(store: Path, entry: RepositoryEntry) -> None:
    transactions = _transaction_directories(store, create=True)
    entries = sorted(transactions.iterdir(), key=lambda item: item.name)
    if len(entries) < MAX_HISTORY:
        return
    terminal_entries = []
    for operation_directory in entries:
        _checked_directory(operation_directory, create=False)
        journal = _read_journal(operation_directory, entry)
        if journal["state"] in _TERMINAL_STATES:
            terminal_entries.append((journal.get("updated_at", ""), operation_directory, journal))
    for _, operation_directory, journal in sorted(terminal_entries):
        if len(list(transactions.iterdir())) < MAX_HISTORY:
            break
        allowed = {"journal.json"}
        for item in journal["targets"]:
            allowed.update(name for name in (item.get("before_file"), item.get("after_file")) if name)
        for child in operation_directory.iterdir():
            if child.name not in allowed or not child.is_file() or child.is_symlink():
                raise ProcedureTransactionError("Cannot safely prune unknown transaction history data.")
        _cleanup_images(operation_directory, journal)
        (operation_directory / "journal.json").unlink()
        operation_directory.rmdir()
        _fsync_directory(transactions)


def _new_journal(
    entry: RepositoryEntry,
    bundle: ProcedureBundle,
    plan: ReconciliationPlan,
    repository: RepositoryEntry,
    operation_id: str,
) -> tuple[dict[str, Any], dict[str, bytes]]:
    paths = _target_paths(bundle)
    generated = dict(_render_artifacts(bundle, entry))
    generated[bundle.lock_path] = _render_lock(bundle, entry, tuple(sorted(generated.items())))
    current_states = {relative: _target_state(repository, relative) for relative in paths}
    expected_snapshots = dict(plan.snapshots)
    for relative, expected_hash in expected_snapshots.items():
        if _target_state(repository, relative)["sha256"] != expected_hash:
            raise ProcedureTransactionError(f"Target changed after confirmation: {relative}")
    directories = _directory_states(repository, paths)
    target_records = []
    images: dict[str, bytes] = {}
    total = 0
    for index, relative in enumerate(paths):
        before_state = current_states[relative]
        after_bytes = generated[relative]
        if len(after_bytes) > MAX_TARGET_BYTES:
            raise ProcedureTransactionError(f"Generated target exceeds its size limit: {relative}")
        before_bytes = None
        if before_state["present"]:
            before_bytes = _read_target_bytes(repository, relative)
            if _sha256(before_bytes) != before_state["sha256"]:
                raise ProcedureTransactionError(f"Target changed while the transaction was being prepared: {relative}")
        total += len(before_bytes or b"") + len(after_bytes)
        if total > MAX_IMAGE_BYTES:
            raise ProcedureTransactionError("Procedure transaction images exceed the aggregate size limit.")
        record = {
            "index": index,
            "path": relative,
            "is_lock": relative == bundle.lock_path,
            "before_present": before_state["present"],
            "before_sha256": before_state["sha256"],
            "before_mode": before_state["mode"],
            "before_gid": before_state["gid"],
            "after_present": True,
            "after_sha256": _sha256(after_bytes),
            "after_mode": before_state["mode"] if before_state["present"] else 0o644,
            "after_gid": before_state["gid"] if before_state["present"] else _nearest_parent_gid(repository, relative),
            "before_file": f"before-{index}.bin" if before_state["present"] else None,
            "before_sha256_image": _sha256(before_bytes) if before_bytes is not None else None,
            "after_file": f"after-{index}.bin",
        }
        target_records.append(record)
        if before_bytes is not None:
            images[record["before_file"]] = before_bytes
        images[record["after_file"]] = after_bytes
    journal = {
        "schema_version": SCHEMA_VERSION,
        "operation_id": operation_id,
        "repository_key": entry.key,
        "repository_id": entry.repository_id,
        "repository_path": str(entry.path),
        "release": bundle.release,
        "manifest_sha256": bundle.manifest_sha256,
        "source_hashes": bundle.source_hashes,
        "plan_sha256": _plan_digest(entry, bundle, plan),
        "root_identity": _root_identity(repository),
        "state": "PREPARING",
        "created_at": _now(),
        "updated_at": _now(),
        "targets": target_records,
        "directories": directories,
    }
    return journal, images


def _read_target_bytes(repository: RepositoryEntry, relative: str) -> bytes:
    descriptor, name = _open_parent(repository, relative)
    if descriptor is None:
        raise ProcedureTransactionError(f"Target disappeared while being read: {relative}")
    try:
        file_descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=descriptor)
        try:
            metadata = os.fstat(file_descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
                raise ProcedureTransactionError(f"Target is unsafe while being read: {relative}")
            if (
                not _safe_permissions(stat.S_IMODE(metadata.st_mode), metadata.st_gid)
                or metadata.st_mode & 0o7000
            ):
                raise ProcedureTransactionError(f"Target permissions are unsafe while being read: {relative}")
            data = bytearray()
            while True:
                chunk = os.read(file_descriptor, 64 * 1024)
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > MAX_TARGET_BYTES:
                    raise ProcedureTransactionError(f"Target exceeds its size limit: {relative}")
            if len(data) != metadata.st_size:
                raise ProcedureTransactionError(f"Target changed while being read: {relative}")
            return bytes(data)
        finally:
            os.close(file_descriptor)
    finally:
        os.close(descriptor)


def _nearest_parent_gid(repository: RepositoryEntry, relative: str) -> int:
    descriptor, _ = _open_parent(repository, relative)
    if descriptor is not None:
        try:
            return os.fstat(descriptor).st_gid
        finally:
            os.close(descriptor)
    parent = repository.path
    for part in PurePosixPath(relative).parts[:-1]:
        candidate = parent / part
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            break
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
        ):
            raise ProcedureTransactionError(f"Unsafe target parent directory: {candidate.name}")
        parent = candidate
    return parent.stat().st_gid


def _confirmation_binding(
    entry: RepositoryEntry,
    active_key: str,
    registry_generation: int,
    plan_digest: str,
) -> dict[str, Any]:
    return {
        "repository_key": entry.key,
        "repository_id": entry.repository_id,
        "repository_path": str(entry.path),
        "active_key": active_key,
        "registry_generation": registry_generation,
        "plan_sha256": plan_digest,
    }


def _assert_active_binding(active_key: str, registry_generation: int, binding: dict[str, Any]) -> None:
    if (
        binding.get("active_key") != active_key
        or binding.get("registry_generation") != registry_generation
    ):
        raise ProcedureTransactionError("Active repository selection changed after review; review again.")


def review_apply(
    entry: RepositoryEntry,
    state_root: str | Path = DEFAULT_STATE_ROOT,
    *,
    active_key: str,
    registry_generation: int,
    reviewed_plan_sha256: str,
    bundle: ProcedureBundle | None = None,
) -> dict[str, Any]:
    active_bundle = bundle or load_bundle()
    _validate_bundle_targets(active_bundle)
    with state_artifact_lock(state_root):
        store = _repository_store(state_root, entry.repository_id, create=True)
        active, errors = _active_operations(store, entry)
        if errors:
            raise ProcedureTransactionError(f"Transaction state requires operator review: {errors[0]}")
        if active:
            raise ProcedureTransactionError("An unresolved procedure transaction blocks another Apply.")
        plan = preview_reconciliation(entry, active_bundle)
        if plan.blockers or not plan.can_materialize:
            raise ProcedureTransactionError("Procedure preview has blockers; no Apply confirmation was issued.")
        digest = _plan_digest(entry, active_bundle, plan)
        if not secrets.compare_digest(digest, reviewed_plan_sha256):
            raise ProcedureTransactionError("Procedure preview changed; review the current generated plan again.")
        binding = _confirmation_binding(entry, active_key, registry_generation, digest)
        _prune_expired_tokens(store)
        _prune_terminal(store, entry)
        confirmation = _issue_token(store, purpose="apply", binding=binding)
        return {"confirmation": confirmation, "plan": plan.as_dict(include_diffs=False)}


def apply_confirmed(
    entry: RepositoryEntry,
    token: str,
    state_root: str | Path = DEFAULT_STATE_ROOT,
    *,
    active_key: str,
    registry_generation: int,
    bundle: ProcedureBundle | None = None,
) -> dict[str, Any]:
    active_bundle = bundle or load_bundle()
    _validate_bundle_targets(active_bundle)
    with state_artifact_lock(state_root):
        store = _repository_store(state_root, entry.repository_id, create=False)
        active, errors = _active_operations(store, entry)
        if errors:
            raise ProcedureTransactionError(f"Transaction state requires operator review: {errors[0]}")
        if active:
            raise ProcedureTransactionError("An unresolved procedure transaction blocks another Apply.")
        token_id = _token_id(token)
        confirmation_path = _token_directory(store, create=False) / f"{token_id}.json"
        raw = _read_regular(confirmation_path, MAX_JOURNAL_BYTES, required=False)
        if raw is None:
            raise ProcedureTransactionError("Confirmation token is unknown, expired, or already used.")
        try:
            record = json.loads(raw.decode("utf-8"))
            binding = record["binding"]
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise ProcedureTransactionError("Apply confirmation record is malformed.") from exc
        _assert_active_binding(active_key, registry_generation, binding)
        if (
            binding.get("repository_key") != entry.key
            or binding.get("repository_id") != entry.repository_id
            or binding.get("repository_path") != str(entry.path)
        ):
            raise ProcedureTransactionError("Apply confirmation belongs to a different managed repository.")
        plan = preview_reconciliation(entry, active_bundle)
        digest = _plan_digest(entry, active_bundle, plan)
        expected_binding = _confirmation_binding(entry, active_key, registry_generation, digest)
        _consume_token(store, token, purpose="apply", expected_binding=expected_binding)
        if plan.blockers or not plan.can_materialize:
            raise ProcedureTransactionError("Procedure preview has new blockers; no target files were written.")
        _invalidate_apply_tokens(store, entry)
        return _execute_apply(entry, active_bundle, plan, store, digest)


def _execute_apply(
    entry: RepositoryEntry,
    bundle: ProcedureBundle,
    plan: ReconciliationPlan,
    store: Path,
    approved_digest: str,
) -> dict[str, Any]:
    transactions = _transaction_directories(store, create=True)
    operation_id = uuid.uuid4().hex
    operation_directory = transactions / operation_id
    operation_directory.mkdir(mode=0o700)
    os.chmod(operation_directory, 0o700, follow_symlinks=False)
    _checked_directory(operation_directory, create=False)
    journal: dict[str, Any] | None = None
    try:
        journal, images = _new_journal(entry, bundle, plan, entry, operation_id)
        if not secrets.compare_digest(journal["plan_sha256"], approved_digest):
            raise ProcedureTransactionError("Procedure targets changed while the durable journal was prepared.")
        _write_journal(operation_directory, journal)
        for name, data in images.items():
            _write_image(operation_directory / name, data)
        _fsync_directory(operation_directory)
        _fsync_directory(operation_directory.parent)
        journal["state"] = "PREPARED"
        _write_journal(operation_directory, journal)
    except Exception as preparation_error:
        try:
            _remove_unpublished_operation(operation_directory)
        except Exception as cleanup_error:
            raise ProcedureTransactionError(
                f"Transaction preparation failed ({preparation_error}); "
                f"incomplete transaction cleanup also failed ({cleanup_error})."
            ) from preparation_error
        if isinstance(preparation_error, OSError):
            raise ProcedureTransactionError(
                f"Transaction preparation failed before any target write: {preparation_error}"
            ) from preparation_error
        raise
    try:
        journal["state"] = "APPLYING"
        _write_journal(operation_directory, journal)
        _assert_root_identity(entry, journal["root_identity"])
        _assert_directory_states(entry, journal["directories"], permit_created=False)
        for relative, expected_hash in plan.snapshots:
            if _target_state(entry, relative)["sha256"] != expected_hash:
                raise ProcedureTransactionError(f"Reviewed target changed immediately before Apply: {relative}")
        for item in [target for target in journal["targets"] if not target["is_lock"]]:
            current = _target_state(entry, item["path"])
            before = _target_before_state(item)
            after = _target_after_state(item)
            if _state_matches(current, after):
                continue
            if not _state_matches(current, before):
                raise ProcedureTransactionError(f"Target changed after confirmation: {item['path']}")
            data = _image_bytes(operation_directory, item, "after")
            _write_target(
                entry,
                item["path"],
                data,
                before,
                item["after_mode"],
                item["after_gid"],
                _temp_name(operation_id, item["index"]),
                directory_states=journal["directories"],
                on_directory_created=lambda: _write_journal(operation_directory, journal),
            )
        overlay_path = plan.status["overlay"]["path"]
        overlay_snapshot = dict(plan.snapshots).get(overlay_path)
        if _target_state(entry, overlay_path)["sha256"] != overlay_snapshot:
            raise ProcedureTransactionError("Project overlay changed during Apply; no overlay file was written.")
        for target in journal["targets"]:
            current = _target_state(entry, target["path"])
            if not _state_matches(current, _target_before_state(target)) and not _state_matches(
                current, _target_after_state(target)
            ):
                raise ProcedureTransactionError(f"Target changed during Apply: {target['path']}")
        item = next(target for target in journal["targets"] if target["is_lock"])
        current = _target_state(entry, item["path"])
        before = _target_before_state(item)
        after = _target_after_state(item)
        if not _state_matches(current, after):
            if not _state_matches(current, before):
                raise ProcedureTransactionError(f"Target changed after confirmation: {item['path']}")
            data = _image_bytes(operation_directory, item, "after")
            _write_target(
                entry,
                item["path"],
                data,
                before,
                item["after_mode"],
                item["after_gid"],
                _temp_name(operation_id, item["index"]),
                directory_states=journal["directories"],
                on_directory_created=lambda: _write_journal(operation_directory, journal),
            )
        result = _status_for(entry.path, entry, bundle)
        if result["status"] != "CURRENT":
            raise ProcedureTransactionError(
                f"Independent verification did not return CURRENT: {result['status']}."
            )
        for item in journal["targets"]:
            if not _state_matches(_target_state(entry, item["path"]), _target_after_state(item)):
                raise ProcedureTransactionError(f"Independent target hash verification failed: {item['path']}")
        journal["state"] = "VERIFIED"
        journal["verified_at"] = _now()
        journal["verified_lock_sha256"] = result["lock_sha256"]
        _write_journal(operation_directory, journal)
        cleanup_error = None
        try:
            _cleanup_images(operation_directory, journal)
        except (OSError, ProcedureTransactionError) as exc:
            cleanup_error = str(exc)
        return {
            "operation_id": operation_id,
            "state": "VERIFIED",
            "repository_key": entry.key,
            "repository_id": entry.repository_id,
            "release": bundle.release,
            "status": "CURRENT",
            "lock_sha256": result["lock_sha256"],
            "target_hashes": {item["path"]: item["after_sha256"] for item in journal["targets"]},
            "affected_files": [item["path"] for item in journal["targets"] if item["before_sha256"] != item["after_sha256"]],
            "completed_at": journal["verified_at"],
            "no_op": not any(item["before_sha256"] != item["after_sha256"] for item in journal["targets"]),
            "cleanup_pending": cleanup_error is not None,
            "cleanup_error": cleanup_error,
        }
    except Exception as apply_error:
        try:
            _restore_original(entry, operation_directory, journal)
            if journal["state"] == "PREPARING":
                journal["preparation_incomplete"] = True
            journal["state"] = "RECOVERED"
            journal["recovered_at"] = _now()
            journal["recovery_reason"] = "Apply failed; the exact original target set was restored."
            _write_journal(operation_directory, journal)
        except Exception as recovery_error:
            journal["state"] = "RECOVERY_REQUIRED"
            journal["failure"] = str(apply_error)[:1000]
            journal["recovery_failure"] = str(recovery_error)[:1000]
            try:
                _write_journal(operation_directory, journal)
            except Exception as journal_error:
                raise ProcedureTransactionError(
                    f"Apply failed ({apply_error}); rollback failed ({recovery_error}); "
                    f"journal update also failed ({journal_error})."
                ) from apply_error
            raise ProcedureTransactionError(
                f"Apply did not complete and exact rollback failed; operation {operation_id} requires PO recovery: "
                f"{recovery_error}"
            ) from apply_error
        cleanup_error = None
        try:
            _cleanup_images(operation_directory, journal)
        except (OSError, ProcedureTransactionError) as exc:
            cleanup_error = str(exc)
        suffix = f" Recovery-image cleanup remains pending: {cleanup_error}" if cleanup_error else ""
        raise ProcedureTransactionError(
            f"Apply failed; original files were restored. Operation {operation_id}: {apply_error}.{suffix}"
        ) from apply_error


def _remove_unpublished_operation(operation_directory: Path) -> None:
    if not operation_directory.exists() and not operation_directory.is_symlink():
        return
    _checked_directory(operation_directory, create=False)
    for child in operation_directory.iterdir():
        if child.is_symlink() or not child.is_file():
            raise ProcedureTransactionError("Cannot safely clean incomplete transaction preparation.")
        child.unlink()
    operation_directory.rmdir()
    _fsync_directory(operation_directory.parent)


def _cleanup_images(operation_directory: Path, journal: dict[str, Any]) -> None:
    for item in journal["targets"]:
        for key in ("before_file", "after_file"):
            name = item.get(key)
            if not name:
                continue
            path = operation_directory / name
            if path.exists() or path.is_symlink():
                data = _read_regular(path, MAX_TARGET_BYTES)
                if not journal.get("preparation_incomplete"):
                    expected_hash = item.get("before_sha256_image") if key == "before_file" else item["after_sha256"]
                    if _sha256(data) != expected_hash:
                        raise ProcedureTransactionError(f"Cannot clean altered transaction image: {name}")
                path.unlink()
    _fsync_directory(operation_directory)


def inspect_transaction(
    entry: RepositoryEntry,
    state_root: str | Path = DEFAULT_STATE_ROOT,
) -> dict[str, Any] | None:
    with state_artifact_lock(state_root):
        try:
            store = _repository_store(state_root, entry.repository_id, create=False)
            if not store.exists():
                return None
            active, errors = _active_operations(store, entry)
            if errors:
                return {"state": "CORRUPT", "blocked": True, "error": errors[0], "operation_id": None}
            if len(active) > 1:
                return {
                    "state": "CORRUPT",
                    "blocked": True,
                    "error": "More than one unresolved procedure transaction exists.",
                    "operation_id": None,
                }
            if active:
                operation_directory, journal = active[0]
                try:
                    _assert_recovery_safe(entry, operation_directory, journal)
                    return {
                        "state": journal["state"],
                        "blocked": False,
                        "recoverable": True,
                        "conflicts": [],
                        "operation_id": journal["operation_id"],
                        "release": journal["release"],
                        "created_at": journal["created_at"],
                    }
                except (OSError, ProcedureTransactionError) as exc:
                    return {
                        "state": "RECOVERY_BLOCKED",
                        "blocked": True,
                        "error": str(exc),
                        "operation_id": journal["operation_id"],
                    }
            transactions = _transaction_directories(store, create=False)
            if not transactions.exists():
                return None
            terminals = []
            for child in transactions.iterdir():
                _checked_directory(child, create=False)
                journal = _read_journal(child, entry)
                if journal["state"] in _TERMINAL_STATES:
                    terminals.append((journal.get("updated_at", ""), journal))
            if not terminals:
                return None
            _, latest = max(terminals, key=lambda item: item[0])
            return {
                "state": latest["state"],
                "blocked": False,
                "operation_id": latest["operation_id"],
                "release": latest["release"],
                "completed_at": latest.get("verified_at") or latest.get("recovered_at"),
                "cleanup_pending": any(
                    child.name != "journal.json"
                    for operation in transactions.iterdir()
                    for child in operation.iterdir()
                ),
            }
        except (OSError, ProcedureTransactionError) as exc:
            return {"state": "CORRUPT", "blocked": True, "error": str(exc), "operation_id": None}


def review_recovery(
    entry: RepositoryEntry,
    operation_id: str,
    state_root: str | Path = DEFAULT_STATE_ROOT,
    *,
    active_key: str,
    registry_generation: int,
) -> dict[str, Any]:
    if len(operation_id) != 32 or any(character not in "0123456789abcdef" for character in operation_id):
        raise ProcedureTransactionError("Recovery operation ID is invalid.")
    with state_artifact_lock(state_root):
        store = _repository_store(state_root, entry.repository_id, create=False)
        active, errors = _active_operations(store, entry)
        if errors or len(active) != 1 or active[0][1]["operation_id"] != operation_id:
            raise ProcedureTransactionError("The requested operation is not the current recoverable transaction.")
        operation_directory, journal = active[0]
        if journal["state"] != "PREPARING":
            _verify_images(operation_directory, journal)
        _assert_recovery_safe(entry, operation_directory, journal)
        binding = {
            "repository_key": entry.key,
            "repository_id": entry.repository_id,
            "repository_path": str(entry.path),
            "operation_id": operation_id,
            "journal_sha256": _sha256(_read_regular(operation_directory / "journal.json", MAX_JOURNAL_BYTES)),
            "active_key": active_key,
            "registry_generation": registry_generation,
        }
        _prune_expired_tokens(store)
        confirmation = _issue_token(store, purpose="recover", binding=binding)
        return {
            "confirmation": confirmation,
            "operation_id": operation_id,
            "targets": [item["path"] for item in journal["targets"]],
            "state": journal["state"],
        }


def _assert_recovery_safe(
    entry: RepositoryEntry,
    operation_directory: Path,
    journal: dict[str, Any],
) -> None:
    preparing = journal["state"] == "PREPARING"
    if not preparing:
        _verify_images(operation_directory, journal)
    _assert_root_identity(entry, journal["root_identity"])
    _assert_directory_states(entry, journal["directories"], permit_created=not preparing)
    for item in journal["targets"]:
        current = _target_state(entry, item["path"])
        before_matches = _state_matches(current, _target_before_state(item))
        after_matches = _state_matches(current, _target_after_state(item))
        if not before_matches and (preparing or not after_matches):
            raise ProcedureTransactionError(
                f"Recovery is blocked because {item['path']} matches neither known transaction image."
            )


def recover_confirmed(
    entry: RepositoryEntry,
    operation_id: str,
    token: str,
    state_root: str | Path = DEFAULT_STATE_ROOT,
    *,
    active_key: str,
    registry_generation: int,
) -> dict[str, Any]:
    with state_artifact_lock(state_root):
        store = _repository_store(state_root, entry.repository_id, create=False)
        active, errors = _active_operations(store, entry)
        if errors or len(active) != 1 or active[0][1]["operation_id"] != operation_id:
            raise ProcedureTransactionError("The requested operation is no longer the current recoverable transaction.")
        operation_directory, journal = active[0]
        _assert_recovery_safe(entry, operation_directory, journal)
        binding = {
            "repository_key": entry.key,
            "repository_id": entry.repository_id,
            "repository_path": str(entry.path),
            "operation_id": operation_id,
            "journal_sha256": _sha256(_read_regular(operation_directory / "journal.json", MAX_JOURNAL_BYTES)),
            "active_key": active_key,
            "registry_generation": registry_generation,
        }
        _assert_active_binding(active_key, registry_generation, binding)
        _consume_token(store, token, purpose="recover", expected_binding=binding)
        try:
            _restore_original(entry, operation_directory, journal)
        except Exception as exc:
            journal["state"] = "RECOVERY_REQUIRED"
            journal["recovery_failure"] = str(exc)[:1000]
            _write_journal(operation_directory, journal)
            raise ProcedureTransactionError(
                f"Recovery did not complete; operation {operation_id} remains blocked: {exc}"
            ) from exc
        journal["state"] = "RECOVERED"
        journal["recovered_at"] = _now()
        journal["recovery_reason"] = "Product Owner confirmed restoration of the exact original target set."
        _write_journal(operation_directory, journal)
        cleanup_error = None
        try:
            _cleanup_images(operation_directory, journal)
        except (OSError, ProcedureTransactionError) as exc:
            cleanup_error = str(exc)
        return {
            "operation_id": operation_id,
            "state": "RECOVERED",
            "repository_key": entry.key,
            "repository_id": entry.repository_id,
            "restored_files": [item["path"] for item in journal["targets"]],
            "completed_at": journal["recovered_at"],
            "cleanup_pending": cleanup_error is not None,
            "cleanup_error": cleanup_error,
        }
