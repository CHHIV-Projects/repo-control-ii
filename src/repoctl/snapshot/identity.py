from __future__ import annotations

from collections import defaultdict
from typing import Any

from ..scanner.util import encode_path_order


def line_sort_key(value: int | None) -> tuple[int, int]:
    if value is None:
        return (1, 0)
    return (0, value)


def bytes_key(value: str | None) -> bytes:
    if value is None:
        return b""
    return value.encode("utf-8")


def normalize_symbol_stream(symbols_payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for file_record in symbols_payload.get("python_files", []):
        path = file_record["path"]
        for symbol_kind, field_name in (
            ("function", "top_level_functions"),
            ("async_function", "top_level_async_functions"),
            ("class", "top_level_classes"),
        ):
            for symbol in file_record.get(field_name, []):
                rows.append(
                    {
                        "path": path,
                        "symbol_kind": symbol_kind,
                        "symbol_name": symbol["name"],
                        "start_line": symbol.get("start_line"),
                        "end_line": symbol.get("end_line"),
                    }
                )

    rows.sort(
        key=lambda row: (
            encode_path_order(row["path"]),
            line_sort_key(row["start_line"]),
            line_sort_key(row["end_line"]),
            bytes_key(row["symbol_kind"]),
            bytes_key(row["symbol_name"]),
        )
    )

    ordinals: defaultdict[tuple[str, str, str], int] = defaultdict(int)
    normalized: list[dict[str, Any]] = []
    for row in rows:
        group = (row["path"], row["symbol_kind"], row["symbol_name"])
        ordinals[group] += 1
        normalized.append({**row, "occurrence_ordinal": ordinals[group]})
    return normalized


def symbol_identity(row: dict[str, Any]) -> tuple[str, str, str, int]:
    return (row["path"], row["symbol_kind"], row["symbol_name"], row["occurrence_ordinal"])


def module_dependency_identity(row: dict[str, Any]) -> tuple[str, str, str, str]:
    return (row["source_file"], row["target_file"], row["import_kind"], row["imported_module_text"])


def imported_symbol_identity(row: dict[str, Any]) -> tuple[str, str, str, str, str, str | None]:
    return (
        row["source_file"],
        row["local_name"],
        row["source_file_target"],
        row["target_symbol"],
        row["target_symbol_kind"],
        row.get("alias"),
    )


def call_identity(row: dict[str, Any]) -> tuple[str, str, str, str, str, str]:
    return (
        row["caller_file"],
        row["caller_symbol_kind"],
        row["caller_symbol"],
        row["callee_file"],
        row["callee_symbol_kind"],
        row["callee_symbol"],
        row["resolution_kind"],
    )


def test_reference_identity(row: dict[str, Any]) -> tuple[str, str | None, str, str, str, str, str]:
    return (
        row["test_info"]["test_file"],
        row["test_info"]["test_class"],
        row["test_info"]["test_name"],
        row["target_file"],
        row["target_symbol"],
        row["target_symbol_kind"],
        row["test_info"]["reference_kind"],
    )


def diagnostic_identity(row: dict[str, Any]) -> tuple[Any, ...]:
    return (
        row["reason"],
        row["relationship_kind"],
        row["source_file"],
        row["source_symbol"],
        row["reference"],
        tuple(row.get("candidates", [])),
    )


def parse_failure_identity(path: str, parse_error: str | None) -> tuple[str, str | None]:
    return (path, parse_error)
