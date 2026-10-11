from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from repoctl.cli import build_parser
from repoctl.web.repository_registry import RepositoryRegistry, RepositoryRegistryError


def _git_repo(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
    return path


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

    def test_registry_accepts_repository_relative_milestone_root(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _git_repo(root / "repo")
            registry_path = root / "repositories.toml"
            registry_path.write_text(
                "\n".join(
                    [
                        'default = "main"',
                        "",
                        "[repositories.main]",
                        'name = "Main"',
                        f'path = "{repo}"',
                        'milestone_root = "project/milestones"',
                    ]
                ),
                encoding="utf-8",
            )

            registry = RepositoryRegistry.from_toml(registry_path)

            self.assertEqual(registry.active().milestone_root, "project/milestones")

    def test_registry_accepts_server_configured_document_roles(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _git_repo(root / "repo")
            registry_path = root / "repositories.toml"
            registry_path.write_text(
                "\n".join(
                    [
                        'default = "main"',
                        "",
                        "[repositories.main]",
                        'name = "Main"',
                        f"path = {json.dumps(str(repo))}",
                        "",
                        "[repositories.main.documents]",
                        'coding_agent_rules = "docs/context/coding_agent_rules.md"',
                        'workflow = "docs/context/project_workflow.md"',
                    ]
                ),
                encoding="utf-8",
            )

            entry = RepositoryRegistry.from_toml(registry_path).active()

            self.assertEqual(entry.documents["coding_agent_rules"], "docs/context/coding_agent_rules.md")
            self.assertEqual(entry.documents["workflow"], "docs/context/project_workflow.md")

    def test_registry_rejects_unsafe_governing_document_paths_and_roles(self) -> None:
        cases = (
            ("coding_agent_rules", "../outside.md"),
            ("workflow", "/outside.md"),
            ("context", "docs\\context.md"),
            ("unknown_role", "docs/context.md"),
        )
        for role, document_path in cases:
            with self.subTest(role=role, document_path=document_path), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                repo = _git_repo(root / "repo")
                registry_path = root / "repositories.toml"
                registry_path.write_text(
                    "\n".join(
                        [
                            'default = "main"',
                            "",
                            "[repositories.main]",
                            'name = "Main"',
                            f"path = {json.dumps(str(repo))}",
                            "",
                            "[repositories.main.documents]",
                            f"{role} = {json.dumps(document_path)}",
                        ]
                    ),
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(RepositoryRegistryError, "document"):
                    RepositoryRegistry.from_toml(registry_path)

    def test_registry_rejects_symlinked_governing_document_path(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _git_repo(root / "repo")
            external = root / "outside.md"
            external.write_text("outside\n", encoding="utf-8")
            (repo / "docs").symlink_to(root)
            registry_path = root / "repositories.toml"
            registry_path.write_text(
                "\n".join(
                    [
                        'default = "main"',
                        "",
                        "[repositories.main]",
                        'name = "Main"',
                        f"path = {json.dumps(str(repo))}",
                        "",
                        "[repositories.main.documents]",
                        'workflow = "docs/outside.md"',
                    ]
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RepositoryRegistryError, "symlinked"):
                RepositoryRegistry.from_toml(registry_path)

    def test_registry_rejects_unsafe_milestone_root(self) -> None:
        for milestone_root in ("../outside", "/absolute/path", "milestones\\nested", "", ".", "a//b", "a/./b"):
            with self.subTest(milestone_root=milestone_root), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                repo = _git_repo(root / "repo")
                registry_path = root / "repositories.toml"
                registry_path.write_text(
                    "\n".join(
                        [
                            'default = "main"',
                            "",
                            "[repositories.main]",
                            'name = "Main"',
                            f'path = "{repo}"',
                            f"milestone_root = {json.dumps(milestone_root)}",
                        ]
                    ),
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(RepositoryRegistryError, "milestone_root"):
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
