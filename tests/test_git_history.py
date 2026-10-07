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
            with mock.patch(
                "repoctl.workflow.git_history._run",
                return_value=(b"", b"", 0, True),
            ):
                result = inspect_history(str(repo))
            self.assertFalse(result["complete"])
            self.assertEqual(result["commits"], [])

            with mock.patch(
                "repoctl.workflow.git_history._run",
                side_effect=GitHistoryError("git log failed"),
            ):
                with self.assertRaisesRegex(GitHistoryError, "git log failed"):
                    inspect_history(str(repo))

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
