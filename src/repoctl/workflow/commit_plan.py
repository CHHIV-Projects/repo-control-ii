from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from tempfile import mkdtemp
from typing import Any

from ..scanner.core import DEFAULT_STATE_ROOT
from ..scanner.util import make_repository_id, write_json_deterministic
from .errors import WorkflowReasonError
from .git_state import WorkflowGitStateError, inspect_git_state
from .status import _calculate_snapshot_candidate

PLAN_REQUIRED_FILES = {"plan.json", "plan.md"}


def _run_git_bytes(repo_root: Path, args: list[str]) -> bytes:
    proc = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace").strip()
        raise WorkflowReasonError("git_commit_failed", f"git command failed: {' '.join(args)}: {stderr}")
    return proc.stdout


def _validate_commit_message(message: str | None) -> str:
    if message is None:
        raise WorkflowReasonError("invalid_commit_message", "commit message is required")
    if message == "":
        raise WorkflowReasonError("invalid_commit_message", "commit message must not be empty")
    if "\x00" in message:
        raise WorkflowReasonError("invalid_commit_message", "commit message must not contain NUL")
    return message


def _precondition_reason(git_state: dict[str, Any]) -> tuple[str, str] | None:
    branch = git_state["branch"]
    wt = git_state["working_tree"]
    if wt["unmerged"]["count"] > 0:
        return ("conflicts_present", "prepare-commit requires zero unmerged entries")
    if branch["state"] != "attached":
        return ("detached_head", "prepare-commit requires an attached branch")
    if wt["unstaged"]["count"] > 0:
        return ("unstaged_changes_present", "prepare-commit requires zero unstaged changes")
    if wt["untracked"]["count"] > 0:
        return ("untracked_changes_present", "prepare-commit requires zero untracked changes")
    if git_state["git_operation_in_progress"]:
        return ("git_operation_in_progress", "prepare-commit requires no active git operation")
    if wt["staged"]["count"] == 0:
        return ("no_staged_changes", "prepare-commit requires staged changes")
    return None


def _parse_staged_record(raw_record: bytes) -> dict[str, Any]:
    if not raw_record.startswith(b":") or b"\t" not in raw_record:
        raise WorkflowReasonError("staged_state_changed", "invalid staged raw record format")

    metadata, path_bytes = raw_record.split(b"\t", 1)
    fields = metadata.decode("utf-8", errors="strict").split(" ")
    if len(fields) < 5:
        raise WorkflowReasonError("staged_state_changed", "invalid staged raw record metadata")

    old_mode = fields[0][1:]
    new_mode = fields[1]
    old_oid = fields[2]
    new_oid = fields[3]
    status = fields[4]
    path = path_bytes.decode("utf-8", errors="strict")

    return {
        "path": path,
        "status": status,
        "old_mode": old_mode,
        "new_mode": new_mode,
        "old_oid": old_oid,
        "new_oid": new_oid,
        "raw": raw_record,
    }


def _parse_staged_raw_output(raw_output: bytes) -> list[dict[str, Any]]:
    chunks = [chunk for chunk in raw_output.split(b"\x00") if chunk]
    if len(chunks) % 2 != 0:
        raise WorkflowReasonError("staged_state_changed", "invalid staged raw output shape")

    records: list[dict[str, Any]] = []
    for i in range(0, len(chunks), 2):
        metadata = chunks[i]
        path_bytes = chunks[i + 1]
        records.append(_parse_staged_record(metadata + b"\t" + path_bytes))
    return records


