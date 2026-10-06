from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from repoctl.cli import main
from repoctl.workflow.errors import WorkflowReasonError
from repoctl.workflow.stage_execution import execute_prepared_stage
from repoctl.workflow.stage_plan import prepare_stage


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


def _init_repo(root: Path, name: str = "wf-stage") -> Path:
    repo = root / name
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "app.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    (repo / "old.txt").write_text("old\n", encoding="utf-8")
    _git(repo, "add", "app.py", "old.txt")
    _git(repo, "commit", "-m", "initial")
    return repo


def _make_visible_changes(repo: Path) -> None:
    (repo / "app.py").write_text("def run():\n    return 2\n", encoding="utf-8")
    (repo / "new.txt").write_text("new\n", encoding="utf-8")
    (repo / "old.txt").unlink()


class WorkflowStageTests(unittest.TestCase):
    def test_prepare_success_is_target_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            state = root / "state"
            _make_visible_changes(repo)

            before_head = _git_text(repo, "rev-parse", "HEAD")
            before_status = _git(repo, "status", "--porcelain=v2", "-z", "--untracked-files=all")
            result = prepare_stage(str(repo), True, state_root=state)
            after_head = _git_text(repo, "rev-parse", "HEAD")
            after_status = _git(repo, "status", "--porcelain=v2", "-z", "--untracked-files=all")

            self.assertEqual(before_head, after_head)
            self.assertEqual(before_status, after_status)
            self.assertEqual(result["candidate_record_count"], 3)
            self.assertTrue((Path(result["plan_dir"]) / "plan.json").exists())

    def test_prepare_blocks_detached_staged_none_conflict_operation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state = root / "state"

            repo1 = _init_repo(root, "d1")
            _git(repo1, "checkout", "--detach", "HEAD")
            (repo1 / "app.py").write_text("def run():\n    return 2\n", encoding="utf-8")
            with self.assertRaises(WorkflowReasonError) as cm1:
                prepare_stage(str(repo1), True, state_root=state)
            self.assertEqual(cm1.exception.code, "detached_head")

            repo2 = _init_repo(root, "d2")
            (repo2 / "app.py").write_text("def run():\n    return 2\n", encoding="utf-8")
            _git(repo2, "add", "app.py")
            with self.assertRaises(WorkflowReasonError) as cm2:
                prepare_stage(str(repo2), True, state_root=state)
            self.assertEqual(cm2.exception.code, "staged_changes_present")

            repo3 = _init_repo(root, "d3")
            with self.assertRaises(WorkflowReasonError) as cm3:
                prepare_stage(str(repo3), True, state_root=state)
            self.assertEqual(cm3.exception.code, "no_stage_candidates")

            repo4 = _init_repo(root, "d4")
            branch = _git_text(repo4, "branch", "--show-current")
            _git(repo4, "checkout", "-b", "feature")
            (repo4 / "app.py").write_text("def run():\n    return 10\n", encoding="utf-8")
            _git(repo4, "add", "app.py")
            _git(repo4, "commit", "-m", "feature")
            _git(repo4, "checkout", branch)
            (repo4 / "app.py").write_text("def run():\n    return 20\n", encoding="utf-8")
            _git(repo4, "add", "app.py")
            _git(repo4, "commit", "-m", "main")
            proc = subprocess.run(["git", "-C", str(repo4), "merge", "feature"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            self.assertNotEqual(proc.returncode, 0)
            with self.assertRaises(WorkflowReasonError) as cm4:
                prepare_stage(str(repo4), True, state_root=state)
            self.assertEqual(cm4.exception.code, "conflicts_present")

            repo5 = _init_repo(root, "d5")
            _make_visible_changes(repo5)
            marker = _git_text(repo5, "rev-parse", "--git-path", "MERGE_HEAD")
            marker_path = Path(marker)
            if not marker_path.is_absolute():
                marker_path = (repo5 / marker_path).resolve()
            marker_path.parent.mkdir(parents=True, exist_ok=True)
            marker_path.write_text("deadbeef\n", encoding="utf-8")
            with self.assertRaises(WorkflowReasonError) as cm5:
                prepare_stage(str(repo5), True, state_root=state)
            self.assertEqual(cm5.exception.code, "git_operation_in_progress")

    def test_prepare_blocks_unsupported_git_filters(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            state = root / "state"
            (repo / ".gitattributes").write_text("*.bin filter=lfs\n", encoding="utf-8")
            (repo / "data.bin").write_text("x\n", encoding="utf-8")

            with self.assertRaises(WorkflowReasonError) as cm:
                prepare_stage(str(repo), True, state_root=state)
            self.assertEqual(cm.exception.code, "unsupported_git_filters")

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink support required")
    def test_prepare_blocks_unsupported_path_type(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            state = root / "state"
            os.symlink("app.py", repo / "link.py")
            with self.assertRaises(WorkflowReasonError) as cm:
                prepare_stage(str(repo), True, state_root=state)
            self.assertEqual(cm.exception.code, "unsupported_path_type")

    def test_plan_deterministic_and_integrity_checks(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            state = root / "state"
            _make_visible_changes(repo)

            plan1 = prepare_stage(str(repo), True, state_root=state)
            plan2 = prepare_stage(str(repo), True, state_root=state)
            self.assertEqual(plan1["plan_id"], plan2["plan_id"])
            self.assertTrue(plan2["reused_existing"])

            plan_dir = Path(plan1["plan_dir"])
            payload = json.loads((plan_dir / "plan.json").read_text(encoding="utf-8"))
            payload["candidate_record_count"] = 999
            (plan_dir / "plan.json").write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
            with self.assertRaises(WorkflowReasonError) as cm:
                execute_prepared_stage(str(repo), plan1["plan_id"], True, state_root=state)
            self.assertEqual(cm.exception.code, "plan_integrity_failed")

    def test_stage_requires_approval(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            state = root / "state"
            _make_visible_changes(repo)
            plan = prepare_stage(str(repo), True, state_root=state)
            before = _git(repo, "status", "--porcelain=v2", "-z", "--untracked-files=all")
            with self.assertRaises(WorkflowReasonError) as cm:
                execute_prepared_stage(str(repo), plan["plan_id"], False, state_root=state)
            self.assertEqual(cm.exception.code, "approval_required")
            self.assertEqual(before, _git(repo, "status", "--porcelain=v2", "-z", "--untracked-files=all"))

    def test_stale_plan_blocks_file_changes_pathset_head_branch(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state = root / "state"

            repo1 = _init_repo(root, "s1")
            _make_visible_changes(repo1)
            plan1 = prepare_stage(str(repo1), True, state_root=state)
            (repo1 / "new.txt").write_text("changed\n", encoding="utf-8")
            with self.assertRaises(WorkflowReasonError) as cm1:
                execute_prepared_stage(str(repo1), plan1["plan_id"], True, state_root=state)
            self.assertEqual(cm1.exception.code, "worktree_state_changed")

            repo2 = _init_repo(root, "s2")
            _make_visible_changes(repo2)
            plan2 = prepare_stage(str(repo2), True, state_root=state)
            (repo2 / "extra.txt").write_text("later\n", encoding="utf-8")
            with self.assertRaises(WorkflowReasonError) as cm2:
                execute_prepared_stage(str(repo2), plan2["plan_id"], True, state_root=state)
            self.assertEqual(cm2.exception.code, "worktree_state_changed")

            repo3 = _init_repo(root, "s3")
            _make_visible_changes(repo3)
            plan3 = prepare_stage(str(repo3), True, state_root=state)
            _git(repo3, "commit", "--allow-empty", "-m", "advance")
            with self.assertRaises(WorkflowReasonError) as cm3:
                execute_prepared_stage(str(repo3), plan3["plan_id"], True, state_root=state)
            self.assertEqual(cm3.exception.code, "head_changed")

            repo4 = _init_repo(root, "s4")
            _make_visible_changes(repo4)
            plan4 = prepare_stage(str(repo4), True, state_root=state)
            _git(repo4, "checkout", "-b", "other")
            with self.assertRaises(WorkflowReasonError) as cm4:
                execute_prepared_stage(str(repo4), plan4["plan_id"], True, state_root=state)
            self.assertEqual(cm4.exception.code, "branch_changed")

    def test_index_lock_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            state = root / "state"
            _make_visible_changes(repo)
            plan = prepare_stage(str(repo), True, state_root=state)

            lock = _git_text(repo, "rev-parse", "--git-path", "index.lock")
            lock_path = Path(lock)
            if not lock_path.is_absolute():
                lock_path = (repo / lock_path).resolve()
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock_path.write_text("locked\n", encoding="utf-8")
            with self.assertRaises(WorkflowReasonError) as cm:
                execute_prepared_stage(str(repo), plan["plan_id"], True, state_root=state)
            self.assertEqual(cm.exception.code, "git_index_locked")

    def test_successful_stage_and_execution_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            state = root / "state"
            _make_visible_changes(repo)
            plan = prepare_stage(str(repo), True, state_root=state)

            before = _git_text(repo, "rev-parse", "HEAD")
            result = execute_prepared_stage(str(repo), plan["plan_id"], True, state_root=state)
            self.assertEqual(before, _git_text(repo, "rev-parse", "HEAD"))
            self.assertEqual(result["head_before"], result["head_after"])
            self.assertEqual(_git_text(repo, "status", "--short"), "M  app.py\nA  new.txt\nD  old.txt")
            self.assertTrue((Path(result["execution_dir"]) / "execution.json").exists())

    def test_git_stage_failed_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            state = root / "state"
            _make_visible_changes(repo)
            plan = prepare_stage(str(repo), True, state_root=state)

            real_run = subprocess.run

            def fake_run(cmd, *args, **kwargs):
                if isinstance(cmd, list) and len(cmd) >= 5 and cmd[0] == "git" and cmd[3] == "add":
                    return subprocess.CompletedProcess(cmd, 1, stdout=b"", stderr=b"stage blocked")
                return real_run(cmd, *args, **kwargs)

            with mock.patch("repoctl.workflow.stage_execution.subprocess.run", side_effect=fake_run):
                with self.assertRaises(WorkflowReasonError) as cm:
                    execute_prepared_stage(str(repo), plan["plan_id"], True, state_root=state)
            self.assertEqual(cm.exception.code, "git_stage_failed")
            self.assertEqual(_git_text(repo, "status", "--short"), "M app.py\n D old.txt\n?? new.txt")

    def test_git_stage_failed_after_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            state = root / "state"
            (repo / "app.py").write_text("def run():\n    return 2\n", encoding="utf-8")
            plan = prepare_stage(str(repo), True, state_root=state)

            real_run = subprocess.run
            mutated = {"done": False}

            def fake_run(cmd, *args, **kwargs):
                if isinstance(cmd, list) and len(cmd) >= 5 and cmd[0] == "git" and cmd[3] == "add" and not mutated["done"]:
                    mutated["done"] = True
                    real_run(cmd, *args, **kwargs)
                    return subprocess.CompletedProcess(cmd, 1, stdout=b"", stderr=b"partial failure")
                return real_run(cmd, *args, **kwargs)

            with mock.patch("repoctl.workflow.stage_execution.subprocess.run", side_effect=fake_run):
                with self.assertRaises(WorkflowReasonError) as cm:
                    execute_prepared_stage(str(repo), plan["plan_id"], True, state_root=state)
            self.assertEqual(cm.exception.code, "git_stage_failed_after_mutation")
            self.assertEqual(_git_text(repo, "status", "--short"), "M  app.py")

    def test_post_stage_verification_failure_and_audit_failure(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state = root / "state"

            repo1 = _init_repo(root, "v1")
            _make_visible_changes(repo1)
            plan1 = prepare_stage(str(repo1), True, state_root=state)
            with self.assertRaises(WorkflowReasonError) as cm1:
                execute_prepared_stage(str(repo1), plan1["plan_id"], True, state_root=state, _test_force_post_verify_failure=True)
            self.assertEqual(cm1.exception.code, "post_stage_verification_failed")
            self.assertEqual(_git_text(repo1, "status", "--short"), "M  app.py\nA  new.txt\nD  old.txt")

            repo2 = _init_repo(root, "v2")
            _make_visible_changes(repo2)
            plan2 = prepare_stage(str(repo2), True, state_root=state)
            with self.assertRaises(WorkflowReasonError) as cm2:
                execute_prepared_stage(str(repo2), plan2["plan_id"], True, state_root=state, _test_force_audit_failure=True)
            self.assertEqual(cm2.exception.code, "stage_succeeded_audit_failed")
            self.assertEqual(_git_text(repo2, "status", "--short"), "M  app.py\nA  new.txt\nD  old.txt")

    def test_cli_prepare_stage_and_stage(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            state = root / "state"
            _make_visible_changes(repo)

            with mock.patch("repoctl.workflow.stage_plan.DEFAULT_STATE_ROOT", state), mock.patch(
                "repoctl.workflow.stage_execution.DEFAULT_STATE_ROOT", state
            ):
                code = main(["milestone", "prepare-stage", "--repository", str(repo), "--all"])
                self.assertEqual(code, 0)

                repo_id_dirs = [p for p in state.iterdir() if p.is_dir()]
                self.assertTrue(repo_id_dirs)
                plans = list((repo_id_dirs[0] / "workflow" / "stage_plans").iterdir())
                self.assertEqual(len(plans), 1)
                plan_id = plans[0].name

                code2 = main(["milestone", "stage", plan_id, "--approve", "--repository", str(repo)])
                self.assertEqual(code2, 0)


if __name__ == "__main__":
    unittest.main()