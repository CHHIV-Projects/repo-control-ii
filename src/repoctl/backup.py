from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import UTC, datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
from typing import Any, Callable, Mapping
import uuid

from . import backup_guard
from .information_store import (
    APPLICATION_ID,
    SCHEMA_VERSION,
    InformationStore,
    InformationStoreError,
    StoreIdentity,
)
from .scanner.core import DEFAULT_STATE_ROOT
from .store_authority import (
    StoreAuthorityConfiguration,
    StoreAuthorityError,
    load_store_authority,
    verify_store_registry,
)


MANIFEST_VERSION = 1
DEFAULT_MAX_AGE_SECONDS = 20 * 60
_MOUNT_ESCAPE = re.compile(r"\\([0-7]{3})")


class BackupError(RuntimeError):
    pass


@dataclass(frozen=True)
class BackupConfig:
    source: Path
    staging: Path
    destination: Path
    restore_root: Path
    mountpoint: Path
    mount_source: str
    repository_id: str
    repository_key: str
    repository_root: str
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS
    alert_hook: Path | None = None
    alert_target: str | None = None
    authority_state_root: Path = DEFAULT_STATE_ROOT
    coverage_path: Path = backup_guard.DEFAULT_RECOVERY_POINT_PATH


@dataclass(frozen=True)
class BackupStatus:
    healthy: bool
    message: str
    newest_verified_at: datetime | None
    age_seconds: float | None
    invalid_manifests: int


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _require_directory(path: Path, label: str, *, writable: bool = True) -> Path:
    if path.is_symlink():
        raise BackupError(f"{label} must not be a symbolic link.")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise BackupError(f"{label} is unavailable: {path}") from exc
    if not resolved.is_dir():
        raise BackupError(f"{label} is not a directory: {resolved}")
    required_access = os.R_OK | os.X_OK | (os.W_OK if writable else 0)
    if not os.access(resolved, required_access):
        requirement = "readable and writable" if writable else "readable"
        raise BackupError(f"{label} is not {requirement} by the service identity: {resolved}")
    return resolved


def _require_source(path: Path) -> Path:
    if path.is_symlink():
        raise BackupError("The source database must not be a symbolic link.")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise BackupError(f"The source database is unavailable: {path}") from exc
    if not resolved.is_file():
        raise BackupError(f"The source database is not a regular file: {resolved}")
    return resolved


def _unescape_mount_field(value: str) -> str:
    return _MOUNT_ESCAPE.sub(lambda match: chr(int(match.group(1), 8)), value)


def parse_mountinfo(text: str) -> list[tuple[Path, str, str]]:
    mounts: list[tuple[Path, str, str]] = []
    for line in text.splitlines():
        fields = line.split(" - ", 1)
        if len(fields) != 2:
            continue
        before, after = fields
        left, right = before.split(), after.split()
        if len(left) < 5 or len(right) < 2:
            continue
        mounts.append((Path(_unescape_mount_field(left[4])), right[0], _unescape_mount_field(right[1])))
    return mounts


def verify_destination_mount(
    destination: Path,
    expected_mountpoint: Path,
    expected_source: str,
    *,
    mountinfo: str | None = None,
    writable: bool = True,
) -> None:
    mount = _require_directory(expected_mountpoint, "Expected NAS mount", writable=writable)
    target = _require_directory(destination, "NAS backup destination", writable=writable)
    if target == mount or not target.is_relative_to(mount):
        raise BackupError("The backup destination must be a child directory of the expected NAS mount.")
    if mountinfo is None:
        mountinfo = Path("/proc/self/mountinfo").read_text(encoding="utf-8")
    matching = [
        item for item in parse_mountinfo(mountinfo)
        if target == item[0] or target.is_relative_to(item[0])
    ]
    if not matching:
        raise BackupError("The backup destination is not on a mounted filesystem.")
    actual_mount, filesystem, source = max(matching, key=lambda item: len(str(item[0])))
    if actual_mount.resolve() != mount:
        raise BackupError(f"The destination is mounted at {actual_mount}, not the expected mount.")
    if filesystem.lower() not in {"cifs", "smb3"} or source != expected_source:
        raise BackupError("The destination mount type or NAS share identity does not match the approved target.")


