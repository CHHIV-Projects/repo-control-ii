from __future__ import annotations

import unittest
from copy import deepcopy
from contextlib import closing, redirect_stderr, redirect_stdout
from dataclasses import replace
from io import StringIO
import json
from pathlib import Path
import sqlite3
import threading
from unittest.mock import Mock, patch

from flask import Flask

from repoctl.document_library import DocumentLibrary, GitFileProvider, GitRepositoryContext, SQLiteRevisionProvider
from repoctl.information_store import (
    DispatchDecisionError,
    DocumentRevisionInput,
    InformationStore,
    StoreIdentity,
    V1FixtureBundle,
    WorkflowDocumentLink,
    dispatch_reconciliation_fingerprint,
)
from repoctl.web.repository_registry import RepositoryEntry, RepositoryRegistry
from repoctl.web.app import create_web_app
from repoctl.cli import main as cli_main
from repoctl.mcp.architect_adapter import handle_rpc_request
from repoctl.procedure_release import inspect_procedure_release_preparation
from repoctl.workflow.information_store_fixture import (
    FixtureInformationStoreClients,
    FixtureIntegrationError,
)
from repoctl.workflow import lifecycle
from repoctl.workflow.coder_interface import (
    CoderInterfaceError,
    get_coder_context,
    read_coder_milestone_document_page,
    submit_coder_question,
)
from tests.test_closeout_publication import _PublicationFixture, _closeout_history


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
                GitFileProvider([GitRepositoryContext(repository)]),
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

        repository = self.library.providers[0].sources[0].repository
        unsafe_library = DocumentLibrary([
            GitFileProvider([GitRepositoryContext(repository, self.identity.milestone_id)]),
            SQLiteRevisionProvider(self.fixture.store),
        ])
        with self.assertRaisesRegex(FixtureIntegrationError, "cannot be configured as current authority"):
            FixtureInformationStoreClients(self.fixture.store, unsafe_library, fixture_only=True)

    @patch("repoctl.web.app.generate_milestone_status")
    def test_architect_history_pages_configured_sources_and_reads_exact_revisions(self, _status) -> None:
        repository = self.library.providers[0].sources[0].repository
        registry = RepositoryRegistry((repository,), repository.key)
        store = self.fixture.store
        first = store.create_immutable_revision(DocumentRevisionInput(
            document_id="archive-document",
            revision_id="archive-revision-1",
            logical_key="12.65.0_legacy_prompt.md",
            logical_type="OTHER",
            source_path=self.historical_path,
            content="# First stored archive revision\n",
        ))
        store.create_immutable_revision(DocumentRevisionInput(
            document_id=first["document_id"],
            revision_id="archive-revision-2",
            logical_key="12.65.0_legacy_prompt.md",
            logical_type="OTHER",
            source_path=self.historical_path,
            parent_revision_id=first["revision_id"],
            content="# Second stored archive revision\n",
        ))
        with patch("repoctl.web.app.RepositoryRegistry.single", return_value=registry):
            app = create_web_app(
                repository_path=self.identity.repository_root,
                state_root=Path(self.fixture.temporary.name) / "history-state",
                information_store_clients=self.clients,
            )
        app.config["TESTING"] = True
        client = app.test_client()
        generation = client.get("/healthz").json["active_repository_generation"]
        common = {"expected_repository_key": repository.key, "expected_generation": generation}

        selected_git = self.library.search(
            filters={"milestone_id": self.identity.milestone_id, "source": "git_worktree"}
        )["documents"]
        self.assertTrue(selected_git)
        self.assertTrue(all(item["authority_classification"] == "historical_evidence" for item in selected_git))

        archive = repository.path / "docs/history/milestones"
        for index in range(551):
            (archive / f"12.65.0_bulk_{index:03d}_prompt.md").write_text(
                f"# Bulk marker {index:03d}\n", encoding="utf-8"
            )
        search_url = "/api/architect/v1/history/search"
        filters = {**common, "query": "Bulk marker", "milestone_id": "12.65.0", "limit": 20}
        first_page = client.get(search_url, query_string=filters)
        self.assertEqual(first_page.status_code, 200, first_page.json)
        self.assertEqual(first_page.json["data"]["total_count"], 551)
        cursor = first_page.json["data"]["next_cursor"]
        self.assertEqual(client.get(
            search_url, query_string={**filters, "query": "different", "cursor": cursor}
        ).status_code, 400)
        (archive / "12.65.0_bulk_550_prompt.md").write_text("# Bulk marker changed\n", encoding="utf-8")
        self.assertEqual(client.get(search_url, query_string={**filters, "cursor": cursor}).status_code, 409)

        paths = []
        cursor = None
        while True:
            response = client.get(search_url, query_string={**filters, **({"cursor": cursor} if cursor else {})})
            self.assertEqual(response.status_code, 200, response.json)
            data = response.json["data"]
            paths.extend(item["path"] for item in data["results"])
            self.assertTrue(all(item["authority_classification"] == "historical_evidence" for item in data["results"]))
            self.assertTrue(data["total_count_complete"])
            if not data["has_more"]:
                break
            cursor = data["next_cursor"]
        self.assertEqual(len(paths), 551)
        self.assertEqual(len(set(paths)), 551)
        self.assertTrue(paths[-1].endswith("12.65.0_bulk_550_prompt.md"))

        search = client.get(search_url, query_string={**common, "query": "legacy", "milestone_id": "12.65.0"})
        self.assertEqual(search.status_code, 200, search.json)
        revisions = search.json["data"]["results"]
        self.assertEqual(len(revisions), 3)
        document_url = "/api/architect/v1/history/document"
        self.assertEqual(client.get(
            document_url, query_string={**common, "path": self.historical_path}
        ).status_code, 409)
        for item in revisions:
            response = client.get(document_url, query_string={
                **common,
                "path": self.historical_path,
                "document_id": item["document_id"],
                "revision_id": item["revision_id"],
            })
            self.assertEqual(response.status_code, 200, response.json)
            self.assertEqual(response.json["data"]["content_sha256"], item["content_sha256"])
            self.assertFalse(response.json["data"]["is_current_authority"])
        large_path = "docs/history/milestones/12.65.0_large_prompt.md"
        large_content = "# Large archive\n" + "é" * 70_000
        (repository.path / large_path).write_text(large_content, encoding="utf-8")
        chunks = []
        cursor = None
        while True:
            response = client.get(document_url, query_string={
                **common, "path": large_path, **({"cursor": cursor} if cursor else {}),
            })
            self.assertEqual(response.status_code, 200, response.json)
            page = response.json["data"]
            chunks.append(page["content"])
            if not page["truncated"]:
                break
            cursor = page["next_cursor"]
        self.assertEqual("".join(chunks), large_content)
        self.assertEqual(len(chunks), 3)
        self.assertEqual(client.get(document_url, query_string={
            **common, "path": self.identity.prompt_path,
        }).status_code, 409)

        def bridge_get(_base_url: str, path: str, query: dict[str, str], timeout: float = 10.0):
            response = client.get(path, query_string=query)
            return response.status_code, response.json

        exact = revisions[0]
        with patch("repoctl.mcp.architect_adapter.execute_get", side_effect=bridge_get):
            rpc = handle_rpc_request({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "repo_history_document",
                    "arguments": {
                        **common,
                        "path": self.historical_path,
                        "document_id": exact["document_id"],
                        "revision_id": exact["revision_id"],
                    },
                },
            }, "http://fixture", StringIO())
        self.assertFalse(rpc["result"]["isError"])
        self.assertEqual(
            json.loads(rpc["result"]["content"][0]["text"])["data"]["content_sha256"],
            exact["content_sha256"],
        )

    @patch("repoctl.web.app.generate_milestone_status")
    def test_browser_and_cli_rebind_to_the_external_fixture_selection(self, _status) -> None:
        repository = self.library.providers[0].sources[0].repository
        registry = RepositoryRegistry((repository,), repository.key)
        second_identity = replace(
            self.identity,
            milestone_id="M999.002",
            prompt_path="docs/milestones/M999.002_second_prompt.md",
            selection_id="second-selection",
            selection_revision=2,
        )
        second = self.fixture.store.register_disposable_scope(second_identity)
        second.create_immutable_revision(DocumentRevisionInput(
            document_id="second-document",
            revision_id="second-revision",
            logical_key=second_identity.prompt_path,
            logical_type="PROMPT",
            source_path=second_identity.prompt_path,
            content="# Second fixture prompt\n",
        ))
        state = Path(self.fixture.temporary.name) / "external-selection-state"
        pointer = state / self.identity.repository_id / "milestones" / "selection.json"
        pointer.parent.mkdir(parents=True)
        pointer.write_text(json.dumps({
            "schema_version": 1,
            "repository_id": self.identity.repository_id,
            "milestone_id": second_identity.milestone_id,
            "prompt_path": second_identity.prompt_path,
            "selection_id": second_identity.selection_id,
            "selection_revision": second_identity.selection_revision,
        }), encoding="utf-8")

        def make_clients() -> FixtureInformationStoreClients:
            store = InformationStore.open_disposable(
                self.fixture.store.path, second_identity,
                allow_unsupported_runtime_for_fixture=True,
                selection_state_root=state,
            )
            library = DocumentLibrary([
                GitFileProvider([GitRepositoryContext(repository)]),
                SQLiteRevisionProvider(store),
            ])
            return FixtureInformationStoreClients(store, library, fixture_only=True)

        clients = make_clients()
        other_clients = make_clients()
        with patch("repoctl.web.app.RepositoryRegistry.single", side_effect=lambda _path: registry):
            app = create_web_app(
                repository_path=self.identity.repository_root,
                state_root=state,
                information_store_clients=clients,
            )
            other_app = create_web_app(
                repository_path=self.identity.repository_root,
                state_root=state,
                information_store_clients=other_clients,
            )
        app.config["TESTING"] = True
        other_app.config["TESTING"] = True
        browser = app.test_client()
        other_browser = other_app.test_client()
        page = browser.get("/milestones")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"M999.001", page.data)
        self.assertIn(b"M999.002", page.data)
        with browser.session_transaction() as browser_session:
            csrf_token = browser_session["csrf_token"]
        form = {
            "csrf_token": csrf_token,
            "repository_key": repository.key,
            "repository_generation": "0",
            "milestone_id": self.identity.milestone_id,
            "expected_selection_id": second_identity.selection_id,
            "expected_selection_revision": str(second_identity.selection_revision),
        }
        self.assertEqual(browser.post("/milestones/select", data=form).status_code, 302)
        self.assertEqual(browser.post("/milestones/select", data=form).status_code, 409)
        self.assertEqual(json.loads(pointer.read_text(encoding="utf-8"))["milestone_id"], self.identity.milestone_id)
        other_health = other_browser.get("/healthz")
        self.assertEqual(other_health.status_code, 200, other_health.json)
        self.assertEqual(other_health.json["information_store_binding"]["milestone_id"], self.identity.milestone_id)
        prompt = other_browser.get("/api/architect/v1/prompt")
        self.assertEqual(prompt.status_code, 200, prompt.json)
        self.assertEqual(prompt.json["data"]["content"], "# Prompt\n")
        self.assertEqual(other_clients.identity.milestone_id, self.identity.milestone_id)
        history = other_browser.get("/api/architect/v1/history/search", query_string={
            "expected_repository_key": repository.key,
            "expected_generation": other_health.json["active_repository_generation"],
            "milestone_id": "M999.002",
        })
        self.assertEqual(history.status_code, 200, history.json)
        historical = history.json["data"]["results"]
        second_revision = next(
            item for item in historical
            if item["source_kind"] == "sqlite_fixture" and item["path"] == second_identity.prompt_path
        )
        exact = other_clients.read_historical(
            repository,
            second_identity.prompt_path,
            document_id=second_revision["document_id"],
            revision_id=second_revision["revision_id"],
        )
        self.assertEqual(exact["content"], "# Second fixture prompt\n")

        output = StringIO()
        with (
            patch("repoctl.workflow.coder_interface.resolve_repository_entry", return_value=repository),
            redirect_stdout(output),
        ):
            result = cli_main(["coder-context", "--format", "json"], information_store_clients=other_clients)
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output.getvalue())["milestone"]["id"], self.identity.milestone_id)

    def test_cli_web_registers_the_same_injected_authority_without_holding_selection_lock(self) -> None:
        repository = self.library.providers[0].sources[0].repository
        registry = RepositoryRegistry((repository,), repository.key)
        responses = []

        def serve_once(app: Flask, **_kwargs) -> None:
            worker = threading.Thread(target=lambda: responses.append(app.test_client().get("/healthz")))
            worker.start()
            worker.join(timeout=3)
            self.assertFalse(worker.is_alive(), "The CLI held the selection lock while serving requests.")

        with (
            patch("repoctl.web.app.RepositoryRegistry.single", return_value=registry),
            patch("repoctl.web.app.DEFAULT_STATE_ROOT", Path(self.fixture.temporary.name) / "web-cli-state"),
            patch("repoctl.web.app.generate_milestone_status"),
            patch.object(Flask, "run", autospec=True, side_effect=serve_once),
        ):
            result = cli_main(
                ["web", "--repository", self.identity.repository_root],
                information_store_clients=self.clients,
            )
        self.assertEqual(result, 0)
        self.assertEqual(len(responses), 1)
        self.assertEqual(responses[0].status_code, 200)
        self.assertEqual(responses[0].json["information_store_binding"], self.clients.runtime_binding())

    @patch("repoctl.web.app.generate_milestone_status")
    def test_browser_repository_switch_binds_the_same_database_or_fails_without_switching(self, _status) -> None:
        first_repository = self.library.providers[0].sources[0].repository
        other_root = Path(self.fixture.temporary.name) / "other-repository"
        (other_root / "docs/milestones").mkdir(parents=True)
        other_identity = replace(
            self.identity,
            repository_id="other-fixture-id",
            repository_key="other-fixture",
            repository_root=str(other_root),
            milestone_id="M999.002",
            prompt_path="docs/milestones/M999.002_other_prompt.md",
            selection_id="other-selection",
            selection_revision=1,
        )
        other_store = self.fixture.store.register_disposable_scope(other_identity)
        other_store.create_immutable_revision(DocumentRevisionInput(
            document_id="other-prompt",
            revision_id="other-revision",
            logical_key=other_identity.prompt_path,
            logical_type="PROMPT",
            source_path=other_identity.prompt_path,
            content="# Other prompt\n",
        ))
        other_repository = RepositoryEntry(
            key=other_identity.repository_key,
            name="Other fixture",
            path=other_root,
            repository_id=other_identity.repository_id,
            milestone_root="docs/milestones",
            documents={},
        )
        registry = RepositoryRegistry((first_repository, other_repository), first_repository.key)
        state = Path(self.fixture.temporary.name) / "repository-switch-state"

        def write_pointer(identity: StoreIdentity) -> None:
            pointer = state / identity.repository_id / "milestones" / "selection.json"
            pointer.parent.mkdir(parents=True, exist_ok=True)
            pointer.write_text(json.dumps({
                "schema_version": 1,
                "repository_id": identity.repository_id,
                "milestone_id": identity.milestone_id,
                "prompt_path": identity.prompt_path,
                "selection_id": identity.selection_id,
                "selection_revision": identity.selection_revision,
            }), encoding="utf-8")

        write_pointer(self.identity)
        first_store = InformationStore.open_disposable(
            self.fixture.store.path, self.identity,
            selection_state_root=state, allow_unsupported_runtime_for_fixture=True,
        )
        library = DocumentLibrary([
            GitFileProvider([GitRepositoryContext(first_repository), GitRepositoryContext(other_repository)]),
            SQLiteRevisionProvider(first_store),
        ])
        clients = FixtureInformationStoreClients(first_store, library, fixture_only=True)
        with patch("repoctl.web.app.RepositoryRegistry.single", return_value=registry):
            app = create_web_app(
                repository_path=self.identity.repository_root,
                state_root=state,
                information_store_clients=clients,
            )
        app.config["TESTING"] = True
        browser = app.test_client()
        self.assertEqual(browser.get("/milestones").status_code, 200)
        with browser.session_transaction() as browser_session:
            csrf_token = browser_session["csrf_token"]
        form = {
            "csrf_token": csrf_token,
            "repository_key": other_repository.key,
            "repository_generation": "0",
        }
        self.assertEqual(browser.post("/repositories/select", data=form).status_code, 409)
        self.assertEqual(registry.active(), first_repository)
        self.assertEqual(clients.identity, self.identity)
        write_pointer(other_identity)
        switched = browser.post("/repositories/select", data=form)
        self.assertEqual(switched.status_code, 302)
        self.assertTrue(switched.location.endswith("/milestones"))
        self.assertEqual(registry.active(), other_repository)
        self.assertEqual(clients.identity, other_identity)
        self.assertEqual(browser.get("/healthz").json["information_store_binding"]["repository_id"], other_identity.repository_id)
        self.assertEqual(browser.get("/milestones").status_code, 200)
        self.assertEqual(browser.post("/repositories/select", data=form).status_code, 409)
        self.assertEqual(registry.active(), other_repository)
        self.assertEqual(browser.post("/repositories/select", data={
            **form, "repository_key": first_repository.key, "repository_generation": "1",
        }).status_code, 302)
        self.assertEqual(clients.identity, self.identity)
        self.assertEqual(registry.active(), first_repository)

    @patch("repoctl.web.app.generate_milestone_status")
    def test_imported_active_prompt_requires_a_new_exact_fixture_confirmation(self, _status) -> None:
        repository = self.library.providers[0].sources[0].repository
        identity = replace(self.identity, selection_id="imported-active-selection")
        store = InformationStore.initialize_disposable(
            Path(self.fixture.temporary.name) / "imported-active.sqlite3",
            identity,
            allow_unsupported_runtime_for_fixture=True,
        )
        events = _closeout_history()[:2]
        ledger = lifecycle._empty_ledger(
            identity.repository_id,
            {"milestone_id": identity.milestone_id, "prompt_path": identity.prompt_path},
        )
        ledger["events"] = events
        workflow_bytes = lifecycle._render_sidecar(ledger).encode("utf-8")
        bundle = V1FixtureBundle(
            prompt_path=identity.prompt_path,
            closeout_path=None,
            workflow_path=identity.prompt_path.removesuffix("_prompt.md") + "_workflow.md",
            prompt_bytes=b"# Imported active prompt\n",
            closeout_bytes=None,
            workflow_bytes=workflow_bytes,
        )
        imported = store.import_v1_fixture(bundle)
        clients = FixtureInformationStoreClients(
            store,
            DocumentLibrary([GitFileProvider([GitRepositoryContext(repository)]), SQLiteRevisionProvider(store)]),
            fixture_only=True,
        )
        registry = RepositoryRegistry((repository,), repository.key)
        with patch("repoctl.web.app.RepositoryRegistry.single", return_value=registry):
            app = create_web_app(
                repository_path=identity.repository_root,
                state_root=Path(self.fixture.temporary.name) / "imported-active-state",
                information_store_clients=clients,
            )
        app.config["TESTING"] = True
        browser = app.test_client()
        page = browser.get("/milestones")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"historical workflow does not establish", page.data)
        self.assertIn(b"Imported active prompt", page.data)
        fragment = browser.get("/milestones/workflow/fragment", query_string={
            "repository_key": repository.key,
            "repository_generation": "0",
            "milestone_id": identity.milestone_id,
            "prompt_path": identity.prompt_path,
        })
        self.assertEqual(fragment.status_code, 200)
        data = clients.browser_snapshot()
        with self.assertRaisesRegex(lifecycle.WorkflowRecordError, "Confirm the exact imported prompt"):
            clients.record_browser_action(
                action="test_result_recorded",
                summary="Must not write yet",
                expected_phase=data["workflow"]["phase"],
                expected_event_count=data["workflow"]["event_count"],
                expected_prompt_identity=data["workflow"]["prompt_identity"],
                fields={"evidence_source": "fixture", "result": "not written"},
                fixture_po_confirmation=True,
            )
        with self.assertRaises(FixtureIntegrationError):
            clients.coder_snapshot()
        prompt = data["workflow"]["unverified_prompt"]
        with browser.session_transaction() as browser_session:
            csrf_token = browser_session["csrf_token"]
        form = {
            "csrf_token": csrf_token,
            "repository_key": repository.key,
            "repository_generation": "0",
            "selection_id": identity.selection_id,
            "selection_revision": str(identity.selection_revision),
            "store_revision": str(data["snapshot"]["store_revision"]),
            "document_id": prompt["document_id"],
            "revision_id": prompt["revision_id"],
            "content_sha256": prompt["content_sha256"],
            "request_id": prompt["request_id"],
        }
        url = "/milestones/confirm-imported-prompt"
        self.assertEqual(browser.post(url, data=form).status_code, 403)
        confirmed = {**form, "fixture_po_confirmation": "confirmed"}
        self.assertEqual(browser.post(url, data={**confirmed, "content_sha256": "0" * 64}).status_code, 409)
        self.assertEqual(browser.post(url, data={**confirmed, "selection_id": "stale"}).status_code, 409)
        self.assertEqual(browser.post(url, data=confirmed).status_code, 302)
        self.assertEqual(browser.post(url, data=confirmed).status_code, 302)
        self.assertEqual(browser.post(url, data={**confirmed, "content_sha256": "0" * 64}).status_code, 409)
        self.assertEqual(clients.selected_prompt()["content"], "# Imported active prompt\n")
        self.assertEqual(store.snapshot()["events"], events)
        self.assertEqual(store.snapshot()["projection"]["phase"], "IN_PROGRESS")
        self.assertEqual(store.import_v1_fixture(bundle), imported)
        with sqlite3.connect(store.path) as connection:
            decision = connection.execute(
                "SELECT po_confirmed, source_kind, payload_json FROM document_revision_decision"
            ).fetchone()
            self.assertEqual(decision[:2], (1, "fixture_reconfirmation"))
            self.assertEqual(json.loads(decision[2])["historical_event_time_revision"], "UNKNOWN/NOT VERIFIED")
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM workflow_document_link").fetchone()[0], 0)
        output = StringIO()
        with (
            patch("repoctl.workflow.coder_interface.resolve_repository_entry", return_value=repository),
            redirect_stdout(output),
        ):
            result = cli_main(["coder-context", "--format", "json"], information_store_clients=clients)
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output.getvalue())["milestone"]["phase"], "IN_PROGRESS")

    @patch("repoctl.web.app.generate_milestone_status")
    def test_dispatch_pages_cover_other_milestones_and_reject_stale_cursors(self, _status) -> None:
        repository = self.library.providers[0].sources[0].repository
        registry = RepositoryRegistry((repository,), repository.key)
        store = self.fixture.store
        for index in range(7):
            store.create_pending_dispatch(
                dispatch_id=f"{index + 1:032x}",
                dispatch_type="PROMPT",
                milestone_id=f"M999.{index + 10:03d}",
                milestone_root=repository.milestone_root,
                title=f"Future fixture {index}",
                body=f"# Future fixture {index}\n",
            )
        with patch("repoctl.web.app.RepositoryRegistry.single", return_value=registry):
            app = create_web_app(
                repository_path=self.identity.repository_root,
                state_root=Path(self.fixture.temporary.name) / "dispatch-page-state",
                information_store_clients=self.clients,
            )
        app.config["TESTING"] = True
        browser = app.test_client()
        generation = browser.get("/healthz").json["active_repository_generation"]
        filters = {
            "expected_repository_key": repository.key,
            "expected_generation": generation,
            "limit": 2,
        }
        ids = []
        cursor = None
        stale_cursor = None
        while True:
            response = browser.get("/api/architect/v1/dispatches", query_string={
                **filters, **({"cursor": cursor} if cursor else {}),
            })
            self.assertEqual(response.status_code, 200, response.json)
            page = response.json["data"]
            ids.extend(item["dispatch_id"] for item in page["dispatches"])
            if not page["has_more"]:
                break
            cursor = page["next_cursor"]
            if stale_cursor is None:
                stale_cursor = cursor
        self.assertEqual(len(ids), 7)
        self.assertEqual(len(set(ids)), 7)
        self.assertTrue(page["complete"])
        filtered = browser.get("/api/architect/v1/dispatches", query_string={
            **filters, "milestone_id": "M999.016",
        })
        self.assertEqual(filtered.status_code, 200)
        self.assertEqual(filtered.json["data"]["dispatches"][0]["milestone_id"], "M999.016")
        review = browser.get("/architect-dispatches")
        self.assertEqual(review.status_code, 200)
        self.assertIn(b"Next dispatches", review.data)
        store.create_pending_dispatch(
            dispatch_id="f" * 32, dispatch_type="PROMPT", milestone_id="M999.099",
            milestone_root=repository.milestone_root, title="Later fixture", body="# Later fixture\n",
        )
        stale = browser.get("/api/architect/v1/dispatches", query_string={
            **filters, "cursor": stale_cursor,
        })
        self.assertEqual(stale.status_code, 409, stale.json)

    @patch("repoctl.web.app.generate_milestone_status")
    def test_existing_git_review_routes_remain_available_with_injected_authority(self, _status) -> None:
        repository = self.library.providers[0].sources[0].repository
        registry = RepositoryRegistry((repository,), repository.key)
        with patch("repoctl.web.app.RepositoryRegistry.single", return_value=registry):
            app = create_web_app(
                repository_path=self.identity.repository_root,
                state_root=Path(self.fixture.temporary.name) / "git-review-state",
                information_store_clients=self.clients,
            )
        app.config["TESTING"] = True
        browser = app.test_client()
        history = {"commits": [], "has_more": False, "complete": True, "selected": None}
        with patch("repoctl.web.app.inspect_history", return_value=history):
            page = browser.get("/git-review?view=history")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Git Review", page.data)
        with browser.session_transaction() as browser_session:
            csrf_token = browser_session["csrf_token"]
        with (
            patch("repoctl.web.app.PreparedGitActions.prepare", return_value=("fixture-token", {})) as prepare,
            patch("repoctl.web.app.render_template", return_value="fixture confirmation"),
        ):
            preview = browser.post("/git-review/action/prepare", data={
                "csrf_token": csrf_token, "action": "stage", "path_token": "fixture-path",
            })
        self.assertEqual(preview.status_code, 200)
        prepare.assert_called_once()
        with patch(
            "repoctl.web.app.PreparedGitActions.confirm",
            return_value={"message": "Mock confirmation", "action": "stage"},
        ) as confirm:
            completed = browser.post("/git-review/action/confirm", data={
                "csrf_token": csrf_token, "action": "stage", "token": "fixture-token",
            })
        self.assertEqual(completed.status_code, 302)
        confirm.assert_called_once()

    @patch("repoctl.web.repository_registry.validate_git_worktree", side_effect=lambda path: Path(path).resolve())
    @patch("repoctl.web.app.generate_milestone_status")
    def test_browser_reconciles_imported_guidance_only_with_a_new_explicit_decision(
        self, _status, _registry_git
    ) -> None:
        registry_path = Path(__file__).resolve().parents[1] / "ops" / "repositories.toml"
        repository = RepositoryRegistry.from_toml(registry_path).active()
        identity = StoreIdentity(
            repository_id=repository.repository_id,
            repository_key=repository.key,
            repository_root=str(repository.path),
            milestone_id="M010.010",
            prompt_path="docs/milestones/M010.010_fixture_prompt.md",
            selection_id="imported-guidance-fixture",
            selection_revision=1,
        )
        store = InformationStore.initialize_disposable(
            Path(self.fixture.temporary.name) / "imported-guidance.sqlite3",
            identity,
            allow_unsupported_runtime_for_fixture=True,
        )
        records = [
            {
                "dispatch_id": character * 32,
                "repository_id": identity.repository_id,
                "type": "INSTRUCTION",
                "status": "APPROVED",
                "body": f"Imported guidance {character}",
                "milestone_id": identity.milestone_id,
                "approved_milestone_id": identity.milestone_id,
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:01+00:00",
            }
            for character in ("a", "b")
        ]
        dispatch_bytes = json.dumps({
            "schema_version": 1,
            "repository_id": identity.repository_id,
            "dispatches": records,
        }).encode("utf-8")
        store.import_v1_fixture(V1FixtureBundle(
            prompt_path=identity.prompt_path,
            closeout_path=None,
            workflow_path=None,
            prompt_bytes=b"# Imported prompt\n",
            closeout_bytes=None,
            workflow_bytes=None,
            dispatch_path=str(Path(self.fixture.temporary.name) / identity.repository_id / "architect_dispatches.json"),
            dispatch_bytes=dispatch_bytes,
        ))
        clients = FixtureInformationStoreClients(
            store, DocumentLibrary([SQLiteRevisionProvider(store)]), fixture_only=True
        )
        app = create_web_app(
            repository_registry_path=registry_path,
            state_root=Path(self.fixture.temporary.name) / "reconciliation-state",
            information_store_clients=clients,
        )
        app.config["TESTING"] = True
        client = app.test_client()
        health = client.get("/healthz")
        page = client.get("/architect-dispatches")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Imported status is not proof", page.data)
        with client.session_transaction() as browser_session:
            csrf_token = browser_session["csrf_token"]
        common = {
            "csrf_token": csrf_token,
            "repository_key": identity.repository_key,
            "repository_generation": str(health.json["active_repository_generation"]),
            "reconciliation_history_id": "1" * 32,
            "reconciliation_fingerprint": dispatch_reconciliation_fingerprint(store.get_dispatch("a" * 32)),
        }
        approve_url = f"/architect-dispatches/{'a' * 32}/reconcile-approve"
        self.assertEqual(client.post(approve_url, data=common).status_code, 403)
        self.assertEqual(client.post(
            approve_url,
            data={**common, "fixture_po_confirmation": "confirmed", "reconciliation_fingerprint": "0" * 64},
        ).status_code, 409)
        approved_form = {**common, "fixture_po_confirmation": "confirmed"}
        self.assertEqual(client.post(approve_url, data=approved_form).status_code, 302)
        self.assertEqual(client.post(approve_url, data=approved_form).status_code, 302)
        reject_form = {
            **approved_form,
            "reconciliation_history_id": "2" * 32,
            "reconciliation_fingerprint": dispatch_reconciliation_fingerprint(store.get_dispatch("b" * 32)),
        }
        self.assertEqual(client.post(
            f"/architect-dispatches/{'b' * 32}/reconcile-reject", data=reject_form
        ).status_code, 302)
        snapshot = store.snapshot()
        self.assertEqual(snapshot["unverified_guidance"], [])
        self.assertEqual([item["dispatch_id"] for item in snapshot["confirmed_guidance"]], ["a" * 32])
        self.assertEqual(store.get_dispatch("b" * 32)["status"], "REJECTED")
        self.assertEqual(
            store.list_dispatch_history("a" * 32)[-1]["payload"]["historical_approval"],
            "UNKNOWN/NOT VERIFIED",
        )
        dispatch_source = next(item for item in store.list_import_sources() if item["source_kind"] == "dispatch")
        self.assertEqual(store.read_import_source(dispatch_source["source_id"]), dispatch_bytes)

    @patch("repoctl.web.repository_registry.validate_git_worktree", side_effect=lambda path: Path(path).resolve())
    @patch("repoctl.web.app.generate_milestone_status")
    def test_web_factory_injects_one_disposable_store_and_fails_closed_without_fallback(
        self, _status, _registry_git
    ) -> None:
        registry_path = Path(__file__).resolve().parents[1] / "ops" / "repositories.toml"
        repository = RepositoryRegistry.from_toml(registry_path).active()
        identity = StoreIdentity(
            repository_id=repository.repository_id,
            repository_key=repository.key,
            repository_root=str(repository.path),
            milestone_id="M010.010",
            prompt_path=(
                "docs/milestones/"
                "M010.010_m010_010_cutover_preparation_production_adapters_legacy_reconciliation_and_runtime_gate_prompt.md"
            ),
            selection_id="application-fixture-selection",
            selection_revision=1,
        )
        store = InformationStore.initialize_disposable(
            Path(self.fixture.temporary.name) / "application-store.sqlite3",
            identity,
            allow_unsupported_runtime_for_fixture=True,
        )
        store.create_immutable_revision(
            DocumentRevisionInput(
                document_id="application-history",
                revision_id="application-history-revision",
                logical_key="12.65.0_legacy_prompt.md",
                logical_type="OTHER",
                source_path="docs/history/milestones/12.65.0_legacy_prompt.md",
                content="# Store-backed application route\n",
                source_kind="fixture",
            )
        )
        prompt = store.create_immutable_revision(
            DocumentRevisionInput(
                document_id="application-prompt",
                revision_id="application-prompt-revision",
                logical_key=identity.prompt_path,
                logical_type="PROMPT",
                source_path=identity.prompt_path,
                content="# Application fixture prompt\n",
                source_kind="fixture",
            )
        )
        library = DocumentLibrary(
            [SQLiteRevisionProvider(store)],
            cursor_secret=b"application-route-fixture",
        )
        clients = FixtureInformationStoreClients(store, library, fixture_only=True)
        application = create_web_app(
            repository_registry_path=registry_path,
            state_root=Path(self.fixture.temporary.name) / "application-state",
            information_store_clients=clients,
        )
        application.config["TESTING"] = True
        client = application.test_client()

        health = client.get("/healthz")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json["information_store_authority"], "disposable_fixture_only")
        missing_history = client.get(
            "/api/architect/v1/history/search",
            query_string={
                "expected_repository_key": identity.repository_key,
                "expected_generation": health.json["active_repository_generation"],
                "query": "historical",
            },
        )
        self.assertEqual(missing_history.status_code, 503, missing_history.json)
        self.assertEqual(missing_history.json["error"]["code"], "historical_source_unavailable")
        planning_proposal = client.post(
            "/api/architect/v1/dispatches/instruction",
            json={
                "expected_repository_key": identity.repository_key,
                "expected_generation": health.json["active_repository_generation"],
                "milestone_id": identity.milestone_id,
                "body": "Fixture guidance in planning",
            },
        )
        self.assertEqual(planning_proposal.status_code, 201, planning_proposal.json)
        self.assertEqual(planning_proposal.json["data"]["dispatch"]["status"], "PENDING")
        role_documents = client.get("/api/architect/v1/documents")
        self.assertEqual(role_documents.status_code, 200)
        context_document = client.get("/api/architect/v1/documents/context")
        self.assertEqual(context_document.status_code, 200)
        self.assertEqual(context_document.json["data"]["content"]["source_path"], repository.documents["context"])
        repository_query = {
            "expected_repository_key": identity.repository_key,
            "expected_generation": health.json["active_repository_generation"],
            "path": repository.documents["context"],
        }
        self.assertEqual(
            client.get("/api/architect/v1/repository-document", query_string=repository_query).status_code, 200
        )
        self.assertEqual(client.get(
            "/api/architect/v1/repository-document",
            query_string={**repository_query, "path": identity.prompt_path},
        ).status_code, 403)

        search = client.get(
            "/document-library/?q=Store-backed+application+route"
            f"&repository_id={identity.repository_id}"
        )
        self.assertEqual(search.status_code, 200)
        self.assertIn(b"12.65.0_legacy_prompt.md", search.data)

        sidecar = repository.path / (identity.prompt_path.removesuffix("_prompt.md") + "_workflow.md")
        sidecar_before = sidecar.read_bytes()
        page = client.get("/milestones")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Lifecycle: PLANNING", page.data)
        with patch("repoctl.workflow.coder_interface._validate_information_store_overrides"):
            with self.assertRaises(CoderInterfaceError):
                read_coder_milestone_document_page(
                    identity.prompt_path,
                    expected_milestone_id=identity.milestone_id,
                    information_store_clients=clients,
                )
        with client.session_transaction() as browser_session:
            csrf_token = browser_session["csrf_token"]
        generation = str(health.json["active_repository_generation"])
        common = {
            "csrf_token": csrf_token,
            "repository_key": identity.repository_key,
            "repository_generation": generation,
            "milestone_id": identity.milestone_id,
            "prompt_path": identity.prompt_path,
        }
        selected = client.post("/milestones/select", data=common)
        self.assertEqual(selected.status_code, 302)
        refused = client.post("/milestones/select", data={**common, "milestone_id": "M999.999"})
        self.assertEqual(refused.status_code, 409)
        self.assertEqual(store.snapshot()["event_count"], 0)
        self.assertEqual(client.post("/milestones/workflow/event", data={}).status_code, 400)

        def action_form(action: str, summary: str) -> dict[str, str]:
            workflow = clients.browser_snapshot()["workflow"]
            return {
                **common,
                "expected_phase": workflow["phase"],
                "expected_event_count": str(workflow["event_count"]),
                "expected_prompt_identity": workflow["prompt_identity"],
                "expected_closeout_identity": workflow["closeout_identity"] or "",
                "event_type": action,
                "summary": summary,
            }

        start_form = action_form("approve_and_start", "Approve fixture prompt and start")
        revised_prompt = store.create_immutable_revision(
            DocumentRevisionInput(
                document_id=prompt["document_id"],
                revision_id="application-prompt-revision-2",
                logical_key=identity.prompt_path,
                logical_type="PROMPT",
                source_path=identity.prompt_path,
                parent_revision_id=prompt["revision_id"],
                content="# Revised application fixture prompt\n",
                source_kind="fixture",
            )
        )
        graph_result = {"outcome": "NOT_AVAILABLE", "receipt": {"id": "fixture"}}
        with (
            patch("repoctl.web.app.refresh_graph", return_value=graph_result),
            patch("repoctl.web.app.source_snapshot", return_value={"files": []}),
        ):
            stale = client.post("/milestones/workflow/event", data=start_form)
            self.assertEqual(stale.status_code, 409)
            self.assertEqual(store.snapshot()["event_count"], 0)
            start_form = action_form("approve_and_start", "Approve fixture prompt and start")
            started = client.post("/milestones/workflow/event", data=start_form)
        self.assertEqual(started.status_code, 302)
        state = store.snapshot()
        self.assertEqual(state["projection"]["phase"], "IN_PROGRESS")
        self.assertEqual([item["event_type"] for item in state["events"]], ["prompt_approved", "implementation_started"])
        self.assertEqual(state["prompt_revision_id"], revised_prompt["revision_id"])
        self.assertEqual(store.list_workflow_document_links(prompt["document_id"], revised_prompt["revision_id"])[0]["link_role"], "prompt")
        proposal = client.post(
            "/api/architect/v1/dispatches/prompt",
            json={
                "expected_repository_key": identity.repository_key,
                "expected_generation": int(generation),
                "milestone_id": "M010.011",
                "title": "New fixture milestone",
                "body": "Fixture prompt proposal",
            },
        )
        self.assertEqual(proposal.status_code, 201, proposal.json)
        proposed_dispatch = proposal.json["data"]["dispatch"]
        self.assertEqual(proposed_dispatch["status"], "PENDING")
        self.assertNotIn("M010.011", [item["milestone_id"] for item in clients.browser_candidates()["candidates"]])
        review_page = client.get("/architect-dispatches")
        self.assertEqual(review_page.status_code, 200)
        self.assertIn(b"M010.011_new_fixture_milestone_prompt.md", review_page.data)
        decision_url = f"/architect-dispatches/{proposed_dispatch['dispatch_id']}/approve"
        decision_form = {
            **common,
            "fixture_po_confirmation": "confirmed",
            "decision_fingerprint": dispatch_reconciliation_fingerprint(
                store.get_dispatch(proposed_dispatch["dispatch_id"])
            ),
        }
        self.assertEqual(client.post(decision_url, data={
            **decision_form, "decision_fingerprint": "0" * 64,
        }).status_code, 409)
        self.assertEqual(client.post(decision_url, data=decision_form).status_code, 302)
        self.assertIn("M010.011", [item["milestone_id"] for item in clients.browser_candidates()["candidates"]])
        decided = store.get_dispatch(proposed_dispatch["dispatch_id"])
        self.assertEqual(decided["payload"]["prompt_sha256"], store.list_dispatch_history(
            proposed_dispatch["dispatch_id"]
        )[-1]["payload"]["prompt_binding"]["content_sha256"])

        wait_form = {
            **action_form("question_submitted", "Fixture blocking question"),
            "waiting_category": "blocker",
            "waiting_reason": "Wait for a fixture answer",
        }
        self.assertEqual(client.post("/milestones/workflow/event", data=wait_form).status_code, 302)
        question_id = store.snapshot()["events"][-1]["event_id"]
        self.assertEqual(store.snapshot()["projection"]["phase"], "WAITING")
        resume_form = {
            **action_form("resolve_and_resume", "Fixture answer"),
            "related_event_id": question_id,
            "lock_in_summary": "Fixture binding decision",
        }
        self.assertEqual(client.post("/milestones/workflow/event", data=resume_form).status_code, 302)
        self.assertEqual(store.snapshot()["projection"]["phase"], "IN_PROGRESS")
        self.assertEqual(
            [item["event_type"] for item in store.snapshot()["events"][-3:]],
            ["answer_recorded", "lock_in_approved", "implementation_resumed"],
        )

        closeout_path = identity.prompt_path.removesuffix("_prompt.md") + "_closeout.md"
        closeout = store.create_immutable_revision(
            DocumentRevisionInput(
                document_id="application-closeout",
                revision_id="application-closeout-revision",
                logical_key=closeout_path,
                logical_type="CLOSEOUT",
                source_path=closeout_path,
                content="# Fixture closeout\n",
            )
        )
        submit_form = action_form("closeout_submitted", "Submit fixture closeout")
        revised_closeout = store.create_immutable_revision(
            DocumentRevisionInput(
                document_id=closeout["document_id"],
                revision_id="application-closeout-revision-2",
                logical_key=closeout_path,
                logical_type="CLOSEOUT",
                source_path=closeout_path,
                parent_revision_id=closeout["revision_id"],
                content="# Revised fixture closeout\n",
            )
        )
        before_submit = store.snapshot()["event_count"]
        self.assertEqual(client.post("/milestones/workflow/event", data=submit_form).status_code, 409)
        self.assertEqual(store.snapshot()["event_count"], before_submit)
        submit_form = action_form("closeout_submitted", "Submit fixture closeout")
        self.assertEqual(client.post("/milestones/workflow/event", data=submit_form).status_code, 302)
        self.assertEqual(store.snapshot()["projection"]["phase"], "REVIEW")
        submitted_event = store.snapshot()["events"][-1]
        self.assertEqual(
            store.get_event_document_link(submitted_event["event_id"], "closeout")["revision_id"],
            revised_closeout["revision_id"],
        )
        store.create_immutable_revision(
            DocumentRevisionInput(
                document_id=closeout["document_id"],
                revision_id="application-closeout-revision-3",
                logical_key=closeout_path,
                logical_type="CLOSEOUT",
                source_path=closeout_path,
                parent_revision_id=revised_closeout["revision_id"],
                content="# Unsubmitted newer draft\n",
            )
        )
        for endpoint in ("status", "milestone", "closeout"):
            self.assertEqual(client.get(f"/api/architect/v1/{endpoint}").status_code, 200)
        git_observation = {"source": {"source_type": "local_git_state"}, "working_tree": {"is_clean": True}}
        with patch("repoctl.web.app._git_view", return_value=git_observation):
            self.assertEqual(client.get("/api/architect/v1/git").json["data"], git_observation)
            review = client.get("/api/architect/v1/review")
        self.assertEqual(review.status_code, 200)
        self.assertEqual(review.json["data"]["milestone"]["phase"], "REVIEW")
        self.assertEqual(review.json["data"]["milestone"]["closeout"]["revision_id"], revised_closeout["revision_id"])
        self.assertEqual(review.json["data"]["context_verification_coverage"]["status"], "MISSING")
        closeout_response = client.get("/api/architect/v1/closeout").json["data"]
        self.assertEqual(closeout_response["revision_id"], revised_closeout["revision_id"])
        self.assertEqual(closeout_response["content"]["content"], "# Revised fixture closeout\n")
        page_arguments = {
            "expected_repository_key": identity.repository_key,
            "expected_generation": generation,
            "expected_milestone_id": identity.milestone_id,
            "path": closeout_path,
        }
        selected_page = client.get("/api/architect/v1/milestone-document", query_string=page_arguments)
        self.assertEqual(selected_page.status_code, 200)
        self.assertEqual(selected_page.json["data"]["revision_id"], revised_closeout["revision_id"])
        self.assertEqual(selected_page.json["data"]["content"], "# Revised fixture closeout\n")
        stale_page = client.get(
            "/api/architect/v1/milestone-document",
            query_string={**page_arguments, "source_fingerprint": "0" * 64},
        )
        self.assertEqual(stale_page.status_code, 409)

        dispatch_id = "d" * 32
        store.create_pending_dispatch(
            dispatch_id=dispatch_id, dispatch_type="INSTRUCTION", body="Newer fixture guidance."
        )
        store.record_dispatch_decision(
            dispatch_id=dispatch_id,
            history_id="c" * 32,
            status="APPROVED",
            actor_role="product_owner",
            actor_attribution="Explicit fixture confirmation.",
            trusted_fixture_po_confirmation=True,
            approved_milestone_id=identity.milestone_id,
            recorded_at="2099-01-01T00:00:00+00:00",
        )
        close_form = action_form("accept_and_close", "Accept fixture closeout")
        before_close = store.snapshot()["event_count"]
        confirmation = client.post("/milestones/workflow/event", data=close_form)
        self.assertEqual(confirmation.status_code, 200, confirmation.data.decode("utf-8"))
        self.assertIn(b"Second confirmation required", confirmation.data)
        self.assertEqual(store.snapshot()["event_count"], before_close)
        close_form.update(
            closure_guard_confirmed="yes",
            closure_guard_dispatch_id=dispatch_id,
            closure_override_reason="Reviewed the fixture guidance",
        )
        self.assertEqual(client.post("/milestones/workflow/event", data=close_form).status_code, 302)
        closed = store.snapshot()
        self.assertEqual(closed["projection"]["phase"], "CLOSED")
        acceptance = closed["events"][-2]
        self.assertEqual(acceptance["event_type"], "acceptance_recorded")
        self.assertEqual(store.get_event_document_link(acceptance["event_id"], "closeout")["revision_id"], revised_closeout["revision_id"])
        self.assertEqual(closed["events"][-1]["outstanding_dispatch_ids"], [dispatch_id])
        self.assertEqual(client.post("/milestones/workflow/event", data=close_form).status_code, 409)

        reopen = action_form("milestone_reopened", "Reopen fixture")
        reopen.update(confirm_reopen="yes", target_phase="IN_PROGRESS", reason="Reopen fixture")
        self.assertEqual(client.post("/milestones/reopen", data=reopen).status_code, 302)
        self.assertEqual(store.snapshot()["projection"]["phase"], "IN_PROGRESS")
        self.assertEqual(store.snapshot()["events"][:-1], closed["events"])
        self.assertEqual(sidecar.read_bytes(), sidecar_before)

        mismatched_identity = StoreIdentity(
            **{
                **identity.__dict__,
                "repository_id": "another-repository",
            }
        )
        mismatched_store = InformationStore.initialize_disposable(
            Path(self.fixture.temporary.name) / "mismatched-application-store.sqlite3",
            mismatched_identity,
            allow_unsupported_runtime_for_fixture=True,
        )
        mismatched_library = DocumentLibrary([SQLiteRevisionProvider(mismatched_store)])
        mismatched_clients = FixtureInformationStoreClients(
            mismatched_store,
            mismatched_library,
            fixture_only=True,
        )
        with self.assertRaisesRegex(ValueError, "does not match the app's selected repository"):
            create_web_app(
                repository_registry_path=registry_path,
                state_root=Path(self.fixture.temporary.name) / "mismatched-application-state",
                information_store_clients=mismatched_clients,
            )

    @patch("repoctl.workflow.coder_interface.validate_git_worktree", side_effect=lambda path: Path(path).resolve())
    @patch("repoctl.web.repository_registry.validate_git_worktree", side_effect=lambda path: Path(path).resolve())
    @patch("repoctl.web.app.generate_milestone_status")
    def test_coder_cli_adapters_read_and_append_only_through_the_injected_store(
        self, _status, _registry_git, _coder_git
    ) -> None:
        registry_path = Path(__file__).resolve().parents[1] / "ops" / "repositories.toml"
        repository = RepositoryRegistry.from_toml(registry_path).active()
        prompt_path = (
            "docs/milestones/"
            "M010.010_m010_010_cutover_preparation_production_adapters_legacy_reconciliation_and_runtime_gate_prompt.md"
        )
        identity = StoreIdentity(
            repository_id=repository.repository_id,
            repository_key=repository.key,
            repository_root=str(repository.path),
            milestone_id="M010.010",
            prompt_path=prompt_path,
            selection_id="coder-cli-fixture-selection",
            selection_revision=1,
        )
        sidecar_path = repository.path / (
            prompt_path.removesuffix("_prompt.md") + "_workflow.md"
        )
        sidecar_before = sidecar_path.read_bytes()
        store = InformationStore.initialize_disposable(
            Path(self.fixture.temporary.name) / "coder-cli-store.sqlite3",
            identity,
            allow_unsupported_runtime_for_fixture=True,
        )
        prompt_content = (repository.path / prompt_path).read_text(encoding="utf-8")
        prompt = store.create_immutable_revision(
            DocumentRevisionInput(
                document_id="coder-cli-prompt",
                revision_id="coder-cli-prompt-revision",
                logical_key=prompt_path,
                logical_type="PROMPT",
                source_path=prompt_path,
                content=prompt_content,
                source_kind="fixture",
            )
        )
        history = _closeout_history()
        store.select_approved_prompt_revision(
            request_id="coder-cli-prompt-approval",
            expected_store_revision=0,
            document_id=prompt["document_id"],
            revision_id=prompt["revision_id"],
            content_sha256=prompt["content_sha256"],
            prompt_approved_event=history[0],
            trusted_fixture_po_confirmation=True,
        )
        store.append_workflow_batch(
            request_id="coder-cli-start",
            expected_repository_id=identity.repository_id,
            expected_milestone_id=identity.milestone_id,
            expected_selection_id=identity.selection_id,
            expected_selection_revision=identity.selection_revision,
            expected_prompt_revision_id=prompt["revision_id"],
            expected_store_revision=1,
            events=[history[1]],
        )
        clients = FixtureInformationStoreClients(
            store,
            DocumentLibrary(
                [SQLiteRevisionProvider(store)],
                cursor_secret=b"coder-cli-store-fixture",
            ),
            fixture_only=True,
        )

        context_output = StringIO()
        with redirect_stdout(context_output):
            context_exit = cli_main(
                ["coder-context", "--format", "json"],
                information_store_clients=clients,
            )
        self.assertEqual(context_exit, 0)
        context = json.loads(context_output.getvalue())
        self.assertEqual(context["milestone"]["phase"], "IN_PROGRESS")
        self.assertEqual(context["prompt"]["content"], prompt_content)
        self.assertEqual(context["prompt"]["identity"], (
            f"{prompt['document_id']}:{prompt['revision_id']}:{prompt['content_sha256']}"
        ))
        self.assertEqual(context["information_store_authority"], "disposable_fixture_only")

        original_read_bytes = Path.read_bytes
        worktree_prompt = repository.path / prompt_path
        forged_marker = b"<!-- repoctl-procedure-release-preparation:v1\nnot-json\n-->\n"

        def read_with_forged_worktree_marker(path: Path) -> bytes:
            if path == worktree_prompt:
                return forged_marker
            return original_read_bytes(path)

        with patch.object(Path, "read_bytes", read_with_forged_worktree_marker):
            store_context = get_coder_context(information_store_clients=clients)
        self.assertEqual(store_context["procedure_release_preparation"]["status"], "NOT_REQUESTED")
        self.assertEqual(store_context["prompt"]["content"], prompt_content)
        invalid_selected_marker = inspect_procedure_release_preparation(
            repository.path,
            {"milestone_id": identity.milestone_id, "prompt_path": prompt_path},
            {"phase": "IN_PROGRESS", "event_history": []},
            repository_key=identity.repository_key,
            repository_id=identity.repository_id,
            prompt_content=forged_marker,
        )
        self.assertEqual(invalid_selected_marker["status"], "NOT_AUTHORIZED")

        application = create_web_app(
            repository_registry_path=registry_path,
            state_root=Path(self.fixture.temporary.name) / "coder-cli-web-state",
            information_store_clients=clients,
        )
        application.config["TESTING"] = True
        web_client = application.test_client()
        health = web_client.get("/healthz")
        self.assertEqual(health.status_code, 200)
        generation = health.json["active_repository_generation"]

        def bridge_get(_base_url: str, path: str, query: dict[str, str], timeout: float = 10.0):
            response = web_client.get(path, query_string=query)
            return response.status_code, response.json

        def bridge_post(_base_url: str, path: str, payload: dict[str, object], timeout: float = 10.0):
            response = web_client.post(path, json=payload)
            return response.status_code, response.json

        proposal_arguments = {
            "expected_repository_key": identity.repository_key,
            "expected_generation": generation,
            "milestone_id": identity.milestone_id,
            "body": "Fixture-only MCP guidance proposal",
        }
        with (
            patch("repoctl.mcp.architect_adapter.execute_get", side_effect=bridge_get),
            patch("repoctl.mcp.architect_adapter.execute_post", side_effect=bridge_post),
            patch.object(Path, "read_bytes", read_with_forged_worktree_marker),
        ):
            prompt_rpc = handle_rpc_request(
                {
                    "jsonrpc": "2.0",
                    "id": 0,
                    "method": "tools/call",
                    "params": {
                        "name": "repo_prompt",
                        "arguments": {
                            "expected_repository_key": identity.repository_key,
                            "expected_generation": generation,
                            "expected_milestone_id": identity.milestone_id,
                        },
                    },
                },
                "http://fixture",
                StringIO(),
            )
            self.assertIsNotNone(prompt_rpc)
            prompt_response = json.loads(prompt_rpc["result"]["content"][0]["text"])
            self.assertFalse(prompt_rpc["result"]["isError"], prompt_response)
            self.assertEqual(prompt_response["data"]["content"], prompt_content)
            self.assertEqual(prompt_response["data"]["revision_id"], prompt["revision_id"])
            self.assertEqual(prompt_response["data"]["content_sha256"], prompt["content_sha256"])

            proposal_rpc = handle_rpc_request(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "architect_submit_instruction_draft",
                        "arguments": proposal_arguments,
                    },
                },
                "http://fixture",
                StringIO(),
            )
            self.assertIsNotNone(proposal_rpc)
            proposal_response = json.loads(proposal_rpc["result"]["content"][0]["text"])
            self.assertFalse(proposal_rpc["result"]["isError"])
            proposed_dispatch = proposal_response["data"]["dispatch"]
            self.assertEqual(proposed_dispatch["type"], "INSTRUCTION")
            self.assertFalse(proposed_dispatch["authorization_granted"])

            read_rpc = handle_rpc_request(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "repo_dispatches",
                        "arguments": {
                            "expected_repository_key": identity.repository_key,
                            "expected_generation": generation,
                            "milestone_id": identity.milestone_id,
                            "limit": 5,
                        },
                    },
                },
                "http://fixture",
                StringIO(),
            )
        self.assertIsNotNone(read_rpc)
        read_response = json.loads(read_rpc["result"]["content"][0]["text"])
        self.assertFalse(read_rpc["result"]["isError"], read_response)
        self.assertEqual(read_response["data"]["dispatches"][0]["dispatch_id"], proposed_dispatch["dispatch_id"])
        self.assertFalse(read_response["data"]["dispatches"][0]["authorization_granted"])
        stale_prompt = web_client.get(
            "/api/architect/v1/prompt",
            query_string={"expected_milestone_id": "M999.999"},
        )
        self.assertEqual(stale_prompt.status_code, 409)
        self.assertEqual(stale_prompt.json["error"]["code"], "stale_milestone_selection")
        for unsafe_content, expected_code, expected_status in (
            ("-----BEGIN PRIVATE KEY-----\nfixture\n", "unsafe_source", 422),
            ("x" * 128_001, "source_oversized", 413),
        ):
            unsafe_snapshot = clients.browser_snapshot()
            unsafe_snapshot["prompt"]["content"] = unsafe_content
            with patch.object(clients, "browser_snapshot", return_value=unsafe_snapshot):
                refused = web_client.get("/api/architect/v1/prompt")
            self.assertEqual(refused.json["error"]["code"], expected_code)
            self.assertEqual(refused.status_code, expected_status)

        review_page = web_client.get("/architect-dispatches")
        self.assertEqual(review_page.status_code, 200)
        self.assertIn(b"fixture_po_confirmation", review_page.data)
        with web_client.session_transaction() as browser_session:
            csrf_token = browser_session["csrf_token"]
        decision_url = f"/architect-dispatches/{proposed_dispatch['dispatch_id']}/approve"
        unconfirmed = web_client.post(
            decision_url,
            data={
                "csrf_token": csrf_token,
                "repository_key": identity.repository_key,
                "repository_generation": str(generation),
            },
        )
        self.assertEqual(unconfirmed.status_code, 403)
        self.assertEqual(store.get_dispatch(proposed_dispatch["dispatch_id"])["status"], "PENDING")
        confirmed = web_client.post(
            decision_url,
            data={
                "csrf_token": csrf_token,
                "repository_key": identity.repository_key,
                "repository_generation": str(generation),
                "fixture_po_confirmation": "confirmed",
                "decision_fingerprint": dispatch_reconciliation_fingerprint(
                    store.get_dispatch(proposed_dispatch["dispatch_id"])
                ),
            },
        )
        self.assertEqual(confirmed.status_code, 302)
        decided = store.get_dispatch(proposed_dispatch["dispatch_id"])
        self.assertEqual(decided["status"], "APPROVED")
        self.assertFalse(decided["authorization_granted"])
        self.assertTrue(store.list_dispatch_history(proposed_dispatch["dispatch_id"])[-1]["po_confirmed"])

        document_output = StringIO()
        with redirect_stdout(document_output):
            document_exit = cli_main(
                [
                    "coder-read-milestone-document",
                    "--path",
                    identity.prompt_path,
                    "--expected-milestone-id",
                    identity.milestone_id,
                ],
                information_store_clients=clients,
            )
        self.assertEqual(document_exit, 0)
        document_page = json.loads(document_output.getvalue())
        self.assertEqual(document_page["path"], identity.prompt_path)
        self.assertEqual(document_page["working_tree_source"], "information_store_revision")
        self.assertEqual(document_page["content"], prompt_content)

        continuation_start = len(prompt_content[:100].encode("utf-8"))
        continuation_output = StringIO()
        with redirect_stdout(continuation_output):
            continuation_exit = cli_main(
                [
                    "coder-read-milestone-document",
                    "--path",
                    identity.prompt_path,
                    "--expected-milestone-id",
                    identity.milestone_id,
                    "--start-byte",
                    str(continuation_start),
                    "--source-fingerprint",
                    document_page["source_fingerprint"],
                ],
                information_store_clients=clients,
            )
        self.assertEqual(continuation_exit, 0)
        continuation_page = json.loads(continuation_output.getvalue())
        self.assertEqual(
            continuation_page["content"],
            prompt_content.encode("utf-8")[continuation_start:].decode("utf-8"),
        )

        before = store.snapshot()
        for arguments in (
            ["coder-status", "--state-root", str(self.fixture.temporary.name)],
            ["coder-question", "Must not write", "--state-root", str(self.fixture.temporary.name)],
            ["coder-question", "Must not write", "--milestone-root", "wrong-root"],
        ):
            error_output = StringIO()
            with redirect_stderr(error_output):
                rejected_exit = cli_main(arguments, information_store_clients=clients)
            self.assertNotEqual(rejected_exit, 0)
            self.assertTrue(error_output.getvalue())
            self.assertEqual(store.snapshot()["event_count"], before["event_count"])

        submitted = submit_coder_question(
            "Verify store-backed Coder submission.",
            information_store_clients=clients,
        )
        self.assertEqual(submitted["phase_before"], "IN_PROGRESS")
        self.assertEqual(submitted["phase_after"], "IN_PROGRESS")
        self.assertEqual(store.snapshot()["event_count"], before["event_count"] + 1)
        self.assertEqual(sidecar_path.read_bytes(), sidecar_before)
        with patch("repoctl.mcp.architect_adapter.execute_get", side_effect=bridge_get):
            workflow_rpc = handle_rpc_request(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "repo_workflow",
                        "arguments": {"expected_milestone_id": identity.milestone_id},
                    },
                },
                "http://fixture",
                StringIO(),
            )
        self.assertIsNotNone(workflow_rpc)
        workflow_response = json.loads(workflow_rpc["result"]["content"][0]["text"])
        self.assertFalse(workflow_rpc["result"]["isError"], workflow_response)
        workflow_data = workflow_response["data"]
        self.assertEqual(workflow_data["source"]["source_type"], "information_store_workflow")
        self.assertEqual(workflow_data["event_count"], store.snapshot()["event_count"])
        self.assertEqual(workflow_data["open_conditions"]["events"][0]["event_id"], submitted["event_id"])

        retry = store.append_workflow_batch(
            request_id=submitted["event_id"],
            expected_repository_id=identity.repository_id,
            expected_milestone_id=identity.milestone_id,
            expected_selection_id=identity.selection_id,
            expected_selection_revision=identity.selection_revision,
            expected_prompt_revision_id=prompt["revision_id"],
            expected_store_revision=before["store_revision"],
            events=[next(event for event in store.snapshot()["events"] if event["event_id"] == submitted["event_id"])],
            expected_guidance_sha256=before["guidance_sha256"],
        )
        self.assertEqual(retry, submitted["transaction_receipt"])

        test_output = StringIO()
        with redirect_stdout(test_output):
            test_exit = cli_main(
                [
                    "coder-test",
                    "Store-backed CLI test",
                    "--source",
                    "tests/test_information_store_fixture.py",
                    "--result",
                    "PASS",
                ],
                information_store_clients=clients,
            )
        self.assertEqual(test_exit, 0)
        self.assertIn("Test result recorded successfully", test_output.getvalue())
        evidence = web_client.get("/api/architect/v1/evidence")
        self.assertEqual(evidence.status_code, 200)
        self.assertEqual(evidence.json["data"]["reported_vs_machine_observed"], "reported")
        self.assertEqual(evidence.json["data"]["events"][-1]["event_type"], "test_result_recorded")

        verify_arguments = [
            "coder-verify",
            "--trigger", "NEW",
            "--expected-repository-key", identity.repository_key,
            "--expected-repository-id", identity.repository_id,
            "--expected-repository-root", identity.repository_root,
            "--expected-milestone-id", identity.milestone_id,
            "--expected-selection-id", identity.selection_id,
            "--expected-selection-revision", str(identity.selection_revision),
            "--expected-prompt-document-id", prompt["document_id"],
            "--expected-prompt-revision-id", prompt["revision_id"],
            "--expected-prompt-sha256", prompt["content_sha256"],
            "--acknowledge-git-boundary",
        ]
        release = {"status": "NOT_REQUESTED", "authorized": False}
        sources = {
            "procedure": {
                "inspection_status": "available",
                "status": "CURRENT",
                "overlay_valid": True,
                "release_preparation": release,
            },
            "release_preparation": release,
            "git_state": {
                "repository_root": str(repository.path),
                "branch": {"state": "attached", "name": "fixture"},
                "head": "a" * 40,
                "working_tree": {"entries": []},
                "git_operation_in_progress": False,
                "git_operations": [],
            },
            "git_failure_type": None,
        }
        response = Mock(status=200)
        response.read.return_value = json.dumps(web_client.get("/healthz").json).encode()
        connection = Mock()
        connection.getresponse.return_value = response
        verify_output = StringIO()
        with (
            patch("repoctl.workflow.coder_interface.collect_verification_sources", return_value=sources),
            patch("repoctl.workflow.runtime_compatibility.http.client.HTTPConnection", return_value=connection),
            redirect_stdout(verify_output),
        ):
            verify_exit = cli_main(verify_arguments, information_store_clients=clients)
        self.assertEqual(verify_exit, 0)
        verified = json.loads(verify_output.getvalue())
        self.assertEqual(verified["result"], "VERIFIED")
        self.assertEqual(verified["authority_snapshot"]["milestone"]["prompt_sha256"], prompt["content_sha256"])
        self.assertEqual(
            verified["authority_snapshot"]["information_store"]["prompt_document_id"],
            prompt["document_id"],
        )
        self.assertEqual(store.snapshot()["events"][-1]["event_id"], verified["event_id"])
        self.assertEqual(sidecar_path.read_bytes(), sidecar_before)

        retry_output = StringIO()
        with (
            patch("repoctl.workflow.coder_interface.collect_verification_sources", return_value=sources),
            patch("repoctl.workflow.runtime_compatibility.http.client.HTTPConnection", return_value=connection),
            redirect_stdout(retry_output),
        ):
            retry_exit = cli_main(verify_arguments, information_store_clients=clients)
        self.assertEqual(retry_exit, 0)
        self.assertTrue(json.loads(retry_output.getvalue())["idempotent"])

        materialized_sources = deepcopy(sources)
        materialized_sources["git_state"]["working_tree"]["entries"] = [
            {"kind": "untracked", "path": prompt_path}
        ]
        event_count = store.snapshot()["event_count"]
        missing_exception_output = StringIO()
        with (
            patch("repoctl.workflow.coder_interface.collect_verification_sources", return_value=materialized_sources),
            redirect_stderr(missing_exception_output),
        ):
            missing_exception_exit = cli_main(verify_arguments, information_store_clients=clients)
        self.assertEqual(missing_exception_exit, 2)
        self.assertIn("preflight failed", missing_exception_output.getvalue())
        self.assertEqual(store.snapshot()["event_count"], event_count)
        materialized_output = StringIO()
        with (
            patch("repoctl.workflow.coder_interface.collect_verification_sources", return_value=materialized_sources),
            patch("repoctl.workflow.runtime_compatibility.http.client.HTTPConnection", return_value=connection),
            redirect_stdout(materialized_output),
        ):
            materialized_exit = cli_main(
                verify_arguments + ["--allow-materialized-prompt"], information_store_clients=clients
            )
        self.assertEqual(materialized_exit, 0)
        preflight = json.loads(materialized_output.getvalue())["authority_snapshot"]["information_store"][
            "materialized_prompt_preflight"
        ]
        self.assertTrue(preflight["allowed_prompt_dirty"])
        for entry in (
            {"kind": "ordinary", "xy": "M.", "path": prompt_path},
            {"kind": "untracked", "path": "src/unrelated.py"},
        ):
            rejected_sources = deepcopy(materialized_sources)
            rejected_sources["git_state"]["working_tree"]["entries"] = [entry]
            event_count = store.snapshot()["event_count"]
            error_output = StringIO()
            with (
                patch("repoctl.workflow.coder_interface.collect_verification_sources", return_value=rejected_sources),
                redirect_stderr(error_output),
            ):
                rejected_exit = cli_main(
                    verify_arguments + ["--allow-materialized-prompt"], information_store_clients=clients
                )
            self.assertEqual(rejected_exit, 2)
            self.assertIn("preflight failed", error_output.getvalue())
            self.assertEqual(store.snapshot()["event_count"], event_count)

        before_rejection = store.snapshot()["event_count"]
        error_output = StringIO()
        with redirect_stderr(error_output):
            rejected_exit = cli_main(
                verify_arguments + ["--expected-prompt-revision-id", "stale-revision"],
                information_store_clients=clients,
            )
        self.assertEqual(rejected_exit, 2)
        self.assertIn("prompt revision ID", error_output.getvalue())
        self.assertEqual(store.snapshot()["event_count"], before_rejection)

        wrong_runtime = web_client.get("/healthz").json
        wrong_runtime["information_store_binding"]["database_identity"] = "0" * 64
        response.read.return_value = json.dumps(wrong_runtime).encode()
        error_output = StringIO()
        with (
            patch("repoctl.workflow.coder_interface.collect_verification_sources", return_value=sources),
            patch("repoctl.workflow.runtime_compatibility.http.client.HTTPConnection", return_value=connection),
            redirect_stderr(error_output),
        ):
            rejected_exit = cli_main(verify_arguments, information_store_clients=clients)
        self.assertEqual(rejected_exit, 2)
        self.assertIn("runtime_authority_mismatch", error_output.getvalue())
        self.assertEqual(store.snapshot()["event_count"], before_rejection)

        response.read.return_value = json.dumps(web_client.get("/healthz").json).encode()
        original_append = clients.append_coder_event

        def append_after_new_guidance(**kwargs):
            dispatch_id = "f" * 32
            store.create_pending_dispatch(
                dispatch_id=dispatch_id, dispatch_type="INSTRUCTION", body="Concurrent fixture guidance."
            )
            store.record_dispatch_decision(
                dispatch_id=dispatch_id,
                history_id="e" * 32,
                status="APPROVED",
                actor_role="product_owner",
                actor_attribution="Explicit fixture confirmation.",
                trusted_fixture_po_confirmation=True,
                approved_milestone_id=identity.milestone_id,
            )
            return original_append(**kwargs)

        error_output = StringIO()
        with (
            patch("repoctl.workflow.coder_interface.collect_verification_sources", return_value=sources),
            patch("repoctl.workflow.runtime_compatibility.http.client.HTTPConnection", return_value=connection),
            patch.object(clients, "append_coder_event", side_effect=append_after_new_guidance),
            redirect_stderr(error_output),
        ):
            rejected_exit = cli_main(
                verify_arguments + ["--trigger", "RECOVERY"], information_store_clients=clients
            )
        self.assertEqual(rejected_exit, 2)
        self.assertIn("guidance changed", error_output.getvalue())
        self.assertEqual(store.snapshot()["event_count"], before_rejection)

        closeout_path = prompt_path.removesuffix("_prompt.md") + "_closeout.md"
        closeout = store.create_immutable_revision(
            DocumentRevisionInput(
                document_id="coder-cli-closeout",
                revision_id="coder-cli-closeout-revision",
                logical_key=closeout_path,
                logical_type="CLOSEOUT",
                source_path=closeout_path,
                content="# Fixture closeout\n",
                source_kind="fixture",
            )
        )
        before_closeout = store.snapshot()
        with self.assertRaisesRegex(FixtureIntegrationError, "authority changed"):
            clients.submit_coder_closeout(
                summary="Stale closeout",
                document_id=closeout["document_id"],
                revision_id=closeout["revision_id"],
                content_sha256=closeout["content_sha256"],
                expected_prompt_revision_id=prompt["revision_id"],
                expected_store_revision=before_closeout["store_revision"] - 1,
            )

        closeout_arguments = [
            "coder-closeout",
            "--document-id", closeout["document_id"],
            "--revision-id", closeout["revision_id"],
            "--content-sha256", closeout["content_sha256"],
        ]
        error_output = StringIO()
        with redirect_stderr(error_output):
            rejected_exit = cli_main(
                closeout_arguments[:-1] + ["0" * 64],
                information_store_clients=clients,
            )
        self.assertNotEqual(rejected_exit, 0)
        self.assertIn("content hash does not match", error_output.getvalue())
        self.assertEqual(store.snapshot()["event_count"], before_closeout["event_count"])

        closeout_output = StringIO()
        with redirect_stdout(closeout_output):
            closeout_exit = cli_main(closeout_arguments, information_store_clients=clients)
        self.assertEqual(closeout_exit, 0)
        self.assertIn("IN_PROGRESS -> REVIEW", closeout_output.getvalue())
        self.assertIn(closeout["content_sha256"], closeout_output.getvalue())
        after_closeout = store.snapshot()
        event = after_closeout["events"][-1]
        self.assertEqual(event["event_type"], "closeout_submitted")
        self.assertEqual(event["evidence_path"], closeout_path)
        links = store.list_workflow_document_links(closeout["document_id"], closeout["revision_id"])
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]["event_id"], event["event_id"])
        self.assertEqual(links[0]["content_sha256"], closeout["content_sha256"])

        backup_path = Path(self.fixture.temporary.name) / "coder-cli-backup.sqlite3"
        with (
            closing(sqlite3.connect(store.path)) as source,
            closing(sqlite3.connect(backup_path)) as destination,
        ):
            source.backup(destination)
        with closing(sqlite3.connect(backup_path)) as connection:
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        restored = InformationStore.open_disposable(
            backup_path,
            identity,
            allow_unsupported_runtime_for_fixture=True,
        )
        self.assertEqual(restored.snapshot()["events"], after_closeout["events"])
        self.assertEqual(restored.snapshot()["projection"], after_closeout["projection"])
        self.assertEqual(
            restored.list_workflow_document_links(closeout["document_id"], closeout["revision_id"]),
            links,
        )
        self.assertEqual(restored.get_dispatch(proposed_dispatch["dispatch_id"]), decided)

        retry_arguments = {
            "request_id": event["event_id"],
            "expected_repository_id": identity.repository_id,
            "expected_milestone_id": identity.milestone_id,
            "expected_selection_id": identity.selection_id,
            "expected_selection_revision": identity.selection_revision,
            "expected_prompt_revision_id": prompt["revision_id"],
            "expected_store_revision": before_closeout["store_revision"],
            "events": [event],
            "links": [WorkflowDocumentLink(**links[0])],
            "expected_guidance_sha256": before_closeout["guidance_sha256"],
        }
        self.assertEqual(
            restored.append_workflow_batch(**retry_arguments),
            store.append_workflow_batch(**retry_arguments),
        )
        self.assertEqual(restored.snapshot()["event_count"], after_closeout["event_count"])

        restored_clients = FixtureInformationStoreClients(
            restored,
            DocumentLibrary(
                [SQLiteRevisionProvider(restored)],
                cursor_secret=b"coder-cli-restored-fixture",
            ),
            fixture_only=True,
        )
        restored_context = get_coder_context(information_store_clients=restored_clients)
        self.assertEqual(restored_context["milestone"]["phase"], "REVIEW")
        self.assertEqual(restored_context["prompt"]["content"], prompt_content)
        restored_app = create_web_app(
            repository_registry_path=registry_path,
            state_root=Path(self.fixture.temporary.name) / "restored-web-state",
            information_store_clients=restored_clients,
        )
        restored_browser = restored_app.test_client()
        restored_prompt = restored_browser.get("/api/architect/v1/prompt")
        restored_workflow = restored_browser.get("/api/architect/v1/workflow")
        self.assertEqual(restored_prompt.status_code, 200)
        self.assertEqual(restored_prompt.json["data"]["content_sha256"], prompt["content_sha256"])
        self.assertEqual(restored_workflow.status_code, 200)
        self.assertEqual(restored_workflow.json["data"]["phase"], "REVIEW")
        self.assertEqual(restored_workflow.json["data"]["event_count"], after_closeout["event_count"])
        self.assertEqual(sidecar_path.read_bytes(), sidecar_before)


if __name__ == "__main__":
    unittest.main()
