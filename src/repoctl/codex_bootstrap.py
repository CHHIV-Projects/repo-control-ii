from __future__ import annotations

import argparse
import json
import math
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TextIO
from uuid import uuid4

from .copilot_bootstrap import (
    MAX_CONTEXT_BYTES,
    MAX_EVENT_CHARS,
    PYTHON_EXECUTABLE,
    REPOSITORY_ID,
    REPOSITORY_KEY,
    REPOSITORY_ROOT,
    _write_acceptance_receipt,
    build_session_start_response as build_copilot_session_start_response,
)
from .copilot_bootstrap import _REPOSITORY_KEY_PATTERN

ADAPTER_VERSION = "1"
MAX_HOOK_OUTPUT_BYTES = 20_000
CODEX_SESSION_SOURCES = {"startup", "resume", "clear", "compact", "fork"}


class _CodexHookError(ValueError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _validate_event(event: Any) -> tuple[str, str]:
    if not isinstance(event, dict) or event.get("hook_event_name") != "SessionStart":
        raise _CodexHookError("invalid_session_start_event")
    session_id = event.get("session_id")
    source = event.get("source")
    cwd = event.get("cwd")
    if (
        not isinstance(session_id, str)
        or not session_id.strip()
        or len(session_id) > 256
        or not isinstance(source, str)
        or source not in CODEX_SESSION_SOURCES
        or not isinstance(cwd, str)
        or not cwd.strip()
        or len(cwd) > 4096
        or "\x00" in cwd
        or not Path(cwd).is_absolute()
    ):
        raise _CodexHookError("invalid_session_start_event")
    return source, cwd


def _invalid_context(
    reason: str,
    observation_id: str,
    observed_at: str,
    source: str | None,
) -> dict[str, Any]:
    return {
        "schema": "repo-control.continuity.v1",
        "status": "INVALID_NO_AUTHORITY",
        "reason": reason,
        "observation": {
            "id": observation_id,
            "observed_at": observed_at,
            "host_event": "SessionStart",
            "source": source,
        },
        "authorization": {"authorized": False, "status": "NOT_AUTHORIZED"},
        "instruction": (
            "STOP: current Repo Control authority could not be verified. "
            "Do not begin work; retrieve fresh canonical context."
        ),
    }


def _map_context(
    bootstrap_text: str,
    diagnostic: dict[str, Any],
    *,
    source: str,
    observation_id: str,
    observed_at: str,
    repository_root: Path,
    repository_key: str,
    repository_id: str,
) -> dict[str, Any]:
    try:
        bootstrap = json.loads(bootstrap_text)
    except (json.JSONDecodeError, TypeError):
        return _invalid_context("bootstrap_payload_invalid", observation_id, observed_at, source)
    if not isinstance(bootstrap, dict):
        return _invalid_context("bootstrap_payload_invalid", observation_id, observed_at, source)

    bootstrap_status = bootstrap.get("status")
    if bootstrap_status == "CURRENT_CONTEXT":
        status = "AUTHORIZED"
    elif bootstrap_status == "NO_AUTHORITY":
        status = "NO_AUTHORITY"
    elif bootstrap_status == "INVALID_NO_AUTHORITY":
        status = "INVALID_NO_AUTHORITY"
    else:
        return _invalid_context("bootstrap_status_invalid", observation_id, observed_at, source)

    base_observation = bootstrap.get("observation")
    if not isinstance(base_observation, dict):
        return _invalid_context("bootstrap_observation_invalid", observation_id, observed_at, source)
    result: dict[str, Any] = {
        "schema": "repo-control.continuity.v1",
        "status": status,
        "observation": {
            "id": base_observation.get("id", observation_id),
            "observed_at": base_observation.get("observedAt", observed_at),
            "host_event": "SessionStart",
            "source": source,
        },
        "authorization": bootstrap.get(
            "authorization",
            {"authorized": False, "status": "NOT_AUTHORIZED"},
        ),
        "instruction": bootstrap.get(
            "instruction",
            "STOP: retrieve and validate current Repo Control context before work.",
        ),
    }
    if isinstance(bootstrap.get("reason"), str):
        result["reason"] = bootstrap["reason"]

    if status != "INVALID_NO_AUTHORITY":
        repository = bootstrap.get("repository")
        selection = bootstrap.get("selection")
        workflow = bootstrap.get("workflow")
        if not all(isinstance(item, dict) for item in (repository, selection, workflow)):
            return _invalid_context("bootstrap_payload_invalid", observation_id, observed_at, source)
        if (
            repository.get("key") != repository_key
            or repository.get("repository_id") != repository_id
            or repository.get("path") != str(repository_root.resolve(strict=True))
        ):
            return _invalid_context("repository_identity_mismatch", observation_id, observed_at, source)
        result.update({
            "repository": {
                "key": repository.get("key"),
                "repository_id": repository.get("repository_id"),
                "path": repository.get("path"),
            },
            "worktree": {"cwd": diagnostic.get("source_worktree")},
            "selection": {
                "milestone_id": selection.get("milestoneId"),
                "prompt_path": selection.get("promptPath"),
            },
            "prompt": {
                "path": selection.get("promptPath"),
                "sha256": selection.get("promptSha256"),
            },
            "lifecycle": {"phase": selection.get("phase")},
            "conditions": {
                "open_questions": workflow.get("openQuestions"),
                "open_bugs": workflow.get("openBugs"),
            },
            "scope": {
                "approved_dispatches": bootstrap.get("approvedArchitectDispatches"),
                "binding_lock_in": workflow.get("latestBindingLockIn"),
            },
            "standing_references": bootstrap.get("standingReferences"),
            "full_context_command": bootstrap.get("fullContextCommand"),
        })

    probe = bootstrap.get("deliveryProbe")
    if isinstance(probe, dict):
        result["delivery_probe"] = {
            "marker": probe.get("marker"),
            "instruction": probe.get("instruction"),
        }
    return result


def _serialize_context(payload: dict[str, Any]) -> tuple[str, int, int]:
    measurement = {
        "method": "ceil(UTF-8 bytes / 4); heuristic, not a model tokenizer",
        "utf8_bytes": 0,
        "estimated_tokens": 0,
    }
    payload["measurement"] = measurement
    for _ in range(8):
        content = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        byte_count = len(content.encode("utf-8"))
        estimated_tokens = math.ceil(byte_count / 4)
        if byte_count > MAX_CONTEXT_BYTES:
            raise _CodexHookError("context_exceeds_bound")
        if (
            measurement["utf8_bytes"] == byte_count
            and measurement["estimated_tokens"] == estimated_tokens
        ):
            return content, byte_count, estimated_tokens
        measurement["utf8_bytes"] = byte_count
        measurement["estimated_tokens"] = estimated_tokens
    content = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    byte_count = len(content.encode("utf-8"))
    estimated_tokens = math.ceil(byte_count / 4)
    if byte_count > MAX_CONTEXT_BYTES:
        raise _CodexHookError("context_exceeds_bound")
    return content, byte_count, estimated_tokens


def build_session_start_response(
    event: Any,
    *,
    repository_root: Path = REPOSITORY_ROOT,
    repository_key: str = REPOSITORY_KEY,
    repository_id: str = REPOSITORY_ID,
    python_executable: Path = PYTHON_EXECUTABLE,
    runner: Callable[..., Any] | None = None,
    observed_at: str | None = None,
    observation_id: str | None = None,
    delivery_marker: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    observed_at = observed_at or _timestamp()
    observation_id = observation_id or uuid4().hex
    source: str | None = None
    cwd: str | None = None
    try:
        source, cwd = _validate_event(event)
        if (
            not repository_root.is_absolute()
            or not isinstance(repository_key, str)
            or not _REPOSITORY_KEY_PATTERN.fullmatch(repository_key)
            or not isinstance(repository_id, str)
            or not repository_id
            or len(repository_id) > 128
            or any(ord(char) < 33 or ord(char) > 126 for char in repository_id)
        ):
            raise _CodexHookError("invalid_expected_repository_identity")
    except _CodexHookError as exc:
        payload = _invalid_context(exc.reason, observation_id, observed_at, source)
        context, byte_count, estimated_tokens = _serialize_context(payload)
        return _host_response(context), {
            "event": "repo_control_codex_session_start",
            "adapter_version": ADAPTER_VERSION,
            "observation_id": observation_id,
            "observed_at": observed_at,
            "source": source,
            "cwd": cwd,
            "outcome": payload["status"],
            "cli_invocation_attempted": False,
            "cli_exit_code": None,
            "context_bytes": byte_count,
            "estimated_tokens": estimated_tokens,
            "delivery_marker": None,
        }

    helper_source = source if source in {"startup", "resume"} else "resume"
    helper_event = {
        "hook_event_name": "SessionStart",
        "session_id": event["session_id"],
        "timestamp": observed_at,
        "cwd": cwd,
        "source": helper_source,
    }
    bootstrap_output, bootstrap_diagnostic = build_copilot_session_start_response(
        helper_event,
        repository_root=repository_root,
        repository_key=repository_key,
        repository_id=repository_id,
        python_executable=python_executable,
        runner=runner,
        observed_at=observed_at,
        observation_id=observation_id,
        delivery_marker=delivery_marker,
    )
    payload = _map_context(
        bootstrap_output.get("additionalContext"),
        bootstrap_diagnostic,
        source=source,
        observation_id=observation_id,
        observed_at=observed_at,
        repository_root=repository_root,
        repository_key=repository_key,
        repository_id=repository_id,
    )
    try:
        context, byte_count, estimated_tokens = _serialize_context(payload)
    except _CodexHookError as exc:
        payload = _invalid_context(exc.reason, observation_id, observed_at, source)
        context, byte_count, estimated_tokens = _serialize_context(payload)

    diagnostic = {
        "event": "repo_control_codex_session_start",
        "adapter_version": ADAPTER_VERSION,
        "observation_id": observation_id,
        "observed_at": observed_at,
        "source": source,
        "cwd": cwd,
        "outcome": payload["status"],
        "reason": payload.get("reason"),
        "cli_invocation_attempted": bootstrap_diagnostic.get("cli_invocation_attempted", False),
        "cli_exit_code": bootstrap_diagnostic.get("cli_exit_code"),
        "source_worktree": bootstrap_diagnostic.get("source_worktree"),
        "repository_key": payload.get("repository", {}).get("key")
        if isinstance(payload.get("repository"), dict)
        else None,
        "repository_id": payload.get("repository", {}).get("repository_id")
        if isinstance(payload.get("repository"), dict)
        else None,
        "milestone_id": payload.get("selection", {}).get("milestone_id")
        if isinstance(payload.get("selection"), dict)
        else None,
        "lifecycle": payload.get("lifecycle", {}).get("phase")
        if isinstance(payload.get("lifecycle"), dict)
        else None,
        "context_bytes": byte_count,
        "estimated_tokens": estimated_tokens,
        "estimate_method": "ceil(UTF-8 bytes / 4)",
        "delivery_marker": delivery_marker,
    }
    return _host_response(context), diagnostic


def _host_response(context: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    }


def _receipt_context_metadata(
    hook_output: dict[str, Any],
    expected_marker: str | None,
) -> tuple[bool, bool, bool, bool]:
    hook_specific = hook_output.get("hookSpecificOutput")
    context_text = hook_specific.get("additionalContext") if isinstance(hook_specific, dict) else None
    if not isinstance(context_text, str):
        return False, False, False, False
    try:
        context = json.loads(context_text)
    except json.JSONDecodeError:
        return True, False, False, False
    if not isinstance(context, dict):
        return True, False, False, False
    valid = (
        context.get("schema") == "repo-control.continuity.v1"
        and context.get("status") in {"AUTHORIZED", "NO_AUTHORITY", "INVALID_NO_AUTHORITY"}
        and isinstance(context.get("authorization"), dict)
        and isinstance(context.get("instruction"), str)
    )
    probe = context.get("delivery_probe")
    marker = probe.get("marker") if isinstance(probe, dict) else None
    has_marker = isinstance(marker, str)
    return True, valid, has_marker, expected_marker is not None and marker == expected_marker


def _emit(
    hook_output: dict[str, Any],
    diagnostic: dict[str, Any],
    stdout: TextIO,
    stderr: TextIO,
    receipt_path: Path | None = None,
    receipt_metadata: dict[str, Any] | None = None,
) -> int:
    output_emitted = False
    try:
        encoded_output = json.dumps(hook_output, ensure_ascii=False, separators=(",", ":"))
        if len(encoded_output.encode("utf-8")) > MAX_HOOK_OUTPUT_BYTES:
            raise _CodexHookError("hook_output_exceeds_bound")
        stdout.write(encoded_output + "\n")
        stdout.flush()
        output_emitted = True
    except (OSError, UnicodeError, _CodexHookError):
        diagnostic["host_output_error"] = True

    receipt_error = None
    if receipt_path is not None:
        context_produced, context_valid, marker_in_context, marker_matches = (
            _receipt_context_metadata(hook_output, diagnostic.get("delivery_marker"))
        )
        receipt = {
            "schema": "repo-control.codex-session-start-receipt.v1",
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
    try:
        stderr.write(json.dumps(diagnostic, ensure_ascii=True, separators=(",", ":")) + "\n")
        stderr.flush()
    except (OSError, UnicodeError):
        return 1
    return 0 if output_emitted else 1


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
    parser.add_argument("--repository-root", type=Path, default=REPOSITORY_ROOT)
    parser.add_argument("--repository-key", default=REPOSITORY_KEY)
    parser.add_argument("--repository-id", default=REPOSITORY_ID)
    args = parser.parse_args(argv)
    observed_at = _timestamp()
    observation_id = uuid4().hex
    raw_event = stdin.read(MAX_EVENT_CHARS + 1)
    event: Any = None
    delivery_marker = (
        f"repoctl-codex-probe-{secrets.token_hex(12)}"
        if args.acceptance_receipt is not None
        else None
    )
    if len(raw_event) > MAX_EVENT_CHARS:
        payload = _invalid_context("session_start_event_exceeds_bound", observation_id, observed_at, None)
        context, byte_count, estimated_tokens = _serialize_context(payload)
        result = _host_response(context)
        diagnostic = {
            "event": "repo_control_codex_session_start",
            "adapter_version": ADAPTER_VERSION,
            "observation_id": observation_id,
            "observed_at": observed_at,
            "outcome": payload["status"],
            "reason": payload["reason"],
            "cli_invocation_attempted": False,
            "cli_exit_code": None,
            "context_bytes": byte_count,
            "estimated_tokens": estimated_tokens,
            "delivery_marker": None,
        }
    else:
        try:
            event = json.loads(raw_event)
        except json.JSONDecodeError:
            payload = _invalid_context("session_start_event_invalid_json", observation_id, observed_at, None)
            context, byte_count, estimated_tokens = _serialize_context(payload)
            result = _host_response(context)
            diagnostic = {
                "event": "repo_control_codex_session_start",
                "adapter_version": ADAPTER_VERSION,
                "observation_id": observation_id,
                "observed_at": observed_at,
                "outcome": payload["status"],
                "reason": payload["reason"],
                "cli_invocation_attempted": False,
                "cli_exit_code": None,
                "context_bytes": byte_count,
                "estimated_tokens": estimated_tokens,
                "delivery_marker": None,
            }
        else:
            result, diagnostic = build_session_start_response(
                event,
                repository_root=args.repository_root,
                repository_key=args.repository_key,
                repository_id=args.repository_id,
                observed_at=observed_at,
                observation_id=observation_id,
                delivery_marker=delivery_marker,
            )
    source = event.get("source") if isinstance(event, dict) else None
    cwd = event.get("cwd") if isinstance(event, dict) else None
    receipt_metadata = {
        "source": source[:32] if isinstance(source, str) else None,
        "cwd": cwd[:512] if isinstance(cwd, str) else None,
    }
    return _emit(result, diagnostic, stdout, stderr, args.acceptance_receipt, receipt_metadata)


if __name__ == "__main__":
    raise SystemExit(main())
