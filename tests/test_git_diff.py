from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from repoctl.scanner.git_ops import ScanError, get_working_tree_with_branch
from repoctl.workflow.diff import (
    MAX_DIFF_OUTPUT_BYTES,
    MAX_UNTRACKED_PREVIEW_BYTES,
    GitReviewError,
    inspect_git_review,
)
from repoctl.workflow.git_state import WorkflowGitStateError


def _git(repo: Path, *args: str) -> bytes:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", errors="replace"))
    return proc.stdout


def _init_repo(root: Path) -> Path:
    repo = root / "review-repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.com")
    (repo / "tracked.txt").write_text("original\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-qm", "initial")
    return repo


def _review_file(repo: Path, path: str, *, selected_side: str | None = None) -> dict:
    review = inspect_git_review(str(repo))
    item = next(file for file in review["files"] if file["path"] == path)
    if selected_side is not None:
        review = inspect_git_review(str(repo), selected_file=item["token"])
        return next(diff for diff in review["selected"]["diffs"] if diff["side"].startswith(selected_side))
    return item


class GitDiffTests(unittest.TestCase):
    def test_clean_repository_and_unstaged_tracked_change(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = _init_repo(Path(td))
            clean = inspect_git_review(str(repo))
            self.assertEqual(clean["workflow_state"], "clean")
            self.assertEqual(clean["files"], [])

            (repo / "tracked.txt").write_text("changed\n", encoding="utf-8")
            item = _review_file(repo, "tracked.txt")
            self.assertEqual(item["label"], "Unstaged")
            diff = _review_file(repo, "tracked.txt", selected_side="Unstaged")
            self.assertIn("-original", diff["text"])
            self.assertIn("+changed", diff["text"])
            self.assertEqual(diff["side"], "Unstaged — working tree vs index")

    def test_staged_and_unstaged_same_file_have_separate_diffs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = _init_repo(Path(td))
            tracked = repo / "tracked.txt"
            tracked.write_text("staged value\n", encoding="utf-8")
            _git(repo, "add", "tracked.txt")
            tracked.write_text("unstaged value\n", encoding="utf-8")

            review = inspect_git_review(str(repo))
            item = next(file for file in review["files"] if file["path"] == "tracked.txt")
            self.assertEqual(item["label"], "Staged + unstaged")
            selected = inspect_git_review(str(repo), selected_file=item["token"])["selected"]
            staged, unstaged = selected["diffs"]
            self.assertEqual(staged["side"], "Staged — index vs HEAD")
            self.assertEqual(unstaged["side"], "Unstaged — working tree vs index")
            self.assertIn("+staged value", staged["text"])
            self.assertNotIn("unstaged value", staged["text"])
            self.assertIn("+unstaged value", unstaged["text"])
            self.assertNotIn("+staged value", unstaged["text"])

    def test_diagnosis_and_preview_semantics_for_staged_and_unstaged_changes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = _init_repo(Path(td))
            tracked = repo / "tracked.txt"
            tracked.write_text("staged version\n", encoding="utf-8")
            _git(repo, "add", "tracked.txt")
            tracked.write_text("current worktree version\n", encoding="utf-8")

            listing = inspect_git_review(str(repo))
            item = next(file for file in listing["files"] if file["path"] == "tracked.txt")
            selected = inspect_git_review(str(repo), selected_file=item["token"])["selected"]
            previews = {preview["key"]: preview for preview in selected["action_previews"]}

            self.assertEqual(
                selected["diagnosis"]["comparison"],
                "Staged: index vs HEAD. Unstaged: working tree vs index.",
            )
            self.assertIn("The index differs from HEAD", selected["diagnosis"]["known"][0])
            self.assertIn("intended", selected["diagnosis"]["unknown"][0])
            self.assertIn("active milestone/file-scope", selected["diagnosis"]["scope"])
            self.assertEqual(previews["unstage"]["affected_layers"], ["index"])
            self.assertIn("worktree would remain", previews["unstage"]["preserves"][0])
            self.assertTrue(previews["unstage"]["possible_content_loss"])
            self.assertIn("+staged version", previews["unstage"]["evidence"][0]["text"])

            self.assertEqual(previews["restore-unstaged"]["affected_layers"], ["worktree"])
            self.assertIn("staged changes", previews["restore-unstaged"]["preserves"][0])
            self.assertIn("+current worktree version", previews["restore-unstaged"]["evidence"][0]["text"])

            restore_head = previews["restore-head"]
            self.assertEqual(restore_head["affected_layers"], ["index", "worktree"])
            self.assertTrue(restore_head["possible_content_loss"])
            self.assertEqual(restore_head["evidence"][0]["side"], "Staged — index vs HEAD")
            self.assertEqual(restore_head["evidence"][1]["side"], "Worktree vs HEAD")
            self.assertIn("+current worktree version", restore_head["evidence"][1]["text"])
            self.assertEqual(restore_head["evidence"][1]["head_oid"], selected["diagnosis"]["head_oid"])

            requested = inspect_git_review(
                str(repo),
                selected_file=item["token"],
                preview_action="restore-unstaged",
            )["selected"]["action_preview"]
            self.assertEqual(requested["key"], "restore-unstaged")
            self.assertIn("No Git operation", inspect_git_review(
                str(repo), selected_file=item["token"], preview_action="leave-unchanged"
            )["selected"]["action_preview"]["comparison"])

    def test_whitespace_equivalence_is_comparison_specific_and_advisory(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = _init_repo(Path(td))
            tracked = repo / "tracked.txt"
            tracked.write_text("original \n", encoding="utf-8")
            listing = inspect_git_review(str(repo))
            item = next(file for file in listing["files"] if file["path"] == "tracked.txt")
            whitespace_diff = inspect_git_review(str(repo), selected_file=item["token"])["selected"]["diffs"][0]
            self.assertEqual(whitespace_diff["whitespace_comparison"]["state"], "whitespace_only")
            self.assertIn("--ignore-all-space", whitespace_diff["whitespace_comparison"]["label"])
            self.assertIn("+original ", whitespace_diff["text"])

            tracked.write_text("different substantive value\n", encoding="utf-8")
            substantive = inspect_git_review(str(repo), selected_file=item["token"])["selected"]["diffs"][0]
            self.assertEqual(
                substantive["whitespace_comparison"]["state"],
                "non_whitespace_difference",
            )

    def test_untracked_conflict_deleted_and_bounded_preview_options(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = _init_repo(Path(td))
            new_file = repo / "new.txt"
            new_file.write_text("new content\n", encoding="utf-8")
            listing = inspect_git_review(str(repo))
            untracked = next(file for file in listing["files"] if file["path"] == "new.txt")
            selected = inspect_git_review(str(repo), selected_file=untracked["token"])["selected"]
            preview_keys = {preview["key"] for preview in selected["action_previews"]}
            self.assertEqual(preview_keys, {"leave-unchanged", "keep-for-later"})
            with self.assertRaisesRegex(ValueError, "not available"):
                inspect_git_review(str(repo), selected_file=untracked["token"], preview_action="restore-head")

            (repo / "tracked.txt").unlink()
            listing = inspect_git_review(str(repo))
            deleted = next(file for file in listing["files"] if file["path"] == "tracked.txt")
            deleted_selected = inspect_git_review(str(repo), selected_file=deleted["token"])["selected"]
            deleted_preview = next(
                preview for preview in deleted_selected["action_previews"]
                if preview["key"] == "restore-unstaged"
            )
            self.assertTrue(deleted_preview["available"])
            self.assertIn("deleted file", deleted_preview["evidence"][0]["text"])

    def test_unstage_preview_explains_staged_addition_and_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = _init_repo(Path(td))
            added = repo / "added.txt"
            added.write_text("staged new file\n", encoding="utf-8")
            _git(repo, "add", "added.txt")
            listing = inspect_git_review(str(repo))
            added_item = next(file for file in listing["files"] if file["path"] == "added.txt")
            added_selected = inspect_git_review(str(repo), selected_file=added_item["token"])["selected"]
            added_unstage = next(
                preview for preview in added_selected["action_previews"]
                if preview["key"] == "unstage"
            )
            self.assertIn("become untracked", added_unstage["preserves"][0])

            _git(repo, "rm", "tracked.txt")
            listing = inspect_git_review(str(repo))
            deleted_item = next(file for file in listing["files"] if file["path"] == "tracked.txt")
            deleted_selected = inspect_git_review(str(repo), selected_file=deleted_item["token"])["selected"]
            deleted_unstage = next(
                preview for preview in deleted_selected["action_previews"]
                if preview["key"] == "unstage"
            )
            self.assertIn("deletion would become unstaged", deleted_unstage["preserves"][0])

    def test_untracked_text_binary_and_large_preview_bounds(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = _init_repo(Path(td))
            (repo / "new.txt").write_text("untracked text\n", encoding="utf-8")
            (repo / "binary.dat").write_bytes(b"\x00\x01binary")
            (repo / "undecodable.txt").write_bytes(b"\xff\xfe")
            (repo / "outside-link").symlink_to(Path(td) / "outside")
            large = repo / "large.txt"
            large.write_bytes(b"a" * (MAX_UNTRACKED_PREVIEW_BYTES + 100))

            review = inspect_git_review(str(repo))
            by_path = {item["path"]: item for item in review["files"]}
            self.assertEqual(by_path["new.txt"]["label"], "Untracked")
            self.assertEqual(by_path["binary.dat"]["label"], "Untracked")
            for path in ("new.txt", "binary.dat", "undecodable.txt", "outside-link", "large.txt"):
                selected = inspect_git_review(str(repo), selected_file=by_path[path]["token"])["selected"]
                self.assertIsNotNone(selected["preview"])

            text_preview = inspect_git_review(
                str(repo), selected_file=by_path["new.txt"]["token"]
            )["selected"]["preview"]
            self.assertEqual(text_preview["text"], "untracked text\n")
            self.assertTrue(text_preview["complete"])

            binary_preview = inspect_git_review(
                str(repo), selected_file=by_path["binary.dat"]["token"]
            )["selected"]["preview"]
            self.assertFalse(binary_preview["available"])
            self.assertIn("Binary", binary_preview["reason"])

            undecodable_preview = inspect_git_review(
                str(repo), selected_file=by_path["undecodable.txt"]["token"]
            )["selected"]["preview"]
            self.assertFalse(undecodable_preview["available"])
            self.assertIn("UTF-8", undecodable_preview["reason"])

            symlink_preview = inspect_git_review(
                str(repo), selected_file=by_path["outside-link"]["token"]
            )["selected"]["preview"]
            self.assertFalse(symlink_preview["available"])
            self.assertIn("Symbolic-link", symlink_preview["reason"])

            large_preview = inspect_git_review(
                str(repo), selected_file=by_path["large.txt"]["token"]
            )["selected"]["preview"]
            self.assertTrue(large_preview["available"])
            self.assertFalse(large_preview["complete"])
            self.assertEqual(len(large_preview["text"].encode("utf-8")), MAX_UNTRACKED_PREVIEW_BYTES)

    def test_binary_and_large_tracked_diffs_are_explicit_and_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = _init_repo(Path(td))
            binary = repo / "image.bin"
            binary.write_bytes(b"\x00before")
            _git(repo, "add", "image.bin")
            _git(repo, "commit", "-qm", "add binary")
            binary.write_bytes(b"\x00after")

            binary_review = inspect_git_review(str(repo))
            binary_file = next(item for item in binary_review["files"] if item["path"] == "image.bin")
            binary_diff = inspect_git_review(str(repo), selected_file=binary_file["token"])["selected"]["diffs"][0]
            self.assertFalse(binary_diff["available"])
            self.assertIn("binary", binary_diff["reason"].lower())

            large = repo / "large.txt"
            large.write_text("a" * (MAX_DIFF_OUTPUT_BYTES * 2), encoding="utf-8")
            _git(repo, "add", "large.txt")
            _git(repo, "commit", "-qm", "add large")
            large.write_text("b" * (MAX_DIFF_OUTPUT_BYTES * 2), encoding="utf-8")
            large_review = inspect_git_review(str(repo))
            large_file = next(item for item in large_review["files"] if item["path"] == "large.txt")
            large_diff = inspect_git_review(str(repo), selected_file=large_file["token"])["selected"]["diffs"][0]
            self.assertTrue(large_diff["truncated"])
            self.assertFalse(large_diff["complete"])
            self.assertLessEqual(len(large_diff["text"].encode("utf-8")), MAX_DIFF_OUTPUT_BYTES)
            large_previews = {
                preview["key"]: preview for preview in inspect_git_review(
                    str(repo), selected_file=large_file["token"]
                )["selected"]["action_previews"]
            }
            self.assertFalse(large_previews["restore-unstaged"]["available"])
            self.assertFalse(large_previews["restore-unstaged"]["complete"])

            undecodable = repo / "undecodable.txt"
            undecodable.write_bytes(b"\xff")
            _git(repo, "add", "undecodable.txt")
            _git(repo, "commit", "-qm", "add undecodable text")
            undecodable.write_bytes(b"\xfe")
            undecodable_review = inspect_git_review(str(repo))
            undecodable_file = next(
                item for item in undecodable_review["files"] if item["path"] == "undecodable.txt"
            )
            undecodable_diff = inspect_git_review(
                str(repo), selected_file=undecodable_file["token"]
            )["selected"]["diffs"][0]
            self.assertFalse(undecodable_diff["available"])
            self.assertIn("undecodable", undecodable_diff["reason"])

    def test_whitespace_warning_does_not_hide_normal_diff(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = _init_repo(Path(td))
            (repo / "tracked.txt").write_text("changed with trailing space \n", encoding="utf-8")
            diff = _review_file(repo, "tracked.txt", selected_side="Unstaged")
            self.assertTrue(diff["available"])
            self.assertIn("changed with trailing space", diff["text"])
            self.assertEqual(diff["whitespace"]["label"], "Whitespace warning")
            self.assertIn("trailing whitespace", diff["whitespace"]["details"])

    def test_path_names_are_path_safe_and_non_utf8_bytes_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = _init_repo(Path(td))
            strange_name = "space tab\tline\n<script>.txt"
            (repo / strange_name).write_text("content\n", encoding="utf-8")

            raw_name = b"nonutf8-\xff.txt"
            fd = os.open(os.fsencode(repo) + b"/" + raw_name, os.O_WRONLY | os.O_CREAT, 0o600)
            try:
                os.write(fd, b"non utf8 path\n")
            finally:
                os.close(fd)

            review = inspect_git_review(str(repo))
            paths = {item["path"]: item for item in review["files"]}
            self.assertIn(strange_name, paths)
            non_utf8_path = os.fsdecode(raw_name)
            self.assertIn(non_utf8_path, paths)
            self.assertIn("\\xff", paths[non_utf8_path]["display_path"])

            selected = inspect_git_review(str(repo), selected_file=paths[strange_name]["token"])["selected"]
            self.assertEqual(selected["preview"]["text"], "content\n")

    def test_staged_deletion_and_rename_are_visible(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = _init_repo(Path(td))
            _git(repo, "mv", "tracked.txt", "renamed.txt")
            rename_review = inspect_git_review(str(repo))
            renamed = next(item for item in rename_review["files"] if item["path"] == "renamed.txt")
            self.assertEqual(renamed["kind"], "rename_or_copy")
            self.assertEqual(renamed["original_path"], "tracked.txt")
            renamed_selected = inspect_git_review(str(repo), selected_file=renamed["token"])["selected"]
            self.assertEqual(
                [preview["key"] for preview in renamed_selected["action_previews"]],
                ["leave-unchanged"],
            )

            _git(repo, "reset", "--hard")
            _git(repo, "rm", "tracked.txt")
            deletion_review = inspect_git_review(str(repo))
            deleted = next(item for item in deletion_review["files"] if item["path"] == "tracked.txt")
            selected = inspect_git_review(str(repo), selected_file=deleted["token"])["selected"]
            self.assertTrue(selected["diffs"])
            self.assertIn("deleted file", selected["diffs"][0]["text"])

    def test_conflicts_are_explicit_and_not_reported_as_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = _init_repo(Path(td))
            main_branch = _git(repo, "branch", "--show-current").decode().strip()
            _git(repo, "branch", "other")
            (repo / "tracked.txt").write_text("main change\n", encoding="utf-8")
            _git(repo, "commit", "-qam", "main edit")
            _git(repo, "checkout", "other")
            (repo / "tracked.txt").write_text("other change\n", encoding="utf-8")
            _git(repo, "commit", "-qam", "other edit")
            _git(repo, "checkout", main_branch)
            proc = subprocess.run(
                ["git", "-C", str(repo), "merge", "other"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertNotEqual(proc.returncode, 0)

            review = inspect_git_review(str(repo))
            self.assertEqual(review["workflow_state"], "conflicted")
            item = next(file for file in review["files"] if file["path"] == "tracked.txt")
            self.assertTrue(item["conflicted"])
            selected = inspect_git_review(str(repo), selected_file=item["token"])["selected"]
            self.assertIn("unmerged", selected["conflict_note"])
            self.assertEqual(selected["diffs"], [])
            self.assertEqual(
                [preview["key"] for preview in selected["action_previews"]],
                ["leave-unchanged"],
            )

    def test_status_output_limit_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = _init_repo(Path(td))
            with self.assertRaisesRegex(ScanError, "state is incomplete"):
                get_working_tree_with_branch(repo, max_output_bytes=8)

    def test_git_failure_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = _init_repo(Path(td))
            with mock.patch(
                "repoctl.workflow.diff.inspect_git_state",
                side_effect=WorkflowGitStateError("git status failed"),
            ):
                with self.assertRaisesRegex(GitReviewError, "git status failed"):
                    inspect_git_review(str(repo))


if __name__ == "__main__":
    unittest.main()
