from __future__ import annotations

import hashlib
import io
import json
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from repoctl.copilot_bootstrap import (
    MAX_CONTEXT_BYTES,
    _write_acceptance_receipt,
    build_session_start_response,
    main,
)


def _event(cwd: Path, **overrides):
    event = {
        "hook_event_name": "SessionStart",
        "session_id": "test-session-id",
        "timestamp": "2026-10-26T12:00:00Z",
        "cwd": str(cwd),
        "source": "startup",
        "initial_prompt": "TOP_SECRET_PROMPT",
    }
    event.update(overrides)
    return event


def _context(root: Path):
    prompt_path = "docs/milestones/M009.001_test_prompt.md"
    prompt_file = root / prompt_path
    prompt_file.parent.mkdir(parents=True, exist_ok=True)
    prompt_content = "# Test prompt\nDo not include this body in the session envelope.\n"
    prompt_bytes = prompt_content.encode("utf-8")
    prompt_file.write_bytes(prompt_bytes)
    return {
        "repository": {
            "name": "Repo Control II",
            "path": str(root),
            "key": "repo-control-ii",
            "repository_id": "repo-control-ii--test",
            "milestone_root": "docs/milestones",
            "documents": {},
        },
        "selection": {"milestone_id": "M009.001", "prompt_path": prompt_path},
        "milestone": {
            "id": "M009.001",
            "phase": "IN_PROGRESS",
            "closeout_exists": False,
            "closeout_path": None,
        },
        "authorization": {
            "authorized": True,
            "can_preflight": True,
            "status": "AUTHORIZED",
            "message": "YES — implementation and validation are authorized.",
        },
        "workflow": {
            "phase": "IN_PROGRESS",
            "waiting_category": None,
            "waiting_reason": None,
            "next_action": "Coder: continue implementation and validation.",
            "open_questions": [],
            "open_bugs": [],
            "latest_binding_lock_in": {
                "event_id": "lockin-event-id",
                "summary": "Keep the adapter read-only.",
                "body": "LOCK_IN_BODY_MUST_NOT_BE_INCLUDED",
            },
            "recent_events": [],
            "event_count": 4,
        },
        "standing_references": {
            "coding_agent_rules": "docs/context/coding_agent_rules.md",
            "project_workflow": "docs/context/project_workflow.md",
            "project_architecture": "docs/context/project_architecture.md",
            "project_context": "docs/context/project_context.md",
            "canonical_parking_lot": "docs/context/canonical_parking_lot.md",
            "git_mutation_policy": "Coders do not mutate Git.",
        },
        "approved_architect_dispatches": [
            {
                "alias": "A1",
                "dispatch_id": "dispatch-id-123",
                "type": "RESPONSE",
                "milestone_id": "M009.001",
                "approved_at": "2026-10-26T12:00:00+00:00",
                "related_event_id": "event-id-123",
                "body": "DISPATCH_BODY_MUST_NOT_BE_INCLUDED",
            }
        ],
        "prompt": {
            "path": prompt_path,
            "content": prompt_content,
            "size_bytes": len(prompt_content.encode("utf-8")),
            "identity": hashlib.sha256(prompt_bytes).hexdigest(),
        },
    }


def _runner_for(context, *, returncode=0, stdout=None, stderr=""):
    def run(command, **kwargs):
        run.last_command = command
        run.last_kwargs = kwargs
        output = json.dumps(context) if stdout is None else stdout
        return subprocess.CompletedProcess(command, returncode, output, stderr)

    run.last_command = None
    run.last_kwargs = None
    return run


