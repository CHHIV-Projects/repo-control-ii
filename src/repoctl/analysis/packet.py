from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..scanner.util import encode_path_order
from .contracts import (
    EVIDENCE_GROUP_ORDER,
    MAX_AI_DIAGNOSTIC_RECORDS,
    MAX_AI_FILE_RECORDS,
    MAX_AI_PARSE_RECORDS,
    MAX_AI_RELATIONSHIP_RECORDS,
    MAX_AI_REQUIREMENTS_RECORDS,
    MAX_AI_SYMBOL_RECORDS,
    MAX_AI_TEST_RECORDS,
    MAX_ANALYSIS_PACKET_BYTES,
    PROMPT_CONTRACT_VERSION,
    canonical_json_bytes,
    derive_packet_id,
    has_structural_delta,
)


class PacketBuildError(RuntimeError):
    pass


@dataclass(frozen=True)
class CandidateRecord:
    category: str
    prefix: str
    kind: str
    change_kind: str
    sort_key: tuple[Any, ...]
    data: dict[str, Any]


def _line_key(value: Any) -> int:
    if isinstance(value, int):
        return value
    return -1


def _bytes_key(value: Any) -> bytes:
    if value is None:
        return b""
    return str(value).encode("utf-8", errors="replace")


def _category_caps() -> dict[str, int]:
    return {
        "parse_failures": MAX_AI_PARSE_RECORDS,
        "diagnostics": MAX_AI_DIAGNOSTIC_RECORDS,
        "file_changes": MAX_AI_FILE_RECORDS,
        "requirements_changes": MAX_AI_REQUIREMENTS_RECORDS,
        "symbol_changes": MAX_AI_SYMBOL_RECORDS,
        "relationship_changes": MAX_AI_RELATIONSHIP_RECORDS,
        "test_reference_changes": MAX_AI_TEST_RECORDS,
    }


def _mk_file_candidates(file_deltas: dict[str, Any]) -> list[CandidateRecord]:
    out: list[CandidateRecord] = []
    for change_kind in ("added", "removed", "content_changed"):
        for row in file_deltas[change_kind]:
            out.append(
                CandidateRecord(
                    category="file_changes",
                    prefix="F",
                    kind="file_change",
                    change_kind=change_kind,
                    sort_key=(
                        {"added": 0, "removed": 1, "content_changed": 2}[change_kind],
                        encode_path_order(row["path"]),
                    ),
                    data={
                        "path": row["path"],
                        "status": row.get("status", change_kind),
                        "before_sha256": row.get("before_sha256"),
                        "after_sha256": row.get("after_sha256"),
                        "before_byte_size": row.get("before_byte_size"),
                        "after_byte_size": row.get("after_byte_size"),
                        "before_line_count": row.get("before_line_count"),
                        "after_line_count": row.get("after_line_count"),
                    },
                )
            )
    return out


def _mk_requirements_candidates(requirement_deltas: dict[str, Any]) -> list[CandidateRecord]:
    out: list[CandidateRecord] = []
    for change_kind in ("added", "removed", "changed"):
        for row in requirement_deltas[change_kind]:
            out.append(
                CandidateRecord(
                    category="requirements_changes",
                    prefix="Q",
                    kind="requirements_change",
                    change_kind=change_kind,
                    sort_key=(
                        {"added": 0, "removed": 1, "changed": 2}[change_kind],
                        encode_path_order(row["path"]),
                    ),
                    data={
                        "path": row["path"],
                        "before_declarations": row.get("before_declarations"),
                        "after_declarations": row.get("after_declarations"),
                    },
                )
            )
    return out


def _mk_symbol_candidates(symbol_deltas: dict[str, Any]) -> list[CandidateRecord]:
    out: list[CandidateRecord] = []
    order = {"added": 0, "removed": 1, "source_location_changed": 2}
    for change_kind in ("added", "removed", "source_location_changed"):
        for row in symbol_deltas[change_kind]:
            out.append(
                CandidateRecord(
                    category="symbol_changes",
                    prefix="S",
                    kind="symbol_change",
                    change_kind=change_kind,
                    sort_key=(
                        order[change_kind],
                        encode_path_order(row["path"]),
                        _bytes_key(row.get("symbol_kind")),
                        _bytes_key(row.get("symbol_name")),
                        _line_key(row.get("start_line", row.get("after_start_line"))),
                    ),
                    data={
                        "path": row["path"],
                        "symbol_kind": row.get("symbol_kind"),
                        "symbol_name": row.get("symbol_name"),
                        "occurrence_ordinal": row.get("occurrence_ordinal"),
                        "before_start_line": row.get("before_start_line"),
                        "after_start_line": row.get("after_start_line"),
                        "before_end_line": row.get("before_end_line"),
                        "after_end_line": row.get("after_end_line"),
                    },
                )
            )
    return out


