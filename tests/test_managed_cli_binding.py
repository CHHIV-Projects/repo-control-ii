from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest

from repoctl.managed_cli_binding import (
    ManagedCliBindingError,
    load_managed_cli_binding,
)


class ManagedCliBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "managed-authority.d"
        self.root.mkdir(mode=0o755)
        self.payload = {
            "schema_version": 1,
            "authority": "sqlite_managed",
            "state_root": "/var/lib/repo-control/state",
            "backup_coverage_path": "/var/lib/repo-control/coverage.json",
            "registry_path": "/srv/repo-control/repositories.toml",
            "authority_configuration_sha256": "a" * 64,
            "authority_generation": "b" * 32,
            "database_identity": "c" * 64,
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write(self, name: str = "live.json", payload=None) -> Path:
        target = self.root / name
        target.write_text(json.dumps(payload if payload is not None else self.payload))
        target.chmod(0o644)
        return target

    def load(self):
        return load_managed_cli_binding(self.root, required_owner_uid=os.geteuid())

    def test_absent_installation_preserves_nonmanaged_cli(self) -> None:
        self.root.rmdir()
        self.assertIsNone(load_managed_cli_binding(self.root, required_owner_uid=os.geteuid()))

    def test_exact_binding_is_loaded(self) -> None:
        target = self.write()
        binding = self.load()
        self.assertEqual(binding.source_path, target)
        self.assertEqual(binding.state_root, Path(self.payload["state_root"]))
        self.assertEqual(binding.backup_coverage_path, Path(self.payload["backup_coverage_path"]))
        self.assertEqual(binding.authority_configuration_sha256, "a" * 64)
        self.assertEqual(binding.authority_generation, "b" * 32)
        self.assertEqual(binding.database_identity, "c" * 64)

    def test_present_but_missing_or_ambiguous_binding_fails_closed(self) -> None:
        with self.assertRaisesRegex(ManagedCliBindingError, "contains no binding"):
            self.load()
        self.write("one.json")
        self.write("two.json")
        with self.assertRaisesRegex(ManagedCliBindingError, "ambiguous"):
            self.load()

    def test_insecure_directory_file_and_symlink_fail_closed(self) -> None:
        target = self.write()
        self.root.chmod(0o777)
        with self.assertRaisesRegex(ManagedCliBindingError, "directory"):
            self.load()
        self.root.chmod(0o755)
        target.chmod(0o666)
        with self.assertRaisesRegex(ManagedCliBindingError, "regular file"):
            self.load()
        target.unlink()
        source = Path(self.temporary.name) / "outside.json"
        source.write_text(json.dumps(self.payload))
        target.symlink_to(source)
        with self.assertRaisesRegex(ManagedCliBindingError, "safely open"):
            self.load()

    def test_unreadable_binding_directory_fails_with_bounded_error(self) -> None:
        self.write()
        self.root.chmod(0o000)
        if os.geteuid() == 0:
            self.root.chmod(0o755)
            self.skipTest("root can open a mode-000 directory")
        try:
            with self.assertRaisesRegex(ManagedCliBindingError, "safely open"):
                self.load()
        finally:
            self.root.chmod(0o755)

    def test_malformed_stale_shape_and_duplicate_keys_fail_closed(self) -> None:
        target = self.write(payload={**self.payload, "extra": True})
        with self.assertRaisesRegex(ManagedCliBindingError, "unsupported shape"):
            self.load()
        target.write_text('{"schema_version":1,"schema_version":1}')
        with self.assertRaisesRegex(ManagedCliBindingError, "duplicate key"):
            self.load()
        target.write_text(json.dumps({**self.payload, "database_identity": "not-a-hash"}))
        with self.assertRaisesRegex(ManagedCliBindingError, "identity values"):
            self.load()


if __name__ == "__main__":
    unittest.main()
