from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from datetime import UTC, datetime
from html import escape
from pathlib import Path, PurePosixPath
from typing import Any

from ..scanner.core import STATE_ARTIFACT_LOCK
from ..scanner.util import make_repository_id
from .architect_dispatches import list_outstanding_guidance
from .milestones import (
    MilestoneArtifactError,
    _contained_file,
    _root_paths,
    discover_milestones,
    load_milestone_selection,
    prompt_identity,
)

_LEDGER_SCHEMA_VERSION = 1
_MAX_SIDECAR_BYTES = 5_000_000
_MAX_EVENTS = 2000
_PHASES = {"PLANNING", "READY", "IN_PROGRESS", "WAITING", "REVIEW", "CLOSED"}
_EVENT_TYPES = {
    "prompt_approved",
    "implementation_started",
    "question_submitted",
    "answer_recorded",
    "decision_recorded",
    "lock_in_approved",
    "escalation_opened",
    "escalation_resolved",
    "bug_discovered",
    "bug_classified",
    "bug_fixed",
    "test_result_recorded",
    "implementation_resumed",
    "closeout_submitted",
    "review_requested",
    "correction_requested",
    "acceptance_recorded",
    "milestone_closed",
    "milestone_reopened",
    "git_disposition_recorded",
}
_ACTOR_ROLES = {"product_owner", "architect", "coder", "repo_control"}
_DISPATCH_ID_RE = re.compile(r"^[a-f0-9]{32}$")
_WAITING_CATEGORIES = {"clarification", "decision", "blocker", "escalation", "other"}
EVENT_OPTIONS = (
    ("prompt_approved", "Mark prompt READY (Product Owner approval)"),
    ("implementation_started", "Start implementation handoff"),
    ("question_submitted", "Record a question"),
    ("answer_recorded", "Record an answer to an open question"),
    ("decision_recorded", "Record a non-binding decision"),
    ("lock_in_approved", "Approve a binding lock-in (Product Owner)"),
    ("escalation_opened", "Open an escalation and wait"),
    ("escalation_resolved", "Record escalation resolution"),
    ("bug_discovered", "Record bug discovered"),
    ("bug_classified", "Classify a discovered bug"),
    ("bug_fixed", "Record an in-scope bug fix"),
    ("test_result_recorded", "Record test result"),
    ("implementation_resumed", "Resume implementation from WAITING"),
    ("closeout_submitted", "Submit available closeout for REVIEW"),
    ("review_requested", "Record review requested"),
    ("correction_requested", "Request bounded correction"),
    ("acceptance_recorded", "Record Product Owner acceptance"),
    ("milestone_closed", "Close milestone after acceptance (Product Owner)"),
    ("git_disposition_recorded", "Record Git disposition (no Git action)"),
)
ACTOR_OPTIONS = (
    ("product_owner", "Product Owner (reported context)"),
    ("architect", "Architect (reported context)"),
    ("coder", "Coder (reported context)"),
    ("repo_control", "Repo Control (reported context)"),
)
_SIDECAR_MARKER = "<!-- repoctl-workflow-ledger:v1 -->"


class WorkflowRecordError(RuntimeError):
    pass


def _sidecar_path(candidate: dict[str, Any]) -> str:
    prompt_path = PurePosixPath(candidate["prompt_path"])
    filename = prompt_path.name.removesuffix("_prompt.md") + "_workflow.md"
    return prompt_path.with_name(filename).as_posix()


def _empty_ledger(repository_id: str, candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": _LEDGER_SCHEMA_VERSION,
        "repository_id": repository_id,
        "milestone_id": candidate["milestone_id"],
        "prompt_path": candidate["prompt_path"],
        "events": [],
    }


def _open_question_ids(events: list[dict[str, Any]]) -> set[str]:
    questions = {
        event["event_id"]
        for event in events
        if event["event_type"] == "question_submitted"
    }
    answered = {
        event["related_event_id"]
        for event in events
        if event["event_type"] == "answer_recorded"
    }
    return questions - answered


def _open_bug_ids(events: list[dict[str, Any]]) -> set[str]:
    discovered = {
        event["event_id"]
        for event in events
        if event["event_type"] == "bug_discovered"
    }
    classified = {
        event["related_event_id"]
        for event in events
        if event["event_type"] == "bug_classified"
    }
    return discovered - classified


def _unfixed_bug_ids(events: list[dict[str, Any]]) -> set[str]:
    in_scope = {
        event["related_event_id"]
        for event in events
        if event["event_type"] == "bug_classified" and event.get("bug_scope") == "in_scope"
    }
    fixed = {
        event["related_event_id"]
        for event in events
        if event["event_type"] == "bug_fixed"
    }
    return in_scope - fixed


