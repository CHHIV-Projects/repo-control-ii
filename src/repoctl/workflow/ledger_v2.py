from __future__ import annotations

import base64
import bisect
import hashlib
import json
import os
import re
import stat
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from ..scanner.core import state_artifact_lock

V1_MARKER = "<!-- repoctl-workflow-ledger:v1 -->"
V2_MARKER = "<!-- repoctl-workflow-ledger:v2 -->"
LEDGER_SCHEMA_VERSION = 2
EVENT_SCHEMA_VERSION = 1
MAX_ANCHOR_BYTES = 256 * 1024
MAX_SEGMENT_EVENTS = 64
MAX_SEGMENT_BYTES = 256 * 1024
MAX_INDEX_PAGE_BYTES = 64 * 1024
MAX_INDEX_LEAF_BYTES = 8 * 1024
MAX_EVENT_PAGE_BYTES = 256 * 1024
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_TX_RE = re.compile(r"^[0-9a-f]{32}$")
_PAGE_SCHEMA_VERSION = 1
_SEGMENT_SCHEMA_VERSION = 1
_PENDING_NAME = ".pending-transaction.json"
_PENDING_TEMP_RE = re.compile(r"^\.pending-transaction-[0-9a-f]{32}-[0-9a-f]{32}\.tmp$")
_MAX_REPORTED_PENDING_TEMPS = 16
_ROOT_NAMES = (
    "segment_catalog",
    "event_ids",
    "questions",
    "bugs",
    "decisions",
    "closeouts",
    "relations",
    "transactions",
)


class LedgerV2Error(RuntimeError):
    pass


class StaleCursorError(LedgerV2Error):
    pass


class CommitUncertainError(LedgerV2Error):
    pass


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def dispatch_marker(text: str) -> int:
    v1_count = text.count(V1_MARKER)
    v2_count = text.count(V2_MARKER)
    if v1_count == 1 and v2_count == 0:
        return 1
    if v2_count == 1 and v1_count == 0:
        return 2
    raise LedgerV2Error("Workflow anchor has a missing, repeated, or conflicting version marker.")


def _extract_json(text: str, marker: str) -> dict[str, Any]:
    if text.count(marker) != 1:
        raise LedgerV2Error("Workflow anchor has no unique version marker.")
    tail = text.split(marker, 1)[1]
    match = re.search(r"(?m)^(`{3,})json[ \t]*\n(.*?)\n\1[ \t]*$", tail, re.DOTALL)
    if match is None:
        raise LedgerV2Error("Workflow anchor has no valid JSON manifest.")
    try:
        value = json.loads(match.group(2))
    except json.JSONDecodeError as exc:
        raise LedgerV2Error(f"Workflow anchor manifest is malformed: {exc}") from exc
    if not isinstance(value, dict):
        raise LedgerV2Error("Workflow anchor manifest must be an object.")
    return value


