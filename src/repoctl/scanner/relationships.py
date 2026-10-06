from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path
from typing import Any

from .util import encode_path_order


def _module_candidates_for_path(relative_path: str) -> set[str]:
    path = Path(relative_path)
    if path.suffix != ".py":
        return set()

    candidates: set[str] = set()
    parts = path.parts

    def _candidate_from_parts(p: tuple[str, ...]) -> str | None:
        if not p:
            return None
        if p[-1] == "__init__.py":
            if len(p) == 1:
                return None
            return ".".join(p[:-1])
        stem = Path(p[-1]).stem
        prefix = p[:-1]
        joined = ".".join([*prefix, stem])
        return joined if joined else None

    root_candidate = _candidate_from_parts(parts)
    if root_candidate:
        candidates.add(root_candidate)

    if len(parts) > 1 and parts[0] == "src":
        src_candidate = _candidate_from_parts(parts[1:])
        if src_candidate:
            candidates.add(src_candidate)

    return candidates


def _top_level_symbols(symbol_record: dict[str, Any]) -> dict[str, str]:
    symbols: dict[str, str] = {}
    for item in symbol_record.get("top_level_functions", []):
        symbols[item["name"]] = "function"
    for item in symbol_record.get("top_level_async_functions", []):
        symbols[item["name"]] = "async_function"
    for item in symbol_record.get("top_level_classes", []):
        symbols[item["name"]] = "class"
    return symbols


def _resolve_module_name(
    source_file: str,
    module_text: str | None,
    level: int,
    file_to_module_candidates: dict[str, set[str]],
    module_to_paths: dict[str, set[str]],
) -> dict[str, Any]:
    if level == 0:
        if not module_text:
            return {"status": "unresolved", "reason": "no_tracked_module_match", "candidates": []}
        candidate_module = module_text
    else:
        source_modules = file_to_module_candidates.get(source_file, set())
        if len(source_modules) != 1:
            candidates = sorted(
                {source_file, *[p for mod in source_modules for p in module_to_paths.get(mod, set())]},
                key=encode_path_order,
            )
            return {"status": "ambiguous", "reason": "ambiguous_module", "candidates": candidates}

        source_module = next(iter(source_modules))
        if source_file.endswith("/__init__.py"):
            package_module = source_module
        else:
            package_module = source_module.rsplit(".", 1)[0] if "." in source_module else ""

        package_parts = package_module.split(".") if package_module else []
        up = level - 1
        if up > len(package_parts):
            return {"status": "unresolved", "reason": "no_tracked_module_match", "candidates": []}

        base_parts = package_parts[: len(package_parts) - up]
        suffix_parts = module_text.split(".") if module_text else []
        full_parts = [*base_parts, *suffix_parts]
        candidate_module = ".".join(part for part in full_parts if part)
        if not candidate_module:
            return {"status": "unresolved", "reason": "no_tracked_module_match", "candidates": []}

    paths = module_to_paths.get(candidate_module, set())
    if not paths:
        return {"status": "unresolved", "reason": "no_tracked_module_match", "candidates": []}
    if len(paths) > 1:
        return {
            "status": "ambiguous",
            "reason": "ambiguous_module",
            "candidates": sorted(paths, key=encode_path_order),
            "module": candidate_module,
        }

    target = next(iter(paths))
    return {
        "status": "resolved",
        "reason": None,
        "candidates": [target],
        "module": candidate_module,
        "target_file": target,
    }


def _name_bindings_from_target(node: ast.AST) -> set[str]:
    names: set[str] = set()
    if isinstance(node, ast.Name):
        names.add(node.id)
    elif isinstance(node, (ast.Tuple, ast.List)):
        for elt in node.elts:
            names.update(_name_bindings_from_target(elt))
    return names


