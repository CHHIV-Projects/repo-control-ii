from __future__ import annotations

import re
import threading
import tomllib
from dataclasses import dataclass
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any

from ..scanner.git_ops import ScanError, validate_git_worktree
from ..scanner.util import make_repository_id


class RepositoryRegistryError(ValueError):
    pass


DOCUMENT_ROLES = {
    "context",
    "architecture",
    "workflow",
    "coding_agent_rules",
    "parking_lot",
}
DEFAULT_DOCUMENT_PATHS = {
    "context": "docs/context/project_context.md",
    "architecture": "docs/context/project_architecture.md",
    "workflow": "docs/context/project_workflow.md",
    "coding_agent_rules": "docs/context/coding_agent_rules.md",
    "parking_lot": "docs/context/canonical_parking_lot.md",
}


def _validate_document_path(root: Path, role: str, configured_path: str, repository_key: str) -> None:
    relative_path = PurePosixPath(configured_path)
    current = root
    for part in relative_path.parts:
        current = current / part
        if current.is_symlink():
            raise RepositoryRegistryError(
                f"Repository registry entry {repository_key!r} document {role!r} "
                "cannot contain symlinked path components."
            )
    if not (root / Path(*relative_path.parts)).resolve(strict=False).is_relative_to(root):
        raise RepositoryRegistryError(
            f"Repository registry entry {repository_key!r} document {role!r} resolves outside its repository."
        )


@dataclass(frozen=True)
class RepositoryEntry:
    key: str
    name: str
    path: Path
    repository_id: str
    milestone_root: str
    documents: dict[str, str]


