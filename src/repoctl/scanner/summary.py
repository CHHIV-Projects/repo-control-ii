from __future__ import annotations

from typing import Any


def build_summary(
    repository: dict[str, Any],
    files_payload: dict[str, Any],
    symbols_payload: dict[str, Any],
    tests_payload: dict[str, Any],
    dependencies_payload: dict[str, Any],
) -> str:
    lines: list[str] = []

    lines.append("# Repository Scan Summary")
    lines.append("")
    lines.append("## Repository")
    lines.append(f"- root: {repository['repository_root']}")
    lines.append(f"- repository_id: {repository['repository_id']}")
    branch = repository["branch"]
    if branch["state"] == "attached":
        lines.append(f"- branch: {branch['name']}")
    else:
        lines.append("- branch: detached")
    lines.append(f"- head: {repository['head_commit']}")
    lines.append(f"- tracked_file_count: {repository['tracked_file_count']}")
    lines.append(f"- working_tree_clean: {repository['working_tree']['is_clean']}")
    lines.append("")

    lines.append("## Tracked Files")
    for record in files_payload["files"]:
        lines.append(
            f"- {record['path']} | type={record['type']} | bytes={record['byte_size']} | lines={record['line_count']}"
        )
    lines.append("")

    lines.append("## Python Files")
    for record in symbols_payload["python_files"]:
        lines.append(f"- {record['path']} | parse_success={record['parse_success']}")
        if not record["parse_success"]:
            lines.append(f"  - parse_error: {record['parse_error']}")
            continue
        fn_names = ", ".join(item["name"] for item in record["top_level_functions"]) or "none"
        afn_names = ", ".join(item["name"] for item in record["top_level_async_functions"]) or "none"
        cls_names = ", ".join(item["name"] for item in record["top_level_classes"]) or "none"
        import_names = ", ".join(record["imported_module_names"]) or "none"
        lines.append(f"  - functions: {fn_names}")
        lines.append(f"  - async_functions: {afn_names}")
        lines.append(f"  - classes: {cls_names}")
        lines.append(f"  - imports: {import_names}")
    lines.append("")

    lines.append("## Tests")
    lines.append(f"- test_like_file_count: {tests_payload['test_like_file_count']}")
    lines.append(f"- test_class_count: {tests_payload['test_class_count']}")
    lines.append(f"- test_method_count: {tests_payload['test_method_count']}")
    lines.append(f"- top_level_test_function_count: {tests_payload['top_level_test_function_count']}")
    lines.append(f"- test_reference_count: {tests_payload.get('test_reference_count', 0)}")
    lines.append("")

    counts = dependencies_payload["counts"]
    lines.append("## Relationships")
    lines.append(f"- internal_module_dependency_count: {counts['module_dependency_count']}")
    lines.append(f"- resolved_imported_symbol_relationship_count: {counts['imported_symbol_relationship_count']}")
    lines.append(f"- resolved_static_internal_call_count: {counts['call_relationship_count']}")
    lines.append(f"- unresolved_relationship_count: {counts['unresolved_relationship_count']}")
    lines.append(f"- ambiguous_relationship_count: {counts['ambiguous_relationship_count']}")
    lines.append(f"- parse_failure_limited_relationship_count: {counts['target_parse_failure_count']}")
    lines.append("")

    lines.append("## Per-File Module Dependencies")
    module_edges = dependencies_payload["module_dependencies"]
    if not module_edges:
        lines.append("- none")
    else:
        for edge in module_edges:
            lines.append(
                f"- {edge['source_file']} -> {edge['target_file']} | kind={edge['import_kind']} | import={edge['imported_module_text']} | line={edge['source_line']}"
            )
    lines.append("")

    lines.append("## Per-Test Resolved References")
    found_test_refs = False
    for test_file in tests_payload.get("test_files", []):
        for cls in test_file.get("classes", []):
            for method in cls.get("test_methods", []):
                refs = method.get("resolved_references", [])
                if not refs:
                    continue
                found_test_refs = True
                lines.append(f"- {test_file['path']}::{cls['name']}.{method['name']}")
                for ref in refs:
                    lines.append(
                        f"  - {ref['target_file']}:{ref['target_symbol']} ({ref['target_symbol_kind']}) kind={ref['reference_kind']} line={ref['source_line']}"
                    )
        for fn in test_file.get("top_level_test_functions", []):
            refs = fn.get("resolved_references", [])
            if not refs:
                continue
            found_test_refs = True
            lines.append(f"- {test_file['path']}::{fn['name']}")
            for ref in refs:
                lines.append(
                    f"  - {ref['target_file']}:{ref['target_symbol']} ({ref['target_symbol_kind']}) kind={ref['reference_kind']} line={ref['source_line']}"
                )
    if not found_test_refs:
        lines.append("- none")
    lines.append("")

    lines.append("## Requirements")
    requirements = repository.get("requirements", [])
    if not requirements:
        lines.append("- none")
    else:
        for req in requirements:
            if req["declarations"]:
                lines.append(f"- {req['path']}: {', '.join(req['declarations'])}")
            else:
                lines.append(f"- {req['path']}: (no declarations)")
    lines.append("")

    lines.append("## Parse Errors")
    parse_errors = []
    for record in symbols_payload["python_files"]:
        if not record["parse_success"] and record["parse_error"]:
            parse_errors.append(f"{record['path']}: {record['parse_error']}")
    for record in tests_payload["test_files"]:
        if not record["parse_success"] and record["parse_error"]:
            parse_errors.append(f"{record['path']}: {record['parse_error']}")

    if not parse_errors:
        lines.append("- none")
    else:
        for entry in parse_errors:
            lines.append(f"- {entry}")

    return "\n".join(lines) + "\n"