def _mk_relationship_candidates(comparison: dict[str, Any]) -> list[CandidateRecord]:
    out: list[CandidateRecord] = []
    rel_type_order = {"module_dependency": 0, "imported_symbol": 1, "call": 2}
    change_order = {"added": 0, "removed": 1, "location_changed": 2}

    def add_rows(rel_type: str, payload: dict[str, Any], source_file_key: str, source_symbol_key: str, target_file_key: str, target_symbol_key: str) -> None:
        for change_kind in ("added", "removed", "location_changed"):
            for row in payload[change_kind]:
                out.append(
                    CandidateRecord(
                        category="relationship_changes",
                        prefix="R",
                        kind="relationship_change",
                        change_kind=change_kind,
                        sort_key=(
                            rel_type_order[rel_type],
                            change_order[change_kind],
                            encode_path_order(row.get(source_file_key) or ""),
                            _bytes_key(row.get(source_symbol_key)),
                            encode_path_order(row.get(target_file_key) or ""),
                            _bytes_key(row.get(target_symbol_key)),
                            _line_key(row.get("source_line", row.get("before_source_line"))),
                        ),
                        data={
                            "relationship_type": rel_type,
                            "source_file": row.get(source_file_key),
                            "source_symbol": row.get(source_symbol_key),
                            "target_file": row.get(target_file_key),
                            "target_symbol": row.get(target_symbol_key),
                            "source_symbol_kind": row.get("source_symbol_kind") or row.get("caller_symbol_kind"),
                            "target_symbol_kind": row.get("target_symbol_kind") or row.get("callee_symbol_kind"),
                            "resolution_kind": row.get("resolution_kind"),
                            "import_kind": row.get("import_kind"),
                            "local_name": row.get("local_name"),
                            "source_file_target": row.get("source_file_target"),
                            "before_source_line": row.get("before_source_line"),
                            "after_source_line": row.get("after_source_line"),
                            "source_line": row.get("source_line"),
                            "import_line": row.get("import_line"),
                            "call_line": row.get("call_line"),
                        },
                    )
                )

    add_rows(
        "module_dependency",
        comparison["module_dependency_deltas"],
        "source_file",
        "source_symbol",
        "target_file",
        "target_symbol",
    )
    add_rows(
        "imported_symbol",
        comparison["imported_symbol_deltas"],
        "source_file",
        "source_symbol",
        "source_file_target",
        "target_symbol",
    )
    add_rows(
        "call",
        comparison["call_deltas"],
        "caller_file",
        "caller_symbol",
        "callee_file",
        "callee_symbol",
    )
    return out


def _mk_test_reference_candidates(test_deltas: dict[str, Any]) -> list[CandidateRecord]:
    out: list[CandidateRecord] = []
    order = {"added": 0, "removed": 1, "location_changed": 2}
    for change_kind in ("added", "removed", "location_changed"):
        for row in test_deltas[change_kind]:
            test_info = row["test_info"]
            out.append(
                CandidateRecord(
                    category="test_reference_changes",
                    prefix="T",
                    kind="test_reference_change",
                    change_kind=change_kind,
                    sort_key=(
                        order[change_kind],
                        encode_path_order(test_info["test_file"]),
                        _bytes_key(test_info.get("test_class")),
                        _bytes_key(test_info["test_name"]),
                        encode_path_order(row["target_file"]),
                        _bytes_key(row.get("target_symbol")),
                        _line_key(row.get("source_line", row.get("before_source_line"))),
                    ),
                    data={
                        "test_info": test_info,
                        "target_file": row.get("target_file"),
                        "target_symbol": row.get("target_symbol"),
                        "target_symbol_kind": row.get("target_symbol_kind"),
                        "before_source_line": row.get("before_source_line"),
                        "after_source_line": row.get("after_source_line"),
                        "source_line": row.get("source_line"),
                    },
                )
            )
    return out


def _mk_parse_candidates(parse_deltas: dict[str, Any]) -> list[CandidateRecord]:
    out: list[CandidateRecord] = []
    order = {"introduced_parse_failures": 0, "resolved_parse_failures": 1}
    for change_kind in ("introduced_parse_failures", "resolved_parse_failures"):
        mapped = "introduced" if change_kind.startswith("introduced") else "resolved"
        for row in parse_deltas[change_kind]:
            out.append(
                CandidateRecord(
                    category="parse_failures",
                    prefix="P",
                    kind="parse_failure_change",
                    change_kind=mapped,
                    sort_key=(order[change_kind], encode_path_order(row["path"]), _bytes_key(row.get("parse_error"))),
                    data={"path": row.get("path"), "parse_error": row.get("parse_error")},
                )
            )
    return out


