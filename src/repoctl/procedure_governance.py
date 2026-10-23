from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .web.repository_registry import RepositoryEntry


BUNDLE_ROOT = Path(__file__).resolve().parents[2] / "shared" / "procedure"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RELEASE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_REQUIRED_SKILLS = {
    "skills/repo-control-implementation/SKILL.md",
    "skills/repo-control-blocker-bug-escalation/SKILL.md",
    "skills/repo-control-validation-closeout/SKILL.md",
}


class ProcedureGovernanceError(ValueError):
    pass


@dataclass(frozen=True)
class ProcedureArtifact:
    source: str
    target: str
    renderer: str


@dataclass(frozen=True)
class ProcedureBundle:
    root: Path
    release: str
    schema_version: int
    overlay_schema_version: int
    overlay_marker: str
    lock_path: str
    host_entrypoint: str
    manifest_sha256: str
    source_hashes: dict[str, str]
    artifacts: tuple[ProcedureArtifact, ...]


@dataclass(frozen=True)
class ReconciliationPlan:
    repository_path: Path
    repository_id: str
    release: str
    manifest_sha256: str
    status: dict[str, Any]
    files: tuple[tuple[str, bytes], ...]
    lock_bytes: bytes
    snapshots: tuple[tuple[str, str | None], ...]
    blockers: tuple[str, ...]
    changes: tuple[dict[str, Any], ...]
    removals: tuple[str, ...] = ()

    @property
    def can_materialize(self) -> bool:
        return not self.blockers

    def as_dict(self, include_diffs: bool = True) -> dict[str, Any]:
        changes: list[dict[str, Any]] = []
        for change in self.changes:
            item = dict(change)
            if not include_diffs:
                item.pop("diff", None)
            changes.append(item)
        return {
            "repository": {
                "key": self.status["repository"]["key"],
                "name": self.status["repository"]["name"],
                "path": str(self.repository_path),
                "repository_id": self.repository_id,
            },
            "current_release": self.status.get("current_release"),
            "target_release": self.release,
            "status": self.status["status"],
            "overlay_untouched": True,
            "overlay": self.status["overlay"],
            "changes": changes,
            "removals": list(self.removals),
            "blockers": list(self.blockers),
            "materialization_available": self.can_materialize,
        }


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _relative_path(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ProcedureGovernanceError(f"Invalid repository-relative {field}.")
    relative = PurePosixPath(value)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in value.split("/")):
        raise ProcedureGovernanceError(f"Invalid repository-relative {field}: {value!r}.")
    return value


def _bundle_file(root: Path, relative: str) -> Path:
    safe = _relative_path(relative, "bundle source path")
    path = root.joinpath(*PurePosixPath(safe).parts)
    current = root
    for part in PurePosixPath(safe).parts:
        current = current / part
        if current.is_symlink():
            raise ProcedureGovernanceError(f"Bundle source path contains a symlink: {safe}")
    if not path.resolve(strict=False).is_relative_to(root.resolve()):
        raise ProcedureGovernanceError(f"Bundle source path escapes the canonical bundle: {safe}")
    return path


