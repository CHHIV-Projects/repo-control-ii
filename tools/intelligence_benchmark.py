#!/usr/bin/env python3
"""Count and optionally record trusted-local controlled evidence benchmarks."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
import math
from pathlib import Path
import re
import sys
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from repoctl.intelligence import measurements, reference_tokens as reference_tokenizer
from repoctl.intelligence.provider import IntelligenceError

MAX_INPUT_BYTES = 2 * 1024 * 1024
MAX_EVIDENCE_ITEMS = 512
MAX_EVIDENCE_ITEM_CHARACTERS = 256 * 1024


def _text_list(value: Any, name: str) -> list[str]:
    if not isinstance(value, list) or len(value) > MAX_EVIDENCE_ITEMS:
        raise IntelligenceError(f"{name} must be a bounded list of text items.")
    if any(not isinstance(item, str) or len(item) > MAX_EVIDENCE_ITEM_CHARACTERS for item in value):
        raise IntelligenceError(f"{name} contains invalid or oversized text.")
    return value


def _canonical_json_list(value: Any, name: str) -> list[str]:
    items = _text_list(value, name)
    canonical_items = []
    for item in items:
        try:
            decoded = json.loads(
                item,
                object_pairs_hook=_unique_object,
                parse_constant=_reject_json_constant,
            )
        except (json.JSONDecodeError, ValueError, RecursionError) as exc:
            raise IntelligenceError(f"{name} items must be valid normalized JSON evidence.") from exc
        normalized = json.dumps(decoded, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        if normalized != item:
            raise IntelligenceError(f"{name} items must use normalized compact JSON serialization.")
        canonical_items.append(item)
    return canonical_items


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON constant: {value}")


def _integer(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise IntelligenceError(f"{name} must be a non-negative integer.")
    return value


def _arm_metrics(raw: Any, encoding: Any) -> dict[str, Any]:
    required = {
        "tool_evidence", "source_windows_read", "source_windows_located_only",
        "files_inspected", "interactions", "elapsed_ms",
    }
    if not isinstance(raw, dict) or set(raw) != required:
        raise IntelligenceError("Each benchmark arm requires tool/source evidence and bounded activity metrics.")
    tool_items = _canonical_json_list(raw["tool_evidence"], "tool_evidence")
    source_items = _text_list(raw["source_windows_read"], "source_windows_read")
    located_only = _integer(raw["source_windows_located_only"], "source_windows_located_only")
    files_inspected = _integer(raw["files_inspected"], "files_inspected")
    interactions = _integer(raw["interactions"], "interactions")
    elapsed_ms = raw["elapsed_ms"]
    if isinstance(elapsed_ms, bool) or not isinstance(elapsed_ms, (int, float)) or not math.isfinite(elapsed_ms) or elapsed_ms < 0:
        raise IntelligenceError("elapsed_ms must be finite and non-negative.")
    if files_inspected < len(source_items):
        raise IntelligenceError("files_inspected cannot be less than the number of source windows read.")

    tool_bytes = sum(len(item.encode("utf-8")) for item in tool_items)
    source_bytes = sum(len(item.encode("utf-8")) for item in source_items)
    tool_characters = sum(map(len, tool_items))
    source_characters = sum(map(len, source_items))
    reference_token_count = sum(
        reference_tokenizer.count_reference_tokens(encoding, item)
        for item in (*tool_items, *source_items)
    )
    total_bytes = tool_bytes + source_bytes
    total_characters = tool_characters + source_characters
    return {
        "files": files_inspected,
        "files_inspected": files_inspected,
        "source_bytes": source_bytes,
        "evidence_bytes": total_bytes,
        "interactions": interactions,
        "elapsed_ms": float(elapsed_ms),
        "source_evidence_bytes": source_bytes,
        "tool_evidence_bytes": tool_bytes,
        "total_evidence_bytes": total_bytes,
        "source_evidence_characters": source_characters,
        "tool_evidence_characters": tool_characters,
        "total_evidence_characters": total_characters,
        "estimated_evidence_tokens": (total_characters + 3) // 4,
        "evidence_metric_method": measurements.CHAR4_ESTIMATOR_ID,
        "evidence_metric_version": measurements.CHAR4_ESTIMATOR_VERSION,
        "evidence_metric_surface": measurements.CONTROLLED_EVIDENCE_SURFACE,
        "evidence_metric_quality": "MEASURED",
        "source_windows_read": len(source_items),
        "source_windows_located_only": located_only,
        "reference_tokenizer_evidence_tokens": reference_token_count,
        "reference_tokenizer_method": reference_tokenizer.REFERENCE_METHOD,
        "reference_tokenizer_method_version": reference_tokenizer.REFERENCE_METHOD_VERSION,
        "reference_tokenizer_package_version": reference_tokenizer.TOKENIZER_PACKAGE_VERSION,
        "reference_tokenizer_encoding": reference_tokenizer.ENCODING_NAME,
        "reference_tokenizer_asset_sha256": reference_tokenizer.ENCODING_ASSET_SHA256,
        "reference_tokenizer_surface": reference_tokenizer.REFERENCE_TOKEN_SURFACE,
        "reference_tokenizer_quality": "MEASURED",
    }


def create_benchmark(payload: dict[str, Any], encoding: Any) -> dict[str, Any]:
    required = {
        "id", "task", "corpus_id", "repository_id", "source_head", "source_identity",
        "generation", "question_id", "control_strategy", "intelligence_strategy",
        "stopping_rule", "stopping_rules_matched", "completion_criteria_matched",
        "control", "graphify",
    }
    if not isinstance(payload, dict) or set(payload) != required:
        raise IntelligenceError("Benchmark input does not match the controlled-evidence-v1 runner contract.")
    for key, limit in (
        ("id", 64), ("task", 240), ("corpus_id", 120), ("repository_id", 120),
        ("source_head", 80), ("source_identity", 128), ("generation", 80),
        ("question_id", 120), ("control_strategy", 240), ("intelligence_strategy", 240),
        ("stopping_rule", 500),
    ):
        if not isinstance(payload[key], str) or not 0 < len(payload[key]) <= limit:
            raise IntelligenceError(f"Invalid benchmark {key}.")
    if (
        not isinstance(payload["stopping_rules_matched"], bool)
        or not isinstance(payload["completion_criteria_matched"], bool)
    ):
        raise IntelligenceError("Benchmark stopping/completion match flags must be boolean.")
    if not re.fullmatch(r"[0-9a-f]{64}", payload["source_identity"]):
        raise IntelligenceError("source_identity must be the lowercase SHA-256 of the fixed snapshot.")
    provenance = {
        "recorded_at": datetime.now(UTC).isoformat(),
        "protocol_version": "controlled-evidence-v1",
        "corpus_id": payload["corpus_id"],
        "repository_id": payload["repository_id"],
        "source_head": payload["source_head"],
        "source_identity": payload["source_identity"],
        "question_id": payload["question_id"],
        "stopping_rule": payload["stopping_rule"],
        "stopping_rules_matched": payload["stopping_rules_matched"],
        "counting_boundary": measurements.CONTROLLED_COUNTING_BOUNDARY,
        "overlap_policy": measurements.CONTROLLED_OVERLAP_POLICY,
        "generation": payload["generation"],
        "control_strategy": payload["control_strategy"],
        "intelligence_strategy": payload["intelligence_strategy"],
        "completion_criteria_matched": payload["completion_criteria_matched"],
    }
    arms = {
        "control": _arm_metrics(payload["control"], encoding),
        "graphify": _arm_metrics(payload["graphify"], encoding),
    }
    return {
        "id": payload["id"],
        "task": payload["task"],
        **arms,
        "assessment": "Controlled evidence-volume comparison; not a model-quality or billing measurement.",
        "method": (
            "Source-first vs Intelligence-first; tool evidence and actually read source-window text counted "
            "as separate delivered items; each occurrence counted once."
        ),
        "provenance": provenance,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-root", type=Path, help="External local Intelligence state directory; omit to print the bounded record.")
    parser.add_argument("--cache-dir", type=Path, default=reference_tokenizer.DEFAULT_CACHE_DIR)
    args = parser.parse_args(argv)
    try:
        raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        if len(raw) > MAX_INPUT_BYTES:
            raise IntelligenceError("Benchmark input exceeds the 2 MiB limit.")
        payload = json.loads(raw)
        encoding = reference_tokenizer.load_encoding(args.cache_dir)
        benchmark = create_benchmark(payload, encoding)
        if args.database_root:
            measurements.add_benchmark(args.database_root, benchmark)
        sys.stdout.write(json.dumps(benchmark, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n")
        return 0
    except (IntelligenceError, ValueError, RecursionError, OSError) as exc:
        sys.stderr.write(f"benchmark refused: {exc}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
