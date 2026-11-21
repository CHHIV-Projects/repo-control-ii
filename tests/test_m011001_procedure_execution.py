from __future__ import annotations

import json
import multiprocessing
import os
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from repoctl.procedure_governance import (
    inspect_procedure,
    load_bundle,
    preview_overlay_initialization,
    preview_reconciliation,
)
from repoctl.procedure_transactions import (
    TRANSACTION_ROOT_ENV,
    ProcedureTransactionError,
    execute_sealed_request,
    initialize_overlay_confirmed,
    inspect_transaction,
    overlay_initialization_plan_digest,
    procedure_plan_digest,
    review_apply,
    review_overlay_initialization,
    review_recovery,
    seal_execution_request,
)
from repoctl.scanner.util import make_repository_id
from repoctl.web.repository_registry import RepositoryEntry


def _crash_sealed_execution_after_first_target(entry: RepositoryEntry, state_root: str) -> None:
    from repoctl import procedure_transactions

    original = procedure_transactions._write_target
    calls = 0

    def crash_after_write(*args, **kwargs):
        nonlocal calls
        original(*args, **kwargs)
        calls += 1
        if calls == 1:
            os._exit(77)

    with mock.patch("repoctl.procedure_transactions._write_target", side_effect=crash_after_write):
        execute_sealed_request(entry, state_root, active_key=entry.key, registry_generation=3)