def load_bundle(root: str | Path = BUNDLE_ROOT) -> ProcedureBundle:
    bundle_root = Path(root).resolve()
    manifest_path = bundle_root / "manifest.toml"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ProcedureGovernanceError(f"Canonical procedure manifest is missing or unsafe: {manifest_path}")
    try:
        manifest_bytes = manifest_path.read_bytes()
        raw: Any = tomllib.loads(manifest_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ProcedureGovernanceError(f"Cannot read canonical procedure manifest: {exc}") from exc

    schema_version = raw.get("schema_version")
    release = raw.get("release")
    overlay_schema_version = raw.get("overlay_schema_version")
    overlay_marker = raw.get("overlay_marker")
    lock_path = raw.get("lock_path")
    host_entrypoint = raw.get("host_entrypoint")
    sources_raw = raw.get("sources")
    artifacts_raw = raw.get("artifacts")
    if (
        type(schema_version) is not int
        or schema_version != 1
        or not isinstance(release, str)
        or not _RELEASE_RE.fullmatch(release)
    ):
        raise ProcedureGovernanceError("Canonical procedure manifest has an unsupported schema or release.")
    if type(overlay_schema_version) is not int or overlay_schema_version < 1:
        raise ProcedureGovernanceError("Canonical procedure manifest has an invalid overlay schema version.")
    if not isinstance(overlay_marker, str) or not overlay_marker.strip():
        raise ProcedureGovernanceError("Canonical procedure manifest has no overlay marker.")
    lock_path = _relative_path(lock_path, "lock path")
    host_entrypoint = _relative_path(host_entrypoint, "host entrypoint")
    if not isinstance(sources_raw, list) or not isinstance(artifacts_raw, list):
        raise ProcedureGovernanceError("Canonical procedure manifest must list sources and artifacts.")

    source_hashes: dict[str, str] = {}
    for source in sources_raw:
        if not isinstance(source, dict):
            raise ProcedureGovernanceError("Canonical procedure source entries must be tables.")
        relative = _relative_path(source.get("path"), "source path")
        expected_hash = source.get("sha256")
        if not isinstance(expected_hash, str) or not _SHA256_RE.fullmatch(expected_hash):
            raise ProcedureGovernanceError(f"Canonical source has an invalid SHA-256: {relative}")
        if relative in source_hashes:
            raise ProcedureGovernanceError(f"Canonical source is listed more than once: {relative}")
        source_path = _bundle_file(bundle_root, relative)
        try:
            actual_hash = _sha256(source_path.read_bytes())
        except OSError as exc:
            raise ProcedureGovernanceError(f"Cannot read canonical source {relative}: {exc}") from exc
        if actual_hash != expected_hash:
            raise ProcedureGovernanceError(f"Canonical source hash mismatch: {relative}")
        source_hashes[relative] = expected_hash

    if not _REQUIRED_SKILLS.issubset(source_hashes):
        raise ProcedureGovernanceError("Canonical bundle must include all three accepted Repo Control Skills.")
    if "bootstrap.md" not in source_hashes or "workflow_core.md" not in source_hashes:
        raise ProcedureGovernanceError("Canonical bundle must include bootstrap.md and workflow_core.md.")

    artifacts: list[ProcedureArtifact] = []
    targets: set[str] = set()
    artifact_sources: set[str] = set()
    for artifact in artifacts_raw:
        if not isinstance(artifact, dict):
            raise ProcedureGovernanceError("Canonical artifact entries must be tables.")
        source = _relative_path(artifact.get("source"), "artifact source")
        target = _relative_path(artifact.get("target"), "artifact target")
        renderer = artifact.get("renderer")
        if source not in source_hashes or renderer not in {"copy", "bootstrap"}:
            raise ProcedureGovernanceError(f"Invalid canonical artifact source or renderer: {target}")
        if target in targets:
            raise ProcedureGovernanceError(f"Canonical artifact target is listed more than once: {target}")
        if source in artifact_sources:
            raise ProcedureGovernanceError(f"Canonical source is materialized more than once: {source}")
        if renderer == "bootstrap" and source != "bootstrap.md":
            raise ProcedureGovernanceError("Only bootstrap.md may use the bootstrap renderer.")
        targets.add(target)
        artifact_sources.add(source)
        artifacts.append(ProcedureArtifact(source, target, renderer))
    expected_artifacts = {
        "bootstrap.md": (host_entrypoint, "bootstrap"),
        "skills/repo-control-implementation/SKILL.md": (
            ".agents/skills/repo-control-implementation/SKILL.md",
            "copy",
        ),
        "skills/repo-control-blocker-bug-escalation/SKILL.md": (
            ".agents/skills/repo-control-blocker-bug-escalation/SKILL.md",
            "copy",
        ),
        "skills/repo-control-validation-closeout/SKILL.md": (
            ".agents/skills/repo-control-validation-closeout/SKILL.md",
            "copy",
        ),
        "workflow_core.md": (".repo-control/workflow_core.md", "copy"),
    }
    configured_artifacts = {item.source: (item.target, item.renderer) for item in artifacts}
    if host_entrypoint not in targets or configured_artifacts != expected_artifacts:
        raise ProcedureGovernanceError("Canonical manifest does not map workflow, bootstrap, and Skills to accepted targets.")
    return ProcedureBundle(
        root=bundle_root,
        release=release,
        schema_version=schema_version,
        overlay_schema_version=overlay_schema_version,
        overlay_marker=overlay_marker,
        lock_path=lock_path,
        host_entrypoint=host_entrypoint,
        manifest_sha256=_sha256(manifest_bytes),
        source_hashes=source_hashes,
        artifacts=tuple(artifacts),
    )


def _target_path(root: Path, relative: str) -> tuple[Path, str | None]:
    safe = _relative_path(relative, "managed artifact path")
    path = root.joinpath(*PurePosixPath(safe).parts)
    current = root
    parts = PurePosixPath(safe).parts
    for index, part in enumerate(parts):
        current = current / part
        if current.is_symlink():
            return path, "path contains a symlink"
        if index < len(parts) - 1 and current.exists() and not current.is_dir():
            return path, "parent path component is not a directory"
    if not path.resolve(strict=False).is_relative_to(root.resolve()):
        return path, "path escapes the managed repository"
    if path.exists() and not path.is_file():
        return path, "target is not a regular file"
    return path, None


def _repository_overlay_path(entry: RepositoryEntry) -> str:
    overlay = entry.documents.get("coding_agent_rules")
    if not overlay:
        raise ProcedureGovernanceError(
            f"Managed repository {entry.key!r} has no coding_agent_rules overlay mapping."
        )
    return _relative_path(overlay, "project overlay path")


def _require_managed_entry(entry: RepositoryEntry) -> RepositoryEntry:
    from .workflow.coder_interface import CoderInterfaceError, resolve_repository_entry

    try:
        managed = resolve_repository_entry(entry.path)
    except CoderInterfaceError as exc:
        raise ProcedureGovernanceError(f"Target is not a centrally managed repository: {exc}") from exc
    if (
        managed.key != entry.key
        or managed.path != Path(entry.path).resolve()
        or managed.repository_id != entry.repository_id
        or managed.milestone_root != entry.milestone_root
        or managed.documents != entry.documents
    ):
        raise ProcedureGovernanceError("Repository identity or mappings changed since the managed entry was resolved.")
    return managed


def _render_artifacts(bundle: ProcedureBundle, entry: RepositoryEntry) -> tuple[tuple[str, bytes], ...]:
    overlay_path = _repository_overlay_path(entry)
    rendered: list[tuple[str, bytes]] = []
    for artifact in bundle.artifacts:
        source_path = _bundle_file(bundle.root, artifact.source)
        content = source_path.read_bytes()
        if artifact.renderer == "bootstrap":
            try:
                template = content.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ProcedureGovernanceError("Canonical bootstrap template must be UTF-8.") from exc
            template = template.replace("{{release}}", bundle.release)
            template = template.replace("{{project_overlay_path}}", overlay_path)
            template = template.replace("{{workflow_core_path}}", ".repo-control/workflow_core.md")
            if "{{" in template or "}}" in template:
                raise ProcedureGovernanceError("Canonical bootstrap contains an unsupported template token.")
            content = template.encode("utf-8")
        rendered.append((artifact.target, content))
    return tuple(sorted(rendered))


def _overlay_status(root: Path, entry: RepositoryEntry, bundle: ProcedureBundle) -> dict[str, Any]:
    relative = _repository_overlay_path(entry)
    path, path_error = _target_path(root, relative)
    if path_error:
        return {"path": relative, "valid": False, "reason": path_error}
    if not path.is_file():
        return {"path": relative, "valid": False, "reason": "overlay file is missing"}
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return {"path": relative, "valid": False, "reason": f"overlay is unreadable: {exc}"}
    first_lines = "\n".join(content.splitlines()[:12])
    required_heading = "## Repository-specific rules"
    if bundle.overlay_marker not in first_lines or required_heading not in content:
        return {
            "path": relative,
            "valid": False,
            "reason": "overlay must declare the current marker and a Repository-specific rules section",
        }
    return {"path": relative, "valid": True, "reason": None}


def _read_lock(root: Path, bundle: ProcedureBundle) -> tuple[dict[str, Any] | None, str | None]:
    path, path_error = _target_path(root, bundle.lock_path)
    if path_error:
        return None, path_error
    if not path.exists():
        return None, None
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        return None, f"lock is unreadable or malformed: {exc}"
    if raw.get("schema_version") != 1:
        return None, "lock schema version is unsupported"
    if not isinstance(raw.get("release"), str) or not raw["release"]:
        return None, "lock has no release identifier"
    if not isinstance(raw.get("bundle_manifest_sha256"), str) or not _SHA256_RE.fullmatch(
        raw["bundle_manifest_sha256"]
    ):
        return None, "lock has no valid canonical manifest hash"
    if type(raw.get("overlay_schema_version")) is not int:
        return None, "lock has no overlay schema version"
    try:
        raw["overlay_path"] = _relative_path(raw.get("overlay_path"), "lock overlay path")
        raw["host_entrypoint"] = _relative_path(raw.get("host_entrypoint"), "lock host entrypoint")
    except ProcedureGovernanceError as exc:
        return None, str(exc)
    files = raw.get("files")
    if not isinstance(files, list):
        return None, "lock has no generated-file list"
    hashes: dict[str, str] = {}
    sources: dict[str, str] = {}
    for item in files:
        if not isinstance(item, dict):
            return None, "lock contains an invalid generated-file entry"
        try:
            target = _relative_path(item.get("path"), "lock file path")
        except ProcedureGovernanceError as exc:
            return None, str(exc)
        digest = item.get("sha256")
        source = item.get("source")
        if not isinstance(source, str):
            return None, "lock contains a generated-file entry without a source"
        try:
            _relative_path(source, "lock source path")
        except ProcedureGovernanceError as exc:
            return None, str(exc)
        if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest) or target in hashes:
            return None, "lock contains an invalid or duplicate generated-file hash"
        hashes[target] = digest
        sources[target] = source
    raw["_file_hashes"] = hashes
    raw["_file_sources"] = sources
    return raw, None