def _staged_delta(repo_root: Path, head: str) -> dict[str, Any]:
    output = _run_git_bytes(repo_root, ["diff-index", "--cached", "--raw", "--no-renames", "--abbrev=40", "-z", "HEAD", "--"])
    records = _parse_staged_raw_output(output)
    records.sort(key=lambda item: (item["path"].encode("utf-8", errors="strict"), item["raw"]))

    digest_parts = bytearray(b"repoctl-staged-delta-v1\0")
    summary: list[dict[str, str]] = []
    full_records: list[dict[str, str]] = []
    for record in records:
        raw = record["raw"]
        digest_parts.extend(raw)
        digest_parts.extend(b"\0")
        summary.append({"status": record["status"], "path": record["path"]})
        full_records.append(
            {
                "status": record["status"],
                "path": record["path"],
                "old_mode": record["old_mode"],
                "new_mode": record["new_mode"],
                "old_oid": record["old_oid"],
                "new_oid": record["new_oid"],
            }
        )

    delta_fingerprint = hashlib.sha256(bytes(digest_parts)).hexdigest()
    state_fingerprint = hashlib.sha256(("repoctl-staged-state-v1\0" + head + "\0" + delta_fingerprint).encode("utf-8")).hexdigest()

    return {
        "record_count": len(records),
        "summary": summary,
        "records": full_records,
        "staged_delta_fingerprint": delta_fingerprint,
        "staged_state_fingerprint": state_fingerprint,
    }


def _plan_base_payload(
    repository_id: str,
    repository_root: Path,
    git_state: dict[str, Any],
    staged: dict[str, Any],
    current_snapshot_id_candidate: str,
    matching_snapshot_id: str,
    commit_message: str,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "repository_id": repository_id,
        "repository_root": str(repository_root),
        "branch": git_state["branch"],
        "head_before": git_state["head"],
        "staged_state_fingerprint": staged["staged_state_fingerprint"],
        "staged_delta_fingerprint": staged["staged_delta_fingerprint"],
        "staged_record_count": staged["record_count"],
        "staged_summary": staged["summary"],
        "staged_records": staged["records"],
        "current_snapshot_id_candidate": current_snapshot_id_candidate,
        "matching_snapshot_id": matching_snapshot_id,
        "commit_message": commit_message,
        "mechanical_preconditions": {
            "branch_attached": True,
            "staged_changes_present": True,
            "unstaged_changes_present": False,
            "untracked_changes_present": False,
            "unmerged_entries_present": False,
            "git_operation_in_progress": False,
        },
        "upstream": git_state["upstream"],
        "remote_refresh_performed": False,
    }


