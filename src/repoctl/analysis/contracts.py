from __future__ import annotations

import hashlib
import json
import re
from typing import Any

MODEL_PROVIDER = "ollama"
MODEL_NAME = "gpt-oss:20b"
PROMPT_CONTRACT_VERSION = "repoctl-structural-analysis-v1"
PROVIDER_ATTEMPTS = 1

MAX_AI_FILE_RECORDS = 25
MAX_AI_REQUIREMENTS_RECORDS = 10
MAX_AI_SYMBOL_RECORDS = 30
MAX_AI_RELATIONSHIP_RECORDS = 50
MAX_AI_TEST_RECORDS = 20
MAX_AI_PARSE_RECORDS = 10
MAX_AI_DIAGNOSTIC_RECORDS = 10
MAX_ANALYSIS_PACKET_BYTES = 32768

MAX_REVIEW_SIGNALS = 10
MAX_REVIEW_QUESTIONS = 8

EVIDENCE_GROUP_ORDER = [
    "aggregate",
    "coverage",
    "parse_failures",
    "diagnostics",
    "file_changes",
    "requirements_changes",
    "symbol_changes",
    "relationship_changes",
    "test_reference_changes",
]

PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}

MODEL_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


def validate_model_digest(digest: str) -> bool:
    return bool(MODEL_DIGEST_RE.fullmatch(digest))


def canonical_json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def derive_packet_id(packet_without_id: dict[str, Any]) -> str:
    data = b"repoctl-analysis-packet-v1\0" + canonical_json_bytes(packet_without_id)
    return "aip--" + hashlib.sha256(data).hexdigest()[:16]


def derive_request_id(packet_id: str, provider: str, model_name: str, model_digest: str, prompt_contract_version: str) -> str:
    data = (
        b"repoctl-analysis-request-v1\0"
        + packet_id.encode("utf-8")
        + b"\0"
        + provider.encode("utf-8")
        + b"\0"
        + model_name.encode("utf-8")
        + b"\0"
        + model_digest.encode("utf-8")
        + b"\0"
        + prompt_contract_version.encode("utf-8")
        + b"\0"
    )
    return "areq--" + hashlib.sha256(data).hexdigest()[:16]


def derive_analysis_id(request_id: str, normalized_model_output: dict[str, Any]) -> str:
    data = (
        b"repoctl-analysis-v1\0"
        + request_id.encode("utf-8")
        + b"\0"
        + canonical_json_bytes(normalized_model_output)
        + b"\0"
    )
    return "ana--" + hashlib.sha256(data).hexdigest()[:16]


def has_structural_delta(aggregate_counts: dict[str, Any]) -> bool:
    files = aggregate_counts["files"]
    requirements = aggregate_counts["requirements"]
    symbols = aggregate_counts["symbols"]
    module_deps = aggregate_counts["module_dependencies"]
    imported_symbols = aggregate_counts["imported_symbols"]
    calls = aggregate_counts["calls"]
    test_refs = aggregate_counts["test_references"]
    parse_failures = aggregate_counts["parse_failures"]
    diagnostics = aggregate_counts["diagnostics"]

    deltas = [
        files["added"],
        files["removed"],
        files["content_changed"],
        requirements["added"],
        requirements["removed"],
        requirements["changed"],
        symbols["added"],
        symbols["removed"],
        symbols["source_location_changed"],
        module_deps["added"],
        module_deps["removed"],
        module_deps["location_changed"],
        imported_symbols["added"],
        imported_symbols["removed"],
        imported_symbols["location_changed"],
        calls["added"],
        calls["removed"],
        calls["location_changed"],
        test_refs["added"],
        test_refs["removed"],
        test_refs["location_changed"],
        parse_failures["introduced"],
        parse_failures["resolved"],
        diagnostics["added"],
        diagnostics["removed"],
        diagnostics["source_location_changed"],
    ]
    return any(value > 0 for value in deltas)
