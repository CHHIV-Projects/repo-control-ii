from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from tempfile import mkdtemp
from typing import Any

from ..scanner.core import DEFAULT_STATE_ROOT
from ..scanner.util import encode_path_order, write_json_deterministic

SCAN_ARTIFACTS = [
    "repository.json",
    "files.json",
    "symbols.json",
    "tests.json",
    "dependencies.json",
    "summary.md",
]
SNAPSHOT_REQUIRED_FILES = {"snapshot.json", *SCAN_ARTIFACTS}


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def derive_snapshot_id(artifact_bytes: dict[str, bytes]) -> str:
    parts = bytearray(b"repoctl-snapshot-v1\0")
    for name in SCAN_ARTIFACTS:
        parts.extend(name.encode("ascii"))
        parts.extend(b"\0")
        parts.extend(_sha256_bytes(artifact_bytes[name]).encode("ascii"))
        parts.extend(b"\0")
    return "snap--" + hashlib.sha256(bytes(parts)).hexdigest()[:16]


def _artifact_hashes(artifact_bytes: dict[str, bytes]) -> dict[str, str]:
    return {name: _sha256_bytes(data) for name, data in artifact_bytes.items()}


def _worktree_summary(repository_payload: dict[str, Any]) -> dict[str, Any]:
    untracked_paths = [
        entry["path"]
        for entry in repository_payload["working_tree"]["entries"]
        if entry["kind"] == "untracked"
    ]
    untracked_paths.sort(key=encode_path_order)
    has_untracked = bool(untracked_paths)
    return {
        "structural_scope": "tracked_files",
        "untracked_entries_present": has_untracked,
        "untracked_paths": untracked_paths,
        "worktree_completeness": "partial_worktree" if has_untracked else "complete_for_tracked_files",
    }


def build_snapshot_payload(scan_result: dict[str, Any], artifact_hashes: dict[str, str], snapshot_id: str) -> dict[str, Any]:
    repository_payload = scan_result["repository_payload"]
    coverage = _worktree_summary(repository_payload)
    return {
        "schema_version": 1,
        "snapshot_id": snapshot_id,
        "repository_id": repository_payload["repository_id"],
        "repository_root": repository_payload["repository_root"],
        "branch": repository_payload["branch"],
        "head_commit": repository_payload["head_commit"],
        "working_tree_clean": repository_payload["working_tree"]["is_clean"],
        "working_tree": repository_payload["working_tree"],
        "working_tree_categories": repository_payload["working_tree_categories"],
        "scan_artifacts": list(SCAN_ARTIFACTS),
        "artifact_hashes": artifact_hashes,
        "structural_coverage": coverage,
    }


def _load_artifact_bytes(scan_output_dir: Path) -> dict[str, bytes]:
    return {name: (scan_output_dir / name).read_bytes() for name in SCAN_ARTIFACTS}


def _verify_staged_snapshot(snapshot_dir: Path, expected_repository_id: str) -> dict[str, Any]:
    names = {child.name for child in snapshot_dir.iterdir()}
    if names != SNAPSHOT_REQUIRED_FILES:
        raise RuntimeError("snapshot artifact set is incomplete or contains unexpected files")

    snapshot_payload = json.loads((snapshot_dir / "snapshot.json").read_text(encoding="utf-8"))
    if snapshot_payload.get("schema_version") != 1:
        raise RuntimeError("unsupported snapshot schema version")
    if snapshot_payload.get("repository_id") != expected_repository_id:
        raise RuntimeError("snapshot repository id mismatch")

    artifact_bytes = {name: (snapshot_dir / name).read_bytes() for name in SCAN_ARTIFACTS}
    artifact_hashes = _artifact_hashes(artifact_bytes)
    if snapshot_payload.get("artifact_hashes") != artifact_hashes:
        raise RuntimeError("snapshot artifact hash mismatch")

    recomputed = derive_snapshot_id(artifact_bytes)
    if snapshot_payload.get("snapshot_id") != recomputed:
        raise RuntimeError("snapshot id mismatch")
    return snapshot_payload


def _verify_existing_snapshot(snapshot_dir: Path, expected_repository_id: str) -> dict[str, Any]:
    return _verify_staged_snapshot(snapshot_dir, expected_repository_id)


def create_snapshot(scan_result: dict[str, Any], state_root: Path | None = None) -> dict[str, Any]:
    root = (state_root or DEFAULT_STATE_ROOT).expanduser()
    repository_id = scan_result["repository_id"]
    repository_dir = root / repository_id
    snapshots_root = repository_dir / "snapshots"
    snapshots_root.mkdir(parents=True, exist_ok=True)

    scan_output_dir = Path(scan_result["output_dir"])
    artifact_bytes = _load_artifact_bytes(scan_output_dir)
    artifact_hashes = _artifact_hashes(artifact_bytes)
    snapshot_id = derive_snapshot_id(artifact_bytes)
    snapshot_payload = build_snapshot_payload(scan_result, artifact_hashes, snapshot_id)

    temp_dir = Path(mkdtemp(prefix="snapshot-tmp-", dir=str(snapshots_root)))
    try:
        for name, data in artifact_bytes.items():
            (temp_dir / name).write_bytes(data)
        write_json_deterministic(temp_dir / "snapshot.json", snapshot_payload)

        _verify_staged_snapshot(temp_dir, repository_id)

        final_dir = snapshots_root / snapshot_id
        if final_dir.exists():
            _verify_existing_snapshot(final_dir, repository_id)
            existing_bytes = {name: (final_dir / name).read_bytes() for name in SNAPSHOT_REQUIRED_FILES}
            staged_bytes = {name: (temp_dir / name).read_bytes() for name in SNAPSHOT_REQUIRED_FILES}
            if existing_bytes != staged_bytes:
                raise RuntimeError("existing snapshot content mismatch for identical snapshot id")
            shutil.rmtree(temp_dir)
            return {
                "snapshot_id": snapshot_id,
                "snapshot_dir": str(final_dir),
                "repository_id": repository_id,
                "repository_root": scan_result["repository_root"],
                "reused_existing": True,
            }

        temp_dir.rename(final_dir)
        return {
            "snapshot_id": snapshot_id,
            "snapshot_dir": str(final_dir),
            "repository_id": repository_id,
            "repository_root": scan_result["repository_root"],
            "reused_existing": False,
        }
    except Exception:
        if temp_dir.exists():
            shutil.rmtree(temp_dir)
        raise
