from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from repoctl.cli import main
from repoctl.scanner.core import run_scan_with_artifacts
from repoctl.scanner.git_ops import ScanError
from repoctl.snapshot.manager import create_snapshot
from repoctl.workflow.status import WorkflowError, _publish_status_pair, generate_milestone_status


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


def _git_text(repo: Path, *args: str) -> str:
    return _git(repo, *args).decode("utf-8", errors="strict").strip()


def _resolved_git_path(repo: Path, marker: str) -> Path:
    text = _git_text(repo, "rev-parse", "--git-path", marker)
    path = Path(text)
    if not path.is_absolute():
        path = (repo / path).resolve()
    return path


def _init_repo(root: Path, name: str = "wf-repo") -> Path:
    repo = root / name
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")

    (repo / "app.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    (repo / "README.md").write_text("# sample\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")
    return repo


def _status_payload(state_root: Path, repository_id: str) -> dict:
    return json.loads((state_root / repository_id / "workflow" / "status.json").read_text(encoding="utf-8"))


class WorkflowStatusTests(unittest.TestCase):
    def test_clean_repository(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            state = root / "state"

            result = generate_milestone_status(str(repo), state_root=state)
            payload = _status_payload(state, result["repository_id"])

            self.assertEqual(payload["workflow_state"], "clean")
            self.assertEqual(payload["working_tree"]["staged"]["count"], 0)
            self.assertEqual(payload["working_tree"]["unstaged"]["count"], 0)
            self.assertEqual(payload["working_tree"]["untracked"]["count"], 0)
            self.assertEqual(payload["working_tree"]["unmerged"]["count"], 0)

    def test_staged_only_repository(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            state = root / "state"

            (repo / "app.py").write_text("def run():\n    return 2\n", encoding="utf-8")
            _git(repo, "add", "app.py")

            result = generate_milestone_status(str(repo), state_root=state)
            payload = _status_payload(state, result["repository_id"])
            self.assertEqual(payload["workflow_state"], "staged_only")

    def test_unstaged_tracked_repository(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            state = root / "state"

            (repo / "app.py").write_text("def run():\n    return 3\n", encoding="utf-8")

            result = generate_milestone_status(str(repo), state_root=state)
            payload = _status_payload(state, result["repository_id"])
            self.assertEqual(payload["workflow_state"], "unstaged_only")
            self.assertEqual(payload["working_tree"]["unstaged"]["count"], 1)

    def test_untracked_only_repository(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            state = root / "state"

            (repo / "new.txt").write_text("x\n", encoding="utf-8")

            result = generate_milestone_status(str(repo), state_root=state)
            payload = _status_payload(state, result["repository_id"])
            self.assertEqual(payload["workflow_state"], "unstaged_only")
            self.assertEqual(payload["working_tree"]["untracked"]["count"], 1)

    def test_staged_and_unstaged_same_path(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            state = root / "state"

            (repo / "app.py").write_text("def run():\n    return 10\n", encoding="utf-8")
            _git(repo, "add", "app.py")
            (repo / "app.py").write_text("def run():\n    return 11\n", encoding="utf-8")

            result = generate_milestone_status(str(repo), state_root=state)
            payload = _status_payload(state, result["repository_id"])
            self.assertEqual(payload["workflow_state"], "staged_and_unstaged")
            self.assertIn("app.py", payload["working_tree"]["staged"]["paths"])
            self.assertIn("app.py", payload["working_tree"]["unstaged"]["paths"])

    def test_staged_and_untracked(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            state = root / "state"

            (repo / "app.py").write_text("def run():\n    return 20\n", encoding="utf-8")
            _git(repo, "add", "app.py")
            (repo / "new.txt").write_text("x\n", encoding="utf-8")

            result = generate_milestone_status(str(repo), state_root=state)
            payload = _status_payload(state, result["repository_id"])
            self.assertEqual(payload["workflow_state"], "staged_and_unstaged")

    def test_conflicted_state(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            state = root / "state"
            base_branch = _git_text(repo, "branch", "--show-current")

            _git(repo, "checkout", "-b", "feature")
            (repo / "app.py").write_text("def run():\n    return 100\n", encoding="utf-8")
            _git(repo, "add", "app.py")
            _git(repo, "commit", "-m", "feature change")

            _git(repo, "checkout", base_branch)
            (repo / "app.py").write_text("def run():\n    return 200\n", encoding="utf-8")
            _git(repo, "add", "app.py")
            _git(repo, "commit", "-m", "main change")

            proc = subprocess.run(
                ["git", "-C", str(repo), "merge", "feature"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertNotEqual(proc.returncode, 0)

            result = generate_milestone_status(str(repo), state_root=state)
            payload = _status_payload(state, result["repository_id"])
            self.assertEqual(payload["workflow_state"], "conflicted")
            self.assertGreater(payload["working_tree"]["unmerged"]["count"], 0)

    def test_detached_head(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            state = root / "state"

            _git(repo, "checkout", "--detach", "HEAD")
            result = generate_milestone_status(str(repo), state_root=state)
            payload = _status_payload(state, result["repository_id"])
            self.assertEqual(payload["branch"]["state"], "detached")
            self.assertIsNone(payload["branch"]["name"])

    def test_upstream_not_configured_reason(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            state = root / "state"

            result = generate_milestone_status(str(repo), state_root=state)
            payload = _status_payload(state, result["repository_id"])
            upstream = payload["upstream"]

            self.assertFalse(upstream["configured"])
            self.assertEqual(upstream["divergence_state"], "unavailable")
            self.assertEqual(upstream["unavailable_reason"], "upstream_not_configured")
            self.assertIsNone(upstream["ahead"])
            self.assertIsNone(upstream["behind"])

    def test_upstream_equal_available(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            remote = root / "remote.git"
            state = root / "state"
            base_branch = _git_text(repo, "branch", "--show-current")

            remote.mkdir(parents=True, exist_ok=True)
            _git(remote, "init", "--bare")
            _git(repo, "remote", "add", "origin", str(remote))
            _git(repo, "push", "-u", "origin", base_branch)

            result = generate_milestone_status(str(repo), state_root=state)
            payload = _status_payload(state, result["repository_id"])
            upstream = payload["upstream"]

            self.assertTrue(upstream["configured"])
            self.assertEqual(upstream["divergence_state"], "available")
            self.assertIsNone(upstream["unavailable_reason"])
            self.assertEqual(upstream["relation"], "equal")
            self.assertEqual(upstream["ahead"], 0)
            self.assertEqual(upstream["behind"], 0)

    def test_upstream_ahead_behind_diverged(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root, name="repo-main")
            remote = root / "remote.git"
            clone = root / "clone"
            state = root / "state"
            base_branch = _git_text(repo, "branch", "--show-current")

            remote.mkdir(parents=True, exist_ok=True)
            _git(remote, "init", "--bare")
            _git(repo, "remote", "add", "origin", str(remote))
            _git(repo, "push", "-u", "origin", base_branch)

            (repo / "ahead.txt").write_text("ahead\n", encoding="utf-8")
            _git(repo, "add", "ahead.txt")
            _git(repo, "commit", "-m", "ahead")
            payload = _status_payload(state, generate_milestone_status(str(repo), state_root=state)["repository_id"])
            self.assertEqual(payload["upstream"]["relation"], "ahead")

            _git(root, "clone", str(remote), str(clone))
            _git(clone, "config", "user.email", "test@example.com")
            _git(clone, "config", "user.name", "Test User")
            (clone / "behind.txt").write_text("behind\n", encoding="utf-8")
            _git(clone, "add", "behind.txt")
            _git(clone, "commit", "-m", "behind")
            _git(clone, "push", "origin", base_branch)
            _git(repo, "fetch", "origin")

            payload = _status_payload(state, generate_milestone_status(str(repo), state_root=state)["repository_id"])
            self.assertEqual(payload["upstream"]["relation"], "diverged")

            _git(repo, "reset", "--hard", f"origin/{base_branch}")
            payload = _status_payload(state, generate_milestone_status(str(repo), state_root=state)["repository_id"])
            self.assertEqual(payload["upstream"]["relation"], "equal")

            (clone / "behind2.txt").write_text("behind2\n", encoding="utf-8")
            _git(clone, "add", "behind2.txt")
            _git(clone, "commit", "-m", "behind2")
            _git(clone, "push", "origin", base_branch)
            _git(repo, "fetch", "origin")
            payload = _status_payload(state, generate_milestone_status(str(repo), state_root=state)["repository_id"])
            self.assertEqual(payload["upstream"]["relation"], "behind")

    def test_upstream_ref_unavailable_reason(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            remote = root / "remote.git"
            state = root / "state"
            base_branch = _git_text(repo, "branch", "--show-current")

            remote.mkdir(parents=True, exist_ok=True)
            _git(remote, "init", "--bare")
            _git(repo, "remote", "add", "origin", str(remote))
            _git(repo, "push", "-u", "origin", base_branch)
            _git(repo, "config", f"branch.{base_branch}.remote", "origin")
            _git(repo, "config", f"branch.{base_branch}.merge", "refs/heads/missing")

            result = generate_milestone_status(str(repo), state_root=state)
            payload = _status_payload(state, result["repository_id"])
            upstream = payload["upstream"]
            self.assertTrue(upstream["configured"])
            self.assertEqual(upstream["divergence_state"], "unavailable")
            self.assertEqual(upstream["unavailable_reason"], "upstream_ref_unavailable")
            self.assertIsNone(upstream["relation"])

    def test_active_git_operations_fixed_order_and_minimal_output(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            state = root / "state"

            marker_values = {
                "MERGE_HEAD": "deadbeef\n",
                "CHERRY_PICK_HEAD": "deadbeef\n",
                "REVERT_HEAD": "deadbeef\n",
                "BISECT_LOG": "bisect log\n",
            }
            for marker, content in marker_values.items():
                marker_path = _resolved_git_path(repo, marker)
                marker_path.parent.mkdir(parents=True, exist_ok=True)
                marker_path.write_text(content, encoding="utf-8")
            rebase_dir = _resolved_git_path(repo, "rebase-merge")
            rebase_dir.mkdir(parents=True, exist_ok=True)

            result = generate_milestone_status(str(repo), state_root=state)
            payload = _status_payload(state, result["repository_id"])

            self.assertEqual(payload["git_operations"], ["merge", "rebase", "cherry_pick", "revert", "bisect"])
            self.assertTrue(payload["git_operation_in_progress"])

            raw = (state / result["repository_id"] / "workflow" / "status.json").read_text(encoding="utf-8")
            self.assertNotIn("rebase-merge", raw)
            self.assertNotIn("MERGE_HEAD", raw)

    def test_unsupported_porcelain_record_is_classified_and_sanitized(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            state = root / "state"

            with mock.patch(
                "repoctl.workflow.git_state.get_working_tree_with_branch",
                side_effect=ScanError("unsupported porcelain v2 record type: ! secret/path.txt"),
            ):
                with self.assertRaises(WorkflowError) as cm:
                    generate_milestone_status(str(repo), state_root=state)

            text = str(cm.exception)
            self.assertIn("unsupported_porcelain_v2_record", text)
            self.assertIn("prefix: !", text)
            self.assertNotIn("secret/path.txt", text)

    def test_transactional_publication_rolls_back_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            workflow_root = Path(td) / "workflow"

            original_payload = {
                "schema_version": 1,
                "repository_id": "repo--x",
                "repository_root": "/tmp/r",
                "head": "a" * 40,
                "branch": {"state": "attached", "name": "main"},
                "upstream": {
                    "configured": False,
                    "ref": None,
                    "divergence_state": "unavailable",
                    "unavailable_reason": "upstream_not_configured",
                    "relation": None,
                    "ahead": None,
                    "behind": None,
                },
                "remote_refresh_performed": False,
                "working_tree": {
                    "is_clean": True,
                    "entries": [],
                    "staged": {"count": 0, "paths": []},
                    "unstaged": {"count": 0, "paths": []},
                    "untracked": {"count": 0, "paths": []},
                    "unmerged": {"count": 0, "paths": []},
                },
                "workflow_state": "clean",
                "git_operation_in_progress": False,
                "git_operations": [],
                "mutation_preconditions": {
                    "branch_attached": True,
                    "staged_changes_present": False,
                    "unstaged_changes_present": False,
                    "untracked_changes_present": False,
                    "unmerged_entries_present": False,
                    "git_operation_in_progress": False,
                    "upstream_configured": False,
                    "upstream_divergence_available": False,
                },
                "current_snapshot_id_candidate": "snap--x",
                "matching_snapshot_exists": False,
                "matching_snapshot_id": None,
            }
            _publish_status_pair(workflow_root, original_payload, "# Milestone Git Status\n")

            original_json = (workflow_root / "status.json").read_bytes()
            original_md = (workflow_root / "status.md").read_bytes()

            updated = dict(original_payload)
            updated["workflow_state"] = "staged_only"

            def fail_hook(stage: str) -> None:
                if stage == "after_status_json_replace":
                    raise RuntimeError("controlled failure")

            with self.assertRaises(RuntimeError):
                _publish_status_pair(workflow_root, updated, "# changed\n", fail_hook=fail_hook)

            self.assertEqual((workflow_root / "status.json").read_bytes(), original_json)
            self.assertEqual((workflow_root / "status.md").read_bytes(), original_md)

    def test_status_does_not_create_snapshot_comparison_or_analysis(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            state = root / "state"

            result = generate_milestone_status(str(repo), state_root=state)
            repo_state = state / result["repository_id"]
            self.assertFalse((repo_state / "comparisons").exists())
            self.assertFalse((repo_state / "analyses").exists())

    def test_matching_snapshot_exists_and_integrity_failure(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            state = root / "state"

            scan = run_scan_with_artifacts(str(repo), state_root=state)
            snap = create_snapshot(scan, state_root=state)

            result = generate_milestone_status(str(repo), state_root=state)
            payload = _status_payload(state, result["repository_id"])
            self.assertTrue(payload["matching_snapshot_exists"])
            self.assertEqual(payload["matching_snapshot_id"], snap["snapshot_id"])

            snap_dir = Path(snap["snapshot_dir"])
            snapshot_json = json.loads((snap_dir / "snapshot.json").read_text(encoding="utf-8"))
            snapshot_json["artifact_hashes"]["repository.json"] = "0" * 64
            (snap_dir / "snapshot.json").write_text(json.dumps(snapshot_json, sort_keys=True) + "\n", encoding="utf-8")

            with self.assertRaises(WorkflowError):
                generate_milestone_status(str(repo), state_root=state)

    def test_repeated_identical_state_is_byte_identical(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            state = root / "state"

            result = generate_milestone_status(str(repo), state_root=state)
            repo_state = state / result["repository_id"] / "workflow"
            first_json = (repo_state / "status.json").read_bytes()
            first_md = (repo_state / "status.md").read_bytes()

            result2 = generate_milestone_status(str(repo), state_root=state)
            repo_state2 = state / result2["repository_id"] / "workflow"
            second_json = (repo_state2 / "status.json").read_bytes()
            second_md = (repo_state2 / "status.md").read_bytes()

            self.assertEqual(first_json, second_json)
            self.assertEqual(first_md, second_md)

    def test_target_git_status_is_unchanged_before_after(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            state = root / "state"

            before = _git(repo, "status", "--porcelain=v2", "-z", "--untracked-files=all")
            generate_milestone_status(str(repo), state_root=state)
            after = _git(repo, "status", "--porcelain=v2", "-z", "--untracked-files=all")
            self.assertEqual(before, after)

    def test_cli_milestone_status_summary(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            state = root / "state"

            with mock.patch("repoctl.workflow.status.DEFAULT_STATE_ROOT", state):
                code = main(["milestone", "status", "--repository", str(repo)])
            self.assertEqual(code, 0)

    def test_no_fetch_pull_push_in_status_path(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            state = root / "state"

            real_run = subprocess.run

            def guarded_run(cmd, *args, **kwargs):
                if isinstance(cmd, list) and len(cmd) >= 5 and cmd[0] == "git":
                    joined = " ".join(cmd)
                    self.assertNotIn(" fetch ", f" {joined} ")
                    self.assertNotIn(" pull ", f" {joined} ")
                    self.assertNotIn(" push ", f" {joined} ")
                return real_run(cmd, *args, **kwargs)

            with mock.patch("subprocess.run", side_effect=guarded_run):
                result = generate_milestone_status(str(repo), state_root=state)
                payload = _status_payload(state, result["repository_id"])
                self.assertFalse(payload["remote_refresh_performed"])

    def test_readme_and_status_markdown_local_ref_warning_present(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            state = root / "state"

            result = generate_milestone_status(str(repo), state_root=state)
            status_md = (state / result["repository_id"] / "workflow" / "status.md").read_text(encoding="utf-8")
            self.assertIn("No Git fetch is performed.", status_md)
            self.assertIn("locally available Git refs", status_md)


if __name__ == "__main__":
    unittest.main()
