from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from repoctl.scanner.util import make_repository_id
from repoctl.workflow.architect_dispatches import (
    ArchitectDispatchError,
    read_owner_dispatch_ledger_snapshot,
)


class ArchitectDispatchSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="repoctl_dispatch_snapshot_", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repository = self.root / "repository"
        self.repository.mkdir()
        self.repository_id = make_repository_id(self.repository)
        self.state_root = self.root / "state"
        self.source = self.state_root / self.repository_id / "architect_dispatches.json"

    def test_absent_ledger_is_reported_without_creating_state_root_or_lock(self) -> None:
        snapshot = read_owner_dispatch_ledger_snapshot(
            self.repository,
            self.state_root,
            self.repository_id,
        )
        self.assertEqual(snapshot["status"], "absent")
        self.assertIsNone(snapshot["source_bytes"])
        self.assertEqual(snapshot["ledger"]["dispatches"], [])
        self.assertFalse(self.state_root.exists())

    def test_present_ledger_returns_validated_exact_source_bytes_and_hash(self) -> None:
        self.source.parent.mkdir(parents=True)
        source_bytes = (
            json.dumps(
                {"schema_version": 1, "repository_id": self.repository_id, "dispatches": []},
                separators=(",", ":"),
            )
            + "\n"
        ).encode()
        self.source.write_bytes(source_bytes)
        snapshot = read_owner_dispatch_ledger_snapshot(
            self.repository,
            self.state_root,
            self.repository_id,
        )
        self.assertEqual(snapshot["status"], "present")
        self.assertEqual(snapshot["source_path"], str(self.source))
        self.assertEqual(snapshot["source_bytes"], source_bytes)
        self.assertEqual(snapshot["source_size_bytes"], len(source_bytes))
        self.assertEqual(snapshot["source_sha256"], hashlib.sha256(source_bytes).hexdigest())
        self.assertEqual(snapshot["ledger"]["dispatches"], [])
        self.assertFalse((self.state_root / ".locks").exists())

    def test_symlinked_ledger_is_rejected_without_following_it(self) -> None:
        self.source.parent.mkdir(parents=True)
        outside = self.root / "outside.json"
        outside.write_text("{}", encoding="utf-8")
        self.source.symlink_to(outside)
        with self.assertRaises(ArchitectDispatchError):
            read_owner_dispatch_ledger_snapshot(
                self.repository,
                self.state_root,
                self.repository_id,
            )
        self.assertEqual(outside.read_text(encoding="utf-8"), "{}")


if __name__ == "__main__":
    unittest.main()
