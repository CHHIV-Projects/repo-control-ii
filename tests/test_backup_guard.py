from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from repoctl import backup_guard


class RecoveryAuthorityBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.root.chmod(0o700)
        self.record_path = self.root / "recovery.json"
        self.configuration = SimpleNamespace(
            configuration_sha256="a" * 64,
            database_path=self.root / "managed.sqlite3",
            generation="b" * 32,
            registry_path=self.root / "repositories.toml",
            registry_sha256="c" * 64,
            state_root=self.root / "state",
        )
        self.binding = backup_guard.authority_binding(
            self.configuration,
            "d" * 64,
            repository_id="repo-id",
            repository_key="repo-key",
            repository_root=str(self.root / "repository"),
            milestone_id="M010.030",
            prompt_path="docs/milestones/M010.030_prompt.md",
            selection_id="selection-107",
            selection_revision=107,
        )
        body = {
            "authority_binding": self.binding,
            "database_sha256": "e" * 64,
            "manifest_path": str(self.root / "snapshot.manifest.json"),
            "manifest_sha256": "f" * 64,
            "record_version": 1,
            "snapshot_sha256": "1" * 64,
            "verified_at_utc": "2026-11-20T00:00:00+00:00",
        }
        record = {**body, "record_sha256": hashlib.sha256(backup_guard.canonical_json(body)).hexdigest()}
        self.record_path.write_bytes(backup_guard.canonical_json(record) + b"\n")
        self.record_path.chmod(0o600)

    def read(self, binding):
        return backup_guard.read_recovery_point(
            self.record_path,
            binding,
            now=datetime(2026, 11, 20, 0, 1, tzinfo=UTC),
        )

    def test_request_context_change_does_not_invalidate_same_database_authority(self) -> None:
        changed = json.loads(json.dumps(self.binding))
        changed["repository"] = {
            "repository_id": "other-repo-id",
            "repository_key": "other-repo-key",
            "repository_root": str(self.root / "other-repository"),
        }
        changed["selection"] = {
            "milestone_id": "M010.031",
            "prompt_path": "docs/milestones/M010.031_prompt.md",
            "selection_id": "selection-108",
            "selection_revision": 108,
        }
        record = self.read(changed)
        self.assertEqual(record["authority_binding"]["selection"]["selection_revision"], 107)

    def test_stable_authority_or_database_change_still_fails_closed(self) -> None:
        for path, value in (
            (("database_identity",), "9" * 64),
            (("authority", "generation"), "8" * 32),
            (("authority", "configuration_sha256"), "7" * 64),
            (("authority", "registry_sha256"), "6" * 64),
            (("authority", "state_root"), str(self.root / "other-state")),
        ):
            changed = json.loads(json.dumps(self.binding))
            target = changed
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
            with self.subTest(path=path):
                with self.assertRaisesRegex(backup_guard.BackupEvidenceError, "different database or authority"):
                    self.read(changed)

    def test_binding_shape_remains_strict(self) -> None:
        changed = json.loads(json.dumps(self.binding))
        changed["selection"]["extra"] = True
        with self.assertRaisesRegex(backup_guard.BackupEvidenceError, "unsupported shape"):
            self.read(changed)

        changed = json.loads(json.dumps(self.binding))
        changed["repository"]["extra"] = True
        with self.assertRaisesRegex(backup_guard.BackupEvidenceError, "unsupported shape"):
            self.read(changed)

    def test_tampered_record_still_fails_checksum(self) -> None:
        record = json.loads(self.record_path.read_text(encoding="utf-8"))
        record["authority_binding"]["repository"]["repository_key"] = "tampered"
        self.record_path.write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaisesRegex(backup_guard.BackupEvidenceError, "checksum"):
            self.read(self.binding)


if __name__ == "__main__":
    unittest.main()
