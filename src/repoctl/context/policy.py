from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Iterable

MAX_SEEDS = 12
MAX_FILES = 20
MAX_SYMBOLS = 40
MAX_RELATIONSHIPS = 40
MAX_TEST_REFERENCES = 20

SELECTION_CONTRACT_VERSION = "m003-selection-v1"

MATCH_STATUS_MATCHED = "matched"
MATCH_STATUS_NO_MATCHES = "no_matches"

WEIGHT_EXACT_SYMBOL_NAME = 500
WEIGHT_EXACT_PATH_OR_MODULE_COMPONENT = 400
WEIGHT_FULL_QUERY_SUBSTRING = 300
WEIGHT_MULTI_TOKEN = 200
WEIGHT_SINGLE_TOKEN = 100
WEIGHT_EXACT_IMPORT_OR_DEPENDENCY = 350
WEIGHT_EXACT_ATTRIBUTE = 250
WEIGHT_SOURCE_TOKEN = 150

RELATIONSHIP_TYPE_ORDER = {
    "imported_symbol": 1,
    "call": 2,
    "module_dependency": 3,
    "test_reference": 4,
}

DIAGNOSTIC_ALLOWLIST = {
    "ambiguous_module",
    "target_parse_failure",
    "shadowed_or_rebound",
    "unresolved_symbol",
    "wildcard_import",
}


@dataclass(frozen=True)
class QueryInfo:
    original_query: str
    canonical_query: str
    canonical_casefolded_query: str
    query_tokens: list[str]
    context_id: str


def canonicalize_query(query: str) -> str:
    return " ".join(query.split())


def tokenize_text(text: str) -> list[str]:
    # Milestone 003 v1 separators are ASCII whitespace + _ - . / \
    casefolded = text.casefold()
    out: list[str] = []
    current: list[str] = []

    def flush() -> None:
        if current:
            out.append("".join(current))
            current.clear()

    for ch in casefolded:
        if ch.isspace() or ch in "_.-/\\":
            flush()
        else:
            current.append(ch)
    flush()

    deduped: list[str] = []
    seen = set()
    for token in out:
        if not token:
            continue
        if token in seen:
            continue
        seen.add(token)
        deduped.append(token)

    return deduped


def make_context_id(canonical_query: str) -> str:
    folded = canonical_query.casefold()
    slug = re.sub(r"[^a-z0-9]+", "-", folded).strip("-")
    if not slug:
        slug = "query"
    digest = hashlib.sha256(folded.encode("utf-8")).hexdigest()[:12]
    return f"{slug}--{digest}"


def build_query_info(query: str) -> QueryInfo:
    canonical_query = canonicalize_query(query)
    if not canonical_query:
        raise ValueError("query must not be empty after normalization")
    folded = canonical_query.casefold()
    tokens = tokenize_text(canonical_query)
    context_id = make_context_id(canonical_query)
    return QueryInfo(
        original_query=query,
        canonical_query=canonical_query,
        canonical_casefolded_query=folded,
        query_tokens=tokens,
        context_id=context_id,
    )


def count_token_matches(tokens: list[str], field_texts_casefolded: Iterable[str]) -> set[str]:
    matched: set[str] = set()
    all_texts = list(field_texts_casefolded)
    for token in tokens:
        for text in all_texts:
            if token in text:
                matched.add(token)
                break
    return matched
