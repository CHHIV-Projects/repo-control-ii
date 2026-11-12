from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import stat
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from ..information_store import (
    IdempotencyConflictError,
    IdentityMismatchError,
    InformationStore,
    InformationStoreError,
    RevisionConflictError,
    StoreIdentity,
)
from .git_actions import GitActionError, inspect_publication_target


_CANONICAL_PROMPT = re.compile(
    r"(?P<base>(?:M[0-9]{3}(?:\.[0-9]{3})+|R[0-9]{3}\.[0-9]{2})_"
    r"[a-z0-9]+(?:_[a-z0-9]+)*)_prompt\.md\Z"
)
_OBJECT_ID = re.compile(r"(?:[a-f0-9]{40}|[a-f0-9]{64})\Z")
_SHA256 = re.compile(r"[a-f0-9]{64}\Z")
_MAX_CLOSEOUT_BYTES = 5_000_000
_NO_FOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _NO_FOLLOW


class CloseoutPublicationError(RuntimeError):
    pass


class PublicationNotAcceptedError(CloseoutPublicationError):
    pass


class PublicationGitUnavailableError(CloseoutPublicationError):
    pass


class PublicationCollisionError(CloseoutPublicationError):
    pass


class PublicationDriftError(CloseoutPublicationError):
    pass


class PublicationMaterializationError(CloseoutPublicationError):
    pass


@dataclass(frozen=True)
class FixturePublicationContext:
    repository_id: str
    repository_key: str
    repository_root: str
    milestone_id: str
    milestone_root: str
    worktree_root: str | Path
    test_only: bool


class PublicationGitObserver(Protocol):
    status: str

    def inspect(
        self,
        worktree_root: Path,
        target_path: str,
        *,
        commit_id: str | None = None,
    ) -> dict[str, Any]: ...


class ReadOnlyGitReviewObserver:
    status = "CONNECTED"

    def inspect(
        self,
        worktree_root: Path,
        target_path: str,
        *,
        commit_id: str | None = None,
    ) -> dict[str, Any]:
        return inspect_publication_target(worktree_root, target_path, commit_id=commit_id)


class NotConnectedGitReviewObserver:
    status = "NOT_CONNECTED"

    def inspect(
        self,
        worktree_root: Path,
        target_path: str,
        *,
        commit_id: str | None = None,
    ) -> dict[str, Any]:
        return {"connected": False, "status": self.status}


