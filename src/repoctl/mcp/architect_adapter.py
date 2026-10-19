"""Bounded Architect MCP adapter for Repo Control II.

Exposes read-only evidence tools and narrowly typed proposal submissions through
the local Repo Control Architect API (http://127.0.0.1:8765/api/architect/v1/*).
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
import logging
import re
import sys
import time
from typing import Any, BinaryIO, TextIO
import urllib.error
import urllib.parse
import urllib.request

from ..intelligence.service import IntelligenceError, QUERY_PROPERTIES, encoded, normalize

ADAPTER_VERSION = "0.1.0"
DEFAULT_REPO_CONTROL_URL = "http://127.0.0.1:8765"
DEFAULT_TIMEOUT_SECONDS = 10.0
MAX_DISPATCH_BODY_BYTES = 64 * 1024
MAX_DISPATCH_READ_LIMIT = 5
_DISPATCH_ID_PATTERN = r"^[0-9a-f]{32}$"
_MILESTONE_ID_PATTERN = r"^(?:M[0-9]{3}(?:\.[0-9]{3})+|R[0-9]{3}\.[0-9]{2})$"

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
        "name": "repo_read_document",
        "description": (
            "Read one explicitly selected current working-tree documentation file from an allowlisted path. "
            "This cannot browse directories, read source code, or retrieve historical artifacts."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 512,
                    "description": "Repository-relative path to one allowlisted Markdown/text document.",
                },
                "expected_repository_key": PRECONDITION_PROPERTIES["expected_repository_key"],
                "expected_generation": PRECONDITION_PROPERTIES["expected_generation"],
            },
            "required": ["path", "expected_repository_key", "expected_generation"],
            "additionalProperties": False,
        },
    },
    {
        "name": "repo_history_search",
        "description": (
            "Discover bounded prior milestone and docs/history artifacts in the selected repository. "
            "Results are historical planning evidence, never current authority."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 256,
                    "description": "Optional lexical query; all query terms must match.",
                },
                "milestone_id": {
                    "type": "string",
                    "pattern": r"^(?:M[0-9]{3}(?:\.[0-9]{3})+|R[0-9]{3}\.[0-9]{2}|[0-9]{3}(?:\.[0-9]+)*)$",
                },
                "artifact_type": {
                    "type": "string",
                    "enum": [
                        "milestone_prompt",
                        "milestone_workflow",
                        "milestone_closeout",
                        "archived_context",
                        "archived_planning",
                        "historical_document",
                    ],
                },
                "category": {"type": "string", "enum": ["milestone", "archive"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                "expected_repository_key": PRECONDITION_PROPERTIES["expected_repository_key"],
                "expected_generation": PRECONDITION_PROPERTIES["expected_generation"],
            },
            "required": ["expected_repository_key", "expected_generation"],
            "additionalProperties": False,
        },
    },
    {
        "name": "repo_history_document",
        "description": (
            "Read one explicitly selected prior milestone or approved docs/history artifact. "
            "Returned content is labeled historical evidence and is not current authority."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 512,
                    "description": "Exact repository-relative historical path returned by repo_history_search.",
                },
                "expected_repository_key": PRECONDITION_PROPERTIES["expected_repository_key"],
                "expected_generation": PRECONDITION_PROPERTIES["expected_generation"],
            },
            "required": ["path", "expected_repository_key", "expected_generation"],
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
    {
        "name": "repo_dispatches",
        "description": "Read bounded Architect dispatch status and content for this repository; supply dispatch_id to verify a specific submission.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "expected_repository_key": PRECONDITION_PROPERTIES["expected_repository_key"],
                "expected_generation": PRECONDITION_PROPERTIES["expected_generation"],
                "milestone_id": {
                    "type": "string",
                    "pattern": _MILESTONE_ID_PATTERN,
                    "description": "Optional canonical milestone filter.",
                },
                "status": {
                    "type": "string",
                    "enum": ["PENDING", "APPROVED", "REJECTED"],
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_DISPATCH_READ_LIMIT,
                },
                "dispatch_id": {
                    "type": "string",
                    "pattern": _DISPATCH_ID_PATTERN,
                    "description": "Optional durable ID of one submitted dispatch.",
                },
            },
            "required": ["expected_repository_key", "expected_generation"],
            "additionalProperties": False,
        },
    },
    {
        "name": "architect_submit_prompt_draft",
        "description": "Submit a bounded PROMPT proposal for Product Owner review. This does not create/select/approve/start a milestone or change Git.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "expected_repository_key": PRECONDITION_PROPERTIES["expected_repository_key"],
                "expected_generation": PRECONDITION_PROPERTIES["expected_generation"],
                "milestone_id": {"type": "string", "pattern": _MILESTONE_ID_PATTERN},
                "title": {"type": "string", "minLength": 1, "maxLength": 200},
                "body": {"type": "string", "minLength": 1, "maxLength": MAX_DISPATCH_BODY_BYTES},
            },
            "required": [
                "expected_repository_key",
                "expected_generation",
                "milestone_id",
                "title",
                "body",
            ],
            "additionalProperties": False,
        },
    },
    {
        "name": "architect_submit_instruction_draft",
        "description": "Submit an INSTRUCTION proposal for the currently active milestone for Product Owner review. It cannot change lifecycle, files, or Git.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "expected_repository_key": PRECONDITION_PROPERTIES["expected_repository_key"],
                "expected_generation": PRECONDITION_PROPERTIES["expected_generation"],
                "milestone_id": {"type": "string", "pattern": _MILESTONE_ID_PATTERN},
                "body": {"type": "string", "minLength": 1, "maxLength": MAX_DISPATCH_BODY_BYTES},
            },
            "required": ["expected_repository_key", "expected_generation", "milestone_id", "body"],
            "additionalProperties": False,
        },
    },
    {
        "name": "architect_submit_response_draft",
        "description": "Submit a RESPONSE proposal, optionally related to a workflow event, for Product Owner review. It does not record a workflow answer.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "expected_repository_key": PRECONDITION_PROPERTIES["expected_repository_key"],
                "expected_generation": PRECONDITION_PROPERTIES["expected_generation"],
                "milestone_id": {"type": "string", "pattern": _MILESTONE_ID_PATTERN},
                "body": {"type": "string", "minLength": 1, "maxLength": MAX_DISPATCH_BODY_BYTES},
                "related_event_id": {"type": "string", "pattern": _DISPATCH_ID_PATTERN},
            },
            "required": ["expected_repository_key", "expected_generation", "milestone_id", "body"],
            "additionalProperties": False,
        },
    },
]

TOOL_INVENTORY.append({
    "name": "repo_intelligence",
    "description": (
        "Bounded static repository structure: stats/freshness, symbol lookup, callers/callees, "
        "filtered neighbors/imports/references, likely tests/reverse impact, lexical context and generic "
        "relationship paths (NOT call chains). No build, arbitrary paths/tools, Git or lifecycle authority."
    ),
    "inputSchema": {
        "type": "object", "additionalProperties": False,
        "required": ["expected_repository_key", "expected_generation"],
        "properties": {
            **QUERY_PROPERTIES,
            "expected_repository_key": PRECONDITION_PROPERTIES["expected_repository_key"],
            "expected_generation": PRECONDITION_PROPERTIES["expected_generation"],
        },
    },
})

TOOL_MAP: dict[str, dict[str, Any]] = {tool["name"]: tool for tool in TOOL_INVENTORY}
SUBMISSION_TOOL_TYPES = {
    "architect_submit_prompt_draft": "PROMPT",
    "architect_submit_instruction_draft": "INSTRUCTION",
    "architect_submit_response_draft": "RESPONSE",
}


class AdapterValidationError(ValueError):
    """Raised when incoming tool call arguments fail validation."""


def validate_tool_call(tool_name: str, arguments: dict[str, Any]) -> tuple[str, dict[str, str]]:
    """Validate tool call arguments strictly against allowlists and return endpoint path + query params."""
    if not isinstance(tool_name, str) or tool_name not in TOOL_MAP:
        raise AdapterValidationError(f"Unknown tool: {tool_name!r}")

    tool_def = TOOL_MAP[tool_name]
    allowed_props = set(tool_def["inputSchema"].get("properties", {}).keys())
    provided_keys = set(arguments.keys())
    unknown_keys = provided_keys - allowed_props
    if unknown_keys:
        raise AdapterValidationError(f"Unknown arguments for {tool_name}: {sorted(unknown_keys)}")

    if tool_name == "repo_intelligence":
        for key in ("expected_repository_key", "expected_generation"):
            if key not in arguments:
                raise AdapterValidationError(f"Missing required argument {key!r}")
        try:
            normalized = normalize({k: v for k, v in arguments.items() if k in QUERY_PROPERTIES})
        except IntelligenceError as exc:
            raise AdapterValidationError(str(exc)) from exc
        key, generation = arguments["expected_repository_key"], arguments["expected_generation"]
        if not isinstance(key, str) or not 1 <= len(key) <= 64:
            raise AdapterValidationError("Invalid repository key.")
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
            raise AdapterValidationError("Invalid registry generation.")
        return "/api/architect/v1/intelligence", {
            "expected_repository_key": key, "expected_generation": str(generation),
            "request": json.dumps(normalized, separators=(",", ":")),
        }

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

    if tool_name == "repo_read_document":
        for key in ("path", "expected_repository_key", "expected_generation"):
            if key not in arguments:
                raise AdapterValidationError(f"Missing required argument {key!r} for repo_read_document")
        path = arguments["path"]
        if not isinstance(path, str) or not 1 <= len(path) <= 512:
            raise AdapterValidationError("path must be a non-empty repository-relative path <= 512 characters")
        return "/api/architect/v1/repository-document", {
            "path": path,
            "expected_repository_key": query_params["expected_repository_key"],
            "expected_generation": query_params["expected_generation"],
        }
    elif tool_name == "repo_history_search":
        for key in ("expected_repository_key", "expected_generation"):
            if key not in arguments:
                raise AdapterValidationError(f"Missing required argument {key!r} for repo_history_search")
        history_params: dict[str, str] = {
            "expected_repository_key": query_params["expected_repository_key"],
            "expected_generation": query_params["expected_generation"],
        }
        if "query" in arguments:
            value = arguments["query"]
            if not isinstance(value, str) or not 1 <= len(value) <= 256:
                raise AdapterValidationError("query must be a non-empty string <= 256 characters")
            history_params["query"] = value
        if "milestone_id" in arguments:
            value = arguments["milestone_id"]
            if not isinstance(value, str) or not re.fullmatch(
                r"(?:M[0-9]{3}(?:\.[0-9]{3})+|R[0-9]{3}\.[0-9]{2}|[0-9]{3}(?:\.[0-9]+)*)",
                value,
            ):
                raise AdapterValidationError("milestone_id must use a supported historical milestone form")
            history_params["milestone_id"] = value
        if "artifact_type" in arguments:
            value = arguments["artifact_type"]
            if not isinstance(value, str) or value not in {
                "milestone_prompt",
                "milestone_workflow",
                "milestone_closeout",
                "archived_context",
                "archived_planning",
                "historical_document",
            }:
                raise AdapterValidationError("artifact_type is not a supported historical artifact type")
            history_params["artifact_type"] = value
        if "category" in arguments:
            value = arguments["category"]
            if not isinstance(value, str) or value not in {"milestone", "archive"}:
                raise AdapterValidationError("category must be 'milestone' or 'archive'")
            history_params["category"] = value
        if "limit" in arguments:
            value = arguments["limit"]
            if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 20:
                raise AdapterValidationError("limit must be an integer from 1 to 20")
            history_params["limit"] = str(value)
        return "/api/architect/v1/history/search", history_params
    elif tool_name == "repo_history_document":
        for key in ("path", "expected_repository_key", "expected_generation"):
            if key not in arguments:
                raise AdapterValidationError(f"Missing required argument {key!r} for repo_history_document")
        path = arguments["path"]
        if not isinstance(path, str) or not 1 <= len(path) <= 512:
            raise AdapterValidationError("path must be a non-empty repository-relative path <= 512 characters")
        return "/api/architect/v1/history/document", {
            "path": path,
            "expected_repository_key": query_params["expected_repository_key"],
            "expected_generation": query_params["expected_generation"],
        }
    elif tool_name == "repo_dispatches":
        if "milestone_id" in arguments:
            value = arguments["milestone_id"]
            if not isinstance(value, str) or not re.fullmatch(_MILESTONE_ID_PATTERN[1:-1], value):
                raise AdapterValidationError("milestone_id must use a supported canonical milestone form")
            query_params["milestone_id"] = value
        if "status" in arguments:
            value = arguments["status"]
            if not isinstance(value, str) or value not in {"PENDING", "APPROVED", "REJECTED"}:
                raise AdapterValidationError("status must be PENDING, APPROVED, or REJECTED")
            query_params["status"] = value
        if "limit" in arguments:
            value = arguments["limit"]
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or not 1 <= value <= MAX_DISPATCH_READ_LIMIT
            ):
                raise AdapterValidationError(f"limit must be an integer from 1 to {MAX_DISPATCH_READ_LIMIT}")
            query_params["limit"] = str(value)
        if "dispatch_id" in arguments:
            value = arguments["dispatch_id"]
            if not isinstance(value, str) or not re.fullmatch(_DISPATCH_ID_PATTERN[1:-1], value):
                raise AdapterValidationError("dispatch_id must be a 32-character lowercase hexadecimal ID")
            query_params["dispatch_id"] = value
        for name in ("expected_repository_key", "expected_generation"):
            if name not in arguments:
                raise AdapterValidationError(f"Missing required argument {name!r} for repo_dispatches")
        return "/api/architect/v1/dispatches", query_params

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
        if not isinstance(role, str) or role not in ALLOWED_DOCUMENT_ROLES:
            raise AdapterValidationError(
                f"Invalid document role {role!r}; must be one of {sorted(ALLOWED_DOCUMENT_ROLES)}"
            )
        return f"/api/architect/v1/documents/{role}", query_params
    elif tool_name == "repo_review":
        return "/api/architect/v1/review", query_params
    else:
        raise AdapterValidationError(f"Unhandled tool: {tool_name!r}")


def validate_submission_call(
    tool_name: str,
    arguments: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    """Validate a typed Architect proposal and return its fixed POST endpoint and payload."""
    if not isinstance(tool_name, str):
        raise AdapterValidationError(f"Unknown submission tool: {tool_name!r}")
    dispatch_type = SUBMISSION_TOOL_TYPES.get(tool_name)
    if dispatch_type is None:
        raise AdapterValidationError(f"Unknown submission tool: {tool_name!r}")
    tool_def = TOOL_MAP[tool_name]
    allowed = set(tool_def["inputSchema"]["properties"])
    required = set(tool_def["inputSchema"].get("required", []))
    unknown = set(arguments) - allowed
    missing = required - set(arguments)
    if unknown:
        raise AdapterValidationError(f"Unknown arguments for {tool_name}: {sorted(unknown)}")
    if missing:
        raise AdapterValidationError(f"Missing required arguments for {tool_name}: {sorted(missing)}")
    repository_key = arguments["expected_repository_key"]
    if not isinstance(repository_key, str) or not repository_key or len(repository_key) > 64:
        raise AdapterValidationError("expected_repository_key must be a non-empty string <= 64 characters")
    generation = arguments["expected_generation"]
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
        raise AdapterValidationError("expected_generation must be a non-negative integer")
    milestone_id = arguments["milestone_id"]
    if not isinstance(milestone_id, str) or not re.fullmatch(_MILESTONE_ID_PATTERN[1:-1], milestone_id):
        raise AdapterValidationError("milestone_id must use a supported canonical milestone form")
    body = arguments["body"]
    if (
        not isinstance(body, str)
        or not body.strip()
        or "\x00" in body
        or any(ord(character) < 32 and character not in "\t\n\r" for character in body)
    ):
        raise AdapterValidationError("body must be non-empty text without unsupported control characters")
    try:
        body_size = len(body.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise AdapterValidationError("body contains invalid Unicode") from exc
    if body_size > MAX_DISPATCH_BODY_BYTES:
        raise AdapterValidationError(f"body exceeds the {MAX_DISPATCH_BODY_BYTES}-byte limit")
    payload: dict[str, Any] = {
        "expected_repository_key": repository_key,
        "expected_generation": generation,
        "milestone_id": milestone_id,
        "body": body,
    }
    if dispatch_type == "PROMPT":
        title = arguments.get("title")
        if not isinstance(title, str) or not title.strip() or len(title) > 200:
            raise AdapterValidationError("title must be non-empty text of at most 200 characters")
        try:
            title.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise AdapterValidationError("title contains invalid Unicode") from exc
        payload["title"] = title
    if dispatch_type == "RESPONSE" and "related_event_id" in arguments:
        related_event_id = arguments["related_event_id"]
        if not isinstance(related_event_id, str) or not re.fullmatch(_DISPATCH_ID_PATTERN[1:-1], related_event_id):
            raise AdapterValidationError("related_event_id must be a 32-character lowercase hexadecimal ID")
        payload["related_event_id"] = related_event_id
    endpoint = {
        "PROMPT": "/api/architect/v1/dispatches/prompt",
        "INSTRUCTION": "/api/architect/v1/dispatches/instruction",
        "RESPONSE": "/api/architect/v1/dispatches/response",
    }[dispatch_type]
    return endpoint, payload


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
            if path == "/api/architect/v1/intelligence":
                raw = resp.read(65537)
                if len(raw) > 65536:
                    return 502, {"error": {"code": "intelligence_response_oversized", "message": "Upstream exceeded the intelligence byte cap."}}
                body = raw.decode("utf-8")
            else:
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


def execute_post(
    base_url: str,
    path: str,
    payload: dict[str, Any],
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> tuple[int, dict[str, Any]]:
    """Execute a fixed, typed JSON POST to a proposal-submission endpoint."""
    url = urllib.parse.urljoin(base_url, path)
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": f"repo-control-architect-adapter/{ADAPTER_VERSION}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(1_048_577)
            if len(body) > 1_048_576:
                return 502, {
                    "error": {
                        "code": "upstream_response_oversized",
                        "message": "Repo Control response exceeded its limit.",
                    }
                }
            try:
                return resp.status, json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return 502, {
                    "error": {
                        "code": "invalid_json_response",
                        "message": "Upstream returned non-JSON body",
                    }
                }
    except urllib.error.HTTPError as err:
        try:
            body = err.read(1_048_577)
            if len(body) > 1_048_576:
                return err.code, {
                    "error": {
                        "code": "upstream_response_oversized",
                        "message": "Repo Control error response exceeded its limit.",
                    }
                }
            return err.code, json.loads(body.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return err.code, {
                "error": {
                    "code": "upstream_http_error",
                    "message": f"HTTP {err.code}: {err.reason}",
                }
            }
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
    except OSError as exc:
        return 500, {
            "error": {
                "code": "internal_adapter_error",
                "message": f"Could not submit proposal: {exc}",
            }
        }
    except Exception as exc:
        return 500, {
            "error": {
                "code": "internal_adapter_error",
                "message": f"Unexpected error submitting proposal: {exc}",
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
            if tool_name in SUBMISSION_TOOL_TYPES:
                path, submission_payload = validate_submission_call(tool_name, arguments)
                query_params = {}
            else:
                path, query_params = validate_tool_call(tool_name, arguments)
                submission_payload = None
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

        if submission_payload is None:
            status_code, response_data = execute_get(base_url, path, query_params)
        else:
            status_code, response_data = execute_post(base_url, path, submission_payload)
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
            if milestone_id is None and isinstance(arguments.get("milestone_id"), str):
                milestone_id = arguments["milestone_id"]

        log_audit_event(
            tool_name=tool_name,
            status_code=status_code,
            duration_ms=duration_ms,
            repo_key=repo_key,
            milestone_id=milestone_id,
            err_stream=err_stream,
        )

        is_error = status_code >= 400
        if tool_name == "repo_intelligence":
            is_error = is_error or response_data.get("outcome") not in {"OK", "TRUNCATED"}
            content_text = encoded(response_data).decode("ascii")
        else:
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
