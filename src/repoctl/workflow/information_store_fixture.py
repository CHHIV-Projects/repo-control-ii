from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

from ..document_library import (
    ArchitectDocumentAdapter,
    CoderDocumentAdapter,
    DocumentLibrary,
    GitFileProvider,
    GitRepositoryContext,
    SQLiteRevisionProvider,
)
from ..information_store import (
    MAX_DISPATCH_COUNT,
    DocumentRevisionInput,
    InformationStore,
    StoreIdentity,
    WorkflowDocumentLink,
)
from ..store_authority import StoreAuthorityConfiguration, verify_store_registry
from ..web.document_library import create_document_library_blueprint
from ..web.repository_registry import RepositoryEntry, RepositoryRegistry
from .closeout_publication import (
    CloseoutPublicationService,
    FixturePublicationContext,
    ManagedPublicationContext,
    PublicationGitObserver,
)
from .architect_dispatches import page_dispatch_records
from .context_verification import VERIFICATION_EVENT_TYPE
from .lifecycle import (
    ACTOR_OPTIONS,
    EVENT_OPTIONS,
    WorkflowRecordError,
    _next_action,
    _open_bug_ids,
    _open_question_ids,
    _reduce_events,
    _unfixed_bug_ids,
    build_next_event,
    waiting_resolution,
)


_SHA256 = re.compile(r"[a-f0-9]{64}\Z")


class FixtureIntegrationError(RuntimeError):
    pass