def _open_readonly(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _store_identity(
    path: Path,
    expected_repository_id: str,
    expected_repository_key: str,
    expected_repository_root: str,
) -> StoreIdentity:
    connection = _open_readonly(path)
    try:
        repository = connection.execute(
            "SELECT repository_id, repository_key, repository_root FROM repository WHERE repository_id = ?",
            (expected_repository_id,),
        ).fetchone()
        if repository is None or tuple(repository) != (
            expected_repository_id, expected_repository_key, expected_repository_root,
        ):
            raise BackupError("The database repository identity does not match the configured repository.")
        selection = connection.execute(
            """
            SELECT s.milestone_id, s.selection_id, s.selection_revision, m.prompt_path
            FROM repository_selection s
            JOIN milestone m ON m.repository_id = s.repository_id AND m.milestone_id = s.milestone_id
            WHERE s.repository_id = ?
            """,
            (expected_repository_id,),
        ).fetchone()
        if selection is None:
            raise BackupError("The database has no valid selected milestone.")
        return StoreIdentity(
            repository_id=expected_repository_id,
            repository_key=expected_repository_key,
            repository_root=expected_repository_root,
            milestone_id=selection["milestone_id"],
            prompt_path=selection["prompt_path"],
            selection_id=selection["selection_id"],
            selection_revision=selection["selection_revision"],
        )
    except sqlite3.DatabaseError as exc:
        raise BackupError(f"The database identity could not be read: {exc}") from exc
    finally:
        connection.close()


def _store_snapshot(
    path: Path,
    expected_repository_id: str,
    expected_repository_key: str,
    expected_repository_root: str,
    authority_configuration: StoreAuthorityConfiguration | None = None,
) -> dict[str, Any]:
    try:
        identity = _store_identity(
            path, expected_repository_id, expected_repository_key, expected_repository_root,
        )
        metadata_connection = _open_readonly(path)
        try:
            metadata = dict(metadata_connection.execute("SELECT key, value FROM store_metadata").fetchall())
        finally:
            metadata_connection.close()
        creation_mode = metadata.get("creation_mode")
        if creation_mode == "disposable_fixture":
            store = InformationStore.open_disposable(
                path, identity, allow_unsupported_runtime_for_fixture=True,
            )
        elif creation_mode == "managed_prepared":
            if authority_configuration is None:
                store = InformationStore.open_managed_prepared(path, identity)
            else:
                store = InformationStore.open_managed_backup_snapshot(path, identity, authority_configuration)
        else:
            raise BackupError("The database creation mode is not recognized.")
        selected = store.snapshot()
        connection = _open_readonly(path)
        try:
            if connection.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
                raise BackupError("The database application ID is not Repo Control's.")
            if connection.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
                raise BackupError("The database schema version is not supported.")
            InformationStore._verify_schema(connection, creation_mode=creation_mode)
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            if integrity != "ok" or violations:
                raise BackupError("The database failed integrity or foreign-key validation.")
            revisions = []
            for row in connection.execute(
                """
                SELECT document_id, revision_id, content, content_sha256
                FROM document_revision WHERE repository_id = ?
                ORDER BY document_id, revision_order
                """,
                (expected_repository_id,),
            ):
                actual_hash = hashlib.sha256(row["content"].encode("utf-8")).hexdigest()
                if actual_hash != row["content_sha256"]:
                    raise BackupError("A document revision's stored hash does not match its exact content.")
                revisions.append({
                    "document_id": row["document_id"],
                    "revision_id": row["revision_id"],
                    "content_sha256": row["content_sha256"],
                })
            workflows = []
            for row in connection.execute(
                """
                SELECT milestone_id, store_revision, event_count
                FROM workflow_state WHERE repository_id = ? ORDER BY milestone_id
                """,
                (expected_repository_id,),
            ):
                events = connection.execute(
                    """
                    SELECT sequence, event_id, event_type, event_timestamp, payload_sha256
                    FROM workflow_event
                    WHERE repository_id = ? AND milestone_id = ? ORDER BY sequence
                    """,
                    (expected_repository_id, row["milestone_id"]),
                ).fetchall()
                if len(events) != row["event_count"]:
                    raise BackupError("A workflow event count does not match its stored projection.")
                workflows.append({
                    "milestone_id": row["milestone_id"],
                    "store_revision": row["store_revision"],
                    "event_count": row["event_count"],
                    "events": [dict(event) for event in events],
                })
            selected_workflow = next(
                (
                    workflow for workflow in workflows
                    if workflow["milestone_id"] == identity.milestone_id
                ),
                None,
            )
            if selected_workflow is None:
                raise BackupError("The selected milestone has no validated workflow history.")
            return {
                "application_id": APPLICATION_ID,
                "schema_version": SCHEMA_VERSION,
                "repository": {
                    "repository_id": identity.repository_id,
                    "repository_key": identity.repository_key,
                    "repository_root": identity.repository_root,
                },
                "selection": {
                    "milestone_id": identity.milestone_id,
                    "prompt_path": identity.prompt_path,
                    "selection_id": identity.selection_id,
                    "selection_revision": identity.selection_revision,
                    "prompt_revision_id": selected["prompt_revision_id"],
                },
                "selected_workflow": {
                    "store_revision": selected["store_revision"],
                    "event_count": selected["event_count"],
                    "events": selected_workflow["events"],
                    "projection": selected["projection"],
                },
                "workflows": workflows,
                "document_revisions": revisions,
            }
        finally:
            connection.close()
    except (sqlite3.DatabaseError, InformationStoreError) as exc:
        raise BackupError(f"The database failed validation: {exc}") from exc


def _write_manifest(path: Path, payload: Mapping[str, Any]) -> None:
    encoded = _canonical_json(payload)
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(encoded)
            stream.write(b"\n")
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)


