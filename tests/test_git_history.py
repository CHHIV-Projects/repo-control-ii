from __future__ import annotations

import base64
import hashlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from repoctl.web.app import create_web_app
from repoctl.workflow import git_history
from repoctl.workflow.git_history import (
    GitHistoryError,
    inspect_branches,
    inspect_file_history,
    inspect_history,
)


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


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD").decode("ascii").strip()


def _fast_import_commits(repo: Path, count: int, *, path: str | None = None) -> None:
    parent = _git(repo, "rev-parse", "refs/heads/main").decode("ascii").strip()
    stream: list[str] = []
    if path is not None:
        for index in range(count):
            content = f"fast-import-{index}\n"
            stream.extend(["blob", f"mark :{index + 1}", f"data {len(content)}", content])
    commit_mark_start = count + 1
    for index in range(count):
        commit_mark = commit_mark_start + index
        message = f"fast-import commit {index:04d}"
        stream.extend([
            "commit refs/heads/main",
            f"mark :{commit_mark}",
            "author History Test <history@example.test> 1700000000 +0000",
            "committer History Test <history@example.test> 1700000000 +0000",
            f"data {len(message)}",
            message,
            f"from {parent if index == 0 else f':{commit_mark - 1}'}",
        ])
        if path is not None:
            stream.append(f"M 100644 :{index + 1} {path}")
    result = subprocess.run(
        ["git", "-C", str(repo), "fast-import", "--quiet"],
        input="\n".join(stream).encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace"))


def _repository(root: Path) -> tuple[Path, str, str, str]:
    repo = root / "history-repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main", "-q")
    _git(repo, "config", "user.name", "History Test")
    _git(repo, "config", "user.email", "history@example.test")
    (repo / "tracked.txt").write_text("root\n", encoding="utf-8")
    (repo / "space <name>.txt").write_text("first\n", encoding="utf-8")
    root_commit = _commit(repo, "root commit")
    _git(repo, "switch", "-q", "-c", "feature/topic")
    (repo / "tracked.txt").write_text("feature change\n", encoding="utf-8")
    feature_commit = _commit(repo, "feature commit")
    _git(repo, "switch", "-q", "main")
    (repo / "space <name>.txt").write_text("second\n", encoding="utf-8")
    _commit(repo, "file second commit")
    (repo / "main.txt").write_text("main only\n", encoding="utf-8")
    main_commit = _commit(repo, "main-only commit")
    _git(repo, "branch", "equal", "main")
    _git(repo, "branch", "behind", root_commit)
    _git(repo, "switch", "-q", "-c", "ahead")
    (repo / "ahead.txt").write_text("ahead\n", encoding="utf-8")
    _commit(repo, "ahead commit")
    _git(repo, "switch", "-q", "main")
    return repo, root_commit, feature_commit, main_commit


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


