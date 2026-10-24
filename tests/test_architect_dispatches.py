from __future__ import annotations

import json
import hashlib
import re
import multiprocessing
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from repoctl.scanner import core as scanner_core
from repoctl.scanner.core import run_scan
from repoctl.scanner.git_ops import ScanError
from repoctl.scanner.util import make_repository_id
from repoctl.web.app import create_web_app
from repoctl.workflow.architect_dispatches import (
    MAX_BODY_BYTES,
    MAX_DISPATCHES_PER_REPOSITORY,
    ArchitectDispatchError,
    get_dispatch,
    list_dispatches,
    list_owner_dispatches,
    submit_dispatch,
    update_dispatch_status,
)
from repoctl.workflow.coder_interface import format_coder_context_text, get_coder_context
from repoctl.workflow.lifecycle import load_milestone_workflow, record_milestone_event
from repoctl.workflow.milestones import discover_milestones, save_milestone_selection


def _git(repository: Path, *args: str) -> None:
    result = subprocess.run(
        ["git", "-C", str(repository), *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise RuntimeError(result.stderr)


def _blocked_scan_worker(repository: str, state_root: str, entered, proceed, result_queue) -> None:
    original_write = scanner_core.write_json_deterministic

    def wait_during_scan(path, payload):
        if not entered.is_set():
            entered.set()
            if not proceed.wait(10):
                raise TimeoutError("test scan barrier timed out")
        original_write(path, payload)

    try:
        with mock.patch("repoctl.scanner.core.write_json_deterministic", side_effect=wait_during_scan):
            run_scan(repository, state_root=Path(state_root))
        result_queue.put("scan-complete")
    except Exception as exc:
        result_queue.put(f"scan-error:{type(exc).__name__}:{exc}")


def _dispatch_worker(
    repository: str,
    state_root: str,
    repository_id: str,
    started,
    lock_attempted,
    finished,
) -> None:
    started.set()
    original_flock = scanner_core.fcntl.flock

    def report_lock_attempt(descriptor, operation):
        if operation == scanner_core.fcntl.LOCK_EX:
            lock_attempted.set()
        return original_flock(descriptor, operation)

    with mock.patch("repoctl.scanner.core.fcntl.flock", side_effect=report_lock_attempt):
        submit_dispatch(
            repository,
            state_root,
            repository_id,
            dispatch_type="INSTRUCTION",
            milestone_id="M003.001",
            body="Concurrent dispatch remains durable.",
        )
    finished.set()


class TestArchitectDispatches(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repository"
        self.repository.mkdir()
        _git(self.repository, "init", "-q")
        _git(self.repository, "config", "user.email", "tests@example.com")
        _git(self.repository, "config", "user.name", "Tests")
        (self.repository / "tracked.txt").write_text("tracked\n", encoding="utf-8")
        _git(self.repository, "add", "tracked.txt")
        _git(self.repository, "commit", "-qm", "initial")
        self.milestones = self.repository / "docs" / "milestones"
        self.milestones.mkdir(parents=True)
        self.prompt_path = self.milestones / "M003.001_test_milestone_prompt.md"
        self.prompt_path.write_text("# Test milestone\n", encoding="utf-8")
        self.state_root = self.root / "state"
        self.repository_id = make_repository_id(self.repository)
        self.registry_path = self.root / "central-repositories.toml"
        self.registry_path.write_text(
            "\n".join(
                [
                    'default = "test-repository"',
                    "",
                    "[repositories.test-repository]",
                    'name = "Test Repository"',
                    f"path = {json.dumps(str(self.repository))}",
                    'milestone_root = "docs/milestones"',
                    "",
                    "[repositories.test-repository.documents]",
                    'context = "docs/context/project_context.md"',
                    'architecture = "docs/context/project_architecture.md"',
                    'workflow = "docs/context/project_workflow.md"',
                    'coding_agent_rules = "docs/context/coding_agent_rules.md"',
                    'parking_lot = "docs/context/canonical_parking_lot.md"',
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        self.registry_patcher = mock.patch(
            "repoctl.workflow.coder_interface.CENTRAL_REPOSITORY_REGISTRY_PATH",
            self.registry_path,
        )
        self.registry_patcher.start()
        self.candidate = discover_milestones(self.repository)["candidates"][0]
        self.active_milestone_id = "M003.001"

    def tearDown(self) -> None:
        self.registry_patcher.stop()
        self.temporary.cleanup()

    def _submit(self, dispatch_type: str, **kwargs):
        defaults = {
            "dispatch_type": dispatch_type,
            "milestone_id": self.active_milestone_id,
            "body": "Bounded proposal content.",
        }
        defaults.update(kwargs)
        return submit_dispatch(
            self.repository,
            self.state_root,
            self.repository_id,
            **defaults,
        )

    def _start_milestone(self):
        selected = save_milestone_selection(
            self.repository,
            self.state_root,
            self.active_milestone_id,
        )
        record_milestone_event(
            self.repository,
            self.state_root,
            "docs/milestones",
            selected,
            event_type="prompt_approved",
            summary="Approve test prompt",
            actor_role="product_owner",
            po_approved=True,
        )
        record_milestone_event(
            self.repository,
            self.state_root,
            "docs/milestones",
            selected,
            event_type="implementation_started",
            summary="Start test milestone",
            actor_role="product_owner",
            po_approved=True,
        )
        return selected

    def test_all_dispatch_types_are_pending_and_storage_is_external(self):
        initial_files = {path.relative_to(self.repository) for path in self.repository.rglob("*") if path.is_file()}
        prompt = self._submit("PROMPT", milestone_id="M003.012", title="Next Milestone")
        instruction = self._submit("INSTRUCTION")
        response = self._submit(
            "RESPONSE",
            related_event_id="a" * 32,
        )

        self.assertEqual([prompt["status"], instruction["status"], response["status"]], ["PENDING"] * 3)
        self.assertEqual(prompt["slug"], "next_milestone")
        self.assertEqual(
            {path.relative_to(self.repository) for path in self.repository.rglob("*") if path.is_file()},
            initial_files,
        )
        self.assertEqual(list_dispatches(self.repository, self.state_root, self.repository_id, limit=2)[0]["dispatch_id"], response["dispatch_id"])
        self.assertTrue((self.state_root / self.repository_id / "architect_dispatches.json").is_file())

    def test_validation_and_repository_scoping(self):
        with self.assertRaises(ArchitectDispatchError):
            self._submit("PROMPT", milestone_id="../M003.012", title="Unsafe")
        with self.assertRaises(ArchitectDispatchError):
            self._submit("INSTRUCTION", body="x" * (MAX_BODY_BYTES + 1))
        with self.assertRaises(ArchitectDispatchError):
            submit_dispatch(
                self.repository,
                self.state_root,
                "another-repository",
                dispatch_type="INSTRUCTION",
                milestone_id=self.active_milestone_id,
                body="Wrong repository",
            )

    def test_retention_limit_rejects_new_dispatch_without_discarding_pending_records(self):
        submitted = [
            self._submit("INSTRUCTION", body=f"Proposal {index}")
            for index in range(MAX_DISPATCHES_PER_REPOSITORY)
        ]
        with self.assertRaises(ArchitectDispatchError):
            self._submit("INSTRUCTION", body="Must not evict pending work")
        records = list_dispatches(
            self.repository,
            self.state_root,
            self.repository_id,
            limit=5,
        )
        self.assertEqual(len(records), 5)
        self.assertTrue(all(record["status"] == "PENDING" for record in records))
        self.assertEqual(
            len(list_owner_dispatches(self.repository, self.state_root, self.repository_id)),
            len(submitted),
        )

    def test_prompt_approval_materializes_canonical_file_once_without_lifecycle_change(self):
        selected = save_milestone_selection(
            self.repository,
            self.state_root,
            self.active_milestone_id,
        )
        before = load_milestone_workflow(
            self.repository,
            self.state_root,
            "docs/milestones",
            selected,
        )
        draft = self._submit("PROMPT", milestone_id="M003.012", title="Next Milestone")
        approved = update_dispatch_status(
            self.repository,
            self.state_root,
            self.repository_id,
            draft["dispatch_id"],
            "APPROVED",
            milestone_root="docs/milestones",
            active_milestone_id=None,
        )
        destination = self.milestones / "M003.012_next_milestone_prompt.md"
        self.assertEqual(approved["status"], "APPROVED")
        self.assertEqual(approved["prompt_path"], "docs/milestones/M003.012_next_milestone_prompt.md")
        self.assertEqual(destination.read_text(encoding="utf-8"), "Bounded proposal content.\n")
        repeated = update_dispatch_status(
            self.repository,
            self.state_root,
            self.repository_id,
            draft["dispatch_id"],
            "APPROVED",
            milestone_root="docs/milestones",
            active_milestone_id=None,
        )
        self.assertEqual(repeated["prompt_path"], approved["prompt_path"])
        after = load_milestone_workflow(
            self.repository,
            self.state_root,
            "docs/milestones",
            selected,
        )
        self.assertEqual(before["event_count"], after["event_count"])

    def test_prompt_collision_and_rejection_do_not_materialize(self):
        (self.milestones / "M003.012_already_present_prompt.md").write_text("# Existing\n", encoding="utf-8")
        collision = self._submit("PROMPT", milestone_id="M003.012", title="Different")
        with self.assertRaises(ArchitectDispatchError):
            update_dispatch_status(
                self.repository,
                self.state_root,
                self.repository_id,
                collision["dispatch_id"],
                "APPROVED",
                milestone_root="docs/milestones",
                active_milestone_id=None,
            )
        self.assertEqual(get_dispatch(self.repository, self.state_root, self.repository_id, collision["dispatch_id"])["status"], "PENDING")

        rejected = self._submit("PROMPT", milestone_id="M003.013", title="Rejected")
        update_dispatch_status(
            self.repository,
            self.state_root,
            self.repository_id,
            rejected["dispatch_id"],
            "REJECTED",
            milestone_root="docs/milestones",
            active_milestone_id=None,
        )
        with self.assertRaises(ArchitectDispatchError):
            update_dispatch_status(
                self.repository,
                self.state_root,
                self.repository_id,
                rejected["dispatch_id"],
                "APPROVED",
                milestone_root="docs/milestones",
                active_milestone_id=None,
            )
        self.assertFalse((self.milestones / "M003.013_rejected_prompt.md").exists())

    def test_prompt_approval_rejects_a_milestone_root_symlink_escape(self):
        draft = self._submit("PROMPT", milestone_id="M003.014", title="Escaped")
        outside = self.root / "outside"
        outside.mkdir()
        shutil.rmtree(self.milestones)
        self.milestones.symlink_to(outside, target_is_directory=True)

        with self.assertRaises(ArchitectDispatchError):
            update_dispatch_status(
                self.repository,
                self.state_root,
                self.repository_id,
                draft["dispatch_id"],
                "APPROVED",
                milestone_root="docs/milestones",
                active_milestone_id=None,
            )
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(
            get_dispatch(self.repository, self.state_root, self.repository_id, draft["dispatch_id"])["status"],
            "PENDING",
        )

    def test_only_approved_active_dispatches_reach_coder_context(self):
        selected = self._start_milestone()
        pending = self._submit("INSTRUCTION", body="Pending guidance")
        rejected = self._submit("RESPONSE", body="Rejected response")
        update_dispatch_status(
            self.repository,
            self.state_root,
            self.repository_id,
            rejected["dispatch_id"],
            "REJECTED",
            milestone_root="docs/milestones",
            active_milestone_id=self.active_milestone_id,
        )
        before = get_coder_context(self.repository, state_root=self.state_root)
        self.assertEqual(before["approved_architect_dispatches"], [])

        approved = update_dispatch_status(
            self.repository,
            self.state_root,
            self.repository_id,
            pending["dispatch_id"],
            "APPROVED",
            milestone_root="docs/milestones",
            active_milestone_id=self.active_milestone_id,
        )
        context = get_coder_context(self.repository, state_root=self.state_root)
        formatted = format_coder_context_text(context)
        self.assertEqual(context["approved_architect_dispatches"][0]["alias"], "A1")
        self.assertEqual(context["approved_architect_dispatches"][0]["dispatch_id"], approved["dispatch_id"])
        self.assertIn("APPROVED ARCHITECT DISPATCHES", formatted)
        self.assertIn("Pending guidance", formatted)
        self.assertNotIn("Rejected response", formatted)

        other_milestone = self._submit("INSTRUCTION", milestone_id="M003.002", body="Other milestone")
        update_dispatch_status(
            self.repository,
            self.state_root,
            self.repository_id,
            other_milestone["dispatch_id"],
            "APPROVED",
            milestone_root="docs/milestones",
            active_milestone_id="M003.002",
        )
        scoped_context = get_coder_context(self.repository, state_root=self.state_root)
        self.assertEqual(
            [item["dispatch_id"] for item in scoped_context["approved_architect_dispatches"]],
            [approved["dispatch_id"]],
        )
        self.assertEqual(selected["milestone_id"], self.active_milestone_id)

    def test_dispatch_ledger_survives_scanner_publication_byte_for_byte(self):
        self._start_milestone()
        pending_prompt = self._submit(
            "PROMPT",
            milestone_id="M003.002",
            title="Next milestone",
            body="Pending prompt must remain pending.",
        )
        approved_instruction = self._submit("INSTRUCTION", body="Approved guidance survives scanning.")
        update_dispatch_status(
            self.repository,
            self.state_root,
            self.repository_id,
            approved_instruction["dispatch_id"],
            "APPROVED",
            milestone_root="docs/milestones",
            active_milestone_id=self.active_milestone_id,
        )
        rejected_response = self._submit("RESPONSE", body="Rejected response remains rejected.")
        update_dispatch_status(
            self.repository,
            self.state_root,
            self.repository_id,
            rejected_response["dispatch_id"],
            "REJECTED",
            milestone_root="docs/milestones",
            active_milestone_id=self.active_milestone_id,
        )

        ledger_path = self.state_root / self.repository_id / "architect_dispatches.json"
        original_bytes = ledger_path.read_bytes()
        original_hash = hashlib.sha256(original_bytes).hexdigest()
        original_ledger = json.loads(original_bytes)

        run_scan(str(self.repository), state_root=self.state_root)
        run_scan(str(self.repository), state_root=self.state_root)

        self.assertTrue(ledger_path.is_file(), "scanner publication must preserve the ledger")
        persisted_bytes = ledger_path.read_bytes()
        self.assertEqual(hashlib.sha256(persisted_bytes).hexdigest(), original_hash)
        self.assertEqual(persisted_bytes, original_bytes)
        self.assertEqual(json.loads(persisted_bytes), original_ledger)
        self.assertEqual(
            {item["dispatch_id"]: item["status"] for item in original_ledger["dispatches"]},
            {
                pending_prompt["dispatch_id"]: "PENDING",
                approved_instruction["dispatch_id"]: "APPROVED",
                rejected_response["dispatch_id"]: "REJECTED",
            },
        )

        context = get_coder_context(self.repository, state_root=self.state_root)
        self.assertEqual(
            [item["dispatch_id"] for item in context["approved_architect_dispatches"]],
            [approved_instruction["dispatch_id"]],
        )

    def test_scanner_fails_closed_on_invalid_dispatch_ledger(self):
        self._submit("INSTRUCTION", body="Valid starting record.")
        ledger_path = self.state_root / self.repository_id / "architect_dispatches.json"
        valid_bytes = ledger_path.read_bytes()

        invalid_ledgers = (
            b"not-json",
            json.dumps(
                {
                    "schema_version": 1,
                    "repository_id": "different-repository",
                    "dispatches": [],
                }
            ).encode("utf-8"),
            b"x" * (8 * 1024 * 1024 + 1),
        )
        for invalid in invalid_ledgers:
            with self.subTest(size=len(invalid)):
                ledger_path.write_bytes(invalid)
                with self.assertRaises(ScanError):
                    run_scan(str(self.repository), state_root=self.state_root)
                self.assertEqual(ledger_path.read_bytes(), invalid)
                self.assertFalse((self.state_root / f"{self.repository_id}.bak").exists())

        ledger_path.write_bytes(valid_bytes)
        outside = self.root / "ledger-outside.json"
        outside.write_bytes(valid_bytes)
        ledger_path.unlink()
        ledger_path.symlink_to(outside)
        with self.assertRaises(ScanError):
            run_scan(str(self.repository), state_root=self.state_root)
        self.assertTrue(ledger_path.is_symlink())
        self.assertEqual(outside.read_bytes(), valid_bytes)

        ledger_path.unlink()
        ledger_path.mkdir()
        with self.assertRaises(ScanError):
            run_scan(str(self.repository), state_root=self.state_root)
        self.assertTrue(ledger_path.is_dir())
        ledger_path.rmdir()
        ledger_path.write_bytes(valid_bytes)
        ledger_path.chmod(0o640)
        run_scan(str(self.repository), state_root=self.state_root)
        self.assertEqual(ledger_path.read_bytes(), valid_bytes)
        self.assertEqual(stat.S_IMODE(ledger_path.stat().st_mode), 0o640)

    def test_scan_failure_preserves_dispatch_ledger_and_existing_state(self):
        run_scan(str(self.repository), state_root=self.state_root)
        self._submit("INSTRUCTION", body="Keep this record on scan failure.")
        ledger_path = self.state_root / self.repository_id / "architect_dispatches.json"
        original_bytes = ledger_path.read_bytes()
        repository_path = self.state_root / self.repository_id / "repository.json"
        original_repository_bytes = repository_path.read_bytes()
        with mock.patch(
            "repoctl.scanner.core.write_json_deterministic",
            side_effect=OSError("fixture write failure"),
        ):
            with self.assertRaises(OSError):
                run_scan(str(self.repository), state_root=self.state_root)
        self.assertEqual(ledger_path.read_bytes(), original_bytes)
        self.assertEqual(repository_path.read_bytes(), original_repository_bytes)
        self.assertFalse((self.state_root / f"{self.repository_id}.bak").exists())
        self.assertFalse(any(path.name.startswith("scan-tmp-") for path in self.state_root.iterdir()))

        original_rename = Path.rename

        def fail_scan_publication(path, target):
            if path.name.startswith("scan-tmp-") and Path(target) == self.state_root / self.repository_id:
                raise OSError("fixture publication failure")
            return original_rename(path, target)

        with mock.patch.object(Path, "rename", fail_scan_publication):
            with self.assertRaisesRegex(OSError, "fixture publication failure"):
                run_scan(str(self.repository), state_root=self.state_root)
        self.assertEqual(ledger_path.read_bytes(), original_bytes)
        self.assertEqual(repository_path.read_bytes(), original_repository_bytes)
        self.assertFalse((self.state_root / f"{self.repository_id}.bak").exists())
        self.assertFalse(any(path.name.startswith("scan-tmp-") for path in self.state_root.iterdir()))

    def test_scanner_and_dispatch_submission_share_a_cross_process_lock(self):
        self._submit("INSTRUCTION", body="Original dispatch remains.")
        context = multiprocessing.get_context("fork")
        entered = context.Event()
        proceed = context.Event()
        started = context.Event()
        lock_attempted = context.Event()
        finished = context.Event()
        result_queue = context.Queue()
        scanner = context.Process(
            target=_blocked_scan_worker,
            args=(str(self.repository), str(self.state_root), entered, proceed, result_queue),
        )
        scanner.start()
        self.assertTrue(entered.wait(10), "scanner did not reach the protected publication window")

        submitter = context.Process(
            target=_dispatch_worker,
            args=(
                str(self.repository),
                str(self.state_root),
                self.repository_id,
                started,
                lock_attempted,
                finished,
            ),
        )
        submitter.start()
        self.assertTrue(started.wait(10), "dispatch submitter did not start")
        self.assertTrue(lock_attempted.wait(10), "dispatch submitter did not request the shared process lock")
        self.assertFalse(finished.wait(0.2), "dispatch submission was not serialized behind the scan")

        proceed.set()
        scanner.join(15)
        submitter.join(15)
        self.assertFalse(scanner.is_alive(), "scanner process did not finish")
        self.assertFalse(submitter.is_alive(), "dispatch process did not finish")
        self.assertEqual(scanner.exitcode, 0)
        self.assertEqual(submitter.exitcode, 0)
        self.assertEqual(result_queue.get(timeout=2), "scan-complete")
        records = list_owner_dispatches(self.repository, self.state_root, self.repository_id)
        self.assertEqual(len(records), 2)
        self.assertEqual(
            {record["body"] for record in records},
            {"Original dispatch remains.", "Concurrent dispatch remains durable."},
        )

    def test_web_submission_stays_pending_until_product_owner_approval(self):
        app = create_web_app(repository_path=str(self.repository), state_root=self.state_root)
        app.testing = True
        client = app.test_client()
        status = client.get("/api/architect/v1/status").get_json()
        payload = {
            "expected_repository_key": status["repository"]["key"],
            "expected_generation": status["repository"]["registry_generation"],
            "milestone_id": "M003.012",
            "title": "API Prompt",
            "body": "# API draft",
        }
        response = client.post("/api/architect/v1/dispatches/prompt", json=payload)
        self.assertEqual(response.status_code, 201)
        dispatch = response.get_json()["data"]["dispatch"]
        self.assertEqual(dispatch["status"], "PENDING")
        destination = self.milestones / "M003.012_api_prompt_prompt.md"
        self.assertFalse(destination.exists())
        readback_query = {
            "expected_repository_key": status["repository"]["key"],
            "expected_generation": status["repository"]["registry_generation"],
            "dispatch_id": dispatch["dispatch_id"],
        }
        self.assertEqual(
            client.get("/api/architect/v1/dispatches", query_string=readback_query)
            .get_json()["data"]["dispatches"][0]["status"],
            "PENDING",
        )
        oversized_readback = client.get(
            "/api/architect/v1/dispatches",
            query_string={**readback_query, "limit": 6},
        )
        self.assertEqual(oversized_readback.status_code, 400)
        malformed_readback = client.get(
            "/api/architect/v1/dispatches",
            query_string={**readback_query, "limit": "many"},
        )
        self.assertEqual(malformed_readback.status_code, 400)

        page = client.get("/architect-dispatches")
        self.assertEqual(page.status_code, 200)
        csrf = re.search(rb'name="csrf_token" value="([^"]+)"', page.data)
        self.assertIsNotNone(csrf)
        approve = client.post(
            f"/architect-dispatches/{dispatch['dispatch_id']}/approve",
            data={
                "csrf_token": csrf.group(1).decode(),
                "repository_key": status["repository"]["key"],
                "repository_generation": str(status["repository"]["registry_generation"]),
            },
        )
        self.assertEqual(approve.status_code, 302)
        self.assertTrue(destination.is_file())
        self.assertEqual(
            get_dispatch(self.repository, self.state_root, self.repository_id, dispatch["dispatch_id"])["status"],
            "APPROVED",
        )
        self.assertEqual(
            client.get("/api/architect/v1/dispatches", query_string=readback_query)
            .get_json()["data"]["dispatches"][0]["status"],
            "APPROVED",
        )

    def test_active_instruction_approval_is_read_back_without_lifecycle_change(self):
        selected = self._start_milestone()
        app = create_web_app(repository_path=str(self.repository), state_root=self.state_root)
        app.testing = True
        client = app.test_client()
        status = client.get("/api/architect/v1/status").get_json()
        preconditions = {
            "expected_repository_key": status["repository"]["key"],
            "expected_generation": status["repository"]["registry_generation"],
            "milestone_id": self.active_milestone_id,
        }
        instruction_response = client.post(
            "/api/architect/v1/dispatches/instruction",
            json={**preconditions, "body": "Approved instruction"},
        )
        self.assertEqual(instruction_response.status_code, 201)
        instruction = instruction_response.get_json()["data"]["dispatch"]
        before_context = get_coder_context(self.repository, state_root=self.state_root)
        self.assertEqual(before_context["approved_architect_dispatches"], [])

        page = client.get("/architect-dispatches")
        csrf = re.search(rb'name="csrf_token" value="([^"]+)"', page.data)
        self.assertIsNotNone(csrf)
        approved = client.post(
            f"/architect-dispatches/{instruction['dispatch_id']}/approve",
            data={
                "csrf_token": csrf.group(1).decode(),
                "repository_key": preconditions["expected_repository_key"],
                "repository_generation": str(preconditions["expected_generation"]),
            },
        )
        self.assertEqual(approved.status_code, 302)

        context = get_coder_context(self.repository, state_root=self.state_root)
        after = load_milestone_workflow(
            self.repository,
            self.state_root,
            "docs/milestones",
            selected,
        )
        self.assertEqual(context["milestone"]["phase"], "IN_PROGRESS")
        self.assertEqual(after["event_count"], 2)
        self.assertEqual(context["approved_architect_dispatches"][0]["body"], "Approved instruction")
        readback = client.get(
            "/api/architect/v1/dispatches",
            query_string={
                **preconditions,
                "dispatch_id": instruction["dispatch_id"],
            },
        )
        self.assertEqual(readback.status_code, 200)
        self.assertEqual(
            readback.get_json()["data"]["dispatches"][0]["status"],
            "APPROVED",
        )

        workflow = load_milestone_workflow(
            self.repository,
            self.state_root,
            "docs/milestones",
            selected,
        )
        related_event_id = workflow["event_history"][-1]["event_id"]
        response = client.post(
            "/api/architect/v1/dispatches/response",
            json={
                **preconditions,
                "body": "Bounded response",
                "related_event_id": related_event_id,
            },
        )
        self.assertEqual(response.status_code, 201)
        invalid_related = client.post(
            "/api/architect/v1/dispatches/response",
            json={
                **preconditions,
                "body": "Invalid related event",
                "related_event_id": "f" * 32,
            },
        )
        self.assertEqual(invalid_related.status_code, 400)


if __name__ == "__main__":
    unittest.main()
