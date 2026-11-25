from __future__ import annotations

from contextlib import redirect_stderr
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import fcntl
import hashlib
from io import StringIO
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from threading import Event
import time
import unittest
from unittest.mock import patch

from repoctl import backup as backup_module, backup_guard
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
from tests.backup_monitor_fixture import fixture_binding, full_scrub, probe_latest
from tests import backup_sandbox_fixture


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

    def _record_fixture(self, created: dict) -> tuple[Path, dict]:
        path = self.root / "fixture-recovery.json"
        binding = fixture_binding(self.root, self.identity)
        backup_guard.write_recovery_point(
            path, manifest_path=Path(created["manifest_path"]), manifest=created, binding=binding,
        )
        return path, binding

    @staticmethod
    def _sidecars(directory: Path) -> set[Path]:
        return {
            path for path in directory.iterdir()
            if path.name.endswith(("-wal", "-shm"))
        }

    def test_backup_restore_preserves_selection_events_and_exact_document_hashes(self) -> None:
        staging_sentinel = self.staging / "unrelated-sentinel.sqlite3.partial-wal"
        destination_sentinel = self.destination / "unrelated-sentinel.sqlite3-shm"
        staging_sentinel.write_bytes(b"keep staging sentinel")
        destination_sentinel.write_bytes(b"keep destination sentinel")

        created = self._backup(now=datetime(2026, 11, 14, 12, 0, tzinfo=UTC))
        backup_path = self.destination / created["database_file"]
        manifest_path = self.destination / Path(created["manifest_path"]).name
        self.assertEqual(self._sidecars(self.staging), {staging_sentinel})
        self.assertEqual(self._sidecars(self.destination), {destination_sentinel})
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
        self.assertEqual(self._sidecars(self.staging), {staging_sentinel})
        self.assertEqual(self._sidecars(self.destination), {destination_sentinel})
        self.assertEqual(self._sidecars(self.restore_root), set())
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
        self.assertEqual(staging_sentinel.read_bytes(), b"keep staging sentinel")
        self.assertEqual(destination_sentinel.read_bytes(), b"keep destination sentinel")
        source_connection = sqlite3.connect(self.source)
        try:
            self.assertEqual(source_connection.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
            self.assertEqual(source_connection.execute("SELECT COUNT(*) FROM workflow_event").fetchone()[0], 2)
        finally:
            source_connection.close()

    def test_transfer_failure_keeps_previous_verified_backup(self) -> None:
        first = self._backup(now=datetime(2026, 11, 14, 12, 0, tzinfo=UTC))
        previous = self.destination / first["database_file"]
        previous_manifest = self.destination / Path(first["manifest_path"]).name
        previous_bytes = previous.read_bytes()
        previous_manifest_bytes = previous_manifest.read_bytes()
        staging_sentinel = self.staging / "unrelated-transfer-sentinel-wal"
        destination_sentinel = self.destination / "unrelated-transfer-sentinel-shm"
        staging_sentinel.write_bytes(b"keep staging sentinel")
        destination_sentinel.write_bytes(b"keep destination sentinel")
        with patch("repoctl.backup._copy_to_partial", side_effect=OSError("simulated transfer interruption")):
            with self.assertRaisesRegex(OSError, "simulated transfer interruption"):
                self._backup(now=datetime(2026, 11, 14, 12, 15, tzinfo=UTC))
        self.assertTrue(previous.is_file())
        self.assertEqual(previous.read_bytes(), previous_bytes)
        self.assertEqual(previous_manifest.read_bytes(), previous_manifest_bytes)
        self.assertEqual(len(list(self.destination.glob("*.manifest.json"))), 1)
        self.assertEqual(list(self.destination.glob("*.partial")), [])
        self.assertEqual(list(self.staging.glob("*.partial")), [])
        self.assertEqual(self._sidecars(self.staging), {staging_sentinel})
        self.assertEqual(self._sidecars(self.destination), {destination_sentinel})
        self.assertEqual(staging_sentinel.read_bytes(), b"keep staging sentinel")
        self.assertEqual(destination_sentinel.read_bytes(), b"keep destination sentinel")

    def test_destination_verification_failure_cleans_owned_sidecars(self) -> None:
        first = self._backup(now=datetime(2026, 11, 14, 12, 0, tzinfo=UTC))
        previous = self.destination / first["database_file"]
        previous_manifest = self.destination / Path(first["manifest_path"]).name
        previous_bytes = previous.read_bytes()
        previous_manifest_bytes = previous_manifest.read_bytes()
        real_snapshot = backup_module._store_snapshot
        calls = 0

        def fail_destination_verification(*args, **kwargs):
            nonlocal calls
            calls += 1
            snapshot = real_snapshot(*args, **kwargs)
            if calls == 2:
                raise BackupError("simulated destination verification failure")
            return snapshot

        staging_sentinel = self.staging / "unrelated-verification-sentinel-wal"
        destination_sentinel = self.destination / "unrelated-verification-sentinel-shm"
        staging_sentinel.write_bytes(b"keep staging sentinel")
        destination_sentinel.write_bytes(b"keep destination sentinel")
        with patch("repoctl.backup._store_snapshot", side_effect=fail_destination_verification):
            with self.assertRaisesRegex(BackupError, "simulated destination verification failure"):
                self._backup(now=datetime(2026, 11, 14, 12, 15, tzinfo=UTC))

        self.assertEqual(calls, 2)
        self.assertEqual(previous.read_bytes(), previous_bytes)
        self.assertEqual(previous_manifest.read_bytes(), previous_manifest_bytes)
        self.assertEqual(len(list(self.destination.glob("*.manifest.json"))), 1)
        self.assertEqual(list(self.destination.glob("*.partial")), [])
        self.assertEqual(list(self.staging.glob("*.partial")), [])
        self.assertEqual(self._sidecars(self.staging), {staging_sentinel})
        self.assertEqual(self._sidecars(self.destination), {destination_sentinel})
        self.assertEqual(staging_sentinel.read_bytes(), b"keep staging sentinel")
        self.assertEqual(destination_sentinel.read_bytes(), b"keep destination sentinel")

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
        threshold = check_backup_freshness(
            self.destination,
            expected_repository_id=self.identity.repository_id,
            expected_repository_key=self.identity.repository_key,
            expected_repository_root=self.identity.repository_root,
            now=datetime(2026, 11, 14, 12, 20, tzinfo=UTC),
        )
        self.assertFalse(threshold.healthy)
        self.assertIn("20.0 minutes old", threshold.message)
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

    def test_prototype_exact_point_bounds_reads_and_exposes_historical_detection_gap(self) -> None:
        now = datetime(2026, 11, 24, 12, tzinfo=UTC)
        older = self._backup(now=now - timedelta(minutes=15))
        newest = self._backup(now=now)
        record, binding = self._record_fixture(newest)
        history = full_scrub(self.destination, self.identity, now=now)
        older_path = self.destination / older["database_file"]
        contents = older_path.read_bytes()
        older_path.write_bytes(bytes([contents[0] ^ 1]) + contents[1:])
        arguments = (self.destination, record, self.staging / ".repoctl-backup.lock", self.identity, binding)
        options = dict(now=now, history=history, last_job_state="success", publication_complete=True)
        descriptors = len(list(Path("/proc/self/fd").iterdir()))

        with patch.object(backup_module, "_sha256_file", wraps=backup_module._sha256_file) as hashing:
            for _ in range(12):
                result = probe_latest(*arguments, **options)
                self.assertTrue(result["ready"], result)
                self.assertEqual(result["history_code"], "VERIFIED_AS_OF")
                self.assertIn("only as of receipt", result["message"])
            self.assertEqual(hashing.call_count, 12)
            self.assertEqual(
                {call.args[0] for call in hashing.call_args_list},
                {self.destination / newest["database_file"]},
            )
        self.assertEqual(len(list(Path("/proc/self/fd").iterdir())), descriptors)

        detected = full_scrub(self.destination, self.identity, now=now)
        self.assertEqual(detected.invalid_manifests, (Path(older["manifest_path"]).name,))
        result = probe_latest(*arguments, **{**options, "history": detected})
        self.assertFalse(result["ready"])
        self.assertEqual(result["freshness_code"], "VERIFIED")
        self.assertEqual(result["code"], "HISTORY_FAILED")
        for receipt in (None, replace(history, checked_at=now - timedelta(minutes=70))):
            result = probe_latest(*arguments, **{**options, "history": receipt})
            self.assertFalse(result["ready"])
            self.assertEqual(result["code"], "HISTORY_DUE")

    def test_prototype_never_falls_back_from_failed_exact_recovery_evidence(self) -> None:
        now = datetime(2026, 11, 24, 12, tzinfo=UTC)
        self._backup(now=now - timedelta(minutes=15))
        newest = self._backup(now=now)
        record, binding = self._record_fixture(newest)
        history = full_scrub(self.destination, self.identity, now=now)
        manifest = Path(newest["manifest_path"])
        payload = self.destination / newest["database_file"]
        original = {path: path.read_bytes() for path in (record, manifest, payload)}
        descriptors = len(list(Path("/proc/self/fd").iterdir()))
        cases = {
            "missing_record": "RECOVERY_UNVERIFIED",
            "expired": "RECOVERY_UNVERIFIED",
            "wrong_database": "RECOVERY_UNVERIFIED",
            "wrong_repository": "PAYLOAD_UNVERIFIED",
            "same_size_corruption": "PAYLOAD_UNVERIFIED",
            "missing_payload": "PAYLOAD_UNVERIFIED",
            "tampered_manifest": "PAYLOAD_UNVERIFIED",
            "partial_manifest": "NOT_PUBLISHED",
            "mismatched_record": "RECORD_MISMATCH",
        }
        for case, expected in cases.items():
            with self.subTest(case=case):
                expected_binding = json.loads(json.dumps(binding))
                identity, instant = self.identity, now
                try:
                    if case == "missing_record":
                        record.unlink()
                    elif case == "expired":
                        instant += timedelta(seconds=1200)
                    elif case == "wrong_database":
                        expected_binding["database_identity"] = "0" * 64
                    elif case == "wrong_repository":
                        identity = replace(self.identity, repository_id="wrong-fixture-repository")
                    elif case == "same_size_corruption":
                        contents = original[payload]
                        payload.write_bytes(bytes([contents[0] ^ 1]) + contents[1:])
                    elif case == "missing_payload":
                        payload.unlink()
                    elif case == "tampered_manifest":
                        manifest.write_bytes(b"{}")
                    elif case == "partial_manifest":
                        partial = self.destination / ".pending.manifest.json.partial"
                        partial.write_bytes(original[manifest])
                        backup_guard.write_recovery_point(
                            record, manifest_path=partial, manifest=newest, binding=binding,
                        )
                    elif case == "mismatched_record":
                        backup_guard.write_recovery_point(
                            record, manifest_path=manifest,
                            manifest={**newest, "snapshot_sha256": "0" * 64}, binding=binding,
                        )
                    result = probe_latest(
                        self.destination, record, self.staging / ".repoctl-backup.lock",
                        identity, expected_binding, now=instant, history=history,
                        last_job_state="success", publication_complete=True,
                    )
                    self.assertFalse(result["ready"], result)
                    self.assertEqual(result["code"], expected, result)
                    self.assertTrue(result["message"])
                    self.assertEqual(json.loads(json.dumps(result)), result)
                    self.assertEqual(len(list(Path("/proc/self/fd").iterdir())), descriptors)
                finally:
                    for path, contents in original.items():
                        path.write_bytes(contents)
                        path.chmod(0o600)

    def test_prototype_publication_lock_survives_offset_overrun_and_failed_jobs(self) -> None:
        now = datetime(2026, 11, 24, 12, tzinfo=UTC)
        newest = self._backup(now=now)
        record, binding = self._record_fixture(newest)
        history = full_scrub(self.destination, self.identity, now=now)
        lock = self.staging / ".repoctl-backup.lock"
        arguments = (self.destination, record, lock, self.identity, binding)
        options = dict(now=now, history=history, last_job_state="success", publication_complete=True)
        descriptor = os.open(lock, os.O_RDWR)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(backup_module, "_sha256_file", wraps=backup_module._sha256_file) as hashing:
                for offset in (60, 120, 360):
                    result = probe_latest(*arguments, **{**options, "now": now + timedelta(seconds=offset)})
                    self.assertEqual(result["code"], "BACKUP_IN_PROGRESS")
                    self.assertFalse(result["ready"])
                hashing.assert_not_called()
        finally:
            os.close(descriptor)
        result = probe_latest(*arguments, **{**options, "publication_complete": False})
        self.assertEqual(result["code"], "PUBLICATION_PENDING")
        for state in ("failed", "unknown"):
            result = probe_latest(*arguments, **{**options, "last_job_state": state})
            self.assertFalse(result["ready"])
            self.assertEqual(result["code"], "BACKUP_JOB_UNVERIFIED")
        self.assertTrue(probe_latest(*arguments, **options)["ready"])
        lock.unlink()
        self.assertEqual(probe_latest(*arguments, **options)["code"], "LOCK_UNAVAILABLE")

    def test_reader_lock_requires_bounded_writer_retry_not_current_nonblocking_writer(self) -> None:
        now = datetime(2026, 11, 24, 12, tzinfo=UTC)
        self._backup(now=now)
        descriptor = os.open(self.staging / ".repoctl-backup.lock", os.O_RDONLY)
        real_flock = fcntl.flock
        waiting = Event()
        retry_seconds = 2.0

        def bounded_fixture_flock(fd: int, operation: int) -> None:
            deadline = time.monotonic() + retry_seconds
            while True:
                try:
                    real_flock(fd, operation)
                    return
                except BlockingIOError:
                    if operation != (fcntl.LOCK_EX | fcntl.LOCK_NB) or time.monotonic() >= deadline:
                        raise
                    waiting.set()
                    time.sleep(0.005)

        try:
            real_flock(descriptor, fcntl.LOCK_SH)
            with self.assertRaisesRegex(BackupError, "Another Repo Control backup"):
                self._backup(now=now + timedelta(seconds=1))
            with patch.object(backup_module.fcntl, "flock", side_effect=bounded_fixture_flock):
                with ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(self._backup, now=now + timedelta(seconds=2))
                    self.assertTrue(waiting.wait(1), "Fixture writer did not encounter the held reader lock.")
                    self.assertFalse(future.done())
                    real_flock(descriptor, fcntl.LOCK_UN)
                    created = future.result(timeout=3)
                    self.assertTrue(Path(created["manifest_path"]).is_file())
                retry_seconds = 0.05
                real_flock(descriptor, fcntl.LOCK_SH)
                with self.assertRaisesRegex(BackupError, "Another Repo Control backup"):
                    self._backup(now=now + timedelta(seconds=3))
                self.assertEqual(len(list(self.destination.glob("*.manifest.json"))), 2)
        finally:
            os.close(descriptor)

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

    def test_restore_preserves_preexisting_backup_sidecars(self) -> None:
        created = self._backup(now=datetime(2026, 11, 14, 12, 0, tzinfo=UTC))
        backup_path = self.destination / created["database_file"]
        manifest_path = self.destination / Path(created["manifest_path"]).name
        backup_bytes = backup_path.read_bytes()
        manifest_bytes = manifest_path.read_bytes()
        sidecar = backup_path.with_name(f"{backup_path.name}-wal")
        sidecar.write_bytes(b"sidecar owned by another operation")

        with self.assertRaisesRegex(BackupError, "pre-existing SQLite sidecars"):
            restore_verified_backup(
                manifest_path,
                self.restore_root / "restored.sqlite3",
                self.restore_root,
                expected_repository_id=self.identity.repository_id,
                expected_repository_key=self.identity.repository_key,
                expected_repository_root=self.identity.repository_root,
            )

        self.assertEqual(sidecar.read_bytes(), b"sidecar owned by another operation")
        self.assertEqual(backup_path.read_bytes(), backup_bytes)
        self.assertEqual(manifest_path.read_bytes(), manifest_bytes)
        self.assertEqual(list(self.restore_root.iterdir()), [])

    def test_restore_refuses_concurrent_validation_without_touching_sidecars(self) -> None:
        created = self._backup(now=datetime(2026, 11, 14, 12, 0, tzinfo=UTC))
        backup_path = self.destination / created["database_file"]
        manifest_path = self.destination / Path(created["manifest_path"]).name
        backup_bytes = backup_path.read_bytes()
        manifest_bytes = manifest_path.read_bytes()
        descriptor = os.open(manifest_path, os.O_RDONLY)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            with self.assertRaisesRegex(BackupError, "Another restore operation"):
                restore_verified_backup(
                    manifest_path,
                    self.restore_root / "restored.sqlite3",
                    self.restore_root,
                    expected_repository_id=self.identity.repository_id,
                    expected_repository_key=self.identity.repository_key,
                    expected_repository_root=self.identity.repository_root,
                )
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

        self.assertEqual(backup_path.read_bytes(), backup_bytes)
        self.assertEqual(manifest_path.read_bytes(), manifest_bytes)
        self.assertEqual(self._sidecars(self.destination), set())
        self.assertEqual(list(self.restore_root.iterdir()), [])

    def test_mount_identity_requires_exact_cifs_share_and_path(self) -> None:
        mount = self.root / "nas-mount"
        destination = mount / "repo-control-ii"
        destination.mkdir(parents=True, mode=0o700)
        expected = f"31 24 0:42 / {mount} rw,relatime - cifs //192.168.1.171/RepoControl rw"
        self.assertEqual(
            parse_mountinfo(expected)[0][1:],
            ("cifs", "//192.168.1.171/RepoControl", Path("/")),
        )
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
        with self.assertRaisesRegex(BackupError, "absent or ambiguous"):
            verify_destination_mount(destination, mount, "//192.168.1.171/RepoControl", mountinfo="")

    def test_sandbox_destination_bind_mount_must_match_exact_cifs_child(self) -> None:
        mount = self.root / "nas-mount"
        destination = mount / "repo-control-ii"
        destination.mkdir(parents=True, mode=0o700)
        expected_source = "//192.168.1.171/ProjectBackups"
        mountinfo = "\n".join((
            f"31 24 0:42 / {mount} rw,relatime - cifs {expected_source} rw",
            f"42 31 0:42 /repo-control-ii {destination} rw,relatime - cifs {expected_source} rw",
        ))

        verify_destination_mount(
            destination,
            mount,
            expected_source,
            mountinfo=mountinfo,
        )

        lines = mountinfo.splitlines()
        invalid_mountinfo = {
            "wrong share": "\n".join((
                lines[0],
                lines[1].replace(expected_source, "//192.168.1.171/OtherShare"),
            )),
            "wrong child path": mountinfo.replace(
                "/repo-control-ii ",
                "/other-repository ",
                1,
            ),
            "ambiguous destination mount": mountinfo + "\n" + mountinfo.splitlines()[1],
        }
        for reason, invalid in invalid_mountinfo.items():
            with self.subTest(reason=reason):
                with self.assertRaises(BackupError):
                    verify_destination_mount(
                        destination,
                        mount,
                        expected_source,
                        mountinfo=invalid,
                    )

    def test_mount_root_needs_traversal_access_but_destination_needs_write_access(self) -> None:
        mount = self.root / "nas-mount"
        destination = mount / "repo-control-ii"
        destination.mkdir(parents=True, mode=0o700)
        mountinfo = f"31 24 0:42 / {mount} rw,relatime - cifs //192.168.1.171/RepoControl rw"
        access_checks: list[tuple[Path, int]] = []

        def simulated_access(
            path: str | os.PathLike[str],
            mode: int,
            *,
            destination_writable: bool = True,
        ) -> bool:
            resolved = Path(path)
            access_checks.append((resolved, mode))
            if resolved == mount:
                return not mode & os.W_OK
            if resolved == destination and not destination_writable and mode & os.W_OK:
                return False
            return True

        with patch("repoctl.backup.os.access", side_effect=simulated_access):
            verify_destination_mount(
                destination,
                mount,
                "//192.168.1.171/RepoControl",
                mountinfo=mountinfo,
            )

        self.assertIn((mount, os.R_OK | os.X_OK), access_checks)
        self.assertIn((destination, os.R_OK | os.X_OK | os.W_OK), access_checks)
        with patch(
            "repoctl.backup.os.access",
            side_effect=lambda path, mode: simulated_access(
                path,
                mode,
                destination_writable=False,
            ),
        ):
            with self.assertRaisesRegex(BackupError, "NAS backup destination is not readable and writable"):
                verify_destination_mount(
                    destination,
                    mount,
                    "//192.168.1.171/RepoControl",
                    mountinfo=mountinfo,
                )

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
        self.assertIn("UNRESOLVED BACKUP WARNING: NAS backup destination check failed: NAS unavailable",
                      error_output.getvalue())
        send_alert.assert_not_called()


class BackupSandboxFixtureTests(unittest.TestCase):
    def test_disposable_setup_preserves_sidecar_cases_and_bounded_cleanup(self) -> None:
        with tempfile.TemporaryDirectory(prefix="m011004-fixture-test-") as temporary:
            root = Path(temporary).resolve() / "m011004-sandbox-test"
            before = backup_sandbox_fixture.prepare(root)
            self.assertFalse(before["prepared_shm_exists"])
            self.assertTrue(backup_sandbox_fixture.inspect(root)["protected_unchanged"])
            for case in backup_sandbox_fixture.CASES:
                case_root = root / case
                self.assertTrue((case_root / "protected/wal/managed.sqlite3-shm").is_file())
                self.assertFalse((case_root / "protected/clean-wal/managed.sqlite3-shm").exists())
                self.assertEqual(list((case_root / "prepared").iterdir()), [])
            intruder = root / "not-owned-by-fixture"
            intruder.write_text("leave this file", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "operation refused"):
                backup_sandbox_fixture.cleanup(root)
            with self.assertRaisesRegex(ValueError, "operation refused"):
                backup_sandbox_fixture.probe(root, "separate")
            self.assertEqual(intruder.read_text(encoding="utf-8"), "leave this file")
            intruder.unlink()
            backup_sandbox_fixture.cleanup(root)
            self.assertFalse(root.exists())

    def test_probe_refuses_execution_without_hardening_before_writes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="m011004-fixture-test-") as temporary:
            root = Path(temporary).resolve() / "m011004-sandbox-test"
            backup_sandbox_fixture.prepare(root)
            real_read = Path.read_text

            def read_without_hardening(path: Path, *args, **kwargs):
                if path == Path("/proc/self/status"):
                    return "NoNewPrivs:\t0\n"
                return real_read(path, *args, **kwargs)

            with patch.object(Path, "read_text", read_without_hardening):
                with self.assertRaisesRegex(ValueError, "no fixture writes"):
                    backup_sandbox_fixture.probe(root, "separate")
            observed = backup_sandbox_fixture.inspect(root)
            self.assertTrue(observed["protected_unchanged"])
            self.assertEqual(observed["reports"], {})
            backup_sandbox_fixture.cleanup(root)

    @unittest.skipUnless(shutil.which("setpriv"), "setpriv is unavailable for the unprivileged negative control")
    def test_negative_control_rejects_unconfined_writes_instead_of_claiming_pass(self) -> None:
        with tempfile.TemporaryDirectory(prefix="m011004-fixture-test-") as temporary:
            root = Path(temporary).resolve() / "m011004-sandbox-test"
            backup_sandbox_fixture.prepare(root)
            for _ in range(2):
                result = subprocess.run(
                    [
                        "setpriv", "--no-new-privs", sys.executable, "-m", "tests.backup_sandbox_fixture",
                        "probe", "--root", str(root), "--case", "parent",
                    ],
                    capture_output=True, text=True, timeout=10, check=False,
                )
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                report = json.loads(result.stdout)
                self.assertFalse(report["passed"])
                self.assertTrue(report["no_new_privileges"])
                self.assertTrue(any(attempt["allowed"] for attempt in report["attempts"]))
                self.assertTrue(report["prepared_create_rename"])
                self.assertTrue(report["prepared_sidecars_removed"])
            backup_sandbox_fixture.cleanup(root)


if __name__ == "__main__":
    unittest.main()
