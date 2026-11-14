from __future__ import annotations

from contextlib import redirect_stderr
from datetime import UTC, datetime, timedelta
import hashlib
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from repoctl.backup import (
    BackupConfig,
    BackupError,
    main as backup_main,
    check_backup_freshness,
    create_verified_backup,
    parse_mountinfo,
    restore_verified_backup,
    verify_destination_mount,
)
from repoctl.information_store import (
    DocumentRevisionInput,
    InformationStore,
    StoreIdentity,
)
from tests.test_sqlite_workflow_poc import _closeout_history


class BackupTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="repoctl_backup_", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repository_root = self.root / "fixture-repository"
        self.identity = StoreIdentity(
            repository_id="fixture-repository-id",
            repository_key="fixture-repository",
            repository_root=str(self.repository_root),
            milestone_id="M999.001",
            prompt_path="fixtures/milestone_prompt.md",
            selection_id="fixture-selection-1",
            selection_revision=1,
        )
        self.source = self.root / "source.sqlite3"
        self.store = InformationStore.initialize_disposable(
            self.source,
            self.identity,
            allow_unsupported_runtime_for_fixture=True,
        )
        self._add_prompt_and_workflow()
        self.staging = self.root / "staging"
        self.destination = self.root / "nas" / "repo-control-ii"
        self.restore_root = self.root / "restore"
        for directory in (self.staging, self.destination, self.restore_root):
            directory.mkdir(parents=True, mode=0o700)

    def _add_prompt_and_workflow(self) -> None:
        created = self.store.create_immutable_revision(DocumentRevisionInput(
            document_id="prompt-document",
            revision_id="prompt-revision-1",
            logical_key=self.identity.prompt_path,
            logical_type="PROMPT",
            source_path=self.identity.prompt_path,
            content="# Fixture prompt\n",
        ))
        events = _closeout_history()
        self.store.select_approved_prompt_revision(
            request_id="select-prompt-1",
            expected_store_revision=0,
            document_id=created["document_id"],
            revision_id=created["revision_id"],
            content_sha256=created["content_sha256"],
            prompt_approved_event=events[0],
            trusted_fixture_po_confirmation=True,
        )
        self.store.append_workflow_batch(
            request_id="start-implementation-1",
            expected_repository_id=self.identity.repository_id,
            expected_milestone_id=self.identity.milestone_id,
            expected_selection_id=self.identity.selection_id,
            expected_selection_revision=self.identity.selection_revision,
            expected_prompt_revision_id=created["revision_id"],
            expected_store_revision=1,
            events=[events[1]],
        )

    def _backup(self, *, now: datetime | None = None) -> dict:
        return create_verified_backup(
            self.source,
            self.staging,
            self.destination,
            expected_repository_id=self.identity.repository_id,
            expected_repository_key=self.identity.repository_key,
            expected_repository_root=self.identity.repository_root,
            now=now,
        )

    def test_backup_restore_preserves_selection_events_and_exact_document_hashes(self) -> None:
        created = self._backup(now=datetime(2026, 11, 14, 12, 0, tzinfo=UTC))
        backup_path = self.destination / created["database_file"]
        manifest_path = self.destination / Path(created["manifest_path"]).name
        self.assertEqual(hashlib.sha256(backup_path.read_bytes()).hexdigest(), created["database_sha256"])
        self.assertEqual(created["snapshot"]["selection"]["selection_id"], self.identity.selection_id)
        self.assertEqual(created["snapshot"]["selected_workflow"]["event_count"], 2)
        self.assertEqual(
            created["snapshot"]["document_revisions"][0]["content_sha256"],
            hashlib.sha256(b"# Fixture prompt\n").hexdigest(),
        )

        restored_path = restore_verified_backup(
            manifest_path,
            self.restore_root / "restored.sqlite3",
            self.restore_root,
            expected_repository_id=self.identity.repository_id,
            expected_repository_key=self.identity.repository_key,
            expected_repository_root=self.identity.repository_root,
        )
        restored = InformationStore.open_disposable(
            restored_path,
            self.identity,
            allow_unsupported_runtime_for_fixture=True,
        )
        snapshot = restored.snapshot()
        self.assertEqual(snapshot["selection_id"], self.identity.selection_id)
        self.assertEqual(snapshot["selection_revision"], self.identity.selection_revision)
        self.assertEqual(snapshot["event_count"], 2)
        revision = restored.get_revision("prompt-document", "prompt-revision-1")
        self.assertEqual(revision["content"], "# Fixture prompt\n")
        self.assertEqual(revision["content_sha256"], created["snapshot"]["document_revisions"][0]["content_sha256"])
        self.assertEqual(restored_path.stat().st_mode & 0o777, 0o600)

    def test_transfer_failure_keeps_previous_verified_backup(self) -> None:
        first = self._backup(now=datetime(2026, 11, 14, 12, 0, tzinfo=UTC))
        previous = self.destination / first["database_file"]
        previous_bytes = previous.read_bytes()
        with patch("repoctl.backup._copy_to_partial", side_effect=OSError("simulated transfer interruption")):
            with self.assertRaisesRegex(OSError, "simulated transfer interruption"):
                self._backup(now=datetime(2026, 11, 14, 12, 15, tzinfo=UTC))
        self.assertTrue(previous.is_file())
        self.assertEqual(previous.read_bytes(), previous_bytes)
        self.assertEqual(len(list(self.destination.glob("*.manifest.json"))), 1)
        self.assertEqual(list(self.destination.glob("*.partial")), [])
        self.assertEqual(list(self.staging.glob("*.partial")), [])

    def test_monitor_reports_stale_copy_and_ignores_corrupted_database(self) -> None:
        created = self._backup(now=datetime(2026, 11, 14, 12, 0, tzinfo=UTC))
        healthy = check_backup_freshness(
            self.destination,
            expected_repository_id=self.identity.repository_id,
            expected_repository_key=self.identity.repository_key,
            expected_repository_root=self.identity.repository_root,
            now=datetime(2026, 11, 14, 12, 19, tzinfo=UTC),
        )
        self.assertTrue(healthy.healthy)
        stale = check_backup_freshness(
            self.destination,
            expected_repository_id=self.identity.repository_id,
            expected_repository_key=self.identity.repository_key,
            expected_repository_root=self.identity.repository_root,
            now=datetime(2026, 11, 14, 12, 21, tzinfo=UTC),
        )
        self.assertFalse(stale.healthy)
        self.assertIn("21.0 minutes old", stale.message)

        (self.destination / created["database_file"]).write_bytes(b"corrupted")
        invalid = check_backup_freshness(
            self.destination,
            expected_repository_id=self.identity.repository_id,
            expected_repository_key=self.identity.repository_key,
            expected_repository_root=self.identity.repository_root,
            now=datetime(2026, 11, 14, 12, 1, tzinfo=UTC),
        )
        self.assertFalse(invalid.healthy)
        self.assertEqual(invalid.invalid_manifests, 1)

    def test_restore_refuses_existing_destination_and_tampered_manifest(self) -> None:
        created = self._backup(now=datetime(2026, 11, 14, 12, 0, tzinfo=UTC))
        manifest_path = self.destination / Path(created["manifest_path"]).name
        target = self.restore_root / "existing.sqlite3"
        target.write_bytes(b"leave intact")
        with self.assertRaisesRegex(BackupError, "new direct child"):
            restore_verified_backup(
                manifest_path,
                target,
                self.restore_root,
                expected_repository_id=self.identity.repository_id,
                expected_repository_key=self.identity.repository_key,
                expected_repository_root=self.identity.repository_root,
            )
        target.unlink()
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["database_sha256"] = "0" * 64
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(BackupError, "manifest"):
            restore_verified_backup(
                manifest_path,
                self.restore_root / "new.sqlite3",
                self.restore_root,
                expected_repository_id=self.identity.repository_id,
                expected_repository_key=self.identity.repository_key,
                expected_repository_root=self.identity.repository_root,
            )
        self.assertEqual(list(self.restore_root.iterdir()), [])

    def test_mount_identity_requires_exact_cifs_share_and_path(self) -> None:
        mount = self.root / "nas-mount"
        destination = mount / "repo-control-ii"
        destination.mkdir(parents=True, mode=0o700)
        expected = f"31 24 0:42 / {mount} rw,relatime - cifs //192.168.1.171/RepoControl rw"
        self.assertEqual(parse_mountinfo(expected)[0][1:], ("cifs", "//192.168.1.171/RepoControl"))
        verify_destination_mount(
            destination,
            mount,
            "//192.168.1.171/RepoControl",
            mountinfo=expected,
        )
        with self.assertRaisesRegex(BackupError, "identity"):
            verify_destination_mount(
                destination,
                mount,
                "//192.168.1.171/PhotoOrganizer",
                mountinfo=expected,
            )
        with self.assertRaisesRegex(BackupError, "mounted filesystem"):
            verify_destination_mount(destination, mount, "//192.168.1.171/RepoControl", mountinfo="")

    def test_mount_parser_decodes_spaces_without_changing_share_identity(self) -> None:
        mount = self.root / "nas mount"
        target = mount / "repo-control"
        target.mkdir(parents=True)
        escaped_mount = str(mount).replace(" ", "\\040")
        mountinfo = f"31 24 0:42 / {escaped_mount} rw - cifs //server/share rw"
        self.assertEqual(parse_mountinfo(mountinfo)[0][0], mount)
        verify_destination_mount(target, mount, "//server/share", mountinfo=mountinfo)

    def test_monitor_attempts_configured_alert_when_nas_mount_is_unavailable(self) -> None:
        config = BackupConfig(
            source=self.source,
            staging=self.staging,
            destination=self.destination,
            restore_root=self.restore_root,
            mountpoint=self.root / "nas-mount",
            mount_source="//server/share",
            repository_id=self.identity.repository_id,
            repository_key=self.identity.repository_key,
            repository_root=self.identity.repository_root,
            alert_hook=Path("/fixture/alert-hook"),
            alert_target="fixture-operator-route",
        )
        error_output = StringIO()
        with (
            patch("repoctl.backup.config_from_environment", return_value=config),
            patch("repoctl.backup.verify_destination_mount", side_effect=BackupError("NAS unavailable")),
            patch("repoctl.backup._send_alert") as send_alert,
            redirect_stderr(error_output),
        ):
            result = backup_main(["monitor"])

        self.assertEqual(result, 1)
        self.assertIn("NAS backup destination check failed: NAS unavailable", error_output.getvalue())
        send_alert.assert_called_once_with(
            config.alert_hook,
            config.alert_target,
            "NAS backup destination check failed: NAS unavailable",
        )


if __name__ == "__main__":
    unittest.main()
