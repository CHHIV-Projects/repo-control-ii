from __future__ import annotations

import hashlib
import json
import shutil
from collections import Counter, defaultdict
from pathlib import Path
from tempfile import mkdtemp
from typing import Any

from ..scanner.core import DEFAULT_STATE_ROOT
from ..scanner.git_ops import validate_git_worktree
from ..scanner.util import encode_path_order, write_json_deterministic
from ..snapshot.identity import (
    bytes_key,
    call_identity,
    diagnostic_identity,
    imported_symbol_identity,
    line_sort_key,
    module_dependency_identity,
    normalize_symbol_stream,
    parse_failure_identity,
    symbol_identity,
    test_reference_identity,
)
from ..snapshot.manager import SCAN_ARTIFACTS, _verify_existing_snapshot

MAX_MD_ITEMS_PER_SECTION = 50
COMPARE_REQUIRED_FILES = {"comparison.json", "comparison.md"}
DIAGNOSTIC_RECORD_REASONS = {
    "ambiguous_module",
    "target_parse_failure",
    "shadowed_or_rebound",
    "unresolved_symbol",
    "wildcard_import",
}


def derive_comparison_id(before_snapshot_id: str, after_snapshot_id: str) -> str:
    data = b"repoctl-comparison-v1\0" + before_snapshot_id.encode("ascii") + b"\0" + after_snapshot_id.encode("ascii") + b"\0"
    return "cmp--" + hashlib.sha256(data).hexdigest()[:16]


def _load_snapshot_artifacts(snapshot_dir: Path) -> dict[str, Any]:
    payloads: dict[str, Any] = {}
    for name in ["snapshot.json", *SCAN_ARTIFACTS]:
        path = snapshot_dir / name
        if name.endswith(".json"):
            payloads[name] = json.loads(path.read_text(encoding="utf-8"))
        else:
            payloads[name] = path.read_text(encoding="utf-8")
    return payloads


def _index_by(items: list[dict[str, Any]], key_fn) -> dict[Any, dict[str, Any]]:
    out: dict[Any, dict[str, Any]] = {}
    for item in items:
        out[key_fn(item)] = item
    return out


def _compare_records(before: dict[Any, dict[str, Any]], after: dict[Any, dict[str, Any]]) -> tuple[list, list, list]:
    before_keys = set(before)
    after_keys = set(after)
    added = [after[k] for k in sorted(after_keys - before_keys, key=lambda k: repr(k))]
    removed = [before[k] for k in sorted(before_keys - after_keys, key=lambda k: repr(k))]
    retained = [after[k] for k in sorted(before_keys & after_keys, key=lambda k: repr(k))]
    return added, removed, retained


def _sort_paths(items: list[dict[str, Any]], field: str = "path") -> list[dict[str, Any]]:
    return sorted(items, key=lambda item: encode_path_order(item[field]))


def _file_delta(before_files: dict[str, Any], after_files: dict[str, Any]) -> dict[str, Any]:
    before_index = {f["path"]: f for f in before_files["files"]}
    after_index = {f["path"]: f for f in after_files["files"]}
    all_paths = sorted(set(before_index) | set(after_index), key=encode_path_order)

    added = []
    removed = []
    content_changed = []
    unchanged = []
    for path in all_paths:
        before = before_index.get(path)
        after = after_index.get(path)
        if before is None:
            added.append({"path": path, "status": "added", "after": after})
        elif after is None:
            removed.append({"path": path, "status": "removed", "before": before})
        elif before["sha256"] != after["sha256"]:
            content_changed.append(
                {
                    "path": path,
                    "status": "content_changed",
                    "before_sha256": before["sha256"],
                    "after_sha256": after["sha256"],
                    "before_byte_size": before["byte_size"],
                    "after_byte_size": after["byte_size"],
                    "before_line_count": before["line_count"],
                    "after_line_count": after["line_count"],
                    "before_type": before["type"],
                    "after_type": after["type"],
                }
            )
        else:
            unchanged.append({"path": path, "status": "unchanged"})

    return {
        "added": added,
        "removed": removed,
        "content_changed": content_changed,
        "unchanged": unchanged,
    }


