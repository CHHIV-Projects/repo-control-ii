from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
import stat
import tempfile
from pathlib import Path
import re
from typing import Any, Mapping

from .store_authority import StoreAuthorityConfiguration


WRITE_GUARD_MAX_AGE_SECONDS = 45 * 60
RECOVERY_POINT_VERSION = 1
MAX_RECOVERY_POINT_BYTES = 16 * 1024
DEFAULT_RECOVERY_POINT_PATH = (
    Path.home() / ".local/state/repo-control-backup/staging/.repoctl-verified-recovery-point.json"
)
_SHA256 = re.compile(r"[a-f0-9]{64}\Z")


class BackupEvidenceError(RuntimeError):
    pass


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")


def utc_now() -> datetime:
    return datetime.now(UTC)


def recovery_point_path(environment: Mapping[str, str] = os.environ) -> Path:
    configured = environment.get("REPOCTL_BACKUP_COVERAGE_PATH")
    path = Path(configured).expanduser() if configured else DEFAULT_RECOVERY_POINT_PATH
    if not path.is_absolute() or path.parent.resolve(strict=False) != path.parent:
        raise BackupEvidenceError("The backup recovery-point path must be absolute and canonical.")
    return path


def authority_binding(
    configuration: StoreAuthorityConfiguration,
    database_identity: str,
    *,
    repository_id: str,
    repository_key: str,
    repository_root: str,
    milestone_id: str,
    prompt_path: str,
    selection_id: str,
    selection_revision: int,
) -> dict[str, Any]:
    return {
        "authority": {
            "configuration_sha256": configuration.configuration_sha256,
            "database_path": str(configuration.database_path),
            "generation": configuration.generation,
            "registry_path": str(configuration.registry_path),
            "registry_sha256": configuration.registry_sha256,
            "state_root": str(configuration.state_root),
        },
        "database_identity": database_identity,
        "repository": {
            "repository_id": repository_id,
            "repository_key": repository_key,
            "repository_root": repository_root,
        },
        "selection": {
            "milestone_id": milestone_id,
            "prompt_path": prompt_path,
            "selection_id": selection_id,
            "selection_revision": selection_revision,
        },
    }


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise BackupEvidenceError("The backup recovery-point record contains a duplicate key.")
        value[key] = item
    return value