def _render_lock(bundle: ProcedureBundle, entry: RepositoryEntry, files: tuple[tuple[str, bytes], ...]) -> bytes:
    lines = [
        "schema_version = 1",
        f"release = {json.dumps(bundle.release)}",
        f"bundle_manifest_sha256 = {json.dumps(bundle.manifest_sha256)}",
        f"overlay_schema_version = {bundle.overlay_schema_version}",
        f"overlay_path = {json.dumps(_repository_overlay_path(entry))}",
        f"host_entrypoint = {json.dumps(bundle.host_entrypoint)}",
    ]
    for target, content in files:
        source = next(artifact.source for artifact in bundle.artifacts if artifact.target == target)
        lines.extend(
            [
                "",
                "[[files]]",
                f"path = {json.dumps(target)}",
                f"source = {json.dumps(source)}",
                f"sha256 = {json.dumps(_sha256(content))}",
            ]
        )
    return ("\n".join(lines) + "\n").encode("utf-8")


def _status_for(root: Path, entry: RepositoryEntry, bundle: ProcedureBundle) -> dict[str, Any]:
    overlay = _overlay_status(root, entry, bundle)
    lock, lock_error = _read_lock(root, bundle)
    issues: list[dict[str, str]] = []
    current_release = lock.get("release") if lock else None
    if not overlay["valid"]:
        issues.append({"status": "OVERLAY_INVALID", "path": overlay["path"], "detail": overlay["reason"]})
    if lock_error:
        issues.append({"status": "INCOMPATIBLE", "path": bundle.lock_path, "detail": lock_error})
    elif lock is None:
        issues.append({"status": "MISSING", "path": bundle.lock_path, "detail": "procedure lock is missing"})
    else:
        if lock.get("release") != bundle.release:
            issues.append(
                {
                    "status": "STALE_VERSION",
                    "path": bundle.lock_path,
                    "detail": f"installed release {lock.get('release')!r}; current release {bundle.release!r}",
                }
            )
        else:
            if lock.get("bundle_manifest_sha256") != bundle.manifest_sha256:
                issues.append(
                    {
                        "status": "HASH_MISMATCH",
                        "path": bundle.lock_path,
                        "detail": "canonical bundle changed without a release version change",
                    }
                )
            expected_sources = {item.target: item.source for item in bundle.artifacts}
            if lock.get("_file_sources") != expected_sources:
                issues.append(
                    {
                        "status": "INCOMPATIBLE",
                        "path": bundle.lock_path,
                        "detail": "lock generated-file list differs from the canonical manifest",
                    }
                )
        if lock.get("overlay_path") != overlay["path"]:
            issues.append(
                {
                    "status": "INCOMPATIBLE",
                    "path": bundle.lock_path,
                    "detail": "lock overlay mapping differs from the central repository mapping",
                }
            )
        if lock.get("overlay_schema_version") != bundle.overlay_schema_version:
            issues.append(
                {
                    "status": "INCOMPATIBLE",
                    "path": bundle.lock_path,
                    "detail": "lock overlay schema differs from the canonical bundle",
                }
            )
        if lock.get("host_entrypoint") != bundle.host_entrypoint:
            issues.append(
                {
                    "status": "INCOMPATIBLE",
                    "path": bundle.lock_path,
                    "detail": "lock host entrypoint differs from the canonical bundle",
                }
            )

    lock_hashes = lock.get("_file_hashes", {}) if lock else {}
    for target, expected_bytes in _render_artifacts(bundle, entry):
        path, path_error = _target_path(root, target)
        if path_error:
            issues.append({"status": "INCOMPATIBLE", "path": target, "detail": path_error})
            continue
        if not path.exists():
            issues.append({"status": "MISSING", "path": target, "detail": "generated artifact is missing"})
            continue
        actual_hash = _sha256(path.read_bytes())
        recorded_hash = lock_hashes.get(target)
        if recorded_hash is None:
            if lock is None and actual_hash == _sha256(expected_bytes):
                continue
            issues.append(
                {
                    "status": "INCOMPATIBLE",
                    "path": target,
                    "detail": "existing file has no matching provenance entry in the procedure lock",
                }
            )
        elif actual_hash != recorded_hash:
            issues.append(
                {
                    "status": "HASH_MISMATCH",
                    "path": target,
                    "detail": f"lock expects {recorded_hash}; current file is {actual_hash}",
                }
            )
        elif actual_hash != _sha256(expected_bytes) and lock and lock.get("release") == bundle.release:
            issues.append(
                {
                    "status": "HASH_MISMATCH",
                    "path": target,
                    "detail": "canonical output changed without a release version change",
                }
            )

    status_order = ("INCOMPATIBLE", "OVERLAY_INVALID", "HASH_MISMATCH", "STALE_VERSION", "MISSING")
    status = next((value for value in status_order if any(issue["status"] == value for issue in issues)), "CURRENT")
    lock_path, lock_path_error = _target_path(root, bundle.lock_path)
    lock_sha256 = None
    if not lock_path_error and lock_path.is_file():
        lock_sha256 = _sha256(lock_path.read_bytes())
    return {
        "repository": {"key": entry.key, "name": entry.name, "path": str(root), "repository_id": entry.repository_id},
        "status": status,
        "current_release": current_release,
        "target_release": bundle.release,
        "lock_sha256": lock_sha256,
        "overlay": overlay,
        "issues": issues,
    }


