from __future__ import annotations

import io
import json
import shlex
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from repoctl.bootstrap_preview import build_bootstrap_preview, format_bootstrap_preview
from repoctl.cli import main
from repoctl.scanner.util import make_repository_id


class BootstrapPreviewTests(unittest.TestCase):
    def test_two_fixture_repositories_render_deterministic_escaped_noninstalling_configs(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            targets = (
                ("fixture-one", base / "fixture repo one"),
                ("fixture-two", base / "fixture repo two"),
            )
            for key, root in targets:
                root.mkdir()
                before = list(root.iterdir())
                repository_id = make_repository_id(root)
                arguments = {
                    "repository_key": key,
                    "repository_id": repository_id,
                    "repository_root": root,
                    "python_executable": base / "trusted central" / "bin" / "python",
                }
                first = build_bootstrap_preview(**arguments)
                second = build_bootstrap_preview(**arguments)
                serialized = format_bootstrap_preview(first)

                self.assertEqual(serialized, format_bootstrap_preview(second))
                self.assertEqual(json.loads(serialized), first)
                self.assertTrue(first["preview_only"])
                self.assertEqual(first["target"]["repository_key"], key)
                self.assertEqual(first["target"]["repository_id"], repository_id)
                self.assertEqual(first["target"]["repository_root"], str(root))
                self.assertTrue(first["trusted_runtime"]["trust_review_required"])
                self.assertIn("preview_only", first)
                self.assertNotIn("Fixture-specific prompt.", serialized)
                self.assertNotIn("M009.007", serialized)
                self.assertNotIn("SECRET", serialized)

                copilot_hook = first["providers"]["copilot"]["registration"]["hooks"]["SessionStart"][0]
                codex_hook = first["providers"]["codex"]["registration"]["hooks"]["SessionStart"][0]["hooks"][0]
                self.assertEqual(copilot_hook["cwd"], ".")
                self.assertEqual(copilot_hook["timeoutSec"], 15)
                self.assertFalse(codex_hook["async"])
                self.assertEqual(codex_hook["timeout"], 20)
                copilot_command = copilot_hook["command"]
                codex_command = codex_hook["command"]
                for command, module in (
                    (copilot_command, "repoctl.copilot_bootstrap"),
                    (codex_command, "repoctl.codex_bootstrap"),
                ):
                    tokens = shlex.split(command)
                    self.assertIn(module, tokens)
                    self.assertIn(str(root), tokens)
                    self.assertIn(key, tokens)
                    self.assertIn(repository_id, tokens)
                self.assertEqual(list(root.iterdir()), before)

    def test_cli_preview_is_json_and_rejects_relative_paths(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve() / "fixture target"
            root.mkdir()
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                result = main([
                    "coder-bootstrap-preview",
                    "--repository-key",
                    "fixture-app",
                    "--repository-id",
                    make_repository_id(root),
                    "--repository-root",
                    str(root),
                    "--python-executable",
                    str(Path(td).resolve() / "trusted python"),
                ])
            self.assertEqual(result, 0)
            self.assertEqual(json.loads(stdout.getvalue())["target"]["repository_root"], str(root))
            self.assertEqual(stderr.getvalue(), "")
            self.assertEqual(list(root.iterdir()), [])

            with redirect_stdout(io.StringIO()), redirect_stderr(stderr := io.StringIO()):
                result = main([
                    "coder-bootstrap-preview",
                    "--repository-key",
                    "fixture-app",
                    "--repository-id",
                    make_repository_id(root),
                    "--repository-root",
                    "relative/fixture",
                ])
            self.assertEqual(result, 2)
            self.assertIn("must be absolute", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
