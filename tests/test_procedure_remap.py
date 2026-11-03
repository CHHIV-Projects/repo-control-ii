from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from repoctl.cli import build_parser
from repoctl.procedure_governance import load_bundle, materialize_preview, preview_reconciliation
from repoctl.procedure_remap import (
    ProcedureRemapError,
    finalize_role_remap,
    inspect_role_remap,
    preview_role_remap,
    read_runtime_binding,
    recover_role_remap,
    stage_role_remap,
)
from repoctl.scanner.util import make_repository_id
from repoctl.web.repository_registry import RepositoryEntry, RepositoryRegistry
from repoctl.workflow.milestones import save_milestone_selection


OLD_DOCUMENTS = {
    "context": "docs/context/project_context_v8.md",
    "architecture": "docs/context/project_architecture_v8.md",
    "workflow": "docs/context/project_workflow_v8.md",
    "coding_agent_rules": "docs/context/coding_agent_rules_v8.md",
    "parking_lot": "docs/context/canonical_parking_lot_v8.md",
}
NEW_DOCUMENTS = {
    "context": "docs/context/project_context.md",
    "architecture": "docs/context/project_architecture.md",
    "workflow": "docs/context/project_workflow.md",
    "coding_agent_rules": "docs/context/coding_agent_rules.md",
    "parking_lot": "docs/context/canonical_parking_lot.md",
}


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        check=True,
        text=True,
    )
    return result.stdout.strip()


