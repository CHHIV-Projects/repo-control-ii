#!/usr/bin/env python3
"""Hash-guarded rollback/reapply for only the M010.031 recovery-authority correction."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sys
import tempfile


REPOSITORY = Path(__file__).resolve().parents[2]

STATES = {
    "prior": {
        "src/repoctl/backup_guard.py": "9be24b61bf469aa0af0f167572f69bf47a047c3af4d612cd6afd2e3796918915",
        "src/repoctl/information_store.py": "559c67a064d85a345b8a8cd58415bdf98a72dd4b9851cf53456bd662266a1804",
        "src/repoctl/web/app.py": "80105768710408e7d9207ed7b10cf8d5e910e64bde34b9e026fa3a66d46f3756",
        "src/repoctl/workflow/information_store_fixture.py": "5de425f9330ec1c39fdfc211e52de0830bdbdf2e5009384cecc06b75831ce4e0",
    },
    "corrected": {
        "src/repoctl/backup_guard.py": "fef8da20c4f6e0afac86cbde45da7123346676ad785047ecefab9d2a0a9f3ba4",
        "src/repoctl/information_store.py": "110b8e0e60031f4b654ec056e2a93c8e18bc740135ed36ec7bdcec6f41227bb5",
        "src/repoctl/web/app.py": "26a85cf8b371afcac7fbdf781653d67b64debb921c2f6e5b21035b99fb1cfe1f",
        "src/repoctl/workflow/information_store_fixture.py": "a4f6758382ba7dcc72b0dac4aa0209b4bafe4f88388c49245071343c9fb4955b",
    },
}

REPLACEMENTS = {
    "src/repoctl/backup_guard.py": [(
        '''    recorded_binding = record.get("authority_binding")
    expected_binding = dict(expected_binding)
    binding_keys = {"authority", "database_identity", "repository", "selection"}
    selection_keys = {"milestone_id", "prompt_path", "selection_id", "selection_revision"}
    if (
        not isinstance(recorded_binding, dict)
        or set(recorded_binding) != binding_keys
        or set(expected_binding) != binding_keys
        or not isinstance(recorded_binding.get("selection"), dict)
        or not isinstance(expected_binding.get("selection"), dict)
        or set(recorded_binding["selection"]) != selection_keys
        or set(expected_binding["selection"]) != selection_keys
    ):
        raise BackupEvidenceError("The verified backup authority binding has an unsupported shape.")
    # Selection is required evidence, but it is a mutable row within this same
    # database/repository authority. A normal selection write must not make the
    # still-fresh recovery point unusable for the immediately following
    # workflow write. Database identity, authority configuration, and
    # repository binding remain fail-closed.
    stable_recorded = {key: recorded_binding[key] for key in binding_keys - {"selection"}}
    stable_expected = {key: expected_binding[key] for key in binding_keys - {"selection"}}
''',
        '''    recorded_binding = record.get("authority_binding")
    expected_binding = dict(expected_binding)
    binding_keys = {"authority", "database_identity", "repository", "selection"}
    authority_keys = {
        "configuration_sha256",
        "database_path",
        "generation",
        "registry_path",
        "registry_sha256",
        "state_root",
    }
    repository_keys = {"repository_id", "repository_key", "repository_root"}
    selection_keys = {"milestone_id", "prompt_path", "selection_id", "selection_revision"}
    if (
        not isinstance(recorded_binding, dict)
        or set(recorded_binding) != binding_keys
        or set(expected_binding) != binding_keys
        or not isinstance(recorded_binding.get("authority"), dict)
        or not isinstance(expected_binding.get("authority"), dict)
        or set(recorded_binding["authority"]) != authority_keys
        or set(expected_binding["authority"]) != authority_keys
        or not isinstance(recorded_binding.get("repository"), dict)
        or not isinstance(expected_binding.get("repository"), dict)
        or set(recorded_binding["repository"]) != repository_keys
        or set(expected_binding["repository"]) != repository_keys
        or not isinstance(recorded_binding.get("selection"), dict)
        or not isinstance(expected_binding.get("selection"), dict)
        or set(recorded_binding["selection"]) != selection_keys
        or set(expected_binding["selection"]) != selection_keys
    ):
        raise BackupEvidenceError("The verified backup authority binding has an unsupported shape.")
    # Repository and selection identify the request context observed when the
    # whole-database backup was published. They remain strict, checksummed audit
    # evidence, but they are not the recovery coverage identity: one managed
    # SQLite authority contains every registered repository. Write eligibility
    # is therefore bound only to the stable authority and database identity.
    stable_keys = {"authority", "database_identity"}
    stable_recorded = {key: recorded_binding[key] for key in stable_keys}
    stable_expected = {key: expected_binding[key] for key in stable_keys}
''',
    )],
    "src/repoctl/information_store.py": [(
        '''    def _validate_backup_coverage(self) -> None:
        configuration = self.authority_configuration
        if configuration is None:
            return
        try:
            binding = backup_guard.authority_binding(
                configuration,
                self.database_identity(),
                repository_id=self.identity.repository_id,
                repository_key=self.identity.repository_key,
                repository_root=self.identity.repository_root,
                milestone_id=self.identity.milestone_id,
                prompt_path=self.identity.prompt_path,
                selection_id=self.identity.selection_id,
                selection_revision=self.identity.selection_revision,
            )
            if self.backup_coverage_path is None:
                raise backup_guard.BackupEvidenceError("The backup recovery-point path is not configured.")
            backup_guard.read_recovery_point(self.backup_coverage_path, binding)
        except backup_guard.BackupEvidenceError as exc:
            raise BackupCoverageError(
                f"Managed SQLite writes are paused until this authority has a fresh verified NAS backup: {exc}"
            ) from exc
''',
        '''    def _read_backup_coverage(self) -> dict[str, Any] | None:
        configuration = self.authority_configuration
        if configuration is None:
            return None
        binding = backup_guard.authority_binding(
            configuration,
            self.database_identity(),
            repository_id=self.identity.repository_id,
            repository_key=self.identity.repository_key,
            repository_root=self.identity.repository_root,
            milestone_id=self.identity.milestone_id,
            prompt_path=self.identity.prompt_path,
            selection_id=self.identity.selection_id,
            selection_revision=self.identity.selection_revision,
        )
        if self.backup_coverage_path is None:
            raise backup_guard.BackupEvidenceError("The backup recovery-point path is not configured.")
        return backup_guard.read_recovery_point(self.backup_coverage_path, binding)

    def backup_coverage_status(self) -> dict[str, Any]:
        if self.authority_configuration is None:
            return {"required": False, "status": "not_required", "write_eligible": True}
        try:
            record = self._read_backup_coverage()
        except backup_guard.BackupEvidenceError as exc:
            return {
                "required": True,
                "status": "blocked",
                "write_eligible": False,
                "reason": str(exc),
            }
        assert record is not None
        return {
            "required": True,
            "status": "ready",
            "write_eligible": True,
            "verified_at_utc": record["verified_at_utc"],
            "record_sha256": record["record_sha256"],
        }

    def _validate_backup_coverage(self) -> None:
        try:
            self._read_backup_coverage()
        except backup_guard.BackupEvidenceError as exc:
            raise BackupCoverageError(
                f"Managed SQLite writes are paused until this authority has a fresh verified NAS backup: {exc}"
            ) from exc
''',
    )],
    "src/repoctl/workflow/information_store_fixture.py": [(
        '''        return InformationStore.open_managed_from_external_selection(
            configuration, repository_id=repository_id, repository_key=repository_key,
            repository_root=repository_root,
        )
''',
        '''        verify_store_registry(configuration)
        registry = RepositoryRegistry.from_toml(configuration.registry_path)
        matches = [
            entry for entry in registry.entries
            if entry.repository_id == repository_id
            and entry.key == repository_key
            and str(entry.path) == repository_root
        ]
        if len(matches) != 1:
            raise FixtureIntegrationError("Selected repository does not match the guarded registry.")
        return InformationStore.open_managed_from_external_selection(
            configuration, repository_id=repository_id, repository_key=repository_key,
            repository_root=repository_root,
        )
''',
    ), (
        '''        }

    def browser_snapshot(self) -> dict[str, Any]:
''',
        '''        }

    def backup_coverage_status(self) -> dict[str, Any]:
        return self.store.backup_coverage_status()

    def browser_snapshot(self) -> dict[str, Any]:
''',
    )],
    "src/repoctl/web/app.py": [(
        '''    def runtime_binding(self) -> dict[str, Any]: ...

    def browser_snapshot(self) -> dict[str, Any]: ...
''',
        '''    def runtime_binding(self) -> dict[str, Any]: ...

    def backup_coverage_status(self) -> dict[str, Any]: ...

    def browser_snapshot(self) -> dict[str, Any]: ...
''',
    ), (
        '''                "information_store_binding": (
                    information_store_clients.runtime_binding()
                    if information_store_clients is not None else None
                ),
                "runtime_capabilities": {
''',
        '''                "information_store_binding": (
                    information_store_clients.runtime_binding()
                    if information_store_clients is not None else None
                ),
                "managed_write_coverage": (
                    information_store_clients.backup_coverage_status()
                    if information_store_clients is not None else {
                        "required": False,
                        "status": "not_required",
                        "write_eligible": True,
                    }
                ),
                "runtime_capabilities": {
''',
    )],
}


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def transform(path: str, content: bytes, source: str, target: str) -> bytes:
    text = content.decode("utf-8")
    for prior, corrected in REPLACEMENTS[path]:
        old, new = (prior, corrected) if source == "prior" else (corrected, prior)
        if text.count(old) != 1:
            raise RuntimeError(f"{path}: expected exactly one {source} correction block")
        text = text.replace(old, new)
    result = text.encode("utf-8")
    expected = STATES[target][path]
    if digest(result) != expected:
        raise RuntimeError(f"{path}: transformed hash does not match {target} state")
    return result


def current_state(path: str, content: bytes) -> str:
    value = digest(content)
    matches = [state for state in STATES if STATES[state][path] == value]
    if len(matches) != 1:
        raise RuntimeError(f"{path}: unrecognized source hash {value}")
    return matches[0]


def verify() -> None:
    for path in STATES["corrected"]:
        content = (REPOSITORY / path).read_bytes()
        state = current_state(path, content)
        other = "prior" if state == "corrected" else "corrected"
        transform(path, content, state, other)
        print(f"PASS {path} state={state}; exact {other} transformation verified in memory")


def apply_state(target: str) -> None:
    prepared: list[tuple[Path, Path]] = []
    try:
        for relative in STATES[target]:
            path = REPOSITORY / relative
            if path.is_symlink() or not path.is_file():
                raise RuntimeError(f"{relative}: source path is not a regular file")
            content = path.read_bytes()
            source = current_state(relative, content)
            if source == target:
                continue
            updated = transform(relative, content, source, target)
            descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
            temporary_path = Path(temporary)
            with os.fdopen(descriptor, "wb") as stream:
                os.fchmod(stream.fileno(), path.stat().st_mode & 0o777)
                stream.write(updated)
                stream.flush()
                os.fsync(stream.fileno())
            prepared.append((path, temporary_path))
        for path, temporary_path in prepared:
            os.replace(temporary_path, path)
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        for relative, expected in STATES[target].items():
            if digest((REPOSITORY / relative).read_bytes()) != expected:
                raise RuntimeError(f"{relative}: post-write {target} hash mismatch")
        print(f"CODE STATE VERIFIED: {target}; database and service configuration were not touched")
    finally:
        for _, temporary_path in prepared:
            temporary_path.unlink(missing_ok=True)


def main() -> int:
    action = sys.argv[1] if len(sys.argv) == 2 else ""
    if action == "verify":
        verify()
    elif action == "rollback":
        apply_state("prior")
    elif action == "reapply":
        apply_state("corrected")
    else:
        print(f"Usage: {Path(sys.argv[0]).name} {{verify|rollback|reapply}}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
