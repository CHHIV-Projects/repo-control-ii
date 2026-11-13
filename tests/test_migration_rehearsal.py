from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from repoctl.information_store import InformationStore, RuntimeGateError, StoreIdentity
from repoctl.migration_rehearsal import (
    MigrationRehearsalError,
    inventory_registered_repositories,
    inventory_selected_repository,
    prepare_managed_repository_imports,
    rehearse_milestone_imports,
    rehearse_registered_repository_imports,
)
from repoctl.scanner.util import make_repository_id
from repoctl.web.repository_registry import RepositoryEntry, RepositoryRegistry
from repoctl.document_library import SourceEnumerationError
from repoctl.workflow import architect_dispatches, lifecycle
from tests.test_sqlite_workflow_poc import _closeout_history


class MigrationRehearsalTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="repoctl_migration_rehearsal_", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repository_root = self.root / "repository"
        (self.repository_root / "docs/context").mkdir(parents=True)
        (self.repository_root / "docs/milestones").mkdir(parents=True)
        (self.repository_root / "docs/history/milestones").mkdir(parents=True)
        (self.repository_root / "docs/context/project_context.md").write_text(
            "# Governing fixture\n",
            encoding="utf-8",
        )
        self.repository_id = make_repository_id(self.repository_root)
        self.repository = RepositoryEntry(
            key="fixture",
            name="Migration rehearsal fixture",
            path=self.repository_root,
            repository_id=self.repository_id,
            milestone_root="docs/milestones",
            documents={"context": "docs/context/project_context.md"},
        )
        self.prompt_path = "docs/milestones/M999.001_fixture_prompt.md"
        self.closeout_path = "docs/milestones/M999.001_fixture_closeout.md"
        self.workflow_path = "docs/milestones/M999.001_fixture_workflow.md"
        (self.repository_root / self.prompt_path).write_text("# Fixture prompt\n", encoding="utf-8")
        (self.repository_root / self.closeout_path).write_text("# Fixture closeout\n", encoding="utf-8")
        (self.repository_root / "docs/milestones/M999.002_prompt_only_prompt.md").write_text(
            "# Prompt-only fixture\n",
            encoding="utf-8",
        )
        (self.repository_root / "docs/milestones/M998.001_orphan_closeout.md").write_text(
            "# Orphan closeout fixture\n",
            encoding="utf-8",
        )
        (self.repository_root / "docs/history/milestones/12.65.0_legacy_prompt.md").write_text(
            "# Legacy milestone evidence\n",
            encoding="utf-8",
        )
        ledger = lifecycle._empty_ledger(
            self.repository_id,
            {"milestone_id": "M999.001", "prompt_path": self.prompt_path},
        )
        ledger["events"] = json.loads(json.dumps(_closeout_history()))
        for event in ledger["events"]:
            if "evidence_path" in event:
                event["evidence_path"] = self.closeout_path
        (self.repository_root / self.workflow_path).write_text(
            lifecycle._render_sidecar(ledger),
            encoding="utf-8",
        )
        self.dispatch_state_root = self.root / "central-state"
        dispatch_path = self.dispatch_state_root / self.repository_id / "architect_dispatches.json"
        dispatch_path.parent.mkdir(parents=True)
        dispatch_path.write_bytes(
            json.dumps(
                {"schema_version": 1, "repository_id": self.repository_id, "dispatches": []},
                separators=(",", ":"),
            ).encode("utf-8")
        )

    def test_inventory_and_disposable_import_preserve_source_ownership_and_omissions(self) -> None:
        inventory = inventory_selected_repository(
            self.repository,
            current_milestone_id="M999.001",
            dispatch_state_root=self.dispatch_state_root,
        )
        report = inventory.report()
        documents_by_path = {item["source_path"]: item for item in report["documents"]}
        self.assertEqual(documents_by_path[self.workflow_path]["v1_event_count"], len(_closeout_history()))
        self.assertEqual(documents_by_path[self.workflow_path]["source_owner"], "git_owned")
        self.assertEqual(
            documents_by_path["docs/history/milestones/12.65.0_legacy_prompt.md"]["source_class"],
            "allowlisted_historical_document",
        )
        self.assertEqual(report["dispatch_source"]["source_owner"], "central_operational")
        self.assertEqual(report["dispatch_source"]["status"], "present")

        destination = self.root / "disposable"
        destination.mkdir()
        result = rehearse_milestone_imports(inventory, destination)
        self.assertEqual(result["imported_milestone_count"], 2)
        self.assertEqual(result["mismatch_count"], 0)
        imported = {item["milestone_id"]: item for item in result["imports"]}
        full = imported["M999.001"]
        prompt_only = imported["M999.002"]
        self.assertEqual(full["event_count"], len(_closeout_history()))
        self.assertEqual(full["source_count"], 4)
        self.assertEqual(full["linked_event_count"], 0)
        self.assertEqual(full["unresolved_document_references"], [])
        self.assertIsNone(full["approved_prompt_revision_id"])
        self.assertEqual(prompt_only["source_count"], 2)
        self.assertEqual(prompt_only["event_count"], 0)
        self.assertEqual(full["exact_retry"], "PASS")
        self.assertEqual(full["source_parity"], "PASS")
        self.assertTrue(all(Path(item["database_path"]).is_relative_to(destination) for item in result["imports"]))
        excluded_sources = {item["source"] for item in result["exclusions"]}
        self.assertIn("docs/history/milestones/12.65.0_legacy_prompt.md", excluded_sources)
        self.assertIn("docs/milestones/M998.001_orphan_closeout.md", excluded_sources)

    def test_disposable_import_refuses_a_repository_local_destination(self) -> None:
        inventory = inventory_selected_repository(
            self.repository,
            current_milestone_id="M999.001",
            dispatch_state_root=self.dispatch_state_root,
        )
        with self.assertRaisesRegex(RuntimeError, "separate disposable temp directory"):
            rehearse_milestone_imports(inventory, self.repository_root)

    def test_quiesced_managed_import_prepares_all_scopes_without_changing_v1_selection(self) -> None:
        pointer_path = self.dispatch_state_root / self.repository_id / "milestones/selection.json"
        pointer_path.parent.mkdir(parents=True)
        pointer = {
            "schema_version": 1,
            "repository_id": self.repository_id,
            "milestone_id": "M999.001",
            "prompt_path": self.prompt_path,
        }
        pointer_bytes = json.dumps(pointer).encode("utf-8")
        pointer_path.write_bytes(pointer_bytes)
        registry = RepositoryRegistry((self.repository,), default_key=self.repository.key)
        destination = self.root / "managed-import"
        destination.mkdir()
        with patch("repoctl.information_store.sqlite_runtime_status", return_value={"production_eligible": True}):
            report = prepare_managed_repository_imports(
                registry, destination, state_root=self.dispatch_state_root, writers_quiesced=True,
            )
            self.assertEqual(report["storage_layout"], "single_managed_prepared_database")
            self.assertEqual(report["imported_milestone_count"], 2)
            self.assertEqual(report["mismatch_count"], 0)
            self.assertFalse(report["external_selections_changed"])
            self.assertEqual(pointer_path.read_bytes(), pointer_bytes)
            binding = dict(report["prepared_selection_bindings"][0])
            expected_hash = binding.pop("v1_selection_sha256")
            identity = StoreIdentity(**binding)
            store = InformationStore.bind_prepared_v1_selection(
                report["prepared_database_path"], identity, selection_state_root=self.dispatch_state_root,
                expected_v1_sha256=expected_hash, cutover_confirmed=True,
            )
            self.assertEqual(len(store.list_disposable_milestones()["candidates"]), 2)
            self.assertEqual(store.identity.milestone_id, "M999.001")
            self.assertEqual((self.repository_root / self.workflow_path).read_text(encoding="utf-8").count(
                "repoctl-workflow-ledger:v1"
            ), 1)

        rejected_destination = self.root / "unsupported-managed-import"
        rejected_destination.mkdir()
        pointer_path.write_bytes(pointer_bytes)
        with (
            patch("repoctl.information_store.sqlite_runtime_status", return_value={
                "production_eligible": False, "sqlite_version": "3.45.1",
            }),
            self.assertRaises(RuntimeGateError),
        ):
            prepare_managed_repository_imports(
                registry, rejected_destination, state_root=self.dispatch_state_root, writers_quiesced=True,
            )
        self.assertFalse((rejected_destination / "managed-prepared.sqlite3").exists())

    def test_registered_repository_inventory_imports_explicit_roots_without_selection_change(self) -> None:
        dispatch_path = self.dispatch_state_root / self.repository_id / "architect_dispatches.json"
        timestamp = "2026-01-01T00:00:00+00:00"
        dispatches = [
            {
                "dispatch_id": "a" * 32, "repository_id": self.repository_id,
                "milestone_id": "M999.001", "type": "INSTRUCTION", "status": "APPROVED",
                "body": "Existing guidance snapshot.", "approved_milestone_id": "M999.001",
                "created_at": timestamp, "updated_at": timestamp,
            },
            {
                "dispatch_id": "b" * 32, "repository_id": self.repository_id,
                "milestone_id": "M999.099", "type": "PROMPT", "status": "PENDING",
                "title": "Future fixture", "slug": "future_fixture", "body": "# Proposed prompt\n",
                "canonical_destination": "docs/milestones/M999.099_future_fixture_prompt.md",
                "created_at": timestamp, "updated_at": timestamp,
            },
        ]
        dispatch_path.write_text(json.dumps({
            "schema_version": 1, "repository_id": self.repository_id, "dispatches": dispatches,
        }), encoding="utf-8")
        second_root = self.root / "second-repository"
        (second_root / "docs/context").mkdir(parents=True)
        (second_root / "docs/milestones").mkdir(parents=True)
        (second_root / "docs/context/project_context.md").write_text(
            "# Second repository\n",
            encoding="utf-8",
        )
        (second_root / "docs/milestones/M999.001_fixture_prompt.md").write_text(
            "# Second prompt\n",
            encoding="utf-8",
        )
        second = RepositoryEntry(
            key="fixture-second",
            name="Second fixture",
            path=second_root.resolve(),
            repository_id=make_repository_id(second_root.resolve()),
            milestone_root="docs/milestones",
            documents={"context": "docs/context/project_context.md"},
        )
        registry = RepositoryRegistry((self.repository, second), default_key=self.repository.key)
        selected_before = registry.active()
        inventory = inventory_registered_repositories(
            registry,
            authorized_repository_keys=(self.repository.key, second.key),
            current_milestone_ids={self.repository.key: "M999.001"},
            dispatch_state_root=self.dispatch_state_root,
        )
        self.assertEqual(registry.active(), selected_before)
        report = inventory.report()
        self.assertEqual(report["accessible_repository_count"], 2)
        self.assertEqual(report["excluded_repository_count"], 0)
        self.assertEqual(report["repository_count"], 2)
        self.assertEqual(report["category_counts"]["canonical_milestone_prompt"], 3)
        self.assertEqual(
            {item["repository_key"] for item in report["repositories"]},
            {self.repository.key, second.key},
        )
        self.assertTrue(
            all(
                item["source_provenance"]["working_tree"] is True
                and item["source_provenance"]["commit_membership"] == "not established"
                for repository_report in report["repositories"]
                for item in repository_report["documents"]
            )
        )

        destination = self.root / "registered-disposable"
        destination.mkdir()
        result = rehearse_registered_repository_imports(inventory, destination)
        self.assertEqual(result["imported_milestone_count"], 3)
        self.assertEqual(result["imported_source_count"], 7)
        self.assertEqual(result["mismatch_count"], 0)
        first_report = result["repositories"][0]["rehearsal"]
        self.assertIn(
            "dispatch:" + "b" * 32,
            [item["source"] for item in first_report["exclusions"]],
        )
        self.assertFalse(result["authority_changed"])
        database_paths = [
            Path(item["database_path"])
            for repository_report in result["repositories"]
            for item in repository_report["rehearsal"]["imports"]
        ]
        self.assertEqual(len(database_paths), 3)
        self.assertEqual(len(set(database_paths)), 3)
        self.assertTrue(all(path.is_relative_to(destination) for path in database_paths))
        self.assertEqual(registry.active(), selected_before)

        central_destination = self.root / "central-disposable"
        central_destination.mkdir()
        central = rehearse_registered_repository_imports(inventory, central_destination, central_database=True)
        self.assertEqual(central["storage_layout"], "single_disposable_database")
        self.assertEqual(central["imported_milestone_count"], 3)
        self.assertEqual(central["imported_source_count"], 7)
        self.assertEqual(central["mismatch_count"], 0)
        self.assertEqual(central["dispatch_record_count"], 2)
        self.assertEqual(central["central_imported_dispatch_count"], 2)
        dispatch_audit = central["repositories"][0]["rehearsal"]["dispatch_snapshot_audit"]
        self.assertEqual(dispatch_audit["source_count"], 2)
        self.assertEqual(dispatch_audit["imported_missing_count"], 1)
        self.assertEqual(dispatch_audit["exact_retry"], "PASS")
        central_paths = {
            item["database_path"]
            for repository_report in central["repositories"]
            for item in repository_report["rehearsal"]["imports"]
        }
        self.assertEqual(len(central_paths), 1)
        with sqlite3.connect(next(iter(central_paths))) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM repository").fetchone()[0], 2)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM milestone").fetchone()[0], 3)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM workflow_state").fetchone()[0], 3)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM architect_dispatch").fetchone()[0], 2)
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM milestone WHERE milestone_id = 'M999.099'"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM dispatch_history WHERE po_confirmed IS NOT NULL").fetchone()[0],
                0,
            )
        self.assertFalse(central["authority_changed"])
        self.assertEqual(registry.active(), selected_before)

    def test_registered_inventory_rejects_unregistered_and_duplicate_authorizations(self) -> None:
        registry = RepositoryRegistry((self.repository,), default_key=self.repository.key)
        with self.assertRaisesRegex(MigrationRehearsalError, "unique sequence"):
            inventory_registered_repositories(
                registry,
                authorized_repository_keys=(self.repository.key, self.repository.key),
                current_milestone_ids=None,
                dispatch_state_root=self.dispatch_state_root,
            )
        with self.assertRaisesRegex(MigrationRehearsalError, "not in the validated registry"):
            inventory_registered_repositories(
                registry,
                authorized_repository_keys=("not-registered",),
                current_milestone_ids=None,
                dispatch_state_root=self.dispatch_state_root,
            )

    def test_central_import_reports_dispatches_without_a_bindable_milestone(self) -> None:
        empty_root = self.root / "no-prompt"
        (empty_root / "docs/milestones").mkdir(parents=True)
        empty = RepositoryEntry(
            key="empty",
            name="No prompt fixture",
            path=empty_root.resolve(),
            repository_id=make_repository_id(empty_root.resolve()),
            milestone_root="docs/milestones",
            documents={},
        )
        dispatch_path = self.dispatch_state_root / empty.repository_id / "architect_dispatches.json"
        dispatch_path.parent.mkdir(parents=True)
        timestamp = "2026-01-01T00:00:00+00:00"
        dispatch_path.write_text(json.dumps({
            "schema_version": 1,
            "repository_id": empty.repository_id,
            "dispatches": [{
                "dispatch_id": "c" * 32,
                "repository_id": empty.repository_id,
                "milestone_id": "M999.099",
                "type": "PROMPT",
                "status": "PENDING",
                "title": "Future fixture",
                "slug": "future_fixture",
                "body": "# Proposed prompt\n",
                "canonical_destination": "docs/milestones/M999.099_future_fixture_prompt.md",
                "created_at": timestamp,
                "updated_at": timestamp,
            }],
        }), encoding="utf-8")
        registry = RepositoryRegistry((self.repository, empty), default_key=self.repository.key)
        inventory = inventory_registered_repositories(
            registry,
            authorized_repository_keys=(self.repository.key, empty.key),
            current_milestone_ids=None,
            dispatch_state_root=self.dispatch_state_root,
        )
        destination = self.root / "no-prompt-rehearsal"
        destination.mkdir()
        result = rehearse_registered_repository_imports(inventory, destination, central_database=True)
        empty_report = result["repositories"][1]["rehearsal"]
        self.assertEqual(result["dispatch_record_count"], 1)
        self.assertEqual(result["central_imported_dispatch_count"], 0)
        self.assertEqual(empty_report["mismatch_count"], 1)
        self.assertIn("No canonical milestone scope", empty_report["mismatches"][0]["reason"])
        with sqlite3.connect(destination / "central-rehearsal.sqlite3") as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM architect_dispatch").fetchone()[0], 0)

    def test_legacy_dispatch_pagination_rejects_stale_snapshot(self) -> None:
        path = self.dispatch_state_root / self.repository_id / "architect_dispatches.json"
        timestamp = "2026-01-01T00:00:00+00:00"
        records = [
            {
                "dispatch_id": str(index) * 32,
                "repository_id": self.repository_id,
                "milestone_id": "M999.001",
                "type": "INSTRUCTION",
                "status": "PENDING",
                "body": f"Instruction {index}",
                "created_at": timestamp,
                "updated_at": timestamp,
            }
            for index in range(1, 4)
        ]
        ledger = {"schema_version": 1, "repository_id": self.repository_id, "dispatches": records}
        path.write_text(json.dumps(ledger), encoding="utf-8")
        kwargs = {
            "repository_root": self.repository_root,
            "state_root": self.dispatch_state_root,
            "repository_id": self.repository_id,
            "milestone_id": "M999.001",
            "status": "PENDING",
            "limit": 2,
        }
        first = architect_dispatches.list_dispatch_page(**kwargs)
        second = architect_dispatches.list_dispatch_page(**kwargs, cursor=first["next_cursor"])
        self.assertEqual(first["total_count"], 3)
        self.assertTrue(first["has_more"])
        self.assertEqual(
            [item["dispatch_id"] for item in first["dispatches"] + second["dispatches"]],
            ["3" * 32, "2" * 32, "1" * 32],
        )
        self.assertTrue(second["complete"])
        ledger["dispatches"][0]["body"] = "Changed source"
        path.write_text(json.dumps(ledger), encoding="utf-8")
        with self.assertRaisesRegex(architect_dispatches.ArchitectDispatchError, "stale"):
            architect_dispatches.list_dispatch_page(**kwargs, cursor=first["next_cursor"])

    def test_registered_inventory_marks_unreadable_root_not_verified_and_continues(self) -> None:
        second = RepositoryEntry(
            key="fixture-second",
            name="Second fixture",
            path=self.repository_root,
            repository_id="different-id",
            milestone_root="docs/milestones",
            documents={"context": "docs/context/project_context.md"},
        )
        registry = RepositoryRegistry((self.repository, second), default_key=self.repository.key)
        from unittest.mock import patch

        original = inventory_selected_repository

        def fail_second(repository, **kwargs):
            if repository.key == second.key:
                raise SourceEnumerationError("fixture permission denied")
            return original(repository, **kwargs)

        with patch("repoctl.migration_rehearsal.inventory_selected_repository", side_effect=fail_second):
            inventory = inventory_registered_repositories(
                registry,
                authorized_repository_keys=(self.repository.key, second.key),
                current_milestone_ids=None,
                dispatch_state_root=self.dispatch_state_root,
            )
        self.assertEqual(len(inventory.inventories), 1)
        self.assertEqual(len(inventory.exclusions), 1)
        self.assertEqual(inventory.exclusions[0]["status"], "NOT_VERIFIED")
        self.assertIn("permission denied", inventory.exclusions[0]["reason"])


if __name__ == "__main__":
    unittest.main()
