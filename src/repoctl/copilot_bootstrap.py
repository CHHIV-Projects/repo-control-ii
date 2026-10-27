from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import secrets
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, TextIO
from uuid import uuid4

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PYTHON_EXECUTABLE = REPOSITORY_ROOT / ".venv" / "bin" / "python"
REPOSITORY_KEY = "repo-control-ii"
ADAPTER_VERSION = "1"
CLI_TIMEOUT_SECONDS = 10
MAX_EVENT_CHARS = 1_000_000
MAX_CLI_OUTPUT_BYTES = 1_000_000
MAX_CONTEXT_BYTES = 8_192
MAX_RECEIPT_BYTES = 4_096
MAX_REFERENCE_ITEMS = 4
MAX_REFERENCE_TEXT = 240
MAX_AUTHORITY_TEXT = 512

_PHASES = {"PLANNING", "READY", "IN_PROGRESS", "WAITING", "REVIEW", "CLOSED"}
_AUTHORIZATION_STATUSES = {
    "AUTHORIZED",
    "AUTHORIZED_CORRECTION",
    "NOT_AUTHORIZED",
    "UNKNOWN",
}
_SESSION_SOURCES = {"startup", "resume", "new"}
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_REFERENCE_FIELDS = (
    "event_id",
    "question_id",
    "bug_id",
    "id",
    "event_type",
    "category",
    "severity",
    "status",
    "summary",
    "title",
)
_STANDING_REFERENCE_FIELDS = (
    "coding_agent_rules",
    "project_workflow",
    "project_architecture",
    "project_context",
    "canonical_parking_lot",
    "git_mutation_policy",
)


