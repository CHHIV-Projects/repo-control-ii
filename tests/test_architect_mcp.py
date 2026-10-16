from __future__ import annotations

import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
import urllib.error

from repoctl.mcp.architect_adapter import (
    ADAPTER_VERSION,
    ALLOWED_DOCUMENT_ROLES,
    TOOL_INVENTORY,
    TOOL_MAP,
    AdapterValidationError,
    execute_get,
    execute_post,
    handle_rpc_request,
    log_audit_event,
    serve_stdio,
    validate_tool_call,
    validate_submission_call,
)


class TestArchitectMcpInventory(unittest.TestCase):
    """Verify tool inventory, schemas, and read-only boundaries."""

    EXPECTED_TOOLS = {
        "repo_status",
        "repo_milestone",
        "repo_prompt",
        "repo_workflow",
        "repo_closeout",
        "repo_git",
        "repo_evidence",
        "repo_documents",
        "repo_document",
        "repo_review",
        "repo_dispatches",
        "architect_submit_prompt_draft",
        "architect_submit_instruction_draft",
        "architect_submit_response_draft",
        "repo_intelligence",
    }

    def test_inventory_exact_match(self):
        advertised_names = {t["name"] for t in TOOL_INVENTORY}
        self.assertEqual(advertised_names, self.EXPECTED_TOOLS)
        self.assertEqual(len(TOOL_INVENTORY), 15)

    def test_schemas_forbid_additional_properties(self):
        for tool in TOOL_INVENTORY:
            schema = tool["inputSchema"]
            self.assertEqual(schema["type"], "object")
            self.assertFalse(
                schema.get("additionalProperties", True),
                f"Tool {tool['name']} must set additionalProperties: False",
            )

    def test_document_roles_allowlist(self):
        doc_tool = TOOL_MAP["repo_document"]
        role_prop = doc_tool["inputSchema"]["properties"]["role"]
        self.assertEqual(set(role_prop["enum"]), ALLOWED_DOCUMENT_ROLES)
        self.assertEqual(
            ALLOWED_DOCUMENT_ROLES,
            {"context", "architecture", "workflow", "coding_agent_rules", "parking_lot"},
        )


class TestArchitectMcpValidation(unittest.TestCase):
    """Verify strict validation and rejection of arbitrary inputs."""

    def test_unknown_tool_rejected(self):
        with self.assertRaises(AdapterValidationError):
            validate_tool_call("arbitrary_tool", {})
        with self.assertRaises(AdapterValidationError):
            validate_tool_call([], {})

    def test_arbitrary_arguments_rejected(self):
        with self.assertRaises(AdapterValidationError) as ctx:
            validate_tool_call("repo_status", {"url": "http://malicious.com", "method": "POST"})
        self.assertIn("Unknown arguments", str(ctx.exception))

    def test_arbitrary_path_or_command_rejected(self):
        with self.assertRaises(AdapterValidationError):
            validate_tool_call("repo_git", {"command": "git commit", "path": "/etc/passwd"})

    def test_document_role_validation(self):
        path, query = validate_tool_call("repo_document", {"role": "architecture"})
        self.assertEqual(path, "/api/architect/v1/documents/architecture")
        self.assertEqual(query, {})

        with self.assertRaises(AdapterValidationError) as ctx:
            validate_tool_call("repo_document", {"role": "invalid_role"})
        self.assertIn("Invalid document role", str(ctx.exception))

        with self.assertRaises(AdapterValidationError) as ctx:
            validate_tool_call("repo_document", {})
        self.assertIn("Missing required argument 'role'", str(ctx.exception))

    def test_precondition_arguments_valid(self):
        path, query = validate_tool_call(
            "repo_review",
            {
                "expected_repository_key": "repo-control-ii",
                "expected_generation": 2,
                "expected_milestone_id": "M003.006",
            },
        )
        self.assertEqual(path, "/api/architect/v1/review")
        self.assertEqual(
            query,
            {
                "expected_repository_key": "repo-control-ii",
                "expected_generation": "2",
                "expected_milestone_id": "M003.006",
            },
        )

    def test_precondition_arguments_invalid(self):
        # negative generation
        with self.assertRaises(AdapterValidationError):
            validate_tool_call("repo_status", {"expected_generation": -1})

        # generation as boolean
        with self.assertRaises(AdapterValidationError):
            validate_tool_call("repo_status", {"expected_generation": True})

        # repository key too long
        with self.assertRaises(AdapterValidationError):
            validate_tool_call("repo_status", {"expected_repository_key": "x" * 65})

        # milestone id too long
        with self.assertRaises(AdapterValidationError):
            validate_tool_call("repo_milestone", {"expected_milestone_id": "x" * 33})


