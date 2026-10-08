from __future__ import annotations

import hashlib
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from repoctl.web.app import create_web_app
from repoctl.workflow.diff import inspect_git_review
from repoctl.workflow.git_actions import GitActionError, PreparedGitActions


def _git(repo: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace"))
    return result.stdout


def _text(repo: Path, *args: str) -> str:
    return _git(repo, *args).decode("utf-8", errors="strict").strip()


def _status(repo: Path) -> str:
    return _git(repo, "status", "--porcelain").decode("utf-8", errors="strict").rstrip("\n")


def _status_lines(repo: Path) -> set[str]:
    return set(_status(repo).splitlines())


def _repo(root: Path) -> Path:
    repo = root / "actions-repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Action Test")
    _git(repo, "config", "user.email", "actions@example.test")
    (repo / "tracked.txt").write_text("original\n", encoding="utf-8")
    (repo / "other.txt").write_text("other original\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "initial")
    return repo


def _token(repo: Path, path: str) -> str:
    review = inspect_git_review(str(repo))
    selected = next(item for item in review["files"] if item["path"] == path)
    return selected["token"]


def _prepare(actions: PreparedGitActions, repo: Path, action: str, path: str) -> str:
    token, _preview = actions.prepare(
        str(repo),
        action,
        path_token=_token(repo, path),
    )
    return token


def _prepare_many(actions: PreparedGitActions, repo: Path, action: str, paths: list[str]) -> tuple[str, dict]:
    tokens = [_token(repo, path) for path in paths]
    return actions.prepare(str(repo), action, path_tokens=tokens)


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix().encode()
        digest.update(relative)
        if path.is_symlink():
            digest.update(b"link\0" + str(path.readlink()).encode())
        elif path.is_file():
            digest.update(b"file\0" + path.read_bytes())
        elif path.is_dir():
            digest.update(b"dir\0")
    return digest.hexdigest()


class GitActionTests(unittest.TestCase):
    def test_batch_stage_explicit_tracked_untracked_and_unselected_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = _repo(Path(temporary))
            actions = PreparedGitActions()
            (repo / "tracked.txt").write_text("staged tracked\n", encoding="utf-8")
            (repo / "other.txt").write_text("leave unstaged\n", encoding="utf-8")
            (repo / "new file.txt").write_text("staged untracked\n", encoding="utf-8")

            token, preview = _prepare_many(actions, repo, "stage", ["new file.txt", "tracked.txt"])
            self.assertEqual([item["path"] for item in preview["paths"]], ["new file.txt", "tracked.txt"])
            self.assertEqual(_status_lines(repo), {' M tracked.txt', ' M other.txt', '?? "new file.txt"'})
            result = actions.confirm(str(repo), "stage", token)

            self.assertEqual(result["paths"], ["new file.txt", "tracked.txt"])
            self.assertEqual(_status_lines(repo), {'A  "new file.txt"', "M  tracked.txt", " M other.txt"})
            self.assertEqual(_git(repo, "show", ":tracked.txt"), b"staged tracked\n")
            self.assertEqual(_git(repo, "show", ":new file.txt"), b"staged untracked\n")

    def test_batch_unstage_preserves_worktrees_and_unselected_staged_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = _repo(Path(temporary))
            actions = PreparedGitActions()
            for path, content in (
                ("tracked.txt", "first staged\n"),
                ("other.txt", "second staged\n"),
            ):
                (repo / path).write_text(content, encoding="utf-8")
                _git(repo, "add", path)
            (repo / "unselected staged.txt").write_text("keep staged\n", encoding="utf-8")
            _git(repo, "add", "unselected staged.txt")

            token, preview = _prepare_many(actions, repo, "unstage", ["tracked.txt", "other.txt"])
            self.assertIn("INDEX WILL CHANGE", preview["warning"])
            self.assertIn("WORKTREE CONTENT WILL NOT BE REPLACED", preview["warning"])
            actions.confirm(str(repo), "unstage", token)

            self.assertEqual((repo / "tracked.txt").read_text(), "first staged\n")
            self.assertEqual((repo / "other.txt").read_text(), "second staged\n")
            self.assertEqual(_git(repo, "show", ":unselected staged.txt"), b"keep staged\n")
            self.assertEqual(_status_lines(repo), {" M tracked.txt", " M other.txt", 'A  "unselected staged.txt"'})

    def test_batch_restore_modes_confirm_once_and_leave_unselected_path_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = _repo(Path(temporary))
            actions = PreparedGitActions()
            (repo / "tracked.txt").write_text("restore first\n", encoding="utf-8")
            (repo / "other.txt").write_text("restore second\n", encoding="utf-8")
            token, preview = _prepare_many(actions, repo, "restore-unstaged", ["tracked.txt", "other.txt"])
            self.assertIn("UNSTAGED WORKTREE CONTENT FOR ALL SELECTED PATHS WILL BE DISCARDED", preview["warning"])
            self.assertEqual(len(preview["paths"]), 2)
            actions.confirm(str(repo), "restore-unstaged", token)
            self.assertEqual((repo / "tracked.txt").read_text(), "original\n")
            self.assertEqual((repo / "other.txt").read_text(), "other original\n")
            self.assertEqual(_status(repo), "")

            (repo / "tracked.txt").write_text("restore head first\n", encoding="utf-8")
            (repo / "other.txt").write_text("restore head second\n", encoding="utf-8")
            token, preview = _prepare_many(actions, repo, "restore-head", ["tracked.txt", "other.txt"])
            self.assertIn("STAGED AND UNSTAGED CHANGES FOR THE SELECTED PATHS MAY BE DISCARDED", preview["warning"])
            actions.confirm(str(repo), "restore-head", token)
            self.assertEqual((repo / "tracked.txt").read_text(), "original\n")
            self.assertEqual((repo / "other.txt").read_text(), "other original\n")
            self.assertEqual(_status(repo), "")

    def test_batch_mixed_ineligible_injected_duplicate_and_oversized_selection_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = _repo(Path(temporary))
            actions = PreparedGitActions()
            (repo / "tracked.txt").write_text("unstaged\n", encoding="utf-8")
            (repo / "other.txt").write_text("staged\n", encoding="utf-8")
            _git(repo, "add", "other.txt")
            with self.assertRaisesRegex(GitActionError, "does not support"):
                _prepare_many(actions, repo, "restore-unstaged", ["tracked.txt", "other.txt"])
            with self.assertRaisesRegex(GitActionError, "no longer available"):
                actions.prepare(str(repo), "stage", path_tokens=["not-a-status-token"])
            token = _token(repo, "tracked.txt")
            with self.assertRaisesRegex(GitActionError, "duplicate"):
                actions.prepare(str(repo), "stage", path_tokens=[token, token])
            with self.assertRaisesRegex(GitActionError, "safe action limit"):
                actions.prepare(str(repo), "stage", path_tokens=[token] * 257)

    def test_one_stale_batch_member_blocks_every_command_before_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = _repo(Path(temporary))
            actions = PreparedGitActions()
            (repo / "tracked.txt").write_text("valid selected path\n", encoding="utf-8")
            (repo / "other.txt").write_text("prepared selected path\n", encoding="utf-8")
            token, preview = _prepare_many(actions, repo, "stage", ["tracked.txt", "other.txt"])
            self.assertEqual(len(preview["paths"]), 2)
            (repo / "other.txt").write_text("changed after review\n", encoding="utf-8")

            with mock.patch("repoctl.workflow.git_actions._run_mutation") as mutate:
                with self.assertRaisesRegex(GitActionError, "stale"):
                    actions.confirm(str(repo), "stage", token)
            mutate.assert_not_called()
            self.assertEqual(_status_lines(repo), {" M tracked.txt", " M other.txt"})

    def test_stale_batch_member_blocks_unstage_and_both_restore_modes(self) -> None:
        for action in ("unstage", "restore-unstaged", "restore-head"):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as temporary:
                repo = _repo(Path(temporary))
                actions = PreparedGitActions()
                for path in ("tracked.txt", "other.txt"):
                    (repo / path).write_text(f"prepared {action}\n", encoding="utf-8")
                    if action in {"unstage", "restore-head"}:
                        _git(repo, "add", path)
                        if action == "restore-head":
                            (repo / path).write_text(f"unstaged {action}\n", encoding="utf-8")
                token, _preview = _prepare_many(actions, repo, action, ["tracked.txt", "other.txt"])
                (repo / "other.txt").write_text("stale before execution\n", encoding="utf-8")

                with mock.patch("repoctl.workflow.git_actions._run_mutation") as mutate:
                    with self.assertRaisesRegex(GitActionError, "stale"):
                        actions.confirm(str(repo), action, token)
                mutate.assert_not_called()
                if action in {"unstage", "restore-head"}:
                    self.assertEqual(_git(repo, "show", ":tracked.txt"), b"prepared " + action.encode() + b"\n")
                self.assertEqual((repo / "tracked.txt").read_text(), f"prepared {action}\n" if action != "restore-head" else f"unstaged {action}\n")

    def test_stage_unstage_and_selected_path_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = _repo(Path(temporary))
            actions = PreparedGitActions()

            (repo / "tracked.txt").write_text("modified\n", encoding="utf-8")
            token, preview = actions.prepare(str(repo), "stage", path_token=_token(repo, "tracked.txt"))
            self.assertIn("index", preview["layers"])
            self.assertEqual(_status(repo), " M tracked.txt")
            result = actions.confirm(str(repo), "stage", token)
            self.assertEqual(result["action"], "stage")
            self.assertEqual(_status(repo), "M  tracked.txt")
            with self.assertRaisesRegex(GitActionError, "unknown, expired, or already used"):
                actions.confirm(str(repo), "stage", token)

            token = _prepare(actions, repo, "unstage", "tracked.txt")
            result = actions.confirm(str(repo), "unstage", token)
            self.assertIn("worktree content remains unchanged", result["message"])
            self.assertEqual(_status(repo), " M tracked.txt")

            (repo / "new file.txt").write_text("new content\n", encoding="utf-8")
            token = _prepare(actions, repo, "stage", "new file.txt")
            actions.confirm(str(repo), "stage", token)
            self.assertEqual(_status_lines(repo), {'A  "new file.txt"', " M tracked.txt"})
            token = _prepare(actions, repo, "unstage", "new file.txt")
            actions.confirm(str(repo), "unstage", token)
            self.assertEqual(_status_lines(repo), {'?? "new file.txt"', " M tracked.txt"})

            (repo / "tracked.txt").unlink()
            token = _prepare(actions, repo, "stage", "tracked.txt")
            actions.confirm(str(repo), "stage", token)
            self.assertEqual(_status_lines(repo), {'D  tracked.txt', '?? "new file.txt"'})
            token = _prepare(actions, repo, "unstage", "tracked.txt")
            actions.confirm(str(repo), "unstage", token)
            self.assertEqual(_status_lines(repo), {' D tracked.txt', '?? "new file.txt"'})

            special = ":(glob)*.txt"
            (repo / special).write_text("literal path\n", encoding="utf-8")
            (repo / "victim.txt").write_text("must remain untracked\n", encoding="utf-8")
            token = _prepare(actions, repo, "stage", special)
            actions.confirm(str(repo), "stage", token)
            self.assertEqual(_git(repo, "ls-files", "--", f":(literal){special}").decode(), f"{special}\n")
            self.assertEqual(_git(repo, "ls-files", "--", "victim.txt"), b"")

    def test_stage_and_unstage_preserve_selected_worktree_and_other_index_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = _repo(Path(temporary))
            actions = PreparedGitActions()
            (repo / "other.txt").write_text("staged other\n", encoding="utf-8")
            _git(repo, "add", "other.txt")
            (repo / "tracked.txt").write_text("staged selected\n", encoding="utf-8")
            _git(repo, "add", "tracked.txt")
            (repo / "tracked.txt").write_text("worktree selected\n", encoding="utf-8")

            token = _prepare(actions, repo, "stage", "tracked.txt")
            actions.confirm(str(repo), "stage", token)
            self.assertEqual(_git(repo, "show", ":tracked.txt"), b"worktree selected\n")
            self.assertEqual((repo / "tracked.txt").read_text(), "worktree selected\n")
            self.assertEqual(_git(repo, "show", ":other.txt"), b"staged other\n")
            self.assertEqual(_status(repo), "M  other.txt\nM  tracked.txt")

            (repo / "tracked.txt").write_text("new unstaged state\n", encoding="utf-8")
            token = _prepare(actions, repo, "unstage", "tracked.txt")
            actions.confirm(str(repo), "unstage", token)
            self.assertEqual((repo / "tracked.txt").read_text(), "new unstaged state\n")
            self.assertEqual(_git(repo, "show", ":tracked.txt"), b"original\n")
            self.assertEqual(_git(repo, "show", ":other.txt"), b"staged other\n")
            self.assertEqual(_status(repo), "M  other.txt\n M tracked.txt")

    def test_restore_unstaged_and_restore_to_head_are_confirmed_and_verified(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = _repo(Path(temporary))
            actions = PreparedGitActions()

            (repo / "tracked.txt").write_text("unstaged\n", encoding="utf-8")
            token, preview = actions.prepare(str(repo), "restore-unstaged", path_token=_token(repo, "tracked.txt"))
            self.assertIn("discards unstaged worktree content", preview["warning"])
            self.assertEqual((repo / "tracked.txt").read_text(), "unstaged\n")
            result = actions.confirm(str(repo), "restore-unstaged", token)
            self.assertIn("no longer present", result["message"])
            self.assertEqual((repo / "tracked.txt").read_text(), "original\n")

            (repo / "tracked.txt").write_text("staged version\n", encoding="utf-8")
            _git(repo, "add", "tracked.txt")
            (repo / "tracked.txt").write_text("unstaged version\n", encoding="utf-8")
            token = _prepare(actions, repo, "restore-unstaged", "tracked.txt")
            actions.confirm(str(repo), "restore-unstaged", token)
            self.assertEqual((repo / "tracked.txt").read_text(), "staged version\n")
            self.assertEqual(_status(repo), "M  tracked.txt")

            token, preview = actions.prepare(str(repo), "restore-head", path_token=_token(repo, "tracked.txt"))
            self.assertEqual(preview["layers"], ["index", "worktree"])
            self.assertIn("current HEAD version", preview["warning"])
            actions.confirm(str(repo), "restore-head", token)
            self.assertEqual((repo / "tracked.txt").read_text(), "original\n")
            self.assertEqual(_status(repo), "")

            (repo / "tracked.txt").unlink()
            token = _prepare(actions, repo, "restore-head", "tracked.txt")
            actions.confirm(str(repo), "restore-head", token)
            self.assertEqual((repo / "tracked.txt").read_text(), "original\n")

            (repo / "tracked.txt").unlink()
            token = _prepare(actions, repo, "restore-unstaged", "tracked.txt")
            actions.confirm(str(repo), "restore-unstaged", token)
            self.assertEqual((repo / "tracked.txt").read_text(), "original\n")

            (repo / "new.txt").write_text("new\n", encoding="utf-8")
            with self.assertRaisesRegex(GitActionError, "does not support"):
                _prepare(actions, repo, "restore-head", "new.txt")

    def test_commit_only_commits_reviewed_staged_state_and_leaves_other_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = _repo(Path(temporary))
            actions = PreparedGitActions()
            (repo / "tracked.txt").write_text("committed content\n", encoding="utf-8")
            _git(repo, "add", "tracked.txt")
            (repo / "second staged.txt").write_text("second committed path\n", encoding="utf-8")
            _git(repo, "add", "second staged.txt")
            (repo / "other.txt").write_text("still unstaged\n", encoding="utf-8")
            (repo / "new.txt").write_text("still untracked\n", encoding="utf-8")
            before_head = _text(repo, "rev-parse", "HEAD")
            state_before = _text(repo, "status", "--porcelain=v2", "-z", "--untracked-files=all")

            token, preview = actions.prepare(str(repo), "commit", message="explicit reviewed subject")
            self.assertEqual(preview["message"], "explicit reviewed subject")
            self.assertEqual(len(preview["staged_paths"]), 2)
            self.assertEqual(len(preview["remaining_paths"]), 2)
            self.assertEqual(_text(repo, "rev-parse", "HEAD"), before_head)
            self.assertEqual(_text(repo, "status", "--porcelain=v2", "-z", "--untracked-files=all"), state_before)

            (repo / "other.txt").write_text("later unrelated unstaged edit\n", encoding="utf-8")
            result = actions.confirm(str(repo), "commit", token)
            after_head = _text(repo, "rev-parse", "HEAD")
            self.assertNotEqual(after_head, before_head)
            self.assertIn(after_head[:12], result["message"])
            self.assertEqual(_text(repo, "show", "-s", "--format=%s", "HEAD"), "explicit reviewed subject")
            self.assertEqual(_text(repo, "show", "HEAD:tracked.txt"), "committed content")
            self.assertEqual(_text(repo, "show", "HEAD:second staged.txt"), "second committed path")
            self.assertEqual((repo / "other.txt").read_text(), "later unrelated unstaged edit\n")
            self.assertEqual((repo / "new.txt").read_text(), "still untracked\n")
            self.assertEqual(result["remaining_unstaged"], ["other.txt"])
            self.assertEqual(result["remaining_untracked"], ["new.txt"])

    def test_stale_expired_wrong_action_cancel_and_invalid_commit_refuse(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = _repo(Path(temporary))
            actions = PreparedGitActions()
            (repo / "tracked.txt").write_text("first edit\n", encoding="utf-8")

            token = _prepare(actions, repo, "stage", "tracked.txt")
            with self.assertRaisesRegex(GitActionError, "different action"):
                actions.confirm(str(repo), "unstage", token)
            (repo / "tracked.txt").write_text("changed after preview\n", encoding="utf-8")
            with self.assertRaisesRegex(GitActionError, "stale"):
                actions.confirm(str(repo), "stage", token)
            self.assertEqual(_status(repo), " M tracked.txt")

            token = _prepare(actions, repo, "stage", "tracked.txt")
            actions._actions[token].expires_at = 0
            with self.assertRaisesRegex(GitActionError, "unknown, expired"):
                actions.confirm(str(repo), "stage", token)

            token = _prepare(actions, repo, "stage", "tracked.txt")
            actions.cancel(token)
            with self.assertRaisesRegex(GitActionError, "unknown, expired"):
                actions.confirm(str(repo), "stage", token)
            with self.assertRaisesRegex(GitActionError, "unknown, expired"):
                actions.confirm(str(repo), "stage", "unknown-token")
            with self.assertRaisesRegex(GitActionError, "Unsupported"):
                actions.prepare(str(repo), "stage-all")
            with self.assertRaisesRegex(GitActionError, "empty"):
                actions.prepare(str(repo), "commit", message=" \t")
            with self.assertRaisesRegex(GitActionError, "There are no explicitly staged"):
                actions.prepare(str(repo), "commit", message="no changes")
            (repo / "tracked.txt").write_text("repository binding check\n", encoding="utf-8")
            token = _prepare(actions, repo, "stage", "tracked.txt")
            other_root = Path(temporary) / "other"
            other_root.mkdir()
            other_repo = _repo(other_root)
            with self.assertRaisesRegex(GitActionError, "different repository"):
                actions.confirm(str(other_repo), "stage", token)

    def test_commit_rejects_changed_staged_state_and_changed_head(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = _repo(Path(temporary))
            actions = PreparedGitActions()
            (repo / "tracked.txt").write_text("reviewed staged version\n", encoding="utf-8")
            _git(repo, "add", "tracked.txt")
            token, _preview = actions.prepare(str(repo), "commit", message="review staged version")
            (repo / "tracked.txt").write_text("changed staged version\n", encoding="utf-8")
            _git(repo, "add", "tracked.txt")
            head = _text(repo, "rev-parse", "HEAD")
            with self.assertRaisesRegex(GitActionError, "stale"):
                actions.confirm(str(repo), "commit", token)
            self.assertEqual(_text(repo, "rev-parse", "HEAD"), head)

            token, _preview = actions.prepare(str(repo), "commit", message="review current staged version")
            previous_head = _text(repo, "rev-parse", "HEAD")
            tree = _text(repo, "rev-parse", "HEAD^{tree}")
            new_head = _text(repo, "commit-tree", tree, "-p", previous_head, "-m", "external head advance")
            _git(repo, "update-ref", "HEAD", new_head)
            head = _text(repo, "rev-parse", "HEAD")
            with self.assertRaisesRegex(GitActionError, "stale"):
                actions.confirm(str(repo), "commit", token)
            self.assertEqual(_text(repo, "rev-parse", "HEAD"), head)

    def test_unsupported_repository_states_filters_and_commit_hooks_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = _repo(Path(temporary))
            actions = PreparedGitActions()
            (repo / "tracked.txt").write_text("changed\n", encoding="utf-8")
            marker = Path(_text(repo, "rev-parse", "--git-path", "MERGE_HEAD"))
            if not marker.is_absolute():
                marker = repo / marker
            marker.write_text("deadbeef\n", encoding="ascii")
            with self.assertRaisesRegex(GitActionError, "in progress"):
                actions.prepare(str(repo), "stage", path_token=_token(repo, "tracked.txt"))
            marker.unlink()

            _git(repo, "config", "filter.example.clean", "cat")
            (repo / ".gitattributes").write_text("tracked.txt filter=example\n", encoding="utf-8")
            with self.assertRaisesRegex(GitActionError, "clean filter"):
                actions.prepare(str(repo), "stage", path_token=_token(repo, "tracked.txt"))
            (repo / ".gitattributes").unlink()
            _git(repo, "add", "tracked.txt")
            hooks = Path(_text(repo, "rev-parse", "--git-path", "hooks"))
            if not hooks.is_absolute():
                hooks = repo / hooks
            hook = hooks / "pre-commit"
            hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            hook.chmod(0o755)
            with self.assertRaisesRegex(GitActionError, "hooks"):
                actions.prepare(str(repo), "commit", message="hook test")

    def test_detached_head_symlink_and_binary_restore_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = _repo(Path(temporary))
            actions = PreparedGitActions()
            (repo / "tracked.txt").write_text("changed\n", encoding="utf-8")
            _git(repo, "checkout", "--detach", "-q")
            with self.assertRaisesRegex(GitActionError, "detached"):
                actions.prepare(str(repo), "stage", path_token=_token(repo, "tracked.txt"))

        with tempfile.TemporaryDirectory() as temporary:
            repo = _repo(Path(temporary))
            actions = PreparedGitActions()
            (repo / "tracked.txt").unlink()
            (repo / "tracked.txt").symlink_to("other.txt")
            with self.assertRaisesRegex(GitActionError, "regular file"):
                actions.prepare(str(repo), "stage", path_token=_token(repo, "tracked.txt"))

        with tempfile.TemporaryDirectory() as temporary:
            repo = _repo(Path(temporary))
            actions = PreparedGitActions()
            (repo / "tracked.txt").write_bytes(b"\x00binary worktree state\n")
            options = actions.options(str(repo), _token(repo, "tracked.txt"))
            self.assertIn("stage", options["actions"])
            self.assertNotIn("restore-unstaged", options["actions"])
            self.assertTrue(any(item["action"] == "restore-unstaged" for item in options["unavailable_actions"]))
            (repo / "tracked.txt").write_text("x" * (130 * 1024), encoding="utf-8")
            options = actions.options(str(repo), _token(repo, "tracked.txt"))
            self.assertIn("stage", options["actions"])
            self.assertNotIn("restore-unstaged", options["actions"])
            (repo / "tracked.txt").write_bytes(b"\x00binary staged state\n")
            _git(repo, "add", "tracked.txt")
            commit_options = actions.can_commit(str(repo))
            self.assertFalse(commit_options["available"])
            self.assertIn("binary", commit_options["reason"].casefold())

    def test_post_only_browser_confirmation_get_is_inert_and_workflow_mutations_are_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = _repo(root)
            (repo / "tracked.txt").write_text("web staged\n", encoding="utf-8")
            app = create_web_app(repository_path=str(repo), state_root=root / "state")
            client = app.test_client()
            before_repo = _tree_digest(repo)
            before_state = _tree_digest(root / "state")
            listing = client.get(f"/git-review?file={_token(repo, 'tracked.txt')}")
            self.assertEqual(listing.status_code, 200)
            self.assertIn("Stage selected", listing.get_data(as_text=True))
            self.assertIn('name="path_token"', listing.get_data(as_text=True))
            self.assertEqual(client.get("/git-review/action/prepare").status_code, 405)
            self.assertEqual(client.get("/git-review/action/confirm").status_code, 405)
            self.assertEqual(client.get("/git-review/action/cancel").status_code, 405)
            self.assertEqual(_tree_digest(repo), before_repo)
            self.assertEqual(_tree_digest(root / "state"), before_state)

            selected = _token(repo, "tracked.txt")
            with client.session_transaction() as session:
                csrf = session["csrf_token"]
            prepared = client.post(
                "/git-review/action/prepare",
                data={"csrf_token": csrf, "action": "stage", "path_token": selected},
            )
            self.assertEqual(prepared.status_code, 200)
            self.assertIn("PREPARED — NO GIT CHANGE HAS BEEN APPLIED", prepared.get_data(as_text=True))
            self.assertEqual(_status(repo), " M tracked.txt")
            self.assertEqual(_tree_digest(repo), before_repo)
            self.assertEqual(_tree_digest(root / "state"), before_state)
            token_match = re.search(rb'name="token" value="([A-Za-z0-9_-]+)"', prepared.data)
            self.assertIsNotNone(token_match)
            action_token = token_match.group(1).decode("ascii")

            confirmed = client.post(
                "/git-review/action/confirm",
                data={"csrf_token": csrf, "action": "stage", "token": action_token},
            )
            self.assertEqual(confirmed.status_code, 302)
            self.assertEqual(_status(repo), "M  tracked.txt")
            refreshed = client.get(confirmed.headers["Location"])
            self.assertEqual(refreshed.status_code, 200)
            self.assertEqual(_status(repo), "M  tracked.txt")
            replay = client.post(
                "/git-review/action/confirm",
                data={"csrf_token": csrf, "action": "stage", "token": action_token},
            )
            self.assertEqual(replay.status_code, 409)
            self.assertIn("already used", replay.get_data(as_text=True))
            csrf_rejected = client.post(
                "/git-review/action/prepare",
                data={"action": "stage", "path_token": selected},
            )
            self.assertEqual(csrf_rejected.status_code, 400)
            cross_origin_rejected = client.post(
                "/git-review/action/prepare",
                data={"csrf_token": csrf, "action": "stage", "path_token": selected},
                headers={"Origin": "https://example.invalid"},
            )
            self.assertEqual(cross_origin_rejected.status_code, 403)

            (repo / "other.txt").write_text("cancel remains unstaged\n", encoding="utf-8")
            cancel_prepare = client.post(
                "/git-review/action/prepare",
                data={
                    "csrf_token": csrf,
                    "action": "stage",
                    "path_token": _token(repo, "other.txt"),
                },
            )
            cancel_match = re.search(rb'name="token" value="([A-Za-z0-9_-]+)"', cancel_prepare.data)
            self.assertIsNotNone(cancel_match)
            canceled = client.post(
                "/git-review/action/cancel",
                data={"csrf_token": csrf, "token": cancel_match.group(1).decode("ascii")},
            )
            self.assertEqual(canceled.status_code, 302)
            self.assertEqual(_status(repo), " M other.txt\nM  tracked.txt")

            self.assertEqual(client.post("/workflow/stage/prepare", data={"csrf_token": csrf}).status_code, 410)
            self.assertEqual(client.post("/workflow/commit/prepare", data={"csrf_token": csrf}).status_code, 410)
            workflow = client.get("/workflow")
            self.assertEqual(workflow.status_code, 200)
            self.assertNotIn("Prepare Stage", workflow.get_data(as_text=True))
            self.assertNotIn("Prepare Commit", workflow.get_data(as_text=True))

    def test_browser_batch_prepare_confirms_exact_displayed_path_tokens_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = _repo(root)
            (repo / "tracked.txt").write_text("selected tracked\n", encoding="utf-8")
            (repo / "other.txt").write_text("must remain unstaged\n", encoding="utf-8")
            (repo / "untracked.txt").write_text("selected new\n", encoding="utf-8")
            app = create_web_app(repository_path=str(repo), state_root=root / "state")
            client = app.test_client()
            listing = client.get("/git-review")
            text = listing.get_data(as_text=True)
            self.assertIn("Select all visible", text)
            self.assertIn("Clear selection", text)
            self.assertEqual(text.count("data-path-selection"), 3)
            csrf = _csrf(client)

            selected_tokens = [_token(repo, "tracked.txt"), _token(repo, "untracked.txt")]
            prepared = client.post(
                "/git-review/action/prepare",
                data={"csrf_token": csrf, "action": "stage", "path_token": selected_tokens},
            )
            self.assertEqual(prepared.status_code, 200)
            prepared_text = prepared.get_data(as_text=True)
            self.assertIn("Selected paths (2)", prepared_text)
            self.assertIn("<code>tracked.txt</code>", prepared_text)
            self.assertIn("<code>untracked.txt</code>", prepared_text)
            self.assertNotIn("<code>other.txt</code>", prepared_text)
            self.assertEqual(_status_lines(repo), {" M tracked.txt", " M other.txt", "?? untracked.txt"})

            token_match = re.search(rb'name="token" value="([A-Za-z0-9_-]+)"', prepared.data)
            self.assertIsNotNone(token_match)
            confirmed = client.post(
                "/git-review/action/confirm",
                data={
                    "csrf_token": csrf,
                    "action": "stage",
                    "token": token_match.group(1).decode("ascii"),
                    "path_token": [_token(repo, "other.txt")],
                },
            )
            self.assertEqual(confirmed.status_code, 302)
            self.assertEqual(_status_lines(repo), {"M  tracked.txt", " M other.txt", "A  untracked.txt"})
            self.assertEqual(_git(repo, "ls-files", "other.txt"), b"other.txt\n")

            arbitrary = client.post(
                "/git-review/action/prepare",
                data={"csrf_token": csrf, "action": "stage", "path_token": "other.txt"},
            )
            self.assertEqual(arbitrary.status_code, 404)

    def test_browser_commit_prepares_without_mutation_and_commits_only_staged_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = _repo(root)
            (repo / "tracked.txt").write_text("staged commit content\n", encoding="utf-8")
            _git(repo, "add", "tracked.txt")
            (repo / "other.txt").write_text("must stay unstaged\n", encoding="utf-8")
            app = create_web_app(repository_path=str(repo), state_root=root / "state")
            client = app.test_client()
            before_head = _text(repo, "rev-parse", "HEAD")
            before_index = _git(repo, "ls-files", "--stage", "-z")
            review = client.get("/git-review")
            self.assertEqual(review.status_code, 200)
            review_text = review.get_data(as_text=True)
            self.assertIn("Staged set: 1 file", review_text)
            self.assertIn("other.txt", review_text)
            self.assertIn("Unstaged and untracked changes remain outside the commit", review_text)
            csrf = _csrf(client)

            prepared = client.post(
                "/git-review/action/prepare",
                data={
                    "csrf_token": csrf,
                    "action": "commit",
                    "commit_message": "browser reviewed commit",
                },
            )
            self.assertEqual(prepared.status_code, 200)
            self.assertIn("Only these explicitly staged changes will be committed", prepared.get_data(as_text=True))
            self.assertEqual(_text(repo, "rev-parse", "HEAD"), before_head)
            self.assertEqual(_git(repo, "ls-files", "--stage", "-z"), before_index)
            token_match = re.search(rb'name="token" value="([A-Za-z0-9_-]+)"', prepared.data)
            self.assertIsNotNone(token_match)

            confirmed = client.post(
                "/git-review/action/confirm",
                data={
                    "csrf_token": csrf,
                    "action": "commit",
                    "token": token_match.group(1).decode("ascii"),
                },
            )
            self.assertEqual(confirmed.status_code, 302)
            self.assertNotEqual(_text(repo, "rev-parse", "HEAD"), before_head)
            self.assertEqual(_text(repo, "show", "-s", "--format=%s", "HEAD"), "browser reviewed commit")
            self.assertEqual(_text(repo, "show", "HEAD:tracked.txt"), "staged commit content")
            self.assertEqual((repo / "other.txt").read_text(), "must stay unstaged\n")
            self.assertEqual(_status_lines(repo), {" M other.txt"})


def _csrf(client) -> str:
    with client.session_transaction() as session:
        return session["csrf_token"]