def _mk_diagnostic_candidates(diag_deltas: dict[str, Any]) -> list[CandidateRecord]:
    out: list[CandidateRecord] = []
    order = {"added": 0, "removed": 1, "source_location_changed": 2}
    for change_kind in ("added", "removed", "source_location_changed"):
        for row in diag_deltas[change_kind]:
            out.append(
                CandidateRecord(
                    category="diagnostics",
                    prefix="D",
                    kind="relationship_diagnostic_change",
                    change_kind=change_kind,
                    sort_key=(
                        order[change_kind],
                        _bytes_key(row.get("reason")),
                        encode_path_order(row.get("source_file") or ""),
                        _bytes_key(row.get("source_symbol")),
                        _bytes_key(row.get("reference")),
                        _line_key(row.get("source_line", row.get("before_source_line"))),
                    ),
                    data={
                        "reason": row.get("reason"),
                        "source_file": row.get("source_file"),
                        "source_symbol": row.get("source_symbol"),
                        "source_symbol_kind": row.get("source_symbol_kind"),
                        "reference": row.get("reference"),
                        "before_source_line": row.get("before_source_line"),
                        "after_source_line": row.get("after_source_line"),
                        "source_line": row.get("source_line"),
                    },
                )
            )
    return out


def _build_candidate_categories(comparison: dict[str, Any]) -> dict[str, list[CandidateRecord]]:
    categories = {
        "parse_failures": _mk_parse_candidates(comparison["parse_failure_deltas"]),
        "diagnostics": _mk_diagnostic_candidates(comparison["relationship_diagnostic_deltas"]),
        "file_changes": _mk_file_candidates(comparison["file_deltas"]),
        "requirements_changes": _mk_requirements_candidates(comparison["requirements_deltas"]),
        "symbol_changes": _mk_symbol_candidates(comparison["symbol_deltas"]),
        "relationship_changes": _mk_relationship_candidates(comparison),
        "test_reference_changes": _mk_test_reference_candidates(comparison["test_reference_deltas"]),
    }
    for key in categories:
        categories[key] = sorted(categories[key], key=lambda item: item.sort_key)
    return categories


def _build_protected_evidence(comparison: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "evidence_id": "A001",
            "kind": "aggregate_counts",
            "category": "aggregate",
            "change_kind": "summary",
            "data": comparison["aggregate_counts"],
        },
        {
            "evidence_id": "C001",
            "kind": "coverage_completeness",
            "category": "coverage",
            "change_kind": "before_snapshot",
            "data": comparison["before_repository_state"]["structural_coverage"],
        },
        {
            "evidence_id": "C002",
            "kind": "coverage_completeness",
            "category": "coverage",
            "change_kind": "after_snapshot",
            "data": comparison["after_repository_state"]["structural_coverage"],
        },
    ]


def _serialize_packet_bytes(packet_payload: dict[str, Any]) -> int:
    return len(canonical_json_bytes(packet_payload))


def _finalize_detailed_evidence(kept_records: list[CandidateRecord]) -> list[dict[str, Any]]:
    counters: dict[str, int] = {}
    out: list[dict[str, Any]] = []
    for record in kept_records:
        next_idx = counters.get(record.prefix, 0) + 1
        counters[record.prefix] = next_idx
        evidence_id = f"{record.prefix}{next_idx:03d}"
        out.append(
            {
                "evidence_id": evidence_id,
                "kind": record.kind,
                "category": record.category,
                "change_kind": record.change_kind,
                "data": record.data,
            }
        )
    return out


def _build_base_packet(comparison: dict[str, Any], truncation_metadata: dict[str, Any], evidence_records: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "prompt_contract_version": PROMPT_CONTRACT_VERSION,
        "authority": "deterministic_comparison_evidence",
        "authority_statement": "Deterministic Repo Control Plane comparison evidence is authoritative. AI interpretation is advisory.",
        "repository_id": comparison["repository_id"],
        "comparison_id": comparison["comparison_id"],
        "before_snapshot_id": comparison["before_snapshot_id"],
        "after_snapshot_id": comparison["after_snapshot_id"],
        "before_repository_state": {
            "branch": comparison["before_repository_state"]["branch"],
            "head_commit": comparison["before_repository_state"]["head_commit"],
            "structural_coverage": comparison["before_repository_state"]["structural_coverage"],
        },
        "after_repository_state": {
            "branch": comparison["after_repository_state"]["branch"],
            "head_commit": comparison["after_repository_state"]["head_commit"],
            "structural_coverage": comparison["after_repository_state"]["structural_coverage"],
        },
        "aggregate_counts": comparison["aggregate_counts"],
        "structural_delta_present": has_structural_delta(comparison["aggregate_counts"]),
        "selection_contract": {
            "max_analysis_packet_bytes": MAX_ANALYSIS_PACKET_BYTES,
            "max_ai_file_records": MAX_AI_FILE_RECORDS,
            "max_ai_requirements_records": MAX_AI_REQUIREMENTS_RECORDS,
            "max_ai_symbol_records": MAX_AI_SYMBOL_RECORDS,
            "max_ai_relationship_records": MAX_AI_RELATIONSHIP_RECORDS,
            "max_ai_test_records": MAX_AI_TEST_RECORDS,
            "max_ai_parse_records": MAX_AI_PARSE_RECORDS,
            "max_ai_diagnostic_records": MAX_AI_DIAGNOSTIC_RECORDS,
            "evidence_group_order": EVIDENCE_GROUP_ORDER,
        },
        "truncation_metadata": truncation_metadata,
        "evidence_records": evidence_records,
    }


