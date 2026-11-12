from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from repoctl.migration_rehearsal import (
    inventory_selected_repository,
    rehearse_milestone_imports,
)
from repoctl.scanner.util import make_repository_id
from repoctl.web.repository_registry import RepositoryEntry
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


if __name__ == "__main__":
    unittest.main()
