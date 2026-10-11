from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from repoctl.workflow.lifecycle import (
    WorkflowRecordError,
    load_milestone_workflow,
    record_milestone_event,
)
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
