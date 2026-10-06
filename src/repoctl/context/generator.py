from __future__ import annotations

import ast
import shutil
from pathlib import Path
from tempfile import mkdtemp
from typing import Any

from ..scanner.util import encode_path_order, write_json_deterministic
from .policy import (
    DIAGNOSTIC_ALLOWLIST,
    MATCH_STATUS_MATCHED,
    MATCH_STATUS_NO_MATCHES,
    MAX_FILES,
    MAX_RELATIONSHIPS,
    MAX_SEEDS,
    MAX_SYMBOLS,
    MAX_TEST_REFERENCES,
    RELATIONSHIP_TYPE_ORDER,
    SELECTION_CONTRACT_VERSION,
    WEIGHT_EXACT_PATH_OR_MODULE_COMPONENT,
    WEIGHT_EXACT_SYMBOL_NAME,
    WEIGHT_EXACT_ATTRIBUTE,
    WEIGHT_EXACT_IMPORT_OR_DEPENDENCY,
    WEIGHT_FULL_QUERY_SUBSTRING,
    WEIGHT_MULTI_TOKEN,
    WEIGHT_SOURCE_TOKEN,
    WEIGHT_SINGLE_TOKEN,
    QueryInfo,
    build_query_info,
    count_token_matches,
    tokenize_text,
)


def _safe_python_path(path: str) -> bool:
    name = Path(path).name.casefold()
    blocked_fragments = (".env", "credential", "secret", "service_account", "private_key", "token")
    return Path(path).suffix.casefold() == ".py" and not any(fragment in name for fragment in blocked_fragments)


def _line_key(value: int | None) -> int:
    return value if value is not None else 2**31 - 1


def _cs_key(value: str | None) -> bytes:
    return value.encode("utf-8") if value is not None else b""


def _symbol_identity(path: str, kind: str, name: str, start_line: int | None) -> tuple[str, str, str, int | None]:
    return (path, kind, name, start_line)


def _collect_module_candidates(dependencies_payload: dict[str, Any]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for entry in dependencies_payload.get("module_resolution", {}).get("file_module_candidates", []):
        out[entry["path"]] = list(entry.get("module_candidates", []))
    return out


def _match_seed(
    query_info: QueryInfo,
    kind: str,
    path: str,
    module_candidates: list[str],
    symbol_name: str | None,
    symbol_kind: str | None,
    start_line: int | None,
) -> dict[str, Any] | None:
    fields = [path, *module_candidates]
    components: list[str] = []
    for field in fields:
        components.extend(tokenize_text(field))

    if symbol_name is not None:
        fields.append(symbol_name)
        components.extend(tokenize_text(symbol_name))

    field_texts = [field.casefold() for field in fields]
    canonical_query = query_info.canonical_casefolded_query

    exact_symbol_match = symbol_name is not None and symbol_name.casefold() == canonical_query
    exact_component_match = canonical_query in components
    full_substring_match = any(canonical_query in text for text in field_texts)
    matched_tokens = count_token_matches(query_info.query_tokens, field_texts)

    if exact_symbol_match:
        score = WEIGHT_EXACT_SYMBOL_NAME + len(matched_tokens)
        reason = f"exact symbol match: {symbol_name}"
    elif exact_component_match:
        score = WEIGHT_EXACT_PATH_OR_MODULE_COMPONENT + len(matched_tokens)
        reason = f"exact path/module component match: {canonical_query}"
    elif full_substring_match:
        score = WEIGHT_FULL_QUERY_SUBSTRING + len(matched_tokens)
        reason = f"full query substring match: {query_info.canonical_query}"
    elif len(matched_tokens) >= 2:
        score = WEIGHT_MULTI_TOKEN + len(matched_tokens)
        reason = f"multiple token matches: {', '.join(sorted(matched_tokens))}"
    elif len(matched_tokens) == 1:
        score = WEIGHT_SINGLE_TOKEN + 1
        reason = f"token match: {next(iter(matched_tokens))}"
    else:
        return None

    if kind == "file":
        identity = ("file", path)
    else:
        identity = ("symbol", path, symbol_kind, symbol_name, start_line)

    return {
        "seed_type": kind,
        "identity": identity,
        "path": path,
        "symbol_kind": symbol_kind,
        "symbol_name": symbol_name,
        "start_line": start_line,
        "module_candidates": module_candidates,
        "score": score,
        "matched_token_count": len(matched_tokens),
        "matched_tokens": sorted(matched_tokens),
        "reason": reason,
    }


def _seed_sort_key(item: dict[str, Any]) -> tuple[Any, ...]:
    seed_type_order = 0 if item["seed_type"] == "symbol" else 1
    return (
        -item["score"],
        -item["matched_token_count"],
        seed_type_order,
        encode_path_order(item["path"]),
        _line_key(item["start_line"]),
        _cs_key(item["symbol_name"]),
    )


def _query_string_evidence(repository_payload: dict[str, Any], symbols_payload: dict[str, Any], query_info: QueryInfo) -> list[dict[str, Any]]:
    repo_root = Path(repository_payload["repository_root"])
    evidence: list[dict[str, Any]] = []
    for file_record in symbols_payload.get("python_files", []):
        path = file_record["path"]
        if not _safe_python_path(path):
            continue
        source_path = repo_root / path
        try:
            source = source_path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=path)
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue

        docstring_nodes: set[ast.AST] = set()
        for parent in ast.walk(tree):
            body = getattr(parent, "body", None)
            if isinstance(body, list) and body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant):
                if isinstance(body[0].value.value, str):
                    docstring_nodes.add(body[0].value)

        parents: dict[ast.AST, ast.AST] = {}
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                parents[child] = parent

        def enclosing(node: ast.AST) -> dict[str, Any] | None:
            current = parents.get(node)
            while current is not None:
                if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    kind = "class" if isinstance(current, ast.ClassDef) else "async_function" if isinstance(current, ast.AsyncFunctionDef) else "function"
                    return {"name": current.name, "kind": kind, "start_line": current.lineno, "end_line": current.end_lineno}
                current = parents.get(current)
            return None

        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str) or node.value in docstring_nodes:
                continue
            if "\n" in node.value or len(node.value) > 256:
                continue
            folded = node.value.casefold()
            matched_tokens = sorted(count_token_matches(query_info.query_tokens, [folded]))
            if not matched_tokens:
                continue
            scope = enclosing(node)
            evidence.append(
                {
                    "evidence_kind": "string_literal",
                    "path": path,
                    "start_line": getattr(node, "lineno", None),
                    "end_line": getattr(node, "end_lineno", None),
                    "matched_tokens": matched_tokens,
                    "enclosing_symbol": scope,
                    "scope": "module_scope" if scope is None else "symbol",
                    "score": WEIGHT_SOURCE_TOKEN + len(matched_tokens),
                }
            )
    return evidence


