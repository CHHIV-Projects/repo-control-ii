from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from repoctl.scanner.util import make_repository_id
from repoctl.workflow.milestones import (
    MilestoneArtifactError,
    discover_milestones,
    load_milestone_selection,
    save_milestone_selection,
)


def _git(repo: Path, *args: str) -> None:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace"))


def _repo(root: Path, name: str = "repo") -> Path:
    path = root / name
    path.mkdir()
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test User")
    (path / "tracked.txt").write_text("content\n", encoding="utf-8")
    _git(path, "add", "tracked.txt")
    _git(path, "commit", "-qm", "initial")
    return path


def _prompt(repo: Path, milestone_id: str, slug: str = "example") -> Path:
    path = repo / "docs" / "milestones" / f"{milestone_id}_{slug}_prompt.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# Prompt\n", encoding="utf-8")
    return path


def _artifact(repo: Path, relative_path: str, contents: str = "# Artifact\n") -> Path:
    path = repo / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents, encoding="utf-8")
    return path


class MilestoneDiscoveryTests(unittest.TestCase):
    def test_discovers_valid_prompts_conservatively_and_pairs_closeouts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = _repo(Path(td))
            first = _prompt(repo, "R003.01", "artifact_discovery")
            _prompt(repo, "R003.00", "architecture")
            (first.parent / "R003.01_artifact_discovery_closeout.md").write_text(
                "# Closeout\n", encoding="utf-8"
            )
            (first.parent / "notes.md").write_text("# Not a prompt\n", encoding="utf-8")
            (first.parent / "R003.01_bad-name_prompt.md").write_text("# malformed\n", encoding="utf-8")
            (first.parent / "R004.00_orphan_closeout.md").write_text("# orphan\n", encoding="utf-8")

            result = discover_milestones(repo)

            self.assertEqual(
                [(item["milestone_id"], item["closeout_exists"]) for item in result["candidates"]],
                [("R003.01", True), ("R003.00", False)],
            )
            self.assertTrue(any("Invalid milestone prompt or closeout filename" in issue for issue in result["issues"]))
            self.assertTrue(any("Orphan or unmatched" in issue for issue in result["issues"]))

    def test_canonical_recursive_discovery_pairs_across_folders_and_sorts_numeric_ids(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = _repo(Path(td))
            _artifact(repo, "milestones/M003.001_first_prompt.md")
            _artifact(repo, "milestones/arc_003/M003.010_name_prompt.md")
            _artifact(repo, "milestones/arc_003/2026/M003.010.001_sub_prompt.md")
            _artifact(repo, "milestones/arc_003/2026/M003.010.001.002_deep_prompt.md")
            _artifact(repo, "milestones/M004.001_next_prompt.md")
            _artifact(repo, "milestones/archive/M003.010_name_closeout.md")

            result = discover_milestones(repo, "milestones")

            self.assertEqual(
                [item["milestone_id"] for item in result["candidates"]],
                [
                    "M004.001",
                    "M003.010.001.002",
                    "M003.010.001",
                    "M003.010",
                    "M003.001",
                ],
            )
            paired = next(item for item in result["candidates"] if item["milestone_id"] == "M003.010")
            self.assertTrue(paired["closeout_exists"])
            self.assertEqual(
                paired["closeout_path"],
                "milestones/archive/M003.010_name_closeout.md",
            )
            self.assertEqual(result["issues"], [])

    def test_malformed_canonical_names_and_unknown_codes_are_reported_not_selected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = _repo(Path(td))
            milestone_root = repo / "docs" / "milestones"
            milestone_root.mkdir(parents=True)
            for filename in (
                "M03.001_short_arc_prompt.md",
                "M003.01_short_sequence_prompt.md",
                "003.001_missing_process_prompt.md",
                "X003.001_unknown_process_prompt.md",
                "M003.001_bad-name_prompt.md",
                "M003.001_extra_artifact_type_spec.md",
            ):
                (milestone_root / filename).write_text("# Invalid\n", encoding="utf-8")
            _artifact(repo, "docs/milestones/M003.001_valid_name_prompt.md")

            result = discover_milestones(repo)

            self.assertEqual([item["milestone_id"] for item in result["candidates"]], ["M003.001"])
            self.assertEqual(len(result["issues"]), 6)
            self.assertTrue(all("Invalid milestone prompt or closeout filename" in issue for issue in result["issues"]))

    def test_duplicate_ids_are_flagged_and_not_selectable(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = _repo(Path(td))
            _prompt(repo, "R003.01", "first")
            _prompt(repo, "R003.01", "second")

            result = discover_milestones(repo)

            self.assertEqual(result["candidates"], [])
            self.assertTrue(any("Ambiguous milestone ID R003.01" in issue for issue in result["issues"]))

    def test_duplicate_canonical_ids_across_folders_are_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = _repo(Path(td))
            _artifact(repo, "docs/milestones/arc_a/M003.001_first_prompt.md")
            _artifact(repo, "docs/milestones/arc_b/M003.001_second_prompt.md")

            result = discover_milestones(repo)

            self.assertEqual(result["candidates"], [])
            self.assertTrue(any("Ambiguous milestone ID M003.001" in issue for issue in result["issues"]))

    def test_multiple_closeouts_for_one_id_are_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = _repo(Path(td))
            _artifact(repo, "docs/milestones/M003.001_example_prompt.md")
            _artifact(repo, "docs/milestones/archive/M003.001_example_closeout.md")
            _artifact(repo, "docs/milestones/old/M003.001_other_closeout.md")

            result = discover_milestones(repo)

            self.assertFalse(result["candidates"][0]["closeout_exists"])
            self.assertTrue(any("Ambiguous closeouts for milestone M003.001" in issue for issue in result["issues"]))

    def test_symlinked_prompt_and_milestone_directory_escape_are_not_discovered(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _repo(root, "repo")
            outside = root / "outside.md"
            outside.write_text("# outside\n", encoding="utf-8")
            milestone_dir = repo / "docs" / "milestones"
            milestone_dir.mkdir(parents=True)
            (milestone_dir / "R003.01_symlink_prompt.md").symlink_to(outside)

            result = discover_milestones(repo)

            self.assertEqual(result["candidates"], [])
            self.assertTrue(any("symlinks are not selectable" in issue for issue in result["issues"]))

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _repo(root, "repo")
            outside = root / "outside"
            outside.mkdir()
            _prompt(outside, "R003.02")
            (repo / "docs").mkdir()
            (repo / "docs" / "milestones").symlink_to(outside, target_is_directory=True)
            result = discover_milestones(repo)
            self.assertEqual(result["candidates"], [])
            self.assertTrue(any("symlinked path components" in issue for issue in result["issues"]))

    def test_recursive_symlink_escape_is_not_traversed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _repo(root, "repo")
            outside = root / "outside"
            outside.mkdir()
            _artifact(outside, "M003.001_escape_prompt.md")
            milestone_dir = repo / "docs" / "milestones"
            milestone_dir.mkdir(parents=True)
            (milestone_dir / "linked").symlink_to(outside, target_is_directory=True)

            result = discover_milestones(repo)

            self.assertEqual(result["candidates"], [])
            self.assertTrue(any("subdirectory symlink is not traversed" in issue for issue in result["issues"]))

    def test_symlinked_closeout_is_not_reported_as_present(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _repo(root, "repo")
            prompt = _prompt(repo, "R003.01")
            outside = root / "outside-closeout.md"
            outside.write_text("# outside\n", encoding="utf-8")
            prompt.with_name("R003.01_example_closeout.md").symlink_to(outside)

            result = discover_milestones(repo)

            self.assertFalse(result["candidates"][0]["closeout_exists"])
            self.assertTrue(any("closeout symlinks are not selectable" in issue.lower() for issue in result["issues"]))

    def test_selection_is_atomic_repository_scoped_and_survives_reload(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo_a = _repo(root, "repo-a")
            repo_b = _repo(root, "repo-b")
            prompt_a = _prompt(repo_a, "R003.01", "a")
            prompt_b = _prompt(repo_b, "R004.01", "b")
            state_root = root / "state"

            saved_a = save_milestone_selection(
                repo_a, state_root, "R003.01"
            )
            saved_b = save_milestone_selection(
                repo_b, state_root, "R004.01"
            )

            self.assertEqual(saved_a["prompt_path"], "docs/milestones/R003.01_a_prompt.md")
            self.assertEqual(saved_b["prompt_path"], "docs/milestones/R004.01_b_prompt.md")
            self.assertEqual(
                load_milestone_selection(repo_a, state_root)["selection"]["prompt_path"],
                "docs/milestones/R003.01_a_prompt.md",
            )
            self.assertEqual(
                load_milestone_selection(repo_b, state_root)["selection"]["prompt_path"],
                "docs/milestones/R004.01_b_prompt.md",
            )
            pointer = state_root / make_repository_id(repo_a) / "milestones" / "selection.json"
            self.assertEqual(json.loads(pointer.read_text(encoding="utf-8"))["repository_id"], make_repository_id(repo_a))
            self.assertEqual(list(pointer.parent.glob(".selection-*.tmp")), [])
            self.assertFalse((state_root / make_repository_id(repo_b) / "milestones" / "selection.json").resolve() == pointer.resolve())
            self.assertTrue(prompt_a.is_file())
            self.assertTrue(prompt_b.is_file())
            self.assertEqual((repo_a / "tracked.txt").read_text(encoding="utf-8"), "content\n")
            self.assertEqual((repo_b / "tracked.txt").read_text(encoding="utf-8"), "content\n")

    def test_reselection_replaces_pointer_and_does_not_mutate_git(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _repo(root)
            _prompt(repo, "R003.01", "first")
            _prompt(repo, "R003.02", "second")
            state_root = root / "state"
            before = subprocess.run(
                ["git", "-C", str(repo), "status", "--porcelain"],
                stdout=subprocess.PIPE,
                check=True,
            ).stdout

            save_milestone_selection(repo, state_root, "R003.01")
            save_milestone_selection(repo, state_root, "R003.02")

            result = load_milestone_selection(repo, state_root)
            after = subprocess.run(
                ["git", "-C", str(repo), "status", "--porcelain"],
                stdout=subprocess.PIPE,
                check=True,
            ).stdout
            self.assertEqual(result["selection"]["milestone_id"], "R003.02")
            self.assertEqual(before, after)

    def test_invalid_pointer_states_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _repo(root)
            _prompt(repo, "R003.01")
            outside_prompt = root / "outside-prompt.md"
            outside_prompt.write_text("# outside\n", encoding="utf-8")
            symlink_prompt = repo / "docs" / "milestones" / "R003.02_escape_prompt.md"
            symlink_prompt.symlink_to(outside_prompt)
            state_root = root / "state"
            pointer = state_root / make_repository_id(repo) / "milestones" / "selection.json"
            pointer.parent.mkdir(parents=True)

            self.assertEqual(load_milestone_selection(repo, state_root)["status"], "none")
            bad_values = [
                b"{",
                json.dumps({"schema_version": 9}).encode(),
                json.dumps(
                    {
                        "schema_version": 1,
                        "repository_id": "other-repo",
                        "milestone_id": "R003.01",
                        "prompt_path": "docs/milestones/R003.01_example_prompt.md",
                    }
                ).encode(),
                json.dumps(
                    {
                        "schema_version": 1,
                        "repository_id": make_repository_id(repo),
                        "milestone_id": "R3.1",
                        "prompt_path": "docs/milestones/R003.01_example_prompt.md",
                    }
                ).encode(),
                json.dumps(
                    {
                        "schema_version": 1,
                        "repository_id": make_repository_id(repo),
                        "milestone_id": "R003.01",
                        "prompt_path": "../outside.md",
                    }
                ).encode(),
                json.dumps(
                    {
                        "schema_version": 1,
                        "repository_id": make_repository_id(repo),
                        "milestone_id": "R003.01",
                        "prompt_path": str(outside_prompt),
                    }
                ).encode(),
                json.dumps(
                    {
                        "schema_version": 1,
                        "repository_id": make_repository_id(repo),
                        "milestone_id": "R003.02",
                        "prompt_path": "docs/milestones/R003.02_escape_prompt.md",
                    }
                ).encode(),
                json.dumps(
                    {
                        "schema_version": 1,
                        "repository_id": make_repository_id(repo),
                        "milestone_id": "R003.02",
                        "prompt_path": "docs/milestones/R003.01_example_prompt.md",
                    }
                ).encode(),
                json.dumps(
                    {
                        "schema_version": 1,
                        "repository_id": make_repository_id(repo),
                        "milestone_id": "R003.01",
                        "prompt_path": "docs/milestones/missing_prompt.md",
                    }
                ).encode(),
            ]
            for invalid in bad_values:
                with self.subTest(invalid=invalid):
                    pointer.write_bytes(invalid)
                    selection = load_milestone_selection(repo, state_root)
                    self.assertEqual(selection["status"], "invalid")
                    self.assertIsNone(selection["selection"])
                    self.assertIn("select", selection["reason"].lower())

    def test_save_rejects_unlisted_id_and_state_inside_repository(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _repo(root)
            _prompt(repo, "R003.01")
            with self.assertRaisesRegex(MilestoneArtifactError, "candidate"):
                save_milestone_selection(repo, root / "state", "R003.02")
            with self.assertRaisesRegex(MilestoneArtifactError, "outside"):
                save_milestone_selection(repo, repo / "state", "R003.01")

    def test_unreadable_pointer_reports_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _repo(root)
            _prompt(repo, "R003.01")
            state_root = root / "state"
            pointer = state_root / make_repository_id(repo) / "milestones" / "selection.json"
            pointer.parent.mkdir(parents=True)
            pointer.symlink_to(root / "missing-pointer")
            result = load_milestone_selection(repo, state_root)
            self.assertEqual(result["status"], "invalid")


if __name__ == "__main__":
    unittest.main()