def _apply_event(
    phase: str,
    waiting_category: str | None,
    waiting_reason: str | None,
    event: dict[str, Any],
    previous_events: list[dict[str, Any]],
) -> tuple[str, str | None, str | None]:
    event_type = event["event_type"]
    summary = event["summary"]
    po_approved = event["po_approved"]
    actor_role = event["actor_role"]
    if phase not in _PHASES:
        raise WorkflowRecordError("Workflow record contains an unknown lifecycle phase.")
    if event_type not in _EVENT_TYPES:
        raise WorkflowRecordError(f"Unsupported workflow event type: {event_type}")

    if phase == "CLOSED" and event_type != "milestone_reopened":
        raise WorkflowRecordError("A closed milestone cannot accept additional workflow events.")
    if event_type in {
        "prompt_approved",
        "lock_in_approved",
        "acceptance_recorded",
        "milestone_closed",
        "milestone_reopened",
    }:
        if not po_approved or actor_role != "product_owner":
            raise WorkflowRecordError(
                f"{event_type} requires explicit Product Owner confirmation in the UI."
            )

    if event_type == "milestone_reopened":
        target_phase = event.get("reopen_target_phase")
        if phase != "CLOSED":
            raise WorkflowRecordError("Only a closed milestone can be reopened.")
        if not isinstance(target_phase, str) or target_phase not in {"REVIEW", "IN_PROGRESS"}:
            raise WorkflowRecordError("A reopened milestone must target REVIEW or IN_PROGRESS.")
        if not summary.strip() or len(summary) > 1000:
            raise WorkflowRecordError("A reopen reason is required and must not exceed 1000 characters.")
        return target_phase, None, None
    if event_type == "prompt_approved":
        if phase != "PLANNING":
            raise WorkflowRecordError("Prompt approval is valid only while the milestone is PLANNING.")
        return "READY", None, None
    if event_type == "implementation_started":
        if phase != "READY":
            raise WorkflowRecordError("Implementation can start only from READY.")
        return "IN_PROGRESS", None, None
    if event_type == "question_submitted":
        if phase != "IN_PROGRESS":
            raise WorkflowRecordError("A new implementation question can be submitted only while IN_PROGRESS.")
        reason = event.get("waiting_reason")
        category = event.get("waiting_category")
        if reason:
            if category not in _WAITING_CATEGORIES:
                raise WorkflowRecordError("WAITING requires a supported waiting category.")
            return "WAITING", category, reason
        if category:
            raise WorkflowRecordError("A waiting category requires a non-empty waiting reason.")
        return phase, waiting_category, waiting_reason
    if event_type == "answer_recorded":
        if phase not in {"IN_PROGRESS", "WAITING"}:
            raise WorkflowRecordError("An answer can be recorded only while work is active or waiting.")
        if event.get("related_event_id") not in _open_question_ids(previous_events):
            raise WorkflowRecordError("An answer must reference a currently open question.")
        return phase, waiting_category, waiting_reason
    if event_type == "decision_recorded":
        if phase not in {"PLANNING", "READY", "IN_PROGRESS", "WAITING", "REVIEW"}:
            raise WorkflowRecordError("A decision cannot be recorded in the current phase.")
        if event.get("binding") is not False:
            raise WorkflowRecordError("Binding decisions must use the Product Owner-approved lock-in event.")
        return phase, waiting_category, waiting_reason
    if event_type == "lock_in_approved":
        if phase not in {"PLANNING", "READY", "IN_PROGRESS", "WAITING", "REVIEW"}:
            raise WorkflowRecordError("A lock-in cannot be approved in the current phase.")
        if event.get("binding") is not True:
            raise WorkflowRecordError("An approved lock-in must be marked as binding.")
        return phase, waiting_category, waiting_reason
    if event_type == "escalation_opened":
        if phase != "IN_PROGRESS":
            raise WorkflowRecordError("An escalation can be opened only while IN_PROGRESS.")
        reason = event.get("waiting_reason")
        if not reason or event.get("waiting_category") != "escalation":
            raise WorkflowRecordError("Opening an escalation requires a reason and the escalation category.")
        return "WAITING", "escalation", reason
    if event_type == "escalation_resolved":
        if phase != "WAITING" or waiting_category != "escalation":
            raise WorkflowRecordError("Only an open escalation can be resolved.")
        return phase, waiting_category, waiting_reason
    if event_type == "bug_discovered":
        if phase not in {"IN_PROGRESS", "WAITING"}:
            raise WorkflowRecordError("A bug can be discovered only while work is active or waiting.")
        return phase, waiting_category, waiting_reason
    if event_type == "bug_classified":
        related_id = event.get("related_event_id")
        if related_id not in _open_bug_ids(previous_events):
            raise WorkflowRecordError("Bug classification must reference an unclassified discovered bug.")
        scope = event.get("bug_scope")
        if scope == "in_scope":
            return phase, waiting_category, waiting_reason
        if scope == "needs_escalation":
            reason = event.get("waiting_reason")
            if phase != "IN_PROGRESS" or not reason:
                raise WorkflowRecordError("An out-of-scope bug requires an escalation reason while IN_PROGRESS.")
            return "WAITING", "escalation", reason
        raise WorkflowRecordError("Bug classification must be in_scope or needs_escalation.")
    if event_type == "bug_fixed":
        if event.get("related_event_id") not in _unfixed_bug_ids(previous_events):
            raise WorkflowRecordError("A bug fix must reference a classified in-scope bug that is not yet fixed.")
        if phase not in {"IN_PROGRESS", "WAITING"}:
            raise WorkflowRecordError("A bug can be fixed only while work is active or waiting.")
        return phase, waiting_category, waiting_reason
    if event_type == "test_result_recorded":
        if phase not in {"IN_PROGRESS", "WAITING", "REVIEW"}:
            raise WorkflowRecordError("Test results can be recorded only during implementation or review.")
        if not event.get("evidence_source") or not event.get("result"):
            raise WorkflowRecordError("A test result requires its source and outcome.")
        return phase, waiting_category, waiting_reason
    if event_type == "implementation_resumed":
        if phase != "WAITING":
            raise WorkflowRecordError("Implementation can resume only from WAITING.")
        last_wait_index = max(
            index
            for index, previous in enumerate(previous_events)
            if previous["phase_after"] == "WAITING" and previous["phase_before"] != "WAITING"
        )
        resolutions = {"answer_recorded", "decision_recorded", "lock_in_approved", "escalation_resolved", "bug_fixed"}
        if not any(previous["event_type"] in resolutions for previous in previous_events[last_wait_index + 1 :]):
            raise WorkflowRecordError("A valid answer, decision, or resolution is required before resuming.")
        return "IN_PROGRESS", None, None
    if event_type == "closeout_submitted":
        if phase != "IN_PROGRESS":
            raise WorkflowRecordError("A closeout can be submitted only while IN_PROGRESS.")
        if not event.get("evidence_path"):
            raise WorkflowRecordError("Closeout submission requires a currently discovered closeout artifact.")
        return "REVIEW", None, None
    if event_type == "review_requested":
        if phase != "REVIEW":
            raise WorkflowRecordError("A review can be requested only while in REVIEW.")
        return phase, waiting_category, waiting_reason
    if event_type == "correction_requested":
        if phase != "REVIEW":
            raise WorkflowRecordError("A bounded correction can be requested only from REVIEW.")
        return "IN_PROGRESS", None, None
    if event_type == "acceptance_recorded":
        if phase != "REVIEW":
            raise WorkflowRecordError("Acceptance can be recorded only from REVIEW.")
        if event.get("binding") is not True:
            raise WorkflowRecordError("Product Owner acceptance must be marked as binding.")
        return phase, waiting_category, waiting_reason
    if event_type == "milestone_closed":
        if phase != "REVIEW":
            raise WorkflowRecordError("A milestone can be closed only from REVIEW.")
        if event.get("binding") is not True:
            raise WorkflowRecordError("Milestone closure must be marked as binding.")
        if not previous_events or previous_events[-1]["event_type"] != "acceptance_recorded":
            raise WorkflowRecordError("Milestone closure requires a preceding explicit acceptance event.")
        return "CLOSED", None, None
    if event_type == "milestone_reopened":
        raise WorkflowRecordError("Only a closed milestone can be reopened.")
    if event_type == "git_disposition_recorded":
        return phase, waiting_category, waiting_reason
    raise WorkflowRecordError(f"Unsupported workflow event type: {event_type}")