def _requirements_delta(before_repo: dict[str, Any], after_repo: dict[str, Any]) -> dict[str, Any]:
    before_index = {r["path"]: r for r in before_repo.get("requirements", [])}
    after_index = {r["path"]: r for r in after_repo.get("requirements", [])}
    all_paths = sorted(set(before_index) | set(after_index), key=encode_path_order)
    added = []
    removed = []
    changed = []
    unchanged = []
    for path in all_paths:
        before = before_index.get(path)
        after = after_index.get(path)
        if before is None:
            added.append({"path": path, "after_declarations": after["declarations"]})
        elif after is None:
            removed.append({"path": path, "before_declarations": before["declarations"]})
        elif before["declarations"] != after["declarations"]:
            changed.append({"path": path, "before_declarations": before["declarations"], "after_declarations": after["declarations"]})
        else:
            unchanged.append({"path": path, "declarations": before["declarations"]})
    return {"added": added, "removed": removed, "changed": changed, "unchanged": unchanged}


def _symbol_delta(before_symbols: dict[str, Any], after_symbols: dict[str, Any]) -> dict[str, Any]:
    before_stream = normalize_symbol_stream(before_symbols)
    after_stream = normalize_symbol_stream(after_symbols)
    before_index = _index_by(before_stream, symbol_identity)
    after_index = _index_by(after_stream, symbol_identity)
    added, removed, retained = _compare_records(before_index, after_index)
    location_changed = []
    retained_records = []
    for row in retained:
        key = symbol_identity(row)
        before = before_index[key]
        if before["start_line"] != row["start_line"] or before["end_line"] != row["end_line"]:
            location_changed.append(
                {
                    "path": row["path"],
                    "symbol_kind": row["symbol_kind"],
                    "symbol_name": row["symbol_name"],
                    "occurrence_ordinal": row["occurrence_ordinal"],
                    "before_start_line": before["start_line"],
                    "after_start_line": row["start_line"],
                    "before_end_line": before["end_line"],
                    "after_end_line": row["end_line"],
                }
            )
        retained_records.append(row)
    return {"added": added, "removed": removed, "retained": retained_records, "source_location_changed": location_changed}


def _relationship_delta(before_list: list[dict[str, Any]], after_list: list[dict[str, Any]], identity_fn, source_line_field: str) -> dict[str, Any]:
    before_index = _index_by(before_list, identity_fn)
    after_index = _index_by(after_list, identity_fn)
    added, removed, retained = _compare_records(before_index, after_index)
    location_changed = []
    for row in retained:
        key = identity_fn(row)
        before = before_index[key]
        if before.get(source_line_field) != row.get(source_line_field):
            location_changed.append({
                **{k: v for k, v in row.items() if k != source_line_field},
                "before_source_line": before.get(source_line_field),
                "after_source_line": row.get(source_line_field),
            })
    return {"added": added, "removed": removed, "retained": retained, "location_changed": location_changed}


def _flatten_test_refs(tests_payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for tf in tests_payload.get("test_files", []):
        for cls in tf.get("classes", []):
            for method in cls.get("test_methods", []):
                for ref in method.get("resolved_references", []):
                    rows.append({
                        "test_info": {"test_file": tf["path"], "test_class": cls["name"], "test_name": method["name"], "reference_kind": ref["reference_kind"]},
                        "target_file": ref["target_file"],
                        "target_symbol": ref["target_symbol"],
                        "target_symbol_kind": ref["target_symbol_kind"],
                        "source_line": ref.get("source_line"),
                    })
        for fn in tf.get("top_level_test_functions", []):
            for ref in fn.get("resolved_references", []):
                rows.append({
                    "test_info": {"test_file": tf["path"], "test_class": None, "test_name": fn["name"], "reference_kind": ref["reference_kind"]},
                    "target_file": ref["target_file"],
                    "target_symbol": ref["target_symbol"],
                    "target_symbol_kind": ref["target_symbol_kind"],
                    "source_line": ref.get("source_line"),
                })
    return rows


def _parse_failure_rows(symbols_payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"path": row["path"], "parse_error": row["parse_error"]}
        for row in symbols_payload.get("python_files", [])
        if not row.get("parse_success", False)
    ]