def _build_seeds(repository_payload: dict[str, Any], symbols_payload: dict[str, Any], dependencies_payload: dict[str, Any], query_info: QueryInfo) -> dict[str, Any]:
    module_candidates_by_path = _collect_module_candidates(dependencies_payload)

    raw_candidates: list[dict[str, Any]] = []
    for file_record in symbols_payload.get("python_files", []):
        path = file_record["path"]
        module_candidates = module_candidates_by_path.get(path, [])

        seed = _match_seed(
            query_info=query_info,
            kind="file",
            path=path,
            module_candidates=module_candidates,
            symbol_name=None,
            symbol_kind=None,
            start_line=None,
        )
        if seed:
            raw_candidates.append(seed)

        if not file_record.get("parse_success", False):
            continue

        for symbol_kind, field_name in (
            ("function", "top_level_functions"),
            ("async_function", "top_level_async_functions"),
            ("class", "top_level_classes"),
        ):
            for symbol in file_record.get(field_name, []):
                symbol_seed = _match_seed(
                    query_info=query_info,
                    kind="symbol",
                    path=path,
                    module_candidates=module_candidates,
                    symbol_name=symbol["name"],
                    symbol_kind=symbol_kind,
                    start_line=symbol.get("start_line"),
                )
                if symbol_seed:
                    raw_candidates.append(symbol_seed)

        for evidence in file_record.get("source_evidence", []):
            if not _safe_python_path(path):
                continue
            fields = [
                evidence.get("imported_module") or "",
                evidence.get("imported_symbol") or "",
                evidence.get("attribute_name") or "",
                evidence.get("expression") or "",
            ]
            matched_tokens = count_token_matches(query_info.query_tokens, [field.casefold() for field in fields if field])
            if not matched_tokens:
                continue
            kind = evidence["evidence_kind"]
            score = WEIGHT_EXACT_IMPORT_OR_DEPENDENCY if kind == "import" else WEIGHT_EXACT_ATTRIBUTE
            raw_candidates.append(
                {
                    "seed_type": "evidence",
                    "identity": ("evidence", path, kind, evidence.get("start_line"), evidence.get("imported_module"), evidence.get("attribute_name")),
                    "path": path,
                    "symbol_kind": None,
                    "symbol_name": None,
                    "start_line": evidence.get("start_line"),
                    "evidence_kind": kind,
                    "evidence": {**evidence, "matched_tokens": sorted(matched_tokens)},
                    "score": score + len(matched_tokens),
                    "matched_token_count": len(matched_tokens),
                    "matched_tokens": sorted(matched_tokens),
                    "reason": f"{kind} evidence: {', '.join(sorted(matched_tokens))}",
                }
            )

    for evidence in _query_string_evidence(repository_payload, symbols_payload, query_info):
        raw_candidates.append(
            {
                "seed_type": "evidence",
                "identity": ("evidence", evidence["path"], "string_literal", evidence["start_line"]),
                "path": evidence["path"],
                "symbol_kind": None,
                "symbol_name": None,
                "start_line": evidence["start_line"],
                "evidence_kind": "string_literal",
                "evidence": evidence,
                "score": evidence["score"],
                "matched_token_count": len(evidence["matched_tokens"]),
                "matched_tokens": evidence["matched_tokens"],
                "reason": f"string_literal evidence: {', '.join(evidence['matched_tokens'])}",
            }
            )

    for requirement in repository_payload.get("requirements", []):
        for line_number, declaration in enumerate(requirement.get("declarations", []), start=1):
            matched_tokens = count_token_matches(query_info.query_tokens, [declaration.casefold()])
            if not matched_tokens:
                continue
            raw_candidates.append(
                {
                    "seed_type": "evidence",
                    "identity": ("evidence", requirement["path"], "dependency", line_number, declaration),
                    "path": requirement["path"],
                    "symbol_kind": None,
                    "symbol_name": None,
                    "start_line": line_number,
                    "evidence_kind": "dependency",
                    "evidence": {"evidence_kind": "dependency", "path": requirement["path"], "start_line": line_number, "matched_tokens": sorted(matched_tokens)},
                    "score": WEIGHT_EXACT_IMPORT_OR_DEPENDENCY + len(matched_tokens),
                    "matched_token_count": len(matched_tokens),
                    "matched_tokens": sorted(matched_tokens),
                    "reason": f"dependency evidence: {', '.join(sorted(matched_tokens))}",
                }
            )

    evidence_by_file: dict[str, list[dict[str, Any]]] = {}
    for candidate in raw_candidates:
        if candidate["seed_type"] == "evidence":
            evidence_by_file.setdefault(candidate["path"], []).append(candidate)
    required_tokens = set(query_info.query_tokens)
    if len(required_tokens) > 1:
        for candidates in evidence_by_file.values():
            file_tokens = {token for candidate in candidates for token in candidate["matched_tokens"]}
            if required_tokens.issubset(file_tokens):
                for candidate in candidates:
                    candidate["score"] += WEIGHT_MULTI_TOKEN
                    candidate["reason"] = f"same-file token-AND evidence: {', '.join(candidate['matched_tokens'])}"

    # Deduplicate by typed seed identity, keeping the strongest record.
    best_by_identity: dict[tuple[Any, ...], dict[str, Any]] = {}
    for seed in sorted(raw_candidates, key=_seed_sort_key):
        key = tuple(seed["identity"])
        if key not in best_by_identity:
            best_by_identity[key] = seed

    all_candidates = sorted(best_by_identity.values(), key=_seed_sort_key)
    selected = all_candidates[:MAX_SEEDS]

    return {
        "all_candidates": all_candidates,
        "selected": selected,
        "total_candidate_count": len(all_candidates),
        "selected_count": len(selected),
        "truncated": len(all_candidates) > MAX_SEEDS,
        "match_status": MATCH_STATUS_MATCHED if selected else MATCH_STATUS_NO_MATCHES,
    }