def _derive_plan_id(base_payload: dict[str, Any]) -> str:
    canonical = json.dumps(base_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "commit-plan--" + hashlib.sha256(canonical).hexdigest()[:16]


def _build_plan_markdown(payload: dict[str, Any]) -> str:
    branch_name = payload["branch"]["name"] if payload["branch"]["state"] == "attached" else "(detached)"
    lines = [
        "# Prepared Commit Plan",
        "",
        f"- Plan id: {payload['plan_id']}",
        f"- Repository root: {payload['repository_root']}",
        f"- Repository id: {payload['repository_id']}",
        f"- Branch: {branch_name}",
        f"- HEAD before commit: {payload['head_before']}",
        f"- Matching snapshot id: {payload['matching_snapshot_id']}",
        f"- Staged files: {payload['staged_record_count']}",
        "",
        "## Staged Summary",
    ]

    for item in payload["staged_summary"]:
        lines.append(f"- {item['status']} {item['path']}")

    lines.extend([
        "",
        "## Commit Message",
        "",
        payload["commit_message"],
        "",
        "NO GIT MUTATION PERFORMED",
    ])
    return "\n".join(lines) + "\n"


def _verify_existing_plan(plan_dir: Path, expected_plan_id: str) -> dict[str, Any]:
    names = {child.name for child in plan_dir.iterdir()}
    if names != PLAN_REQUIRED_FILES:
        raise WorkflowReasonError("plan_integrity_failed", "plan artifact set is incomplete or unexpected")

    payload = json.loads((plan_dir / "plan.json").read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1:
        raise WorkflowReasonError("plan_integrity_failed", "unsupported plan schema version")
    if payload.get("plan_id") != expected_plan_id:
        raise WorkflowReasonError("plan_integrity_failed", "plan id mismatch")

    base = dict(payload)
    base.pop("plan_id", None)
    recomputed = _derive_plan_id(base)
    if recomputed != expected_plan_id:
        raise WorkflowReasonError("plan_integrity_failed", "plan id/content integrity mismatch")

    if not (plan_dir / "plan.md").read_text(encoding="utf-8"):
        raise WorkflowReasonError("plan_integrity_failed", "plan markdown is empty")

    return payload


def _publish_plan(plan_root: Path, payload: dict[str, Any], markdown: str) -> tuple[str, bool]:
    plan_root.mkdir(parents=True, exist_ok=True)
    plan_id = payload["plan_id"]
    final_dir = plan_root / plan_id
    temp_dir = Path(mkdtemp(prefix="commit-plan-tmp-", dir=str(plan_root)))
    try:
        write_json_deterministic(temp_dir / "plan.json", payload)
        (temp_dir / "plan.md").write_text(markdown, encoding="utf-8", newline="\n")
        _verify_existing_plan(temp_dir, plan_id)

        if final_dir.exists():
            _verify_existing_plan(final_dir, plan_id)
            existing_json = (final_dir / "plan.json").read_bytes()
            existing_md = (final_dir / "plan.md").read_bytes()
            staged_json = (temp_dir / "plan.json").read_bytes()
            staged_md = (temp_dir / "plan.md").read_bytes()
            if existing_json != staged_json or existing_md != staged_md:
                raise WorkflowReasonError("plan_integrity_failed", "existing plan content mismatch for identical plan id")
            shutil.rmtree(temp_dir)
            return str(final_dir), True

        temp_dir.rename(final_dir)
        return str(final_dir), False
    except Exception:
        if temp_dir.exists():
            shutil.rmtree(temp_dir)
        raise


def prepare_commit(repository_path: str, commit_message: str, state_root: Path | None = None) -> dict[str, Any]:
    message = _validate_commit_message(commit_message)
    root = (state_root or DEFAULT_STATE_ROOT).expanduser()
    root.mkdir(parents=True, exist_ok=True)

    try:
        git_state = inspect_git_state(repository_path)
    except WorkflowGitStateError as exc:
        raise WorkflowReasonError("staged_state_changed", str(exc)) from exc

    precondition = _precondition_reason(git_state)
    if precondition:
        raise WorkflowReasonError(precondition[0], precondition[1])

    repository_root = Path(git_state["repository_root"])
    repository_id = make_repository_id(repository_root)
    candidate_id, matching_exists, matching_id = _calculate_snapshot_candidate(str(repository_root), repository_id, root)
    if not matching_exists or not matching_id:
        raise WorkflowReasonError(
            "matching_snapshot_required",
            "prepare-commit requires an exact matching immutable snapshot; run repoctl snapshot first",
        )

    staged = _staged_delta(repository_root, git_state["head"])
    if staged["record_count"] == 0:
        raise WorkflowReasonError("no_staged_changes", "prepare-commit requires staged changes")

    base_payload = _plan_base_payload(
        repository_id=repository_id,
        repository_root=repository_root,
        git_state=git_state,
        staged=staged,
        current_snapshot_id_candidate=candidate_id,
        matching_snapshot_id=matching_id,
        commit_message=message,
    )
    plan_id = _derive_plan_id(base_payload)
    payload = dict(base_payload)
    payload["plan_id"] = plan_id

    markdown = _build_plan_markdown(payload)
    plan_root = root / repository_id / "workflow" / "commit_plans"
    plan_dir, reused_existing = _publish_plan(plan_root, payload, markdown)

    return {
        "plan_id": plan_id,
        "repository_id": repository_id,
        "repository_root": str(repository_root),
        "branch": git_state["branch"],
        "head_before": git_state["head"],
        "matching_snapshot_id": matching_id,
        "staged_summary": staged["summary"],
        "staged_record_count": staged["record_count"],
        "unstaged_count": git_state["working_tree"]["unstaged"]["count"],
        "untracked_count": git_state["working_tree"]["untracked"]["count"],
        "unmerged_count": git_state["working_tree"]["unmerged"]["count"],
        "git_operation_in_progress": git_state["git_operation_in_progress"],
        "commit_message": message,
        "plan_dir": plan_dir,
        "reused_existing": reused_existing,
    }