def _diagnostic_delta(before_deps: dict[str, Any], after_deps: dict[str, Any]) -> dict[str, Any]:
    before_rows = [d for d in before_deps.get("unresolved_relationships", []) if d["reason"] in DIAGNOSTIC_RECORD_REASONS]
    after_rows = [d for d in after_deps.get("unresolved_relationships", []) if d["reason"] in DIAGNOSTIC_RECORD_REASONS]
    before_index = _index_by(before_rows, diagnostic_identity)
    after_index = _index_by(after_rows, diagnostic_identity)
    added, removed, retained = _compare_records(before_index, after_index)
    location_changed = []
    for row in retained:
        key = diagnostic_identity(row)
        before = before_index[key]
        if before.get("source_line") != row.get("source_line"):
            location_changed.append({
                **{k: v for k, v in row.items() if k != "source_line"},
                "before_source_line": before.get("source_line"),
                "after_source_line": row.get("source_line"),
            })
    before_counts = Counter(d["reason"] for d in before_deps.get("unresolved_relationships", []))
    after_counts = Counter(d["reason"] for d in after_deps.get("unresolved_relationships", []))
    reasons = sorted(set(before_counts) | set(after_counts))
    count_deltas = [
        {"reason": reason, "before_count": before_counts.get(reason, 0), "after_count": after_counts.get(reason, 0)}
        for reason in reasons
    ]
    return {"added": added, "removed": removed, "retained": retained, "source_location_changed": location_changed, "count_deltas": count_deltas}


def _aggregate_counts(comparison: dict[str, Any]) -> dict[str, Any]:
    return {
        "files": {k: len(v) for k, v in comparison["file_deltas"].items()},
        "requirements": {
            "added": len(comparison["requirements_deltas"]["added"]),
            "removed": len(comparison["requirements_deltas"]["removed"]),
            "changed": len(comparison["requirements_deltas"]["changed"]),
            "unchanged": len(comparison["requirements_deltas"]["unchanged"]),
        },
        "symbols": {
            "added": len(comparison["symbol_deltas"]["added"]),
            "removed": len(comparison["symbol_deltas"]["removed"]),
            "retained": len(comparison["symbol_deltas"]["retained"]),
            "source_location_changed": len(comparison["symbol_deltas"]["source_location_changed"]),
        },
        "module_dependencies": {k: len(v) for k, v in comparison["module_dependency_deltas"].items()},
        "imported_symbols": {k: len(v) for k, v in comparison["imported_symbol_deltas"].items()},
        "calls": {k: len(v) for k, v in comparison["call_deltas"].items()},
        "test_references": {k: len(v) for k, v in comparison["test_reference_deltas"].items()},
        "parse_failures": {
            "introduced": len(comparison["parse_failure_deltas"]["introduced_parse_failures"]),
            "resolved": len(comparison["parse_failure_deltas"]["resolved_parse_failures"]),
            "retained": len(comparison["parse_failure_deltas"]["retained_parse_failures"]),
        },
        "diagnostics": {
            "added": len(comparison["relationship_diagnostic_deltas"]["added"]),
            "removed": len(comparison["relationship_diagnostic_deltas"]["removed"]),
            "retained": len(comparison["relationship_diagnostic_deltas"]["retained"]),
            "source_location_changed": len(comparison["relationship_diagnostic_deltas"]["source_location_changed"]),
        },
    }


