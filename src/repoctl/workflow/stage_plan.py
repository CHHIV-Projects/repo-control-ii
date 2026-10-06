from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path
from tempfile import mkdtemp
from typing import Any

from ..scanner.core import DEFAULT_STATE_ROOT
from ..scanner.util import make_repository_id, write_json_deterministic
from .errors import WorkflowReasonError
from .git_state import WorkflowGitStateError, inspect_git_state

ZERO_OID = "0" * 40
REGULAR_FILE_MODES = {"100644", "100755"}
STAGE_PLAN_REQUIRED_FILES = {"plan.json", "plan.md"}


def _run_git_bytes(repo_root: Path, args: list[str], *, error_code: str, error_prefix: str) -> bytes:
    proc = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace").strip()
        raise WorkflowReasonError(error_code, f"{error_prefix}: {stderr}")
    return proc.stdout


def _run_git_text(repo_root: Path, args: list[str], *, error_code: str, error_prefix: str) -> str:
    return _run_git_bytes(repo_root, args, error_code=error_code, error_prefix=error_prefix).decode("utf-8", errors="strict")


def _prepare_precondition_reason(git_state: dict[str, Any]) -> tuple[str, str] | None:
    branch = git_state["branch"]
    wt = git_state["working_tree"]
    if branch["state"] != "attached":
        return ("detached_head", "prepare-stage requires an attached branch")
    if wt["staged"]["count"] > 0:
        return ("staged_changes_present", "prepare-stage requires zero staged changes")
    if wt["unmerged"]["count"] > 0:
        return ("conflicts_present", "prepare-stage requires zero unmerged entries")
    if git_state["git_operation_in_progress"]:
        return ("git_operation_in_progress", "prepare-stage requires no active git operation")
    return None


def _parse_diff_raw_record(raw_record: bytes) -> dict[str, str]:
    if not raw_record.startswith(b":") or b"\t" not in raw_record:
        raise WorkflowReasonError("worktree_state_changed", "invalid raw diff record format")
    metadata, path_bytes = raw_record.split(b"\t", 1)
    fields = metadata.decode("utf-8", errors="strict").split(" ")
    if len(fields) < 5:
        raise WorkflowReasonError("worktree_state_changed", "invalid raw diff record metadata")

    return {
        "old_mode": fields[0][1:],
        "new_mode": fields[1],
        "old_oid": fields[2],
        "new_oid": fields[3],
        "status": fields[4],
        "path": path_bytes.decode("utf-8", errors="strict"),
    }


def _parse_diff_raw_output(raw_output: bytes) -> list[dict[str, str]]:
    chunks = [chunk for chunk in raw_output.split(b"\x00") if chunk]
    if len(chunks) % 2 != 0:
        raise WorkflowReasonError("worktree_state_changed", "invalid raw diff output shape")
    records: list[dict[str, str]] = []
    for i in range(0, len(chunks), 2):
        records.append(_parse_diff_raw_record(chunks[i] + b"\t" + chunks[i + 1]))
    return records


def _expected_mode_for_path(repo_root: Path, rel_path: str) -> str:
    full_path = repo_root / rel_path
    try:
        stat_result = full_path.lstat()
    except FileNotFoundError as exc:
        raise WorkflowReasonError("worktree_state_changed", f"candidate path disappeared before planning: {rel_path}") from exc

    if stat.S_ISLNK(stat_result.st_mode):
        raise WorkflowReasonError("unsupported_path_type", f"symlink candidate path is not supported: {rel_path}")
    if not stat.S_ISREG(stat_result.st_mode):
        raise WorkflowReasonError("unsupported_path_type", f"unsupported candidate path type: {rel_path}")
    return "100755" if (stat_result.st_mode & stat.S_IXUSR) else "100644"


def _ensure_supported_tracked_modes(old_mode: str, new_mode: str, rel_path: str) -> None:
    for mode in (old_mode, new_mode):
        if mode in {"000000", *REGULAR_FILE_MODES}:
            continue
        if mode == "120000":
            raise WorkflowReasonError("unsupported_path_type", f"symlink change is not supported: {rel_path}")
        if mode == "160000":
            raise WorkflowReasonError("unsupported_path_type", f"submodule/gitlink change is not supported: {rel_path}")
        raise WorkflowReasonError("unsupported_path_type", f"unsupported tracked path type or mode for {rel_path}: {mode}")


