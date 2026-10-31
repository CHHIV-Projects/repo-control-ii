from __future__ import annotations

import contextlib
import hashlib
import io
import json
import multiprocessing
import os
import shutil
import subprocess
import tempfile
import tomllib
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from repoctl.cli import main
from repoctl import procedure_transactions
from repoctl.procedure_governance import (
    ProcedureGovernanceError,
    inspect_procedure,
    load_bundle,
    materialize_preview,
    preview_reconciliation,
)
from repoctl.procedure_transactions import (
    FIXED_TARGETS,
    ProcedureTransactionError,
    apply_confirmed,
    inspect_transaction,
    procedure_plan_digest,
    recover_confirmed,
    review_apply,
    review_recovery,
)
from repoctl.scanner.util import make_repository_id
from repoctl.web.repository_registry import RepositoryEntry
from repoctl.workflow.architect_dispatches import get_dispatch, submit_dispatch
from repoctl.scanner.core import run_scan


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
    return path.resolve()


def _crash_apply_after_first_target(entry: RepositoryEntry, state_root: str, token: str) -> None:
    from repoctl import procedure_transactions

    original = procedure_transactions._write_target
    calls = 0

    def write_then_exit(*args, **kwargs):
        nonlocal calls
        original(*args, **kwargs)
        calls += 1
        if calls == 1:
            os._exit(73)

    with mock.patch("repoctl.procedure_transactions._write_target", side_effect=write_then_exit):
        apply_confirmed(
            entry,
            token,
            state_root,
            active_key=entry.key,
            registry_generation=0,
        )


def _crash_apply_after_prepared(entry: RepositoryEntry, state_root: str, token: str) -> None:
    original = procedure_transactions._write_journal

    def write_then_exit(operation_directory, journal):
        original(operation_directory, journal)
        if journal["state"] == "PREPARED":
            os._exit(74)

    with mock.patch("repoctl.procedure_transactions._write_journal", side_effect=write_then_exit):
        apply_confirmed(
            entry,
            token,
            state_root,
            active_key=entry.key,
            registry_generation=0,
        )


def _crash_apply_during_preparation(entry: RepositoryEntry, state_root: str, token: str) -> None:
    original = procedure_transactions._write_image

    def write_then_exit(path, data):
        original(path, data)
        os._exit(76)

    with mock.patch("repoctl.procedure_transactions._write_image", side_effect=write_then_exit):
        apply_confirmed(
            entry,
            token,
            state_root,
            active_key=entry.key,
            registry_generation=0,
        )


def _crash_apply_after_lock_replacement(entry: RepositoryEntry, state_root: str, token: str) -> None:
    original = procedure_transactions._write_target
    calls = 0

    def write_then_exit(*args, **kwargs):
        nonlocal calls
        original(*args, **kwargs)
        calls += 1
        if calls == len(FIXED_TARGETS) + 1:
            os._exit(75)

    with mock.patch("repoctl.procedure_transactions._write_target", side_effect=write_then_exit):
        apply_confirmed(
            entry,
            token,
            state_root,
            active_key=entry.key,
            registry_generation=0,
        )


def _concurrent_apply_worker(
    entry: RepositoryEntry,
    state_root: str,
    token: str,
    barrier,
    results,
) -> None:
    barrier.wait(10)
    try:
        result = apply_confirmed(
            entry,
            token,
            state_root,
            active_key=entry.key,
            registry_generation=0,
        )
        results.put(("applied", result["operation_id"]))
    except ProcedureTransactionError as exc:
        results.put(("refused", str(exc)))


def _concurrent_recovery_worker(
    entry: RepositoryEntry,
    operation_id: str,
    state_root: str,
    token: str,
    barrier,
    results,
) -> None:
    barrier.wait(10)
    try:
        result = recover_confirmed(
            entry,
            operation_id,
            token,
            state_root,
            active_key=entry.key,
            registry_generation=0,
        )
        results.put(("recovered", result["state"]))
    except ProcedureTransactionError as exc:
        results.put(("refused", str(exc)))


def _concurrent_stale_apply_worker(
    entry: RepositoryEntry,
    state_root: str,
    token: str,
    barrier,
    results,
) -> None:
    barrier.wait(10)
    try:
        result = apply_confirmed(
            entry,
            token,
            state_root,
            active_key=entry.key,
            registry_generation=0,
        )
        results.put(("applied", result["state"]))
    except ProcedureTransactionError as exc:
        results.put(("refused", str(exc)))


class ProcedureGovernanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.repo = _git_repo(self.root / "repo")
        (self.repo / "docs/milestones").mkdir(parents=True)
        overlay = self.repo / "docs/context/coding_agent_rules.md"
        overlay.parent.mkdir(parents=True)
        overlay.write_text(
            "# Project overlay\n\n"
            "<!-- repo-control-project-overlay:v1 -->\n\n"
            "## Repository-specific rules\n\n"
            "- Keep fixture data local.\n",
            encoding="utf-8",
        )
        self.entry = RepositoryEntry(
            key="repo-control-ii",
            name="Repo Control II",
            path=self.repo,
            repository_id=make_repository_id(self.repo),
            milestone_root="docs/milestones",
            documents={"coding_agent_rules": "docs/context/coding_agent_rules.md"},
        )
        self.bundle = load_bundle()
        self.registry_path = self.root / "repositories.toml"
        self._write_registry([self.entry])
        self.registry_patcher = mock.patch(
            "repoctl.workflow.coder_interface.CENTRAL_REPOSITORY_REGISTRY_PATH",
            self.registry_path,
        )
        self.registry_patcher.start()

    def tearDown(self) -> None:
        self.registry_patcher.stop()
        self.temp_dir.cleanup()

    def _write_registry(self, entries: list[RepositoryEntry]) -> None:
        lines = [f"default = {json.dumps(entries[0].key)}"]
        for entry in entries:
            lines.extend(
                [
                    "",
                    f"[repositories.{entry.key}]",
                    f"name = {json.dumps(entry.name)}",
                    f"path = {json.dumps(str(entry.path))}",
                    f"milestone_root = {json.dumps(entry.milestone_root)}",
                    "",
                    f"[repositories.{entry.key}.documents]",
                ]
            )
            for role, path in entry.documents.items():
                lines.append(f"{role} = {json.dumps(path)}")
        self.registry_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _materialize(self) -> None:
        plan = preview_reconciliation(self.entry, self.bundle)
        self.assertTrue(plan.can_materialize, plan.blockers)
        materialize_preview(self.entry, plan)

    def _apply_confirmation(self, state_root: Path) -> dict:
        plan = preview_reconciliation(self.entry, self.bundle)
        self.assertTrue(plan.can_materialize, plan.blockers)
        return review_apply(
            self.entry,
            state_root,
            active_key=self.entry.key,
            registry_generation=0,
            reviewed_plan_sha256=procedure_plan_digest(self.entry, self.bundle, plan),
            bundle=self.bundle,
        )

    def test_confirmed_apply_writes_exact_target_set_and_verifies_current(self) -> None:
        state_root = self.root / "state"
        overlay = self.repo / "docs/context/coding_agent_rules.md"
        original_overlay = overlay.read_bytes()
        dispatch = submit_dispatch(
            self.repo,
            state_root,
            self.entry.repository_id,
            dispatch_type="INSTRUCTION",
            milestone_id="M008.004",
            body="The scanner must preserve this fixture dispatch.",
        )
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "Fixture"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "fixture@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "fixture baseline"], check=True)

        confirmation = self._apply_confirmation(state_root)
        self.assertEqual(len(list(self.repo.rglob("*.md"))), 1)
        result = apply_confirmed(
            self.entry,
            confirmation["confirmation"]["token"],
            state_root,
            active_key=self.entry.key,
            registry_generation=0,
            bundle=self.bundle,
        )

        self.assertEqual(result["state"], "VERIFIED")
        self.assertEqual(result["status"], "CURRENT")
        self.assertFalse(result["no_op"])
        self.assertEqual(set(result["target_hashes"]), {*FIXED_TARGETS, self.bundle.lock_path})
        self.assertEqual(overlay.read_bytes(), original_overlay)
        self.assertEqual(inspect_procedure(self.entry, self.bundle)["status"], "CURRENT")
        self.assertEqual(
            get_dispatch(self.repo, state_root, self.entry.repository_id, dispatch["dispatch_id"]),
            dispatch,
        )
        transaction = inspect_transaction(self.entry, state_root)
        self.assertEqual(transaction["state"], "VERIFIED")
        self.assertFalse(transaction["cleanup_pending"])

        run_scan(str(self.repo), state_root=state_root)
        self.assertEqual(
            get_dispatch(self.repo, state_root, self.entry.repository_id, dispatch["dispatch_id"]),
            dispatch,
        )
        self.assertEqual(inspect_transaction(self.entry, state_root)["state"], "VERIFIED")

        with self.assertRaisesRegex(ProcedureTransactionError, "already used"):
            apply_confirmed(
                self.entry,
                confirmation["confirmation"]["token"],
                state_root,
                active_key=self.entry.key,
                registry_generation=0,
                bundle=self.bundle,
            )

    def test_confirmed_apply_is_idempotent_when_already_current(self) -> None:
        state_root = self.root / "state"
        self._materialize()
        confirmation = self._apply_confirmation(state_root)
        result = apply_confirmed(
            self.entry,
            confirmation["confirmation"]["token"],
            state_root,
            active_key=self.entry.key,
            registry_generation=0,
            bundle=self.bundle,
        )
        self.assertEqual(result["state"], "VERIFIED")
        self.assertEqual(result["status"], "CURRENT")
        self.assertTrue(result["no_op"])
        self.assertEqual(result["affected_files"], [])

    def test_apply_adopts_exact_existing_target_and_preserves_its_mode(self) -> None:
        self._materialize()
        adopted = self.repo / FIXED_TARGETS[0]
        for relative in (*FIXED_TARGETS[1:], self.bundle.lock_path):
            (self.repo / relative).unlink()
        os.chmod(adopted, 0o640)
        adopted_bytes = adopted.read_bytes()
        state_root = self.root / "state"
        confirmation = self._apply_confirmation(state_root)

        result = apply_confirmed(
            self.entry,
            confirmation["confirmation"]["token"],
            state_root,
            active_key=self.entry.key,
            registry_generation=0,
            bundle=self.bundle,
        )

        self.assertEqual(result["state"], "VERIFIED")
        self.assertEqual(adopted.read_bytes(), adopted_bytes)
        self.assertEqual(adopted.stat().st_mode & 0o777, 0o640)
        self.assertEqual(inspect_procedure(self.entry, self.bundle)["status"], "CURRENT")

    def test_caught_apply_failure_restores_exact_original_files(self) -> None:
        state_root = self.root / "state"
        confirmation = self._apply_confirmation(state_root)
        original_write = procedure_transactions._write_target
        attempts = 0

        def fail_after_one_replacement(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 2:
                raise OSError("fixture replacement failure")
            return original_write(*args, **kwargs)

        with mock.patch(
            "repoctl.procedure_transactions._write_target",
            side_effect=fail_after_one_replacement,
        ):
            with self.assertRaisesRegex(ProcedureTransactionError, "original files were restored"):
                apply_confirmed(
                    self.entry,
                    confirmation["confirmation"]["token"],
                    state_root,
                    active_key=self.entry.key,
                    registry_generation=0,
                    bundle=self.bundle,
                )
        self.assertEqual(inspect_procedure(self.entry, self.bundle)["status"], "MISSING")
        self.assertEqual(inspect_transaction(self.entry, state_root)["state"], "RECOVERED")
        self.assertFalse((self.repo / ".github/copilot-instructions.md").exists())
        self.assertFalse((self.repo / ".github").exists())

    def test_faults_before_and_after_each_target_replacement_restore_exact_original_state(self) -> None:
        original_write = procedure_transactions._write_target
        paths = (*FIXED_TARGETS, self.bundle.lock_path)
        for when in ("before", "after"):
            for failure_index in range(1, len(paths) + 1):
                with self.subTest(when=when, target=paths[failure_index - 1]):
                    state_root = self.root / f"state-{when}-{failure_index}"
                    confirmation = self._apply_confirmation(state_root)
                    calls = 0

                    def inject_failure(*args, **kwargs):
                        nonlocal calls
                        calls += 1
                        if when == "before" and calls == failure_index:
                            raise OSError(f"fixture failure before target {failure_index}")
                        result = original_write(*args, **kwargs)
                        if when == "after" and calls == failure_index:
                            raise OSError(f"fixture failure after target {failure_index}")
                        return result

                    with mock.patch(
                        "repoctl.procedure_transactions._write_target",
                        side_effect=inject_failure,
                    ):
                        with self.assertRaisesRegex(ProcedureTransactionError, "original files were restored"):
                            apply_confirmed(
                                self.entry,
                                confirmation["confirmation"]["token"],
                                state_root,
                                active_key=self.entry.key,
                                registry_generation=0,
                                bundle=self.bundle,
                            )
                    self.assertEqual(inspect_procedure(self.entry, self.bundle)["status"], "MISSING")
                    self.assertEqual(inspect_transaction(self.entry, state_root)["state"], "RECOVERED")
                    self.assertFalse(any((self.repo / path).exists() for path in paths))
                    self.assertFalse((self.repo / ".github").exists())
                    self.assertFalse((self.repo / ".repo-control").exists())
                    self.assertFalse((self.repo / ".agents").exists())

    def test_release_lock_replace_permission_failure_rolls_back_generated_files(self) -> None:
        state_root = self.root / "state"
        confirmation = self._apply_confirmation(state_root)
        original_replace = os.replace

        def fail_lock_replace(source, destination, *args, **kwargs):
            if isinstance(source, str) and source.endswith("-5.tmp"):
                raise PermissionError("fixture EACCES replacing release lock")
            return original_replace(source, destination, *args, **kwargs)

        with mock.patch("repoctl.procedure_transactions.os.replace", side_effect=fail_lock_replace):
            with self.assertRaisesRegex(ProcedureTransactionError, "original files were restored"):
                apply_confirmed(
                    self.entry,
                    confirmation["confirmation"]["token"],
                    state_root,
                    active_key=self.entry.key,
                    registry_generation=0,
                    bundle=self.bundle,
                )
        self.assertEqual(inspect_procedure(self.entry, self.bundle)["status"], "MISSING")
        self.assertEqual(inspect_transaction(self.entry, state_root)["state"], "RECOVERED")
        self.assertFalse(any((self.repo / path).exists() for path in (*FIXED_TARGETS, self.bundle.lock_path)))

    def test_verified_journal_publish_failure_rolls_back_instead_of_reporting_success(self) -> None:
        state_root = self.root / "state"
        confirmation = self._apply_confirmation(state_root)
        original_write_journal = procedure_transactions._write_journal
        failed = False

        def fail_verified_transition(operation_directory, journal):
            nonlocal failed
            if journal["state"] == "VERIFIED" and not failed:
                failed = True
                raise OSError("fixture EIO publishing VERIFIED")
            return original_write_journal(operation_directory, journal)

        with mock.patch(
            "repoctl.procedure_transactions._write_journal",
            side_effect=fail_verified_transition,
        ):
            with self.assertRaisesRegex(ProcedureTransactionError, "original files were restored"):
                apply_confirmed(
                    self.entry,
                    confirmation["confirmation"]["token"],
                    state_root,
                    active_key=self.entry.key,
                    registry_generation=0,
                    bundle=self.bundle,
                )
        self.assertTrue(failed)
        self.assertEqual(inspect_procedure(self.entry, self.bundle)["status"], "MISSING")
        self.assertEqual(inspect_transaction(self.entry, state_root)["state"], "RECOVERED")
        self.assertFalse(any((self.repo / path).exists() for path in (*FIXED_TARGETS, self.bundle.lock_path)))

    def test_target_parent_symlink_swap_cannot_write_outside_repository(self) -> None:
        state_root = self.root / "state"
        confirmation = self._apply_confirmation(state_root)
        outside = self.root / "outside"
        outside.mkdir()
        sentinel = outside / "keep.txt"
        sentinel.write_text("unrelated fixture data\n", encoding="utf-8")
        original_write = procedure_transactions._write_target
        calls = 0

        def swap_parent_then_write(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                (self.repo / ".repo-control").symlink_to(outside, target_is_directory=True)
            return original_write(*args, **kwargs)

        with mock.patch(
            "repoctl.procedure_transactions._write_target",
            side_effect=swap_parent_then_write,
        ):
            with self.assertRaises(ProcedureTransactionError):
                apply_confirmed(
                    self.entry,
                    confirmation["confirmation"]["token"],
                    state_root,
                    active_key=self.entry.key,
                    registry_generation=0,
                    bundle=self.bundle,
                )
        transaction = inspect_transaction(self.entry, state_root)
        self.assertIn(transaction["state"], {"RECOVERY_REQUIRED", "RECOVERY_BLOCKED"})
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "unrelated fixture data\n")
        self.assertFalse((outside / "workflow_core.md").exists())
        self.assertTrue((self.repo / FIXED_TARGETS[0]).exists())
        self.assertFalse((self.repo / self.bundle.lock_path).exists())

    def test_parent_rename_after_final_check_is_detected_and_remains_blocked(self) -> None:
        state_root = self.root / "state"
        confirmation = self._apply_confirmation(state_root)
        original_replace = os.replace
        moved_parent = self.repo / ".github-moved"
        swapped = False

        def rename_parent_at_replace(source, destination, *args, **kwargs):
            nonlocal swapped
            if (
                not swapped
                and isinstance(source, str)
                and source.endswith("-0.tmp")
                and kwargs.get("src_dir_fd") is not None
            ):
                os.rename(self.repo / ".github", moved_parent)
                swapped = True
            return original_replace(source, destination, *args, **kwargs)

        with mock.patch(
            "repoctl.procedure_transactions.os.replace",
            side_effect=rename_parent_at_replace,
        ):
            with self.assertRaises(ProcedureTransactionError):
                apply_confirmed(
                    self.entry,
                    confirmation["confirmation"]["token"],
                    state_root,
                    active_key=self.entry.key,
                    registry_generation=0,
                    bundle=self.bundle,
                )
        self.assertTrue(swapped)
        self.assertTrue((moved_parent / "copilot-instructions.md").is_file())
        expected_hash = next(
            change["expected_sha256"]
            for change in preview_reconciliation(self.entry, self.bundle).as_dict()["changes"]
            if change["path"] == FIXED_TARGETS[0]
        )
        self.assertEqual(
            hashlib.sha256((moved_parent / "copilot-instructions.md").read_bytes()).hexdigest(),
            expected_hash,
        )
        transaction = inspect_transaction(self.entry, state_root)
        self.assertIn(transaction["state"], {"RECOVERY_REQUIRED", "RECOVERY_BLOCKED"})
        self.assertFalse((self.repo / ".repo-control").exists())
        self.assertFalse((self.repo / ".agents").exists())

    def test_terminal_image_cleanup_failure_is_truthfully_reported(self) -> None:
        state_root = self.root / "state"
        confirmation = self._apply_confirmation(state_root)
        with mock.patch(
            "repoctl.procedure_transactions._cleanup_images",
            side_effect=OSError("fixture EACCES cleaning images"),
        ):
            result = apply_confirmed(
                self.entry,
                confirmation["confirmation"]["token"],
                state_root,
                active_key=self.entry.key,
                registry_generation=0,
                bundle=self.bundle,
            )
        self.assertEqual(result["state"], "VERIFIED")
        self.assertTrue(result["cleanup_pending"])
        self.assertEqual(inspect_procedure(self.entry, self.bundle)["status"], "CURRENT")
        self.assertTrue(inspect_transaction(self.entry, state_root)["cleanup_pending"])

    def test_independent_verification_failure_restores_exact_original_state(self) -> None:
        state_root = self.root / "state"
        confirmation = self._apply_confirmation(state_root)
        with mock.patch(
            "repoctl.procedure_transactions._status_for",
            return_value={"status": "STALE_VERSION", "lock_sha256": None},
        ):
            with self.assertRaisesRegex(
                ProcedureTransactionError,
                "Independent verification did not return CURRENT",
            ):
                apply_confirmed(
                    self.entry,
                    confirmation["confirmation"]["token"],
                    state_root,
                    active_key=self.entry.key,
                    registry_generation=0,
                    bundle=self.bundle,
                )
        self.assertEqual(inspect_procedure(self.entry, self.bundle)["status"], "MISSING")
        self.assertEqual(inspect_transaction(self.entry, state_root)["state"], "RECOVERED")

    def test_journal_and_image_preparation_failures_leave_targets_untouched(self) -> None:
        for failure_point in ("journal", "image"):
            with self.subTest(failure_point=failure_point):
                state_root = self.root / f"state-{failure_point}"
                confirmation = self._apply_confirmation(state_root)
                patch_target = (
                    "repoctl.procedure_transactions._write_journal"
                    if failure_point == "journal"
                    else "repoctl.procedure_transactions._write_image"
                )
                with mock.patch(patch_target, side_effect=OSError(f"fixture {failure_point} failure")):
                    with self.assertRaises(ProcedureTransactionError):
                        apply_confirmed(
                            self.entry,
                            confirmation["confirmation"]["token"],
                            state_root,
                            active_key=self.entry.key,
                            registry_generation=0,
                            bundle=self.bundle,
                        )
                self.assertEqual(inspect_procedure(self.entry, self.bundle)["status"], "MISSING")
                self.assertIsNone(inspect_transaction(self.entry, state_root))
                self.assertFalse((self.repo / ".github/copilot-instructions.md").exists())
                self.assertFalse((self.repo / ".agents").exists())

    def test_journal_image_and_parent_sync_failures_are_fail_closed_before_writes(self) -> None:
        for failure_point in ("journal_write", "image_write", "journal_fsync", "transaction_parent_fsync"):
            with self.subTest(failure_point=failure_point):
                state_root = self.root / f"state-{failure_point}"
                original_fsync = procedure_transactions._fsync_directory
                failed_once = False

                def injected_fsync(path):
                    nonlocal failed_once
                    if not failed_once and (
                        (failure_point == "journal_fsync" and path.name == "transactions" and path.parent.name == self.entry.repository_id)
                        or (failure_point == "transaction_parent_fsync" and path.name == self.entry.repository_id)
                    ):
                        failed_once = True
                        raise OSError("fixture ENOSPC directory sync")
                    return original_fsync(path)

                patch_target = {
                    "journal_write": "repoctl.procedure_transactions._write_journal",
                    "image_write": "repoctl.procedure_transactions._write_image",
                }.get(failure_point)
                confirmation = None
                if failure_point != "transaction_parent_fsync":
                    confirmation = self._apply_confirmation(state_root)
                with contextlib.ExitStack() as patches:
                    if patch_target is not None:
                        patches.enter_context(mock.patch(patch_target, side_effect=OSError("fixture ENOSPC write")))
                    else:
                        patches.enter_context(
                            mock.patch("repoctl.procedure_transactions._fsync_directory", side_effect=injected_fsync)
                        )
                    with self.assertRaises(ProcedureTransactionError):
                        if failure_point == "transaction_parent_fsync":
                            self._apply_confirmation(state_root)
                        else:
                            apply_confirmed(
                                self.entry,
                                confirmation["confirmation"]["token"],
                                state_root,
                                active_key=self.entry.key,
                                registry_generation=0,
                                bundle=self.bundle,
                            )
                self.assertEqual(inspect_procedure(self.entry, self.bundle)["status"], "MISSING")
                transaction = inspect_transaction(self.entry, state_root)
                self.assertTrue(transaction is None or transaction["blocked"])
                self.assertFalse(any((self.repo / path).exists() for path in (*FIXED_TARGETS, self.bundle.lock_path)))
                if failure_point in {"journal_fsync", "transaction_parent_fsync"}:
                    self.assertTrue(failed_once)

    def test_image_partial_write_fsync_and_journal_publish_failures_leave_no_targets(self) -> None:
        original_write = os.write
        original_fsync = os.fsync
        original_replace = os.replace
        for failure_point in ("image_partial_write", "image_fsync", "journal_publish"):
            with self.subTest(failure_point=failure_point):
                state_root = self.root / f"state-{failure_point}"
                confirmation = self._apply_confirmation(state_root)
                written_fds: set[int] = set()

                def descriptor_path(descriptor):
                    return Path(os.readlink(f"/proc/self/fd/{descriptor}"))

                def fail_partial_image_write(descriptor, data):
                    path = descriptor_path(descriptor)
                    if path.name.startswith(("before-", "after-")):
                        if descriptor in written_fds:
                            raise OSError("fixture ENOSPC during recovery-image write")
                        written_fds.add(descriptor)
                        return original_write(descriptor, data[:8])
                    return original_write(descriptor, data)

                def fail_image_fsync(descriptor):
                    path = descriptor_path(descriptor)
                    if path.name.startswith(("before-", "after-")):
                        raise OSError("fixture EIO fsyncing recovery image")
                    return original_fsync(descriptor)

                def fail_journal_publish(source, destination, *args, **kwargs):
                    if isinstance(source, Path) and source.name.startswith(".journal.json."):
                        raise OSError("fixture EIO publishing journal")
                    return original_replace(source, destination, *args, **kwargs)

                with contextlib.ExitStack() as patches:
                    if failure_point == "image_partial_write":
                        patches.enter_context(
                            mock.patch("repoctl.procedure_transactions.os.write", side_effect=fail_partial_image_write)
                        )
                    elif failure_point == "image_fsync":
                        patches.enter_context(
                            mock.patch("repoctl.procedure_transactions.os.fsync", side_effect=fail_image_fsync)
                        )
                    else:
                        patches.enter_context(
                            mock.patch("repoctl.procedure_transactions.os.replace", side_effect=fail_journal_publish)
                        )
                    with self.assertRaises(ProcedureTransactionError):
                        apply_confirmed(
                            self.entry,
                            confirmation["confirmation"]["token"],
                            state_root,
                            active_key=self.entry.key,
                            registry_generation=0,
                            bundle=self.bundle,
                        )
                self.assertEqual(inspect_procedure(self.entry, self.bundle)["status"], "MISSING")
                self.assertIsNone(inspect_transaction(self.entry, state_root))
                self.assertFalse(any((self.repo / path).exists() for path in (*FIXED_TARGETS, self.bundle.lock_path)))

    def test_target_temp_fsync_failure_rolls_back_and_removes_staged_temp(self) -> None:
        state_root = self.root / "state"
        confirmation = self._apply_confirmation(state_root)
        original_fsync = os.fsync

        def fail_target_temp_fsync(descriptor):
            path = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
            if path.name.startswith(".repoctl-"):
                raise OSError("fixture ENOSPC fsyncing generated target temp")
            return original_fsync(descriptor)

        with mock.patch(
            "repoctl.procedure_transactions.os.fsync",
            side_effect=fail_target_temp_fsync,
        ):
            with self.assertRaisesRegex(ProcedureTransactionError, "original files were restored"):
                apply_confirmed(
                    self.entry,
                    confirmation["confirmation"]["token"],
                    state_root,
                    active_key=self.entry.key,
                    registry_generation=0,
                    bundle=self.bundle,
                )
        self.assertEqual(inspect_procedure(self.entry, self.bundle)["status"], "MISSING")
        self.assertEqual(inspect_transaction(self.entry, state_root)["state"], "RECOVERED")
        self.assertFalse(any((self.repo / path).exists() for path in (*FIXED_TARGETS, self.bundle.lock_path)))
        self.assertFalse(list(self.repo.rglob(".repoctl-*.tmp")))

    def test_target_temp_creation_failures_restore_generated_files_and_lock(self) -> None:
        original_open = os.open
        for failure_index in (1, len(FIXED_TARGETS) + 1):
            with self.subTest(target_index=failure_index):
                state_root = self.root / f"state-temp-create-{failure_index}"
                confirmation = self._apply_confirmation(state_root)
                target_creates = 0

                def fail_target_temp_create(path, flags, mode=0o777, *, dir_fd=None):
                    nonlocal target_creates
                    if dir_fd is not None and flags & os.O_CREAT:
                        target_creates += 1
                        if target_creates == failure_index:
                            raise OSError("fixture ENOSPC creating generated target temp")
                    return original_open(path, flags, mode, dir_fd=dir_fd)

                with mock.patch(
                    "repoctl.procedure_transactions.os.open",
                    side_effect=fail_target_temp_create,
                ):
                    with self.assertRaisesRegex(
                        ProcedureTransactionError,
                        "original files were restored",
                    ):
                        apply_confirmed(
                            self.entry,
                            confirmation["confirmation"]["token"],
                            state_root,
                            active_key=self.entry.key,
                            registry_generation=0,
                            bundle=self.bundle,
                        )
                self.assertEqual(target_creates, failure_index)
                self.assertEqual(inspect_procedure(self.entry, self.bundle)["status"], "MISSING")
                self.assertEqual(inspect_transaction(self.entry, state_root)["state"], "RECOVERED")
                self.assertFalse(any((self.repo / path).exists() for path in (*FIXED_TARGETS, self.bundle.lock_path)))
                self.assertFalse(list(self.repo.rglob(".repoctl-*.tmp")))

    def test_interrupted_apply_is_detected_and_product_owner_can_restore_original(self) -> None:
        state_root = self.root / "state"
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "Fixture"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "fixture@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "fixture baseline"], check=True)
        confirmation = self._apply_confirmation(state_root)
        process = multiprocessing.get_context("fork").Process(
            target=_crash_apply_after_first_target,
            args=(self.entry, str(state_root), confirmation["confirmation"]["token"]),
        )
        process.start()
        process.join(15)
        self.assertFalse(process.is_alive())
        self.assertEqual(process.exitcode, 73)

        run_scan(str(self.repo), state_root=state_root)
        pending = inspect_transaction(self.entry, state_root)
        self.assertEqual(pending["state"], "APPLYING")
        self.assertTrue(pending["recoverable"])
        recovery_review = review_recovery(
            self.entry,
            pending["operation_id"],
            state_root,
            active_key=self.entry.key,
            registry_generation=0,
        )
        result = recover_confirmed(
            self.entry,
            pending["operation_id"],
            recovery_review["confirmation"]["token"],
            state_root,
            active_key=self.entry.key,
            registry_generation=0,
        )
        self.assertEqual(result["state"], "RECOVERED")
        self.assertEqual(inspect_procedure(self.entry, self.bundle)["status"], "MISSING")
        self.assertFalse((self.repo / ".github/copilot-instructions.md").exists())
        self.assertFalse((self.repo / ".github").exists())

    def test_two_processes_cannot_consume_the_same_apply_confirmation(self) -> None:
        state_root = self.root / "state"
        confirmation = self._apply_confirmation(state_root)
        context = multiprocessing.get_context("fork")
        barrier = context.Barrier(2)
        results = context.Queue()
        processes = [
            context.Process(
                target=_concurrent_apply_worker,
                args=(self.entry, str(state_root), confirmation["confirmation"]["token"], barrier, results),
            )
            for _ in range(2)
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(30)
            self.assertFalse(process.is_alive())
            self.assertEqual(process.exitcode, 0)
        outcomes = [results.get(timeout=5)[0] for _ in processes]
        self.assertCountEqual(outcomes, ["applied", "refused"])
        self.assertEqual(inspect_procedure(self.entry, self.bundle)["status"], "CURRENT")
        self.assertEqual(inspect_transaction(self.entry, state_root)["state"], "VERIFIED")

    def test_competing_recoveries_and_stale_apply_cannot_replay_into_newer_state(self) -> None:
        state_root = self.root / "state"
        first_confirmation = self._apply_confirmation(state_root)
        stale_apply_confirmation = self._apply_confirmation(state_root)
        process = multiprocessing.get_context("fork").Process(
            target=_crash_apply_after_first_target,
            args=(self.entry, str(state_root), first_confirmation["confirmation"]["token"]),
        )
        process.start()
        process.join(15)
        self.assertEqual(process.exitcode, 73)

        pending = inspect_transaction(self.entry, state_root)
        recovery = review_recovery(
            self.entry,
            pending["operation_id"],
            state_root,
            active_key=self.entry.key,
            registry_generation=0,
        )
        context = multiprocessing.get_context("fork")
        barrier = context.Barrier(3)
        results = context.Queue()
        processes = [
            context.Process(
                target=_concurrent_recovery_worker,
                args=(
                    self.entry,
                    pending["operation_id"],
                    str(state_root),
                    recovery["confirmation"]["token"],
                    barrier,
                    results,
                ),
            ),
            context.Process(
                target=_concurrent_recovery_worker,
                args=(
                    self.entry,
                    pending["operation_id"],
                    str(state_root),
                    recovery["confirmation"]["token"],
                    barrier,
                    results,
                ),
            ),
            context.Process(
                target=_concurrent_stale_apply_worker,
                args=(
                    self.entry,
                    str(state_root),
                    stale_apply_confirmation["confirmation"]["token"],
                    barrier,
                    results,
                ),
            ),
        ]
        for child in processes:
            child.start()
        for child in processes:
            child.join(30)
            self.assertFalse(child.is_alive())
            self.assertEqual(child.exitcode, 0)
        outcomes = [results.get(timeout=5) for _ in processes]
        self.assertEqual(sum(1 for status, _ in outcomes if status == "recovered"), 1)
        self.assertEqual(sum(1 for status, _ in outcomes if status == "refused"), 2)
        self.assertEqual(inspect_procedure(self.entry, self.bundle)["status"], "MISSING")
        self.assertEqual(inspect_transaction(self.entry, state_root)["state"], "RECOVERED")
        self.assertFalse(any((self.repo / path).exists() for path in (*FIXED_TARGETS, self.bundle.lock_path)))

        newer_confirmation = self._apply_confirmation(state_root)
        newer_result = apply_confirmed(
            self.entry,
            newer_confirmation["confirmation"]["token"],
            state_root,
            active_key=self.entry.key,
            registry_generation=0,
            bundle=self.bundle,
        )
        self.assertEqual(newer_result["state"], "VERIFIED")
        newer_images = {
            path: (self.repo / path).read_bytes()
            for path in (*FIXED_TARGETS, self.bundle.lock_path)
        }
        with self.assertRaisesRegex(ProcedureTransactionError, "no longer the current recoverable transaction"):
            recover_confirmed(
                self.entry,
                pending["operation_id"],
                recovery["confirmation"]["token"],
                state_root,
                active_key=self.entry.key,
                registry_generation=0,
            )
        self.assertEqual(
            newer_images,
            {path: (self.repo / path).read_bytes() for path in newer_images},
        )
        self.assertEqual(inspect_procedure(self.entry, self.bundle)["status"], "CURRENT")

    def test_recovery_failure_is_journaled_and_can_be_retried_with_fresh_confirmation(self) -> None:
        state_root = self.root / "state"
        confirmation = self._apply_confirmation(state_root)
        process = multiprocessing.get_context("fork").Process(
            target=_crash_apply_after_first_target,
            args=(self.entry, str(state_root), confirmation["confirmation"]["token"]),
        )
        process.start()
        process.join(15)
        self.assertEqual(process.exitcode, 73)
        pending = inspect_transaction(self.entry, state_root)
        recovery = review_recovery(
            self.entry,
            pending["operation_id"],
            state_root,
            active_key=self.entry.key,
            registry_generation=0,
        )
        first_target = self.repo / FIXED_TARGETS[0]
        after_hash = hashlib.sha256(first_target.read_bytes()).hexdigest()
        original_unlink = os.unlink

        def fail_target_unlink(path, *args, **kwargs):
            if kwargs.get("dir_fd") is not None and path == first_target.name:
                raise PermissionError("fixture EACCES during recovery")
            return original_unlink(path, *args, **kwargs)

        with mock.patch("repoctl.procedure_transactions.os.unlink", side_effect=fail_target_unlink):
            with self.assertRaisesRegex(ProcedureTransactionError, "remains blocked"):
                recover_confirmed(
                    self.entry,
                    pending["operation_id"],
                    recovery["confirmation"]["token"],
                    state_root,
                    active_key=self.entry.key,
                    registry_generation=0,
                )
        blocked = inspect_transaction(self.entry, state_root)
        self.assertEqual(blocked["state"], "RECOVERY_REQUIRED")
        self.assertEqual(hashlib.sha256(first_target.read_bytes()).hexdigest(), after_hash)
        retry = review_recovery(
            self.entry,
            pending["operation_id"],
            state_root,
            active_key=self.entry.key,
            registry_generation=0,
        )
        result = recover_confirmed(
            self.entry,
            pending["operation_id"],
            retry["confirmation"]["token"],
            state_root,
            active_key=self.entry.key,
            registry_generation=0,
        )
        self.assertEqual(result["state"], "RECOVERED")
        self.assertEqual(inspect_procedure(self.entry, self.bundle)["status"], "MISSING")

    def test_prepared_interruption_is_recoverable_before_any_target_write(self) -> None:
        state_root = self.root / "state"
        confirmation = self._apply_confirmation(state_root)
        process = multiprocessing.get_context("fork").Process(
            target=_crash_apply_after_prepared,
            args=(self.entry, str(state_root), confirmation["confirmation"]["token"]),
        )
        process.start()
        process.join(15)
        self.assertEqual(process.exitcode, 74)

        pending = inspect_transaction(self.entry, state_root)
        self.assertEqual(pending["state"], "PREPARED")
        self.assertFalse((self.repo / ".github/copilot-instructions.md").exists())
        plan = preview_reconciliation(self.entry, self.bundle)
        with self.assertRaisesRegex(ProcedureTransactionError, "unresolved procedure transaction"):
            review_apply(
                self.entry,
                state_root,
                active_key=self.entry.key,
                registry_generation=0,
                reviewed_plan_sha256=procedure_plan_digest(self.entry, self.bundle, plan),
                bundle=self.bundle,
            )
        recovery_review = review_recovery(
            self.entry,
            pending["operation_id"],
            state_root,
            active_key=self.entry.key,
            registry_generation=0,
        )
        result = recover_confirmed(
            self.entry,
            pending["operation_id"],
            recovery_review["confirmation"]["token"],
            state_root,
            active_key=self.entry.key,
            registry_generation=0,
        )
        self.assertEqual(result["state"], "RECOVERED")
        self.assertEqual(inspect_procedure(self.entry, self.bundle)["status"], "MISSING")

    def test_preparation_interruption_is_journaled_and_does_not_need_partial_images(self) -> None:
        state_root = self.root / "state"
        confirmation = self._apply_confirmation(state_root)
        process = multiprocessing.get_context("fork").Process(
            target=_crash_apply_during_preparation,
            args=(self.entry, str(state_root), confirmation["confirmation"]["token"]),
        )
        process.start()
        process.join(15)
        self.assertEqual(process.exitcode, 76)

        pending = inspect_transaction(self.entry, state_root)
        self.assertEqual(pending["state"], "PREPARING")
        self.assertTrue(pending["recoverable"])
        self.assertFalse((self.repo / ".github/copilot-instructions.md").exists())
        recovery_review = review_recovery(
            self.entry,
            pending["operation_id"],
            state_root,
            active_key=self.entry.key,
            registry_generation=0,
        )
        result = recover_confirmed(
            self.entry,
            pending["operation_id"],
            recovery_review["confirmation"]["token"],
            state_root,
            active_key=self.entry.key,
            registry_generation=0,
        )
        self.assertEqual(result["state"], "RECOVERED")
        self.assertEqual(inspect_procedure(self.entry, self.bundle)["status"], "MISSING")
        self.assertFalse(inspect_transaction(self.entry, state_root)["cleanup_pending"])

    def test_lock_replacement_interruption_is_recoverable_before_verification(self) -> None:
        state_root = self.root / "state"
        confirmation = self._apply_confirmation(state_root)
        process = multiprocessing.get_context("fork").Process(
            target=_crash_apply_after_lock_replacement,
            args=(self.entry, str(state_root), confirmation["confirmation"]["token"]),
        )
        process.start()
        process.join(15)
        self.assertEqual(process.exitcode, 75)

        lock = self.repo / ".repo-control/shared-procedure.lock.toml"
        self.assertTrue(lock.is_file())
        pending = inspect_transaction(self.entry, state_root)
        self.assertEqual(pending["state"], "APPLYING")
        recovery_review = review_recovery(
            self.entry,
            pending["operation_id"],
            state_root,
            active_key=self.entry.key,
            registry_generation=0,
        )
        result = recover_confirmed(
            self.entry,
            pending["operation_id"],
            recovery_review["confirmation"]["token"],
            state_root,
            active_key=self.entry.key,
            registry_generation=0,
        )
        self.assertEqual(result["state"], "RECOVERED")
        self.assertEqual(inspect_procedure(self.entry, self.bundle)["status"], "MISSING")
        self.assertFalse((self.repo / ".repo-control").exists())

    def test_recovery_refuses_target_changed_to_unknown_image(self) -> None:
        state_root = self.root / "state"
        confirmation = self._apply_confirmation(state_root)
        process = multiprocessing.get_context("fork").Process(
            target=_crash_apply_after_first_target,
            args=(self.entry, str(state_root), confirmation["confirmation"]["token"]),
        )
        process.start()
        process.join(15)
        self.assertEqual(process.exitcode, 73)

        target = self.repo / ".github/copilot-instructions.md"
        target.write_text("independent external edit\n", encoding="utf-8")
        before_conflict = target.read_bytes()
        pending = inspect_transaction(self.entry, state_root)
        self.assertEqual(pending["state"], "RECOVERY_BLOCKED")
        self.assertTrue(pending["blocked"])
        with self.assertRaisesRegex(ProcedureTransactionError, "matches neither known transaction image"):
            review_recovery(
                self.entry,
                pending["operation_id"],
                state_root,
                active_key=self.entry.key,
                registry_generation=0,
            )
        self.assertEqual(target.read_bytes(), before_conflict)

    def test_recovery_blocks_when_a_transaction_image_is_corrupted(self) -> None:
        state_root = self.root / "state"
        confirmation = self._apply_confirmation(state_root)
        process = multiprocessing.get_context("fork").Process(
            target=_crash_apply_after_first_target,
            args=(self.entry, str(state_root), confirmation["confirmation"]["token"]),
        )
        process.start()
        process.join(15)
        self.assertEqual(process.exitcode, 73)

        operation_directory = next(
            (self.root / ".state-procedure-transactions" / self.entry.repository_id / "transactions").iterdir()
        )
        image = operation_directory / "after-0.bin"
        image.write_bytes(image.read_bytes() + b"tampered")
        target = self.repo / FIXED_TARGETS[0]
        target_before = target.read_bytes()

        pending = inspect_transaction(self.entry, state_root)
        self.assertEqual(pending["state"], "RECOVERY_BLOCKED")
        with self.assertRaisesRegex(ProcedureTransactionError, "integrity check failed"):
            review_recovery(
                self.entry,
                pending["operation_id"],
                state_root,
                active_key=self.entry.key,
                registry_generation=0,
            )
        self.assertEqual(target.read_bytes(), target_before)

    def test_recovery_blocks_when_a_transaction_image_is_missing(self) -> None:
        state_root = self.root / "state"
        confirmation = self._apply_confirmation(state_root)
        process = multiprocessing.get_context("fork").Process(
            target=_crash_apply_after_first_target,
            args=(self.entry, str(state_root), confirmation["confirmation"]["token"]),
        )
        process.start()
        process.join(15)
        self.assertEqual(process.exitcode, 73)

        operation_directory = next(
            (self.root / ".state-procedure-transactions" / self.entry.repository_id / "transactions").iterdir()
        )
        (operation_directory / "after-0.bin").unlink()
        target = self.repo / FIXED_TARGETS[0]
        target_before = target.read_bytes()

        pending = inspect_transaction(self.entry, state_root)
        self.assertEqual(pending["state"], "RECOVERY_BLOCKED")
        with self.assertRaisesRegex(ProcedureTransactionError, "Required transaction file is missing"):
            review_recovery(
                self.entry,
                pending["operation_id"],
                state_root,
                active_key=self.entry.key,
                registry_generation=0,
            )
        plan = preview_reconciliation(self.entry, self.bundle)
        with self.assertRaisesRegex(ProcedureTransactionError, "unresolved procedure transaction"):
            review_apply(
                self.entry,
                state_root,
                active_key=self.entry.key,
                registry_generation=0,
                reviewed_plan_sha256=procedure_plan_digest(self.entry, self.bundle, plan),
                bundle=self.bundle,
            )
        self.assertEqual(target.read_bytes(), target_before)

    def test_manifest_sources_are_hashed_and_installed_skills_match_their_lock(self) -> None:
        self.assertEqual(self.bundle.schema_version, 1)
        self.assertEqual(self.bundle.release, "1.2.0")
        for source, expected in self.bundle.source_hashes.items():
            data = (self.bundle.root / source).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), expected)
        repository_root = Path(__file__).parents[1]
        installed_lock = tomllib.loads(
            (repository_root / self.bundle.lock_path).read_text(encoding="utf-8")
        )
        installed_hashes = {item["path"]: item["sha256"] for item in installed_lock["files"]}
        for skill in (
            "repo-control-implementation",
            "repo-control-blocker-bug-escalation",
            "repo-control-validation-closeout",
        ):
            canonical = self.bundle.root / "skills" / skill / "SKILL.md"
            target = f".agents/skills/{skill}/SKILL.md"
            accepted = repository_root / target
            installed_bytes = accepted.read_bytes()
            self.assertEqual(hashlib.sha256(installed_bytes).hexdigest(), installed_hashes[target])
            if installed_lock["release"] == self.bundle.release:
                self.assertEqual(canonical.read_bytes(), installed_bytes)

    def test_manifest_missing_malformed_and_source_hash_mismatch_fail_clearly(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            copied = Path(td) / "bundle"
            shutil.copytree(self.bundle.root, copied)
            (copied / "manifest.toml").unlink()
            with self.assertRaisesRegex(ProcedureGovernanceError, "manifest is missing"):
                load_bundle(copied)

        with tempfile.TemporaryDirectory() as td:
            copied = Path(td) / "bundle"
            shutil.copytree(self.bundle.root, copied)
            (copied / "manifest.toml").write_text("not valid TOML =", encoding="utf-8")
            with self.assertRaisesRegex(ProcedureGovernanceError, "Cannot read"):
                load_bundle(copied)

        with tempfile.TemporaryDirectory() as td:
            copied = Path(td) / "bundle"
            shutil.copytree(self.bundle.root, copied)
            with (copied / "bootstrap.md").open("a", encoding="utf-8") as stream:
                stream.write("\nchanged\n")
            with self.assertRaisesRegex(ProcedureGovernanceError, "hash mismatch"):
                load_bundle(copied)

        with tempfile.TemporaryDirectory() as td:
            copied = Path(td) / "bundle"
            shutil.copytree(self.bundle.root, copied)
            manifest = copied / "manifest.toml"
            manifest.write_text(
                manifest.read_text(encoding="utf-8").replace(
                    'target = ".github/copilot-instructions.md"',
                    'target = "../outside.md"',
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ProcedureGovernanceError, "repository-relative"):
                load_bundle(copied)

    def test_rendering_is_deterministic_and_project_agnostic(self) -> None:
        first = preview_reconciliation(self.entry, self.bundle)
        second = preview_reconciliation(self.entry, self.bundle)
        self.assertEqual(first.files, second.files)
        self.assertEqual(first.lock_bytes, second.lock_bytes)
        rendered = dict(first.files)[".github/copilot-instructions.md"].decode()
        self.assertIn("`docs/context/coding_agent_rules.md`", rendered)
        self.assertIn(f"`{self.bundle.release}`", rendered)
        self.assertNotIn("/home/chuck/projects/repo-control-ii`", rendered)
        self.assertNotIn("Vocab", rendered)

    def test_initial_preview_is_exact_read_only_and_materializes_only_generated_files(self) -> None:
        overlay_path = self.repo / "docs/context/coding_agent_rules.md"
        before_overlay = overlay_path.read_bytes()
        plan = preview_reconciliation(self.entry, self.bundle)
        self.assertTrue(plan.can_materialize, plan.blockers)
        data = plan.as_dict()
        self.assertTrue(data["overlay_untouched"])
        self.assertEqual(data["removals"], [])
        self.assertEqual(sum(change["action"] == "create" for change in data["changes"]), 6)
        bootstrap = next(item for item in data["changes"] if item["path"] == ".github/copilot-instructions.md")
        self.assertIn("+This file is generated", bootstrap["diff"])
        self.assertFalse((self.repo / ".github/copilot-instructions.md").exists())
        self.assertEqual(overlay_path.read_bytes(), before_overlay)

        result = materialize_preview(self.entry, plan)
        self.assertEqual(result["status"], "CURRENT")
        self.assertEqual(
            result["lock_sha256"],
            hashlib.sha256((self.repo / self.bundle.lock_path).read_bytes()).hexdigest(),
        )
        self.assertEqual(overlay_path.read_bytes(), before_overlay)
        self.assertEqual(inspect_procedure(self.entry, self.bundle)["status"], "CURRENT")

    def test_exact_existing_skill_copies_are_adopted_without_rewriting(self) -> None:
        skill = "repo-control-implementation"
        target = self.repo / ".agents/skills" / skill / "SKILL.md"
        target.parent.mkdir(parents=True)
        canonical = self.bundle.root / "skills" / skill / "SKILL.md"
        target.write_bytes(canonical.read_bytes())
        original = target.read_bytes()

        plan = preview_reconciliation(self.entry, self.bundle)
        skill_change = next(item for item in plan.changes if item["path"] == f".agents/skills/{skill}/SKILL.md")
        self.assertEqual(skill_change["action"], "adopt")
        self.assertTrue(plan.can_materialize, plan.blockers)
        result = materialize_preview(self.entry, plan)
        self.assertEqual(result["status"], "CURRENT")
        self.assertEqual(target.read_bytes(), original)

    def test_missing_skill_and_edited_generated_file_are_detected_and_not_overwritten(self) -> None:
        self._materialize()
        skill = self.repo / ".agents/skills/repo-control-implementation/SKILL.md"
        skill.unlink()
        self.assertEqual(inspect_procedure(self.entry, self.bundle)["status"], "MISSING")
        plan = preview_reconciliation(self.entry, self.bundle)
        self.assertTrue(plan.can_materialize)
        materialize_preview(self.entry, plan)
        self.assertEqual(inspect_procedure(self.entry, self.bundle)["status"], "CURRENT")

        skill.write_text("unmanaged edit\n", encoding="utf-8")
        status = inspect_procedure(self.entry, self.bundle)
        self.assertEqual(status["status"], "HASH_MISMATCH")
        plan = preview_reconciliation(self.entry, self.bundle)
        self.assertFalse(plan.can_materialize)
        self.assertTrue(any("unmanaged edit" in item for item in plan.blockers))
        with self.assertRaisesRegex(ProcedureGovernanceError, "blocked"):
            materialize_preview(self.entry, plan)
        self.assertEqual(skill.read_text(encoding="utf-8"), "unmanaged edit\n")

    def test_stale_release_can_be_previewed_and_updated_but_stale_plan_is_refused(self) -> None:
        self._materialize()
        original_bundle = self.bundle
        major, minor, patch = (int(part) for part in self.bundle.release.split("."))
        next_bundle = replace(self.bundle, release=f"{major}.{minor}.{patch + 1}")
        status = inspect_procedure(self.entry, next_bundle)
        self.assertEqual(status["status"], "STALE_VERSION")
        plan = preview_reconciliation(self.entry, next_bundle)
        self.assertTrue(plan.can_materialize, plan.blockers)
        self.assertTrue(any(item["path"] == ".github/copilot-instructions.md" and item["action"] == "update" for item in plan.changes))
        (self.repo / "docs/context/coding_agent_rules.md").write_text(
            "# Changed overlay\n\n<!-- repo-control-project-overlay:v1 -->\n\n"
            "## Repository-specific rules\n\n- Updated constraint.\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ProcedureGovernanceError, "stale"):
            materialize_preview(self.entry, plan, next_bundle)

        fresh_plan = preview_reconciliation(self.entry, next_bundle)
        materialize_preview(self.entry, fresh_plan, next_bundle)
        self.assertEqual(inspect_procedure(self.entry, next_bundle)["status"], "CURRENT")
        self.assertEqual(original_bundle.release, self.bundle.release)

    def test_corrupt_lock_and_unversioned_bundle_changes_are_not_reconciled_silently(self) -> None:
        self._materialize()
        lock = self.repo / ".repo-control/shared-procedure.lock.toml"
        valid_lock = lock.read_bytes()
        lock.write_text("bad TOML =", encoding="utf-8")
        self.assertEqual(inspect_procedure(self.entry, self.bundle)["status"], "INCOMPATIBLE")
        plan = preview_reconciliation(self.entry, self.bundle)
        self.assertFalse(plan.can_materialize)

        lock.write_bytes(valid_lock)
        lock.write_text(
            lock.read_text(encoding="utf-8").replace(
                'overlay_path = "docs/context/coding_agent_rules.md"',
                'overlay_path = "docs/context/other_rules.md"',
            ),
            encoding="utf-8",
        )
        self.assertEqual(inspect_procedure(self.entry, self.bundle)["status"], "INCOMPATIBLE")
        self.assertFalse(preview_reconciliation(self.entry, self.bundle).can_materialize)

        lock.write_bytes(valid_lock)
        changed_bundle = replace(self.bundle, manifest_sha256="0" * 64)
        self.assertEqual(inspect_procedure(self.entry, changed_bundle)["status"], "HASH_MISMATCH")
        self.assertFalse(preview_reconciliation(self.entry, changed_bundle).can_materialize)

    def test_overlay_and_target_collisions_block_writes(self) -> None:
        overlay = self.repo / "docs/context/coding_agent_rules.md"
        overlay.write_text("# Unmarked overlay\n", encoding="utf-8")
        self.assertEqual(inspect_procedure(self.entry, self.bundle)["status"], "OVERLAY_INVALID")
        plan = preview_reconciliation(self.entry, self.bundle)
        self.assertFalse(plan.can_materialize)
        self.assertTrue(plan.as_dict()["overlay_untouched"])
        self.assertFalse((self.repo / ".github").exists())

        overlay.write_text(
            "# Project overlay\n\n<!-- repo-control-project-overlay:v1 -->\n\n"
            "## Repository-specific rules\n",
            encoding="utf-8",
        )
        target = self.repo / ".github/copilot-instructions.md"
        target.parent.mkdir()
        target.write_text("hand-written instructions\n", encoding="utf-8")
        self.assertEqual(inspect_procedure(self.entry, self.bundle)["status"], "INCOMPATIBLE")
        plan = preview_reconciliation(self.entry, self.bundle)
        self.assertFalse(plan.can_materialize)
        self.assertTrue(any(change["action"] == "blocked" for change in plan.changes))
        self.assertEqual(target.read_text(encoding="utf-8"), "hand-written instructions\n")
        target.unlink()
        target.mkdir()
        self.assertEqual(inspect_procedure(self.entry, self.bundle)["status"], "INCOMPATIBLE")
        self.assertFalse(preview_reconciliation(self.entry, self.bundle).can_materialize)
        target.rmdir()
        target.parent.rmdir()
        target.parent.write_text("parent collision\n", encoding="utf-8")
        status = inspect_procedure(self.entry, self.bundle)
        self.assertEqual(status["status"], "INCOMPATIBLE")
        self.assertTrue(any("parent path component is not a directory" in issue["detail"] for issue in status["issues"]))

    def test_symlinked_targets_and_unmanaged_repositories_are_refused(self) -> None:
        outside = self.root / "outside.md"
        outside.write_text("outside\n", encoding="utf-8")
        (self.repo / ".github").symlink_to(self.root, target_is_directory=True)
        self.assertEqual(inspect_procedure(self.entry, self.bundle)["status"], "INCOMPATIBLE")
        plan = preview_reconciliation(self.entry, self.bundle)
        self.assertFalse(plan.can_materialize)
        self.assertEqual(outside.read_text(encoding="utf-8"), "outside\n")

        unmanaged = _git_repo(self.root / "unmanaged")
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            result = main(["coder-procedure-status", "--repository", str(unmanaged)])
        self.assertEqual(result, 2)
        self.assertIn("not managed", stderr.getvalue())

    def test_cli_inspect_and_preview_use_central_registry_and_do_not_write(self) -> None:
        vocab = _git_repo(self.root / "vocab")
        (vocab / "docs/milestones").mkdir(parents=True)
        vocab_overlay = vocab / "docs/context/coding_agent_rules.md"
        vocab_overlay.parent.mkdir(parents=True)
        vocab_overlay.write_text(
            "# Vocab overlay\n\n<!-- repo-control-project-overlay:v1 -->\n\n"
            "## Repository-specific rules\n\n- Preserve learner data.\n",
            encoding="utf-8",
        )
        vocab_entry = RepositoryEntry(
            key="vocab-app",
            name="Vocab App",
            path=vocab,
            repository_id=make_repository_id(vocab),
            milestone_root="docs/milestones",
            documents={"coding_agent_rules": "docs/context/coding_agent_rules.md"},
        )
        self._write_registry([self.entry, vocab_entry])
        before = sorted(path.relative_to(self.repo).as_posix() for path in self.repo.rglob("*"))
        git_status_before = subprocess.run(
            ["git", "-C", str(self.repo), "status", "--short"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = main(["coder-procedure-preview", "--repository", str(self.repo), "--format", "json"])
        self.assertEqual(result, 0)
        preview = json.loads(out.getvalue())
        self.assertEqual(preview["repository"]["key"], "repo-control-ii")
        self.assertEqual(preview["status"], "MISSING")
        self.assertTrue(preview["overlay_untouched"])
        after = sorted(path.relative_to(self.repo).as_posix() for path in self.repo.rglob("*"))
        self.assertEqual(before, after)
        self.assertEqual(
            git_status_before,
            subprocess.run(
                ["git", "-C", str(self.repo), "status", "--short"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout,
        )

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = main(["coder-procedure-status", "--repository", str(self.repo), "--format", "json"])
        self.assertEqual(result, 0)
        status = json.loads(out.getvalue())
        self.assertEqual(status["status"], "MISSING")

        vocab_before = sorted(path.relative_to(vocab).as_posix() for path in vocab.rglob("*"))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = main(["coder-procedure-preview", "--repository", str(vocab), "--format", "json"])
        self.assertEqual(result, 0)
        vocab_preview = json.loads(out.getvalue())
        self.assertEqual(vocab_preview["repository"]["key"], "vocab-app")
        self.assertEqual(vocab_preview["repository"]["repository_id"], make_repository_id(vocab))
        self.assertEqual(vocab_preview["status"], "MISSING")
        self.assertTrue(vocab_preview["overlay_untouched"])
        self.assertEqual(
            vocab_before,
            sorted(path.relative_to(vocab).as_posix() for path in vocab.rglob("*")),
        )
        vocab_plan = preview_reconciliation(vocab_entry, self.bundle)
        with self.assertRaisesRegex(ProcedureGovernanceError, "different managed repository"):
            materialize_preview(self.entry, vocab_plan)
        self.assertEqual(
            vocab_before,
            sorted(path.relative_to(vocab).as_posix() for path in vocab.rglob("*")),
        )


if __name__ == "__main__":
    unittest.main()