def _flatten_relationships(dependencies_payload: dict[str, Any], tests_payload: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    for item in dependencies_payload.get("module_dependencies", []):
        out.append(
            {
                "relationship_type": "module_dependency",
                "source_file": item["source_file"],
                "source_line": item.get("source_line"),
                "source_symbol": None,
                "source_symbol_kind": None,
                "target_file": item["target_file"],
                "target_symbol": None,
                "target_symbol_kind": None,
                "resolution_kind": item["import_kind"],
                "reference": item.get("imported_module_text"),
                "test_info": None,
            }
        )

    for item in dependencies_payload.get("imported_symbol_relationships", []):
        out.append(
            {
                "relationship_type": "imported_symbol",
                "source_file": item["source_file"],
                "source_line": item.get("import_line"),
                "source_symbol": item.get("local_name"),
                "source_symbol_kind": None,
                "target_file": item["source_file_target"],
                "target_symbol": item["target_symbol"],
                "target_symbol_kind": item.get("target_symbol_kind"),
                "resolution_kind": "imported_symbol",
                "reference": item.get("source_module"),
                "test_info": None,
            }
        )

    for item in dependencies_payload.get("call_relationships", []):
        out.append(
            {
                "relationship_type": "call",
                "source_file": item["caller_file"],
                "source_line": item.get("call_line"),
                "source_symbol": item.get("caller_symbol"),
                "source_symbol_kind": item.get("caller_symbol_kind"),
                "target_file": item["callee_file"],
                "target_symbol": item.get("callee_symbol"),
                "target_symbol_kind": item.get("callee_symbol_kind"),
                "resolution_kind": item.get("resolution_kind"),
                "reference": None,
                "test_info": None,
            }
        )

    for test_file in tests_payload.get("test_files", []):
        path = test_file["path"]
        for cls in test_file.get("classes", []):
            for method in cls.get("test_methods", []):
                for ref in method.get("resolved_references", []):
                    out.append(
                        {
                            "relationship_type": "test_reference",
                            "source_file": path,
                            "source_line": ref.get("source_line"),
                            "source_symbol": method["name"],
                            "source_symbol_kind": "test_method",
                            "target_file": ref["target_file"],
                            "target_symbol": ref["target_symbol"],
                            "target_symbol_kind": ref.get("target_symbol_kind"),
                            "resolution_kind": ref.get("reference_kind"),
                            "reference": None,
                            "test_info": {
                                "test_file": path,
                                "test_class": cls["name"],
                                "test_name": method["name"],
                                "reference_kind": ref.get("reference_kind"),
                            },
                        }
                    )
        for fn in test_file.get("top_level_test_functions", []):
            for ref in fn.get("resolved_references", []):
                out.append(
                    {
                        "relationship_type": "test_reference",
                        "source_file": path,
                        "source_line": ref.get("source_line"),
                        "source_symbol": fn["name"],
                        "source_symbol_kind": "test_function",
                        "target_file": ref["target_file"],
                        "target_symbol": ref["target_symbol"],
                        "target_symbol_kind": ref.get("target_symbol_kind"),
                        "resolution_kind": ref.get("reference_kind"),
                        "reference": None,
                        "test_info": {
                            "test_file": path,
                            "test_class": None,
                            "test_name": fn["name"],
                            "reference_kind": ref.get("reference_kind"),
                        },
                    }
                )

    return out


def _select_context(
    repository_payload: dict[str, Any],
    symbols_payload: dict[str, Any],
    tests_payload: dict[str, Any],
    dependencies_payload: dict[str, Any],
    query_info: QueryInfo,
) -> dict[str, Any]:
    seed_info = _build_seeds(repository_payload, symbols_payload, dependencies_payload, query_info)
    selected_seeds = seed_info["selected"]

    seed_symbol_ids = {
        _symbol_identity(s["path"], s["symbol_kind"], s["symbol_name"], s["start_line"])
        for s in selected_seeds
        if s["seed_type"] == "symbol"
    }
    for seed in selected_seeds:
        enclosing = seed.get("evidence", {}).get("enclosing_symbol")
        if seed["seed_type"] == "evidence" and enclosing:
            seed_symbol_ids.add(
                _symbol_identity(seed["path"], enclosing["kind"], enclosing["name"], enclosing.get("start_line"))
            )
    seed_symbol_files = {s["path"] for s in selected_seeds if s["seed_type"] == "symbol"}
    seed_files_direct = {s["path"] for s in selected_seeds if s["seed_type"] == "file"}
    seed_evidence_files = {s["path"] for s in selected_seeds if s["seed_type"] == "evidence"}
    seed_all_files = set(seed_symbol_files) | set(seed_files_direct) | set(seed_evidence_files)

    all_relationships = _flatten_relationships(dependencies_payload, tests_payload)

    def rel_involves_seed_symbol(rel: dict[str, Any]) -> bool:
        src = _symbol_identity(rel["source_file"], rel.get("source_symbol_kind") or "", rel.get("source_symbol") or "", None)
        tgt = _symbol_identity(rel["target_file"], rel.get("target_symbol_kind") or "", rel.get("target_symbol") or "", None)
        return any(
            sid[0] == src[0] and sid[1] == src[1] and sid[2] == src[2]
            for sid in seed_symbol_ids
        ) or any(
            sid[0] == tgt[0] and sid[1] == tgt[1] and sid[2] == tgt[2]
            for sid in seed_symbol_ids
        )

    def one_hop_eligible(rel: dict[str, Any]) -> bool:
        rtype = rel["relationship_type"]
        if rtype == "module_dependency":
            return rel["source_file"] in seed_all_files or rel["target_file"] in seed_all_files
        if rtype == "imported_symbol":
            if rel["source_file"] in seed_all_files or rel["target_file"] in seed_all_files:
                return True
            return rel_involves_seed_symbol(rel)
        if rtype == "call":
            return rel_involves_seed_symbol(rel)
        if rtype == "test_reference":
            if rel["target_file"] in seed_all_files:
                return True
            return rel_involves_seed_symbol(rel)
        return False

    one_hop_relationships = [r for r in all_relationships if one_hop_eligible(r)]

    file_candidates: dict[str, dict[str, Any]] = {}

    def add_file(path: str, priority: int, reason: str, score: int = 0) -> None:
        rec = file_candidates.get(path)
        if rec is None:
            file_candidates[path] = {
                "path": path,
                "priority": priority,
                "max_seed_score": score,
                "reasons": [reason],
            }
            return
        rec["priority"] = min(rec["priority"], priority)
        rec["max_seed_score"] = max(rec["max_seed_score"], score)
        if reason not in rec["reasons"]:
            rec["reasons"].append(reason)

    for seed in selected_seeds:
        if seed["seed_type"] == "symbol":
            add_file(seed["path"], 1, seed["reason"], seed["score"])
        elif seed["seed_type"] == "file":
            add_file(seed["path"], 2, seed["reason"], seed["score"])
        else:
            add_file(seed["path"], 2, seed["reason"], seed["score"])

    for rel in one_hop_relationships:
        if rel["relationship_type"] == "test_reference":
            add_file(rel["source_file"], 4, "referenced by test")
            continue

        if rel["relationship_type"] == "module_dependency":
            add_file(rel["source_file"], 3, "imports seed module")
            add_file(rel["target_file"], 3, "imported by seed file")
        elif rel["relationship_type"] == "imported_symbol":
            add_file(rel["source_file"], 3, "imports seed symbol")
            add_file(rel["target_file"], 3, "provides imported seed symbol")
        elif rel["relationship_type"] == "call":
            add_file(rel["source_file"], 3, "calls seed symbol")
            add_file(rel["target_file"], 3, "called by seed symbol")

    ordered_files = sorted(
        file_candidates.values(),
        key=lambda item: (
            item["priority"],
            -item["max_seed_score"],
            encode_path_order(item["path"]),
        ),
    )
    selected_files = ordered_files[:MAX_FILES]
    selected_file_paths = {f["path"] for f in selected_files}

    all_symbols: list[dict[str, Any]] = []
    for file_record in symbols_payload.get("python_files", []):
        if not file_record.get("parse_success", False):
            continue
        for symbol_kind, field_name in (
            ("function", "top_level_functions"),
            ("async_function", "top_level_async_functions"),
            ("class", "top_level_classes"),
        ):
            for sym in file_record.get(field_name, []):
                all_symbols.append(
                    {
                        "path": file_record["path"],
                        "symbol_kind": symbol_kind,
                        "symbol_name": sym["name"],
                        "start_line": sym.get("start_line"),
                    }
                )

    seed_symbol_lookup = {(s["path"], s["symbol_kind"], s["symbol_name"], s["start_line"]): s for s in selected_seeds if s["seed_type"] == "symbol"}
    enclosing_symbol_lookup = {
        (s["path"], s["evidence"]["enclosing_symbol"]["kind"], s["evidence"]["enclosing_symbol"]["name"], s["evidence"]["enclosing_symbol"].get("start_line")): s
        for s in selected_seeds
        if s["seed_type"] == "evidence" and s.get("evidence", {}).get("enclosing_symbol")
    }

    related_symbol_keys: set[tuple[str, str, str, int | None]] = set()
    for rel in one_hop_relationships:
        if rel["relationship_type"] in {"call", "imported_symbol", "test_reference"}:
            if rel.get("target_symbol") and rel.get("target_symbol_kind"):
                related_symbol_keys.add((rel["target_file"], rel["target_symbol_kind"], rel["target_symbol"], None))
            if rel.get("source_symbol") and rel.get("source_symbol_kind"):
                related_symbol_keys.add((rel["source_file"], rel["source_symbol_kind"], rel["source_symbol"], None))

    selected_symbols_raw: list[dict[str, Any]] = []
    for sym in all_symbols:
        sid_full = (sym["path"], sym["symbol_kind"], sym["symbol_name"], sym["start_line"])
        sid_loose = (sym["path"], sym["symbol_kind"], sym["symbol_name"], None)
        if sid_full in seed_symbol_lookup:
            selected_symbols_raw.append(
                {
                    **sym,
                    "priority": 1,
                    "reason": seed_symbol_lookup[sid_full]["reason"],
                    "seed_score": seed_symbol_lookup[sid_full]["score"],
                }
            )
            continue

        if sid_full in enclosing_symbol_lookup:
            evidence_seed = enclosing_symbol_lookup[sid_full]
            selected_symbols_raw.append(
                {
                    **sym,
                    "priority": 2,
                    "reason": f"enclosing symbol for {evidence_seed['evidence_kind']} evidence",
                    "seed_score": evidence_seed["score"],
                }
            )
            continue

        if sid_loose in related_symbol_keys:
            selected_symbols_raw.append({**sym, "priority": 2, "reason": "one-hop relationship with seed symbol", "seed_score": 0})
            continue

        if sym["path"] in seed_files_direct:
            token_hits = count_token_matches(query_info.query_tokens, [sym["symbol_name"].casefold()])
            if token_hits:
                selected_symbols_raw.append(
                    {
                        **sym,
                        "priority": 3,
                        "reason": "token match in seed file",
                        "seed_score": len(token_hits),
                    }
                )

    # deduplicate symbol identities
    dedup_symbols: dict[tuple[str, str, str, int | None], dict[str, Any]] = {}
    for sym in sorted(
        selected_symbols_raw,
        key=lambda item: (
            item["priority"],
            -item["seed_score"],
            encode_path_order(item["path"]),
            _line_key(item["start_line"]),
            _cs_key(item["symbol_name"]),
        ),
    ):
        key = _symbol_identity(sym["path"], sym["symbol_kind"], sym["symbol_name"], sym["start_line"])
        if key not in dedup_symbols:
            dedup_symbols[key] = sym

    selected_symbols = list(dedup_symbols.values())[:MAX_SYMBOLS]
    selected_symbol_lookup = {
        _symbol_identity(s["path"], s["symbol_kind"], s["symbol_name"], s["start_line"]): s for s in selected_symbols
    }
    selected_symbol_names_by_file: dict[str, set[str]] = {}
    for sym in selected_symbols:
        selected_symbol_names_by_file.setdefault(sym["path"], set()).add(sym["symbol_name"])

    candidate_relationships = [
        rel
        for rel in one_hop_relationships
        if rel["source_file"] in selected_file_paths or rel["target_file"] in selected_file_paths
    ]

    def rel_bucket(rel: dict[str, Any]) -> int:
        if rel_involves_seed_symbol(rel):
            return 1
        if rel["source_file"] in seed_files_direct or rel["target_file"] in seed_files_direct:
            return 2
        if rel["relationship_type"] == "test_reference":
            return 3
        return 4

    def rel_sort_key(rel: dict[str, Any]) -> tuple[Any, ...]:
        return (
            rel_bucket(rel),
            RELATIONSHIP_TYPE_ORDER.get(rel["relationship_type"], 99),
            encode_path_order(rel["source_file"]),
            _line_key(rel.get("source_line")),
            _cs_key(rel.get("source_symbol")),
            encode_path_order(rel["target_file"]),
            _cs_key(rel.get("target_symbol")),
            _cs_key(rel.get("resolution_kind")),
        )

    candidate_relationships = sorted(candidate_relationships, key=rel_sort_key)
    selected_relationships = candidate_relationships[:MAX_RELATIONSHIPS]

    test_refs = [r for r in selected_relationships if r["relationship_type"] == "test_reference"]
    selected_test_refs = test_refs[:MAX_TEST_REFERENCES]

    diagnostics_out = []
    for diag in dependencies_payload.get("unresolved_relationships", []):
        reason = diag.get("reason")
        if reason not in DIAGNOSTIC_ALLOWLIST:
            continue
        if reason == "no_tracked_module_match":
            continue

        intersects = False
        if diag.get("source_file") in selected_file_paths:
            intersects = True
        if not intersects:
            for cand in diag.get("candidates", []):
                if cand in selected_file_paths:
                    intersects = True
                    break
        if not intersects and diag.get("source_symbol"):
            symbols_in_file = selected_symbol_names_by_file.get(diag.get("source_file"), set())
            if diag["source_symbol"] in symbols_in_file:
                intersects = True

        if intersects:
            diagnostics_out.append(diag)

    diagnostics_out = sorted(
        diagnostics_out,
        key=lambda d: (
            d.get("reason", ""),
            encode_path_order(d.get("source_file", "")),
            _line_key(d.get("source_line")),
            _cs_key(d.get("source_symbol")),
            _cs_key(d.get("reference")),
        ),
    )

    parse_limitations = []
    for file_record in symbols_payload.get("python_files", []):
        if file_record["path"] in selected_file_paths and not file_record.get("parse_success", False):
            parse_limitations.append(
                {
                    "path": file_record["path"],
                    "parse_error": file_record.get("parse_error"),
                }
            )
    parse_limitations = sorted(parse_limitations, key=lambda p: encode_path_order(p["path"]))

    return {
        "seed_info": seed_info,
        "selected_files": selected_files,
        "selected_symbols": selected_symbols,
        "selected_relationships": selected_relationships,
        "selected_test_references": selected_test_refs,
        "diagnostics": diagnostics_out,
        "parse_limitations": parse_limitations,
        "selection_counts": {
            "seed_total_candidates": seed_info["total_candidate_count"],
            "seed_selected": len(selected_seeds),
            "seed_truncated": seed_info["truncated"],
            "selected_file_total_candidates": len(ordered_files),
            "selected_file_count": len(selected_files),
            "selected_file_truncated": len(ordered_files) > MAX_FILES,
            "selected_symbol_total_candidates": len(dedup_symbols),
            "selected_symbol_count": len(selected_symbols),
            "selected_symbol_truncated": len(dedup_symbols) > MAX_SYMBOLS,
            "selected_relationship_total_candidates": len(candidate_relationships),
            "selected_relationship_count": len(selected_relationships),
            "selected_relationship_truncated": len(candidate_relationships) > MAX_RELATIONSHIPS,
            "selected_test_reference_total_candidates": len(test_refs),
            "selected_test_reference_count": len(selected_test_refs),
            "selected_test_reference_truncated": len(test_refs) > MAX_TEST_REFERENCES,
        },
    }


def _render_context_markdown(context_json: dict[str, Any]) -> str:
    lines: list[str] = []

    lines.append("# Repository Context")
    lines.append("")

    lines.append("## Repository State")
    lines.append(f"- repository_id: {context_json['repository_id']}")
    lines.append(f"- repository_root: {context_json['repository_root']}")
    branch = context_json["branch"]
    lines.append(f"- branch_state: {branch['state']}")
    lines.append(f"- branch_name: {branch['name']}")
    lines.append(f"- head: {context_json['head_commit']}")
    lines.append(f"- working_tree_clean: {context_json['working_tree']['is_clean']}")
    lines.append("")

    lines.append("## Query")
    lines.append(f"- original_query: {context_json['original_query']}")
    lines.append(f"- canonical_query: {context_json['canonical_query']}")
    lines.append(f"- query_tokens: {', '.join(context_json['query_tokens']) if context_json['query_tokens'] else 'none'}")
    lines.append(f"- match_status: {context_json['match_status']}")
    lines.append("")

    lines.append("## Suggested Source Inspection")
    if not context_json["selected_files"]:
        lines.append("- none")
    else:
        for rec in context_json["selected_files"]:
            lines.append(f"- {rec['path']}")
            for reason in rec["reasons"]:
                lines.append(f"  - {reason}")
    lines.append("")

    lines.append("## Relevant Symbols")
    if not context_json["selected_symbols"]:
        lines.append("- none")
    else:
        for sym in context_json["selected_symbols"]:
            lines.append(
                f"- {sym['path']}::{sym['symbol_name']} ({sym['symbol_kind']}) line={sym['start_line']} reason={sym['reason']}"
            )
    lines.append("")

    lines.append("## Discovery Evidence")
    if not context_json["seed_matches"]:
        lines.append("- none")
    else:
        for seed in context_json["seed_matches"]:
            evidence_kind = seed.get("evidence_kind", seed.get("seed_type", "unknown"))
            tokens = ", ".join(seed.get("matched_tokens", [])) or "none"
            lines.append(
                f"- {evidence_kind} | {seed['path']} | line={seed.get('start_line', 'unknown')} | tokens={tokens} | {seed['reason']}"
            )
    lines.append("")

    lines.append("## Internal Relationships")
    if not context_json["relevant_internal_relationships"]:
        lines.append("- none")
    else:
        for rel in context_json["relevant_internal_relationships"]:
            lines.append(
                f"- {rel['relationship_type']} | {rel['source_file']}:{rel['source_symbol']} -> {rel['target_file']}:{rel['target_symbol']} | line={rel['source_line']} | kind={rel['resolution_kind']}"
            )
    lines.append("")

    lines.append("## Related Tests")
    if not context_json["relevant_tests"]:
        lines.append("- none")
    else:
        for test in context_json["relevant_tests"]:
            test_class = test["test_info"]["test_class"]
            test_name = test["test_info"]["test_name"]
            if test_class:
                label = f"{test['test_info']['test_file']}::{test_class}.{test_name}"
            else:
                label = f"{test['test_info']['test_file']}::{test_name}"
            lines.append(
                f"- {label} -> {test['target_file']}::{test['target_symbol']} ({test['target_symbol_kind']}) kind={test['test_info']['reference_kind']} line={test['source_line']}"
            )
    lines.append("")

    lines.append("## Limitations / Ambiguities")
    if not context_json["relevant_limitations"] and not context_json["parse_limitations"]:
        lines.append("- none")
    else:
        for lim in context_json["parse_limitations"]:
            lines.append(f"- parse_failure: {lim['path']} | {lim['parse_error']}")
        for diag in context_json["relevant_limitations"]:
            lines.append(
                f"- {diag['reason']} | source={diag['source_file']} | symbol={diag['source_symbol']} | reference={diag['reference']} | line={diag['source_line']}"
            )
    lines.append("")

    lines.append("## Selection Metadata")
    meta = context_json["selection_metadata"]
    for key in sorted(meta.keys()):
        lines.append(f"- {key}: {meta[key]}")

    return "\n".join(lines) + "\n"


def build_context_payload(scan_result: dict[str, Any], query: str) -> tuple[dict[str, Any], str]:
    query_info = build_query_info(query)

    repository_payload = scan_result["repository_payload"]
    symbols_payload = scan_result["symbols_payload"]
    tests_payload = scan_result["tests_payload"]
    dependencies_payload = scan_result["dependencies_payload"]

    selection = _select_context(
        repository_payload=repository_payload,
        symbols_payload=symbols_payload,
        tests_payload=tests_payload,
        dependencies_payload=dependencies_payload,
        query_info=query_info,
    )

    context_json = {
        "schema_version": 1,
        "repository_id": repository_payload["repository_id"],
        "repository_root": repository_payload["repository_root"],
        "branch": repository_payload["branch"],
        "head_commit": repository_payload["head_commit"],
        "working_tree": repository_payload["working_tree"],
        "original_query": query_info.original_query,
        "canonical_query": query_info.canonical_query,
        "query_tokens": query_info.query_tokens,
        "context_id": query_info.context_id,
        "selection_contract_version": SELECTION_CONTRACT_VERSION,
        "match_status": selection["seed_info"]["match_status"],
        "seed_matches": selection["seed_info"]["selected"],
        "selected_files": selection["selected_files"],
        "selected_symbols": selection["selected_symbols"],
        "relevant_internal_relationships": selection["selected_relationships"],
        "relevant_tests": selection["selected_test_references"],
        "relevant_limitations": selection["diagnostics"],
        "parse_limitations": selection["parse_limitations"],
        "selection_metadata": {
            **selection["selection_counts"],
            "max_seeds": MAX_SEEDS,
            "max_files": MAX_FILES,
            "max_symbols": MAX_SYMBOLS,
            "max_relationships": MAX_RELATIONSHIPS,
            "max_test_references": MAX_TEST_REFERENCES,
        },
    }

    context_md = _render_context_markdown(context_json)
    return context_json, context_md


def publish_context(repository_output_dir: Path, context_id: str, context_json: dict[str, Any], context_md: str) -> Path:
    contexts_root = repository_output_dir / "contexts"
    contexts_root.mkdir(parents=True, exist_ok=True)

    final_dir = contexts_root / context_id
    temp_dir = Path(mkdtemp(prefix="context-tmp-", dir=str(contexts_root)))
    try:
        write_json_deterministic(temp_dir / "context.json", context_json)
        (temp_dir / "context.md").write_text(context_md, encoding="utf-8", newline="\n")

        backup_dir = None
        if final_dir.exists():
            backup_dir = contexts_root / f"{context_id}.bak"
            if backup_dir.exists():
                shutil.rmtree(backup_dir)
            final_dir.rename(backup_dir)

        temp_dir.rename(final_dir)

        if backup_dir and backup_dir.exists():
            shutil.rmtree(backup_dir)
    except Exception:
        if temp_dir.exists():
            shutil.rmtree(temp_dir)
        raise

    return final_dir


def build_and_publish_context(scan_result: dict[str, Any], query: str) -> dict[str, Any]:
    context_json, context_md = build_context_payload(scan_result=scan_result, query=query)
    output_dir = Path(scan_result["output_dir"])
    context_dir = publish_context(
        repository_output_dir=output_dir,
        context_id=context_json["context_id"],
        context_json=context_json,
        context_md=context_md,
    )
    return {
        "context_id": context_json["context_id"],
        "context_dir": str(context_dir),
        "match_status": context_json["match_status"],
        "seed_count": len(context_json["seed_matches"]),
        "selected_file_count": len(context_json["selected_files"]),
        "selected_symbol_count": len(context_json["selected_symbols"]),
        "selected_relationship_count": len(context_json["relevant_internal_relationships"]),
        "selected_test_reference_count": len(context_json["relevant_tests"]),
        "repository_root": context_json["repository_root"],
        "repository_id": context_json["repository_id"],
    }
