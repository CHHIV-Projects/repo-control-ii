from __future__ import annotations

import re
from datetime import UTC, datetime
from html import escape
from pathlib import Path, PurePosixPath
from typing import Any

from .git_state import WorkflowGitStateError, inspect_git_state
from .lifecycle import WorkflowRecordError, load_milestone_workflow
from .milestones import (
    MilestoneArtifactError,
    _root_paths,
    discover_milestones,
    load_milestone_selection,
)

MAX_PACKET_BYTES = 512_000
MAX_SOURCE_BYTES = 128_000
MAX_GIT_PATHS = 40
_EVENT_SUMMARY_LIMIT = 30
_LEGACY_MILESTONE_PREFIX = "R"
_CODER_REQUIRED_DOCUMENTS = ("coding_agent_rules", "workflow")
_ARCHITECT_OPTIONAL_DOCUMENTS = ("context", "architecture", "workflow")
_PRIVATE_KEY_MARKER = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")


class HandoffPacketError(RuntimeError):
    pass


def _markdown_inline(value: str) -> str:
    flattened = " ".join(value.splitlines())
    return escape(flattened, quote=False).replace("`", "&#96;")


def _read_relative_source(
    repository_root: Path,
    relative_path: str,
    *,
    max_bytes: int = MAX_SOURCE_BYTES,
) -> str:
    relative = PurePosixPath(relative_path)
    if (
        not relative_path
        or relative.is_absolute()
        or "\\" in relative_path
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise HandoffPacketError(f"Unsafe repository-relative source path: {relative_path!r}.")
    source_path = repository_root.joinpath(*relative.parts)
    current = repository_root
    try:
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise HandoffPacketError(f"Source {relative_path} uses a symlinked path component.")
        resolved = source_path.resolve(strict=True)
        if not resolved.is_relative_to(repository_root) or not resolved.is_file():
            raise HandoffPacketError(f"Source {relative_path} is not a regular file inside the repository.")
        if resolved.stat().st_size > max_bytes:
            raise HandoffPacketError(
                f"Source {relative_path} exceeds the {max_bytes}-byte packet source limit; "
                "no content was truncated."
            )
        content = resolved.read_text(encoding="utf-8")
        if _PRIVATE_KEY_MARKER.search(content):
            raise HandoffPacketError(
                f"Source {relative_path} contains private-key material and cannot be included in a handoff packet."
            )
        return content
    except HandoffPacketError:
        raise
    except (OSError, UnicodeDecodeError, RuntimeError) as exc:
        raise HandoffPacketError(f"Cannot read required source {relative_path}: {exc}") from exc


def _required_documents(documents: dict[str, str], roles: tuple[str, ...]) -> None:
    missing = [role for role in roles if not documents.get(role)]
    if missing:
        names = ", ".join(missing)
        raise HandoffPacketError(
            f"Required governing document role(s) are not configured for this repository: {names}."
        )


def _read_configured_document(
    repository_root: Path,
    documents: dict[str, str],
    role: str,
    *,
    required: bool,
) -> tuple[str, str] | None:
    relative_path = documents.get(role)
    if not relative_path:
        if required:
            raise HandoffPacketError(f"Required governing document role {role!r} is not configured.")
        return None
    content = _read_relative_source(repository_root, relative_path)
    return relative_path, content


def _validate_target(
    repository_root: Path,
    state_root: str | Path,
    milestone_root: str,
    candidate: dict[str, Any],
) -> tuple[Path, dict[str, Any]]:
    root = repository_root.resolve(strict=True)
    selection = load_milestone_selection(root, state_root, milestone_root)
    if (
        selection["status"] != "selected"
        or selection["selection"]["milestone_id"] != candidate.get("milestone_id")
        or selection["selection"]["prompt_path"] != candidate.get("prompt_path")
    ):
        raise HandoffPacketError("The selected milestone changed or is invalid; refresh and try again.")
    inventory = discover_milestones(root, milestone_root)
    current = next(
        (
            item
            for item in inventory["candidates"]
            if item["milestone_id"] == candidate.get("milestone_id")
            and item["prompt_path"] == candidate.get("prompt_path")
        ),
        None,
    )
    if current is None:
        raise HandoffPacketError("The selected prompt is no longer a valid milestone candidate.")
    _, milestone_directory = _root_paths(root, milestone_root)
    return root, {**current, "_milestone_directory": milestone_directory}


def _workflow_state(
    repository_root: Path,
    state_root: str | Path,
    milestone_root: str,
    candidate: dict[str, Any],
) -> tuple[dict[str, Any] | None, bool]:
    workflow_relative = PurePosixPath(candidate["prompt_path"]).with_name(
        PurePosixPath(candidate["prompt_path"]).name.removesuffix("_prompt.md") + "_workflow.md"
    ).as_posix()
    workflow_path = repository_root.joinpath(*PurePosixPath(workflow_relative).parts)
    sidecar_exists = workflow_path.exists() or workflow_path.is_symlink()
    if sidecar_exists:
        _read_relative_source(repository_root, workflow_relative, max_bytes=5_000_000)
    if candidate["milestone_id"].startswith(_LEGACY_MILESTONE_PREFIX) and not sidecar_exists:
        return None, True
    try:
        return load_milestone_workflow(repository_root, state_root, milestone_root, candidate), False
    except (MilestoneArtifactError, WorkflowRecordError) as exc:
        raise HandoffPacketError(f"Workflow record is invalid: {exc}") from exc


def _correction_mode(workflow: dict[str, Any] | None, phase: str | None) -> bool:
    if not workflow or phase != "IN_PROGRESS":
        return False
    events = workflow["event_history"]
    correction_indexes = [
        index for index, event in enumerate(events)
        if event["event_type"] == "correction_requested"
    ]
    closeout_indexes = [
        index for index, event in enumerate(events)
        if event["event_type"] == "closeout_submitted"
    ]
    return bool(correction_indexes and correction_indexes[-1] > max(closeout_indexes, default=-1))


def _authority_notice(
    phase: str | None,
    legacy_mode: bool,
    packet_type: str,
    correction_mode: bool,
) -> str:
    if legacy_mode:
        return (
            "LIMITED LEGACY MODE: no workflow sidecar exists; lifecycle phase, approvals, "
            "and decision history are unavailable and have not been inferred."
        )
    notices = {
        "PLANNING": "PLANNING: prompt readiness is not approved; this packet does not authorize implementation.",
        "READY": "READY: implementation handoff is permitted only under the recorded prompt and approvals.",
        "IN_PROGRESS": "IN_PROGRESS: this phase is reported from the durable workflow ledger.",
        "WAITING": "WAITING: implementation is paused; this packet does not authorize work to resume.",
        "REVIEW": (
            "REVIEW: the Coder has no active implementation authorization; the Product Owner/Architect "
            "must accept or request a bounded correction."
            if packet_type == "coder"
            else "REVIEW: this packet presents review evidence and does not imply acceptance."
        ),
        "CLOSED": "CLOSED: the milestone is closed; this packet does not authorize new implementation.",
    }
    if correction_mode:
        return (
            "IN_PROGRESS correction mode: work is limited to the most recent bounded correction request; "
            "do not expand scope."
        )
    return notices.get(phase or "", "Lifecycle phase is unavailable.")


def _event_reference(event: dict[str, Any]) -> str:
    event_type = event["event_type"]
    summary = event["summary"].strip()
    line = (
        f"- `{event['event_id']}` {event['timestamp']} - `{event_type}` "
        f"(reported {event['actor_role']}; not authenticated): {_markdown_inline(summary)}"
    )
    if event_type == "lock_in_approved":
        line += " **Binding Product Owner-approved lock-in (reported context).**"
    elif event_type == "decision_recorded":
        line += " Non-binding decision."
    return line


def _binding_decisions(workflow: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not workflow:
        return []
    return [
        event
        for event in workflow["event_history"]
        if event["event_type"] == "lock_in_approved" and event.get("binding") is True
    ]


def _open_conditions(workflow: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not workflow:
        return []
    events = workflow["event_history"]
    open_events: dict[str, dict[str, Any]] = {}
    answered = {
        event.get("related_event_id")
        for event in events
        if event["event_type"] == "answer_recorded"
    }
    classified = {
        event.get("related_event_id")
        for event in events
        if event["event_type"] == "bug_classified"
    }
    fixed = {
        event.get("related_event_id")
        for event in events
        if event["event_type"] == "bug_fixed"
    }
    for event in events:
        event_type = event["event_type"]
        if event_type == "question_submitted" and event["event_id"] not in answered:
            open_events[event["event_id"]] = event
        elif event_type == "bug_discovered" and event["event_id"] not in classified:
            open_events[event["event_id"]] = event
        elif event_type == "bug_classified" and event.get("bug_scope") == "in_scope":
            bug_id = event.get("related_event_id")
            discovered = next(
                (item for item in events if item["event_id"] == bug_id and item["event_type"] == "bug_discovered"),
                None,
            )
            if bug_id not in fixed and discovered:
                open_events[bug_id] = discovered
        elif event_type == "escalation_opened":
            resolved_after = any(
                later["event_type"] == "escalation_resolved"
                for later in events[events.index(event) + 1 :]
            )
            if not resolved_after:
                open_events[event["event_id"]] = event
        elif event_type == "bug_classified" and event.get("bug_scope") == "needs_escalation":
            open_events[event["event_id"]] = event
    if workflow.get("phase") == "WAITING" and workflow.get("waiting_reason"):
        latest_wait_event = next(
            (
                event for event in reversed(events)
                if event.get("phase_after") == "WAITING" and event.get("phase_before") != "WAITING"
            ),
            None,
        )
        if latest_wait_event:
            open_events.setdefault(latest_wait_event["event_id"], latest_wait_event)
    return list(open_events.values())


def _event_section(events: list[dict[str, Any]], limit: int) -> list[str]:
    lines = []
    selected = events[-limit:]
    for event in selected:
        lines.append(_event_reference(event))
    omitted = len(events) - len(selected)
    if omitted:
        lines.append(
            f"- {omitted} earlier event(s) omitted from this bounded summary; "
            "consult the workflow sidecar for the complete chronology."
        )
    if not lines:
        lines.append("- No workflow events recorded.")
    return lines


def _git_summary(git_state: dict[str, Any]) -> list[str]:
    working_tree = git_state["working_tree"]
    upstream = git_state["upstream"]
    branch = git_state["branch"]
    branch_name = branch.get("name") if branch.get("state") == "attached" else None
    lines = [
        f"- Branch: `{_markdown_inline(branch_name or '(detached)')}`",
        f"- HEAD: `{git_state['head'] or '(unavailable)'}`",
        f"- Working tree: {'clean' if working_tree['is_clean'] else 'dirty'} "
        f"(`{git_state['workflow_state']}`)",
    ]
    if upstream["configured"] and upstream["divergence_state"] == "available":
        lines.append(
            f"- Upstream: `{_markdown_inline(upstream['ref'])}`; {upstream['ahead']} ahead / {upstream['behind']} behind "
            "using local tracking refs; no remote refresh was performed."
        )
    elif upstream["configured"]:
        lines.append(
            f"- Upstream: `{_markdown_inline(upstream['ref'])}`; ahead/behind unavailable from local tracking refs."
        )
    else:
        lines.append("- Upstream: not configured; ahead/behind unavailable.")
    path_sets = (
        ("Staged", working_tree["staged"]),
        ("Unstaged", working_tree["unstaged"]),
        ("Untracked", working_tree["untracked"]),
        ("Unmerged", working_tree["unmerged"]),
    )
    for label, details in path_sets:
        lines.append(f"- {label}: {details['count']}")
        shown = details["paths"][:MAX_GIT_PATHS]
        lines.extend(f"  - `{_markdown_inline(path)}`" for path in shown)
        omitted = details["count"] - len(shown)
        if omitted:
            lines.append(f"  - {omitted} additional path(s) omitted from the bounded path list.")
    lines.append("- Full diffs are not included.")
    return lines


def _finish_packet(lines: list[str]) -> str:
    packet = "\n".join(lines).rstrip() + "\n"
    if len(packet.encode("utf-8")) > MAX_PACKET_BYTES:
        raise HandoffPacketError(
            f"Generated packet exceeds the {MAX_PACKET_BYTES}-byte limit; no source was silently truncated."
        )
    return packet


def generate_handoff_packet(
    *,
    repository_root: str | Path,
    repository_name: str,
    repository_id: str,
    state_root: str | Path,
    milestone_root: str,
    documents: dict[str, str],
    candidate: dict[str, Any],
    packet_type: str,
) -> str:
    if packet_type not in {"coder", "architect"}:
        raise HandoffPacketError("Select a supported handoff packet type.")
    required_roles = _CODER_REQUIRED_DOCUMENTS if packet_type == "coder" else ()
    _required_documents(documents, required_roles)
    root, current_candidate = _validate_target(
        Path(repository_root),
        state_root,
        milestone_root,
        candidate,
    )
    prompt_path = current_candidate["prompt_path"]
    prompt = _read_relative_source(root, prompt_path)
    workflow, legacy_mode = _workflow_state(root, state_root, milestone_root, current_candidate)
    phase = workflow["phase"] if workflow else None
    correction_mode = _correction_mode(workflow, phase)

    timestamp = datetime.now(UTC).isoformat(timespec="seconds")
    try:
        git_state = inspect_git_state(str(root))
    except WorkflowGitStateError as exc:
        raise HandoffPacketError(f"Cannot refresh required deterministic Git evidence: {exc}") from exc
    branch = git_state["branch"]
    branch_name = branch.get("name") if branch.get("state") == "attached" else None
    lines = [
        f"# {'Coder Handoff Packet' if packet_type == 'coder' else 'Architect Review Packet'}",
        "",
        "> Derived, read-only handoff output. Source artifacts remain authoritative; do not edit this packet as project state.",
        "",
        "## Generation Context",
        "",
        f"- Repository: {_markdown_inline(repository_name)} (`{repository_id}`)",
        f"- Repository path: `{_markdown_inline(str(root))}`",
        f"- Milestone: `{current_candidate['milestone_id']}`",
        f"- Lifecycle: `{phase or 'unavailable (legacy mode)'}`",
        f"- Branch: `{_markdown_inline(branch_name or '(detached)')}`",
        f"- HEAD: `{git_state['head'] or '(unavailable)'}`",
        f"- Generated at (UTC): `{timestamp}`",
        f"- Authorization: {_authority_notice(phase, legacy_mode, packet_type, correction_mode)}",
        "",
    ]
    sources: list[str] = [prompt_path]

    if packet_type == "coder":
        lines.extend(
            [
                "## Authoritative Milestone Prompt",
                "",
                f"Source: `{_markdown_inline(prompt_path)}`",
                "",
                prompt.rstrip(),
                "",
                "## Product Owner-Approved Binding Lock-Ins",
                "",
            ]
        )
        decisions = _binding_decisions(workflow)
        if workflow is None:
            lines.append("Unavailable: no workflow sidecar exists for this legacy milestone.")
        elif decisions:
            lines.extend(_event_section(decisions, len(decisions)))
            sources.append(workflow["sidecar_path"])
        else:
            lines.append("No binding lock-in events are recorded.")
            sources.append(workflow["sidecar_path"])
        lines.extend(["", "## Current Blockers and Unresolved Conditions", ""])
        conditions = _open_conditions(workflow)
        if workflow is None:
            lines.append("Unavailable: no workflow sidecar exists for this legacy milestone.")
        elif conditions:
            lines.extend(_event_section(conditions, len(conditions)))
            if phase == "WAITING":
                lines.append(f"- Waiting reason: {_markdown_inline(workflow.get('waiting_reason') or '')}")
            sources.append(workflow["sidecar_path"])
        else:
            lines.append("No open questions, blockers, escalations, or unresolved bugs are recorded.")
        if correction_mode and workflow:
            correction_event = next(
                event for event in reversed(workflow["event_history"])
                if event["event_type"] == "correction_requested"
            )
            lines.extend(
                [
                    "",
                    "## Bounded Correction Request",
                    "",
                    _event_reference(correction_event),
                ]
            )
            if current_candidate.get("closeout_exists"):
                closeout_path = current_candidate["closeout_path"]
                correction_closeout = _read_relative_source(root, closeout_path)
                lines.extend(
                    [
                        "",
                        "## Existing Closeout for Correction Context",
                        "",
                        f"Source: `{_markdown_inline(closeout_path)}`",
                        "",
                        correction_closeout.rstrip(),
                    ]
                )
                sources.append(closeout_path)
        for role in _CODER_REQUIRED_DOCUMENTS:
            source = _read_configured_document(root, documents, role, required=True)
            if source is None:
                continue
            relative_path, content = source
            heading = "Coding-Agent Rules" if role == "coding_agent_rules" else "Standing Workflow Rules"
            lines.extend(
                [
                    "",
                    f"## {heading}",
                    "",
                    f"Source: `{_markdown_inline(relative_path)}`",
                    "",
                    content.rstrip(),
                    "",
                ]
            )
            sources.append(relative_path)
        lines.extend(
            [
                "",
                "## Governing Document Inclusion",
                "",
                "Only configured coding-agent rules and standing workflow rules are included for the Coder. "
                "Project context, architecture, and parking-lot documents are intentionally omitted from this packet.",
            ]
        )
        lines.extend(
            [
                "",
                "## Repository Identity and Validation",
                "",
                f"- Branch: `{branch_name or '(detached)'}`",
                f"- HEAD: `{git_state['head'] or '(unavailable)'}`",
                "- Follow validation and testing expectations stated in the authoritative prompt and standing rules.",
                "",
                "## Git Mutation Prohibition",
                "",
                "Do not stage, commit, push, tag, merge, rebase, reset, stash, clean, switch/checkout, "
                "create/delete branches, or mutate remotes. Product Owner owns Git mutations.",
            ]
        )
    else:
        lines.extend(
            [
                "## Authoritative Milestone Prompt and Objective",
                "",
                f"Source: `{_markdown_inline(prompt_path)}`",
                "",
                prompt.rstrip(),
                "",
                "## Product Owner-Approved Binding Lock-Ins",
                "",
            ]
        )
        decisions = _binding_decisions(workflow)
        if workflow is None:
            lines.append("Unavailable: no workflow sidecar exists for this legacy milestone.")
        elif decisions:
            lines.extend(_event_section(decisions, len(decisions)))
            sources.append(workflow["sidecar_path"])
        else:
            lines.append("No binding lock-in events are recorded.")
        lines.extend(["", "## Relevant Workflow Events", ""])
        if workflow is None:
            lines.append("Unavailable: no workflow sidecar exists for this legacy milestone.")
        else:
            lines.extend(_event_section(workflow["event_history"], _EVENT_SUMMARY_LIMIT))
            sources.append(workflow["sidecar_path"])
        lines.extend(["", "## Open Questions, Blockers, Escalations, and Bugs", ""])
        conditions = _open_conditions(workflow)
        if workflow is None:
            lines.append("Unavailable: no workflow sidecar exists for this legacy milestone.")
        elif conditions:
            lines.extend(_event_section(conditions, len(conditions)))
            if phase == "WAITING":
                lines.append(f"- Waiting category: {_markdown_inline(workflow.get('waiting_category') or '')}")
                lines.append(f"- Waiting reason: {_markdown_inline(workflow.get('waiting_reason') or '')}")
        else:
            lines.append("No unresolved conditions are recorded.")

        closeout_path = current_candidate.get("closeout_path")
        lines.extend(["", "## Closeout", ""])
        if closeout_path:
            closeout = _read_relative_source(root, closeout_path)
            lines.extend([f"Source: `{_markdown_inline(closeout_path)}`", "", closeout.rstrip()])
            sources.append(closeout_path)
        elif phase == "REVIEW":
            raise HandoffPacketError("A closeout is required for Architect review while the milestone is in REVIEW.")
        else:
            lines.append("No matching closeout is currently available.")

        lines.extend(["", "## Deterministic Git State", "", "Source: Git local repository state at generation time.", ""])
        lines.extend(_git_summary(git_state))
        lines.extend(
            [
                "",
                "## Test and Evidence Records",
                "",
            ]
        )
        test_events = [
            event for event in workflow["event_history"] if event["event_type"] == "test_result_recorded"
        ] if workflow else []
        if test_events:
            for event in test_events[-_EVENT_SUMMARY_LIMIT:]:
                lines.append(
                    f"- `{event['event_id']}` {event['timestamp']}: "
                    f"{_markdown_inline(event.get('result') or '(no result)')} "
                    f"(reported by {event['actor_role']}; source "
                    f"`{_markdown_inline(event.get('evidence_source') or '(not recorded)')}`; "
                    "not independently verified by Repo Control)."
                )
            omitted = len(test_events) - min(len(test_events), _EVENT_SUMMARY_LIMIT)
            if omitted:
                lines.append(f"- {omitted} earlier test-result event(s) omitted; consult the workflow sidecar.")
        else:
            lines.append("No test-result events are recorded.")

        for role in _ARCHITECT_OPTIONAL_DOCUMENTS:
            if role not in documents:
                continue
            source = _read_configured_document(root, documents, role, required=False)
            if source is None:
                continue
            relative_path, content = source
            label = {
                "context": "Project Context (configured standing document)",
                "architecture": "Project Architecture (configured standing document)",
                "workflow": "Standing Project Workflow",
            }[role]
            lines.extend(
                [
                    "",
                    f"## {label}",
                    "",
                    f"Source: `{_markdown_inline(relative_path)}`",
                    "",
                    content.rstrip(),
                ]
            )
            sources.append(relative_path)
        lines.extend(
            [
                "",
                "## Governing Document Inclusion",
                "",
                "Only configured project context, architecture, and standing workflow documents are included. "
                "Coding-agent rules and the parking lot are intentionally omitted from this review packet.",
            ]
        )

    lines.extend(["", "## Source References", ""])
    unique_sources = list(dict.fromkeys(sources))
    lines.extend(f"- `{_markdown_inline(source)}`" for source in unique_sources)
    if workflow is None:
        lines.append("- Workflow/decision history unavailable: legacy milestone has no workflow sidecar.")
    elif workflow["sidecar_path"] not in unique_sources:
        lines.append(
            f"- `{_markdown_inline(workflow['sidecar_path'])}` (consult for full event chronology)."
        )
    lines.append("- Generated packet only; no source artifact, lifecycle, selection, or Git state was changed.")
    return _finish_packet(lines)