class TestCopilotBootstrap(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        self.context = _context(self.root)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _run(self, event=None, runner=None, **kwargs):
        return build_session_start_response(
            event or _event(self.root),
            repository_root=self.root,
            python_executable=self.root / "python",
            runner=runner or _runner_for(self.context),
            observed_at="2026-10-26T12:00:00.000Z",
            observation_id="observation-id",
            **kwargs,
        )

    def test_valid_context_uses_canonical_cli_and_emits_bounded_host_context(self):
        runner = _runner_for(self.context)
        hook_output, diagnostic = self._run(runner=runner)
        context_text = hook_output["additionalContext"]
        context = json.loads(context_text)

        self.assertEqual(context["status"], "CURRENT_CONTEXT")
        self.assertEqual(context["repository"]["key"], "repo-control-ii")
        self.assertEqual(context["repository"]["path"], str(self.root))
        self.assertEqual(context["selection"]["milestoneId"], "M009.001")
        self.assertEqual(context["selection"]["phase"], "IN_PROGRESS")
        self.assertNotIn("deliveryProbe", context)
        self.assertEqual(context["approvedArchitectDispatches"]["items"][0]["dispatch_id"], "dispatch-id-123")
        self.assertEqual(context["workflow"]["latestBindingLockIn"]["summary"], "Keep the adapter read-only.")
        self.assertNotIn("Do not include this body", context_text)
        self.assertNotIn("DISPATCH_BODY_MUST_NOT_BE_INCLUDED", context_text)
        self.assertNotIn("LOCK_IN_BODY_MUST_NOT_BE_INCLUDED", context_text)
        self.assertNotIn("TOP_SECRET_PROMPT", context_text)
        self.assertEqual(context["measurement"]["utf8_bytes"], len(context_text.encode("utf-8")))
        self.assertEqual(context["measurement"]["estimated_tokens"], (len(context_text.encode("utf-8")) + 3) // 4)
        self.assertLessEqual(len(context_text.encode("utf-8")), MAX_CONTEXT_BYTES)
        self.assertEqual(diagnostic["cli_exit_code"], 0)
        self.assertEqual(diagnostic["source_worktree"], str(self.root))
        self.assertEqual(diagnostic["repository_id"], "repo-control-ii--test")
        self.assertTrue(diagnostic["cli_invocation_attempted"])
        self.assertNotIn("TOP_SECRET_PROMPT", json.dumps(diagnostic))
        self.assertEqual(
            runner.last_command,
            [
                str(self.root / "python"),
                "-m",
                "repoctl.cli",
                "coder-context",
                "--format",
                "json",
                "--repository",
                str(self.root),
            ],
        )
        self.assertEqual(runner.last_kwargs["cwd"], str(self.root))
        self.assertIs(runner.last_kwargs["stdin"], subprocess.DEVNULL)
        self.assertFalse(runner.last_kwargs.get("shell", False))

    def test_delivery_probe_is_added_only_when_acceptance_mode_is_requested(self):
        hook_output, diagnostic = self._run(delivery_marker="repoctl-probe-test-marker")
        context = json.loads(hook_output["additionalContext"])
        self.assertEqual(context["deliveryProbe"]["marker"], "repoctl-probe-test-marker")
        self.assertIn("first response", context["deliveryProbe"]["instruction"])
        self.assertEqual(diagnostic["delivery_marker"], "repoctl-probe-test-marker")

    def test_untrusted_prompt_and_dispatch_bodies_are_not_in_output_or_diagnostics(self):
        hook_output, diagnostic = self._run()
        self.assertNotIn("TOP_SECRET_PROMPT", hook_output["additionalContext"])
        self.assertNotIn("DISPATCH_BODY_MUST_NOT_BE_INCLUDED", hook_output["additionalContext"])
        self.assertNotIn("LOCK_IN_BODY_MUST_NOT_BE_INCLUDED", hook_output["additionalContext"])
        self.assertNotIn("TOP_SECRET_PROMPT", json.dumps(diagnostic))

    def test_unauthorized_context_is_explicit_no_authority(self):
        self.context["authorization"].update({
            "authorized": False,
            "status": "NOT_AUTHORIZED",
            "message": "NO — implementation is paused in WAITING.",
        })
        self.context["milestone"]["phase"] = "WAITING"
        self.context["workflow"]["phase"] = "WAITING"
        self.context["workflow"]["waiting_category"] = "blocker"
        self.context["workflow"]["waiting_reason"] = "Waiting for the Product Owner."
        hook_output, _ = self._run()
        context = json.loads(hook_output["additionalContext"])
        self.assertEqual(context["status"], "NO_AUTHORITY")
        self.assertFalse(context["authorization"]["authorized"])
        self.assertIn("STOP:", context["instruction"])
        self.assertEqual(context["workflow"]["waitingReason"], "Waiting for the Product Owner.")

    def test_authorized_stale_context_is_rejected_when_lifecycle_is_not_in_progress(self):
        self.context["milestone"]["phase"] = "WAITING"
        self.context["workflow"]["phase"] = "WAITING"
        hook_output, diagnostic = self._run()
        context = json.loads(hook_output["additionalContext"])

        self.assertEqual(context["status"], "INVALID_NO_AUTHORITY")
        self.assertEqual(context["reason"], "invalid_authorization")
        self.assertFalse(context["authorization"]["authorized"])
        self.assertEqual(diagnostic["outcome"], "invalid_no_authority")

    def test_each_resume_invocation_uses_new_live_lifecycle_without_cached_authority(self):
        snapshots = []
        for phase in ("IN_PROGRESS", "WAITING", "REVIEW", "CLOSED"):
            snapshot = json.loads(json.dumps(self.context))
            snapshot["milestone"]["phase"] = phase
            snapshot["workflow"]["phase"] = phase
            snapshot["authorization"] = {
                "authorized": phase == "IN_PROGRESS",
                "can_preflight": phase == "IN_PROGRESS",
                "status": "AUTHORIZED" if phase == "IN_PROGRESS" else "NOT_AUTHORIZED",
                "message": "Live fixture lifecycle.",
            }
            if phase == "WAITING":
                snapshot["workflow"]["waiting_category"] = "blocker"
                snapshot["workflow"]["waiting_reason"] = "Resolve the current blocker."
                snapshot["workflow"]["open_questions"] = [
                    {"event_id": "question-1", "summary": "Resolve the current blocker."}
                ]
            snapshots.append(snapshot)

        invocation_count = 0

        def live_runner(command, **kwargs):
            nonlocal invocation_count
            snapshot = snapshots[invocation_count]
            invocation_count += 1
            return subprocess.CompletedProcess(command, 0, json.dumps(snapshot), "")

        responses = []
        for _ in snapshots:
            hook_output, _ = self._run(
                event=_event(self.root, source="resume"),
                runner=live_runner,
            )
            responses.append(json.loads(hook_output["additionalContext"]))

        self.assertEqual(invocation_count, 4)
        self.assertEqual(
            [response["selection"]["phase"] for response in responses],
            ["IN_PROGRESS", "WAITING", "REVIEW", "CLOSED"],
        )
        self.assertEqual(
            [response["status"] for response in responses],
            ["CURRENT_CONTEXT", "NO_AUTHORITY", "NO_AUTHORITY", "NO_AUTHORITY"],
        )
        self.assertTrue(responses[0]["authorization"]["authorized"])
        self.assertTrue(all(not response["authorization"]["authorized"] for response in responses[1:]))
        self.assertEqual(
            [response["observation"]["source"] for response in responses],
            ["resume"] * 4,
        )
        self.assertEqual(
            responses[1]["workflow"]["openQuestions"]["items"][0]["event_id"],
            "question-1",
        )

    def test_invalid_worktree_is_rejected_before_running_cli(self):
        with tempfile.TemporaryDirectory(prefix="repo-control-bootstrap-outside-", dir=self.root.parent) as outside:
            runner = _runner_for(self.context)
            hook_output, diagnostic = self._run(event=_event(Path(outside)), runner=runner)
            context = json.loads(hook_output["additionalContext"])
            self.assertEqual(context["status"], "INVALID_NO_AUTHORITY")
            self.assertEqual(context["reason"], "worktree_mismatch")
            self.assertIsNone(runner.last_command)
            self.assertEqual(diagnostic["outcome"], "invalid_no_authority")

    def test_in_repo_symlink_is_resolved_before_cli_invocation(self):
        subdirectory = self.root / "nested"
        subdirectory.mkdir()
        alias = self.root / "alias"
        alias.symlink_to(subdirectory, target_is_directory=True)
        runner = _runner_for(self.context)
        hook_output, _ = self._run(event=_event(alias), runner=runner)
        self.assertEqual(json.loads(hook_output["additionalContext"])["status"], "CURRENT_CONTEXT")
        self.assertEqual(runner.last_kwargs["cwd"], str(subdirectory))

    def test_relative_worktree_and_malformed_event_fields_fail_closed(self):
        relative = self._run(event=_event("nested"))
        self.assertEqual(
            json.loads(relative[0]["additionalContext"])["reason"],
            "invalid_session_start_event",
        )

        malformed_source = self._run(event=_event(self.root, source=[]))
        self.assertEqual(
            json.loads(malformed_source[0]["additionalContext"])["reason"],
            "invalid_session_start_event",
        )

    def test_missing_cli_nonzero_exit_timeout_and_malformed_output_fail_closed(self):
        missing_cli = self._run(runner=lambda *_args, **_kwargs: (_ for _ in ()).throw(FileNotFoundError()))
        self.assertEqual(json.loads(missing_cli[0]["additionalContext"])["reason"], "canonical_cli_unavailable")

        failed_cli = self._run(runner=_runner_for(self.context, returncode=2, stdout="", stderr="SECRET STDERR"))
        failed_context = json.loads(failed_cli[0]["additionalContext"])
        self.assertEqual(failed_context["reason"], "coder_context_failed")
        self.assertEqual(failed_context["cliExitCode"], 2)
        self.assertNotIn("SECRET STDERR", json.dumps(failed_cli))

        def timeout(*_args, **_kwargs):
            raise subprocess.TimeoutExpired(["repoctl"], timeout=10)

        timed_out = self._run(runner=timeout)
        self.assertEqual(json.loads(timed_out[0]["additionalContext"])["reason"], "coder_context_timeout")

        malformed = self._run(runner=_runner_for(self.context, stdout="diagnostic noise\n{}"))
        self.assertEqual(json.loads(malformed[0]["additionalContext"])["reason"], "coder_context_invalid_json")

        empty = self._run(runner=_runner_for(self.context, stdout=""))
        self.assertEqual(json.loads(empty[0]["additionalContext"])["reason"], "coder_context_invalid_json")

        oversized = self._run(runner=_runner_for(self.context, stdout="x" * 1_000_001))
        self.assertEqual(
            json.loads(oversized[0]["additionalContext"])["reason"],
            "coder_context_output_exceeds_bound",
        )

    def test_wrong_identity_unrecognized_lifecycle_and_prompt_hash_are_rejected(self):
        self.context["repository"]["key"] = "another-repository"
        mismatch = self._run()
        self.assertEqual(json.loads(mismatch[0]["additionalContext"])["reason"], "repository_identity_mismatch")

        self.context["repository"]["key"] = "repo-control-ii"
        self.context["repository"]["path"] = str(self.root.parent)
        wrong_path = self._run()
        self.assertEqual(
            json.loads(wrong_path[0]["additionalContext"])["reason"],
            "repository_identity_mismatch",
        )
        self.context["repository"]["path"] = str(self.root)

        self.context["selection"] = {}
        missing_selection = self._run()
        self.assertEqual(
            json.loads(missing_selection[0]["additionalContext"])["reason"],
            "milestone_identity_mismatch",
        )
        self.context["selection"] = {
            "milestone_id": "M009.001",
            "prompt_path": "docs/milestones/M009.001_test_prompt.md",
        }

        self.context["milestone"]["phase"] = "UNKNOWN"
        unknown_phase = self._run()
        self.assertEqual(json.loads(unknown_phase[0]["additionalContext"])["reason"], "unrecognized_lifecycle")

        self.context["milestone"]["phase"] = "IN_PROGRESS"
        self.context["workflow"]["phase"] = "IN_PROGRESS"
        self.context["prompt"]["identity"] = "0" * 64
        bad_hash = self._run()
        self.assertEqual(json.loads(bad_hash[0]["additionalContext"])["reason"], "prompt_identity_mismatch")

    def test_bounded_reference_lists_preserve_counts_and_report_omissions(self):
        self.context["workflow"]["open_questions"] = [
            {"event_id": f"question-{index}", "event_type": "question_submitted", "summary": "Question"}
            for index in range(7)
        ]
        hook_output, _ = self._run()
        context = json.loads(hook_output["additionalContext"])
        questions = context["workflow"]["openQuestions"]
        self.assertEqual(questions["count"], 7)
        self.assertEqual(len(questions["items"]), 4)
        self.assertEqual(questions["omitted"], 3)

    def test_main_emits_one_valid_host_json_line_and_redacted_diagnostic(self):
        stdin = io.StringIO(json.dumps(_event(self.root)))
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch("repoctl.copilot_bootstrap.build_session_start_response") as builder:
            builder.return_value = (
                {"additionalContext": "{\"status\":\"CURRENT_CONTEXT\"}"},
                {"event": "repo_control_session_start", "outcome": "current_context"},
            )
            self.assertEqual(main(stdin=stdin, stdout=stdout, stderr=stderr, argv=[]), 0)
        self.assertEqual(json.loads(stdout.getvalue()), {"additionalContext": "{\"status\":\"CURRENT_CONTEXT\"}"})
        self.assertEqual(len(stdout.getvalue().splitlines()), 1)
        self.assertEqual(json.loads(stderr.getvalue())["event"], "repo_control_session_start")
        self.assertNotIn("TOP_SECRET_PROMPT", stderr.getvalue())

    def test_acceptance_receipt_is_bounded_private_and_matches_context_marker(self):
        with tempfile.TemporaryDirectory(prefix="repo-control-receipt-") as home:
            home_path = Path(home)
            receipt = home_path / ".local" / "state" / "repo-control-ii-acceptance" / "session-start.jsonl"
            stdin = io.StringIO(json.dumps(_event(self.root)))
            stdout = io.StringIO()
            stderr = io.StringIO()

            def fake_builder(event, **kwargs):
                marker = kwargs["delivery_marker"]
                context = json.dumps({
                    "schema": "repo-control.bootstrap.v1",
                    "status": "CURRENT_CONTEXT",
                    "authorization": {"authorized": True},
                    "instruction": "No authority granted.",
                    "deliveryProbe": {"marker": marker},
                })
                return (
                    {"additionalContext": context},
                    {
                        "event": "repo_control_session_start",
                        "observation_id": "observation-id",
                        "observed_at": "2026-10-26T12:00:00Z",
                        "outcome": "current_context",
                        "cli_invocation_attempted": True,
                        "cli_exit_code": 0,
                        "source_worktree": str(self.root),
                        "repository_key": "repo-control-ii",
                        "repository_id": "repo-control-ii--test",
                        "milestone_id": "M009.001",
                        "lifecycle": "IN_PROGRESS",
                        "delivery_marker": marker,
                    },
                )

            with mock.patch("repoctl.copilot_bootstrap.Path.home", return_value=home_path):
                with mock.patch("repoctl.copilot_bootstrap.build_session_start_response", side_effect=fake_builder):
                    self.assertEqual(
                        main(
                            stdin=stdin,
                            stdout=stdout,
                            stderr=stderr,
                            argv=["--acceptance-receipt", str(receipt)],
                        ),
                        0,
                    )

            output = json.loads(stdout.getvalue())
            output_context = json.loads(output["additionalContext"])
            lines = receipt.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            record = json.loads(lines[0])
            self.assertEqual(record["schema"], "repo-control.copilot-session-start-receipt.v1")
            self.assertTrue(record["hook_entered"])
            self.assertEqual(record["source"], "startup")
            self.assertEqual(record["cwd"], str(self.root))
            self.assertTrue(record["cli_invocation_attempted"])
            self.assertEqual(record["cli_exit_code"], 0)
            self.assertTrue(record["additional_context_produced"])
            self.assertTrue(record["additional_context_valid"])
            self.assertTrue(record["delivery_marker_in_context"])
            self.assertTrue(record["delivery_marker_matches_context"])
            self.assertTrue(record["stdout_emitted"])
            self.assertEqual(record["delivery_marker"], output_context["deliveryProbe"]["marker"])
            self.assertEqual(stat.S_IMODE(receipt.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(receipt.stat().st_mode), 0o600)

    def test_acceptance_receipt_rejects_paths_outside_user_home(self):
        with tempfile.TemporaryDirectory(prefix="repo-control-receipt-home-") as home:
            home_path = Path(home)
            with tempfile.TemporaryDirectory(prefix="repo-control-receipt-outside-") as outside:
                path = Path(outside) / "receipt.jsonl"
                with mock.patch("repoctl.copilot_bootstrap.Path.home", return_value=home_path):
                    result = _write_acceptance_receipt(path, {"event": "SessionStart"})
                self.assertEqual(result, "receipt_write_failed")
                self.assertFalse(path.exists())

    def test_acceptance_receipt_records_invalid_worktree_without_cli_authority(self):
        with tempfile.TemporaryDirectory(prefix="repo-control-receipt-home-") as home:
            home_path = Path(home)
            receipt = home_path / "private" / "session-start.jsonl"
            stdout = io.StringIO()
            stderr = io.StringIO()
            event = _event(Path("/tmp"))
            with mock.patch("repoctl.copilot_bootstrap.Path.home", return_value=home_path):
                self.assertEqual(
                    main(
                        stdin=io.StringIO(json.dumps(event)),
                        stdout=stdout,
                        stderr=stderr,
                        argv=["--acceptance-receipt", str(receipt)],
                    ),
                    0,
                )
            hook_output = json.loads(stdout.getvalue())
            context = json.loads(hook_output["additionalContext"])
            record = json.loads(receipt.read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(context["status"], "INVALID_NO_AUTHORITY")
            self.assertEqual(context["reason"], "worktree_mismatch")
            self.assertFalse(context["authorization"]["authorized"])
            self.assertTrue(record["hook_entered"])
            self.assertFalse(record["cli_invocation_attempted"])
            self.assertIsNone(record["cli_exit_code"])
            self.assertEqual(record["result"], "invalid_no_authority")
            self.assertTrue(record["additional_context_valid"])
            self.assertTrue(record["stdout_emitted"])

    def test_hook_registration_is_repo_scoped_session_start_with_fixed_command(self):
        config_path = Path(__file__).parents[1] / ".github" / "hooks" / "repo_control_context.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        self.assertEqual(config["version"], 1)
        self.assertEqual(list(config["hooks"]), ["SessionStart"])
        hook = config["hooks"]["SessionStart"][0]
        self.assertEqual(hook["type"], "command")
        self.assertEqual(
            hook["command"],
            "/home/chuck/projects/repo-control-ii/.venv/bin/python -m repoctl.copilot_bootstrap",
        )
        self.assertEqual(hook["cwd"], ".")
        self.assertLessEqual(hook["timeoutSec"], 15)


if __name__ == "__main__":
    unittest.main()