class InformationStoreApplicationClients:
    """Shared application clients bound to one selected InformationStore and document catalog."""

    def __init__(
        self,
        store: InformationStore,
        library: DocumentLibrary,
    ) -> None:
        identity = store.identity
        matching_providers = [
            provider
            for provider in library.providers
            if isinstance(provider, SQLiteRevisionProvider)
            and provider.store.identity.repository_id == identity.repository_id
            and provider.store.identity.milestone_id == identity.milestone_id
        ]
        if len(matching_providers) != 1 or matching_providers[0].store is not store:
            raise FixtureIntegrationError(
                "The selected repository/milestone must have exactly one SQLite reader bound to this store."
            )
        if any(
            source.repository.repository_id == identity.repository_id and source.current_milestone_id is not None
            for provider in library.providers
            if isinstance(provider, GitFileProvider)
            for source in provider.sources
        ):
            raise FixtureIntegrationError(
                "Git milestone documents cannot be configured as current authority under InformationStore."
            )
        self.store = store
        self.library = library
        self._selection_lock = threading.RLock()
        self.identity: StoreIdentity = identity
        self.authority = store.snapshot()["authority"]
        if self.authority not in {"disposable_fixture_only", "sqlite_managed"}:
            raise FixtureIntegrationError("An inactive prepared store is not an application authority.")
        self.fixture_only = self.authority == "disposable_fixture_only"
        self.coder = CoderDocumentAdapter(library)
        self.architect = ArchitectDocumentAdapter(library)

    def _bind_store(self, store: InformationStore) -> None:
        if self.store.database_identity() != store.database_identity():
            raise FixtureIntegrationError("A selection change cannot replace the bound InformationStore database.")
        if any(
            source.repository.repository_id == store.identity.repository_id
            and source.current_milestone_id is not None
            for provider in self.library.providers
            if isinstance(provider, GitFileProvider)
            for source in provider.sources
        ):
            raise FixtureIntegrationError(
                "Git milestone documents cannot be configured as current authority under InformationStore."
            )
        matching = [
            provider for provider in self.library.providers
            if isinstance(provider, SQLiteRevisionProvider) and provider.store is self.store
        ]
        if len(matching) != 1:
            raise FixtureIntegrationError("The previous selection no longer has one matching SQLite provider.")
        old_provider = matching[0]
        new_provider = SQLiteRevisionProvider(store, repository_name=old_provider.repository_name)
        providers = tuple(new_provider if provider is old_provider else provider for provider in self.library.providers)
        provider_ids = [provider.provider_id for provider in providers]
        if len(set(provider_ids)) != len(provider_ids):
            raise FixtureIntegrationError("The new selection conflicts with another SQLite document provider.")
        self.library.providers = providers
        self.store = store
        self.identity = store.identity

    def refresh_external_selection(self) -> None:
        if self.store.selection_state_root is None:
            return
        self.store.database_identity()
        store = self._open_repository(
            self.identity.repository_id, self.identity.repository_key, self.identity.repository_root,
        )
        if store.identity != self.identity:
            self._bind_store(store)

    def switch_repository(self, repository: RepositoryEntry) -> None:
        if self.store.selection_state_root is None:
            raise FixtureIntegrationError("Repository switching requires an external selection binding.")
        store = self._open_repository(repository.repository_id, repository.key, str(repository.path))
        if store.identity != self.identity:
            self._bind_store(store)

    def _open_repository(self, repository_id: str, repository_key: str, repository_root: str) -> InformationStore:
        if self.store.selection_state_root is None:
            raise FixtureIntegrationError("An external selection binding is required.")
        if self.fixture_only:
            return InformationStore.open_disposable_from_external_selection(
                self.store.path,
                repository_id=repository_id,
                repository_key=repository_key,
                repository_root=repository_root,
                selection_state_root=self.store.selection_state_root,
                allow_unsupported_runtime_for_fixture=self.store.allow_unsupported_runtime_for_fixture,
            )
        configuration = self.store.authority_configuration
        if configuration is None:
            raise FixtureIntegrationError("The managed authority configuration is unavailable.")
        return InformationStore.open_managed_from_external_selection(
            configuration, repository_id=repository_id, repository_key=repository_key,
            repository_root=repository_root,
        )

    def _po_confirmation(self, fixture: bool, managed: bool) -> dict[str, bool]:
        if (self.fixture_only and (fixture is not True or managed is not False)) or (
            not self.fixture_only and (managed is not True or fixture is not False)
        ):
            raise FixtureIntegrationError("This action requires explicit Product Owner confirmation for its authority.")
        return {
            "trusted_fixture_po_confirmation": fixture,
            "trusted_managed_po_confirmation": managed,
        }

    @contextmanager
    def selection_scope(self) -> Iterator[None]:
        with self._selection_lock:
            self.refresh_external_selection()
            yield

    def browser_candidates(self) -> dict[str, Any]:
        return self.store.list_disposable_milestones()

    def confirm_imported_prompt(
        self,
        *,
        request_id: str,
        expected_store_revision: int,
        document_id: str,
        revision_id: str,
        content_sha256: str,
        fixture_po_confirmation: bool = False,
        managed_po_confirmation: bool = False,
    ) -> dict[str, Any]:
        confirmation = self._po_confirmation(fixture_po_confirmation, managed_po_confirmation)
        return self.store.confirm_imported_prompt_revision(
            request_id=request_id,
            expected_store_revision=expected_store_revision,
            document_id=document_id,
            revision_id=revision_id,
            content_sha256=content_sha256,
            **confirmation,
        )

    def switch_milestone(
        self,
        milestone_id: str,
        *,
        expected_selection_id: str,
        expected_selection_revision: int,
    ) -> dict[str, Any]:
        store = self.store.switch_selection(
            milestone_id,
            expected_selection_id=expected_selection_id,
            expected_selection_revision=expected_selection_revision,
        )
        if store is not self.store:
            self._bind_store(store)
        return self.browser_snapshot()["candidate"]

    def read_selected_prompt(
        self,
        revision_id: str,
        content_sha256: str,
    ) -> dict[str, Any]:
        if not isinstance(content_sha256, str) or not _SHA256.fullmatch(content_sha256):
            raise FixtureIntegrationError("Selected prompt hash must be a lowercase SHA-256 digest.")
        snapshot = self.store.snapshot()
        if snapshot["prompt_revision_id"] != revision_id:
            raise FixtureIntegrationError("Prompt revision does not match the selected store binding.")
        documents: list[dict[str, Any]] = []
        cursor = 0
        while True:
            page = self.store.list_documents(after_order=cursor)
            documents.extend(page["documents"])
            if page["next_cursor"] is None:
                break
            cursor = page["next_cursor"]
        prompts = [
            item
            for item in documents
            if item["source_path"] == self.identity.prompt_path and item["logical_type"] == "PROMPT"
        ]
        if len(prompts) != 1:
            raise FixtureIntegrationError("Selected store does not contain one exact prompt document.")
        document = prompts[0]
        revision = self.store.get_revision(document["document_id"], revision_id)
        if (
            revision["source_path"] != self.identity.prompt_path
            or revision["content_sha256"] != content_sha256
        ):
            raise FixtureIntegrationError("Selected prompt source path or hash does not match the store binding.")
        matches: list[dict[str, Any]] = []
        cursor_token: str | None = None
        while True:
            page = self.library.search(
                filters={
                    "repository_id": self.identity.repository_id,
                    "milestone_id": self.identity.milestone_id,
                    "document_class": "PROMPT",
                    "source": "sqlite_fixture" if self.fixture_only else "sqlite_managed",
                    "status": "approved",
                },
                page_size=100,
                cursor=cursor_token,
            )
            if not page["total_count_complete"]:
                raise FixtureIntegrationError("Selected prompt enumeration did not report complete coverage.")
            matches.extend(page["documents"])
            if not page["has_more"]:
                break
            cursor_token = page["next_cursor"]
        exact_matches = [
            item
            for item in matches
            if item["path"] == self.identity.prompt_path
            and item["revision_id"] == f"sqlite-revision:{revision_id}"
            and item["content_sha256"] == content_sha256
        ]
        if len(exact_matches) != 1:
            raise FixtureIntegrationError("DocumentLibrary did not resolve exactly one selected approved revision.")
        library_record = exact_matches[0]
        exact_content = self.library.get(library_record["document_id"], library_record["revision_id"])
        if exact_content["content"] != revision["content"]:
            raise FixtureIntegrationError("InformationStore and DocumentLibrary returned different prompt bytes.")
        return {
            "repository_id": self.identity.repository_id,
            "repository_key": self.identity.repository_key,
            "repository_root": self.identity.repository_root,
            "milestone_id": self.identity.milestone_id,
            "prompt_path": self.identity.prompt_path,
            "selection_id": self.identity.selection_id,
            "selection_revision": self.identity.selection_revision,
            "document_id": document["document_id"],
            "revision_id": revision_id,
            "library_document_id": library_record["document_id"],
            "library_revision_id": library_record["revision_id"],
            "content_sha256": content_sha256,
            "content": revision["content"],
            "approval_state": library_record["approval_state"],
            "store_authority": snapshot["authority"],
        }

    def workflow_snapshot(self) -> dict[str, Any]:
        snapshot = self.store.snapshot()
        reduced = _reduce_events(snapshot["events"])
        projection = snapshot["projection"]
        if any(projection.get(key) != value for key, value in reduced.items()):
            raise FixtureIntegrationError("Stored lifecycle projection differs from the original reducer.")
        if projection.get("event_count") != len(snapshot["events"]):
            raise FixtureIntegrationError("Stored lifecycle event count differs from the event chronology.")
        return snapshot

    def selected_prompt(self) -> dict[str, Any]:
        snapshot = self.workflow_snapshot()
        revision_id = snapshot["prompt_revision_id"]
        if not isinstance(revision_id, str) or not revision_id:
            raise FixtureIntegrationError("InformationStore has no approved selected prompt revision.")
        documents: list[dict[str, Any]] = []
        cursor = 0
        while True:
            page = self.store.list_documents(after_order=cursor)
            documents.extend(page["documents"])
            if page["next_cursor"] is None:
                break
            cursor = page["next_cursor"]
        prompts = [
            item
            for item in documents
            if item["source_path"] == self.identity.prompt_path
            and item["logical_type"] == "PROMPT"
            and item["approved_revision_id"] == revision_id
        ]
        if len(prompts) != 1:
            raise FixtureIntegrationError("Selected prompt revision is not uniquely approved for this identity.")
        revision = self.store.get_revision(prompts[0]["document_id"], revision_id)
        return self.read_selected_prompt(revision_id, revision["content_sha256"])

    def list_documents(self, *, after_order: int = 0) -> dict[str, Any]:
        return self.store.list_documents(after_order=after_order)

    def search_historical(self, repository: RepositoryEntry, **filters: Any) -> dict[str, Any]:
        return self.library.search_historical(repository, self.identity.milestone_id, **filters)

    def read_historical(
        self,
        repository: RepositoryEntry,
        path: str,
        *,
        document_id: str | None = None,
        revision_id: str | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        return self.library.read_historical(
            repository,
            self.identity.milestone_id,
            path,
            document_id=document_id,
            revision_id=revision_id,
            cursor=cursor,
        )

    def get_dispatch(self, dispatch_id: str) -> dict[str, Any]:
        return self.store.get_dispatch(dispatch_id)

    def create_architect_dispatch(
        self,
        *,
        dispatch_type: str,
        milestone_id: str,
        body: str,
        milestone_root: str,
        title: str | None = None,
        related_event_id: str | None = None,
    ) -> dict[str, Any]:
        if dispatch_type != "PROMPT" and milestone_id != self.identity.milestone_id:
            raise FixtureIntegrationError("Architect dispatch proposal escaped the selected fixture milestone.")
        return self.store.create_pending_dispatch(
            dispatch_id=secrets.token_hex(16),
            dispatch_type=dispatch_type,
            body=body,
            milestone_id=milestone_id,
            milestone_root=milestone_root,
            title=title,
            related_event_id=related_event_id,
        )

    def list_architect_dispatches(
        self,
        *,
        milestone_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        return self.store.list_dispatches(milestone_id=milestone_id, status=status, limit=limit)

    def list_architect_dispatch_page(
        self,
        *,
        milestone_id: str | None = None,
        status: str | None = None,
        limit: int,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        all_records = self.store.list_dispatches(limit=MAX_DISPATCH_COUNT)
        snapshot = hashlib.sha256(
            json.dumps(all_records, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        records = [
            {**item, "type": item["dispatch_type"], "authorization_granted": False}
            for item in all_records
            if (milestone_id is None or item["milestone_id"] == milestone_id)
            and (status is None or item["status"] == status)
        ]
        return page_dispatch_records(
            records, repository_id=self.identity.repository_id, milestone_id=milestone_id,
            status=status, limit=limit, cursor=cursor, snapshot=snapshot,
        )

    def list_unverified_architect_guidance(self) -> list[dict[str, Any]]:
        return self.workflow_snapshot()["unverified_guidance"]

    def reconcile_imported_guidance(
        self,
        *,
        dispatch_id: str,
        history_id: str,
        status: str,
        expected_fingerprint: str,
        fixture_po_confirmation: bool = False,
        managed_po_confirmation: bool = False,
    ) -> dict[str, Any]:
        confirmation = self._po_confirmation(fixture_po_confirmation, managed_po_confirmation)
        return self.store.reconcile_imported_guidance(
            dispatch_id=dispatch_id,
            history_id=history_id,
            status=status,
            expected_fingerprint=expected_fingerprint,
            **confirmation,
        )

    def record_architect_dispatch_decision(
        self,
        *,
        dispatch_id: str,
        status: str,
        fixture_po_confirmation: bool = False,
        managed_po_confirmation: bool = False,
        milestone_root: str | None = None,
        expected_fingerprint: str | None = None,
    ) -> dict[str, Any]:
        confirmation = self._po_confirmation(fixture_po_confirmation, managed_po_confirmation)
        dispatch = self.store.get_dispatch(dispatch_id)
        if (
            (
                dispatch["dispatch_type"] != "PROMPT"
                and dispatch["milestone_id"] != self.identity.milestone_id
            )
            or dispatch["status"] != "PENDING"
        ):
            raise FixtureIntegrationError("Only a pending dispatch for the selected fixture milestone may be decided.")
        approved_milestone_id = (
            self.identity.milestone_id
            if status == "APPROVED" and dispatch["dispatch_type"] in {"INSTRUCTION", "RESPONSE"}
            else None
        )
        return self.store.record_dispatch_decision(
            dispatch_id=dispatch_id,
            history_id=secrets.token_hex(16),
            status=status,
            actor_role="product_owner",
            actor_attribution=(
                "Explicit Product Owner confirmation reported by the isolated fixture; identity is not authenticated."
                if self.fixture_only else
                "Explicit Product Owner browser decision; identity is not authenticated."
            ),
            **confirmation,
            approved_milestone_id=approved_milestone_id,
            related_event_id=dispatch["related_event_id"],
            milestone_root=milestone_root,
            expected_fingerprint=expected_fingerprint,
        )

    def get_revision(self, document_id: str, revision_id: str) -> dict[str, Any]:
        return self.store.get_revision(document_id, revision_id)

    def get_event_document_link(self, event_id: str, link_role: str) -> dict[str, Any]:
        return self.store.get_event_document_link(event_id, link_role)

    def read_revision_page(
        self,
        document_id: str,
        revision_id: str,
        *,
        byte_offset: int = 0,
        max_bytes: int = 64 * 1024,
    ) -> dict[str, Any]:
        return self.store.read_revision_page(
            document_id,
            revision_id,
            byte_offset=byte_offset,
            max_bytes=max_bytes,
        )

    def read_selected_milestone_document_page(
        self,
        document_path: str,
        *,
        start_byte: int = 0,
        source_fingerprint: str | None = None,
    ) -> dict[str, Any]:
        from ..information_store import MAX_DOCUMENT_PAGE_BYTES, RevisionConflictError
        from ..web.architect_contract import (
            MAX_MILESTONE_DOCUMENT_PAGE_BYTES,
            MAX_REPOSITORY_DOCUMENT_FILE_BYTES,
            ArchitectContractError,
            _selected_milestone_document_path,
            read_selected_milestone_document_page,
        )
        from .handoff_packets import _PRIVATE_KEY_MARKER

        if type(start_byte) is not int or start_byte < 0 or start_byte > MAX_REPOSITORY_DOCUMENT_FILE_BYTES:
            raise ArchitectContractError("invalid_document_offset", "The page offset is invalid.", 400)
        if source_fingerprint is not None and (
            not isinstance(source_fingerprint, str) or not _SHA256.fullmatch(source_fingerprint)
        ):
            raise ArchitectContractError("invalid_document_fingerprint", "The source fingerprint is invalid.", 400)
        if start_byte > 0 and source_fingerprint is None:
            raise ArchitectContractError(
                "document_fingerprint_required", "A source fingerprint is required to continue.", 400
            )
        path = _selected_milestone_document_path(document_path, self.identity.prompt_path)
        view = self.browser_snapshot()
        prompt = view["prompt"]
        revision_status = "selected_prompt"
        closeout_path = self.identity.prompt_path.removesuffix("_prompt.md") + "_closeout.md"
        if (
            path not in {self.identity.prompt_path, closeout_path}
            and path not in prompt["content"]
            and PurePosixPath(path).name not in prompt["content"]
        ):
            raise ArchitectContractError(
                "milestone_evidence_not_approved",
                "The requested evidence path is not named by the selected milestone prompt.",
                403,
            )
        if path == self.identity.prompt_path:
            revision = prompt
        elif path == closeout_path:
            latest = next(
                (event for event in reversed(view["snapshot"]["events"])
                 if event["event_type"] == "closeout_submitted"),
                None,
            )
            if latest is not None:
                try:
                    link = self.get_event_document_link(latest["event_id"], "closeout")
                except RevisionConflictError as exc:
                    raise ArchitectContractError(
                        "unverified_closeout_revision",
                        "The submission has no verified event-time revision; "
                        "read the archival source separately without treating it as the submitted revision.",
                        409,
                    ) from exc
                revision = self.get_revision(link["document_id"], link["revision_id"])
                revision_status = "submitted"
            elif view["closeout"] and view["closeout"]["draft_revision_id"]:
                document = view["closeout"]
                revision = self.get_revision(document["document_id"], document["draft_revision_id"])
                revision_status = "draft"
            else:
                raise ArchitectContractError("missing_source", "The selected closeout is not stored.", 404)
        else:
            documents: list[dict[str, Any]] = []
            cursor = 0
            while True:
                document_page = self.store.list_documents(after_order=cursor)
                documents.extend(item for item in document_page["documents"] if item["source_path"] == path)
                if document_page["next_cursor"] is None:
                    break
                cursor = document_page["next_cursor"]
            if not documents:
                if path.endswith(("_prompt.md", "_workflow.md", "_closeout.md")):
                    raise ArchitectContractError(
                        "missing_source", "Current workflow documents require an InformationStore revision.", 404
                    )
                if view["snapshot"]["prompt_revision_id"] != prompt["revision_id"]:
                    raise ArchitectContractError(
                        "unapproved_prompt", "Git-owned evidence requires the exact approved prompt revision.", 409
                    )
                sources = [
                    source.repository
                    for provider in self.library.providers
                    if isinstance(provider, GitFileProvider)
                    for source in provider.sources
                    if (
                        source.repository.repository_id == self.identity.repository_id
                        and source.repository.key == self.identity.repository_key
                        and str(source.repository.path) == self.identity.repository_root
                    )
                ]
                if len(sources) != 1:
                    raise ArchitectContractError(
                        "missing_source", "The explicit Git-owned evidence provider is unavailable.", 404
                    )
                page = read_selected_milestone_document_page(
                    sources[0],
                    {"milestone_id": self.identity.milestone_id, "prompt_path": self.identity.prompt_path},
                    path,
                    start_byte=start_byte,
                    source_fingerprint=source_fingerprint,
                    approved_prompt_content=prompt["content"],
                )
                current = self.workflow_snapshot()
                if (
                    current["store_revision"] != view["snapshot"]["store_revision"]
                    or current["prompt_revision_id"] != prompt["revision_id"]
                ):
                    raise ArchitectContractError(
                        "milestone_document_changed", "The selected authority changed; restart from byte zero.", 409
                    )
                page["authority_classification"] = "git_owned_milestone_evidence"
                return page
            if len(documents) != 1 or not documents[0]["draft_revision_id"]:
                raise ArchitectContractError("missing_source", "The selected evidence revision is ambiguous.", 409)
            revision = self.get_revision(documents[0]["document_id"], documents[0]["draft_revision_id"])
            revision_status = "draft"
        content = revision["content"]
        raw = content.encode("utf-8")
        if len(raw) > MAX_REPOSITORY_DOCUMENT_FILE_BYTES:
            raise ArchitectContractError("repository_document_oversized", "The selected revision is too large.", 413)
        if _PRIVATE_KEY_MARKER.search(content):
            raise ArchitectContractError("unsafe_source", "Selected document contains private-key material.", 422)
        fingerprint = hashlib.sha256(
            json.dumps(
                [
                    self.identity.repository_id,
                    self.identity.milestone_id,
                    path,
                    revision["revision_id"],
                    len(raw),
                    revision["content_sha256"],
                ],
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode("ascii")
        ).hexdigest()
        if source_fingerprint is not None and not hmac.compare_digest(source_fingerprint, fingerprint):
            raise ArchitectContractError(
                "milestone_document_changed", "The selected revision changed; restart from byte zero.", 409
            )
        try:
            page = self.store.read_revision_page(
                revision["document_id"],
                revision["revision_id"],
                byte_offset=start_byte,
                max_bytes=min(MAX_MILESTONE_DOCUMENT_PAGE_BYTES, MAX_DOCUMENT_PAGE_BYTES),
            )
        except ValueError as exc:
            raise ArchitectContractError("invalid_document_offset", str(exc), 400) from exc
        current = self.browser_snapshot()
        if (
            current["snapshot"]["store_revision"] != view["snapshot"]["store_revision"]
            or current["workflow"]["prompt_identity"] != view["workflow"]["prompt_identity"]
            or current["closeout"] != view["closeout"]
            or (
                path not in {self.identity.prompt_path, closeout_path}
                and self.get_revision(revision["document_id"], revision["revision_id"])["draft_revision_id"]
                != revision["revision_id"]
            )
        ):
            raise ArchitectContractError(
                "milestone_document_changed", "The selected authority changed; restart from byte zero.", 409
            )
        end = page["next_byte_offset"]
        line_start = raw.count(b"\n", 0, start_byte) + 1
        line_end = raw.count(b"\n", 0, end - 1) + 1 if end > start_byte else line_start
        return {
            "source_type": "selected_milestone_document",
            "authority_classification": "selected_milestone_evidence",
            "repository_key": self.identity.repository_key,
            "repository_id": self.identity.repository_id,
            "milestone_id": self.identity.milestone_id,
            "path": path,
            "file_size_bytes": len(raw),
            "sha256": revision["content_sha256"],
            "source_fingerprint": fingerprint,
            "start_byte": start_byte,
            "end_byte": end,
            "bytes_returned": end - start_byte,
            "character_count": len(page["content"]),
            "line_start": line_start,
            "line_end": line_end,
            "complete": page["complete"],
            "next_offset": None if page["complete"] else end,
            "page_limit_bytes": min(MAX_MILESTONE_DOCUMENT_PAGE_BYTES, MAX_DOCUMENT_PAGE_BYTES),
            "working_tree_source": "information_store_revision",
            "observed_at": revision["created_at"],
            "document_id": revision["document_id"],
            "revision_id": revision["revision_id"],
            "revision_status": revision_status,
            "content": page["content"],
        }

    def coder_snapshot(self) -> dict[str, Any]:
        snapshot = self.workflow_snapshot()
        prompt = self.selected_prompt()
        verified_snapshot = self.workflow_snapshot()
        if (
            snapshot["store_revision"] != verified_snapshot["store_revision"]
            or snapshot["prompt_revision_id"] != verified_snapshot["prompt_revision_id"]
            or snapshot["guidance_sha256"] != verified_snapshot["guidance_sha256"]
        ):
            raise FixtureIntegrationError("Store state changed while resolving the Coder authority snapshot.")
        return {
            "identity": self.identity,
            "snapshot": verified_snapshot,
            "prompt": prompt,
            "approved_architect_dispatches": list(reversed(verified_snapshot["confirmed_guidance"])),
            "unverified_approved_architect_dispatches": verified_snapshot["unverified_guidance"],
        }

    def runtime_binding(self) -> dict[str, Any]:
        snapshot = self.workflow_snapshot()
        prompt = self.selected_prompt() if snapshot["prompt_revision_id"] is not None else None
        current = self.workflow_snapshot()
        if (
            snapshot["store_revision"] != current["store_revision"]
            or snapshot["prompt_revision_id"] != current["prompt_revision_id"]
        ):
            raise FixtureIntegrationError("Store state changed while resolving the running reader binding.")
        configuration = self.store.authority_configuration
        return {
            "authority": self.authority,
            "database_identity": self.store.database_identity(),
            "authority_state_root": str(configuration.state_root) if configuration is not None else None,
            "authority_configuration_sha256": (
                configuration.configuration_sha256 if configuration is not None else None
            ),
            "authority_generation": configuration.generation if configuration is not None else None,
            "repository_id": self.identity.repository_id,
            "repository_key": self.identity.repository_key,
            "repository_root": self.identity.repository_root,
            "milestone_id": self.identity.milestone_id,
            "prompt_path": self.identity.prompt_path,
            "selection_id": self.identity.selection_id,
            "selection_revision": self.identity.selection_revision,
            "prompt_revision_id": snapshot["prompt_revision_id"],
            "prompt_sha256": prompt["content_sha256"] if prompt else None,
        }

    def browser_snapshot(self) -> dict[str, Any]:
        snapshot = self.workflow_snapshot()
        documents: list[dict[str, Any]] = []
        cursor = 0
        while True:
            page = self.store.list_documents(after_order=cursor)
            documents.extend(page["documents"])
            if page["next_cursor"] is None:
                break
            cursor = page["next_cursor"]
        prompts = [
            item for item in documents
            if item["logical_type"] == "PROMPT" and item["source_path"] == self.identity.prompt_path
        ]
        if len(prompts) != 1:
            raise FixtureIntegrationError("The selected milestone requires one exact stored prompt document.")
        prompt_document = prompts[0]
        prompt_revision_id = snapshot["prompt_revision_id"] or prompt_document["draft_revision_id"]
        if not prompt_revision_id:
            raise FixtureIntegrationError("The selected milestone prompt has no stored revision.")
        unverified_prompt = (
            snapshot["projection"]["phase"] != "PLANNING" and snapshot["prompt_revision_id"] is None
        )
        prompt = self.store.get_revision(prompt_document["document_id"], prompt_revision_id)
        closeout_path = self.identity.prompt_path.removesuffix("_prompt.md") + "_closeout.md"
        closeouts = [
            item for item in documents
            if item["logical_type"] == "CLOSEOUT" and item["source_path"] == closeout_path
        ]
        if len(closeouts) > 1:
            raise FixtureIntegrationError("The selected milestone contains ambiguous closeout documents.")
        candidate = {
            "milestone_id": self.identity.milestone_id,
            "prompt_path": self.identity.prompt_path,
            "title_slug": PurePosixPath(self.identity.prompt_path).name.split("_", 1)[-1].removesuffix("_prompt.md"),
            "closeout_exists": bool(closeouts),
            "closeout_path": closeout_path if closeouts else None,
        }
        events = snapshot["events"]
        questions = _open_question_ids(events)
        bugs = _open_bug_ids(events)
        fixable = _unfixed_bug_ids(events)
        workflow = dict(snapshot["projection"])
        workflow.update(
            status="valid",
            event_history=events,
            event_count=len(events),
            latest_events=events[-8:][::-1],
            open_questions=[event for event in events if event["event_id"] in questions],
            open_bugs=[event for event in events if event["event_id"] in bugs],
            fixable_bugs=[event for event in events if event["event_id"] in fixable],
            open_blocker=snapshot["projection"]["phase"] == "WAITING",
            latest_authoritative_decision=next(
                (
                    event for event in reversed(events)
                    if event["event_type"] in {"prompt_approved", "lock_in_approved", "acceptance_recorded"}
                    and event["po_approved"]
                ),
                None,
            ),
            sidecar_path="SQLite information store (no workflow sidecar write)",
            prompt_identity=(
                f"{self.identity.selection_id}:{self.identity.selection_revision}:"
                f"{prompt['document_id']}:{prompt_revision_id}:{prompt['content_sha256']}:"
                f"{'unverified' if unverified_prompt else 'selected'}"
            ),
            events=EVENT_OPTIONS,
            actors=ACTOR_OPTIONS,
            waiting_categories=("blocker", "clarification", "decision", "escalation", "other"),
            store_revision=snapshot["store_revision"],
        )
        if closeouts and closeouts[0]["draft_revision_id"]:
            closeout_revision = self.store.get_revision(
                closeouts[0]["document_id"], closeouts[0]["draft_revision_id"]
            )
            workflow["closeout_identity"] = (
                f"{closeout_revision['document_id']}:{closeout_revision['revision_id']}:"
                f"{closeout_revision['content_sha256']}"
            )
        else:
            workflow["closeout_identity"] = None
        workflow["next_action"] = _next_action(workflow, candidate, workflow["open_questions"])
        workflow["unverified_prompt"] = (
            {
                "document_id": prompt["document_id"],
                "revision_id": prompt_revision_id,
                "content_sha256": prompt["content_sha256"],
                "content": prompt["content"],
                "request_id": secrets.token_hex(16),
            }
            if unverified_prompt else None
        )
        if unverified_prompt:
            workflow["next_action"] = (
                "Product Owner must review and explicitly confirm the imported prompt revision; "
                "historical event-time bytes remain unverified."
            )
        closeout_event = next(
            (event for event in reversed(events) if event["event_type"] == "closeout_submitted"),
            None,
        )
        outstanding = []
        if closeout_event is not None:
            submitted_at = datetime.fromisoformat(closeout_event["timestamp"])
            for dispatch in snapshot["confirmed_guidance"]:
                if datetime.fromisoformat(dispatch["updated_at"]) > submitted_at:
                    body = " ".join(dispatch["body"].split())
                    outstanding.append(
                        {
                            "dispatch_id": dispatch["dispatch_id"],
                            "type": dispatch["dispatch_type"],
                            "approved_at": dispatch["updated_at"],
                            "excerpt": body[:320] + ("..." if len(body) > 320 else ""),
                        }
                    )
        workflow["outstanding_dispatches"] = outstanding
        workflow["unverified_guidance"] = snapshot["unverified_guidance"]
        current = self.workflow_snapshot()
        if (
            current["store_revision"] != snapshot["store_revision"]
            or current["prompt_revision_id"] != snapshot["prompt_revision_id"]
            or current["guidance_sha256"] != snapshot["guidance_sha256"]
        ):
            raise FixtureIntegrationError("Store authority changed during browser snapshot; refresh and retry.")
        return {
            "candidate": candidate,
            "workflow": workflow,
            "snapshot": snapshot,
            "prompt": prompt,
            "closeout": closeouts[0] if closeouts else None,
        }

    def record_browser_action(
        self,
        *,
        action: str,
        summary: str,
        expected_phase: str,
        expected_event_count: int,
        expected_prompt_identity: str,
        fields: dict[str, Any],
        fixture_po_confirmation: bool = False,
        managed_po_confirmation: bool = False,
    ) -> dict[str, Any]:
        confirmation = self._po_confirmation(fixture_po_confirmation, managed_po_confirmation)
        current = self.browser_snapshot()
        workflow = current["workflow"]
        snapshot = current["snapshot"]
        if workflow["unverified_prompt"] is not None:
            raise WorkflowRecordError("Confirm the exact imported prompt revision before recording workflow actions.")
        if (
            workflow["phase"] != expected_phase
            or workflow["event_count"] != expected_event_count
            or workflow["prompt_identity"] != expected_prompt_identity
        ):
            raise WorkflowRecordError("Workflow or prompt changed; refresh before submitting this action.")
        summary = summary.strip() if isinstance(summary, str) else ""
        if (
            not summary or len(summary) > 2000
            or any(ord(char) < 32 and char not in "\n\t" for char in summary)
        ):
            raise WorkflowRecordError("Event summary is required and must contain supported text.")
        if action == "milestone_closed":
            raise WorkflowRecordError("Use the guarded Accept & Close action to close a milestone.")
        if workflow["unverified_guidance"] and action in {
            "approve_and_start", "implementation_started", "resolve_and_resume",
        }:
            raise WorkflowRecordError(
                "Imported approved guidance lacks explicit decision evidence; "
                "Product Owner reconciliation is required before authorizing work."
            )
        if action == "closeout_submitted" and (
            workflow["closeout_identity"] is None
            or fields.get("expected_closeout_identity") != workflow["closeout_identity"]
        ):
            raise WorkflowRecordError("The closeout revision changed; refresh before submitting it for review.")
        events: list[dict[str, Any]] = []
        links: list[WorkflowDocumentLink] = []
        expected_drafts: dict[str, str] = {}

        def append(event_type: str, event_summary: str, event_fields: dict[str, Any] | None = None) -> dict[str, Any]:
            event = build_next_event(
                [*snapshot["events"], *events],
                event_type=event_type,
                summary=event_summary,
                actor_role="product_owner",
                po_approved=True,
                event_fields=event_fields,
            )
            events.append(event)
            return event

        def closeout_link(event: dict[str, Any], *, submitted: bool) -> None:
            if submitted:
                document = current["closeout"]
                if document is None or not document["draft_revision_id"]:
                    raise WorkflowRecordError("A stored closeout revision is required before review submission.")
                revision = self.store.get_revision(document["document_id"], document["draft_revision_id"])
                expected_drafts[document["document_id"]] = document["draft_revision_id"]
            else:
                latest = next(
                    (item for item in reversed(snapshot["events"]) if item["event_type"] == "closeout_submitted"),
                    None,
                )
                if latest is None:
                    raise WorkflowRecordError("Acceptance requires a submitted closeout.")
                revision = self.store.get_event_document_link(latest["event_id"], "closeout")
            links.append(
                WorkflowDocumentLink(
                    event_id=event["event_id"],
                    document_id=revision["document_id"],
                    revision_id=revision["revision_id"],
                    content_sha256=revision["content_sha256"],
                    link_role="closeout",
                    evidence="Exact closeout revision selected by the deliberate browser action.",
                )
            )

        if action == "approve_and_start":
            approved = append("prompt_approved", summary)
            append("implementation_started", f"Start {self.identity.milestone_id} implementation")
            receipt = self.store.select_approved_prompt_revision(
                request_id=approved["event_id"],
                expected_store_revision=snapshot["store_revision"],
                document_id=current["prompt"]["document_id"],
                revision_id=current["prompt"]["revision_id"],
                content_sha256=current["prompt"]["content_sha256"],
                prompt_approved_event=approved,
                following_events=events[1:],
                **confirmation,
                expected_guidance_sha256=snapshot["guidance_sha256"],
            )
            return {"events": events, "receipt": receipt}
        if action == "resolve_and_resume":
            resolution_type, blocking_id = waiting_resolution(workflow)
            if fields.get("related_event_id") != blocking_id:
                raise WorkflowRecordError("Resolve & Resume must reference the active blocking question.")
            append(
                resolution_type,
                summary,
                {"related_event_id": blocking_id} if blocking_id else None,
            )
            lock_in = fields.get("lock_in_summary")
            if lock_in:
                lock_in = lock_in.strip()
                if not lock_in or len(lock_in) > 2000 or any(
                    ord(char) < 32 and char not in "\n\t" for char in lock_in
                ):
                    raise WorkflowRecordError("Lock-in summary must contain supported text.")
                append("lock_in_approved", lock_in, {"binding": True})
            append("implementation_resumed", f"Resume {self.identity.milestone_id} implementation")
        elif action == "accept_and_close":
            if workflow["unverified_guidance"]:
                raise WorkflowRecordError(
                    "Imported approved guidance lacks explicit decision evidence; "
                    "Product Owner reconciliation is required before closure."
                )
            outstanding_ids = [item["dispatch_id"] for item in workflow["outstanding_dispatches"]]
            confirmed_ids = fields.get("outstanding_dispatch_ids")
            override_reason = fields.get("closure_override_reason")
            if outstanding_ids:
                if confirmed_ids != outstanding_ids or not isinstance(override_reason, str) or not override_reason.strip():
                    raise WorkflowRecordError("Review and explicitly confirm the current outstanding Architect guidance.")
                override_reason = override_reason.strip()
            elif confirmed_ids or override_reason:
                raise WorkflowRecordError("The outstanding-guidance condition changed; refresh before closing.")
            if not snapshot["events"] or snapshot["events"][-1]["event_type"] != "acceptance_recorded":
                accepted = append("acceptance_recorded", summary, {"binding": True})
                closeout_link(accepted, submitted=False)
            closing_fields: dict[str, Any] = {"binding": True}
            if outstanding_ids:
                closing_fields.update(
                    closure_override_reason=override_reason,
                    outstanding_dispatch_ids=outstanding_ids,
                )
            append("milestone_closed", summary, closing_fields)
        else:
            event_fields: dict[str, Any] = {}
            if action == "question_submitted":
                for key in ("waiting_category", "waiting_reason"):
                    if fields.get(key):
                        event_fields[key] = fields[key].strip()
            if action == "escalation_opened":
                event_fields.update(waiting_category="escalation", waiting_reason=fields.get("waiting_reason"))
            if action == "bug_classified":
                event_fields.update(
                    bug_scope=fields.get("bug_scope"), related_event_id=fields.get("related_event_id")
                )
                if fields.get("bug_scope") == "needs_escalation":
                    event_fields.update(waiting_category="escalation", waiting_reason=fields.get("waiting_reason"))
            if action in {"answer_recorded", "bug_fixed"}:
                event_fields["related_event_id"] = fields.get("related_event_id")
            if action == "decision_recorded":
                event_fields["binding"] = False
            if action in {"lock_in_approved", "acceptance_recorded"}:
                event_fields["binding"] = True
            if action == "test_result_recorded":
                event_fields.update(evidence_source=fields.get("evidence_source"), result=fields.get("result"))
            if action == "closeout_submitted":
                event_fields["evidence_path"] = current["candidate"]["closeout_path"]
            if action == "milestone_reopened":
                event_fields["reopen_target_phase"] = fields.get("reopen_target_phase")
            if action == "prompt_approved":
                raise WorkflowRecordError("Use Approve & Start to approve a stored prompt.")
            event = append(action, summary, event_fields)
            if action == "closeout_submitted":
                closeout_link(event, submitted=True)
            elif action == "acceptance_recorded":
                closeout_link(event, submitted=False)
        receipt = self.store.append_workflow_batch(
            request_id=events[0]["event_id"],
            expected_repository_id=self.identity.repository_id,
            expected_milestone_id=self.identity.milestone_id,
            expected_selection_id=self.identity.selection_id,
            expected_selection_revision=self.identity.selection_revision,
            expected_prompt_revision_id=snapshot["prompt_revision_id"],
            expected_store_revision=snapshot["store_revision"],
            events=events,
            links=links,
            **confirmation,
            expected_guidance_sha256=snapshot["guidance_sha256"],
            expected_document_drafts=expected_drafts or None,
        )
        return {"events": events, "receipt": receipt}

    def append_coder_event(
        self,
        *,
        event_type: str,
        summary: str,
        event_fields: dict[str, Any] | None = None,
        expected_store_revision: int | None = None,
        expected_prompt_revision_id: str | None = None,
        expected_guidance_sha256: str | None = None,
    ) -> dict[str, Any]:
        snapshot = self.workflow_snapshot()
        prompt_revision_id = snapshot["prompt_revision_id"]
        if not isinstance(prompt_revision_id, str) or not prompt_revision_id:
            raise FixtureIntegrationError("Coder workflow writes require an approved selected prompt revision.")
        if snapshot["unverified_guidance"] and event_type not in {
            "question_submitted", "bug_discovered", VERIFICATION_EVENT_TYPE,
        }:
            raise FixtureIntegrationError("Unverified imported guidance blocks Coder workflow writes.")
        if expected_store_revision is not None and snapshot["store_revision"] != expected_store_revision:
            raise FixtureIntegrationError("Workflow changed before the Coder event; refresh and retry.")
        if expected_prompt_revision_id is not None and prompt_revision_id != expected_prompt_revision_id:
            raise FixtureIntegrationError("Selected prompt changed before the Coder event; refresh and retry.")
        event = build_next_event(
            snapshot["events"],
            event_type=event_type,
            summary=summary,
            actor_role="coder",
            po_approved=False,
            event_fields=event_fields,
            actor_attribution=(
                "Reported through repoctl Coder interface; role and identity are not authenticated by Repo Control."
            ),
        )
        receipt = self.store.append_workflow_batch(
            request_id=event["event_id"],
            expected_repository_id=self.identity.repository_id,
            expected_milestone_id=self.identity.milestone_id,
            expected_selection_id=self.identity.selection_id,
            expected_selection_revision=self.identity.selection_revision,
            expected_prompt_revision_id=prompt_revision_id,
            expected_store_revision=snapshot["store_revision"],
            events=[event],
            expected_guidance_sha256=(
                snapshot["guidance_sha256"] if expected_guidance_sha256 is None else expected_guidance_sha256
            ),
        )
        return {"event": event, "receipt": receipt}

    def submit_coder_closeout(
        self,
        *,
        summary: str,
        document_id: str,
        revision_id: str,
        content_sha256: str,
        expected_prompt_revision_id: str,
        expected_store_revision: int,
    ) -> dict[str, Any]:
        snapshot = self.workflow_snapshot()
        if (
            snapshot["prompt_revision_id"] != expected_prompt_revision_id
            or snapshot["store_revision"] != expected_store_revision
            or snapshot["unverified_guidance"]
        ):
            raise FixtureIntegrationError("Store authority changed or guidance is unverified; refresh and retry.")
        revision = self.store.get_revision(document_id, revision_id)
        closeout_path = self.identity.prompt_path.removesuffix("_prompt.md") + "_closeout.md"
        if (
            revision["logical_type"] != "CLOSEOUT"
            or revision["source_path"] != closeout_path
            or revision["content_sha256"] != content_sha256
        ):
            raise FixtureIntegrationError("Closeout identity, canonical path, or content hash does not match.")
        event = build_next_event(
            snapshot["events"],
            event_type="closeout_submitted",
            summary=summary,
            actor_role="coder",
            po_approved=False,
            event_fields={"evidence_path": closeout_path},
            actor_attribution=(
                "Reported through repoctl Coder interface; role and identity are not authenticated by Repo Control."
            ),
        )
        receipt = self.store.append_workflow_batch(
            request_id=event["event_id"],
            expected_repository_id=self.identity.repository_id,
            expected_milestone_id=self.identity.milestone_id,
            expected_selection_id=self.identity.selection_id,
            expected_selection_revision=self.identity.selection_revision,
            expected_prompt_revision_id=expected_prompt_revision_id,
            expected_store_revision=expected_store_revision,
            events=[event],
            links=[
                WorkflowDocumentLink(
                    event_id=event["event_id"],
                    document_id=document_id,
                    revision_id=revision_id,
                    content_sha256=content_sha256,
                    link_role="closeout",
                    evidence="Exact immutable closeout revision selected by the Coder.",
                )
            ],
            expected_guidance_sha256=snapshot["guidance_sha256"],
        )
        return {"event": event, "receipt": receipt}

    def submit_coder_closeout_draft(
        self,
        *,
        summary: str,
        content: str,
        content_sha256: str,
        expected_prompt_revision_id: str,
        expected_store_revision: int,
    ) -> dict[str, Any]:
        snapshot = self.workflow_snapshot()
        if (
            snapshot["prompt_revision_id"] != expected_prompt_revision_id
            or snapshot["store_revision"] != expected_store_revision
            or snapshot["unverified_guidance"]
        ):
            raise FixtureIntegrationError("Store authority changed or guidance is unverified; refresh and retry.")
        if not isinstance(content, str) or hashlib.sha256(content.encode("utf-8")).hexdigest() != content_sha256:
            raise FixtureIntegrationError("Closeout draft bytes do not match the explicitly supplied hash.")
        closeout_path = self.identity.prompt_path.removesuffix("_prompt.md") + "_closeout.md"
        documents: list[dict[str, Any]] = []
        cursor = 0
        while True:
            page = self.store.list_documents(after_order=cursor)
            documents.extend(
                item for item in page["documents"]
                if item["logical_type"] == "CLOSEOUT" and item["source_path"] == closeout_path
            )
            if page["next_cursor"] is None:
                break
            cursor = page["next_cursor"]
        if len(documents) > 1:
            raise FixtureIntegrationError("Closeout draft has an ambiguous document identity.")
        document_id = documents[0]["document_id"] if documents else secrets.token_hex(16)
        parent_revision_id = documents[0]["draft_revision_id"] if documents else None
        revision_id = secrets.token_hex(16)
        event = build_next_event(
            snapshot["events"],
            event_type="closeout_submitted",
            summary=summary,
            actor_role="coder",
            po_approved=False,
            event_fields={"evidence_path": closeout_path},
            actor_attribution=(
                "Reported through repoctl Coder interface; role and identity are not authenticated by Repo Control."
            ),
        )
        receipt = self.store.submit_closeout_revision(
            request_id=event["event_id"],
            expected_prompt_revision_id=expected_prompt_revision_id,
            expected_store_revision=expected_store_revision,
            revision=DocumentRevisionInput(
                document_id=document_id,
                revision_id=revision_id,
                logical_key=closeout_path,
                logical_type="CLOSEOUT",
                source_path=closeout_path,
                content=content,
                parent_revision_id=parent_revision_id,
                source_kind="coder_draft",
                provenance={"explicit_draft_path": closeout_path, "content_sha256": content_sha256},
            ),
            closeout_submitted_event=event,
            expected_guidance_sha256=snapshot["guidance_sha256"],
        )
        return {
            "event": event,
            "receipt": receipt,
            "document_id": document_id,
            "revision_id": revision_id,
            "content_sha256": content_sha256,
        }

    def document_links(self, document_id: str, revision_id: str) -> list[dict[str, Any]]:
        return self.store.list_workflow_document_links(document_id, revision_id)

    def dispatch_snapshot(self, dispatch_id: str) -> dict[str, Any]:
        dispatch = self.store.get_dispatch(dispatch_id)
        if (
            dispatch["milestone_id"] != self.identity.milestone_id
            or dispatch["authorization_granted"] is not False
        ):
            raise FixtureIntegrationError("Dispatch snapshot escaped its fixture identity or authority boundary.")
        return {
            "dispatch": dispatch,
            "history": self.store.list_dispatch_history(dispatch_id),
            "authorization_granted": False,
            "authority": self.authority,
        }

    def create_browser_blueprint(self, *, name: str = "fixture_information_store_documents"):
        return create_document_library_blueprint(self.library, name=name)

    def create_publication_service(
        self,
        context: FixturePublicationContext | ManagedPublicationContext,
        git_observer: PublicationGitObserver,
    ) -> CloseoutPublicationService:
        return CloseoutPublicationService(self.store, context, git_observer)


class FixtureInformationStoreClients(InformationStoreApplicationClients):
    """Explicit disposable-fixture client bridge for isolated integration tests."""

    def __init__(
        self,
        store: InformationStore,
        library: DocumentLibrary,
        *,
        fixture_only: bool,
    ) -> None:
        if fixture_only is not True:
            raise FixtureIntegrationError("InformationStore client integration is restricted to fixtures.")
        if store.snapshot()["authority"] != "disposable_fixture_only":
            raise FixtureIntegrationError("Fixture clients require a disposable InformationStore.")
        super().__init__(store, library)


def create_managed_information_store_clients(
    configuration: StoreAuthorityConfiguration,
    repository: RepositoryEntry,
) -> InformationStoreApplicationClients:
    verify_store_registry(configuration)
    registry = RepositoryRegistry.from_toml(configuration.registry_path)
    verify_store_registry(configuration)
    matches = [
        entry for entry in registry.entries
        if entry.repository_id == repository.repository_id
        and entry.key == repository.key
        and entry.path == repository.path
    ]
    if len(matches) != 1 or matches[0] != repository:
        raise FixtureIntegrationError("Selected repository does not match the guarded registry.")
    database = configuration.database_path.resolve(strict=False)
    state_root = configuration.state_root
    for entry in registry.entries:
        root = Path(entry.path).resolve(strict=False)
        if database == root or database.is_relative_to(root):
            raise FixtureIntegrationError("The managed database must be outside every registered repository.")
        if state_root == root or state_root.is_relative_to(root) or root.is_relative_to(state_root):
            raise FixtureIntegrationError("The managed state root must be separate from every registered repository.")
    store = InformationStore.open_managed_from_external_selection(
        configuration,
        repository_id=repository.repository_id,
        repository_key=repository.key,
        repository_root=str(repository.path),
    )
    library = DocumentLibrary(
        [
            GitFileProvider([GitRepositoryContext(entry) for entry in registry.entries]),
            SQLiteRevisionProvider(store, repository_name=repository.name),
        ]
    )
    return InformationStoreApplicationClients(store, library)