def _filter_values(repo_root: Path, paths: list[str]) -> dict[str, str]:
    if not paths:
        return {}
    proc = subprocess.run(
        ["git", "-C", str(repo_root), "check-attr", "-z", "--stdin", "filter"],
        input=b"\x00".join(path.encode("utf-8") for path in paths) + b"\x00",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace").strip()
        raise WorkflowReasonError("unsupported_git_filters", f"unable to inspect git filter attributes: {stderr}")

    chunks = [chunk.decode("utf-8", errors="strict") for chunk in proc.stdout.split(b"\x00") if chunk]
    if len(chunks) % 3 != 0:
        raise WorkflowReasonError("unsupported_git_filters", "unexpected git check-attr output shape")

    values: dict[str, str] = {}
    for i in range(0, len(chunks), 3):
        path, _attr, value = chunks[i : i + 3]
        values[path] = value
    return values


def _expected_filtered_oid(repo_root: Path, rel_path: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo_root), "hash-object", f"--path={rel_path}", str(repo_root / rel_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace").strip()
        raise WorkflowReasonError("worktree_state_changed", f"unable to compute expected staged object id for {rel_path}: {stderr}")
    return proc.stdout.decode("utf-8", errors="strict").strip()


def _summary_status_for_candidate(is_untracked: bool, is_deleted: bool) -> str:
    if is_untracked:
        return "A"
    if is_deleted:
        return "D"
    return "M"


def _raw_record_bytes(record: dict[str, Any]) -> bytes:
    return (
        f":{record['old_mode']} {record['expected_mode']} {record['old_oid']} {record['expected_oid']} {record['summary_status']}"
        .encode("utf-8")
        + b"\t"
        + record["path"].encode("utf-8")
    )


def _fingerprint_from_raw_records(records: list[bytes], prefix: bytes) -> str:
    parts = bytearray(prefix)
    for record in records:
        parts.extend(record)
        parts.extend(b"\0")
    return hashlib.sha256(bytes(parts)).hexdigest()


def _enumerate_stage_candidates(repo_root: Path, git_state: dict[str, Any]) -> dict[str, Any]:
    tracked_raw = _run_git_bytes(
        repo_root,
        ["diff-files", "--raw", "--no-renames", "--abbrev=40", "-z", "--"],
        error_code="worktree_state_changed",
        error_prefix="unable to enumerate tracked worktree changes",
    )
    tracked_records = _parse_diff_raw_output(tracked_raw)

    untracked_raw = _run_git_bytes(
        repo_root,
        ["ls-files", "--others", "--exclude-standard", "-z"],
        error_code="worktree_state_changed",
        error_prefix="unable to enumerate untracked worktree changes",
    )
    untracked_paths = [chunk.decode("utf-8", errors="strict") for chunk in untracked_raw.split(b"\x00") if chunk]

    candidate_records: list[dict[str, Any]] = []
    filter_candidate_paths: list[str] = []

    for record in tracked_records:
        rel_path = record["path"]
        _ensure_supported_tracked_modes(record["old_mode"], record["new_mode"], rel_path)
        is_deleted = record["new_mode"] == "000000" or record["status"].startswith("D")
        candidate_record = {
            "path": rel_path,
            "source": "tracked",
            "summary_status": _summary_status_for_candidate(False, is_deleted),
            "old_mode": record["old_mode"],
            "old_oid": record["old_oid"],
            "deleted": is_deleted,
            "classification": "tracked_deleted" if is_deleted else "tracked_modified",
        }
        if is_deleted:
            candidate_record["expected_mode"] = "000000"
            candidate_record["expected_oid"] = ZERO_OID
        else:
            filter_candidate_paths.append(rel_path)
            candidate_record["expected_mode"] = _expected_mode_for_path(repo_root, rel_path)
        candidate_records.append(candidate_record)

    for rel_path in untracked_paths:
        filter_candidate_paths.append(rel_path)
        candidate_records.append(
            {
                "path": rel_path,
                "source": "untracked",
                "summary_status": "A",
                "old_mode": "000000",
                "old_oid": ZERO_OID,
                "expected_mode": _expected_mode_for_path(repo_root, rel_path),
                "deleted": False,
                "classification": "untracked_addition",
            }
        )

    if not candidate_records:
        raise WorkflowReasonError("no_stage_candidates", "prepare-stage requires at least one eligible unstaged or untracked change")

    filter_values = _filter_values(repo_root, sorted(set(filter_candidate_paths), key=lambda p: p.encode("utf-8")))
    for path, value in filter_values.items():
        if value not in {"unspecified", "unset"}:
            raise WorkflowReasonError("unsupported_git_filters", f"custom git filter applies to candidate path: {path} ({value})")

    for record in candidate_records:
        if not record["deleted"]:
            record["expected_oid"] = _expected_filtered_oid(repo_root, record["path"])

    candidate_records.sort(key=lambda item: item["path"].encode("utf-8"))
    raw_records = [_raw_record_bytes(record) for record in candidate_records]
    expected_staged_delta_fingerprint = _fingerprint_from_raw_records(raw_records, b"repoctl-staged-delta-v1\0")

    summary = [{"status": record["summary_status"], "path": record["path"]} for record in candidate_records]
    fingerprint_payload = {
        "repository_root": str(repo_root),
        "head": git_state["head"],
        "branch": git_state["branch"],
        "candidates": [
            {
                "path": record["path"],
                "classification": record["classification"],
                "summary_status": record["summary_status"],
                "old_mode": record["old_mode"],
                "expected_mode": record["expected_mode"],
                "old_oid": record["old_oid"],
                "expected_oid": record["expected_oid"],
                "deleted": record["deleted"],
            }
            for record in candidate_records
        ],
        "filter_values": filter_values,
    }
    candidate_fingerprint = hashlib.sha256(
        ("repoctl-stage-candidates-v1\0" + json.dumps(fingerprint_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))).encode(
            "utf-8"
        )
    ).hexdigest()

    tracked_paths = [record["path"] for record in candidate_records if record["source"] == "tracked"]
    untracked_paths = [record["path"] for record in candidate_records if record["source"] == "untracked"]

    return {
        "candidate_records": candidate_records,
        "candidate_record_count": len(candidate_records),
        "candidate_summary": summary,
        "stage_candidate_fingerprint": candidate_fingerprint,
        "expected_staged_delta_fingerprint": expected_staged_delta_fingerprint,
        "filter_policy": {
            "custom_filters_blocked": True,
            "checked_path_count": len(filter_values),
            "filter_values": filter_values,
        },
        "tracked_paths": tracked_paths,
        "untracked_paths": untracked_paths,
    }


def _plan_base_payload(repository_id: str, repository_root: Path, git_state: dict[str, Any], candidates: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "repository_id": repository_id,
        "repository_root": str(repository_root),
        "branch": git_state["branch"],
        "head": git_state["head"],
        "stage_candidate_fingerprint": candidates["stage_candidate_fingerprint"],
        "expected_staged_delta_fingerprint": candidates["expected_staged_delta_fingerprint"],
        "candidate_record_count": candidates["candidate_record_count"],
        "candidate_summary": candidates["candidate_summary"],
        "candidate_records": candidates["candidate_records"],
        "mechanical_preconditions": {
            "branch_attached": True,
            "staged_changes_present": False,
            "unmerged_entries_present": False,
            "git_operation_in_progress": False,
        },
        "filter_policy": candidates["filter_policy"],
        "no_git_mutation_performed": True,
        "remote_refresh_performed": False,
    }


def _derive_stage_plan_id(base_payload: dict[str, Any]) -> str:
    canonical = json.dumps(base_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "stage-plan--" + hashlib.sha256(canonical).hexdigest()[:16]


def _build_stage_plan_markdown(payload: dict[str, Any]) -> str:
    branch_name = payload["branch"]["name"] if payload["branch"]["state"] == "attached" else "(detached)"
    lines = [
        "# Prepared Staging Plan",
        "",
        f"- Plan id: {payload['plan_id']}",
        f"- Repository root: {payload['repository_root']}",
        f"- Repository id: {payload['repository_id']}",
        f"- Branch: {branch_name}",
        f"- HEAD: {payload['head']}",
        f"- Changes proposed for staging: {payload['candidate_record_count']}",
        "",
        "## Candidate Summary",
    ]
    for item in payload["candidate_summary"]:
        lines.append(f"- {item['status']} {item['path']}")
    lines.extend([
        "",
        "NO TARGET GIT MUTATION PERFORMED",
        "",
        f"To approve this exact plan: repoctl milestone stage {payload['plan_id']} --approve",
    ])
    return "\n".join(lines) + "\n"


def _verify_existing_stage_plan(plan_dir: Path, expected_plan_id: str) -> dict[str, Any]:
    names = {child.name for child in plan_dir.iterdir()}
    if names != STAGE_PLAN_REQUIRED_FILES:
        raise WorkflowReasonError("plan_integrity_failed", "stage plan artifact set is incomplete or unexpected")

    payload = json.loads((plan_dir / "plan.json").read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1:
        raise WorkflowReasonError("plan_integrity_failed", "unsupported stage plan schema version")
    if payload.get("plan_id") != expected_plan_id:
        raise WorkflowReasonError("plan_integrity_failed", "stage plan id mismatch")

    base = dict(payload)
    base.pop("plan_id", None)
    recomputed = _derive_stage_plan_id(base)
    if recomputed != expected_plan_id:
        raise WorkflowReasonError("plan_integrity_failed", "stage plan id/content integrity mismatch")

    if not (plan_dir / "plan.md").read_text(encoding="utf-8"):
        raise WorkflowReasonError("plan_integrity_failed", "stage plan markdown is empty")
    return payload


def _publish_stage_plan(plan_root: Path, payload: dict[str, Any], markdown: str) -> tuple[str, bool]:
    plan_root.mkdir(parents=True, exist_ok=True)
    plan_id = payload["plan_id"]
    final_dir = plan_root / plan_id
    temp_dir = Path(mkdtemp(prefix="stage-plan-tmp-", dir=str(plan_root)))
    try:
        write_json_deterministic(temp_dir / "plan.json", payload)
        (temp_dir / "plan.md").write_text(markdown, encoding="utf-8", newline="\n")
        _verify_existing_stage_plan(temp_dir, plan_id)

        if final_dir.exists():
            _verify_existing_stage_plan(final_dir, plan_id)
            if (final_dir / "plan.json").read_bytes() != (temp_dir / "plan.json").read_bytes() or (
                final_dir / "plan.md"
            ).read_bytes() != (temp_dir / "plan.md").read_bytes():
                raise WorkflowReasonError("plan_integrity_failed", "existing stage plan content mismatch for identical plan id")
            shutil.rmtree(temp_dir)
            return str(final_dir), True

        temp_dir.rename(final_dir)
        return str(final_dir), False
    except Exception:
        if temp_dir.exists():
            shutil.rmtree(temp_dir)
        raise


def prepare_stage(repository_path: str, include_all: bool, state_root: Path | None = None) -> dict[str, Any]:
    if not include_all:
        raise WorkflowReasonError("no_stage_candidates", "prepare-stage currently requires --all")

    root = (state_root or DEFAULT_STATE_ROOT).expanduser()
    root.mkdir(parents=True, exist_ok=True)

    try:
        git_state = inspect_git_state(repository_path)
    except WorkflowGitStateError as exc:
        raise WorkflowReasonError("worktree_state_changed", str(exc)) from exc

    precondition = _prepare_precondition_reason(git_state)
    if precondition:
        raise WorkflowReasonError(precondition[0], precondition[1])

    repository_root = Path(git_state["repository_root"])
    repository_id = make_repository_id(repository_root)
    candidates = _enumerate_stage_candidates(repository_root, git_state)

    base_payload = _plan_base_payload(repository_id, repository_root, git_state, candidates)
    plan_id = _derive_stage_plan_id(base_payload)
    payload = dict(base_payload)
    payload["plan_id"] = plan_id
    markdown = _build_stage_plan_markdown(payload)
    plan_root = root / repository_id / "workflow" / "stage_plans"
    plan_dir, reused_existing = _publish_stage_plan(plan_root, payload, markdown)

    return {
        "plan_id": plan_id,
        "repository_id": repository_id,
        "repository_root": str(repository_root),
        "branch": git_state["branch"],
        "head": git_state["head"],
        "candidate_record_count": candidates["candidate_record_count"],
        "candidate_summary": candidates["candidate_summary"],
        "staged_count": git_state["working_tree"]["staged"]["count"],
        "unmerged_count": git_state["working_tree"]["unmerged"]["count"],
        "git_operation_in_progress": git_state["git_operation_in_progress"],
        "plan_dir": plan_dir,
        "reused_existing": reused_existing,
    }