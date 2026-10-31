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

from repoctl.codex_bootstrap import (
    MAX_HOOK_OUTPUT_BYTES,
    build_session_start_response,
    main,
)
from repoctl.scanner.util import make_repository_id


def _event(cwd: Path, **overrides):
    event = {
        "hook_event_name": "SessionStart",
        "session_id": "codex-test-session",
        "cwd": str(cwd),
        "source": "startup",
        "transcript_path": "/private/session.jsonl",
    }
    event.update(overrides)
    return event


def _context(root: Path):
    prompt_path = "docs/milestones/M009.003_test_prompt.md"
    prompt_file = root / prompt_path
    prompt_file.parent.mkdir(parents=True, exist_ok=True)
    prompt_content = "# Test prompt\nPROMPT_BODY_MUST_NOT_BE_INCLUDED\n"
    prompt_bytes = prompt_content.encode("utf-8")
    prompt_file.write_bytes(prompt_bytes)
    return {
        "repository": {
            "name": "Repo Control II",
            "path": str(root),
            "key": "repo-control-ii",
            "repository_id": make_repository_id(root),
            "milestone_root": "docs/milestones",
            "documents": {},
        },
        "selection": {"milestone_id": "M009.003", "prompt_path": prompt_path},
        "milestone": {
            "id": "M009.003",
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
                "summary": "Use the canonical CLI.",
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
                "milestone_id": "M009.003",
                "approved_at": "2026-10-28T02:00:00+00:00",
                "related_event_id": "event-id-123",
                "body": "DISPATCH_BODY_MUST_NOT_BE_INCLUDED",
            }
        ],
        "prompt": {
            "path": prompt_path,
            "content": prompt_content,
            "size_bytes": len(prompt_bytes),
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


class TestCodexBootstrap(unittest.TestCase):
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
            repository_id=self.context["repository"]["repository_id"],
            python_executable=self.root / "python",
            runner=runner or _runner_for(self.context),
            observed_at="2026-10-28T02:00:00.000Z",
            observation_id="codex-observation-id",
            **kwargs,
        )

    @staticmethod
    def _context_from_output(output):
        return json.loads(output["hookSpecificOutput"]["additionalContext"])

    def test_valid_context_uses_codex_output_and_continuity_v1_fields(self):
        runner = _runner_for(self.context)
        output, diagnostic = self._run(runner=runner)
        context_text = output["hookSpecificOutput"]["additionalContext"]
        context = json.loads(context_text)

        self.assertEqual(
            output.keys(),
            {"hookSpecificOutput"},
        )
        self.assertEqual(output["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertEqual(context["schema"], "repo-control.continuity.v1")
        self.assertEqual(context["status"], "AUTHORIZED")
        self.assertEqual(context["repository"]["key"], "repo-control-ii")
        self.assertEqual(context["repository"]["repository_id"], make_repository_id(self.root))
        self.assertEqual(context["repository"]["path"], str(self.root))
        self.assertEqual(context["worktree"]["cwd"], str(self.root))
        self.assertEqual(context["selection"]["milestone_id"], "M009.003")
        self.assertEqual(context["selection"]["prompt_path"], "docs/milestones/M009.003_test_prompt.md")
        self.assertEqual(context["prompt"]["sha256"], self.context["prompt"]["identity"])
        self.assertEqual(context["lifecycle"]["phase"], "IN_PROGRESS")
        self.assertTrue(context["authorization"]["authorized"])
        self.assertEqual(context["scope"]["approved_dispatches"]["items"][0]["dispatch_id"], "dispatch-id-123")
        self.assertEqual(context["scope"]["binding_lock_in"]["summary"], "Use the canonical CLI.")
        self.assertEqual(context["observation"]["source"], "startup")
        self.assertEqual(context["measurement"]["utf8_bytes"], len(context_text.encode("utf-8")))
        self.assertEqual(context["measurement"]["estimated_tokens"], (len(context_text.encode("utf-8")) + 3) // 4)
        self.assertNotIn("PROMPT_BODY_MUST_NOT_BE_INCLUDED", context_text)
        self.assertNotIn("DISPATCH_BODY_MUST_NOT_BE_INCLUDED", context_text)
        self.assertNotIn("LOCK_IN_BODY_MUST_NOT_BE_INCLUDED", context_text)
        self.assertNotIn("/private/session.jsonl", context_text)
        self.assertLessEqual(len(context_text.encode("utf-8")), 8_192)
        self.assertLessEqual(len(json.dumps(output).encode("utf-8")), MAX_HOOK_OUTPUT_BYTES)
        self.assertEqual(diagnostic["cli_exit_code"], 0)
        self.assertTrue(diagnostic["cli_invocation_attempted"])
        self.assertEqual(diagnostic["source_worktree"], str(self.root))
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

    def test_codex_session_sources_are_preserved(self):
        for source in ("startup", "resume", "clear", "compact", "fork"):
            with self.subTest(source=source):
                output, _ = self._run(event=_event(self.root, source=source))
                self.assertEqual(self._context_from_output(output)["observation"]["source"], source)

    def test_two_fixture_repository_identities_use_codex_envelope_and_fresh_prompt_hashes(self):
        fixtures = (
            ("fixture-vocab-a", "V001.002", "alpha_prompt.md"),
            ("fixture-vocab-b", "V004.003", "beta_prompt.md"),
        )
        for key, milestone_id, prompt_name in fixtures:
            with self.subTest(repository=key):
                root = self.root / key
                root.mkdir()
                repository_id = make_repository_id(root)
                context = _context(root)
                prompt_path = f"docs/milestones/{prompt_name}"
                prompt_content = f"# {milestone_id}\nFixture-specific prompt.\n"
                (root / prompt_path).write_text(prompt_content, encoding="utf-8")
                context["repository"].update({
                    "name": key,
                    "key": key,
                    "repository_id": repository_id,
                })
                context["selection"].update({
                    "milestone_id": milestone_id,
                    "prompt_path": prompt_path,
                })
                context["milestone"]["id"] = milestone_id
                for dispatch in context["approved_architect_dispatches"]:
                    dispatch["milestone_id"] = milestone_id
                prompt_bytes = prompt_content.encode("utf-8")
                context["prompt"].update({
                    "path": prompt_path,
                    "content": prompt_content,
                    "size_bytes": len(prompt_bytes),
                    "identity": hashlib.sha256(prompt_bytes).hexdigest(),
                })
                runner = _runner_for(context)
                output, diagnostic = build_session_start_response(
                    _event(root),
                    repository_root=root,
                    repository_key=key,
                    repository_id=repository_id,
                    python_executable=Path("/trusted/central/python"),
                    runner=runner,
                    observed_at="2026-10-30T00:00:00.000Z",
                    observation_id=f"observation-{key}",
                )
                payload = self._context_from_output(output)
                self.assertEqual(output["hookSpecificOutput"]["hookEventName"], "SessionStart")
                self.assertEqual(payload["schema"], "repo-control.continuity.v1")
                self.assertEqual(payload["repository"]["key"], key)
                self.assertEqual(payload["repository"]["repository_id"], repository_id)
                self.assertEqual(payload["selection"]["milestone_id"], milestone_id)
                self.assertEqual(payload["selection"]["prompt_path"], prompt_path)
                self.assertEqual(payload["prompt"]["sha256"], context["prompt"]["identity"])
                self.assertEqual(diagnostic["repository_id"], repository_id)
                self.assertEqual(runner.last_kwargs["cwd"], str(root))
                self.assertNotIn("Fixture-specific prompt.", json.dumps(output))
                self.assertNotIn("/private/session.jsonl", json.dumps(output))

    def test_main_accepts_preview_binding_arguments(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        event = json.dumps(_event(self.root))
        with mock.patch("repoctl.codex_bootstrap.build_session_start_response") as builder:
            builder.return_value = (
                {"hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": "{}",
                }},
                {"event": "repo_control_codex_session_start", "outcome": "INVALID_NO_AUTHORITY"},
            )
            result = main(
                stdin=io.StringIO(event),
                stdout=stdout,
                stderr=stderr,
                argv=[
                    "--repository-root", str(self.root),
                    "--repository-key", "fixture-repo",
                    "--repository-id", "fixture-repo--0123456789ab",
                ],
            )
        self.assertEqual(result, 0)
        self.assertEqual(builder.call_args.kwargs["repository_root"], self.root)
        self.assertEqual(builder.call_args.kwargs["repository_key"], "fixture-repo")
        self.assertEqual(builder.call_args.kwargs["repository_id"], "fixture-repo--0123456789ab")

    def test_no_authority_and_incoherent_stale_authority_are_not_success(self):
        self.context["milestone"]["phase"] = "WAITING"
        self.context["workflow"]["phase"] = "WAITING"
        self.context["authorization"].update({
            "authorized": False,
            "status": "NOT_AUTHORIZED",
            "message": "Waiting for a Product Owner decision.",
        })
        output, _ = self._run()
        context = self._context_from_output(output)
        self.assertEqual(context["status"], "NO_AUTHORITY")
        self.assertFalse(context["authorization"]["authorized"])
        self.assertIn("STOP:", context["instruction"])

        self.context["authorization"].update({"authorized": True, "status": "AUTHORIZED"})
        output, _ = self._run()
        context = self._context_from_output(output)
        self.assertEqual(context["status"], "INVALID_NO_AUTHORITY")
        self.assertFalse(context["authorization"]["authorized"])

    def test_invalid_codex_event_and_outside_worktree_fail_before_cli(self):
        runner = _runner_for(self.context)
        output, diagnostic = self._run(event=_event(self.root, source="unknown"), runner=runner)
        context = self._context_from_output(output)
        self.assertEqual(context["status"], "INVALID_NO_AUTHORITY")
        self.assertEqual(context["reason"], "invalid_session_start_event")
        self.assertFalse(diagnostic["cli_invocation_attempted"])
        self.assertIsNone(runner.last_command)

        with tempfile.TemporaryDirectory(prefix="repo-control-codex-outside-", dir=self.root.parent) as outside:
            output, diagnostic = self._run(event=_event(Path(outside)), runner=runner)
            context = self._context_from_output(output)
            self.assertEqual(context["status"], "INVALID_NO_AUTHORITY")
            self.assertEqual(context["reason"], "worktree_mismatch")
            self.assertFalse(diagnostic["cli_invocation_attempted"])

    def test_identity_selection_prompt_hash_and_lifecycle_failures_are_invalid(self):
        mutations = (
            ("wrong repo key", lambda: self.context["repository"].update({"key": "other"})),
            ("wrong repo id", lambda: self.context["repository"].update({"repository_id": "other-id"})),
            ("wrong repo path", lambda: self.context["repository"].update({"path": str(self.root.parent)})),
            ("missing selection", lambda: self.context.update({"selection": {}})),
            ("unsupported phase", lambda: self.context["milestone"].update({"phase": "UNKNOWN"})),
            ("wrong prompt hash", lambda: self.context["prompt"].update({"identity": "0" * 64})),
        )
        for name, mutate in mutations:
            with self.subTest(name=name):
                self.context = _context(self.root)
                mutate()
                output, _ = self._run()
                context = self._context_from_output(output)
                self.assertEqual(context["status"], "INVALID_NO_AUTHORITY")
                self.assertFalse(context["authorization"]["authorized"])

    def test_cli_failure_and_malformed_output_never_fall_back_to_authorized(self):
        failed = self._run(runner=_runner_for(self.context, returncode=2, stdout="", stderr="PRIVATE STDERR"))
        failed_context = self._context_from_output(failed[0])
        self.assertEqual(failed_context["status"], "INVALID_NO_AUTHORITY")
        self.assertFalse(failed_context["authorization"]["authorized"])
        self.assertNotIn("PRIVATE STDERR", json.dumps(failed))

        malformed = self._run(runner=_runner_for(self.context, stdout="noise\n{}"))
        malformed_context = self._context_from_output(malformed[0])
        self.assertEqual(malformed_context["status"], "INVALID_NO_AUTHORITY")
        self.assertFalse(malformed_context["authorization"]["authorized"])

        def timeout(*_args, **_kwargs):
            raise subprocess.TimeoutExpired(["repoctl"], timeout=10)

        timed_out = self._run(runner=timeout)
        timeout_context = self._context_from_output(timed_out[0])
        self.assertEqual(timeout_context["status"], "INVALID_NO_AUTHORITY")
        self.assertFalse(timeout_context["authorization"]["authorized"])

    def test_acceptance_marker_is_opt_in_and_host_output_is_one_json_line(self):
        output, _ = self._run()
        self.assertNotIn("delivery_probe", self._context_from_output(output))

        stdin = io.StringIO(json.dumps(_event(self.root)))
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch("repoctl.codex_bootstrap.build_session_start_response") as builder:
            def fake_builder(event, **kwargs):
                marker = kwargs["delivery_marker"]
                result_context = {
                    "schema": "repo-control.continuity.v1",
                    "status": "AUTHORIZED",
                    "authorization": {"authorized": True},
                    "instruction": "Point-in-time context.",
                    "delivery_probe": {"marker": marker, "instruction": "Return marker once."},
                }
                return (
                    {"hookSpecificOutput": {
                        "hookEventName": "SessionStart",
                        "additionalContext": json.dumps(result_context),
                    }},
                    {
                        "event": "repo_control_codex_session_start",
                        "observation_id": "codex-observation-id",
                        "observed_at": "2026-10-28T02:00:00Z",
                        "source": "startup",
                        "cwd": str(self.root),
                        "outcome": "AUTHORIZED",
                        "cli_invocation_attempted": True,
                        "cli_exit_code": 0,
                        "source_worktree": str(self.root),
                        "repository_key": "repo-control-ii",
                        "repository_id": "repo-control-ii--test",
                        "milestone_id": "M009.003",
                        "lifecycle": "IN_PROGRESS",
                        "delivery_marker": marker,
                    },
                )

            builder.side_effect = fake_builder
            self.assertEqual(
                main(stdin=stdin, stdout=stdout, stderr=stderr, argv=[]),
                0,
            )
        self.assertEqual(len(stdout.getvalue().splitlines()), 1)
        self.assertEqual(json.loads(stdout.getvalue())["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertNotIn("PRIVATE", stderr.getvalue())

    def test_acceptance_receipt_is_private_and_correlates_marker(self):
        with tempfile.TemporaryDirectory(prefix="repo-control-codex-receipt-") as home:
            receipt = Path(home) / ".local" / "state" / "repo-control-ii-acceptance" / "codex.jsonl"
            stdout = io.StringIO()
            stderr = io.StringIO()

            def fake_builder(_event, **kwargs):
                marker = kwargs["delivery_marker"]
                output = {
                    "hookSpecificOutput": {
                        "hookEventName": "SessionStart",
                        "additionalContext": json.dumps({
                            "schema": "repo-control.continuity.v1",
                            "status": "AUTHORIZED",
                            "authorization": {"authorized": True},
                            "instruction": "Current context only.",
                            "delivery_probe": {"marker": marker},
                        }),
                    }
                }
                diagnostic = {
                    "event": "repo_control_codex_session_start",
                    "observation_id": "observation-id",
                    "observed_at": "2026-10-28T02:00:00Z",
                    "source": "startup",
                    "cwd": str(self.root),
                    "outcome": "AUTHORIZED",
                    "cli_invocation_attempted": True,
                    "cli_exit_code": 0,
                    "source_worktree": str(self.root),
                    "repository_key": "repo-control-ii",
                    "repository_id": "repo-control-ii--test",
                    "milestone_id": "M009.003",
                    "lifecycle": "IN_PROGRESS",
                    "delivery_marker": marker,
                }
                return output, diagnostic

            with mock.patch("repoctl.copilot_bootstrap.Path.home", return_value=Path(home)):
                with mock.patch("repoctl.codex_bootstrap.build_session_start_response", side_effect=fake_builder):
                    self.assertEqual(
                        main(
                            stdin=io.StringIO(json.dumps(_event(self.root))),
                            stdout=stdout,
                            stderr=stderr,
                            argv=["--acceptance-receipt", str(receipt)],
                        ),
                        0,
                    )

            record = json.loads(receipt.read_text(encoding="utf-8").splitlines()[0])
            context = json.loads(json.loads(stdout.getvalue())["hookSpecificOutput"]["additionalContext"])
            self.assertEqual(record["schema"], "repo-control.codex-session-start-receipt.v1")
            self.assertTrue(record["hook_entered"])
            self.assertTrue(record["cli_invocation_attempted"])
            self.assertTrue(record["additional_context_valid"])
            self.assertTrue(record["delivery_marker_matches_context"])
            self.assertEqual(record["delivery_marker"], context["delivery_probe"]["marker"])
            self.assertEqual(stat.S_IMODE(receipt.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(receipt.stat().st_mode), 0o600)

    def test_codex_adapter_is_registered_as_static_project_hook(self):
        config_path = Path(__file__).parents[1] / ".codex" / "hooks.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        self.assertEqual(
            config,
            {
                "hooks": {
                    "SessionStart": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": (
                                        "/home/chuck/projects/repo-control-ii/.venv/bin/python "
                                        "-m repoctl.codex_bootstrap"
                                    ),
                                    "timeout": 20,
                                    "async": False,
                                }
                            ]
                        }
                    ]
                }
            },
        )
        command = config["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        self.assertNotIn("--acceptance-receipt", command)


if __name__ == "__main__":
    unittest.main()