def inspect_procedure(
    entry: RepositoryEntry,
    bundle: ProcedureBundle | None = None,
) -> dict[str, Any]:
    entry = _require_managed_entry(entry)
    active_bundle = bundle or load_bundle()
    root = Path(entry.path).resolve()
    return _status_for(root, entry, active_bundle)


def _file_snapshot(root: Path, relative: str) -> str | None:
    path, path_error = _target_path(root, relative)
    if path_error:
        raise ProcedureGovernanceError(f"Cannot prepare reconciliation for {relative}: {path_error}.")
    return _sha256(path.read_bytes()) if path.exists() else None


def _make_diff(target: str, current: bytes | None, expected: bytes) -> str:
    old_text = current.decode("utf-8").splitlines(keepends=True) if current is not None else []
    new_text = expected.decode("utf-8").splitlines(keepends=True)
    return "".join(
        difflib.unified_diff(
            old_text,
            new_text,
            fromfile=f"a/{target}" if current is not None else "/dev/null",
            tofile=f"b/{target}",
        )
    )


def preview_reconciliation(
    entry: RepositoryEntry,
    bundle: ProcedureBundle | None = None,
) -> ReconciliationPlan:
    entry = _require_managed_entry(entry)
    active_bundle = bundle or load_bundle()
    root = Path(entry.path).resolve()
    status = _status_for(root, entry, active_bundle)
    overlay = status["overlay"]
    lock, lock_error = _read_lock(root, active_bundle)
    lock_hashes = lock.get("_file_hashes", {}) if lock else {}
    blocker_messages: list[str] = []
    changes: list[dict[str, Any]] = []
    rendered_files = _render_artifacts(active_bundle, entry)
    snapshots: dict[str, str | None] = {}
    write_files: list[tuple[str, bytes]] = []
    trusted_lock = lock is not None and lock_error is None
    for issue in status["issues"]:
        if issue["status"] in {"INCOMPATIBLE", "HASH_MISMATCH"}:
            blocker = f"{issue['path']}: {issue['detail']}"
            if blocker not in blocker_messages:
                blocker_messages.append(blocker)

    if not overlay["valid"]:
        blocker_messages.append(f"Project overlay {overlay['path']} is invalid: {overlay['reason']}")
        snapshots[overlay["path"]] = None
    else:
        snapshots[overlay["path"]] = _file_snapshot(root, overlay["path"])
    if lock_error:
        blocker_messages.append(f"Procedure lock is incompatible: {lock_error}")
    if (
        trusted_lock
        and lock
        and lock.get("release") == active_bundle.release
        and lock.get("bundle_manifest_sha256") != active_bundle.manifest_sha256
    ):
        blocker = "Canonical bundle changed without a release version change"
        if blocker not in blocker_messages:
            blocker_messages.append(blocker)

    for target, expected in rendered_files:
        path, path_error = _target_path(root, target)
        if path_error:
            blocker_messages.append(f"{target}: {path_error}")
            changes.append({"path": target, "action": "blocked", "reason": path_error})
            continue
        current = path.read_bytes() if path.is_file() else None
        snapshots[target] = _sha256(current) if current is not None else None
        recorded_hash = lock_hashes.get(target)
        if current is not None and not trusted_lock:
            if lock is None and lock_error is None and current == expected:
                changes.append(
                    {
                        "path": target,
                        "action": "adopt",
                        "current_sha256": _sha256(current),
                        "expected_sha256": _sha256(expected),
                    }
                )
                continue
            reason = "existing target has no valid procedure-lock provenance"
            blocker_messages.append(f"{target}: {reason}")
            changes.append(
                {
                    "path": target,
                    "action": "blocked",
                    "current_sha256": _sha256(current),
                    "expected_sha256": _sha256(expected),
                    "reason": reason,
                }
            )
            continue
        if current is not None and recorded_hash != _sha256(current):
            reason = "current file differs from its locked hash; refusing to overwrite an unmanaged edit"
            blocker_messages.append(f"{target}: {reason}")
            changes.append(
                {
                    "path": target,
                    "action": "blocked",
                    "current_sha256": _sha256(current),
                    "expected_sha256": _sha256(expected),
                    "reason": reason,
                }
            )
            continue
        if current == expected:
            changes.append(
                {
                    "path": target,
                    "action": "unchanged",
                    "current_sha256": _sha256(expected),
                    "expected_sha256": _sha256(expected),
                }
            )
            continue
        action = "update" if current is not None else "create"
        write_files.append((target, expected))
        try:
            diff = _make_diff(target, current, expected)
        except UnicodeDecodeError:
            reason = "existing generated artifact is not valid UTF-8"
            blocker_messages.append(f"{target}: {reason}")
            changes.append({"path": target, "action": "blocked", "reason": reason})
            write_files.pop()
            continue
        changes.append(
            {
                "path": target,
                "action": action,
                "current_sha256": _sha256(current) if current is not None else None,
                "expected_sha256": _sha256(expected),
                "diff": diff,
            }
        )

    lock_path, lock_path_error = _target_path(root, active_bundle.lock_path)
    if lock_path_error:
        blocker_messages.append(f"{active_bundle.lock_path}: {lock_path_error}")
        changes.append({"path": active_bundle.lock_path, "action": "blocked", "reason": lock_path_error})
        snapshots[active_bundle.lock_path] = None
    else:
        old_lock = lock_path.read_bytes() if lock_path.is_file() else None
        snapshots[active_bundle.lock_path] = _sha256(old_lock) if old_lock is not None else None
        lock_bytes = _render_lock(active_bundle, entry, rendered_files)
        if old_lock is not None and not trusted_lock:
            reason = "existing lock has no valid parseable provenance"
            blocker_messages.append(f"{active_bundle.lock_path}: {reason}")
            changes.append({"path": active_bundle.lock_path, "action": "blocked", "reason": reason})
        elif old_lock == lock_bytes:
            changes.append(
                {
                    "path": active_bundle.lock_path,
                    "action": "unchanged",
                    "current_sha256": _sha256(lock_bytes),
                    "expected_sha256": _sha256(lock_bytes),
                }
            )
        else:
            changes.append(
                {
                    "path": active_bundle.lock_path,
                    "action": "update" if old_lock is not None else "create",
                    "current_sha256": _sha256(old_lock) if old_lock is not None else None,
                    "expected_sha256": _sha256(lock_bytes),
                    "diff": _make_diff(active_bundle.lock_path, old_lock, lock_bytes),
                }
            )

    # A damaged or missing overlay must be reviewed before any shared files are installed.
    plan = ReconciliationPlan(
        repository_path=root,
        repository_id=entry.repository_id,
        release=active_bundle.release,
        manifest_sha256=active_bundle.manifest_sha256,
        status=status,
        files=tuple(write_files),
        lock_bytes=_render_lock(active_bundle, entry, rendered_files),
        snapshots=tuple(sorted(snapshots.items())),
        blockers=tuple(blocker_messages),
        changes=tuple(changes),
    )
    return plan


