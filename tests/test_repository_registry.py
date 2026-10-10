from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from repoctl.cli import build_parser
from repoctl.web.repository_registry import RepositoryRegistry, RepositoryRegistryError


class RepositoryRegistryTests(unittest.TestCase):
    def test_registry_requires_absolute_configured_paths(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            registry_path = Path(td) / "repositories.toml"
            registry_path.write_text(
                """
default = "main"

[repositories.main]
name = "Main"
path = "relative/repository"
""",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RepositoryRegistryError, "absolute path"):
                RepositoryRegistry.from_toml(registry_path)

    def test_web_cli_accepts_registry_mode_without_a_browser_path_argument(self) -> None:
        args = build_parser().parse_args(
            ["web", "--repository-registry", "/etc/repo-control/repositories.toml"]
        )
        self.assertIsNone(args.repository)
        self.assertEqual(args.repository_registry, "/etc/repo-control/repositories.toml")

    def test_web_cli_requires_exactly_one_repository_source(self) -> None:
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["web"])
        with self.assertRaises(SystemExit):
            parser.parse_args(
                [
                    "web",
                    "--repository",
                    "/repo/one",
                    "--repository-registry",
                    "/etc/repo-control/repositories.toml",
                ]
            )