def build_comparison_payload(repository_id: str, before_snapshot_id: str, after_snapshot_id: str, before_artifacts: dict[str, Any], after_artifacts: dict[str, Any], comparison_id: str) -> dict[str, Any]:
    before_repo = before_artifacts["repository.json"]
    after_repo = after_artifacts["repository.json"]
    before_deps = before_artifacts["dependencies.json"]
    after_deps = after_artifacts["dependencies.json"]
    before_tests = before_artifacts["tests.json"]
    after_tests = after_artifacts["tests.json"]

    parse_before = _parse_failure_rows(before_artifacts["symbols.json"])
    parse_after = _parse_failure_rows(after_artifacts["symbols.json"])
    parse_before_index = _index_by(parse_before, lambda row: parse_failure_identity(row["path"], row["parse_error"]))
    parse_after_index = _index_by(parse_after, lambda row: parse_failure_identity(row["path"], row["parse_error"]))
    introduced_parse_failures, resolved_parse_failures, retained_parse_failures = _compare_records(parse_before_index, parse_after_index)

    comparison = {
        "schema_version": 1,
        "comparison_id": comparison_id,
        "repository_id": repository_id,
        "before_snapshot_id": before_snapshot_id,
        "after_snapshot_id": after_snapshot_id,
        "before_repository_state": {
            "branch": before_repo["branch"],
            "head_commit": before_repo["head_commit"],
            "working_tree": before_repo["working_tree"],
            "working_tree_categories": before_repo["working_tree_categories"],
            "structural_coverage": before_artifacts["snapshot.json"]["structural_coverage"],
        },
        "after_repository_state": {
            "branch": after_repo["branch"],
            "head_commit": after_repo["head_commit"],
            "working_tree": after_repo["working_tree"],
            "working_tree_categories": after_repo["working_tree_categories"],
            "structural_coverage": after_artifacts["snapshot.json"]["structural_coverage"],
        },
        "file_deltas": _file_delta(before_artifacts["files.json"], after_artifacts["files.json"]),
        "requirements_deltas": _requirements_delta(before_repo, after_repo),
        "symbol_deltas": _symbol_delta(before_artifacts["symbols.json"], after_artifacts["symbols.json"]),
        "module_dependency_deltas": _relationship_delta(before_deps.get("module_dependencies", []), after_deps.get("module_dependencies", []), module_dependency_identity, "source_line"),
        "imported_symbol_deltas": _relationship_delta(before_deps.get("imported_symbol_relationships", []), after_deps.get("imported_symbol_relationships", []), imported_symbol_identity, "import_line"),
        "call_deltas": _relationship_delta(before_deps.get("call_relationships", []), after_deps.get("call_relationships", []), call_identity, "call_line"),
        "test_reference_deltas": _relationship_delta(_flatten_test_refs(before_tests), _flatten_test_refs(after_tests), test_reference_identity, "source_line"),
        "parse_failure_deltas": {
            "introduced_parse_failures": introduced_parse_failures,
            "resolved_parse_failures": resolved_parse_failures,
            "retained_parse_failures": retained_parse_failures,
        },
        "relationship_diagnostic_deltas": _diagnostic_delta(before_deps, after_deps),
    }
    comparison["aggregate_counts"] = _aggregate_counts(comparison)
    return comparison


def _sort_md_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def rec_key(item: dict[str, Any]) -> tuple[Any, ...]:
        source_file = item.get("path") or item.get("source_file") or item.get("caller_file") or item.get("test_info", {}).get("test_file") or ""
        target_file = item.get("target_file") or item.get("callee_file") or ""
        source_symbol = item.get("symbol_name") or item.get("source_symbol") or item.get("caller_symbol") or item.get("test_info", {}).get("test_name") or ""
        target_symbol = item.get("target_symbol") or item.get("callee_symbol") or ""
        line = item.get("start_line")
        if line is None:
            line = item.get("source_line")
        if line is None:
            line = item.get("call_line")
        return (encode_path_order(source_file), bytes_key(source_symbol), encode_path_order(target_file), bytes_key(target_symbol), line_sort_key(line))
    return sorted(records, key=rec_key)


def _render_section(lines: list[str], title: str, category_pairs: list[tuple[str, list[dict[str, Any]]]], formatter) -> None:
    lines.append(title)
    total = sum(len(items) for _, items in category_pairs)
    rendered = 0
    lines.append(f"- total_records: {total}")
    chosen: list[tuple[str, dict[str, Any]]] = []
    for category, items in category_pairs:
        for item in _sort_md_records(items):
            if rendered >= MAX_MD_ITEMS_PER_SECTION:
                break
            chosen.append((category, item))
            rendered += 1
        if rendered >= MAX_MD_ITEMS_PER_SECTION:
            break
    lines.append(f"- rendered_records: {rendered}")
    lines.append(f"- markdown_truncated: {total > rendered}")
    if not chosen:
        lines.append("- none")
    else:
        for category, item in chosen:
            lines.append(f"- {category}: {formatter(item)}")
    lines.append("")


