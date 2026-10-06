from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from repoctl.cli import main
from repoctl.compare.manager import compare_snapshots
from repoctl.compare.manager import derive_comparison_id
from repoctl.scanner.core import run_scan_with_artifacts
from repoctl.snapshot.manager import SCAN_ARTIFACTS, derive_snapshot_id, create_snapshot


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
    repo = root / "snap-repo"
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")

    (repo / "module_a.py").write_text(
        "from module_b import helper\n\n\ndef run():\n    return helper()\n",
        encoding="utf-8",
    )
    (repo / "module_b.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    (repo / "test_module_a.py").write_text(
        "from module_b import helper\n\n\ndef test_run():\n    helper()\n",
        encoding="utf-8",
    )
    (repo / "requirements.txt").write_text("requests\n", encoding="utf-8")

    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")
    return repo


class SnapshotCompareTests(unittest.TestCase):
    def test_snapshot_with_current_directory_default(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            old = Path.cwd()
            try:
                import os

                os.chdir(repo)
                code = main(["snapshot"])
                self.assertEqual(code, 0)
            finally:
                os.chdir(old)

    def test_snapshot_with_explicit_repository(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            code = main(["snapshot", "--repository", str(repo)])
            self.assertEqual(code, 0)

    def test_snapshot_invalid_repository_fails(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            code = main(["snapshot", "--repository", td])
            self.assertNotEqual(code, 0)

    def test_snapshot_performs_fresh_scan_and_hashes_match(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            state = root / "state"
            scan = run_scan_with_artifacts(str(repo), state_root=state)
            old_hash = json.loads((Path(scan["output_dir"]) / "files.json").read_text())["files"][0]["sha256"]
            (repo / "module_b.py").write_text("def helper():\n    return 2\n", encoding="utf-8")

            scan2 = run_scan_with_artifacts(str(repo), state_root=state)
            snap = create_snapshot(scan2, state_root=state)
            snap_dir = Path(snap["snapshot_dir"])
            files_payload = json.loads((snap_dir / "files.json").read_text())
            hashes = {row["path"]: row["sha256"] for row in files_payload["files"]}
            self.assertNotEqual(old_hash, hashes["module_b.py"])

            snapshot_json = json.loads((snap_dir / "snapshot.json").read_text())
            for name in SCAN_ARTIFACTS:
                self.assertEqual(snapshot_json["artifact_hashes"][name], _sha256((snap_dir / name).read_bytes()))

    def test_snapshot_id_deterministic_and_reused(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            state = root / "state"
            scan = run_scan_with_artifacts(str(repo), state_root=state)
            snap1 = create_snapshot(scan, state_root=state)
            scan_again = run_scan_with_artifacts(str(repo), state_root=state)
            snap2 = create_snapshot(scan_again, state_root=state)
            self.assertEqual(snap1["snapshot_id"], snap2["snapshot_id"])
            self.assertTrue(snap2["reused_existing"])

    def test_snapshot_required_artifacts_exact_copy(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            state = root / "state"
            scan = run_scan_with_artifacts(str(repo), state_root=state)
            scan_dir = Path(scan["output_dir"])
            snap = create_snapshot(scan, state_root=state)
            snap_dir = Path(snap["snapshot_dir"])
            for name in SCAN_ARTIFACTS:
                self.assertEqual((scan_dir / name).read_bytes(), (snap_dir / name).read_bytes())

    def test_snapshot_completeness_metadata_clean_and_partial(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            state = root / "state"
            clean = create_snapshot(run_scan_with_artifacts(str(repo), state_root=state), state_root=state)
            clean_payload = json.loads((Path(clean["snapshot_dir"]) / "snapshot.json").read_text())
            self.assertEqual(clean_payload["structural_coverage"]["worktree_completeness"], "complete_for_tracked_files")

            (repo / "untracked.py").write_text("x = 1\n", encoding="utf-8")
            partial = create_snapshot(run_scan_with_artifacts(str(repo), state_root=state), state_root=state)
            partial_payload = json.loads((Path(partial["snapshot_dir"]) / "snapshot.json").read_text())
            self.assertEqual(partial_payload["structural_coverage"]["worktree_completeness"], "partial_worktree")
            self.assertIn("untracked.py", partial_payload["structural_coverage"]["untracked_paths"])

    def test_snapshot_does_not_mutate_target_git_state(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            before = _git(repo, "status", "--porcelain=v2", "-z", "--untracked-files=all")
            code = main(["snapshot", "--repository", str(repo)])
            self.assertEqual(code, 0)
            after = _git(repo, "status", "--porcelain=v2", "-z", "--untracked-files=all")
            self.assertEqual(before, after)

    def test_compare_same_snapshot_zero_delta_with_retained_counts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            state = root / "state"
            snap = create_snapshot(run_scan_with_artifacts(str(repo), state_root=state), state_root=state)
            result = compare_snapshots(snap["snapshot_id"], snap["snapshot_id"], str(repo), state_root=state)
            self.assertEqual(result["comparison_id"], derive_comparison_id(snap["snapshot_id"], snap["snapshot_id"]))
            cmp_id = derive_comparison_id(snap["snapshot_id"], snap["snapshot_id"])
            cmp_json = json.loads((state / snap["repository_id"] / "comparisons" / cmp_id / "comparison.json").read_text())
            self.assertEqual(cmp_json["aggregate_counts"]["files"]["added"], 0)
            self.assertEqual(cmp_json["aggregate_counts"]["files"]["removed"], 0)
            self.assertEqual(cmp_json["aggregate_counts"]["files"]["content_changed"], 0)
            self.assertGreater(cmp_json["aggregate_counts"]["files"]["unchanged"], 0)
            self.assertGreater(cmp_json["aggregate_counts"]["symbols"]["retained"], 0)

    def test_compare_missing_snapshot_failure(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            state = root / "state"
            create_snapshot(run_scan_with_artifacts(str(repo), state_root=state), state_root=state)
            with self.assertRaises(RuntimeError):
                compare_snapshots("snap--doesnotexist", "snap--doesnotexist", str(repo), state_root=state)

    def test_compare_corrupted_snapshot_failure(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            state = root / "state"
            snap = create_snapshot(run_scan_with_artifacts(str(repo), state_root=state), state_root=state)
            snap_dir = Path(snap["snapshot_dir"])
            snapshot_payload = json.loads((snap_dir / "snapshot.json").read_text())
            snapshot_payload["artifact_hashes"]["repository.json"] = "0" * 64
            (snap_dir / "snapshot.json").write_text(json.dumps(snapshot_payload, sort_keys=True) + "\n", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                compare_snapshots(snap["snapshot_id"], snap["snapshot_id"], str(repo), state_root=state)

    def test_compare_directional_id_and_current_repo_independence(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            state = root / "state"
            before = create_snapshot(run_scan_with_artifacts(str(repo), state_root=state), state_root=state)

            (repo / "module_c.py").write_text("def new_func():\n    return 3\n", encoding="utf-8")
            _git(repo, "add", ".")
            _git(repo, "commit", "-m", "after change")
            after = create_snapshot(run_scan_with_artifacts(str(repo), state_root=state), state_root=state)

            result = compare_snapshots(before["snapshot_id"], after["snapshot_id"], str(repo), state_root=state)
            self.assertEqual(result["comparison_id"], derive_comparison_id(before["snapshot_id"], after["snapshot_id"]))
            forward_id = derive_comparison_id(before["snapshot_id"], after["snapshot_id"])
            reverse_id = derive_comparison_id(after["snapshot_id"], before["snapshot_id"])
            self.assertNotEqual(forward_id, reverse_id)
            cmp_path = state / before["repository_id"] / "comparisons" / forward_id / "comparison.json"
            forward_bytes = cmp_path.read_bytes()

            (repo / "later.py").write_text("y = 2\n", encoding="utf-8")
            _git(repo, "add", ".")
            _git(repo, "commit", "-m", "later unrelated change")

            result = compare_snapshots(before["snapshot_id"], after["snapshot_id"], str(repo), state_root=state)
            self.assertEqual(result["comparison_id"], forward_id)
            self.assertEqual(forward_bytes, cmp_path.read_bytes())

    def test_controlled_structural_delta(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            state = root / "state"
            before = create_snapshot(run_scan_with_artifacts(str(repo), state_root=state), state_root=state)

            (repo / "module_b.py").write_text(
                "from module_c import new_func\n\ndef helper():\n    return new_func()\n",
                encoding="utf-8",
            )
            (repo / "module_c.py").write_text("def new_func():\n    return 4\n", encoding="utf-8")
            (repo / "test_module_a.py").write_text(
                "from module_c import new_func\n\n\ndef test_run():\n    new_func()\n",
                encoding="utf-8",
            )
            (repo / "requirements.txt").write_text("requests\npytest\n", encoding="utf-8")
            _git(repo, "add", ".")
            _git(repo, "commit", "-m", "controlled delta")

            after = create_snapshot(run_scan_with_artifacts(str(repo), state_root=state), state_root=state)
            result = compare_snapshots(before["snapshot_id"], after["snapshot_id"], str(repo), state_root=state)
            self.assertEqual(result["comparison_id"], derive_comparison_id(before["snapshot_id"], after["snapshot_id"]))
            cmp_id = derive_comparison_id(before["snapshot_id"], after["snapshot_id"])
            payload = json.loads((state / before["repository_id"] / "comparisons" / cmp_id / "comparison.json").read_text())

            self.assertTrue(any(item["path"] == "module_c.py" for item in payload["file_deltas"]["added"]))
            self.assertTrue(any(item["path"] == "requirements.txt" for item in payload["requirements_deltas"]["changed"]))
            self.assertTrue(any(item["symbol_name"] == "new_func" for item in payload["symbol_deltas"]["added"]))
            self.assertTrue(any(item["target_file"] == "module_c.py" for item in payload["module_dependency_deltas"]["added"]))
            self.assertTrue(any(item["callee_file"] == "module_c.py" for item in payload["call_deltas"]["added"]))
            self.assertTrue(any(item["target_file"] == "module_c.py" for item in payload["test_reference_deltas"]["added"]))

    def test_parse_failure_and_markdown_bound(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            state = root / "state"
            before = create_snapshot(run_scan_with_artifacts(str(repo), state_root=state), state_root=state)

            (repo / "broken.py").write_text("def broken(:\n", encoding="utf-8")
            for i in range(60):
                (repo / f"extra_{i}.py").write_text(f"x_{i} = {i}\n", encoding="utf-8")
            _git(repo, "add", ".")
            _git(repo, "commit", "-m", "parse failure and many files")
            after = create_snapshot(run_scan_with_artifacts(str(repo), state_root=state), state_root=state)
            result = compare_snapshots(before["snapshot_id"], after["snapshot_id"], str(repo), state_root=state)
            self.assertEqual(result["comparison_id"], derive_comparison_id(before["snapshot_id"], after["snapshot_id"]))
            cmp_id = derive_comparison_id(before["snapshot_id"], after["snapshot_id"])
            cmp_dir = state / before["repository_id"] / "comparisons" / cmp_id
            payload = json.loads((cmp_dir / "comparison.json").read_text())
            self.assertTrue(payload["parse_failure_deltas"]["introduced_parse_failures"])
            md = (cmp_dir / "comparison.md").read_text(encoding="utf-8")
            self.assertIn("markdown_truncated: True", md)


def _sha256(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


if __name__ == "__main__":
    unittest.main()