def _write_atomic(root: Path, relative: str, content: bytes) -> None:
    target, path_error = _target_path(root, relative)
    if path_error:
        raise ProcedureGovernanceError(f"Cannot materialize {relative}: {path_error}.")
    target.parent.mkdir(parents=True, exist_ok=True)
    # Recheck after creating parents so newly introduced symlinks are rejected.
    target, path_error = _target_path(root, relative)
    if path_error:
        raise ProcedureGovernanceError(f"Cannot materialize {relative}: {path_error}.")
    temporary_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".repoctl-", delete=False) as stream:
            temporary_path = stream.name
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, target)
    except OSError as exc:
        if temporary_path and os.path.exists(temporary_path):
            try:
                os.unlink(temporary_path)
            except OSError as cleanup_exc:
                raise ProcedureGovernanceError(
                    f"Could not materialize {relative}: {exc}; temporary-file cleanup also failed: {cleanup_exc}"
                ) from exc
        raise ProcedureGovernanceError(f"Could not materialize {relative}: {exc}") from exc


def materialize_preview(
    entry: RepositoryEntry,
    plan: ReconciliationPlan,
    bundle: ProcedureBundle | None = None,
) -> dict[str, Any]:
    entry = _require_managed_entry(entry)
    root = Path(entry.path).resolve()
    if root != plan.repository_path or entry.repository_id != plan.repository_id:
        raise ProcedureGovernanceError("Prepared reconciliation belongs to a different managed repository.")
    active_bundle = bundle or load_bundle()
    if active_bundle.release != plan.release or active_bundle.manifest_sha256 != plan.manifest_sha256:
        raise ProcedureGovernanceError("Prepared reconciliation uses a different canonical release.")
    if not plan.can_materialize:
        raise ProcedureGovernanceError("Prepared reconciliation is blocked; inspect preview blockers before writing.")
    current_plan = preview_reconciliation(entry, active_bundle)
    if (
        current_plan.files != plan.files
        or current_plan.lock_bytes != plan.lock_bytes
        or current_plan.snapshots != plan.snapshots
        or current_plan.blockers != plan.blockers
        or current_plan.changes != plan.changes
    ):
        raise ProcedureGovernanceError("Prepared reconciliation is stale or was not produced by the current preview.")
    for relative, expected_snapshot in plan.snapshots:
        if _file_snapshot(root, relative) != expected_snapshot:
            raise ProcedureGovernanceError(f"Prepared reconciliation is stale; target changed: {relative}")
    for relative, content in plan.files:
        _write_atomic(root, relative, content)
    _write_atomic(root, active_bundle.lock_path, plan.lock_bytes)
    result = inspect_procedure(entry, active_bundle)
    if result["status"] != "CURRENT":
        raise ProcedureGovernanceError(
            f"Materialization did not produce a CURRENT repository state: {result['status']}."
        )
    return result


