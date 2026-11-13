from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import stat
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from repoctl import information_store
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
    dispatch_reconciliation_fingerprint,
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
            fixed_backport_evidence="unverified caller-provided evidence",
        )
        self.assertEqual(actual["sqlite_version_info"], sqlite3.sqlite_version_info)
        self.assertEqual(actual["production_eligible"], tuple(sqlite3.sqlite_version_info) >= MINIMUM_SQLITE_VERSION)
        self.assertFalse(controlled_low["production_eligible"])
        self.assertTrue(controlled_floor["production_eligible"])
        self.assertEqual(controlled_backport["eligibility_basis"], "backport_evidence_unverified")
        self.assertEqual(controlled_backport["fixed_backport_evidence_status"], "unverified")
        self.assertFalse(controlled_backport["production_eligible"])
        with self.assertRaises(ValueError):
            sqlite_runtime_status((3, 45, 1), fixed_backport_evidence=" ")
        self.assertEqual(
            self.store.snapshot()["production_eligible"],
            actual["production_eligible"],
        )

    def test_runtime_probe_does_not_require_site_packages_or_flask(self) -> None:
        repository_root = Path(__file__).resolve().parents[1]
        probe = repository_root / "ops/verify_sqlite_runtime.py"
        result = subprocess.run(
            [sys.executable, "-S", str(probe)],
            capture_output=True,
            text=True,
            check=False,
        )
        report = json.loads(result.stdout)
        expected_eligible = tuple(sqlite3.sqlite_version_info) >= MINIMUM_SQLITE_VERSION
        self.assertEqual(result.returncode, 0 if expected_eligible else 2, result.stderr)
        self.assertEqual(report["production_eligible"], expected_eligible)

    def test_quiesced_central_fixture_keeps_repository_and_milestone_scopes_separate(self) -> None:
        self._create_prompt()
        second_identity = replace(
            self.identity,
            milestone_id="M999.002",
            prompt_path="fixtures/second_prompt.md",
            selection_id="fixture-selection-2",
            selection_revision=2,
        )
        second = self.store.register_disposable_scope(second_identity)
        with self.assertRaises(IdentityMismatchError):
            self.store.snapshot()
        second.create_immutable_revision(DocumentRevisionInput(
            document_id="second-prompt",
            revision_id="second-revision",
            logical_key=second_identity.prompt_path,
            logical_type="PROMPT",
            source_path=second_identity.prompt_path,
            content="# Second milestone\n",
        ))
        self.assertEqual(len(second.list_documents()["documents"]), 1)
        with self.assertRaises(StaleStateError):
            second.register_disposable_scope(self.identity)
        with self.assertRaises(IdentityMismatchError):
            second.register_disposable_scope(replace(second_identity, repository_id="conflicting-id"))

        other_identity = replace(
            second_identity,
            repository_id="other-repository-id",
            repository_key="other-repository",
            repository_root=str(self.root / "other-repository"),
            selection_id="other-selection",
            selection_revision=1,
        )
        other = second.register_disposable_scope(other_identity)
        self.assertEqual(other.list_documents()["documents"], [])
        self.assertEqual(len(second.list_documents()["documents"]), 1)
        reopened = other.register_disposable_scope(replace(
            self.identity, selection_id="fixture-selection-3", selection_revision=3
        ))
        self.assertEqual(reopened.get_revision("prompt-document", "prompt-revision-1")["content"], "# Fixture prompt\n")
        with self.assertRaises(IdentityMismatchError):
            second.snapshot()
        with self.assertRaises(IdentityMismatchError):
            self.store.snapshot()

    def test_external_fixture_selection_change_during_write_rolls_back(self) -> None:
        state = self.root / "fixture-selection-state"
        pointer = state / self.identity.repository_id / "milestones" / "selection.json"
        pointer.parent.mkdir(parents=True)
        payload = {
            "schema_version": 1,
            "repository_id": self.identity.repository_id,
            "milestone_id": self.identity.milestone_id,
            "prompt_path": self.identity.prompt_path,
            "selection_id": self.identity.selection_id,
            "selection_revision": self.identity.selection_revision,
        }
        pointer.write_text(json.dumps(payload), encoding="utf-8")
        bound = InformationStore.open_disposable(
            self.database_path,
            self.identity,
            allow_unsupported_runtime_for_fixture=True,
            selection_state_root=state,
        )
        context = multiprocessing.get_context("fork")
        change_requested = context.Event()
        changed = context.Event()

        def replace_selection() -> None:
            if not change_requested.wait(10):
                raise RuntimeError("The fixture write did not reach its transaction.")
            pointer.write_text(json.dumps({**payload, "selection_id": "different-selection"}), encoding="utf-8")
            changed.set()

        process = context.Process(target=replace_selection)
        process.start()
        self.addCleanup(lambda: process.join(timeout=10))
        original_insert = bound._insert_revision

        def pause_before_commit(connection, item) -> None:
            original_insert(connection, item)
            change_requested.set()
            if not changed.wait(10):
                raise RuntimeError("The competing fixture process did not update its selection.")

        with patch.object(bound, "_insert_revision", side_effect=pause_before_commit):
            with self.assertRaisesRegex(StaleStateError, "External fixture selection changed"):
                bound.create_immutable_revision(DocumentRevisionInput(
                    document_id="racing-prompt",
                    revision_id="racing-revision",
                    logical_key=FIXTURE_PROMPT_PATH,
                    logical_type="PROMPT",
                    source_path=FIXTURE_PROMPT_PATH,
                    content="# Must roll back\n",
                ))
        process.join(timeout=10)
        self.assertEqual(process.exitcode, 0)
        with sqlite3.connect(self.database_path) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM document").fetchone()[0], 0)
        with self.assertRaises(StaleStateError):
            bound.snapshot()

    def test_v1_selection_binding_checks_exact_bytes_and_recovers_an_interrupted_fixture_bind(self) -> None:
        self._create_prompt()
        state = self.root / "v1-selection-state"
        pointer = state / self.identity.repository_id / "milestones" / "selection.json"
        pointer.parent.mkdir(parents=True)
        original = json.dumps({
            "schema_version": 1,
            "repository_id": self.identity.repository_id,
            "milestone_id": self.identity.milestone_id,
            "prompt_path": self.identity.prompt_path,
        }, indent=2).encode("utf-8")
        pointer.write_bytes(original)
        request = {
            "selection_state_root": state,
            "expected_v1_sha256": hashlib.sha256(original).hexdigest(),
            "allow_unsupported_runtime_for_fixture": True,
        }
        with self.assertRaisesRegex(InformationStoreError, "explicit disposable-fixture confirmation"):
            InformationStore.bind_disposable_v1_selection(
                self.database_path, self.identity, **request, fixture_cutover_confirmed=False,
            )
        with self.assertRaisesRegex(StaleStateError, "bytes changed"):
            InformationStore.bind_disposable_v1_selection(
                self.database_path, self.identity, **{**request, "expected_v1_sha256": "0" * 64},
                fixture_cutover_confirmed=True,
            )
        self.assertEqual(pointer.read_bytes(), original)
        original_writer = information_store._write_fixture_selection

        def fail_after_write(*args) -> None:
            original_writer(*args)
            raise OSError("injected fixture bind interruption")

        with patch("repoctl.information_store._write_fixture_selection", side_effect=fail_after_write):
            with self.assertRaisesRegex(OSError, "injected fixture bind interruption"):
                InformationStore.bind_disposable_v1_selection(
                    self.database_path, self.identity, **request, fixture_cutover_confirmed=True,
                )
        bound = InformationStore.bind_disposable_v1_selection(
            self.database_path, self.identity, **request, fixture_cutover_confirmed=True,
        )
        self.assertEqual(bound.identity, self.identity)
        self.assertEqual(bound.snapshot()["event_count"], 0)
        self.assertEqual(json.loads(pointer.read_bytes())["v1_selection_sha256"], request["expected_v1_sha256"])
        with self.assertRaises(StaleStateError):
            InformationStore.bind_disposable_v1_selection(
                self.database_path, self.identity, **{**request, "expected_v1_sha256": "0" * 64},
                fixture_cutover_confirmed=True,
            )

    def test_external_fixture_selection_switch_is_bound_and_stale_clients_fail_closed(self) -> None:
        self._create_prompt()
        second_identity = replace(
            self.identity,
            milestone_id="M999.002",
            prompt_path="fixtures/second_prompt.md",
            selection_id="fixture-selection-2",
            selection_revision=2,
        )
        second = self.store.register_disposable_scope(second_identity)
        second.create_immutable_revision(DocumentRevisionInput(
            document_id="second-prompt",
            revision_id="second-revision",
            logical_key=second_identity.prompt_path,
            logical_type="PROMPT",
            source_path=second_identity.prompt_path,
            content="# Second milestone\n",
        ))
        state = self.root / "switch-state"
        pointer = state / second_identity.repository_id / "milestones" / "selection.json"
        pointer.parent.mkdir(parents=True)
        pointer.write_text(json.dumps({
            "schema_version": 1,
            "repository_id": second_identity.repository_id,
            "milestone_id": second_identity.milestone_id,
            "prompt_path": second_identity.prompt_path,
            "selection_id": second_identity.selection_id,
            "selection_revision": second_identity.selection_revision,
        }), encoding="utf-8")
        bound = InformationStore.open_disposable(
            self.database_path, second_identity,
            allow_unsupported_runtime_for_fixture=True,
            selection_state_root=state,
        )
        with self.assertRaises(IdentityMismatchError):
            bound.switch_disposable_selection(
                "M999.404",
                expected_selection_id=second_identity.selection_id,
                expected_selection_revision=second_identity.selection_revision,
            )
        first = bound.switch_disposable_selection(
            self.identity.milestone_id,
            expected_selection_id=second_identity.selection_id,
            expected_selection_revision=second_identity.selection_revision,
        )
        self.assertEqual(first.identity.selection_revision, 3)
        self.assertEqual(first.get_revision("prompt-document", "prompt-revision-1")["content"], "# Fixture prompt\n")
        with self.assertRaises(StaleStateError):
            bound.snapshot()
        with self.assertRaises(StaleStateError):
            bound.switch_disposable_selection(
                self.identity.milestone_id,
                expected_selection_id=second_identity.selection_id,
                expected_selection_revision=second_identity.selection_revision,
            )
        reopened = InformationStore.open_disposable_from_external_selection(
            self.database_path,
            repository_id=self.identity.repository_id,
            repository_key=self.identity.repository_key,
            repository_root=self.identity.repository_root,
            selection_state_root=state,
            allow_unsupported_runtime_for_fixture=True,
        )
        self.assertEqual(reopened.identity, first.identity)
        self.assertEqual(reopened.snapshot()["event_count"], 0)

    def test_interrupted_fixture_selection_switch_requires_explicit_bound_recovery(self) -> None:
        self._create_prompt()
        second_identity = replace(
            self.identity,
            milestone_id="M999.002",
            prompt_path="fixtures/second_prompt.md",
            selection_id="fixture-selection-2",
            selection_revision=2,
        )
        second = self.store.register_disposable_scope(second_identity)
        second.create_immutable_revision(DocumentRevisionInput(
            document_id="second-prompt",
            revision_id="second-revision",
            logical_key=second_identity.prompt_path,
            logical_type="PROMPT",
            source_path=second_identity.prompt_path,
            content="# Second milestone\n",
        ))
        state = self.root / "interrupted-selection-state"
        pointer = state / second_identity.repository_id / "milestones" / "selection.json"
        pointer.parent.mkdir(parents=True)
        pointer.write_text(json.dumps({
            "schema_version": 1,
            "repository_id": second_identity.repository_id,
            "milestone_id": second_identity.milestone_id,
            "prompt_path": second_identity.prompt_path,
            "selection_id": second_identity.selection_id,
            "selection_revision": second_identity.selection_revision,
        }), encoding="utf-8")
        bound = InformationStore.open_disposable(
            self.database_path, second_identity,
            allow_unsupported_runtime_for_fixture=True,
            selection_state_root=state,
        )
        from repoctl.information_store import _write_fixture_selection

        def fail_after_pointer_write(*args) -> None:
            _write_fixture_selection(*args)
            raise OSError("injected failure before database commit")

        with patch("repoctl.information_store._write_fixture_selection", side_effect=fail_after_pointer_write):
            with self.assertRaisesRegex(OSError, "injected failure"):
                bound.switch_disposable_selection(
                    self.identity.milestone_id,
                    expected_selection_id=second_identity.selection_id,
                    expected_selection_revision=second_identity.selection_revision,
                )
        published = json.loads(pointer.read_text(encoding="utf-8"))
        target = replace(
            self.identity,
            selection_id=published["selection_id"],
            selection_revision=published["selection_revision"],
        )
        with self.assertRaises(IdentityMismatchError):
            InformationStore.open_disposable_from_external_selection(
                self.database_path,
                repository_id=target.repository_id,
                repository_key=target.repository_key,
                repository_root=target.repository_root,
                selection_state_root=state,
                allow_unsupported_runtime_for_fixture=True,
            )
        recovery = {
            "expected_external": target,
            "expected_mirror": second_identity,
            "selection_state_root": state,
            "allow_unsupported_runtime_for_fixture": True,
        }
        with self.assertRaisesRegex(InformationStoreError, "explicit disposable-fixture confirmation"):
            InformationStore.recover_disposable_selection_mirror(
                self.database_path, **recovery, fixture_recovery_confirmed=False,
            )
        with self.assertRaises(StaleStateError):
            InformationStore.recover_disposable_selection_mirror(
                self.database_path,
                **{**recovery, "expected_external": replace(target, selection_id="wrong-selection")},
                fixture_recovery_confirmed=True,
            )
        restored = InformationStore.recover_disposable_selection_mirror(
            self.database_path, **recovery, fixture_recovery_confirmed=True,
        )
        self.assertEqual(restored.identity, target)
        self.assertEqual(restored.get_revision("prompt-document", "prompt-revision-1")["content"], "# Fixture prompt\n")
        self.assertEqual(restored.snapshot()["event_count"], 0)
        with self.assertRaises(StaleStateError):
            bound.snapshot()

    def test_external_fixture_rejects_database_replacement_before_commit(self) -> None:
        state = self.root / "replacement-selection-state"
        pointer = state / self.identity.repository_id / "milestones" / "selection.json"
        pointer.parent.mkdir(parents=True)
        pointer.write_text(json.dumps({
            "schema_version": 1,
            "repository_id": self.identity.repository_id,
            "milestone_id": self.identity.milestone_id,
            "prompt_path": self.identity.prompt_path,
            "selection_id": self.identity.selection_id,
            "selection_revision": self.identity.selection_revision,
        }), encoding="utf-8")
        bound = InformationStore.open_disposable(
            self.database_path, self.identity,
            allow_unsupported_runtime_for_fixture=True,
            selection_state_root=state,
        )
        replacement = self.root / "replacement.sqlite3"
        replacement.write_bytes(b"not the bound database")
        original_insert = bound._insert_revision

        def replace_before_commit(connection, item) -> None:
            original_insert(connection, item)
            os.replace(replacement, self.database_path)

        with patch.object(bound, "_insert_revision", side_effect=replace_before_commit):
            with self.assertRaisesRegex(IdentityMismatchError, "was replaced"):
                bound.create_immutable_revision(DocumentRevisionInput(
                    document_id="replaced-prompt",
                    revision_id="replaced-revision",
                    logical_key=FIXTURE_PROMPT_PATH,
                    logical_type="PROMPT",
                    source_path=FIXTURE_PROMPT_PATH,
                    content="# Must not be acknowledged\n",
                ))
        self.assertEqual(self.database_path.read_bytes(), b"not the bound database")

    def test_operator_runtime_probe_reports_the_linked_library_and_enforces_the_gate(self) -> None:
        script = Path(__file__).resolve().parents[1] / "ops" / "verify_sqlite_runtime.py"
        result = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True,
            check=False,
            text=True,
        )
        report = json.loads(result.stdout)
        expected_eligible = tuple(sqlite3.sqlite_version_info) >= MINIMUM_SQLITE_VERSION
        self.assertEqual(result.returncode, 0 if expected_eligible else 2, result.stderr)
        self.assertEqual(report["python_executable"], str(Path(sys.executable).resolve()))
        self.assertEqual(report["sqlite_version_info"], list(sqlite3.sqlite_version_info))
        self.assertEqual(report["production_eligible"], expected_eligible)
        self.assertIn("sqlite_source_id", report)
        self.assertTrue(Path(report["sqlite_extension"]).is_file())
        if Path("/proc/self/maps").is_file():
            self.assertIn("libsqlite3", " ".join(report["loaded_sqlite_libraries"]))

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

    def test_replaced_database_requires_a_new_binding(self) -> None:
        original_identity = self.store.database_identity()
        replacement_path = self.root / "replacement.sqlite3"
        InformationStore.initialize_disposable(
            replacement_path,
            self.identity,
            allow_unsupported_runtime_for_fixture=True,
        )
        os.replace(replacement_path, self.database_path)
        with self.assertRaisesRegex(IdentityMismatchError, "replaced"):
            self.store.snapshot()
        with self.assertRaisesRegex(IdentityMismatchError, "replaced"):
            self.store.database_identity()
        reopened = InformationStore.open_disposable(
            self.database_path,
            self.identity,
            allow_unsupported_runtime_for_fixture=True,
        )
        self.assertNotEqual(reopened.database_identity(), original_identity)
        self.assertEqual(reopened.snapshot()["event_count"], 0)

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

    def test_revision_listing_pages_exact_versions_and_reports_status(self) -> None:
        first = self._create_prompt()
        second = self.store.create_immutable_revision(
            DocumentRevisionInput(
                document_id="prompt-document",
                revision_id="prompt-revision-2",
                logical_key=FIXTURE_PROMPT_PATH,
                logical_type="PROMPT",
                source_path=FIXTURE_PROMPT_PATH,
                content="# Updated fixture prompt\n",
                parent_revision_id=first["revision_id"],
            )
        )
        page_one = self.store.list_revisions(first["document_id"], limit=1)
        self.assertEqual(len(page_one["revisions"]), 1)
        self.assertEqual(page_one["revisions"][0]["revision_id"], first["revision_id"])
        self.assertEqual(page_one["revisions"][0]["status"], "historical")
        self.assertIsNotNone(page_one["next_cursor"])
        page_two = self.store.list_revisions(
            first["document_id"],
            after_order=page_one["next_cursor"],
            limit=1,
        )
        self.assertEqual(len(page_two["revisions"]), 1)
        self.assertEqual(page_two["revisions"][0]["revision_id"], second["revision_id"])
        self.assertEqual(page_two["revisions"][0]["status"], "draft")
        self.assertIsNone(page_two["next_cursor"])
        with self.assertRaises(RevisionConflictError):
            self.store.list_revisions("missing-document")

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

    def test_prompt_dispatch_creates_exact_unselected_draft_atomically_after_fixture_decision(self) -> None:
        dispatch_id = "a" * 32
        proposal = self.store.create_pending_dispatch(
            dispatch_id=dispatch_id,
            dispatch_type="PROMPT",
            milestone_id="M999.002",
            milestone_root="docs/milestones",
            body="# Proposed fixture prompt",
            title="Proposed fixture",
        )
        self.assertEqual(self.store.list_disposable_milestones()["candidates"], [])
        decision = {
            "dispatch_id": dispatch_id,
            "history_id": "b" * 32,
            "status": "APPROVED",
            "actor_role": "product_owner",
            "actor_attribution": "Reported fixture PO",
            "trusted_fixture_po_confirmation": True,
            "recorded_at": "2026-01-01T00:00:00+00:00",
            "milestone_root": "docs/milestones",
            "expected_fingerprint": dispatch_reconciliation_fingerprint(proposal),
        }
        with self.assertRaises(DispatchDecisionError):
            self.store.record_dispatch_decision(**{**decision, "trusted_fixture_po_confirmation": False})
        with self.assertRaises(StaleStateError):
            self.store.record_dispatch_decision(**{**decision, "expected_fingerprint": "0" * 64})
        original_insert = self.store._insert_revision

        def fail_after_revision(*args, **kwargs) -> None:
            original_insert(*args, **kwargs)
            raise RuntimeError("injected fixture decision failure")

        with patch.object(self.store, "_insert_revision", side_effect=fail_after_revision):
            with self.assertRaisesRegex(RuntimeError, "injected fixture decision"):
                self.store.record_dispatch_decision(**decision)
        self.assertEqual(self.store.get_dispatch(dispatch_id)["status"], "PENDING")
        self.assertEqual(len(self.store.list_dispatch_history(dispatch_id)), 1)
        self.assertEqual(self.store.list_disposable_milestones()["candidates"], [])
        approved = self.store.record_dispatch_decision(**decision)
        self.assertEqual(approved["status"], "APPROVED")
        self.assertFalse(approved["authorization_granted"])
        self.assertEqual(self.store.record_dispatch_decision(**decision), approved)
        history = self.store.list_dispatch_history(dispatch_id)
        self.assertEqual([row["record_kind"] for row in history], ["CREATED", "DECISION"])
        binding = history[-1]["payload"]["prompt_binding"]
        self.assertEqual(binding["source_path"], "docs/milestones/M999.002_proposed_fixture_prompt.md")
        self.assertEqual(binding["content_sha256"], hashlib.sha256(b"# Proposed fixture prompt\n").hexdigest())
        self.assertEqual(self.store.snapshot()["event_count"], 0)
        self.assertEqual(self.store.identity.milestone_id, "M999.001")
        candidate = self.store.list_disposable_milestones()["candidates"][0]
        self.assertEqual(candidate["milestone_id"], "M999.002")
        self.assertFalse((self.repository_root / binding["source_path"]).exists())
        revisions = self.store.list_repository_revisions()
        self.assertEqual(revisions[0]["status"], "draft")
        self.assertEqual(revisions[0]["content"], "# Proposed fixture prompt\n")

        duplicate = self.store.create_pending_dispatch(
            dispatch_id="c" * 32, dispatch_type="PROMPT", milestone_id="M999.002",
            milestone_root="docs/milestones", body="Another proposal", title="Another proposal",
        )
        with self.assertRaisesRegex(DispatchDecisionError, "already exists"):
            self.store.record_dispatch_decision(**{
                **decision, "dispatch_id": duplicate["dispatch_id"], "history_id": "d" * 32,
                "expected_fingerprint": dispatch_reconciliation_fingerprint(duplicate),
            })
        self.assertEqual(self.store.get_dispatch(duplicate["dispatch_id"])["status"], "PENDING")

    def test_repository_dispatch_import_retry_detects_changed_snapshot_and_stored_state(self) -> None:
        source = self.root / "dispatch-source" / self.identity.repository_id / "architect_dispatches.json"
        source.parent.mkdir(parents=True)
        timestamp = "2026-01-01T00:00:00+00:00"
        record = {
            "dispatch_id": "e" * 32,
            "repository_id": self.identity.repository_id,
            "milestone_id": "M999.099",
            "type": "PROMPT",
            "status": "PENDING",
            "title": "Future fixture",
            "slug": "future_fixture",
            "canonical_destination": "docs/milestones/M999.099_future_fixture_prompt.md",
            "body": "# Proposed prompt\n",
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        source_bytes = json.dumps({
            "schema_version": 1, "repository_id": self.identity.repository_id, "dispatches": [record],
        }).encode("utf-8")
        source.write_bytes(source_bytes)
        request = {"source_path": str(source), "source_bytes": source_bytes}
        receipt = self.store.reconcile_disposable_dispatch_snapshot(**request)
        self.assertEqual(receipt["source_count"], 1)
        self.assertEqual(receipt["imported_missing_count"], 1)
        self.assertEqual(self.store.reconcile_disposable_dispatch_snapshot(**request), receipt)
        self.assertEqual(self.store.list_dispatch_history(record["dispatch_id"])[0]["po_confirmed"], None)
        changed = json.dumps({
            "schema_version": 1, "repository_id": self.identity.repository_id,
            "dispatches": [{**record, "body": "# Different source\n"}],
        }).encode("utf-8")
        with self.assertRaises(ImportSourceError):
            self.store.reconcile_disposable_dispatch_snapshot(**{**request, "source_bytes": changed})
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                "UPDATE architect_dispatch SET status = 'REJECTED' WHERE dispatch_id = ?",
                (record["dispatch_id"],),
            )
        with self.assertRaisesRegex(ImportSourceError, "differs from its unmodified source"):
            self.store.reconcile_disposable_dispatch_snapshot(**request)

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
            dispatch_path=str(self.root / self.identity.repository_id / "architect_dispatches.json"),
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
        guidance = self.store.snapshot()
        self.assertEqual(guidance["confirmed_guidance"], [])
        self.assertEqual([item["dispatch_id"] for item in guidance["unverified_guidance"]], ["d" * 32])
        self.assertEqual(self._counts()["workflow_document_link"], 0)
        for dispatch_path in (
            str(self.root / "different-repository-id" / "architect_dispatches.json"),
            str(Path(self.identity.repository_root) / self.identity.repository_id / "architect_dispatches.json"),
        ):
            with self.subTest(dispatch_path=dispatch_path):
                with self.assertRaises(ImportSourceError):
                    self.store.import_v1_fixture(replace(bundle, dispatch_path=dispatch_path))
        fingerprint = information_store.dispatch_reconciliation_fingerprint(dispatch)
        reconciliation = {
            "dispatch_id": dispatch["dispatch_id"],
            "history_id": "f" * 32,
            "status": "APPROVED",
            "expected_fingerprint": fingerprint,
            "trusted_fixture_po_confirmation": True,
        }
        with self.assertRaises(DispatchDecisionError):
            self.store.reconcile_imported_guidance(
                **{**reconciliation, "trusted_fixture_po_confirmation": False}
            )
        with self.assertRaises(StaleStateError):
            self.store.reconcile_imported_guidance(
                **{**reconciliation, "expected_fingerprint": "0" * 64}
            )
        confirmed = self.store.reconcile_imported_guidance(**reconciliation)
        self.assertEqual(self.store.reconcile_imported_guidance(**reconciliation), confirmed)
        self.assertEqual(self.store.snapshot()["unverified_guidance"], [])
        self.assertEqual(
            [item["dispatch_id"] for item in self.store.snapshot()["confirmed_guidance"]],
            [dispatch["dispatch_id"]],
        )
        history = self.store.list_dispatch_history(dispatch["dispatch_id"])
        self.assertEqual([item["record_kind"] for item in history], ["LEGACY_SNAPSHOT", "DECISION"])
        self.assertEqual(history[-1]["payload"]["historical_approval"], "UNKNOWN/NOT VERIFIED")
        self.assertEqual(self.store.read_import_source(
            next(item["source_id"] for item in sources if item["source_kind"] == "dispatch")
        ), dispatch_bytes)
        with self.assertRaises(IdempotencyConflictError):
            self.store.reconcile_imported_guidance(**{**reconciliation, "status": "REJECTED"})

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

    def test_v1_import_accepts_absent_closeout_and_workflow_without_inventing_sources(self) -> None:
        bundle = V1FixtureBundle(
            prompt_path=self.identity.prompt_path,
            closeout_path=None,
            workflow_path=None,
            prompt_bytes=b"# Prompt only\n",
            closeout_bytes=None,
            workflow_bytes=None,
        )
        first = self.store.import_v1_fixture(bundle)
        retry = self.store.import_v1_fixture(bundle)
        self.assertEqual(first, retry)
        self.assertIsNone(first["closeout_document_id"])
        self.assertIsNone(first["closeout_revision_id"])
        self.assertIsNone(first["closeout_sha256"])
        self.assertEqual(first["imported_event_count"], 0)
        self.assertEqual(self.store.snapshot()["projection"]["phase"], "PLANNING")
        self.assertEqual([item["source_kind"] for item in self.store.list_import_sources()], ["prompt"])
        with self.assertRaisesRegex(ImportSourceError, "snapshot differs"):
            self.store.import_v1_fixture(replace(bundle, prompt_bytes=b"# Changed prompt\n"))

    def test_v1_import_is_idempotent_for_exact_sources_and_refuses_changed_snapshots(self) -> None:
        events = _closeout_history()
        ledger = {
            "schema_version": lifecycle.WORKFLOW_LEDGER_SCHEMA_VERSION,
            "repository_id": self.identity.repository_id,
            "milestone_id": self.identity.milestone_id,
            "prompt_path": self.identity.prompt_path,
            "events": events,
        }
        workflow_bytes = lifecycle._render_sidecar(ledger).encode("utf-8")
        bundle = V1FixtureBundle(
            prompt_path=self.identity.prompt_path,
            closeout_path=FIXTURE_CLOSEOUT_PATH,
            workflow_path="fixtures/workflow.md",
            prompt_bytes=b"# Prompt\n",
            closeout_bytes=b"# Closeout\n",
            workflow_bytes=workflow_bytes,
        )
        first = self.store.import_v1_fixture(bundle)
        self.assertEqual(self.store.import_v1_fixture(bundle), first)
        before = self.store.snapshot()
        sources = self.store.list_import_sources()
        with self.assertRaisesRegex(ImportSourceError, "snapshot differs"):
            self.store.import_v1_fixture(replace(bundle, closeout_bytes=b"# Changed closeout\n"))
        self.assertEqual(self.store.snapshot(), before)
        self.assertEqual(self.store.list_import_sources(), sources)
        self.assertEqual(self.store.snapshot()["events"], events)

    def test_disposable_information_store_online_backup_restore_preserves_snapshot(self) -> None:
        prompt = self._create_prompt("# Backup prompt\n")
        events = _closeout_history()
        self.store.select_approved_prompt_revision(
            request_id="backup-fixture-approval",
            expected_store_revision=0,
            document_id=prompt["document_id"],
            revision_id=prompt["revision_id"],
            content_sha256=prompt["content_sha256"],
            prompt_approved_event=events[0],
            trusted_fixture_po_confirmation=True,
        )
        self.store.append_workflow_batch(
            request_id="backup-fixture-start",
            expected_repository_id=self.identity.repository_id,
            expected_milestone_id=self.identity.milestone_id,
            expected_selection_id=self.identity.selection_id,
            expected_selection_revision=self.identity.selection_revision,
            expected_prompt_revision_id=prompt["revision_id"],
            expected_store_revision=1,
            events=[events[1]],
        )
        dispatch_id = "d" * 32
        self.store.create_pending_dispatch(
            dispatch_id=dispatch_id,
            dispatch_type="INSTRUCTION",
            body="Disposable backup dispatch.",
        )
        dispatch = self.store.record_dispatch_decision(
            dispatch_id=dispatch_id,
            history_id="e" * 32,
            status="APPROVED",
            actor_role="product_owner",
            actor_attribution="Reported fixture PO",
            trusted_fixture_po_confirmation=True,
            approved_milestone_id=self.identity.milestone_id,
            recorded_at="2026-01-01T00:00:00+00:00",
        )
        before = self.store.snapshot()
        prompt_links = self.store.list_workflow_document_links(prompt["document_id"], prompt["revision_id"])
        dispatch_history = self.store.list_dispatch_history(dispatch_id)
        backup_path = self.root / "backup.sqlite3"
        source = sqlite3.connect(f"{self.database_path.as_uri()}?mode=ro", uri=True)
        destination = sqlite3.connect(backup_path)
        try:
            destination.execute("PRAGMA journal_mode = WAL")
            source.backup(destination)
            destination.commit()
        finally:
            destination.close()
            source.close()
        integrity_connection = sqlite3.connect(backup_path)
        try:
            integrity = integrity_connection.execute("PRAGMA integrity_check").fetchone()[0]
            application_id = integrity_connection.execute("PRAGMA application_id").fetchone()[0]
            user_version = integrity_connection.execute("PRAGMA user_version").fetchone()[0]
        finally:
            integrity_connection.close()
        self.assertEqual(integrity, "ok")
        self.assertEqual(application_id, APPLICATION_ID)
        self.assertEqual(user_version, SCHEMA_VERSION)
        restored = InformationStore.open_disposable(
            backup_path,
            self.identity,
            allow_unsupported_runtime_for_fixture=True,
        )
        self.assertEqual(restored.identity, self.identity)
        self.assertEqual(restored.snapshot(), before)
        self.assertEqual(restored.get_revision(prompt["document_id"], prompt["revision_id"])["content"], "# Backup prompt\n")
        self.assertEqual(
            restored.list_workflow_document_links(prompt["document_id"], prompt["revision_id"]),
            prompt_links,
        )
        self.assertEqual(restored.get_dispatch(dispatch_id), dispatch)
        self.assertEqual(restored.list_dispatch_history(dispatch_id), dispatch_history)

    def test_disposable_database_owner_and_restrictive_mode_preflight(self) -> None:
        database_stat = self.database_path.stat()
        directory_stat = self.database_path.parent.stat()
        os.chmod(self.database_path.parent, 0o700)
        os.chmod(self.database_path, 0o600)
        self.addCleanup(os.chmod, self.database_path.parent, stat.S_IMODE(directory_stat.st_mode))
        self.addCleanup(os.chmod, self.database_path, stat.S_IMODE(database_stat.st_mode))
        self.assertEqual(self.database_path.stat().st_uid, os.getuid())
        self.assertEqual(stat.S_IMODE(self.database_path.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.database_path.stat().st_mode), 0o600)

    @unittest.skipIf(os.geteuid() == 0, "Permission-denial probe requires an unprivileged test process.")
    def test_disposable_database_permission_failure_is_explicit(self) -> None:
        restricted_root = self.root / "restricted"
        restricted_root.mkdir(mode=0o700)
        database_path = restricted_root / "restricted.sqlite3"
        store = InformationStore.initialize_disposable(
            database_path,
            self.identity,
            allow_unsupported_runtime_for_fixture=True,
        )
        original_mode = stat.S_IMODE(restricted_root.stat().st_mode)
        try:
            os.chmod(restricted_root, original_mode & ~0o222)
            with self.assertRaises((PermissionError, sqlite3.OperationalError)):
                store.create_immutable_revision(
                    DocumentRevisionInput(
                        document_id="permission-denied",
                        revision_id="permission-denied-revision",
                        logical_key="fixtures/permission-denied.md",
                        logical_type="OTHER",
                        source_path="fixtures/permission-denied.md",
                        content="permission probe",
                    )
                )
        finally:
            os.chmod(restricted_root, original_mode)


if __name__ == "__main__":
    unittest.main()
