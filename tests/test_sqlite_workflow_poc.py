from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import resource
import sqlite3
import statistics
import tempfile
import time
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from repoctl.workflow import lifecycle
from repoctl.workflow.context_verification import make_verification_record
from repoctl.workflow.lifecycle import WorkflowRecordError

import poc.m010_002_sqlite_workflow as workflow_poc
from poc.m010_002_sqlite_workflow import (
    CursorConflict,
    IntegrityFailure,
    IdempotencyConflict,
    PocError,
    PromptIdentityConflict,
    RevisionConflict,
    SQLiteWorkflowPoc,
    WorkflowIdentity,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
STATE_ROOT = Path("~/.local/share/repoctl").expanduser().resolve()
FIXTURE_IDENTITY = WorkflowIdentity(
    repository_id="fixture-repository",
    milestone_id="MPOC.001",
    prompt_path="fixtures/prompt.md",
    prompt_sha256=hashlib.sha256(b"disposable POC prompt").hexdigest(),
)
FIXTURE_START = datetime(2026, 1, 1, tzinfo=UTC)
WRITER_COUNT = 8
WRITER_EVENTS_PER_PROCESS = 250


def _temporary_root(test: unittest.TestCase) -> Path:
    temporary = tempfile.TemporaryDirectory(prefix="m010_002_sqlite_", dir="/tmp")
    test.addCleanup(temporary.cleanup)
    root = Path(temporary.name).resolve()
    if root.is_relative_to(REPOSITORY_ROOT) or root.is_relative_to(STATE_ROOT):
        raise AssertionError(f"POC temporary path is inside a managed root: {root}")
    return root


def _event(
    events: list[dict],
    *,
    event_type: str,
    summary: str,
    actor_role: str,
    po_approved: bool = False,
    event_fields: dict | None = None,
) -> dict:
    if events:
        previous = events[-1]
        phase = previous["phase_after"]
        waiting_category = previous["waiting_category_after"]
        waiting_reason = previous["waiting_reason_after"]
    else:
        phase = "PLANNING"
        waiting_category = None
        waiting_reason = None
    event = lifecycle._new_event(
        event_type=event_type,
        summary=summary,
        actor_role=actor_role,
        po_approved=po_approved,
        phase=phase,
        waiting_category=waiting_category,
        waiting_reason=waiting_reason,
        event_fields=event_fields or {},
    )
    event["event_id"] = f"{len(events) + 1:032x}"
    event["timestamp"] = (FIXTURE_START + timedelta(seconds=len(events))).isoformat()
    phase_after, category_after, reason_after = lifecycle._apply_event(
        phase,
        waiting_category,
        waiting_reason,
        event,
        events,
    )
    event.update(
        phase_after=phase_after,
        waiting_category_after=category_after,
        waiting_reason_after=reason_after,
    )
    lifecycle._validate_event_shape(event)
    events.append(event)
    return event


def _closeout_history() -> list[dict]:
    events: list[dict] = []
    _event(
        events,
        event_type="prompt_approved",
        summary="Approve fixture prompt",
        actor_role="product_owner",
        po_approved=True,
    )
    _event(
        events,
        event_type="implementation_started",
        summary="Start fixture work",
        actor_role="coder",
    )
    question = _event(
        events,
        event_type="question_submitted",
        summary="Fixture blocking question",
        actor_role="coder",
        event_fields={
            "waiting_category": "blocker",
            "waiting_reason": "Resolve the fixture question",
        },
    )
    _event(
        events,
        event_type="answer_recorded",
        summary="Answer fixture question",
        actor_role="product_owner",
        po_approved=True,
        event_fields={"related_event_id": question["event_id"]},
    )
    _event(
        events,
        event_type="implementation_resumed",
        summary="Resume fixture work",
        actor_role="coder",
    )
    bug = _event(
        events,
        event_type="bug_discovered",
        summary="Fixture bug",
        actor_role="coder",
    )
    _event(
        events,
        event_type="bug_classified",
        summary="Classify fixture bug in scope",
        actor_role="product_owner",
        po_approved=True,
        event_fields={
            "related_event_id": bug["event_id"],
            "bug_scope": "in_scope",
        },
    )
    _event(
        events,
        event_type="bug_fixed",
        summary="Fix fixture bug",
        actor_role="coder",
        event_fields={"related_event_id": bug["event_id"]},
    )
    _event(
        events,
        event_type="decision_recorded",
        summary="Non-binding fixture decision",
        actor_role="architect",
        event_fields={"binding": False},
    )
    _event(
        events,
        event_type="test_result_recorded",
        summary="Record fixture result",
        actor_role="coder",
        event_fields={"evidence_source": "fixture", "result": "PASS"},
    )
    _event(
        events,
        event_type="closeout_submitted",
        summary="Submit fixture closeout",
        actor_role="coder",
        event_fields={"evidence_path": "fixtures/closeout.md"},
    )
    _event(
        events,
        event_type="acceptance_recorded",
        summary="Accept fixture closeout",
        actor_role="product_owner",
        po_approved=True,
        event_fields={"binding": True},
    )
    _event(
        events,
        event_type="milestone_closed",
        summary="Close fixture milestone",
        actor_role="product_owner",
        po_approved=True,
        event_fields={"binding": True},
    )
    return events


def _all_event_types_history() -> list[dict]:
    events: list[dict] = []
    _event(
        events,
        event_type="prompt_approved",
        summary="Approve fixture prompt",
        actor_role="product_owner",
        po_approved=True,
        event_fields={"binding": True},
    )
    _event(events, event_type="implementation_started", summary="Start fixture work", actor_role="coder")
    question = _event(
        events,
        event_type="question_submitted",
        summary="Open fixture question",
        actor_role="coder",
        event_fields={
            "waiting_category": "blocker",
            "waiting_reason": "Resolve the fixture question",
        },
    )
    _event(
        events,
        event_type="answer_recorded",
        summary="Answer fixture question",
        actor_role="product_owner",
        po_approved=True,
        event_fields={"related_event_id": question["event_id"]},
    )
    _event(events, event_type="implementation_resumed", summary="Resume after fixture answer", actor_role="coder")
    _event(
        events,
        event_type="decision_recorded",
        summary="Record non-binding decision",
        actor_role="architect",
        event_fields={"binding": False},
    )
    _event(
        events,
        event_type="lock_in_approved",
        summary="Approve fixture lock-in",
        actor_role="product_owner",
        po_approved=True,
        event_fields={"binding": True},
    )
    _event(
        events,
        event_type="escalation_opened",
        summary="Open fixture escalation",
        actor_role="coder",
        event_fields={
            "waiting_category": "escalation",
            "waiting_reason": "Resolve the fixture escalation",
        },
    )
    _event(events, event_type="escalation_resolved", summary="Resolve fixture escalation", actor_role="product_owner")
    _event(events, event_type="implementation_resumed", summary="Resume fixture work", actor_role="coder")
    bug = _event(events, event_type="bug_discovered", summary="Discover fixture bug", actor_role="coder")
    _event(
        events,
        event_type="bug_classified",
        summary="Classify fixture bug",
        actor_role="product_owner",
        po_approved=True,
        event_fields={"related_event_id": bug["event_id"], "bug_scope": "in_scope"},
    )
    _event(
        events,
        event_type="bug_fixed",
        summary="Fix fixture bug",
        actor_role="coder",
        event_fields={"related_event_id": bug["event_id"]},
    )
    _event(
        events,
        event_type="test_result_recorded",
        summary="Record fixture test result",
        actor_role="coder",
        event_fields={"evidence_source": "fixture test", "result": "passed"},
    )
    _event(
        events,
        event_type="closeout_submitted",
        summary="Submit fixture closeout",
        actor_role="coder",
        event_fields={"evidence_path": "fixtures/closeout.md"},
    )
    _event(events, event_type="review_requested", summary="Request fixture review", actor_role="coder")
    _event(events, event_type="correction_requested", summary="Request fixture correction", actor_role="product_owner")
    _event(
        events,
        event_type="closeout_submitted",
        summary="Resubmit fixture closeout",
        actor_role="coder",
        event_fields={"evidence_path": "fixtures/closeout.md"},
    )
    _event(
        events,
        event_type="acceptance_recorded",
        summary="Accept fixture closeout",
        actor_role="product_owner",
        po_approved=True,
        event_fields={"binding": True},
    )
    _event(
        events,
        event_type="milestone_closed",
        summary="Close fixture milestone",
        actor_role="product_owner",
        po_approved=True,
        event_fields={"binding": True},
    )
    _event(
        events,
        event_type="milestone_reopened",
        summary="Reopen fixture milestone",
        actor_role="product_owner",
        po_approved=True,
        event_fields={"reopen_target_phase": "IN_PROGRESS"},
    )
    _event(events, event_type="git_disposition_recorded", summary="Record fixture Git disposition", actor_role="coder")

    snapshot = {
        "repository": {"key": "fixture", "id": FIXTURE_IDENTITY.repository_id, "root": "/tmp/fixture"},
        "milestone": {
            "id": FIXTURE_IDENTITY.milestone_id,
            "prompt_path": FIXTURE_IDENTITY.prompt_path,
            "prompt_sha256": FIXTURE_IDENTITY.prompt_sha256,
            "lifecycle": "IN_PROGRESS",
            "authorization": {"authorized": True, "status": "AUTHORIZED"},
            "closeout": {"exists": True, "path": "fixtures/closeout.md"},
        },
        "workflow_evidence": {"test_result_count": 1},
        "approved_architect_dispatches": {"inspection_status": "available"},
        "procedure": {"inspection_status": "available", "status": "CURRENT", "overlay_valid": True},
        "git": {"inspection_status": "available"},
    }
    verification = make_verification_record(
        trigger="NEW",
        snapshot=snapshot,
        result="VERIFIED",
        git_boundary_acknowledged=True,
        runtime_compatibility={
            "status": "COMPATIBLE",
            "runtime_instance_id": "b" * 32,
            "producer_contract_id": "repoctl-workflow-event-writer:v1",
            "event_type": "context_verification_recorded",
            "required_event_capabilities": {
                "context_verification_record_schema_version": 2,
            },
            "ledger_schema_version": 1,
            "event_schema_version": 1,
            "context_verification_record_schema_version": 2,
        },
    )
    _event(
        events,
        event_type=lifecycle.VERIFICATION_EVENT_TYPE,
        summary="Record fixture context verification",
        actor_role="coder",
        event_fields={"context_verification": verification},
    )
    return events


def _decision_events(count: int, *, padding: int = 0, id_offset: int = 0) -> list[dict]:
    events = []
    for index in range(count):
        event = lifecycle._new_event(
            event_type="decision_recorded",
            summary=f"Characterization event {index + 1} " + ("x" * min(padding, 1500)),
            actor_role="coder",
            po_approved=False,
            phase="IN_PROGRESS",
            waiting_category=None,
            waiting_reason=None,
            event_fields={
                "binding": False,
                **(
                    {"fixture_padding": "x" * (padding - 1500)}
                    if padding > 1500
                    else {}
                ),
            },
        )
        event["event_id"] = f"{id_offset + index + 1:032x}"
        event["timestamp"] = (FIXTURE_START + timedelta(seconds=id_offset + index)).isoformat()
        lifecycle._validate_event_shape(event)
        events.append(event)
    return events


def _serialized_event_bytes(events: list[dict]) -> int:
    return sum(
        len(json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        for event in events
    )


def _resource_metrics() -> dict:
    descriptor_directory = Path("/proc/self/fd")
    status_path = Path("/proc/self/status")
    resident_kib = None
    if status_path.exists():
        for line in status_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("VmRSS:"):
                resident_kib = int(line.split()[1])
                break
    return {
        "max_rss_platform_units": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "current_rss_kib": resident_kib,
        "open_fd_count": len(list(descriptor_directory.iterdir())) if descriptor_directory.exists() else None,
    }


def _worker(
    database_path: str,
    identity: WorkflowIdentity,
    worker_number: int,
    barrier,
    results,
) -> None:
    store = SQLiteWorkflowPoc(database_path, identity, initialize=False)
    acknowledged = 0
    rejected = 0
    busy_errors = 0
    attempts_used = 0
    try:
        barrier.wait(timeout=10)
        for index in range(WRITER_EVENTS_PER_PROCESS):
            event = lifecycle._new_event(
                event_type="decision_recorded",
                summary=f"Worker {worker_number} decision {index}",
                actor_role="coder",
                po_approved=False,
                phase="IN_PROGRESS",
                waiting_category=None,
                waiting_reason=None,
                event_fields={"binding": False},
            )
            event["event_id"] = f"{worker_number + 1:08x}{index + 1:024x}"
            event["timestamp"] = (FIXTURE_START + timedelta(seconds=100)).isoformat()
            lifecycle._validate_event_shape(event)
            transaction_id = f"worker-{worker_number}-{index}"
            revision = store.snapshot()["revision"]
            for attempt in range(5000):
                attempts_used += 1
                try:
                    result = store.append(
                        transaction_id=transaction_id,
                        expected_revision=revision,
                        prompt_sha256=identity.prompt_sha256,
                        events=[event],
                    )
                    retry = store.append(
                        transaction_id=transaction_id,
                        expected_revision=revision,
                        prompt_sha256=identity.prompt_sha256,
                        events=[event],
                    )
                    if retry != result:
                        raise AssertionError("exact transaction retry returned a different result")
                    acknowledged += 1
                    break
                except RevisionConflict:
                    rejected += 1
                    revision = store.snapshot()["revision"]
                    time.sleep(0.01)
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc).lower():
                        raise
                    busy_errors += 1
                    time.sleep(0.005)
            else:
                raise AssertionError("bounded stale-revision retry count was exhausted")
        results.put(
            {
                "worker": worker_number,
                "acknowledged": acknowledged,
                "rejected": rejected,
                "duplicates": 0,
                "busy_errors": busy_errors,
                "attempts": attempts_used,
                "errors": 0,
            }
        )
    except Exception as exc:
        results.put(
            {
                "worker": worker_number,
                "acknowledged": acknowledged,
                "rejected": rejected,
                "duplicates": 0,
                "busy_errors": busy_errors,
                "attempts": attempts_used,
                "errors": 1,
                "error": repr(exc),
            }
        )
        raise


def _precommit_interruption_worker(
    database_path: str,
    identity: WorkflowIdentity,
    event: dict,
    entered_projection,
) -> None:
    store = SQLiteWorkflowPoc(database_path, identity, initialize=False)
    write_projection = store._write_projection

    def pause_before_commit(connection, expected_revision, new_revision, events, projection) -> None:
        write_projection(connection, expected_revision, new_revision, events, projection)
        entered_projection.set()
        time.sleep(60)

    store._write_projection = pause_before_commit
    store.append(
        transaction_id="precommit-interruption",
        expected_revision=0,
        prompt_sha256=identity.prompt_sha256,
        events=[event],
    )


def _postcommit_interruption_worker(
    database_path: str,
    identity: WorkflowIdentity,
    event: dict,
    committed,
) -> None:
    store = SQLiteWorkflowPoc(database_path, identity, initialize=False)
    store.append(
        transaction_id="postcommit-interruption",
        expected_revision=1,
        prompt_sha256=identity.prompt_sha256,
        events=[event],
    )
    committed.set()
    os._exit(73)


def _percentiles(samples_ms: list[float]) -> dict[str, float]:
    ordered = sorted(samples_ms)
    p95_index = max(0, (95 * len(ordered) + 99) // 100 - 1)
    return {
        "p50_ms": round(statistics.median(ordered), 3),
        "p95_ms": round(ordered[p95_index], 3),
        "max_ms": round(ordered[-1], 3),
    }


class SQLiteWorkflowPocTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = _temporary_root(self)
        self.database_path = self.root / "workflow.sqlite3"
        self.store = SQLiteWorkflowPoc(self.database_path, FIXTURE_IDENTITY)

    def test_history_projection_and_small_append_page_characterization(self) -> None:
        events = _closeout_history()
        groups = (
            (0, 2),
            (2, 3),
            (3, 5),
            (5, 8),
            (8, 13),
        )
        append_samples: list[float] = []
        revision = 0
        question_id = events[2]["event_id"]
        for group_number, (start, stop) in enumerate(groups):
            before = time.perf_counter()
            self.store.append(
                transaction_id=f"fixture-group-{group_number}",
                expected_revision=revision,
                prompt_sha256=FIXTURE_IDENTITY.prompt_sha256,
                events=events[start:stop],
            )
            append_samples.append((time.perf_counter() - before) * 1000)
            revision += 1
            if group_number == 1:
                waiting = self.store.snapshot()["projection"]
                self.assertEqual(waiting["phase"], "WAITING")
                self.assertEqual(waiting["open_question_ids"], [question_id])
                self.assertEqual(waiting["waiting_category"], "blocker")
            if group_number == 2:
                resumed = self.store.snapshot()["projection"]
                self.assertEqual(resumed["phase"], "IN_PROGRESS")
                self.assertEqual(resumed["open_question_ids"], [])
                self.assertIsNone(resumed["waiting_reason"])
            if group_number == 3:
                bugs = self.store.snapshot()["projection"]
                self.assertEqual(bugs["open_bug_ids"], [])
                self.assertEqual(bugs["unfixed_bug_ids"], [])

        snapshot = self.store.snapshot()
        before_page = time.perf_counter()
        page = self.store.page(
            snapshot_revision=snapshot["revision"],
            snapshot_event_count=snapshot["event_count"],
            after_sequence=0,
            limit=100,
        )
        page_latency_ms = (time.perf_counter() - before_page) * 1000
        self.assertEqual(snapshot["revision"], 5)
        self.assertEqual(snapshot["event_count"], len(events))
        self.assertEqual(snapshot["events"], events)
        self.assertEqual(snapshot["projection"]["phase"], "CLOSED")
        self.assertEqual(snapshot["projection"]["open_question_ids"], [])
        self.assertEqual(snapshot["projection"]["open_bug_ids"], [])
        self.assertEqual(page["revision"], 5)
        self.assertEqual([item["event_id"] for item in page["events"]], [
            event["event_id"] for event in events
        ])
        self.assertEqual(self.store.audit()["projection"], snapshot["projection"])

        files = {
            path.name: path.stat().st_size
            for path in sorted(self.root.iterdir())
            if path.is_file()
        }
        metrics = {
            "transaction_event_batch_sizes": [stop - start for start, stop in groups],
            "append": _percentiles(append_samples),
            "single_page_ms": round(page_latency_ms, 3),
            "files_and_bytes": files,
            "settings": self.store.settings(),
        }
        print(f"PHASE_A_CHARACTERIZATION {json.dumps(metrics, sort_keys=True)}")

    def test_projection_and_event_rows_roll_back_together(self) -> None:
        events = _closeout_history()[:2]
        with patch.object(
            self.store,
            "_write_projection",
            side_effect=RuntimeError("injected failure after event inserts"),
        ):
            with self.assertRaisesRegex(RuntimeError, "injected failure"):
                self.store.append(
                    transaction_id="rollback-fixture",
                    expected_revision=0,
                    prompt_sha256=FIXTURE_IDENTITY.prompt_sha256,
                    events=events,
                )
        snapshot = self.store.snapshot()
        self.assertEqual(snapshot["revision"], 0)
        self.assertEqual(snapshot["event_count"], 0)
        self.assertEqual(snapshot["projection"]["phase"], "PLANNING")
        self.assertEqual(snapshot["events"], [])

    def test_revision_and_idempotency_conflicts_are_explicit(self) -> None:
        events = _closeout_history()
        result = self.store.append(
            transaction_id="start-fixture",
            expected_revision=0,
            prompt_sha256=FIXTURE_IDENTITY.prompt_sha256,
            events=events[:2],
        )
        retry = self.store.append(
            transaction_id="start-fixture",
            expected_revision=0,
            prompt_sha256=FIXTURE_IDENTITY.prompt_sha256,
            events=events[:2],
        )
        self.assertEqual(retry, result)

        changed_event = dict(events[1], summary="Changed request")
        with self.assertRaises(IdempotencyConflict):
            self.store.append(
                transaction_id="start-fixture",
                expected_revision=0,
                prompt_sha256=FIXTURE_IDENTITY.prompt_sha256,
                events=[events[0], changed_event],
            )

        with self.assertRaises(RevisionConflict):
            self.store.append(
                transaction_id="stale-fixture",
                expected_revision=0,
                prompt_sha256=FIXTURE_IDENTITY.prompt_sha256,
                events=[events[2]],
            )
        with self.assertRaises(PromptIdentityConflict):
            self.store.append(
                transaction_id="wrong-prompt",
                expected_revision=1,
                prompt_sha256="0" * 64,
                events=[events[2]],
            )
        with self.assertRaises(WorkflowRecordError):
            self.store.append(
                transaction_id="duplicate-event-id",
                expected_revision=1,
                prompt_sha256=FIXTURE_IDENTITY.prompt_sha256,
                events=[events[0]],
            )
        snapshot = self.store.snapshot()
        self.assertEqual(snapshot["revision"], 1)
        self.assertEqual(snapshot["event_count"], 2)
        self.assertEqual(self.store.audit()["receipt_count"], 1)

    def test_all_event_types_and_six_phases_match_lifecycle_replay(self) -> None:
        events = _all_event_types_history()
        self.assertEqual({event["event_type"] for event in events}, lifecycle._EVENT_TYPES)
        observed_phases = {
            phase
            for event in events
            for phase in (event["phase_before"], event["phase_after"])
        }
        self.assertEqual(
            observed_phases,
            {"PLANNING", "READY", "IN_PROGRESS", "WAITING", "REVIEW", "CLOSED"},
        )

        self.store.append(
            transaction_id="all-event-types",
            expected_revision=0,
            prompt_sha256=FIXTURE_IDENTITY.prompt_sha256,
            events=events,
        )
        snapshot = self.store.snapshot()
        expected_state = lifecycle._reduce_events(events)
        self.assertEqual(snapshot["events"], events)
        self.assertEqual(snapshot["projection"]["phase"], expected_state["phase"])
        self.assertEqual(snapshot["projection"]["phase"], "IN_PROGRESS")
        self.assertEqual(snapshot["projection"]["event_count"], len(events))
        self.assertEqual(self.store.audit()["projection"], snapshot["projection"])

    def test_2001_events_over_five_megabytes_and_active_wal_backup_restore(self) -> None:
        events = _closeout_history()[:2] + _decision_events(1999, padding=2600, id_offset=2)
        serialized_bytes = _serialized_event_bytes(events)
        self.assertGreater(serialized_bytes, 5_000_000)

        total_started = time.perf_counter()
        keeper = sqlite3.connect(self.database_path)
        try:
            self.assertEqual(
                keeper.execute("PRAGMA journal_mode").fetchone()[0].lower(),
                "wal",
            )
            append_started = time.perf_counter()
            self.store.append(
                transaction_id="over-v1-limits",
                expected_revision=0,
                prompt_sha256=FIXTURE_IDENTITY.prompt_sha256,
                events=events,
            )
            append_ms = (time.perf_counter() - append_started) * 1000
            snapshot = self.store.snapshot()
            self.assertEqual(snapshot["event_count"], 2001)
            self.assertEqual(snapshot["events"], events)

            source_paths = [
                self.database_path,
                Path(f"{self.database_path}-wal"),
                Path(f"{self.database_path}-shm"),
            ]
            source_files = {
                path.name: path.stat().st_size
                for path in source_paths
                if path.exists()
            }
            self.assertTrue(any(name.endswith("-wal") for name in source_files))

            restore_directory = self.root / "restore"
            restore_directory.mkdir()
            restored_path = restore_directory / "workflow.sqlite3"
            destination = sqlite3.connect(restored_path)
            backup_started = time.perf_counter()
            keeper.backup(destination)
            backup_ms = (time.perf_counter() - backup_started) * 1000
            integrity = destination.execute("PRAGMA integrity_check").fetchone()[0]
            destination.close()
            self.assertEqual(integrity, "ok")

            restored = SQLiteWorkflowPoc(restored_path, FIXTURE_IDENTITY, initialize=False)
            restored_audit = restored.audit()
            self.assertEqual(restored_audit["event_count"], 2001)
            self.assertEqual(restored_audit["projection"], snapshot["projection"])
            self.assertEqual(restored.snapshot()["events"], events)

            metrics = {
                "events": len(events),
                "serialized_event_bytes": serialized_bytes,
                "append_ms": round(append_ms, 3),
                "backup_ms": round(backup_ms, 3),
                "total_elapsed_ms": round((time.perf_counter() - total_started) * 1000, 3),
                "source_files": source_files,
                "source_file_count": len(source_files),
                "restored_db_bytes": restored_path.stat().st_size,
                "restored_file_count": len(list(restore_directory.iterdir())),
                "restored_integrity": integrity,
                "resource": _resource_metrics(),
            }
            print(f"PHASE_B_2001_AND_BACKUP {json.dumps(metrics, sort_keys=True)}")
        finally:
            keeper.close()

    def test_10000_event_workload_pages_and_audits_losslessly(self) -> None:
        events = _closeout_history()[:2] + _decision_events(9998, padding=600, id_offset=2)
        serialized_bytes = _serialized_event_bytes(events)
        self.assertGreater(serialized_bytes, 5_000_000)

        total_started = time.perf_counter()
        keeper = sqlite3.connect(self.database_path)
        self.assertEqual(keeper.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
        append_samples: list[float] = []
        try:
            for group_number in range(10):
                group = events[group_number * 1000 : (group_number + 1) * 1000]
                started = time.perf_counter()
                self.store.append(
                    transaction_id=f"ten-thousand-{group_number}",
                    expected_revision=group_number,
                    prompt_sha256=FIXTURE_IDENTITY.prompt_sha256,
                    events=group,
                )
                append_samples.append((time.perf_counter() - started) * 1000)

            snapshot = self.store.snapshot()
            audit_started = time.perf_counter()
            audit = self.store.audit()
            audit_ms = (time.perf_counter() - audit_started) * 1000
            self.assertEqual(snapshot["event_count"], 10_000)
            self.assertEqual(snapshot["events"], events)
            self.assertEqual(audit["projection"], snapshot["projection"])
            self.assertEqual(audit["payload_hashes_verified"], 10_000)

            paged_events = []
            page_samples: list[float] = []
            after_sequence = 0
            while after_sequence < snapshot["event_count"]:
                page_started = time.perf_counter()
                page = self.store.page(
                    snapshot_revision=snapshot["revision"],
                    snapshot_event_count=snapshot["event_count"],
                    after_sequence=after_sequence,
                    limit=100,
                )
                page_samples.append((time.perf_counter() - page_started) * 1000)
                self.assertEqual(len(page["events"]), 100)
                self.assertGreater(page["next_sequence"], after_sequence)
                paged_events.extend(page["events"])
                after_sequence = page["next_sequence"]
            self.assertEqual(
                [event["event_id"] for event in paged_events],
                [event["event_id"] for event in events],
            )

            checkpoint_started = time.perf_counter()
            checkpoint = keeper.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
            checkpoint_ms = (time.perf_counter() - checkpoint_started) * 1000
            file_sizes = {
                path.name: path.stat().st_size
                for path in (
                    self.database_path,
                    Path(f"{self.database_path}-wal"),
                    Path(f"{self.database_path}-shm"),
                )
                if path.exists()
            }
            metrics = {
                "events": len(events),
                "transactions": len(append_samples),
                "batch_size": 1000,
                "serialized_event_bytes": serialized_bytes,
                "append_ms": _percentiles(append_samples),
                "audit_ms": round(audit_ms, 3),
                "page_100_ms": _percentiles(page_samples),
                "checkpoint": {
                    "busy": checkpoint[0],
                    "log_frames": checkpoint[1],
                    "checkpointed_frames": checkpoint[2],
                    "elapsed_ms": round(checkpoint_ms, 3),
                },
                "file_sizes": file_sizes,
                "file_count": len(file_sizes),
                "total_elapsed_ms": round((time.perf_counter() - total_started) * 1000, 3),
                "resource": _resource_metrics(),
            }
            print(f"PHASE_B_10000 {json.dumps(metrics, sort_keys=True)}")
        finally:
            keeper.close()

    def test_snapshot_pagination_excludes_later_appends(self) -> None:
        initial = _closeout_history()[:2] + _decision_events(248, id_offset=2)
        later = _decision_events(25, id_offset=250)
        self.store.append(
            transaction_id="page-snapshot",
            expected_revision=0,
            prompt_sha256=FIXTURE_IDENTITY.prompt_sha256,
            events=initial,
        )
        snapshot = self.store.snapshot()
        self.store.append(
            transaction_id="page-later-append",
            expected_revision=snapshot["revision"],
            prompt_sha256=FIXTURE_IDENTITY.prompt_sha256,
            events=later,
        )

        paged_events = []
        latencies_ms = []
        after_sequence = 0
        while after_sequence < snapshot["event_count"]:
            started = time.perf_counter()
            page = self.store.page(
                snapshot_revision=snapshot["revision"],
                snapshot_event_count=snapshot["event_count"],
                after_sequence=after_sequence,
                limit=100,
            )
            latencies_ms.append((time.perf_counter() - started) * 1000)
            paged_events.extend(page["events"])
            after_sequence = page["next_sequence"]
        self.assertEqual(
            [event["event_id"] for event in paged_events],
            [event["event_id"] for event in initial],
        )
        with self.assertRaises(CursorConflict):
            self.store.page(
                snapshot_revision=snapshot["revision"],
                snapshot_event_count=snapshot["event_count"] + 1,
            )
        with self.assertRaises(CursorConflict):
            self.store.page(
                snapshot_revision=snapshot["revision"],
                snapshot_event_count=snapshot["event_count"],
                after_sequence=snapshot["event_count"] + 1,
            )
        print(
            "PHASE_B_SNAPSHOT_PAGING "
            + json.dumps(
                {
                    "snapshot_events": snapshot["event_count"],
                    "events_appended_after_snapshot": len(later),
                    "returned_once": len(paged_events),
                    "page_sizes": [100, 100, 50],
                    "page_ms": _percentiles(latencies_ms),
                },
                sort_keys=True,
            )
        )

    def test_audit_detects_payload_sequence_hash_and_projection_tampering(self) -> None:
        events = _closeout_history()[:2] + _decision_events(1, id_offset=2)
        self.store.append(
            transaction_id="tamper-source",
            expected_revision=0,
            prompt_sha256=FIXTURE_IDENTITY.prompt_sha256,
            events=events,
        )
        tamper_operations = {
            "payload": (
                "UPDATE workflow_event SET payload_json = ? WHERE sequence = 1",
                (json.dumps(dict(events[0], summary="tampered"), sort_keys=True, separators=(",", ":")),),
            ),
            "sequence": (
                "UPDATE workflow_event SET sequence = 99 WHERE sequence = 2",
                (),
            ),
            "hash": (
                "UPDATE workflow_event SET payload_sha256 = ? WHERE sequence = 1",
                ("0" * 64,),
            ),
            "projection": (
                "UPDATE milestone_state SET projection_json = ?",
                ("{}",),
            ),
        }

        for name, (statement, parameters) in tamper_operations.items():
            with self.subTest(tampering=name):
                tampered_path = self.root / f"{name}.sqlite3"
                source = sqlite3.connect(self.database_path)
                destination = sqlite3.connect(tampered_path)
                source.backup(destination)
                destination.close()
                source.close()
                connection = sqlite3.connect(tampered_path)
                connection.execute("DROP TRIGGER IF EXISTS workflow_event_no_update")
                connection.execute(statement, parameters)
                connection.commit()
                connection.close()

                tampered = SQLiteWorkflowPoc(tampered_path, FIXTURE_IDENTITY, initialize=False)
                with self.assertRaises(IntegrityFailure):
                    tampered.audit()

    def test_precommit_and_postcommit_process_interruptions_are_recoverable(self) -> None:
        context = multiprocessing.get_context("fork")
        initial_events = _closeout_history()[:2]
        first_event = initial_events[0]
        entered_projection = context.Event()
        before_commit = context.Process(
            target=_precommit_interruption_worker,
            args=(str(self.database_path), FIXTURE_IDENTITY, first_event, entered_projection),
        )
        before_commit.start()
        try:
            self.assertTrue(entered_projection.wait(timeout=20))
            before_commit.terminate()
            before_commit.join(timeout=10)
        finally:
            if before_commit.is_alive():
                before_commit.terminate()
                before_commit.join(timeout=5)
        self.assertFalse(before_commit.is_alive())
        self.assertEqual(self.store.snapshot()["event_count"], 0)

        first_result = self.store.append(
            transaction_id="precommit-interruption",
            expected_revision=0,
            prompt_sha256=FIXTURE_IDENTITY.prompt_sha256,
            events=[first_event],
        )
        second_event = initial_events[1]
        committed = context.Event()
        after_commit = context.Process(
            target=_postcommit_interruption_worker,
            args=(str(self.database_path), FIXTURE_IDENTITY, second_event, committed),
        )
        after_commit.start()
        self.assertTrue(committed.wait(timeout=20))
        after_commit.join(timeout=10)
        self.assertFalse(after_commit.is_alive())
        self.assertEqual(after_commit.exitcode, 73)

        retry = self.store.append(
            transaction_id="postcommit-interruption",
            expected_revision=1,
            prompt_sha256=FIXTURE_IDENTITY.prompt_sha256,
            events=[second_event],
        )
        self.assertEqual(retry["revision"], 2)
        self.assertEqual(first_result["revision"], 1)
        snapshot = self.store.snapshot()
        self.assertEqual(snapshot["event_count"], 2)
        self.assertEqual(snapshot["events"], [first_event, second_event])
        self.assertEqual(self.store.audit()["receipt_count"], 2)

    def test_busy_timeout_missing_database_and_corrupt_database_errors(self) -> None:
        missing_path = self.root / "missing.sqlite3"
        with self.assertRaises(PocError):
            SQLiteWorkflowPoc(missing_path, FIXTURE_IDENTITY, initialize=False)

        corrupt_path = self.root / "corrupt.sqlite3"
        corrupt_path.write_bytes(b"not a sqlite database")
        with self.assertRaises(sqlite3.DatabaseError):
            SQLiteWorkflowPoc(corrupt_path, FIXTURE_IDENTITY, initialize=False)

        lock = sqlite3.connect(self.database_path, isolation_level=None)
        lock.execute("BEGIN IMMEDIATE")
        started = time.perf_counter()
        try:
            with patch.object(workflow_poc, "BUSY_TIMEOUT_MS", 100):
                with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                    self.store.append(
                        transaction_id="busy-timeout",
                        expected_revision=0,
                        prompt_sha256=FIXTURE_IDENTITY.prompt_sha256,
                        events=_closeout_history()[:2],
                    )
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000
            lock.rollback()
            lock.close()
        self.assertGreaterEqual(elapsed_ms, 50)
        self.assertLess(elapsed_ms, 2000)
        self.assertEqual(self.store.snapshot()["event_count"], 0)
        print(
            "PHASE_B_ERROR_BEHAVIOR "
            + json.dumps(
                {
                    "missing_db": "explicit PocError",
                    "corrupt_db": "explicit sqlite3.DatabaseError",
                    "busy_timeout_ms": 100,
                    "busy_elapsed_ms": round(elapsed_ms, 3),
                    "busy_error": "explicit sqlite3.OperationalError",
                    "permissions": "NOT TESTED",
                    "disk_full": "NOT TESTED",
                },
                sort_keys=True,
            )
        )

    def test_eight_process_writers_account_for_all_events(self) -> None:
        events = _closeout_history()
        self.store.append(
            transaction_id="writer-test-start",
            expected_revision=0,
            prompt_sha256=FIXTURE_IDENTITY.prompt_sha256,
            events=events[:2],
        )

        context = multiprocessing.get_context("fork")
        barrier = context.Barrier(WRITER_COUNT)
        results = context.Queue()
        processes = [
            context.Process(
                target=_worker,
                args=(str(self.database_path), FIXTURE_IDENTITY, worker, barrier, results),
            )
            for worker in range(WRITER_COUNT)
        ]
        started = time.perf_counter()
        for process in processes:
            process.start()
        try:
            outputs = [results.get(timeout=180) for _ in processes]
        finally:
            for process in processes:
                process.join(180)
                if process.is_alive():
                    process.terminate()
                    process.join(5)

        self.assertTrue(all(not process.is_alive() for process in processes))
        self.assertEqual([process.exitcode for process in processes], [0] * WRITER_COUNT)
        self.assertEqual(sum(output["errors"] for output in outputs), 0, outputs)
        acknowledged = sum(output["acknowledged"] for output in outputs)
        rejected = sum(output["rejected"] for output in outputs)
        duplicates = sum(output["duplicates"] for output in outputs)
        busy_errors = sum(output["busy_errors"] for output in outputs)
        final = self.store.snapshot()
        lost = acknowledged - (final["event_count"] - 2)
        elapsed_ms = (time.perf_counter() - started) * 1000
        self.assertEqual(acknowledged, WRITER_COUNT * WRITER_EVENTS_PER_PROCESS)
        self.assertEqual(duplicates, 0)
        self.assertEqual(lost, 0)
        self.assertEqual(final["event_count"], 2 + WRITER_COUNT * WRITER_EVENTS_PER_PROCESS)
        self.assertEqual(self.store.audit()["event_count"], final["event_count"])
        print(
            "PHASE_B_WRITERS "
            + json.dumps(
                {
                    "processes": len(processes),
                    "transactions_per_process": WRITER_EVENTS_PER_PROCESS,
                    "acknowledged": acknowledged,
                    "explicit_revision_rejections": rejected,
                    "busy_errors": busy_errors,
                    "attempts": sum(output["attempts"] for output in outputs),
                    "max_worker_attempts": max(output["attempts"] for output in outputs),
                    "busy_timeout_ms": workflow_poc.BUSY_TIMEOUT_MS,
                    "transaction_retry": "same transaction id and request",
                    "duplicates": duplicates,
                    "lost": lost,
                    "elapsed_ms": round(elapsed_ms, 3),
                    "resource": _resource_metrics(),
                },
                sort_keys=True,
            )
        )


if __name__ == "__main__":
    unittest.main()
