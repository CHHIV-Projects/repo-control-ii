from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from repoctl.cli import main
from repoctl.context.policy import MATCH_STATUS_MATCHED, MATCH_STATUS_NO_MATCHES, make_context_id, tokenize_text
from repoctl.scanner.core import run_scan_with_artifacts
from repoctl.context.generator import build_and_publish_context


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
    repo = root / "ctx-repo"
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")

    (repo / "vocab_utils.py").write_text(
        "def normalize_synonym_candidates(items):\n    return items\n\ndef helper():\n    return 1\n",
        encoding="utf-8",
    )
    (repo / "app.py").write_text(
        "from vocab_utils import normalize_synonym_candidates\n\ndef get_synonyms_nltk(data):\n    return normalize_synonym_candidates(data)\n",
        encoding="utf-8",
    )
    (repo / "tests").mkdir()
    (repo / "tests" / "test_vocab_utils.py").write_text(
        "from vocab_utils import normalize_synonym_candidates\n\nclass TestNormalize:\n    def test_limit_zero(self):\n        normalize_synonym_candidates([])\n",
        encoding="utf-8",
    )

    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init")
    return repo


class ContextTests(unittest.TestCase):
    def test_tokenization_rules(self) -> None:
        self.assertEqual(tokenize_text("get_synonyms_nltk"), ["get", "synonyms", "nltk"])
        self.assertEqual(tokenize_text("HTTPServer"), ["httpserver"])
        self.assertEqual(tokenize_text("source—readiness"), ["source—readiness"])
        self.assertEqual(tokenize_text("naïve-path"), ["naïve", "path"])

    def test_context_with_explicit_repository(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            code = main(["context", "synonym", "--repository", str(repo)])
            self.assertEqual(code, 0)

    def test_context_with_current_directory_default(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            old = Path.cwd()
            try:
                os.chdir(repo)
                code = main(["context", "synonym"])
                self.assertEqual(code, 0)
            finally:
                os.chdir(old)

    def test_invalid_repository_fails(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            code = main(["context", "synonym", "--repository", td])
            self.assertNotEqual(code, 0)

    def test_empty_query_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            code = main(["context", "   ", "--repository", str(repo)])
            self.assertNotEqual(code, 0)

    def test_canonical_query_and_context_id(self) -> None:
        self.assertEqual(make_context_id("source readiness"), make_context_id("source readiness"))

    def test_context_output_and_no_source_snippets(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            scan = run_scan_with_artifacts(str(repo), state_root=root / "state")
            result = build_and_publish_context(scan_result=scan, query="synonym handling")
            out = Path(result["context_dir"])
            self.assertTrue((out / "context.json").exists())
            self.assertTrue((out / "context.md").exists())
            md = (out / "context.md").read_text(encoding="utf-8")
            self.assertIn("# Repository Context", md)
            self.assertNotIn("def normalize_synonym_candidates", md)

    def test_no_match_is_successful(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            scan = run_scan_with_artifacts(str(repo), state_root=root / "state")
            result = build_and_publish_context(scan_result=scan, query="zzzzzzzzzz")
            out = Path(result["context_dir"])
            payload = json.loads((out / "context.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["match_status"], MATCH_STATUS_NO_MATCHES)
            self.assertEqual(payload["selected_files"], [])

    def test_source_evidence_preserves_import_attribute_string_and_dependency_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            (repo / "requirements.txt").write_text("sample-lib\n", encoding="utf-8")
            (repo / "app.py").write_text(
                "import sample_lib\n\nstate = sample_lib.session_state\nmessage = 'Practice words'\n",
                encoding="utf-8",
            )
            _git(repo, "add", ".")
            _git(repo, "commit", "-m", "source evidence")
            scan = run_scan_with_artifacts(str(repo), state_root=root / "state")

            for query, evidence_kind in (("sample_lib", "import"), ("session_state", "attribute"), ("practice", "string_literal"), ("sample", "dependency")):
                result = build_and_publish_context(scan_result=scan, query=query)
                payload = json.loads((Path(result["context_dir"]) / "context.json").read_text(encoding="utf-8"))
                evidence = [seed for seed in payload["seed_matches"] if seed.get("evidence_kind") == evidence_kind]
                self.assertTrue(evidence, query)
                self.assertIn("app.py" if evidence_kind != "dependency" else "requirements.txt", [row["path"] for row in payload["selected_files"]])

    def test_source_evidence_uses_same_file_multiword_matching(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            (repo / "app.py").write_text("google_term = 'google'\nsheet_term = 'sheets'\n", encoding="utf-8")
            _git(repo, "add", "app.py")
            _git(repo, "commit", "-m", "multiword source evidence")
            scan = run_scan_with_artifacts(str(repo), state_root=root / "state")
            result = build_and_publish_context(scan_result=scan, query="google sheets")
            payload = json.loads((Path(result["context_dir"]) / "context.json").read_text(encoding="utf-8"))
            matches = [seed for seed in payload["seed_matches"] if seed.get("evidence_kind") == "string_literal"]
            self.assertEqual({token for match in matches for token in match["matched_tokens"]}, {"google", "sheets"})
            self.assertTrue(all("same-file token-AND evidence" in match["reason"] for match in matches))
            self.assertEqual([row["path"] for row in payload["selected_files"]], ["app.py"])

    def test_source_evidence_maps_to_enclosing_symbol(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            (repo / "app.py").write_text(
                "def persist():\n    value = 'practice'\n    return value\n",
                encoding="utf-8",
            )
            _git(repo, "add", "app.py")
            _git(repo, "commit", "-m", "enclosing source evidence")
            scan = run_scan_with_artifacts(str(repo), state_root=root / "state")
            result = build_and_publish_context(scan_result=scan, query="practice")
            payload = json.loads((Path(result["context_dir"]) / "context.json").read_text(encoding="utf-8"))
            evidence = [seed for seed in payload["seed_matches"] if seed.get("evidence_kind") == "string_literal"]
            self.assertEqual(evidence[0]["evidence"]["enclosing_symbol"]["name"], "persist")
            self.assertIn("persist", [symbol["symbol_name"] for symbol in payload["selected_symbols"]])

    def test_secret_named_python_files_are_excluded_from_source_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            (repo / "credentials.py").write_text("secret_value = 'fake-secret-value'\n", encoding="utf-8")
            _git(repo, "add", "credentials.py")
            _git(repo, "commit", "-m", "secret fixture")
            scan = run_scan_with_artifacts(str(repo), state_root=root / "state")
            result = build_and_publish_context(scan_result=scan, query="fake-secret-value")
            payload = json.loads((Path(result["context_dir"]) / "context.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["match_status"], MATCH_STATUS_NO_MATCHES)
            self.assertEqual(payload["selected_files"], [])

    def test_source_evidence_module_scope_priority_bounds_and_line_ranges(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            (repo / "app.py").write_text(
                "target = 'target'\n\ndef target():\n    return 'target'\n" + "\n".join(f"noise_{i} = 'noise'" for i in range(30)) + "\n",
                encoding="utf-8",
            )
            _git(repo, "add", "app.py")
            _git(repo, "commit", "-m", "module scope and priority")
            scan = run_scan_with_artifacts(str(repo), state_root=root / "state")

            target_result = build_and_publish_context(scan_result=scan, query="target")
            target_payload = json.loads((Path(target_result["context_dir"]) / "context.json").read_text(encoding="utf-8"))
            self.assertEqual(target_payload["seed_matches"][0]["seed_type"], "symbol")
            self.assertEqual(target_payload["seed_matches"][0]["symbol_name"], "target")

            noise_result = build_and_publish_context(scan_result=scan, query="noise")
            noise_payload = json.loads((Path(noise_result["context_dir"]) / "context.json").read_text(encoding="utf-8"))
            self.assertLessEqual(len(noise_payload["seed_matches"]), 12)
            string_matches = [seed for seed in noise_payload["seed_matches"] if seed.get("evidence_kind") == "string_literal"]
            self.assertTrue(string_matches)
            self.assertTrue(all(seed["evidence"]["scope"] == "module_scope" for seed in string_matches))
            self.assertTrue(all(seed["evidence"]["start_line"] <= seed["evidence"]["end_line"] for seed in string_matches))

            repeat_result = build_and_publish_context(scan_result=scan, query="noise")
            repeat_payload = json.loads((Path(repeat_result["context_dir"]) / "context.json").read_text(encoding="utf-8"))
            self.assertEqual(noise_payload["seed_matches"], repeat_payload["seed_matches"])

    def test_ignored_source_and_incidental_or_database_terms_do_not_claim_implementations(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            (repo / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
            (repo / "ignored.py").write_text("secret_value = 'container database'\n", encoding="utf-8")
            (repo / "app.py").write_text("width = 'use_container_width'\nmessage = 'database error'\n", encoding="utf-8")
            _git(repo, "add", ".gitignore", "app.py")
            _git(repo, "commit", "-m", "ignored and negative controls")
            scan = run_scan_with_artifacts(str(repo), state_root=root / "state")

            for query in ("container", "database"):
                result = build_and_publish_context(scan_result=scan, query=query)
                payload = json.loads((Path(result["context_dir"]) / "context.json").read_text(encoding="utf-8"))
                self.assertNotIn("ignored.py", [row["path"] for row in payload["selected_files"]])
                self.assertFalse(any(row.get("symbol_name") == "database" for row in payload["selected_symbols"]))
                if query == "container":
                    self.assertFalse(payload["selected_symbols"])
                    self.assertTrue(all(seed.get("evidence_kind") == "string_literal" for seed in payload["seed_matches"]))

    def test_related_test_inclusion(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            scan = run_scan_with_artifacts(str(repo), state_root=root / "state")
            result = build_and_publish_context(scan_result=scan, query="normalize_synonym_candidates")
            payload = json.loads((Path(result["context_dir"]) / "context.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["match_status"], MATCH_STATUS_MATCHED)
            self.assertTrue(payload["relevant_tests"])

    def test_deterministic_context_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            scan1 = run_scan_with_artifacts(str(repo), state_root=root / "state")
            r1 = build_and_publish_context(scan_result=scan1, query="synonym")
            out1 = Path(r1["context_dir"])
            b1_json = (out1 / "context.json").read_bytes()
            b1_md = (out1 / "context.md").read_bytes()

            scan2 = run_scan_with_artifacts(str(repo), state_root=root / "state")
            r2 = build_and_publish_context(scan_result=scan2, query="synonym")
            out2 = Path(r2["context_dir"])
            b2_json = (out2 / "context.json").read_bytes()
            b2_md = (out2 / "context.md").read_bytes()

            self.assertEqual(b1_json, b2_json)
            self.assertEqual(b1_md, b2_md)

    def test_context_triggers_fresh_scan(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            scan1 = run_scan_with_artifacts(str(repo), state_root=root / "state")
            build_and_publish_context(scan_result=scan1, query="newsymbol")

            (repo / "newsymbol.py").write_text("def newsymbol():\n    return 1\n", encoding="utf-8")
            _git(repo, "add", "newsymbol.py")
            _git(repo, "commit", "-m", "add newsymbol")

            code = main(["context", "newsymbol", "--repository", str(repo)])
            self.assertEqual(code, 0)

    def test_target_repo_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            before = _git(repo, "status", "--porcelain=v2", "-z", "--untracked-files=all")
            code = main(["context", "synonym", "--repository", str(repo)])
            self.assertEqual(code, 0)
            after = _git(repo, "status", "--porcelain=v2", "-z", "--untracked-files=all")
            self.assertEqual(before, after)

    def test_markdown_fixed_sections_present(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            scan = run_scan_with_artifacts(str(repo), state_root=root / "state")
            result = build_and_publish_context(scan_result=scan, query="synonym")
            md = (Path(result["context_dir"]) / "context.md").read_text(encoding="utf-8")
            for header in [
                "# Repository Context",
                "## Repository State",
                "## Query",
                "## Suggested Source Inspection",
                "## Relevant Symbols",
                "## Internal Relationships",
                "## Related Tests",
                "## Limitations / Ambiguities",
                "## Selection Metadata",
            ]:
                self.assertIn(header, md)

    def test_no_tracked_module_match_never_projected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            (repo / "extra.py").write_text("import requests\n\ndef run():\n    requests.get('x')\n", encoding="utf-8")
            _git(repo, "add", "extra.py")
            _git(repo, "commit", "-m", "add external unresolved")

            scan = run_scan_with_artifacts(str(repo), state_root=root / "state")
            result = build_and_publish_context(scan_result=scan, query="extra")
            payload = json.loads((Path(result["context_dir"]) / "context.json").read_text(encoding="utf-8"))
            reasons = [item["reason"] for item in payload["relevant_limitations"]]
            self.assertNotIn("no_tracked_module_match", reasons)

    def test_seed_and_file_symbol_limits(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            for i in range(30):
                (repo / f"synonym_{i}.py").write_text(
                    "def synonym_token_{}():\n    return 1\n".format(i),
                    encoding="utf-8",
                )
            _git(repo, "add", ".")
            _git(repo, "commit", "-m", "many synonym files")

            scan = run_scan_with_artifacts(str(repo), state_root=root / "state")
            result = build_and_publish_context(scan_result=scan, query="synonym")
            payload = json.loads((Path(result["context_dir"]) / "context.json").read_text(encoding="utf-8"))
            meta = payload["selection_metadata"]
            self.assertTrue(meta["seed_truncated"])
            self.assertLessEqual(meta["seed_selected"], 12)
            self.assertLessEqual(meta["selected_file_count"], 20)
            self.assertLessEqual(meta["selected_symbol_count"], 40)

    def test_allowlisted_limitation_intersection(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            (repo / "shadow.py").write_text(
                "from vocab_utils import helper\n\ndef run(helper):\n    helper()\n",
                encoding="utf-8",
            )
            _git(repo, "add", "shadow.py")
            _git(repo, "commit", "-m", "shadowed call")

            scan = run_scan_with_artifacts(str(repo), state_root=root / "state")
            result = build_and_publish_context(scan_result=scan, query="shadow")
            payload = json.loads((Path(result["context_dir"]) / "context.json").read_text(encoding="utf-8"))
            reasons = [item["reason"] for item in payload["relevant_limitations"]]
            # When intersecting selected context, allowlisted shadow diagnostics are retained.
            self.assertIn("shadowed_or_rebound", reasons)

    def test_no_recursive_second_hop_expansion(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = root / "ctx-repo"
            repo.mkdir(parents=True, exist_ok=True)
            _git(repo, "init")
            _git(repo, "config", "user.email", "test@example.com")
            _git(repo, "config", "user.name", "Test User")

            (repo / "chain.py").write_text(
                "def seed_target():\n    b()\n\ndef b():\n    c()\n\ndef c():\n    return 1\n",
                encoding="utf-8",
            )
            _git(repo, "add", ".")
            _git(repo, "commit", "-m", "call chain")

            scan = run_scan_with_artifacts(str(repo), state_root=root / "state")
            result = build_and_publish_context(scan_result=scan, query="seed_target")
            payload = json.loads((Path(result["context_dir"]) / "context.json").read_text(encoding="utf-8"))
            rels = [
                (r["source_symbol"], r["target_symbol"], r["relationship_type"]) for r in payload["relevant_internal_relationships"]
            ]
            self.assertIn(("seed_target", "b", "call"), rels)
            self.assertNotIn(("b", "c", "call"), rels)


if __name__ == "__main__":
    unittest.main()
