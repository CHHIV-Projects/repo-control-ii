from __future__ import annotations

import http.client
import json
import os
import re
from typing import Any
from urllib.parse import urlsplit

from .context_verification import VERIFICATION_EVENT_TYPE, VERIFICATION_RECORD_SCHEMA_VERSION
from .lifecycle import WORKFLOW_EVENT_SCHEMA_VERSION, WORKFLOW_LEDGER_SCHEMA_VERSION


DEFAULT_RUNTIME_URL = "http://127.0.0.1:8765"
RUNTIME_TIMEOUT_SECONDS = 1.5
MAX_RUNTIME_RESPONSE_BYTES = 8_192
_INSTANCE_ID_RE = re.compile(r"^[a-f0-9]{32}$")
PRODUCER_CONTRACT_ID = "repoctl-workflow-event-writer:v1"
EVENT_CAPABILITY_REQUIREMENTS = {
    VERIFICATION_EVENT_TYPE: {
        "context_verification_record_schema_version": VERIFICATION_RECORD_SCHEMA_VERSION,
    },
}


class RuntimeCompatibilityError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def require_event_runtime_compatibility(
    event_type: str,
    runtime_url: str | None = None,
) -> dict[str, Any]:
    event_requirements = EVENT_CAPABILITY_REQUIREMENTS.get(event_type)
    if event_requirements is None:
        raise RuntimeCompatibilityError(
            "runtime_contract_missing",
            f"No runtime compatibility contract is registered for event {event_type!r}. "
            "No workflow event was written; register an explicit event capability requirement before adding it.",
        )
    configured_url = runtime_url or os.environ.get("REPO_CONTROL_RUNTIME_URL", DEFAULT_RUNTIME_URL)
    if not isinstance(configured_url, str):
        raise RuntimeCompatibilityError(
            "runtime_capability_unknown",
            "The runtime URL must be an unauthenticated loopback HTTP origin. No workflow event was written.",
        )
    try:
        parsed_url = urlsplit(configured_url)
        port = parsed_url.port or 80
    except ValueError as exc:
        raise RuntimeCompatibilityError(
            "runtime_capability_unknown",
            "The runtime URL is invalid. No workflow event was written.",
        ) from exc
    if (
        parsed_url.scheme != "http"
        or parsed_url.hostname not in {"127.0.0.1", "localhost"}
        or parsed_url.username is not None
        or parsed_url.password is not None
        or parsed_url.path not in {"", "/"}
        or parsed_url.query
        or parsed_url.fragment
    ):
        raise RuntimeCompatibilityError(
            "runtime_capability_unknown",
            "The runtime URL must be an unauthenticated loopback HTTP origin, for example "
            "'http://127.0.0.1:8765'. No workflow event was written.",
        )
    host = parsed_url.hostname
    connection = http.client.HTTPConnection(
        host,
        port,
        timeout=RUNTIME_TIMEOUT_SECONDS,
    )
    try:
        connection.request("GET", "/healthz", headers={"Accept": "application/json"})
        response = connection.getresponse()
        body = response.read(MAX_RUNTIME_RESPONSE_BYTES + 1)
    except (OSError, http.client.HTTPException, TimeoutError) as exc:
        raise RuntimeCompatibilityError(
            "runtime_unavailable",
            "Cannot verify the running Repo Control reader at "
            f"http://{host}:{port}/healthz: {exc}. "
            f"No {event_type} event was written. Ask the Product Owner to inspect and deliberately "
            "reconcile the service runtime; the Coder will not restart it.",
        ) from exc
    finally:
        connection.close()

    if response.status != 200 or len(body) > MAX_RUNTIME_RESPONSE_BYTES:
        raise RuntimeCompatibilityError(
            "runtime_upgrade_required",
            "The running Repo Control service did not return a bounded successful capability response "
            f"(HTTP {response.status}, {len(body)} bytes). No {event_type} event was written. "
            "Ask the Product Owner to inspect the deployed runtime.",
        )
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeCompatibilityError(
            "runtime_capability_unknown",
            "The running Repo Control service returned malformed runtime capabilities. "
            f"No {event_type} event was written; ask the Product Owner to inspect the deployed runtime.",
        ) from exc

    runtime = payload.get("runtime_capabilities") if isinstance(payload, dict) else None
    workflow = runtime.get("workflow") if isinstance(runtime, dict) else None
    if (
        not isinstance(payload, dict)
        or payload.get("service") != "repo-control"
        or not isinstance(runtime, dict)
        or type(runtime.get("capability_schema_version")) is not int
        or runtime["capability_schema_version"] != 1
        or not isinstance(runtime.get("instance_id"), str)
        or not _INSTANCE_ID_RE.fullmatch(runtime["instance_id"])
        or not isinstance(workflow, dict)
    ):
        raise RuntimeCompatibilityError(
            "runtime_capability_unknown",
            "The running Repo Control service does not publish the required runtime capability contract. "
            f"No {event_type} event was written; ask the Product Owner to deliberately deploy a "
            "compatible service.",
        )

    required = {
        "ledger_schema_version": WORKFLOW_LEDGER_SCHEMA_VERSION,
        "event_schema_version": WORKFLOW_EVENT_SCHEMA_VERSION,
        **event_requirements,
    }
    mismatches = {
        name: {"required": expected, "running": workflow.get(name)}
        for name, expected in required.items()
        if type(workflow.get(name)) is not int or workflow.get(name) != expected
    }
    event_types = workflow.get("supported_event_types")
    if (
        not isinstance(event_types, list)
        or len(event_types) > 256
        or any(not isinstance(value, str) or len(value) > 128 for value in event_types)
        or event_type not in event_types
    ):
        mismatches["supported_event_types"] = {
            "required": event_type,
            "running": event_types if isinstance(event_types, list) else None,
        }
    if mismatches:
        raise RuntimeCompatibilityError(
            "runtime_upgrade_required",
            "The running Repo Control reader is incompatible with this Coder CLI. "
            f"Producer contract {PRODUCER_CONTRACT_ID} requires {json.dumps(required, sort_keys=True)} "
            f"and event {event_type!r}; deployed runtime instance {runtime['instance_id']} reports "
            f"mismatches {json.dumps(mismatches, sort_keys=True)}. "
            f"No {event_type} event was written. Ask the Product Owner to review, deploy, and verify "
            "the compatible runtime; the Coder will not restart it.",
        )

    return {
        "status": "COMPATIBLE",
        "runtime_instance_id": runtime["instance_id"],
        "producer_contract_id": PRODUCER_CONTRACT_ID,
        "event_type": event_type,
        "required_event_capabilities": event_requirements,
        "ledger_schema_version": workflow["ledger_schema_version"],
        "event_schema_version": workflow["event_schema_version"],
        **{name: workflow[name] for name in event_requirements},
    }
