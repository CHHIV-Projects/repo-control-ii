from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tomllib
from datetime import UTC, datetime
from typing import Any
from urllib.parse import unquote, urlsplit

from .procedure_governance import (
    ProcedureBundle,
    ProcedureGovernanceError,
    _overlay_status,
    _render_artifacts,
    _render_lock,
    _sha256,
    _target_path,
    inspect_procedure,
    load_bundle,
)
from .procedure_transactions import (
    FIXED_TARGETS,
    MAX_IMAGE_BYTES,
    MAX_JOURNAL_BYTES,
    MAX_TARGET_BYTES,
    OPTIONAL_TARGETS,
    ProcedureTransactionError,
    _atomic_write,
    _checked_directory,
    _directory_states,
    _fsync_directory,
    _read_regular,
    _repository_store,
    _safe_permissions,
    _target_state,
    _write_image,
    _write_target,
    _active_operations,
)
from .scanner.core import DEFAULT_STATE_ROOT, state_artifact_lock
from .web.repository_registry import (
    DOCUMENT_ROLES,
    RepositoryEntry,
    RepositoryRegistry,
    RepositoryRegistryError,
)
from .workflow.coder_interface import CoderInterfaceError, resolve_repository_entry
from .workflow.git_state import WorkflowGitStateError, inspect_git_state
from .workflow.lifecycle import WorkflowRecordError, load_milestone_workflow
from .workflow.milestones import MilestoneArtifactError, load_milestone_selection


_REMAP_SCHEMA = 1
_ACTIVE_STATES = {
    "PREPARING",
    "REVIEWED",
    "STAGING",
    "ARTIFACTS_STAGED",
    "AWAITING_RESTART",
    "RECOVERY_REQUIRED",
    "ROLLING_BACK",
    "ROLLBACK_AWAITING_RESTART",
}
_TERMINAL_STATES = {"ACTIVATED", "ROLLED_BACK", "ABORTED"}
_REMAP_STORE_NAME = ".repoctl-procedure-remaps"
_REGISTRY_MAX_BYTES = 1024 * 1024
_ROLES = tuple(sorted(DOCUMENT_ROLES))
_MARKDOWN_LINK = re.compile(r"!?\[[^\]]*\]\(\s*(?:<([^>]+)>|([^\s)]+))")


class ProcedureRemapError(RuntimeError):
    pass


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("ascii")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _safe_repository_path(value: str, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or PurePosixPath(value).is_absolute()
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise ProcedureRemapError(f"{label} must be a safe repository-relative path.")
    return value


def _mapped_document_references(
    relative: str,
    content: bytes,
    mapped_paths: set[str],
) -> list[str]:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProcedureRemapError(f"Mapped governing document {relative} is not valid UTF-8.") from exc
    references: set[str] = set()
    for match in _MARKDOWN_LINK.finditer(text):
        target = match.group(1) or match.group(2)
        try:
            parsed = urlsplit(target)
        except ValueError as exc:
            raise ProcedureRemapError(f"Mapped governing document {relative} contains a malformed link.") from exc
        if parsed.scheme or parsed.netloc or not parsed.path:
            continue
        path = unquote(parsed.path)
        parts = [] if path.startswith("/") else list(PurePosixPath(relative).parent.parts)
        escaped = False
        for component in path.split("/"):
            if component in {"", "."}:
                continue
            if component == "..":
                if not parts:
                    escaped = True
                    break
                parts.pop()
            else:
                parts.append(component)
        resolved = "/".join(parts)
        if not escaped and resolved in mapped_paths:
            references.add(resolved)
    return sorted(references)


def _read_repository_file(entry: RepositoryEntry, relative: str, *, limit: int = MAX_TARGET_BYTES) -> bytes:
    target, error = _target_path(entry.path, relative)
    if error:
        raise ProcedureRemapError(f"Unsafe mapped file {relative}: {error}.")
    try:
        metadata = target.lstat()
    except OSError as exc:
        raise ProcedureRemapError(f"Cannot inspect mapped file {relative}: {exc}") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_size > limit
        or metadata.st_mode & 0o7000
        or not _safe_permissions(stat.S_IMODE(metadata.st_mode), metadata.st_gid)
    ):
        raise ProcedureRemapError(f"Mapped file {relative} has unsafe type, ownership, permissions, or size.")
    try:
        content = target.read_bytes()
    except OSError as exc:
        raise ProcedureRemapError(f"Cannot read mapped file {relative}: {exc}") from exc
    if len(content) != metadata.st_size:
        raise ProcedureRemapError(f"Mapped file {relative} changed while being read.")
    return content


def _registry_file(
    registry_path: str | Path,
) -> tuple[RepositoryRegistry, RepositoryEntry, str, Path, bytes, int, int]:
    path = Path(registry_path).expanduser().absolute()
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        if current.is_symlink():
            raise ProcedureRemapError("Central repository registry path cannot contain symlinked components.")
    try:
        metadata = path.lstat()
        registry_bytes = path.read_bytes()
    except OSError as exc:
        raise ProcedureRemapError(f"Cannot safely read central repository registry: {exc}") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_size > _REGISTRY_MAX_BYTES
        or metadata.st_mode & 0o7000
        or not _safe_permissions(stat.S_IMODE(metadata.st_mode), metadata.st_gid)
        or len(registry_bytes) != metadata.st_size
    ):
        raise ProcedureRemapError("Central repository registry has unsafe type, owner, permissions, or size.")
    try:
        registry = RepositoryRegistry.from_toml(path)
    except RepositoryRegistryError as exc:
        raise ProcedureRemapError(f"Central repository registry is invalid: {exc}") from exc
    try:
        from .scanner.git_ops import ScanError, validate_git_worktree

        host_root = validate_git_worktree(path.parent)
    except (OSError, ScanError) as exc:
        raise ProcedureRemapError(f"Central registry is not inside a valid managed worktree: {exc}") from exc
    host_entry = next(
        (entry for entry in registry.entries if entry.path == host_root.resolve(strict=True)),
        None,
    )
    if host_entry is None:
        raise ProcedureRemapError("Central registry must be inside a centrally managed repository.")
    relative = path.relative_to(host_entry.path).as_posix()
    _safe_repository_path(relative, "Central registry path")
    return (
        registry,
        host_entry,
        relative,
        path,
        registry_bytes,
        stat.S_IMODE(metadata.st_mode),
        metadata.st_gid,
    )


