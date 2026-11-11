from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from repoctl.information_store import (
    APPLICATION_ID,
    MINIMUM_SQLITE_VERSION,
    SCHEMA_VERSION,
    BusyStoreError,
    DispatchDecisionError,
    DocumentRevisionInput,
    IdempotencyConflictError,
    IdentityMismatchError,
    InformationStoreError,
    ImportSourceError,
    InformationStore,
    RevisionConflictError,
    RuntimeGateError,
    SchemaMismatchError,
    StaleStateError,
    StoreIdentity,
    V1FixtureBundle,
    WorkflowDocumentLink,
    sqlite_runtime_status,
)
from repoctl.workflow import lifecycle
from repoctl.workflow import architect_dispatches
from tests.test_sqlite_workflow_poc import _all_event_types_history, _closeout_history, _event


FIXTURE_PROMPT_PATH = "fixtures/milestone_prompt.md"
FIXTURE_CLOSEOUT_PATH = "fixtures/closeout.md"


class InformationStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="repoctl_information_store_", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repository_root = self.root / "fixture-repository"
        self.identity = StoreIdentity(
            repository_id="fixture-repository-id",
            repository_key="fixture-repository",
            repository_root=str(self.repository_root),
            milestone_id="M999.001",
            prompt_path=FIXTURE_PROMPT_PATH,
            selection_id="fixture-selection-1",
            selection_revision=1,
        )
        self.database_path = self.root / "control.sqlite3"
        self.store = InformationStore.initialize_disposable(
            self.database_path,
            self.identity,
            allow_unsupported_runtime_for_fixture=True,
        )

    def _create_prompt(self, content: str = "# Fixture prompt\n") -> dict[str, str]:
        revision = DocumentRevisionInput(
            document_id="prompt-document",
            revision_id="prompt-revision-1",
            logical_key=FIXTURE_PROMPT_PATH,
            logical_type="PROMPT",
            source_path=FIXTURE_PROMPT_PATH,
            content=content,
        )
        return self.store.create_immutable_revision(revision)

    def _start_workflow(self) -> tuple[dict[str, str], list[dict]]:
        created = self._create_prompt()
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
        return created, events

    def _append(
        self,
        request_id: str,
        event: dict,
        *,
        expected_store_revision: int,
        prompt_revision_id: str | None,
        trusted_fixture_po_confirmation: bool = False,
        links: tuple[WorkflowDocumentLink, ...] = (),
        **overrides,
    ) -> dict:
        arguments = {
            "request_id": request_id,
            "expected_repository_id": self.identity.repository_id,
            "expected_milestone_id": self.identity.milestone_id,
            "expected_selection_id": self.identity.selection_id,
            "expected_selection_revision": self.identity.selection_revision,
            "expected_prompt_revision_id": prompt_revision_id,
            "expected_store_revision": expected_store_revision,
            "events": [event],
            "links": links,
            "trusted_fixture_po_confirmation": trusted_fixture_po_confirmation,
        }
        arguments.update(overrides)
        return self.store.append_workflow_batch(**arguments)

    def _counts(self) -> dict[str, int]:
        connection = sqlite3.connect(self.database_path)
        try:
            return {
                table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "document",
                    "document_revision",
                    "workflow_event",
                    "workflow_document_link",
                    "transaction_receipt",
                    "document_revision_decision",
                )
            }
        finally:
            connection.close()

    def test_runtime_gate_reports_actual_and_controlled_versions(self) -> None:
        actual = sqlite_runtime_status()
        controlled_low = sqlite_runtime_status((3, 51, 2))
        controlled_floor = sqlite_runtime_status(MINIMUM_SQLITE_VERSION)
        controlled_backport = sqlite_runtime_status(
            (3, 45, 1),
            fixed_backport_evidence="verified fixture evidence",
        )
        self.assertEqual(actual["sqlite_version_info"], sqlite3.sqlite_version_info)
        self.assertEqual(actual["production_eligible"], tuple(sqlite3.sqlite_version_info) >= MINIMUM_SQLITE_VERSION)
        self.assertFalse(controlled_low["production_eligible"])
        self.assertTrue(controlled_floor["production_eligible"])
        self.assertEqual(controlled_backport["eligibility_basis"], "explicit_fixed_backport_evidence")
        self.assertTrue(controlled_backport["production_eligible"])
        self.assertFalse(self.store.snapshot()["production_eligible"])

    def test_runtime_gate_requires_explicit_fixture_override_on_old_runtime(self) -> None:
        if tuple(sqlite3.sqlite_version_info) >= MINIMUM_SQLITE_VERSION:
            self.skipTest("Linked SQLite meets the production floor; older-runtime override is not applicable.")
        rejected_path = self.root / "must-not-be-created.sqlite3"
        with self.assertRaises(RuntimeGateError):
            InformationStore.initialize_disposable(rejected_path, self.identity)
        self.assertFalse(rejected_path.exists())

    def test_schema_application_id_and_identity_are_fail_closed(self) -> None:
        connection = sqlite3.connect(self.database_path)
        try:
            self.assertEqual(connection.execute("PRAGMA application_id").fetchone()[0], APPLICATION_ID)
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
        finally:
            connection.close()
        reopened = InformationStore.open_disposable(
            self.database_path,
            self.identity,
            allow_unsupported_runtime_for_fixture=True,
        )
        self.assertEqual(reopened.snapshot()["authority"], "disposable_fixture_only")
        wrong_identity = StoreIdentity(
            repository_id="other-repository-id",
            repository_key="other-repository",
            repository_root=str(self.root / "other-repository"),
            milestone_id=self.identity.milestone_id,
            prompt_path=self.identity.prompt_path,
            selection_id="other-selection",
            selection_revision=1,
        )
        with self.assertRaises(IdentityMismatchError):
            InformationStore.open_disposable(
                self.database_path,
                wrong_identity,
                allow_unsupported_runtime_for_fixture=True,
            )
        absent_path = self.root / "absent.sqlite3"
        with self.assertRaises(SchemaMismatchError):
            InformationStore.open_disposable(
                absent_path,
                self.identity,
                allow_unsupported_runtime_for_fixture=True,
            )
        self.assertFalse(absent_path.exists())

    def test_unknown_application_or_schema_id_is_rejected_without_repair(self) -> None:
        for name, app_id, user_version in (
            ("wrong-application", APPLICATION_ID + 1, SCHEMA_VERSION),
            ("newer-schema", APPLICATION_ID, SCHEMA_VERSION + 1),
        ):
            path = self.root / f"{name}.sqlite3"
            connection = sqlite3.connect(path)
            try:
                connection.execute(f"PRAGMA application_id = {app_id}")
                connection.execute(f"PRAGMA user_version = {user_version}")
                connection.commit()
            finally:
                connection.close()
            before = path.read_bytes()
            with self.assertRaises(SchemaMismatchError):
                InformationStore.open_disposable(
                    path,
                    self.identity,
                    allow_unsupported_runtime_for_fixture=True,
                )
            self.assertEqual(path.read_bytes(), before)

    def test_document_revision_is_immutable_and_exact_hash_retrieval_works(self) -> None:
        content = "# UTF-8 revision\nCafé 🙂\n"
        revision = self._create_prompt(content)
        found = self.store.get_revision(revision["document_id"], revision["revision_id"])
        self.assertEqual(found["content"], content)
        self.assertEqual(found["content_sha256"], hashlib.sha256(content.encode("utf-8")).hexdigest())
        self.assertEqual(
            self.store.get_revision_by_hash(revision["document_id"], revision["content_sha256"])["revision_id"],
            revision["revision_id"],
        )
        connection = sqlite3.connect(self.database_path)
        try:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                connection.execute(
                    "UPDATE document_revision SET content = ? WHERE revision_id = ?",
                    ("changed", revision["revision_id"]),
                )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                connection.execute(
                    "DELETE FROM document_revision WHERE revision_id = ?",
                    (revision["revision_id"],),
                )
        finally:
            connection.close()
        self.assertEqual(self.store.get_revision(revision["document_id"], revision["revision_id"])["content"], content)

    def test_utf8_document_pages_are_hash_bound_and_do_not_split_codepoints(self) -> None:
        content = "Aé🙂Z\n" * 8
        revision = self._create_prompt(content)
        collected = []
        offset = 0
        while offset < len(content.encode("utf-8")):
            page = self.store.read_revision_page(
                revision["document_id"],
                revision["revision_id"],
                byte_offset=offset,
                max_bytes=5,
            )
            collected.append(page["content"])
            self.assertEqual(page["content_sha256"], revision["content_sha256"])
            self.assertGreater(page["next_byte_offset"], offset)
            offset = page["next_byte_offset"]
        self.assertEqual("".join(collected), content)
        with self.assertRaises(ValueError):
            self.store.read_revision_page(
                revision["document_id"],
                revision["revision_id"],
                byte_offset=2,
                max_bytes=5,
            )
        with self.assertRaises(ValueError):
            self.store.read_revision_page(
                revision["document_id"],
                revision["revision_id"],
            byte_offset=1,
                max_bytes=1,
            )

    def test_document_listing_is_bounded_and_revision_identity_is_exact(self) -> None:
        prompt = self._create_prompt()
        closeout = DocumentRevisionInput(
            document_id="closeout-document",
            revision_id="closeout-revision-1",
            logical_key=FIXTURE_CLOSEOUT_PATH,
            logical_type="CLOSEOUT",
            source_path=FIXTURE_CLOSEOUT_PATH,
            content="# Closeout\n",
        )
        self.store.create_immutable_revision(closeout)
        first = self.store.list_documents(limit=1)
        self.assertEqual(len(first["documents"]), 1)
        self.assertIsNotNone(first["next_cursor"])
        second = self.store.list_documents(after_order=first["next_cursor"], limit=1)
        self.assertEqual(len(second["documents"]), 1)
        self.assertIsNone(second["next_cursor"])
        with self.assertRaises(RevisionConflictError):
            self.store.get_revision(prompt["document_id"], "wrong-revision")

    def test_prompt_approval_requires_explicit_fixture_po_confirmation(self) -> None:
        prompt = self._create_prompt()
        event = _closeout_history()[0]
        with self.assertRaisesRegex(InformationStoreError, "trusted fixture PO confirmation"):
            self.store.select_approved_prompt_revision(
                request_id="select-without-fixture-confirmation",
                expected_store_revision=0,
                document_id=prompt["document_id"],
                revision_id=prompt["revision_id"],
                content_sha256=prompt["content_sha256"],
                prompt_approved_event=event,
                trusted_fixture_po_confirmation=False,
            )
        self.assertEqual(self.store.snapshot()["event_count"], 0)
        self.assertIsNone(self.store.snapshot()["prompt_revision_id"])
        self.assertEqual(self._counts()["transaction_receipt"], 0)
        result = self.store.select_approved_prompt_revision(
            request_id="select-with-fixture-confirmation",
            expected_store_revision=0,
            document_id=prompt["document_id"],
            revision_id=prompt["revision_id"],
            content_sha256=prompt["content_sha256"],
            prompt_approved_event=event,
            trusted_fixture_po_confirmation=True,
        )
        self.assertEqual(result["store_revision"], 1)
        snapshot = self.store.snapshot()
        self.assertEqual(snapshot["prompt_revision_id"], prompt["revision_id"])
        self.assertEqual(snapshot["projection"]["phase"], "READY")
        self.assertEqual(self._counts()["document_revision_decision"], 1)

    def test_closeout_revision_link_receipt_and_projection_commit_together(self) -> None:
        prompt, events = self._start_workflow()
        closeout = DocumentRevisionInput(
            document_id="closeout-document",
            revision_id="closeout-revision-1",
            logical_key=FIXTURE_CLOSEOUT_PATH,
            logical_type="CLOSEOUT",
            source_path=FIXTURE_CLOSEOUT_PATH,
            content="# Exact closeout\n",
        )
        submitted = events[10]
        result = self.store.submit_closeout_revision(
            request_id="submit-closeout-1",
            expected_prompt_revision_id=prompt["revision_id"],
            expected_store_revision=2,
            revision=closeout,
            closeout_submitted_event=submitted,
        )
        self.assertEqual(result["store_revision"], 3)
        self.assertEqual(result["document_revision_ids"], ["closeout-revision-1"])
        snapshot = self.store.snapshot()
        self.assertEqual(snapshot["projection"]["phase"], "REVIEW")
        self.assertEqual(snapshot["event_count"], 3)
        self.assertEqual(self._counts()["workflow_document_link"], 2)
        self.assertEqual(self._counts()["transaction_receipt"], 3)
        link = sqlite3.connect(self.database_path).execute(
            "SELECT revision_id, content_sha256, link_role FROM workflow_document_link WHERE event_id = ?",
            (submitted["event_id"],),
        ).fetchone()
        self.assertEqual(
            link,
            (
                "closeout-revision-1",
                hashlib.sha256(closeout.content.encode("utf-8")).hexdigest(),
                "closeout",
            ),
        )

    def test_closeout_submission_rejects_mismatched_source_path_without_changes(self) -> None:
        prompt, events = self._start_workflow()
        before_snapshot = self.store.snapshot()
        before_counts = self._counts()
        closeout = DocumentRevisionInput(
            document_id="wrong-closeout-document",
            revision_id="wrong-closeout-revision",
            logical_key="fixtures/other-closeout.md",
            logical_type="CLOSEOUT",
            source_path="fixtures/other-closeout.md",
            content="# Wrong source path\n",
        )
        with self.assertRaisesRegex(IdentityMismatchError, "path does not match"):
            self.store.submit_closeout_revision(
                request_id="submit-closeout-wrong-path",
                expected_prompt_revision_id=prompt["revision_id"],
                expected_store_revision=2,
                revision=closeout,
                closeout_submitted_event=events[10],
            )
        self.assertEqual(self.store.snapshot(), before_snapshot)
        self.assertEqual(self._counts(), before_counts)

    def test_precommit_failure_rolls_back_revisions_links_events_receipts_and_projection(self) -> None:
        prompt, events = self._start_workflow()
        closeout = DocumentRevisionInput(
            document_id="closeout-document",
            revision_id="closeout-revision-1",
            logical_key=FIXTURE_CLOSEOUT_PATH,
            logical_type="CLOSEOUT",
            source_path=FIXTURE_CLOSEOUT_PATH,
            content="# Rollback closeout\n",
        )
        before_counts = self._counts()
        before_snapshot = self.store.snapshot()
        with patch.object(
            self.store,
            "_write_workflow_state",
            side_effect=RuntimeError("injected failure before commit"),
        ):
            with self.assertRaisesRegex(RuntimeError, "injected failure"):
                self.store.submit_closeout_revision(
                    request_id="rollback-closeout",
                    expected_prompt_revision_id=prompt["revision_id"],
                    expected_store_revision=2,
                    revision=closeout,
                    closeout_submitted_event=events[10],
                )
        self.assertEqual(self._counts(), before_counts)
        self.assertEqual(self.store.snapshot(), before_snapshot)

    def test_same_id_retry_precedes_stale_revision_check_and_changed_request_fails(self) -> None:
        prompt, events = self._start_workflow()
        first = events[8]
        first_result = self._append(
            "decision-request-1",
            first,
            expected_store_revision=2,
            prompt_revision_id=prompt["revision_id"],
        )
        second = _event(
            events[:9],
            event_type="decision_recorded",
            summary="Later non-binding decision",
            actor_role="coder",
            event_fields={"binding": False},
        )
        self._append(
            "decision-request-2",
            second,
            expected_store_revision=3,
            prompt_revision_id=prompt["revision_id"],
        )
        retry = self._append(
            "decision-request-1",
            first,
            expected_store_revision=2,
            prompt_revision_id=prompt["revision_id"],
        )
        self.assertEqual(retry, first_result)
        changed = dict(first, summary="Different decision")
        with self.assertRaises(IdempotencyConflictError):
            self._append(
                "decision-request-1",
                changed,
                expected_store_revision=2,
                prompt_revision_id=prompt["revision_id"],
            )
        self.assertEqual(self.store.snapshot()["event_count"], 4)
        self.assertEqual(self._counts()["transaction_receipt"], 4)

    def test_stale_repository_selection_prompt_and_state_fail_without_changes(self) -> None:
        prompt, events = self._start_workflow()
        before = self.store.snapshot()
        before_counts = self._counts()
        invalid_requests = (
            (
                IdentityMismatchError,
                {"expected_repository_id": "wrong-repository"},
            ),
            (
                StaleStateError,
                {"expected_selection_id": "stale-selection"},
            ),
            (
                StaleStateError,
                {"expected_selection_revision": 2},
            ),
            (
                StaleStateError,
                {"expected_prompt_revision_id": "stale-prompt"},
            ),
            (
                StaleStateError,
                {"expected_store_revision": 1},
            ),
        )
        for index, (error_type, overrides) in enumerate(invalid_requests):
            with self.subTest(overrides=overrides):
                requested_revision = overrides.pop("expected_store_revision", 2)
                with self.assertRaises(error_type):
                    self._append(
                        f"stale-request-{index}",
                        events[8],
                        expected_store_revision=requested_revision,
                        prompt_revision_id=prompt["revision_id"],
                        **overrides,
                    )
                self.assertEqual(self.store.snapshot(), before)
                self.assertEqual(self._counts(), before_counts)

    def test_duplicate_event_id_is_rejected_without_appending(self) -> None:
        prompt, events = self._start_workflow()
        with self.assertRaises(lifecycle.WorkflowRecordError):
            self._append(
                "duplicate-event-request",
                events[0],
                expected_store_revision=2,
                prompt_revision_id=prompt["revision_id"],
                trusted_fixture_po_confirmation=True,
            )
        self.assertEqual(self.store.snapshot()["event_count"], 2)
        self.assertEqual(self._counts()["transaction_receipt"], 2)

    def test_all_21_event_types_and_six_phases_match_existing_reducer(self) -> None:
        events = _all_event_types_history()
        prompt = self._create_prompt()
        self.store.select_approved_prompt_revision(
            request_id="all-event-prompt",
            expected_store_revision=0,
            document_id=prompt["document_id"],
            revision_id=prompt["revision_id"],
            content_sha256=prompt["content_sha256"],
            prompt_approved_event=events[0],
            trusted_fixture_po_confirmation=True,
        )
        closeout = DocumentRevisionInput(
            document_id="all-event-closeout-doc",
            revision_id="all-event-closeout-rev",
            logical_key=FIXTURE_CLOSEOUT_PATH,
            logical_type="CLOSEOUT",
            source_path=FIXTURE_CLOSEOUT_PATH,
            content="# Exact fixture closeout\n",
        )
        closeout_result = self.store.create_immutable_revision(closeout)
        closeout_links = tuple(
            WorkflowDocumentLink(
                event_id=event["event_id"],
                document_id=closeout_result["document_id"],
                revision_id=closeout_result["revision_id"],
                content_sha256=closeout_result["content_sha256"],
                link_role="closeout",
                evidence="Exact synthetic fixture closeout revision.",
            )
            for event in events[1:]
            if event["event_type"] in {"closeout_submitted", "acceptance_recorded"}
        )
        self.store.append_workflow_batch(
            request_id="all-event-batch",
            expected_repository_id=self.identity.repository_id,
            expected_milestone_id=self.identity.milestone_id,
            expected_selection_id=self.identity.selection_id,
            expected_selection_revision=self.identity.selection_revision,
            expected_prompt_revision_id=prompt["revision_id"],
            expected_store_revision=1,
            events=events[1:],
            links=closeout_links,
            trusted_fixture_po_confirmation=True,
        )
        snapshot = self.store.snapshot()
        self.assertEqual({event["event_type"] for event in snapshot["events"]}, lifecycle._EVENT_TYPES)
        phases = {
            phase
            for event in snapshot["events"]
            for phase in (event["phase_before"], event["phase_after"])
        }
        self.assertEqual(
            phases,
            {"PLANNING", "READY", "IN_PROGRESS", "WAITING", "REVIEW", "CLOSED"},
        )
        self.assertEqual(snapshot["events"], events)
        self.assertEqual(snapshot["projection"], {
            "phase": lifecycle._reduce_events(events)["phase"],
            "waiting_category": lifecycle._reduce_events(events)["waiting_category"],
            "waiting_reason": lifecycle._reduce_events(events)["waiting_reason"],
            "open_question_ids": sorted(lifecycle._open_question_ids(events)),
            "open_bug_ids": sorted(lifecycle._open_bug_ids(events)),
            "unfixed_bug_ids": sorted(lifecycle._unfixed_bug_ids(events)),
            "event_count": len(events),
        })
        self.assertEqual(snapshot["event_count"], len(events))
        self.assertEqual(self._counts()["workflow_document_link"], 4)
        self.assertEqual(self._counts()["document_revision_decision"], 2)

    def test_snapshot_paging_is_bounded_and_excludes_later_appends(self) -> None:
        prompt, events = self._start_workflow()
        initial = events[2:10]
        self.store.append_workflow_batch(
            request_id="initial-page-events",
            expected_repository_id=self.identity.repository_id,
            expected_milestone_id=self.identity.milestone_id,
            expected_selection_id=self.identity.selection_id,
            expected_selection_revision=self.identity.selection_revision,
            expected_prompt_revision_id=prompt["revision_id"],
            expected_store_revision=2,
            events=initial,
        )
        snapshot_revision = self.store.snapshot()["store_revision"]
        later = _event(
            events[:2] + initial,
            event_type="test_result_recorded",
            summary="Later page result",
            actor_role="coder",
            event_fields={"evidence_source": "fixture", "result": "PASS"},
        )
        self._append(
            "later-page-event",
            later,
            expected_store_revision=snapshot_revision,
            prompt_revision_id=prompt["revision_id"],
        )
        paged = []
        cursor = 0
        while cursor < snapshot_revision:
            page = self.store.page_events(snapshot_revision=snapshot_revision, after_sequence=cursor, limit=3)
            self.assertLessEqual(len(page["events"]), 3)
            paged.extend(page["events"])
            self.assertGreater(page["next_sequence"], cursor)
            cursor = page["next_sequence"]
        expected_ids = [event["event_id"] for event in events[:10]]
        self.assertEqual([event["event_id"] for event in paged], expected_ids)
        with self.assertRaises(StaleStateError):
            self.store.page_events(snapshot_revision=snapshot_revision + 2)
        with self.assertRaises(ValueError):
            self.store.page_events(snapshot_revision=snapshot_revision, limit=101)

    def test_writer_contention_returns_bounded_busy_error(self) -> None:
        prompt, events = self._start_workflow()
        lock = sqlite3.connect(self.database_path, timeout=0, isolation_level=None)
        try:
            lock.execute("BEGIN IMMEDIATE")
            started = time.monotonic()
            with self.assertRaises(BusyStoreError):
                self._append(
                    "contended-write",
                    events[8],
                    expected_store_revision=2,
                    prompt_revision_id=prompt["revision_id"],
                )
            self.assertLess(time.monotonic() - started, 2.0)
        finally:
            lock.rollback()
            lock.close()
        self.assertEqual(self.store.snapshot()["event_count"], 2)

    def test_dispatch_requires_explicit_fixture_confirmation_and_records_history(self) -> None:
        prompt, events = self._start_workflow()
        dispatch_id = "a" * 32
        dispatch = self.store.create_pending_dispatch(
            dispatch_id=dispatch_id,
            dispatch_type="INSTRUCTION",
            body="Use the fixture only.",
            related_event_id=events[1]["event_id"],
        )
        self.assertEqual(dispatch["status"], "PENDING")
        self.assertFalse(dispatch["authorization_granted"])
        with self.assertRaises(DispatchDecisionError):
            self.store.record_dispatch_decision(
                dispatch_id=dispatch_id,
                history_id="b" * 32,
                status="APPROVED",
                actor_role="product_owner",
                actor_attribution="Reported fixture PO",
                approved_milestone_id=self.identity.milestone_id,
            )
        self.assertEqual(self.store.get_dispatch(dispatch_id)["status"], "PENDING")
        decision = {
            "dispatch_id": dispatch_id,
            "history_id": "b" * 32,
            "status": "APPROVED",
            "actor_role": "product_owner",
            "actor_attribution": "Reported fixture PO",
            "trusted_fixture_po_confirmation": True,
            "approved_milestone_id": self.identity.milestone_id,
            "related_event_id": events[1]["event_id"],
            "recorded_at": "2026-01-01T00:00:00+00:00",
        }
        approved = self.store.record_dispatch_decision(**decision)
        self.assertEqual(approved["status"], "APPROVED")
        self.assertFalse(approved["authorization_granted"])
        history = self.store.list_dispatch_history(dispatch_id)
        self.assertEqual([item["record_kind"] for item in history], ["CREATED", "DECISION"])
        self.assertIs(history[-1]["po_confirmed"], 1)
        self.assertEqual(history[-1]["related_event_id"], events[1]["event_id"])
        self.assertEqual(self.store.record_dispatch_decision(**decision)["status"], "APPROVED")
        self.assertEqual(len(self.store.list_dispatch_history(dispatch_id)), 2)
        with self.assertRaises(DispatchDecisionError):
            self.store.record_dispatch_decision(
                **{**decision, "history_id": "c" * 32, "recorded_at": "2026-01-01T00:00:01+00:00"}
            )

    def test_import_preserves_exact_sources_and_does_not_invent_approvals_or_links(self) -> None:
        events = _closeout_history()
        candidate = {
            "milestone_id": self.identity.milestone_id,
            "prompt_path": self.identity.prompt_path,
        }
        ledger = lifecycle._empty_ledger(self.identity.repository_id, candidate)
        ledger["events"] = events
        sidecar_bytes = lifecycle._render_sidecar(ledger).encode("utf-8")
        dispatch_record = {
            "dispatch_id": "d" * 32,
            "repository_id": self.identity.repository_id,
            "type": "INSTRUCTION",
            "status": "APPROVED",
            "body": "Existing status snapshot only.",
            "milestone_id": self.identity.milestone_id,
            "approved_milestone_id": self.identity.milestone_id,
            "related_event_id": events[1]["event_id"],
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:01+00:00",
            "approved_at": "2026-01-01T00:00:01+00:00",
        }
        dispatch_bytes = json.dumps(
            {
                "schema_version": 1,
                "repository_id": self.identity.repository_id,
                "dispatches": [dispatch_record],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        prompt_bytes = b"# Imported prompt\n"
        closeout_bytes = b"# Imported closeout\n"
        bundle = V1FixtureBundle(
            prompt_path=self.identity.prompt_path,
            closeout_path=FIXTURE_CLOSEOUT_PATH,
            workflow_path="fixtures/workflow.md",
            prompt_bytes=prompt_bytes,
            closeout_bytes=closeout_bytes,
            workflow_bytes=sidecar_bytes,
            dispatch_path="fixtures/architect_dispatches.json",
            dispatch_bytes=dispatch_bytes,
        )
        result = self.store.import_v1_fixture(bundle)
        self.assertEqual(result["transaction_boundaries"], "not_invented")
        self.assertIsNone(result["approved_prompt_revision_id"])
        self.assertEqual(result["linked_event_count"], 0)
        self.assertEqual(result["imported_event_count"], len(events))
        self.assertEqual(self.store.snapshot()["events"], events)
        self.assertIsNone(self.store.snapshot()["prompt_revision_id"])
        self.assertEqual(
            self.store.get_revision(result["prompt_document_id"], result["prompt_revision_id"])["content"],
            prompt_bytes.decode("utf-8"),
        )
        sources = self.store.list_import_sources()
        self.assertEqual(len(sources), 4)
        source_bytes_by_kind = {
            item["source_kind"]: self.store.read_import_source(item["source_id"])
            for item in sources
        }
        self.assertEqual(source_bytes_by_kind["prompt"], prompt_bytes)
        self.assertEqual(source_bytes_by_kind["closeout"], closeout_bytes)
        self.assertEqual(source_bytes_by_kind["workflow"], sidecar_bytes)
        self.assertEqual(source_bytes_by_kind["dispatch"], dispatch_bytes)
        dispatch = self.store.get_dispatch("d" * 32)
        self.assertEqual(dispatch["status"], "APPROVED")
        self.assertFalse(dispatch["authorization_granted"])
        legacy_history = self.store.list_dispatch_history("d" * 32)
        self.assertEqual(legacy_history[0]["record_kind"], "LEGACY_SNAPSHOT")
        self.assertIsNone(legacy_history[0]["po_confirmed"])
        self.assertEqual(self._counts()["workflow_document_link"], 0)

    def test_malformed_import_rolls_back_all_fixture_records(self) -> None:
        bundle = V1FixtureBundle(
            prompt_path=self.identity.prompt_path,
            closeout_path=FIXTURE_CLOSEOUT_PATH,
            workflow_path="fixtures/workflow.md",
            prompt_bytes=b"# Prompt\n",
            closeout_bytes=b"# Closeout\n",
            workflow_bytes=b"not a Repo Control sidecar",
        )
        before = self._counts()
        with self.assertRaises(ImportSourceError):
            self.store.import_v1_fixture(bundle)
        self.assertEqual(self._counts(), before)
        self.assertEqual(self.store.snapshot()["event_count"], 0)


if __name__ == "__main__":
    unittest.main()
