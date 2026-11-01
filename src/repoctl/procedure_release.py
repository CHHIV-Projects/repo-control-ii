from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import tomllib
from pathlib import Path, PurePosixPath
from typing import Any

from .procedure_governance import (
    BUNDLE_ROOT,
    ProcedureGovernanceError,
    _bundle_file,
    _relative_path,
    load_bundle,
    preview_reconciliation,
)
from .workflow.authority import get_authorization_status, is_correction_mode


_MARKER_RE = re.compile(
    r"<!--\s*repoctl-procedure-release-preparation:v1\s*\n(.*?)\n\s*-->",
    re.DOTALL,
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_VERSION_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
_MAX_PROMPT_BYTES = 100_000
_MAX_MANIFEST_BYTES = 100_000
_MAX_SOURCE_BYTES = 1_000_000
_MAX_BUNDLE_BYTES = 5_000_000
_MAX_SOURCE_COUNT = 32


class ProcedureReleaseError(ValueError):
    pass


def render_release_preparation_marker(
    candidate_release: str,
    authorized_sources: list[str],
    bundle_root: str | Path = BUNDLE_ROOT,
) -> str:
    bundle = load_bundle(bundle_root)
    candidate_version = _version_tuple(candidate_release)
    base_version = _version_tuple(bundle.release)
    if candidate_version is None or base_version is None or candidate_version <= base_version:
        raise ProcedureReleaseError(
            "Candidate release must be a higher numeric major.minor.patch version."
        )
    if (
        not isinstance(authorized_sources, list)
        or not authorized_sources
        or len(authorized_sources) > _MAX_SOURCE_COUNT
        or any(not isinstance(path, str) for path in authorized_sources)
        or len(set(authorized_sources)) != len(authorized_sources)
    ):
        raise ProcedureReleaseError("Provide a bounded, unique list of authorized canonical source paths.")
    normalized_sources = sorted(
        _relative_path(path, "authorized source path")
        for path in authorized_sources
    )
    if not set(normalized_sources).issubset(bundle.source_hashes):
        raise ProcedureReleaseError("Every authorized source must be listed in the current canonical bundle.")
    marker = {
        "schema_version": 1,
        "base_manifest_sha256": bundle.manifest_sha256,
        "candidate_release": candidate_release,
        "authorized_sources": normalized_sources,
    }
    return (
        "<!-- repoctl-procedure-release-preparation:v1\n"
        + json.dumps(marker, ensure_ascii=False, indent=2)
        + "\n-->"
    )


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _version_tuple(value: Any) -> tuple[int, int, int] | None:
    if not isinstance(value, str):
        return None
    match = _VERSION_RE.fullmatch(value)
    return tuple(int(part) for part in match.groups()) if match else None


def _manifest(root: Path) -> tuple[bytes, dict[str, Any]]:
    path = root / "manifest.toml"
    if path.is_symlink() or not path.is_file():
        raise ProcedureReleaseError("Canonical procedure manifest is missing or unsafe.")
    try:
        content = path.read_bytes()
        if len(content) > _MAX_MANIFEST_BYTES:
            raise ProcedureReleaseError("Canonical procedure manifest exceeds the size limit.")
        raw = tomllib.loads(content.decode("utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ProcedureReleaseError(f"Cannot read canonical procedure manifest: {exc}") from exc
    return content, raw


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=True)


def _render_manifest(raw: dict[str, Any], release: str, hashes: dict[str, str]) -> bytes:
    expected_top_level = {
        "schema_version",
        "release",
        "overlay_schema_version",
        "overlay_marker",
        "lock_path",
        "host_entrypoint",
        "sources",
        "artifacts",
    }
    if set(raw) != expected_top_level:
        raise ProcedureReleaseError("Canonical manifest contains unsupported fields.")
    sources = raw["sources"]
    artifacts = raw["artifacts"]
    if (
        type(raw["schema_version"]) is not int
        or raw["schema_version"] != 1
        or type(raw["overlay_schema_version"]) is not int
        or raw["overlay_schema_version"] < 1
        or not isinstance(raw["overlay_marker"], str)
        or not raw["overlay_marker"].strip()
        or not isinstance(raw["lock_path"], str)
        or not isinstance(raw["host_entrypoint"], str)
        or not isinstance(sources, list)
        or not isinstance(artifacts, list)
        or not 1 <= len(sources) <= _MAX_SOURCE_COUNT
    ):
        raise ProcedureReleaseError("Canonical manifest source or artifact tables are invalid.")

    lines = [
        f"schema_version = {raw['schema_version']}",
        f"release = {_toml_string(release)}",
        f"overlay_schema_version = {raw['overlay_schema_version']}",
        f"overlay_marker = {_toml_string(raw['overlay_marker'])}",
        f"lock_path = {_toml_string(raw['lock_path'])}",
        f"host_entrypoint = {_toml_string(raw['host_entrypoint'])}",
    ]
    for source in sources:
        if not isinstance(source, dict) or set(source) != {"path", "sha256"}:
            raise ProcedureReleaseError("Canonical source table contains unsupported fields.")
        path = _relative_path(source["path"], "source path")
        if path not in hashes or not _SHA256_RE.fullmatch(hashes[path]):
            raise ProcedureReleaseError(f"Candidate source hash is unavailable: {path}")
        lines.extend(
            [
                "",
                "[[sources]]",
                f"path = {_toml_string(path)}",
                f"sha256 = {_toml_string(hashes[path])}",
            ]
        )
    for artifact in artifacts:
        if not isinstance(artifact, dict) or set(artifact) != {"source", "target", "renderer"}:
            raise ProcedureReleaseError("Canonical artifact table contains unsupported fields.")
        lines.extend(
            [
                "",
                "[[artifacts]]",
                f"source = {_toml_string(artifact['source'])}",
                f"target = {_toml_string(artifact['target'])}",
                f"renderer = {_toml_string(artifact['renderer'])}",
            ]
        )
    return ("\n".join(lines) + "\n").encode("utf-8")


def _prompt_marker(
    repository_root: Path,
    candidate: dict[str, Any],
) -> dict[str, Any] | None:
    prompt_relative = _relative_path(candidate.get("prompt_path"), "milestone prompt path")
    prompt_path = repository_root.joinpath(*PurePosixPath(prompt_relative).parts)
    if prompt_path.is_symlink() or not prompt_path.resolve(strict=True).is_relative_to(repository_root):
        raise ProcedureReleaseError("Active milestone prompt is missing or unsafe.")
    content = prompt_path.read_bytes()
    if len(content) > _MAX_PROMPT_BYTES:
        raise ProcedureReleaseError("Active milestone prompt exceeds the release-authorization size limit.")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProcedureReleaseError("Active milestone prompt is not valid UTF-8.") from exc
    matches = list(_MARKER_RE.finditer(text))
    if len(matches) > 1:
        raise ProcedureReleaseError("Active milestone prompt contains multiple release-preparation markers.")
    if not matches:
        return None
    try:
        marker = json.loads(matches[0].group(1))
    except json.JSONDecodeError as exc:
        raise ProcedureReleaseError("Release-preparation marker is malformed JSON.") from exc
    required = {
        "schema_version",
        "base_manifest_sha256",
        "candidate_release",
        "authorized_sources",
    }
    if not isinstance(marker, dict) or set(marker) != required:
        raise ProcedureReleaseError("Release-preparation marker has an unsupported shape.")
    if (
        type(marker["schema_version"]) is not int
        or marker["schema_version"] != 1
        or not isinstance(marker["base_manifest_sha256"], str)
        or not _SHA256_RE.fullmatch(marker["base_manifest_sha256"])
        or _version_tuple(marker["candidate_release"]) is None
        or not isinstance(marker["authorized_sources"], list)
        or not marker["authorized_sources"]
        or len(marker["authorized_sources"]) > _MAX_SOURCE_COUNT
        or any(not isinstance(path, str) for path in marker["authorized_sources"])
        or len(set(marker["authorized_sources"])) != len(marker["authorized_sources"])
    ):
        raise ProcedureReleaseError("Release-preparation marker values are invalid.")
    marker["authorized_sources"] = [
        _relative_path(path, "authorized source path")
        for path in marker["authorized_sources"]
    ]
    return marker


def _validated_candidate(
    root: Path,
    manifest_raw: dict[str, Any],
    candidate_release: str,
) -> tuple[bytes, dict[str, bytes]]:
    sources = manifest_raw.get("sources")
    if not isinstance(sources, list) or not 1 <= len(sources) <= _MAX_SOURCE_COUNT:
        raise ProcedureReleaseError("Canonical manifest source table is invalid.")
    source_bytes: dict[str, bytes] = {}
    hashes: dict[str, str] = {}
    total = 0
    for source in sources:
        if not isinstance(source, dict) or set(source) != {"path", "sha256"}:
            raise ProcedureReleaseError("Canonical source table contains unsupported fields.")
        relative = _relative_path(source["path"], "source path")
        if not isinstance(source["sha256"], str) or not _SHA256_RE.fullmatch(source["sha256"]):
            raise ProcedureReleaseError(f"Canonical source hash is malformed: {relative}")
        if relative in source_bytes:
            raise ProcedureReleaseError(f"Canonical source is listed more than once: {relative}")
        path = _bundle_file(root, relative)
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise ProcedureReleaseError(f"Cannot inspect candidate canonical source {relative}: {exc}") from exc
        if size > _MAX_SOURCE_BYTES or total + size > _MAX_BUNDLE_BYTES:
            raise ProcedureReleaseError("Candidate canonical bundle exceeds the source size limit.")
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise ProcedureReleaseError(f"Cannot read candidate canonical source {relative}: {exc}") from exc
        total += len(content)
        if len(content) != size:
            raise ProcedureReleaseError(f"Candidate source changed during validation: {relative}")
        source_bytes[relative] = content
        hashes[relative] = _digest(content)
    rendered = _render_manifest(manifest_raw, candidate_release, hashes)
    with tempfile.TemporaryDirectory(prefix="repoctl-procedure-candidate-") as directory:
        temp_root = Path(directory)
        (temp_root / "manifest.toml").write_bytes(rendered)
        for relative, content in source_bytes.items():
            path = temp_root.joinpath(*PurePosixPath(relative).parts)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        bundle = load_bundle(temp_root)
        if bundle.release != candidate_release:
            raise ProcedureReleaseError("Candidate bundle release identity did not validate.")
    return rendered, source_bytes


def inspect_procedure_release_preparation(
    repository_root: str | Path,
    candidate: dict[str, Any],
    workflow: dict[str, Any],
    bundle_root: str | Path = BUNDLE_ROOT,
) -> dict[str, Any]:
    root = Path(bundle_root).resolve()
    repo_root = Path(repository_root).resolve(strict=True)
    try:
        manifest_bytes, manifest_raw = _manifest(root)
    except (OSError, ProcedureReleaseError) as exc:
        return {
            "status": "INTEGRITY_UNAVAILABLE",
            "authorized": False,
            "reason": str(exc),
        }
    try:
        marker = _prompt_marker(repo_root, candidate)
    except (OSError, ProcedureReleaseError) as marker_error:
        try:
            load_bundle(root)
        except (OSError, ProcedureGovernanceError) as integrity_error:
            return {
                "status": "INTEGRITY_UNAVAILABLE",
                "authorized": False,
                "integrity_error": str(integrity_error),
                "reason": f"Release marker is invalid and canonical integrity is unavailable: {marker_error}",
            }
        return {
            "status": "NOT_AUTHORIZED",
            "authorized": False,
            "integrity_status": "CURRENT",
            "reason": f"Release-preparation marker is invalid: {marker_error}",
        }
    if marker is None:
        try:
            load_bundle(root)
        except (OSError, ProcedureGovernanceError) as exc:
            return {
                "status": "INTEGRITY_UNAVAILABLE",
                "authorized": False,
                "reason": f"Canonical bundle integrity is unavailable: {exc}",
            }
        return {
            "status": "NOT_REQUESTED",
            "authorized": False,
            "integrity_status": "CURRENT",
            "reason": "The active milestone does not contain an explicit release-preparation authorization.",
        }

    if repo_root != root.parent.parent.resolve():
        return {
            "status": "NOT_AUTHORIZED",
            "authorized": False,
            "reason": "Canonical release preparation is limited to the repository that owns this shared bundle.",
        }

    manifest_digest = _digest(manifest_bytes)
    if marker["base_manifest_sha256"] != manifest_digest:
        try:
            published_bundle = load_bundle(root)
        except (OSError, ProcedureGovernanceError):
            published_bundle = None
        if published_bundle and published_bundle.release == marker["candidate_release"]:
            return {
                "status": "RELEASE_PUBLISHED",
                "authorized": False,
                "integrity_status": "CURRENT",
                "candidate_release": published_bundle.release,
                "manifest_sha256": published_bundle.manifest_sha256,
                "reason": "The authorized candidate release is published and the canonical bundle validates.",
            }
        return {
            "status": "INTEGRITY_UNAVAILABLE",
            "authorized": False,
            "reason": "The canonical manifest changed from the base approved by the active milestone.",
            "base_manifest_sha256": marker["base_manifest_sha256"],
            "observed_manifest_sha256": manifest_digest,
        }

    authorization = get_authorization_status(
        workflow.get("phase", ""),
        is_correction_mode(workflow),
    )
    if not authorization["authorized"]:
        try:
            load_bundle(root)
        except (OSError, ProcedureGovernanceError) as exc:
            integrity_status = "UNAVAILABLE"
            integrity_error = str(exc)
        else:
            integrity_status = "CURRENT"
            integrity_error = None
        return {
            "status": "NOT_AUTHORIZED",
            "authorized": False,
            "reason": "Release preparation requires the active milestone to be authorized IN_PROGRESS.",
            "integrity_status": integrity_status,
            "integrity_error": integrity_error,
        }
    if not isinstance(manifest_raw.get("sources"), list):
        return {
            "status": "INTEGRITY_UNAVAILABLE",
            "authorized": False,
            "reason": "Canonical manifest source table is malformed.",
        }
    current_release = manifest_raw.get("release")
    current_tuple = _version_tuple(current_release)
    candidate_release = marker["candidate_release"]
    if current_tuple is None or _version_tuple(candidate_release) <= current_tuple:
        return {
            "status": "INTEGRITY_UNAVAILABLE",
            "authorized": False,
            "reason": "Candidate release must be a higher numeric major.minor.patch version.",
        }

    manifest_sources = {
        source.get("path")
        for source in manifest_raw["sources"]
        if isinstance(source, dict)
    }
    authorized_sources = set(marker["authorized_sources"])
    if not authorized_sources.issubset(manifest_sources):
        return {
            "status": "INTEGRITY_UNAVAILABLE",
            "authorized": False,
            "reason": "Release-preparation authorization names a source absent from the base manifest.",
        }

    mismatches: list[str] = []
    total_source_bytes = 0
    try:
        for source in manifest_raw["sources"]:
            if not isinstance(source, dict):
                raise ProcedureReleaseError("Canonical source table is malformed.")
            relative = _relative_path(source.get("path"), "source path")
            expected = source.get("sha256")
            if not isinstance(expected, str) or not _SHA256_RE.fullmatch(expected):
                raise ProcedureReleaseError(f"Canonical source hash is malformed: {relative}")
            source_path = _bundle_file(root, relative)
            try:
                source_size = source_path.stat().st_size
            except FileNotFoundError:
                if relative in authorized_sources:
                    mismatches.append(relative)
                    continue
                raise
            total_source_bytes += source_size
            if source_size > _MAX_SOURCE_BYTES or total_source_bytes > _MAX_BUNDLE_BYTES:
                raise ProcedureReleaseError(f"Candidate source exceeds the size limit: {relative}")
            content = source_path.read_bytes()
            if len(content) != source_size:
                raise ProcedureReleaseError(f"Canonical source changed during inspection: {relative}")
            actual = _digest(content)
            if actual != expected:
                mismatches.append(relative)
    except (OSError, ProcedureReleaseError) as exc:
        return {
            "status": "INTEGRITY_UNAVAILABLE",
            "authorized": False,
            "reason": str(exc),
        }
    unauthorized = sorted(set(mismatches) - authorized_sources)
    if unauthorized:
        return {
            "status": "INTEGRITY_UNAVAILABLE",
            "authorized": False,
            "reason": "Canonical sources changed outside the active milestone allowlist: "
            + ", ".join(unauthorized),
            "changed_sources": sorted(mismatches),
        }
    try:
        _validated_candidate(root, manifest_raw, candidate_release)
    except (OSError, ProcedureGovernanceError, ProcedureReleaseError) as exc:
        if mismatches:
            return {
                "status": "RELEASE_IN_PREPARATION",
                "authorized": True,
                "milestone_id": candidate["milestone_id"],
                "candidate_release": candidate_release,
                "base_manifest_sha256": manifest_digest,
                "authorized_sources": sorted(authorized_sources),
                "changed_sources": sorted(mismatches),
                "candidate_validation_status": "INVALID",
                "candidate_validation_error": str(exc)[:1200],
            }
        return {
            "status": "INTEGRITY_UNAVAILABLE",
            "authorized": False,
            "reason": f"Candidate bundle validation failed: {exc}",
            "changed_sources": sorted(mismatches),
        }
    return {
        "status": "RELEASE_IN_PREPARATION" if mismatches else "CANDIDATE_READY",
        "authorized": True,
        "milestone_id": candidate["milestone_id"],
        "candidate_release": candidate_release,
        "base_manifest_sha256": manifest_digest,
        "authorized_sources": sorted(authorized_sources),
        "changed_sources": sorted(mismatches),
        "candidate_validation_status": "BUNDLE_VALID_PREVIEW_REQUIRED",
        "candidate_manifest_sha256": _digest(
            _validated_candidate(root, manifest_raw, candidate_release)[0]
        ),
    }


def preview_authorized_procedure_release(
    entry: Any,
    candidate: dict[str, Any],
    workflow: dict[str, Any],
    bundle_root: str | Path = BUNDLE_ROOT,
) -> dict[str, Any]:
    root = Path(bundle_root).resolve()
    preparation = inspect_procedure_release_preparation(
        entry.path,
        candidate,
        workflow,
        root,
    )
    if not preparation.get("authorized"):
        raise ProcedureReleaseError(
            f"Candidate release preview is not authorized: {preparation.get('reason')}"
        )
    _, manifest_raw = _manifest(root)
    candidate_manifest, source_bytes = _validated_candidate(
        root,
        manifest_raw,
        preparation["candidate_release"],
    )
    if _digest(candidate_manifest) != preparation["candidate_manifest_sha256"]:
        raise ProcedureReleaseError("Candidate sources changed during preview; nothing was published.")
    with tempfile.TemporaryDirectory(prefix="repoctl-procedure-preview-") as directory:
        temp_root = Path(directory)
        (temp_root / "manifest.toml").write_bytes(candidate_manifest)
        for relative, content in source_bytes.items():
            path = temp_root.joinpath(*PurePosixPath(relative).parts)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        bundle = load_bundle(temp_root)
        plan = preview_reconciliation(entry, bundle)
        preview = plan.as_dict(include_diffs=True)
    return {
        "release_preparation": preparation,
        "preview": preview,
        "preview_read_only": True,
        "materialization_available": preview["materialization_available"],
    }


def publish_authorized_procedure_release(
    entry: Any,
    candidate: dict[str, Any],
    workflow: dict[str, Any],
    bundle_root: str | Path = BUNDLE_ROOT,
) -> dict[str, Any]:
    preview = preview_authorized_procedure_release(entry, candidate, workflow, bundle_root)
    root = Path(bundle_root).resolve()
    manifest_path = root / "manifest.toml"
    original, manifest_raw = _manifest(root)
    if _digest(original) != preview["release_preparation"]["base_manifest_sha256"]:
        raise ProcedureReleaseError("Canonical manifest changed after candidate preview; nothing was published.")
    candidate_manifest, _ = _validated_candidate(
        root,
        manifest_raw,
        preview["release_preparation"]["candidate_release"],
    )
    if _digest(candidate_manifest) != preview["release_preparation"]["candidate_manifest_sha256"]:
        raise ProcedureReleaseError("Candidate sources changed after preview; nothing was published.")
    descriptor, temporary_name = tempfile.mkstemp(prefix=".manifest.", suffix=".tmp", dir=root)
    temporary_path = Path(temporary_name)
    try:
        original_mode = manifest_path.stat().st_mode & 0o777
        os.chmod(temporary_path, original_mode)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(candidate_manifest)
            stream.flush()
            os.fsync(stream.fileno())
        current, _ = _manifest(root)
        if _digest(current) != _digest(original):
            raise ProcedureReleaseError("Canonical manifest changed before publish; nothing was published.")
        os.replace(temporary_path, manifest_path)
        directory_descriptor = os.open(root, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()
    published = load_bundle(root)
    return {
        "status": "PUBLISHED",
        "release": published.release,
        "manifest_sha256": published.manifest_sha256,
        "preview": preview["preview"],
    }
