from __future__ import annotations

import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from repoctl.cli import build_parser, main
from repoctl.scanner.util import make_repository_id
from repoctl.workflow.coder_interface import (
    CoderInterfaceError,
    format_coder_context_text,
    format_coder_status_text,
    GIT_MUTATION_POLICY_NOTICE,
    get_authorization_status,
    get_coder_context,
    get_coder_status,
    resolve_repository_entry,
    submit_coder_bug,
    submit_coder_closeout,
    submit_coder_question,
    submit_coder_test_result,
)
from repoctl.workflow.lifecycle import (
    load_milestone_workflow,
    record_milestone_event,
    record_milestone_reopen,
)
from repoctl.workflow.architect_dispatches import submit_dispatch, update_dispatch_status
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
        self.registry_path = self.temp_path / "central-repositories.toml"
        self._write_registry(
            [
                {
                    "key": "test-repository",
                    "name": "Test Repository",
                    "path": self.repo,
                    "milestone_root": "docs/milestones",
                }
            ]
        )
        self.registry_patcher = mock.patch(
            "repoctl.workflow.coder_interface.CENTRAL_REPOSITORY_REGISTRY_PATH",
            self.registry_path,
        )
        self.registry_patcher.start()
        self.candidate = discover_milestones(self.repo)["candidates"][0]

    def tearDown(self):
        self.registry_patcher.stop()
        self.temp_dir.cleanup()

    def _write_registry(self, entries: list[dict]) -> None:
        lines = [f"default = {json.dumps(entries[0]['key'])}"]
        for entry in entries:
            lines.extend(
                [
                    "",
                    f"[repositories.{entry['key']}]",
                    f"name = {json.dumps(entry['name'])}",
                    f"path = {json.dumps(str(entry['path']))}",
                    f"milestone_root = {json.dumps(entry['milestone_root'])}",
                    "",
                    f"[repositories.{entry['key']}.documents]",
                    'context = "docs/context/project_context.md"',
                    'architecture = "docs/context/project_architecture.md"',
                    'workflow = "docs/context/project_workflow.md"',
                    'coding_agent_rules = "docs/context/coding_agent_rules.md"',
                    'parking_lot = "docs/context/canonical_parking_lot.md"',
                ]
            )
        self.registry_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_managed_repository_resolution_uses_central_registry(self):
        entry = resolve_repository_entry(self.repo)
        self.assertEqual(entry.key, "test-repository")
        self.assertEqual(entry.path, self.repo.resolve())
        self.assertEqual(entry.repository_id, make_repository_id(self.repo))
        self.assertEqual(entry.milestone_root, "docs/milestones")
        self.assertEqual(entry.documents["coding_agent_rules"], "docs/context/coding_agent_rules.md")

        with mock.patch("repoctl.workflow.coder_interface.Path.cwd", return_value=self.repo):
            cwd_entry = resolve_repository_entry()
        self.assertEqual(cwd_entry, entry)

        save_milestone_selection(self.repo, self.state_root, "M003.001")
        status = get_coder_status(self.repo, state_root=self.state_root)
        self.assertEqual(status["repository"]["key"], "test-repository")
        self.assertEqual(status["repository"]["path"], str(self.repo.resolve()))
        self.assertEqual(status["repository"]["milestone_root"], "docs/milestones")
        self.assertEqual(status["repository"]["documents"], entry.documents)
        text = format_coder_status_text(status)
        self.assertIn("Managed Key:    test-repository", text)
        self.assertIn("Repository ID:", text)
        self.assertIn("Milestone Root: docs/milestones", text)
        context = get_coder_context(self.repo, state_root=self.state_root)
        self.assertEqual(context["repository"], status["repository"])
        self.assertEqual(
            context["standing_references"]["coding_agent_rules"],
            "docs/context/coding_agent_rules.md",
        )
        self.assertEqual(
            context["standing_references"]["project_context"],
            "docs/context/project_context.md",
        )
        self.assertEqual(
            context["standing_references"]["canonical_parking_lot"],
            "docs/context/canonical_parking_lot.md",
        )

    def test_registered_repo_control_and_vocab_keys_resolve_distinct_worktrees(self):
        vocab = _init_test_repo(self.temp_path, "vocab-app")
        self._write_registry(
            [
                {
                    "key": "repo-control-ii",
                    "name": "Repo Control II",
                    "path": self.repo,
                    "milestone_root": "docs/milestones",
                },
                {
                    "key": "vocab-app",
                    "name": "Vocab App",
                    "path": vocab,
                    "milestone_root": "docs/milestones",
                },
            ]
        )

        repo_control_entry = resolve_repository_entry(self.repo)
        vocab_entry = resolve_repository_entry(vocab)
        self.assertEqual(repo_control_entry.key, "repo-control-ii")
        self.assertEqual(vocab_entry.key, "vocab-app")
        self.assertNotEqual(repo_control_entry.repository_id, vocab_entry.repository_id)
        self.assertEqual(vocab_entry.repository_id, make_repository_id(vocab))
        self.assertEqual(vocab_entry.milestone_root, "docs/milestones")
        self.assertFalse((vocab / "ops" / "repositories.toml").exists())

    def test_unmanaged_repository_fails_closed(self):
        unmanaged = _init_test_repo(self.temp_path, "unmanaged")
        with self.assertRaisesRegex(CoderInterfaceError, "not managed by the central Repo Control registry"):
            resolve_repository_entry(unmanaged)

    def test_registry_missing_or_invalid_fails_closed(self):
        self.registry_path.unlink()
        with self.assertRaisesRegex(CoderInterfaceError, "registry is unavailable"):
            resolve_repository_entry(self.repo)

        self.registry_path.write_text("not valid TOML =", encoding="utf-8")
        with self.assertRaisesRegex(CoderInterfaceError, "registry is invalid"):
            resolve_repository_entry(self.repo)

    def test_duplicate_managed_path_fails_as_ambiguous(self):
        self._write_registry(
            [
                {
                    "key": "test-repository",
                    "name": "Test Repository",
                    "path": self.repo,
                    "milestone_root": "docs/milestones",
                },
                {
                    "key": "duplicate",
                    "name": "Duplicate Mapping",
                    "path": self.repo,
                    "milestone_root": "docs/milestones",
                },
            ]
        )
        with self.assertRaisesRegex(CoderInterfaceError, "mapped by multiple"):
            resolve_repository_entry(self.repo)

    def test_milestone_root_must_exist_and_match_registry(self):
        self._write_registry(
            [
                {
                    "key": "test-repository",
                    "name": "Test Repository",
                    "path": self.repo,
                    "milestone_root": "missing/milestones",
                }
            ]
        )
        with self.assertRaisesRegex(CoderInterfaceError, "milestone root is missing"):
            resolve_repository_entry(self.repo)

        self._write_registry(
            [
                {
                    "key": "test-repository",
                    "name": "Test Repository",
                    "path": self.repo,
                    "milestone_root": "docs/milestones",
                }
            ]
        )
        with self.assertRaisesRegex(CoderInterfaceError, "controlled by the central"):
            resolve_repository_entry(self.repo, "elsewhere")

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

    def test_reopened_phase_controls_coder_authority_without_changing_prompt(self):
        save_milestone_selection(self.repo, self.state_root, "M003.001")
        _record(
            self.repo,
            self.state_root,
            self.candidate,
            "prompt_approved",
            "Approve prompt",
            actor_role="product_owner",
            po_approved=True,
        )
        _record(self.repo, self.state_root, self.candidate, "implementation_started", "Start work")
        dispatch = submit_dispatch(
            self.repo,
            self.state_root,
            make_repository_id(self.repo),
            dispatch_type="INSTRUCTION",
            milestone_id="M003.001",
            body="Continue the approved bounded validation.",
        )
        update_dispatch_status(
            self.repo,
            self.state_root,
            make_repository_id(self.repo),
            dispatch["dispatch_id"],
            "APPROVED",
            milestone_root="docs/milestones",
            active_milestone_id="M003.001",
        )
        (self.repo / "docs/milestones/M003.001_test_milestone_closeout.md").write_text(
            "# Closeout\n",
            encoding="utf-8",
        )
        self.candidate = discover_milestones(self.repo)["candidates"][0]
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
            "milestone_closed",
            "Accept and close",
            actor_role="product_owner",
            po_approved=True,
        )
        closed = load_milestone_workflow(
            self.repo,
            self.state_root,
            "docs/milestones",
            self.candidate,
        )
        original_prompt_path = self.candidate["prompt_path"]

        review = record_milestone_reopen(
            self.repo,
            self.state_root,
            "docs/milestones",
            self.candidate,
            reason="Reconsider the closeout without authorizing edits.",
            target_phase="REVIEW",
            expected_event_count=closed["event_count"],
        )
        context = get_coder_context(self.repo, state_root=self.state_root)
        self.assertEqual(context["milestone"]["phase"], "REVIEW")
        self.assertFalse(context["authorization"]["authorized"])
        self.assertEqual(context["prompt"]["path"], original_prompt_path)

        _record(
            self.repo,
            self.state_root,
            self.candidate,
            "acceptance_recorded",
            "Accept the reviewed closeout",
            actor_role="product_owner",
            po_approved=True,
        )
        _record(
            self.repo,
            self.state_root,
            self.candidate,
            "milestone_closed",
            "Close after review",
            actor_role="product_owner",
            po_approved=True,
        )
        closed_again = load_milestone_workflow(
            self.repo,
            self.state_root,
            "docs/milestones",
            self.candidate,
        )
        record_milestone_reopen(
            self.repo,
            self.state_root,
            "docs/milestones",
            self.candidate,
            reason="Resume bounded work under the same objective.",
            target_phase="IN_PROGRESS",
            expected_event_count=closed_again["event_count"],
        )
        context = get_coder_context(self.repo, state_root=self.state_root)
        self.assertEqual(context["milestone"]["phase"], "IN_PROGRESS")
        self.assertTrue(context["authorization"]["authorized"])
        self.assertEqual(context["prompt"]["path"], original_prompt_path)
        self.assertEqual(
            [item["dispatch_id"] for item in context["approved_architect_dispatches"]],
            [dispatch["dispatch_id"]],
        )

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

    def test_coder_intelligence_refresh_cli_is_fixed_and_authorization_gated(self):
        parsed = build_parser().parse_args(["coder-intelligence-refresh"])
        self.assertEqual(parsed.command, "coder-intelligence-refresh")
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["coder-intelligence-refresh", "--repository", str(self.repo)])

        entry = resolve_repository_entry(self.repo)
        status = {
            "repository": {"repository_id": entry.repository_id, "key": entry.key},
            "milestone": {"id": "M003.001", "phase": "IN_PROGRESS"},
            "authorization": {"authorized": True},
            "workflow": {
                "waiting_category": None,
                "waiting_reason": None,
                "event_count": 2,
            },
            "prompt": {"path": self.candidate["prompt_path"], "identity": "a" * 64},
        }
        stdout = io.StringIO()
        with (
            mock.patch("repoctl.workflow.coder_interface.resolve_repository_entry", return_value=entry),
            mock.patch("repoctl.workflow.coder_interface.get_coder_status", return_value=status),
            mock.patch(
                "repoctl.intelligence.client.refresh",
                return_value={"outcome": "CURRENT"},
            ) as refresh,
            mock.patch("sys.stdout", stdout),
        ):
            result = main(["coder-intelligence-refresh"])
        self.assertEqual(result, 0)
        refresh.assert_called_once_with(
            workspace_repository_key=entry.key,
            workspace_repository_id=entry.repository_id,
            milestone_id="M003.001",
            prompt_path=self.candidate["prompt_path"],
            prompt_identity="a" * 64,
            event_count=2,
        )
        self.assertIn('"outcome":"CURRENT"', stdout.getvalue())

        unauthorized = {
            **status,
            "milestone": {"id": "M003.001", "phase": "READY"},
            "authorization": {"authorized": False},
        }
        stderr = io.StringIO()
        with (
            mock.patch("repoctl.workflow.coder_interface.resolve_repository_entry", return_value=entry),
            mock.patch("repoctl.workflow.coder_interface.get_coder_status", return_value=unauthorized),
            mock.patch("repoctl.intelligence.client.refresh") as refresh,
            mock.patch("sys.stderr", stderr),
        ):
            result = main(["coder-intelligence-refresh"])
        self.assertEqual(result, 2)
        refresh.assert_not_called()
        self.assertIn("authorized IN_PROGRESS", stderr.getvalue())

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
