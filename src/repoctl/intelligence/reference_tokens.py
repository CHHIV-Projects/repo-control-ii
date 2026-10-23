from __future__ import annotations

import hashlib
import importlib
from importlib import metadata
import os
from pathlib import Path
from typing import Any

from .provider import IntelligenceError

REFERENCE_METHOD = "o200k-base-reference-v1"
REFERENCE_METHOD_VERSION = "1"
TOKENIZER_PACKAGE = "tiktoken"
TOKENIZER_PACKAGE_VERSION = "0.11.0"
ENCODING_NAME = "o200k_base"
ENCODING_URL = "https://openaipublic.blob.core.windows.net/encodings/o200k_base.tiktoken"
ENCODING_ASSET_SHA256 = "446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d"
REFERENCE_TOKEN_SURFACE = "controlled-evidence-v1-separated-delivered-items-encode_ordinary"
DEFAULT_CACHE_DIR = Path.home() / ".cache/repoctl/intelligence-benchmark/tiktoken"
_CACHE_KEY = hashlib.sha1(ENCODING_URL.encode("utf-8")).hexdigest()


def asset_path(cache_dir: Path = DEFAULT_CACHE_DIR) -> Path:
    return cache_dir / _CACHE_KEY


def load_encoding(cache_dir: Path = DEFAULT_CACHE_DIR) -> Any:
    cache_dir = Path(cache_dir).expanduser()
    if cache_dir.is_symlink() or not cache_dir.is_dir():
        raise IntelligenceError("The pinned o200k_base cache directory is missing or unsafe.")
    path = asset_path(cache_dir)
    if path.is_symlink() or not path.is_file():
        raise IntelligenceError("The pinned o200k_base vocabulary asset is missing; refusing network fallback.")
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise IntelligenceError(f"The pinned o200k_base vocabulary asset cannot be read: {exc}") from exc
    if digest != ENCODING_ASSET_SHA256:
        raise IntelligenceError("The pinned o200k_base vocabulary asset SHA-256 does not match; refusing network fallback.")
    try:
        installed_version = metadata.version(TOKENIZER_PACKAGE)
    except metadata.PackageNotFoundError as exc:
        raise IntelligenceError("The isolated benchmark environment does not contain pinned tiktoken.") from exc
    if installed_version != TOKENIZER_PACKAGE_VERSION:
        raise IntelligenceError(
            f"Unsupported tiktoken version {installed_version}; expected {TOKENIZER_PACKAGE_VERSION}."
        )
    os.environ["TIKTOKEN_CACHE_DIR"] = str(cache_dir.resolve())
    try:
        tiktoken = importlib.import_module("tiktoken")
        encoding = tiktoken.get_encoding(ENCODING_NAME)
    except (ImportError, OSError, ValueError) as exc:
        raise IntelligenceError(f"The verified o200k_base encoding could not be loaded: {exc}") from exc
    if getattr(encoding, "name", None) != ENCODING_NAME:
        raise IntelligenceError("The tokenizer package returned an unexpected encoding.")
    return encoding


def count_reference_tokens(encoding: Any, text: str) -> int:
    if not isinstance(text, str):
        raise IntelligenceError("Reference token counting requires text.")
    try:
        return len(encoding.encode_ordinary(text))
    except (AttributeError, TypeError, ValueError, UnicodeError) as exc:
        raise IntelligenceError(f"Reference tokenizer could not count ordinary text: {exc}") from exc


def provenance() -> dict[str, str]:
    return {
        "method": REFERENCE_METHOD,
        "method_version": REFERENCE_METHOD_VERSION,
        "package": TOKENIZER_PACKAGE,
        "package_version": TOKENIZER_PACKAGE_VERSION,
        "encoding": ENCODING_NAME,
        "asset_sha256": ENCODING_ASSET_SHA256,
        "surface": REFERENCE_TOKEN_SURFACE,
        "handling": "encode_ordinary; special-token-like strings treated as ordinary text",
    }