def _anchor_text(manifest: dict[str, Any]) -> bytes:
    text = (
        "# Workflow record - "
        + str(manifest["milestone_id"])
        + "\n\n"
        + V2_MARKER
        + "\n```json\n"
        + json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n```\n"
    )
    result = text.encode("utf-8")
    if len(result) > MAX_ANCHOR_BYTES:
        raise LedgerV2Error("Workflow v2 anchor exceeds the 256 KiB safety limit.")
    return result


def _validate_manifest(
    manifest: dict[str, Any],
    repository_id: str,
    milestone_id: str,
    prompt_path: str,
    prompt_sha256: str,
) -> None:
    if (
        not isinstance(manifest.get("schema_version"), int)
        or isinstance(manifest.get("schema_version"), bool)
        or manifest["schema_version"] != LEDGER_SCHEMA_VERSION
        or not isinstance(manifest.get("event_schema_version"), int)
        or isinstance(manifest.get("event_schema_version"), bool)
        or manifest.get("event_schema_version") != EVENT_SCHEMA_VERSION
        or manifest.get("repository_id") != repository_id
        or manifest.get("milestone_id") != milestone_id
        or manifest.get("prompt_path") != prompt_path
        or manifest.get("prompt_sha256") != prompt_sha256
    ):
        raise LedgerV2Error("Workflow v2 manifest identity or schema does not match the selected milestone.")
    if not isinstance(prompt_sha256, str) or not _HASH_RE.fullmatch(prompt_sha256):
        raise LedgerV2Error("Workflow v2 prompt identity is invalid.")
    if not isinstance(manifest.get("generation"), str) or not _TX_RE.fullmatch(manifest["generation"]):
        raise LedgerV2Error("Workflow v2 generation identifier is invalid.")
    if not isinstance(manifest.get("transaction_id"), str) or not _TX_RE.fullmatch(
        manifest["transaction_id"]
    ):
        raise LedgerV2Error("Workflow v2 transaction identifier is invalid.")
    count = manifest.get("event_count")
    segment_count = manifest.get("segment_count")
    last_sequence = manifest.get("last_sequence")
    if (
        not isinstance(count, int)
        or isinstance(count, bool)
        or count < 0
        or not isinstance(segment_count, int)
        or isinstance(segment_count, bool)
        or segment_count < 0
        or not isinstance(last_sequence, int)
        or isinstance(last_sequence, bool)
        or last_sequence != count
        or segment_count > count
    ):
        raise LedgerV2Error("Workflow v2 manifest counts are invalid.")
    last_event_id = manifest.get("last_event_id")
    if (
        (count == 0 and last_event_id is not None)
        or (
            count > 0
            and (
                not isinstance(last_event_id, str)
                or not last_event_id
                or len(last_event_id) > 128
            )
        )
    ):
        raise LedgerV2Error("Workflow v2 manifest terminal event metadata is inconsistent.")
    last_segment = manifest.get("last_segment_sha256")
    if (segment_count == 0) != (last_segment is None) or (
        last_segment is not None
        and (not isinstance(last_segment, str) or not _HASH_RE.fullmatch(last_segment))
    ):
        raise LedgerV2Error("Workflow v2 terminal segment metadata is inconsistent.")
    roots = manifest.get("roots")
    if not isinstance(roots, dict) or set(roots) != set(_ROOT_NAMES):
        raise LedgerV2Error("Workflow v2 manifest index roots are incomplete.")
    if any(
        value is not None and (not isinstance(value, str) or not _HASH_RE.fullmatch(value))
        for value in roots.values()
    ):
        raise LedgerV2Error("Workflow v2 manifest contains an invalid index root.")
    checkpoint = manifest.get("checkpoint")
    if not isinstance(checkpoint, dict):
        raise LedgerV2Error("Workflow v2 lifecycle checkpoint is invalid.")
    if (
        type(checkpoint.get("event_count")) is not int
        or checkpoint["event_count"] != count
    ):
        raise LedgerV2Error("Workflow v2 lifecycle checkpoint count is inconsistent.")
    from . import lifecycle

    phase = checkpoint.get("phase")
    waiting_category = checkpoint.get("waiting_category")
    waiting_reason = checkpoint.get("waiting_reason")
    last_timestamp = checkpoint.get("last_timestamp")
    last_type = checkpoint.get("last_event_type")
    if (
        not isinstance(phase, str)
        or phase not in lifecycle._PHASES
        or (
            waiting_category is not None
            and (
                not isinstance(waiting_category, str)
                or waiting_category not in lifecycle._WAITING_CATEGORIES
            )
        )
        or (waiting_reason is not None and not isinstance(waiting_reason, str))
        or (phase == "WAITING" and (waiting_category is None or not waiting_reason))
        or (phase != "WAITING" and (waiting_category is not None or waiting_reason is not None))
        or (count == 0 and (phase != "PLANNING" or last_timestamp is not None or last_type is not None))
        or (
            count > 0
            and (
                not isinstance(last_timestamp, str)
                or not isinstance(last_type, str)
                or last_type not in lifecycle._EVENT_TYPES
            )
        )
        or checkpoint.get("last_event_id") != last_event_id
    ):
        raise LedgerV2Error("Workflow v2 lifecycle checkpoint fields are inconsistent.")
    if not isinstance(manifest.get("history_verified_generation"), (str, type(None))):
        raise LedgerV2Error("Workflow v2 audit checkpoint is malformed.")
    if manifest["history_verified_generation"] is not None and (
        not _TX_RE.fullmatch(manifest["history_verified_generation"])
        or manifest["history_verified_generation"] != manifest["generation"]
    ):
        raise LedgerV2Error("Workflow v2 audit checkpoint names a different generation.")
    source = manifest.get("migration_source")
    if source is not None and (
        not isinstance(source, dict)
        or not isinstance(source.get("sha256"), str)
        or not _HASH_RE.fullmatch(source["sha256"])
        or not isinstance(source.get("byte_length"), int)
        or isinstance(source.get("byte_length"), bool)
        or source["byte_length"] < 0
        or source["byte_length"] > 5_000_000
        or source.get("path") != f"v1-source-{source['sha256']}.md"
    ):
        raise LedgerV2Error("Workflow v2 migration source metadata is malformed.")


def _read_anchor(
    path: Path,
    repository_id: str,
    milestone_id: str,
    prompt_path: str,
    prompt_sha256: str,
) -> tuple[dict[str, Any], str]:
    parent_fd = _safe_dir(path.parent)
    try:
        raw = _read_named_file(parent_fd, path.name, MAX_ANCHOR_BYTES)
    finally:
        os.close(parent_fd)
    if len(raw) > MAX_ANCHOR_BYTES:
        raise LedgerV2Error("Workflow anchor exceeds the 256 KiB safety limit.")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise LedgerV2Error("Workflow anchor is not valid UTF-8.") from exc
    version = dispatch_marker(text)
    if version != LEDGER_SCHEMA_VERSION:
        raise LedgerV2Error("Workflow v1 data requires the strict v1 reader; it is not an empty v2 ledger.")
    manifest = _extract_json(text, V2_MARKER)
    _validate_manifest(manifest, repository_id, milestone_id, prompt_path, prompt_sha256)
    return manifest, sha256_bytes(raw)


def _sidecar_dirs(anchor: Path) -> tuple[Path, Path]:
    stem = anchor.name.removesuffix("_workflow.md")
    return (
        anchor.with_name(stem + "_workflow_segments"),
        anchor.with_name(stem + "_workflow_indexes"),
    )


def _safe_dir(path: Path, create: bool = False) -> int:
    try:
        if create:
            path.mkdir(mode=0o700, parents=False, exist_ok=True)
        if path.is_symlink() or not path.is_dir():
            raise LedgerV2Error(f"Workflow storage path is not a regular directory: {path.name}")
        resolved = path.resolve(strict=True)
        if resolved != Path(os.path.abspath(path)):
            raise LedgerV2Error(f"Workflow storage path does not resolve canonically: {path.name}")
        return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except LedgerV2Error:
        raise
    except OSError as exc:
        raise LedgerV2Error(f"Workflow storage directory cannot be opened: {exc}") from exc


def _write_new_file(
    directory_fd: int,
    name: str,
    content: bytes,
    *,
    maximum: int = MAX_SEGMENT_BYTES,
) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        fd = os.open(name, flags, 0o600, dir_fd=directory_fd)
    except FileExistsError:
        existing = _read_named_file(directory_fd, name, maximum)
        if existing != content:
            raise LedgerV2Error(f"Immutable workflow artifact already exists with different bytes: {name}")
        fd = os.open(name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=directory_fd)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        return
    except OSError as exc:
        raise LedgerV2Error(f"Could not create immutable workflow artifact {name}: {exc}") from exc
    try:
        view = memoryview(content)
        written = 0
        while written < len(view):
            count = os.write(fd, view[written:])
            if count <= 0:
                raise OSError("Workflow artifact write made no forward progress.")
            written += count
        os.fsync(fd)
    except OSError as exc:
        raise LedgerV2Error(f"Could not durably write immutable workflow artifact {name}: {exc}") from exc
    finally:
        os.close(fd)


def _read_pending_transaction(index_fd: int) -> dict[str, Any] | None:
    try:
        info = os.stat(_PENDING_NAME, dir_fd=index_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise LedgerV2Error(f"Workflow recovery record cannot be inspected safely: {exc}") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_ANCHOR_BYTES:
        raise LedgerV2Error("Workflow recovery record is not a bounded regular file.")
    raw = _read_named_file(index_fd, _PENDING_NAME, MAX_ANCHOR_BYTES)
    try:
        pending = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise LedgerV2Error("Workflow recovery record is malformed and blocks writes.") from exc
    if (
        not isinstance(pending, dict)
        or set(pending)
        != {
            "schema_version",
            "transaction_id",
            "base_generation",
            "payload_sha256",
            "segment_names",
        }
        or type(pending.get("schema_version")) is not int
        or pending["schema_version"] != 1
        or not isinstance(pending.get("transaction_id"), str)
        or not _TX_RE.fullmatch(pending["transaction_id"])
        or not isinstance(pending.get("base_generation"), str)
        or not _TX_RE.fullmatch(pending["base_generation"])
        or not isinstance(pending.get("payload_sha256"), str)
        or not _HASH_RE.fullmatch(pending["payload_sha256"])
        or not isinstance(pending.get("segment_names"), list)
        or not pending["segment_names"]
        or any(
            not isinstance(name, str)
            or re.fullmatch(r"[0-9]{20}-[0-9a-f]{64}\.json", name) is None
            for name in pending["segment_names"]
        )
    ):
        raise LedgerV2Error("Workflow recovery record is invalid and blocks writes.")
    return pending


def _write_pending_transaction(index_fd: int, pending: dict[str, Any]) -> None:
    content = canonical_json_bytes(pending)
    temporary_name = f".pending-transaction-{pending['transaction_id']}-{uuid.uuid4().hex}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        fd = os.open(temporary_name, flags, 0o600, dir_fd=index_fd)
    except OSError as exc:
        raise LedgerV2Error(f"Could not create workflow recovery temporary: {exc}") from exc
    try:
        view = memoryview(content)
        written = 0
        while written < len(view):
            count = os.write(fd, view[written:])
            if count <= 0:
                raise OSError("Workflow recovery record write made no forward progress.")
            written += count
        os.fsync(fd)
    except OSError as exc:
        raise LedgerV2Error(
            f"Could not durably write workflow recovery temporary {temporary_name}: {exc}"
        ) from exc
    finally:
        os.close(fd)

    try:
        os.link(
            temporary_name,
            _PENDING_NAME,
            src_dir_fd=index_fd,
            dst_dir_fd=index_fd,
            follow_symlinks=False,
        )
    except FileExistsError as exc:
        raise LedgerV2Error("A workflow recovery record appeared before it could be published.") from exc
    except OSError as exc:
        raise LedgerV2Error(f"Could not atomically publish workflow recovery record: {exc}") from exc
    try:
        os.unlink(temporary_name, dir_fd=index_fd)
        os.fsync(index_fd)
    except OSError as exc:
        raise LedgerV2Error(
            f"Workflow recovery record was published but directory durability is uncertain: {exc}"
        ) from exc


def _pending_temporary_artifacts(index_fd: int) -> tuple[int, list[str]]:
    names = sorted(name for name in os.listdir(index_fd) if _PENDING_TEMP_RE.fullmatch(name))
    return len(names), names[:_MAX_REPORTED_PENDING_TEMPS]


def _clear_pending_transaction(index_fd: int) -> None:
    try:
        os.unlink(_PENDING_NAME, dir_fd=index_fd)
        os.fsync(index_fd)
    except OSError as exc:
        raise LedgerV2Error(f"Workflow recovery record could not be durably cleared: {exc}") from exc


def _sync_artifact_dirs(anchor: Path, segment_fd: int, index_fd: int) -> None:
    try:
        os.fsync(segment_fd)
        os.fsync(index_fd)
        parent_fd = _safe_dir(anchor.parent)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    except OSError as exc:
        raise LedgerV2Error(f"Could not durably sync workflow artifact directories: {exc}") from exc


def _read_named_file(directory_fd: int, name: str, maximum: int) -> bytes:
    try:
        fd = os.open(name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=directory_fd)
    except OSError as exc:
        raise LedgerV2Error(f"Workflow artifact cannot be opened safely: {exc}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
            raise LedgerV2Error("Workflow artifact is not a bounded regular file.")
        chunks: list[bytes] = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        result = b"".join(chunks)
        if len(result) > maximum:
            raise LedgerV2Error("Workflow artifact exceeds its size limit.")
        return result
    finally:
        os.close(fd)


def _page_name(digest: str) -> str:
    return digest + ".json"


class PersistentBTree:
    def __init__(self, directory_fd: int, root: str | None, page_limit: int = MAX_INDEX_PAGE_BYTES):
        self.directory_fd = directory_fd
        self.root = root
        self.page_limit = page_limit
        self.leaf_limit = min(page_limit, MAX_INDEX_LEAF_BYTES)
        self.pending: dict[str, bytes] = {}
        self.page_cache: OrderedDict[str, dict[str, Any]] = OrderedDict()

    def _read(self, digest: str) -> dict[str, Any]:
        if not _HASH_RE.fullmatch(digest):
            raise LedgerV2Error("Workflow index contains an invalid page reference.")
        if digest in self.page_cache:
            page = self.page_cache[digest]
            self.page_cache.move_to_end(digest)
            return page
        raw = self.pending.get(digest)
        if raw is None:
            raw = _read_named_file(self.directory_fd, _page_name(digest), self.page_limit)
        if sha256_bytes(raw) != digest:
            raise LedgerV2Error("Workflow index page digest does not match its content address.")
        try:
            page = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise LedgerV2Error("Workflow index page is malformed.") from exc
        if (
            not isinstance(page, dict)
            or page.get("schema_version") != _PAGE_SCHEMA_VERSION
            or page.get("type") not in {"leaf", "internal"}
        ):
            raise LedgerV2Error("Workflow index page has an unsupported structure.")
        if page["type"] == "leaf":
            if len(raw) > self.leaf_limit:
                raise LedgerV2Error("Workflow index leaf exceeds its bounded page limit.")
            entries = page.get("entries")
            if not isinstance(entries, list):
                raise LedgerV2Error("Workflow index leaf entries are invalid.")
            keys = [item[0] for item in entries if isinstance(item, list) and len(item) == 2]
            if len(keys) != len(entries) or any(not isinstance(key, str) for key in keys):
                raise LedgerV2Error("Workflow index leaf key/value pairs are invalid.")
            if keys != sorted(set(keys)):
                raise LedgerV2Error("Workflow index leaf keys are not strictly ordered.")
        else:
            keys = page.get("keys")
            children = page.get("children")
            if (
                not isinstance(keys, list)
                or not isinstance(children, list)
                or len(children) != len(keys) + 1
                or len(children) < 2
                or any(not isinstance(key, str) for key in keys)
                or keys != sorted(set(keys))
                or any(not isinstance(child, str) or not _HASH_RE.fullmatch(child) for child in children)
            ):
                raise LedgerV2Error("Workflow index internal page structure is invalid.")
        self.page_cache[digest] = page
        if len(self.page_cache) > 32:
            self.page_cache.popitem(last=False)
        return page

    def _write(self, page: dict[str, Any]) -> str:
        raw = canonical_json_bytes(page)
        if len(raw) > self.page_limit:
            raise LedgerV2Error("Workflow index page exceeds the 64 KiB safety limit.")
        digest = sha256_bytes(raw)
        self.pending[digest] = raw
        return digest

    def flush(self) -> None:
        visited: set[str] = set()

        def persist(digest: str) -> None:
            if digest in visited or digest not in self.pending:
                return
            visited.add(digest)
            raw = self.pending[digest]
            page = json.loads(raw)
            if page["type"] == "internal":
                for child in page["children"]:
                    persist(child)
            _write_new_file(self.directory_fd, _page_name(digest), raw)

        if self.root is not None:
            persist(self.root)
        self.pending.clear()

    def _first_key(self, digest: str) -> str:
        page = self._read(digest)
        while page["type"] == "internal":
            page = self._read(page["children"][0])
        if not page["entries"]:
            raise LedgerV2Error("Workflow index contains an empty non-root leaf.")
        return page["entries"][0][0]

    def _internal(self, children: list[str]) -> dict[str, Any]:
        return {
            "schema_version": _PAGE_SCHEMA_VERSION,
            "type": "internal",
            "keys": [self._first_key(child) for child in children[1:]],
            "children": children,
        }

    def get(self, key: str) -> Any | None:
        digest = self.root
        while digest is not None:
            page = self._read(digest)
            if page["type"] == "leaf":
                keys = [entry[0] for entry in page["entries"]]
                index = bisect.bisect_left(keys, key)
                return page["entries"][index][1] if index < len(keys) and keys[index] == key else None
            index = bisect.bisect_right(page["keys"], key)
            child = page["children"][index]
            if index > 0 and self._first_key(child) != page["keys"][index - 1]:
                raise LedgerV2Error("Workflow index separator does not match its right child.")
            digest = child
        return None

    def _insert(self, digest: str, key: str, value: Any) -> tuple[str, tuple[str, str] | None]:
        page = self._read(digest)
        if page["type"] == "leaf":
            entries = list(page["entries"])
            keys = [entry[0] for entry in entries]
            index = bisect.bisect_left(keys, key)
            if index < len(keys) and keys[index] == key:
                entries[index] = [key, value]
            else:
                entries.insert(index, [key, value])
            candidate = {"schema_version": _PAGE_SCHEMA_VERSION, "type": "leaf", "entries": entries}
            if len(canonical_json_bytes(candidate)) <= self.leaf_limit:
                return self._write(candidate), None
            midpoint = len(entries) // 2
            left = {"schema_version": _PAGE_SCHEMA_VERSION, "type": "leaf", "entries": entries[:midpoint]}
            right = {"schema_version": _PAGE_SCHEMA_VERSION, "type": "leaf", "entries": entries[midpoint:]}
            left_hash, right_hash = self._write(left), self._write(right)
            if len(canonical_json_bytes(left)) > self.leaf_limit or len(canonical_json_bytes(right)) > self.leaf_limit:
                raise LedgerV2Error("A workflow index entry cannot fit in a bounded page.")
            return left_hash, (right["entries"][0][0], right_hash)

        children = list(page["children"])
        index = bisect.bisect_right(page["keys"], key)
        children[index], split = self._insert(children[index], key, value)
        if split is not None:
            children.insert(index + 1, split[1])
        candidate = self._internal(children)
        if len(canonical_json_bytes(candidate)) <= self.page_limit:
            return self._write(candidate), None
        midpoint = len(children) // 2
        left_children, right_children = children[:midpoint], children[midpoint:]
        left_hash, right_hash = self._write(self._internal(left_children)), self._write(self._internal(right_children))
        return left_hash, (self._first_key(right_hash), right_hash)

    def set(self, key: str, value: Any) -> None:
        if not isinstance(key, str) or not key:
            raise LedgerV2Error("Workflow index keys must be non-empty strings.")
        if self.root is None:
            self.root = self._write(
                {"schema_version": _PAGE_SCHEMA_VERSION, "type": "leaf", "entries": [[key, value]]}
            )
            return
        root, split = self._insert(self.root, key, value)
        if split is not None:
            root = self._write(self._internal([root, split[1]]))
        self.root = root

    def set_many(self, updates: list[tuple[str, Any]]) -> None:
        if not updates:
            return
        if any(not isinstance(key, str) or not key for key, _ in updates):
            raise LedgerV2Error("Workflow index keys must be non-empty strings.")
        if len({key for key, _ in updates}) != len(updates):
            raise LedgerV2Error("Workflow index batch contains duplicate keys.")
        updates.sort(key=lambda item: item[0])

        def pack_leaves(entries: list[list[Any]]) -> list[str]:
            chunks: list[list[list[Any]]] = []
            current: list[list[Any]] = []
            base_size = len(
                canonical_json_bytes(
                    {"schema_version": _PAGE_SCHEMA_VERSION, "type": "leaf", "entries": []}
                )
            )
            current_size = base_size
            for entry in entries:
                entry_size = len(canonical_json_bytes(entry))
                addition = entry_size + (1 if current else 0)
                if current and current_size + addition > self.leaf_limit:
                    chunks.append(current)
                    current = [entry]
                    current_size = base_size + entry_size
                else:
                    current.append(entry)
                    current_size += addition
                if current_size > self.leaf_limit:
                    raise LedgerV2Error("A workflow index entry cannot fit in a bounded page.")
            if current:
                chunks.append(current)
            return [
                self._write({"schema_version": _PAGE_SCHEMA_VERSION, "type": "leaf", "entries": chunk})
                for chunk in chunks
            ]

        def pack_internal(children: list[str]) -> list[str]:
            if len(children) < 2:
                return children
            max_children = max(2, min(128, (self.page_limit - 64) // 196))
            group_count = (len(children) + max_children - 1) // max_children
            while group_count > 1 and len(children) // group_count < 2:
                group_count -= 1
            base, extra = divmod(len(children), group_count)
            groups: list[list[str]] = []
            offset = 0
            for index in range(group_count):
                group_size = base + (1 if index < extra else 0)
                groups.append(children[offset : offset + group_size])
                offset += group_size
            pages = [self._internal(group) for group in groups]
            if any(len(canonical_json_bytes(page)) > self.page_limit for page in pages):
                raise LedgerV2Error("A workflow index internal page cannot fit in its bounded size.")
            return [self._write(page) for page in pages]

        def update_node(digest: str, node_updates: list[tuple[str, Any]]) -> list[str]:
            page = self._read(digest)
            if page["type"] == "leaf":
                merged = {key: value for key, value in page["entries"]}
                merged.update(node_updates)
                return pack_leaves([[key, value] for key, value in sorted(merged.items())])
            buckets: list[list[tuple[str, Any]]] = [[] for _ in page["children"]]
            for key, value in node_updates:
                buckets[bisect.bisect_right(page["keys"], key)].append((key, value))
            children: list[str] = []
            for child, child_updates in zip(page["children"], buckets):
                children.extend(update_node(child, child_updates) if child_updates else [child])
            return pack_internal(children)

        if self.root is None:
            level = pack_leaves([[key, value] for key, value in updates])
        else:
            level = update_node(self.root, updates)
        while len(level) > 1:
            level = pack_internal(level)
        self.root = level[0]

    def items(self) -> Iterator[tuple[str, Any]]:
        def walk(digest: str) -> Iterator[tuple[str, Any]]:
            page = self._read(digest)
            if page["type"] == "leaf":
                yield from ((key, value) for key, value in page["entries"])
            else:
                for child in page["children"]:
                    yield from walk(child)

        if self.root is not None:
            yield from walk(self.root)

    def validate(self) -> None:
        if self.root is None:
            return

        def walk(
            digest: str,
            lower: str | None,
            upper: str | None,
            ancestors: frozenset[str],
        ) -> tuple[str, str]:
            if digest in ancestors:
                raise LedgerV2Error("Workflow index contains a cyclic page reference.")
            page = self._read(digest)
            if page["type"] == "leaf":
                keys = [entry[0] for entry in page["entries"]]
                if not keys:
                    raise LedgerV2Error("Workflow index contains an empty leaf.")
                if (
                    (lower is not None and keys[0] < lower)
                    or (upper is not None and keys[-1] >= upper)
                ):
                    raise LedgerV2Error("Workflow index leaf violates its parent key bounds.")
                return keys[0], keys[-1]
            bounds = [lower, *page["keys"], upper]
            minima: list[str] = []
            maxima: list[str] = []
            for index, child in enumerate(page["children"]):
                child_min, child_max = walk(
                    child,
                    bounds[index],
                    bounds[index + 1],
                    ancestors | {digest},
                )
                minima.append(child_min)
                maxima.append(child_max)
                if index > 0 and child_min != page["keys"][index - 1]:
                    raise LedgerV2Error("Workflow index separator does not match its right subtree.")
                if index < len(page["keys"]) and child_max >= page["keys"][index]:
                    raise LedgerV2Error("Workflow index left subtree overlaps its separator.")
            return minima[0], maxima[-1]

        walk(self.root, None, None, frozenset())

    def items_from(self, key: str) -> Iterator[tuple[str, Any]]:
        if self.root is None:
            return
        path: list[tuple[dict[str, Any], int]] = []
        digest = self.root
        while True:
            page = self._read(digest)
            if page["type"] == "leaf":
                break
            child_index = bisect.bisect_right(page["keys"], key)
            path.append((page, child_index))
            digest = page["children"][child_index]
        entries = page["entries"]
        keys = [entry[0] for entry in entries]
        index = bisect.bisect_left(keys, key)
        while True:
            yield from ((item_key, value) for item_key, value in entries[index:])
            while path:
                parent, child_index = path.pop()
                if child_index + 1 < len(parent["children"]):
                    child_index += 1
                    path.append((parent, child_index))
                    digest = parent["children"][child_index]
                    page = self._read(digest)
                    while page["type"] == "internal":
                        path.append((page, 0))
                        page = self._read(page["children"][0])
                    entries = page["entries"]
                    index = 0
                    break
            else:
                return

    def floor_item(self, key: str) -> tuple[str, Any] | None:
        if self.root is None:
            return None
        path: list[tuple[dict[str, Any], int]] = []
        page = self._read(self.root)
        while page["type"] == "internal":
            index = bisect.bisect_right(page["keys"], key)
            path.append((page, index))
            page = self._read(page["children"][index])
        keys = [entry[0] for entry in page["entries"]]
        index = bisect.bisect_right(keys, key) - 1
        if index >= 0:
            return tuple(page["entries"][index])
        while path:
            parent, child_index = path.pop()
            if child_index:
                page = self._read(parent["children"][child_index - 1])
                while page["type"] == "internal":
                    page = self._read(page["children"][-1])
                if page["entries"]:
                    return tuple(page["entries"][-1])
        return None

    @classmethod
    def bulk_load(
        cls, directory_fd: int, items: list[tuple[str, Any]], page_limit: int = MAX_INDEX_PAGE_BYTES
    ) -> str | None:
        tree = cls(directory_fd, None, page_limit)
        if not items:
            return None
        if [key for key, _ in items] != sorted({key for key, _ in items}):
            raise LedgerV2Error("Bulk workflow index keys must be unique and sorted.")
        leaves: list[str] = []
        chunk: list[list[Any]] = []
        for key, value in items:
            candidate = {"schema_version": _PAGE_SCHEMA_VERSION, "type": "leaf", "entries": chunk + [[key, value]]}
            if chunk and len(canonical_json_bytes(candidate)) > tree.leaf_limit:
                leaves.append(tree._write({"schema_version": _PAGE_SCHEMA_VERSION, "type": "leaf", "entries": chunk}))
                chunk = [[key, value]]
            else:
                chunk.append([key, value])
            if len(canonical_json_bytes({"schema_version": _PAGE_SCHEMA_VERSION, "type": "leaf", "entries": chunk})) > tree.leaf_limit:
                raise LedgerV2Error("A workflow index entry cannot fit in a bounded page.")
        if chunk:
            leaves.append(tree._write({"schema_version": _PAGE_SCHEMA_VERSION, "type": "leaf", "entries": chunk}))
        level = leaves
        while len(level) > 1:
            next_level: list[str] = []
            children: list[str] = []
            for child in level:
                candidate_children = children + [child]
                if children and len(canonical_json_bytes(tree._internal(candidate_children))) > page_limit:
                    next_level.append(tree._write(tree._internal(children)))
                    children = [child]
                else:
                    children = candidate_children
            if children:
                if len(children) == 1 and next_level:
                    prior = next_level.pop()
                    prior_page = tree._read(prior)
                    children = prior_page["children"] + children
                next_level.append(tree._write(tree._internal(children)))
            level = next_level
        tree.root = level[0]
        tree.flush()
        return tree.root


@dataclass
class IndexSet:
    directory_fd: int
    roots: dict[str, str | None]
    memory: dict[str, dict[str, Any]] | None = None
    cache: dict[str, PersistentBTree] | None = None
    pending: dict[str, dict[str, Any]] | None = None

    def tree(self, name: str) -> PersistentBTree | MemoryBTree:
        if self.memory is not None:
            return MemoryBTree(self.memory[name])
        if self.cache is None:
            self.cache = {}
        if name not in self.cache:
            self.cache[name] = PersistentBTree(self.directory_fd, self.roots[name])
        if self.pending is None:
            self.pending = {}
        return BufferedBTree(self, name)

    def set(self, name: str, key: str, value: Any) -> None:
        if self.memory is not None:
            self.memory[name][key] = value
            return
        if self.cache is None:
            self.cache = {}
        if name not in self.cache:
            self.cache[name] = PersistentBTree(self.directory_fd, self.roots[name])
        if self.pending is None:
            self.pending = {}
        self.pending.setdefault(name, {})[key] = value

    def flush(self) -> None:
        if self.memory is not None:
            return
        for name, updates in (self.pending or {}).items():
            tree = (self.cache or {})[name]
            tree.set_many(list(updates.items()))
            self.roots[name] = tree.root
            tree.flush()
        self.pending = {}


@dataclass
class BufferedBTree:
    indexes: IndexSet
    name: str

    def get(self, key: str) -> Any | None:
        pending = (self.indexes.pending or {}).get(self.name, {})
        if key in pending:
            return pending[key]
        return (self.indexes.cache or {})[self.name].get(key)

    def set(self, key: str, value: Any) -> None:
        self.indexes.set(self.name, key, value)

    def items(self) -> Iterator[tuple[str, Any]]:
        values = dict((self.indexes.cache or {})[self.name].items())
        values.update((self.indexes.pending or {}).get(self.name, {}))
        yield from sorted(values.items())


class MemoryBTree:
    def __init__(self, values: dict[str, Any]):
        self.values = values

    def get(self, key: str) -> Any | None:
        return self.values.get(key)

    def set(self, key: str, value: Any) -> None:
        self.values[key] = value

    def items(self) -> Iterator[tuple[str, Any]]:
        yield from sorted(self.values.items())


def _blank_checkpoint() -> dict[str, Any]:
    return {
        "event_count": 0,
        "phase": "PLANNING",
        "waiting_category": None,
        "waiting_reason": None,
        "last_timestamp": None,
        "last_event_id": None,
        "last_event_type": None,
        "waiting_boundary": None,
        "latest_authoritative_decision": None,
        "last_acceptance": None,
    }


def _base_manifest(
    repository_id: str,
    milestone_id: str,
    prompt_path: str,
    prompt_sha256: str,
    transaction_id: str,
) -> dict[str, Any]:
    return {
        "schema_version": LEDGER_SCHEMA_VERSION,
        "event_schema_version": EVENT_SCHEMA_VERSION,
        "repository_id": repository_id,
        "milestone_id": milestone_id,
        "prompt_path": prompt_path,
        "prompt_sha256": prompt_sha256,
        "generation": uuid.uuid4().hex,
        "transaction_id": transaction_id,
        "event_count": 0,
        "last_sequence": 0,
        "last_event_id": None,
        "segment_count": 0,
        "last_segment_sha256": None,
        "roots": {name: None for name in _ROOT_NAMES},
        "checkpoint": _blank_checkpoint(),
        "history_verified_generation": None,
        "migration_source": None,
    }


def _initial_root_dirs(anchor: Path, create: bool) -> tuple[int, int]:
    segment_path, index_path = _sidecar_dirs(anchor)
    segment_fd = _safe_dir(segment_path, create)
    try:
        index_fd = _safe_dir(index_path, create)
    except BaseException:
        os.close(segment_fd)
        raise
    return segment_fd, index_fd


def create_v2(
    anchor: str | Path,
    repository_id: str,
    milestone_id: str,
    prompt_path: str,
    prompt_sha256: str,
    *,
    state_root: str | Path,
    transaction_id: str | None = None,
) -> dict[str, Any]:
    path = Path(anchor)
    txid = transaction_id or uuid.uuid4().hex
    if not _TX_RE.fullmatch(txid):
        raise LedgerV2Error("Workflow transaction identifier must be 32 lowercase hexadecimal characters.")
    with state_artifact_lock(state_root):
        if path.exists() or path.is_symlink():
            raise LedgerV2Error("Workflow anchor already exists; v1 data is never implicitly replaced.")
        parent_fd = _safe_dir(path.parent)
        os.close(parent_fd)
        segment_fd, index_fd = _initial_root_dirs(path, create=True)
        try:
            manifest = _base_manifest(repository_id, milestone_id, prompt_path, prompt_sha256, txid)
            _validate_manifest(manifest, repository_id, milestone_id, prompt_path, prompt_sha256)
            _sync_artifact_dirs(path, segment_fd, index_fd)
            _commit_anchor(
                path,
                _anchor_text(manifest),
                expected_old_hash=None,
                transaction_id=txid,
            )
            return manifest
        finally:
            os.close(index_fd)
            os.close(segment_fd)


def _commit_anchor(
    path: Path,
    content: bytes,
    expected_old_hash: str | None,
    *,
    expected_old_maximum: int = MAX_ANCHOR_BYTES,
    transaction_id: str | None = None,
) -> None:
    parent_fd = _safe_dir(path.parent)
    if transaction_id is not None and not _TX_RE.fullmatch(transaction_id):
        os.close(parent_fd)
        raise LedgerV2Error("Workflow anchor transaction identifier is invalid.")
    temp_name = f".{path.name}.{transaction_id or uuid.uuid4().hex}.tmp"
    try:
        if path.exists() and path.is_symlink():
            raise LedgerV2Error("Workflow anchor became a symlink during commit.")
        if expected_old_hash is not None:
            current = _read_named_file(parent_fd, path.name, expected_old_maximum)
            if sha256_bytes(current) != expected_old_hash:
                raise LedgerV2Error("Workflow anchor changed before commit.")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
        try:
            fd = os.open(temp_name, flags, 0o600, dir_fd=parent_fd)
        except FileExistsError:
            if _read_named_file(parent_fd, temp_name, MAX_ANCHOR_BYTES) != content:
                raise LedgerV2Error(
                    f"Workflow anchor temporary is incomplete or differs; retained: {temp_name}"
                )
            fd = os.open(
                temp_name,
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=parent_fd,
            )
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        else:
            try:
                view = memoryview(content)
                written = 0
                while written < len(view):
                    count = os.write(fd, view[written:])
                    if count <= 0:
                        raise OSError("Workflow anchor write made no forward progress.")
                    written += count
                os.fsync(fd)
            finally:
                os.close(fd)
        if expected_old_hash is None:
            linked = False
            try:
                os.link(
                    temp_name,
                    path.name,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                    follow_symlinks=False,
                )
                linked = True
                os.unlink(temp_name, dir_fd=parent_fd)
            except FileExistsError as exc:
                raise LedgerV2Error("Workflow anchor appeared before initial commit.") from exc
            except OSError as exc:
                if linked:
                    raise CommitUncertainError(
                        f"Workflow anchor was created but initial commit cleanup failed: {exc}"
                    ) from exc
                raise
        else:
            os.replace(temp_name, path.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        try:
            os.fsync(parent_fd)
        except OSError as exc:
            raise CommitUncertainError(
                f"Workflow anchor was replaced but its directory sync failed: {exc}"
            ) from exc
    except CommitUncertainError:
        raise
    except OSError as exc:
        raise LedgerV2Error(f"Workflow anchor commit failed: {exc}") from exc
    finally:
        os.close(parent_fd)


def _segment_bytes(
    manifest: dict[str, Any],
    segment_index: int,
    first_sequence: int,
    events: list[dict[str, Any]],
    predecessor_sha256: str | None,
) -> tuple[bytes, int]:
    event_bytes = canonical_json_bytes(events)
    segment = {
        "schema_version": _SEGMENT_SCHEMA_VERSION,
        "repository_id": manifest["repository_id"],
        "milestone_id": manifest["milestone_id"],
        "prompt_path": manifest["prompt_path"],
        "prompt_sha256": manifest["prompt_sha256"],
        "segment_index": segment_index,
        "first_sequence": first_sequence,
        "last_sequence": first_sequence + len(events) - 1,
        "event_count": len(events),
        "byte_length": len(event_bytes),
        "predecessor_sha256": predecessor_sha256,
        "events": events,
    }
    raw = canonical_json_bytes(segment)
    if not 1 <= len(events) <= MAX_SEGMENT_EVENTS:
        raise LedgerV2Error("Workflow segment event count is outside its supported range.")
    if len(raw) > MAX_SEGMENT_BYTES:
        raise LedgerV2Error("Workflow segment exceeds the 256 KiB safety limit.")
    return raw, len(event_bytes)


def _segment_filename(index: int, digest: str) -> str:
    return f"{index:020d}-{digest}.json"


def _read_segment(
    segment_fd: int,
    digest: str,
    expected_index: int,
    expected_first: int,
    expected_predecessor: str | None,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(digest, str) or not _HASH_RE.fullmatch(digest):
        raise LedgerV2Error("Workflow segment catalog contains an invalid digest.")
    raw = _read_named_file(
        segment_fd,
        _segment_filename(expected_index, digest),
        MAX_SEGMENT_BYTES,
    )
    if sha256_bytes(raw) != digest:
        raise LedgerV2Error("Workflow segment digest does not match its catalog entry.")
    try:
        segment = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise LedgerV2Error("Workflow segment is malformed.") from exc
    if not isinstance(segment, dict):
        raise LedgerV2Error("Workflow segment must be an object.")
    events = segment.get("events")
    if (
        type(segment.get("schema_version")) is not int
        or segment["schema_version"] != _SEGMENT_SCHEMA_VERSION
        or segment.get("repository_id") != manifest["repository_id"]
        or segment.get("milestone_id") != manifest["milestone_id"]
        or segment.get("prompt_path") != manifest["prompt_path"]
        or segment.get("prompt_sha256") != manifest["prompt_sha256"]
        or type(segment.get("segment_index")) is not int
        or segment["segment_index"] != expected_index
        or type(segment.get("first_sequence")) is not int
        or segment["first_sequence"] != expected_first
        or not isinstance(events, list)
        or not 1 <= len(events) <= MAX_SEGMENT_EVENTS
        or type(segment.get("event_count")) is not int
        or segment["event_count"] != len(events)
        or type(segment.get("last_sequence")) is not int
        or segment["last_sequence"] != expected_first + len(events) - 1
        or segment.get("predecessor_sha256") != expected_predecessor
        or type(segment.get("byte_length")) is not int
        or segment["byte_length"] != len(canonical_json_bytes(events))
    ):
        raise LedgerV2Error("Workflow segment metadata or identity is inconsistent.")
    return segment


def _load_trees(index_fd: int, roots: dict[str, str | None]) -> IndexSet:
    return IndexSet(index_fd, dict(roots))


def _validate_index_roots(index_fd: int, roots: dict[str, str | None]) -> None:
    for root in roots.values():
        if root is not None:
            PersistentBTree(index_fd, root)._read(root)


def _validate_append_tail(
    manifest: dict[str, Any],
    segment_fd: int,
    index_fd: int,
) -> None:
    if manifest["event_count"] == 0:
        if manifest["segment_count"] != 0 or manifest["last_segment_sha256"] is not None:
            raise LedgerV2Error("Empty workflow history has inconsistent segment metadata.")
        return
    if any(
        manifest["roots"][name] is None
        for name in ("segment_catalog", "event_ids", "transactions")
    ):
        raise LedgerV2Error("Workflow history lacks a required event or transaction index root.")
    catalog = PersistentBTree(index_fd, manifest["roots"]["segment_catalog"])
    last = catalog.floor_item(_key(manifest["event_count"]))
    if last is None:
        raise LedgerV2Error("Workflow segment catalog omits the committed history tail.")
    last_key, entry = last
    if not isinstance(entry, dict):
        raise LedgerV2Error("Workflow segment catalog tail is malformed.")
    first_sequence = entry.get("first_sequence")
    segment_index = entry.get("segment_index")
    if (
        type(first_sequence) is not int
        or type(segment_index) is not int
        or first_sequence < 1
        or segment_index != manifest["segment_count"] - 1
        or last_key != _key(first_sequence)
        or type(entry.get("last_sequence")) is not int
        or entry["last_sequence"] != manifest["event_count"]
    ):
        raise LedgerV2Error("Workflow segment catalog tail does not match the manifest.")
    previous = catalog.floor_item(_key(first_sequence - 1)) if first_sequence > 1 else None
    if first_sequence > 1:
        if previous is None or not isinstance(previous[1], dict):
            raise LedgerV2Error("Workflow segment catalog tail has no contiguous predecessor.")
        previous_entry = previous[1]
        if (
            type(previous_entry.get("last_sequence")) is not int
            or previous_entry["last_sequence"] != first_sequence - 1
            or type(previous_entry.get("segment_index")) is not int
            or previous_entry["segment_index"] != segment_index - 1
        ):
            raise LedgerV2Error("Workflow segment catalog tail has no contiguous predecessor.")
    previous_digest = previous[1].get("sha256") if previous else None
    digest = entry.get("sha256")
    if not isinstance(digest, str) or not _HASH_RE.fullmatch(digest):
        raise LedgerV2Error("Workflow segment catalog tail digest is invalid.")
    segment = _read_segment(
        segment_fd,
        digest,
        segment_index,
        first_sequence,
        previous_digest,
        manifest,
    )
    if (
        digest != manifest["last_segment_sha256"]
        or type(entry.get("event_count")) is not int
        or entry["event_count"] != segment["event_count"]
        or type(entry.get("byte_length")) is not int
        or entry["byte_length"] != len(canonical_json_bytes(segment))
    ):
        raise LedgerV2Error("Workflow committed segment tail disagrees with its catalog or manifest.")


def _event_ref(sequence: int, segment_hash: str, offset: int) -> dict[str, Any]:
    return {"sequence": sequence, "segment_sha256": segment_hash, "offset": offset}


def _key(sequence: int) -> str:
    return f"{sequence:020d}"


def _read_events(
    manifest: dict[str, Any],
    segment_fd: int,
    index_fd: int,
) -> Iterator[tuple[int, dict[str, Any], dict[str, Any]]]:
    catalog = PersistentBTree(index_fd, manifest["roots"]["segment_catalog"])
    expected_index = 0
    expected_sequence = 1
    previous_digest = None
    for key, entry in catalog.items():
        if key != _key(expected_sequence) or not isinstance(entry, dict):
            raise LedgerV2Error("Workflow segment catalog is not contiguous.")
        if type(entry.get("segment_index")) is not int or entry["segment_index"] != expected_index:
            raise LedgerV2Error("Workflow segment catalog indexes are not contiguous.")
        digest = entry.get("sha256")
        segment = _read_segment(
            segment_fd,
            digest,
            expected_index,
            expected_sequence,
            previous_digest,
            manifest,
        )
        if (
            type(entry.get("first_sequence")) is not int
            or entry["first_sequence"] != segment["first_sequence"]
            or type(entry.get("last_sequence")) is not int
            or entry["last_sequence"] != segment["last_sequence"]
            or type(entry.get("event_count")) is not int
            or entry["event_count"] != segment["event_count"]
            or type(entry.get("byte_length")) is not int
            or entry["byte_length"] != len(canonical_json_bytes(segment))
        ):
            raise LedgerV2Error("Workflow segment catalog entry does not match its segment.")
        for offset, event in enumerate(segment["events"]):
            sequence = expected_sequence + offset
            yield sequence, event, _event_ref(sequence, digest, offset)
        expected_sequence += segment["event_count"]
        expected_index += 1
        previous_digest = digest
    if (
        expected_index != manifest["segment_count"]
        or expected_sequence - 1 != manifest["event_count"]
        or previous_digest != manifest["last_segment_sha256"]
    ):
        raise LedgerV2Error("Workflow segment catalog does not cover the committed history.")


def _phase_projection(
    manifest: dict[str, Any],
    indexes: IndexSet,
    event: dict[str, Any],
    sequence: int,
    ref: dict[str, Any],
    closeout_path: str | None,
) -> dict[str, Any]:
    from . import lifecycle

    lifecycle._validate_event_shape(event)
    checkpoint = dict(manifest["checkpoint"])
    phase = checkpoint["phase"]
    category = checkpoint["waiting_category"]
    reason = checkpoint["waiting_reason"]
    event_type = event["event_type"]
    summary = event["summary"]
    if indexes.tree("event_ids").get(event["event_id"]) is not None:
        raise LedgerV2Error("Workflow v2 append contains a duplicate event ID.")
    if checkpoint["last_timestamp"] is not None:
        from datetime import datetime

        if datetime.fromisoformat(event["timestamp"]) < datetime.fromisoformat(checkpoint["last_timestamp"]):
            raise LedgerV2Error("Workflow v2 events are not stored in chronological order.")
    if event.get("phase_before") != phase:
        raise LedgerV2Error("Workflow v2 event chronology has an inconsistent starting phase.")

    po_approved = event["po_approved"]
    actor_role = event["actor_role"]
    if event_type == lifecycle.VERIFICATION_EVENT_TYPE:
        from .context_verification import validate_verification_record

        if (
            actor_role != "coder"
            or po_approved
            or not validate_verification_record(event.get("context_verification"))
        ):
            raise LedgerV2Error("Context verification event is malformed or has invalid reported actor context.")
        phase_after, category_after, reason_after = phase, category, reason
    else:
        if phase == "CLOSED" and event_type != "milestone_reopened":
            raise LedgerV2Error("A closed milestone cannot accept additional workflow events.")
        if event_type in {
            "prompt_approved",
            "lock_in_approved",
            "acceptance_recorded",
            "milestone_closed",
            "milestone_reopened",
        } and (not po_approved or actor_role != "product_owner"):
            raise LedgerV2Error(f"{event_type} requires explicit Product Owner confirmation.")

        phase_after, category_after, reason_after = phase, category, reason
        if event_type == "milestone_reopened":
            target = event.get("reopen_target_phase")
            if phase != "CLOSED" or target not in {"REVIEW", "IN_PROGRESS"} or not summary.strip() or len(summary) > 1000:
                raise LedgerV2Error("Workflow reopen transition is invalid.")
            phase_after, category_after, reason_after = target, None, None
        elif event_type == "prompt_approved":
            if phase != "PLANNING":
                raise LedgerV2Error("Prompt approval is valid only while PLANNING.")
            phase_after = "READY"
        elif event_type == "implementation_started":
            if phase != "READY":
                raise LedgerV2Error("Implementation can start only from READY.")
            phase_after = "IN_PROGRESS"
        elif event_type == "question_submitted":
            if phase != "IN_PROGRESS":
                raise LedgerV2Error("A question can be submitted only while IN_PROGRESS.")
            if event.get("waiting_reason"):
                if event.get("waiting_category") not in lifecycle._WAITING_CATEGORIES:
                    raise LedgerV2Error("WAITING requires a supported waiting category.")
                phase_after, category_after, reason_after = (
                    "WAITING",
                    event["waiting_category"],
                    event["waiting_reason"],
                )
            elif event.get("waiting_category"):
                raise LedgerV2Error("A waiting category requires a non-empty reason.")
        elif event_type == "answer_recorded":
            if phase not in {"IN_PROGRESS", "WAITING"}:
                raise LedgerV2Error("An answer can be recorded only while active or waiting.")
            question = indexes.tree("questions").get(event.get("related_event_id", ""))
            if not isinstance(question, dict) or question.get("status") != "open":
                raise LedgerV2Error("An answer must reference a currently open question.")
        elif event_type in {"decision_recorded", "lock_in_approved"}:
            if phase not in {"PLANNING", "READY", "IN_PROGRESS", "WAITING", "REVIEW"}:
                raise LedgerV2Error("A decision cannot be recorded in the current phase.")
            expected_binding = event_type == "lock_in_approved"
            if event.get("binding") is not expected_binding:
                raise LedgerV2Error("Workflow decision binding marker is inconsistent.")
        elif event_type == "escalation_opened":
            if phase != "IN_PROGRESS" or not event.get("waiting_reason") or event.get("waiting_category") != "escalation":
                raise LedgerV2Error("Opening an escalation requires a reason while IN_PROGRESS.")
            phase_after, category_after, reason_after = "WAITING", "escalation", event["waiting_reason"]
        elif event_type == "escalation_resolved":
            if phase != "WAITING" or category != "escalation":
                raise LedgerV2Error("Only an open escalation can be resolved.")
        elif event_type == "bug_discovered":
            if phase not in {"IN_PROGRESS", "WAITING"}:
                raise LedgerV2Error("A bug can be discovered only while active or waiting.")
        elif event_type == "bug_classified":
            bug_id = event.get("related_event_id", "")
            bug = indexes.tree("bugs").get(bug_id)
            if not isinstance(bug, dict) or bug.get("classification_event_id") is not None:
                raise LedgerV2Error("Bug classification must reference an unclassified discovery.")
            scope = event.get("bug_scope")
            if scope == "in_scope":
                pass
            elif scope == "needs_escalation":
                if phase != "IN_PROGRESS" or not event.get("waiting_reason"):
                    raise LedgerV2Error("An out-of-scope bug needs an escalation reason while IN_PROGRESS.")
                phase_after, category_after, reason_after = "WAITING", "escalation", event["waiting_reason"]
            else:
                raise LedgerV2Error("Bug classification must be in_scope or needs_escalation.")
        elif event_type == "bug_fixed":
            bug = indexes.tree("bugs").get(event.get("related_event_id", ""))
            if (
                not isinstance(bug, dict)
                or bug.get("scope") != "in_scope"
                or bug.get("fix_event_id") is not None
            ):
                raise LedgerV2Error("A bug fix must reference an unfixed in-scope classification.")
            if phase not in {"IN_PROGRESS", "WAITING"}:
                raise LedgerV2Error("A bug can be fixed only while active or waiting.")
        elif event_type == "test_result_recorded":
            if phase not in {"IN_PROGRESS", "WAITING", "REVIEW"}:
                raise LedgerV2Error("Test results can be recorded only during implementation or review.")
            if not event.get("evidence_source") or not event.get("result"):
                raise LedgerV2Error("A test result requires its source and outcome.")
        elif event_type == "implementation_resumed":
            boundary = checkpoint.get("waiting_boundary")
            if phase != "WAITING" or not isinstance(boundary, dict) or not boundary.get("has_qualifying_resolution"):
                raise LedgerV2Error("A valid resolution after the active WAITING boundary is required.")
            if boundary.get("event_type") == "question_submitted":
                question = indexes.tree("questions").get(boundary["event_id"])
                resolution = boundary.get("first_resolution") or {}
                resolved_by_blocking_answer = (
                    resolution.get("event_type") == "answer_recorded"
                    and resolution.get("related_event_id") == boundary["event_id"]
                )
                if (
                    not isinstance(question, dict)
                    or (question.get("status") != "open" and not resolved_by_blocking_answer)
                ):
                    raise LedgerV2Error("The active blocking question is already answered.")
            phase_after, category_after, reason_after = "IN_PROGRESS", None, None
        elif event_type == "closeout_submitted":
            if phase != "IN_PROGRESS" or not event.get("evidence_path"):
                raise LedgerV2Error("Closeout submission requires IN_PROGRESS and an evidence path.")
            if closeout_path is not None and event.get("evidence_path") != closeout_path:
                raise LedgerV2Error("Closeout event references a missing or mismatched artifact.")
            phase_after, category_after, reason_after = "REVIEW", None, None
        elif event_type == "review_requested":
            if phase != "REVIEW":
                raise LedgerV2Error("A review can be requested only in REVIEW.")
        elif event_type == "correction_requested":
            if phase != "REVIEW":
                raise LedgerV2Error("A correction can be requested only in REVIEW.")
            phase_after, category_after, reason_after = "IN_PROGRESS", None, None
        elif event_type == "acceptance_recorded":
            if phase != "REVIEW" or event.get("binding") is not True:
                raise LedgerV2Error("Acceptance requires a binding Product Owner event in REVIEW.")
        elif event_type == "milestone_closed":
            last_acceptance = checkpoint.get("last_acceptance")
            if (
                phase != "REVIEW"
                or event.get("binding") is not True
                or not isinstance(last_acceptance, dict)
                or last_acceptance.get("sequence") != sequence - 1
            ):
                raise LedgerV2Error("Closure requires an immediately preceding acceptance event.")
            phase_after, category_after, reason_after = "CLOSED", None, None
        elif event_type == "git_disposition_recorded":
            pass
        else:
            raise LedgerV2Error(f"Unsupported workflow event type: {event_type}")

    if (
        event.get("phase_after") != phase_after
        or event.get("waiting_category_after") != category_after
        or event.get("waiting_reason_after") != reason_after
    ):
        raise LedgerV2Error("Workflow event chronology has an inconsistent resulting state.")

    if event_type == "question_submitted":
        indexes.set(
            "questions",
            event["event_id"],
            {
                "status": "open",
                "sequence": sequence,
                "event_id": event["event_id"],
                **ref,
            },
        )
    elif event_type == "answer_recorded":
        question_id = event["related_event_id"]
        question = indexes.tree("questions").get(question_id)
        question = dict(question)
        question.update(status="answered", answer_event_id=event["event_id"], answer_sequence=sequence)
        indexes.set("questions", question_id, question)

    if event_type == "bug_discovered":
        indexes.set(
            "bugs",
            event["event_id"],
            {
                "discovery_event_id": event["event_id"],
                "discovery_sequence": sequence,
                "classification_event_id": None,
                "scope": None,
                "fix_event_id": None,
                "discovery_ref": ref,
            },
        )
    elif event_type == "bug_classified":
        bug_id = event["related_event_id"]
        bug = dict(indexes.tree("bugs").get(bug_id))
        bug.update(
            classification_event_id=event["event_id"],
            classification_sequence=sequence,
            scope=event["bug_scope"],
            classification_ref=ref,
        )
        indexes.set("bugs", bug_id, bug)
    elif event_type == "bug_fixed":
        bug_id = event["related_event_id"]
        bug = dict(indexes.tree("bugs").get(bug_id))
        bug.update(fix_event_id=event["event_id"], fix_sequence=sequence, fix_ref=ref)
        indexes.set("bugs", bug_id, bug)

    if event_type in {"decision_recorded", "lock_in_approved", "acceptance_recorded", "prompt_approved"}:
        decision = {
            "event_id": event["event_id"],
            "event_type": event_type,
            "po_approved": po_approved,
            "binding": event.get("binding"),
            **ref,
        }
        indexes.set("decisions", _key(sequence), decision)
        if event_type in {"prompt_approved", "lock_in_approved", "acceptance_recorded"} and po_approved:
            checkpoint["latest_authoritative_decision"] = decision
    if event_type == "acceptance_recorded":
        checkpoint["last_acceptance"] = {"event_id": event["event_id"], "sequence": sequence, **ref}
    elif event_type != "milestone_closed":
        checkpoint["last_acceptance"] = None

    if event_type == "closeout_submitted":
        indexes.set(
            "closeouts",
            _key(sequence),
            {"event_id": event["event_id"], "evidence_path": event["evidence_path"], **ref},
        )
    if event.get("related_event_id") is not None:
        indexes.set(
            "relations",
            _key(sequence),
            {
                "event_id": event["event_id"],
                "related_event_id": event["related_event_id"],
                "event_type": event_type,
                **ref,
            },
        )
    if event_type == "milestone_closed":
        indexes.set(
            "relations",
            _key(sequence),
            {
                "event_id": event["event_id"],
                "related_event_id": checkpoint["last_acceptance"]["event_id"],
                "event_type": "acceptance_closure",
                **ref,
            },
        )
    if event_type == "milestone_reopened":
        indexes.set(
            "relations",
            _key(sequence),
            {
                "event_id": event["event_id"],
                "related_event_id": checkpoint["last_event_id"],
                "event_type": "closure_reopen",
                **ref,
            },
        )

    boundary = checkpoint.get("waiting_boundary")
    if phase_after == "WAITING" and phase != "WAITING":
        boundary = {
            "event_id": event["event_id"],
            "sequence": sequence,
            "event_type": event_type,
            "category": category_after,
            "reason": reason_after,
            "has_qualifying_resolution": False,
            "first_resolution": None,
        }
    elif phase == "WAITING" and event_type in {
        "answer_recorded",
        "decision_recorded",
        "lock_in_approved",
        "escalation_resolved",
        "bug_fixed",
    } and boundary is not None and not boundary.get("has_qualifying_resolution"):
        boundary = dict(boundary)
        boundary["has_qualifying_resolution"] = True
        boundary["first_resolution"] = {
            "event_id": event["event_id"],
            "sequence": sequence,
            "event_type": event_type,
            "related_event_id": event.get("related_event_id"),
            **ref,
        }
    if event_type == "implementation_resumed" or event_type == "milestone_reopened":
        boundary = None
    checkpoint.update(
        event_count=sequence,
        phase=phase_after,
        waiting_category=category_after,
        waiting_reason=reason_after,
        last_timestamp=event["timestamp"],
        last_event_id=event["event_id"],
        last_event_type=event_type,
        waiting_boundary=boundary,
    )
    indexes.set("event_ids", event["event_id"], {"event_type": event_type, **ref})
    return checkpoint


def _split_events(
    manifest: dict[str, Any], events: list[dict[str, Any]]
) -> list[list[dict[str, Any]]]:
    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for event in events:
        candidate = current + [event]
        if len(candidate) > MAX_SEGMENT_EVENTS:
            batches.append(current)
            current = [event]
            candidate = current
        batch_index = len(batches)
        first_sequence = manifest["event_count"] + 1 + sum(len(batch) for batch in batches)
        raw, _ = _segment_bytes(
            manifest,
            manifest["segment_count"] + batch_index,
            first_sequence,
            candidate,
            "0" * 64,
        )
        if len(raw) > MAX_SEGMENT_BYTES:
            if len(candidate) == 1:
                raise LedgerV2Error("A valid workflow event cannot fit in a segment.")
            batches.append(current)
            current = [event]
            first_sequence = manifest["event_count"] + 1 + sum(len(batch) for batch in batches)
            raw, _ = _segment_bytes(
                manifest,
                manifest["segment_count"] + len(batches),
                first_sequence,
                current,
                "0" * 64,
            )
            if len(raw) > MAX_SEGMENT_BYTES:
                raise LedgerV2Error("A valid workflow event cannot fit in a segment.")
        else:
            current = candidate
    if current:
        batches.append(current)
    return batches


def append_events(
    anchor: str | Path,
    repository_id: str,
    milestone_id: str,
    prompt_path: str,
    prompt_sha256: str,
    events: list[dict[str, Any]],
    *,
    state_root: str | Path,
    expected_generation: str,
    transaction_id: str,
    closeout_path: str | None = None,
    client_schema_version: int = LEDGER_SCHEMA_VERSION,
) -> dict[str, Any]:
    if client_schema_version != LEDGER_SCHEMA_VERSION:
        raise LedgerV2Error("This writer does not support the client workflow ledger version.")
    if not events:
        raise LedgerV2Error("A workflow transaction must contain at least one event.")
    if len(events) > MAX_SEGMENT_EVENTS:
        raise LedgerV2Error("A workflow transaction cannot contain more than 64 events.")
    if not _TX_RE.fullmatch(transaction_id):
        raise LedgerV2Error("Workflow transaction identifier must be 32 lowercase hexadecimal characters.")
    path = Path(anchor)
    with state_artifact_lock(state_root):
        manifest, anchor_hash = _read_anchor(
            path, repository_id, milestone_id, prompt_path, prompt_sha256
        )
        segment_fd, index_fd = _initial_root_dirs(path, create=False)
        try:
            _validate_index_roots(index_fd, manifest["roots"])
            _validate_append_tail(manifest, segment_fd, index_fd)
            indexes = _load_trees(index_fd, manifest["roots"])
            tx_tree = indexes.tree("transactions")
            existing_tx = tx_tree.get(transaction_id)
            payload_hash = sha256_bytes(canonical_json_bytes(events))
            if existing_tx is not None:
                if existing_tx.get("payload_sha256") != payload_hash:
                    raise LedgerV2Error("Transaction identifier was reused with different event data.")
                current, _ = _read_anchor(
                    path, repository_id, milestone_id, prompt_path, prompt_sha256
                )
                return {"manifest": current, "reconciled": True}
            if manifest["generation"] != expected_generation:
                raise LedgerV2Error("Workflow v2 generation changed; refresh before retrying.")
            pending = _read_pending_transaction(index_fd)
            if pending is not None:
                pending_tx = tx_tree.get(pending["transaction_id"])
                if pending_tx is not None:
                    if pending_tx.get("payload_sha256") != pending["payload_sha256"]:
                        raise LedgerV2Error(
                            "Workflow recovery record disagrees with the committed transaction index."
                        )
                    _clear_pending_transaction(index_fd)
                    pending = None
                elif (
                    pending["transaction_id"] != transaction_id
                    or pending["base_generation"] != manifest["generation"]
                    or pending["payload_sha256"] != payload_hash
                ):
                    raise LedgerV2Error(
                        f"Unresolved workflow transaction {pending['transaction_id']} "
                        "blocks writes; retry its exact transaction or reconcile it explicitly."
                    )
            batches = _split_events(manifest, events)
            starting_sequence = manifest["event_count"] + 1
            previous_digest = manifest["last_segment_sha256"]
            segment_records: list[tuple[str, bytes, int, int, int, int]] = []
            for batch_index, batch in enumerate(batches):
                segment_index = manifest["segment_count"] + batch_index
                first_sequence = starting_sequence + sum(len(items) for items in batches[:batch_index])
                raw, _ = _segment_bytes(
                    manifest, segment_index, first_sequence, batch, previous_digest
                )
                digest = sha256_bytes(raw)
                segment_records.append(
                    (digest, raw, segment_index, first_sequence, len(batch), len(raw))
                )
                previous_digest = digest

            next_manifest = json.loads(json.dumps(manifest))
            checkpoint = dict(next_manifest["checkpoint"])
            sequence = starting_sequence
            record_refs: list[tuple[dict[str, Any], int, dict[str, Any]]] = []
            for digest, _, segment_index, first_sequence, count, _ in segment_records:
                for offset in range(count):
                    record_refs.append(
                        (
                            events[sequence - starting_sequence],
                            sequence,
                            _event_ref(sequence, digest, offset),
                        )
                    )
                    sequence += 1
            for event, event_sequence, ref in record_refs:
                checkpoint = _phase_projection(
                    next_manifest,
                    indexes,
                    event,
                    event_sequence,
                    ref,
                    closeout_path,
                )
                next_manifest["checkpoint"] = checkpoint
                next_manifest["event_count"] = event_sequence
            if pending is None:
                _write_pending_transaction(
                    index_fd,
                    {
                        "schema_version": 1,
                        "transaction_id": transaction_id,
                        "base_generation": manifest["generation"],
                        "payload_sha256": payload_hash,
                        "segment_names": [
                            _segment_filename(segment_index, digest)
                            for digest, _, segment_index, _, _, _ in segment_records
                        ],
                    },
                )
            for digest, raw, segment_index, first_sequence, count, byte_length in segment_records:
                _write_new_file(segment_fd, _segment_filename(segment_index, digest), raw)
                indexes.set(
                    "segment_catalog",
                    _key(first_sequence),
                    {
                        "sha256": digest,
                        "segment_index": segment_index,
                        "first_sequence": first_sequence,
                        "last_sequence": first_sequence + count - 1,
                        "event_count": count,
                        "byte_length": byte_length,
                    },
                )
            first_sequence = starting_sequence
            last_sequence = starting_sequence + len(events) - 1
            indexes.set(
                "transactions",
                transaction_id,
                {
                    "payload_sha256": payload_hash,
                    "first_sequence": first_sequence,
                    "last_sequence": last_sequence,
                    "event_ids": [event["event_id"] for event in events],
                },
            )
            next_manifest["generation"] = uuid.uuid4().hex
            next_manifest["transaction_id"] = transaction_id
            next_manifest["event_count"] = last_sequence
            next_manifest["last_sequence"] = last_sequence
            next_manifest["last_event_id"] = events[-1]["event_id"]
            next_manifest["segment_count"] += len(segment_records)
            next_manifest["last_segment_sha256"] = segment_records[-1][0]
            next_manifest["checkpoint"] = checkpoint
            next_manifest["history_verified_generation"] = None
            indexes.flush()
            next_manifest["roots"] = indexes.roots
            _validate_manifest(
                next_manifest, repository_id, milestone_id, prompt_path, prompt_sha256
            )
            _sync_artifact_dirs(path, segment_fd, index_fd)
            try:
                _commit_anchor(
                    path,
                    _anchor_text(next_manifest),
                    anchor_hash,
                    transaction_id=transaction_id,
                )
            except CommitUncertainError:
                current, _ = _read_anchor(
                    path, repository_id, milestone_id, prompt_path, prompt_sha256
                )
                if current.get("transaction_id") == transaction_id:
                    raise CommitUncertainError(
                        "Workflow anchor names this transaction, but directory durability is uncertain."
                    )
                raise
            try:
                _clear_pending_transaction(index_fd)
            except LedgerV2Error as exc:
                raise LedgerV2Error(
                    "Workflow transaction committed, but its recovery record remains unresolved; "
                    "retry the same transaction."
                ) from exc
            return {"manifest": next_manifest, "reconciled": False}
        finally:
            os.close(index_fd)
            os.close(segment_fd)


def create_migrated_v2(
    anchor: str | Path,
    repository_id: str,
    milestone_id: str,
    prompt_path: str,
    prompt_sha256: str,
    events: list[dict[str, Any]],
    v1_image: bytes,
    *,
    state_root: str | Path,
    transaction_id: str,
    closeout_path: str | None = None,
    fixture_only: bool = False,
) -> dict[str, Any]:
    if fixture_only is not True:
        raise LedgerV2Error("Workflow v1 migration is restricted to disposable fixtures.")
    if not _TX_RE.fullmatch(transaction_id):
        raise LedgerV2Error("Workflow transaction identifier must be 32 lowercase hexadecimal characters.")
    if not isinstance(v1_image, bytes):
        raise LedgerV2Error("Migration source image must be immutable bytes.")
    from . import lifecycle

    if len(v1_image) > lifecycle._MAX_SIDECAR_BYTES or len(events) > lifecycle._MAX_EVENTS:
        raise LedgerV2Error("Production v1 limits reject this migration source.")
    source_digest = sha256_bytes(v1_image)
    with state_artifact_lock(state_root):
        if Path(anchor).is_symlink():
            raise LedgerV2Error("Migration anchor is a symlink.")
        parent_fd = _safe_dir(Path(anchor).parent)
        try:
            raw = _read_named_file(parent_fd, Path(anchor).name, lifecycle._MAX_SIDECAR_BYTES)
        finally:
            os.close(parent_fd)
        if raw != v1_image:
            try:
                committed, _ = _read_anchor(
                    Path(anchor), repository_id, milestone_id, prompt_path, prompt_sha256
                )
            except LedgerV2Error as exc:
                raise LedgerV2Error("Migration source changed before fixture conversion.") from exc
            source = committed.get("migration_source")
            if (
                committed.get("transaction_id") != transaction_id
                or not isinstance(source, dict)
                or source.get("sha256") != source_digest
                or source.get("byte_length") != len(v1_image)
            ):
                raise LedgerV2Error(
                    "Workflow anchor does not reconcile to this migration transaction and source image."
                )
            audit = audit_history(
                anchor,
                repository_id,
                milestone_id,
                prompt_path,
                prompt_sha256,
                closeout_path=closeout_path,
            )
            if not audit["history_verified"]:
                raise LedgerV2Error("Committed workflow migration did not pass complete history audit.")
            return committed
        parsed = _parse_v1_image(
            raw,
            repository_id,
            milestone_id,
            prompt_path,
            closeout_path,
        )
        if parsed["events"] != events:
            raise LedgerV2Error("Migration input differs from the fully validated v1 source.")
        segment_fd, index_fd = _initial_root_dirs(Path(anchor), create=True)
        try:
            manifest = _base_manifest(
                repository_id, milestone_id, prompt_path, prompt_sha256, transaction_id
            )
            manifest["migration_source"] = {
                "sha256": source_digest,
                "byte_length": len(v1_image),
                "path": f"v1-source-{source_digest}.md",
            }
            _write_new_file(
                segment_fd,
                f"v1-source-{source_digest}.md",
                v1_image,
                maximum=lifecycle._MAX_SIDECAR_BYTES,
            )
            if events:
                result = _migration_build(
                    manifest, events, segment_fd, index_fd, transaction_id, closeout_path
                )
                manifest = result
            _validate_manifest(manifest, repository_id, milestone_id, prompt_path, prompt_sha256)
            _verify_manifest_history(manifest, segment_fd, index_fd, closeout_path)
            _sync_artifact_dirs(Path(anchor), segment_fd, index_fd)
            old_hash = sha256_bytes(raw)
            _commit_anchor(
                Path(anchor),
                _anchor_text(manifest),
                old_hash,
                expected_old_maximum=lifecycle._MAX_SIDECAR_BYTES,
                transaction_id=transaction_id,
            )
            return manifest
        finally:
            os.close(index_fd)
            os.close(segment_fd)


def _migration_build(
    manifest: dict[str, Any],
    events: list[dict[str, Any]],
    segment_fd: int,
    index_fd: int,
    transaction_id: str,
    closeout_path: str | None,
) -> dict[str, Any]:
    memory = {name: {} for name in _ROOT_NAMES}
    indexes = IndexSet(index_fd, dict(manifest["roots"]), memory)
    batches = _split_events(manifest, events)
    previous_digest = None
    sequence = 1
    transaction_ids: list[str] = []
    transaction_payload = canonical_json_bytes(events)
    for segment_index, batch in enumerate(batches):
        first_sequence = sequence
        raw, _ = _segment_bytes(manifest, segment_index, sequence, batch, previous_digest)
        digest = sha256_bytes(raw)
        _write_new_file(segment_fd, _segment_filename(segment_index, digest), raw)
        for offset, event in enumerate(batch):
            ref = _event_ref(sequence, digest, offset)
            checkpoint = _phase_projection(manifest, indexes, event, sequence, ref, closeout_path)
            manifest["checkpoint"] = checkpoint
            manifest["event_count"] = sequence
            manifest["last_sequence"] = sequence
            manifest["last_event_id"] = event["event_id"]
            transaction_ids.append(event["event_id"])
            sequence += 1
        indexes.set(
            "segment_catalog",
            _key(first_sequence),
            {
                "sha256": digest,
                "segment_index": segment_index,
                "first_sequence": first_sequence,
                "last_sequence": sequence - 1,
                "event_count": len(batch),
                "byte_length": len(raw),
            },
        )
        previous_digest = digest
    indexes.set(
        "transactions",
        transaction_id,
        {
            "payload_sha256": sha256_bytes(transaction_payload),
            "first_sequence": 1,
            "last_sequence": len(events),
            "event_ids_sha256": sha256_bytes(canonical_json_bytes(transaction_ids)),
        },
    )
    for name in _ROOT_NAMES:
        indexes.roots[name] = PersistentBTree.bulk_load(
            index_fd,
            sorted(memory[name].items()),
        )
    indexes.memory = None
    manifest.update(
        generation=uuid.uuid4().hex,
        transaction_id=transaction_id,
        event_count=len(events),
        last_sequence=len(events),
        last_event_id=events[-1]["event_id"] if events else None,
        segment_count=len(batches),
        last_segment_sha256=previous_digest,
        roots=indexes.roots,
        checkpoint=checkpoint,
        history_verified_generation=None,
    )
    return manifest


def _verify_manifest_history(
    manifest: dict[str, Any],
    segment_fd: int,
    index_fd: int,
    closeout_path: str | None,
) -> tuple[dict[str, Any], IndexSet]:
    indexes = _load_trees(index_fd, manifest["roots"])
    replayed, _ = _audit_replay(
        _read_events(manifest, segment_fd, index_fd),
        manifest,
        indexes,
        closeout_path,
    )
    _verify_tree_roots(indexes)
    checkpoint = manifest["checkpoint"]
    if (
        replayed["phase"] != checkpoint["phase"]
        or replayed["waiting_category"] != checkpoint["waiting_category"]
        or replayed["waiting_reason"] != checkpoint["waiting_reason"]
        or checkpoint["event_count"] != manifest["event_count"]
        or replayed["last_event_id"] != manifest["last_event_id"]
    ):
        raise LedgerV2Error("Workflow v2 checkpoint disagrees with complete event replay.")
    if manifest.get("migration_source") is not None:
        from . import lifecycle

        source = manifest["migration_source"]
        raw_source = _read_named_file(
            segment_fd,
            source["path"],
            lifecycle._MAX_SIDECAR_BYTES,
        )
        try:
            source_version = dispatch_marker(raw_source.decode("utf-8"))
        except (LedgerV2Error, UnicodeDecodeError) as exc:
            raise LedgerV2Error("Workflow v2 migration source image failed integrity validation.") from exc
        if (
            len(raw_source) != source["byte_length"]
            or sha256_bytes(raw_source) != source["sha256"]
            or source_version != 1
        ):
            raise LedgerV2Error("Workflow v2 migration source image failed integrity validation.")
    return replayed, indexes


def audit_history(
    anchor: str | Path,
    repository_id: str,
    milestone_id: str,
    prompt_path: str,
    prompt_sha256: str,
    *,
    closeout_path: str | None = None,
) -> dict[str, Any]:
    path = Path(anchor)
    manifest, anchor_hash = _read_anchor(path, repository_id, milestone_id, prompt_path, prompt_sha256)
    segment_fd, index_fd = _initial_root_dirs(path, create=False)
    try:
        replayed, indexes = _verify_manifest_history(
            manifest,
            segment_fd,
            index_fd,
            closeout_path,
        )
        pending_error = None
        try:
            pending = _read_pending_transaction(index_fd)
        except LedgerV2Error as exc:
            pending = None
            pending_error = str(exc)
        recovery_status = "clean"
        pending_transaction_id = None
        pending_segment_names: list[str] = []
        if pending_error is not None:
            recovery_status = "invalid_pending_marker"
        elif pending is not None:
            pending_transaction_id = pending["transaction_id"]
            committed = indexes.tree("transactions").get(pending_transaction_id)
            if committed is None:
                recovery_status = "unresolved"
                pending_segment_names = pending["segment_names"]
            elif committed.get("payload_sha256") == pending["payload_sha256"]:
                recovery_status = "committed_marker_pending_cleanup"
            else:
                raise LedgerV2Error(
                    "Workflow recovery record disagrees with the committed transaction index."
                )
        pending_temp_count, pending_temp_names = _pending_temporary_artifacts(index_fd)
        if recovery_status == "clean" and pending_temp_count:
            recovery_status = "orphan_pending_temporary"
        if _read_anchor(path, repository_id, milestone_id, prompt_path, prompt_sha256)[1] != anchor_hash:
            raise StaleCursorError("Workflow generation changed while the full audit was running.")
        return {
            "generation": manifest["generation"],
            "event_count": manifest["event_count"],
            "history_verified": True,
            "phase": replayed["phase"],
            "index_pages_verified": True,
            "segment_chain_verified": True,
            "recovery_status": recovery_status,
            "recovery_error": pending_error,
            "pending_transaction_id": pending_transaction_id,
            "pending_segment_names": pending_segment_names,
            "pending_temporary_count": pending_temp_count,
            "pending_temporary_names": pending_temp_names,
            "omitted_pending_temporary_count": max(
                0, pending_temp_count - len(pending_temp_names)
            ),
        }
    finally:
        os.close(index_fd)
        os.close(segment_fd)


def _synthetic_previous(
    questions: dict[str, tuple[int, dict[str, Any]]],
    unclassified_bugs: dict[str, tuple[int, dict[str, Any]]],
    fixable_bugs: dict[str, tuple[tuple[int, dict[str, Any]], tuple[int, dict[str, Any]]]],
    waiting_boundary: tuple[int, dict[str, Any]] | None,
    waiting_resolution: tuple[int, dict[str, Any]] | None,
    last_event: tuple[int, dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    relevant: dict[str, tuple[int, dict[str, Any]]] = {}
    relevant.update(questions)
    relevant.update(unclassified_bugs)
    for discovery, classification in fixable_bugs.values():
        relevant[discovery[1]["event_id"]] = discovery
        relevant[classification[1]["event_id"]] = classification
    for item in (waiting_boundary, waiting_resolution, last_event):
        if item is not None:
            relevant[item[1]["event_id"]] = item
    return [event for _, event in sorted(relevant.values(), key=lambda item: item[0])]


def _audit_replay(
    records: Iterator[tuple[int, dict[str, Any], dict[str, Any]]],
    manifest: dict[str, Any],
    indexes: IndexSet,
    closeout_path: str | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    from . import lifecycle

    phase = "PLANNING"
    waiting_category = None
    waiting_reason = None
    previous_timestamp = None
    previous_timestamp_text = None
    previous_event: tuple[int, dict[str, Any]] | None = None
    waiting_boundary: tuple[int, dict[str, Any]] | None = None
    waiting_resolution: tuple[int, dict[str, Any]] | None = None
    last_acceptance: dict[str, Any] | None = None
    latest_authoritative_decision: dict[str, Any] | None = None
    questions: dict[str, tuple[int, dict[str, Any]]] = {}
    unclassified_bugs: dict[str, tuple[int, dict[str, Any]]] = {}
    fixable_bugs: dict[str, tuple[tuple[int, dict[str, Any]], tuple[int, dict[str, Any]]]] = {}
    seen_ids: set[str] = set()
    counters = {
        "event_ids": 0,
        "questions": 0,
        "bugs": 0,
        "decisions": 0,
        "closeouts": 0,
        "relations": 0,
    }
    transaction_entries = list(indexes.tree("transactions").items())
    if any(
        not _TX_RE.fullmatch(key)
        or not isinstance(value, dict)
        or type(value.get("first_sequence")) is not int
        or type(value.get("last_sequence")) is not int
        or not _HASH_RE.fullmatch(str(value.get("payload_sha256", "")))
        for key, value in transaction_entries
    ):
        raise LedgerV2Error("Workflow transaction index contains malformed entries.")
    transaction_entries.sort(key=lambda item: item[1]["first_sequence"])
    expected_transaction_start = 1
    for _, transaction in transaction_entries:
        if (
            transaction["first_sequence"] != expected_transaction_start
            or transaction["last_sequence"] < transaction["first_sequence"]
            or transaction["last_sequence"] - transaction["first_sequence"]
            >= (2000 if "event_ids_sha256" in transaction else MAX_SEGMENT_EVENTS)
        ):
            raise LedgerV2Error("Workflow transaction ranges are not contiguous or bounded.")
        expected_transaction_start = transaction["last_sequence"] + 1
    if (
        expected_transaction_start != manifest["event_count"] + 1
        or (transaction_entries and transaction_entries[-1][0] != manifest["transaction_id"])
    ):
        raise LedgerV2Error("Workflow transaction index does not match the manifest.")
    tx_index = 0
    transaction_ids: list[str] = []
    transaction_events: list[dict[str, Any]] = []

    for sequence, event, ref in records:
        lifecycle._validate_event_shape(event)
        if sequence != counters["event_ids"] + 1 or event["event_id"] in seen_ids:
            raise LedgerV2Error("Workflow history contains a gap or duplicate event ID.")
        seen_ids.add(event["event_id"])
        event_index = indexes.tree("event_ids").get(event["event_id"])
        if event_index != {"event_type": event["event_type"], **ref}:
            raise LedgerV2Error("Workflow event ID index disagrees with immutable history.")
        timestamp = datetime.fromisoformat(event["timestamp"])
        if previous_timestamp is not None and timestamp < previous_timestamp:
            raise LedgerV2Error("Workflow events are not stored in chronological order.")
        if event.get("phase_before") != phase:
            raise LedgerV2Error("Workflow event chronology has an inconsistent starting phase.")
        prior = _synthetic_previous(
            questions,
            unclassified_bugs,
            fixable_bugs,
            waiting_boundary,
            waiting_resolution,
            previous_event,
        )
        try:
            next_phase, next_category, next_reason = lifecycle._apply_event(
                phase,
                waiting_category,
                waiting_reason,
                event,
                prior,
            )
        except lifecycle.WorkflowRecordError as exc:
            raise LedgerV2Error(f"Workflow v2 full replay rejected event {sequence}: {exc}") from exc
        if (
            event.get("phase_after") != next_phase
            or event.get("waiting_category_after") != next_category
            or event.get("waiting_reason_after") != next_reason
        ):
            raise LedgerV2Error("Workflow event chronology has an inconsistent resulting state.")

        event_type = event["event_type"]
        if event_type == "question_submitted":
            counters["questions"] += 1
            indexed = indexes.tree("questions").get(event["event_id"])
            if (
                not isinstance(indexed, dict)
                or indexed.get("sequence") != sequence
                or indexed.get("event_id") != event["event_id"]
                or {key: indexed.get(key) for key in ("segment_sha256", "offset")}
                != {key: ref[key] for key in ("segment_sha256", "offset")}
            ):
                raise LedgerV2Error("Workflow question index is missing or misbinds a question.")
            questions[event["event_id"]] = (sequence, event)
        elif event_type == "answer_recorded":
            question_id = event.get("related_event_id")
            indexed = indexes.tree("questions").get(question_id)
            if (
                not isinstance(indexed, dict)
                or indexed.get("answer_event_id") != event["event_id"]
                or indexed.get("answer_sequence") != sequence
                or indexed.get("status") != "answered"
                or question_id not in questions
            ):
                raise LedgerV2Error("Workflow question answer relation is not indexed exactly.")
            questions.pop(question_id)
        elif event_type == "bug_discovered":
            counters["bugs"] += 1
            indexed = indexes.tree("bugs").get(event["event_id"])
            if (
                not isinstance(indexed, dict)
                or indexed.get("discovery_sequence") != sequence
                or indexed.get("discovery_ref") != ref
                or indexed.get("discovery_event_id") != event["event_id"]
            ):
                raise LedgerV2Error("Workflow bug index is missing or misbinds a discovery.")
            unclassified_bugs[event["event_id"]] = (sequence, event)
        elif event_type == "bug_classified":
            bug_id = event.get("related_event_id")
            indexed = indexes.tree("bugs").get(bug_id)
            if (
                not isinstance(indexed, dict)
                or indexed.get("classification_event_id") != event["event_id"]
                or indexed.get("classification_sequence") != sequence
                or indexed.get("classification_ref") != ref
                or indexed.get("scope") != event.get("bug_scope")
                or bug_id not in unclassified_bugs
            ):
                raise LedgerV2Error("Workflow bug classification relation is not indexed exactly.")
            discovery = unclassified_bugs.pop(bug_id)
            if event["bug_scope"] == "in_scope":
                fixable_bugs[bug_id] = (discovery, (sequence, event))
        elif event_type == "bug_fixed":
            bug_id = event.get("related_event_id")
            indexed = indexes.tree("bugs").get(bug_id)
            if (
                not isinstance(indexed, dict)
                or indexed.get("fix_event_id") != event["event_id"]
                or indexed.get("fix_sequence") != sequence
                or indexed.get("fix_ref") != ref
                or bug_id not in fixable_bugs
            ):
                raise LedgerV2Error("Workflow bug fix relation is not indexed exactly.")
            fixable_bugs.pop(bug_id)

        if event_type in {
            "decision_recorded",
            "lock_in_approved",
            "acceptance_recorded",
            "prompt_approved",
        }:
            counters["decisions"] += 1
            expected_decision = {
                "event_id": event["event_id"],
                "event_type": event_type,
                "po_approved": event["po_approved"],
                "binding": event.get("binding"),
                **ref,
            }
            if indexes.tree("decisions").get(_key(sequence)) != expected_decision:
                raise LedgerV2Error("Workflow decision index disagrees with immutable history.")
        if event_type == "closeout_submitted":
            counters["closeouts"] += 1
            if closeout_path is None or event.get("evidence_path") != closeout_path:
                raise LedgerV2Error("Workflow closeout history references a missing or mismatched artifact.")
            if indexes.tree("closeouts").get(_key(sequence)) != {
                "event_id": event["event_id"],
                "evidence_path": event["evidence_path"],
                **ref,
            }:
                raise LedgerV2Error("Workflow closeout index disagrees with immutable history.")
        relation = None
        if event.get("related_event_id") is not None:
            relation = {
                "event_id": event["event_id"],
                "related_event_id": event["related_event_id"],
                "event_type": event_type,
                **ref,
            }
        elif event_type in {"milestone_closed", "milestone_reopened"}:
            if previous_event is None:
                raise LedgerV2Error("Workflow closure or reopen lacks its preceding event.")
            relation = {
                "event_id": event["event_id"],
                "related_event_id": previous_event[1]["event_id"],
                "event_type": "acceptance_closure" if event_type == "milestone_closed" else "closure_reopen",
                **ref,
            }
        if relation is not None:
            counters["relations"] += 1
            if indexes.tree("relations").get(_key(sequence)) != relation:
                raise LedgerV2Error("Workflow relation index disagrees with immutable history.")

        if next_phase == "WAITING" and phase != "WAITING":
            waiting_boundary = (sequence, event)
            waiting_resolution = None
        elif phase == "WAITING" and event_type in {
            "answer_recorded",
            "decision_recorded",
            "lock_in_approved",
            "escalation_resolved",
            "bug_fixed",
        } and waiting_boundary is not None and waiting_resolution is None:
            waiting_resolution = (sequence, event)
        if event_type in {"implementation_resumed", "milestone_reopened"}:
            waiting_boundary = None
            waiting_resolution = None
        if event_type == "acceptance_recorded":
            last_acceptance = {"event_id": event["event_id"], "sequence": sequence, **ref}
        elif event_type != "milestone_closed":
            last_acceptance = None
        if event_type in {
            "prompt_approved",
            "lock_in_approved",
            "acceptance_recorded",
        } and event["po_approved"]:
            latest_authoritative_decision = {
                "event_id": event["event_id"],
                "event_type": event_type,
                "po_approved": event["po_approved"],
                "binding": event.get("binding"),
                **ref,
            }

        if tx_index >= len(transaction_entries):
            raise LedgerV2Error("Workflow transaction index does not cover the event stream.")
        transaction_id, transaction = transaction_entries[tx_index]
        if sequence == transaction.get("first_sequence"):
            transaction_ids = []
            transaction_events = []
        if not transaction.get("first_sequence") <= sequence <= transaction.get("last_sequence"):
            raise LedgerV2Error("Workflow transaction ranges omit or overlap an event.")
        transaction_ids.append(event["event_id"])
        transaction_events.append(event)
        if sequence == transaction.get("last_sequence"):
            if (
                transaction.get("payload_sha256") != sha256_bytes(canonical_json_bytes(transaction_events))
                or transaction.get("event_ids_sha256")
                not in (None, sha256_bytes(canonical_json_bytes(transaction_ids)))
                or transaction.get("event_ids")
                not in (None, transaction_ids)
            ):
                raise LedgerV2Error(f"Workflow transaction {transaction_id} does not match its event range.")
            tx_index += 1

        counters["event_ids"] += 1
        previous_timestamp = timestamp
        previous_timestamp_text = event["timestamp"]
        previous_event = (sequence, event)
        phase, waiting_category, waiting_reason = next_phase, next_category, next_reason

    if counters["event_ids"] != manifest["event_count"] or tx_index != len(transaction_entries):
        raise LedgerV2Error("Workflow transaction or event indexes do not cover the committed history.")
    if (
        len(list(indexes.tree("questions").items())) != counters["questions"]
        or len(list(indexes.tree("bugs").items())) != counters["bugs"]
        or len(list(indexes.tree("decisions").items())) != counters["decisions"]
        or len(list(indexes.tree("closeouts").items())) != counters["closeouts"]
        or len(list(indexes.tree("relations").items())) != counters["relations"]
        or len(list(indexes.tree("event_ids").items())) != counters["event_ids"]
    ):
        raise LedgerV2Error("Workflow projection index contains missing or extra entries.")
    checkpoint = manifest["checkpoint"]
    expected_waiting_boundary = None
    if waiting_boundary is not None:
        boundary_sequence, boundary_event = waiting_boundary
        first_resolution = None
        if waiting_resolution is not None:
            resolution_sequence, resolution_event = waiting_resolution
            resolution_ref = indexes.tree("event_ids").get(resolution_event["event_id"])
            first_resolution = {
                "event_id": resolution_event["event_id"],
                "sequence": resolution_sequence,
                "event_type": resolution_event["event_type"],
                "related_event_id": resolution_event.get("related_event_id"),
                "segment_sha256": resolution_ref["segment_sha256"],
                "offset": resolution_ref["offset"],
            }
        expected_waiting_boundary = {
            "event_id": boundary_event["event_id"],
            "sequence": boundary_sequence,
            "event_type": boundary_event["event_type"],
            "category": boundary_event.get("waiting_category_after"),
            "reason": boundary_event.get("waiting_reason_after"),
            "has_qualifying_resolution": first_resolution is not None,
            "first_resolution": first_resolution,
        }
    if (
        checkpoint.get("last_timestamp") != previous_timestamp_text
        or checkpoint.get("last_event_type")
        != (previous_event[1]["event_type"] if previous_event else None)
        or checkpoint.get("waiting_boundary") != expected_waiting_boundary
        or checkpoint.get("last_acceptance") != last_acceptance
        or checkpoint.get("latest_authoritative_decision") != latest_authoritative_decision
    ):
        raise LedgerV2Error("Workflow lifecycle checkpoint disagrees with event history.")
    if any(
        not isinstance((question := indexes.tree("questions").get(question_id)), dict)
        or question.get("status") != "open"
        for question_id in questions
    ):
        raise LedgerV2Error("Workflow open-question index disagrees with event history.")
    return {
        "phase": phase,
        "waiting_category": waiting_category,
        "waiting_reason": waiting_reason,
        "last_event_id": previous_event[1]["event_id"] if previous_event else None,
    }, counters


def _verify_tree_roots(indexes: IndexSet) -> None:
    for name in _ROOT_NAMES:
        indexes.tree(name)
        if indexes.cache is not None:
            indexes.cache[name].validate()


def page_events(
    anchor: str | Path,
    repository_id: str,
    milestone_id: str,
    prompt_path: str,
    prompt_sha256: str,
    *,
    limit: int = 100,
    max_bytes: int = MAX_EVENT_PAGE_BYTES,
    cursor: str | None = None,
) -> dict[str, Any]:
    if type(limit) is not int or not 1 <= limit <= MAX_SEGMENT_EVENTS * 10:
        raise LedgerV2Error("Workflow event page limit is outside the supported range.")
    if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_EVENT_PAGE_BYTES:
        raise LedgerV2Error("Workflow event page byte limit is outside the supported range.")
    path = Path(anchor)
    manifest, anchor_hash = _read_anchor(path, repository_id, milestone_id, prompt_path, prompt_sha256)
    next_sequence = 1
    if cursor:
        try:
            raw_cursor = base64.urlsafe_b64decode(cursor.encode("ascii") + b"=" * (-len(cursor) % 4))
            value = json.loads(raw_cursor)
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LedgerV2Error("Workflow event cursor is malformed.") from exc
        if (
            not isinstance(value, dict)
            or value.get("generation") != manifest["generation"]
            or value.get("anchor_sha256") != anchor_hash
            or value.get("repository_id") != repository_id
            or value.get("milestone_id") != milestone_id
            or value.get("prompt_sha256") != prompt_sha256
        ):
            raise StaleCursorError("Workflow event cursor belongs to a different snapshot.")
        next_sequence = value.get("next_sequence")
        if type(next_sequence) is not int or not 1 <= next_sequence <= manifest["event_count"] + 1:
            raise LedgerV2Error("Workflow event cursor sequence is invalid.")
    segment_fd, index_fd = _initial_root_dirs(path, create=False)
    try:
        selected: list[dict[str, Any]] = []
        size = 2
        catalog = PersistentBTree(index_fd, manifest["roots"]["segment_catalog"])
        start = catalog.floor_item(_key(next_sequence))
        stop = False
        if manifest["event_count"] and start is None:
            raise LedgerV2Error("Workflow segment catalog has no entry for the requested sequence.")
        if start is not None:
            start_key, start_entry = start
            if not isinstance(start_entry, dict):
                raise LedgerV2Error("Workflow segment catalog entry is malformed.")
            expected_sequence = start_entry.get("first_sequence")
            expected_index = start_entry.get("segment_index")
            if (
                type(expected_sequence) is not int
                or type(expected_index) is not int
                or type(start_entry.get("last_sequence")) is not int
                or expected_sequence > next_sequence
                or start_entry.get("last_sequence", 0) < next_sequence
            ):
                raise LedgerV2Error("Workflow segment catalog does not cover the requested sequence.")
            previous = catalog.floor_item(_key(expected_sequence - 1)) if expected_sequence > 1 else None
            if previous is not None and not isinstance(previous[1], dict):
                raise LedgerV2Error("Workflow segment catalog predecessor is malformed.")
            previous_digest = previous[1].get("sha256") if previous else None
            for catalog_key, entry in catalog.items_from(start_key):
                if (
                    catalog_key != _key(expected_sequence)
                    or not isinstance(entry, dict)
                    or type(entry.get("segment_index")) is not int
                    or entry.get("segment_index") != expected_index
                ):
                    raise LedgerV2Error("Workflow segment catalog is not contiguous.")
                digest = entry.get("sha256")
                segment = _read_segment(
                    segment_fd,
                    digest,
                    expected_index,
                    expected_sequence,
                    previous_digest,
                    manifest,
                )
                if (
                    type(entry.get("last_sequence")) is not int
                    or entry["last_sequence"] != segment["last_sequence"]
                    or type(entry.get("event_count")) is not int
                    or entry["event_count"] != segment["event_count"]
                    or type(entry.get("byte_length")) is not int
                    or entry["byte_length"] != len(canonical_json_bytes(segment))
                ):
                    raise LedgerV2Error("Workflow segment catalog entry does not match its segment.")
                for offset, event in enumerate(segment["events"]):
                    sequence = expected_sequence + offset
                    if sequence < next_sequence:
                        continue
                    encoded = canonical_json_bytes(event)
                    addition = len(encoded) + (1 if selected else 0)
                    if selected and (len(selected) >= limit or size + addition > max_bytes):
                        stop = True
                        break
                    if not selected and size + addition > max_bytes:
                        raise LedgerV2Error("A workflow event cannot fit in the requested page bound.")
                    selected.append(event)
                    size += addition
                if stop or len(selected) >= limit:
                    break
                expected_sequence = segment["last_sequence"] + 1
                expected_index += 1
                previous_digest = digest
        if _read_anchor(path, repository_id, milestone_id, prompt_path, prompt_sha256)[1] != anchor_hash:
            raise StaleCursorError("Workflow generation changed while reading the event page.")

        def result_page() -> dict[str, Any]:
            last_sequence = next_sequence + len(selected) - 1
            has_more = last_sequence < manifest["event_count"]
            next_cursor = None
            if has_more:
                cursor_value = {
                    "generation": manifest["generation"],
                    "anchor_sha256": anchor_hash,
                    "repository_id": repository_id,
                    "milestone_id": milestone_id,
                    "prompt_sha256": prompt_sha256,
                    "next_sequence": last_sequence + 1,
                }
                next_cursor = base64.urlsafe_b64encode(
                    canonical_json_bytes(cursor_value)
                ).decode("ascii").rstrip("=")
            return {
                "generation": manifest["generation"],
                "total_count": manifest["event_count"],
                "first_sequence": next_sequence if selected else None,
                "last_sequence": last_sequence if selected else None,
                "events": selected,
                "has_more": has_more,
                "next_cursor": next_cursor,
                "page_integrity_verified": True,
                "history_verified": False,
                "completeness": "page_only",
            }

        page = result_page()
        while len(canonical_json_bytes(page)) > max_bytes and selected:
            selected.pop()
            page = result_page()
        if len(canonical_json_bytes(page)) > max_bytes:
            raise LedgerV2Error("Workflow event page metadata exceeds the requested byte bound.")
        return page
    finally:
        os.close(index_fd)
        os.close(segment_fd)


def _parse_v1_image(
    raw: bytes,
    repository_id: str,
    milestone_id: str,
    prompt_path: str,
    closeout_path: str | None,
) -> dict[str, Any]:
    from . import lifecycle

    if len(raw) > lifecycle._MAX_SIDECAR_BYTES:
        raise LedgerV2Error("Workflow v1 source exceeds its existing byte limit.")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise LedgerV2Error("Workflow v1 source is not valid UTF-8.") from exc
    if dispatch_marker(text) != 1:
        raise LedgerV2Error("Fixture migration requires an unchanged v1 anchor.")
    manifest = _extract_json(text, V1_MARKER)
    if (
        manifest.get("schema_version") != lifecycle.WORKFLOW_LEDGER_SCHEMA_VERSION
        or manifest.get("repository_id") != repository_id
        or manifest.get("milestone_id") != milestone_id
        or manifest.get("prompt_path") != prompt_path
        or not isinstance(manifest.get("events"), list)
    ):
        raise LedgerV2Error("Workflow v1 metadata does not match the migration fixture.")
    events = manifest["events"]
    if len(events) > lifecycle._MAX_EVENTS:
        raise LedgerV2Error("Workflow v1 source exceeds its existing event limit.")
    if any(
        isinstance(event, dict)
        and event.get("event_type") == "closeout_submitted"
        and (closeout_path is None or event.get("evidence_path") != closeout_path)
        for event in events
    ):
        raise LedgerV2Error("Workflow v1 source references a missing or mismatched closeout.")
    try:
        manifest["derived_state"] = lifecycle._reduce_events(events)
    except lifecycle.WorkflowRecordError as exc:
        raise LedgerV2Error(f"Workflow v1 source failed complete validation: {exc}") from exc
    return manifest


def v1_source_identity(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    parent_fd = _safe_dir(source.parent)
    try:
        raw = _read_named_file(parent_fd, source.name, 5_000_000)
    finally:
        os.close(parent_fd)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise LedgerV2Error("Workflow v1 source is not valid UTF-8.") from exc
    if dispatch_marker(text) != 1:
        raise LedgerV2Error("Workflow v1 source has an unknown or incompatible version.")
    return {"byte_length": len(raw), "sha256": sha256_bytes(raw)}