def format_procedure_status_text(status: dict[str, Any]) -> str:
    repo = status["repository"]
    lines = [
        f"Repository: {repo['name']} ({repo['key']})",
        f"Path: {repo['path']}",
        f"Repository ID: {repo['repository_id']}",
        f"Procedure status: {status['status']}",
        f"Installed release: {status.get('current_release') or 'none'}",
        f"Target release: {status['target_release']}",
        f"Overlay: {status['overlay']['path']} ({'valid' if status['overlay']['valid'] else 'invalid'})",
    ]
    for issue in status["issues"]:
        lines.append(f"- {issue['status']} {issue['path']}: {issue['detail']}")
    return "\n".join(lines)


def format_procedure_preview_text(plan: ReconciliationPlan) -> str:
    data = plan.as_dict(include_diffs=True)
    repo = data["repository"]
    lines = [
        f"Repository: {repo['name']} ({repo['key']})",
        f"Path: {repo['path']}",
        f"Current release: {data['current_release'] or 'none'}",
        f"Target release: {data['target_release']}",
        f"Status: {data['status']}",
        f"Project overlay: {data['overlay']['path']} (untouched; {'valid' if data['overlay']['valid'] else 'invalid'})",
        "Proposed files:",
    ]
    for change in data["changes"]:
        lines.append(f"- {change['action'].upper()}: {change['path']}")
        if "current_sha256" in change:
            lines.append(f"  current SHA-256: {change['current_sha256'] or 'missing'}")
            lines.append(f"  expected SHA-256: {change['expected_sha256']}")
        if change.get("reason"):
            lines.append(f"  blocked: {change['reason']}")
        if change.get("diff"):
            lines.append(change["diff"].rstrip("\n"))
    lines.append("Removals: none (the preview never deletes files)")
    lines.append(f"Materialization available: {'yes' if data['materialization_available'] else 'no'}")
    for blocker in data["blockers"]:
        lines.append(f"BLOCKER: {blocker}")
    return "\n".join(lines)