def _read_manifest_bytes(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise BackupError("The backup manifest is absent, oversized, or not a regular file.")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise BackupError("The backup manifest is unreadable.") from exc


def _copy_to_partial(source: Path, destination: Path) -> None:
    descriptor = os.open(
        destination,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with source.open("rb") as input_stream, os.fdopen(descriptor, "wb", closefd=False) as output_stream:
            shutil.copyfileobj(input_stream, output_stream, length=1024 * 1024)
            output_stream.flush()
            os.fsync(output_stream.fileno())
    finally:
        os.close(descriptor)


def _remove_if_present(path: Path | None) -> None:
    if path is not None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _sqlite_sidecar_paths(path: Path) -> tuple[Path, Path]:
    return (
        path.with_name(f"{path.name}-wal"),
        path.with_name(f"{path.name}-shm"),
    )


def _existing_sqlite_sidecars(path: Path) -> frozenset[Path]:
    existing = []
    for sidecar in _sqlite_sidecar_paths(path):
        try:
            sidecar.lstat()
        except FileNotFoundError:
            continue
        existing.append(sidecar)
    return frozenset(existing)


def _remove_new_sqlite_sidecars(path: Path | None, preexisting: frozenset[Path]) -> None:
    if path is None:
        return
    for sidecar in _sqlite_sidecar_paths(path):
        if sidecar in preexisting:
            continue
        try:
            metadata = sidecar.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise BackupError(f"Refusing to remove a non-regular SQLite sidecar: {sidecar.name}")
        if sidecar.name.endswith("-wal") and metadata.st_size != 0:
            raise BackupError(f"Refusing to remove a non-empty SQLite WAL sidecar: {sidecar.name}")
        try:
            sidecar.unlink()
        except FileNotFoundError:
            pass


def _lock_manifest(path: Path) -> int:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise BackupError("The backup manifest is not a regular file.")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BackupError("Another restore operation is already running for this backup.") from exc
        except OSError as exc:
            raise BackupError("The backup manifest could not be locked for restore.") from exc
    except (OSError, BackupError):
        os.close(descriptor)
        raise
    return descriptor


def create_verified_backup(
    source: Path,
    staging: Path,
    destination: Path,
    *,
    expected_repository_id: str,
    expected_repository_key: str,
    expected_repository_root: str,
    now: datetime | None = None,
    authority_configuration: StoreAuthorityConfiguration | None = None,
    coverage_path: Path | None = None,
) -> dict[str, Any]:
    source_path = _require_source(source)
    staging_path = _require_directory(staging, "Local backup staging directory")
    destination_path = _require_directory(destination, "NAS backup destination")
    managed_identity: StoreIdentity | None = None
    managed_database_identity: str | None = None
    if authority_configuration is not None:
        if authority_configuration.database_path.resolve(strict=False) != source_path:
            raise BackupError("The configured source is not the active managed InformationStore database.")
        try:
            verify_store_registry(authority_configuration)
            managed_store = InformationStore.open_managed_from_external_selection(
                authority_configuration,
                repository_id=expected_repository_id,
                repository_key=expected_repository_key,
                repository_root=expected_repository_root,
            )
            managed_identity = managed_store.identity
            managed_database_identity = managed_store.database_identity()
        except (InformationStoreError, StoreAuthorityError) as exc:
            raise BackupError(f"The active managed authority could not be verified for backup: {exc}") from exc
        if coverage_path is None:
            try:
                coverage_path = backup_guard.recovery_point_path()
            except backup_guard.BackupEvidenceError as exc:
                raise BackupError(str(exc)) from exc
    lock_path = staging_path / ".repoctl-backup.lock"
    lock_descriptor = os.open(
        lock_path,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        try:
            fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BackupError("Another Repo Control backup operation is already running.") from exc
        stamp = (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        stem = f"repo-control-{stamp}-{uuid.uuid4().hex[:12]}"
        local_snapshot: Path | None = None
        local_snapshot_sidecars: frozenset[Path] = frozenset()
        remote_partial: Path | None = None
        remote_partial_sidecars: frozenset[Path] = frozenset()
        manifest_partial: Path | None = None
        final_database: Path | None = None
        final_manifest: Path | None = None
        published = False
        try:
            descriptor, local_name = tempfile.mkstemp(
                prefix=".repo-control-", suffix=".sqlite3.partial", dir=staging_path,
            )
            os.fchmod(descriptor, 0o600)
            os.close(descriptor)
            local_snapshot = Path(local_name)
            local_snapshot_sidecars = _existing_sqlite_sidecars(local_snapshot)
            if local_snapshot_sidecars:
                raise BackupError("A new local snapshot has unexpected pre-existing SQLite sidecars.")
            source_uri = f"{source_path.as_uri()}?mode=ro"
            source_connection = sqlite3.connect(source_uri, uri=True, timeout=30)
            try:
                target_connection = sqlite3.connect(local_snapshot, timeout=30)
                try:
                    source_connection.backup(target_connection, pages=512, sleep=0.1)
                finally:
                    target_connection.close()
            finally:
                source_connection.close()
            snapshot = _store_snapshot(
                local_snapshot,
                expected_repository_id,
                expected_repository_key,
                expected_repository_root,
                authority_configuration,
            )
            if managed_identity is not None and snapshot["selection"] != {
                "milestone_id": managed_identity.milestone_id,
                "prompt_path": managed_identity.prompt_path,
                "selection_id": managed_identity.selection_id,
                "selection_revision": managed_identity.selection_revision,
                "prompt_revision_id": snapshot["selection"]["prompt_revision_id"],
            }:
                raise BackupError("The selected managed authority changed while the backup snapshot was created.")
            remote_partial = destination_path / f".{stem}.sqlite3.partial"
            final_database = destination_path / f"{stem}.sqlite3"
            manifest_partial = destination_path / f".{stem}.manifest.json.partial"
            final_manifest = destination_path / f"{stem}.manifest.json"
            _copy_to_partial(local_snapshot, remote_partial)
            remote_partial_sidecars = _existing_sqlite_sidecars(remote_partial)
            if remote_partial_sidecars:
                raise BackupError("A new destination partial has unexpected pre-existing SQLite sidecars.")
            backup_hash = _sha256_file(remote_partial)
            if backup_hash != _sha256_file(local_snapshot):
                raise BackupError("The transferred database does not match the local snapshot bytes.")
            if _store_snapshot(
                remote_partial,
                expected_repository_id,
                expected_repository_key,
                expected_repository_root,
                authority_configuration,
            ) != snapshot:
                raise BackupError("The destination copy does not match the consistent local SQLite snapshot.")
            _remove_new_sqlite_sidecars(remote_partial, remote_partial_sidecars)
            verified_at = (now or datetime.now(UTC)).astimezone(UTC)
            manifest: dict[str, Any] = {
                "manifest_version": MANIFEST_VERSION,
                "verified": True,
                "verified_at_utc": verified_at.isoformat(),
                "database_file": final_database.name,
                "database_bytes": remote_partial.stat().st_size,
                "database_sha256": backup_hash,
                "snapshot_sha256": hashlib.sha256(_canonical_json(snapshot)).hexdigest(),
                "snapshot": snapshot,
                "integrity_check": "ok",
                "foreign_key_violations": 0,
            }
            manifest["manifest_sha256"] = hashlib.sha256(_canonical_json(manifest)).hexdigest()
            _write_manifest(manifest_partial, manifest)
            os.replace(remote_partial, final_database)
            os.replace(manifest_partial, final_manifest)
            published = True
            if authority_configuration is not None:
                try:
                    if load_store_authority(authority_configuration.state_root) != authority_configuration:
                        raise BackupError("The managed authority changed before recovery-point publication.")
                    current_store = InformationStore.open_managed_from_external_selection(
                        authority_configuration,
                        repository_id=expected_repository_id,
                        repository_key=expected_repository_key,
                        repository_root=expected_repository_root,
                    )
                    if (
                        current_store.identity != managed_identity
                        or current_store.database_identity() != managed_database_identity
                    ):
                        raise BackupError(
                            "The managed database or selection changed before recovery-point publication."
                        )
                except (InformationStoreError, StoreAuthorityError) as exc:
                    raise BackupError(f"The active managed authority changed during backup: {exc}") from exc
                verified_manifest = _read_manifest_for_monitor(
                    final_manifest,
                    expected_repository_id=expected_repository_id,
                    expected_repository_key=expected_repository_key,
                    expected_repository_root=expected_repository_root,
                )
                if verified_manifest is None:
                    raise BackupError("The published NAS manifest could not be reverified for write coverage.")
                if coverage_path is None or managed_identity is None or managed_database_identity is None:
                    raise BackupError("The active managed backup is missing its authority-bound recovery-point inputs.")
                try:
                    published_manifest = json.loads(_read_manifest_bytes(final_manifest))
                except (BackupError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise BackupError(f"The published NAS manifest could not be read for write coverage: {exc}") from exc
                if not isinstance(published_manifest, dict):
                    raise BackupError("The published NAS manifest is not a JSON object.")
                binding = backup_guard.authority_binding(
                    authority_configuration,
                    managed_database_identity,
                    repository_id=managed_identity.repository_id,
                    repository_key=managed_identity.repository_key,
                    repository_root=managed_identity.repository_root,
                    milestone_id=managed_identity.milestone_id,
                    prompt_path=managed_identity.prompt_path,
                    selection_id=managed_identity.selection_id,
                    selection_revision=managed_identity.selection_revision,
                )
                try:
                    backup_guard.write_recovery_point(
                        coverage_path,
                        manifest_path=final_manifest,
                        manifest=published_manifest,
                        binding=binding,
                    )
                except backup_guard.BackupEvidenceError as exc:
                    raise BackupError(f"The verified NAS backup could not authorize managed writes: {exc}") from exc
            return {**manifest, "manifest_path": str(final_manifest)}
        finally:
            _remove_new_sqlite_sidecars(local_snapshot, local_snapshot_sidecars)
            _remove_new_sqlite_sidecars(remote_partial, remote_partial_sidecars)
            _remove_if_present(local_snapshot)
            _remove_if_present(remote_partial)
            _remove_if_present(manifest_partial)
            if not published:
                _remove_if_present(final_database)
                _remove_if_present(final_manifest)
    finally:
        fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
        os.close(lock_descriptor)


def _read_verified_manifest(
    manifest_path: Path,
    *,
    expected_repository_id: str,
    expected_repository_key: str,
    expected_repository_root: str,
    authority_configuration: StoreAuthorityConfiguration | None = None,
) -> tuple[dict[str, Any], Path]:
    try:
        manifest = json.loads(_read_manifest_bytes(manifest_path).decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupError("The backup manifest is unreadable or malformed.") from exc
    if not isinstance(manifest, dict):
        raise BackupError("The backup manifest has an unsupported shape.")
    claimed_hash = manifest.get("manifest_sha256")
    unsigned = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    if (
        manifest.get("manifest_version") != MANIFEST_VERSION
        or manifest.get("verified") is not True
        or manifest.get("integrity_check") != "ok"
        or manifest.get("foreign_key_violations") != 0
        or not isinstance(claimed_hash, str)
        or hashlib.sha256(_canonical_json(unsigned)).hexdigest() != claimed_hash
    ):
        raise BackupError("The backup manifest is not a valid successful-verification record.")
    database_name = manifest.get("database_file")
    if not isinstance(database_name, str) or Path(database_name).name != database_name:
        raise BackupError("The backup manifest names an unsafe database path.")
    database = manifest_path.parent / database_name
    if database.is_symlink() or not database.is_file():
        raise BackupError("The verified backup database is absent or not a regular file.")
    if database.stat().st_size != manifest.get("database_bytes"):
        raise BackupError("The verified backup database size has changed.")
    if _sha256_file(database) != manifest.get("database_sha256"):
        raise BackupError("The verified backup database hash has changed.")
    snapshot = manifest.get("snapshot")
    expected_snapshot_hash = hashlib.sha256(_canonical_json(snapshot)).hexdigest()
    if expected_snapshot_hash != manifest.get("snapshot_sha256"):
        raise BackupError("The backup snapshot identity hash does not match its manifest.")
    identity = snapshot.get("repository") if isinstance(snapshot, dict) else None
    if identity != {
        "repository_id": expected_repository_id,
        "repository_key": expected_repository_key,
        "repository_root": expected_repository_root,
    }:
        raise BackupError("The backup manifest belongs to a different repository.")
    sidecars_before = _existing_sqlite_sidecars(database)
    if sidecars_before:
        raise BackupError("The verified backup has pre-existing SQLite sidecars.")
    try:
        actual_snapshot = _store_snapshot(
            database,
            expected_repository_id,
            expected_repository_key,
            expected_repository_root,
            authority_configuration,
        )
    finally:
        _remove_new_sqlite_sidecars(database, sidecars_before)
    if actual_snapshot != snapshot:
        raise BackupError("The backup database does not match its verified snapshot manifest.")
    return manifest, database


def restore_verified_backup(
    manifest_path: Path,
    destination: Path,
    restore_root: Path,
    *,
    expected_repository_id: str,
    expected_repository_key: str,
    expected_repository_root: str,
    authority_configuration: StoreAuthorityConfiguration | None = None,
) -> Path:
    manifest_lock = _lock_manifest(manifest_path)
    try:
        root = _require_directory(restore_root, "Separate restore directory")
        target = destination.absolute()
        if target.parent.resolve(strict=True) != root or target.exists() or target.is_symlink():
            raise BackupError("Restore destination must be a new direct child of the separate restore directory.")
        _, backup = _read_verified_manifest(
            manifest_path,
            expected_repository_id=expected_repository_id,
            expected_repository_key=expected_repository_key,
            expected_repository_root=expected_repository_root,
            authority_configuration=authority_configuration,
        )
        backup_sidecars_before = _existing_sqlite_sidecars(backup)
        if backup_sidecars_before:
            raise BackupError("The verified backup has pre-existing SQLite sidecars.")
        descriptor, temporary_name = tempfile.mkstemp(prefix=".repo-control-restore-", suffix=".partial", dir=root)
        os.fchmod(descriptor, 0o600)
        os.close(descriptor)
        temporary = Path(temporary_name)
        temporary_sidecars_before = _existing_sqlite_sidecars(temporary)
        try:
            if temporary_sidecars_before:
                raise BackupError("A new restore temporary has unexpected pre-existing SQLite sidecars.")
            try:
                source_connection = sqlite3.connect(f"{backup.as_uri()}?mode=ro", uri=True, timeout=30)
                try:
                    target_connection = sqlite3.connect(temporary, timeout=30)
                    try:
                        source_connection.backup(target_connection, pages=512, sleep=0.1)
                    finally:
                        target_connection.close()
                finally:
                    source_connection.close()
            finally:
                _remove_new_sqlite_sidecars(backup, backup_sidecars_before)
            restored = _store_snapshot(
                temporary,
                expected_repository_id,
                expected_repository_key,
                expected_repository_root,
                authority_configuration,
            )
            _remove_new_sqlite_sidecars(temporary, temporary_sidecars_before)
            manifest, _ = _read_verified_manifest(
                manifest_path,
                expected_repository_id=expected_repository_id,
                expected_repository_key=expected_repository_key,
                expected_repository_root=expected_repository_root,
                authority_configuration=authority_configuration,
            )
            if restored != manifest["snapshot"]:
                raise BackupError("The restored database does not match the verified backup snapshot.")
            os.replace(temporary, target)
            return target
        finally:
            _remove_new_sqlite_sidecars(temporary, temporary_sidecars_before)
            _remove_if_present(temporary)
    finally:
        try:
            fcntl.flock(manifest_lock, fcntl.LOCK_UN)
        finally:
            os.close(manifest_lock)


def _read_manifest_for_monitor(
    path: Path,
    *,
    expected_repository_id: str,
    expected_repository_key: str,
    expected_repository_root: str,
) -> tuple[datetime, Path] | None:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
            return None
    except OSError:
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    claimed_hash = value.get("manifest_sha256")
    unsigned = {key: item for key, item in value.items() if key != "manifest_sha256"}
    if (
        value.get("manifest_version") != MANIFEST_VERSION
        or value.get("verified") is not True
        or value.get("integrity_check") != "ok"
        or value.get("foreign_key_violations") != 0
        or not isinstance(claimed_hash, str)
        or hashlib.sha256(_canonical_json(unsigned)).hexdigest() != claimed_hash
    ):
        return None
    snapshot = value.get("snapshot")
    if not isinstance(snapshot, dict):
        return None
    if hashlib.sha256(_canonical_json(snapshot)).hexdigest() != value.get("snapshot_sha256"):
        return None
    if snapshot.get("repository") != {
        "repository_id": expected_repository_id,
        "repository_key": expected_repository_key,
        "repository_root": expected_repository_root,
    }:
        return None
    database_name = value.get("database_file")
    if not isinstance(database_name, str) or Path(database_name).name != database_name:
        return None
    database = path.parent / database_name
    if database.is_symlink() or not database.is_file():
        return None
    try:
        if database.stat().st_size != value.get("database_bytes"):
            return None
        if _sha256_file(database) != value.get("database_sha256"):
            return None
        timestamp = datetime.fromisoformat(value["verified_at_utc"])
        if timestamp.tzinfo is None:
            return None
    except (OSError, KeyError, TypeError, ValueError):
        return None
    return timestamp.astimezone(UTC), database


def check_backup_freshness(
    destination: Path,
    *,
    expected_repository_id: str,
    expected_repository_key: str,
    expected_repository_root: str,
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
    now: datetime | None = None,
) -> BackupStatus:
    directory = _require_directory(destination, "NAS backup destination", writable=False)
    if type(max_age_seconds) is not int or max_age_seconds <= 0:
        raise ValueError("The backup age limit must be a positive number of seconds.")
    valid: list[datetime] = []
    invalid = 0
    for path in directory.glob("*.manifest.json"):
        result = _read_manifest_for_monitor(
            path,
            expected_repository_id=expected_repository_id,
            expected_repository_key=expected_repository_key,
            expected_repository_root=expected_repository_root,
        )
        if result is None:
            invalid += 1
        else:
            valid.append(result[0])
    if not valid:
        return BackupStatus(False, "No verified NAS backup is available.", None, None, invalid)
    newest = max(valid)
    age = ((now or datetime.now(UTC)).astimezone(UTC) - newest).total_seconds()
    if age < -300:
        return BackupStatus(False, "The newest verified NAS backup timestamp is unexpectedly in the future.", newest, age, invalid)
    if age >= max_age_seconds:
        return BackupStatus(
            False,
            f"The newest verified NAS backup is {age / 60:.1f} minutes old; limit is {max_age_seconds / 60:.0f} minutes.",
            newest,
            age,
            invalid,
        )
    note = f"; {invalid} invalid manifest(s) ignored" if invalid else ""
    return BackupStatus(
        True,
        f"Newest verified NAS backup age is {age / 60:.1f} minutes{note}.",
        newest,
        age,
        invalid,
    )


def _load_active_authority(configuration: BackupConfig) -> StoreAuthorityConfiguration | None:
    try:
        return load_store_authority(configuration.authority_state_root)
    except StoreAuthorityError as exc:
        raise BackupError(f"The managed InformationStore authority could not be verified: {exc}") from exc


def _validate_local_coverage_path(configuration: BackupConfig) -> Path:
    staging = _require_directory(
        configuration.staging, "Local backup staging directory", writable=False,
    )
    mountpoint = configuration.mountpoint.resolve(strict=True)
    coverage_path = configuration.coverage_path
    if (
        not coverage_path.is_absolute()
        or coverage_path.parent.resolve(strict=True) != staging
        or staging == mountpoint
        or staging.is_relative_to(mountpoint)
    ):
        raise BackupError("The managed recovery-point record must be a local direct child of backup staging.")
    return coverage_path


def _verify_managed_coverage(
    configuration: BackupConfig,
    authority_configuration: StoreAuthorityConfiguration,
) -> dict[str, Any]:
    try:
        store = InformationStore.open_managed_from_external_selection(
            authority_configuration,
            repository_id=configuration.repository_id,
            repository_key=configuration.repository_key,
            repository_root=configuration.repository_root,
        )
        binding = backup_guard.authority_binding(
            authority_configuration,
            store.database_identity(),
            repository_id=store.identity.repository_id,
            repository_key=store.identity.repository_key,
            repository_root=store.identity.repository_root,
            milestone_id=store.identity.milestone_id,
            prompt_path=store.identity.prompt_path,
            selection_id=store.identity.selection_id,
            selection_revision=store.identity.selection_revision,
        )
        record = backup_guard.read_recovery_point(
            _validate_local_coverage_path(configuration),
            binding,
            max_age_seconds=configuration.max_age_seconds,
        )
    except (InformationStoreError, StoreAuthorityError, backup_guard.BackupEvidenceError) as exc:
        raise BackupError(f"Managed SQLite backup coverage is unresolved: {exc}") from exc
    manifest_path = Path(record["manifest_path"])
    destination = _require_directory(configuration.destination, "NAS backup destination", writable=False)
    if (
        manifest_path.resolve(strict=True) != manifest_path
        or manifest_path.parent.resolve(strict=True) != destination
    ):
        raise BackupError("The recovery-point manifest is not a direct canonical file in the NAS destination.")
    verified = _read_manifest_for_monitor(
        manifest_path,
        expected_repository_id=configuration.repository_id,
        expected_repository_key=configuration.repository_key,
        expected_repository_root=configuration.repository_root,
    )
    if verified is None:
        raise BackupError("The recovery-point NAS manifest or database is no longer verifiable.")
    try:
        manifest = json.loads(_read_manifest_bytes(manifest_path))
    except (BackupError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupError(f"The recovery-point NAS manifest could not be re-read: {exc}") from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("manifest_sha256") != record["manifest_sha256"]
        or manifest.get("database_sha256") != record["database_sha256"]
        or manifest.get("snapshot_sha256") != record["snapshot_sha256"]
        or manifest.get("verified_at_utc") != record["verified_at_utc"]
        or verified[0].isoformat() != record["verified_at_utc"]
    ):
        raise BackupError("The recovery-point record no longer matches its verified NAS manifest.")
    return record


def _send_alert(hook: Path | None, target: str | None, message: str) -> None:
    if hook is None or not target:
        raise BackupError("No approved alert hook and destination are configured.")
    if not hook.is_absolute() or hook.is_symlink() or not hook.is_file() or not os.access(hook, os.X_OK):
        raise BackupError("The configured alert hook is unavailable or not executable.")
    result = subprocess.run(
        [str(hook), target],
        input=message,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )
    if result.returncode != 0:
        raise BackupError(f"The configured alert hook failed with exit status {result.returncode}.")


def config_from_environment(environment: Mapping[str, str] = os.environ) -> BackupConfig:
    required = (
        "REPOCTL_BACKUP_SOURCE",
        "REPOCTL_BACKUP_STAGING",
        "REPOCTL_BACKUP_DESTINATION",
        "REPOCTL_BACKUP_RESTORE_ROOT",
        "REPOCTL_BACKUP_MOUNTPOINT",
        "REPOCTL_BACKUP_MOUNT_SOURCE",
        "REPOCTL_BACKUP_REPOSITORY_ID",
        "REPOCTL_BACKUP_REPOSITORY_KEY",
        "REPOCTL_BACKUP_REPOSITORY_ROOT",
    )
    missing = [key for key in required if not environment.get(key)]
    if missing:
        raise BackupError(f"Required backup configuration is missing: {', '.join(missing)}.")
    try:
        max_age = int(environment.get("REPOCTL_BACKUP_MAX_AGE_SECONDS", str(DEFAULT_MAX_AGE_SECONDS)))
    except ValueError as exc:
        raise BackupError("REPOCTL_BACKUP_MAX_AGE_SECONDS must be an integer.") from exc
    if max_age <= 0:
        raise BackupError("REPOCTL_BACKUP_MAX_AGE_SECONDS must be positive.")
    hook = environment.get("REPOCTL_BACKUP_ALERT_HOOK") or None
    target = environment.get("REPOCTL_BACKUP_ALERT_TARGET") or None
    try:
        coverage_path = backup_guard.recovery_point_path(environment)
    except backup_guard.BackupEvidenceError as exc:
        raise BackupError(str(exc)) from exc
    authority_state_root = Path(
        environment.get("REPOCTL_BACKUP_AUTHORITY_STATE_ROOT", str(DEFAULT_STATE_ROOT))
    ).expanduser()
    if not authority_state_root.is_absolute():
        raise BackupError("REPOCTL_BACKUP_AUTHORITY_STATE_ROOT must be an absolute path.")
    return BackupConfig(
        source=Path(environment["REPOCTL_BACKUP_SOURCE"]),
        staging=Path(environment["REPOCTL_BACKUP_STAGING"]),
        destination=Path(environment["REPOCTL_BACKUP_DESTINATION"]),
        restore_root=Path(environment["REPOCTL_BACKUP_RESTORE_ROOT"]),
        mountpoint=Path(environment["REPOCTL_BACKUP_MOUNTPOINT"]),
        mount_source=environment["REPOCTL_BACKUP_MOUNT_SOURCE"],
        repository_id=environment["REPOCTL_BACKUP_REPOSITORY_ID"],
        repository_key=environment["REPOCTL_BACKUP_REPOSITORY_KEY"],
        repository_root=environment["REPOCTL_BACKUP_REPOSITORY_ROOT"],
        max_age_seconds=max_age,
        alert_hook=Path(hook) if hook else None,
        alert_target=target,
        authority_state_root=authority_state_root,
        coverage_path=coverage_path,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m repoctl.backup")
    parser.add_argument("operation", choices=("backup", "monitor", "restore"))
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--restore-name")
    arguments = parser.parse_args(argv)
    try:
        config = config_from_environment()
        if arguments.operation == "backup":
            verify_destination_mount(config.destination, config.mountpoint, config.mount_source)
            authority_configuration = _load_active_authority(config)
            coverage_path = None
            if authority_configuration is not None:
                coverage_path = _validate_local_coverage_path(config)
            result = create_verified_backup(
                config.source,
                config.staging,
                config.destination,
                expected_repository_id=config.repository_id,
                expected_repository_key=config.repository_key,
                expected_repository_root=config.repository_root,
                authority_configuration=authority_configuration,
                coverage_path=coverage_path,
            )
            print(json.dumps(result, sort_keys=True))
            return 0
        if arguments.operation == "monitor":
            mount_error: str | None = None
            try:
                verify_destination_mount(
                    config.destination, config.mountpoint, config.mount_source, writable=False,
                )
            except (BackupError, OSError) as exc:
                mount_error = f"NAS backup destination check failed: {exc}"
            if mount_error is not None:
                print(f"UNRESOLVED BACKUP WARNING: {mount_error}", file=sys.stderr)
                return 1
            try:
                status = check_backup_freshness(
                    config.destination,
                    expected_repository_id=config.repository_id,
                    expected_repository_key=config.repository_key,
                    expected_repository_root=config.repository_root,
                    max_age_seconds=config.max_age_seconds,
                )
            except (BackupError, OSError) as exc:
                print(f"UNRESOLVED BACKUP WARNING: NAS backup monitor failed: {exc}", file=sys.stderr)
                return 1
            if not status.healthy:
                print(f"UNRESOLVED BACKUP WARNING: {status.message}", file=sys.stderr)
                return 1
            authority_configuration = _load_active_authority(config)
            if authority_configuration is not None:
                try:
                    _verify_managed_coverage(config, authority_configuration)
                except (BackupError, OSError) as exc:
                    print(f"UNRESOLVED BACKUP WARNING: {exc}", file=sys.stderr)
                    return 1
            print(f"Backup coverage healthy: {status.message}")
            return 0
        if arguments.manifest is None or not arguments.restore_name:
            raise BackupError("Restore requires --manifest and --restore-name.")
        if Path(arguments.restore_name).name != arguments.restore_name:
            raise BackupError("Restore name must be a single safe filename.")
        restore_root = _require_directory(config.restore_root, "Separate restore directory")
        verify_destination_mount(config.destination, config.mountpoint, config.mount_source, writable=False)
        mountpoint = config.mountpoint.resolve(strict=True)
        if restore_root == mountpoint or restore_root.is_relative_to(mountpoint):
            raise BackupError("The restore directory must be outside the NAS backup mount.")
        if restore_root.stat().st_dev == config.destination.resolve(strict=True).stat().st_dev:
            raise BackupError("The restore directory must be on a different filesystem from the NAS backup.")
        restored = restore_verified_backup(
            arguments.manifest,
            restore_root / arguments.restore_name,
            restore_root,
            expected_repository_id=config.repository_id,
            expected_repository_key=config.repository_key,
            expected_repository_root=config.repository_root,
            authority_configuration=_load_active_authority(config),
        )
        print(f"Restored verified backup to separate path: {restored}")
        return 0
    except (
        BackupError,
        backup_guard.BackupEvidenceError,
        StoreAuthorityError,
        OSError,
        sqlite3.Error,
        subprocess.TimeoutExpired,
    ) as exc:
        print(f"repoctl backup {arguments.operation} failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
