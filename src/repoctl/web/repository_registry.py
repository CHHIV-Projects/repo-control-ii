from __future__ import annotations

import re
import threading
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..scanner.git_ops import ScanError, validate_git_worktree
from ..scanner.util import make_repository_id


class RepositoryRegistryError(ValueError):
    pass


@dataclass(frozen=True)
class RepositoryEntry:
    key: str
    name: str
    path: Path
    repository_id: str


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
        entry = RepositoryEntry(
            key="default",
            name=root.name,
            path=root,
            repository_id=make_repository_id(root),
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
            if not isinstance(name, str) or not name.strip():
                raise RepositoryRegistryError(f"Repository registry entry {key!r} requires a human-readable name.")
            if not isinstance(configured_path, str) or not Path(configured_path).is_absolute():
                raise RepositoryRegistryError(f"Repository registry entry {key!r} requires an absolute path.")
            path = Path(configured_path).resolve()
            try:
                root = validate_git_worktree(path)
            except ScanError as exc:
                raise RepositoryRegistryError(
                    f"Repository registry entry {key!r} is not a usable Git worktree: {exc}"
                ) from exc
            entries.append(
                RepositoryEntry(
                    key=key,
                    name=name.strip(),
                    path=root,
                    repository_id=make_repository_id(root),
                )
            )
        return cls(tuple(entries), default_key=default_key)
