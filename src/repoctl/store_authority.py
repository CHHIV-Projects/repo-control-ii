from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .scanner.core import StateArtifactLockError, state_artifact_lock


_CONFIG_NAME = "information-store-authority.json"
_MAX_CONFIG_BYTES = 16_384


class StoreAuthorityError(RuntimeError):
    pass


@dataclass(frozen=True)
class StoreAuthorityConfiguration:
    state_root: Path
    database_path: Path
    registry_path: Path
    registry_sha256: str
    generation: str
    configuration_sha256: str
    guard_sha256: str


def authority_binding_digest(binding: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(binding, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _read_bounded_file(root: Path, name: str, *, max_bytes: int = _MAX_CONFIG_BYTES) -> bytes | None:
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        except FileNotFoundError:
            return None
        with os.fdopen(descriptor, "rb") as source:
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
                raise StoreAuthorityError(f"Authority file {name!r} is not a bounded regular file.")
            content = source.read(max_bytes + 1)
            after = os.fstat(source.fileno())
            named = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if (
            len(content) > max_bytes
            or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
            or (before.st_dev, before.st_ino) != (named.st_dev, named.st_ino)
        ):
            raise StoreAuthorityError(f"Authority file {name!r} changed while being read.")
        return content
    finally:
        os.close(directory)


def _decode_object(content: bytes, name: str) -> dict[str, Any]:
    def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise StoreAuthorityError(f"Authority file {name!r} contains a duplicate key.")
            value[key] = item
        return value

    try:
        value = json.loads(content, object_pairs_hook=unique_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StoreAuthorityError(f"Authority file {name!r} is malformed.") from exc
    if not isinstance(value, dict):
        raise StoreAuthorityError(f"Authority file {name!r} must contain an object.")
    return value


def load_store_authority(state_root: str | Path) -> StoreAuthorityConfiguration | None:
    root = Path(state_root).expanduser().absolute()
    if not root.name:
        raise StoreAuthorityError("The filesystem root cannot be an InformationStore state root.")
    guard_name = f".{root.name}.information-store-authority.guard"
    try:
        metadata = root.lstat()
    except FileNotFoundError:
        try:
            if root.parent.is_dir() and _read_bounded_file(root.parent, guard_name) is not None:
                raise StoreAuthorityError("The guarded InformationStore state directory is missing.")
        except OSError as exc:
            raise StoreAuthorityError(f"Cannot safely read the InformationStore guard: {exc}") from exc
        return None
    except OSError as exc:
        raise StoreAuthorityError(f"Cannot inspect the authority state directory: {exc}") from exc
    if not stat.S_ISDIR(metadata.st_mode) or root.is_symlink():
        raise StoreAuthorityError("The authority state root must be a real directory.")
    root = root.resolve(strict=True)
    try:
        configuration = _read_bounded_file(root, _CONFIG_NAME)
        guard = _read_bounded_file(root.parent, guard_name)
        if configuration is None and guard is None:
            return None
        with state_artifact_lock(root):
            configuration = _read_bounded_file(root, _CONFIG_NAME)
            guard = _read_bounded_file(root.parent, guard_name)
    except (OSError, StateArtifactLockError) as exc:
        raise StoreAuthorityError(f"Cannot safely read InformationStore authority state: {exc}") from exc
    if configuration is None or guard is None:
        raise StoreAuthorityError("InformationStore authority configuration is incomplete; legacy fallback is disabled.")
    payload = _decode_object(configuration, _CONFIG_NAME)
    guard_payload = _decode_object(guard, guard_name)
    digest = hashlib.sha256(configuration).hexdigest()
    if (
        set(payload) != {
            "schema_version", "authority", "database_path", "registry_path", "registry_sha256", "generation",
        }
        or type(payload.get("schema_version")) is not int
        or payload.get("schema_version") != 1
        or payload.get("authority") != "sqlite"
        or not isinstance(payload.get("database_path"), str)
        or not Path(payload["database_path"]).is_absolute()
        or "\x00" in payload["database_path"]
        or not isinstance(payload.get("registry_path"), str)
        or not Path(payload["registry_path"]).is_absolute()
        or "\x00" in payload["registry_path"]
        or not isinstance(payload.get("registry_sha256"), str)
        or not re.fullmatch(r"[a-f0-9]{64}", payload["registry_sha256"])
        or not isinstance(payload.get("generation"), str)
        or not re.fullmatch(r"[a-f0-9]{32}", payload["generation"])
        or type(guard_payload.get("schema_version")) is not int
        or guard_payload != {"schema_version": 1, "configuration_sha256": digest}
    ):
        raise StoreAuthorityError("InformationStore authority configuration or its guard is invalid.")
    return StoreAuthorityConfiguration(
        state_root=root,
        database_path=Path(payload["database_path"]),
        registry_path=Path(payload["registry_path"]),
        registry_sha256=payload["registry_sha256"],
        generation=payload["generation"],
        configuration_sha256=digest,
        guard_sha256=hashlib.sha256(guard).hexdigest(),
    )


def verify_store_registry(configuration: StoreAuthorityConfiguration) -> None:
    path = configuration.registry_path
    try:
        if path.is_symlink() or path.resolve(strict=True) != path:
            raise StoreAuthorityError("The configured registry path is not a canonical regular file.")
        content = _read_bounded_file(path.parent, path.name, max_bytes=5_000_000)
    except OSError as exc:
        raise StoreAuthorityError(f"Cannot safely read the configured registry: {exc}") from exc
    if content is None or hashlib.sha256(content).hexdigest() != configuration.registry_sha256:
        raise StoreAuthorityError("The configured registry changed or is unavailable; reload is required.")


def _write_new_authority_file(root: Path, name: str, content: bytes) -> None:
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        with os.fdopen(descriptor, "wb") as target:
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
        os.fsync(directory)
    finally:
        os.close(directory)


def _remove_authority_file(root: Path, name: str, expected: bytes) -> None:
    actual = _read_bounded_file(root, name)
    if actual is None:
        return
    if actual != expected:
        raise StoreAuthorityError(f"Authority file {name!r} changed; recovery was refused.")
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.unlink(name, dir_fd=directory)
        os.fsync(directory)
    finally:
        os.close(directory)


def _prepared_data_digest(connection: sqlite3.Connection, tables: set[str]) -> str:
    digest = hashlib.sha256()
    for table in sorted(tables):
        digest.update((table + "\n").encode())
        query = f'SELECT * FROM "{table}"'
        if table == "store_metadata":
            query += " WHERE key NOT LIKE 'authority_%'"
        query += " ORDER BY rowid"
        for row in connection.execute(query):
            values = [
                {"bytes_sha256": hashlib.sha256(value).hexdigest(), "size": len(value)}
                if isinstance(value, bytes) else value
                for value in row
            ]
            digest.update(json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            digest.update(b"\n")
    return digest.hexdigest()


def _prepared_activation_plan(
    database_path: str | Path,
    state_root: str | Path,
    registry_path: str | Path,
    generation: str,
) -> tuple[dict[str, Any], bytes, bytes, dict[str, str]]:
    from .information_store import (
        _EXPECTED_TABLES,
        _read_fixture_selection_snapshot,
        _validate_database_path,
        _validate_selection_root,
        InformationStore,
        sqlite_runtime_status,
    )
    from .migration_rehearsal import MigrationRehearsalError, verify_prepared_source_parity
    from .web.repository_registry import RepositoryRegistry

    if not isinstance(generation, str) or not re.fullmatch(r"[a-f0-9]{32}", generation):
        raise StoreAuthorityError("Activation generation must be an explicit 32-character lowercase hex value.")
    runtime = sqlite_runtime_status()
    if not runtime["production_eligible"]:
        raise StoreAuthorityError("The linked SQLite runtime is not eligible for managed authority.")
    root = Path(state_root).expanduser().absolute()
    registry_file = Path(registry_path).expanduser().absolute()
    database = Path(database_path).expanduser().absolute()
    if (
        not root.is_dir() or root.is_symlink() or root.resolve(strict=True) != root
        or registry_file.is_symlink() or registry_file.resolve(strict=True) != registry_file
        or database.is_symlink() or database.resolve(strict=True) != database
    ):
        raise StoreAuthorityError("Activation paths must be existing canonical, non-symlink paths.")
    registry_bytes = _read_bounded_file(registry_file.parent, registry_file.name, max_bytes=5_000_000)
    if registry_bytes is None:
        raise StoreAuthorityError("The exact registry is unavailable.")
    registry_hash = hashlib.sha256(registry_bytes).hexdigest()
    registry = RepositoryRegistry.from_toml(registry_file)
    entries = {entry.repository_id: entry for entry in registry.entries}
    if not entries or len(entries) != len(registry.entries):
        raise StoreAuthorityError("The registry has no unique repository identities.")
    for entry in entries.values():
        _validate_selection_root(root, str(entry.path), fixture=False)
        if _validate_database_path(database, str(entry.path)) != database:
            raise StoreAuthorityError("The prepared database is not canonical.")
    metadata = database.stat(follow_symlinks=False)
    if not stat.S_ISREG(metadata.st_mode):
        raise StoreAuthorityError("The prepared database must be a regular file.")
    database_identity = hashlib.sha256(f"{database}:{metadata.st_dev}:{metadata.st_ino}".encode()).hexdigest()
    connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN")
        InformationStore._verify_schema(connection, creation_mode="managed_prepared")
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise StoreAuthorityError("The prepared database failed integrity_check.")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise StoreAuthorityError("The prepared database failed foreign_key_check.")
        rows = connection.execute(
            """SELECT r.repository_id, r.repository_key, r.repository_root, s.milestone_id,
                      m.prompt_path, s.selection_id, s.selection_revision
               FROM repository r JOIN repository_selection s USING (repository_id)
               JOIN milestone m ON m.repository_id = s.repository_id AND m.milestone_id = s.milestone_id"""
        ).fetchall()
        if {row["repository_id"] for row in rows} != set(entries) or len(rows) != len(entries):
            raise StoreAuthorityError("Every registered repository requires one imported selection.")
        selections: list[dict[str, Any]] = []
        for row in rows:
            entry = entries[row["repository_id"]]
            if row["repository_key"] != entry.key or row["repository_root"] != str(entry.path):
                raise StoreAuthorityError("A prepared repository identity differs from the registry.")
            pointer, pointer_hash = _read_fixture_selection_snapshot(root, entry.repository_id)
            fields = ("repository_id", "milestone_id", "prompt_path", "selection_id", "selection_revision")
            if any(pointer.get(key) != row[key] for key in fields):
                raise StoreAuthorityError("A prepared selection does not match its exact external pointer.")
            prompt = connection.execute(
                """SELECT count(*) FROM document
                   WHERE repository_id = ? AND milestone_id = ? AND logical_type = 'PROMPT'
                     AND source_path = ? AND draft_revision_id IS NOT NULL""",
                (entry.repository_id, row["milestone_id"], row["prompt_path"]),
            ).fetchone()[0]
            if prompt != 1:
                raise StoreAuthorityError("Every selected milestone requires one imported prompt revision.")
            selections.append({**{key: row[key] for key in fields}, "pointer_sha256": pointer_hash})
        missing_imports = connection.execute(
            """SELECT count(*) FROM milestone m WHERE NOT EXISTS (
                   SELECT 1 FROM import_source s
                   WHERE s.repository_id = m.repository_id AND s.milestone_id = m.milestone_id
                     AND s.source_kind = 'prompt' AND s.source_path = m.prompt_path
               )"""
        ).fetchone()[0]
        if missing_imports:
            raise StoreAuthorityError("The prepared store contains milestones without exact imported prompt sources.")
        try:
            parity = verify_prepared_source_parity(
                registry,
                root,
                {entries[row["repository_id"]].key: row["milestone_id"] for row in rows},
                connection,
            )
        except MigrationRehearsalError as exc:
            raise StoreAuthorityError(f"Prepared source parity is not verified: {exc}") from exc
        data_hash = _prepared_data_digest(connection, _EXPECTED_TABLES)
        source_count = connection.execute("SELECT count(*) FROM import_source").fetchone()[0]
        markers = dict(connection.execute(
            "SELECT key, value FROM store_metadata WHERE key LIKE 'authority_%'"
        ).fetchall())
        connection.commit()
    finally:
        connection.close()
    current = database.stat(follow_symlinks=False)
    if (metadata.st_dev, metadata.st_ino) != (current.st_dev, current.st_ino):
        raise StoreAuthorityError("The prepared database was replaced during preflight.")
    payload = {
        "schema_version": 1,
        "authority": "sqlite",
        "database_path": str(database),
        "registry_path": str(registry_file),
        "registry_sha256": registry_hash,
        "generation": generation,
    }
    config = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
    configuration_hash = hashlib.sha256(config).hexdigest()
    guard = json.dumps(
        {"schema_version": 1, "configuration_sha256": configuration_hash},
        sort_keys=True, separators=(",", ":"),
    ).encode("utf-8") + b"\n"
    plan = {
        **payload,
        "state_root": str(root),
        "database_identity": database_identity,
        "prepared_data_sha256": data_hash,
        "configuration_sha256": configuration_hash,
        "selections": sorted(selections, key=lambda value: value["repository_id"]),
        "import_source_count": source_count,
        "source_inventory_sha256": parity["source_inventory_sha256"],
    }
    plan["plan_sha256"] = authority_binding_digest(plan)
    return plan, config, guard, markers


def preview_prepared_authority(
    database_path: str | Path,
    state_root: str | Path,
    registry_path: str | Path,
    generation: str,
) -> dict[str, Any]:
    root = Path(state_root).expanduser().absolute()
    with state_artifact_lock(root):
        if load_store_authority(root) is not None:
            raise StoreAuthorityError("An InformationStore authority is already active.")
        plan, _, _, markers = _prepared_activation_plan(database_path, root, registry_path, generation)
        if markers:
            raise StoreAuthorityError("The prepared database already has an activation marker.")
        return plan


def activate_prepared_authority(
    database_path: str | Path,
    state_root: str | Path,
    registry_path: str | Path,
    generation: str,
    *,
    expected_plan_sha256: str,
    po_confirmed: bool,
    writers_quiesced: bool,
    source_parity_confirmed: bool,
) -> StoreAuthorityConfiguration:
    if po_confirmed is not True or writers_quiesced is not True or source_parity_confirmed is not True:
        raise StoreAuthorityError("Activation requires explicit PO, writer-quiescence, and source-parity confirmation.")
    root = Path(state_root).expanduser().absolute()
    guard_name = f".{root.name}.information-store-authority.guard"
    with state_artifact_lock(root):
        plan, config, guard, markers = _prepared_activation_plan(database_path, root, registry_path, generation)
        if plan["plan_sha256"] != expected_plan_sha256:
            raise StoreAuthorityError("The activation preflight changed; review and retry.")
        expected_markers = {
            "authority_generation": generation,
            "authority_configuration_sha256": plan["configuration_sha256"],
            "authority_state_root": str(root),
            "authority_first_write": "0",
        }
        existing_guard = _read_bounded_file(root.parent, guard_name)
        existing_config = _read_bounded_file(root, _CONFIG_NAME)
        if existing_guard not in (None, guard) or existing_config not in (None, config):
            raise StoreAuthorityError("Existing authority files do not match the reviewed activation plan.")
        if existing_config is not None and existing_guard is None:
            raise StoreAuthorityError("Activation configuration exists without its guard.")
        if markers not in ({}, expected_markers):
            raise StoreAuthorityError("The activation marker differs or a managed write has occurred.")
        if existing_guard is None:
            _write_new_authority_file(root.parent, guard_name, guard)
        if not markers:
            from .information_store import _EXPECTED_TABLES

            connection = sqlite3.connect(f"{Path(plan['database_path']).as_uri()}?mode=rw", uri=True)
            try:
                connection.execute("BEGIN IMMEDIATE")
                if _prepared_data_digest(connection, _EXPECTED_TABLES) != plan["prepared_data_sha256"]:
                    raise StoreAuthorityError("Prepared data changed during activation; the guard remains in place.")
                connection.executemany("INSERT INTO store_metadata(key, value) VALUES (?, ?)", expected_markers.items())
                connection.commit()
            finally:
                connection.close()
        if existing_config is None:
            _write_new_authority_file(root, _CONFIG_NAME, config)
        configuration = load_store_authority(root)
        if configuration is None:
            raise StoreAuthorityError("Authority activation did not produce a readable configuration.")
        verify_store_registry(configuration)
        return configuration


def abort_prepared_authority(
    database_path: str | Path,
    state_root: str | Path,
    registry_path: str | Path,
    generation: str,
    *,
    expected_plan_sha256: str,
    po_confirmed: bool,
    writers_quiesced: bool,
) -> None:
    if po_confirmed is not True or writers_quiesced is not True:
        raise StoreAuthorityError("Abort requires explicit PO confirmation and quiesced writers.")
    root = Path(state_root).expanduser().absolute()
    guard_name = f".{root.name}.information-store-authority.guard"
    with state_artifact_lock(root):
        from .information_store import InformationStore

        database = Path(database_path).expanduser().absolute()
        if database.is_symlink() or database.resolve(strict=True) != database or not database.is_file():
            raise StoreAuthorityError("Abort requires the canonical prepared database.")
        connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            InformationStore._verify_schema(connection, creation_mode="managed_prepared")
            first_write = connection.execute(
                "SELECT value FROM store_metadata WHERE key = 'authority_first_write'"
            ).fetchone()
        finally:
            connection.close()
        if first_write is not None and first_write[0] == "1":
            raise StoreAuthorityError("A managed write occurred; recover SQLite authority instead of aborting.")
        plan, config, guard, markers = _prepared_activation_plan(database_path, root, registry_path, generation)
        if plan["plan_sha256"] != expected_plan_sha256:
            raise StoreAuthorityError("Prepared data or external selection changed; abort is unsafe.")
        expected_markers = {
            "authority_generation": generation,
            "authority_configuration_sha256": plan["configuration_sha256"],
            "authority_state_root": str(root),
            "authority_first_write": "0",
        }
        if markers not in ({}, expected_markers):
            raise StoreAuthorityError("A managed write occurred or the activation marker changed; recover SQLite authority.")
        if _read_bounded_file(root.parent, guard_name) != guard:
            raise StoreAuthorityError("The expected activation guard is missing or changed.")
        _remove_authority_file(root, _CONFIG_NAME, config)
        if markers:
            connection = sqlite3.connect(f"{Path(plan['database_path']).as_uri()}?mode=rw", uri=True)
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.executemany("DELETE FROM store_metadata WHERE key = ?", [(key,) for key in markers])
                connection.commit()
            finally:
                connection.close()
        _remove_authority_file(root.parent, guard_name, guard)
