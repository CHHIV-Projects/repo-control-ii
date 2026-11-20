from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
import re
import stat
from typing import Any


DEFAULT_MANAGED_CLI_BINDING_DIRECTORY = Path("/etc/repo-control/managed-authority.d")
MAX_MANAGED_CLI_BINDING_BYTES = 16_384
_SHA256 = re.compile(r"[a-f0-9]{64}\Z")
_GENERATION = re.compile(r"[a-f0-9]{32}\Z")


class ManagedCliBindingError(RuntimeError):
    pass


@dataclass(frozen=True)
class ManagedCliBinding:
    state_root: Path
    backup_coverage_path: Path
    registry_path: Path
    authority_configuration_sha256: str
    authority_generation: str
    database_identity: str
    source_path: Path


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ManagedCliBindingError(f"Managed CLI binding contains duplicate key {key!r}.")
        value[key] = item
    return value


def _absolute_path(value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value or not Path(value).is_absolute():
        raise ManagedCliBindingError(f"Managed CLI binding {label} must be an absolute path.")
    return Path(value)


def load_managed_cli_binding(
    directory: str | Path | None = None,
    *,
    required_owner_uid: int = 0,
) -> ManagedCliBinding | None:
    root = Path(directory or DEFAULT_MANAGED_CLI_BINDING_DIRECTORY)
    try:
        root_metadata = root.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ManagedCliBindingError(f"Cannot inspect managed CLI binding directory: {exc}") from exc
    if (
        not stat.S_ISDIR(root_metadata.st_mode)
        or root_metadata.st_uid != required_owner_uid
        or stat.S_IMODE(root_metadata.st_mode) & 0o022
    ):
        raise ManagedCliBindingError(
            "Managed CLI binding directory must be a non-writable root-controlled real directory."
        )
    try:
        directory_descriptor = os.open(
            root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
    except OSError as exc:
        raise ManagedCliBindingError(f"Cannot safely open managed CLI binding directory: {exc}") from exc
    try:
        opened_root = os.fstat(directory_descriptor)
        if (
            (opened_root.st_dev, opened_root.st_ino) != (root_metadata.st_dev, root_metadata.st_ino)
            or not stat.S_ISDIR(opened_root.st_mode)
            or opened_root.st_uid != required_owner_uid
            or stat.S_IMODE(opened_root.st_mode) & 0o022
        ):
            raise ManagedCliBindingError("Managed CLI binding directory changed while being opened.")
        try:
            names = sorted(name for name in os.listdir(directory_descriptor) if name.endswith(".json"))
        except OSError as exc:
            raise ManagedCliBindingError(f"Cannot list managed CLI binding directory: {exc}") from exc
        if not names:
            raise ManagedCliBindingError("Managed CLI binding directory is present but contains no binding.")
        if len(names) != 1:
            raise ManagedCliBindingError("Managed CLI binding is ambiguous; exactly one JSON binding is required.")
        name = names[0]
        try:
            descriptor = os.open(name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=directory_descriptor)
        except OSError as exc:
            raise ManagedCliBindingError(f"Cannot safely open managed CLI binding: {exc}") from exc
        with os.fdopen(descriptor, "rb") as source:
            before = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid != required_owner_uid
                or stat.S_IMODE(before.st_mode) & 0o022
                or before.st_size > MAX_MANAGED_CLI_BINDING_BYTES
            ):
                raise ManagedCliBindingError(
                    "Managed CLI binding must be a bounded non-writable root-controlled regular file."
                )
            content = source.read(MAX_MANAGED_CLI_BINDING_BYTES + 1)
            after = os.fstat(source.fileno())
            try:
                named = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
            except OSError as exc:
                raise ManagedCliBindingError(f"Cannot recheck managed CLI binding: {exc}") from exc
        if (
            len(content) > MAX_MANAGED_CLI_BINDING_BYTES
            or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
            or (before.st_dev, before.st_ino) != (named.st_dev, named.st_ino)
        ):
            raise ManagedCliBindingError("Managed CLI binding changed while being read.")
    finally:
        os.close(directory_descriptor)
    try:
        payload = json.loads(content, object_pairs_hook=_unique_object)
    except ManagedCliBindingError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManagedCliBindingError("Managed CLI binding is malformed.") from exc
    required_keys = {
        "schema_version", "authority", "state_root", "backup_coverage_path", "registry_path",
        "authority_configuration_sha256", "authority_generation", "database_identity",
    }
    if not isinstance(payload, dict) or set(payload) != required_keys:
        raise ManagedCliBindingError("Managed CLI binding has an unsupported shape.")
    if payload.get("schema_version") != 1 or payload.get("authority") != "sqlite_managed":
        raise ManagedCliBindingError("Managed CLI binding has an unsupported version or authority.")
    configuration_sha256 = payload.get("authority_configuration_sha256")
    generation = payload.get("authority_generation")
    database_identity = payload.get("database_identity")
    if (
        not isinstance(configuration_sha256, str)
        or not _SHA256.fullmatch(configuration_sha256)
        or not isinstance(generation, str)
        or not _GENERATION.fullmatch(generation)
        or not isinstance(database_identity, str)
        or not _SHA256.fullmatch(database_identity)
    ):
        raise ManagedCliBindingError("Managed CLI binding identity values are malformed.")
    return ManagedCliBinding(
        state_root=_absolute_path(payload["state_root"], "state root"),
        backup_coverage_path=_absolute_path(payload["backup_coverage_path"], "backup coverage path"),
        registry_path=_absolute_path(payload["registry_path"], "registry path"),
        authority_configuration_sha256=configuration_sha256,
        authority_generation=generation,
        database_identity=database_identity,
        source_path=root / name,
    )