def _read_record(path: Path) -> dict[str, Any]:
    if not path.is_absolute() or path.parent.resolve(strict=False) != path.parent:
        raise BackupEvidenceError("The backup recovery-point path is not absolute and canonical.")
    try:
        parent = path.parent.stat(follow_symlinks=False)
        if (
            not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid != os.geteuid()
            or stat.S_IMODE(parent.st_mode) & 0o077
        ):
            raise BackupEvidenceError("The backup recovery-point directory is not private and service-owned.")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError as exc:
        raise BackupEvidenceError("No verified recovery-point evidence is available.") from exc
    except OSError as exc:
        raise BackupEvidenceError(f"The backup recovery-point record could not be opened safely: {exc}") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) & 0o077
            or before.st_size > MAX_RECOVERY_POINT_BYTES
        ):
            raise BackupEvidenceError("The backup recovery-point record is not a private bounded regular file.")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            content = stream.read(MAX_RECOVERY_POINT_BYTES + 1)
        after = os.fstat(descriptor)
        named = path.stat(follow_symlinks=False)
        if (
            len(content) > MAX_RECOVERY_POINT_BYTES
            or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
            or (before.st_dev, before.st_ino) != (named.st_dev, named.st_ino)
        ):
            raise BackupEvidenceError("The backup recovery-point record changed while being read.")
    except OSError as exc:
        raise BackupEvidenceError(f"The backup recovery-point record could not be read safely: {exc}") from exc
    finally:
        os.close(descriptor)
    try:
        value = json.loads(content, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupEvidenceError("The backup recovery-point record is malformed.") from exc
    if not isinstance(value, dict):
        raise BackupEvidenceError("The backup recovery-point record must contain an object.")
    return value


def _verified_timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise BackupEvidenceError("The backup recovery-point timestamp is missing or invalid.")
    try:
        timestamp = datetime.fromisoformat(value)
    except ValueError as exc:
        raise BackupEvidenceError("The backup recovery-point timestamp is malformed.") from exc
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise BackupEvidenceError("The backup recovery-point timestamp has no timezone.")
    return timestamp.astimezone(UTC)


def read_recovery_point(
    path: Path,
    expected_binding: Mapping[str, Any],
    *,
    max_age_seconds: int = WRITE_GUARD_MAX_AGE_SECONDS,
    now: datetime | None = None,
) -> dict[str, Any]:
    if type(max_age_seconds) is not int or max_age_seconds <= 0:
        raise ValueError("The backup recovery-point age limit must be a positive number of seconds.")
    record = _read_record(path)
    expected_keys = {
        "authority_binding",
        "database_sha256",
        "manifest_path",
        "manifest_sha256",
        "record_sha256",
        "record_version",
        "snapshot_sha256",
        "verified_at_utc",
    }
    if set(record) != expected_keys or record.get("record_version") != RECOVERY_POINT_VERSION:
        raise BackupEvidenceError("The backup recovery-point record has an unsupported shape or version.")
    claimed_digest = record.get("record_sha256")
    unsigned = {key: value for key, value in record.items() if key != "record_sha256"}
    if not isinstance(claimed_digest, str) or hashlib.sha256(canonical_json(unsigned)).hexdigest() != claimed_digest:
        raise BackupEvidenceError("The backup recovery-point record checksum does not match.")
    for field in ("database_sha256", "manifest_sha256", "snapshot_sha256"):
        if not isinstance(record.get(field), str) or not _SHA256.fullmatch(record[field]):
            raise BackupEvidenceError(f"The backup recovery-point {field} is invalid.")
    if (
        not isinstance(record.get("manifest_path"), str)
        or not Path(record["manifest_path"]).is_absolute()
        or "\x00" in record["manifest_path"]
    ):
        raise BackupEvidenceError("The backup recovery-point manifest path is invalid.")
    if record.get("authority_binding") != dict(expected_binding):
        raise BackupEvidenceError("The verified backup belongs to a different database or authority binding.")
    verified_at = _verified_timestamp(record.get("verified_at_utc"))
    try:
        current = now if now is not None else utc_now()
    except (OSError, OverflowError, ValueError) as exc:
        raise BackupEvidenceError(f"The system clock could not be read safely: {exc}") from exc
    if current.tzinfo is None or current.utcoffset() is None:
        raise BackupEvidenceError("The system clock returned a timestamp without a timezone.")
    age = (current.astimezone(UTC) - verified_at).total_seconds()
    if age < 0:
        raise BackupEvidenceError("The verified backup timestamp is in the future.")
    if age >= max_age_seconds:
        raise BackupEvidenceError(
            f"The latest verified recovery point is {age / 60:.1f} minutes old; "
            f"managed writes pause at {max_age_seconds / 60:.0f} minutes."
        )
    return record


def write_recovery_point(
    path: Path,
    *,
    manifest_path: Path,
    manifest: Mapping[str, Any],
    binding: Mapping[str, Any],
) -> dict[str, Any]:
    verified_at = _verified_timestamp(manifest.get("verified_at_utc"))
    if (
        manifest.get("verified") is not True
        or manifest.get("integrity_check") != "ok"
        or manifest.get("foreign_key_violations") != 0
    ):
        raise BackupEvidenceError("Only a fully verified NAS backup can create recovery-point evidence.")
    database_sha256 = manifest.get("database_sha256")
    snapshot_sha256 = manifest.get("snapshot_sha256")
    manifest_sha256 = manifest.get("manifest_sha256")
    if any(
        not isinstance(digest, str) or not _SHA256.fullmatch(digest)
        for digest in (database_sha256, snapshot_sha256, manifest_sha256)
    ):
        raise BackupEvidenceError("The verified NAS manifest contains an invalid digest.")
    if not manifest_path.is_absolute() or manifest_path.parent.resolve(strict=True) != manifest_path.parent:
        raise BackupEvidenceError("The verified NAS manifest path is not absolute and canonical.")
    body = {
        "authority_binding": dict(binding),
        "database_sha256": database_sha256,
        "manifest_path": str(manifest_path),
        "manifest_sha256": manifest_sha256,
        "record_version": RECOVERY_POINT_VERSION,
        "snapshot_sha256": snapshot_sha256,
        "verified_at_utc": verified_at.isoformat(),
    }
    record = {**body, "record_sha256": hashlib.sha256(canonical_json(body)).hexdigest()}
    _write_record(path, record)
    return record


def _write_record(path: Path, record: Mapping[str, Any]) -> None:
    if not path.is_absolute() or path.parent.resolve(strict=True) != path.parent:
        raise BackupEvidenceError("The backup recovery-point directory is unavailable or not canonical.")
    try:
        parent = path.parent.stat(follow_symlinks=False)
        if (
            not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid != os.geteuid()
            or stat.S_IMODE(parent.st_mode) & 0o077
        ):
            raise BackupEvidenceError("The backup recovery-point directory is not private and service-owned.")
        descriptor, temporary_name = tempfile.mkstemp(prefix=".repoctl-recovery-point-", dir=path.parent)
    except OSError as exc:
        raise BackupEvidenceError(f"The backup recovery-point directory could not be prepared: {exc}") from exc
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(canonical_json(record) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        directory_descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except OSError as exc:
        raise BackupEvidenceError(f"The backup recovery-point record could not be persisted: {exc}") from exc
    finally:
        os.close(descriptor)
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
