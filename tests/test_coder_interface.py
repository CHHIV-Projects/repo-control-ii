from __future__ import annotations

import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from repoctl.cli import build_parser, main
from repoctl.workflow.coder_interface import (
    GIT_MUTATION_POLICY_NOTICE,
    CoderInterfaceError,
    format_coder_context_text,
    format_coder_status_text,
    get_authorization_status,
    get_coder_context,
    get_coder_status,
    resolve_repository_entry,
    submit_coder_bug,
    submit_coder_closeout,
    submit_coder_question,
    submit_coder_test_result,
)
from repoctl.workflow.lifecycle import record_milestone_event
from repoctl.workflow.milestones import (
    discover_milestones,
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


def _init_test_repo(root: Path, name: str = "repo") -> Path:
    repo = root / name
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "tracked.txt").write_text("content\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-qm", "initial")

    milestones_dir = repo / "docs" / "milestones"
    milestones_dir.mkdir(parents=True, exist_ok=True)
    prompt = milestones_dir / "M003.001_test_milestone_prompt.md"
    prompt.write_text("# Test Milestone Prompt\nLine 1\nLine 2\n", encoding="utf-8")
    return repo


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


class TestCoderInterface(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp_dir.name)
        self.repo = _init_test_repo(self.temp_path)
        self.state_root = self.temp_path / "state"
        self.state_root.mkdir(parents=True, exist_ok=True)
        self.candidate = discover_milestones(self.repo)["candidates"][0]

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_no_milestone_selected(self):
        with self.assertRaises(CoderInterfaceError) as ctx:
            get_coder_context(self.repo, state_root=self.state_root)
        self.assertIn("No milestone is currently selected", str(ctx.exception))

    def test_invalid_repository_path(self):
        non_repo = self.temp_path / "non_repo"
        non_repo.mkdir()
        with self.assertRaises(CoderInterfaceError) as ctx:
            get_coder_context(non_repo, state_root=self.state_root)
        self.assertIn("Target is not a valid Git repository", str(ctx.exception))

    def test_stale_or_invalid_selection_pointer(self):
        save_milestone_selection(self.repo, self.state_root, "M003.001")
        # Remove the prompt to make selection invalid
        (self.repo / self.candidate["prompt_path"]).unlink()
        with self.assertRaises(CoderInterfaceError) as ctx:
            get_coder_context(self.repo, state_root=self.state_root)
        self.assertIn("The saved milestone prompt is missing", str(ctx.exception))

    def test_authorization_matrix_and_phases(self):
        save_milestone_selection(self.repo, self.state_root, "M003.001")

        # 1. PLANNING phase (initial)
        ctx = get_coder_context(self.repo, state_root=self.state_root)
        self.assertEqual(ctx["milestone"]["phase"], "PLANNING")
        self.assertFalse(ctx["authorization"]["authorized"])
        self.assertEqual(ctx["authorization"]["status"], "NOT_AUTHORIZED")
        self.assertIn("NO", ctx["authorization"]["message"])

        # 2. READY phase
        _record(
            self.repo,
            self.state_root,
            self.candidate,
            "prompt_approved",
            "Prompt approved",
            actor_role="product_owner",
            po_approved=True,
        )
        ctx = get_coder_context(self.repo, state_root=self.state_root)
        self.assertEqual(ctx["milestone"]["phase"], "READY")
        self.assertFalse(ctx["authorization"]["authorized"])
        self.assertTrue(ctx["authorization"]["can_preflight"])
        self.assertEqual(ctx["authorization"]["status"], "PREFLIGHT_ONLY")
        self.assertIn("preflight only", ctx["authorization"]["message"])

        # 3. IN_PROGRESS phase
        _record(self.repo, self.state_root, self.candidate, "implementation_started", "Starting work")
        ctx = get_coder_context(self.repo, state_root=self.state_root)
        self.assertEqual(ctx["milestone"]["phase"], "IN_PROGRESS")
        self.assertTrue(ctx["authorization"]["authorized"])
        self.assertEqual(ctx["authorization"]["status"], "AUTHORIZED")
        self.assertIn("YES", ctx["authorization"]["message"])

        # 4. WAITING phase
        _record(
            self.repo,
            self.state_root,
            self.candidate,
            "question_submitted",
            "Need clarification",
            waiting_category="blocker",
            waiting_reason="Waiting on design",
        )
        ctx = get_coder_context(self.repo, state_root=self.state_root)
        self.assertEqual(ctx["milestone"]["phase"], "WAITING")
        self.assertFalse(ctx["authorization"]["authorized"])
        self.assertEqual(ctx["authorization"]["status"], "NOT_AUTHORIZED")
        self.assertIn("WAITING", ctx["authorization"]["message"])
        self.assertEqual(ctx["workflow"]["waiting_category"], "blocker")
        self.assertEqual(ctx["workflow"]["waiting_reason"], "Waiting on design")

        # Resume to IN_PROGRESS
        _record(
            self.repo,
            self.state_root,
            self.candidate,
            "answer_recorded",
            "Here is the answer",
            actor_role="architect",
            related_event_id=ctx["workflow"]["recent_events"][-1]["event_id"],
        )
        _record(
            self.repo,
            self.state_root,
            self.candidate,
            "implementation_resumed",
            "Resuming work",
            actor_role="product_owner",
            po_approved=True,
        )

        # 5. REVIEW phase (requires closeout)
        closeout = self.repo / "docs" / "milestones" / "M003.001_test_milestone_closeout.md"
        closeout.write_text("# Closeout\nDone\n", encoding="utf-8")
        _record(self.repo, self.state_root, self.candidate, "closeout_submitted", "Closeout ready")
        ctx = get_coder_context(self.repo, state_root=self.state_root)
        self.assertEqual(ctx["milestone"]["phase"], "REVIEW")
        self.assertFalse(ctx["authorization"]["authorized"])
        self.assertEqual(ctx["authorization"]["status"], "NOT_AUTHORIZED")
        self.assertIn("REVIEW", ctx["authorization"]["message"])

        # Correction requested -> IN_PROGRESS in correction mode
        _record(
            self.repo,
            self.state_root,
            self.candidate,
            "correction_requested",
            "Please fix typo",
            actor_role="product_owner",
            po_approved=True,
        )
        ctx = get_coder_context(self.repo, state_root=self.state_root)
        self.assertEqual(ctx["milestone"]["phase"], "IN_PROGRESS")
        self.assertTrue(ctx["authorization"]["authorized"])
        self.assertEqual(ctx["authorization"]["status"], "AUTHORIZED_CORRECTION")
        self.assertIn("correction mode", ctx["authorization"]["message"])

        # 6. CLOSED phase
        _record(self.repo, self.state_root, self.candidate, "closeout_submitted", "Updated closeout")
        _record(
            self.repo,
            self.state_root,
            self.candidate,
            "milestone_closed",
            "Milestone closed",
            actor_role="product_owner",
            po_approved=True,
        )
        ctx = get_coder_context(self.repo, state_root=self.state_root)
        self.assertEqual(ctx["milestone"]["phase"], "CLOSED")
        self.assertFalse(ctx["authorization"]["authorized"])
        self.assertEqual(ctx["authorization"]["status"], "NOT_AUTHORIZED")
        self.assertIn("CLOSED", ctx["authorization"]["message"])

    def test_coder_context_and_status_payload(self):
        save_milestone_selection(self.repo, self.state_root, "M003.001")
        ctx = get_coder_context(self.repo, state_root=self.state_root)
        status = get_coder_status(self.repo, state_root=self.state_root)

        # Keys present
        for payload in (ctx, status):
            self.assertIn("repository", payload)
            self.assertIn("selection", payload)
            self.assertIn("milestone", payload)
            self.assertIn("authorization", payload)
            self.assertIn("workflow", payload)
            self.assertIn("standing_references", payload)
            self.assertIn("prompt", payload)

        # Prompt content presence/absence
        self.assertIn("content", ctx["prompt"])
        self.assertIn("Test Milestone Prompt", ctx["prompt"]["content"])
        self.assertNotIn("content", status["prompt"])
        self.assertEqual(status["prompt"]["path"], "docs/milestones/M003.001_test_milestone_prompt.md")

        # References and Git mutation policy
        refs = ctx["standing_references"]
        self.assertEqual(refs["coding_agent_rules"], "docs/context/coding_agent_rules.md")
        self.assertEqual(refs["project_workflow"], "docs/context/project_workflow.md")
        self.assertEqual(refs["project_architecture"], "docs/context/project_architecture.md")
        self.assertIn("Coding agents must not stage, commit, push", refs["git_mutation_policy"])

        # Text formatting
        txt_ctx = format_coder_context_text(ctx)
        self.assertIn("REPO CONTROL — CODER STATUS", txt_ctx)
        self.assertIn("STANDING DOCUMENT REFERENCES & RULES", txt_ctx)
        self.assertIn("AUTHORITATIVE PROMPT CONTENT", txt_ctx)
        self.assertIn("Test Milestone Prompt", txt_ctx)

        txt_status = format_coder_status_text(status)
        self.assertIn("REPO CONTROL — CODER STATUS", txt_status)
        self.assertNotIn("AUTHORITATIVE PROMPT CONTENT", txt_status)

    def test_submit_coder_question_non_blocking_and_blocking(self):
        save_milestone_selection(self.repo, self.state_root, "M003.001")
        _record(
            self.repo,
            self.state_root,
            self.candidate,
            "prompt_approved",
            "Approved",
            actor_role="product_owner",
            po_approved=True,
        )
        _record(self.repo, self.state_root, self.candidate, "implementation_started", "Start")

        # Non-blocking question
        res1 = submit_coder_question(
            summary="What should the timeout be?",
            blocking=False,
            repository_path=self.repo,
            state_root=self.state_root,
        )
        self.assertFalse(res1["blocking"])
        self.assertEqual(res1["phase_before"], "IN_PROGRESS")
        self.assertEqual(res1["phase_after"], "IN_PROGRESS")
        self.assertIsNone(res1["waiting_category"])
        self.assertIsNone(res1["waiting_reason"])

        ctx = get_coder_context(self.repo, state_root=self.state_root)
        self.assertEqual(len(ctx["workflow"]["open_questions"]), 1)
        self.assertEqual(ctx["milestone"]["phase"], "IN_PROGRESS")

        # Blocking question
        res2 = submit_coder_question(
            summary="Need database password to run migration",
            blocking=True,
            category="blocker",
            repository_path=self.repo,
            state_root=self.state_root,
        )
        self.assertTrue(res2["blocking"])
        self.assertEqual(res2["phase_before"], "IN_PROGRESS")
        self.assertEqual(res2["phase_after"], "WAITING")
        self.assertEqual(res2["waiting_category"], "blocker")
        self.assertEqual(res2["waiting_reason"], "Need database password to run migration")

        ctx2 = get_coder_context(self.repo, state_root=self.state_root)
        self.assertEqual(ctx2["milestone"]["phase"], "WAITING")
        self.assertFalse(ctx2["authorization"]["authorized"])

    def test_submit_coder_question_validation_and_phase_restrictions(self):
        save_milestone_selection(self.repo, self.state_root, "M003.001")

        # Empty summary
        with self.assertRaises(CoderInterfaceError) as ctx:
            submit_coder_question("   ", repository_path=self.repo, state_root=self.state_root)
        self.assertIn("summary must not be empty", str(ctx.exception))

        # Invalid category
        with self.assertRaises(CoderInterfaceError) as ctx:
            submit_coder_question(
                "A question",
                category="invalid_cat",
                repository_path=self.repo,
                state_root=self.state_root,
            )
        self.assertIn("Invalid category", str(ctx.exception))

        # Question in PLANNING phase should fail
        with self.assertRaises(CoderInterfaceError) as ctx:
            submit_coder_question(
                "A question",
                repository_path=self.repo,
                state_root=self.state_root,
            )
        self.assertIn("only while IN_PROGRESS", str(ctx.exception))

    def test_submit_coder_bug(self):
        save_milestone_selection(self.repo, self.state_root, "M003.001")
        _record(
            self.repo,
            self.state_root,
            self.candidate,
            "prompt_approved",
            "Approved",
            actor_role="product_owner",
            po_approved=True,
        )
        _record(self.repo, self.state_root, self.candidate, "implementation_started", "Start")

        # Valid in IN_PROGRESS
        res = submit_coder_bug(
            "Found off-by-one error in pagination",
            repository_path=self.repo,
            state_root=self.state_root,
        )
        self.assertEqual(res["phase"], "IN_PROGRESS")
        self.assertEqual(res["summary"], "Found off-by-one error in pagination")

        # Empty summary fails
        with self.assertRaises(CoderInterfaceError):
            submit_coder_bug("   ", repository_path=self.repo, state_root=self.state_root)

    def test_submit_coder_test_result(self):
        save_milestone_selection(self.repo, self.state_root, "M003.001")
        _record(
            self.repo,
            self.state_root,
            self.candidate,
            "prompt_approved",
            "Approved",
            actor_role="product_owner",
            po_approved=True,
        )
        _record(self.repo, self.state_root, self.candidate, "implementation_started", "Start")

        res = submit_coder_test_result(
            summary="All unit tests passed",
            source="pytest tests/test_coder_interface.py",
            result="15 passed in 0.05s",
            repository_path=self.repo,
            state_root=self.state_root,
        )
        self.assertEqual(res["phase"], "IN_PROGRESS")
        self.assertEqual(res["evidence_source"], "pytest tests/test_coder_interface.py")
        self.assertEqual(res["result"], "15 passed in 0.05s")

        # Validation errors
        with self.assertRaises(CoderInterfaceError):
            submit_coder_test_result("", "source", "result", repository_path=self.repo, state_root=self.state_root)
        with self.assertRaises(CoderInterfaceError):
            submit_coder_test_result("summary", "", "result", repository_path=self.repo, state_root=self.state_root)
        with self.assertRaises(CoderInterfaceError):
            submit_coder_test_result("summary", "source", "", repository_path=self.repo, state_root=self.state_root)

    def test_submit_coder_closeout(self):
        save_milestone_selection(self.repo, self.state_root, "M003.001")
        _record(
            self.repo,
            self.state_root,
            self.candidate,
            "prompt_approved",
            "Approved",
            actor_role="product_owner",
            po_approved=True,
        )
        _record(self.repo, self.state_root, self.candidate, "implementation_started", "Start")

        # Fails when closeout artifact is missing
        with self.assertRaises(CoderInterfaceError) as ctx:
            submit_coder_closeout(repository_path=self.repo, state_root=self.state_root)
        self.assertIn("closeout artifact is required", str(ctx.exception))

        # Create closeout artifact
        closeout = self.repo / "docs" / "milestones" / "M003.001_test_milestone_closeout.md"
        closeout.write_text("# Closeout Report\nComplete.\n", encoding="utf-8")

        res = submit_coder_closeout(
            summary="Completed implementation and validation",
            repository_path=self.repo,
            state_root=self.state_root,
        )
        self.assertEqual(res["phase_before"], "IN_PROGRESS")
        self.assertEqual(res["phase_after"], "REVIEW")
        self.assertEqual(res["evidence_path"], "docs/milestones/M003.001_test_milestone_closeout.md")

        # Now in REVIEW phase, calling closeout again should fail
        with self.assertRaises(CoderInterfaceError) as ctx:
            submit_coder_closeout(repository_path=self.repo, state_root=self.state_root)
        self.assertIn("only while IN_PROGRESS", str(ctx.exception))

    def test_prohibited_actions_not_exposed(self):
        parser = build_parser()
        # Verify no generic coder-event subcommand exists
        with self.assertRaises(SystemExit):
            parser.parse_args(["coder-event", "--type", "prompt_approved"])

        # Check subparser dest
        subcommands = parser._subparsers._actions[1].choices.keys()
        prohibited = [
            "coder-event",
            "prompt-approve",
            "implementation-start",
            "decision-record",
            "lock-in",
            "milestone-close",
        ]
        for cmd in prohibited:
            self.assertNotIn(cmd, subcommands)

    def test_cli_execution_via_main(self):
        save_milestone_selection(self.repo, self.state_root, "M003.001")
        _record(
            self.repo,
            self.state_root,
            self.candidate,
            "prompt_approved",
            "Approved",
            actor_role="product_owner",
            po_approved=True,
        )
        _record(self.repo, self.state_root, self.candidate, "implementation_started", "Start")

        # 1. coder-context (text)
        stdout = io.StringIO()
        with mock.patch("sys.stdout", stdout):
            ret = main([
                "coder-context",
                "--repository", str(self.repo),
                "--state-root", str(self.state_root),
            ])
        self.assertEqual(ret, 0)
        out = stdout.getvalue()
        self.assertIn("REPO CONTROL — CODER STATUS", out)
        self.assertIn("AUTHORITATIVE PROMPT CONTENT", out)

        # 2. coder-context (json)
        stdout = io.StringIO()
        with mock.patch("sys.stdout", stdout):
            ret = main([
                "coder-context",
                "--repository", str(self.repo),
                "--state-root", str(self.state_root),
                "--format", "json",
            ])
        self.assertEqual(ret, 0)
        parsed = json.loads(stdout.getvalue())
        self.assertEqual(parsed["milestone"]["phase"], "IN_PROGRESS")
        self.assertTrue(parsed["authorization"]["authorized"])

        # 3. coder-status (text)
        stdout = io.StringIO()
        with mock.patch("sys.stdout", stdout):
            ret = main([
                "coder-status",
                "--repository", str(self.repo),
                "--state-root", str(self.state_root),
            ])
        self.assertEqual(ret, 0)
        out = stdout.getvalue()
        self.assertIn("REPO CONTROL — CODER STATUS", out)
        self.assertNotIn("AUTHORITATIVE PROMPT CONTENT", out)

        # 4. coder-status (json)
        stdout = io.StringIO()
        with mock.patch("sys.stdout", stdout):
            ret = main([
                "coder-status",
                "--repository", str(self.repo),
                "--state-root", str(self.state_root),
                "--format", "json",
            ])
        self.assertEqual(ret, 0)
        parsed = json.loads(stdout.getvalue())
        self.assertNotIn("content", parsed["prompt"])

        # 5. coder-question (non-blocking)
        stdout = io.StringIO()
        with mock.patch("sys.stdout", stdout):
            ret = main([
                "coder-question",
                "Non-blocking question",
                "--repository", str(self.repo),
                "--state-root", str(self.state_root),
            ])
        self.assertEqual(ret, 0)
        self.assertIn("Question recorded successfully", stdout.getvalue())

        # 6. coder-bug
        stdout = io.StringIO()
        with mock.patch("sys.stdout", stdout):
            ret = main([
                "coder-bug",
                "Minor bug discovered",
                "--repository", str(self.repo),
                "--state-root", str(self.state_root),
            ])
        self.assertEqual(ret, 0)
        self.assertIn("Bug recorded successfully", stdout.getvalue())

        # 7. coder-test
        stdout = io.StringIO()
        with mock.patch("sys.stdout", stdout):
            ret = main([
                "coder-test",
                "Ran unit tests",
                "--source", "pytest",
                "--result", "12 passed",
                "--repository", str(self.repo),
                "--state-root", str(self.state_root),
            ])
        self.assertEqual(ret, 0)
        self.assertIn("Test result recorded successfully", stdout.getvalue())

        # 8. coder-closeout (with closeout file present)
        closeout = self.repo / "docs" / "milestones" / "M003.001_test_milestone_closeout.md"
        closeout.write_text("# Closeout\nDone\n", encoding="utf-8")
        stdout = io.StringIO()
        with mock.patch("sys.stdout", stdout):
            ret = main([
                "coder-closeout",
                "--summary", "Submitting closeout",
                "--repository", str(self.repo),
                "--state-root", str(self.state_root),
            ])
        self.assertEqual(ret, 0)
        self.assertIn("Closeout submitted successfully", stdout.getvalue())

        # 9. Failure exit code on error
        stderr = io.StringIO()
        with mock.patch("sys.stderr", stderr):
            # Calling closeout again while in REVIEW should return 2
            ret = main([
                "coder-closeout",
                "--repository", str(self.repo),
                "--state-root", str(self.state_root),
            ])
        self.assertEqual(ret, 2)
        self.assertIn("coder closeout failed", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
