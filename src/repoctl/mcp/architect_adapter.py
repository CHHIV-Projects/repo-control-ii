"""Read-only Architect MCP adapter for Repo Control II.

Exposes a strictly bounded, GET-only set of Model Context Protocol (MCP) tools
backed by the local Repo Control Architect API (http://127.0.0.1:8765/api/architect/v1/*).
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
import logging
import sys
import time
from typing import Any, BinaryIO, TextIO
import urllib.error
import urllib.parse
import urllib.request

ADAPTER_VERSION = "0.1.0"
DEFAULT_REPO_CONTROL_URL = "http://127.0.0.1:8765"
DEFAULT_TIMEOUT_SECONDS = 10.0

ALLOWED_DOCUMENT_ROLES = frozenset(
    {"context", "architecture", "workflow", "coding_agent_rules", "parking_lot"}
)

PRECONDITION_PROPERTIES = {
    "expected_repository_key": {
        "type": "string",
        "description": "Optional repository key precondition to guard against concurrent repository changes.",
    },
    "expected_generation": {
        "type": "integer",
        "minimum": 0,
        "description": "Optional registry generation precondition to guard against concurrent registry reloads.",
    },
    "expected_milestone_id": {
        "type": "string",
        "description": "Optional milestone ID precondition to guard against concurrent milestone switches.",
    },
}

TOOL_INVENTORY: list[dict[str, Any]] = [
    {
        "name": "repo_status",
        "description": "Get read-only overview of the active repository selection and current milestone status.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "expected_repository_key": PRECONDITION_PROPERTIES["expected_repository_key"],
                "expected_generation": PRECONDITION_PROPERTIES["expected_generation"],
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "repo_milestone",
        "description": "Get detailed metadata for the current milestone including prompt, workflow sidecar, and closeout state.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "expected_repository_key": PRECONDITION_PROPERTIES["expected_repository_key"],
                "expected_generation": PRECONDITION_PROPERTIES["expected_generation"],
                "expected_milestone_id": PRECONDITION_PROPERTIES["expected_milestone_id"],
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "repo_prompt",
        "description": "Get the full content and metadata of the active milestone prompt document.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "expected_repository_key": PRECONDITION_PROPERTIES["expected_repository_key"],
                "expected_generation": PRECONDITION_PROPERTIES["expected_generation"],
                "expected_milestone_id": PRECONDITION_PROPERTIES["expected_milestone_id"],
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "repo_workflow",
        "description": "Get the current milestone workflow lifecycle state, phase, open conditions, and recent bounded events.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "expected_repository_key": PRECONDITION_PROPERTIES["expected_repository_key"],
                "expected_generation": PRECONDITION_PROPERTIES["expected_generation"],
                "expected_milestone_id": PRECONDITION_PROPERTIES["expected_milestone_id"],
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "repo_closeout",
        "description": "Get the milestone closeout report content or status for review and acceptance.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "expected_repository_key": PRECONDITION_PROPERTIES["expected_repository_key"],
                "expected_generation": PRECONDITION_PROPERTIES["expected_generation"],
                "expected_milestone_id": PRECONDITION_PROPERTIES["expected_milestone_id"],
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "repo_git",
        "description": "Get deterministic read-only Git status, head commit, branch name, and uncommitted changes.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "expected_repository_key": PRECONDITION_PROPERTIES["expected_repository_key"],
                "expected_generation": PRECONDITION_PROPERTIES["expected_generation"],
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "repo_evidence",
        "description": "Get recorded evidence, test execution outcomes, and binding verification items for the milestone.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "expected_repository_key": PRECONDITION_PROPERTIES["expected_repository_key"],
                "expected_generation": PRECONDITION_PROPERTIES["expected_generation"],
                "expected_milestone_id": PRECONDITION_PROPERTIES["expected_milestone_id"],
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "repo_documents",
        "description": "List registered repository governing documents (context, architecture, workflow, rules, parking lot).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "expected_repository_key": PRECONDITION_PROPERTIES["expected_repository_key"],
                "expected_generation": PRECONDITION_PROPERTIES["expected_generation"],
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "repo_document",
        "description": "Get content of one specific governing document by role (context, architecture, workflow, coding_agent_rules, parking_lot).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "role": {
                    "type": "string",
                    "enum": sorted(ALLOWED_DOCUMENT_ROLES),
                    "description": "Governing document role to read.",
                },
                "expected_repository_key": PRECONDITION_PROPERTIES["expected_repository_key"],
                "expected_generation": PRECONDITION_PROPERTIES["expected_generation"],
            },
            "required": ["role"],
            "additionalProperties": False,
        },
    },
    {
        "name": "repo_review",
        "description": "Get comprehensive bounded review summary of the active milestone, combining status, git, and lifecycle.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "expected_repository_key": PRECONDITION_PROPERTIES["expected_repository_key"],
                "expected_generation": PRECONDITION_PROPERTIES["expected_generation"],
                "expected_milestone_id": PRECONDITION_PROPERTIES["expected_milestone_id"],
            },
            "additionalProperties": False,
        },
    },
]

TOOL_MAP: dict[str, dict[str, Any]] = {tool["name"]: tool for tool in TOOL_INVENTORY}


class AdapterValidationError(ValueError):
    """Raised when incoming tool call arguments fail validation."""


def validate_tool_call(tool_name: str, arguments: dict[str, Any]) -> tuple[str, dict[str, str]]:
    """Validate tool call arguments strictly against allowlists and return endpoint path + query params."""
    if tool_name not in TOOL_MAP:
        raise AdapterValidationError(f"Unknown tool: {tool_name!r}")

    tool_def = TOOL_MAP[tool_name]
    allowed_props = set(tool_def["inputSchema"].get("properties", {}).keys())
    provided_keys = set(arguments.keys())
    unknown_keys = provided_keys - allowed_props
    if unknown_keys:
        raise AdapterValidationError(f"Unknown arguments for {tool_name}: {sorted(unknown_keys)}")

    query_params: dict[str, str] = {}
    if "expected_repository_key" in arguments:
        val = arguments["expected_repository_key"]
        if not isinstance(val, str) or not val or len(val) > 64:
            raise AdapterValidationError("expected_repository_key must be a non-empty string <= 64 characters")
        query_params["expected_repository_key"] = val

    if "expected_generation" in arguments:
        val = arguments["expected_generation"]
        if not isinstance(val, int) or isinstance(val, bool) or val < 0:
            raise AdapterValidationError("expected_generation must be a non-negative integer")
        query_params["expected_generation"] = str(val)

    if "expected_milestone_id" in arguments:
        val = arguments["expected_milestone_id"]
        if not isinstance(val, str) or not val or len(val) > 32:
            raise AdapterValidationError("expected_milestone_id must be a non-empty string <= 32 characters")
        query_params["expected_milestone_id"] = val

    if tool_name == "repo_status":
        return "/api/architect/v1/status", query_params
    elif tool_name == "repo_milestone":
        return "/api/architect/v1/milestone", query_params
    elif tool_name == "repo_prompt":
        return "/api/architect/v1/prompt", query_params
    elif tool_name == "repo_workflow":
        return "/api/architect/v1/workflow", query_params
    elif tool_name == "repo_closeout":
        return "/api/architect/v1/closeout", query_params
    elif tool_name == "repo_git":
        return "/api/architect/v1/git", query_params
    elif tool_name == "repo_evidence":
        return "/api/architect/v1/evidence", query_params
    elif tool_name == "repo_documents":
        return "/api/architect/v1/documents", query_params
    elif tool_name == "repo_document":
        if "role" not in arguments:
            raise AdapterValidationError("Missing required argument 'role' for repo_document")
        role = arguments["role"]
        if role not in ALLOWED_DOCUMENT_ROLES:
            raise AdapterValidationError(
                f"Invalid document role {role!r}; must be one of {sorted(ALLOWED_DOCUMENT_ROLES)}"
            )
        return f"/api/architect/v1/documents/{role}", query_params
    elif tool_name == "repo_review":
        return "/api/architect/v1/review", query_params
    else:
        raise AdapterValidationError(f"Unhandled tool: {tool_name!r}")


def execute_get(
    base_url: str,
    path: str,
    query_params: dict[str, str],
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> tuple[int, dict[str, Any]]:
    """Execute fixed HTTP GET request to Repo Control loopback endpoint."""
    url = urllib.parse.urljoin(base_url, path)
    if query_params:
        url = f"{url}?{urllib.parse.urlencode(query_params)}"

    req = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": f"repo-control-architect-adapter/{ADAPTER_VERSION}"},
        method="GET",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status_code = resp.status
            body = resp.read().decode("utf-8")
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                data = {"error": {"code": "invalid_json_response", "message": "Upstream returned non-JSON body"}}
            return status_code, data
    except urllib.error.HTTPError as err:
        status_code = err.code
        try:
            err_body = err.read().decode("utf-8")
            data = json.loads(err_body)
        except Exception:
            data = {"error": {"code": "upstream_http_error", "message": f"HTTP {status_code}: {err.reason}"}}
        return status_code, data
    except urllib.error.URLError as err:
        return 503, {
            "error": {
                "code": "upstream_unavailable",
                "message": f"Failed to connect to Repo Control at {base_url}: {err.reason}",
            }
        }
    except TimeoutError:
        return 504, {
            "error": {
                "code": "upstream_timeout",
                "message": f"Timed out connecting to Repo Control at {base_url}",
            }
        }
    except Exception as exc:
        return 500, {
            "error": {
                "code": "internal_adapter_error",
                "message": f"Unexpected error executing GET: {exc}",
            }
        }


def log_audit_event(
    tool_name: str,
    status_code: int,
    duration_ms: float,
    repo_key: str | None = None,
    milestone_id: str | None = None,
    err_stream: TextIO = sys.stderr,
) -> None:
    """Emit content-free metadata-only audit record to stderr."""
    timestamp = datetime.now(UTC).isoformat()
    record = {
        "timestamp": timestamp,
        "transport": "mcp_stdio",
        "tool": tool_name,
        "status_code": status_code,
        "duration_ms": round(duration_ms, 2),
        "repository_key": repo_key or "none",
        "milestone_id": milestone_id or "none",
    }
    err_stream.write(f"AUDIT {json.dumps(record, separators=(',', ':'))}\n")
    err_stream.flush()


def handle_rpc_request(
    req: dict[str, Any],
    base_url: str,
    err_stream: TextIO = sys.stderr,
) -> dict[str, Any] | None:
    """Handle a single MCP JSON-RPC 2.0 request."""
    req_id = req.get("id")
    method = req.get("method")
    params = req.get("params", {})

    if method == "initialize":
        protocol_version = params.get("protocolVersion", "2024-11-05")
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": protocol_version,
                "capabilities": {
                    "tools": {},
                },
                "serverInfo": {
                    "name": "repo-control-architect-adapter",
                    "version": ADAPTER_VERSION,
                },
            },
        }
    elif method == "notifications/initialized":
        return None
    elif method == "ping":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {},
        }
    elif method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "tools": TOOL_INVENTORY,
            },
        }
    elif method == "tools/call":
        tool_name = params.get("name")
        arguments = params.get("arguments", {})
        start_time = time.perf_counter()

        if not isinstance(arguments, dict):
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {
                    "code": -32602,
                    "message": "Tool arguments must be a JSON object",
                },
            }

        try:
            path, query_params = validate_tool_call(tool_name, arguments)
        except AdapterValidationError as err:
            duration_ms = (time.perf_counter() - start_time) * 1000.0
            log_audit_event(
                tool_name=str(tool_name),
                status_code=400,
                duration_ms=duration_ms,
                err_stream=err_stream,
            )
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {
                                    "error": {
                                        "code": "validation_error",
                                        "message": str(err),
                                    }
                                },
                                indent=2,
                            ),
                        }
                    ],
                    "isError": True,
                },
            }

        status_code, response_data = execute_get(base_url, path, query_params)
        duration_ms = (time.perf_counter() - start_time) * 1000.0

        repo_key = None
        milestone_id = None
        if isinstance(response_data, dict):
            repo_data = response_data.get("repository")
            if isinstance(repo_data, dict):
                repo_key = repo_data.get("key")
            source_scope = response_data.get("source_scope")
            if isinstance(source_scope, dict):
                milestone_id = source_scope.get("milestone_id")

        log_audit_event(
            tool_name=tool_name,
            status_code=status_code,
            duration_ms=duration_ms,
            repo_key=repo_key,
            milestone_id=milestone_id,
            err_stream=err_stream,
        )

        is_error = status_code >= 400
        content_text = json.dumps(response_data, indent=2)

        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": content_text,
                    }
                ],
                "isError": is_error,
            },
        }
    else:
        if req_id is not None:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {
                    "code": -32601,
                    "message": f"Method not supported: {method!r}",
                },
            }
        return None


def serve_stdio(
    repo_control_url: str = DEFAULT_REPO_CONTROL_URL,
    in_stream: TextIO = sys.stdin,
    out_stream: TextIO = sys.stdout,
    err_stream: TextIO = sys.stderr,
) -> None:
    """Serve stdio MCP protocol until EOF."""
    err_stream.write(
        f"Repo Control Architect MCP adapter {ADAPTER_VERSION} started. Target: {repo_control_url}\n"
    )
    err_stream.flush()

    for line in in_stream:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as err:
            err_resp = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {
                    "code": -32700,
                    "message": f"Parse error: {err}",
                },
            }
            out_stream.write(json.dumps(err_resp) + "\n")
            out_stream.flush()
            continue

        resp = handle_rpc_request(req, base_url=repo_control_url, err_stream=err_stream)
        if resp is not None:
            out_stream.write(json.dumps(resp) + "\n")
            out_stream.flush()


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Repo Control Architect MCP adapter")
    parser.add_argument(
        "--repo-control-url",
        default=DEFAULT_REPO_CONTROL_URL,
        help="Base URL for Repo Control web server (default: http://127.0.0.1:8765)",
    )
    args = parser.parse_args(argv)
    serve_stdio(repo_control_url=args.repo_control_url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