class TestArchitectMcpExecution(unittest.TestCase):
    """Verify HTTP GET execution and error handling."""

    @patch("urllib.request.urlopen")
    def test_execute_get_success(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = json.dumps({"contract_version": "1", "data": {"status": "ok"}}).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        status, data = execute_get("http://127.0.0.1:8765", "/api/architect/v1/status", {"expected_generation": "1"})
        self.assertEqual(status, 200)
        self.assertEqual(data["data"]["status"], "ok")

        # Verify request used method GET
        req = mock_urlopen.call_args[0][0]
        self.assertEqual(req.get_method(), "GET")
        self.assertTrue(req.get_full_url().endswith("/api/architect/v1/status?expected_generation=1"))

    @patch("urllib.request.urlopen")
    def test_execute_get_http_error(self, mock_urlopen):
        err_body = json.dumps({"contract_version": "1", "error": {"code": "not_found", "message": "Missing"}}).encode("utf-8")
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="http://127.0.0.1:8765/api/architect/v1/prompt",
            code=404,
            msg="Not Found",
            hdrs={},
            fp=io.BytesIO(err_body),
        )

        status, data = execute_get("http://127.0.0.1:8765", "/api/architect/v1/prompt", {})
        self.assertEqual(status, 404)
        self.assertEqual(data["error"]["code"], "not_found")

    @patch("urllib.request.urlopen")
    def test_execute_get_network_error(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")

        status, data = execute_get("http://127.0.0.1:8765", "/api/architect/v1/status", {})
        self.assertEqual(status, 503)
        self.assertEqual(data["error"]["code"], "upstream_unavailable")

    @patch("urllib.request.urlopen")
    def test_execute_get_timeout_error(self, mock_urlopen):
        mock_urlopen.side_effect = TimeoutError("timed out")

        status, data = execute_get("http://127.0.0.1:8765", "/api/architect/v1/status", {})
        self.assertEqual(status, 504)
        self.assertEqual(data["error"]["code"], "upstream_timeout")


class TestArchitectMcpRpcProtocol(unittest.TestCase):
    """Verify JSON-RPC 2.0 dispatch and audit logging."""

    def test_initialize(self):
        req = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}}
        resp = handle_rpc_request(req, "http://127.0.0.1:8765")
        self.assertEqual(resp["id"], 1)
        self.assertEqual(resp["result"]["serverInfo"]["name"], "repo-control-architect-adapter")
        self.assertEqual(resp["result"]["serverInfo"]["version"], ADAPTER_VERSION)

    def test_tools_list(self):
        req = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        resp = handle_rpc_request(req, "http://127.0.0.1:8765")
        self.assertEqual(resp["id"], 2)
        tools = resp["result"]["tools"]
        self.assertEqual(len(tools), 15)

    def test_submission_tools_are_fixed_and_typed(self):
        path, payload = validate_submission_call(
            "architect_submit_prompt_draft",
            {
                "expected_repository_key": "repo-control-ii",
                "expected_generation": 3,
                "milestone_id": "M003.012",
                "title": "Next milestone",
                "body": "# Prompt",
            },
        )
        self.assertEqual(path, "/api/architect/v1/dispatches/prompt")
        self.assertEqual(payload["title"], "Next milestone")
        self.assertEqual(payload["milestone_id"], "M003.012")
        with self.assertRaises(AdapterValidationError):
            validate_submission_call(
                "architect_submit_instruction_draft",
                {
                    "expected_repository_key": "repo-control-ii",
                    "expected_generation": 3,
                    "milestone_id": "M003.011",
                    "body": "Do this",
                    "path": "../../arbitrary",
                },
            )
        with self.assertRaises(AdapterValidationError):
            validate_submission_call([], {})

    def test_dispatch_readback_filters_are_type_checked(self):
        with self.assertRaises(AdapterValidationError):
            validate_tool_call(
                "repo_dispatches",
                {
                    "expected_repository_key": "repo-control-ii",
                    "expected_generation": 0,
                    "status": [],
                },
            )
        with self.assertRaises(AdapterValidationError):
            validate_submission_call(
                "architect_submit_response_draft",
                {
                    "expected_repository_key": "repo-control-ii",
                    "expected_generation": 3,
                    "milestone_id": "M003.011",
                    "body": "Answer",
                    "related_event_id": "not-an-event-id",
                },
            )

    @patch("urllib.request.urlopen")
    def test_execute_post_success(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.status = 201
        mock_resp.read.return_value = json.dumps(
            {"contract_version": "1", "data": {"dispatch": {"status": "PENDING"}}}
        ).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        status, data = execute_post(
            "http://127.0.0.1:8765",
            "/api/architect/v1/dispatches/instruction",
            {"milestone_id": "M003.011", "body": "Bounded"},
        )
        self.assertEqual(status, 201)
        self.assertEqual(data["data"]["dispatch"]["status"], "PENDING")
        request = mock_urlopen.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Content-type"), "application/json")

    @patch("repoctl.mcp.architect_adapter.execute_get")
    def test_tools_call_success(self, mock_exec):
        mock_exec.return_value = (
            200,
            {
                "contract_version": "1",
                "repository": {"key": "repo-control-ii"},
                "source_scope": {"milestone_id": "M003.006"},
                "data": {"status": "ok"},
            },
        )
        err_stream = io.StringIO()
        req = {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "repo_status", "arguments": {}}}
        resp = handle_rpc_request(req, "http://127.0.0.1:8765", err_stream=err_stream)

        self.assertEqual(resp["id"], 3)
        self.assertFalse(resp["result"]["isError"])
        content_text = resp["result"]["content"][0]["text"]
        self.assertIn('"status": "ok"', content_text)

        # Audit log verification
        audit_output = err_stream.getvalue()
        self.assertIn("AUDIT", audit_output)
        audit_json = json.loads(audit_output.strip().removeprefix("AUDIT "))
        self.assertEqual(audit_json["tool"], "repo_status")
        self.assertEqual(audit_json["status_code"], 200)
        self.assertEqual(audit_json["repository_key"], "repo-control-ii")
        self.assertEqual(audit_json["milestone_id"], "M003.006")
        self.assertNotIn("status", audit_json)  # Content must NOT be logged

    @patch("repoctl.mcp.architect_adapter.execute_get")
    def test_tools_call_upstream_error(self, mock_exec):
        mock_exec.return_value = (
            409,
            {
                "contract_version": "1",
                "error": {"code": "stale_repository_selection", "message": "Stale"},
            },
        )
        err_stream = io.StringIO()
        req = {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "repo_milestone", "arguments": {}},
        }
        resp = handle_rpc_request(req, "http://127.0.0.1:8765", err_stream=err_stream)

        self.assertEqual(resp["id"], 4)
        self.assertTrue(resp["result"]["isError"])
        content_text = resp["result"]["content"][0]["text"]
        self.assertIn("stale_repository_selection", content_text)

    def test_tools_call_validation_error(self):
        err_stream = io.StringIO()
        req = {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {"name": "repo_document", "arguments": {"role": "invalid_role"}},
        }
        resp = handle_rpc_request(req, "http://127.0.0.1:8765", err_stream=err_stream)

        self.assertEqual(resp["id"], 5)
        self.assertTrue(resp["result"]["isError"])
        content_text = resp["result"]["content"][0]["text"]
        self.assertIn("validation_error", content_text)

        audit_output = err_stream.getvalue()
        self.assertIn("AUDIT", audit_output)
        audit_json = json.loads(audit_output.strip().removeprefix("AUDIT "))
        self.assertEqual(audit_json["tool"], "repo_document")
        self.assertEqual(audit_json["status_code"], 400)


class TestArchitectMcpReadOnlyInvariant(unittest.TestCase):
    """Verify that calling adapter functions cannot alter Git state or files."""

    def test_read_only_invariant(self):
        # Inspect git status of current repository before test
        res_before = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        )
        head_before = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        # Execute mock calls through the adapter
        for tool_name in ["repo_status", "repo_git", "repo_documents"]:
            path, query = validate_tool_call(tool_name, {})
            self.assertTrue(path.startswith("/api/architect/v1/"))

        # Inspect git status of current repository after test
        res_after = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        )
        head_after = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        self.assertEqual(res_before.stdout, res_after.stdout)
        self.assertEqual(head_before, head_after)


if __name__ == "__main__":
    unittest.main()
