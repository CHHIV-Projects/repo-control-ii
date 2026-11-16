from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from threading import Thread
from urllib.request import urlopen
from unittest.mock import Mock, patch

from flask import Flask
from werkzeug.serving import make_server

from repoctl.cli import main as cli_main
from repoctl.information_store import (
    BackupCoverageError,
    DocumentRevisionInput,
    InformationStore,
    InformationStoreError,
    RuntimeGateError,
    SchemaMismatchError,
    StaleStateError,
    StoreIdentity,
    V1FixtureBundle,
    dispatch_reconciliation_fingerprint,
)
from repoctl.migration_rehearsal import MigrationRehearsalError, verify_prepared_source_parity
from repoctl import information_store
from repoctl import backup_guard
from repoctl.mcp import architect_adapter
from repoctl.backup import create_verified_backup, restore_verified_backup
from repoctl.scanner.util import make_repository_id
from repoctl import store_authority
from repoctl.store_authority import (
    StoreAuthorityError,
    abort_prepared_authority,
    activate_prepared_authority,
    load_store_authority,
    preview_prepared_authority,
)
from repoctl.web.app import create_web_app
from repoctl.web.architect_contract import ArchitectContractError
from repoctl.web.repository_registry import RepositoryEntry, RepositoryRegistry
from repoctl.workflow import lifecycle
from repoctl.workflow.information_store_fixture import create_managed_information_store_clients
from tests.test_sqlite_workflow_poc import _closeout_history
from tests.test_closeout_publication import FixtureGitObserver


_ELIGIBLE_RUNTIME = {
    "sqlite_version_info": (3, 51, 3),
    "sqlite_version": "3.51.3",
    "minimum_required": (3, 51, 3),
    "production_eligible": True,
    "eligibility_basis": "minimum_version",
    "fixed_backport_evidence": None,
    "fixed_backport_evidence_status": "not_provided",
}


class ManagedStoreAuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="repoctl_managed_authority_", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repository_root = self.root / "repository"
        self.state_root = self.root / "state"
        self.state_root.mkdir()
        self.backup_staging = self.root / "backup-staging"
        self.backup_destination = self.root / "backup-destination"
        self.restore_root = self.root / "restore"
        self.backup_staging.mkdir(mode=0o700)
        self.backup_destination.mkdir(mode=0o700)
        self.restore_root.mkdir(mode=0o700)
        self.coverage_path = self.backup_staging / ".repoctl-verified-recovery-point.json"
        coverage_environment = patch.dict(
            os.environ,
            {"REPOCTL_BACKUP_COVERAGE_PATH": str(self.coverage_path)},
        )
        coverage_environment.start()
        self.addCleanup(coverage_environment.stop)
        (self.repository_root / "docs/milestones").mkdir(parents=True)
        (self.repository_root / "docs/context").mkdir(parents=True)
        (self.repository_root / "docs/context/project_context.md").write_text("# Context\n", encoding="utf-8")
        self.identity = StoreIdentity(
            repository_id=make_repository_id(self.repository_root),
            repository_key="managed-test",
            repository_root=str(self.repository_root),
            milestone_id="M999.001",
            prompt_path="docs/milestones/M999.001_fixture_prompt.md",
            selection_id="import-selection",
            selection_revision=1,
        )
        self.repository = RepositoryEntry(
            key=self.identity.repository_key,
            name="Managed test",
            path=self.repository_root,
            repository_id=self.identity.repository_id,
            milestone_root="docs/milestones",
            documents={"context": "docs/context/project_context.md"},
        )
        self.registry = RepositoryRegistry((self.repository,), self.repository.key)
        self.registry_path = self.root / "repositories.toml"
        self.registry_path.write_text("# Registry supplied by isolated test\n", encoding="utf-8")
        self.database_path = self.root / "prepared.sqlite3"
        self.prompt_bytes = (
            b"# Imported managed test prompt\n"
            b"Evidence: docs/milestones/M999.001_evidence.md\n"
            b"Legacy sidecar: docs/milestones/M999.001_fixture_workflow.md\n"
        )
        self.prompt_path = self.repository_root / self.identity.prompt_path
        self.prompt_path.write_bytes(self.prompt_bytes)
        self.events = _closeout_history()[:2]
        ledger = lifecycle._empty_ledger(
            self.identity.repository_id,
            {"milestone_id": self.identity.milestone_id, "prompt_path": self.identity.prompt_path},
        )
        ledger["events"] = self.events
        self.workflow_bytes = lifecycle._render_sidecar(ledger).encode("utf-8")
        self.workflow_path = self.prompt_path.with_name(
            self.prompt_path.name.removesuffix("_prompt.md") + "_workflow.md"
        )
        self.workflow_path.write_bytes(self.workflow_bytes)
        self.selection_path = self.state_root / self.identity.repository_id / "milestones/selection.json"
        self.selection_path.parent.mkdir(parents=True)
        self.v1_selection = json.dumps({
            "schema_version": 1,
            "repository_id": self.identity.repository_id,
            "milestone_id": self.identity.milestone_id,
            "prompt_path": self.identity.prompt_path,
        }).encode("utf-8")
        self.selection_path.write_bytes(self.v1_selection)
        self.runtime_patch = patch("repoctl.information_store.sqlite_runtime_status", return_value=_ELIGIBLE_RUNTIME)
        self.runtime_patch.start()
        self.addCleanup(self.runtime_patch.stop)
        self.registry_patch = patch(
            "repoctl.web.repository_registry.RepositoryRegistry.from_toml", return_value=self.registry,
        )
        self.registry_patch.start()
        self.addCleanup(self.registry_patch.stop)
        prepared = InformationStore.initialize_managed_prepared(self.database_path, self.identity)
        self.bundle = V1FixtureBundle(
            prompt_path=self.identity.prompt_path,
            closeout_path=None,
            workflow_path=self.identity.prompt_path.removesuffix("_prompt.md") + "_workflow.md",
            prompt_bytes=self.prompt_bytes,
            closeout_bytes=None,
            workflow_bytes=self.workflow_bytes,
        )
        self.import_receipt = prepared.import_v1_fixture(self.bundle)
        InformationStore.bind_prepared_v1_selection(
            self.database_path,
            self.identity,
            selection_state_root=self.state_root,
            expected_v1_sha256=hashlib.sha256(self.v1_selection).hexdigest(),
            cutover_confirmed=True,
        )

    def activate_fixture_configuration(self) -> None:
        payload = {
            "schema_version": 1,
            "authority": "sqlite",
            "database_path": str(self.database_path),
            "registry_path": str(self.registry_path),
            "registry_sha256": hashlib.sha256(self.registry_path.read_bytes()).hexdigest(),
            "generation": "a" * 32,
        }
        raw = json.dumps(payload, sort_keys=True).encode("utf-8")
        guard = {
            "schema_version": 1,
            "configuration_sha256": hashlib.sha256(raw).hexdigest(),
        }
        with sqlite3.connect(self.database_path) as connection:
            connection.executemany(
                "INSERT OR REPLACE INTO store_metadata(key, value) VALUES (?, ?)",
                (
                    ("authority_generation", payload["generation"]),
                    ("authority_configuration_sha256", guard["configuration_sha256"]),
                    ("authority_state_root", str(self.state_root)),
                    ("authority_first_write", "0"),
                ),
            )
        (self.state_root.parent / f".{self.state_root.name}.information-store-authority.guard").write_text(
            json.dumps(guard), encoding="utf-8",
        )
        (self.state_root / "information-store-authority.json").write_bytes(raw)
        self.create_fixture_recovery_point()

    def create_fixture_recovery_point(self, state_root: Path | None = None) -> dict:
        configuration = load_store_authority(state_root or self.state_root)
        if configuration is None:
            raise AssertionError("The isolated managed authority fixture is not active.")
        return create_verified_backup(
            configuration.database_path,
            self.backup_staging,
            self.backup_destination,
            expected_repository_id=self.identity.repository_id,
            expected_repository_key=self.identity.repository_key,
            expected_repository_root=str(self.repository_root),
            authority_configuration=configuration,
            coverage_path=self.coverage_path,
        )

    def managed_clients(self, state_root: Path | None = None):
        configuration = load_store_authority(state_root or self.state_root)
        self.assertIsNotNone(configuration)
        return create_managed_information_store_clients(configuration, self.repository)

    def invoke_managed_cli(self, arguments: list[str]) -> tuple[int, str, str]:
        output, errors = StringIO(), StringIO()
        args = list(arguments)
        if "--state-root" not in args:
            args.extend(["--state-root", str(self.state_root)])
        with (
            patch("repoctl.workflow.coder_interface.CENTRAL_REPOSITORY_REGISTRY_PATH", self.registry_path),
            patch("repoctl.workflow.coder_interface.resolve_repository_entry", return_value=self.repository),
            redirect_stdout(output),
            redirect_stderr(errors),
        ):
            code = cli_main(args)
        return code, output.getvalue(), errors.getvalue()

    def post_browser_workflow_event(self, browser, clients, summary: str):
        with browser.session_transaction() as browser_session:
            token = browser_session["csrf_token"]
        workflow = clients.browser_snapshot()["workflow"]
        return browser.post("/milestones/workflow/event", data={
            "csrf_token": token,
            "repository_key": self.repository.key,
            "repository_generation": "0",
            "milestone_id": self.identity.milestone_id,
            "prompt_path": self.identity.prompt_path,
            "expected_phase": workflow["phase"],
            "expected_event_count": str(workflow["event_count"]),
            "expected_prompt_identity": workflow["prompt_identity"],
            "event_type": "test_result_recorded",
            "summary": summary,
            "evidence_source": "isolated managed backup-guard test",
            "result": "PASS",
            "po_confirmation": "confirmed",
        })

    def test_interrupted_activation_resumes_and_aborts_only_before_first_write(self) -> None:
        generation = "a" * 32
        prepared = InformationStore.open_managed_prepared(self.database_path, self.identity)
        plan = preview_prepared_authority(self.database_path, self.state_root, self.registry_path, generation)
        arguments = {
            "expected_plan_sha256": plan["plan_sha256"],
            "po_confirmed": True,
            "writers_quiesced": True,
            "source_parity_confirmed": True,
        }
        write_file = store_authority._write_new_authority_file

        def interrupt_config(root, name, content):
            if name == "information-store-authority.json":
                raise OSError("simulated interruption before configuration publication")
            return write_file(root, name, content)

        with patch("repoctl.store_authority._write_new_authority_file", side_effect=interrupt_config):
            with self.assertRaisesRegex(OSError, "simulated interruption"):
                activate_prepared_authority(
                    self.database_path, self.state_root, self.registry_path, generation, **arguments,
                )
        with self.assertRaises(StoreAuthorityError):
            load_store_authority(self.state_root)
        with self.assertRaises(StaleStateError):
            prepared.snapshot()
        configuration = activate_prepared_authority(
            self.database_path, self.state_root, self.registry_path, generation, **arguments,
        )
        self.assertEqual(configuration.generation, generation)
        self.assertEqual(self.managed_clients().workflow_snapshot()["event_count"], len(self.events))
        abort_prepared_authority(
            self.database_path, self.state_root, self.registry_path, generation,
            expected_plan_sha256=plan["plan_sha256"], po_confirmed=True, writers_quiesced=True,
        )
        self.assertIsNone(load_store_authority(self.state_root))
        self.assertEqual(prepared.snapshot()["event_count"], len(self.events))
        pointer = json.loads(self.selection_path.read_text(encoding="utf-8"))
        self.assertEqual(pointer["milestone_id"], self.identity.milestone_id)
        self.assertEqual(self.workflow_path.read_bytes(), self.workflow_bytes)

    def test_operator_cli_requires_exact_preview_and_explicit_cutover_confirmations(self) -> None:
        common = [
            "--registry", str(self.registry_path), "--state-root", str(self.state_root),
            "--database", str(self.database_path), "--generation", "d" * 32,
        ]

        def invoke(arguments):
            output, errors = StringIO(), StringIO()
            with (
                patch("repoctl.workflow.coder_interface.CENTRAL_REPOSITORY_REGISTRY_PATH", self.registry_path),
                redirect_stdout(output),
                redirect_stderr(errors),
            ):
                code = cli_main(["store-authority", *arguments])
            return code, output.getvalue(), errors.getvalue()

        code, output, error = invoke(["preview", *common])
        self.assertEqual(code, 0, error)
        plan = json.loads(output)
        review = ["--expected-plan-sha256", plan["plan_sha256"]]
        code, _, error = invoke(["activate", *common, *review])
        self.assertNotEqual(code, 0)
        self.assertIn("requires explicit PO", error)
        self.assertIsNone(load_store_authority(self.state_root))
        code, _, error = invoke([
            "activate", *common, *review, "--po-confirm", "--writers-quiesced", "--source-parity-confirmed",
        ])
        self.assertEqual(code, 0, error)
        code, output, error = invoke([
            "abort", *common, *review, "--po-confirm", "--writers-quiesced",
        ])
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output)["status"], "aborted_before_first_write")
        self.assertIsNone(load_store_authority(self.state_root))

    def test_activation_rechecks_source_parity_and_refuses_drift_before_guard(self) -> None:
        generation = "e" * 32
        plan = preview_prepared_authority(self.database_path, self.state_root, self.registry_path, generation)
        self.prompt_path.write_bytes(self.prompt_bytes + b"Changed after preflight\n")
        with self.assertRaisesRegex(StoreAuthorityError, "source parity"):
            activate_prepared_authority(
                self.database_path, self.state_root, self.registry_path, generation,
                expected_plan_sha256=plan["plan_sha256"], po_confirmed=True, writers_quiesced=True,
                source_parity_confirmed=True,
            )
        self.assertIsNone(load_store_authority(self.state_root))
        self.assertFalse(
            (self.state_root.parent / f".{self.state_root.name}.information-store-authority.guard").exists()
        )

    def test_activation_refuses_invented_import_approval(self) -> None:
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                "UPDATE milestone SET prompt_revision_id = (SELECT draft_revision_id FROM document)"
            )
        with self.assertRaisesRegex(StoreAuthorityError, "prompt approval is not unverified"):
            preview_prepared_authority(self.database_path, self.state_root, self.registry_path, "e" * 32)
        self.assertIsNone(load_store_authority(self.state_root))

    def test_abort_after_managed_write_is_refused_and_prepared_handle_stays_blocked(self) -> None:
        generation = "b" * 32
        prepared = InformationStore.open_managed_prepared(self.database_path, self.identity)
        plan = preview_prepared_authority(self.database_path, self.state_root, self.registry_path, generation)
        activate_prepared_authority(
            self.database_path, self.state_root, self.registry_path, generation,
            expected_plan_sha256=plan["plan_sha256"], po_confirmed=True, writers_quiesced=True,
            source_parity_confirmed=True,
        )
        self.create_fixture_recovery_point()
        clients = self.managed_clients()
        snapshot = clients.browser_snapshot()
        prompt = snapshot["workflow"]["unverified_prompt"]
        clients.confirm_imported_prompt(
            request_id=prompt["request_id"],
            expected_store_revision=snapshot["snapshot"]["store_revision"],
            document_id=prompt["document_id"],
            revision_id=prompt["revision_id"],
            content_sha256=prompt["content_sha256"],
            managed_po_confirmation=True,
        )
        with self.assertRaises(StoreAuthorityError):
            abort_prepared_authority(
                self.database_path, self.state_root, self.registry_path, generation,
                expected_plan_sha256=plan["plan_sha256"], po_confirmed=True, writers_quiesced=True,
            )
        with self.assertRaises(StaleStateError):
            prepared.snapshot()
        self.assertIsNotNone(load_store_authority(self.state_root))
        with sqlite3.connect(self.database_path) as connection:
            marker = connection.execute(
                "SELECT value FROM store_metadata WHERE key = 'authority_first_write'"
            ).fetchone()[0]
        self.assertEqual(marker, "1")
        self.assertEqual(self.workflow_path.read_bytes(), self.workflow_bytes)

    def test_interrupted_managed_selection_recovers_only_exact_adjacent_binding(self) -> None:
        prepared = InformationStore.open_managed_prepared(self.database_path, self.identity)
        next_scope = replace(
            self.identity,
            milestone_id="M999.002",
            prompt_path="docs/milestones/M999.002_second_prompt.md",
            selection_id="second-import",
            selection_revision=2,
        )
        second = prepared.register_import_scope(next_scope)
        (self.repository_root / next_scope.prompt_path).write_text("# Second prompt\n", encoding="utf-8")
        second.import_v1_fixture(V1FixtureBundle(
            prompt_path=next_scope.prompt_path,
            prompt_bytes=b"# Second prompt\n",
            workflow_path=None,
            workflow_bytes=None,
            closeout_path=None,
            closeout_bytes=None,
        ))
        old = replace(self.identity, selection_id="first-restored", selection_revision=3)
        second.register_import_scope(old)
        pointer = json.loads(self.selection_path.read_text(encoding="utf-8"))
        pointer.update(selection_id=old.selection_id, selection_revision=old.selection_revision)
        self.selection_path.write_text(json.dumps(pointer), encoding="utf-8")
        generation = "c" * 32
        plan = preview_prepared_authority(self.database_path, self.state_root, self.registry_path, generation)
        configuration = activate_prepared_authority(
            self.database_path, self.state_root, self.registry_path, generation,
            expected_plan_sha256=plan["plan_sha256"], po_confirmed=True, writers_quiesced=True,
            source_parity_confirmed=True,
        )
        self.create_fixture_recovery_point()
        clients = self.managed_clients()
        selection_bytes = self.selection_path.read_bytes()
        selected_before = clients.store.identity
        coverage = json.loads(self.coverage_path.read_text(encoding="utf-8"))
        with patch(
            "repoctl.backup_guard.utc_now",
            return_value=datetime.fromisoformat(coverage["verified_at_utc"])
            + timedelta(seconds=backup_guard.WRITE_GUARD_MAX_AGE_SECONDS),
        ):
            with self.assertRaisesRegex(InformationStoreError, "writes are paused"):
                clients.store.switch_selection(
                    next_scope.milestone_id,
                    expected_selection_id=old.selection_id,
                    expected_selection_revision=old.selection_revision,
                )
        self.assertEqual(self.selection_path.read_bytes(), selection_bytes)
        self.assertEqual(clients.store.identity, selected_before)
        self.create_fixture_recovery_point()
        write_selection = information_store._write_fixture_selection

        def interrupt_after_pointer(*args):
            write_selection(*args)
            raise OSError("simulated failure after pointer replacement")

        with patch("repoctl.information_store._write_fixture_selection", side_effect=interrupt_after_pointer):
            with self.assertRaisesRegex(OSError, "simulated failure"):
                clients.store.switch_selection(
                    next_scope.milestone_id,
                    expected_selection_id=old.selection_id,
                    expected_selection_revision=old.selection_revision,
                )
        external = json.loads(self.selection_path.read_text(encoding="utf-8"))
        new = replace(
            old,
            milestone_id=external["milestone_id"],
            prompt_path=external["prompt_path"],
            selection_id=external["selection_id"],
            selection_revision=external["selection_revision"],
        )
        with self.assertRaises(InformationStoreError):
            self.managed_clients()
        with self.assertRaises(InformationStoreError):
            InformationStore.recover_managed_selection_mirror(
                configuration, expected_external=new, expected_mirror=old, po_recovery_confirmed=False,
            )
        common = [
            "--registry", str(self.registry_path), "--state-root", str(self.state_root),
            "--repository-key", self.repository.key,
        ]
        with patch("repoctl.workflow.coder_interface.CENTRAL_REPOSITORY_REGISTRY_PATH", self.registry_path):
            output, error = StringIO(), StringIO()
            with redirect_stdout(output), redirect_stderr(error):
                code = cli_main(["store-authority", "preview-selection-recovery", *common])
            self.assertEqual(code, 0, error.getvalue())
            preview = json.loads(output.getvalue())
            self.assertEqual(preview["external"]["selection_id"], new.selection_id)
            self.assertEqual(preview["mirror"]["selection_id"], old.selection_id)
            output, error = StringIO(), StringIO()
            with redirect_stdout(output), redirect_stderr(error):
                code = cli_main([
                    "store-authority", "recover-selection", *common, "--po-confirm",
                    "--expected-plan-sha256", preview["plan_sha256"],
                ])
            self.assertEqual(code, 0, error.getvalue())
            self.assertEqual(json.loads(output.getvalue())["selection"]["selection_id"], new.selection_id)
        self.assertEqual(self.managed_clients().identity, new)
        with self.assertRaises(StoreAuthorityError):
            abort_prepared_authority(
                self.database_path, self.state_root, self.registry_path, generation,
                expected_plan_sha256=plan["plan_sha256"], po_confirmed=True, writers_quiesced=True,
            )

    def test_runtime_gate_rejects_managed_authority_and_fixture_database(self) -> None:
        self.activate_fixture_configuration()
        configuration = load_store_authority(self.state_root)
        self.assertIsNotNone(configuration)
        unsupported = {**_ELIGIBLE_RUNTIME, "sqlite_version": "3.45.1", "production_eligible": False}
        with patch("repoctl.information_store.sqlite_runtime_status", return_value=unsupported):
            with self.assertRaises(RuntimeGateError):
                InformationStore.open_managed_from_external_selection(
                    configuration,
                    repository_id=self.identity.repository_id,
                    repository_key=self.identity.repository_key,
                    repository_root=self.identity.repository_root,
                )
            with self.assertRaises(RuntimeGateError):
                InformationStore.initialize_managed_prepared(self.root / "must-not-exist.sqlite3", self.identity)
        self.assertFalse((self.root / "must-not-exist.sqlite3").exists())

        fixture_path = self.root / "not-managed.sqlite3"
        InformationStore.initialize_disposable(fixture_path, self.identity)
        with self.assertRaises(SchemaMismatchError):
            InformationStore._open(fixture_path, self.identity, creation_mode="managed_prepared")

    @patch("repoctl.web.app.generate_milestone_status")
    def test_browser_cli_architect_share_guarded_authority_without_dual_write(self, _status) -> None:
        self.activate_fixture_configuration()
        app = create_web_app(repository_registry_path=self.registry_path, state_root=self.state_root)
        app.config["TESTING"] = True
        browser = app.test_client()
        clients = app.extensions["repoctl_information_store_clients"]
        page = browser.get("/milestones")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"historical workflow does not establish", page.data)
        data = clients.browser_snapshot()
        prompt = data["workflow"]["unverified_prompt"]
        with browser.session_transaction() as browser_session:
            csrf_token = browser_session["csrf_token"]
        form = {
            "csrf_token": csrf_token,
            "repository_key": self.repository.key,
            "repository_generation": "0",
            "selection_id": self.identity.selection_id,
            "selection_revision": str(self.identity.selection_revision),
            "store_revision": str(data["snapshot"]["store_revision"]),
            "document_id": prompt["document_id"],
            "revision_id": prompt["revision_id"],
            "content_sha256": prompt["content_sha256"],
            "request_id": prompt["request_id"],
        }
        self.assertEqual(browser.post("/milestones/confirm-imported-prompt", data=form).status_code, 403)
        self.assertEqual(
            browser.post("/milestones/confirm-imported-prompt", data={**form, "po_confirmation": "confirmed"}).status_code,
            302,
        )
        self.assertEqual(
            browser.post("/milestones/confirm-imported-prompt", data={**form, "po_confirmation": "confirmed"}).status_code,
            302,
        )
        with sqlite3.connect(self.database_path) as connection:
            decision = connection.execute(
                "SELECT source_kind, payload_json FROM document_revision_decision"
            ).fetchone()
        self.assertEqual(decision[0], "po_browser_reconfirmation")
        self.assertEqual(json.loads(decision[1])["historical_event_time_revision"], "UNKNOWN/NOT VERIFIED")
        self.assertEqual(clients.workflow_snapshot()["events"], self.events)
        with self.assertRaises(InformationStoreError):
            clients.store.confirm_imported_prompt_revision(
                request_id="fixture-confirmation-is-not-authority",
                expected_store_revision=len(self.events),
                document_id=prompt["document_id"],
                revision_id=prompt["revision_id"],
                content_sha256=prompt["content_sha256"],
                trusted_fixture_po_confirmation=True,
            )

        proposal = browser.post(
            "/api/architect/v1/dispatches/instruction",
            json={
                "expected_repository_key": self.repository.key,
                "expected_generation": 0,
                "milestone_id": self.identity.milestone_id,
                "body": "Isolated guidance",
            },
        )
        self.assertEqual(proposal.status_code, 201, proposal.json)
        dispatch = proposal.json["data"]["dispatch"]
        decision_url = f"/architect-dispatches/{dispatch['dispatch_id']}/approve"
        review = {
            "csrf_token": csrf_token,
            "repository_key": self.repository.key,
            "repository_generation": "0",
            "decision_fingerprint": dispatch_reconciliation_fingerprint(clients.store.get_dispatch(dispatch["dispatch_id"])),
        }
        self.assertEqual(browser.post(decision_url, data=review).status_code, 403)
        self.assertEqual(
            browser.post(decision_url, data={**review, "po_confirmation": "confirmed"}).status_code, 302,
        )
        self.assertEqual(clients.store.list_dispatch_history(dispatch["dispatch_id"])[-1]["source_kind"], "po_browser")

        output = StringIO()
        errors = StringIO()
        with (
            patch("repoctl.workflow.coder_interface.CENTRAL_REPOSITORY_REGISTRY_PATH", self.registry_path),
            patch("repoctl.workflow.coder_interface.resolve_repository_entry", return_value=self.repository),
            redirect_stdout(output),
            redirect_stderr(errors),
        ):
            result = cli_main(["coder-context", "--state-root", str(self.state_root), "--format", "json"])
        self.assertEqual(result, 0, errors.getvalue())
        self.assertEqual(json.loads(output.getvalue())["information_store_authority"], "sqlite_managed")
        self.assertEqual(self.prompt_path.read_bytes(), self.prompt_bytes)
        self.assertEqual(self.workflow_path.read_bytes(), self.workflow_bytes)
        self.assertEqual(clients.store.import_v1_fixture(self.bundle), self.import_receipt)

    @patch("repoctl.web.app.generate_milestone_status")
    def test_managed_browser_requires_explicit_workflow_confirmation(self, _status) -> None:
        self.activate_fixture_configuration()
        app = create_web_app(repository_registry_path=self.registry_path, state_root=self.state_root)
        app.config["TESTING"] = True
        clients = app.extensions["repoctl_information_store_clients"]
        initial = clients.browser_snapshot()
        prompt = initial["workflow"]["unverified_prompt"]
        clients.confirm_imported_prompt(
            request_id=prompt["request_id"],
            expected_store_revision=initial["snapshot"]["store_revision"],
            document_id=prompt["document_id"],
            revision_id=prompt["revision_id"],
            content_sha256=prompt["content_sha256"],
            managed_po_confirmation=True,
        )
        browser = app.test_client()
        page = browser.get("/milestones")
        self.assertIn(b'name="po_confirmation"', page.data)
        with browser.session_transaction() as browser_session:
            token = browser_session["csrf_token"]
        workflow = clients.browser_snapshot()["workflow"]
        form = {
            "csrf_token": token,
            "repository_key": self.repository.key,
            "repository_generation": "0",
            "milestone_id": self.identity.milestone_id,
            "prompt_path": self.identity.prompt_path,
            "expected_phase": workflow["phase"],
            "expected_event_count": str(workflow["event_count"]),
            "expected_prompt_identity": workflow["prompt_identity"],
            "event_type": "test_result_recorded",
            "summary": "Product Owner reviewed fixture evidence",
            "evidence_source": "isolated fixture",
            "result": "PASS",
        }
        self.assertEqual(browser.post("/milestones/workflow/event", data=form).status_code, 403)
        self.assertEqual(clients.workflow_snapshot()["event_count"], len(self.events))
        accepted = browser.post("/milestones/workflow/event", data={**form, "po_confirmation": "confirmed"})
        self.assertEqual(accepted.status_code, 302)
        self.assertEqual(clients.workflow_snapshot()["events"][-1]["event_type"], "test_result_recorded")
        self.assertEqual(self.workflow_path.read_bytes(), self.workflow_bytes)

        with (
            patch("repoctl.web.app._read_status", return_value={}),
            patch("repoctl.web.app.load_dashboard_view", return_value={}),
            patch("repoctl.web.app.render_template", return_value="Git-owned dashboard"),
        ):
            dashboard = browser.get("/")
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn(b"Git-owned dashboard", dashboard.data)

    @patch("repoctl.web.app.generate_milestone_status")
    def test_procedure_preview_reads_confirmed_store_prompt_and_blocks_unknown_authority(self, _status) -> None:
        self.activate_fixture_configuration()
        app = create_web_app(repository_registry_path=self.registry_path, state_root=self.state_root)
        app.config["TESTING"] = True
        clients = app.extensions["repoctl_information_store_clients"]
        browser = app.test_client()
        self.assertEqual(browser.get("/milestones").status_code, 200)
        captured = {}

        def render(_template, **context):
            captured.update(context)
            return "procedure fixture"

        with patch("repoctl.web.app.render_template", side_effect=render):
            self.assertEqual(browser.get("/procedure-updates").status_code, 200)
        self.assertEqual(captured["release_preparation"]["status"], "UNAVAILABLE")
        with browser.session_transaction() as browser_session:
            token = browser_session["csrf_token"]
        with patch("repoctl.procedure_transactions.review_apply") as review:
            rejected = browser.post("/procedure-updates/apply-review", data={
                "csrf_token": token,
                "repository_key": self.repository.key,
                "active_repository_key": self.repository.key,
                "repository_generation": "0",
                "plan_sha256": "0" * 64,
            })
        self.assertEqual(rejected.status_code, 409)
        review.assert_not_called()
        initial = clients.browser_snapshot()
        prompt = initial["workflow"]["unverified_prompt"]
        clients.confirm_imported_prompt(
            request_id=prompt["request_id"],
            expected_store_revision=initial["snapshot"]["store_revision"],
            document_id=prompt["document_id"],
            revision_id=prompt["revision_id"],
            content_sha256=prompt["content_sha256"],
            managed_po_confirmation=True,
        )
        with (
            patch("repoctl.web.app.render_template", side_effect=render),
            patch(
                "repoctl.procedure_release.inspect_procedure_release_preparation",
                return_value={"status": "NOT_REQUESTED", "authorized": False},
            ) as inspect,
            patch("repoctl.workflow.coder_interface.resolve_coder_release_preparation_context") as legacy,
        ):
            self.assertEqual(browser.get("/procedure-updates").status_code, 200)
        self.assertEqual(inspect.call_args.kwargs["prompt_content"], self.prompt_bytes)
        legacy.assert_not_called()
        self.assertEqual(self.workflow_path.read_bytes(), self.workflow_bytes)

    @patch("repoctl.web.app.generate_milestone_status")
    def test_incomplete_or_changed_configuration_fails_closed(self, _status) -> None:
        legacy = create_web_app(repository_registry_path=self.registry_path, state_root=self.state_root)
        legacy.config["TESTING"] = True
        self.activate_fixture_configuration()
        with self.assertRaises(StoreAuthorityError):
            (self.state_root / "information-store-authority.json").write_text("{}", encoding="utf-8")
            load_store_authority(self.state_root)
        self.assertEqual(legacy.test_client().get("/milestones").status_code, 503)

        self.activate_fixture_configuration()
        clients = self.managed_clients()
        self.registry_path.write_text("# changed registry\n", encoding="utf-8")
        with self.assertRaises(StaleStateError):
            clients.workflow_snapshot()

    def test_coder_cli_explicitly_imports_closeout_draft_and_submits_atomically(self) -> None:
        self.activate_fixture_configuration()
        clients = self.managed_clients()
        initial = clients.browser_snapshot()
        prompt = initial["workflow"]["unverified_prompt"]
        clients.confirm_imported_prompt(
            request_id=prompt["request_id"],
            expected_store_revision=initial["snapshot"]["store_revision"],
            document_id=prompt["document_id"],
            revision_id=prompt["revision_id"],
            content_sha256=prompt["content_sha256"],
            managed_po_confirmation=True,
        )
        closeout_path = self.identity.prompt_path.removesuffix("_prompt.md") + "_closeout.md"
        content = b"# Explicit closeout draft\n"
        (self.repository_root / closeout_path).write_bytes(content)
        digest = hashlib.sha256(content).hexdigest()

        def submit(expected_hash: str) -> tuple[int, str]:
            output, errors = StringIO(), StringIO()
            with (
                patch("repoctl.workflow.coder_interface.CENTRAL_REPOSITORY_REGISTRY_PATH", self.registry_path),
                patch("repoctl.workflow.coder_interface.resolve_repository_entry", return_value=self.repository),
                redirect_stdout(output),
                redirect_stderr(errors),
            ):
                code = cli_main([
                    "coder-closeout", "--state-root", str(self.state_root), "--summary", "Submit exact draft",
                    "--draft-file", closeout_path, "--content-sha256", expected_hash,
                ])
            return code, output.getvalue() + errors.getvalue()

        rejected, message = submit("0" * 64)
        self.assertNotEqual(rejected, 0, message)
        self.assertEqual(clients.workflow_snapshot()["event_count"], len(self.events))
        accepted, message = submit(digest)
        self.assertEqual(accepted, 0, message)
        snapshot = clients.workflow_snapshot()
        self.assertEqual(snapshot["projection"]["phase"], "REVIEW")
        event = snapshot["events"][-1]
        link = clients.store.get_event_document_link(event["event_id"], "closeout")
        self.assertEqual(link["content_sha256"], digest)
        self.assertEqual(clients.store.get_revision(link["document_id"], link["revision_id"])["content"], content.decode())
        self.assertEqual(self.workflow_path.read_bytes(), self.workflow_bytes)

    def test_prompt_approved_git_evidence_never_falls_back_to_legacy_workflow(self) -> None:
        self.activate_fixture_configuration()
        clients = self.managed_clients()
        snapshot = clients.browser_snapshot()
        prompt = snapshot["workflow"]["unverified_prompt"]
        clients.confirm_imported_prompt(
            request_id=prompt["request_id"],
            expected_store_revision=snapshot["snapshot"]["store_revision"],
            document_id=prompt["document_id"],
            revision_id=prompt["revision_id"],
            content_sha256=prompt["content_sha256"],
            managed_po_confirmation=True,
        )
        evidence_path = "docs/milestones/M999.001_evidence.md"
        evidence = ("evidence \u00e9\n" * 20_000)
        (self.repository_root / evidence_path).write_text(evidence, encoding="utf-8")
        first = clients.read_selected_milestone_document_page(evidence_path)
        self.assertEqual(first["authority_classification"], "git_owned_milestone_evidence")
        self.assertFalse(first["complete"])
        second = clients.read_selected_milestone_document_page(
            evidence_path, start_byte=first["next_offset"], source_fingerprint=first["source_fingerprint"],
        )
        self.assertEqual(first["content"] + second["content"], evidence)
        with self.assertRaisesRegex(ArchitectContractError, "workflow documents require"):
            clients.read_selected_milestone_document_page(
                self.identity.prompt_path.removesuffix("_prompt.md") + "_workflow.md"
            )
        (self.repository_root / evidence_path).write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(ArchitectContractError, "changed between pages"):
            clients.read_selected_milestone_document_page(
                evidence_path, start_byte=first["next_offset"], source_fingerprint=first["source_fingerprint"],
            )

    @patch("repoctl.web.app.generate_milestone_status")
    def test_registered_mcp_uses_bound_http_routes_and_rejects_mismatched_authority(self, _status) -> None:
        self.activate_fixture_configuration()
        app = create_web_app(repository_registry_path=self.registry_path, state_root=self.state_root)
        app.config["TESTING"] = True
        clients = app.extensions["repoctl_information_store_clients"]
        initial = clients.browser_snapshot()
        prompt = initial["workflow"]["unverified_prompt"]
        clients.confirm_imported_prompt(
            request_id=prompt["request_id"],
            expected_store_revision=initial["snapshot"]["store_revision"],
            document_id=prompt["document_id"],
            revision_id=prompt["revision_id"],
            content_sha256=prompt["content_sha256"],
            managed_po_confirmation=True,
        )
        browser = app.test_client()
        generation = browser.get("/healthz").json["active_repository_generation"]
        payload = {
            "expected_repository_key": self.repository.key,
            "expected_generation": generation,
            "milestone_id": self.identity.milestone_id,
            "body": "Proposed instruction",
        }
        rejected = browser.post(
            "/api/architect/v1/store-bound/dispatches/instruction",
            json=payload,
            headers={"X-Repoctl-Authority-Binding": "0" * 64},
        )
        self.assertEqual(rejected.status_code, 409)
        self.assertEqual(clients.list_architect_dispatch_page(limit=5)["dispatches"], [])

        server = make_server("127.0.0.1", 0, app, threaded=True)
        thread = Thread(target=server.serve_forever)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_port}"
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "repo_workflow", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "architect_submit_instruction_draft", "arguments": payload}},
        ]
        input_stream = StringIO("".join(json.dumps(item) + "\n" for item in requests))
        output_stream, error_stream = StringIO(), StringIO()
        serve = architect_adapter.serve_stdio

        def serve_fixture(**kwargs):
            return serve(
                **kwargs, in_stream=input_stream, out_stream=output_stream, err_stream=error_stream,
            )

        with (
            patch("repoctl.workflow.coder_interface.CENTRAL_REPOSITORY_REGISTRY_PATH", self.registry_path),
            patch("repoctl.workflow.coder_interface.resolve_repository_entry", return_value=self.repository),
            patch("repoctl.mcp.architect_adapter.serve_stdio", side_effect=serve_fixture),
        ):
            code = cli_main(["mcp-architect", "--state-root", str(self.state_root), "--repo-control-url", url])
        self.assertEqual(code, 0, error_stream.getvalue())
        responses = [json.loads(line) for line in output_stream.getvalue().splitlines()]
        self.assertEqual(len(responses), 2)
        for response in responses:
            self.assertFalse(response["result"]["isError"], response)
        self.assertEqual(clients.list_architect_dispatch_page(limit=5)["dispatches"][0]["body"], payload["body"])
        self.assertEqual(self.workflow_path.read_bytes(), self.workflow_bytes)

        legacy = Flask("legacy-fixture")
        legacy_writes: list[bool] = []

        @legacy.post("/api/architect/v1/dispatches/instruction")
        def legacy_submission():
            legacy_writes.append(True)
            return {"status": "written"}

        legacy_server = make_server("127.0.0.1", 0, legacy, threaded=True)
        legacy_thread = Thread(target=legacy_server.serve_forever)
        legacy_thread.start()
        self.addCleanup(legacy_thread.join, 5)
        self.addCleanup(legacy_server.shutdown)
        legacy_url = f"http://127.0.0.1:{legacy_server.server_port}"
        legacy_response = architect_adapter.handle_rpc_request(requests[1], legacy_url, StringIO(), clients)
        self.assertTrue(legacy_response["result"]["isError"])
        self.assertEqual(legacy_writes, [])
        remote_response = architect_adapter.handle_rpc_request(
            requests[0], "http://example.com", StringIO(), clients,
        )
        self.assertTrue(remote_response["result"]["isError"])
        self.assertIn("literal loopback", remote_response["result"]["content"][0]["text"])

        guard_path = self.state_root.parent / f".{self.state_root.name}.information-store-authority.guard"
        guard_path.write_text('{"schema_version": 1, "configuration_sha256": "changed"}')
        stale = architect_adapter.handle_rpc_request(requests[0], url, StringIO(), clients)
        self.assertTrue(stale["result"]["isError"])
        self.assertIn("information_store_binding_unavailable", stale["result"]["content"][0]["text"])

    @patch("repoctl.web.app.generate_milestone_status")
    def test_coder_intelligence_requires_same_bound_service(self, _status) -> None:
        self.activate_fixture_configuration()
        app = create_web_app(repository_registry_path=self.registry_path, state_root=self.state_root)
        app.config["TESTING"] = True
        browser_clients = app.extensions["repoctl_information_store_clients"]
        initial = browser_clients.browser_snapshot()
        prompt = initial["workflow"]["unverified_prompt"]
        browser_clients.confirm_imported_prompt(
            request_id=prompt["request_id"],
            expected_store_revision=initial["snapshot"]["store_revision"],
            document_id=prompt["document_id"],
            revision_id=prompt["revision_id"],
            content_sha256=prompt["content_sha256"],
            managed_po_confirmation=True,
        )
        cli_clients = self.managed_clients()
        server = make_server("127.0.0.1", 0, app, threaded=True)
        thread = Thread(target=server.serve_forever)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_port}"

        def invoke(arguments):
            output, errors = StringIO(), StringIO()
            with redirect_stdout(output), redirect_stderr(errors):
                code = cli_main(arguments, information_store_clients=cli_clients)
            return code, output.getvalue(), errors.getvalue()

        with (
            patch("repoctl.intelligence.client.BASE_URL", url),
            patch("repoctl.workflow.coder_interface.resolve_repository_entry", return_value=self.repository),
            patch("repoctl.web.app.intelligence_query", return_value={"outcome": "OK", "results": []}) as query,
            patch("repoctl.web.app.refresh_graph", return_value={"outcome": "CURRENT"}) as refresh,
            patch("repoctl.web.app.intelligence_measurements.rate") as rate,
        ):
            code, output, error = invoke(["coder-intelligence"])
            self.assertEqual(code, 0, error)
            self.assertEqual(json.loads(output)["outcome"], "OK")
            query.assert_called_once()
            code, _, error = invoke(["coder-intelligence-rate", "fixture-use", "USEFUL", "--inspected", "yes"])
            self.assertEqual(code, 0, error)
            rate.assert_called_once()
            code, output, error = invoke(["coder-intelligence-refresh"])
            self.assertEqual(code, 0, error)
            self.assertEqual(json.loads(output)["outcome"], "CURRENT")
            refresh.assert_called_once()
            rejected = app.test_client().post(
                "/api/coder/v1/store-bound/intelligence/refresh",
                json={},
                headers={"X-Repoctl-Authority-Binding": "0" * 64},
            )
            self.assertEqual(rejected.status_code, 409)
            refresh.assert_called_once()

        legacy = Flask("legacy-intelligence")
        legacy_calls: list[str] = []

        @legacy.get("/api/architect/v1/status")
        def legacy_status():
            legacy_calls.append("status")
            return {"repository": {"key": self.repository.key, "repository_id": self.repository.repository_id}}

        @legacy.post("/api/coder/v1/intelligence/refresh")
        def legacy_refresh():
            legacy_calls.append("refresh")
            return {"outcome": "CURRENT"}

        legacy_server = make_server("127.0.0.1", 0, legacy, threaded=True)
        legacy_thread = Thread(target=legacy_server.serve_forever)
        legacy_thread.start()
        self.addCleanup(legacy_thread.join, 5)
        self.addCleanup(legacy_server.shutdown)
        with (
            patch("repoctl.intelligence.client.BASE_URL", f"http://127.0.0.1:{legacy_server.server_port}"),
            patch("repoctl.workflow.coder_interface.resolve_repository_entry", return_value=self.repository),
        ):
            code, _, error = invoke(["coder-intelligence-refresh"])
        self.assertNotEqual(code, 0)
        self.assertIn("HTTP 404", error)
        self.assertEqual(legacy_calls, [])

    @patch("repoctl.web.app.generate_milestone_status")
    def test_disposable_operator_cutover_mixed_clients_backup_and_recovery(self, _status) -> None:
        self.selection_path.write_bytes(self.v1_selection)
        candidate_state_root = self.root / "candidate-state"
        candidate_state_root.mkdir()
        candidate_selection_path = (
            candidate_state_root / self.identity.repository_id / "milestones/selection.json"
        )
        source_selection_before = self.selection_path.read_bytes()
        source_workflow_before = self.workflow_path.read_bytes()
        dispatch_path = self.state_root / self.identity.repository_id / "architect_dispatches.json"
        timestamp = "2026-01-01T00:00:00+00:00"
        imported_dispatch = {
            "dispatch_id": "a" * 32,
            "repository_id": self.identity.repository_id,
            "milestone_id": self.identity.milestone_id,
            "type": "INSTRUCTION",
            "status": "APPROVED",
            "body": "Historical guidance snapshot.",
            "approved_milestone_id": self.identity.milestone_id,
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        dispatch_bytes = json.dumps({
            "schema_version": 1,
            "repository_id": self.identity.repository_id,
            "dispatches": [imported_dispatch],
        }).encode("utf-8")
        dispatch_path.write_bytes(dispatch_bytes)
        destination = self.root / "operator-cutover"
        destination.mkdir()

        def invoke(arguments):
            output, errors = StringIO(), StringIO()
            with (
                patch("repoctl.workflow.coder_interface.CENTRAL_REPOSITORY_REGISTRY_PATH", self.registry_path),
                patch("repoctl.workflow.coder_interface.resolve_repository_entry", return_value=self.repository),
                redirect_stdout(output),
                redirect_stderr(errors),
            ):
                code = cli_main(arguments)
            return code, output.getvalue(), errors.getvalue()

        operator = ["--registry", str(self.registry_path), "--state-root", str(self.state_root)]
        code, output, error = invoke(["store-authority", "freeze-writers", *operator])
        self.assertEqual(code, 0, error)
        freeze_id = json.loads(output)["freeze_id"]
        prepare = [
            "store-authority", "prepare", *operator,
            "--candidate-state-root", str(candidate_state_root),
            "--destination", str(destination),
        ]
        code, _, error = invoke(prepare)
        self.assertNotEqual(code, 0)
        self.assertIn("quiesced", error)
        code, output, error = invoke([*prepare, "--writers-quiesced"])
        self.assertEqual(code, 0, error)
        imported = json.loads(output)
        self.assertEqual(imported["imported_milestone_count"], 1)
        self.assertEqual(imported["imported_event_count"], len(self.events))
        self.assertEqual(imported["import_refusal_count"], 0)
        self.assertEqual(imported["mismatch_count"], 0)
        database = Path(imported["prepared_database_path"])
        binding = imported["prepared_selection_bindings"][0]
        candidate_operator = ["--registry", str(self.registry_path), "--state-root", str(candidate_state_root)]
        bind = [
            "store-authority", "bind-selection", *candidate_operator, "--database", str(database),
            "--binding-json", json.dumps(binding),
        ]
        code, _, error = invoke(bind)
        self.assertNotEqual(code, 0)
        self.assertIn("confirmation", error)
        code, _, error = invoke([*bind, "--po-confirm"])
        self.assertEqual(code, 0, error)
        common = [
            *candidate_operator,
            "--source-state-root", str(self.state_root),
            "--database", str(database),
            "--generation", "f" * 32,
        ]

        tampered_path = self.root / "tampered-import.sqlite3"
        original, tampered = sqlite3.connect(database), sqlite3.connect(tampered_path)
        try:
            original.backup(tampered)
            tampered.execute(
                """INSERT INTO dispatch_history
                   SELECT repository_id, dispatch_id, 2, ?, 'DECISION', 'APPROVED', 'APPROVED',
                          'product_owner', 'fabricated decision', 1, related_event_id, recorded_at,
                          'po_browser', payload_json
                   FROM dispatch_history WHERE sequence = 1""",
                ("c" * 32,),
            )
            tampered.commit()
            tampered.row_factory = sqlite3.Row
            with self.assertRaisesRegex(MigrationRehearsalError, "Dispatch provenance parity"):
                verify_prepared_source_parity(
                    self.registry, self.state_root, {self.repository.key: self.identity.milestone_id}, tampered,
                )
        finally:
            original.close()
            tampered.close()
        with sqlite3.connect(database) as connection:
            connection.execute("UPDATE architect_dispatch SET status = 'REJECTED'")
        code, _, error = invoke(["store-authority", "preview", *common])
        self.assertNotEqual(code, 0)
        self.assertIn("Dispatch provenance parity", error)
        with sqlite3.connect(database) as connection:
            connection.execute("UPDATE architect_dispatch SET status = 'APPROVED'")
        code, output, error = invoke(["store-authority", "preview", *common])
        self.assertEqual(code, 0, error)
        plan = json.loads(output)
        confirmed = [
            "--expected-plan-sha256", plan["plan_sha256"], "--po-confirm",
            "--writers-quiesced", "--source-parity-confirmed",
        ]
        unsupported = {**_ELIGIBLE_RUNTIME, "sqlite_version": "3.45.1", "production_eligible": False}
        with patch("repoctl.information_store.sqlite_runtime_status", return_value=unsupported):
            code, _, error = invoke(["store-authority", "activate", *common, *confirmed])
        self.assertNotEqual(code, 0)
        self.assertIn("not eligible", error)
        self.assertIsNone(load_store_authority(self.state_root))
        code, _, error = invoke(["store-authority", "activate", *common, *confirmed])
        self.assertEqual(code, 0, error)
        abort = [
            "store-authority", "abort", *common, "--expected-plan-sha256", plan["plan_sha256"],
            "--po-confirm", "--writers-quiesced",
        ]
        code, output, error = invoke(abort)
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output)["status"], "aborted_before_first_write")
        self.assertIsNone(load_store_authority(self.state_root))
        code, _, error = invoke(["store-authority", "activate", *common, *confirmed])
        self.assertEqual(code, 0, error)
        code, output, error = invoke([
            "store-authority", "release-writers", *operator, "--freeze-id", freeze_id,
        ])
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output)["status"], "released")
        self.create_fixture_recovery_point(candidate_state_root)

        scanner_cache_root = self.root / "candidate-scanner-cache"
        app = create_web_app(
            repository_registry_path=self.registry_path,
            state_root=candidate_state_root,
            scanner_cache_root=scanner_cache_root,
        )
        self.assertEqual(app.config["STATE_ROOT"], candidate_state_root)
        self.assertEqual(app.config["SCANNER_CACHE_ROOT"], scanner_cache_root)
        app.config["TESTING"] = True
        browser = app.test_client()
        clients = app.extensions["repoctl_information_store_clients"]
        page = browser.get("/milestones")
        self.assertEqual(page.status_code, 200)
        with browser.session_transaction() as browser_session:
            token = browser_session["csrf_token"]
        initial = clients.browser_snapshot()
        prompt = initial["workflow"]["unverified_prompt"]
        self.assertEqual(len(initial["workflow"]["unverified_guidance"]), 1)
        po = {
            "csrf_token": token,
            "repository_key": self.repository.key,
            "repository_generation": "0",
            "po_confirmation": "confirmed",
        }
        confirmation = {
            **po,
            "selection_id": binding["selection_id"],
            "selection_revision": str(binding["selection_revision"]),
            "store_revision": str(initial["snapshot"]["store_revision"]),
            "document_id": prompt["document_id"],
            "revision_id": prompt["revision_id"],
            "content_sha256": prompt["content_sha256"],
            "request_id": prompt["request_id"],
        }
        self.assertEqual(browser.post("/milestones/confirm-imported-prompt", data=confirmation).status_code, 302)
        backup_report = self.create_fixture_recovery_point(candidate_state_root)
        server = make_server("127.0.0.1", 0, app, threaded=True)
        thread = Thread(target=server.serve_forever)
        thread.start()
        try:
            with urlopen(f"http://127.0.0.1:{server.server_port}/healthz", timeout=5) as response:
                health = json.loads(response.read())
        finally:
            server.shutdown()
            thread.join(5)
            server.server_close()
        browser_binding = health["information_store_binding"]
        self.assertEqual(browser_binding["authority_state_root"], str(candidate_state_root))
        self.assertEqual(browser_binding["milestone_id"], binding["milestone_id"])
        self.assertEqual(browser_binding["selection_id"], binding["selection_id"])
        self.assertEqual(browser_binding["selection_revision"], binding["selection_revision"])

        cli_script = (
            "import sys\n"
            "from pathlib import Path\n"
            "from repoctl.scanner import core as scanner_core\n"
            "from repoctl.web.repository_registry import RepositoryEntry, RepositoryRegistry\n"
            "from repoctl.workflow import coder_interface\n"
            "repository = Path(sys.argv[1]).resolve()\n"
            "entry = RepositoryEntry(\n"
            "    key=sys.argv[2], name='Managed test', path=repository, repository_id=sys.argv[3],\n"
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
        def launch_independent_cli(state_root: Path, environment=None):
            return subprocess.run(
                [
                sys.executable,
                "-c",
                cli_script,
                str(self.repository_root),
                self.identity.repository_key,
                self.identity.repository_id,
                str(self.registry_path),
                str(state_root),
                "coder-status",
                "--repository",
                str(self.repository_root),
                "--state-root",
                str(state_root),
                "--format",
                "json",
                ],
                cwd=self.repository_root,
                capture_output=True,
                text=True,
                check=False,
                env=environment,
            )

        cli_result = launch_independent_cli(candidate_state_root)
        self.assertEqual(cli_result.returncode, 0, cli_result.stderr)
        cli_status = json.loads(cli_result.stdout)
        self.assertEqual(cli_status["information_store_authority"], "sqlite_managed")
        self.assertEqual(cli_status["selection"]["milestone_id"], browser_binding["milestone_id"])
        self.assertEqual(cli_status["selection"]["selection_id"], browser_binding["selection_id"])
        self.assertEqual(cli_status["selection"]["selection_revision"], browser_binding["selection_revision"])
        self.assertEqual(
            cli_status["workflow"]["event_count"],
            backup_report["snapshot"]["selected_workflow"]["event_count"],
        )
        self.assertEqual(
            backup_report["snapshot"]["selection"],
            {
                "milestone_id": browser_binding["milestone_id"],
                "prompt_path": browser_binding["prompt_path"],
                "selection_id": browser_binding["selection_id"],
                "selection_revision": browser_binding["selection_revision"],
                "prompt_revision_id": browser_binding["prompt_revision_id"],
            },
        )
        recovery_record = json.loads(self.coverage_path.read_text(encoding="utf-8"))
        configuration = load_store_authority(candidate_state_root)
        self.assertIsNotNone(configuration)
        expected_backup_binding = backup_guard.authority_binding(
            configuration,
            browser_binding["database_identity"],
            repository_id=browser_binding["repository_id"],
            repository_key=browser_binding["repository_key"],
            repository_root=browser_binding["repository_root"],
            milestone_id=browser_binding["milestone_id"],
            prompt_path=browser_binding["prompt_path"],
            selection_id=browser_binding["selection_id"],
            selection_revision=browser_binding["selection_revision"],
        )
        self.assertEqual(recovery_record["authority_binding"], expected_backup_binding)
        candidate_selection = json.loads(candidate_selection_path.read_text(encoding="utf-8"))
        self.assertEqual(candidate_selection["milestone_id"], binding["milestone_id"])
        self.assertEqual(candidate_selection["prompt_path"], binding["prompt_path"])
        self.assertEqual(candidate_selection["selection_id"], binding["selection_id"])
        self.assertEqual(candidate_selection["selection_revision"], binding["selection_revision"])
        self.assertEqual(
            candidate_selection["v1_selection_sha256"],
            hashlib.sha256(self.v1_selection).hexdigest(),
        )
        self.assertEqual(self.selection_path.read_bytes(), source_selection_before)
        self.assertEqual(self.workflow_path.read_bytes(), source_workflow_before)
        self.assertIsNone(load_store_authority(self.state_root))

        missing_state_root = self.root / "missing-candidate-state"
        missing_state_root.mkdir()
        missing_app = create_web_app(
            repository_registry_path=self.registry_path,
            state_root=missing_state_root,
        )
        missing_page = missing_app.test_client().get("/milestones")
        self.assertEqual(missing_page.status_code, 200)
        self.assertIn("No milestone selected", missing_page.get_data(as_text=True))
        missing_cli = launch_independent_cli(missing_state_root)
        self.assertNotEqual(missing_cli.returncode, 0)
        self.assertIn("No milestone is currently selected", missing_cli.stderr)

        invalid_state_root = self.root / "invalid-candidate-state"
        invalid_state_root.mkdir()
        (invalid_state_root / "information-store-authority.json").write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(StoreAuthorityError, "configuration is incomplete"):
            create_web_app(
                repository_registry_path=self.registry_path,
                state_root=invalid_state_root,
            )
        invalid_cli = launch_independent_cli(invalid_state_root)
        self.assertNotEqual(invalid_cli.returncode, 0)
        self.assertIn("legacy fallback is disabled", invalid_cli.stderr)

        candidate_environment = os.environ.copy()
        candidate_environment.pop("REPOCTL_BACKUP_COVERAGE_PATH", None)
        with patch.dict(os.environ, {"REPOCTL_BACKUP_COVERAGE_PATH": ""}):
            with self.assertRaisesRegex(BackupCoverageError, "requires its own explicit"):
                create_web_app(
                    repository_registry_path=self.registry_path,
                    state_root=candidate_state_root,
                )
        missing_coverage_cli = launch_independent_cli(candidate_state_root, candidate_environment)
        self.assertNotEqual(missing_coverage_cli.returncode, 0)
        self.assertIn("requires its own explicit", missing_coverage_cli.stderr)
        default_coverage_environment = candidate_environment | {
            "REPOCTL_BACKUP_COVERAGE_PATH": str(backup_guard.DEFAULT_RECOVERY_POINT_PATH),
        }
        with patch.dict(
            os.environ,
            {"REPOCTL_BACKUP_COVERAGE_PATH": str(backup_guard.DEFAULT_RECOVERY_POINT_PATH)},
        ):
            with self.assertRaisesRegex(BackupCoverageError, "live default recovery path"):
                create_web_app(
                    repository_registry_path=self.registry_path,
                    state_root=candidate_state_root,
                )
        default_coverage_cli = launch_independent_cli(candidate_state_root, default_coverage_environment)
        self.assertNotEqual(default_coverage_cli.returncode, 0)
        self.assertIn("live default recovery path", default_coverage_cli.stderr)

        reconcile = {
            **po,
            "reconciliation_history_id": "b" * 32,
            "reconciliation_fingerprint": dispatch_reconciliation_fingerprint(
                clients.store.get_dispatch(imported_dispatch["dispatch_id"])
            ),
        }
        self.assertEqual(
            browser.post(
                f"/architect-dispatches/{imported_dispatch['dispatch_id']}/reconcile-approve", data=reconcile,
            ).status_code,
            302,
        )
        self.assertEqual(clients.browser_snapshot()["workflow"]["unverified_guidance"], [])
        code, _, error = invoke(abort)
        self.assertNotEqual(code, 0)
        self.assertIn("A managed write occurred", error)

        release = {"status": "NOT_REQUESTED", "authorized": False}
        sources = {
            "procedure": {
                "inspection_status": "available", "status": "CURRENT", "overlay_valid": True,
                "release_preparation": release,
            },
            "release_preparation": release,
            "git_state": {
                "repository_root": str(self.repository_root),
                "branch": {"state": "attached", "name": "fixture"},
                "head": "a" * 40,
                "working_tree": {"entries": []},
                "git_operation_in_progress": False,
                "git_operations": [],
            },
            "git_failure_type": None,
        }
        response = Mock(status=200)
        response.read.return_value = json.dumps(browser.get("/healthz").json).encode()
        connection = Mock()
        connection.getresponse.return_value = response
        verify = [
            "coder-verify", "--state-root", str(candidate_state_root), "--trigger", "NEW",
            "--expected-repository-key", self.identity.repository_key,
            "--expected-repository-id", self.identity.repository_id,
            "--expected-repository-root", self.identity.repository_root,
            "--expected-milestone-id", self.identity.milestone_id,
            "--expected-selection-id", binding["selection_id"],
            "--expected-selection-revision", str(binding["selection_revision"]),
            "--expected-prompt-document-id", prompt["document_id"],
            "--expected-prompt-revision-id", prompt["revision_id"],
            "--expected-prompt-sha256", prompt["content_sha256"],
            "--acknowledge-git-boundary",
        ]
        with (
            patch("repoctl.workflow.coder_interface.collect_verification_sources", return_value=sources),
            patch("repoctl.workflow.runtime_compatibility.http.client.HTTPConnection", return_value=connection),
        ):
            code, output, error = invoke(verify)
            self.assertEqual(code, 0, error)
            self.assertEqual(json.loads(output)["result"], "VERIFIED")
            code, output, error = invoke(verify)
            self.assertEqual(code, 0, error)
            self.assertTrue(json.loads(output)["idempotent"])

        server = make_server("127.0.0.1", 0, app, threaded=True)
        thread = Thread(target=server.serve_forever)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}"
            requests = [
                {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                 "params": {"name": "repo_workflow", "arguments": {}}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                 "params": {"name": "architect_submit_instruction_draft", "arguments": {
                     "expected_repository_key": self.repository.key, "expected_generation": 0,
                     "milestone_id": self.identity.milestone_id, "body": "New fixture guidance.",
                 }}},
            ]
            input_stream = StringIO("".join(json.dumps(item) + "\n" for item in requests))
            output_stream, error_stream = StringIO(), StringIO()
            serve = architect_adapter.serve_stdio

            def serve_fixture(**kwargs):
                return serve(**kwargs, in_stream=input_stream, out_stream=output_stream, err_stream=error_stream)

            with patch("repoctl.mcp.architect_adapter.serve_stdio", side_effect=serve_fixture):
                code, _, error = invoke([
                    "mcp-architect", "--state-root", str(candidate_state_root), "--repo-control-url", url,
                ])
            self.assertEqual(code, 0, error or error_stream.getvalue())
            replies = [json.loads(line) for line in output_stream.getvalue().splitlines()]
            self.assertEqual(len(replies), 2)
            self.assertTrue(all(not reply["result"]["isError"] for reply in replies))
        finally:
            server.shutdown()
            thread.join(5)
            server.server_close()
        new_dispatch = next(
            item for item in clients.store.list_dispatches() if item["body"] == "New fixture guidance."
        )
        decision = {**po, "decision_fingerprint": dispatch_reconciliation_fingerprint(new_dispatch)}
        code, _, error = invoke([
            "coder-test", "Fixture test result", "--state-root", str(candidate_state_root),
            "--source", "integrated fixture", "--result", "PASS",
        ])
        self.assertEqual(code, 0, error)
        closeout_path = self.identity.prompt_path.removesuffix("_prompt.md") + "_closeout.md"
        closeout = b"# Integrated fixture closeout\n"
        (self.repository_root / closeout_path).write_bytes(closeout)
        code, _, error = invoke([
            "coder-closeout", "--state-root", str(candidate_state_root), "--summary", "Submit fixture closeout",
            "--draft-file", closeout_path, "--content-sha256", hashlib.sha256(closeout).hexdigest(),
        ])
        self.assertEqual(code, 0, error)
        self.assertEqual(
            browser.post(f"/architect-dispatches/{new_dispatch['dispatch_id']}/approve", data=decision).status_code,
            302,
        )
        workflow = clients.browser_snapshot()["workflow"]
        acceptance = {
            **po,
            "milestone_id": self.identity.milestone_id,
            "prompt_path": self.identity.prompt_path,
            "expected_phase": workflow["phase"],
            "expected_event_count": str(workflow["event_count"]),
            "expected_prompt_identity": workflow["prompt_identity"],
            "expected_closeout_identity": workflow["closeout_identity"],
            "event_type": "accept_and_close",
            "summary": "Accept fixture closeout",
        }
        self.assertEqual(browser.post("/milestones/workflow/event", data=acceptance).status_code, 200)
        outstanding_ids = [item["dispatch_id"] for item in workflow["outstanding_dispatches"]]
        self.assertIn(new_dispatch["dispatch_id"], outstanding_ids)
        self.assertEqual(browser.post("/milestones/workflow/event", data={
            **acceptance,
            "closure_guard_confirmed": "yes",
            "closure_guard_dispatch_id": outstanding_ids,
            "closure_override_reason": "Reviewed newer fixture guidance.",
        }).status_code, 302)
        before_backup = clients.workflow_snapshot()
        self.assertEqual(before_backup["projection"]["phase"], "CLOSED")
        self.assertEqual(self.prompt_path.read_bytes(), self.prompt_bytes)
        self.assertEqual(self.workflow_path.read_bytes(), self.workflow_bytes)
        self.assertEqual(dispatch_path.read_bytes(), dispatch_bytes)

        backup_path = self.root / "latest-backup.sqlite3"
        source, backup = sqlite3.connect(database), sqlite3.connect(backup_path)
        try:
            source.backup(backup)
            self.assertEqual(backup.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertIsNone(backup.execute("PRAGMA foreign_key_check").fetchone())
        finally:
            source.close()
            backup.close()
        backup, target = sqlite3.connect(backup_path), sqlite3.connect(database)
        try:
            backup.backup(target)
        finally:
            backup.close()
            target.close()
        restored = self.managed_clients(candidate_state_root)
        self.assertEqual(restored.workflow_snapshot(), before_backup)
        self.assertEqual(browser.post("/milestones/confirm-imported-prompt", data=confirmation).status_code, 302)
        self.assertEqual(restored.store.list_dispatch_history(imported_dispatch["dispatch_id"])[-1]["po_confirmed"], 1)
        code, _, error = invoke(abort)
        self.assertNotEqual(code, 0)
        self.assertIn("A managed write occurred", error)
        self.assertIsNotNone(load_store_authority(candidate_state_root))
        self.assertIsNone(load_store_authority(self.state_root))
        self.assertEqual(self.selection_path.read_bytes(), source_selection_before)
        self.assertEqual(self.workflow_path.read_bytes(), source_workflow_before)

    @patch("repoctl.web.app.generate_milestone_status")
    def test_registered_publication_handoff_uses_explicit_po_confirmation(self, _status) -> None:
        self.activate_fixture_configuration()
        clients = self.managed_clients()
        initial = clients.browser_snapshot()
        prompt = initial["workflow"]["unverified_prompt"]
        clients.confirm_imported_prompt(
            request_id=prompt["request_id"],
            expected_store_revision=initial["snapshot"]["store_revision"],
            document_id=prompt["document_id"],
            revision_id=prompt["revision_id"],
            content_sha256=prompt["content_sha256"],
            managed_po_confirmation=True,
        )
        closeout_path = self.identity.prompt_path.removesuffix("_prompt.md") + "_closeout.md"
        revision = clients.store.create_immutable_revision(DocumentRevisionInput(
            document_id="managed-closeout",
            revision_id="managed-closeout-revision",
            logical_key=closeout_path,
            logical_type="CLOSEOUT",
            source_path=closeout_path,
            content="# Accepted closeout\n",
            source_kind="coder_draft",
        ))
        clients.submit_coder_closeout(
            summary="Submit isolated closeout",
            document_id=revision["document_id"],
            revision_id=revision["revision_id"],
            content_sha256=revision["content_sha256"],
            expected_prompt_revision_id=prompt["revision_id"],
            expected_store_revision=len(self.events),
        )
        review = clients.browser_snapshot()
        clients.record_browser_action(
            action="accept_and_close",
            summary="Accept the isolated closeout",
            expected_phase=review["workflow"]["phase"],
            expected_event_count=review["workflow"]["event_count"],
            expected_prompt_identity=review["workflow"]["prompt_identity"],
            fields={},
            managed_po_confirmation=True,
        )
        observer = FixtureGitObserver()
        with patch("repoctl.workflow.closeout_publication.ReadOnlyGitReviewObserver", return_value=observer):
            app = create_web_app(repository_registry_path=self.registry_path, state_root=self.state_root)
            app.config["TESTING"] = True
            browser = app.test_client()
            self.assertEqual(browser.get("/closeout-publication/").status_code, 200)
            with browser.session_transaction() as browser_session:
                token = browser_session["csrf_token"]
            common = {
                "csrf_token": token,
                "repository_key": self.repository.key,
                "repository_generation": "0",
            }
            preview = {
                **common,
                "request_id": "managed-preview",
                "document_id": revision["document_id"],
                "revision_id": revision["revision_id"],
                "content_sha256": revision["content_sha256"],
            }
            self.assertEqual(browser.post("/closeout-publication/preview", data={
                **preview, "csrf_token": "invalid",
            }).status_code, 400)
            response = browser.post("/closeout-publication/preview", data=preview)
            self.assertEqual(response.status_code, 200, response.data)
            record = clients.store.get_document_publication(request_id="managed-preview")
            publish_url = f"/closeout-publication/{record['publication_id']}/publish"
            publish = {**common, "preview_fingerprint": record["preview_fingerprint"]}
            self.assertEqual(browser.post(publish_url, data=publish).status_code, 403)
            self.assertEqual(browser.post(publish_url, data={**publish, "confirm_publish": "yes"}).status_code, 200)
            self.assertEqual((self.repository_root / closeout_path).read_text(encoding="utf-8"), "# Accepted closeout\n")
            handoff = browser.get(f"/closeout-publication/{record['publication_id']}/handoff")
            self.assertEqual(handoff.status_code, 200)
            self.assertEqual(handoff.json["git_review_path"], "/git-review?view=changes")
            self.assertFalse(handoff.json["automatic_stage_or_commit"])

            observer.is_staged = True
            observer.staged_blob_sha256 = revision["content_sha256"]
            reconcile_url = f"/closeout-publication/{record['publication_id']}/reconcile"
            staged = browser.post(reconcile_url, data=common)
            self.assertEqual(staged.status_code, 200)
            self.assertEqual(staged.json["status"], "staged")
            observer.head_blob_sha256 = revision["content_sha256"]
            committed = browser.post(reconcile_url, data=common)
            self.assertEqual(committed.status_code, 200)
            self.assertEqual(committed.json["status"], "committed")
        self.assertEqual(self.workflow_path.read_bytes(), self.workflow_bytes)

    @patch("repoctl.web.app.generate_milestone_status")
    def test_managed_write_guard_pauses_browser_and_coder_cli_until_verified_recovery(self, _status) -> None:
        self.activate_fixture_configuration()
        clients = self.managed_clients()
        initial = clients.browser_snapshot()
        prompt = initial["workflow"]["unverified_prompt"]
        clients.confirm_imported_prompt(
            request_id=prompt["request_id"],
            expected_store_revision=initial["snapshot"]["store_revision"],
            document_id=prompt["document_id"],
            revision_id=prompt["revision_id"],
            content_sha256=prompt["content_sha256"],
            managed_po_confirmation=True,
        )

        code, _, error = self.invoke_managed_cli([
            "coder-test", "Fresh managed CLI write", "--source", "isolated fixture", "--result", "PASS",
        ])
        self.assertEqual(code, 0, error)
        coverage = json.loads(self.coverage_path.read_text(encoding="utf-8"))
        verified_at = datetime.fromisoformat(coverage["verified_at_utc"])
        cutoff = verified_at + timedelta(seconds=backup_guard.WRITE_GUARD_MAX_AGE_SECONDS)
        before = clients.workflow_snapshot()

        app = create_web_app(repository_registry_path=self.registry_path, state_root=self.state_root)
        app.config["TESTING"] = True
        browser = app.test_client()
        self.assertEqual(browser.get("/milestones").status_code, 200)
        with patch("repoctl.backup_guard.utc_now", return_value=cutoff):
            code, _, error = self.invoke_managed_cli([
                "coder-test", "Stale managed CLI write", "--source", "isolated fixture", "--result", "PASS",
            ])
        self.assertNotEqual(code, 0, error)
        self.assertIn("writes are paused", error)
        self.assertEqual(browser.get("/milestones").status_code, 200)
        with patch("repoctl.backup_guard.utc_now", return_value=cutoff):
            rejected = self.post_browser_workflow_event(
                browser,
                app.extensions["repoctl_information_store_clients"],
                "Stale managed browser write",
            )
        self.assertEqual(rejected.status_code, 409)
        self.assertEqual(clients.workflow_snapshot(), before)
        with patch("repoctl.backup_guard.utc_now", return_value=cutoff):
            restored_path = restore_verified_backup(
                Path(coverage["manifest_path"]),
                self.restore_root / "paused-restore.sqlite3",
                self.restore_root,
                expected_repository_id=self.identity.repository_id,
                expected_repository_key=self.identity.repository_key,
                expected_repository_root=self.identity.repository_root,
                authority_configuration=load_store_authority(self.state_root),
            )
        self.assertTrue(restored_path.is_file())
        self.assertEqual(restored_path.stat().st_mode & 0o777, 0o600)

        code, _, error = self.invoke_managed_cli([
            "coder-context", "--format", "json",
        ])
        self.assertEqual(code, 0, error)
        self.assertEqual(clients.workflow_snapshot(), before)

        self.create_fixture_recovery_point()
        refreshed = json.loads(self.coverage_path.read_text(encoding="utf-8"))
        self.assertGreater(
            datetime.fromisoformat(refreshed["verified_at_utc"]),
            verified_at,
        )
        code, _, error = self.invoke_managed_cli([
            "coder-test", "Resumed managed CLI write", "--source", "verified recovery", "--result", "PASS",
        ])
        self.assertEqual(code, 0, error)
        self.assertEqual(clients.workflow_snapshot()["event_count"], before["event_count"] + 1)

    @patch("repoctl.web.app.generate_milestone_status")
    def test_managed_write_guard_rechecks_in_flight_browser_commit(self, _status) -> None:
        self.activate_fixture_configuration()
        clients = self.managed_clients()
        initial = clients.browser_snapshot()
        prompt = initial["workflow"]["unverified_prompt"]
        clients.confirm_imported_prompt(
            request_id=prompt["request_id"],
            expected_store_revision=initial["snapshot"]["store_revision"],
            document_id=prompt["document_id"],
            revision_id=prompt["revision_id"],
            content_sha256=prompt["content_sha256"],
            managed_po_confirmation=True,
        )
        coverage = json.loads(self.coverage_path.read_text(encoding="utf-8"))
        verified_at = datetime.fromisoformat(coverage["verified_at_utc"])
        event_before = clients.workflow_snapshot()
        workflow_bytes = self.workflow_path.read_bytes()
        selection_bytes = self.selection_path.read_bytes()
        app = create_web_app(repository_registry_path=self.registry_path, state_root=self.state_root)
        app.config["TESTING"] = True
        browser = app.test_client()
        self.assertEqual(browser.get("/milestones").status_code, 200)
        with patch(
            "repoctl.backup_guard.utc_now",
            side_effect=[
                verified_at + timedelta(seconds=backup_guard.WRITE_GUARD_MAX_AGE_SECONDS - 1),
                verified_at + timedelta(seconds=backup_guard.WRITE_GUARD_MAX_AGE_SECONDS),
            ],
        ):
            rejected = self.post_browser_workflow_event(
                browser,
                app.extensions["repoctl_information_store_clients"],
                "In-flight browser write crossing the freshness cutoff",
            )
        self.assertEqual(rejected.status_code, 409)
        self.assertEqual(clients.workflow_snapshot(), event_before)
        self.assertEqual(self.workflow_path.read_bytes(), workflow_bytes)
        self.assertEqual(self.selection_path.read_bytes(), selection_bytes)

    def test_managed_write_guard_rejects_missing_invalid_and_unbound_evidence(self) -> None:
        self.activate_fixture_configuration()
        clients = self.managed_clients()
        initial = clients.browser_snapshot()
        prompt = initial["workflow"]["unverified_prompt"]
        clients.confirm_imported_prompt(
            request_id=prompt["request_id"],
            expected_store_revision=initial["snapshot"]["store_revision"],
            document_id=prompt["document_id"],
            revision_id=prompt["revision_id"],
            content_sha256=prompt["content_sha256"],
            managed_po_confirmation=True,
        )
        before = clients.workflow_snapshot()

        self.coverage_path.unlink()
        code, _, error = self.invoke_managed_cli([
            "coder-test", "Missing coverage write", "--source", "isolated fixture", "--result", "PASS",
        ])
        self.assertNotEqual(code, 0, error)
        self.assertIn("No verified recovery-point evidence", error)

        self.create_fixture_recovery_point()
        with patch("repoctl.backup_guard.utc_now", side_effect=OSError("clock unavailable")):
            code, _, error = self.invoke_managed_cli([
                "coder-test", "Unavailable clock write", "--source", "isolated fixture", "--result", "PASS",
            ])
        self.assertNotEqual(code, 0, error)
        self.assertIn("system clock could not be read safely", error)

        self.coverage_path.chmod(0o644)
        code, _, error = self.invoke_managed_cli([
            "coder-test", "Non-private coverage write", "--source", "isolated fixture", "--result", "PASS",
        ])
        self.assertNotEqual(code, 0, error)
        self.assertIn("private bounded regular file", error)

        self.coverage_path.write_text("{", encoding="utf-8")
        self.coverage_path.chmod(0o600)
        code, _, error = self.invoke_managed_cli([
            "coder-test", "Malformed coverage write", "--source", "isolated fixture", "--result", "PASS",
        ])
        self.assertNotEqual(code, 0, error)
        self.assertIn("malformed", error)

        self.create_fixture_recovery_point()
        record = json.loads(self.coverage_path.read_text(encoding="utf-8"))
        record["authority_binding"]["authority"]["generation"] = "0" * 32
        unsigned = {key: value for key, value in record.items() if key != "record_sha256"}
        record["record_sha256"] = hashlib.sha256(backup_guard.canonical_json(unsigned)).hexdigest()
        self.coverage_path.write_bytes(backup_guard.canonical_json(record) + b"\n")
        code, _, error = self.invoke_managed_cli([
            "coder-test", "Unbound coverage write", "--source", "isolated fixture", "--result", "PASS",
        ])
        self.assertNotEqual(code, 0, error)
        self.assertIn("different database or authority binding", error)
        self.assertEqual(clients.workflow_snapshot(), before)

        self.create_fixture_recovery_point()
        record = json.loads(self.coverage_path.read_text(encoding="utf-8"))
        record["verified_at_utc"] = (datetime.now(UTC) + timedelta(minutes=1)).isoformat()
        unsigned = {key: value for key, value in record.items() if key != "record_sha256"}
        record["record_sha256"] = hashlib.sha256(backup_guard.canonical_json(unsigned)).hexdigest()
        self.coverage_path.write_bytes(backup_guard.canonical_json(record) + b"\n")
        code, _, error = self.invoke_managed_cli([
            "coder-test", "Future-dated coverage write", "--source", "isolated fixture", "--result", "PASS",
        ])
        self.assertNotEqual(code, 0, error)
        self.assertIn("timestamp is in the future", error)

        self.create_fixture_recovery_point()
        code, _, error = self.invoke_managed_cli([
            "coder-test", "Recovered after evidence repair", "--source", "verified recovery", "--result", "PASS",
        ])
        self.assertEqual(code, 0, error)
        self.assertEqual(clients.workflow_snapshot()["event_count"], before["event_count"] + 1)


if __name__ == "__main__":
    unittest.main()