def _validate_event_shape(event: Any) -> None:
    if not isinstance(event, dict):
        raise WorkflowRecordError("Workflow record contains an invalid event shape.")
    required_strings = ("event_id", "timestamp", "event_type", "summary", "actor_role", "actor_attribution")
    if any(not isinstance(event.get(key), str) or not event[key] for key in required_strings):
        raise WorkflowRecordError("Workflow record contains an event with missing required text.")
    if event["event_type"] not in _EVENT_TYPES:
        raise WorkflowRecordError("Workflow record contains an unsupported event type.")
    if len(event["event_id"]) > 128 or len(event["timestamp"]) > 64:
        raise WorkflowRecordError("Workflow record contains event metadata above the supported size limit.")
    if event["actor_role"] not in _ACTOR_ROLES:
        raise WorkflowRecordError("Workflow record contains an unsupported actor role.")
    if len(event["summary"]) > 2000 or len(event["actor_attribution"]) > 500:
        raise WorkflowRecordError("Workflow record contains event text above the supported size limit.")
    if not isinstance(event.get("po_approved"), bool):
        raise WorkflowRecordError("Workflow record contains an invalid approval marker.")
    for field in ("phase_before", "phase_after"):
        if not isinstance(event.get(field), str) or event[field] not in _PHASES:
            raise WorkflowRecordError("Workflow record contains an invalid lifecycle phase.")
    for field in ("waiting_category_after", "waiting_category"):
        value = event.get(field)
        if value is not None and (
            not isinstance(value, str) or value not in _WAITING_CATEGORIES
        ):
            raise WorkflowRecordError("Workflow record contains an invalid waiting category.")
    for field in ("waiting_reason_after", "waiting_reason", "evidence_source", "result", "evidence_path"):
        value = event.get(field)
        if value is not None and not isinstance(value, str):
            raise WorkflowRecordError(f"Workflow record contains invalid {field} text.")
    for field in ("related_event_id", "bug_scope"):
        value = event.get(field)
        if value is not None and not isinstance(value, str):
            raise WorkflowRecordError(f"Workflow record contains invalid {field} metadata.")
    if (
        event.get("bug_scope") is not None
        and event["bug_scope"] not in {"in_scope", "needs_escalation"}
    ):
        raise WorkflowRecordError("Workflow record contains an invalid bug classification.")
    reopen_target = event.get("reopen_target_phase")
    if reopen_target is not None and (
        not isinstance(reopen_target, str) or reopen_target not in {"REVIEW", "IN_PROGRESS"}
    ):
        raise WorkflowRecordError("Workflow record contains an invalid reopen target phase.")
    closure_override_reason = event.get("closure_override_reason")
    if closure_override_reason is not None and (
        not isinstance(closure_override_reason, str)
        or not closure_override_reason.strip()
        or len(closure_override_reason) > 1000
    ):
        raise WorkflowRecordError("Workflow record contains an invalid closure override reason.")
    dispatch_ids = event.get("outstanding_dispatch_ids")
    if dispatch_ids is not None and (
        not isinstance(dispatch_ids, list)
        or not 1 <= len(dispatch_ids) <= 100
        or any(not isinstance(item, str) or not _DISPATCH_ID_RE.fullmatch(item) for item in dispatch_ids)
        or len(set(dispatch_ids)) != len(dispatch_ids)
    ):
        raise WorkflowRecordError("Workflow record contains invalid outstanding dispatch identifiers.")
    if event["event_type"] == "milestone_reopened":
        if reopen_target is None:
            raise WorkflowRecordError("A reopen event requires a target phase.")
    elif reopen_target is not None:
        raise WorkflowRecordError("Only a reopen event may contain a reopen target phase.")
    if event["event_type"] == "milestone_closed":
        if (closure_override_reason is None) != (dispatch_ids is None):
            raise WorkflowRecordError("A closure override requires both a reason and dispatch identifiers.")
    elif closure_override_reason is not None or dispatch_ids is not None:
        raise WorkflowRecordError("Closure override evidence is valid only on a milestone closure event.")
    text_limits = {
        "waiting_reason_after": 1000,
        "waiting_reason": 1000,
        "evidence_source": 500,
        "result": 1000,
        "evidence_path": 1000,
        "related_event_id": 128,
    }
    if any(
        event.get(field) is not None and len(event[field]) > limit
        for field, limit in text_limits.items()
    ):
        raise WorkflowRecordError("Workflow record contains metadata above the supported size limit.")
    if event.get("binding") is not None and not isinstance(event["binding"], bool):
        raise WorkflowRecordError("Workflow record contains an invalid binding marker.")
    try:
        parsed_timestamp = datetime.fromisoformat(event["timestamp"])
    except ValueError as exc:
        raise WorkflowRecordError("Workflow record contains an invalid event timestamp.") from exc
    if parsed_timestamp.utcoffset() is None:
        raise WorkflowRecordError("Workflow event timestamps must include a UTC offset.")