class _BootstrapError(ValueError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _bounded_text(value: str, limit: int = MAX_REFERENCE_TEXT) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + "... [truncated]"


def _mapping(value: Any, reason: str = "invalid_context_shape") -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _BootstrapError(reason)
    return value


def _required_text(data: dict[str, Any], field: str, reason: str = "invalid_context_shape") -> str:
    value = data.get(field)
    if not isinstance(value, str) or not value.strip():
        raise _BootstrapError(reason)
    return value


def _bounded_identifier(value: str, reason: str, limit: int = 128) -> str:
    if len(value) > limit:
        raise _BootstrapError(reason)
    return value


def _required_list(data: dict[str, Any], field: str) -> list[Any]:
    value = data.get(field)
    if not isinstance(value, list):
        raise _BootstrapError("invalid_context_shape")
    return value


def _measure_context(payload: dict[str, Any]) -> tuple[str, int, int]:
    measurements = {
        "method": "ceil(UTF-8 bytes / 4); heuristic, not a model tokenizer",
        "utf8_bytes": 0,
        "estimated_tokens": 0,
    }
    payload["measurement"] = measurements
    try:
        for _ in range(5):
            content = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            byte_count = len(content.encode("utf-8"))
            token_estimate = math.ceil(byte_count / 4)
            if measurements["utf8_bytes"] == byte_count and measurements["estimated_tokens"] == token_estimate:
                break
            measurements["utf8_bytes"] = byte_count
            measurements["estimated_tokens"] = token_estimate
        content = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        byte_count = len(content.encode("utf-8"))
        token_estimate = math.ceil(byte_count / 4)
        if byte_count > MAX_CONTEXT_BYTES:
            raise _BootstrapError("context_exceeds_bound")
        if measurements["utf8_bytes"] != byte_count or measurements["estimated_tokens"] != token_estimate:
            measurements["utf8_bytes"] = byte_count
            measurements["estimated_tokens"] = token_estimate
            content = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            byte_count = len(content.encode("utf-8"))
            token_estimate = math.ceil(byte_count / 4)
        if byte_count > MAX_CONTEXT_BYTES:
            raise _BootstrapError("context_exceeds_bound")
    except UnicodeError as exc:
        raise _BootstrapError("context_encoding_invalid") from exc
    return content, byte_count, token_estimate


def _invalid_payload(
    reason: str,
    observation_id: str,
    observed_at: str,
    source: str | None,
    cli_exit_code: int | None,
    core: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema": "repo-control.bootstrap.v1",
        "status": "INVALID_NO_AUTHORITY",
        "reason": reason,
        "observation": {
            "id": observation_id,
            "observedAt": observed_at,
            "hostEvent": "SessionStart",
            "source": source,
        },
        "authorization": {"authorized": False, "status": "NOT_AUTHORIZED"},
        "instruction": "STOP. No current Repo Control authority was verified. Do not begin work; retrieve fresh canonical context.",
    }
    if cli_exit_code is not None:
        payload["cliExitCode"] = cli_exit_code
    if core:
        payload["verifiedIdentity"] = core
    return payload


def _invalid_result(
    reason: str,
    observation_id: str,
    observed_at: str,
    source: str | None,
    cli_exit_code: int | None = None,
    core: dict[str, Any] | None = None,
    cli_invocation_attempted: bool = False,
) -> tuple[dict[str, str], dict[str, Any]]:
    payload = _invalid_payload(reason, observation_id, observed_at, source, cli_exit_code, core)
    context, byte_count, token_estimate = _measure_context(payload)
    diagnostic = {
        "event": "repo_control_session_start",
        "adapter_version": ADAPTER_VERSION,
        "observation_id": observation_id,
        "observed_at": observed_at,
        "outcome": "invalid_no_authority",
        "reason": reason,
        "cli_invocation_attempted": cli_invocation_attempted,
        "cli_exit_code": cli_exit_code,
        "source_worktree": core.get("repository", {}).get("path") if core else None,
        "repository_key": core.get("repository", {}).get("key") if core else None,
        "repository_id": core.get("repository", {}).get("repository_id") if core else None,
        "milestone_id": core.get("milestone", {}).get("id") if core else None,
        "lifecycle": core.get("milestone", {}).get("phase") if core else None,
        "additional_context_bytes": byte_count,
        "estimated_tokens": token_estimate,
        "estimate_method": "ceil(UTF-8 bytes / 4)",
        "delivery_marker": None,
    }
    return {"additionalContext": context}, diagnostic


def _validate_event(event: Any) -> tuple[str, str]:
    if not isinstance(event, dict) or event.get("hook_event_name") != "SessionStart":
        raise _BootstrapError("invalid_session_start_event")
    session_id = event.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        raise _BootstrapError("invalid_session_start_event")
    timestamp = event.get("timestamp")
    if not isinstance(timestamp, str):
        raise _BootstrapError("invalid_session_start_event")
    try:
        parsed_timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise _BootstrapError("invalid_session_start_event") from exc
    if parsed_timestamp.tzinfo is None:
        raise _BootstrapError("invalid_session_start_event")
    source = event.get("source")
    if not isinstance(source, str) or source not in _SESSION_SOURCES:
        raise _BootstrapError("invalid_session_start_event")
    cwd = event.get("cwd")
    if not isinstance(cwd, str) or "\x00" in cwd or not Path(cwd).is_absolute():
        raise _BootstrapError("invalid_session_start_event")
    return source, cwd


def _resolve_worktree(cwd: str, repository_root: Path) -> tuple[Path, Path]:
    try:
        root = repository_root.resolve(strict=True)
        worktree = Path(cwd).resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise _BootstrapError("worktree_unavailable") from exc
    if not root.is_dir() or not worktree.is_dir():
        raise _BootstrapError("worktree_unavailable")
    try:
        worktree.relative_to(root)
    except ValueError as exc:
        raise _BootstrapError("worktree_mismatch") from exc
    return root, worktree


def _condition_references(items: list[Any]) -> dict[str, Any]:
    references: list[dict[str, Any]] = []
    for item in items[:MAX_REFERENCE_ITEMS]:
        condition = _mapping(item)
        reference: dict[str, Any] = {}
        for field in _REFERENCE_FIELDS:
            value = condition.get(field)
            if isinstance(value, str) and value.strip():
                reference[field] = _bounded_text(value)
        if not reference:
            reference["summary"] = "Open Repo Control condition; retrieve full context."
        references.append(reference)
    return {
        "count": len(items),
        "items": references,
        "omitted": max(0, len(items) - len(references)),
    }


def _validate_context(
    data: Any,
    repository_root: Path,
    source: str,
    observation_id: str,
    observed_at: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    context = _mapping(data)
    repository = _mapping(context.get("repository"))
    repo_key = _required_text(repository, "key")
    repo_path = _required_text(repository, "path")
    repository_id = _bounded_identifier(
        _required_text(repository, "repository_id"),
        "repository_identity_mismatch",
    )
    repo_name = _required_text(repository, "name")
    _required_text(repository, "milestone_root")
    if repo_key != REPOSITORY_KEY:
        raise _BootstrapError("repository_identity_mismatch")
    try:
        reported_root = Path(repo_path).resolve(strict=True)
        canonical_root = repository_root.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise _BootstrapError("repository_identity_mismatch") from exc
    if not Path(repo_path).is_absolute() or reported_root != canonical_root:
        raise _BootstrapError("repository_identity_mismatch")

    selection = _mapping(context.get("selection"))
    milestone = _mapping(context.get("milestone"))
    milestone_id = _bounded_identifier(
        _required_text(milestone, "id"),
        "milestone_identity_mismatch",
    )
    phase = _required_text(milestone, "phase")
    if phase not in _PHASES:
        raise _BootstrapError("unrecognized_lifecycle")
    if selection.get("milestone_id") != milestone_id:
        raise _BootstrapError("milestone_identity_mismatch")

    prompt = _mapping(context.get("prompt"))
    prompt_path = _required_text(prompt, "path")
    if len(prompt_path) > 1024:
        raise _BootstrapError("prompt_identity_mismatch")
    if selection.get("prompt_path") != prompt_path:
        raise _BootstrapError("prompt_identity_mismatch")
    relative_prompt = PurePosixPath(prompt_path)
    if relative_prompt.is_absolute() or ".." in relative_prompt.parts:
        raise _BootstrapError("prompt_identity_mismatch")
    prompt_identity = _required_text(prompt, "identity")
    if not _SHA256_RE.fullmatch(prompt_identity):
        raise _BootstrapError("prompt_identity_mismatch")
    prompt_size = prompt.get("size_bytes")
    prompt_content = prompt.get("content")
    if isinstance(prompt_size, bool) or not isinstance(prompt_size, int) or prompt_size <= 0:
        raise _BootstrapError("prompt_identity_mismatch")
    if not isinstance(prompt_content, str) or not prompt_content:
        raise _BootstrapError("prompt_identity_mismatch")
    prompt_file = canonical_root.joinpath(*relative_prompt.parts)
    try:
        resolved_prompt = prompt_file.resolve(strict=True)
        resolved_prompt.relative_to(canonical_root)
        actual_identity = hashlib.sha256(resolved_prompt.read_bytes()).hexdigest()
    except (OSError, RuntimeError, ValueError) as exc:
        raise _BootstrapError("prompt_identity_mismatch") from exc
    if actual_identity != prompt_identity:
        raise _BootstrapError("prompt_identity_mismatch")

    authorization = _mapping(context.get("authorization"))
    authorized = authorization.get("authorized")
    auth_status = _required_text(authorization, "status")
    auth_message = _required_text(authorization, "message")
    if not isinstance(authorized, bool) or auth_status not in _AUTHORIZATION_STATUSES:
        raise _BootstrapError("invalid_authorization")
    if authorized != (auth_status in {"AUTHORIZED", "AUTHORIZED_CORRECTION"}):
        raise _BootstrapError("invalid_authorization")

    workflow = _mapping(context.get("workflow"))
    if workflow.get("phase") != phase:
        raise _BootstrapError("workflow_lifecycle_mismatch")
    questions = _required_list(workflow, "open_questions")
    bugs = _required_list(workflow, "open_bugs")
    event_count = workflow.get("event_count")
    if isinstance(event_count, bool) or not isinstance(event_count, int) or event_count < 0:
        raise _BootstrapError("invalid_context_shape")
    next_action = _required_text(workflow, "next_action")
    waiting_category = workflow.get("waiting_category")
    waiting_reason = workflow.get("waiting_reason")
    if waiting_category is not None and not isinstance(waiting_category, str):
        raise _BootstrapError("invalid_context_shape")
    if waiting_reason is not None and not isinstance(waiting_reason, str):
        raise _BootstrapError("invalid_context_shape")

    latest_lock_in = workflow.get("latest_binding_lock_in")
    lock_in_reference = None
    if latest_lock_in is not None:
        lock_in = _mapping(latest_lock_in)
        lock_in_reference = {
            "event_id": _bounded_identifier(_required_text(lock_in, "event_id"), "invalid_context_shape"),
            "summary": _bounded_text(_required_text(lock_in, "summary"), MAX_AUTHORITY_TEXT),
        }

    raw_dispatches = _required_list(context, "approved_architect_dispatches")
    dispatch_references: list[dict[str, str]] = []
    for item in raw_dispatches[:MAX_REFERENCE_ITEMS]:
        dispatch = _mapping(item)
        dispatch_id = _bounded_identifier(
            _required_text(dispatch, "dispatch_id"),
            "invalid_dispatch_reference",
        )
        dispatch_type = _required_text(dispatch, "type")
        dispatch_milestone = _required_text(dispatch, "milestone_id")
        if dispatch_type not in {"INSTRUCTION", "RESPONSE"} or dispatch_milestone != milestone_id:
            raise _BootstrapError("invalid_dispatch_reference")
        related_event_id = dispatch.get("related_event_id")
        if related_event_id is not None:
            if not isinstance(related_event_id, str):
                raise _BootstrapError("invalid_dispatch_reference")
            related_event_id = _bounded_identifier(related_event_id, "invalid_dispatch_reference")
        dispatch_references.append({
            "alias": _bounded_text(_required_text(dispatch, "alias"), 32),
            "dispatch_id": dispatch_id,
            "type": dispatch_type,
            "milestone_id": dispatch_milestone,
            "approved_at": _bounded_text(_required_text(dispatch, "approved_at"), 64),
            "related_event_id": related_event_id,
        })

    standing_references = _mapping(context.get("standing_references"))
    safe_references: dict[str, str] = {}
    for field in _STANDING_REFERENCE_FIELDS:
        value = standing_references.get(field)
        if not isinstance(value, str) or not value.strip():
            raise _BootstrapError("missing_standing_reference")
        safe_references[field] = _bounded_text(value, MAX_AUTHORITY_TEXT)

    core = {
        "repository": {
            "name": _bounded_text(repo_name, MAX_REFERENCE_TEXT),
            "key": repo_key,
            "repository_id": repository_id,
            "path": str(canonical_root),
        },
        "milestone": {"id": milestone_id, "phase": phase},
        "prompt": {"path": prompt_path, "sha256": prompt_identity},
        "authorization": {"authorized": authorized, "status": auth_status},
    }
    status = "CURRENT_CONTEXT" if authorized else "NO_AUTHORITY"
    instruction = (
        "This is fresh, point-in-time context, not persistent permission. Read the full canonical prompt and guidance on demand, and rerun coder-context before consequential actions. The adapter does not grant filesystem, shell, or Git authority."
    )
    if not authorized:
        instruction = (
            "STOP: Repo Control currently reports no Coder implementation authorization. Do not begin work. Resolve the current stop condition and retrieve fresh context."
        )
    payload = {
        "schema": "repo-control.bootstrap.v1",
        "adapterVersion": ADAPTER_VERSION,
        "status": status,
        "observation": {
            "id": observation_id,
            "observedAt": observed_at,
            "hostEvent": "SessionStart",
            "source": source,
        },
        "repository": core["repository"],
        "selection": {
            "milestoneId": milestone_id,
            "phase": phase,
            "promptPath": prompt_path,
            "promptSha256": prompt_identity,
            "promptSizeBytes": prompt_size,
        },
        "authorization": {
            "authorized": authorized,
            "status": auth_status,
            "message": _bounded_text(auth_message, MAX_AUTHORITY_TEXT),
        },
        "workflow": {
            "phase": phase,
            "nextAction": _bounded_text(next_action, MAX_AUTHORITY_TEXT),
            "waitingCategory": _bounded_text(waiting_category, MAX_REFERENCE_TEXT) if waiting_category else None,
            "waitingReason": _bounded_text(waiting_reason, MAX_AUTHORITY_TEXT) if waiting_reason else None,
            "openQuestions": _condition_references(questions),
            "openBugs": _condition_references(bugs),
            "latestBindingLockIn": lock_in_reference,
            "eventCount": event_count,
        },
        "approvedArchitectDispatches": {
            "count": len(raw_dispatches),
            "items": dispatch_references,
            "omitted": max(0, len(raw_dispatches) - len(dispatch_references)),
        },
        "standingReferences": safe_references,
        "instruction": instruction,
        "fullContextCommand": f"{PYTHON_EXECUTABLE} -m repoctl.cli coder-context",
    }
    return payload, core


def build_session_start_response(
    event: Any,
    *,
    repository_root: Path = REPOSITORY_ROOT,
    python_executable: Path = PYTHON_EXECUTABLE,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    observed_at: str | None = None,
    observation_id: str | None = None,
    delivery_marker: str | None = None,
) -> tuple[dict[str, str], dict[str, Any]]:
    observed_at = observed_at or _timestamp()
    observation_id = observation_id or uuid4().hex
    source: str | None = None
    cli_exit_code: int | None = None
    try:
        source, cwd = _validate_event(event)
        canonical_root, worktree = _resolve_worktree(cwd, repository_root)
    except _BootstrapError as exc:
        return _invalid_result(exc.reason, observation_id, observed_at, source, cli_exit_code)

    run = runner or subprocess.run
    command = [
        str(python_executable),
        "-m",
        "repoctl.cli",
        "coder-context",
        "--format",
        "json",
        "--repository",
        str(worktree),
    ]
    try:
        completed = run(
            command,
            cwd=str(worktree),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=CLI_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return _invalid_result(
            "coder_context_timeout",
            observation_id,
            observed_at,
            source,
            cli_invocation_attempted=True,
        )
    except FileNotFoundError:
        return _invalid_result(
            "canonical_cli_unavailable",
            observation_id,
            observed_at,
            source,
            cli_invocation_attempted=True,
        )
    except PermissionError:
        return _invalid_result(
            "canonical_cli_not_executable",
            observation_id,
            observed_at,
            source,
            cli_invocation_attempted=True,
        )
    except (OSError, UnicodeError):
        return _invalid_result(
            "canonical_cli_execution_failed",
            observation_id,
            observed_at,
            source,
            cli_invocation_attempted=True,
        )

    cli_exit_code = completed.returncode
    if cli_exit_code != 0:
        return _invalid_result(
            "coder_context_failed",
            observation_id,
            observed_at,
            source,
            cli_exit_code,
            cli_invocation_attempted=True,
        )
    try:
        output_size = len(completed.stdout.encode("utf-8")) if isinstance(completed.stdout, str) else None
    except UnicodeError:
        output_size = None
    if output_size is None:
        return _invalid_result(
            "coder_context_invalid_output",
            observation_id,
            observed_at,
            source,
            cli_exit_code,
            cli_invocation_attempted=True,
        )
    if output_size > MAX_CLI_OUTPUT_BYTES:
        return _invalid_result(
            "coder_context_output_exceeds_bound",
            observation_id,
            observed_at,
            source,
            cli_exit_code,
            cli_invocation_attempted=True,
        )
    try:
        context_data = json.loads(completed.stdout)
    except (json.JSONDecodeError, TypeError, UnicodeError):
        return _invalid_result(
            "coder_context_invalid_json",
            observation_id,
            observed_at,
            source,
            cli_exit_code,
            cli_invocation_attempted=True,
        )

    core: dict[str, Any] | None = None
    try:
        payload, core = _validate_context(
            context_data,
            canonical_root,
            source,
            observation_id,
            observed_at,
        )
        context, byte_count, token_estimate = _measure_context(payload)
    except _BootstrapError as exc:
        return _invalid_result(
            exc.reason,
            observation_id,
            observed_at,
            source,
            cli_exit_code,
            core,
            cli_invocation_attempted=True,
        )
    if delivery_marker is not None:
        payload["deliveryProbe"] = {
            "marker": delivery_marker,
            "instruction": (
                "Acceptance instrumentation only: repeat this exact marker in your first response so delivery can be verified. "
                "The marker grants no authority."
            ),
        }
        try:
            context, byte_count, token_estimate = _measure_context(payload)
        except _BootstrapError as exc:
            return _invalid_result(
                exc.reason,
                observation_id,
                observed_at,
                source,
                cli_exit_code,
                core,
                cli_invocation_attempted=True,
            )

    diagnostic = {
        "event": "repo_control_session_start",
        "adapter_version": ADAPTER_VERSION,
        "observation_id": observation_id,
        "observed_at": observed_at,
        "outcome": payload["status"].lower(),
        "cli_invocation_attempted": True,
        "cli_exit_code": cli_exit_code,
        "source_worktree": core["repository"]["path"],
        "repository_key": core["repository"]["key"],
        "repository_id": core["repository"]["repository_id"],
        "milestone_id": core["milestone"]["id"],
        "lifecycle": core["milestone"]["phase"],
        "additional_context_bytes": byte_count,
        "estimated_tokens": token_estimate,
        "estimate_method": "ceil(UTF-8 bytes / 4)",
        "delivery_marker": delivery_marker,
    }
    return {"additionalContext": context}, diagnostic


def _write_acceptance_receipt(path: Path, record: dict[str, Any]) -> str | None:
    try:
        home = Path.home().resolve(strict=True)
        receipt_path = path.expanduser()
        if not receipt_path.is_absolute() or "\x00" in str(receipt_path):
            return "receipt_path_invalid"

        receipt_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        resolved_parent = receipt_path.parent.resolve(strict=True)
        resolved_parent.relative_to(home)
        parent_info = resolved_parent.stat()
        if not stat.S_ISDIR(parent_info.st_mode) or parent_info.st_uid != os.getuid():
            return "receipt_directory_not_user_owned"
        if parent_info.st_mode & 0o077:
            return "receipt_directory_not_private"

        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(receipt_path, flags, 0o600)
        try:
            file_info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(file_info.st_mode)
                or file_info.st_uid != os.getuid()
                or file_info.st_nlink != 1
            ):
                return "receipt_file_not_user_owned_regular_file"
            os.fchmod(descriptor, 0o600)
            encoded = (json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n").encode("utf-8")
            if len(encoded) > MAX_RECEIPT_BYTES:
                return "receipt_record_exceeds_bound"
            offset = 0
            while offset < len(encoded):
                offset += os.write(descriptor, encoded[offset:])
        finally:
            os.close(descriptor)
    except (OSError, RuntimeError, ValueError):
        return "receipt_write_failed"
    return None


def _additional_context_metadata(
    hook_output: dict[str, str],
    expected_marker: str | None,
) -> tuple[bool, bool, bool, bool]:
    context_text = hook_output.get("additionalContext")
    if not isinstance(context_text, str):
        return False, False, False, False
    try:
        context = json.loads(context_text)
    except json.JSONDecodeError:
        return True, False, False, False
    if not isinstance(context, dict):
        return True, False, False, False
    valid = (
        context.get("schema") == "repo-control.bootstrap.v1"
        and context.get("status") in {"CURRENT_CONTEXT", "NO_AUTHORITY", "INVALID_NO_AUTHORITY"}
        and isinstance(context.get("authorization"), dict)
        and isinstance(context.get("instruction"), str)
    )
    marker = context.get("deliveryProbe")
    marker_value = marker.get("marker") if isinstance(marker, dict) else None
    has_marker = isinstance(marker_value, str)
    return True, valid, has_marker, expected_marker is not None and marker_value == expected_marker


def _emit(
    result: tuple[dict[str, str], dict[str, Any]],
    stdout: TextIO,
    stderr: TextIO,
    receipt_path: Path | None = None,
    receipt_metadata: dict[str, Any] | None = None,
) -> None:
    hook_output, diagnostic = result
    output_emitted = False
    try:
        stdout.write(json.dumps(hook_output, ensure_ascii=True, separators=(",", ":")) + "\n")
        stdout.flush()
        output_emitted = True
    except (OSError, UnicodeError):
        diagnostic["host_output_error"] = True

    receipt_error = None
    if receipt_path is not None:
        context_produced, context_valid, marker_in_context, marker_matches = (
            _additional_context_metadata(hook_output, diagnostic.get("delivery_marker"))
        )
        receipt = {
            "schema": "repo-control.copilot-session-start-receipt.v1",
            "hook_entered": True,
            "event": "SessionStart",
            "observation_id": diagnostic.get("observation_id"),
            "observed_at": diagnostic.get("observed_at"),
            "source": (receipt_metadata or {}).get("source"),
            "cwd": (receipt_metadata or {}).get("cwd"),
            "cli_invocation_attempted": diagnostic.get("cli_invocation_attempted", False),
            "cli_exit_code": diagnostic.get("cli_exit_code"),
            "result": diagnostic.get("outcome"),
            "reason": diagnostic.get("reason"),
            "repository_key": diagnostic.get("repository_key"),
            "repository_id": diagnostic.get("repository_id"),
            "repository_path": diagnostic.get("source_worktree"),
            "milestone_id": diagnostic.get("milestone_id"),
            "lifecycle": diagnostic.get("lifecycle"),
            "additional_context_produced": context_produced,
            "additional_context_valid": context_valid,
            "delivery_marker_in_context": marker_in_context,
            "delivery_marker_matches_context": marker_matches,
            "stdout_emitted": output_emitted,
            "delivery_marker": diagnostic.get("delivery_marker"),
        }
        receipt_error = _write_acceptance_receipt(receipt_path, receipt)
        diagnostic["receipt_status"] = "written" if receipt_error is None else "failed"
        diagnostic["receipt_error"] = receipt_error
    stderr.write(json.dumps(diagnostic, ensure_ascii=True, separators=(",", ":")) + "\n")


def main(
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    argv: list[str] | None = None,
) -> int:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--acceptance-receipt", type=Path)
    args = parser.parse_args(argv)
    observed_at = _timestamp()
    observation_id = uuid4().hex
    raw_event = stdin.read(MAX_EVENT_CHARS + 1)
    event: Any = None
    if len(raw_event) > MAX_EVENT_CHARS:
        result = _invalid_result("session_start_event_exceeds_bound", observation_id, observed_at, None)
    else:
        try:
            event = json.loads(raw_event)
        except json.JSONDecodeError:
            result = _invalid_result("session_start_event_invalid_json", observation_id, observed_at, None)
        else:
            result = build_session_start_response(
                event,
                observed_at=observed_at,
                observation_id=observation_id,
                delivery_marker=(
                    f"repoctl-probe-{secrets.token_hex(12)}"
                    if args.acceptance_receipt is not None
                    else None
                ),
            )
    receipt_metadata = {}
    if isinstance(event, dict):
        source = event.get("source")
        cwd = event.get("cwd")
        receipt_metadata = {
            "source": source[:32] if isinstance(source, str) else None,
            "cwd": cwd[:512] if isinstance(cwd, str) else None,
        }
    _emit(result, stdout, stderr, args.acceptance_receipt, receipt_metadata)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