def _collect_local_bound_names(func_node: ast.AST) -> set[str]:
    names: set[str] = set()

    if isinstance(func_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        all_args = [
            *func_node.args.posonlyargs,
            *func_node.args.args,
            *func_node.args.kwonlyargs,
        ]
        names.update(a.arg for a in all_args)
        if func_node.args.vararg:
            names.add(func_node.args.vararg.arg)
        if func_node.args.kwarg:
            names.add(func_node.args.kwarg.arg)

    for node in ast.walk(func_node):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node is not func_node:
                names.add(node.name)

        if isinstance(node, ast.Assign):
            for target in node.targets:
                names.update(_name_bindings_from_target(target))
        elif isinstance(node, ast.AnnAssign):
            names.update(_name_bindings_from_target(node.target))
        elif isinstance(node, ast.AugAssign):
            names.update(_name_bindings_from_target(node.target))
        elif isinstance(node, ast.For):
            names.update(_name_bindings_from_target(node.target))
        elif isinstance(node, ast.With):
            for item in node.items:
                if item.optional_vars is not None:
                    names.update(_name_bindings_from_target(item.optional_vars))
        elif isinstance(node, ast.ExceptHandler):
            if node.name:
                names.add(node.name)
        elif isinstance(node, ast.NamedExpr):
            names.update(_name_bindings_from_target(node.target))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name != "*":
                    names.add(alias.asname or alias.name)

    return names


def _module_level_rebound_names(module_tree: ast.Module, imported_names: set[str]) -> set[str]:
    rebound: set[str] = set()
    seen_imported = set(imported_names)

    for node in module_tree.body:
        bound_here: set[str] = set()

        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound_here.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                bound_here.update(_name_bindings_from_target(target))
        elif isinstance(node, ast.AnnAssign):
            bound_here.update(_name_bindings_from_target(node.target))
        elif isinstance(node, ast.AugAssign):
            bound_here.update(_name_bindings_from_target(node.target))
        elif isinstance(node, ast.For):
            bound_here.update(_name_bindings_from_target(node.target))
        elif isinstance(node, ast.With):
            for item in node.items:
                if item.optional_vars is not None:
                    bound_here.update(_name_bindings_from_target(item.optional_vars))
        elif isinstance(node, ast.ExceptHandler):
            if node.name:
                bound_here.add(node.name)
        elif isinstance(node, ast.NamedExpr):
            bound_here.update(_name_bindings_from_target(node.target))

        for name in bound_here:
            if name in seen_imported:
                rebound.add(name)

    return rebound


def _sorted_diagnostics(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        items,
        key=lambda item: (
            item["relationship_kind"],
            encode_path_order(item["source_file"]),
            item["source_line"] or 0,
            item["source_symbol"] or "",
            item["reference"],
            item["reason"],
        ),
    )


def build_relationships(
    repo_root: Path,
    tracked_files: list[str],
    symbols_payload: dict[str, Any],
    tests_payload_v1: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    python_files = sorted([p for p in tracked_files if p.endswith(".py")], key=encode_path_order)

    symbol_by_file = {item["path"]: item for item in symbols_payload.get("python_files", [])}
    top_level_by_file: dict[str, dict[str, str]] = {}
    parse_success_by_file: dict[str, bool] = {}
    for rel_path in python_files:
        rec = symbol_by_file.get(rel_path, {"parse_success": False})
        parse_success_by_file[rel_path] = bool(rec.get("parse_success", False))
        top_level_by_file[rel_path] = _top_level_symbols(rec)

    module_to_paths: dict[str, set[str]] = defaultdict(set)
    file_to_module_candidates: dict[str, set[str]] = {}
    for rel_path in python_files:
        candidates = _module_candidates_for_path(rel_path)
        file_to_module_candidates[rel_path] = candidates
        for module_name in candidates:
            module_to_paths[module_name].add(rel_path)

    trees: dict[str, ast.Module] = {}
    for rel_path in python_files:
        if not parse_success_by_file.get(rel_path, False):
            continue
        source = (repo_root / rel_path).read_text(encoding="utf-8")
        trees[rel_path] = ast.parse(source, filename=rel_path)

    module_edges: list[dict[str, Any]] = []
    imported_symbol_edges: list[dict[str, Any]] = []
    call_edges: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []

    import_bindings_by_file: dict[str, dict[str, dict[str, Any]]] = {}
    test_reference_map: dict[tuple[str, str, int], list[dict[str, Any]]] = {}

    for rel_path in python_files:
        tree = trees.get(rel_path)
        if tree is None:
            import_bindings_by_file[rel_path] = {}
            continue

        bindings: dict[str, dict[str, Any]] = {}
        imported_names: set[str] = set()

        for node in tree.body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    local_name = alias.asname or alias.name.split(".")[0]
                    imported_names.add(local_name)

                    module_result = _resolve_module_name(
                        source_file=rel_path,
                        module_text=alias.name,
                        level=0,
                        file_to_module_candidates=file_to_module_candidates,
                        module_to_paths=module_to_paths,
                    )

                    if module_result["status"] == "resolved":
                        module_edges.append(
                            {
                                "source_file": rel_path,
                                "imported_module_text": alias.name,
                                "target_file": module_result["target_file"],
                                "import_kind": "import_module",
                                "source_line": getattr(node, "lineno", None),
                            }
                        )
                        bindings[local_name] = {
                            "binding_kind": "module_alias",
                            "status": "resolved",
                            "target_file": module_result["target_file"],
                            "target_module": module_result.get("module"),
                            "source_line": getattr(node, "lineno", None),
                            "asname": alias.asname,
                        }
                    else:
                        diagnostics.append(
                            {
                                "relationship_kind": "module_import",
                                "source_file": rel_path,
                                "source_symbol": None,
                                "source_line": getattr(node, "lineno", None),
                                "reference": alias.name,
                                "reason": module_result["reason"],
                                "candidates": module_result["candidates"],
                            }
                        )
                        bindings[local_name] = {
                            "binding_kind": "module_alias",
                            "status": module_result["status"],
                            "reason": module_result["reason"],
                            "candidates": module_result["candidates"],
                            "source_line": getattr(node, "lineno", None),
                            "asname": alias.asname,
                        }

            elif isinstance(node, ast.ImportFrom):
                module_text = node.module
                module_result = _resolve_module_name(
                    source_file=rel_path,
                    module_text=module_text,
                    level=node.level,
                    file_to_module_candidates=file_to_module_candidates,
                    module_to_paths=module_to_paths,
                )

                as_written = ("." * node.level) + (module_text or "")

                if module_result["status"] == "resolved":
                    module_edges.append(
                        {
                            "source_file": rel_path,
                            "imported_module_text": as_written,
                            "target_file": module_result["target_file"],
                            "import_kind": "from_module",
                            "source_line": getattr(node, "lineno", None),
                        }
                    )
                else:
                    diagnostics.append(
                        {
                            "relationship_kind": "module_import",
                            "source_file": rel_path,
                            "source_symbol": None,
                            "source_line": getattr(node, "lineno", None),
                            "reference": as_written,
                            "reason": module_result["reason"],
                            "candidates": module_result["candidates"],
                        }
                    )

                for alias in node.names:
                    if alias.name == "*":
                        diagnostics.append(
                            {
                                "relationship_kind": "imported_symbol",
                                "source_file": rel_path,
                                "source_symbol": None,
                                "source_line": getattr(node, "lineno", None),
                                "reference": f"{as_written}.*",
                                "reason": "wildcard_import",
                                "candidates": [],
                            }
                        )
                        continue

                    local_name = alias.asname or alias.name
                    imported_names.add(local_name)

                    binding = {
                        "binding_kind": "imported_symbol",
                        "status": module_result["status"],
                        "target_module": module_result.get("module"),
                        "target_file": module_result.get("target_file"),
                        "target_symbol": alias.name,
                        "local_name": local_name,
                        "asname": alias.asname,
                        "source_line": getattr(node, "lineno", None),
                    }

                    if module_result["status"] != "resolved":
                        binding["reason"] = module_result["reason"]
                        binding["candidates"] = module_result["candidates"]
                        diagnostics.append(
                            {
                                "relationship_kind": "imported_symbol",
                                "source_file": rel_path,
                                "source_symbol": None,
                                "source_line": getattr(node, "lineno", None),
                                "reference": f"{as_written}:{alias.name}",
                                "reason": module_result["reason"],
                                "candidates": module_result["candidates"],
                            }
                        )
                    else:
                        target_file = module_result["target_file"]
                        if not parse_success_by_file.get(target_file, False):
                            binding["status"] = "unresolved"
                            binding["reason"] = "target_parse_failure"
                            binding["candidates"] = [target_file]
                            diagnostics.append(
                                {
                                    "relationship_kind": "imported_symbol",
                                    "source_file": rel_path,
                                    "source_symbol": None,
                                    "source_line": getattr(node, "lineno", None),
                                    "reference": f"{as_written}:{alias.name}",
                                    "reason": "target_parse_failure",
                                    "candidates": [target_file],
                                }
                            )
                        else:
                            symbol_kind = top_level_by_file.get(target_file, {}).get(alias.name)
                            if not symbol_kind:
                                binding["status"] = "unresolved"
                                binding["reason"] = "unresolved_symbol"
                                binding["candidates"] = [target_file]
                                diagnostics.append(
                                    {
                                        "relationship_kind": "imported_symbol",
                                        "source_file": rel_path,
                                        "source_symbol": None,
                                        "source_line": getattr(node, "lineno", None),
                                        "reference": f"{as_written}:{alias.name}",
                                        "reason": "unresolved_symbol",
                                        "candidates": [target_file],
                                    }
                                )
                            else:
                                binding["status"] = "resolved"
                                binding["resolved_symbol_kind"] = symbol_kind
                                imported_symbol_edges.append(
                                    {
                                        "source_file": rel_path,
                                        "local_name": local_name,
                                        "source_module": module_result.get("module"),
                                        "source_file_target": target_file,
                                        "target_symbol": alias.name,
                                        "target_symbol_kind": symbol_kind,
                                        "import_line": getattr(node, "lineno", None),
                                        "alias": alias.asname,
                                    }
                                )

                    bindings[local_name] = binding

        import_bindings_by_file[rel_path] = bindings

        module_rebound = _module_level_rebound_names(tree, imported_names)

        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue

            caller_name = node.name
            caller_kind = "async_function" if isinstance(node, ast.AsyncFunctionDef) else "function"
            local_bound = _collect_local_bound_names(node)

            for sub in ast.walk(node):
                if not isinstance(sub, ast.Call):
                    continue

                callee_expr = sub.func
                call_line = getattr(sub, "lineno", None)

                if isinstance(callee_expr, ast.Name):
                    callee_name = callee_expr.id
                    if callee_name not in bindings and callee_name not in top_level_by_file.get(rel_path, {}):
                        continue

                    if callee_name in local_bound or callee_name in module_rebound:
                        diagnostics.append(
                            {
                                "relationship_kind": "call",
                                "source_file": rel_path,
                                "source_symbol": caller_name,
                                "source_line": call_line,
                                "reference": callee_name,
                                "reason": "shadowed_or_rebound",
                                "candidates": [],
                            }
                        )
                        continue

                    if callee_name in top_level_by_file.get(rel_path, {}):
                        callee_kind = top_level_by_file[rel_path][callee_name]
                        if callee_kind in ("function", "async_function"):
                            call_edges.append(
                                {
                                    "caller_file": rel_path,
                                    "caller_symbol": caller_name,
                                    "caller_symbol_kind": caller_kind,
                                    "callee_file": rel_path,
                                    "callee_symbol": callee_name,
                                    "callee_symbol_kind": callee_kind,
                                    "call_line": call_line,
                                    "resolution_kind": "same_module",
                                }
                            )
                        continue

                    binding = bindings.get(callee_name)
                    if not binding:
                        continue

                    if binding.get("binding_kind") != "imported_symbol":
                        continue

                    if binding.get("status") != "resolved":
                        diagnostics.append(
                            {
                                "relationship_kind": "call",
                                "source_file": rel_path,
                                "source_symbol": caller_name,
                                "source_line": call_line,
                                "reference": callee_name,
                                "reason": binding.get("reason", "unresolved_symbol"),
                                "candidates": binding.get("candidates", []),
                            }
                        )
                        continue

                    callee_kind = binding.get("resolved_symbol_kind")
                    if callee_kind not in ("function", "async_function"):
                        diagnostics.append(
                            {
                                "relationship_kind": "call",
                                "source_file": rel_path,
                                "source_symbol": caller_name,
                                "source_line": call_line,
                                "reference": callee_name,
                                "reason": "unresolved_symbol",
                                "candidates": [binding.get("target_file")],
                            }
                        )
                        continue

                    call_edges.append(
                        {
                            "caller_file": rel_path,
                            "caller_symbol": caller_name,
                            "caller_symbol_kind": caller_kind,
                            "callee_file": binding.get("target_file"),
                            "callee_symbol": binding.get("target_symbol"),
                            "callee_symbol_kind": callee_kind,
                            "call_line": call_line,
                            "resolution_kind": "imported_symbol",
                        }
                    )

                elif isinstance(callee_expr, ast.Attribute) and isinstance(callee_expr.value, ast.Name):
                    base_name = callee_expr.value.id
                    attribute_name = callee_expr.attr
                    binding = bindings.get(base_name)
                    if not binding or binding.get("binding_kind") != "module_alias":
                        continue

                    if base_name in local_bound or base_name in module_rebound:
                        diagnostics.append(
                            {
                                "relationship_kind": "call",
                                "source_file": rel_path,
                                "source_symbol": caller_name,
                                "source_line": call_line,
                                "reference": f"{base_name}.{attribute_name}",
                                "reason": "shadowed_or_rebound",
                                "candidates": [],
                            }
                        )
                        continue

                    if binding.get("status") != "resolved":
                        diagnostics.append(
                            {
                                "relationship_kind": "call",
                                "source_file": rel_path,
                                "source_symbol": caller_name,
                                "source_line": call_line,
                                "reference": f"{base_name}.{attribute_name}",
                                "reason": binding.get("reason", "unresolved_symbol"),
                                "candidates": binding.get("candidates", []),
                            }
                        )
                        continue

                    target_file = binding.get("target_file")
                    if not parse_success_by_file.get(target_file, False):
                        diagnostics.append(
                            {
                                "relationship_kind": "call",
                                "source_file": rel_path,
                                "source_symbol": caller_name,
                                "source_line": call_line,
                                "reference": f"{base_name}.{attribute_name}",
                                "reason": "target_parse_failure",
                                "candidates": [target_file],
                            }
                        )
                        continue

                    callee_kind = top_level_by_file.get(target_file, {}).get(attribute_name)
                    if callee_kind not in ("function", "async_function"):
                        diagnostics.append(
                            {
                                "relationship_kind": "call",
                                "source_file": rel_path,
                                "source_symbol": caller_name,
                                "source_line": call_line,
                                "reference": f"{base_name}.{attribute_name}",
                                "reason": "unresolved_symbol",
                                "candidates": [target_file],
                            }
                        )
                        continue

                    call_edges.append(
                        {
                            "caller_file": rel_path,
                            "caller_symbol": caller_name,
                            "caller_symbol_kind": caller_kind,
                            "callee_file": target_file,
                            "callee_symbol": attribute_name,
                            "callee_symbol_kind": callee_kind,
                            "call_line": call_line,
                            "resolution_kind": "imported_module_attribute",
                        }
                    )

    # Extend tests.json to v2 with deterministic resolved references.
    tests_v2: dict[str, Any] = {
        **tests_payload_v1,
        "schema_version": 2,
    }
    total_refs = 0

    for test_file in tests_v2.get("test_files", []):
        path = test_file["path"]
        tree = trees.get(path)
        bindings = import_bindings_by_file.get(path, {})
        module_rebound = set()
        if tree is not None:
            imported_names = set(bindings.keys())
            module_rebound = _module_level_rebound_names(tree, imported_names)

        class_nodes = {}
        top_level_nodes = {}
        if tree is not None:
            for node in tree.body:
                if isinstance(node, ast.ClassDef):
                    class_nodes[node.name] = node
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    top_level_nodes[(node.name, getattr(node, "lineno", None))] = node

        for cls in test_file.get("classes", []):
            class_node = class_nodes.get(cls["name"])
            method_lookup = {}
            if class_node is not None:
                for child in class_node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        method_lookup[(child.name, getattr(child, "lineno", None))] = child

            for method in cls.get("test_methods", []):
                node = method_lookup.get((method["name"], method["start_line"]))
                refs: list[dict[str, Any]] = []
                if node is not None:
                    local_bound = _collect_local_bound_names(node)
                    # Name references to resolved imported symbols.
                    for sub in ast.walk(node):
                        if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
                            binding = bindings.get(sub.id)
                            if not binding or binding.get("binding_kind") != "imported_symbol":
                                continue
                            if sub.id in local_bound or sub.id in module_rebound:
                                diagnostics.append(
                                    {
                                        "relationship_kind": "test_reference",
                                        "source_file": path,
                                        "source_symbol": method["name"],
                                        "source_line": getattr(sub, "lineno", None),
                                        "reference": sub.id,
                                        "reason": "shadowed_or_rebound",
                                        "candidates": [],
                                    }
                                )
                                continue
                            if binding.get("status") == "resolved":
                                refs.append(
                                    {
                                        "target_file": binding.get("target_file"),
                                        "target_symbol": binding.get("target_symbol"),
                                        "target_symbol_kind": binding.get("resolved_symbol_kind"),
                                        "reference_kind": "import",
                                        "source_line": getattr(sub, "lineno", None),
                                    }
                                )
                            else:
                                diagnostics.append(
                                    {
                                        "relationship_kind": "test_reference",
                                        "source_file": path,
                                        "source_symbol": method["name"],
                                        "source_line": getattr(sub, "lineno", None),
                                        "reference": sub.id,
                                        "reason": binding.get("reason", "unresolved_symbol"),
                                        "candidates": binding.get("candidates", []),
                                    }
                                )

                        if isinstance(sub, ast.Call):
                            call_line = getattr(sub, "lineno", None)
                            if isinstance(sub.func, ast.Name):
                                binding = bindings.get(sub.func.id)
                                if not binding or binding.get("binding_kind") != "imported_symbol":
                                    continue
                                if sub.func.id in local_bound or sub.func.id in module_rebound:
                                    diagnostics.append(
                                        {
                                            "relationship_kind": "test_reference",
                                            "source_file": path,
                                            "source_symbol": method["name"],
                                            "source_line": call_line,
                                            "reference": sub.func.id,
                                            "reason": "shadowed_or_rebound",
                                            "candidates": [],
                                        }
                                    )
                                    continue
                                if binding.get("status") == "resolved":
                                    refs.append(
                                        {
                                            "target_file": binding.get("target_file"),
                                            "target_symbol": binding.get("target_symbol"),
                                            "target_symbol_kind": binding.get("resolved_symbol_kind"),
                                            "reference_kind": "call",
                                            "source_line": call_line,
                                        }
                                    )
                                else:
                                    diagnostics.append(
                                        {
                                            "relationship_kind": "test_reference",
                                            "source_file": path,
                                            "source_symbol": method["name"],
                                            "source_line": call_line,
                                            "reference": sub.func.id,
                                            "reason": binding.get("reason", "unresolved_symbol"),
                                            "candidates": binding.get("candidates", []),
                                        }
                                    )

                            elif isinstance(sub.func, ast.Attribute) and isinstance(sub.func.value, ast.Name):
                                base_name = sub.func.value.id
                                attr_name = sub.func.attr
                                binding = bindings.get(base_name)
                                if not binding or binding.get("binding_kind") != "module_alias":
                                    continue
                                if base_name in local_bound or base_name in module_rebound:
                                    diagnostics.append(
                                        {
                                            "relationship_kind": "test_reference",
                                            "source_file": path,
                                            "source_symbol": method["name"],
                                            "source_line": call_line,
                                            "reference": f"{base_name}.{attr_name}",
                                            "reason": "shadowed_or_rebound",
                                            "candidates": [],
                                        }
                                    )
                                    continue

                                if binding.get("status") != "resolved":
                                    diagnostics.append(
                                        {
                                            "relationship_kind": "test_reference",
                                            "source_file": path,
                                            "source_symbol": method["name"],
                                            "source_line": call_line,
                                            "reference": f"{base_name}.{attr_name}",
                                            "reason": binding.get("reason", "unresolved_symbol"),
                                            "candidates": binding.get("candidates", []),
                                        }
                                    )
                                    continue

                                target_file = binding.get("target_file")
                                if not parse_success_by_file.get(target_file, False):
                                    diagnostics.append(
                                        {
                                            "relationship_kind": "test_reference",
                                            "source_file": path,
                                            "source_symbol": method["name"],
                                            "source_line": call_line,
                                            "reference": f"{base_name}.{attr_name}",
                                            "reason": "target_parse_failure",
                                            "candidates": [target_file],
                                        }
                                    )
                                    continue

                                symbol_kind = top_level_by_file.get(target_file, {}).get(attr_name)
                                if not symbol_kind:
                                    diagnostics.append(
                                        {
                                            "relationship_kind": "test_reference",
                                            "source_file": path,
                                            "source_symbol": method["name"],
                                            "source_line": call_line,
                                            "reference": f"{base_name}.{attr_name}",
                                            "reason": "unresolved_symbol",
                                            "candidates": [target_file],
                                        }
                                    )
                                    continue

                                refs.append(
                                    {
                                        "target_file": target_file,
                                        "target_symbol": attr_name,
                                        "target_symbol_kind": symbol_kind,
                                        "reference_kind": "call",
                                        "source_line": call_line,
                                    }
                                )

                unique = {
                    (
                        item["target_file"],
                        item["target_symbol"],
                        item["target_symbol_kind"],
                        item["reference_kind"],
                        item["source_line"],
                    ): item
                    for item in refs
                }
                ordered_refs = sorted(
                    unique.values(),
                    key=lambda item: (
                        encode_path_order(item["target_file"]),
                        item["target_symbol"],
                        item["reference_kind"],
                        item["source_line"] or 0,
                    ),
                )
                method["resolved_references"] = ordered_refs
                total_refs += len(ordered_refs)

        for func in test_file.get("top_level_test_functions", []):
            node = top_level_nodes.get((func["name"], func["start_line"]))
            refs: list[dict[str, Any]] = []
            if node is not None:
                local_bound = _collect_local_bound_names(node)
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
                        binding = bindings.get(sub.id)
                        if not binding or binding.get("binding_kind") != "imported_symbol":
                            continue
                        if sub.id in local_bound or sub.id in module_rebound:
                            diagnostics.append(
                                {
                                    "relationship_kind": "test_reference",
                                    "source_file": path,
                                    "source_symbol": func["name"],
                                    "source_line": getattr(sub, "lineno", None),
                                    "reference": sub.id,
                                    "reason": "shadowed_or_rebound",
                                    "candidates": [],
                                }
                            )
                            continue
                        if binding.get("status") == "resolved":
                            refs.append(
                                {
                                    "target_file": binding.get("target_file"),
                                    "target_symbol": binding.get("target_symbol"),
                                    "target_symbol_kind": binding.get("resolved_symbol_kind"),
                                    "reference_kind": "import",
                                    "source_line": getattr(sub, "lineno", None),
                                }
                            )
                        else:
                            diagnostics.append(
                                {
                                    "relationship_kind": "test_reference",
                                    "source_file": path,
                                    "source_symbol": func["name"],
                                    "source_line": getattr(sub, "lineno", None),
                                    "reference": sub.id,
                                    "reason": binding.get("reason", "unresolved_symbol"),
                                    "candidates": binding.get("candidates", []),
                                }
                            )

                for sub in ast.walk(node):
                    if not isinstance(sub, ast.Call):
                        continue
                    call_line = getattr(sub, "lineno", None)
                    if isinstance(sub.func, ast.Name):
                        binding = bindings.get(sub.func.id)
                        if not binding or binding.get("binding_kind") != "imported_symbol":
                            continue
                        if sub.func.id in local_bound or sub.func.id in module_rebound:
                            diagnostics.append(
                                {
                                    "relationship_kind": "test_reference",
                                    "source_file": path,
                                    "source_symbol": func["name"],
                                    "source_line": call_line,
                                    "reference": sub.func.id,
                                    "reason": "shadowed_or_rebound",
                                    "candidates": [],
                                }
                            )
                            continue
                        if binding.get("status") == "resolved":
                            refs.append(
                                {
                                    "target_file": binding.get("target_file"),
                                    "target_symbol": binding.get("target_symbol"),
                                    "target_symbol_kind": binding.get("resolved_symbol_kind"),
                                    "reference_kind": "call",
                                    "source_line": call_line,
                                }
                            )
                        else:
                            diagnostics.append(
                                {
                                    "relationship_kind": "test_reference",
                                    "source_file": path,
                                    "source_symbol": func["name"],
                                    "source_line": call_line,
                                    "reference": sub.func.id,
                                    "reason": binding.get("reason", "unresolved_symbol"),
                                    "candidates": binding.get("candidates", []),
                                }
                            )

                    elif isinstance(sub.func, ast.Attribute) and isinstance(sub.func.value, ast.Name):
                        base_name = sub.func.value.id
                        attr_name = sub.func.attr
                        binding = bindings.get(base_name)
                        if not binding or binding.get("binding_kind") != "module_alias":
                            continue
                        if base_name in local_bound or base_name in module_rebound:
                            diagnostics.append(
                                {
                                    "relationship_kind": "test_reference",
                                    "source_file": path,
                                    "source_symbol": func["name"],
                                    "source_line": call_line,
                                    "reference": f"{base_name}.{attr_name}",
                                    "reason": "shadowed_or_rebound",
                                    "candidates": [],
                                }
                            )
                            continue
                        if binding.get("status") != "resolved":
                            diagnostics.append(
                                {
                                    "relationship_kind": "test_reference",
                                    "source_file": path,
                                    "source_symbol": func["name"],
                                    "source_line": call_line,
                                    "reference": f"{base_name}.{attr_name}",
                                    "reason": binding.get("reason", "unresolved_symbol"),
                                    "candidates": binding.get("candidates", []),
                                }
                            )
                            continue
                        target_file = binding.get("target_file")
                        if not parse_success_by_file.get(target_file, False):
                            diagnostics.append(
                                {
                                    "relationship_kind": "test_reference",
                                    "source_file": path,
                                    "source_symbol": func["name"],
                                    "source_line": call_line,
                                    "reference": f"{base_name}.{attr_name}",
                                    "reason": "target_parse_failure",
                                    "candidates": [target_file],
                                }
                            )
                            continue
                        symbol_kind = top_level_by_file.get(target_file, {}).get(attr_name)
                        if not symbol_kind:
                            diagnostics.append(
                                {
                                    "relationship_kind": "test_reference",
                                    "source_file": path,
                                    "source_symbol": func["name"],
                                    "source_line": call_line,
                                    "reference": f"{base_name}.{attr_name}",
                                    "reason": "unresolved_symbol",
                                    "candidates": [target_file],
                                }
                            )
                            continue
                        refs.append(
                            {
                                "target_file": target_file,
                                "target_symbol": attr_name,
                                "target_symbol_kind": symbol_kind,
                                "reference_kind": "call",
                                "source_line": call_line,
                            }
                        )

            unique = {
                (
                    item["target_file"],
                    item["target_symbol"],
                    item["target_symbol_kind"],
                    item["reference_kind"],
                    item["source_line"],
                ): item
                for item in refs
            }
            ordered_refs = sorted(
                unique.values(),
                key=lambda item: (
                    encode_path_order(item["target_file"]),
                    item["target_symbol"],
                    item["reference_kind"],
                    item["source_line"] or 0,
                ),
            )
            func["resolved_references"] = ordered_refs
            total_refs += len(ordered_refs)

    tests_v2["test_reference_count"] = total_refs

    module_edges = sorted(
        module_edges,
        key=lambda item: (
            encode_path_order(item["source_file"]),
            item["source_line"] or 0,
            item["import_kind"],
            item["imported_module_text"],
            encode_path_order(item["target_file"]),
        ),
    )
    imported_symbol_edges = sorted(
        imported_symbol_edges,
        key=lambda item: (
            encode_path_order(item["source_file"]),
            item["import_line"] or 0,
            item["local_name"],
            encode_path_order(item["source_file_target"]),
            item["target_symbol"],
        ),
    )
    call_edges = sorted(
        call_edges,
        key=lambda item: (
            encode_path_order(item["caller_file"]),
            item["call_line"] or 0,
            item["caller_symbol"],
            encode_path_order(item["callee_file"]),
            item["callee_symbol"],
            item["resolution_kind"],
        ),
    )

    diagnostics = _sorted_diagnostics(diagnostics)

    module_identity = []
    for rel_path in sorted(file_to_module_candidates.keys(), key=encode_path_order):
        module_identity.append(
            {
                "path": rel_path,
                "module_candidates": sorted(file_to_module_candidates[rel_path]),
            }
        )

    dependencies_payload = {
        "schema_version": 1,
        "module_resolution": {
            "file_module_candidates": module_identity,
        },
        "module_dependencies": module_edges,
        "imported_symbol_relationships": imported_symbol_edges,
        "call_relationships": call_edges,
        "unresolved_relationships": diagnostics,
        "counts": {
            "module_dependency_count": len(module_edges),
            "imported_symbol_relationship_count": len(imported_symbol_edges),
            "call_relationship_count": len(call_edges),
            "unresolved_relationship_count": len(diagnostics),
            "ambiguous_relationship_count": sum(1 for d in diagnostics if d["reason"] == "ambiguous_module"),
            "target_parse_failure_count": sum(1 for d in diagnostics if d["reason"] == "target_parse_failure"),
        },
    }

    return dependencies_payload, tests_v2
