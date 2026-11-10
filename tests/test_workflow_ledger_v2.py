from __future__ import annotations

import hashlib
import tempfile
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from repoctl.workflow import ledger_v2
from repoctl.workflow import lifecycle
from repoctl.workflow.context_verification import make_verification_record


class WorkflowLedgerV2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.anchor = self.root / "repo" / "docs" / "milestones" / "M003.001_example_workflow.md"
        self.anchor.parent.mkdir(parents=True)
        self.state_root = self.root / "state"
        self.repository_id = "repo-control-test--0123456789ab"
        self.milestone_id = "M003.001"
        self.prompt_path = "docs/milestones/M003.001_example_prompt.md"
        self.prompt_sha256 = hashlib.sha256(b"# Prompt\n").hexdigest()
        self.closeout_path = "docs/milestones/M003.001_example_closeout.md"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def create(self) -> dict:
        return ledger_v2.create_v2(
            self.anchor,
            self.repository_id,
            self.milestone_id,
            self.prompt_path,
            self.prompt_sha256,
            state_root=self.state_root,
        )

    def event(
        self,
        event_type: str,
        phase_before: str,
        phase_after: str,
        *,
        actor_role: str = "coder",
        po_approved: bool = False,
        fields: dict | None = None,
    ) -> dict:
        timestamp = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=self._clock)
        self._clock += 1
        return {
            "event_id": uuid.uuid4().hex,
            "timestamp": timestamp.isoformat(),
            "event_type": event_type,
            "summary": event_type.replace("_", " "),
            "actor_role": actor_role,
            "actor_attribution": "Reported test actor.",
            "po_approved": po_approved,
            "phase_before": phase_before,
            "phase_after": phase_after,
            "waiting_category_after": None,
            "waiting_reason_after": None,
            **(fields or {}),
        }

    def append(self, manifest: dict, events: list[dict], tx: str | None = None) -> dict:
        return ledger_v2.append_events(
            self.anchor,
            self.repository_id,
            self.milestone_id,
            self.prompt_path,
            self.prompt_sha256,
            events,
            state_root=self.state_root,
            expected_generation=manifest["generation"],
            transaction_id=tx or uuid.uuid4().hex,
            closeout_path=self.closeout_path,
        )

    def start_events(self) -> list[dict]:
        self._clock = 0
        approved = self.event(
            "prompt_approved",
            "PLANNING",
            "READY",
            actor_role="product_owner",
            po_approved=True,
        )
        approved["binding"] = True
        started = self.event("implementation_started", "READY", "IN_PROGRESS")
        return [approved, started]

    def test_btree_splits_and_ordered_range_reads(self) -> None:
        directory = self.root / "index"
        directory.mkdir()
        directory_fd = ledger_v2._safe_dir(directory)
        try:
            tree = ledger_v2.PersistentBTree(directory_fd, None, page_limit=1024)
            for number in range(500):
                tree.set(f"{number:06d}", {"number": number})
            self.assertEqual(tree.get("000217"), {"number": 217})
            items = list(tree.items())
            self.assertEqual([key for key, _ in items], [f"{number:06d}" for number in range(500)])
            self.assertEqual(list(tree.items_from("000217"))[0], ("000217", {"number": 217}))
            self.assertEqual(tree.floor_item("000217"), ("000217", {"number": 217}))
        finally:
            directory_fd and __import__("os").close(directory_fd)

    def test_append_audit_and_snapshot_pagination(self) -> None:
        manifest = self.create()
        started = self.append(manifest, self.start_events())
        manifest = started["manifest"]
        question = self.event(
            "question_submitted",
            "IN_PROGRESS",
            "WAITING",
            fields={
                "waiting_category": "clarification",
                "waiting_reason": "Need an answer.",
                "waiting_category_after": "clarification",
                "waiting_reason_after": "Need an answer.",
            },
        )
        resolution = self.event(
            "answer_recorded",
            "WAITING",
            "WAITING",
            fields={
                "related_event_id": question["event_id"],
                "waiting_category_after": "clarification",
                "waiting_reason_after": "Need an answer.",
            },
        )
        resumed = self.event("implementation_resumed", "WAITING", "IN_PROGRESS")
        resumed_events = self.append(manifest, [question, resolution, resumed])
        manifest = resumed_events["manifest"]
        audit = ledger_v2.audit_history(
            self.anchor,
            self.repository_id,
            self.milestone_id,
            self.prompt_path,
            self.prompt_sha256,
        )
        self.assertTrue(audit["history_verified"])
        self.assertEqual(audit["event_count"], 5)
        first_page = ledger_v2.page_events(
            self.anchor,
            self.repository_id,
            self.milestone_id,
            self.prompt_path,
            self.prompt_sha256,
            limit=2,
        )
        self.assertEqual(first_page["first_sequence"], 1)
        self.assertEqual(first_page["last_sequence"], 2)
        self.assertFalse(first_page["history_verified"])
        second_page = ledger_v2.page_events(
            self.anchor,
            self.repository_id,
            self.milestone_id,
            self.prompt_path,
            self.prompt_sha256,
            limit=2,
            cursor=first_page["next_cursor"],
        )
        self.assertEqual(second_page["first_sequence"], 3)
        self.assertEqual(second_page["last_sequence"], 4)
        decision = self.event(
            "decision_recorded",
            "IN_PROGRESS",
            "IN_PROGRESS",
            fields={"binding": False},
        )
        self.append(manifest, [decision])
        with self.assertRaises(ledger_v2.StaleCursorError):
            ledger_v2.page_events(
                self.anchor,
                self.repository_id,
                self.milestone_id,
                self.prompt_path,
                self.prompt_sha256,
                limit=2,
                cursor=second_page["next_cursor"],
            )

    def test_page_byte_limit_includes_response_metadata(self) -> None:
        manifest = self.create()
        manifest = self.append(manifest, self.start_events())["manifest"]
        large = self.event("decision_recorded", "IN_PROGRESS", "IN_PROGRESS")
        large["summary"] = "D" * 2000
        large["binding"] = False
        small = self.event("decision_recorded", "IN_PROGRESS", "IN_PROGRESS")
        small["binding"] = False
        self.append(manifest, [large, small])

        full_page = ledger_v2.page_events(
            self.anchor,
            self.repository_id,
            self.milestone_id,
            self.prompt_path,
            self.prompt_sha256,
            limit=2,
        )
        one_event_page = ledger_v2.page_events(
            self.anchor,
            self.repository_id,
            self.milestone_id,
            self.prompt_path,
            self.prompt_sha256,
            limit=1,
        )
        maximum = len(ledger_v2.canonical_json_bytes(full_page)) - 1
        self.assertLessEqual(len(ledger_v2.canonical_json_bytes(one_event_page)), maximum)
        bounded_page = ledger_v2.page_events(
            self.anchor,
            self.repository_id,
            self.milestone_id,
            self.prompt_path,
            self.prompt_sha256,
            limit=2,
            max_bytes=maximum,
        )
        self.assertEqual(len(bounded_page["events"]), 1)
        self.assertLessEqual(len(ledger_v2.canonical_json_bytes(bounded_page)), maximum)

    def test_migration_preserves_v1_source_image_and_event_payloads(self) -> None:
        self._clock = 0
        events = self.start_events()
        ledger = lifecycle._empty_ledger(self.repository_id, {
            "milestone_id": self.milestone_id,
            "prompt_path": self.prompt_path,
        })
        ledger["events"] = events
        source = lifecycle._render_sidecar(ledger).encode("utf-8")
        self.anchor.write_bytes(source)
        candidate = {
            "milestone_id": self.milestone_id,
            "prompt_path": self.prompt_path,
            "closeout_exists": False,
            "closeout_path": None,
        }
        manifest = ledger_v2.create_migrated_v2(
            self.anchor,
            self.repository_id,
            self.milestone_id,
            self.prompt_path,
            self.prompt_sha256,
            events,
            source,
            state_root=self.state_root,
            transaction_id=uuid.uuid4().hex,
            fixture_only=True,
        )
        self.assertEqual(manifest["event_count"], len(events))
        segment_dir, _ = ledger_v2._sidecar_dirs(self.anchor)
        preserved = segment_dir / manifest["migration_source"]["path"]
        self.assertEqual(preserved.read_bytes(), source)
        self.assertEqual(
            ledger_v2.audit_history(
                self.anchor,
                self.repository_id,
                self.milestone_id,
                self.prompt_path,
                self.prompt_sha256,
            )["phase"],
            "IN_PROGRESS",
        )

    def test_v1_migration_requires_fixture_only_acknowledgement(self) -> None:
        events = self.start_events()
        ledger = lifecycle._empty_ledger(
            self.repository_id,
            {"milestone_id": self.milestone_id, "prompt_path": self.prompt_path},
        )
        ledger["events"] = events
        source = lifecycle._render_sidecar(ledger).encode("utf-8")
        self.anchor.write_bytes(source)
        with self.assertRaisesRegex(ledger_v2.LedgerV2Error, "disposable fixtures"):
            ledger_v2.create_migrated_v2(
                self.anchor,
                self.repository_id,
                self.milestone_id,
                self.prompt_path,
                self.prompt_sha256,
                events,
                source,
                state_root=self.state_root,
                transaction_id=uuid.uuid4().hex,
            )
        self.assertEqual(self.anchor.read_bytes(), source)

    def test_migration_precommit_failure_preserves_v1_and_retries_large_source(self) -> None:
        events = self.start_events()
        for _ in range(300):
            event = self.event("decision_recorded", "IN_PROGRESS", "IN_PROGRESS")
            event["summary"] = "D" * 900
            event["binding"] = False
            events.append(event)
        ledger = lifecycle._empty_ledger(
            self.repository_id,
            {"milestone_id": self.milestone_id, "prompt_path": self.prompt_path},
        )
        ledger["events"] = events
        source = lifecycle._render_sidecar(ledger).encode("utf-8")
        self.assertGreater(len(source), ledger_v2.MAX_SEGMENT_BYTES)
        self.anchor.write_bytes(source)
        transaction_id = uuid.uuid4().hex

        with patch(
            "repoctl.workflow.ledger_v2._commit_anchor",
            side_effect=ledger_v2.LedgerV2Error("injected precommit failure"),
        ):
            with self.assertRaisesRegex(ledger_v2.LedgerV2Error, "injected"):
                ledger_v2.create_migrated_v2(
                    self.anchor,
                    self.repository_id,
                    self.milestone_id,
                    self.prompt_path,
                    self.prompt_sha256,
                    events,
                    source,
                    state_root=self.state_root,
                    transaction_id=transaction_id,
                    fixture_only=True,
                )
        self.assertEqual(self.anchor.read_bytes(), source)

        manifest = ledger_v2.create_migrated_v2(
            self.anchor,
            self.repository_id,
            self.milestone_id,
            self.prompt_path,
            self.prompt_sha256,
            events,
            source,
            state_root=self.state_root,
            transaction_id=transaction_id,
            fixture_only=True,
        )
        self.assertEqual(manifest["event_count"], len(events))
        self.assertTrue(
            ledger_v2.audit_history(
                self.anchor,
                self.repository_id,
                self.milestone_id,
                self.prompt_path,
                self.prompt_sha256,
            )["history_verified"]
        )

    def test_postcommit_migration_uncertainty_reconciles_by_transaction(self) -> None:
        events = self.start_events()
        ledger = lifecycle._empty_ledger(
            self.repository_id,
            {"milestone_id": self.milestone_id, "prompt_path": self.prompt_path},
        )
        ledger["events"] = events
        source = lifecycle._render_sidecar(ledger).encode("utf-8")
        self.anchor.write_bytes(source)
        transaction_id = uuid.uuid4().hex
        original_commit = ledger_v2._commit_anchor

        def commit_then_raise(path, content, expected_old_hash, **kwargs):
            original_commit(path, content, expected_old_hash, **kwargs)
            raise ledger_v2.CommitUncertainError("injected postcommit acknowledgement loss")

        with patch(
            "repoctl.workflow.ledger_v2._commit_anchor",
            side_effect=commit_then_raise,
        ):
            with self.assertRaises(ledger_v2.CommitUncertainError):
                ledger_v2.create_migrated_v2(
                    self.anchor,
                    self.repository_id,
                    self.milestone_id,
                    self.prompt_path,
                    self.prompt_sha256,
                    events,
                    source,
                    state_root=self.state_root,
                    transaction_id=transaction_id,
                    fixture_only=True,
                )

        manifest = ledger_v2.create_migrated_v2(
            self.anchor,
            self.repository_id,
            self.milestone_id,
            self.prompt_path,
            self.prompt_sha256,
            events,
            source,
            state_root=self.state_root,
            transaction_id=transaction_id,
            fixture_only=True,
        )
        self.assertEqual(manifest["transaction_id"], transaction_id)
        self.assertTrue(
            ledger_v2.audit_history(
                self.anchor,
                self.repository_id,
                self.milestone_id,
                self.prompt_path,
                self.prompt_sha256,
            )["history_verified"]
        )

    def test_transaction_retry_reconciles_without_duplicate_events(self) -> None:
        manifest = self.create()
        events = self.start_events()
        transaction_id = uuid.uuid4().hex
        first = self.append(manifest, events, transaction_id)
        retry = self.append(manifest, events, transaction_id)
        self.assertFalse(first["reconciled"])
        self.assertTrue(retry["reconciled"])
        self.assertEqual(retry["manifest"]["event_count"], len(events))

        altered = [dict(event) for event in events]
        altered[0]["summary"] = "different payload"
        with self.assertRaisesRegex(ledger_v2.LedgerV2Error, "reused"):
            self.append(manifest, altered, transaction_id)

    def test_postcommit_uncertainty_is_not_reported_as_success(self) -> None:
        manifest = self.create()
        events = self.start_events()
        transaction_id = uuid.uuid4().hex
        original_commit = ledger_v2._commit_anchor

        def commit_then_raise(path, content, expected_old_hash, **kwargs):
            original_commit(path, content, expected_old_hash, **kwargs)
            raise ledger_v2.CommitUncertainError("injected postcommit acknowledgement loss")

        with patch(
            "repoctl.workflow.ledger_v2._commit_anchor",
            side_effect=commit_then_raise,
        ):
            with self.assertRaisesRegex(ledger_v2.CommitUncertainError, "uncertain"):
                self.append(manifest, events, transaction_id)
        audit = ledger_v2.audit_history(
            self.anchor,
            self.repository_id,
            self.milestone_id,
            self.prompt_path,
            self.prompt_sha256,
        )
        self.assertEqual(audit["event_count"], len(events))
        retry = self.append(manifest, events, transaction_id)
        self.assertTrue(retry["reconciled"])

    def test_uncommitted_transaction_marker_blocks_other_writes_and_allows_exact_retry(self) -> None:
        manifest = self.create()
        events = self.start_events()
        transaction_id = uuid.uuid4().hex
        with patch(
            "repoctl.workflow.ledger_v2._commit_anchor",
            side_effect=ledger_v2.LedgerV2Error("injected precommit failure"),
        ):
            with self.assertRaisesRegex(ledger_v2.LedgerV2Error, "injected"):
                self.append(manifest, events, transaction_id)

        audit = ledger_v2.audit_history(
            self.anchor,
            self.repository_id,
            self.milestone_id,
            self.prompt_path,
            self.prompt_sha256,
        )
        self.assertEqual(audit["recovery_status"], "unresolved")
        self.assertEqual(audit["pending_transaction_id"], transaction_id)
        self.assertEqual(len(audit["pending_segment_names"]), 1)

        different = self.event("decision_recorded", "PLANNING", "PLANNING")
        different["binding"] = False
        with self.assertRaisesRegex(ledger_v2.LedgerV2Error, "blocks writes"):
            self.append(manifest, [different], uuid.uuid4().hex)

        retry = self.append(manifest, events, transaction_id)
        self.assertFalse(retry["reconciled"])
        audit = ledger_v2.audit_history(
            self.anchor,
            self.repository_id,
            self.milestone_id,
            self.prompt_path,
            self.prompt_sha256,
        )
        self.assertEqual(audit["recovery_status"], "clean")

    def test_artifact_fsync_failure_blocks_until_same_transaction_retry(self) -> None:
        import os

        manifest = self.create()
        events = self.start_events()
        transaction_id = uuid.uuid4().hex
        original_fsync = os.fsync
        calls = 0

        def fail_segment_sync(fd: int) -> None:
            nonlocal calls
            calls += 1
            if calls == 3:
                raise OSError("injected segment fsync failure")
            original_fsync(fd)

        with patch("repoctl.workflow.ledger_v2.os.fsync", side_effect=fail_segment_sync):
            with self.assertRaisesRegex(ledger_v2.LedgerV2Error, "durably write"):
                self.append(manifest, events, transaction_id)
        audit = ledger_v2.audit_history(
            self.anchor,
            self.repository_id,
            self.milestone_id,
            self.prompt_path,
            self.prompt_sha256,
        )
        self.assertEqual(audit["recovery_status"], "unresolved")
        self.assertEqual(len(audit["pending_segment_names"]), 1)

        retry = self.append(manifest, events, transaction_id)
        self.assertFalse(retry["reconciled"])
        self.assertEqual(
            ledger_v2.audit_history(
                self.anchor,
                self.repository_id,
                self.milestone_id,
                self.prompt_path,
                self.prompt_sha256,
            )["recovery_status"],
            "clean",
        )

    def test_partial_pending_record_write_is_isolated_and_retryable(self) -> None:
        import os

        manifest = self.create()
        events = self.start_events()
        transaction_id = uuid.uuid4().hex
        original_write = os.write
        calls = 0

        def partial_then_fail(fd: int, data: bytes) -> int:
            nonlocal calls
            calls += 1
            if calls == 1:
                return original_write(fd, data[:1])
            raise OSError("injected partial recovery-record write")

        with patch("repoctl.workflow.ledger_v2.os.write", side_effect=partial_then_fail):
            with self.assertRaisesRegex(ledger_v2.LedgerV2Error, "recovery temporary"):
                self.append(manifest, events, transaction_id)

        _, index_dir = ledger_v2._sidecar_dirs(self.anchor)
        pending = index_dir / ledger_v2._PENDING_NAME
        temporary_files = list(index_dir.glob(".pending-transaction-*.tmp"))
        self.assertFalse(pending.exists())
        self.assertEqual(len(temporary_files), 1)
        self.assertEqual(temporary_files[0].read_bytes(), b"{")

        audit = ledger_v2.audit_history(
            self.anchor,
            self.repository_id,
            self.milestone_id,
            self.prompt_path,
            self.prompt_sha256,
        )
        self.assertTrue(audit["history_verified"])
        self.assertEqual(audit["event_count"], 0)
        self.assertEqual(audit["recovery_status"], "orphan_pending_temporary")
        self.assertEqual(audit["pending_temporary_count"], 1)

        result = self.append(manifest, events, transaction_id)
        self.assertFalse(result["reconciled"])
        audit = ledger_v2.audit_history(
            self.anchor,
            self.repository_id,
            self.milestone_id,
            self.prompt_path,
            self.prompt_sha256,
        )
        self.assertTrue(audit["history_verified"])
        self.assertEqual(audit["event_count"], len(events))
        self.assertEqual(audit["recovery_status"], "orphan_pending_temporary")
        page = ledger_v2.page_events(
            self.anchor,
            self.repository_id,
            self.milestone_id,
            self.prompt_path,
            self.prompt_sha256,
        )
        self.assertEqual([event["event_id"] for event in page["events"]], [
            event["event_id"] for event in events
        ])
        self.assertEqual(temporary_files[0].read_bytes(), b"{")

    def test_audit_reports_malformed_pending_marker_separately_from_history(self) -> None:
        manifest = self.create()
        _, index_dir = ledger_v2._sidecar_dirs(self.anchor)
        (index_dir / ledger_v2._PENDING_NAME).write_bytes(b"{")

        audit = ledger_v2.audit_history(
            self.anchor,
            self.repository_id,
            self.milestone_id,
            self.prompt_path,
            self.prompt_sha256,
        )
        self.assertTrue(audit["history_verified"])
        self.assertEqual(audit["event_count"], 0)
        self.assertEqual(audit["recovery_status"], "invalid_pending_marker")
        self.assertIn("malformed", audit["recovery_error"])

        event = self.start_events()[0]
        with self.assertRaisesRegex(ledger_v2.LedgerV2Error, "malformed"):
            self.append(manifest, [event], uuid.uuid4().hex)

    def test_published_pending_marker_survives_directory_sync_uncertainty(self) -> None:
        import os

        manifest = self.create()
        events = self.start_events()
        transaction_id = uuid.uuid4().hex
        original_fsync = os.fsync
        calls = 0

        def fail_pending_directory_sync(fd: int) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("injected pending directory fsync failure")
            original_fsync(fd)

        with patch("repoctl.workflow.ledger_v2.os.fsync", side_effect=fail_pending_directory_sync):
            with self.assertRaisesRegex(ledger_v2.LedgerV2Error, "published"):
                self.append(manifest, events, transaction_id)

        _, index_dir = ledger_v2._sidecar_dirs(self.anchor)
        pending = index_dir / ledger_v2._PENDING_NAME
        self.assertTrue(pending.exists())
        self.assertEqual(
            ledger_v2.audit_history(
                self.anchor,
                self.repository_id,
                self.milestone_id,
                self.prompt_path,
                self.prompt_sha256,
            )["recovery_status"],
            "unresolved",
        )
        retry = self.append(manifest, events, transaction_id)
        self.assertFalse(retry["reconciled"])
        audit = ledger_v2.audit_history(
            self.anchor,
            self.repository_id,
            self.milestone_id,
            self.prompt_path,
            self.prompt_sha256,
        )
        self.assertTrue(audit["history_verified"])
        self.assertEqual(audit["event_count"], len(events))
        self.assertEqual(audit["recovery_status"], "clean")

    def test_tampered_segment_and_index_pages_fail_closed(self) -> None:
        manifest = self.create()
        manifest = self.append(manifest, self.start_events())["manifest"]
        segment_dir, index_dir = ledger_v2._sidecar_dirs(self.anchor)
        segment = next(segment_dir.glob("*.json"))
        original_segment = segment.read_bytes()
        segment.write_bytes(original_segment + b" ")
        with self.assertRaisesRegex(ledger_v2.LedgerV2Error, "digest"):
            ledger_v2.audit_history(
                self.anchor,
                self.repository_id,
                self.milestone_id,
                self.prompt_path,
                self.prompt_sha256,
            )
        segment.write_bytes(original_segment)
        segment.write_bytes(original_segment + b" ")
        decision = self.event("decision_recorded", "IN_PROGRESS", "IN_PROGRESS")
        decision["binding"] = False
        with self.assertRaisesRegex(ledger_v2.LedgerV2Error, "digest"):
            self.append(manifest, [decision])
        segment.write_bytes(original_segment)

        root_hash = manifest["roots"]["segment_catalog"]
        page = index_dir / f"{root_hash}.json"
        page.write_bytes(page.read_bytes() + b" ")
        with self.assertRaises(ledger_v2.LedgerV2Error):
            ledger_v2.audit_history(
                self.anchor,
                self.repository_id,
                self.milestone_id,
                self.prompt_path,
                self.prompt_sha256,
            )

    def test_symlinked_sidecar_directory_is_rejected(self) -> None:
        segment_dir, _ = ledger_v2._sidecar_dirs(self.anchor)
        target = self.root / "target"
        target.mkdir()
        segment_dir.symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(ledger_v2.LedgerV2Error, "regular directory"):
            self.create()

    def test_initial_anchor_directory_sync_failure_is_uncertain(self) -> None:
        import os

        self.anchor.parent.mkdir(parents=True, exist_ok=True)
        original_fsync = os.fsync
        calls = 0

        def fail_directory_sync(fd: int) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("injected directory fsync failure")
            original_fsync(fd)

        with patch("repoctl.workflow.ledger_v2.os.fsync", side_effect=fail_directory_sync):
            with self.assertRaises(ledger_v2.CommitUncertainError):
                ledger_v2._commit_anchor(self.anchor, b"committed anchor", None)
        self.assertEqual(self.anchor.read_bytes(), b"committed anchor")

    def test_precommit_anchor_temporary_is_retained_and_reused(self) -> None:
        self.anchor.parent.mkdir(parents=True, exist_ok=True)
        transaction_id = uuid.uuid4().hex
        content = b"complete anchor image"
        temporary = self.anchor.with_name(f".{self.anchor.name}.{transaction_id}.tmp")
        with patch("repoctl.workflow.ledger_v2.os.fsync", side_effect=OSError("injected fsync")):
            with self.assertRaisesRegex(ledger_v2.LedgerV2Error, "commit failed"):
                ledger_v2._commit_anchor(
                    self.anchor,
                    content,
                    None,
                    transaction_id=transaction_id,
                )
        self.assertEqual(temporary.read_bytes(), content)
        ledger_v2._commit_anchor(
            self.anchor,
            content,
            None,
            transaction_id=transaction_id,
        )
        self.assertEqual(self.anchor.read_bytes(), content)
        self.assertFalse(temporary.exists())

    def test_incremental_projection_matches_full_replay_for_all_event_types(self) -> None:
        manifest = self.create()
        self._clock = 0
        history: list[dict] = []
        phase = "PLANNING"
        category = None
        reason = None

        def append(
            event_type: str,
            *,
            actor_role: str = "coder",
            po_approved: bool = False,
            fields: dict | None = None,
        ) -> dict:
            nonlocal manifest, phase, category, reason
            event = self.event(
                event_type,
                phase,
                phase,
                actor_role=actor_role,
                po_approved=po_approved,
                fields={
                    "waiting_category_after": category,
                    "waiting_reason_after": reason,
                    **(fields or {}),
                },
            )
            next_phase, next_category, next_reason = lifecycle._apply_event(
                phase, category, reason, event, history,
            )
            event.update(
                phase_after=next_phase,
                waiting_category_after=next_category,
                waiting_reason_after=next_reason,
            )
            manifest = self.append(manifest, [event])["manifest"]
            history.append(event)
            replayed = lifecycle._reduce_events(history)
            self.assertEqual(manifest["checkpoint"]["phase"], replayed["phase"], event_type)
            self.assertEqual(
                manifest["checkpoint"]["waiting_category"],
                replayed["waiting_category"],
                event_type,
            )
            self.assertEqual(
                manifest["checkpoint"]["waiting_reason"],
                replayed["waiting_reason"],
                event_type,
            )
            self.assertEqual(manifest["checkpoint"]["event_count"], len(history), event_type)
            audit = ledger_v2.audit_history(
                self.anchor,
                self.repository_id,
                self.milestone_id,
                self.prompt_path,
                self.prompt_sha256,
                closeout_path=self.closeout_path,
            )
            self.assertTrue(audit["history_verified"], event_type)
            self.assertEqual(audit["event_count"], len(history), event_type)
            phase, category, reason = next_phase, next_category, next_reason
            return event

        prompt = append(
            "prompt_approved",
            actor_role="product_owner",
            po_approved=True,
            fields={"binding": True},
        )
        self.assertEqual(prompt["phase_after"], "READY")
        append("implementation_started")
        question = append("question_submitted")
        append("answer_recorded", fields={"related_event_id": question["event_id"]})
        append("decision_recorded", fields={"binding": False})
        append(
            "lock_in_approved",
            actor_role="product_owner",
            po_approved=True,
            fields={"binding": True},
        )
        append(
            "escalation_opened",
            fields={
                "waiting_category": "escalation",
                "waiting_reason": "Need escalation.",
            },
        )
        append("escalation_resolved")
        append("implementation_resumed")
        bug = append("bug_discovered")
        append(
            "bug_classified",
            fields={"related_event_id": bug["event_id"], "bug_scope": "in_scope"},
        )
        append("bug_fixed", fields={"related_event_id": bug["event_id"]})
        append(
            "test_result_recorded",
            fields={"evidence_source": "unit test", "result": "passed"},
        )
        (self.anchor.parent / Path(self.closeout_path).name).write_text("# Closeout\n", encoding="utf-8")
        append("closeout_submitted", fields={"evidence_path": self.closeout_path})
        append("review_requested")
        append("correction_requested")
        append("closeout_submitted", fields={"evidence_path": self.closeout_path})
        append(
            "acceptance_recorded",
            actor_role="product_owner",
            po_approved=True,
            fields={"binding": True},
        )
        append(
            "milestone_closed",
            actor_role="product_owner",
            po_approved=True,
            fields={"binding": True},
        )
        append(
            "milestone_reopened",
            actor_role="product_owner",
            po_approved=True,
            fields={"reopen_target_phase": "IN_PROGRESS"},
        )
        append("git_disposition_recorded")
        snapshot = {
            "repository": {"key": "repo", "id": "repo--123", "root": str(self.root / "repo")},
            "milestone": {
                "id": self.milestone_id,
                "prompt_path": self.prompt_path,
                "prompt_sha256": self.prompt_sha256,
                "lifecycle": "IN_PROGRESS",
                "authorization": {"authorized": True, "status": "AUTHORIZED"},
                "closeout": {"exists": True, "path": self.closeout_path},
            },
            "workflow_evidence": {"test_result_count": 1},
            "approved_architect_dispatches": {"inspection_status": "available"},
            "procedure": {
                "inspection_status": "available",
                "status": "CURRENT",
                "overlay_valid": True,
            },
            "git": {"inspection_status": "available"},
        }
        verification = make_verification_record(
            trigger="NEW",
            snapshot=snapshot,
            result="VERIFIED",
            git_boundary_acknowledged=True,
            runtime_compatibility={
                "status": "COMPATIBLE",
                "runtime_instance_id": "b" * 32,
                "producer_contract_id": "repoctl-workflow-event-writer:v1",
                "event_type": "context_verification_recorded",
                "required_event_capabilities": {
                    "context_verification_record_schema_version": 2,
                },
                "ledger_schema_version": 1,
                "event_schema_version": 1,
                "context_verification_record_schema_version": 2,
            },
        )
        append(
            lifecycle.VERIFICATION_EVENT_TYPE,
            fields={"context_verification": verification},
        )
        self.assertEqual({event["event_type"] for event in history}, lifecycle._EVENT_TYPES)
        audit = ledger_v2.audit_history(
            self.anchor,
            self.repository_id,
            self.milestone_id,
            self.prompt_path,
            self.prompt_sha256,
            closeout_path=self.closeout_path,
        )
        self.assertTrue(audit["history_verified"])
        self.assertEqual(audit["event_count"], len(history))
        self.assertEqual(audit["phase"], phase)

    def test_large_history_crosses_v1_limits_and_pages_losslessly(self) -> None:
        manifest = self.create()
        self._clock = 0
        initial = self.start_events()
        manifest = self.append(manifest, initial)["manifest"]
        history = list(initial)
        thresholds = [1999, 2000, 2001, 5000, 10000]
        observed_counts: list[int] = []
        expected_prefixes: list[dict] = []
        total = len(history)

        for target in thresholds:
            while total < target:
                batch_size = min(ledger_v2.MAX_SEGMENT_EVENTS, target - total)
                batch = []
                for _ in range(batch_size):
                    event = self.event("decision_recorded", "IN_PROGRESS", "IN_PROGRESS")
                    event["summary"] = "D" * 900
                    event["binding"] = False
                    batch.append(event)
                prefix_checkpoints: list[dict] = []
                original_projection = ledger_v2._phase_projection

                def observe_projection(*args, **kwargs):
                    checkpoint = original_projection(*args, **kwargs)
                    prefix_checkpoints.append(dict(checkpoint))
                    return checkpoint

                with patch(
                    "repoctl.workflow.ledger_v2._phase_projection",
                    side_effect=observe_projection,
                ):
                    manifest = self.append(manifest, batch)["manifest"]
                self.assertEqual(len(prefix_checkpoints), len(batch))
                phase, category, reason = "IN_PROGRESS", None, None
                for offset, event in enumerate(batch):
                    next_phase, next_category, next_reason = lifecycle._apply_event(
                        phase,
                        category,
                        reason,
                        event,
                        history,
                    )
                    expected_prefix = prefix_checkpoints[offset]
                    self.assertEqual(expected_prefix["phase"], next_phase)
                    self.assertEqual(expected_prefix["waiting_category"], next_category)
                    self.assertEqual(expected_prefix["waiting_reason"], next_reason)
                    self.assertEqual(expected_prefix["event_count"], total + offset + 1)
                    history.append(event)
                    phase, category, reason = next_phase, next_category, next_reason
                total += len(batch)
            self.assertEqual(manifest["event_count"], target)
            self.assertEqual(manifest["checkpoint"]["phase"], "IN_PROGRESS")
            observed_counts.append(manifest["event_count"])

        event_bytes = len(ledger_v2.canonical_json_bytes(history))
        self.assertGreater(event_bytes, 5_000_000)
        self.assertEqual(observed_counts, thresholds)
        self.assertLess(self.anchor.stat().st_size, ledger_v2.MAX_ANCHOR_BYTES)
        segment_dir, index_dir = ledger_v2._sidecar_dirs(self.anchor)
        segment_total_bytes = 0
        for path in segment_dir.iterdir():
            if path.name.endswith(".json"):
                self.assertLessEqual(path.stat().st_size, ledger_v2.MAX_SEGMENT_BYTES)
                segment_total_bytes += path.stat().st_size
        index_page_count = 0
        for path in index_dir.iterdir():
            if path.name.endswith(".json"):
                self.assertLessEqual(path.stat().st_size, ledger_v2.MAX_INDEX_PAGE_BYTES)
                index_page_count += 1
        self.assertGreater(segment_total_bytes, event_bytes)
        self.assertGreater(index_page_count, 0)

        audit = ledger_v2.audit_history(
            self.anchor,
            self.repository_id,
            self.milestone_id,
            self.prompt_path,
            self.prompt_sha256,
        )
        self.assertTrue(audit["history_verified"])
        self.assertEqual(audit["event_count"], 10000)
        traversed: list[dict] = []
        cursor = None
        while True:
            page = ledger_v2.page_events(
                self.anchor,
                self.repository_id,
                self.milestone_id,
                self.prompt_path,
                self.prompt_sha256,
                limit=100,
                cursor=cursor,
            )
            self.assertLessEqual(
                len(ledger_v2.canonical_json_bytes(page)),
                ledger_v2.MAX_EVENT_PAGE_BYTES,
            )
            traversed.extend(page["events"])
            if not page["has_more"]:
                break
            cursor = page["next_cursor"]
        self.assertEqual([event["event_id"] for event in traversed], [event["event_id"] for event in history])
        self.assertEqual(traversed, history)

    def test_v1_parser_still_rejects_event_and_byte_caps(self) -> None:
        self._clock = 0
        events = self.start_events()
        for _ in range(1999):
            event = self.event("decision_recorded", "IN_PROGRESS", "IN_PROGRESS")
            event["binding"] = False
            events.append(event)
        ledger = lifecycle._empty_ledger(self.repository_id, {
            "milestone_id": self.milestone_id,
            "prompt_path": self.prompt_path,
        })
        ledger["events"] = events
        self.anchor.write_text(lifecycle._render_sidecar(ledger), encoding="utf-8")
        candidate = {
            "milestone_id": self.milestone_id,
            "prompt_path": self.prompt_path,
            "closeout_exists": False,
            "closeout_path": None,
        }
        with self.assertRaisesRegex(lifecycle.WorkflowRecordError, "2000-event"):
            lifecycle._read_ledger(self.anchor, self.repository_id, candidate)

        large_events = events[:]
        for event in large_events:
            event["summary"] = "B" * 2000
        large_ledger = dict(ledger, events=large_events)
        large_image = lifecycle._render_sidecar(large_ledger).encode("utf-8")
        self.assertGreater(len(large_image), lifecycle._MAX_SIDECAR_BYTES)
        self.anchor.write_bytes(large_image)
        with self.assertRaisesRegex(lifecycle.WorkflowRecordError, "5 MB"):
            lifecycle._read_ledger(self.anchor, self.repository_id, candidate)

    def test_unknown_and_mixed_markers_fail_closed(self) -> None:
        with self.assertRaises(ledger_v2.LedgerV2Error):
            ledger_v2.dispatch_marker("<!-- repoctl-workflow-ledger:v3 -->")
        with self.assertRaises(ledger_v2.LedgerV2Error):
            ledger_v2.dispatch_marker(ledger_v2.V1_MARKER + ledger_v2.V2_MARKER)

    def test_manifest_rejects_boolean_versions_and_malformed_roots(self) -> None:
        manifest = self.create()
        with self.assertRaises(ledger_v2.LedgerV2Error):
            ledger_v2._validate_manifest(
                dict(manifest, schema_version=True),
                self.repository_id,
                self.milestone_id,
                self.prompt_path,
                self.prompt_sha256,
            )
        roots = dict(manifest["roots"], event_ids=[])
        with self.assertRaises(ledger_v2.LedgerV2Error):
            ledger_v2._validate_manifest(
                dict(manifest, roots=roots),
                self.repository_id,
                self.milestone_id,
                self.prompt_path,
                self.prompt_sha256,
            )


if __name__ == "__main__":
    unittest.main()