def build_analysis_packet(comparison: dict[str, Any]) -> dict[str, Any]:
    if comparison.get("schema_version") != 1:
        raise PacketBuildError("unsupported comparison schema version")

    categories = _build_candidate_categories(comparison)
    caps = _category_caps()

    total_before_cap = {category: len(records) for category, records in categories.items()}
    kept_after_cap = {category: min(len(records), caps[category]) for category, records in categories.items()}
    removed_by_cap = {category: total_before_cap[category] - kept_after_cap[category] for category in categories}

    post_cap_records: dict[str, list[CandidateRecord]] = {
        category: records[: caps[category]] for category, records in categories.items()
    }

    canonical_stream: list[CandidateRecord] = []
    for group in EVIDENCE_GROUP_ORDER:
        if group in ("aggregate", "coverage"):
            continue
        canonical_stream.extend(post_cap_records[group])

    protected_evidence = _build_protected_evidence(comparison)

    protected_metadata = {
        "total_candidate_records_by_category_before_category_cap": total_before_cap,
        "records_retained_after_category_cap": kept_after_cap,
        "records_removed_by_category_cap": removed_by_cap,
        "records_removed_by_total_byte_enforcement": {k: 0 for k in categories},
        "final_records_sent_by_category": {**{"aggregate": 1, "coverage": 2}, **{k: 0 for k in categories}},
        "category_truncation_occurred": any(value > 0 for value in removed_by_cap.values()),
        "byte_truncation_occurred": False,
        "final_serialized_packet_byte_count": 0,
    }

    protected_packet = _build_base_packet(comparison, protected_metadata, protected_evidence)
    protected_without_id = dict(protected_packet)
    protected_without_id["packet_id"] = ""
    if _serialize_packet_bytes(protected_without_id) > MAX_ANALYSIS_PACKET_BYTES:
        raise PacketBuildError("protected packet content exceeds MAX_ANALYSIS_PACKET_BYTES")

    kept_stream = list(canonical_stream)
    while True:
        finalized_detailed = _finalize_detailed_evidence(kept_stream)
        all_evidence = [*protected_evidence, *finalized_detailed]

        removed_by_bytes = {k: 0 for k in categories}
        for record in canonical_stream[len(kept_stream) :]:
            removed_by_bytes[record.category] += 1

        final_counts = {**{"aggregate": 1, "coverage": 2}, **{k: 0 for k in categories}}
        for row in finalized_detailed:
            final_counts[row["category"]] += 1

        truncation_metadata = {
            "total_candidate_records_by_category_before_category_cap": total_before_cap,
            "records_retained_after_category_cap": kept_after_cap,
            "records_removed_by_category_cap": removed_by_cap,
            "records_removed_by_total_byte_enforcement": removed_by_bytes,
            "final_records_sent_by_category": final_counts,
            "category_truncation_occurred": any(value > 0 for value in removed_by_cap.values()),
            "byte_truncation_occurred": any(value > 0 for value in removed_by_bytes.values()),
            "final_serialized_packet_byte_count": 0,
        }

        packet_without_id = _build_base_packet(comparison, truncation_metadata, all_evidence)
        packet_id = derive_packet_id(packet_without_id)
        packet_without_id["packet_id"] = packet_id
        final_size = _serialize_packet_bytes(packet_without_id)
        packet_without_id["truncation_metadata"]["final_serialized_packet_byte_count"] = final_size

        # Recompute after adding byte-count metadata.
        final_size = _serialize_packet_bytes(packet_without_id)
        packet_without_id["truncation_metadata"]["final_serialized_packet_byte_count"] = final_size

        if final_size <= MAX_ANALYSIS_PACKET_BYTES:
            return packet_without_id

        if not kept_stream:
            raise PacketBuildError("unable to satisfy MAX_ANALYSIS_PACKET_BYTES even after removing all detailed records")

        kept_stream.pop()