def _init_repo(root: Path) -> Path:
    root.mkdir(parents=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Repo Control fixture")
    _git(root, "config", "user.email", "repo-control-fixture@example.invalid")
    (root / "docs/milestones").mkdir(parents=True)
    return root.resolve()


class ProcedureRemapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.state_root = self.root / "state"
        self.control = _init_repo(self.root / "control")
        self.target = _init_repo(self.root / "photo-organizer")
        self.unrelated = _init_repo(self.root / "unrelated")
        (self.control / "ops").mkdir()
        self.registry_path = self.control / "ops/repositories.toml"
        self.entry = RepositoryEntry(
            key="photo-organizer",
            name="Photo Organizer",
            path=self.target,
            repository_id=make_repository_id(self.target),
            milestone_root="docs/milestones",
            documents=dict(OLD_DOCUMENTS),
        )
        self.control_entry = RepositoryEntry(
            key="repo-control-ii",
            name="Repo Control II",
            path=self.control,
            repository_id=make_repository_id(self.control),
            milestone_root="docs/milestones",
            documents={},
        )
        self.unrelated_documents = {
            role: f"docs/unrelated/{role}.md" for role in OLD_DOCUMENTS
        }
        self._write_registry()
        self.registry_patcher = mock.patch(
            "repoctl.workflow.coder_interface.CENTRAL_REPOSITORY_REGISTRY_PATH",
            self.registry_path,
        )
        self.registry_patcher.start()
        self._write_documents()
        self.prompt = self.target / "docs/milestones/M001.003_role_remap_prompt.md"
        self.prompt.write_text("# Fixture milestone\n", encoding="utf-8")
        self.bundle = load_bundle()
        plan = preview_reconciliation(self.entry, self.bundle)
        self.assertTrue(plan.can_materialize, plan.blockers)
        materialize_preview(self.entry, plan)
        _git(self.target, "add", ".")
        _git(self.target, "commit", "-qm", "fixture baseline")
        _git(self.control, "add", ".")
        _git(self.control, "commit", "-qm", "fixture registry")
        save_milestone_selection(self.target, self.state_root, "M001.003")

    def tearDown(self) -> None:
        self.registry_patcher.stop()
        self.temporary.cleanup()

    def _write_registry(self) -> None:
        lines = [
            'default = "repo-control-ii"',
            "",
            "[repositories.repo-control-ii]",
            'name = "Repo Control II"',
            f"path = {json.dumps(str(self.control))}",
            'milestone_root = "docs/milestones"',
            "",
            "[repositories.repo-control-ii.documents]",
            "",
            "[repositories.photo-organizer]",
            'name = "Photo Organizer"',
            f"path = {json.dumps(str(self.target))}",
            'milestone_root = "docs/milestones"',
            "",
            "[repositories.photo-organizer.documents]",
        ]
        lines.extend(f"{role} = {json.dumps(path)}" for role, path in OLD_DOCUMENTS.items())
        lines.extend(
            [
                "",
                "[repositories.unrelated]",
                'name = "Unrelated Repository"',
                f"path = {json.dumps(str(self.unrelated))}",
                'milestone_root = "docs/milestones"',
                "",
                "[repositories.unrelated.documents]",
            ]
        )
        lines.extend(f"{role} = {json.dumps(path)}" for role, path in self.unrelated_documents.items())
        self.registry_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _write_documents(self) -> None:
        for role, relative in OLD_DOCUMENTS.items():
            path = self.target / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            content = f"# Existing {role} v8\n\nOld authoritative {role} text.\n"
            if role == "context":
                content += "\nSee [architecture](project_architecture_v8.md).\n"
            if role == "coding_agent_rules":
                content += "\n<!-- repo-control-project-overlay:v1 -->\n\n## Repository-specific rules\n\n- Preserve old rules.\n"
            path.write_text(content, encoding="utf-8")
        for role, relative in NEW_DOCUMENTS.items():
            path = self.target / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            content = f"# Existing {role}\n\nCanonical destination {role} content.\n"
            if role == "context":
                content += "\nSee [architecture](project_architecture.md).\n"
            if role == "coding_agent_rules":
                content += "\n<!-- repo-control-project-overlay:v1 -->\n\n## Repository-specific rules\n\n- Preserve canonical rules.\n"
            path.write_text(content, encoding="utf-8")

    def _preview(self, documents: dict[str, str] | None = None) -> dict:
        return preview_role_remap(
            self.entry,
            documents or NEW_DOCUMENTS,
            registry_path=self.registry_path,
            state_root=self.state_root,
            active_key=self.entry.key,
            registry_generation=3,
            runtime_instance_id="a" * 32,
            bundle=self.bundle,
        )

    def _stage(self, plan: dict | None = None) -> dict:
        preview = plan or self._preview()
        return stage_role_remap(
            self.entry,
            NEW_DOCUMENTS,
            preview["plan_sha256"],
            self.state_root,
            registry_path=self.registry_path,
            active_key=self.entry.key,
            registry_generation=3,
            runtime_instance_id="a" * 32,
            bundle=self.bundle,
        )

    def test_preview_and_confirmed_stage_preserve_documents_and_finish_after_restart(self) -> None:
        old_bytes = {path: (self.target / path).read_bytes() for path in OLD_DOCUMENTS.values()}
        new_bytes = {path: (self.target / path).read_bytes() for path in NEW_DOCUMENTS.values()}

        preview = self._preview()

        self.assertTrue(preview["materialization_available"], preview["blockers"])
        self.assertEqual(preview["old_documents"], OLD_DOCUMENTS)
        self.assertEqual(preview["new_documents"], NEW_DOCUMENTS)
        context_dependencies = next(item for item in preview["document_dependencies"] if item["role"] == "context")
        self.assertEqual(context_dependencies["source_references"], [OLD_DOCUMENTS["architecture"]])
        self.assertEqual(context_dependencies["destination_references"], [NEW_DOCUMENTS["architecture"]])
        self.assertEqual(len(preview["generated_artifacts"]), len(self.bundle.artifacts))
        self.assertIn("AGENTS.md", {item["path"] for item in preview["generated_artifacts"]})
        self.assertTrue(preview["restart_implications"]["required"])
        self.assertTrue(preview["rollback_steps"])
        self.assertEqual(
            {change["path"] for change in preview["procedure_changes"]},
            {".github/copilot-instructions.md", "AGENTS.md", ".repo-control/shared-procedure.lock.toml"},
        )
        self.assertEqual(_git(self.target, "status", "--porcelain"), "")
        self.assertEqual((self.target / "docs/context/coding_agent_rules_v8.md").read_bytes(), old_bytes[OLD_DOCUMENTS["coding_agent_rules"]])

        staged = self._stage(preview)

        self.assertEqual(staged["state"], "AWAITING_RESTART")
        self.assertTrue(staged["restart_required"])
        self.assertEqual(
            {path: (self.target / path).read_bytes() for path in OLD_DOCUMENTS.values()},
            old_bytes,
        )
        self.assertEqual(
            {path: (self.target / path).read_bytes() for path in NEW_DOCUMENTS.values()},
            new_bytes,
        )
        self.assertEqual(_git(self.control, "status", "--porcelain"), "M ops/repositories.toml")
        self.assertEqual(inspect_role_remap(self.entry, self.state_root)["state"], "AWAITING_RESTART")

        current_registry = RepositoryRegistry.from_toml(self.registry_path)
        updated_entry = next(item for item in current_registry.entries if item.key == self.entry.key)
        self.assertEqual(updated_entry.documents, NEW_DOCUMENTS)
        unrelated_entry = next(item for item in current_registry.entries if item.key == "unrelated")
        self.assertEqual(unrelated_entry.documents, self.unrelated_documents)
        self.assertEqual(_git(self.unrelated, "status", "--porcelain"), "")
        from repoctl.procedure_governance import inspect_procedure

        self.assertEqual(inspect_procedure(updated_entry, self.bundle)["status"], "CURRENT")
        completed = finalize_role_remap(
            updated_entry,
            staged["operation_id"],
            self.state_root,
            active_key=self.entry.key,
            registry_generation=0,
            runtime_instance_id="b" * 32,
            bundle=self.bundle,
        )
        self.assertEqual(completed["state"], "ACTIVATED")
        self.assertTrue(completed["recovery_images_retained"])

    def test_unrelated_dirty_path_and_invalid_overlay_fail_closed(self) -> None:
        unrelated = self.target / "unrelated.txt"
        unrelated.write_text("unreviewed\n", encoding="utf-8")

        preview = self._preview()

        self.assertFalse(preview["materialization_available"])
        self.assertTrue(any("unrelated dirty path" in blocker for blocker in preview["blockers"]))
        self.assertEqual(self.registry_path.read_text(encoding="utf-8").count("coding_agent_rules_v8.md"), 1)
        unrelated.unlink()

        unrelated_sidecar = self.target / "docs/milestones/M999_unselected_workflow.md"
        unrelated_sidecar.write_text("unselected workflow history\n", encoding="utf-8")
        sidecar_preview = self._preview()
        self.assertFalse(sidecar_preview["materialization_available"])
        self.assertTrue(any("unrelated dirty path" in blocker for blocker in sidecar_preview["blockers"]))
        unrelated_sidecar.unlink()

        invalid_overlay = dict(NEW_DOCUMENTS)
        (self.target / NEW_DOCUMENTS["coding_agent_rules"]).write_text("# Not a procedure overlay\n", encoding="utf-8")
        with self.assertRaisesRegex(ProcedureRemapError, "overlay is invalid"):
            self._preview(invalid_overlay)

    def test_stale_plan_and_mapping_collisions_are_rejected(self) -> None:
        preview = self._preview()
        (self.target / NEW_DOCUMENTS["context"]).write_text("# changed after review\n", encoding="utf-8")
        with self.assertRaises(ProcedureRemapError):
            self._stage(preview)

        duplicate = dict(NEW_DOCUMENTS)
        duplicate["parking_lot"] = duplicate["context"]
        with self.assertRaisesRegex(ProcedureRemapError, "five distinct"):
            self._preview(duplicate)

    def test_confirmed_rollback_restores_exact_registry_and_generated_images(self) -> None:
        old_registry = self.registry_path.read_bytes()
        old_generated = {
            path: (self.target / path).read_bytes()
            for path in (
                ".github/copilot-instructions.md",
                "AGENTS.md",
                ".repo-control/shared-procedure.lock.toml",
            )
        }
        staged = self._stage()

        restored = recover_role_remap(self.entry, staged["operation_id"], self.state_root)

        self.assertEqual(restored["state"], "ROLLBACK_AWAITING_RESTART")
        self.assertTrue(restored["restart_required"])
        self.assertEqual(self.registry_path.read_bytes(), old_registry)
        self.assertEqual(
            {path: (self.target / path).read_bytes() for path in old_generated},
            old_generated,
        )
        finalized = finalize_role_remap(
            self.entry,
            staged["operation_id"],
            self.state_root,
            active_key=self.entry.key,
            registry_generation=0,
            runtime_instance_id="b" * 32,
            bundle=self.bundle,
        )
        self.assertEqual(finalized["state"], "ROLLED_BACK")

    def test_partial_stage_failure_retains_recovery_images_and_recovers(self) -> None:
        preview = self._preview()
        original_writer = __import__("repoctl.procedure_remap", fromlist=["_write_target"])._write_target
        calls = 0

        def fail_after_one(*args, **kwargs):
            nonlocal calls
            original_writer(*args, **kwargs)
            calls += 1
            if calls == 4:
                raise OSError("injected partial-stage failure")

        with mock.patch("repoctl.procedure_remap._write_target", side_effect=fail_after_one):
            with self.assertRaisesRegex(ProcedureRemapError, "retains exact recovery images"):
                self._stage(preview)

        transaction = inspect_role_remap(self.entry, self.state_root)
        self.assertEqual(transaction["state"], "RECOVERY_REQUIRED")
        self.assertFalse(transaction["blocked"])
        self.assertEqual(
            RepositoryRegistry.from_toml(self.registry_path).entries[1].documents,
            NEW_DOCUMENTS,
        )
        restored = recover_role_remap(self.entry, transaction["operation_id"], self.state_root)
        self.assertEqual(restored["state"], "ROLLBACK_AWAITING_RESTART")
        self.assertEqual(RepositoryRegistry.from_toml(self.registry_path).entries[1].documents, OLD_DOCUMENTS)

    def test_concurrent_unknown_generated_edit_is_never_overwritten(self) -> None:
        original_writer = __import__("repoctl.procedure_remap", fromlist=["_write_target"])._write_target
        external_content = b"# concurrent operator edit\n"
        calls = 0

        def inject_concurrent_edit(*args, **kwargs):
            nonlocal calls
            original_writer(*args, **kwargs)
            calls += 1
            if calls == 1:
                (self.target / "AGENTS.md").write_bytes(external_content)

        with mock.patch("repoctl.procedure_remap._write_target", side_effect=inject_concurrent_edit):
            with self.assertRaisesRegex(ProcedureRemapError, "retains exact recovery images"):
                self._stage()

        transaction = inspect_role_remap(self.entry, self.state_root)
        self.assertEqual(transaction["state"], "RECOVERY_REQUIRED")
        self.assertTrue(transaction["blocked"])
        self.assertEqual((self.target / "AGENTS.md").read_bytes(), external_content)
        self.assertEqual(RepositoryRegistry.from_toml(self.registry_path).entries[1].documents, OLD_DOCUMENTS)
        with self.assertRaisesRegex(ProcedureRemapError, "neither recorded transaction image"):
            recover_role_remap(self.entry, transaction["operation_id"], self.state_root)

    def test_active_selection_and_one_point_three_installation_are_required(self) -> None:
        with self.assertRaisesRegex(ProcedureRemapError, "actively selected"):
            preview_role_remap(
                self.entry,
                NEW_DOCUMENTS,
                registry_path=self.registry_path,
                state_root=self.state_root,
                active_key=self.control_entry.key,
                registry_generation=0,
                runtime_instance_id="a" * 32,
                bundle=self.bundle,
            )

    def test_stale_registry_and_unsafe_destination_fail_closed(self) -> None:
        preview = self._preview()
        self.registry_path.write_text(
            self.registry_path.read_text(encoding="utf-8") + "# concurrent registry edit\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ProcedureRemapError, "preview has blockers"):
            self._stage(preview)

        self.registry_path.write_text(
            self.registry_path.read_text(encoding="utf-8").replace(
                "# concurrent registry edit\n",
                "",
            ),
            encoding="utf-8",
        )
        destination = self.target / NEW_DOCUMENTS["context"]
        destination.unlink()
        destination.symlink_to(self.target / OLD_DOCUMENTS["context"])
        with self.assertRaisesRegex(ProcedureRemapError, "Unsafe mapped file"):
            self._preview()

        traversal = dict(NEW_DOCUMENTS)
        traversal["context"] = "../outside.md"
        with self.assertRaisesRegex(ProcedureRemapError, "safe repository-relative path"):
            self._preview(traversal)

    def test_malformed_journal_fingerprints_block_inspection_and_recovery(self) -> None:
        staged = self._stage()
        transaction = inspect_role_remap(self.entry, self.state_root)
        journal_path = Path(transaction["operation_directory"]) / "journal.json"
        journal = json.loads(journal_path.read_text(encoding="ascii"))
        journal["document_hashes"] = {"../../outside": "0" * 64}
        journal_path.write_text(json.dumps(journal), encoding="ascii")

        inspected = inspect_role_remap(self.entry, self.state_root)

        self.assertEqual(inspected["state"], "CORRUPT")
        self.assertTrue(inspected["blocked"])
        with self.assertRaisesRegex(ProcedureRemapError, "fingerprints are invalid"):
            recover_role_remap(self.entry, staged["operation_id"], self.state_root)

    def test_runtime_binding_rejects_a_stale_registry_image(self) -> None:
        payload = {
            "service": "repo-control",
            "active_repository": self.entry.key,
            "active_repository_generation": 3,
            "central_registry_stale": True,
            "runtime_capabilities": {"instance_id": "a" * 32},
        }
        response = mock.Mock(status=200)
        response.read.return_value = json.dumps(payload).encode("utf-8")
        connection = mock.Mock()
        connection.getresponse.return_value = response
        with mock.patch("repoctl.procedure_remap.http.client.HTTPConnection", return_value=connection):
            with self.assertRaisesRegex(ProcedureRemapError, "Restart the service"):
                read_runtime_binding("http://127.0.0.1:8765")
        connection.close.assert_called_once()

    def test_remap_stage_cli_requires_explicit_confirmation(self) -> None:
        arguments = [
            "coder-procedure-remap-stage",
            "--repository-key",
            "photo-organizer",
            "--context",
            NEW_DOCUMENTS["context"],
            "--architecture",
            NEW_DOCUMENTS["architecture"],
            "--workflow",
            NEW_DOCUMENTS["workflow"],
            "--coding-agent-rules",
            NEW_DOCUMENTS["coding_agent_rules"],
            "--parking-lot",
            NEW_DOCUMENTS["parking_lot"],
            "--plan-sha256",
            "a" * 64,
        ]
        with self.assertRaises(SystemExit):
            build_parser().parse_args(arguments)

        confirmed = build_parser().parse_args([*arguments, "--confirm"])

        self.assertTrue(confirmed.confirm)

        legacy = load_bundle()
        from tests.procedure_fixtures import copy_five_artifact_v130_bundle

        legacy = copy_five_artifact_v130_bundle(
            self.bundle.root,
            self.root / "legacy-procedure",
        )
        with self.assertRaisesRegex(ProcedureRemapError, "six-artifact"):
            preview_role_remap(
                self.entry,
                NEW_DOCUMENTS,
                registry_path=self.registry_path,
                state_root=self.state_root,
                active_key=self.entry.key,
                registry_generation=0,
                runtime_instance_id="a" * 32,
                bundle=legacy,
            )


if __name__ == "__main__":
    unittest.main()
