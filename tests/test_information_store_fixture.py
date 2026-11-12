from __future__ import annotations

import unittest
from pathlib import Path

from flask import Flask

from repoctl.document_library import DocumentLibrary, GitFileProvider, GitRepositoryContext, SQLiteRevisionProvider
from repoctl.information_store import DispatchDecisionError
from repoctl.web.repository_registry import RepositoryEntry
from repoctl.workflow.information_store_fixture import (
    FixtureInformationStoreClients,
    FixtureIntegrationError,
)
from tests.test_closeout_publication import _PublicationFixture


class FixtureInformationStoreIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = _PublicationFixture("accepted")
        self.addCleanup(self.fixture.cleanup)
        self.identity = self.fixture.store.identity
        root = Path(self.identity.repository_root)
        (root / "docs/context").mkdir(parents=True)
        (root / "docs/history/milestones").mkdir(parents=True)
        (root / self.identity.prompt_path).parent.mkdir(parents=True)
        (root / "docs/context/project_context.md").write_text("# Git-owned context\n", encoding="utf-8")
        (root / self.identity.prompt_path).write_text("# Prompt\n", encoding="utf-8")
        self.historical_path = "docs/history/milestones/12.65.0_legacy_prompt.md"
        self.historical_content = "# Historical 12.65.0\nExact archive evidence.\n"
        (root / self.historical_path).write_text(self.historical_content, encoding="utf-8")
        repository = RepositoryEntry(
            key=self.identity.repository_key,
            name="Fixture repository",
            path=root,
            repository_id=self.identity.repository_id,
            milestone_root="docs/milestones",
            documents={"context": "docs/context/project_context.md"},
        )
        self.library = DocumentLibrary(
            [
                GitFileProvider([GitRepositoryContext(repository, current_milestone_id=self.identity.milestone_id)]),
                SQLiteRevisionProvider(self.fixture.store),
            ],
            cursor_secret=b"integrated-information-store-cursor",
        )
        self.clients = FixtureInformationStoreClients(
            self.fixture.store,
            self.library,
            fixture_only=True,
        )

    def test_browser_coder_architect_workflow_dispatch_and_publication_share_fixture_binding(self) -> None:
        prompt_document = next(
            item
            for item in self.fixture.store.list_documents()["documents"]
            if item["source_path"] == self.identity.prompt_path
        )
        prompt = self.fixture.store.get_revision(prompt_document["document_id"], "case-prompt-revision")
        selected = self.clients.read_selected_prompt(
            "case-prompt-revision",
            prompt["content_sha256"],
        )
        self.assertEqual(selected["repository_id"], self.identity.repository_id)
        self.assertEqual(selected["repository_key"], self.identity.repository_key)
        self.assertEqual(selected["milestone_id"], self.identity.milestone_id)
        self.assertEqual(selected["prompt_path"], self.identity.prompt_path)
        self.assertEqual(selected["revision_id"], "case-prompt-revision")
        self.assertEqual(selected["content_sha256"], prompt["content_sha256"])
        self.assertEqual(selected["approval_state"], "fixture_approved")

        prompt_links = self.clients.document_links(prompt_document["document_id"], "case-prompt-revision")
        self.assertEqual(len(prompt_links), 1)
        self.assertEqual(prompt_links[0]["link_role"], "prompt")
        self.assertEqual(prompt_links[0]["content_sha256"], prompt["content_sha256"])
        closeout_links = self.clients.document_links(
            self.fixture.closeout["document_id"],
            self.fixture.closeout["revision_id"],
        )
        self.assertEqual(len(closeout_links), 2)
        self.assertTrue(all(item["link_role"] == "closeout" for item in closeout_links))
        self.assertTrue(
            all(item["content_sha256"] == self.fixture.closeout["content_sha256"] for item in closeout_links)
        )

        snapshot = self.clients.workflow_snapshot()
        self.assertEqual(snapshot["projection"]["phase"], "CLOSED")
        self.assertEqual(snapshot["event_count"], len(snapshot["events"]))

        dispatch_id = "a" * 32
        pending = self.fixture.store.create_pending_dispatch(
            dispatch_id=dispatch_id,
            dispatch_type="INSTRUCTION",
            body="Fixture guidance",
        )
        self.assertEqual(pending["status"], "PENDING")
        pending_view = self.clients.dispatch_snapshot(dispatch_id)
        self.assertFalse(pending_view["authorization_granted"])
        self.assertFalse(pending_view["dispatch"]["authorization_granted"])
        with self.assertRaisesRegex(DispatchDecisionError, "explicit trusted fixture"):
            self.fixture.store.record_dispatch_decision(
                dispatch_id=dispatch_id,
                history_id="b" * 32,
                status="APPROVED",
                actor_role="product_owner",
                actor_attribution="reported fixture context",
                approved_milestone_id=self.identity.milestone_id,
                trusted_fixture_po_confirmation=False,
            )
        approved = self.fixture.store.record_dispatch_decision(
            dispatch_id=dispatch_id,
            history_id="b" * 32,
            status="APPROVED",
            actor_role="product_owner",
            actor_attribution="reported fixture context",
            approved_milestone_id=self.identity.milestone_id,
            trusted_fixture_po_confirmation=True,
        )
        self.assertEqual(approved["status"], "APPROVED")
        self.assertFalse(approved["authorization_granted"])
        self.assertFalse(self.clients.dispatch_snapshot(dispatch_id)["authorization_granted"])

        query = {"repository_id": self.identity.repository_id, "milestone_text": "12.65.0"}
        coder_page = self.clients.coder.search(query="Exact archive evidence", filters=query)["data"]
        architect_page = self.clients.architect.search(query="Exact archive evidence", filters=query)["data"]
        self.assertEqual(coder_page["documents"], architect_page["documents"])
        self.assertEqual(coder_page["total_count"], 1)
        historical = coder_page["documents"][0]
        self.assertEqual(historical["path"], self.historical_path)
        self.assertEqual(historical["authority_classification"], "historical_evidence")
        self.assertEqual(historical["version_kind"], "current_worktree")
        coder_read = self.clients.coder.read_page(
            historical["document_id"],
            historical["revision_id"],
        )["data"]
        architect_read = self.clients.architect.read_page(
            historical["document_id"],
            historical["revision_id"],
        )["data"]
        self.assertEqual(coder_read, architect_read)
        self.assertEqual(coder_read["content"], self.historical_content)
        self.assertEqual(coder_read["content_sha256"], historical["content_sha256"])

        application = Flask(__name__)
        application.register_blueprint(
            self.clients.create_browser_blueprint(name="test_fixture_library"),
            url_prefix="/library",
        )
        response = application.test_client().get(
            f"/library/?q=Exact+archive+evidence&repository_id={self.identity.repository_id}"
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"12.65.0_legacy_prompt.md", response.data)

        service = self.clients.create_publication_service(
            self.fixture.service.context,
            self.fixture.observer,
        )
        self.assertIs(service.store, self.fixture.store)
        preview = service.preview(
            request_id="integrated-publication",
            document_id=self.fixture.closeout["document_id"],
            revision_id=self.fixture.closeout["revision_id"],
            expected_sha256=self.fixture.closeout["content_sha256"],
        )
        self.assertEqual(preview["status"], "preview_ready")

    def test_fixture_bridge_rejects_split_reader_or_nonfixture_activation(self) -> None:
        with self.assertRaisesRegex(FixtureIntegrationError, "restricted to fixtures"):
            FixtureInformationStoreClients(
                self.fixture.store,
                self.library,
                fixture_only=False,
            )

        no_store_library = DocumentLibrary(
            [provider for provider in self.library.providers if not isinstance(provider, SQLiteRevisionProvider)],
        )
        with self.assertRaisesRegex(FixtureIntegrationError, "exactly one SQLite reader"):
            FixtureInformationStoreClients(
                self.fixture.store,
                no_store_library,
                fixture_only=True,
            )

        second_store = type(self.fixture.store).initialize_disposable(
            Path(self.fixture.temporary.name) / "second-store.sqlite3",
            self.identity,
            allow_unsupported_runtime_for_fixture=True,
        )
        second_provider = SQLiteRevisionProvider(second_store)
        second_provider.provider_id += ":split"
        split_library = DocumentLibrary(
            [
                *[
                    provider
                    for provider in self.library.providers
                    if not isinstance(provider, SQLiteRevisionProvider)
                ],
                SQLiteRevisionProvider(self.fixture.store),
                second_provider,
            ],
        )
        with self.assertRaisesRegex(FixtureIntegrationError, "exactly one SQLite reader"):
            FixtureInformationStoreClients(
                self.fixture.store,
                split_library,
                fixture_only=True,
            )


if __name__ == "__main__":
    unittest.main()