def _render_registry_update(
    original: bytes,
    repository_key: str,
    old_documents: dict[str, str],
    new_documents: dict[str, str],
) -> bytes:
    try:
        text = original.decode("utf-8")
        parsed = tomllib.loads(text)
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ProcedureRemapError(f"Central repository registry is not valid UTF-8 TOML: {exc}") from exc
    repositories = parsed.get("repositories")
    configured = repositories.get(repository_key) if isinstance(repositories, dict) else None
    configured_documents = configured.get("documents") if isinstance(configured, dict) else None
    if configured_documents != old_documents:
        raise ProcedureRemapError("Registered governing-role map changed after preview.")

    table_header = f"[repositories.{repository_key}.documents]"
    lines = text.splitlines(keepends=True)
    table_count = 0
    in_target = False
    found: dict[str, int] = {}
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_target = stripped == table_header
            if in_target:
                table_count += 1
            continue
        if not in_target:
            continue
        for role in _ROLES:
            match = re.fullmatch(
                rf'([ \t]*{re.escape(role)}[ \t]*=[ \t]*)("(?:\\.|[^"\\])*")([ \t]*(?:#.*)?)(\r?\n)?',
                line,
            )
            if match:
                if role in found:
                    raise ProcedureRemapError(f"Registry contains duplicate {role!r} mapping lines.")
                try:
                    value = json.loads(match.group(2))
                except json.JSONDecodeError as exc:
                    raise ProcedureRemapError(f"Registry mapping {role!r} is not a plain quoted string.") from exc
                if value != old_documents[role]:
                    raise ProcedureRemapError(f"Registry mapping {role!r} changed after preview.")
                lines[index] = (
                    match.group(1)
                    + json.dumps(new_documents[role])
                    + match.group(3)
                    + (match.group(4) or "")
                )
                found[role] = index
    if table_count != 1 or set(found) != set(_ROLES):
        raise ProcedureRemapError("The exact five-role TOML table is not safely editable in place.")
    updated = "".join(lines).encode("utf-8")
    try:
        updated_payload = tomllib.loads(updated.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ProcedureRemapError(f"Generated registry candidate is invalid TOML: {exc}") from exc
    updated_entry = updated_payload["repositories"][repository_key]
    if updated_entry["documents"] != new_documents:
        raise ProcedureRemapError("Generated registry candidate does not contain the reviewed role map.")
    for key, value in parsed["repositories"].items():
        if key == repository_key:
            continue
        if updated_payload["repositories"].get(key) != value:
            raise ProcedureRemapError("Generated registry candidate changed an unrelated repository entry.")
    for key, value in parsed.items():
        if key != "repositories" and updated_payload.get(key) != value:
            raise ProcedureRemapError("Generated registry candidate changed an unrelated top-level setting.")
    return updated


def _git_snapshot(
    entry: RepositoryEntry,
    allowed_documents: set[str],
    registry_relative: str | None,
    workflow_sidecar: str | None = None,
) -> dict[str, Any]:
    try:
        state = inspect_git_state(str(entry.path))
    except WorkflowGitStateError as exc:
        raise ProcedureRemapError(f"Cannot inspect Git state for {entry.key}: {exc}") from exc
    if state["git_operation_in_progress"] or state["branch"].get("state") != "attached":
        raise ProcedureRemapError(f"Repository {entry.key} has a detached HEAD or active Git operation.")
    entries = state["working_tree"]["entries"]
    classified = []
    blockers: list[str] = []
    for item in entries:
        relative = item["path"]
        allowed = relative in allowed_documents or relative == workflow_sidecar
        if registry_relative is not None and relative == registry_relative:
            allowed = False
            blockers.append(f"{relative}: central registry must be clean before remap.")
        if relative in {
            ".github/copilot-instructions.md",
            "AGENTS.md",
            ".repo-control/shared-procedure.lock.toml",
            ".repo-control/workflow_core.md",
            ".agents/skills/repo-control-implementation/SKILL.md",
            ".agents/skills/repo-control-blocker-bug-escalation/SKILL.md",
            ".agents/skills/repo-control-validation-closeout/SKILL.md",
        }:
            allowed = False
            blockers.append(f"{relative}: generated procedure artifacts must be clean before remap.")
        disposition = "reviewed governing-document input" if relative in allowed_documents else (
            "Repo Control workflow sidecar" if allowed else "unrelated dirty path"
        )
        classified.append(
            {
                "path": relative,
                "kind": item.get("kind"),
                "xy": item.get("xy"),
                "disposition": disposition,
            }
        )
        if not allowed:
            blockers.append(f"{relative}: unrelated dirty path blocks remapping.")
    snapshot = {
        "root": str(entry.path),
        "head": state["head"],
        "branch": state["branch"],
        "working_tree": classified,
        "git_operations": state["git_operations"],
    }
    return {"snapshot": snapshot, "sha256": _digest(snapshot), "blockers": blockers}


def _milestone_snapshot(entry: RepositoryEntry, state_root: str | Path) -> dict[str, Any]:
    try:
        selection = load_milestone_selection(entry.path, state_root, entry.milestone_root)
        if selection["status"] != "selected":
            raise ProcedureRemapError("A valid selected milestone is required for governing-role remapping.")
        candidate = selection["selection"]
        workflow = load_milestone_workflow(entry.path, state_root, entry.milestone_root, candidate)
    except (MilestoneArtifactError, WorkflowRecordError) as exc:
        raise ProcedureRemapError(f"Cannot verify selected milestone state: {exc}") from exc
    return {
        "milestone_id": candidate["milestone_id"],
        "prompt_path": candidate["prompt_path"],
        "prompt_identity": workflow["prompt_identity"],
        "phase": workflow["phase"],
        "event_count": workflow["event_count"],
    }


def _runtime_snapshot(
    entry: RepositoryEntry,
    *,
    active_key: str,
    registry_generation: int,
    runtime_instance_id: str,
) -> dict[str, Any]:
    if (
        active_key != entry.key
        or type(registry_generation) is not int
        or registry_generation < 0
        or not isinstance(runtime_instance_id, str)
        or not runtime_instance_id
    ):
        raise ProcedureRemapError("The exact target repository must be actively selected in the live service.")
    return {
        "active_key": active_key,
        "registry_generation": registry_generation,
        "runtime_instance_id": runtime_instance_id,
    }


def _validate_maps(old_documents: dict[str, str], new_documents: dict[str, str]) -> dict[str, str]:
    if set(old_documents) != set(_ROLES) or set(new_documents) != set(_ROLES):
        raise ProcedureRemapError("Remapping requires exactly the five established governing-document roles.")
    normalized_old = {
        role: _safe_repository_path(old_documents[role], f"Existing {role} path")
        for role in _ROLES
    }
    normalized = {
        role: _safe_repository_path(new_documents[role], f"New {role} path")
        for role in _ROLES
    }
    if len(set(normalized.values())) != len(_ROLES):
        raise ProcedureRemapError("New governing-role mappings must use five distinct paths.")
    if len(set(normalized_old.values())) != len(_ROLES):
        raise ProcedureRemapError("Existing governing-role mappings must use five distinct paths.")
    if normalized == normalized_old:
        raise ProcedureRemapError("The proposed role map is unchanged.")
    return normalized


def _plan_data(
    entry: RepositoryEntry,
    registry_entry: RepositoryEntry,
    registry_relative: str,
    registry_bytes: bytes,
    registry_after: bytes,
    new_documents: dict[str, str],
    bundle: ProcedureBundle,
    artifact_before: dict[str, bytes],
    artifact_after: dict[str, bytes],
    artifact_metadata: dict[str, dict[str, Any]],
    document_hashes: dict[str, str],
    registry_mode: int,
    registry_gid: int,
    git_snapshots: dict[str, dict[str, Any]],
    milestone: dict[str, Any],
    runtime: dict[str, Any],
) -> dict[str, Any]:
    artifact_changes = [
        {
            "path": path,
            "before_sha256": _sha256(artifact_before[path]),
            "after_sha256": _sha256(artifact_after[path]),
            "before_bytes": len(artifact_before[path]),
            "after_bytes": len(artifact_after[path]),
            "mode": artifact_metadata[path]["mode"],
            "gid": artifact_metadata[path]["gid"],
        }
        for path in sorted(artifact_after)
        if artifact_before[path] != artifact_after[path]
    ]
    return {
        "repository": {
            "key": entry.key,
            "repository_id": entry.repository_id,
            "root": str(entry.path),
        },
        "registry": {
            "path": str(registry_entry.path / registry_relative),
            "sha256": _sha256(registry_bytes),
            "after_sha256": _sha256(registry_after),
            "mode": registry_mode,
            "gid": registry_gid,
        },
        "old_documents": entry.documents,
        "new_documents": new_documents,
        "document_hashes": document_hashes,
        "bundle": {
            "release": bundle.release,
            "manifest_sha256": bundle.manifest_sha256,
            "source_hashes": bundle.source_hashes,
        },
        "procedure_changes": artifact_changes,
        "lock": {
            "path": bundle.lock_path,
            "before_sha256": _sha256(artifact_before[bundle.lock_path]),
            "after_sha256": _sha256(artifact_after[bundle.lock_path]),
        },
        "git": git_snapshots,
        "milestone": milestone,
        "runtime": runtime,
    }


def preview_role_remap(
    entry: RepositoryEntry,
    new_documents: dict[str, str],
    *,
    registry_path: str | Path | None = None,
    state_root: str | Path = DEFAULT_STATE_ROOT,
    active_key: str,
    registry_generation: int,
    runtime_instance_id: str,
    bundle: ProcedureBundle | None = None,
    _state_lock_held: bool = False,
) -> dict[str, Any]:
    if registry_path is None:
        from .workflow.coder_interface import CENTRAL_REPOSITORY_REGISTRY_PATH

        registry_path = CENTRAL_REPOSITORY_REGISTRY_PATH
    try:
        managed = resolve_repository_entry(entry.path)
    except CoderInterfaceError as exc:
        raise ProcedureRemapError(f"Target is not currently centrally managed: {exc}") from exc
    if managed != entry:
        raise ProcedureRemapError("Repository identity or current role map changed before remap preview.")
    normalized_new = _validate_maps(entry.documents, new_documents)
    active_bundle = bundle or load_bundle()
    if not any(item.target == "AGENTS.md" for item in active_bundle.artifacts):
        raise ProcedureRemapError("Governing-role remapping requires the six-artifact procedure release.")
    current_status = inspect_procedure(entry, active_bundle)
    if current_status["status"] != "CURRENT" or current_status["current_release"] != active_bundle.release:
        raise ProcedureRemapError("Remap requires the installed shared procedure to be CURRENT.")
    if _state_lock_held:
        store = _repository_store(state_root, entry.repository_id, create=False)
        active_transactions, transaction_errors = _active_operations(store, entry)
        if active_transactions or transaction_errors:
            raise ProcedureRemapError("An unresolved procedure transaction blocks remapping.")
    else:
        from .procedure_transactions import inspect_transaction

        pending = inspect_transaction(entry, state_root)
        if pending and (pending.get("blocked") or pending.get("state") not in {"VERIFIED", "RECOVERED"}):
            raise ProcedureRemapError("An unresolved procedure transaction blocks remapping.")
    root = Path(entry.path).resolve(strict=True)
    proposed_entry = RepositoryEntry(
        key=entry.key,
        name=entry.name,
        path=root,
        repository_id=entry.repository_id,
        milestone_root=entry.milestone_root,
        documents=normalized_new,
    )
    overlay = _overlay_status(root, proposed_entry, active_bundle)
    if not overlay["valid"]:
        raise ProcedureRemapError(f"New coding-agent overlay is invalid: {overlay['reason']}.")
    mapped_paths = sorted(set(entry.documents.values()) | set(normalized_new.values()))
    document_images = {relative: _read_repository_file(entry, relative) for relative in mapped_paths}
    document_hashes = {relative: _sha256(content) for relative, content in document_images.items()}
    mapped_path_set = set(mapped_paths)
    document_dependencies = [
        {
            "role": role,
            "source": entry.documents[role],
            "destination": normalized_new[role],
            "source_sha256": document_hashes[entry.documents[role]],
            "destination_sha256": document_hashes[normalized_new[role]],
            "source_references": _mapped_document_references(
                entry.documents[role],
                document_images[entry.documents[role]],
                mapped_path_set,
            ),
            "destination_references": _mapped_document_references(
                normalized_new[role],
                document_images[normalized_new[role]],
                mapped_path_set,
            ),
        }
        for role in _ROLES
    ]
    parsed_registry, host_entry, registry_relative, _, registry_bytes, registry_mode, registry_gid = _registry_file(
        registry_path
    )
    if host_entry.path == entry.path:
        raise ProcedureRemapError("A role remap cannot target the repository that owns the central registry.")
    registered_target = next((item for item in parsed_registry.entries if item.key == entry.key), None)
    if registered_target != entry:
        raise ProcedureRemapError("The supplied registry does not contain the exact current managed repository entry.")
    registry_after = _render_registry_update(registry_bytes, entry.key, entry.documents, normalized_new)
    rendered = dict(_render_artifacts(active_bundle, proposed_entry))
    artifact_before: dict[str, bytes] = {}
    artifact_metadata: dict[str, dict[str, Any]] = {}
    for path in (*rendered.keys(), active_bundle.lock_path):
        artifact_before[path] = _read_repository_file(entry, path)
        artifact_metadata[path] = _target_state(entry, path)
    artifact_after = dict(rendered)
    artifact_after[active_bundle.lock_path] = _render_lock(active_bundle, proposed_entry, tuple(sorted(rendered.items())))
    changed_targets = {
        path for path in artifact_after if artifact_before[path] != artifact_after[path]
    }
    generated_artifacts = [
        {
            "path": path,
            "before_sha256": _sha256(artifact_before[path]),
            "after_sha256": _sha256(artifact_after[path]),
            "changed": artifact_before[path] != artifact_after[path],
        }
        for path in sorted(item.target for item in active_bundle.artifacts)
    ]
    expected_changed_targets = {
        ".github/copilot-instructions.md",
        "AGENTS.md",
        active_bundle.lock_path,
    }
    if changed_targets != expected_changed_targets:
        raise ProcedureRemapError(
            "The remap would alter an unexpected generated-procedure target set."
        )
    allowed_documents = set(entry.documents.values()) | set(normalized_new.values())
    milestone = _milestone_snapshot(entry, state_root)
    prompt_name = PurePosixPath(milestone["prompt_path"]).name
    workflow_sidecar = str(
        PurePosixPath(milestone["prompt_path"]).with_name(
            prompt_name.removesuffix("_prompt.md") + "_workflow.md"
        )
    )
    target_git = _git_snapshot(entry, allowed_documents, None, workflow_sidecar)
    registry_git = _git_snapshot(host_entry, set(), registry_relative)
    git_snapshots = {
        entry.key: target_git["snapshot"],
        host_entry.key: registry_git["snapshot"],
    }
    git_blockers = target_git["blockers"] + registry_git["blockers"]
    runtime = _runtime_snapshot(
        entry,
        active_key=active_key,
        registry_generation=registry_generation,
        runtime_instance_id=runtime_instance_id,
    )
    blockers = list(git_blockers)
    for role, relative in normalized_new.items():
        if relative == entry.documents[role]:
            continue
        if relative in entry.documents.values() and entry.documents[role] != relative:
            blockers.append(f"{relative}: proposed destination is currently assigned to a different role.")
    data = _plan_data(
        entry,
        host_entry,
        registry_relative,
        registry_bytes,
        registry_after,
        normalized_new,
        active_bundle,
        artifact_before,
        artifact_after,
        artifact_metadata,
        document_hashes,
        registry_mode,
        registry_gid,
        git_snapshots,
        milestone,
        runtime,
    )
    plan_sha256 = _digest(data)
    return {
        "plan_sha256": plan_sha256,
        "repository": data["repository"],
        "registry": data["registry"],
        "old_documents": entry.documents,
        "new_documents": normalized_new,
        "document_hashes": document_hashes,
        "document_dependencies": document_dependencies,
        "procedure_release": active_bundle.release,
        "manifest_sha256": active_bundle.manifest_sha256,
        "procedure_changes": data["procedure_changes"],
        "generated_artifacts": generated_artifacts,
        "lock": data["lock"],
        "milestone": milestone,
        "runtime": runtime,
        "git": git_snapshots,
        "blockers": blockers,
        "materialization_available": not blockers,
        "operator_prerequisites": [
            "All five source and destination files exist as safe, owned regular files; their hashes are recorded above.",
            "The exact managed repository is selected, the running registry is current, and the 1.4.0 procedure is CURRENT.",
            "Only reviewed governing-document inputs and the selected milestone workflow sidecar may be dirty.",
            "Review the complete plan digest and separately confirm staging as Product Owner.",
        ],
        "restart_implications": {
            "required": True,
            "registry_write_order": "after generated procedure entrypoints and lock",
            "service_mutations_blocked_until_restart": True,
            "repository_selection_resets_on_restart": True,
            "cross_root_atomicity_claimed": False,
        },
        "rollback_steps": [
            "Inspect the operation with coder-procedure-remap-status.",
            "Run coder-procedure-remap-recover --confirm through the out-of-band operator path.",
            "Restart the service, select the target repository, and activate rollback verification.",
        ],
        "_internal": {
            "registry_entry": host_entry,
            "registry_relative": registry_relative,
            "registry_before": registry_bytes,
            "registry_after": registry_after,
            "registry_mode": registry_mode,
            "registry_gid": registry_gid,
            "artifact_before": artifact_before,
            "artifact_after": artifact_after,
            "bundle": active_bundle,
            "milestone": milestone,
            "runtime": runtime,
            "plan_data": data,
        },
    }


def _remap_store(state_root: str | Path, repository_id: str, *, create: bool) -> Path:
    store = _repository_store(state_root, repository_id, create=create)
    root = store.parent / _REMAP_STORE_NAME
    if create:
        _checked_directory(root, create=True)
    elif not root.exists() and not root.is_symlink():
        return root / repository_id
    else:
        _checked_directory(root, create=False)
    repository_store = root / repository_id
    if create:
        _checked_directory(repository_store, create=True)
    elif not repository_store.exists() and not repository_store.is_symlink():
        return repository_store
    else:
        _checked_directory(repository_store, create=False)
    for child in repository_store.iterdir():
        if not child.is_dir() or child.is_symlink():
            raise ProcedureRemapError("Remap transaction storage contains unsafe data.")
        _checked_directory(child, create=False)
    return repository_store


def _operation_path(state_root: str | Path, repository_id: str, operation_id: str, *, create: bool = False) -> Path:
    if len(operation_id) != 32 or any(char not in "0123456789abcdef" for char in operation_id):
        raise ProcedureRemapError("Remap operation ID is invalid.")
    store = _remap_store(state_root, repository_id, create=create)
    path = store / operation_id
    if create:
        _checked_directory(path, create=True)
    else:
        _checked_directory(path, create=False)
    return path


def _write_remap_journal(operation_directory: Path, journal: dict[str, Any]) -> None:
    journal["updated_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    encoded = _canonical_json(journal)
    if len(encoded) > MAX_JOURNAL_BYTES:
        raise ProcedureRemapError("Remap transaction journal exceeds its size limit.")
    _atomic_write(operation_directory / "journal.json", encoded)


def _read_remap_journal(operation_directory: Path, entry: RepositoryEntry) -> dict[str, Any]:
    raw = _read_regular(operation_directory / "journal.json", MAX_JOURNAL_BYTES)
    try:
        journal = json.loads(raw.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProcedureRemapError(f"Remap transaction journal is malformed: {exc}") from exc
    if (
        not isinstance(journal, dict)
        or journal.get("schema_version") != _REMAP_SCHEMA
        or journal.get("operation_id") != operation_directory.name
        or journal.get("repository_key") != entry.key
        or journal.get("repository_id") != entry.repository_id
        or journal.get("repository_path") != str(entry.path)
        or journal.get("state") not in _ACTIVE_STATES | _TERMINAL_STATES
        or not isinstance(journal.get("targets"), list)
        or not isinstance(journal.get("registry_target"), dict)
        or not isinstance(journal.get("registry_repository_key"), str)
        or not isinstance(journal.get("registry_repository_id"), str)
        or not isinstance(journal.get("registry_repository_path"), str)
        or not Path(journal["registry_repository_path"]).is_absolute()
        or journal.get("registry_repository_key") == journal.get("repository_key")
    ):
        raise ProcedureRemapError("Remap journal identity or schema is invalid.")
    if journal.get("registry_repository_path") == journal.get("repository_path"):
        raise ProcedureRemapError("Remap journal cannot use the target repository as its registry owner.")
    old_documents = journal.get("old_documents")
    new_documents = journal.get("new_documents")
    if not isinstance(old_documents, dict) or not isinstance(new_documents, dict):
        raise ProcedureRemapError("Remap journal role maps are invalid.")
    _validate_maps(old_documents, new_documents)
    document_hashes = journal.get("document_hashes")
    expected_document_paths = set(old_documents.values()) | set(new_documents.values())
    if (
        not isinstance(document_hashes, dict)
        or set(document_hashes) != expected_document_paths
        or any(
            not isinstance(path, str)
            or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
            for path, digest in document_hashes.items()
        )
    ):
        raise ProcedureRemapError("Remap journal governing-document fingerprints are invalid.")
    for path in document_hashes:
        _safe_repository_path(path, "Remap journal governing document")
    if (
        not isinstance(journal.get("bundle"), dict)
        or not isinstance(journal["bundle"].get("release"), str)
        or not isinstance(journal["bundle"].get("manifest_sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", journal["bundle"]["manifest_sha256"])
        or not isinstance(journal.get("plan_sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", journal["plan_sha256"])
    ):
        raise ProcedureRemapError("Remap journal procedure release is invalid.")
    seen_targets: set[str] = set()
    expected_image_names = {"journal.json", "before-registry.bin", "after-registry.bin"}
    allowed_targets = {
        *FIXED_TARGETS,
        *OPTIONAL_TARGETS,
        ".repo-control/shared-procedure.lock.toml",
    }
    for index, item in enumerate(journal["targets"]):
        if (
            not isinstance(item, dict)
            or item.get("index") != index
            or not isinstance(item.get("path"), str)
            or item["path"] in seen_targets
            or item["path"] not in allowed_targets
            or not isinstance(item.get("before_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", item["before_sha256"])
            or not isinstance(item.get("after_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", item["after_sha256"])
            or item.get("before_file") != f"before-{index}.bin"
            or item.get("after_file") != f"after-{index}.bin"
            or type(item.get("before_mode")) is not int
            or type(item.get("after_mode")) is not int
            or type(item.get("before_gid")) is not int
            or type(item.get("after_gid")) is not int
            or item["before_mode"] < 0
            or item["before_mode"] > 0o777
            or item["after_mode"] < 0
            or item["after_mode"] > 0o777
            or item["before_gid"] < 0
            or item["after_gid"] < 0
            or not _safe_permissions(item["before_mode"], item["before_gid"])
            or not _safe_permissions(item["after_mode"], item["after_gid"])
        ):
            raise ProcedureRemapError("Remap journal contains an invalid procedure target.")
        _safe_repository_path(item["path"], "Remap journal target")
        seen_targets.add(item["path"])
        expected_image_names.update({item["before_file"], item["after_file"]})
        for name in (item["before_file"], item["after_file"]):
            image = operation_directory / name
            try:
                image.lstat()
            except FileNotFoundError:
                if journal["state"] == "PREPARING":
                    continue
                raise ProcedureRemapError("Remap journal is missing a required recovery image.")
            image_data = _read_regular(image, MAX_TARGET_BYTES)
            expected_hash = item["before_sha256"] if name == item["before_file"] else item["after_sha256"]
            if _sha256(image_data) != expected_hash:
                raise ProcedureRemapError(f"Remap journal image hash is invalid for {item['path']}.")
    if seen_targets != {
        ".github/copilot-instructions.md",
        "AGENTS.md",
        ".repo-control/shared-procedure.lock.toml",
    }:
        raise ProcedureRemapError("Remap journal contains an unexpected generated-procedure target set.")
    registry_target = journal["registry_target"]
    if (
        not isinstance(registry_target.get("relative"), str)
        or not isinstance(registry_target.get("before_sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", registry_target["before_sha256"])
        or not isinstance(registry_target.get("after_sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", registry_target["after_sha256"])
        or type(registry_target.get("before_mode")) is not int
        or type(registry_target.get("after_mode")) is not int
        or type(registry_target.get("before_gid")) is not int
        or type(registry_target.get("after_gid")) is not int
        or not 0 <= registry_target["before_mode"] <= 0o777
        or not 0 <= registry_target["after_mode"] <= 0o777
        or registry_target["before_gid"] < 0
        or registry_target["after_gid"] < 0
        or not _safe_permissions(registry_target["before_mode"], registry_target["before_gid"])
        or not _safe_permissions(registry_target["after_mode"], registry_target["after_gid"])
    ):
        raise ProcedureRemapError("Remap journal registry target is invalid.")
    _safe_repository_path(registry_target["relative"], "Remap journal registry path")
    if registry_target["relative"] != "ops/repositories.toml":
        raise ProcedureRemapError("Remap journal registry target is outside the canonical registry path.")
    for name in ("before-registry.bin", "after-registry.bin"):
        image = operation_directory / name
        try:
            image.lstat()
        except FileNotFoundError:
            if journal["state"] == "PREPARING":
                continue
            raise ProcedureRemapError("Remap journal is missing a required registry image.")
        image_data = _read_regular(image, _REGISTRY_MAX_BYTES)
        expected_hash = (
            registry_target["before_sha256"] if name == "before-registry.bin" else registry_target["after_sha256"]
        )
        if _sha256(image_data) != expected_hash:
            raise ProcedureRemapError("Remap journal registry image hash is invalid.")
    actual_names = {child.name for child in operation_directory.iterdir()}
    if not actual_names <= expected_image_names or (
        journal["state"] != "PREPARING" and actual_names != expected_image_names
    ):
        raise ProcedureRemapError("Remap operation directory contains unknown or missing recovery data.")
    return journal


def stage_role_remap(
    entry: RepositoryEntry,
    new_documents: dict[str, str],
    approved_plan_sha256: str,
    state_root: str | Path = DEFAULT_STATE_ROOT,
    *,
    registry_path: str | Path | None = None,
    active_key: str,
    registry_generation: int,
    runtime_instance_id: str,
    bundle: ProcedureBundle | None = None,
) -> dict[str, Any]:
    if len(approved_plan_sha256) != 64 or any(char not in "0123456789abcdef" for char in approved_plan_sha256):
        raise ProcedureRemapError("The reviewed remap plan SHA-256 is required.")
    with state_artifact_lock(state_root):
        plan = preview_role_remap(
            entry,
            new_documents,
            registry_path=registry_path,
            state_root=state_root,
            active_key=active_key,
            registry_generation=registry_generation,
            runtime_instance_id=runtime_instance_id,
            bundle=bundle,
            _state_lock_held=True,
        )
        if not plan["materialization_available"]:
            raise ProcedureRemapError("Remap preview has blockers; no operation was prepared.")
        if not hmac.compare_digest(plan["plan_sha256"], approved_plan_sha256):
            raise ProcedureRemapError("Remap preview changed after review; preview and confirm again.")
        internal = plan["_internal"]
        existing = inspect_role_remap(entry, state_root)
        if existing and existing["state"] in _ACTIVE_STATES:
            raise ProcedureRemapError("An unresolved role-remap operation already blocks this repository.")
        operation_id = os.urandom(16).hex()
        operation_directory = _operation_path(state_root, entry.repository_id, operation_id, create=True)
        artifact_paths = sorted(internal["artifact_after"])
        targets = []
        images: dict[str, bytes] = {}
        total_bytes = len(internal["registry_before"]) + len(internal["registry_after"])
        for relative in artifact_paths:
            before = internal["artifact_before"][relative]
            after = internal["artifact_after"][relative]
            if before == after:
                continue
            index = len(targets)
            state = _target_state(entry, relative)
            if (
                not state["present"]
                or state["sha256"] != _sha256(before)
                or state["mode"] != next(
                    item["mode"] for item in plan["procedure_changes"] if item["path"] == relative
                )
                or state["gid"] != next(
                    item["gid"] for item in plan["procedure_changes"] if item["path"] == relative
                )
            ):
                raise ProcedureRemapError(f"Generated procedure target changed after preview: {relative}.")
            images[f"before-{index}.bin"] = before
            images[f"after-{index}.bin"] = after
            total_bytes += len(before) + len(after)
            targets.append(
                {
                    "index": index,
                    "path": relative,
                    "before_file": f"before-{index}.bin",
                    "after_file": f"after-{index}.bin",
                    "before_sha256": _sha256(before),
                    "after_sha256": _sha256(after),
                    "before_mode": state["mode"],
                    "after_mode": state["mode"],
                    "before_gid": state["gid"],
                    "after_gid": state["gid"],
                }
            )
        if total_bytes > MAX_IMAGE_BYTES:
            raise ProcedureRemapError("Remap recovery images exceed the aggregate size limit.")
        registry_entry: RepositoryEntry = internal["registry_entry"]
        registry_relative: str = internal["registry_relative"]
        registry_state = _target_state(registry_entry, registry_relative)
        if not registry_state["present"] or registry_state["sha256"] != _sha256(internal["registry_before"]):
            raise ProcedureRemapError("Central registry changed after preview.")
        images["before-registry.bin"] = internal["registry_before"]
        images["after-registry.bin"] = internal["registry_after"]
        journal = {
            "schema_version": _REMAP_SCHEMA,
            "operation_id": operation_id,
            "repository_key": entry.key,
            "repository_id": entry.repository_id,
            "repository_path": str(entry.path),
            "registry_repository_key": registry_entry.key,
            "registry_repository_id": registry_entry.repository_id,
            "registry_repository_path": str(registry_entry.path),
            "registry_target": {
                "relative": registry_relative,
                "before_sha256": _sha256(internal["registry_before"]),
                "after_sha256": _sha256(internal["registry_after"]),
                "before_mode": registry_state["mode"],
                "after_mode": registry_state["mode"],
                "before_gid": registry_state["gid"],
                "after_gid": registry_state["gid"],
            },
            "old_documents": entry.documents,
            "new_documents": plan["new_documents"],
            "document_hashes": plan["document_hashes"],
            "bundle": {
                "release": internal["bundle"].release,
                "manifest_sha256": internal["bundle"].manifest_sha256,
            },
            "plan_sha256": plan["plan_sha256"],
            "milestone": internal["milestone"],
            "runtime": internal["runtime"],
            "git": plan["git"],
            "state": "PREPARING",
            "targets": targets,
            "created_at": "",
            "updated_at": "",
        }
        _write_remap_journal(operation_directory, journal)
        for name, content in images.items():
            _write_image(operation_directory / name, content)
        _fsync_directory(operation_directory)
        journal["state"] = "STAGING"
        _write_remap_journal(operation_directory, journal)
        try:
            target_directories = _directory_states(entry, tuple(item["path"] for item in targets))
            for item in targets:
                current = _target_state(entry, item["path"])
                expected_before = {
                    "present": True,
                    "sha256": item["before_sha256"],
                    "mode": item["before_mode"],
                    "gid": item["before_gid"],
                }
                if current != expected_before:
                    raise ProcedureRemapError(
                        f"Generated procedure target changed after review: {item['path']}."
                    )
                _write_target(
                    entry,
                    item["path"],
                    _read_regular(operation_directory / item["after_file"], MAX_TARGET_BYTES),
                    current,
                    item["after_mode"],
                    item["after_gid"],
                    f".repoctl-remap-{operation_id}-{item['index']}.tmp",
                    directory_states=target_directories,
                )
                journal["state"] = "STAGING"
                _write_remap_journal(operation_directory, journal)
            journal["state"] = "ARTIFACTS_STAGED"
            _write_remap_journal(operation_directory, journal)
            registry_directories = _directory_states(registry_entry, (registry_relative,))
            current_registry = _target_state(registry_entry, registry_relative)
            registry_state_after = journal["registry_target"]
            expected_registry_before = {
                "present": True,
                "sha256": registry_state_after["before_sha256"],
                "mode": registry_state_after["before_mode"],
                "gid": registry_state_after["before_gid"],
            }
            if current_registry != expected_registry_before:
                raise ProcedureRemapError("Central registry changed after remap review.")
            _write_target(
                registry_entry,
                registry_relative,
                internal["registry_after"],
                current_registry,
                registry_state_after["after_mode"],
                registry_state_after["after_gid"],
                f".repoctl-remap-{operation_id}-registry.tmp",
                directory_states=registry_directories,
            )
            for item in targets:
                expected_after = {
                    "present": True,
                    "sha256": item["after_sha256"],
                    "mode": item["after_mode"],
                    "gid": item["after_gid"],
                }
                if _target_state(entry, item["path"]) != expected_after:
                    raise ProcedureRemapError(
                        f"Generated procedure target changed while staging: {item['path']}."
                    )
            expected_registry_after = {
                "present": True,
                "sha256": registry_state_after["after_sha256"],
                "mode": registry_state_after["after_mode"],
                "gid": registry_state_after["after_gid"],
            }
            if _target_state(registry_entry, registry_relative) != expected_registry_after:
                raise ProcedureRemapError("Central registry changed while staging.")
            journal["state"] = "AWAITING_RESTART"
            _write_remap_journal(operation_directory, journal)
            return {
                "operation_id": operation_id,
                "state": journal["state"],
                "changed_artifacts": [item["path"] for item in targets],
                "registry_path": str(registry_entry.path / registry_relative),
                "restart_required": True,
            }
        except (OSError, ProcedureTransactionError, ProcedureRemapError) as exc:
            journal["state"] = "RECOVERY_REQUIRED"
            journal["failure"] = str(exc)[:1000]
            _write_remap_journal(operation_directory, journal)
            raise ProcedureRemapError(
                f"Remap staging failed; operation {operation_id} retains exact recovery images: {exc}"
            ) from exc


def _find_operation(entry: RepositoryEntry, operation_id: str, state_root: str | Path) -> tuple[Path, dict[str, Any]]:
    directory = _operation_path(state_root, entry.repository_id, operation_id)
    journal = _read_remap_journal(directory, entry)
    return directory, journal


def _journal_registry_entry(journal: dict[str, Any]) -> RepositoryEntry:
    try:
        registry_entry = resolve_repository_entry(journal["registry_repository_path"])
    except CoderInterfaceError as exc:
        raise ProcedureRemapError(f"Recorded registry owner is no longer centrally managed: {exc}") from exc
    if (
        registry_entry.key != journal["registry_repository_key"]
        or registry_entry.repository_id != journal["registry_repository_id"]
        or str(registry_entry.path) != journal["registry_repository_path"]
    ):
        raise ProcedureRemapError("Recorded registry owner identity changed.")
    return registry_entry


def inspect_role_remap(
    entry: RepositoryEntry,
    state_root: str | Path = DEFAULT_STATE_ROOT,
) -> dict[str, Any] | None:
    try:
        store = _remap_store(state_root, entry.repository_id, create=False)
        if not store.exists():
            return None
        records: list[tuple[str, Path, dict[str, Any]]] = []
        for child in store.iterdir():
            if not re.fullmatch(r"[0-9a-f]{32}", child.name):
                raise ProcedureRemapError("Remap transaction store contains an unknown operation directory.")
            _checked_directory(child, create=False)
            if not any(child.iterdir()):
                continue
            journal = _read_remap_journal(child, entry)
            records.append((journal.get("updated_at", ""), child, journal))
        if not records:
            return None
        _, operation_directory, journal = max(records, key=lambda item: item[0])
        conflicts: list[str] = []
        registry_path = Path(journal["registry_repository_path"]) / journal["registry_target"]["relative"]
        try:
            registry_entry = _journal_registry_entry(journal)
            registry_state = _target_state(registry_entry, journal["registry_target"]["relative"])
            registry_target = journal["registry_target"]
            if registry_state not in (
                {
                    "present": True,
                    "sha256": registry_target["before_sha256"],
                    "mode": registry_target["before_mode"],
                    "gid": registry_target["before_gid"],
                },
                {
                    "present": True,
                    "sha256": registry_target["after_sha256"],
                    "mode": registry_target["after_mode"],
                    "gid": registry_target["after_gid"],
                },
            ):
                conflicts.append("Central registry matches neither recorded remap image.")
        except (
            CoderInterfaceError,
            OSError,
            ProcedureRemapError,
            ProcedureTransactionError,
            RepositoryRegistryError,
        ) as exc:
            conflicts.append(f"Central registry cannot be safely inspected: {exc}")
        for target in journal["targets"]:
            try:
                state = _target_state(entry, target["path"])
            except ProcedureTransactionError as exc:
                conflicts.append(f"{target['path']}: {exc}")
                continue
            if state not in (
                {
                    "present": True,
                    "sha256": target["before_sha256"],
                    "mode": target["before_mode"],
                    "gid": target["before_gid"],
                },
                {
                    "present": True,
                    "sha256": target["after_sha256"],
                    "mode": target["after_mode"],
                    "gid": target["after_gid"],
                },
            ):
                conflicts.append(f"{target['path']} matches neither recorded remap image.")
        for relative, expected_hash in journal.get("document_hashes", {}).items():
            try:
                if _sha256(_read_repository_file(entry, relative)) != expected_hash:
                    conflicts.append(f"Mapped governing document changed after remap review: {relative}.")
            except ProcedureRemapError as exc:
                conflicts.append(f"Mapped governing document cannot be verified: {relative}: {exc}")
        return {
            "operation_id": journal["operation_id"],
            "state": journal["state"],
            "repository_key": journal["repository_key"],
            "old_documents": journal["old_documents"],
            "new_documents": journal["new_documents"],
            "registry_path": str(registry_path),
            "release": journal["bundle"]["release"],
            "plan_sha256": journal["plan_sha256"],
            "targets": [item["path"] for item in journal["targets"]],
            "conflicts": conflicts,
            "blocked": bool(conflicts),
            "operation_directory": str(operation_directory),
        }
    except (
        CoderInterfaceError,
        OSError,
        ProcedureRemapError,
        ProcedureTransactionError,
        RepositoryRegistryError,
        KeyError,
        TypeError,
    ) as exc:
        return {"state": "CORRUPT", "blocked": True, "error": str(exc), "operation_id": None}


def _load_images(operation_directory: Path, journal: dict[str, Any]) -> tuple[bytes, bytes, dict[str, bytes], dict[str, bytes]]:
    registry_before = _read_regular(operation_directory / "before-registry.bin", _REGISTRY_MAX_BYTES)
    registry_after = _read_regular(operation_directory / "after-registry.bin", _REGISTRY_MAX_BYTES)
    artifact_before: dict[str, bytes] = {}
    artifact_after: dict[str, bytes] = {}
    for item in journal["targets"]:
        artifact_before[item["path"]] = _read_regular(operation_directory / item["before_file"], MAX_TARGET_BYTES)
        artifact_after[item["path"]] = _read_regular(operation_directory / item["after_file"], MAX_TARGET_BYTES)
        if (
            _sha256(artifact_before[item["path"]]) != item["before_sha256"]
            or _sha256(artifact_after[item["path"]]) != item["after_sha256"]
        ):
            raise ProcedureRemapError(f"Remap recovery image is corrupt for {item['path']}.")
    if (
        _sha256(registry_before) != journal["registry_target"]["before_sha256"]
        or _sha256(registry_after) != journal["registry_target"]["after_sha256"]
    ):
        raise ProcedureRemapError("Central registry recovery image is corrupt.")
    try:
        before_registry = tomllib.loads(registry_before.decode("utf-8"))
        after_registry = tomllib.loads(registry_after.decode("utf-8"))
        before_documents = before_registry["repositories"][journal["repository_key"]]["documents"]
        after_documents = after_registry["repositories"][journal["repository_key"]]["documents"]
    except (UnicodeDecodeError, tomllib.TOMLDecodeError, KeyError, TypeError) as exc:
        raise ProcedureRemapError(f"Central registry recovery image is invalid: {exc}") from exc
    if before_documents != journal["old_documents"] or after_documents != journal["new_documents"]:
        raise ProcedureRemapError("Central registry images do not contain the reviewed role maps.")
    before_registry["repositories"][journal["repository_key"]]["documents"] = {}
    after_registry["repositories"][journal["repository_key"]]["documents"] = {}
    if before_registry != after_registry:
        raise ProcedureRemapError("Central registry images differ outside the reviewed role map.")
    return registry_before, registry_after, artifact_before, artifact_after


def recover_role_remap(
    entry: RepositoryEntry,
    operation_id: str,
    state_root: str | Path = DEFAULT_STATE_ROOT,
) -> dict[str, Any]:
    with state_artifact_lock(state_root):
        operation_directory, journal = _find_operation(entry, operation_id, state_root)
        if journal["state"] in _TERMINAL_STATES:
            raise ProcedureRemapError("A completed remap operation cannot be rolled back by this recovery path.")
        if journal["state"] == "PREPARING":
            registry_entry = _journal_registry_entry(journal)
            registry_target = journal["registry_target"]
            expected_before = {
                "present": True,
                "sha256": registry_target["before_sha256"],
                "mode": registry_target["before_mode"],
                "gid": registry_target["before_gid"],
            }
            if _target_state(registry_entry, registry_target["relative"]) != expected_before:
                raise ProcedureRemapError("Incomplete preparation is blocked because the central registry changed.")
            for item in journal["targets"]:
                if _target_state(entry, item["path"]) != {
                    "present": True,
                    "sha256": item["before_sha256"],
                    "mode": item["before_mode"],
                    "gid": item["before_gid"],
                }:
                    raise ProcedureRemapError(
                        f"Incomplete preparation is blocked because {item['path']} changed."
                    )
            journal["state"] = "ABORTED"
            _write_remap_journal(operation_directory, journal)
            return {"operation_id": operation_id, "state": "ABORTED", "restart_required": False}
        registry_before, registry_after, artifact_before, artifact_after = _load_images(operation_directory, journal)
        registry_entry = _journal_registry_entry(journal)
        registry_relative = journal["registry_target"]["relative"]
        expected_states: list[tuple[RepositoryEntry, str, set[str]]] = [
            (
                entry,
                item["path"],
                {item["before_sha256"], item["after_sha256"]},
            )
            for item in journal["targets"]
        ]
        expected_states.append(
            (
                registry_entry,
                registry_relative,
                {
                    journal["registry_target"]["before_sha256"],
                    journal["registry_target"]["after_sha256"],
                },
            )
        )
        for repository, relative, allowed_hashes in expected_states:
            current = _target_state(repository, relative)
            if not current["present"] or current["sha256"] not in allowed_hashes:
                raise ProcedureRemapError(
                    f"Recovery is blocked because {relative} matches neither recorded transaction image."
                )
            target = (
                journal["registry_target"]
                if relative == registry_relative
                else next(item for item in journal["targets"] if item["path"] == relative)
            )
            allowed_states = (
                {
                    "present": True,
                    "sha256": target["before_sha256"],
                    "mode": target["before_mode"],
                    "gid": target["before_gid"],
                },
                {
                    "present": True,
                    "sha256": target["after_sha256"],
                    "mode": target["after_mode"],
                    "gid": target["after_gid"],
                },
            )
            if current not in allowed_states:
                raise ProcedureRemapError(f"Recovery is blocked because {relative} metadata changed.")
        journal["state"] = "ROLLING_BACK"
        _write_remap_journal(operation_directory, journal)
        directories = _directory_states(entry, tuple(artifact_before))
        for item in journal["targets"]:
            relative = item["path"]
            current = _target_state(entry, relative)
            expected_before = {
                "present": True,
                "sha256": item["before_sha256"],
                "mode": item["before_mode"],
                "gid": item["before_gid"],
            }
            expected_after = {
                "present": True,
                "sha256": item["after_sha256"],
                "mode": item["after_mode"],
                "gid": item["after_gid"],
            }
            if current == expected_before:
                continue
            if current != expected_after:
                raise ProcedureRemapError(
                    f"Recovery is blocked because {relative} changed during restoration."
                )
            _write_target(
                entry,
                relative,
                artifact_before[relative],
                current,
                item["before_mode"],
                item["before_gid"],
                f".repoctl-remap-{operation_id}-restore-{item['index']}.tmp",
                directory_states=directories,
            )
            _write_remap_journal(operation_directory, journal)
        registry_state = journal["registry_target"]
        current_registry = _target_state(registry_entry, registry_relative)
        expected_registry_before = {
            "present": True,
            "sha256": registry_state["before_sha256"],
            "mode": registry_state["before_mode"],
            "gid": registry_state["before_gid"],
        }
        expected_registry_after = {
            "present": True,
            "sha256": registry_state["after_sha256"],
            "mode": registry_state["after_mode"],
            "gid": registry_state["after_gid"],
        }
        if current_registry == expected_registry_after:
            registry_directories = _directory_states(registry_entry, (registry_relative,))
            _write_target(
                registry_entry,
                registry_relative,
                registry_before,
                current_registry,
                registry_state["before_mode"],
                registry_state["before_gid"],
                f".repoctl-remap-{operation_id}-restore-registry.tmp",
                directory_states=registry_directories,
            )
        elif current_registry != expected_registry_before:
            raise ProcedureRemapError("Recovery is blocked because the central registry changed during restoration.")
        if any(
            _target_state(entry, item["path"])
            != {
                "present": True,
                "sha256": item["before_sha256"],
                "mode": item["before_mode"],
                "gid": item["before_gid"],
            }
            for item in journal["targets"]
        ):
            raise ProcedureRemapError("Generated artifacts changed during remap recovery.")
        if _target_state(registry_entry, registry_relative) != expected_registry_before:
            raise ProcedureRemapError("Central registry changed during remap recovery.")
        journal["state"] = "ROLLBACK_AWAITING_RESTART"
        _write_remap_journal(operation_directory, journal)
        return {
            "operation_id": operation_id,
            "state": journal["state"],
            "restart_required": True,
            "restored_artifacts": list(artifact_before),
            "registry_path": str(registry_entry.path / registry_relative),
        }


def finalize_role_remap(
    entry: RepositoryEntry,
    operation_id: str,
    state_root: str | Path = DEFAULT_STATE_ROOT,
    *,
    active_key: str,
    registry_generation: int,
    runtime_instance_id: str,
    bundle: ProcedureBundle | None = None,
) -> dict[str, Any]:
    active_bundle = bundle or load_bundle()
    with state_artifact_lock(state_root):
        operation_directory, journal = _find_operation(entry, operation_id, state_root)
        if journal["state"] not in {"AWAITING_RESTART", "ROLLBACK_AWAITING_RESTART"}:
            raise ProcedureRemapError("The remap operation is not awaiting post-restart verification.")
        if (
            active_key != entry.key
            or type(registry_generation) is not int
            or registry_generation < 0
            or not runtime_instance_id
            or runtime_instance_id == journal["runtime"]["runtime_instance_id"]
        ):
            raise ProcedureRemapError("Select the target repository in the restarted service before verification.")
        rollback = journal["state"] == "ROLLBACK_AWAITING_RESTART"
        expected_documents = journal["old_documents"] if rollback else journal["new_documents"]
        if entry.documents != expected_documents:
            raise ProcedureRemapError("Post-restart registry does not match the recorded remap checkpoint.")
        if _milestone_snapshot(entry, state_root) != journal["milestone"]:
            raise ProcedureRemapError("Selected milestone or lifecycle changed during the remap restart.")
        for relative, expected_hash in journal["document_hashes"].items():
            if _sha256(_read_repository_file(entry, relative)) != expected_hash:
                raise ProcedureRemapError(f"Mapped governing document changed during remap: {relative}.")
        status = inspect_procedure(entry, active_bundle)
        if status["status"] != "CURRENT":
            raise ProcedureRemapError(
                f"Post-restart procedure verification is {status['status']}, not CURRENT."
            )
        current_map = {
            "active_key": active_key,
            "registry_generation": registry_generation,
            "runtime_instance_id": runtime_instance_id,
        }
        journal["post_restart_runtime"] = current_map
        journal["state"] = "ROLLED_BACK" if rollback else "ACTIVATED"
        _write_remap_journal(operation_directory, journal)
        return {
            "operation_id": operation_id,
            "state": journal["state"],
            "procedure_status": status["status"],
            "release": status["current_release"],
            "recovery_images_retained": True,
        }


def read_runtime_binding(runtime_url: str | None = None) -> dict[str, Any]:
    configured_url = runtime_url or os.environ.get("REPO_CONTROL_RUNTIME_URL", "http://127.0.0.1:8765")
    try:
        parsed = urlsplit(configured_url)
        port = parsed.port or 80
    except ValueError as exc:
        raise ProcedureRemapError("Runtime URL is invalid.") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ProcedureRemapError("Runtime URL must be an unauthenticated loopback HTTP origin.")
    connection = http.client.HTTPConnection(parsed.hostname, port, timeout=1.5)
    try:
        connection.request("GET", "/healthz", headers={"Accept": "application/json"})
        response = connection.getresponse()
        payload = response.read(8_193)
        if response.status != 200 or len(payload) > 8_192:
            raise ProcedureRemapError("Loopback service health response is unavailable or oversized.")
    except (OSError, http.client.HTTPException, TimeoutError) as exc:
        raise ProcedureRemapError(f"Cannot verify the live Repo Control service: {exc}") from exc
    finally:
        connection.close()
    try:
        result = json.loads(payload.decode("utf-8"))
        active_key = result["active_repository"]
        generation = result["active_repository_generation"]
        instance_id = result["runtime_capabilities"]["instance_id"]
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ProcedureRemapError("Loopback service health response lacks remap selection preconditions.") from exc
    if (
        result.get("service") != "repo-control"
        or not isinstance(active_key, str)
        or type(generation) is not int
        or generation < 0
        or not isinstance(instance_id, str)
        or not re.fullmatch(r"[a-f0-9]{32}", instance_id)
    ):
        raise ProcedureRemapError("Loopback service health response has invalid selection identity.")
    if result.get("central_registry_stale") is not False:
        raise ProcedureRemapError("Restart the service after any registry change before remapping.")
    return {
        "active_key": active_key,
        "registry_generation": generation,
        "runtime_instance_id": instance_id,
    }
