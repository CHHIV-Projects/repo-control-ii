from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping, Sequence


_SHA256 = re.compile(r"[a-f0-9]{64}\Z")
_DIRTY_STATUSES = {"untracked", "modified", "staged", "conflicted"}
_PROMPT_ARTIFACT_STATUSES = {"untracked", "modified"}
_SOURCE_MODES = {"database_revision", "database_revision_materialized"}


class PromptPreflightError(ValueError):
    pass


@dataclass(frozen=True)
class PromptRevisionBinding:
    repository_key: str
    repository_id: str
    repository_root: str
    milestone_id: str
    prompt_path: str
    document_id: str
    revision_id: str
    content_sha256: str
    selection_id: str
    selection_revision: int


@dataclass(frozen=True)
class PromptPreflightObservation:
    binding: PromptRevisionBinding
    approval_status: str
    lifecycle_phase: str
    lifecycle_authorized: bool
    source_mode: str


def validate_fixture_prompt_preflight(
    expected: PromptRevisionBinding,
    observed: PromptPreflightObservation,
    dirty_entries: Sequence[Mapping[str, str]],
    *,
    fixture_only: bool,
) -> dict[str, object]:
    """Evaluate the future exact-prompt exception without changing current preflight."""
    if fixture_only is not True:
        raise PromptPreflightError("This prompt-preflight contract is restricted to disposable fixtures.")
    if expected != observed.binding:
        raise PromptPreflightError("Observed repository, milestone, selection, or prompt revision does not match.")
    if (
        not expected.repository_key
        or not expected.repository_id
        or not expected.repository_root.startswith("/")
        or not expected.milestone_id
        or not expected.prompt_path
        or expected.prompt_path.startswith("/")
        or "\\" in expected.prompt_path
        or any(part in {"", ".", ".."} for part in expected.prompt_path.split("/"))
        or not expected.document_id
        or not expected.revision_id
        or not isinstance(expected.content_sha256, str)
        or not _SHA256.fullmatch(expected.content_sha256)
        or not expected.selection_id
        or type(expected.selection_revision) is not int
        or expected.selection_revision < 0
    ):
        raise PromptPreflightError("Expected prompt binding is incomplete or malformed.")
    if observed.approval_status != "APPROVED":
        raise PromptPreflightError("The exact selected prompt revision is not approved.")
    if observed.lifecycle_phase != "IN_PROGRESS" or observed.lifecycle_authorized is not True:
        raise PromptPreflightError("Live lifecycle authorization for the selected milestone is absent.")
    if observed.source_mode not in _SOURCE_MODES:
        raise PromptPreflightError("Prompt source mode is unsupported.")
    if not isinstance(dirty_entries, Sequence) or isinstance(dirty_entries, (str, bytes)):
        raise PromptPreflightError("Git dirty-entry observations must be a sequence.")

    seen_paths: set[str] = set()
    allowed_prompt_dirty = False
    for entry in dirty_entries:
        if not isinstance(entry, Mapping):
            raise PromptPreflightError("A Git dirty-entry observation has an invalid shape.")
        path = entry.get("path")
        status = entry.get("status")
        content_sha256 = entry.get("content_sha256")
        if (
            not isinstance(path, str)
            or not path
            or path.startswith("/")
            or "\\" in path
            or any(part in {"", ".", ".."} for part in path.split("/"))
            or path in seen_paths
            or not isinstance(status, str)
            or status not in _DIRTY_STATUSES
        ):
            raise PromptPreflightError("A Git dirty-entry observation is invalid or duplicated.")
        seen_paths.add(path)
        if path != expected.prompt_path:
            raise PromptPreflightError(f"Unrelated dirty Git path is not permitted: {path}.")
        if observed.source_mode != "database_revision_materialized":
            raise PromptPreflightError("A database-authoritative prompt must not fall back to a dirty worktree file.")
        if status not in _PROMPT_ARTIFACT_STATUSES or content_sha256 != expected.content_sha256:
            raise PromptPreflightError("The materialized prompt path is staged, conflicted, or hash-mismatched.")
        allowed_prompt_dirty = True

    return {
        "accepted": True,
        "authority": "fixture_only",
        "source_mode": observed.source_mode,
        "repository_id": expected.repository_id,
        "milestone_id": expected.milestone_id,
        "document_id": expected.document_id,
        "revision_id": expected.revision_id,
        "content_sha256": expected.content_sha256,
        "allowed_prompt_dirty": allowed_prompt_dirty,
        "unrelated_dirty_paths": [],
    }
