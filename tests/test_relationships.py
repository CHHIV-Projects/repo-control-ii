from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from repoctl.scanner.core import run_scan


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
    repo = root / "rel-repo"
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    return repo


def _scan(repo: Path, state_root: Path) -> tuple[dict, dict, str]:
    result = run_scan(str(repo), state_root=state_root)
    out = Path(result["output_dir"])
    deps = json.loads((out / "dependencies.json").read_text(encoding="utf-8"))
    tests_payload = json.loads((out / "tests.json").read_text(encoding="utf-8"))
    summary = (out / "summary.md").read_text(encoding="utf-8")
    return deps, tests_payload, summary


class RelationshipTests(unittest.TestCase):
    def test_module_and_symbol_resolution_and_calls(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            (repo / "util.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
            (repo / "app.py").write_text(
                "import util\nfrom util import helper as helper_alias\n\n\ndef local_b():\n    return 2\n\n\ndef local_a():\n    local_b()\n    helper_alias()\n    util.helper()\n",
                encoding="utf-8",
            )
            _git(repo, "add", ".")
            _git(repo, "commit", "-m", "add relationship files")

            deps, _, _ = _scan(repo, root / "state")

            self.assertEqual(deps["schema_version"], 1)
            self.assertTrue(any(e["import_kind"] == "import_module" and e["target_file"] == "util.py" for e in deps["module_dependencies"]))
            self.assertTrue(any(e["import_kind"] == "from_module" and e["target_file"] == "util.py" for e in deps["module_dependencies"]))
            self.assertTrue(any(e["local_name"] == "helper_alias" and e["target_symbol"] == "helper" for e in deps["imported_symbol_relationships"]))

            call_kinds = {(c["caller_symbol"], c["callee_symbol"], c["resolution_kind"]) for c in deps["call_relationships"]}
            self.assertIn(("local_a", "local_b", "same_module"), call_kinds)
            self.assertIn(("local_a", "helper", "imported_symbol"), call_kinds)
            self.assertIn(("local_a", "helper", "imported_module_attribute"), call_kinds)

    def test_external_import_not_internal(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            (repo / "app.py").write_text("import json\n", encoding="utf-8")
            _git(repo, "add", ".")
            _git(repo, "commit", "-m", "external import")

            deps, _, _ = _scan(repo, root / "state")
            self.assertFalse(deps["module_dependencies"])
            diag = [d for d in deps["unresolved_relationships"] if d["relationship_kind"] == "module_import"]
            self.assertTrue(any(d["reason"] == "no_tracked_module_match" for d in diag))

    def test_ambiguous_src_and_root_module(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            (repo / "pkg").mkdir()
            (repo / "pkg" / "mod.py").write_text("def fn():\n    return 1\n", encoding="utf-8")
            (repo / "src").mkdir()
            (repo / "src" / "pkg").mkdir()
            (repo / "src" / "pkg" / "mod.py").write_text("def fn():\n    return 2\n", encoding="utf-8")
            (repo / "app.py").write_text("import pkg.mod\n", encoding="utf-8")
            _git(repo, "add", ".")
            _git(repo, "commit", "-m", "ambiguous module")

            deps, _, _ = _scan(repo, root / "state")
            self.assertFalse(deps["module_dependencies"])
            ambiguous = [d for d in deps["unresolved_relationships"] if d["reason"] == "ambiguous_module"]
            self.assertTrue(ambiguous)
            self.assertTrue(all(d["candidates"] for d in ambiguous))

    def test_wildcard_import_no_symbol_relationship(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            (repo / "util.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
            (repo / "app.py").write_text("from util import *\n", encoding="utf-8")
            _git(repo, "add", ".")
            _git(repo, "commit", "-m", "wildcard import")

            deps, _, _ = _scan(repo, root / "state")
            self.assertFalse(deps["imported_symbol_relationships"])
            self.assertTrue(any(d["reason"] == "wildcard_import" for d in deps["unresolved_relationships"]))

    def test_shadowed_imported_function_no_call_edge(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            (repo / "util.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
            (repo / "app.py").write_text(
                "from util import helper\n\n\ndef run(helper):\n    helper()\n",
                encoding="utf-8",
            )
            _git(repo, "add", ".")
            _git(repo, "commit", "-m", "shadowed imported function")

            deps, _, _ = _scan(repo, root / "state")
            self.assertFalse(deps["call_relationships"])
            self.assertTrue(any(d["reason"] == "shadowed_or_rebound" and d["relationship_kind"] == "call" for d in deps["unresolved_relationships"]))

    def test_module_rebinding_prevents_false_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            (repo / "util.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
            (repo / "app.py").write_text(
                "from util import helper\nhelper = lambda: 2\n\n\ndef run():\n    helper()\n",
                encoding="utf-8",
            )
            _git(repo, "add", ".")
            _git(repo, "commit", "-m", "rebound imported symbol")

            deps, _, _ = _scan(repo, root / "state")
            self.assertFalse(deps["call_relationships"])
            self.assertTrue(any(d["reason"] == "shadowed_or_rebound" for d in deps["unresolved_relationships"]))

    def test_parse_failure_target_still_allows_module_edge(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            (repo / "bad.py").write_text("def broken(:\n", encoding="utf-8")
            (repo / "app.py").write_text("import bad\nfrom bad import broken\n\n\ndef run():\n    bad.broken()\n    broken()\n", encoding="utf-8")
            _git(repo, "add", ".")
            _git(repo, "commit", "-m", "parse failure target")

            deps, _, _ = _scan(repo, root / "state")
            self.assertTrue(any(e["target_file"] == "bad.py" for e in deps["module_dependencies"]))
            self.assertFalse(deps["imported_symbol_relationships"])
            self.assertFalse(deps["call_relationships"])
            self.assertTrue(any(d["reason"] == "target_parse_failure" for d in deps["unresolved_relationships"]))

    def test_test_reference_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            (repo / "util.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
            (repo / "tests").mkdir()
            (repo / "tests" / "test_util.py").write_text(
                "from util import helper\n\n\ndef test_top():\n    helper\n    helper()\n\n\nclass TestUtil:\n    def test_method(self):\n        helper()\n",
                encoding="utf-8",
            )
            _git(repo, "add", ".")
            _git(repo, "commit", "-m", "test reference")

            _, tests_payload, summary = _scan(repo, root / "state")
            self.assertEqual(tests_payload["schema_version"], 2)
            refs = tests_payload["test_files"][0]["top_level_test_functions"][0]["resolved_references"]
            self.assertTrue(any(r["target_symbol"] == "helper" and r["reference_kind"] == "import" for r in refs))
            self.assertTrue(any(r["target_symbol"] == "helper" and r["reference_kind"] == "call" for r in refs))
            method_refs = tests_payload["test_files"][0]["classes"][0]["test_methods"][0]["resolved_references"]
            self.assertTrue(any(r["target_symbol"] == "helper" for r in method_refs))
            self.assertNotIn("fully tested", summary)
            self.assertNotIn("adequate coverage", summary)

    def test_deterministic_dependencies_and_required_diagnostic_fields(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            (repo / "app.py").write_text("import json\n", encoding="utf-8")
            _git(repo, "add", ".")
            _git(repo, "commit", "-m", "determinism")

            deps1, tests1, _ = _scan(repo, root / "state")
            deps2, tests2, _ = _scan(repo, root / "state")
            self.assertEqual(deps1, deps2)
            self.assertEqual(tests1, tests2)

            self.assertTrue(deps1["unresolved_relationships"])
            sample = deps1["unresolved_relationships"][0]
            for key in [
                "relationship_kind",
                "source_file",
                "source_symbol",
                "source_line",
                "reference",
                "reason",
                "candidates",
            ]:
                self.assertIn(key, sample)


if __name__ == "__main__":
    unittest.main()