class CloseoutPublicationService:
    """Fixture-only accepted-closeout publisher; it has no Git mutation API."""

    def __init__(
        self,
        store: InformationStore,
        context: FixturePublicationContext,
        git_observer: PublicationGitObserver,
    ) -> None:
        if context.test_only is not True:
            raise CloseoutPublicationError("Closeout publication is restricted to explicit test fixtures.")
        self.store = store
        self.context = context
        self.identity: StoreIdentity = store.identity
        if (
            context.repository_id != self.identity.repository_id
            or context.repository_key != self.identity.repository_key
            or context.repository_root != self.identity.repository_root
            or context.milestone_id != self.identity.milestone_id
        ):
            raise IdentityMismatchError("Publication context does not match the fixture store identity.")
        if store.snapshot()["authority"] != "disposable_fixture_only":
            raise CloseoutPublicationError("Publication requires a disposable InformationStore fixture.")
        self.worktree_root = self._validate_worktree_root(context.worktree_root)
        self.target_path = self._canonical_target_path(self.identity, context.milestone_root)
        self._validate_target_parent()
        if not callable(getattr(git_observer, "inspect", None)):
            raise ValueError("A read-only Git observer is required.")
        self.git_observer = git_observer

    @property
    def integration_status(self) -> str:
        return getattr(self.git_observer, "status", "NOT_CONNECTED")

    def preview(
        self,
        *,
        request_id: str,
        document_id: str,
        revision_id: str,
        expected_sha256: str,
    ) -> dict[str, Any]:
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", request_id):
            raise ValueError("Publication request ID must be a simple identifier of at most 128 characters.")
        if not isinstance(expected_sha256, str) or not _SHA256.fullmatch(expected_sha256):
            raise ValueError("Expected revision hash must be a lowercase SHA-256 digest.")
        request_hash = _fingerprint(
            {
                "repository_id": self.identity.repository_id,
                "repository_key": self.identity.repository_key,
                "repository_root": self.identity.repository_root,
                "milestone_id": self.identity.milestone_id,
                "document_id": document_id,
                "revision_id": revision_id,
                "content_sha256": expected_sha256,
                "target_path": self.target_path,
            }
        )
        existing = self.store.get_document_publication(request_id=request_id)
        if existing is not None:
            if existing["request_hash"] != request_hash:
                raise IdempotencyConflictError(
                    "Publication request ID was already used with different inputs."
                )
            source = self._accepted_source(document_id, revision_id, expected_sha256)
            if existing["status"] == "accepted_not_published":
                existing = self._update(
                    existing,
                    status="preview_ready",
                    observation=existing["git_observation"],
                )
            return self._render_record(existing, source)

        source = self._accepted_source(document_id, revision_id, expected_sha256)
        target_bytes = self._read_target()
        observation = self._observe()
        self._assert_safe_target(target_bytes, expected_sha256, observation)
        content = source["revision"]["content"]
        observation["source_authority_fingerprint"] = source["publication_authority_fingerprint"]
        proposed_diff = _proposed_diff(self.target_path, target_bytes, content.encode("utf-8"))
        observation["proposed_diff"] = proposed_diff
        observation["target_worktree_sha256"] = (
            hashlib.sha256(target_bytes).hexdigest() if target_bytes is not None else None
        )
        preview_fingerprint = _fingerprint(
            {
                "request_hash": request_hash,
                "branch": observation["branch"],
                "head": observation["head"],
                "worktree_fingerprint": observation["worktree_fingerprint"],
                "index_fingerprint": observation["index_fingerprint"],
                "source_authority_fingerprint": observation["source_authority_fingerprint"],
                "target": observation["target"],
            }
        )
        record = self.store.create_document_publication(
            publication_id=uuid.uuid4().hex,
            request_id=request_id,
            request_hash=request_hash,
            document_id=document_id,
            revision_id=revision_id,
            content_sha256=expected_sha256,
            target_path=self.target_path,
            target_branch=observation["branch"],
            target_head=observation["head"],
            worktree_fingerprint=observation["worktree_fingerprint"],
            index_fingerprint=observation["index_fingerprint"],
            preview_fingerprint=preview_fingerprint,
            git_observation=observation,
        )
        if record["status"] == "accepted_not_published":
            record = self._update(record, status="preview_ready", observation=observation)
        return self._render_record(record, source)

    def materialize(
        self,
        publication_id: str,
        preview_fingerprint: str,
        *,
        fixture_po_confirmation: bool,
    ) -> dict[str, Any]:
        if fixture_po_confirmation is not True:
            raise PublicationNotAcceptedError(
                "Explicit test-fixture PO confirmation is required to materialize the closeout."
            )
        record = self.store.get_document_publication(publication_id=publication_id)
        if record is None:
            raise RevisionConflictError("Publication operation was not found.")
        if preview_fingerprint != record["preview_fingerprint"]:
            raise PublicationDriftError("PO confirmation does not match the exact stored preview.")
        if record["status"] in {"materialized_unstaged", "staged", "committed"}:
            return record
        if record["status"] != "preview_ready":
            raise PublicationDriftError(f"Publication cannot materialize from {record['status']}.")
        try:
            source = self._accepted_source(
                record["document_id"],
                record["revision_id"],
                record["content_sha256"],
            )
        except (CloseoutPublicationError, InformationStoreError) as exc:
            return self._drift(record, record["git_observation"], str(exc))
        if (
            source["publication_authority_fingerprint"]
            != record["git_observation"].get("source_authority_fingerprint")
        ):
            return self._drift(record, record["git_observation"], "Acceptance evidence changed after preview.")
        target_bytes = self._read_target()
        before = self._observe()
        if not self._matches_preview(record, before, target_bytes):
            return self._drift(record, before, "Repository or target changed after preview.")
        record = self._update(
            record,
            status="preview_ready",
            observation=record["git_observation"],
            confirm=True,
        )

        wrote_file = False
        try:
            if target_bytes is None:
                self._write_new_file(record["publication_id"], source["revision"]["content"].encode("utf-8"))
                wrote_file = True
            after = self._observe()
            materialized = self._read_target()
            if materialized is None or hashlib.sha256(materialized).hexdigest() != record["content_sha256"]:
                raise PublicationMaterializationError("Materialized target does not match the accepted revision.")
            if (
                after["branch"] != before["branch"]
                or after["head"] != before["head"]
                or after["index_fingerprint"] != before["index_fingerprint"]
                or after["worktree_fingerprint_without_target"]
                != before["worktree_fingerprint_without_target"]
            ):
                return self._drift(record, after, "Git state changed while materializing the closeout.")
            current_source = self._accepted_source(
                record["document_id"],
                record["revision_id"],
                record["content_sha256"],
            )
            if (
                current_source["publication_authority_fingerprint"]
                != record["git_observation"].get("source_authority_fingerprint")
            ):
                return self._drift(record, after, "Acceptance evidence changed while materializing the closeout.")
            if after["target"].get("head_blob_sha256") == record["content_sha256"]:
                return self._update(
                    record,
                    status="committed",
                    observation=after,
                    related_commit_id=after["head"],
                )
            status = (
                "staged"
                if after["target"].get("staged") is True
                and after["target"].get("staged_blob_sha256") == record["content_sha256"]
                else "materialized_unstaged"
            )
            return self._update(record, status=status, observation=after)
        except (OSError, GitActionError, CloseoutPublicationError) as exc:
            failed_observation = self._best_effort_observation(record)
            failed = self._update(
                record,
                status="failed",
                observation=failed_observation,
                failure_reason=str(exc),
            )
            if wrote_file:
                return failed
            raise PublicationMaterializationError(str(exc)) from exc

    def reconcile(
        self,
        publication_id: str,
        *,
        commit_id: str | None = None,
    ) -> dict[str, Any]:
        record = self.store.get_document_publication(publication_id=publication_id)
        if record is None:
            raise RevisionConflictError("Publication operation was not found.")
        if record["status"] in {"committed", "drifted"}:
            return record
        if commit_id is not None and not _OBJECT_ID.fullmatch(commit_id):
            raise ValueError("Commit observation requires a full Git object ID.")
        try:
            source = self._accepted_source(
                record["document_id"],
                record["revision_id"],
                record["content_sha256"],
            )
        except (CloseoutPublicationError, InformationStoreError) as exc:
            return self._drift(record, record["git_observation"], str(exc))
        if source["publication_authority_fingerprint"] != record["git_observation"].get(
            "source_authority_fingerprint"
        ):
            return self._drift(record, record["git_observation"], "Acceptance evidence changed after preview.")
        target_bytes = self._read_target()
        if target_bytes is None:
            if record["status"] in {"materialized_unstaged", "staged"}:
                return self._drift(record, record["git_observation"], "Published target is missing.")
            return record
        if hashlib.sha256(target_bytes).hexdigest() != record["content_sha256"]:
            return self._drift(record, record["git_observation"], "Target content differs from the accepted revision.")
        if record["confirmed_at"] is None:
            return record

        observation = self._observe(commit_id=commit_id)
        if observation.get("branch") != record["target_branch"]:
            return self._drift(record, observation, "Current branch differs from the confirmed preview.")
        verified_head = observation["target"].get("head_blob_sha256") == record["content_sha256"]
        verified_commit = (
            commit_id is not None
            and observation.get("head") == commit_id
            and observation.get("verified_commit_id") == commit_id
            and observation.get("commit_blob_sha256") == record["content_sha256"]
        )
        if verified_head or verified_commit:
            related_commit = observation["head"] if verified_head else commit_id
            return self._update(
                record,
                status="committed",
                observation=observation,
                related_commit_id=related_commit,
            )
        if observation.get("head") != record["target_head"]:
            return self._drift(record, observation, "HEAD changed without a verified publication commit.")

        previous = record["git_observation"]
        if record["status"] in {"preview_ready", "failed"} and (
            observation["index_fingerprint"] != record["index_fingerprint"]
            or observation["worktree_fingerprint_without_target"]
            != previous.get("worktree_fingerprint_without_target")
        ):
            return self._drift(record, observation, "Unrelated worktree or index state changed.")
        target = observation["target"]
        status = (
            "staged"
            if target.get("staged") is True
            and target.get("staged_blob_sha256") == record["content_sha256"]
            else "materialized_unstaged"
        )
        return self._update(record, status=status, observation=observation)

    def _accepted_source(
        self,
        document_id: str,
        revision_id: str,
        expected_sha256: str,
    ) -> dict[str, Any]:
        evidence = self.store.get_closeout_publication_evidence(document_id, revision_id)
        if (
            evidence["repository_id"] != self.identity.repository_id
            or evidence["milestone_id"] != self.identity.milestone_id
        ):
            raise IdentityMismatchError("Accepted closeout belongs to a different repository or milestone.")
        revision = evidence["revision"]
        if revision["logical_type"] != "CLOSEOUT" or revision["source_path"] != self.target_path:
            raise PublicationNotAcceptedError("Only the canonical closeout revision can be published.")
        if revision["content_sha256"] != expected_sha256:
            raise RevisionConflictError("Accepted closeout hash differs from the requested exact revision.")
        if evidence["projection"]["phase"] != "CLOSED":
            raise PublicationNotAcceptedError("Milestone must be legitimately accepted and currently closed.")

        links = {
            item["event_id"]
            for item in evidence["links"]
            if item["document_id"] == document_id
            and item["revision_id"] == revision_id
            and item["content_sha256"] == expected_sha256
            and item["link_role"] == "closeout"
        }
        events = evidence["events"]
        acceptance_positions = [
            index
            for index, event in enumerate(events)
            if event["event_type"] == "acceptance_recorded"
        ]
        if not acceptance_positions:
            raise PublicationNotAcceptedError("No acceptance event is linked to the exact closeout revision.")
        acceptance_index = acceptance_positions[-1]
        acceptance = events[acceptance_index]
        if (
            acceptance["event_id"] not in links
            or acceptance.get("po_approved") is not True
            or acceptance.get("actor_role") != "product_owner"
        ):
            raise PublicationNotAcceptedError("Latest acceptance does not approve the exact closeout revision.")
        decisions = [
            item
            for item in evidence["decisions"]
            if item["decision_type"] == "APPROVED"
            and item["actor_role"] == "product_owner"
            and item["po_confirmed"] == 1
            and item["source_kind"] == "trusted_fixture"
            and item["payload"].get("event_id") == acceptance["event_id"]
            and item["payload"].get("fixture_confirmation") is True
        ]
        if not decisions:
            raise PublicationNotAcceptedError("Acceptance lacks an explicit trusted fixture PO decision.")
        submitted_events = [
            (index, event)
            for index, event in enumerate(events)
            if index < acceptance_index
            and event["event_type"] == "closeout_submitted"
            and event["event_id"] in links
            and event.get("evidence_path") == self.target_path
        ]
        if not submitted_events:
            raise PublicationNotAcceptedError("The exact closeout revision was not submitted before acceptance.")
        if any(event["event_type"] == "milestone_reopened" for event in events[acceptance_index + 1 :]):
            raise PublicationNotAcceptedError("A later milestone reopen invalidates this publication preview.")
        closures = [
            event
            for event in events[acceptance_index + 1 :]
            if event["event_type"] == "milestone_closed"
            and event.get("po_approved") is True
            and event.get("actor_role") == "product_owner"
        ]
        if not closures:
            raise PublicationNotAcceptedError("The accepted milestone has no later Product Owner closure event.")
        evidence["publication_authority_fingerprint"] = _fingerprint(
            {
                "repository_id": self.identity.repository_id,
                "repository_key": self.identity.repository_key,
                "repository_root": self.identity.repository_root,
                "milestone_id": self.identity.milestone_id,
                "document_id": document_id,
                "revision_id": revision_id,
                "content_sha256": expected_sha256,
                "submission_event_id": submitted_events[-1][1]["event_id"],
                "acceptance_event_id": acceptance["event_id"],
                "closure_event_id": closures[-1]["event_id"],
                "decision": decisions[-1],
            }
        )
        return evidence

    def _observe(self, *, commit_id: str | None = None) -> dict[str, Any]:
        try:
            observation = self.git_observer.inspect(
                self.worktree_root,
                self.target_path,
                commit_id=commit_id,
            )
        except GitActionError as exc:
            raise PublicationGitUnavailableError(str(exc)) from exc
        if not isinstance(observation, dict) or observation.get("connected") is not True:
            raise PublicationGitUnavailableError("Git Review observation is NOT_CONNECTED.")
        if observation.get("repository_root") != str(self.worktree_root):
            raise IdentityMismatchError("Git observation resolved a different fixture worktree.")
        if not isinstance(observation.get("branch"), str) or not observation["branch"]:
            raise PublicationGitUnavailableError("Git Review did not provide an attached branch.")
        if not isinstance(observation.get("head"), str) or not _OBJECT_ID.fullmatch(observation["head"]):
            raise PublicationGitUnavailableError("Git Review did not provide a full HEAD object ID.")
        for field in ("index_fingerprint", "worktree_fingerprint", "worktree_fingerprint_without_target"):
            if not isinstance(observation.get(field), str) or not _SHA256.fullmatch(observation[field]):
                raise PublicationGitUnavailableError(f"Git Review did not provide a valid {field}.")
        target = observation.get("target")
        if not isinstance(target, dict) or target.get("path") != self.target_path:
            raise PublicationGitUnavailableError("Git Review did not inspect the canonical publication target.")
        if target.get("staged_blob_sha256") is not None and not _SHA256.fullmatch(
            target["staged_blob_sha256"]
        ):
            raise PublicationGitUnavailableError("Git Review returned an invalid staged blob hash.")
        if not isinstance(target.get("staged"), bool):
            raise PublicationGitUnavailableError("Git Review did not provide an explicit staged-state observation.")
        if target["staged"] and target.get("staged_blob_sha256") is None:
            raise PublicationGitUnavailableError("Git Review marked the target staged without an index blob.")
        for field in ("head_blob_sha256",):
            if target.get(field) is not None and not _SHA256.fullmatch(target[field]):
                raise PublicationGitUnavailableError("Git Review returned an invalid HEAD blob hash.")
        return observation

    def _assert_safe_target(
        self,
        target_bytes: bytes | None,
        expected_sha256: str,
        observation: dict[str, Any],
    ) -> None:
        if target_bytes is not None and hashlib.sha256(target_bytes).hexdigest() != expected_sha256:
            raise PublicationCollisionError("Canonical target already contains different bytes.")
        target = observation["target"]
        worktree = target.get("worktree")
        observed_hash = worktree.get("sha256") if isinstance(worktree, dict) else None
        expected_target_hash = hashlib.sha256(target_bytes).hexdigest() if target_bytes is not None else None
        if observed_hash != expected_target_hash:
            raise PublicationGitUnavailableError("Git Review and safe filesystem inspection disagree on the target.")
        if target.get("head_blob_sha256") not in {None, expected_sha256}:
            raise PublicationCollisionError("Canonical target is tracked with different committed bytes.")
        if target.get("staged_blob_sha256") not in {None, expected_sha256}:
            raise PublicationCollisionError("Canonical target is staged with different bytes.")
        if target_bytes is None and target.get("head_blob_sha256") is not None:
            raise PublicationCollisionError("Tracked target is absent from the worktree and will not be restored.")
        status = target.get("status")
        if isinstance(status, dict) and "D" in (status.get("xy") or ""):
            raise PublicationCollisionError("A tracked deletion exists at the canonical target.")

    def _matches_preview(
        self,
        record: dict[str, Any],
        observation: dict[str, Any],
        target_bytes: bytes | None,
    ) -> bool:
        previous = record["git_observation"]
        expected_target_hash = previous.get("target_worktree_sha256")
        actual_target_hash = hashlib.sha256(target_bytes).hexdigest() if target_bytes is not None else None
        return (
            observation["branch"] == record["target_branch"]
            and observation["head"] == record["target_head"]
            and observation["worktree_fingerprint"] == record["worktree_fingerprint"]
            and observation["index_fingerprint"] == record["index_fingerprint"]
            and actual_target_hash == expected_target_hash
        )

    def _drift(
        self,
        record: dict[str, Any],
        observation: dict[str, Any],
        reason: str,
    ) -> dict[str, Any]:
        if record["status"] == "drifted":
            return record
        return self._update(
            record,
            status="drifted",
            observation=observation,
            failure_reason=reason,
        )

    def _best_effort_observation(self, record: dict[str, Any]) -> dict[str, Any]:
        try:
            return self._observe()
        except (PublicationGitUnavailableError, IdentityMismatchError):
            return record["git_observation"]

    def _update(
        self,
        record: dict[str, Any],
        *,
        status: str,
        observation: dict[str, Any],
        related_commit_id: str | None = None,
        failure_reason: str | None = None,
        confirm: bool = False,
    ) -> dict[str, Any]:
        updated_observation = dict(observation)
        original_observation = record["git_observation"]
        if "proposed_diff" in original_observation:
            updated_observation["proposed_diff"] = original_observation["proposed_diff"]
        if "source_authority_fingerprint" in original_observation:
            updated_observation["source_authority_fingerprint"] = original_observation[
                "source_authority_fingerprint"
            ]
        target = updated_observation.get("target")
        worktree = target.get("worktree") if isinstance(target, dict) else None
        if isinstance(worktree, dict):
            updated_observation["target_worktree_sha256"] = worktree.get("sha256")
        elif "target_worktree_sha256" in original_observation:
            updated_observation["target_worktree_sha256"] = original_observation["target_worktree_sha256"]
        return self.store.update_document_publication(
            record["publication_id"],
            expected_status=record["status"],
            status=status,
            target_branch=observation["branch"],
            target_head=observation["head"],
            worktree_fingerprint=observation["worktree_fingerprint"],
            index_fingerprint=observation["index_fingerprint"],
            preview_fingerprint=record["preview_fingerprint"],
            git_observation=updated_observation,
            related_commit_id=related_commit_id,
            failure_reason=failure_reason,
            confirm=confirm,
        )

    def _render_record(self, record: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
        observation = record["git_observation"]
        return {
            **record,
            "repository_key": self.identity.repository_key,
            "registered_repository_root": self.identity.repository_root,
            "branch": record["target_branch"],
            "head": record["target_head"],
            "target_path": self.target_path,
            "source": {
                "document_id": record["document_id"],
                "revision_id": record["revision_id"],
                "content_sha256": record["content_sha256"],
                "content": source["revision"]["content"],
            },
            "proposed_diff": observation.get("proposed_diff", ""),
            "integration_status": self.integration_status,
        }

    def _canonical_target_path(self, identity: StoreIdentity, milestone_root: str) -> str:
        if not isinstance(milestone_root, str):
            raise CloseoutPublicationError("Milestone root must be a repository-relative path.")
        root = PurePosixPath(milestone_root)
        prompt = PurePosixPath(identity.prompt_path)
        match = _CANONICAL_PROMPT.fullmatch(prompt.name)
        if (
            not milestone_root
            or root.is_absolute()
            or "\\" in milestone_root
            or any(part in {"", ".", ".."} for part in milestone_root.split("/"))
            or prompt.parent.as_posix() != milestone_root
            or match is None
            or not match.group("base").startswith(f"{identity.milestone_id}_")
        ):
            raise CloseoutPublicationError("Selected prompt does not define a canonical milestone target.")
        return f"{milestone_root}/{match.group('base')}_closeout.md"

    def _validate_worktree_root(self, value: str | Path) -> Path:
        candidate = Path(value).expanduser()
        if candidate.is_symlink() or not candidate.is_dir():
            raise CloseoutPublicationError("Caller-supplied fixture worktree must be an existing real directory.")
        resolved = candidate.resolve(strict=True)
        temporary_root = Path(tempfile.gettempdir()).resolve(strict=True)
        registered_root = Path(self.identity.repository_root).expanduser().resolve(strict=False)
        if (
            not resolved.is_relative_to(temporary_root)
            or resolved == registered_root
            or resolved.is_relative_to(registered_root)
            or registered_root.is_relative_to(resolved)
        ):
            raise CloseoutPublicationError("Publication writes are restricted to a separate disposable test worktree.")
        return resolved

    def _validate_target_parent(self) -> None:
        root_fd, parent_fd, _filename = self._open_target_parent()
        os.close(parent_fd)
        if parent_fd != root_fd:
            os.close(root_fd)

    def _open_target_parent(self) -> tuple[int, int, str]:
        root_fd = os.open(self.worktree_root, _DIRECTORY_FLAGS)
        current_fd = root_fd
        parts = PurePosixPath(self.target_path).parts
        try:
            for part in parts[:-1]:
                next_fd = os.open(part, _DIRECTORY_FLAGS, dir_fd=current_fd)
                if current_fd != root_fd:
                    os.close(current_fd)
                current_fd = next_fd
            return root_fd, current_fd, parts[-1]
        except OSError as exc:
            if current_fd != root_fd:
                os.close(current_fd)
            os.close(root_fd)
            raise CloseoutPublicationError(
                "Canonical milestone directory is missing or contains a symlink."
            ) from exc

    def _read_target(self) -> bytes | None:
        root_fd, parent_fd, filename = self._open_target_parent()
        try:
            try:
                descriptor = os.open(filename, os.O_RDONLY | _NO_FOLLOW, dir_fd=parent_fd)
            except FileNotFoundError:
                return None
            except OSError as exc:
                raise PublicationCollisionError("Canonical target cannot be opened without following links.") from exc
            try:
                metadata = os.fstat(descriptor)
                if not stat.S_ISREG(metadata.st_mode):
                    raise PublicationCollisionError("Canonical target is not an ordinary file.")
                if metadata.st_size > _MAX_CLOSEOUT_BYTES:
                    raise PublicationCollisionError("Canonical target exceeds the closeout size limit.")
                chunks = []
                total = 0
                while True:
                    chunk = os.read(descriptor, 1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > _MAX_CLOSEOUT_BYTES:
                        raise PublicationCollisionError("Canonical target exceeds the closeout size limit.")
                    chunks.append(chunk)
                return b"".join(chunks)
            finally:
                os.close(descriptor)
        finally:
            if parent_fd != root_fd:
                os.close(parent_fd)
            os.close(root_fd)

    def _write_new_file(self, publication_id: str, content: bytes) -> None:
        root_fd, parent_fd, filename = self._open_target_parent()
        temporary_name = f".repoctl-publication-{publication_id}.tmp"
        descriptor = None
        created_temporary = False
        try:
            descriptor = os.open(
                temporary_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NO_FOLLOW,
                0o600,
                dir_fd=parent_fd,
            )
            created_temporary = True
            offset = 0
            while offset < len(content):
                written = os.write(descriptor, content[offset:])
                if written <= 0:
                    raise OSError("Temporary closeout write made no progress.")
                offset += written
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            os.link(
                temporary_name,
                filename,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
                follow_symlinks=False,
            )
            os.unlink(temporary_name, dir_fd=parent_fd)
        except OSError as exc:
            if descriptor is not None:
                os.close(descriptor)
            if created_temporary:
                try:
                    os.unlink(temporary_name, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass
                except OSError as cleanup_error:
                    raise PublicationMaterializationError(
                        f"Safe materialization failed and its temporary file could not be removed: {cleanup_error}"
                    ) from exc
            raise PublicationMaterializationError(
                f"Safe exclusive materialization failed: {exc}"
            ) from exc
        finally:
            if parent_fd != root_fd:
                os.close(parent_fd)
            os.close(root_fd)


def _fingerprint(value: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _proposed_diff(target_path: str, existing: bytes | None, expected: bytes) -> str:
    old_text = existing.decode("utf-8") if existing is not None else ""
    new_text = expected.decode("utf-8")
    return "".join(
        difflib.unified_diff(
            old_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=target_path if existing is not None else "/dev/null",
            tofile=target_path,
        )
    )