def _reduce_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    phase = "PLANNING"
    waiting_category: str | None = None
    waiting_reason: str | None = None
    seen_ids: set[str] = set()
    previous_timestamp: datetime | None = None
    for index, event in enumerate(events):
        _validate_event_shape(event)
        if event["event_id"] in seen_ids:
            raise WorkflowRecordError("Workflow record contains duplicate event IDs.")
        seen_ids.add(event["event_id"])
        timestamp = datetime.fromisoformat(event["timestamp"])
        if previous_timestamp is not None and timestamp < previous_timestamp:
            raise WorkflowRecordError("Workflow events are not stored in chronological order.")
        previous_timestamp = timestamp
        if event.get("phase_before") != phase:
            raise WorkflowRecordError("Workflow event chronology has an inconsistent starting phase.")
        next_phase, next_category, next_reason = _apply_event(
            phase,
            waiting_category,
            waiting_reason,
            event,
            events[:index],
        )
        if (
            event.get("phase_after") != next_phase
            or event.get("waiting_category_after") != next_category
            or event.get("waiting_reason_after") != next_reason
        ):
            raise WorkflowRecordError("Workflow event chronology has an inconsistent resulting state.")
        phase, waiting_category, waiting_reason = next_phase, next_category, next_reason
    return {
        "phase": phase,
        "waiting_category": waiting_category,
        "waiting_reason": waiting_reason,
    }


def _read_ledger(path: Path, repository_id: str, candidate: dict[str, Any]) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise WorkflowRecordError("Workflow sidecar is not a regular file.")
    try:
        if path.stat().st_size > _MAX_SIDECAR_BYTES:
            raise WorkflowRecordError("Workflow sidecar exceeds the 5 MB safety limit.")
        text = path.read_text(encoding="utf-8")
    except WorkflowRecordError:
        raise
    except (OSError, UnicodeDecodeError) as exc:
        raise WorkflowRecordError(f"Workflow sidecar cannot be read: {exc}") from exc
    if text.count(_SIDECAR_MARKER) != 1:
        raise WorkflowRecordError("Workflow sidecar is missing its unique Repo Control ledger marker.")
    marker_index = text.index(_SIDECAR_MARKER) + len(_SIDECAR_MARKER)
    match = re.search(r"(?m)^(`{3,})json[ \t]*\n(.*?)\n\1[ \t]*$", text[marker_index:], re.DOTALL)
    if match is None:
        raise WorkflowRecordError("Workflow sidecar has no valid JSON ledger block.")
    try:
        ledger = json.loads(match.group(2))
    except json.JSONDecodeError as exc:
        raise WorkflowRecordError(f"Workflow sidecar JSON ledger is malformed: {exc}") from exc
    if (
        not isinstance(ledger, dict)
        or ledger.get("schema_version") != _LEDGER_SCHEMA_VERSION
        or ledger.get("repository_id") != repository_id
        or ledger.get("milestone_id") != candidate["milestone_id"]
        or ledger.get("prompt_path") != candidate["prompt_path"]
        or not isinstance(ledger.get("events"), list)
    ):
        raise WorkflowRecordError("Workflow sidecar metadata does not match the selected repository and milestone.")
    if len(ledger["events"]) > _MAX_EVENTS:
        raise WorkflowRecordError("Workflow sidecar exceeds the 2000-event safety limit.")
    for event in ledger["events"]:
        if (
            isinstance(event, dict)
            and event.get("event_type") == "closeout_submitted"
            and (
                not candidate.get("closeout_exists")
                or event.get("evidence_path") != candidate.get("closeout_path")
            )
        ):
            raise WorkflowRecordError("Workflow sidecar references a missing or mismatched closeout artifact.")
    ledger["derived_state"] = _reduce_events(ledger["events"])
    return ledger


def _markdown_safe(value: str) -> str:
    escaped_lines = [
        escape(line, quote=False).replace("`", "&#96;")
        for line in value.splitlines() or [""]
    ]
    return "\n> ".join(escaped_lines)


