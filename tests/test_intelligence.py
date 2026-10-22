from __future__ import annotations

import copy
from contextlib import contextmanager
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from repoctl.cli import main
from repoctl.intelligence import client, measurements
from repoctl.intelligence.provider import GraphifyProvider, IntelligenceError, RUNTIME
from repoctl.intelligence.service import (
    RefreshAuthorizationError, artifact_root, build_graph, encoded, graph_status,
    intelligence_query, normalize, refresh_graph, safe_path, source_snapshot, write_json,
)
from repoctl.mcp.architect_adapter import AdapterValidationError, handle_rpc_request, validate_tool_call
from repoctl.web.app import create_web_app
from repoctl.web.repository_registry import RepositoryRegistry
from tests.test_architect_contract import _repo


class FakeProvider:
    version = "0.9.91"

    def build(self, source, output):
        (output / "graph.json").write_text(json.dumps({
            "graph": {"schema_version": 1, "graphify_version": self.version},
            "nodes": [{"id": "one"}], "links": [],
        }))

    def query(self, graph, arguments, timeout):
        return {
            "outcome": "OK",
            "nodes": [{"id": "one", "label": "one", "source_file": "src/one.py", "source_location": "L1"}],
            "relationships": [],
        }


class IntelligenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.repository_path = _repo(self.directory, "repo", "intelligence")
        (self.repository_path / "src").mkdir()
        (self.repository_path / "src/one.py").write_text("def one():\n    return 1\n")
        self.repository = RepositoryRegistry.single(self.repository_path).active()
        self.state = self.directory / "state"
        self.root = artifact_root(self.repository, self.state)
        self.provider = FakeProvider()

    def build(self):
        return build_graph(self.repository, self.state, self.provider)

    def query(self, arguments=None, **kwargs):
        return intelligence_query(
            self.repository, self.state, arguments or {"operation": "lookup", "query": "one"},
            consumer="Coder", registry_generation=0, provider=kwargs.get("provider", self.provider),
        )

    def test_freshness_all_states_and_dirty_add_delete(self):
        self.assertEqual(graph_status(self.repository, self.root)["state"], "MISSING")
        status = self.build()
        self.assertEqual(status["state"], "CURRENT")
        self.assertEqual(status["source"]["file_count"], 1)
        path = self.repository_path / "src/one.py"
        original = path.read_bytes()
        path.write_bytes(original + b"\n")
        self.assertEqual(graph_status(self.repository, self.root)["state"], "STALE")
        self.assertEqual(self.query()["outcome"], "STALE")
        path.write_bytes(original)
        self.assertEqual(graph_status(self.repository, self.root)["state"], "CURRENT")
        extra = path.with_name("two.py")
        extra.write_text("x = 1\n")
        self.assertEqual(graph_status(self.repository, self.root)["state"], "STALE")
        extra.unlink()
        self.assertEqual(graph_status(self.repository, self.root)["state"], "CURRENT")
        manifest_path = self.root / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["producer"] = "future"
        write_json(manifest_path, manifest)
        self.assertEqual(graph_status(self.repository, self.root)["state"], "INCOMPATIBLE")
        manifest.update(producer="0.9.91", state="BUILDING")
        write_json(manifest_path, manifest)
        self.assertEqual(graph_status(self.repository, self.root)["state"], "FAILED")

    def test_building_failure_no_stale_fallback_and_copy_cleanup(self):
        parent = self
        class FailingProvider(FakeProvider):
            def build(self, source, output):
                self.state = graph_status(parent.repository, parent.root)["state"]
                raise IntelligenceError("expected failure")
        provider = FailingProvider()
        with self.assertRaisesRegex(IntelligenceError, "expected failure"):
            build_graph(self.repository, self.state, provider)
        self.assertEqual(provider.state, "BUILDING")
        self.assertEqual(graph_status(self.repository, self.root)["state"], "FAILED")
        self.assertFalse(list((self.root / "generations").glob("*/source")))
        self.assertEqual(self.query()["evidence"]["nodes"], [])

    def test_refresh_current_is_noop_and_maintenance_is_separate_from_query_uses(self):
        current = self.build()
        with patch("repoctl.intelligence.service.build_graph") as build:
            result = refresh_graph(
                self.repository,
                self.state,
                actor="po_manual",
                registry_generation=0,
            )
        build.assert_not_called()
        self.assertEqual(result["outcome"], "CURRENT")
        self.assertEqual(result["receipt"]["action"], "noop_current")
        self.assertTrue(result["receipt"]["completed"])
        self.assertEqual(result["receipt"]["generation_before"], current["generation"])
        self.assertEqual(result["receipt"]["generation_after"], current["generation"])
        summary = measurements.summary(self.root)
        self.assertEqual(summary["uses"], 0)
        self.assertEqual(summary["refresh_attempts"], 1)
        self.assertEqual(summary["completed_refreshes"], 1)
        self.assertEqual(summary["refreshes"][0]["actor"], "po_manual")
        self.assertNotIn("query", summary["refreshes"][0])

    def test_refresh_missing_graph_builds_and_records_source_matched_receipt(self):
        result = refresh_graph(
            self.repository,
            self.state,
            actor="coder",
            milestone_id="M003.001",
            registry_generation=0,
            provider=self.provider,
        )
        self.assertEqual(result["outcome"], "REFRESHED")
        self.assertEqual(result["graph"]["state"], "CURRENT")
        self.assertEqual(result["receipt"]["action"], "build")
        self.assertEqual(result["receipt"]["triggering_freshness"], "MISSING")
        self.assertEqual(
            result["receipt"]["source_identity_before"],
            result["receipt"]["source_identity_after"],
        )
        self.assertTrue(result["receipt"]["completed"])
        self.assertEqual(measurements.summary(self.root)["uses"], 0)

    def test_refresh_without_supported_code_is_explicitly_not_available(self):
        (self.repository_path / "src/one.py").unlink()
        with patch("repoctl.intelligence.service.build_graph") as build:
            result = refresh_graph(
                self.repository,
                self.state,
                actor="po_lifecycle",
                milestone_id="M003.001",
                registry_generation=0,
            )
        build.assert_not_called()
        self.assertEqual(result["outcome"], "NOT_AVAILABLE")
        self.assertEqual(result["receipt"]["action"], "not_available")
        self.assertEqual(result["receipt"]["final_freshness"], "NOT_AVAILABLE")
        self.assertTrue(result["receipt"]["completed"])

    def test_refresh_authorization_change_prevents_current_publication(self):
        @contextmanager
        def reject_publication():
            raise RefreshAuthorizationError("milestone authorization changed")
            yield

        result = refresh_graph(
            self.repository,
            self.state,
            actor="coder",
            milestone_id="M003.001",
            registry_generation=0,
            publication_guard=reject_publication,
            provider=self.provider,
        )
        self.assertEqual(result["outcome"], "FAILED")
        self.assertEqual(result["receipt"]["error_code"], "authorization_changed")
        self.assertFalse(result["receipt"]["completed"])
        self.assertEqual(graph_status(self.repository, self.root)["state"], "FAILED")

    def test_refresh_contention_and_source_change_fail_explicitly(self):
        with patch("repoctl.intelligence.service.fcntl.flock", side_effect=BlockingIOError):
            contended = refresh_graph(
                self.repository,
                self.state,
                actor="coder",
                milestone_id="M003.001",
                registry_generation=0,
                provider=self.provider,
            )
        self.assertEqual(contended["outcome"], "FAILED")
        self.assertEqual(contended["receipt"]["error_code"], "build_failed")
        self.assertFalse(contended["receipt"]["completed"])

        class EditingProvider(FakeProvider):
            def build(inner_self, source, output):
                super().build(source, output)
                (self.repository_path / "src/one.py").write_text("changed_during_build = True\n")

        changed = refresh_graph(
            self.repository,
            self.state,
            actor="coder",
            milestone_id="M003.001",
            registry_generation=0,
            provider=EditingProvider(),
        )
        self.assertEqual(changed["outcome"], "FAILED")
        self.assertEqual(changed["receipt"]["error_code"], "source_changed")
        self.assertFalse(changed["receipt"]["completed"])
        self.assertEqual(graph_status(self.repository, self.root)["state"], "STALE")

    def test_source_identity_tracks_untracked_code_not_docs_or_state(self):
        before = source_snapshot(self.repository)
        (self.repository_path / "readme.md").write_text("docs\n")
        self.assertEqual(before, source_snapshot(self.repository))
        (self.repository_path / "src/two.js").write_text("function two() {}")
        self.assertNotEqual(before["content_hash"], source_snapshot(self.repository)["content_hash"])

    def test_containment_symlinks_and_external_state(self):
        for value in ("../secret.py", "/etc/passwd", "src//one.py", "src/./one.py", "src\\one.py"):
            with self.subTest(value=value), self.assertRaises(IntelligenceError):
                safe_path(self.repository_path, value)
        (self.repository_path / "src/link.py").symlink_to(self.directory / "outside.py")
        with self.assertRaises(IntelligenceError):
            source_snapshot(self.repository)
        with self.assertRaises(IntelligenceError):
            artifact_root(self.repository, self.repository_path / "state")

    def test_graph_tampering_never_serves_evidence(self):
        status = self.build()
        (self.root / f"generations/{status['generation']}/graph.json").write_text("{}")
        result = self.query()
        self.assertEqual(result["graph"]["state"], "FAILED")
        self.assertFalse(result["evidence"]["nodes"])

    def test_normalization_and_all_hard_input_bounds(self):
        for arguments in (
            {"operation": "arbitrary"}, {"graph": "/tmp/graph.json"}, {"tool": "triage_prs"},
            {"operation": "lookup"}, {"depth": 99}, {"timeout": 11}, {"max_nodes": 121},
            {"max_edges": 241}, {"max_bytes": 65537}, {"max_bytes": 100}, {"depth": True},
            {"direction": []}, {"relations": ["unknown"]}, {"query": "a" * 301},
            {"operation": "path", "query": "one", "target": "two", "max_nodes": 1},
        ):
            with self.subTest(arguments=arguments), self.assertRaises(IntelligenceError):
                normalize(arguments)
        for operation, direction in (("callers", "incoming"), ("callees", "outgoing")):
            result = normalize({"operation": operation, "query": "one"})
            self.assertEqual(result["relations"], ["calls"])
            self.assertEqual(result["direction"], direction)

    def test_result_node_edge_and_independent_byte_caps(self):
        self.build()
        class Broad(FakeProvider):
            def query(self, graph, arguments, timeout):
                return {
                    "outcome": "OK",
                    "nodes": [{"id": str(i), "source_file": "src/one.py", "label": "z" * 300} for i in range(500)],
                    "relationships": [{"source": "0", "target": str(i), "relation": "calls"} for i in range(500)],
                }
        result = self.query(
            {"operation": "context", "query": "workflow", "max_bytes": 4096, "max_nodes": 20, "max_edges": 10},
            provider=Broad(),
        )
        self.assertEqual(result["outcome"], "TRUNCATED")
        self.assertLessEqual(len(encoded(result)), 4096)
        self.assertLessEqual(result["counts"]["nodes"], 20)
        self.assertLessEqual(result["counts"]["relationships"], 10)
        self.assertEqual(result["counts"]["omitted_nodes"] + result["counts"]["nodes"], 500)
        self.assertEqual(result["counts"]["omitted_relationships"] + result["counts"]["relationships"], 500)
        self.assertTrue(result["truncated"])
        with patch("repoctl.mcp.architect_adapter.execute_get", return_value=(200, result)):
            rpc = handle_rpc_request({
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "repo_intelligence", "arguments": {
                    "expected_repository_key": "default", "expected_generation": 0,
                    "operation": "context", "query": "workflow", "max_bytes": 4096,
                }},
            }, "http://127.0.0.1:8765", err_stream=io.StringIO())
        self.assertEqual(rpc["result"]["content"][0]["text"], encoded(result).decode("ascii"))
        self.assertLessEqual(len(rpc["result"]["content"][0]["text"].encode()), 4096)

    def test_outcomes_ambiguous_missing_and_failures(self):
        self.build()
        class Outcome(FakeProvider):
            def query(self, graph, arguments, timeout):
                return {"outcome": arguments["query"], "nodes": [], "relationships": []}
        for outcome in ("AMBIGUOUS", "NOT_FOUND", "INCONCLUSIVE"):
            result = self.query({"operation": "lookup", "query": outcome}, provider=Outcome())
            self.assertEqual(result["outcome"], outcome)
        with patch.object(FakeProvider, "query", side_effect=IntelligenceError("isError=false native failure")):
            self.assertEqual(self.query()["outcome"], "FAILED")
        result = self.query()
        self.assertEqual(result["outcome"], "OK")
        self.assertEqual(result["counts"]["nodes"], 1)

    def test_source_change_during_query_discards_evidence(self):
        self.build()
        parent = self
        class Editing(FakeProvider):
            def query(self, graph, arguments, timeout):
                (parent.repository_path / "src/one.py").write_text("changed = 1\n")
                return super().query(graph, arguments, timeout)
        result = self.query(provider=Editing())
        self.assertEqual(result["outcome"], "STALE")
        self.assertFalse(result["evidence"]["nodes"])

    def test_measurements_minimal_private_and_ratings(self):
        self.build()
        result = self.query({"operation": "lookup", "query": "DO_NOT_STORE_THIS"})
        overview = measurements.summary(self.root)
        self.assertEqual(overview["uses"], 1)
        row = overview["recent"][0]
        self.assertNotIn("DO_NOT_STORE_THIS", json.dumps(row))
        self.assertIsNone(row["inspected"])
        self.assertEqual(row["response_bytes"], len(encoded(result)))
        measurements.rate(self.root, result["use_id"], "MISLEADING", True)
        measurements.rate(self.root, result["use_id"], "PARTIAL", False)
        overview = measurements.summary(self.root)
        self.assertEqual(overview["misleading_ever"], 1)
        self.assertEqual(overview["rated"], 1)
        self.assertEqual(overview["ratings"][1]["percent"], 100)
        self.assertFalse(overview["recent"][0]["inspected"])
        self.assertEqual((self.root / "measurements.sqlite3").stat().st_mode & 0o777, 0o600)
        with self.assertRaises(IntelligenceError):
            measurements.rate(self.root, "absent", "USEFUL")
        with self.assertRaises(IntelligenceError):
            measurements.rate(self.root, result["use_id"], "invalid")

    def test_aggregate_percentiles_and_benchmarks(self):
        for i in range(20):
            measurements.record(self.root, {
                "id": str(i), "timestamp": str(i).zfill(2), "repository_key": "default",
                "generation": None, "consumer": "Coder", "operation": "lookup",
                "latency_ms": i + 1, "response_bytes": 100 + i,
                "nodes": 1, "relationships": 0, "outcome": "AMBIGUOUS" if i == 0 else "OK", "truncated": i == 1,
            })
        overview = measurements.summary(self.root)
        self.assertEqual(overview["median_latency_ms"], 10.5)
        self.assertEqual(overview["p95_latency_ms"], 19)
        self.assertEqual(overview["ambiguous"], 1)
        self.assertEqual(overview["truncated"], 1)
        arm = {"files": 1, "source_bytes": 12, "evidence_bytes": 50, "interactions": 1, "elapsed_ms": 2}
        measurements.add_benchmark(self.root, {
            "id": "one", "task": "Find symbol", "control": arm, "graphify": arm,
            "assessment": "Equivalent", "method": "Mechanical replay, not model tokens",
        })
        self.assertEqual(len(measurements.summary(self.root)["benchmarks"]), 1)

    def test_architect_schema_rejects_passthrough_and_requires_preconditions(self):
        for args in ({}, {"expected_repository_key": "default", "expected_generation": 0, "graph": "/tmp/a"},
                     {"expected_repository_key": "default", "expected_generation": 0, "depth": 5}):
            with self.assertRaises(AdapterValidationError):
                validate_tool_call("repo_intelligence", args)
        path, args = validate_tool_call("repo_intelligence", {
            "expected_repository_key": "default", "expected_generation": 0, "operation": "lookup", "query": "one",
        })
        self.assertEqual(path, "/api/architect/v1/intelligence")
        self.assertEqual(json.loads(args["request"]), normalize({"operation": "lookup", "query": "one"}))

    def test_api_consumer_parity_preconditions_page_and_no_git_changes(self):
        self.build()
        app = create_web_app(repository_path=str(self.repository_path), state_root=self.state)
        browser = app.test_client()
        before = subprocess.check_output(["git", "-C", str(self.repository_path), "status", "--porcelain=v1"])
        arguments = {
            "expected_repository_key": "default", "expected_generation": "0",
            "request": json.dumps({"operation": "lookup", "query": "one"}),
        }
        with patch("repoctl.intelligence.service.GraphifyProvider", return_value=self.provider):
            architect = browser.get("/api/architect/v1/intelligence", query_string=arguments).get_json()
            coder = browser.get("/api/coder/v1/intelligence", query_string=arguments).get_json()
        for key in ("repository", "graph", "operation", "arguments", "evidence", "counts", "outcome"):
            self.assertEqual(architect[key], coder[key])
        self.assertEqual(coder["outcome"], "OK")
        self.assertEqual(coder["counts"]["nodes"], 1)
        arguments["expected_generation"] = "1"
        self.assertEqual(browser.get("/api/architect/v1/intelligence", query_string=arguments).status_code, 409)
        self.assertEqual(browser.post("/intelligence/build", data={"approve": "yes"}).status_code, 400)
        page = browser.get("/intelligence")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Repository Intelligence Value", page.data)
        self.assertIn(b"2</strong> recorded uses", page.data)
        self.assertEqual(
            before, subprocess.check_output(["git", "-C", str(self.repository_path), "status", "--porcelain=v1"])
        )
        self.assertEqual({row["consumer"] for row in measurements.summary(self.root)["recent"]}, {"Architect", "Coder"})

    def test_scanner_publication_preserves_graph_and_measurements(self):
        from repoctl.scanner.core import run_scan
        generation = self.build()["generation"]
        use = self.query()["use_id"]
        run_scan(self.repository_path, state_root=self.state)
        self.assertEqual(graph_status(self.repository, self.root)["generation"], generation)
        self.assertEqual(graph_status(self.repository, self.root)["state"], "CURRENT")
        self.assertEqual(measurements.summary(self.root)["recent"][0]["id"], use)

    def test_deliberate_owner_build_and_rating_forms(self):
        app = create_web_app(repository_path=str(self.repository_path), state_root=self.state)
        browser = app.test_client()
        browser.get("/intelligence")
        with browser.session_transaction() as session:
            token = session["csrf_token"]
        form = {"csrf_token": token, "repository_key": "default", "repository_generation": "0"}
        self.assertNotIn(b'name="approve"', browser.get("/intelligence").data)
        with patch("repoctl.intelligence.service.GraphifyProvider", return_value=self.provider):
            self.assertEqual(browser.post("/intelligence/build", data={**form, "approve": "yes"}).status_code, 400)
            self.assertEqual(
                browser.post("/intelligence/build", data={**form, "csrf_token": ""}).status_code,
                400,
            )
            self.assertEqual(
                browser.post("/intelligence/build", data={**form, "repository_generation": "9"}).status_code,
                400,
            )
            response = browser.post("/intelligence/build", data=form)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(graph_status(self.repository, self.root)["state"], "CURRENT")
        result = self.query()
        response = browser.post("/intelligence/rating", data={
            **form, "use_id": result["use_id"], "rating": "MISLEADING", "inspected": "yes",
        })
        self.assertEqual(response.status_code, 302)
        self.assertIn(b"MISLEADING ever reported: 1", browser.get("/intelligence").data)
        response = browser.post("/intelligence/rating", data={
            **form, "repository_generation": "9", "use_id": result["use_id"], "rating": "USEFUL",
        })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(measurements.summary(self.root)["recent"][0]["rating"], "MISLEADING")

    def test_generation_retention_is_bounded(self):
        for _ in range(4):
            self.build()
        self.assertEqual(len(list((self.root / "generations").iterdir())), 2)

    def test_cli_prints_same_contract_and_does_not_add_context_graph(self):
        expected = {"outcome": "OK", "graph": {"generation": "one"}}
        with patch.object(client, "query", return_value=expected) as called, patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(main(["coder-intelligence", "lookup", "one"]), 0)
            self.assertEqual(json.loads(output.getvalue()), expected)
            self.assertEqual(called.call_args.args[0]["query"], "one")


