from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ParsedPythonFile:
    path: str
    parse_success: bool
    parse_error: str | None
    top_level_functions: list[dict]
    top_level_async_functions: list[dict]
    top_level_classes: list[dict]
    imports: list[dict]
    imported_module_names: list[str]
    imported_symbols: list[dict]
    source_evidence: list[dict]


def _line_span(node: ast.AST) -> tuple[int | None, int | None]:
    return getattr(node, "lineno", None), getattr(node, "end_lineno", None)


def parse_python_file(repo_root: Path, relative_path: str) -> ParsedPythonFile:
    full_path = repo_root / relative_path
    try:
        source_text = full_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        return ParsedPythonFile(
            path=relative_path,
            parse_success=False,
            parse_error=f"UnicodeDecodeError: {exc}",
            top_level_functions=[],
            top_level_async_functions=[],
            top_level_classes=[],
            imports=[],
            imported_module_names=[],
            imported_symbols=[],
            source_evidence=[],
        )

    try:
        tree = ast.parse(source_text, filename=relative_path)
    except SyntaxError as exc:
        message = f"SyntaxError: {exc.msg} (line {exc.lineno}, offset {exc.offset})"
        return ParsedPythonFile(
            path=relative_path,
            parse_success=False,
            parse_error=message,
            top_level_functions=[],
            top_level_async_functions=[],
            top_level_classes=[],
            imports=[],
            imported_module_names=[],
            imported_symbols=[],
            source_evidence=[],
        )

    functions: list[dict] = []
    async_functions: list[dict] = []
    classes: list[dict] = []
    imports: list[dict] = []
    module_names: set[str] = set()
    imported_symbols: list[dict] = []
    source_evidence: list[dict] = []

    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            start_line, end_line = _line_span(node)
            functions.append({"name": node.name, "start_line": start_line, "end_line": end_line})
            continue

        if isinstance(node, ast.AsyncFunctionDef):
            start_line, end_line = _line_span(node)
            async_functions.append({"name": node.name, "start_line": start_line, "end_line": end_line})
            continue

        if isinstance(node, ast.ClassDef):
            start_line, end_line = _line_span(node)
            classes.append({"name": node.name, "start_line": start_line, "end_line": end_line})
            continue

        if isinstance(node, ast.Import):
            imported = []
            for alias in node.names:
                module_names.add(alias.name)
                imported.append({"name": alias.name, "asname": alias.asname})
                imported_symbols.append(
                    {"module": alias.name, "symbol": None, "asname": alias.asname, "kind": "import"}
                )
            imports.append({"kind": "import", "module": None, "level": 0, "names": imported})
            for alias in node.names:
                source_evidence.append(
                    {
                        "evidence_kind": "import",
                        "imported_module": alias.name,
                        "imported_symbol": None,
                        "start_line": getattr(node, "lineno", None),
                        "end_line": getattr(node, "end_lineno", None),
                    }
                )
            continue

        if isinstance(node, ast.ImportFrom):
            imported = []
            mod = node.module
            if mod:
                module_names.add(mod)
            for alias in node.names:
                imported.append({"name": alias.name, "asname": alias.asname})
                imported_symbols.append(
                    {
                        "module": mod,
                        "symbol": alias.name,
                        "asname": alias.asname,
                        "kind": "from_import",
                    }
                )
            imports.append(
                {
                    "kind": "from_import",
                    "module": mod,
                    "level": node.level,
                    "names": imported,
                }
            )
            for alias in node.names:
                source_evidence.append(
                    {
                        "evidence_kind": "import",
                        "imported_module": mod,
                        "imported_symbol": alias.name,
                        "start_line": getattr(node, "lineno", None),
                        "end_line": getattr(node, "end_lineno", None),
                    }
                )

    parent_nodes: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parent_nodes[child] = parent

    def enclosing_symbol_for(node: ast.AST) -> dict | None:
        current = parent_nodes.get(node)
        while current is not None:
            if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                return {
                    "name": current.name,
                    "kind": (
                        "async_function"
                        if isinstance(current, ast.AsyncFunctionDef)
                        else "function"
                        if isinstance(current, ast.FunctionDef)
                        else "class"
                    ),
                    "start_line": getattr(current, "lineno", None),
                    "end_line": getattr(current, "end_lineno", None),
                }
            current = parent_nodes.get(current)
        return None

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            base = node.value.id if isinstance(node.value, ast.Name) else None
            evidence = {
                "evidence_kind": "attribute",
                "attribute_name": node.attr,
                "base": base,
                "expression": f"{base}.{node.attr}" if base else None,
                "start_line": getattr(node, "lineno", None),
                "end_line": getattr(node, "end_lineno", None),
                "enclosing_symbol": enclosing_symbol_for(node),
                "scope": "module_scope" if enclosing_symbol_for(node) is None else "symbol",
            }
            source_evidence.append(evidence)

    functions.sort(key=lambda item: (item["start_line"] or 0, item["name"]))
    async_functions.sort(key=lambda item: (item["start_line"] or 0, item["name"]))
    classes.sort(key=lambda item: (item["start_line"] or 0, item["name"]))

    return ParsedPythonFile(
        path=relative_path,
        parse_success=True,
        parse_error=None,
        top_level_functions=functions,
        top_level_async_functions=async_functions,
        top_level_classes=classes,
        imports=imports,
        imported_module_names=sorted(module_names),
        imported_symbols=imported_symbols,
        source_evidence=sorted(
            source_evidence,
            key=lambda item: (
                item.get("start_line") or 0,
                item.get("end_line") or 0,
                item.get("evidence_kind", ""),
                item.get("attribute_name") or item.get("imported_module") or "",
            ),
        ),
    )


def is_test_like_python_path(relative_path: str) -> bool:
    path = Path(relative_path)
    if path.suffix.lower() != ".py":
        return False
    name = path.name
    if name.startswith("test_") or name.endswith("_test.py"):
        return True
    if "tests" in path.parts:
        return True
    return False


def discover_test_structure(repo_root: Path, relative_path: str) -> dict:
    parsed = parse_python_file(repo_root, relative_path)
    result = {
        "path": relative_path,
        "parse_success": parsed.parse_success,
        "parse_error": parsed.parse_error,
        "classes": [],
        "top_level_test_functions": [],
    }

    if not parsed.parse_success:
        return result

    source_text = (repo_root / relative_path).read_text(encoding="utf-8")
    tree = ast.parse(source_text, filename=relative_path)

    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test"):
            result["top_level_test_functions"].append(
                {
                    "name": node.name,
                    "file": relative_path,
                    "start_line": getattr(node, "lineno", None),
                    "end_line": getattr(node, "end_lineno", None),
                }
            )
            continue

        if isinstance(node, ast.ClassDef):
            methods = []
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name.startswith("test"):
                    methods.append(
                        {
                            "name": child.name,
                            "file": relative_path,
                            "start_line": getattr(child, "lineno", None),
                            "end_line": getattr(child, "end_lineno", None),
                        }
                    )
            methods.sort(key=lambda item: (item["start_line"] or 0, item["name"]))
            result["classes"].append(
                {
                    "name": node.name,
                    "start_line": getattr(node, "lineno", None),
                    "end_line": getattr(node, "end_lineno", None),
                    "test_methods": methods,
                }
            )

    result["classes"].sort(key=lambda item: (item["start_line"] or 0, item["name"]))
    result["top_level_test_functions"].sort(key=lambda item: (item["start_line"] or 0, item["name"]))
    return result