class RepositoryRegistry:
    def __init__(self, entries: tuple[RepositoryEntry, ...], default_key: str) -> None:
        if not entries:
            raise RepositoryRegistryError("Repository registry must contain at least one repository.")
        self._entries = entries
        self._entries_by_key = {entry.key: entry for entry in entries}
        if len(self._entries_by_key) != len(entries):
            raise RepositoryRegistryError("Repository registry contains duplicate keys.")
        if default_key not in self._entries_by_key:
            raise RepositoryRegistryError("Repository registry default must reference a configured repository.")
        self._active_key = default_key
        self._generation = 0
        self._lock = threading.RLock()

    @property
    def entries(self) -> tuple[RepositoryEntry, ...]:
        return self._entries

    def active(self) -> RepositoryEntry:
        with self._lock:
            return self._entries_by_key[self._active_key]

    def snapshot(self) -> tuple[RepositoryEntry, int]:
        with self._lock:
            return self._entries_by_key[self._active_key], self._generation

    def select(self, key: str) -> tuple[RepositoryEntry, bool]:
        with self._lock:
            entry = self._entries_by_key.get(key)
            if entry is None:
                raise RepositoryRegistryError("Select a repository from the server-configured repository list.")
            changed = key != self._active_key
            if changed:
                self._generation += 1
            self._active_key = key
            return entry, changed

    @classmethod
    def single(cls, repository_path: str | Path) -> RepositoryRegistry:
        path = Path(repository_path).expanduser().resolve()
        try:
            root = validate_git_worktree(path)
        except ScanError as exc:
            raise RepositoryRegistryError(str(exc)) from exc
        for role, document_path in DEFAULT_DOCUMENT_PATHS.items():
            _validate_document_path(root, role, document_path, "default")
        entry = RepositoryEntry(
            key="default",
            name=root.name,
            path=root,
            repository_id=make_repository_id(root),
            milestone_root="docs/milestones",
            documents=dict(DEFAULT_DOCUMENT_PATHS),
        )
        return cls((entry,), default_key=entry.key)

    @classmethod
    def from_toml(cls, registry_path: str | Path) -> RepositoryRegistry:
        config_path = Path(registry_path).expanduser()
        try:
            with config_path.open("rb") as stream:
                raw: Any = tomllib.load(stream)
        except OSError as exc:
            raise RepositoryRegistryError(f"Cannot read repository registry {config_path}: {exc}") from exc
        except tomllib.TOMLDecodeError as exc:
            raise RepositoryRegistryError(f"Invalid repository registry {config_path}: {exc}") from exc

        default_key = raw.get("default")
        repositories = raw.get("repositories")
        if not isinstance(default_key, str) or not isinstance(repositories, dict) or not repositories:
            raise RepositoryRegistryError(
                "Repository registry must define a default key and at least one [repositories.<key>] entry."
            )

        entries: list[RepositoryEntry] = []
        for key, value in repositories.items():
            if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", key):
                raise RepositoryRegistryError("Repository registry keys must be simple identifiers.")
            if not isinstance(value, dict):
                raise RepositoryRegistryError(f"Repository registry entry {key!r} must be a table.")
            name = value.get("name")
            configured_path = value.get("path")
            configured_milestone_root = value.get("milestone_root", "docs/milestones")
            configured_documents = value.get("documents", {})
            if not isinstance(name, str) or not name.strip():
                raise RepositoryRegistryError(f"Repository registry entry {key!r} requires a human-readable name.")
            if not isinstance(configured_path, str) or not Path(configured_path).is_absolute():
                raise RepositoryRegistryError(f"Repository registry entry {key!r} requires an absolute path.")
            if not isinstance(configured_milestone_root, str):
                raise RepositoryRegistryError(
                    f"Repository registry entry {key!r} milestone_root must be repository-relative."
                )
            milestone_relative = PurePosixPath(configured_milestone_root)
            if (
                not configured_milestone_root
                or milestone_relative.is_absolute()
                or "\\" in configured_milestone_root
                or any(part in {"", ".", ".."} for part in configured_milestone_root.split("/"))
            ):
                raise RepositoryRegistryError(
                    f"Repository registry entry {key!r} milestone_root must be a safe repository-relative path."
                )
            if not isinstance(configured_documents, dict):
                raise RepositoryRegistryError(
                    f"Repository registry entry {key!r} documents must be a table of repository-relative paths."
                )
            documents: dict[str, str] = {}
            for role, configured_document in configured_documents.items():
                if role not in DOCUMENT_ROLES:
                    raise RepositoryRegistryError(
                        f"Repository registry entry {key!r} has unsupported document role {role!r}."
                    )
                if not isinstance(configured_document, str):
                    raise RepositoryRegistryError(
                        f"Repository registry entry {key!r} document {role!r} must be repository-relative."
                    )
                relative_document = PurePosixPath(configured_document)
                if (
                    not configured_document
                    or relative_document.is_absolute()
                    or "\\" in configured_document
                    or any(part in {"", ".", ".."} for part in configured_document.split("/"))
                ):
                    raise RepositoryRegistryError(
                        f"Repository registry entry {key!r} document {role!r} must be a safe repository-relative path."
                    )
            path = Path(configured_path).resolve()
            try:
                root = validate_git_worktree(path)
            except ScanError as exc:
                raise RepositoryRegistryError(
                    f"Repository registry entry {key!r} is not a usable Git worktree: {exc}"
                ) from exc
            milestone_path = root.joinpath(*milestone_relative.parts)
            current = root
            for part in milestone_relative.parts:
                current = current / part
                if current.is_symlink():
                    raise RepositoryRegistryError(
                        f"Repository registry entry {key!r} milestone_root cannot contain symlinked path components."
                    )
            if not milestone_path.resolve(strict=False).is_relative_to(root):
                raise RepositoryRegistryError(
                    f"Repository registry entry {key!r} milestone_root resolves outside its repository."
                )
            for role, configured_document in configured_documents.items():
                _validate_document_path(root, role, configured_document, key)
                documents[role] = configured_document
            entries.append(
                RepositoryEntry(
                    key=key,
                    name=name.strip(),
                    path=root,
                    repository_id=make_repository_id(root),
                    milestone_root=configured_milestone_root,
                    documents=documents,
                )
            )
        return cls(tuple(entries), default_key=default_key)