class GitHistoryTests(unittest.TestCase):
    def test_recent_commit_detail_root_merge_and_tracked_file_history(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo, root_commit, _feature_commit, _main_commit = _repository(Path(temporary))
            history = inspect_history(str(repo))
            self.assertEqual(history["commits"][0]["subject"], "main-only commit")
            self.assertEqual(len(history["commits"]), 3)

            root = inspect_history(str(repo), selected_commit=root_commit)["selected"]
            self.assertEqual(root["comparison"], "Root commit vs empty tree.")
            self.assertEqual({item["status"] for item in root["files"]}, {"A"})
            self.assertIn("tracked.txt", root["diff"]["text"])

            space_token = base64.urlsafe_b64encode(b"space <name>.txt").decode().rstrip("=")
            file_history = inspect_file_history(str(repo), space_token)
            self.assertEqual(file_history["path"], "space <name>.txt")
            self.assertEqual(
                [commit["subject"] for commit in file_history["commits"]],
                ["file second commit", "root commit"],
            )

            _git(repo, "switch", "-q", "main")
            _git(repo, "merge", "--no-ff", "-qm", "merge feature", "feature/topic")
            merge_oid = _git(repo, "rev-parse", "HEAD").decode("ascii").strip()
            merge = inspect_history(str(repo), selected_commit=merge_oid)["selected"]
            self.assertTrue(merge["merge"])
            self.assertEqual(merge["comparison"], "First parent vs this commit.")
            self.assertIn("tracked.txt", merge["diff"]["text"])
            self.assertTrue(any(item["path"] == "tracked.txt" for item in merge["files"]))

    def test_staged_deletion_can_still_show_head_file_history(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo, _root_commit, _feature_commit, _main_commit = _repository(Path(temporary))
            path = "space <name>.txt"
            token = base64.urlsafe_b64encode(path.encode()).decode().rstrip("=")
            _git(repo, "rm", "-q", path)
            history = inspect_file_history(str(repo), token)
            self.assertEqual(history["path"], path)
            self.assertEqual(history["commits"][0]["subject"], "file second commit")

    def test_file_history_follows_a_rename(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo, _root_commit, _feature_commit, _main_commit = _repository(Path(temporary))
            _git(repo, "mv", "space <name>.txt", "renamed <name>.txt")
            _commit(repo, "rename file")
            token = base64.urlsafe_b64encode(b"renamed <name>.txt").decode().rstrip("=")
            history = inspect_file_history(str(repo), token)
            self.assertEqual(
                [commit["subject"] for commit in history["commits"]],
                ["rename file", "file second commit", "root commit"],
            )

    def test_history_output_limit_and_git_error_are_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo, _root_commit, _feature_commit, _main_commit = _repository(Path(temporary))
            original_run = git_history._run

            def truncated_log(repository, args, **kwargs):
                if args[0] == "log":
                    return b"", b"", 0, True
                return original_run(repository, args, **kwargs)

            with mock.patch("repoctl.workflow.git_history._run", side_effect=truncated_log):
                result = inspect_history(str(repo))
            self.assertFalse(result["complete"])
            self.assertEqual(result["commits"], [])

            def failing_log(repository, args, **kwargs):
                if args[0] == "log":
                    raise GitHistoryError("git log failed")
                return original_run(repository, args, **kwargs)

            with mock.patch("repoctl.workflow.git_history._run", side_effect=failing_log):
                with self.assertRaisesRegex(GitHistoryError, "git log failed"):
                    inspect_history(str(repo))

    def test_commit_and_file_history_continue_through_1000_records_and_stale_after_head_move(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo, _root_commit, _feature_commit, _main_commit = _repository(Path(temporary))
            _fast_import_commits(repo, 997, path="tracked.txt")

            commits = []
            cursor = None
            while True:
                page = inspect_history(str(repo), cursor=cursor, limit=30)
                commits.extend(commit["oid"] for commit in page["commits"])
                if not page["has_more"]:
                    self.assertTrue(page["complete"])
                    break
                cursor = page["next_cursor"]
            self.assertEqual(len(commits), 1000)
            self.assertEqual(len(set(commits)), 1000)

            token = base64.urlsafe_b64encode(b"tracked.txt").decode().rstrip("=")
            file_commits = []
            cursor = None
            while True:
                page = inspect_file_history(str(repo), token, cursor=cursor, limit=30)
                file_commits.extend(commit["oid"] for commit in page["commits"])
                if not page["has_more"]:
                    self.assertTrue(page["complete"])
                    break
                cursor = page["next_cursor"]
            self.assertGreater(len(file_commits), 30)
            self.assertEqual(len(file_commits), len(set(file_commits)))
            client = create_web_app(
                repository_path=str(repo),
                state_root=Path(temporary) / "state",
            ).test_client()
            history_page = client.get("/git-review?view=history")
            file_page = client.get(f"/git-review?view=history&file_history={token}")
            self.assertEqual(history_page.status_code, 200)
            self.assertIn(b"Next commit page", history_page.data)
            self.assertEqual(file_page.status_code, 200)
            self.assertIn(b"Next file-history page", file_page.data)

            first = inspect_history(str(repo), limit=1)
            _commit(repo, "move HEAD")
            with self.assertRaisesRegex(GitHistoryError, "cursor is stale"):
                inspect_history(str(repo), cursor=first["next_cursor"], limit=1)

    def test_branch_relationship_counts_and_local_upstream_staleness(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo, root_commit, feature_commit, _main_commit = _repository(Path(temporary))
            _git(repo, "switch", "-q", "feature/topic")
            _git(repo, "update-ref", "refs/remotes/origin/feature/topic", root_commit)
            _git(repo, "config", "branch.feature/topic.remote", "origin")
            _git(repo, "config", "branch.feature/topic.merge", "refs/heads/feature/topic")
            _git(repo, "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
            _git(repo, "config", "branch.equal.remote", "origin")
            _git(repo, "config", "branch.equal.merge", "refs/heads/equal")

            listing = inspect_branches(str(repo))
            self.assertEqual(len(listing["branches"]), 5)
            feature = next(branch for branch in listing["branches"] if branch["name"] == "feature/topic")
            self.assertEqual(feature["main_relation"]["ahead"], 1)
            self.assertEqual(feature["main_relation"]["behind"], 2)
            self.assertFalse(feature["main_relation"]["fully_merged"])
            self.assertTrue(feature["current"])
            self.assertEqual(feature["upstream_relation"]["ahead"], 1)
            self.assertEqual(feature["upstream_relation"]["behind"], 0)
            self.assertIn("local tracking ref", feature["upstream_relation"]["label"])
            branch_by_name = {branch["name"]: branch for branch in listing["branches"]}
            self.assertEqual(branch_by_name["equal"]["main_relation"]["ahead"], 0)
            self.assertEqual(branch_by_name["equal"]["main_relation"]["behind"], 0)
            self.assertTrue(branch_by_name["equal"]["main_relation"]["fully_merged"])
            self.assertEqual(branch_by_name["ahead"]["main_relation"]["ahead"], 1)
            self.assertEqual(branch_by_name["ahead"]["main_relation"]["behind"], 0)
            self.assertFalse(branch_by_name["ahead"]["main_relation"]["fully_merged"])
            self.assertEqual(branch_by_name["behind"]["main_relation"]["ahead"], 0)
            self.assertEqual(branch_by_name["behind"]["main_relation"]["behind"], 2)
            self.assertTrue(branch_by_name["behind"]["main_relation"]["fully_merged"])
            self.assertEqual(branch_by_name["behind"]["upstream_relation"]["state"], "not_configured")
            self.assertEqual(branch_by_name["equal"]["upstream_relation"]["state"], "unavailable")

            selected = inspect_branches(str(repo), selected_branch=feature["token"])["selected"]
            self.assertIn("tracked.txt", [item["path"] for item in selected["comparison"]["files"]])
            self.assertEqual([commit["oid"] for commit in selected["recent_commits"]], [feature_commit])
            self.assertEqual(
                [commit["subject"] for commit in selected["main_only_commits"]],
                ["main-only commit", "file second commit"],
            )

            _git(repo, "switch", "-q", "main")
            _git(repo, "merge", "--no-ff", "-qm", "merge feature", "feature/topic")
            merged = inspect_branches(str(repo))
            feature_after_merge = next(branch for branch in merged["branches"] if branch["name"] == "feature/topic")
            self.assertTrue(feature_after_merge["main_relation"]["fully_merged"])

            _git(repo, "checkout", "-q", "--detach", "main")
            detached = inspect_branches(str(repo))
            self.assertEqual(detached["head_state"], "detached")
            self.assertFalse(any(branch["current"] for branch in detached["branches"]))

    def test_branch_pages_cover_1000_local_refs_and_reject_stale_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo, _root_commit, _feature_commit, _main_commit = _repository(Path(temporary))
            oid = _git(repo, "rev-parse", "refs/heads/main").decode("ascii").strip()
            commands = "".join(
                f"create refs/heads/paged/{index:04d} {oid}\n"
                for index in range(995)
            )
            result = subprocess.run(
                ["git", "-C", str(repo), "update-ref", "--stdin"],
                input=commands.encode("ascii"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))

            first = inspect_branches(str(repo), limit=1)
            self.assertEqual(first["total_count"], 1000)
            self.assertEqual(first["returned_count"], 1)
            self.assertTrue(first["has_more"])
            last_cursor = git_history._encode_cursor(
                "branches",
                first["snapshot"],
                "",
                999,
                git_history._repository_binding(repo),
            )
            last = inspect_branches(str(repo), cursor=last_cursor, limit=1)
            self.assertEqual(last["returned_count"], 1)
            self.assertTrue(last["complete"])
            client = create_web_app(
                repository_path=str(repo),
                state_root=Path(temporary) / "state",
            ).test_client()
            browser_page = client.get("/git-review?view=branches")
            self.assertEqual(browser_page.status_code, 200)
            self.assertIn(b"Local branches (1000)", browser_page.data)
            self.assertIn(b"Next local branches", browser_page.data)

            _git(repo, "branch", "new-after-snapshot")
            with self.assertRaisesRegex(GitHistoryError, "cursor is stale"):
                inspect_branches(str(repo), cursor=first["next_cursor"], limit=1)

    def test_selections_are_validated_and_inspection_is_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo, _root_commit, _feature_commit, _main_commit = _repository(root)
            app = create_web_app(repository_path=str(repo), state_root=root / "state")
            client = app.test_client()
            before = _tree_digest(repo)
            state_before = _tree_digest(root / "state")

            with mock.patch("repoctl.workflow.git_history._run", wraps=git_history._run) as git_run:
                history_response = client.get("/git-review?view=history")
                branch_response = client.get("/git-review?view=branches")
                self.assertEqual(history_response.status_code, 200)
                self.assertIn(b"Recent commit history", history_response.data)
                self.assertEqual(branch_response.status_code, 200)
                self.assertIn(b"feature/topic", branch_response.data)

                history = inspect_history(str(repo))
                selected = client.get(f"/git-review?view=history&commit={history['commits'][0]['oid']}")
                self.assertEqual(selected.status_code, 200)
                self.assertIn(b"Commit ", selected.data)
                space_token = base64.urlsafe_b64encode(b"space <name>.txt").decode().rstrip("=")
                file_response = client.get(f"/git-review?view=history&file_history={space_token}")
                self.assertEqual(file_response.status_code, 200)
                self.assertIn(b"file second commit", file_response.data)
                self.assertIn(b"space &lt;name&gt;.txt", file_response.data)
                file_history = inspect_file_history(str(repo), space_token)
                historical_commit = file_history["commits"][0]["oid"]
                file_commit_response = client.get(
                    f"/git-review?view=history&file_history={space_token}&commit={historical_commit}"
                )
                self.assertIn(b"Back to this file's history", file_commit_response.data)

                feature = next(branch for branch in inspect_branches(str(repo))["branches"] if branch["name"] == "feature/topic")
                detail = client.get(f"/git-review?view=branches&branch={feature['token']}")
                self.assertEqual(detail.status_code, 200)
                self.assertIn(b"Comparison with main", detail.data)
                self.assertFalse(any(call.args[1][0] in {"fetch", "pull", "push"} for call in git_run.call_args_list))
            self.assertEqual(_tree_digest(repo), before)
            self.assertEqual(_tree_digest(root / "state"), state_before)

            self.assertEqual(client.get("/git-review?view=history&commit=not-an-object-id").status_code, 400)
            self.assertEqual(client.get("/git-review?view=branches&branch=unknown").status_code, 404)
            with self.assertRaises(FileNotFoundError):
                inspect_file_history(str(repo), "not-a-file-token")
