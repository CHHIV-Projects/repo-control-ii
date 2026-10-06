from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any


def make_repository_id(repository_root: Path) -> str:
    canonical_root = repository_root.resolve(strict=True)
    slug_source = canonical_root.name.lower()
    slug = re.sub(r"[^a-z0-9._-]+", "-", slug_source).strip("-._")
    if not slug:
        slug = "repo"
    digest = hashlib.sha256(os.fsencode(str(canonical_root))).hexdigest()[:12]
    return f"{slug}--{digest}"


def encode_path_order(path_text: str) -> bytes:
    return os.fsencode(path_text)


def write_json_deterministic(path: Path, payload: dict[str, Any]) -> None:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    path.write_text(text, encoding="utf-8", newline="\n")


def count_lines_if_text(file_bytes: bytes) -> int | None:
    try:
        text = file_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return None

    if not text:
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)


def classify_file_type(path: str) -> str:
    suffix = Path(path).suffix.lower()
    if suffix == ".py":
        return "python"
    if suffix == ".txt":
        return "text"
    if suffix == ".md":
        return "markdown"
    if suffix == ".json":
        return "json"
    if suffix == ".yaml" or suffix == ".yml":
        return "yaml"
    if suffix == ".toml":
        return "toml"
    if suffix == ".bat":
        return "batch"
    if suffix == ".vbs":
        return "vbscript"
    if suffix == "":
        return "no_extension"
    return "other"