def _render_sidecar(ledger: dict[str, Any]) -> str:
    state = _reduce_events(ledger["events"])
    lines = [
        f"# Workflow record - {ledger['milestone_id']}",
        "",
        "> Tracked milestone-scoped event history. This sidecar is not the prompt, closeout, or active selection anchor.",
        "",
        "## Current derived lifecycle state",
        "",
        f"- Phase: **{state['phase']}**",
    ]
    if state["phase"] == "WAITING":
        lines.extend(
            [
                f"- Waiting category: **{state['waiting_category']}**",
                f"- Waiting reason: {_markdown_safe(state['waiting_reason'] or '')}",
            ]
        )
    lines.extend(["", "## Chronological events", ""])
    if not ledger["events"]:
        lines.extend(["No durable workflow events have been recorded.", ""])
    for event in ledger["events"]:
        lines.extend(
            [
                f"### {event['timestamp']} — {event['event_type']}",
                "",
                f"- Reported actor context: `{event['actor_role']}`",
                f"- Attribution: {_markdown_safe(event['actor_attribution'])}",
                f"- Summary: {_markdown_safe(event['summary'])}",
            ]
        )
        if event.get("binding"):
            lines.append("- Binding decision: Product Owner approval explicitly confirmed in UI.")
        elif event.get("po_approved"):
            lines.append("- Product Owner approval explicitly confirmed in UI; identity is not authenticated.")
        if event.get("evidence_source"):
            lines.append(f"- Evidence source: {_markdown_safe(event['evidence_source'])}")
        if event.get("result"):
            lines.append(f"- Result: {_markdown_safe(event['result'])}")
        if event.get("evidence_path"):
            lines.append(f"- Supporting artifact: `{_markdown_safe(event['evidence_path'])}`")
        if event.get("reopen_target_phase"):
            lines.append(f"- Reopened to: **{event['reopen_target_phase']}**")
        if event.get("event_type") == "milestone_reopened":
            lines.append("- Prior acceptance and closure events remain unchanged in this history.")
        if event.get("closure_override_reason"):
            lines.append(f"- Closure guard override reason: {_markdown_safe(event['closure_override_reason'])}")
            lines.append(
                "- Outstanding Architect dispatches explicitly acknowledged: "
                + ", ".join(f"`{dispatch_id}`" for dispatch_id in event["outstanding_dispatch_ids"])
            )
        lines.append("")
    machine_json = json.dumps(
        {key: value for key, value in ledger.items() if key != "derived_state"},
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    longest_backtick_run = max((len(run) for run in re.findall(r"`+", machine_json)), default=0)
    fence = "`" * max(3, longest_backtick_run + 1)
    lines.extend(
        [
            "## Machine-readable event ledger",
            "",
            _SIDECAR_MARKER,
            f"{fence}json",
            machine_json,
            fence,
            "",
        ]
    )
    return "\n".join(lines)


def _next_action(state: dict[str, Any], candidate: dict[str, Any], open_questions: list[dict[str, Any]]) -> str:
    phase = state["phase"]
    if phase == "PLANNING":
        return "Product Owner: review the prompt and lock-ins, then use Approve & Start."
    if phase == "READY":
        return "Product Owner/Coder: explicitly start the implementation handoff."
    if phase == "WAITING":
        return "Product Owner: resolve the active blocking condition and resume implementation."
    if phase == "IN_PROGRESS":
        if open_questions:
            return "Record answers to open questions; submit the closeout for review only when ready."
        if candidate["closeout_exists"]:
            return "Coder: continue work or explicitly submit the available closeout for REVIEW."
        return "Coder: continue implementation/validation and record only meaningful durable events."
    if phase == "REVIEW":
        return "Product Owner: explicitly accept and close, or request a bounded correction."
    return "Milestone is closed; the Product Owner may reopen it to REVIEW or IN_PROGRESS with a reason."


def _load_milestone_workflow_locked(
    repository_root: str | Path,
    state_root: str | Path,
    milestone_root: str,
    candidate: dict[str, Any],
) -> dict[str, Any]:
    root = Path(repository_root).resolve(strict=True)
    repository_id = make_repository_id(root)
    selection = load_milestone_selection(root, state_root, milestone_root)
    if (
        selection["status"] != "selected"
        or selection["selection"]["milestone_id"] != candidate["milestone_id"]
        or selection["selection"]["prompt_path"] != candidate["prompt_path"]
    ):
        raise WorkflowRecordError("The selected milestone changed or is invalid; refresh and select again.")
    _, milestone_dir = _root_paths(root, milestone_root)
    sidecar_relative_path = _sidecar_path(candidate)
    sidecar_path = root / Path(*PurePosixPath(sidecar_relative_path).parts)
    if not sidecar_path.exists() and not sidecar_path.is_symlink():
        ledger = _empty_ledger(repository_id, candidate)
    else:
        try:
            _contained_file(root, milestone_dir, sidecar_relative_path)
        except MilestoneArtifactError as exc:
            raise WorkflowRecordError(f"Workflow sidecar path is invalid: {exc}") from exc
        ledger = _read_ledger(sidecar_path, repository_id, candidate)
    state = ledger.get("derived_state") or _reduce_events(ledger["events"])
    questions_by_id = {
        event["event_id"]: event
        for event in ledger["events"]
        if event["event_type"] == "question_submitted"
    }
    answered_ids = {
        event.get("related_event_id")
        for event in ledger["events"]
        if event["event_type"] == "answer_recorded"
    }
    open_questions = [event for event_id, event in questions_by_id.items() if event_id not in answered_ids]
    bugs_by_id = {
        event["event_id"]: event
        for event in ledger["events"]
        if event["event_type"] == "bug_discovered"
    }
    open_bug_ids = _open_bug_ids(ledger["events"])
    fixable_bug_ids = _unfixed_bug_ids(ledger["events"])
    open_bugs = [event for event_id, event in bugs_by_id.items() if event_id in open_bug_ids]
    fixable_bugs = [event for event_id, event in bugs_by_id.items() if event_id in fixable_bug_ids]
    last_authoritative = next(
        (
            event
            for event in reversed(ledger["events"])
            if event["event_type"] in {"prompt_approved", "lock_in_approved", "acceptance_recorded"}
            and event["po_approved"]
        ),
        None,
    )
    state["open_questions"] = open_questions
    state["open_bugs"] = open_bugs
    state["fixable_bugs"] = fixable_bugs
    state["open_blocker"] = state["phase"] == "WAITING"
    state["latest_events"] = ledger["events"][-8:][::-1]
    state["event_history"] = ledger["events"]
    state["latest_authoritative_decision"] = last_authoritative
    state["next_action"] = _next_action(state, candidate, open_questions)
    state["sidecar_path"] = sidecar_relative_path
    state["prompt_identity"] = prompt_identity(root, milestone_root, candidate)
    state["event_count"] = len(ledger["events"])
    state["status"] = "valid"
    state["events"] = EVENT_OPTIONS
    state["actors"] = ACTOR_OPTIONS
    state["waiting_categories"] = tuple(sorted(_WAITING_CATEGORIES))
    return state


def load_milestone_workflow(
    repository_root: str | Path,
    state_root: str | Path,
    milestone_root: str,
    candidate: dict[str, Any],
) -> dict[str, Any]:
    with STATE_ARTIFACT_LOCK:
        return _load_milestone_workflow_locked(
            repository_root,
            state_root,
            milestone_root,
            candidate,
        )


def _record_milestone_event_locked(
    repository_root: str | Path,
    state_root: str | Path,
    milestone_root: str,
    candidate: dict[str, Any],
    *,
    event_type: str,
    summary: str,
    actor_role: str,
    po_approved: bool = False,
    waiting_category: str | None = None,
    waiting_reason: str | None = None,
    related_event_id: str | None = None,
    bug_scope: str | None = None,
    evidence_source: str | None = None,
    result: str | None = None,
    reopen_target_phase: str | None = None,
    closure_override_reason: str | None = None,
    outstanding_dispatch_ids: tuple[str, ...] | None = None,
    following_events: tuple[tuple[str, str], ...] = (),
) -> dict[str, Any]:
    summary = summary.strip()
    if not summary or len(summary) > 2000:
        raise WorkflowRecordError("Event summary is required and must not exceed 2000 characters.")
    if any(ord(char) < 32 and char not in "\n\t" for char in summary):
        raise WorkflowRecordError("Event summary contains unsupported control characters.")
    if actor_role not in _ACTOR_ROLES:
        raise WorkflowRecordError("Select a supported reported actor context.")
    if event_type in {
        "prompt_approved",
        "lock_in_approved",
        "acceptance_recorded",
        "milestone_closed",
        "milestone_reopened",
    }:
        if actor_role != "product_owner" or not po_approved:
            raise WorkflowRecordError("Select Product Owner and explicitly confirm this approval action.")
    if event_type == "milestone_reopened":
        if (
            not isinstance(reopen_target_phase, str)
            or reopen_target_phase not in {"REVIEW", "IN_PROGRESS"}
        ):
            raise WorkflowRecordError("Choose REVIEW or IN_PROGRESS as the reopen target.")
        summary = summary.strip()
        if not summary or len(summary) > 1000:
            raise WorkflowRecordError("A reopen reason is required and must not exceed 1000 characters.")
    elif reopen_target_phase is not None:
        raise WorkflowRecordError("Only a reopen event may specify a reopen target phase.")
    if event_type == "milestone_closed":
        if (closure_override_reason is None) != (outstanding_dispatch_ids is None):
            raise WorkflowRecordError("A closure override requires both a reason and dispatch identifiers.")
        if closure_override_reason is not None:
            closure_override_reason = closure_override_reason.strip()
            if not closure_override_reason or len(closure_override_reason) > 1000:
                raise WorkflowRecordError("A closure override reason is required and must not exceed 1000 characters.")
            if (
                not isinstance(outstanding_dispatch_ids, tuple)
                or not outstanding_dispatch_ids
                or len(outstanding_dispatch_ids) > 100
                or any(
                    not isinstance(item, str) or not _DISPATCH_ID_RE.fullmatch(item)
                    for item in outstanding_dispatch_ids
                )
                or len(set(outstanding_dispatch_ids)) != len(outstanding_dispatch_ids)
            ):
                raise WorkflowRecordError("Closure override requires valid outstanding dispatch identifiers.")
    elif closure_override_reason is not None or outstanding_dispatch_ids is not None:
        raise WorkflowRecordError("Closure override evidence is valid only when closing a milestone.")
    if waiting_reason is not None:
        waiting_reason = waiting_reason.strip() or None
        if waiting_reason and len(waiting_reason) > 1000:
            raise WorkflowRecordError("Waiting reason must not exceed 1000 characters.")
    if evidence_source is not None:
        evidence_source = evidence_source.strip() or None
        if evidence_source and len(evidence_source) > 500:
            raise WorkflowRecordError("Evidence source must not exceed 500 characters.")
    if result is not None:
        result = result.strip() or None
        if result and len(result) > 1000:
            raise WorkflowRecordError("Event result must not exceed 1000 characters.")

    root = Path(repository_root).resolve(strict=True)
    repository_id = make_repository_id(root)
    selection = load_milestone_selection(root, state_root, milestone_root)
    if (
        selection["status"] != "selected"
        or selection["selection"]["milestone_id"] != candidate["milestone_id"]
        or selection["selection"]["prompt_path"] != candidate["prompt_path"]
    ):
        raise WorkflowRecordError("The selected milestone changed or is invalid; refresh and retry.")
    inventory = discover_milestones(root, milestone_root)
    current_candidate = next(
        (
            item for item in inventory["candidates"]
            if item["milestone_id"] == candidate["milestone_id"]
            and item["prompt_path"] == candidate["prompt_path"]
        ),
        None,
    )
    if current_candidate is None:
        raise WorkflowRecordError("The selected prompt is no longer a valid milestone candidate.")
    if event_type == "closeout_submitted" and not current_candidate["closeout_exists"]:
        raise WorkflowRecordError("A currently discovered closeout artifact is required before review submission.")

    _, milestone_dir = _root_paths(root, milestone_root)
    relative_sidecar = _sidecar_path(current_candidate)
    sidecar_path = root / Path(*PurePosixPath(relative_sidecar).parts)
    with STATE_ARTIFACT_LOCK:
        if sidecar_path.exists() or sidecar_path.is_symlink():
            try:
                _contained_file(root, milestone_dir, relative_sidecar)
            except MilestoneArtifactError as exc:
                raise WorkflowRecordError(f"Workflow sidecar path is invalid: {exc}") from exc
            ledger = _read_ledger(sidecar_path, repository_id, current_candidate)
        else:
            ledger = _empty_ledger(repository_id, current_candidate)
        events = list(ledger["events"])
        state = _reduce_events(events)
        additions: list[dict[str, Any]] = []
        if event_type == "milestone_closed":
            latest_closeout = next(
                (
                    event for event in reversed(events)
                    if event["event_type"] == "closeout_submitted"
                ),
                None,
            )
            outstanding = (
                list_outstanding_guidance(
                    root,
                    state_root,
                    repository_id,
                    current_candidate["milestone_id"],
                    latest_closeout["timestamp"],
                )
                if latest_closeout is not None
                else []
            )
            current_dispatch_ids = tuple(item["dispatch_id"] for item in outstanding)
            if current_dispatch_ids:
                if (
                    not closure_override_reason
                    or tuple(outstanding_dispatch_ids or ()) != current_dispatch_ids
                ):
                    raise WorkflowRecordError(
                        "Approved applicable Architect guidance is newer than the latest closeout; "
                        "review and explicitly confirm the current dispatches with a reason."
                    )
            elif closure_override_reason is not None or outstanding_dispatch_ids is not None:
                raise WorkflowRecordError(
                    "The outstanding-guidance condition changed; refresh before confirming closure."
                )
        if event_type == "milestone_closed":
            events_with_acceptance = events
            if not events or events[-1]["event_type"] != "acceptance_recorded":
                acceptance = _new_event(
                    event_type="acceptance_recorded",
                    summary=summary,
                    actor_role=actor_role,
                    po_approved=po_approved,
                    phase=state["phase"],
                    waiting_category=state["waiting_category"],
                    waiting_reason=state["waiting_reason"],
                    event_fields={"binding": True},
                )
                acceptance_phase, acceptance_category, acceptance_reason = _apply_event(
                    state["phase"],
                    state["waiting_category"],
                    state["waiting_reason"],
                    acceptance,
                    events,
                )
                acceptance.update(
                    phase_after=acceptance_phase,
                    waiting_category_after=acceptance_category,
                    waiting_reason_after=acceptance_reason,
                )
                additions.append(acceptance)
                events_with_acceptance = events + additions
                state = {
                    "phase": acceptance_phase,
                    "waiting_category": acceptance_category,
                    "waiting_reason": acceptance_reason,
                }
            event = _new_event(
                event_type="milestone_closed",
                summary=summary,
                actor_role=actor_role,
                po_approved=po_approved,
                phase=state["phase"],
                waiting_category=state["waiting_category"],
                waiting_reason=state["waiting_reason"],
                event_fields={
                    "binding": True,
                    **(
                        {
                            "closure_override_reason": closure_override_reason,
                            "outstanding_dispatch_ids": list(outstanding_dispatch_ids or ()),
                        }
                        if closure_override_reason is not None
                        else {}
                    ),
                },
            )
            phase_after, category_after, reason_after = _apply_event(
                state["phase"],
                state["waiting_category"],
                state["waiting_reason"],
                event,
                events_with_acceptance,
            )
        else:
            event_fields: dict[str, Any] = {}
            if event_type == "question_submitted":
                if waiting_category:
                    event_fields["waiting_category"] = waiting_category
                if waiting_reason:
                    event_fields["waiting_reason"] = waiting_reason
            if event_type == "escalation_opened":
                event_fields.update(waiting_category="escalation", waiting_reason=waiting_reason)
            if event_type == "bug_classified":
                event_fields.update(bug_scope=bug_scope, related_event_id=related_event_id)
                if bug_scope == "needs_escalation":
                    event_fields.update(
                        waiting_category="escalation",
                        waiting_reason=waiting_reason,
                    )
            if event_type in {"answer_recorded", "bug_fixed"}:
                event_fields["related_event_id"] = related_event_id
            if event_type == "decision_recorded":
                event_fields["binding"] = False
            if event_type in {"lock_in_approved", "acceptance_recorded"}:
                event_fields["binding"] = True
            if event_type == "test_result_recorded":
                event_fields.update(evidence_source=evidence_source, result=result)
            if event_type == "closeout_submitted":
                event_fields["evidence_path"] = current_candidate["closeout_path"]
            if event_type == "milestone_reopened":
                event_fields["reopen_target_phase"] = reopen_target_phase
            event = _new_event(
                event_type=event_type,
                summary=summary,
                actor_role=actor_role,
                po_approved=po_approved,
                phase=state["phase"],
                waiting_category=state["waiting_category"],
                waiting_reason=state["waiting_reason"],
                event_fields=event_fields,
            )
            phase_after, category_after, reason_after = _apply_event(
                state["phase"],
                state["waiting_category"],
                state["waiting_reason"],
                event,
                events,
            )
        event.update(
            phase_after=phase_after,
            waiting_category_after=category_after,
            waiting_reason_after=reason_after,
        )
        additions.append(event)
        for following_type, following_summary in following_events:
            following_summary = following_summary.strip()
            if not following_summary or any(
                ord(char) < 32 and char not in "\n\t" for char in following_summary
            ):
                raise WorkflowRecordError("Event summary is required and must contain supported text.")
            following = _new_event(
                event_type=following_type,
                summary=following_summary,
                actor_role=actor_role,
                po_approved=po_approved,
                phase=phase_after,
                waiting_category=category_after,
                waiting_reason=reason_after,
                event_fields={"binding": True} if following_type == "lock_in_approved" else {},
            )
            phase_after, category_after, reason_after = _apply_event(
                phase_after, category_after, reason_after, following, events + additions,
            )
            following.update(
                phase_after=phase_after,
                waiting_category_after=category_after,
                waiting_reason_after=reason_after,
            )
            _validate_event_shape(following)
            additions.append(following)
        if len(events) + len(additions) > _MAX_EVENTS:
            raise WorkflowRecordError("Workflow record exceeds the 2000-event safety limit.")
        ledger["events"] = ledger["events"] + additions
        ledger.pop("derived_state", None)
        ledger["derived_state"] = _reduce_events(ledger["events"])
        _write_ledger(sidecar_path, _render_sidecar(ledger))
    return load_milestone_workflow(root, state_root, milestone_root, current_candidate)


def waiting_resolution(workflow: dict[str, Any]) -> tuple[str, str | None]:
    """Identify the active blocking condition, not an unrelated open question."""
    if workflow["phase"] != "WAITING":
        raise WorkflowRecordError("Resolve & Resume is valid only from WAITING.")
    entry = next(
        event for event in reversed(workflow["event_history"])
        if event["phase_after"] == "WAITING" and event["phase_before"] != "WAITING"
    )
    if entry["event_type"] == "question_submitted":
        if entry["event_id"] not in {event["event_id"] for event in workflow["open_questions"]}:
            raise WorkflowRecordError("The blocking question is already answered; use Resume implementation.")
        return "answer_recorded", entry["event_id"]
    if workflow["waiting_category"] == "escalation":
        return "escalation_resolved", None
    raise WorkflowRecordError("The active waiting condition has no supported combined resolution.")


def record_milestone_action(
    repository_root: str | Path,
    state_root: str | Path,
    milestone_root: str,
    candidate: dict[str, Any],
    *,
    action: str,
    summary: str,
    expected_phase: str,
    expected_event_count: int,
    related_event_id: str | None = None,
    lock_in_summary: str | None = None,
    closure_override_reason: str | None = None,
    outstanding_dispatch_ids: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Validate the entire browser action before one atomic sidecar replacement."""
    with STATE_ARTIFACT_LOCK:
        workflow = _load_milestone_workflow_locked(
            repository_root, state_root, milestone_root, candidate,
        )
        if workflow["phase"] != expected_phase or workflow["event_count"] != expected_event_count:
            raise WorkflowRecordError("Workflow state changed; refresh before submitting this action.")
        following: tuple[tuple[str, str], ...] = ()
        if action == "approve_and_start":
            event_type = "prompt_approved"
            following = (("implementation_started", f"Start {candidate['milestone_id']} implementation"),)
        elif action == "accept_and_close":
            event_type = "milestone_closed"
        elif action == "resolve_and_resume":
            event_type, blocking_id = waiting_resolution(workflow)
            if related_event_id != blocking_id:
                raise WorkflowRecordError("Resolve & Resume must reference the active blocking question.")
            if lock_in_summary:
                following = (("lock_in_approved", lock_in_summary),)
            following += (("implementation_resumed", f"Resume {candidate['milestone_id']} implementation"),)
        else:
            raise WorkflowRecordError("Unsupported combined milestone action.")
        if lock_in_summary and action != "resolve_and_resume":
            raise WorkflowRecordError("An optional lock-in is supported only by Resolve & Resume.")
        if action != "accept_and_close" and (
            closure_override_reason is not None or outstanding_dispatch_ids is not None
        ):
            raise WorkflowRecordError("Closure override evidence is valid only for Accept & Close.")
        return _record_milestone_event_locked(
            repository_root, state_root, milestone_root, candidate,
            event_type=event_type,
            summary=summary,
            actor_role="product_owner",
            po_approved=True,
            related_event_id=related_event_id,
            closure_override_reason=closure_override_reason,
            outstanding_dispatch_ids=outstanding_dispatch_ids,
            following_events=following,
        )


def record_milestone_reopen(
    repository_root: str | Path,
    state_root: str | Path,
    milestone_root: str,
    candidate: dict[str, Any],
    *,
    reason: str,
    target_phase: str,
    expected_event_count: int,
) -> dict[str, Any]:
    reason = reason.strip() if isinstance(reason, str) else ""
    if not reason or len(reason) > 1000:
        raise WorkflowRecordError("A reopen reason is required and must not exceed 1000 characters.")
    if not isinstance(target_phase, str) or target_phase not in {"REVIEW", "IN_PROGRESS"}:
        raise WorkflowRecordError("Choose REVIEW or IN_PROGRESS as the reopen target.")
    with STATE_ARTIFACT_LOCK:
        workflow = _load_milestone_workflow_locked(
            repository_root, state_root, milestone_root, candidate,
        )
        if workflow["phase"] != "CLOSED" or workflow["event_count"] != expected_event_count:
            raise WorkflowRecordError("The milestone is no longer CLOSED or its history changed; refresh and retry.")
        return _record_milestone_event_locked(
            repository_root,
            state_root,
            milestone_root,
            candidate,
            event_type="milestone_reopened",
            summary=reason,
            actor_role="product_owner",
            po_approved=True,
            reopen_target_phase=target_phase,
        )


def record_milestone_event(
    repository_root: str | Path,
    state_root: str | Path,
    milestone_root: str,
    candidate: dict[str, Any],
    *,
    event_type: str,
    summary: str,
    actor_role: str,
    po_approved: bool = False,
    waiting_category: str | None = None,
    waiting_reason: str | None = None,
    related_event_id: str | None = None,
    bug_scope: str | None = None,
    evidence_source: str | None = None,
    result: str | None = None,
) -> dict[str, Any]:
    with STATE_ARTIFACT_LOCK:
        return _record_milestone_event_locked(
            repository_root,
            state_root,
            milestone_root,
            candidate,
            event_type=event_type,
            summary=summary,
            actor_role=actor_role,
            po_approved=po_approved,
            waiting_category=waiting_category,
            waiting_reason=waiting_reason,
            related_event_id=related_event_id,
            bug_scope=bug_scope,
            evidence_source=evidence_source,
            result=result,
        )


def _new_event(
    *,
    event_type: str,
    summary: str,
    actor_role: str,
    po_approved: bool,
    phase: str,
    waiting_category: str | None,
    waiting_reason: str | None,
    event_fields: dict[str, Any],
) -> dict[str, Any]:
    return {
        "event_id": uuid.uuid4().hex,
        "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
        "event_type": event_type,
        "summary": summary,
        "actor_role": actor_role,
        "actor_attribution": "Reported through the local UI; role and identity are not authenticated by Repo Control.",
        "po_approved": po_approved,
        "phase_before": phase,
        "phase_after": phase,
        "waiting_category_after": waiting_category,
        "waiting_reason_after": waiting_reason,
        **event_fields,
    }


def _write_ledger(path: Path, content: str) -> None:
    if len(content.encode("utf-8")) > _MAX_SIDECAR_BYTES:
        raise WorkflowRecordError("Workflow sidecar exceeds the 5 MB safety limit.")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise WorkflowRecordError(f"Could not create workflow sidecar directory: {exc}") from exc
    fd, temporary_name = tempfile.mkstemp(prefix=".workflow-", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    except OSError as exc:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise WorkflowRecordError(f"Could not persist milestone workflow sidecar: {exc}") from exc
