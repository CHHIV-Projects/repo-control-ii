from __future__ import annotations

import base64
import difflib
import hashlib
import hmac
import json
import os
import re
import stat
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Mapping, Protocol, Sequence

from .information_store import InformationStore
from .web import architect_contract
from .web.repository_registry import RepositoryEntry
from .workflow.milestones import discover_milestones


MAX_LIBRARY_PAGE_SIZE = 100
MAX_DOCUMENT_PAGE_BYTES = 64 * 1024
MAX_COMPARE_BYTES = 1_000_000
MAX_EXPORT_DOCUMENTS = 25
MAX_EXPORT_BYTES = 5_000_000
MAX_FILTER_LENGTH = 256
MAX_CURSOR_LENGTH = 4096

_ROLE_TYPES = {
    "context": "PROJECT_CONTEXT",
    "architecture": "ARCHITECTURE",
    "workflow": "WORKFLOW",
    "coding_agent_rules": "CODING_RULES",
    "parking_lot": "PARKING_LOT",
}
_HISTORY_TYPES = {
    "milestone_prompt": "PROMPT",
    "milestone_closeout": "CLOSEOUT",
    "milestone_workflow": "WORKFLOW",
    "archived_context": "PROJECT_CONTEXT",
    "archived_planning": "PLANNING",
    "historical_document": "HISTORICAL",
}
_FILTER_KEYS = {
    "repository_id",
    "repository_key",
    "milestone_id",
    "milestone_text",
    "document_class",
    "authority",
    "source",
    "status",
}
_TOKEN_RE = re.compile(r"\S+")
_LEGACY_ARCHIVE_MILESTONE_ID = re.compile(r"^([0-9]+(?:\.[0-9]+)+)_")


class DocumentLibraryError(RuntimeError):
    pass


class SourceEnumerationError(DocumentLibraryError):
    pass


class DocumentNotFoundError(DocumentLibraryError):
    pass


class RevisionNotFoundError(DocumentLibraryError):
    pass


class DuplicateSourceIdentityError(DocumentLibraryError):
    pass


class InvalidCursorError(DocumentLibraryError):
    pass


class StaleCursorError(DocumentLibraryError):
    pass


class DocumentTooLargeError(DocumentLibraryError):
    pass


@dataclass(frozen=True)
class GitRepositoryContext:
    repository: RepositoryEntry
    current_milestone_id: str | None = None


@dataclass(frozen=True)
class DocumentFilters:
    repository_id: str | None = None
    repository_key: str | None = None
    milestone_id: str | None = None
    milestone_text: str | None = None
    document_class: str | None = None
    authority: str | None = None
    source: str | None = None
    status: str | None = None

    @classmethod
    def parse(cls, value: DocumentFilters | Mapping[str, Any] | None) -> DocumentFilters:
        if value is None:
            return cls()
        if isinstance(value, cls):
            source: Mapping[str, Any] = value.as_dict()
        elif isinstance(value, Mapping):
            source = value
        else:
            raise ValueError("Document filters must be an object.")
        unknown = set(source) - _FILTER_KEYS
        if unknown:
            raise ValueError(f"Unsupported document filter(s): {', '.join(sorted(unknown))}.")
        parsed: dict[str, str | None] = {}
        for key in _FILTER_KEYS:
            item = source.get(key)
            if item is None or item == "":
                parsed[key] = None
            elif not isinstance(item, str) or len(item) > MAX_FILTER_LENGTH:
                raise ValueError(f"Filter {key!r} must be text of at most {MAX_FILTER_LENGTH} characters.")
            else:
                parsed[key] = item
        return cls(**parsed)

    def as_dict(self) -> dict[str, str | None]:
        return {
            "repository_id": self.repository_id,
            "repository_key": self.repository_key,
            "milestone_id": self.milestone_id,
            "milestone_text": self.milestone_text,
            "document_class": self.document_class,
            "authority": self.authority,
            "source": self.source,
            "status": self.status,
        }


@dataclass(frozen=True)
class DocumentRecord:
    provider_id: str
    source_identity: str
    repository_id: str
    repository_key: str
    repository_name: str
    document_id: str
    revision_id: str
    logical_type: str
    name: str
    path: str
    milestone_id: str | None
    content_sha256: str
    source_kind: str
    authority_classification: str
    authority_note: str
    revision_status: str
    approval_state: str
    version_kind: str
    version_provenance: dict[str, Any]
    content: str = field(repr=False, compare=False)
    title: str | None = None

    def public(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "source_identity": self.source_identity,
            "repository_id": self.repository_id,
            "repository_key": self.repository_key,
            "repository_name": self.repository_name,
            "document_id": self.document_id,
            "revision_id": self.revision_id,
            "logical_type": self.logical_type,
            "name": self.name,
            "path": self.path,
            "milestone_id": self.milestone_id,
            "content_sha256": self.content_sha256,
            "source_kind": self.source_kind,
            "authority_classification": self.authority_classification,
            "authority_note": self.authority_note,
            "revision_status": self.revision_status,
            "approval_state": self.approval_state,
            "version_kind": self.version_kind,
            "version_provenance": dict(self.version_provenance),
            "title": self.title,
        }


@dataclass(frozen=True)
class ProviderSnapshot:
    provider_id: str
    records: tuple[DocumentRecord, ...]
    fingerprint: str
    bytes_scanned: int
    warnings: tuple[str, ...] = ()


