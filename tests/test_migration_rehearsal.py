from __future__ import annotations

import json
import hashlib
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from repoctl.information_store import InformationStore, RuntimeGateError, StoreIdentity
from repoctl.migration_rehearsal import (
    MigrationRehearsalError,
    freeze_managed_import_writers,
    inventory_registered_repositories,
    inventory_selected_repository,
    prepare_managed_repository_imports,
    release_managed_import_writers,
    rehearse_milestone_imports,
    rehearse_registered_repository_imports,
)
from repoctl.scanner.core import StateArtifactWriteFreezeError
from repoctl.scanner.util import make_repository_id
from repoctl.store_authority import activate_prepared_authority, preview_prepared_authority
from repoctl.web.app import create_web_app
from repoctl.web.repository_registry import RepositoryEntry, RepositoryRegistry
from repoctl.document_library import SourceEnumerationError
from repoctl.workflow import architect_dispatches, lifecycle
from repoctl.workflow.milestones import (
    discover_milestones,
    load_milestone_selection,
    prompt_identity,
    save_milestone_selection,
)
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

    def test_import_does_not_promote_approval_after_prompt_bytes_change(self) -> None:
        state_root = self.dispatch_state_root
        save_milestone_selection(self.repository_root, state_root, "M999.002")
        candidate = next(
            item for item in discover_milestones(self.repository_root)["candidates"]
            if item["milestone_id"] == "M999.002"
        )
        reviewed_sha256 = prompt_identity(self.repository_root, "docs/milestones", candidate)
        lifecycle.record_milestone_event(
            self.repository_root,
            state_root,
            "docs/milestones",
            candidate,
            event_type="prompt_approved",
            summary="Approve before the source prompt changes.",
            actor_role="product_owner",
            po_approved=True,
            approval_repository_key=self.repository.key,
            expected_prompt_sha256=reviewed_sha256,
        )
        prompt = self.repository_root / candidate["prompt_path"]
        prompt.write_bytes(prompt.read_bytes() + b"\nChanged after approval.\n")

        inventory = inventory_selected_repository(
            self.repository,
            current_milestone_id="M999.002",
            dispatch_state_root=state_root,
        )
        destination = self.root / "stale-approval-import"
        destination.mkdir()
        report = rehearse_milestone_imports(inventory, destination)
        imported = next(
            item for item in report["imports"] if item["milestone_id"] == "M999.002"
        )
        self.assertEqual(imported["prompt_approval_provenance"]["status"], "unverified_lifecycle_only")
        self.assertIsNone(imported["approved_prompt_revision_id"])
        self.assertEqual(imported["linked_event_count"], 0)

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
        candidate_state_root = self.root / "candidate-state"
        destination.mkdir()
        candidate_state_root.mkdir()
        with self.assertRaisesRegex(MigrationRehearsalError, "active Repo Control writer freeze"):
            prepare_managed_repository_imports(
                registry,
                destination,
                state_root=self.dispatch_state_root,
                candidate_state_root=candidate_state_root,
                writers_quiesced=True,
            )
        freeze = freeze_managed_import_writers(registry, state_root=self.dispatch_state_root)
        dispatch_path = self.dispatch_state_root / self.repository_id / "architect_dispatches.json"
        workflow_path = self.repository_root / self.workflow_path
        selection_before = pointer_path.read_bytes()
        dispatch_before = dispatch_path.read_bytes()
        workflow_before = workflow_path.read_bytes()
        with self.assertRaises(StateArtifactWriteFreezeError):
            save_milestone_selection(self.repository_root, self.dispatch_state_root, "M999.002")
        with self.assertRaises(StateArtifactWriteFreezeError):
            architect_dispatches.submit_dispatch(
                self.repository_root,
                self.dispatch_state_root,
                self.repository_id,
                dispatch_type="INSTRUCTION",
                milestone_id="M999.001",
                body="Blocked while the registered source snapshot is frozen.",
            )
        with self.assertRaises(StateArtifactWriteFreezeError):
            lifecycle.record_milestone_event(
                self.repository_root,
                self.dispatch_state_root,
                "docs/milestones",
                {"milestone_id": "M999.001", "prompt_path": self.prompt_path},
                event_type="test_result_recorded",
                summary="Blocked while the registered source snapshot is frozen.",
                actor_role="coder",
                evidence_source="disposable fixture",
                result="PASS",
            )
        self.assertEqual(pointer_path.read_bytes(), selection_before)
        self.assertEqual(dispatch_path.read_bytes(), dispatch_before)
        self.assertEqual(workflow_path.read_bytes(), workflow_before)
        with patch("repoctl.information_store.sqlite_runtime_status", return_value={"production_eligible": True}):
            report = prepare_managed_repository_imports(
                registry,
                destination,
                state_root=self.dispatch_state_root,
                candidate_state_root=candidate_state_root,
                writers_quiesced=True,
            )
            self.assertEqual(report["storage_layout"], "single_managed_prepared_database")
            self.assertEqual(report["imported_milestone_count"], 2)
            self.assertEqual(report["mismatch_count"], 0)
            self.assertFalse(report["external_selections_changed"])
            self.assertEqual(report["writer_freeze_id"], freeze["freeze_id"])
            self.assertEqual(report["source_inventory_sha256"], freeze["source_inventory_sha256"])
            self.assertEqual(pointer_path.read_bytes(), pointer_bytes)
            candidate_pointer = candidate_state_root / self.repository_id / "milestones/selection.json"
            self.assertEqual(candidate_pointer.read_bytes(), pointer_bytes)
            binding = dict(report["prepared_selection_bindings"][0])
            expected_hash = binding.pop("v1_selection_sha256")
            identity = StoreIdentity(**binding)
            store = InformationStore.bind_prepared_v1_selection(
                report["prepared_database_path"], identity, selection_state_root=candidate_state_root,
                expected_v1_sha256=expected_hash, cutover_confirmed=True,
            )
            self.assertEqual(len(store.list_disposable_milestones()["candidates"]), 2)
            self.assertEqual(store.identity.milestone_id, "M999.001")
            self.assertEqual((self.repository_root / self.workflow_path).read_text(encoding="utf-8").count(
                "repoctl-workflow-ledger:v1"
            ), 1)
            self.assertNotEqual(candidate_pointer.read_bytes(), pointer_bytes)
            self.assertEqual(pointer_path.read_bytes(), pointer_bytes)
        release = release_managed_import_writers(
            registry,
            state_root=self.dispatch_state_root,
            freeze_id=freeze["freeze_id"],
        )
        self.assertEqual(release["status"], "released")

        rejected_destination = self.root / "unsupported-managed-import"
        rejected_candidate_state_root = self.root / "rejected-candidate-state"
        rejected_destination.mkdir()
        rejected_candidate_state_root.mkdir()
        pointer_path.write_bytes(pointer_bytes)
        failed_runtime_freeze = freeze_managed_import_writers(registry, state_root=self.dispatch_state_root)
        with (
            patch("repoctl.information_store.sqlite_runtime_status", return_value={
                "production_eligible": False, "sqlite_version": "3.45.1",
            }),
            self.assertRaises(RuntimeGateError),
        ):
            prepare_managed_repository_imports(
                registry,
                rejected_destination,
                state_root=self.dispatch_state_root,
                candidate_state_root=rejected_candidate_state_root,
                writers_quiesced=True,
            )
        self.assertFalse((rejected_destination / "managed-prepared.sqlite3").exists())
        release_managed_import_writers(
            registry,
            state_root=self.dispatch_state_root,
            freeze_id=failed_runtime_freeze["freeze_id"],
            abort_reason="The pinned SQLite runtime eligibility check failed.",
        )

    def test_exact_browser_approval_survives_import_activation_and_fresh_coder_status(self) -> None:
        source_state_root = self.dispatch_state_root
        save_milestone_selection(self.repository_root, source_state_root, "M999.002")
        for arguments in (
            ["git", "-C", str(self.repository_root), "init", "-q"],
            ["git", "-C", str(self.repository_root), "config", "user.email", "test@example.com"],
            ["git", "-C", str(self.repository_root), "config", "user.name", "Test User"],
            ["git", "-C", str(self.repository_root), "add", "-A"],
            ["git", "-C", str(self.repository_root), "commit", "-qm", "migration fixture"],
        ):
            subprocess.run(arguments, check=True, capture_output=True, text=True)
        registry = RepositoryRegistry((self.repository,), default_key=self.repository.key)
        registry_path = self.root / "repositories.toml"
        registry_path.write_text("# Isolated migration fixture registry\n", encoding="utf-8")
        with patch(
            "repoctl.web.repository_registry.RepositoryRegistry.from_toml",
            return_value=registry,
        ):
            app = create_web_app(
                repository_registry_path=registry_path,
                state_root=source_state_root,
            )
        app.config["TESTING"] = True
        browser = app.test_client()
        self.assertEqual(browser.get("/milestones").status_code, 200)
        selection = load_milestone_selection(self.repository_root, source_state_root)["selection"]
        workflow = lifecycle.load_milestone_workflow(
            self.repository_root,
            source_state_root,
            self.repository.milestone_root,
            selection,
        )
        with browser.session_transaction() as browser_session:
            csrf_token = browser_session["csrf_token"]
        prompt_bytes = (self.repository_root / selection["prompt_path"]).read_bytes()
        approval_response = browser.post(
            "/milestones/workflow/event",
            data={
                "csrf_token": csrf_token,
                "repository_key": self.repository.key,
                "repository_generation": "0",
                "milestone_id": selection["milestone_id"],
                "prompt_path": selection["prompt_path"],
                "expected_prompt_identity": workflow["prompt_identity"],
                "expected_phase": "PLANNING",
                "expected_event_count": str(workflow["event_count"]),
                "event_type": "prompt_approved",
                "summary": "Approve the exact reviewed fixture prompt.",
                "actor_role": "product_owner",
                "po_approved": "yes",
            },
        )
        self.assertEqual(approval_response.status_code, 302)
        workflow = lifecycle.load_milestone_workflow(
            self.repository_root,
            source_state_root,
            self.repository.milestone_root,
            selection,
        )
        approval_event = workflow["event_history"][0]
        prompt_sha256 = hashlib.sha256(prompt_bytes).hexdigest()
        self.assertEqual(
            approval_event["prompt_approval"],
            {
                "schema_version": 1,
                "repository_key": self.repository.key,
                "repository_id": self.repository_id,
                "milestone_id": selection["milestone_id"],
                "prompt_path": selection["prompt_path"],
                "content_sha256": prompt_sha256,
            },
        )
        legacy_candidate = next(
            item for item in discover_milestones(self.repository_root)["candidates"]
            if item["milestone_id"] == "M999.001"
        )
        legacy_workflow_path = (
            self.repository_root / self.workflow_path
        )
        legacy_workflow_events = lifecycle._read_ledger(
            legacy_workflow_path,
            self.repository_id,
            legacy_candidate,
        )["events"]
        source_selection_path = (
            source_state_root / self.repository_id / "milestones/selection.json"
        )
        dispatch_path = (
            source_state_root / self.repository_id / "architect_dispatches.json"
        )
        source_snapshot = {
            self.repository_root / selection["prompt_path"]: prompt_bytes,
            self.repository_root / workflow["sidecar_path"]: (
                self.repository_root / workflow["sidecar_path"]
            ).read_bytes(),
            source_selection_path: source_selection_path.read_bytes(),
            dispatch_path: dispatch_path.read_bytes(),
        }

        destination = self.root / "exact-approval-import"
        candidate_state_root = self.root / "exact-approval-candidate-state"
        destination.mkdir()
        candidate_state_root.mkdir()
        freeze = freeze_managed_import_writers(registry, state_root=source_state_root)
        with (
            patch(
                "repoctl.web.repository_registry.RepositoryRegistry.from_toml",
                return_value=registry,
            ),
        ):
            report = prepare_managed_repository_imports(
                registry,
                destination,
                state_root=source_state_root,
                candidate_state_root=candidate_state_root,
                writers_quiesced=True,
            )
            self.assertEqual(report["mismatch_count"], 0)
            repository_report = next(
                item for item in report["repositories"]
                if item["repository_key"] == self.repository.key
            )["rehearsal"]
            imported_approval = next(
                item for item in repository_report["imports"]
                if item["milestone_id"] == selection["milestone_id"]
            )
            self.assertEqual(
                imported_approval["prompt_approval_provenance"]["status"],
                "migrated_exact_byte_source_approval",
            )
            self.assertEqual(imported_approval["linked_event_count"], 1)
            self.assertIsNotNone(imported_approval["approved_prompt_revision_id"])
            binding = next(
                item for item in report["prepared_selection_bindings"]
                if item["milestone_id"] == selection["milestone_id"]
            )
            expected_selection_hash = binding["v1_selection_sha256"]
            identity_fields = {
                key: value for key, value in binding.items() if key != "v1_selection_sha256"
            }
            store = InformationStore.bind_prepared_v1_selection(
                report["prepared_database_path"],
                StoreIdentity(**identity_fields),
                selection_state_root=candidate_state_root,
                expected_v1_sha256=expected_selection_hash,
                cutover_confirmed=True,
            )
            imported_document = next(
                item for item in store.list_documents()["documents"]
                if item["logical_type"] == "PROMPT"
            )
            self.assertEqual(
                imported_document["approved_revision_id"],
                imported_document["draft_revision_id"],
            )
            imported_prompt = store.get_revision(
                imported_document["document_id"],
                imported_document["approved_revision_id"],
            )
            self.assertEqual(imported_prompt["content_sha256"], prompt_sha256)
            generation = "c" * 32
            plan = preview_prepared_authority(
                report["prepared_database_path"],
                candidate_state_root,
                registry_path,
                generation,
                source_state_root=source_state_root,
            )
            activate_prepared_authority(
                report["prepared_database_path"],
                candidate_state_root,
                registry_path,
                generation,
                expected_plan_sha256=plan["plan_sha256"],
                po_confirmed=True,
                writers_quiesced=True,
                source_parity_confirmed=True,
                source_state_root=source_state_root,
            )
        release_managed_import_writers(
            registry,
            state_root=source_state_root,
            freeze_id=freeze["freeze_id"],
        )
        self.assertEqual(
            {path: path.read_bytes() for path in source_snapshot},
            source_snapshot,
        )
        with sqlite3.connect(report["prepared_database_path"]) as connection:
            connection.row_factory = sqlite3.Row
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            target_events = [
                json.loads(row["payload_json"])
                for row in connection.execute(
                    """
                    SELECT payload_json FROM workflow_event
                    WHERE repository_id = ? AND milestone_id = ? ORDER BY sequence
                    """,
                    (self.repository_id, selection["milestone_id"]),
                )
            ]
            legacy_events = [
                json.loads(row["payload_json"])
                for row in connection.execute(
                    """
                    SELECT payload_json FROM workflow_event
                    WHERE repository_id = ? AND milestone_id = ? ORDER BY sequence
                    """,
                    (self.repository_id, "M999.001"),
                )
            ]
            decision = connection.execute(
                """
                SELECT decision_type, actor_role, po_confirmed, source_kind, payload_json
                FROM document_revision_decision
                WHERE repository_id = ? AND milestone_id = ?
                """,
                (self.repository_id, selection["milestone_id"]),
            ).fetchone()
            link_count = connection.execute(
                """
                SELECT COUNT(*) FROM workflow_document_link
                WHERE repository_id = ? AND milestone_id = ?
                """,
                (self.repository_id, selection["milestone_id"]),
            ).fetchone()[0]
        self.assertEqual(target_events, workflow["event_history"])
        self.assertEqual(legacy_events, legacy_workflow_events)
        self.assertEqual(link_count, 1)
        self.assertEqual(
            (decision["decision_type"], decision["actor_role"], decision["po_confirmed"], decision["source_kind"]),
            ("APPROVED", "product_owner", 1, "v1_imported_prompt_approval"),
        )
        decision_payload = json.loads(decision["payload_json"])
        self.assertEqual(decision_payload["source_event_id"], approval_event["event_id"])
        self.assertEqual(decision_payload["source_prompt_sha256"], prompt_sha256)

        cli_script = (
            "import sys\n"
            "import os\n"
            "from pathlib import Path\n"
            "from repoctl.scanner import core as scanner_core\n"
            "from repoctl.web.repository_registry import RepositoryEntry, RepositoryRegistry\n"
            "from repoctl.workflow import coder_interface\n"
            "os.environ['REPOCTL_BACKUP_COVERAGE_PATH'] = str(\n"
            "    Path(sys.argv[5]).parent / 'candidate-test-recovery-point.json'\n"
            ")\n"
            "repository = Path(sys.argv[1]).resolve()\n"
            "entry = RepositoryEntry(\n"
            "    key=sys.argv[2], name='Migration fixture', path=repository, repository_id=sys.argv[3],\n"
            "    milestone_root='docs/milestones', documents={'context': 'docs/context/project_context.md'},\n"
            ")\n"
            "registry = RepositoryRegistry((entry,), default_key=entry.key)\n"
            "RepositoryRegistry.from_toml = classmethod(lambda cls, path: registry)\n"
            "coder_interface.CENTRAL_REPOSITORY_REGISTRY_PATH = Path(sys.argv[4])\n"
            "coder_interface.resolve_repository_entry = lambda repository_path=None, milestone_root=None: entry\n"
            "scanner_core.DEFAULT_STATE_ROOT = Path(sys.argv[5]).parent / 'unused-default-state'\n"
            "from repoctl.cli import main\n"
            "raise SystemExit(main(sys.argv[6:]))\n"
        )
        def run_independent_status():
            return subprocess.run(
                [
                sys.executable,
                "-c",
                cli_script,
                str(self.repository_root),
                self.repository.key,
                self.repository_id,
                str(registry_path),
                str(candidate_state_root),
                "coder-status",
                "--repository",
                str(self.repository_root),
                "--state-root",
                str(candidate_state_root),
                "--format",
                "json",
                ],
                cwd=self.repository_root,
                capture_output=True,
                text=True,
                check=False,
            )

        statuses = []
        for _ in range(2):
            cli_result = run_independent_status()
            self.assertEqual(cli_result.returncode, 0, cli_result.stderr)
            status = json.loads(cli_result.stdout)
            self.assertEqual(status["information_store_authority"], "sqlite_managed")
            self.assertEqual(status["selection"]["milestone_id"], selection["milestone_id"])
            self.assertEqual(status["prompt"]["identity"], (
                f"{imported_prompt['document_id']}:{imported_prompt['revision_id']}:{prompt_sha256}"
            ))
            statuses.append(status)
        self.assertEqual(statuses[0]["prompt"], statuses[1]["prompt"])
        self.assertEqual(statuses[0]["selection"], statuses[1]["selection"])

    def test_registered_source_drift_requires_abort_before_writer_release(self) -> None:
        pointer_path = self.dispatch_state_root / self.repository_id / "milestones/selection.json"
        pointer_path.parent.mkdir(parents=True)
        pointer_path.write_text(json.dumps({
            "schema_version": 1,
            "repository_id": self.repository_id,
            "milestone_id": "M999.001",
            "prompt_path": self.prompt_path,
        }), encoding="utf-8")
        registry = RepositoryRegistry((self.repository,), default_key=self.repository.key)
        freeze = freeze_managed_import_writers(registry, state_root=self.dispatch_state_root)
        prompt = self.repository_root / self.prompt_path
        original = prompt.read_bytes()
        prompt.write_bytes(original + b"\nChanged outside the cooperating writer boundary.\n")
        destination = self.root / "drifted-import"
        candidate_state_root = self.root / "drifted-candidate-state"
        destination.mkdir()
        candidate_state_root.mkdir()
        with self.assertRaisesRegex(MigrationRehearsalError, "sources drifted after the writer freeze"):
            prepare_managed_repository_imports(
                registry,
                destination,
                state_root=self.dispatch_state_root,
                candidate_state_root=candidate_state_root,
                writers_quiesced=True,
            )
        with self.assertRaisesRegex(MigrationRehearsalError, "abort the candidate"):
            release_managed_import_writers(
                registry,
                state_root=self.dispatch_state_root,
                freeze_id=freeze["freeze_id"],
            )
        aborted = release_managed_import_writers(
            registry,
            state_root=self.dispatch_state_root,
            freeze_id=freeze["freeze_id"],
            abort_reason="External source drift invalidated the disposable import.",
        )
        self.assertEqual(aborted["status"], "aborted")
        self.assertIn("External source drift", aborted["abort_reason"])

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