class M011001ProcedureExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repository"
        self.repository.mkdir()
        overlay = self.repository / "docs/context/coding_agent_rules.md"
        overlay.parent.mkdir(parents=True)
        overlay.write_text(
            "<!-- repo-control-project-overlay:v1 -->\n\n"
            "## Repository-specific rules\n\n- Fixture rule.\n",
            encoding="utf-8",
        )
        self.entry = RepositoryEntry(
            key="fixture",
            name="Fixture",
            path=self.repository,
            repository_id=make_repository_id(self.repository),
            milestone_root="docs/milestones",
            documents={"coding_agent_rules": "docs/context/coding_agent_rules.md"},
        )
        self.state_root = self.root / "root-managed/state"
        self.state_root.mkdir(parents=True)
        self.transaction_root = self.root / "legacy-private-transactions"
        self.transaction_root.mkdir(mode=0o700)
        self.environment = mock.patch.dict(
            os.environ,
            {TRANSACTION_ROOT_ENV: str(self.transaction_root)},
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.managed_entry = mock.patch(
            "repoctl.workflow.coder_interface.resolve_repository_entry",
            return_value=self.entry,
        )
        self.managed_entry.start()
        self.addCleanup(self.managed_entry.stop)

    def _review_and_seal(self) -> dict:
        bundle = load_bundle()
        plan = preview_reconciliation(self.entry, bundle)
        review = review_apply(
            self.entry,
            self.state_root,
            active_key=self.entry.key,
            registry_generation=3,
            reviewed_plan_sha256=procedure_plan_digest(self.entry, bundle, plan),
            bundle=bundle,
        )
        return seal_execution_request(
            self.entry,
            action="apply",
            token=review["confirmation"]["token"],
            state_root=self.state_root,
            active_key=self.entry.key,
            registry_generation=3,
        )

    def test_explicit_private_store_bypasses_root_managed_parent_and_absence_is_unavailable(self) -> None:
        with mock.patch(
            "repoctl.procedure_transactions._stable_parent",
            side_effect=AssertionError("explicit store must not inspect the state parent"),
        ):
            self.assertIsNone(inspect_transaction(self.entry, self.state_root))

        missing = self.root / "missing-private-store"
        with mock.patch.dict(os.environ, {TRANSACTION_ROOT_ENV: str(missing)}):
            status = inspect_transaction(self.entry, self.state_root)
        self.assertEqual(status["state"], "STORAGE_UNAVAILABLE")
        self.assertNotIn("CORRUPT", status["state"])

        linked = self.root / "linked-private-store"
        linked.symlink_to(self.transaction_root, target_is_directory=True)
        with mock.patch.dict(os.environ, {TRANSACTION_ROOT_ENV: str(linked)}):
            status = inspect_transaction(self.entry, self.state_root)
        self.assertEqual(status["state"], "STORAGE_UNAVAILABLE")
        self.assertIn("symlink", status["error"])

    def test_browser_confirmation_seals_without_worktree_write_then_one_shot_executes(self) -> None:
        request = self._review_and_seal()
        self.assertEqual(request["state"], "CONFIRMED")
        self.assertFalse((self.repository / ".github/copilot-instructions.md").exists())
        pending = inspect_transaction(self.entry, self.state_root)
        self.assertEqual(pending["state"], "EXECUTION_PENDING")
        self.assertEqual(pending["request_id"], request["request_id"])

        result = execute_sealed_request(
            self.entry, self.state_root, active_key=self.entry.key, registry_generation=3
        )
        self.assertEqual(result["state"], "VERIFIED")
        self.assertEqual(inspect_procedure(self.entry, load_bundle())["status"], "CURRENT")
        self.assertTrue((self.repository / "AGENTS.md").is_file())
        self.assertIsNotNone(inspect_transaction(self.entry, self.state_root))
        with self.assertRaisesRegex(ProcedureTransactionError, "Exactly one confirmed"):
            execute_sealed_request(
                self.entry, self.state_root, active_key=self.entry.key, registry_generation=3
            )

    def test_changed_target_and_tampered_request_fail_closed(self) -> None:
        request = self._review_and_seal()
        (self.repository / "AGENTS.md").write_text("external change\n", encoding="utf-8")
        with self.assertRaisesRegex(ProcedureTransactionError, "no longer matches|changed|blockers"):
            execute_sealed_request(
                self.entry, self.state_root, active_key=self.entry.key, registry_generation=3
            )
        self.assertEqual(inspect_procedure(self.entry, load_bundle())["status"], "INCOMPATIBLE")

        # A newly sealed request cannot coexist with the failed audit record,
        # but a direct integrity check must still reject request modification.
        request_path = (
            self.transaction_root / self.entry.repository_id / "requests" /
            f"{request['request_id']}.json"
        )
        record = json.loads(request_path.read_text(encoding="utf-8"))
        record["repository_key"] = "other"
        request_path.write_text(json.dumps(record), encoding="utf-8")
        request_path.chmod(0o600)
        status = inspect_transaction(self.entry, self.state_root)
        self.assertEqual(status["state"], "CORRUPT")

    def test_executor_rejects_stale_live_repository_selection(self) -> None:
        self._review_and_seal()
        with self.assertRaisesRegex(ProcedureTransactionError, "selection changed"):
            execute_sealed_request(
                self.entry,
                self.state_root,
                active_key="other-repository",
                registry_generation=4,
            )
        status = inspect_transaction(self.entry, self.state_root)
        self.assertTrue(status is None or status["state"] != "EXECUTION_PENDING")
        replacement = self._review_and_seal()
        self.assertEqual(replacement["state"], "CONFIRMED")

    def test_interrupted_execution_exposes_and_seals_explicit_recovery(self) -> None:
        self._review_and_seal()
        process = multiprocessing.Process(
            target=_crash_sealed_execution_after_first_target,
            args=(self.entry, str(self.state_root)),
        )
        process.start()
        process.join(15)
        self.assertEqual(process.exitcode, 77)

        interrupted = inspect_transaction(self.entry, self.state_root)
        self.assertEqual(interrupted["state"], "APPLYING")
        recovery = review_recovery(
            self.entry,
            interrupted["operation_id"],
            self.state_root,
            active_key=self.entry.key,
            registry_generation=3,
        )
        sealed = seal_execution_request(
            self.entry,
            action="recover",
            token=recovery["confirmation"]["token"],
            state_root=self.state_root,
            active_key=self.entry.key,
            registry_generation=3,
            operation_id=interrupted["operation_id"],
        )
        self.assertEqual(sealed["action"], "recover")
        self.assertEqual(inspect_transaction(self.entry, self.state_root)["state"], "EXECUTION_PENDING")
        result = execute_sealed_request(
            self.entry, self.state_root, active_key=self.entry.key, registry_generation=3
        )
        self.assertEqual(result["state"], "RECOVERED")
        self.assertFalse((self.repository / "AGENTS.md").exists())
        self.assertFalse((self.repository / ".github/copilot-instructions.md").exists())

    def test_malformed_journal_is_corrupt_not_storage_unavailable(self) -> None:
        store = self.transaction_root / self.entry.repository_id
        operation = store / "transactions" / ("a" * 32)
        operation.mkdir(parents=True, mode=0o700)
        store.chmod(0o700)
        (store / "transactions").chmod(0o700)
        journal = operation / "journal.json"
        journal.write_text("not-json", encoding="utf-8")
        journal.chmod(0o600)
        status = inspect_transaction(self.entry, self.state_root)
        self.assertEqual(status["state"], "CORRUPT")
        self.assertIn("malformed", status["error"])

    def test_terminal_overlay_journal_survives_later_registry_remap(self) -> None:
        original_path = self.repository / "docs/context/coding_agent_rules.md"
        original_path.write_text("# Legacy project rules\n", encoding="utf-8")
        bundle = load_bundle()
        plan = preview_overlay_initialization(self.entry, bundle)
        review = review_overlay_initialization(
            self.entry,
            self.state_root,
            active_key=self.entry.key,
            registry_generation=3,
            reviewed_plan_sha256=overlay_initialization_plan_digest(self.entry, bundle, plan),
            bundle=bundle,
        )
        result = initialize_overlay_confirmed(
            self.entry,
            review["confirmation"]["token"],
            self.state_root,
            active_key=self.entry.key,
            registry_generation=3,
            bundle=bundle,
        )
        self.assertEqual(result["state"], "VERIFIED")

        alternate = self.repository / "docs/context/alternate_rules.md"
        alternate.write_text(
            "<!-- repo-control-project-overlay:v1 -->\n\n"
            "## Repository-specific rules\n\n- Remapped rule.\n",
            encoding="utf-8",
        )
        remapped = replace(
            self.entry,
            documents={"coding_agent_rules": "docs/context/alternate_rules.md"},
        )
        status = inspect_transaction(remapped, self.state_root)
        self.assertEqual(status["state"], "VERIFIED")
        self.assertEqual(status["operation_type"], "overlay_initialize")


if __name__ == "__main__":
    unittest.main()
