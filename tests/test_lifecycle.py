from __future__ import annotations

import subprocess
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from repoctl.workflow.lifecycle import (
    WorkflowRecordError,
    load_milestone_workflow,
    record_milestone_event,
    record_milestone_action,
    record_milestone_reopen,
)
from repoctl.workflow.context_verification import make_verification_record
from repoctl.workflow.milestones import (
    discover_milestones,
    load_milestone_selection,
    save_milestone_selection,
)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        text=True,
    )
    if result.returncode:
        raise RuntimeError(result.stderr)
    return result.stdout.strip()


def _repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "tracked.txt").write_text("content\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-qm", "initial")
    prompt = repo / "docs" / "milestones" / "M003.001_example_prompt.md"
    prompt.parent.mkdir(parents=True)
    prompt.write_text("# Prompt\n", encoding="utf-8")
    return repo


def _candidate(repo: Path) -> dict:
    return discover_milestones(repo)["candidates"][0]


def _record(repo: Path, state_root: Path, candidate: dict, event_type: str, summary: str, **kwargs):
    return record_milestone_event(
        repo,
        state_root,
        "docs/milestones",
        candidate,
        event_type=event_type,
        summary=summary,
        actor_role=kwargs.pop("actor_role", "coder"),
        **kwargs,
    )


class MilestoneLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.repo = _repo(self.root)
        self.state_root = self.root / "state"
        self.candidate = _candidate(self.repo)
        save_milestone_selection(self.repo, self.state_root, self.candidate["milestone_id"])

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def action(self, action: str, **fields):
        workflow = load_milestone_workflow(
            self.repo, self.state_root, "docs/milestones", self.candidate,
        )
        return record_milestone_action(
            self.repo, self.state_root, "docs/milestones", self.candidate,
            action=action, summary=fields.pop("summary", "Deliberate Product Owner action"),
            expected_phase=fields.pop("expected_phase", workflow["phase"]),
            expected_event_count=fields.pop("expected_event_count", workflow["event_count"]),
            **fields,
        )

    def close_milestone(self) -> dict:
        self.action("approve_and_start")
        (self.repo / "docs/milestones/M003.001_example_closeout.md").write_text(
            "# Closeout\n",
            encoding="utf-8",
        )
        self.candidate = _candidate(self.repo)
        _record(
            self.repo,
            self.state_root,
            self.candidate,
            "closeout_submitted",
            "Closeout submitted.",
        )
        return self.action("accept_and_close")

    def test_closed_milestone_reopens_append_only_to_review_or_in_progress(self) -> None:
        closed = self.close_milestone()
        prior_events = list(closed["event_history"])
        head = _git(self.repo, "rev-parse", "HEAD")
        refs = _git(self.repo, "for-each-ref", "--format=%(refname) %(objectname)")
        index = _git(self.repo, "diff", "--cached", "--name-only")

        reopened = record_milestone_reopen(
            self.repo,
            self.state_root,
            "docs/milestones",
            self.candidate,
            reason="The close was premature; bounded review remains necessary.",
            target_phase="IN_PROGRESS",
            expected_event_count=closed["event_count"],
        )
        self.assertEqual(reopened["phase"], "IN_PROGRESS")
        self.assertEqual(reopened["event_history"][:-1], prior_events)
        reopen_event = reopened["event_history"][-1]
        self.assertEqual(reopen_event["event_type"], "milestone_reopened")
        self.assertEqual(reopen_event["phase_before"], "CLOSED")
        self.assertEqual(reopen_event["phase_after"], "IN_PROGRESS")
        self.assertEqual(reopen_event["reopen_target_phase"], "IN_PROGRESS")
        self.assertEqual(reopen_event["summary"], "The close was premature; bounded review remains necessary.")
        self.assertEqual(reopen_event["actor_role"], "product_owner")
        self.assertTrue(reopen_event["po_approved"])
        self.assertEqual(_git(self.repo, "rev-parse", "HEAD"), head)
        self.assertEqual(_git(self.repo, "for-each-ref", "--format=%(refname) %(objectname)"), refs)
        self.assertEqual(_git(self.repo, "diff", "--cached", "--name-only"), index)

        self.candidate = _candidate(self.repo)
        self._record_closeout_again()
        closed_again = self.action("accept_and_close")
        self.assertEqual(closed_again["phase"], "CLOSED")
        reopened_again = record_milestone_reopen(
            self.repo,
            self.state_root,
            "docs/milestones",
            self.candidate,
            reason="Return to review without authorizing edits.",
            target_phase="REVIEW",
            expected_event_count=closed_again["event_count"],
        )
        self.assertEqual(reopened_again["phase"], "REVIEW")
        self.assertEqual(
            [event["event_type"] for event in reopened_again["event_history"]].count("milestone_closed"),
            2,
        )
        self.assertEqual(
            [event["event_type"] for event in reopened_again["event_history"]].count("milestone_reopened"),
            2,
        )
        self.assertEqual(
            reopened_again["event_history"][-1]["reopen_target_phase"],
            "REVIEW",
        )

    def _record_closeout_again(self) -> None:
        _record(
            self.repo,
            self.state_root,
            self.candidate,
            "closeout_submitted",
            "Amended closeout submitted.",
        )

    def test_reopen_requires_closed_phase_reason_and_product_owner(self) -> None:
        with self.assertRaisesRegex(WorkflowRecordError, "no longer CLOSED"):
            record_milestone_reopen(
                self.repo,
                self.state_root,
                "docs/milestones",
                self.candidate,
                reason="A reason",
                target_phase="REVIEW",
                expected_event_count=0,
            )
        with self.assertRaisesRegex(WorkflowRecordError, "Product Owner"):
            _record(
                self.repo,
                self.state_root,
                self.candidate,
                "milestone_reopened",
                "Unauthorized reopen",
                actor_role="coder",
            )

        closed = self.close_milestone()
        for reason, target_phase in (("", "REVIEW"), (" ", "IN_PROGRESS"), ("Required", "CLOSED")):
            with self.subTest(reason=reason, target_phase=target_phase):
                with self.assertRaises(WorkflowRecordError):
                    record_milestone_reopen(
                        self.repo,
                        self.state_root,
                        "docs/milestones",
                        self.candidate,
                        reason=reason,
                        target_phase=target_phase,
                        expected_event_count=closed["event_count"],
                    )
        self.assertEqual(
            load_milestone_workflow(
                self.repo,
                self.state_root,
                "docs/milestones",
                self.candidate,
            )["phase"],
            "CLOSED",
        )

    def test_combined_actions_preserve_order_and_distinct_events(self) -> None:
        workflow = self.action("approve_and_start")
        self.assertEqual(workflow["phase"], "IN_PROGRESS")
        self.assertEqual(
            [event["event_type"] for event in workflow["event_history"]],
            ["prompt_approved", "implementation_started"],
        )
        workflow = _record(
            self.repo, self.state_root, self.candidate, "question_submitted", "Blocking question",
            waiting_category="blocker", waiting_reason="Need an answer",
        )
        question_id = workflow["open_questions"][0]["event_id"]
        workflow = self.action(
            "resolve_and_resume", related_event_id=question_id,
            summary="The controlled answer", lock_in_summary="Binding choice",
        )
        self.assertEqual(workflow["phase"], "IN_PROGRESS")
        self.assertEqual(workflow["open_questions"], [])
        self.assertEqual(
            [event["event_type"] for event in workflow["event_history"][-3:]],
            ["answer_recorded", "lock_in_approved", "implementation_resumed"],
        )
        self.assertEqual(workflow["latest_authoritative_decision"]["summary"], "Binding choice")
        closeout = self.repo / "docs/milestones/M003.001_example_closeout.md"
        closeout.write_text("# Closeout\n", encoding="utf-8")
        self.candidate = _candidate(self.repo)
        _record(self.repo, self.state_root, self.candidate, "closeout_submitted", "Ready for review")
        workflow = self.action("accept_and_close")
        self.assertEqual(workflow["phase"], "CLOSED")
        events = workflow["event_history"]
        self.assertEqual([event["event_type"] for event in events[-2:]], ["acceptance_recorded", "milestone_closed"])
        self.assertEqual(len({event["event_id"] for event in events}), len(events))
        self.assertTrue(all(event["actor_role"] == "product_owner" for event in events[-2:]))

    def test_combined_question_resolution_targets_only_active_blocker(self) -> None:
        self.action("approve_and_start")
        workflow = _record(self.repo, self.state_root, self.candidate, "question_submitted", "Nonblocking")
        unrelated_id = workflow["open_questions"][0]["event_id"]
        workflow = _record(
            self.repo, self.state_root, self.candidate, "question_submitted", "Blocking",
            waiting_category="decision", waiting_reason="Choose",
        )
        blocker_id = workflow["open_questions"][-1]["event_id"]
        sidecar = self.repo / workflow["sidecar_path"]
        before = sidecar.read_bytes()
        for bad_id in (None, unrelated_id, "missing"):
            with self.subTest(related_id=bad_id):
                with self.assertRaisesRegex(WorkflowRecordError, "active blocking question"):
                    self.action("resolve_and_resume", related_event_id=bad_id)
                self.assertEqual(sidecar.read_bytes(), before)
        workflow = self.action("resolve_and_resume", related_event_id=blocker_id)
        self.assertEqual([event["event_type"] for event in workflow["event_history"][-2:]], ["answer_recorded", "implementation_resumed"])
        self.assertEqual([event["event_id"] for event in workflow["open_questions"]], [unrelated_id])

    def test_combined_escalation_resolution(self) -> None:
        self.action("approve_and_start")
        _record(
            self.repo, self.state_root, self.candidate, "escalation_opened", "Escalate",
            waiting_reason="Scope decision",
        )
        workflow = self.action("resolve_and_resume")
        self.assertEqual(workflow["phase"], "IN_PROGRESS")
        self.assertEqual(
            [event["event_type"] for event in workflow["event_history"][-2:]],
            ["escalation_resolved", "implementation_resumed"],
        )

    def test_combined_actions_reject_stale_or_invalid_requests_without_writing(self) -> None:
        sidecar = self.repo / "docs/milestones/M003.001_example_workflow.md"
        for action in ("resolve_and_resume", "accept_and_close"):
            with self.subTest(action=action):
                with self.assertRaises(WorkflowRecordError):
                    self.action(action)
                self.assertFalse(sidecar.exists())
        self.action("approve_and_start")
        before = sidecar.read_bytes()
        for fields in ({"expected_phase": "PLANNING"}, {"expected_event_count": 0}, {}):
            with self.subTest(fields=fields):
                with self.assertRaises(WorkflowRecordError):
                    self.action("approve_and_start", **fields)
                self.assertEqual(sidecar.read_bytes(), before)

    def test_following_event_failure_does_not_persist_first_event(self) -> None:
        sidecar = self.repo / "docs/milestones/M003.001_example_workflow.md"
        with patch("repoctl.workflow.lifecycle._MAX_EVENTS", 1):
            with self.assertRaisesRegex(WorkflowRecordError, "2000-event"):
                self.action("approve_and_start")
        self.assertFalse(sidecar.exists())
        self.action("approve_and_start")
        workflow = _record(
            self.repo, self.state_root, self.candidate, "question_submitted", "Blocking",
            waiting_category="blocker", waiting_reason="Question",
        )
        before = sidecar.read_bytes()
        with self.assertRaisesRegex(WorkflowRecordError, "size limit"):
            self.action(
                "resolve_and_resume",
                related_event_id=workflow["open_questions"][0]["event_id"],
                lock_in_summary="x" * 2001,
            )
        self.assertEqual(sidecar.read_bytes(), before)
        with patch("repoctl.workflow.lifecycle.os.replace", side_effect=OSError("Controlled write failure")):
            with self.assertRaisesRegex(WorkflowRecordError, "persist"):
                self.action("resolve_and_resume", related_event_id=workflow["open_questions"][0]["event_id"])
        self.assertEqual(sidecar.read_bytes(), before)
        self.assertEqual(list(sidecar.parent.glob(".workflow-*.tmp")), [])

    def test_combined_close_limit_failure_leaves_review_unchanged(self) -> None:
        self.action("approve_and_start")
        (self.repo / "docs/milestones/M003.001_example_closeout.md").write_text(
            "# Closeout\n", encoding="utf-8",
        )
        self.candidate = _candidate(self.repo)
        workflow = _record(
            self.repo, self.state_root, self.candidate, "closeout_submitted", "Review",
        )
        sidecar = self.repo / workflow["sidecar_path"]
        before = sidecar.read_bytes()
        with patch("repoctl.workflow.lifecycle._MAX_EVENTS", workflow["event_count"] + 1):
            with self.assertRaisesRegex(WorkflowRecordError, "2000-event"):
                self.action("accept_and_close")
        self.assertEqual(sidecar.read_bytes(), before)

    def test_phase_transitions_waiting_reason_and_resume_are_explicit(self) -> None:
        workflow = load_milestone_workflow(self.repo, self.state_root, "docs/milestones", self.candidate)
        self.assertEqual(workflow["phase"], "PLANNING")
        self.assertEqual(workflow["next_action"].split(":")[0], "Product Owner")

        with self.assertRaisesRegex(WorkflowRecordError, "Product Owner"):
            _record(self.repo, self.state_root, self.candidate, "prompt_approved", "Prompt approved")
        workflow = _record(
            self.repo,
            self.state_root,
            self.candidate,
            "prompt_approved",
            "Prompt reviewed and approved",
            actor_role="product_owner",
            po_approved=True,
        )
        self.assertEqual(workflow["phase"], "READY")

        workflow = _record(
            self.repo, self.state_root, self.candidate, "implementation_started", "Coder handoff"
        )
        self.assertEqual(workflow["phase"], "IN_PROGRESS")

        with self.assertRaisesRegex(WorkflowRecordError, "waiting reason"):
            _record(
                self.repo,
                self.state_root,
                self.candidate,
                "question_submitted",
                "Need a clarification",
                waiting_category="clarification",
            )
        workflow = _record(
            self.repo,
            self.state_root,
            self.candidate,
            "question_submitted",
            "Should lifecycle state be restart-safe?",
            waiting_category="clarification",
            waiting_reason="Awaiting Architect clarification",
        )
        question = workflow["open_questions"][0]
        self.assertEqual(workflow["phase"], "WAITING")
        self.assertEqual(workflow["waiting_reason"], "Awaiting Architect clarification")
        with self.assertRaisesRegex(WorkflowRecordError, "resolution"):
            _record(
                self.repo,
                self.state_root,
                self.candidate,
                "implementation_resumed",
                "Resume",
            )

        workflow = _record(
            self.repo,
            self.state_root,
            self.candidate,
            "answer_recorded",
            "Persist the projection in the tracked sidecar.",
            actor_role="architect",
            related_event_id=question["event_id"],
        )
        self.assertEqual(workflow["phase"], "WAITING")
        self.assertEqual(workflow["open_questions"], [])
        workflow = _record(
            self.repo,
            self.state_root,
            self.candidate,
            "implementation_resumed",
            "Work resumed after the answer.",
        )
        self.assertEqual(workflow["phase"], "IN_PROGRESS")
        self.assertIsNone(workflow["waiting_reason"])

    def test_out_of_scope_bug_classification_escalates_without_silent_scope_expansion(self) -> None:
        _record(
            self.repo,
            self.state_root,
            self.candidate,
            "prompt_approved",
            "Approved",
            actor_role="product_owner",
            po_approved=True,
        )
        _record(self.repo, self.state_root, self.candidate, "implementation_started", "Started")
        workflow = _record(self.repo, self.state_root, self.candidate, "bug_discovered", "Found unrelated subsystem issue")
        bug = workflow["open_bugs"][0]
        workflow = _record(
            self.repo,
            self.state_root,
            self.candidate,
            "bug_classified",
            "This exceeds the approved objective.",
            related_event_id=bug["event_id"],
            bug_scope="needs_escalation",
            waiting_reason="Architect and Product Owner scope decision required.",
        )
        self.assertEqual(workflow["phase"], "WAITING")
        self.assertEqual(workflow["waiting_category"], "escalation")
        with self.assertRaisesRegex(WorkflowRecordError, "only while IN_PROGRESS"):
            _record(
                self.repo,
                self.state_root,
                self.candidate,
                "escalation_opened",
                "Open another escalation without reason",
            )
        workflow = _record(
            self.repo,
            self.state_root,
            self.candidate,
            "escalation_resolved",
            "Scope was not expanded; defer the unrelated issue.",
            actor_role="product_owner",
        )
        workflow = _record(
            self.repo,
            self.state_root,
            self.candidate,
            "implementation_resumed",
            "Resume within approved scope.",
        )
        self.assertEqual(workflow["phase"], "IN_PROGRESS")

    def test_direct_phase_skip_is_rejected(self) -> None:
        with self.assertRaisesRegex(WorkflowRecordError, "only from READY"):
            _record(
                self.repo,
                self.state_root,
                self.candidate,
                "implementation_started",
                "Skip approval",
            )
        with self.assertRaisesRegex(WorkflowRecordError, "only from REVIEW"):
            _record(
                self.repo,
                self.state_root,
                self.candidate,
                "milestone_closed",
                "Close without review",
                actor_role="product_owner",
                po_approved=True,
            )

    def test_binding_decision_bug_test_review_correction_acceptance_and_closure(self) -> None:
        _record(
            self.repo,
            self.state_root,
            self.candidate,
            "prompt_approved",
            "Approved",
            actor_role="product_owner",
            po_approved=True,
        )
        _record(self.repo, self.state_root, self.candidate, "implementation_started", "Started")

        with self.assertRaisesRegex(WorkflowRecordError, "Product Owner"):
            _record(self.repo, self.state_root, self.candidate, "lock_in_approved", "Binding choice")
        workflow = _record(
            self.repo,
            self.state_root,
            self.candidate,
            "lock_in_approved",
            "Use Markdown as the durable source of truth.",
            actor_role="product_owner",
            po_approved=True,
        )
        self.assertTrue(workflow["latest_authoritative_decision"]["po_approved"])

        _record(self.repo, self.state_root, self.candidate, "decision_recorded", "Non-binding implementation note")
        workflow = _record(self.repo, self.state_root, self.candidate, "bug_discovered", "Found bounded rendering defect")
        bug = workflow["open_bugs"][0]
        workflow = _record(
            self.repo,
            self.state_root,
            self.candidate,
            "bug_classified",
            "Directly related and bounded.",
            related_event_id=bug["event_id"],
            bug_scope="in_scope",
        )
        self.assertEqual(len(workflow["open_bugs"]), 0)
        self.assertEqual(len(workflow["fixable_bugs"]), 1)
        workflow = _record(
            self.repo,
            self.state_root,
            self.candidate,
            "bug_fixed",
            "Escaped line breaks in the view.",
            related_event_id=bug["event_id"],
        )
        self.assertEqual(workflow["fixable_bugs"], [])
        workflow = _record(
            self.repo,
            self.state_root,
            self.candidate,
            "test_result_recorded",
            "Focused regression passed.",
            evidence_source=".venv/bin/pytest tests/test_lifecycle.py",
            result="PASS",
        )
        self.assertEqual(workflow["phase"], "IN_PROGRESS")

        closeout = self.repo / "docs" / "milestones" / "M003.001_example_closeout.md"
        closeout.write_text("# Closeout\n", encoding="utf-8")
        candidate = _candidate(self.repo)
        workflow = _record(
            self.repo,
            self.state_root,
            candidate,
            "closeout_submitted",
            "Closeout submitted for review.",
        )
        self.assertEqual(workflow["phase"], "REVIEW")
        workflow = _record(
            self.repo,
            self.state_root,
            candidate,
            "correction_requested",
            "Make a bounded documentation correction.",
            actor_role="architect",
        )
        self.assertEqual(workflow["phase"], "IN_PROGRESS")
        _record(self.repo, self.state_root, candidate, "closeout_submitted", "Correction complete.")
        workflow = _record(
            self.repo,
            self.state_root,
            candidate,
            "acceptance_recorded",
            "Product Owner accepts the implementation.",
            actor_role="product_owner",
            po_approved=True,
        )
        self.assertEqual(workflow["phase"], "REVIEW")
        workflow = _record(
            self.repo,
            self.state_root,
            candidate,
            "milestone_closed",
            "Product Owner closes the milestone.",
            actor_role="product_owner",
            po_approved=True,
        )
        self.assertEqual(workflow["phase"], "CLOSED")
        with self.assertRaisesRegex(WorkflowRecordError, "closed milestone"):
            _record(self.repo, self.state_root, candidate, "git_disposition_recorded", "Pushed")

    def test_context_verification_is_audit_only_even_after_closure(self) -> None:
        _record(
            self.repo,
            self.state_root,
            self.candidate,
            "prompt_approved",
            "Approved prompt",
            actor_role="product_owner",
            po_approved=True,
        )
        _record(self.repo, self.state_root, self.candidate, "implementation_started", "Start")
        (self.repo / "docs/milestones/M003.001_example_closeout.md").write_text(
            "# Closeout\n", encoding="utf-8"
        )
        _record(
            self.repo,
            self.state_root,
            self.candidate,
            "closeout_submitted",
            "Submit closeout",
        )
        _record(
            self.repo,
            self.state_root,
            self.candidate,
            "acceptance_recorded",
            "Accept",
            actor_role="product_owner",
            po_approved=True,
        )
        closed = _record(
            self.repo,
            self.state_root,
            self.candidate,
            "milestone_closed",
            "Close",
            actor_role="product_owner",
            po_approved=True,
        )
        snapshot = {
            "repository": {"key": "repo", "id": "repo--123", "root": str(self.repo)},
            "milestone": {
                "id": self.candidate["milestone_id"],
                "prompt_path": self.candidate["prompt_path"],
                "prompt_sha256": "c" * 64,
                "lifecycle": "CLOSED",
                "authorization": {"authorized": False, "status": "NOT_AUTHORIZED"},
                "closeout": {
                    "exists": True,
                    "path": "docs/milestones/M003.001_example_closeout.md",
                },
            },
            "workflow_evidence": {"test_result_count": 0},
            "approved_architect_dispatches": {"inspection_status": "available"},
            "procedure": {
                "inspection_status": "available",
                "status": "CURRENT",
                "overlay_valid": True,
            },
            "git": {"inspection_status": "available"},
        }
        record = make_verification_record(
            trigger="NEW",
            snapshot=snapshot,
            result="BLOCKED",
            git_boundary_acknowledged=True,
        )

        updated = record_milestone_event(
            self.repo,
            self.state_root,
            "docs/milestones",
            self.candidate,
            event_type="context_verification_recorded",
            summary="Context verification: BLOCKED (NEW)",
            actor_role="coder",
            context_verification=record,
            expected_phase="CLOSED",
            expected_event_count=closed["event_count"],
        )
        self.assertEqual(updated["phase"], "CLOSED")
        self.assertEqual(updated["event_count"], closed["event_count"] + 1)
        self.assertEqual(updated["event_history"][-1]["phase_before"], "CLOSED")
        self.assertEqual(updated["event_history"][-1]["phase_after"], "CLOSED")
        self.assertEqual(updated["event_history"][-1]["actor_role"], "coder")

    def test_sidecar_is_human_readable_durable_and_does_not_change_git(self) -> None:
        before_head = _git(self.repo, "rev-parse", "HEAD")
        before_refs = _git(self.repo, "for-each-ref", "--format=%(refname) %(objectname)")
        before_index = _git(self.repo, "diff", "--cached", "--name-only")
        _record(
            self.repo,
            self.state_root,
            self.candidate,
            "prompt_approved",
            "Approved prompt",
            actor_role="product_owner",
            po_approved=True,
        )
        sidecar = self.repo / "docs" / "milestones" / "M003.001_example_workflow.md"
        rendered = sidecar.read_text(encoding="utf-8")
        self.assertIn("## Chronological events", rendered)
        self.assertIn("Reported through the local UI", rendered)
        self.assertIn('"event_type": "prompt_approved"', rendered)
        self.assertEqual(_git(self.repo, "rev-parse", "HEAD"), before_head)
        self.assertEqual(
            _git(self.repo, "for-each-ref", "--format=%(refname) %(objectname)"),
            before_refs,
        )
        self.assertEqual(_git(self.repo, "diff", "--cached", "--name-only"), before_index)

        recreated = load_milestone_workflow(self.repo, self.state_root, "docs/milestones", self.candidate)
        self.assertEqual(recreated["phase"], "READY")
        self.assertEqual(recreated["event_count"], 1)
        self.assertTrue(
            load_milestone_selection(self.repo, self.state_root, "docs/milestones")["status"] == "selected"
        )
        inventory = discover_milestones(self.repo)
        self.assertEqual(len(inventory["candidates"]), 1)
        self.assertEqual(inventory["issues"], [])

    def test_unselected_milestone_and_invalid_sidecar_are_rejected(self) -> None:
        other = self.repo / "docs" / "milestones" / "M003.002_other_prompt.md"
        other.write_text("# Other\n", encoding="utf-8")
        with self.assertRaisesRegex(WorkflowRecordError, "changed or is invalid"):
            load_milestone_workflow(
                self.repo,
                self.state_root,
                "docs/milestones",
                {"milestone_id": "M003.002", "prompt_path": "docs/milestones/M003.002_other_prompt.md"},
            )

        sidecar = self.repo / "docs" / "milestones" / "M003.001_example_workflow.md"
        sidecar.write_text("malformed", encoding="utf-8")
        with self.assertRaisesRegex(WorkflowRecordError, "marker"):
            load_milestone_workflow(self.repo, self.state_root, "docs/milestones", self.candidate)

    def test_wrongly_typed_event_metadata_is_reported_as_invalid_workflow(self) -> None:
        _record(
            self.repo,
            self.state_root,
            self.candidate,
            "prompt_approved",
            "Approved prompt",
            actor_role="product_owner",
            po_approved=True,
        )
        sidecar = self.repo / "docs" / "milestones" / "M003.001_example_workflow.md"
        valid = sidecar.read_text(encoding="utf-8")
        malformed_values = (
            ('"event_type": "prompt_approved"', '"event_type": []'),
            ('"waiting_category_after": null', '"waiting_category_after": []'),
        )
        for expected, replacement in malformed_values:
            with self.subTest(replacement=replacement):
                sidecar.write_text(valid.replace(expected, replacement), encoding="utf-8")
                with self.assertRaises(WorkflowRecordError):
                    load_milestone_workflow(
                        self.repo,
                        self.state_root,
                        "docs/milestones",
                        self.candidate,
                    )
        sidecar.write_text(valid, encoding="utf-8")

    def test_github_and_cleanliness_are_not_lifecycle_inputs(self) -> None:
        baseline_status = _git(self.repo, "status", "--porcelain")
        workflow = load_milestone_workflow(self.repo, self.state_root, "docs/milestones", self.candidate)
        self.assertEqual(workflow["phase"], "PLANNING")
        self.assertEqual(_git(self.repo, "status", "--porcelain"), baseline_status)

    def test_closeout_presence_does_not_advance_lifecycle(self) -> None:
        (self.repo / "docs" / "milestones" / "M003.001_example_closeout.md").write_text(
            "# Closeout\n",
            encoding="utf-8",
        )
        workflow = load_milestone_workflow(self.repo, self.state_root, "docs/milestones", _candidate(self.repo))
        self.assertEqual(workflow["phase"], "PLANNING")


if __name__ == "__main__":
    unittest.main()