class DocumentProvider(Protocol):
    provider_id: str

    def enumerate_records(self) -> ProviderSnapshot: ...


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _stat_snapshot(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _stable_id(prefix: str, *parts: str) -> str:
    return f"{prefix}:{_sha256(_canonical_json(parts))}"


def _records_fingerprint(records: Sequence[DocumentRecord]) -> str:
    stable = [item.public() for item in records]
    return _sha256(_canonical_json(stable))


def _read_repository_file(
    repository: RepositoryEntry,
    path: str,
    *,
    historical: bool,
    expected_snapshot: tuple[int, int, int, int, int] | None = None,
) -> dict[str, Any]:
    try:
        result = architect_contract._read_repository_document(
            repository,
            path,
            allow_historical=historical,
            max_bytes=architect_contract.MAX_REPOSITORY_DOCUMENT_FILE_BYTES,
            expected_snapshot=expected_snapshot,
        )
    except architect_contract.ArchitectContractError as exc:
        raise SourceEnumerationError(f"Cannot completely read {path!r}: {exc.code}: {exc}") from exc
    if result.get("truncated") or result.get("bytes_returned") != result.get("file_size_bytes"):
        raise SourceEnumerationError(f"Source document {path!r} was not read completely.")
    content = result.get("content")
    if not isinstance(content, str):
        raise SourceEnumerationError(f"Source document {path!r} did not decode as text.")
    return result


def _git_head(repository_root: Path) -> str | None:
    git_metadata = repository_root / ".git"
    if not git_metadata.exists():
        return None
    try:
        result = subprocess.run(
            ["git", "-C", str(repository_root), "rev-parse", "--verify", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    head = result.stdout.strip()
    return head if re.fullmatch(r"[0-9a-f]{40,64}", head) else None


class GitFileProvider:
    """Read-only provider over caller-supplied repository and allowlisted roots."""

    provider_id = "git-files"

    def __init__(self, sources: Sequence[GitRepositoryContext]) -> None:
        if not sources:
            raise ValueError("At least one caller-supplied Git repository is required.")
        repositories: dict[str, GitRepositoryContext] = {}
        keys: set[str] = set()
        for source in sources:
            repository = source.repository
            if repository.repository_id in repositories or repository.key in keys:
                raise ValueError("Git repository IDs and keys must be unique within the provider.")
            root = Path(repository.path)
            if root.is_symlink():
                raise SourceEnumerationError("A repository source root may not be a symlink.")
            try:
                resolved = root.resolve(strict=True)
            except OSError as exc:
                raise SourceEnumerationError("A repository source root cannot be resolved.") from exc
            if not resolved.is_dir():
                raise SourceEnumerationError("A repository source root must be a directory.")
            if source.current_milestone_id is not None and not source.current_milestone_id.strip():
                raise ValueError("Current milestone identity must be non-empty text or None.")
            repositories[repository.repository_id] = source
            keys.add(repository.key)
        self.sources = tuple(sorted(repositories.values(), key=lambda item: (item.repository.key, item.repository.repository_id)))

    def enumerate_records(self) -> ProviderSnapshot:
        records: list[DocumentRecord] = []
        warnings: list[str] = []
        bytes_scanned = 0
        for source in self.sources:
            source_records, source_bytes, source_warnings = self._enumerate_repository(source)
            records.extend(source_records)
            bytes_scanned += source_bytes
            warnings.extend(source_warnings)
        records.sort(key=_record_sort_key)
        return ProviderSnapshot(
            provider_id=self.provider_id,
            records=tuple(records),
            fingerprint=_records_fingerprint(records),
            bytes_scanned=bytes_scanned,
            warnings=tuple(warnings),
        )

    def _enumerate_repository(
        self,
        source: GitRepositoryContext,
    ) -> tuple[list[DocumentRecord], int, list[str]]:
        repository = source.repository
        root_path = Path(repository.path)
        if root_path.is_symlink():
            raise SourceEnumerationError("A repository source root may not become a symlink.")
        try:
            root = root_path.resolve(strict=True)
        except OSError as exc:
            raise SourceEnumerationError("A repository source root cannot be resolved.") from exc
        try:
            roots = architect_contract._historical_roots(repository)
        except architect_contract.ArchitectContractError as exc:
            raise SourceEnumerationError(f"Historical roots cannot be enumerated: {exc.code}: {exc}") from exc
        milestone_root_exists = any(relative == repository.milestone_root for relative, _ in roots)
        milestone_snapshots: dict[str, tuple[int, int, int, int, int]] = {}
        if milestone_root_exists:
            for path, metadata in self._walk_tree(repository, repository.milestone_root):
                if Path(path).suffix.lower() in architect_contract._DOCUMENT_EXTENSIONS:
                    milestone_snapshots[path] = _stat_snapshot(metadata)
        inventory = discover_milestones(root, repository.milestone_root)
        issues = inventory["issues"]
        fatal_issues = [
            issue for issue in issues
            if any(marker in issue.casefold() for marker in ("symlink", "ambiguous", "configured milestone root"))
        ]
        if fatal_issues:
            raise SourceEnumerationError(
                f"Milestone inventory for repository {repository.key!r} is incomplete: "
                + "; ".join(fatal_issues[:5])
            )
        candidates = inventory["candidates"]
        current_id = source.current_milestone_id
        if current_id is not None and not any(item["milestone_id"] == current_id for item in candidates):
            raise SourceEnumerationError(
                f"Supplied current milestone {current_id!r} is not a valid Git prompt candidate."
            )
        head = _git_head(root)
        records: list[DocumentRecord] = []
        bytes_scanned = 0
        enumerated_milestone_paths: set[str] = set()
        for role, relative_path in sorted(repository.documents.items()):
            document = _read_repository_file(repository, relative_path, historical=False)
            content = document["content"]
            bytes_scanned += document["bytes_returned"]
            logical_type = _ROLE_TYPES.get(role, "OTHER")
            records.append(
                self._git_record(
                    repository,
                    relative_path,
                    content,
                    logical_type=logical_type,
                    milestone_id=None,
                    title=Path(relative_path).stem,
                    authority="current_governing_document",
                    authority_note="Registry-mapped Git document; the current working tree remains its authority.",
                    status="current_worktree",
                    approval_state="source_controlled",
                    head=head,
                )
            )

        for candidate in candidates:
            is_current = candidate["milestone_id"] == current_id
            for path, logical_type, title in (
                (candidate["prompt_path"], "PROMPT", candidate["title_slug"]),
                (candidate["closeout_path"], "CLOSEOUT", candidate["title_slug"]),
            ):
                if path is None:
                    continue
                document = _read_repository_file(
                    repository,
                    path,
                    historical=True,
                    expected_snapshot=milestone_snapshots.get(path),
                )
                enumerated_milestone_paths.add(path)
                content = document["content"]
                bytes_scanned += document["bytes_returned"]
                records.append(
                    self._git_record(
                        repository,
                        path,
                        content,
                        logical_type=logical_type,
                        milestone_id=candidate["milestone_id"],
                        title=title,
                        authority="current_milestone_document" if is_current else "historical_evidence",
                        authority_note=(
                            "Selected milestone file from caller-supplied fixture context; approval and lifecycle "
                            "are not inferred."
                            if is_current
                            else "Prior milestone file; no approval or lifecycle history is inferred."
                        ),
                        status="current_worktree" if is_current else "historical",
                        approval_state="unknown",
                        head=head,
                    )
                )
            prompt_name = PurePosixPath(candidate["prompt_path"]).name
            workflow_path = PurePosixPath(candidate["prompt_path"]).with_name(
                prompt_name.removesuffix("_prompt.md") + "_workflow.md"
            ).as_posix()
            workflow_file = root.joinpath(*PurePosixPath(workflow_path).parts)
            if workflow_file.is_symlink():
                raise SourceEnumerationError(f"Workflow sidecar {workflow_path!r} is a symlink.")
            if workflow_file.exists():
                document = _read_repository_file(
                    repository,
                    workflow_path,
                    historical=True,
                    expected_snapshot=milestone_snapshots.get(workflow_path),
                )
                enumerated_milestone_paths.add(workflow_path)
                content = document["content"]
                bytes_scanned += document["bytes_returned"]
                records.append(
                    self._git_record(
                        repository,
                        workflow_path,
                        content,
                        logical_type="WORKFLOW",
                        milestone_id=candidate["milestone_id"],
                        title=workflow_file.stem,
                        authority="current_workflow_history" if is_current else "historical_evidence",
                        authority_note=(
                            "Current Git workflow sidecar; its event history remains the workflow authority."
                            if is_current
                            else "Archived workflow history; no current lifecycle authority is inferred."
                        ),
                        status="current_worktree" if is_current else "historical",
                        approval_state="not_applicable",
                        head=head,
                    )
                )

        for path in sorted(milestone_snapshots, key=os.fsencode):
            if path in enumerated_milestone_paths:
                continue
            try:
                info = architect_contract._historical_path_info(repository, path, current_id)
            except architect_contract.ArchitectContractError as exc:
                if exc.code in {"historical_path_not_allowed", "unsupported_historical_document_type"}:
                    continue
                raise SourceEnumerationError(
                    f"Milestone path {path!r} is invalid: {exc.code}: {exc}"
                ) from exc
            logical_type = _HISTORY_TYPES.get(info["artifact_type"])
            if logical_type is None:
                raise SourceEnumerationError(f"Milestone path {path!r} has an unsupported artifact type.")
            document = _read_repository_file(
                repository,
                path,
                historical=True,
                expected_snapshot=milestone_snapshots[path],
            )
            content = document["content"]
            bytes_scanned += document["bytes_returned"]
            is_current = info["is_current"]
            if is_current:
                authority = (
                    "current_workflow_history"
                    if logical_type == "WORKFLOW"
                    else "current_milestone_document"
                )
                authority_note = "Caller-designated current milestone file; approval and lifecycle are not inferred."
            else:
                authority = "historical_evidence"
                authority_note = "Unpaired historical milestone artifact; no approval or lifecycle is inferred."
            records.append(
                self._git_record(
                    repository,
                    path,
                    content,
                    logical_type=logical_type,
                    milestone_id=info["milestone_id"],
                    title=info["title"],
                    authority=authority,
                    authority_note=authority_note,
                    status="current_worktree" if is_current else "historical",
                    approval_state="not_applicable" if logical_type == "WORKFLOW" else "unknown",
                    head=head,
                )
            )

        try:
            roots = architect_contract._historical_roots(repository)
        except architect_contract.ArchitectContractError as exc:
            raise SourceEnumerationError(f"Historical roots cannot be enumerated: {exc.code}: {exc}") from exc
        for relative_root, _absolute_root in roots:
            if relative_root != "docs/history":
                continue
            archive_records, archive_bytes = self._scan_archive(repository, relative_root, head=head)
            records.extend(archive_records)
            bytes_scanned += archive_bytes
        nonfatal = [issue for issue in issues if issue not in fatal_issues]
        return records, bytes_scanned, nonfatal

    def _scan_archive(
        self,
        repository: RepositoryEntry,
        relative_root: str,
        *,
        head: str | None,
    ) -> tuple[list[DocumentRecord], int]:
        records: list[DocumentRecord] = []
        bytes_scanned = 0
        for child, metadata in self._walk_tree(repository, relative_root):
            if Path(child).suffix.lower() not in architect_contract._DOCUMENT_EXTENSIONS:
                continue
            try:
                info = architect_contract._historical_path_info(repository, child, None)
            except architect_contract.ArchitectContractError as exc:
                if exc.code in {"historical_path_not_allowed", "unsupported_historical_document_type"}:
                    continue
                raise SourceEnumerationError(f"Historical path {child!r} is invalid: {exc.code}: {exc}") from exc
            snapshot = _stat_snapshot(metadata)
            document = _read_repository_file(
                repository,
                child,
                historical=True,
                expected_snapshot=snapshot,
            )
            content = document["content"]
            bytes_scanned += document["bytes_returned"]
            milestone_id = info["milestone_id"]
            if milestone_id is None:
                legacy_id_match = _LEGACY_ARCHIVE_MILESTONE_ID.match(PurePosixPath(child).name)
                if legacy_id_match is not None:
                    milestone_id = legacy_id_match.group(1)
            records.append(
                self._git_record(
                    repository,
                    child,
                    content,
                    logical_type=_HISTORY_TYPES[info["artifact_type"]],
                    milestone_id=milestone_id,
                    title=info["title"],
                    authority="historical_evidence",
                    authority_note="Archived Git document; historical evidence is not current approval or authority.",
                    status="historical",
                    approval_state="unknown",
                    head=head,
                    source_snapshot=snapshot,
                )
            )
        return records, bytes_scanned

    @staticmethod
    def _walk_tree(
        repository: RepositoryEntry,
        relative_root: str,
    ) -> Iterator[tuple[str, os.stat_result]]:
        pending = [relative_root]
        while pending:
            relative_directory = pending.pop()
            try:
                directory_fd = architect_contract._open_repository_directory(repository, relative_directory)
            except architect_contract.ArchitectContractError as exc:
                raise SourceEnumerationError(
                    f"Document directory {relative_directory!r} cannot be opened: {exc.code}: {exc}"
                ) from exc
            children: list[str] = []
            files: list[tuple[str, os.stat_result]] = []
            try:
                with os.scandir(directory_fd) as iterator:
                    for entry in iterator:
                        if entry.is_symlink():
                            path = PurePosixPath(relative_directory, entry.name).as_posix()
                            raise SourceEnumerationError(f"Document source contains a symlink at {path!r}.")
                        if entry.is_dir(follow_symlinks=False):
                            children.append(entry.name)
                        else:
                            metadata = entry.stat(follow_symlinks=False)
                            if stat.S_ISREG(metadata.st_mode):
                                files.append(
                                    (PurePosixPath(relative_directory, entry.name).as_posix(), metadata)
                                )
            except OSError as exc:
                raise SourceEnumerationError(
                    f"Document directory {relative_directory!r} cannot be enumerated."
                ) from exc
            finally:
                os.close(directory_fd)
            for path, metadata in sorted(files, key=lambda item: os.fsencode(item[0])):
                yield path, metadata
            pending.extend(
                reversed(
                    [
                        PurePosixPath(relative_directory, child).as_posix()
                        for child in sorted(children, key=os.fsencode)
                    ]
                )
            )

    def _git_record(
        self,
        repository: RepositoryEntry,
        path: str,
        content: str,
        *,
        logical_type: str,
        milestone_id: str | None,
        title: str | None,
        authority: str,
        authority_note: str,
        status: str,
        approval_state: str,
        head: str | None,
        source_snapshot: tuple[int, int, int, int, int] | None = None,
    ) -> DocumentRecord:
        content_hash = _sha256(content.encode("utf-8"))
        document_id = _stable_id("git-document", repository.repository_id, path)
        revision_id = f"git-worktree:{content_hash}"
        provenance = {
            "head_commit": head,
            "working_tree": True,
            "source_snapshot": list(source_snapshot) if source_snapshot is not None else None,
        }
        source_identity = f"git:{repository.repository_id}:{path}:{content_hash}"
        return DocumentRecord(
            provider_id=self.provider_id,
            source_identity=source_identity,
            repository_id=repository.repository_id,
            repository_key=repository.key,
            repository_name=repository.name,
            document_id=document_id,
            revision_id=revision_id,
            logical_type=logical_type,
            name=PurePosixPath(path).name,
            path=path,
            milestone_id=milestone_id,
            content_sha256=content_hash,
            source_kind="git_worktree",
            authority_classification=authority,
            authority_note=authority_note,
            revision_status=status,
            approval_state=approval_state,
            version_kind="current_worktree",
            version_provenance=provenance,
            content=content,
            title=title,
        )


class SQLiteRevisionProvider:
    """Read-only adapter over an explicitly bound InformationStore."""

    def __init__(self, store: InformationStore, *, repository_name: str | None = None) -> None:
        self.store = store
        self.repository_name = repository_name or store.identity.repository_key
        self.provider_id = (
            f"sqlite:{store.identity.repository_id}:{store.identity.milestone_id}"
        )

    def enumerate_records(self) -> ProviderSnapshot:
        records: list[DocumentRecord] = []
        bytes_scanned = 0
        authority = self.store.snapshot()["authority"]
        if authority not in {"disposable_fixture_only", "sqlite_managed"}:
            raise SourceEnumerationError("A prepared store is not an active document authority.")
        fixture = authority == "disposable_fixture_only"
        for revision in self.store.list_repository_revisions():
            content = revision["content"]
            content_hash = revision["content_sha256"]
            bytes_scanned += len(content.encode("utf-8"))
            public_document_id = _stable_id(
                "sqlite-document",
                self.store.identity.repository_id,
                revision["milestone_id"],
                revision["document_id"],
            )
            status = revision["status"]
            records.append(
                DocumentRecord(
                    provider_id=self.provider_id,
                    source_identity=f"{self.provider_id}:{revision['document_id']}:{revision['revision_id']}",
                    repository_id=self.store.identity.repository_id,
                    repository_key=self.store.identity.repository_key,
                    repository_name=self.repository_name,
                    document_id=public_document_id,
                    revision_id=f"sqlite-revision:{revision['revision_id']}",
                    logical_type=revision["logical_type"],
                    name=PurePosixPath(revision["logical_key"]).name,
                    path=revision["source_path"],
                    milestone_id=revision["milestone_id"],
                    content_sha256=content_hash,
                    source_kind="sqlite_fixture" if fixture else "sqlite_managed",
                    authority_classification="disposable_fixture" if fixture else "managed_revision",
                    authority_note=(
                        "Disposable SQLite fixture data; stored approval or actor labels do not confer "
                        "managed authority."
                        if fixture else
                        "Revision from the configured SQLite authority; reported actor identity is not authenticated."
                    ),
                    revision_status=status,
                    approval_state="fixture_approved" if fixture and status == "approved" else status,
                    version_kind="immutable_fixture_revision" if fixture else "immutable_revision",
                    version_provenance={
                        "revision_source_kind": revision["source_kind"],
                        "created_at": revision["created_at"],
                        "fixture_provenance" if fixture else "provenance": revision["provenance"],
                    },
                    content=content,
                    title=revision["logical_key"],
                )
            )
        records.sort(key=_record_sort_key)
        return ProviderSnapshot(
            provider_id=self.provider_id,
            records=tuple(records),
            fingerprint=_records_fingerprint(records),
            bytes_scanned=bytes_scanned,
        )


def _record_sort_key(record: DocumentRecord) -> tuple[str, str, str, str, str, str]:
    return (
        record.repository_key.casefold(),
        record.repository_id,
        (record.milestone_id or "").casefold(),
        record.path.casefold(),
        record.document_id,
        record.revision_id,
    )


def _query_tokens(query: str) -> list[str]:
    if not isinstance(query, str) or len(query) > MAX_FILTER_LENGTH * 2:
        raise ValueError(f"Search query must be text of at most {MAX_FILTER_LENGTH * 2} characters.")
    return [token.casefold() for token in _TOKEN_RE.findall(query.strip())]


def _matches_filters(record: DocumentRecord, filters: DocumentFilters) -> bool:
    exact_filters = (
        (filters.repository_id, record.repository_id),
        (filters.repository_key, record.repository_key),
        (filters.milestone_id, record.milestone_id),
        (filters.document_class, record.logical_type),
        (filters.authority, record.authority_classification),
        (filters.source, record.source_kind),
        (filters.status, record.revision_status),
    )
    if any(expected is not None and (actual or "").casefold() != expected.casefold() for expected, actual in exact_filters):
        return False
    if filters.milestone_text is not None:
        searchable_milestone = f"{record.milestone_id or ''} {record.title or ''} {record.path}".casefold()
        if filters.milestone_text.casefold() not in searchable_milestone:
            return False
    return True


class DocumentLibrary:
    """Complete read-only search and exact-revision facade over injected providers."""

    def __init__(self, providers: Sequence[DocumentProvider], *, cursor_secret: bytes | None = None) -> None:
        if not providers:
            raise ValueError("At least one read-only document provider is required.")
        provider_ids = [provider.provider_id for provider in providers]
        if len(set(provider_ids)) != len(provider_ids):
            raise ValueError("Document provider IDs must be unique.")
        if cursor_secret is not None and (not isinstance(cursor_secret, bytes) or len(cursor_secret) < 16):
            raise ValueError("Cursor secret must contain at least 16 bytes.")
        self.providers = tuple(providers)
        self._cursor_secret = cursor_secret or os.urandom(32)

    def _catalog(self) -> tuple[list[DocumentRecord], str, int, int, list[str]]:
        records: list[DocumentRecord] = []
        provider_fingerprints: list[tuple[str, str]] = []
        source_identities: set[str] = set()
        document_bindings: dict[str, tuple[str, str, str]] = {}
        revision_bindings: dict[tuple[str, str], str] = {}
        bytes_scanned = 0
        warnings: list[str] = []
        for provider in self.providers:
            snapshot = provider.enumerate_records()
            if snapshot.provider_id != provider.provider_id:
                raise SourceEnumerationError("A document provider returned a mismatched provider identity.")
            provider_fingerprints.append((snapshot.provider_id, snapshot.fingerprint))
            bytes_scanned += snapshot.bytes_scanned
            warnings.extend(snapshot.warnings)
            for record in snapshot.records:
                if record.provider_id != snapshot.provider_id:
                    raise SourceEnumerationError("A document record does not match its provider identity.")
                if _sha256(record.content.encode("utf-8")) != record.content_sha256:
                    raise SourceEnumerationError("A provider record's content does not match its SHA-256.")
                if record.source_identity in source_identities:
                    raise DuplicateSourceIdentityError(
                        f"Duplicate document source identity {record.source_identity!r}."
                    )
                source_identities.add(record.source_identity)
                document_binding = (record.provider_id, record.repository_id, record.path)
                prior_document_binding = document_bindings.setdefault(record.document_id, document_binding)
                if prior_document_binding != document_binding:
                    raise DuplicateSourceIdentityError(
                        f"Document ID {record.document_id!r} identifies conflicting provider sources."
                    )
                revision_key = (record.document_id, record.revision_id)
                prior_revision_hash = revision_bindings.setdefault(revision_key, record.content_sha256)
                if prior_revision_hash != record.content_sha256:
                    raise DuplicateSourceIdentityError(
                        f"Exact revision {record.document_id!r}/{record.revision_id!r} has conflicting content."
                    )
                records.append(record)
        records.sort(key=_record_sort_key)
        fingerprint = _sha256(_canonical_json(sorted(provider_fingerprints)))
        return records, fingerprint, bytes_scanned, len(records), warnings

    def _encode_cursor(self, payload: dict[str, Any]) -> str:
        unsigned = dict(payload)
        unsigned.pop("signature", None)
        signature = hmac.new(self._cursor_secret, _canonical_json(unsigned), hashlib.sha256).hexdigest()
        encoded = _canonical_json({**unsigned, "signature": signature})
        return base64.urlsafe_b64encode(encoded).decode("ascii").rstrip("=")

    def _decode_cursor(self, cursor: str) -> dict[str, Any]:
        if not isinstance(cursor, str) or not 1 <= len(cursor) <= MAX_CURSOR_LENGTH:
            raise InvalidCursorError("Continuation cursor is missing or exceeds its size bound.")
        try:
            encoded = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
            payload = json.loads(encoded)
        except (ValueError, json.JSONDecodeError) as exc:
            raise InvalidCursorError("Continuation cursor is malformed.") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("signature"), str):
            raise InvalidCursorError("Continuation cursor has an unsupported shape.")
        unsigned = {key: value for key, value in payload.items() if key != "signature"}
        expected = hmac.new(self._cursor_secret, _canonical_json(unsigned), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(payload["signature"], expected):
            raise InvalidCursorError("Continuation cursor signature is invalid or belongs to another library.")
        return unsigned

    def _historical_catalog(
        self,
        repository: RepositoryEntry,
        selected_milestone_id: str,
    ) -> tuple[list[tuple[DocumentRecord, dict[str, Any]]], str, int, list[str]]:
        has_git_history = any(
            isinstance(provider, GitFileProvider)
            and any(
                source.repository.repository_id == repository.repository_id
                and source.repository.path == repository.path
                for source in provider.sources
            )
            for provider in self.providers
        )
        if not has_git_history:
            raise SourceEnumerationError(
                "Historical search requires an explicitly configured Git historical-evidence provider."
            )
        records, fingerprint, bytes_scanned, _candidate_count, warnings = self._catalog()
        historical: list[tuple[DocumentRecord, dict[str, Any]]] = []
        for record in records:
            if record.repository_id != repository.repository_id:
                continue
            try:
                info = architect_contract._historical_path_info(repository, record.path, selected_milestone_id)
            except architect_contract.ArchitectContractError as exc:
                if exc.code in {"historical_path_not_allowed", "unsupported_historical_document_type"}:
                    continue
                raise
            if info["is_current"]:
                continue
            raw = record.content.encode("utf-8")
            if len(raw) > architect_contract.MAX_REPOSITORY_DOCUMENT_FILE_BYTES:
                raise DocumentTooLargeError("Historical source exceeds the complete-content size limit.")
            if architect_contract._PRIVATE_KEY_MARKER.search(record.content):
                raise SourceEnumerationError("Historical source contains private-key material.")
            historical.append((record, info))
        historical.sort(key=lambda item: (item[0].path, item[0].document_id, item[0].revision_id))
        return historical, fingerprint, bytes_scanned, warnings

    def search_historical(
        self,
        repository: RepositoryEntry,
        selected_milestone_id: str,
        *,
        query: str | None,
        milestone_id: str | None,
        artifact_type: str | None,
        category: str | None,
        limit: int | None,
        cursor: str | None,
    ) -> dict[str, Any]:
        page_size = architect_contract._history_filters(query, milestone_id, artifact_type, category, limit)
        records, fingerprint, bytes_scanned, warnings = self._historical_catalog(repository, selected_milestone_id)
        request = {
            "repository_id": repository.repository_id,
            "selected_milestone_id": selected_milestone_id,
            "query": query,
            "milestone_id": milestone_id,
            "artifact_type": artifact_type,
            "category": category,
            "limit": page_size,
        }
        request_hash = _sha256(_canonical_json(request))
        offset = 0
        if cursor is not None:
            payload = self._decode_cursor(cursor)
            if payload.get("type") != "history" or payload.get("request") != request_hash:
                raise InvalidCursorError("Historical cursor does not match the query and filters.")
            if payload.get("catalog") != fingerprint:
                raise StaleCursorError("Historical sources changed; restart the search.")
            offset = payload.get("offset")
            if type(offset) is not int or offset < 0:
                raise InvalidCursorError("Historical cursor offset is invalid.")
        tokens = architect_contract._history_query_tokens(query) if query is not None else []
        matches = [
            (record, info) for record, info in records
            if (milestone_id is None or info["milestone_id"] == milestone_id)
            and (artifact_type is None or info["artifact_type"] == artifact_type)
            and (category is None or info["category"] == category)
            and all(token in f"{record.path} {info['title']} {info['milestone_id'] or ''} {record.content}".casefold()
                    for token in tokens)
        ]
        if offset > len(matches) or (cursor is not None and offset == len(matches)):
            raise StaleCursorError("Historical cursor is outside the current result set.")
        selected = matches[offset:offset + page_size]
        next_offset = offset + len(selected)
        has_more = next_offset < len(matches)
        next_cursor = (
            self._encode_cursor({
                "type": "history", "request": request_hash, "catalog": fingerprint, "offset": next_offset,
            }) if has_more else None
        )
        results = [
            {
                **{key: value for key, value in info.items() if key != "is_current"},
                "file_size_bytes": len(record.content.encode("utf-8")),
                "source_type": "historical_evidence",
                "authority_classification": "historical_evidence",
                "is_current_authority": False,
                "document_id": record.document_id,
                "revision_id": record.revision_id,
                "content_sha256": record.content_sha256,
                "source_kind": record.source_kind,
                "version_kind": record.version_kind,
                "version_provenance": record.version_provenance,
            }
            for record, info in selected
        ]
        return {
            "query": query,
            "filters": {"milestone_id": milestone_id, "artifact_type": artifact_type, "category": category},
            "ordering": "repository_relative_path_ascending",
            "results": results,
            "returned_count": len(results),
            "total_count": len(matches),
            "omitted_count": 0,
            "total_count_complete": not warnings,
            "limited": False,
            "truncated": has_more,
            "has_more": has_more,
            "next_cursor": next_cursor,
            "candidates_considered": len(records),
            "files_scanned": len(records) if query is not None else 0,
            "bytes_scanned": bytes_scanned,
            "skipped_count": 0,
            "warnings": warnings,
            "source_coverage": "configured_document_providers",
        }

    def read_historical(
        self,
        repository: RepositoryEntry,
        selected_milestone_id: str,
        path: str,
        *,
        document_id: str | None = None,
        revision_id: str | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        if (document_id is None) != (revision_id is None):
            raise ValueError("Historical reads require both document ID and revision ID, or neither.")
        info = architect_contract._historical_path_info(repository, path, selected_milestone_id)
        if info["is_current"]:
            raise architect_contract.ArchitectContractError(
                "current_artifact_not_historical", "Read the selected milestone through its current tools.", 409
            )
        records, _fingerprint, _bytes_scanned, _warnings = self._historical_catalog(
            repository, selected_milestone_id
        )
        matches = [
            record for record, _info in records
            if record.path == path and (document_id is None or (
                record.document_id == document_id and record.revision_id == revision_id
            ))
        ]
        if not matches:
            raise DocumentNotFoundError("The exact historical source was not found in configured providers.")
        if len(matches) != 1:
            raise DuplicateSourceIdentityError(
                "The historical path has multiple revisions; supply its exact document and revision IDs."
            )
        record = matches[0]
        byte_count = len(record.content.encode("utf-8"))
        page = self._read_record_page(record, cursor=cursor, page_bytes=MAX_DOCUMENT_PAGE_BYTES)
        return {
            "source_type": "historical_evidence",
            **{key: value for key, value in info.items() if key != "is_current"},
            "repository_key": repository.key,
            "repository_id": repository.repository_id,
            "document_id": record.document_id,
            "revision_id": record.revision_id,
            "content_sha256": record.content_sha256,
            "file_size_bytes": byte_count,
            "bytes_returned": len(page["content"].encode("utf-8")),
            "character_count": len(page["content"]),
            "truncated": not page["complete"],
            "byte_offset": page["byte_offset"],
            "next_byte_offset": page["next_byte_offset"],
            "next_cursor": page["next_cursor"],
            "working_tree_source": record.source_kind,
            "authority_classification": "historical_evidence",
            "authority_note": "Historical evidence only; not current governing or lifecycle authority.",
            "is_current_authority": False,
            "observed_at": datetime.now(UTC).isoformat(),
            "version_provenance": record.version_provenance,
            "content": page["content"],
        }

    def search(
        self,
        *,
        query: str = "",
        filters: DocumentFilters | Mapping[str, Any] | None = None,
        page_size: int = 20,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        tokens = _query_tokens(query)
        parsed_filters = DocumentFilters.parse(filters)
        if type(page_size) is not int or not 1 <= page_size <= MAX_LIBRARY_PAGE_SIZE:
            raise ValueError(f"Page size must be between 1 and {MAX_LIBRARY_PAGE_SIZE}.")
        records, catalog_fingerprint, bytes_scanned, candidate_count, warnings = self._catalog()
        request = {
            "query": tokens,
            "filters": parsed_filters.as_dict(),
            "page_size": page_size,
        }
        request_fingerprint = _sha256(_canonical_json(request))
        offset = 0
        if cursor is not None:
            payload = self._decode_cursor(cursor)
            if payload.get("type") != "search" or payload.get("request") != request_fingerprint:
                raise InvalidCursorError("Continuation cursor does not match the query, filters, or page size.")
            if payload.get("catalog") != catalog_fingerprint:
                raise StaleCursorError("Document sources changed after the preceding page; restart the search.")
            offset_value = payload.get("offset")
            if type(offset_value) is not int or offset_value < 0:
                raise InvalidCursorError("Continuation cursor offset is invalid.")
            offset = offset_value
        filtered = [record for record in records if _matches_filters(record, parsed_filters)]
        if tokens:
            matches = [
                record
                for record in filtered
                if all(
                    token in (
                        f"{record.path} {record.name} {record.title or ''} "
                        f"{record.milestone_id or ''} {record.content}"
                    ).casefold()
                    for token in tokens
                )
            ]
        else:
            matches = filtered
        if offset > len(matches) or (cursor is not None and offset == len(matches)):
            raise StaleCursorError("Continuation cursor is outside the current result set.")
        selected = matches[offset : offset + page_size]
        next_offset = offset + len(selected)
        has_more = next_offset < len(matches)
        next_cursor = (
            self._encode_cursor(
                {
                    "type": "search",
                    "request": request_fingerprint,
                    "catalog": catalog_fingerprint,
                    "offset": next_offset,
                }
            )
            if has_more
            else None
        )
        searched_bytes = (
            sum(len(record.content.encode("utf-8")) for record in filtered)
            if tokens
            else 0
        )
        return {
            "query": query,
            "filters": parsed_filters.as_dict(),
            "documents": [record.public() for record in selected],
            "returned_count": len(selected),
            "total_count": len(matches),
            "matched_count": len(matches),
            "candidate_count": candidate_count,
            "searched_count": len(filtered),
            "bytes_scanned": searched_bytes,
            "provider_bytes_read": bytes_scanned,
            "omitted_count": 0,
            "deferred_count": len(matches) - next_offset,
            "limited": has_more,
            "has_more": has_more,
            "next_cursor": next_cursor,
            "total_count_complete": True,
            "catalog_fingerprint": catalog_fingerprint,
            "warnings": warnings,
        }

    @staticmethod
    def _resolve_from_records(
        records: Sequence[DocumentRecord],
        document_id: str,
        revision_id: str,
    ) -> DocumentRecord:
        if not isinstance(document_id, str) or not document_id or not isinstance(revision_id, str) or not revision_id:
            raise ValueError("Exact document and revision IDs are required.")
        document_records = [record for record in records if record.document_id == document_id]
        if not document_records:
            raise DocumentNotFoundError("Document ID was not found in the configured providers.")
        for record in document_records:
            if record.revision_id == revision_id:
                return record
        raise RevisionNotFoundError("Exact revision ID was not found for this document.")

    def _resolve(self, document_id: str, revision_id: str) -> DocumentRecord:
        records, _fingerprint, _bytes, _count, _warnings = self._catalog()
        return self._resolve_from_records(records, document_id, revision_id)

    def get(self, document_id: str, revision_id: str) -> dict[str, Any]:
        record = self._resolve(document_id, revision_id)
        return {**record.public(), "content": record.content}

    def read_page(
        self,
        document_id: str,
        revision_id: str,
        *,
        cursor: str | None = None,
        page_bytes: int = MAX_DOCUMENT_PAGE_BYTES,
    ) -> dict[str, Any]:
        record = self._resolve(document_id, revision_id)
        return self._read_record_page(record, cursor=cursor, page_bytes=page_bytes)

    def _read_record_page(
        self,
        record: DocumentRecord,
        *,
        cursor: str | None,
        page_bytes: int,
    ) -> dict[str, Any]:
        if type(page_bytes) is not int or not 1 <= page_bytes <= MAX_DOCUMENT_PAGE_BYTES:
            raise ValueError(f"Document page size must be between 1 and {MAX_DOCUMENT_PAGE_BYTES} bytes.")
        document_id, revision_id = record.document_id, record.revision_id
        content_bytes = record.content.encode("utf-8")
        request_fingerprint = _sha256(
            _canonical_json([document_id, revision_id, record.content_sha256, page_bytes])
        )
        offset = 0
        if cursor is not None:
            payload = self._decode_cursor(cursor)
            if payload.get("type") != "read" or payload.get("request") != request_fingerprint:
                raise InvalidCursorError("Document cursor does not match the requested exact revision and page size.")
            offset_value = payload.get("offset")
            if type(offset_value) is not int or offset_value < 0:
                raise InvalidCursorError("Document cursor offset is invalid.")
            offset = offset_value
        if offset > len(content_bytes):
            raise StaleCursorError("Document cursor offset exceeds the exact revision.")
        try:
            content_bytes[:offset].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise InvalidCursorError("Document cursor splits a UTF-8 code point.") from exc
        end = min(offset + page_bytes, len(content_bytes))
        while end > offset:
            try:
                page = content_bytes[offset:end].decode("utf-8")
                break
            except UnicodeDecodeError:
                end -= 1
        else:
            if offset < len(content_bytes):
                raise ValueError("Page size is too small to contain the next UTF-8 code point.")
            page = ""
        complete = end == len(content_bytes)
        next_cursor = (
            self._encode_cursor(
                {
                    "type": "read",
                    "request": request_fingerprint,
                    "offset": end,
                }
            )
            if not complete
            else None
        )
        return {
            **record.public(),
            "byte_offset": offset,
            "next_byte_offset": end,
            "content": page,
            "complete": complete,
            "next_cursor": next_cursor,
        }

    def compare(
        self,
        left_document_id: str,
        left_revision_id: str,
        right_document_id: str,
        right_revision_id: str,
    ) -> dict[str, Any]:
        records, _fingerprint, _bytes, _count, _warnings = self._catalog()
        left_record = self._resolve_from_records(records, left_document_id, left_revision_id)
        right_record = self._resolve_from_records(records, right_document_id, right_revision_id)
        left = {**left_record.public(), "content": left_record.content}
        right = {**right_record.public(), "content": right_record.content}
        diff = "".join(
            difflib.unified_diff(
                left["content"].splitlines(keepends=True),
                right["content"].splitlines(keepends=True),
                fromfile=f"{left['path']}@{left['revision_id']}",
                tofile=f"{right['path']}@{right['revision_id']}",
            )
        )
        diff_bytes = len(diff.encode("utf-8"))
        if diff_bytes > MAX_COMPARE_BYTES:
            raise DocumentTooLargeError(f"Revision comparison exceeds {MAX_COMPARE_BYTES} bytes.")
        return {
            "left": {key: value for key, value in left.items() if key != "content"},
            "right": {key: value for key, value in right.items() if key != "content"},
            "diff": diff,
            "diff_bytes": diff_bytes,
            "complete": True,
        }

    def export(self, items: Sequence[tuple[str, str]]) -> dict[str, Any]:
        if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
            raise ValueError("Export selection must be a sequence of exact document/revision pairs.")
        if not 1 <= len(items) <= MAX_EXPORT_DOCUMENTS:
            raise ValueError(f"Export must select between 1 and {MAX_EXPORT_DOCUMENTS} revisions.")
        if any(
            not isinstance(item, (tuple, list))
            or len(item) != 2
            or not all(isinstance(value, str) and value for value in item)
            for item in items
        ):
            raise ValueError("Each export selection must contain a document ID and revision ID.")
        pairs = [tuple(item) for item in items]
        if len(set(pairs)) != len(pairs):
            raise ValueError("Export selection contains duplicate exact revisions.")
        records, _fingerprint, _bytes, _count, _warnings = self._catalog()
        selected = [
            self._resolve_from_records(records, document_id, revision_id)
            for document_id, revision_id in pairs
        ]
        documents = [{**record.public(), "content": record.content} for record in selected]
        byte_count = sum(len(item["content"].encode("utf-8")) for item in documents)
        if byte_count > MAX_EXPORT_BYTES:
            raise DocumentTooLargeError(f"Export exceeds the {MAX_EXPORT_BYTES}-byte fixture limit.")
        manifest = [
            {
                **{key: item[key] for key in (
                    "repository_id",
                    "repository_key",
                    "repository_name",
                    "source_identity",
                    "document_id",
                    "revision_id",
                    "logical_type",
                    "name",
                    "path",
                    "milestone_id",
                    "content_sha256",
                    "source_kind",
                    "authority_classification",
                    "authority_note",
                    "revision_status",
                    "approval_state",
                    "version_kind",
                    "version_provenance",
                )},
                "bytes": len(item["content"].encode("utf-8")),
            }
            for item in documents
        ]
        markdown = (
            documents[0]["content"]
            if len(documents) == 1
            else "\n\n".join(f"# {item['name']}\n\n{item['content']}" for item in documents)
        )
        export_bytes = len(markdown.encode("utf-8"))
        if export_bytes > MAX_EXPORT_BYTES:
            raise DocumentTooLargeError(f"Rendered export exceeds the {MAX_EXPORT_BYTES}-byte fixture limit.")
        return {
            "markdown": markdown,
            "manifest": manifest,
            "document_count": len(documents),
            "source_bytes": byte_count,
            "export_bytes": export_bytes,
            "attachments_included": False,
            "attachment_note": (
                "Relative or local attachments are not fetched or bundled; review Markdown links before treating "
                "this export as attachment-complete."
            ),
        }


class ReadOnlyDocumentAdapter:
    """Fixture-injected, read-only search and paged retrieval surface for an agent role."""

    role = "reader"

    def __init__(self, library: DocumentLibrary) -> None:
        self.library = library

    def search(
        self,
        *,
        query: str = "",
        filters: Mapping[str, Any] | None = None,
        page_size: int = 20,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        return {
            "adapter_role": self.role,
            "data": self.library.search(
                query=query,
                filters=filters,
                page_size=page_size,
                cursor=cursor,
            ),
        }

    def read_page(
        self,
        document_id: str,
        revision_id: str,
        *,
        cursor: str | None = None,
        page_bytes: int = MAX_DOCUMENT_PAGE_BYTES,
    ) -> dict[str, Any]:
        return {
            "adapter_role": self.role,
            "data": self.library.read_page(
                document_id,
                revision_id,
                cursor=cursor,
                page_bytes=page_bytes,
            ),
        }


class ArchitectDocumentAdapter(ReadOnlyDocumentAdapter):
    role = "architect"


class CoderDocumentAdapter(ReadOnlyDocumentAdapter):
    role = "coder"


def estimate_tokens(text: str) -> int:
    """Return a documented byte-based estimate (UTF-8 bytes / 4), not a model tokenizer result."""
    if not isinstance(text, str):
        raise ValueError("Token estimation requires text.")
    return (len(text.encode("utf-8")) + 3) // 4