def render_comparison_markdown(comparison: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("# Repository Structural Comparison")
    lines.append("")
    lines.append("## Comparison State")
    lines.append(f"- comparison_id: {comparison['comparison_id']}")
    lines.append(f"- before_snapshot_id: {comparison['before_snapshot_id']}")
    lines.append(f"- after_snapshot_id: {comparison['after_snapshot_id']}")
    lines.append("")
    lines.append("## Structural Coverage")
    lines.append(f"- before: {comparison['before_repository_state']['structural_coverage']['worktree_completeness']}")
    lines.append(f"- after: {comparison['after_repository_state']['structural_coverage']['worktree_completeness']}")
    lines.append("")
    _render_section(lines, "## File Changes", [
        ("added", comparison["file_deltas"]["added"]),
        ("removed", comparison["file_deltas"]["removed"]),
        ("content_changed", comparison["file_deltas"]["content_changed"]),
        ("unchanged", comparison["file_deltas"]["unchanged"]),
    ], lambda item: item["path"])
    _render_section(lines, "## Requirements Changes", [
        ("added", comparison["requirements_deltas"]["added"]),
        ("removed", comparison["requirements_deltas"]["removed"]),
        ("changed", comparison["requirements_deltas"]["changed"]),
        ("unchanged", comparison["requirements_deltas"]["unchanged"]),
    ], lambda item: item["path"])
    _render_section(lines, "## Symbol Changes", [
        ("added", comparison["symbol_deltas"]["added"]),
        ("removed", comparison["symbol_deltas"]["removed"]),
        ("source_location_changed", comparison["symbol_deltas"]["source_location_changed"]),
        ("retained", comparison["symbol_deltas"]["retained"]),
    ], lambda item: f"{item['path']}::{item['symbol_name']} ({item['symbol_kind']})")
    _render_section(lines, "## Internal Dependency Changes", [
        ("added", comparison["module_dependency_deltas"]["added"]),
        ("removed", comparison["module_dependency_deltas"]["removed"]),
        ("location_changed", comparison["module_dependency_deltas"]["location_changed"]),
        ("retained", comparison["module_dependency_deltas"]["retained"]),
    ], lambda item: f"{item['source_file']} -> {item['target_file']} ({item.get('import_kind') or item.get('resolution_kind')})")
    _render_section(lines, "## Imported-Symbol Changes", [
        ("added", comparison["imported_symbol_deltas"]["added"]),
        ("removed", comparison["imported_symbol_deltas"]["removed"]),
        ("location_changed", comparison["imported_symbol_deltas"]["location_changed"]),
        ("retained", comparison["imported_symbol_deltas"]["retained"]),
    ], lambda item: f"{item['source_file']}::{item['local_name']} -> {item['source_file_target']}::{item['target_symbol']}")
    _render_section(lines, "## Static Call Changes", [
        ("added", comparison["call_deltas"]["added"]),
        ("removed", comparison["call_deltas"]["removed"]),
        ("location_changed", comparison["call_deltas"]["location_changed"]),
        ("retained", comparison["call_deltas"]["retained"]),
    ], lambda item: f"{item['caller_file']}::{item['caller_symbol']} -> {item['callee_file']}::{item['callee_symbol']}")
    _render_section(lines, "## Test Reference Changes", [
        ("added", comparison["test_reference_deltas"]["added"]),
        ("removed", comparison["test_reference_deltas"]["removed"]),
        ("location_changed", comparison["test_reference_deltas"]["location_changed"]),
        ("retained", comparison["test_reference_deltas"]["retained"]),
    ], lambda item: f"{item['test_info']['test_file']}::{item['test_info']['test_name']} -> {item['target_file']}::{item['target_symbol']}")
    _render_section(lines, "## Parse / Resolution Limitations", [
        ("introduced_parse_failures", comparison["parse_failure_deltas"]["introduced_parse_failures"]),
        ("resolved_parse_failures", comparison["parse_failure_deltas"]["resolved_parse_failures"]),
        ("retained_parse_failures", comparison["parse_failure_deltas"]["retained_parse_failures"]),
        ("diagnostic_added", comparison["relationship_diagnostic_deltas"]["added"]),
        ("diagnostic_removed", comparison["relationship_diagnostic_deltas"]["removed"]),
        ("diagnostic_source_location_changed", comparison["relationship_diagnostic_deltas"]["source_location_changed"]),
        ("diagnostic_retained", comparison["relationship_diagnostic_deltas"]["retained"]),
    ], lambda item: item.get("path") or f"{item['reason']}:{item['source_file']}:{item['reference']}")
    lines.append("## Aggregate Delta Counts")
    lines.append(json.dumps(comparison["aggregate_counts"], sort_keys=True))
    lines.append("")
    return "\n".join(lines) + "\n"


def _verify_existing_comparison(comp_dir: Path) -> tuple[dict[str, Any], bytes]:
    names = {child.name for child in comp_dir.iterdir()}
    if names != COMPARE_REQUIRED_FILES:
        raise RuntimeError("comparison artifact set is incomplete or contains unexpected files")
    comparison_json = json.loads((comp_dir / "comparison.json").read_text(encoding="utf-8"))
    comparison_md = (comp_dir / "comparison.md").read_bytes()
    if comparison_json.get("schema_version") != 1:
        raise RuntimeError("unsupported comparison schema version")
    return comparison_json, comparison_md


def compare_snapshots(before_snapshot_id: str, after_snapshot_id: str, repository_path: str, state_root: Path | None = None) -> dict[str, Any]:
    repo_root = validate_git_worktree(Path(repository_path).expanduser().resolve())
    from ..scanner.util import make_repository_id
    repository_id = make_repository_id(repo_root)

    root = (state_root or DEFAULT_STATE_ROOT).expanduser()
    repo_state_dir = root / repository_id
    snapshots_root = repo_state_dir / "snapshots"
    before_dir = snapshots_root / before_snapshot_id
    after_dir = snapshots_root / after_snapshot_id
    if not before_dir.exists() or not after_dir.exists():
        raise RuntimeError("snapshot does not exist")

    _verify_existing_snapshot(before_dir, repository_id)
    _verify_existing_snapshot(after_dir, repository_id)
    before_artifacts = _load_snapshot_artifacts(before_dir)
    after_artifacts = _load_snapshot_artifacts(after_dir)

    comparison_id = derive_comparison_id(before_snapshot_id, after_snapshot_id)
    comparison_payload = build_comparison_payload(repository_id, before_snapshot_id, after_snapshot_id, before_artifacts, after_artifacts, comparison_id)
    comparison_md = render_comparison_markdown(comparison_payload)

    comparisons_root = repo_state_dir / "comparisons"
    comparisons_root.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(mkdtemp(prefix="compare-tmp-", dir=str(comparisons_root)))
    try:
        write_json_deterministic(temp_dir / "comparison.json", comparison_payload)
        (temp_dir / "comparison.md").write_text(comparison_md, encoding="utf-8", newline="\n")
        final_dir = comparisons_root / comparison_id
        if final_dir.exists():
            existing_json, existing_md = _verify_existing_comparison(final_dir)
            if existing_json != comparison_payload or existing_md != comparison_md.encode("utf-8"):
                raise RuntimeError("existing comparison content mismatch for identical comparison id")
            shutil.rmtree(temp_dir)
            return {"comparison_id": comparison_id, "comparison_dir": str(final_dir), "repository_id": repository_id, "repository_root": str(repo_root), "reused_existing": True}
        temp_dir.rename(final_dir)
        return {"comparison_id": comparison_id, "comparison_dir": str(final_dir), "repository_id": repository_id, "repository_root": str(repo_root), "reused_existing": False}
    except Exception:
        if temp_dir.exists():
            shutil.rmtree(temp_dir)
        raise
