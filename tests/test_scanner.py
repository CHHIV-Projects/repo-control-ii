from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from repoctl.cli import main
from repoctl.scanner.core import run_scan
from repoctl.scanner.git_ops import ScanError


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
    repo = root / "sample-repo"
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")

    (repo / "app.py").write_text(
        "import os\nfrom math import sqrt\n\n\ndef top_level():\n    def nested():\n        return 1\n    return nested()\n\n\nclass Greeter:\n    pass\n",
        encoding="utf-8",
    )
    (repo / "tests").mkdir(exist_ok=True)
    (repo / "tests" / "test_app.py").write_text(
        "class TestApp:\n    def test_run(self):\n        return True\n\n\ndef test_top_level():\n    return True\n",
        encoding="utf-8",
    )
    (repo / "requirements.txt").write_text("# comment\n\nrequests\nnumpy>=1.0\n", encoding="utf-8")
    (repo / "empty_requirements.txt").write_text("", encoding="utf-8")
    (repo / "notes.txt").write_text("line1\nline2\n", encoding="utf-8")

    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")
    return repo


class ScannerTests(unittest.TestCase):
    def test_invalid_target_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            missing = Path(td) / "missing"
            with self.assertRaises(ScanError):
                run_scan(str(missing), state_root=Path(td) / "state")

    def test_valid_scan_and_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            state_root = root / "state"

            result = run_scan(str(repo), state_root=state_root)
            output_dir = Path(result["output_dir"])
            self.assertTrue(output_dir.exists())

            expected = [
                "repository.json",
                "files.json",
                "symbols.json",
                "tests.json",
                "dependencies.json",
                "summary.md",
            ]
            for name in expected:
                self.assertTrue((output_dir / name).exists())

            repository = json.loads((output_dir / "repository.json").read_text(encoding="utf-8"))
            self.assertEqual(repository["schema_version"], 1)
            self.assertEqual(repository["repository_root"], str(repo.resolve()))
            self.assertIn("--", repository["repository_id"])
            self.assertEqual(repository["tracked_file_count"], 5)

    def test_files_hash_size_line_count(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            result = run_scan(str(repo), state_root=root / "state")
            files = json.loads((Path(result["output_dir"]) / "files.json").read_text(encoding="utf-8"))["files"]
            by_path = {item["path"]: item for item in files}
            self.assertEqual(by_path["notes.txt"]["line_count"], 2)
            self.assertGreater(by_path["notes.txt"]["byte_size"], 0)
            self.assertEqual(len(by_path["notes.txt"]["sha256"]), 64)

    def test_python_symbols_and_no_nested_top_level(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            result = run_scan(str(repo), state_root=root / "state")
            symbols = json.loads((Path(result["output_dir"]) / "symbols.json").read_text(encoding="utf-8"))["python_files"]
            app = [s for s in symbols if s["path"] == "app.py"][0]
            self.assertTrue(app["parse_success"])
            function_names = [f["name"] for f in app["top_level_functions"]]
            self.assertIn("top_level", function_names)
            self.assertNotIn("nested", function_names)
            class_names = [c["name"] for c in app["top_level_classes"]]
            self.assertIn("Greeter", class_names)

    def test_import_extraction(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            result = run_scan(str(repo), state_root=root / "state")
            symbols = json.loads((Path(result["output_dir"]) / "symbols.json").read_text(encoding="utf-8"))["python_files"]
            app = [s for s in symbols if s["path"] == "app.py"][0]
            modules = app["imported_module_names"]
            self.assertIn("os", modules)
            self.assertIn("math", modules)
            imported_symbols = app["imported_symbols"]
            self.assertTrue(any(s["symbol"] == "sqrt" for s in imported_symbols))

    def test_malformed_python_does_not_abort(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            (repo / "broken.py").write_text("def broken(:\n", encoding="utf-8")
            _git(repo, "add", "broken.py")
            _git(repo, "commit", "-m", "add broken file")

            result = run_scan(str(repo), state_root=root / "state")
            symbols = json.loads((Path(result["output_dir"]) / "symbols.json").read_text(encoding="utf-8"))["python_files"]
            broken = [s for s in symbols if s["path"] == "broken.py"][0]
            self.assertFalse(broken["parse_success"])
            self.assertIsNotNone(broken["parse_error"])

    def test_test_structure_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            result = run_scan(str(repo), state_root=root / "state")
            tests_payload = json.loads((Path(result["output_dir"]) / "tests.json").read_text(encoding="utf-8"))
            self.assertEqual(tests_payload["schema_version"], 2)
            self.assertEqual(tests_payload["test_like_file_count"], 1)
            self.assertEqual(tests_payload["test_class_count"], 1)
            self.assertEqual(tests_payload["test_method_count"], 1)
            self.assertEqual(tests_payload["top_level_test_function_count"], 1)

    def test_requirements_extraction_and_empty_list_entry(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            (repo / "requirements.txt").write_text("\n# c\n", encoding="utf-8")
            _git(repo, "add", "requirements.txt")
            _git(repo, "commit", "-m", "empty requirements")

            result = run_scan(str(repo), state_root=root / "state")
            repository = json.loads((Path(result["output_dir"]) / "repository.json").read_text(encoding="utf-8"))
            self.assertEqual(len(repository["requirements"]), 1)
            self.assertEqual(repository["requirements"][0]["path"], "requirements.txt")
            self.assertEqual(repository["requirements"][0]["declarations"], [])

    def test_dirty_working_tree_reporting(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            (repo / "app.py").write_text("import os\n", encoding="utf-8")
            (repo / "new.txt").write_text("x\n", encoding="utf-8")

            result = run_scan(str(repo), state_root=root / "state")
            repository = json.loads((Path(result["output_dir"]) / "repository.json").read_text(encoding="utf-8"))
            self.assertFalse(repository["working_tree"]["is_clean"])
            kinds = [entry["kind"] for entry in repository["working_tree"]["entries"]]
            self.assertIn("ordinary", kinds)
            self.assertIn("untracked", kinds)

    def test_deterministic_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            state = root / "state"

            first = run_scan(str(repo), state_root=state)
            out = Path(first["output_dir"])
            first_bytes = {
                name: (out / name).read_bytes()
                for name in ["repository.json", "files.json", "symbols.json", "tests.json", "dependencies.json", "summary.md"]
            }

            second = run_scan(str(repo), state_root=state)
            out2 = Path(second["output_dir"])
            second_bytes = {
                name: (out2 / name).read_bytes()
                for name in ["repository.json", "files.json", "symbols.json", "tests.json", "dependencies.json", "summary.md"]
            }

            self.assertEqual(first_bytes, second_bytes)

    def test_external_output_location(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            state = root / "my-state"
            result = run_scan(str(repo), state_root=state)
            output_dir = Path(result["output_dir"]).resolve()
            self.assertTrue(str(output_dir).startswith(str(state.resolve())))

    def test_target_repository_not_modified(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            before_head = _git(repo, "rev-parse", "HEAD")
            before_status = _git(repo, "status", "--porcelain=v2", "-z", "--untracked-files=all")

            run_scan(str(repo), state_root=root / "state")

            after_head = _git(repo, "rev-parse", "HEAD")
            after_status = _git(repo, "status", "--porcelain=v2", "-z", "--untracked-files=all")
            self.assertEqual(before_head, after_head)
            self.assertEqual(before_status, after_status)

    def test_cli_scan_non_git_returns_non_zero(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            code = main(["scan", td])
            self.assertNotEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
