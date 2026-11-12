from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from repoctl.migration_rehearsal import (
    MigrationRehearsalError,
    inventory_registered_repositories,
    inventory_selected_repository,
    rehearse_milestone_imports,
    rehearse_registered_repository_imports,
)
from repoctl.scanner.util import make_repository_id
from repoctl.web.repository_registry import RepositoryEntry, RepositoryRegistry
from repoctl.document_library import SourceEnumerationError
from repoctl.workflow import lifecycle
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

    def test_registered_repository_inventory_imports_explicit_roots_without_selection_change(self) -> None:
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