@unittest.skipUnless(RUNTIME.is_file(), "Dedicated pinned Graphify environment is not provisioned")
class NativeBridgeTests(unittest.TestCase):
    def test_native_bridge_isolation_direction_ambiguity_context_impact_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "one.py").write_text(
                "def target():\n    return 1\n\ndef caller():\n    return target()\n\ndef top():\n    return caller()\n"
            )
            (source / "two.py").write_text("def target():\n    return 2\n")
            provider = GraphifyProvider()
            output = root / "output"
            provider.build(source, output)
            graph = output / "graph.json"
            def query(operation, value, **kwargs):
                return provider.query(graph, normalize({"operation": operation, "query": value, **kwargs}), 5)
            self.assertEqual(query("lookup", "target")["outcome"], "AMBIGUOUS")
            self.assertEqual(query("lookup", "not_found_symbol")["outcome"], "NOT_FOUND")
            self.assertEqual(query("lookup", "one.py::target")["outcome"], "OK")
            self.assertEqual(len(query("lookup", "one.py")["nodes"]), 1)
            self.assertEqual(query("lookup", "missing.py::target")["outcome"], "NOT_FOUND")
            incoming = query("callers", "one.py::target")
            self.assertTrue(incoming["relationships"])
            self.assertTrue(all(edge["relation"] == "calls" for edge in incoming["relationships"]))
            self.assertTrue(all("caller" in edge["source"] for edge in incoming["relationships"]))
            outgoing = query("callees", "caller")
            self.assertEqual(incoming["relationships"], outgoing["relationships"])
            self.assertTrue(query("context", "caller")["nodes"])
            self.assertTrue(query("impact", "one.py::target").get("impact"))
            self.assertIn("Shortest path", query("path", "caller", target="one.py::target")["native_text"])
            self.assertEqual(query("path", "top", target="one.py::target", depth=1)["outcome"], "TRUNCATED")
            self.assertEqual(query("path", "caller", target="two.py::target")["outcome"], "INCONCLUSIVE")
            self.assertFalse(list(source.glob(".graphify*")))
            self.assertFalse(list((output / "home").glob("**/*credentials*")))

    def test_native_missing_corrupt_and_oversized_graphs_fail_explicitly(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "graph.json"
            arguments = normalize({"operation": "lookup", "query": "one"})
            for content in (None, "{broken", " " * (24 * 1024 * 1024 + 1)):
                if content is not None:
                    path.write_text(content)
                with self.assertRaisesRegex(IntelligenceError, "Graphify child failed"):
                    GraphifyProvider().query(path, arguments, 5)

    def test_network_guard_fails_closed_in_children(self):
        worker = Path(__file__).parents[1] / "src/repoctl/intelligence/worker.py"
        code = (
            "import runpy,socket,subprocess,sys; "
            f"guard=runpy.run_path({str(worker)!r}); guard['deny_network'](); "
            "exec('try:\\n socket.socket()\\nexcept PermissionError:\\n print(\"denied\")'); "
            "p=subprocess.run([sys.executable,'-c','import socket; socket.socket()'],capture_output=True); "
            "assert p.returncode!=0 and b'PermissionError' in p.stderr"
        )
        result = subprocess.run([str(RUNTIME), "-I", "-c", code], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("denied", result.stdout)

    def test_runtime_environment_and_hard_timeout_and_native_pipe_cap(self):
        from repoctl.intelligence import provider as module
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            fake = directory / "worker.py"
            fake.write_text("import time; time.sleep(10)")
            with patch.object(module, "__file__", str(directory / "provider.py")):
                started = time.monotonic()
                with self.assertRaisesRegex(IntelligenceError, "timed out"):
                    GraphifyProvider()._run({}, directory / "run", .15)
                self.assertLess(time.monotonic() - started, 2)
                fake.write_text("import sys; sys.stdout.write('x'*3000000)")
                with self.assertRaisesRegex(IntelligenceError, "pipe limit"):
                    GraphifyProvider()._run({}, directory / "run", 5)
                fake.write_text("import json,os; print(json.dumps(dict(os.environ)))")
                with patch.dict(os.environ, {"SECRET_PROVIDER_KEY": "not-in-child"}):
                    environment = json.loads(GraphifyProvider()._run({}, directory / "run", 5))
                self.assertNotIn("SECRET_PROVIDER_KEY", environment)
                self.assertEqual(environment["GRAPHIFY_NO_AUTO_REFRESH"], "1")
                self.assertNotEqual(environment["HOME"], str(Path.home()))


if __name__ == "__main__":
    unittest.main()
