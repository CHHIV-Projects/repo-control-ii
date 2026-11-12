from __future__ import annotations

import re
from typing import Any

from ..document_library import (
    ArchitectDocumentAdapter,
    CoderDocumentAdapter,
    DocumentLibrary,
    SQLiteRevisionProvider,
)
from ..information_store import InformationStore, StoreIdentity
from ..web.document_library import create_document_library_blueprint
from .closeout_publication import (
    CloseoutPublicationService,
    FixturePublicationContext,
    PublicationGitObserver,
)
from .lifecycle import _reduce_events


_SHA256 = re.compile(r"[a-f0-9]{64}\Z")


class FixtureIntegrationError(RuntimeError):
    pass


class FixtureInformationStoreClients:
    """Explicit test-only client bridge; it is never registered by the production app."""

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
        self.store = store
        self.library = library
        self.identity: StoreIdentity = identity
        self.coder = CoderDocumentAdapter(library)
        self.architect = ArchitectDocumentAdapter(library)

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
                    "source": "sqlite_fixture",
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
            "authority": "disposable_fixture_only",
        }

    def create_browser_blueprint(self, *, name: str = "fixture_information_store_documents"):
        return create_document_library_blueprint(self.library, name=name)

    def create_publication_service(
        self,
        context: FixturePublicationContext,
        git_observer: PublicationGitObserver,
    ) -> CloseoutPublicationService:
        if context.test_only is not True:
            raise FixtureIntegrationError("Publication context must be an explicit fixture.")
        return CloseoutPublicationService(self.store, context, git_observer)
