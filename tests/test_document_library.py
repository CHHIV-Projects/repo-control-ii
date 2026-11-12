from __future__ import annotations

import hashlib
import json
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlencode

from flask import Flask

from repoctl.document_library import (
    ArchitectDocumentAdapter,
    CoderDocumentAdapter,
    DocumentLibrary,
    GitFileProvider,
    GitRepositoryContext,
    DuplicateSourceIdentityError,
    InvalidCursorError,
    ProviderSnapshot,
    SQLiteRevisionProvider,
    SourceEnumerationError,
    StaleCursorError,
    estimate_tokens,
)
from repoctl.information_store import DocumentRevisionInput, InformationStore, StoreIdentity
from repoctl.web import architect_contract
from repoctl.web.document_library import create_document_library_blueprint
from repoctl.web.repository_registry import RepositoryEntry
from tests.test_sqlite_workflow_poc import _closeout_history


class DocumentLibraryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory(prefix="repoctl_document_library_", dir="/tmp")
        cls.root = Path(cls.temporary.name)
        cls.repository_root = cls.root / "git-fixture"
        (cls.repository_root / "docs/context").mkdir(parents=True)
        (cls.repository_root / "docs/milestones").mkdir(parents=True)
        (cls.repository_root / "docs/history/archive").mkdir(parents=True)
        (cls.repository_root / "docs/history/milestones").mkdir(parents=True)
        (cls.repository_root / "docs/context/project_context.md").write_text(
            "# Current context\nCurrent governing fixture.\n",
            encoding="utf-8",
        )
        (cls.repository_root / "docs/milestones/M010.006_fixture_prompt.md").write_text(
            "# Current fixture prompt\n",
            encoding="utf-8",
        )
        (cls.repository_root / "docs/milestones/M010.006_fixture_closeout.md").write_text(
            "# Current fixture closeout\n",
            encoding="utf-8",
        )
        (cls.repository_root / "docs/milestones/M009.001_orphan_closeout.md").write_text(
            "# Unpaired but valid historical closeout\n",
            encoding="utf-8",
        )
        for index in range(279):
            path = cls.repository_root / f"docs/history/archive/archive-{index:03d}.md"
            path.write_text(
                f"# Archived Git fixture {index}\nwide-tail-token git-marker-{index:03d}\n",
                encoding="utf-8",
            )
        cls.legacy_path = cls.repository_root / "docs/history/milestones/12.65.0_legacy_prompt.md"
        cls.legacy_path.write_text(
            "# Legacy milestone 12.65.0\nwide-tail-token legacy-tail-marker\n",
            encoding="utf-8",
        )

        cls.repository = RepositoryEntry(
            key="alpha-git",
            name="Git fixture",
            path=cls.repository_root,
            repository_id="git-fixture-id",
            milestone_root="docs/milestones",
            documents={"context": "docs/context/project_context.md"},
        )
        cls.identity = StoreIdentity(
            repository_id="sqlite-fixture-id",
            repository_key="beta-sqlite",
            repository_root=str(cls.root / "sqlite-fixture"),
            milestone_id="M999.001",
            prompt_path="fixtures/milestone_prompt.md",
            selection_id="fixture-selection",
            selection_revision=1,
        )
        cls.store = InformationStore.initialize_disposable(
            cls.root / "fixture.sqlite3",
            cls.identity,
            allow_unsupported_runtime_for_fixture=True,
        )
        for index in range(275):
            content = f"# SQLite fixture {index}\nwide-tail-token sqlite-marker-{index:03d}\n"
            if index == 274:
                content += "Café 🙂 exact tail content.\n"
            cls.store.create_immutable_revision(
                DocumentRevisionInput(
                    document_id=f"sqlite-document-{index:03d}",
                    revision_id=f"sqlite-revision-{index:03d}",
                    logical_key=f"fixtures/document-{index:03d}.md",
                    logical_type="OTHER",
                    source_path=f"fixtures/document-{index:03d}.md",
                    content=content,
                )
            )
        cls.store.create_immutable_revision(
            DocumentRevisionInput(
                document_id="sqlite-document-274",
                revision_id="sqlite-revision-274-v2",
                logical_key="fixtures/document-274.md",
                logical_type="OTHER",
                source_path="fixtures/document-274.md",
                content="# Updated tail\nwide-tail-token sqlite-new-version-274\n",
                parent_revision_id="sqlite-revision-274",
            )
        )
        cls.git_provider = GitFileProvider(
            [GitRepositoryContext(cls.repository, current_milestone_id="M010.006")]
        )
        cls.sqlite_provider = SQLiteRevisionProvider(cls.store)
        cls.library = DocumentLibrary(
            [cls.git_provider, cls.sqlite_provider],
            cursor_secret=b"document-library-test-cursor-secret",
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def test_complete_search_pages_more_than_548_matches_and_reads_tail_revision(self) -> None:
        page = self.library.search(query="wide-tail-token", page_size=37)
        self.assertEqual(page["candidate_count"], 560)
        self.assertEqual(page["searched_count"], 560)
        self.assertEqual(page["total_count"], 556)
        self.assertTrue(page["total_count_complete"])
        self.assertTrue(page["has_more"])

        documents = list(page["documents"])
        while page["has_more"]:
            page = self.library.search(
                query="wide-tail-token",
                page_size=37,
                cursor=page["next_cursor"],
            )
            documents.extend(page["documents"])
            self.assertTrue(page["total_count_complete"])

        identities = [(item["document_id"], item["revision_id"]) for item in documents]
        self.assertEqual(len(documents), 556)
        self.assertEqual(len(set(identities)), 556)
        self.assertTrue(
            {
                item["authority_classification"]
                for item in documents
            }
            <= {"historical_evidence", "disposable_fixture"}
        )
        self.assertFalse(page["has_more"])
        self.assertIsNone(page["next_cursor"])
        self.assertEqual(page["returned_count"], len(documents) % 37)
        self.assertEqual(page["omitted_count"], 0)
        self.assertEqual(page["deferred_count"], 0)

        tail_matches = [
            item for item in documents
            if item["path"] == "fixtures/document-274.md"
        ]
        self.assertEqual(len(tail_matches), 2)
        tail = next(item for item in tail_matches if item["revision_id"].endswith("sqlite-revision-274"))
        self.assertGreater(documents.index(tail), 300)
        exact = self.library.get(tail["document_id"], tail["revision_id"])
        self.assertIn("Café 🙂 exact tail content.", exact["content"])
        updated_tail = next(
            item for item in tail_matches if item["revision_id"].endswith("sqlite-revision-274-v2")
        )
        revision_diff = self.library.compare(
            tail["document_id"],
            tail["revision_id"],
            updated_tail["document_id"],
            updated_tail["revision_id"],
        )
        self.assertIn("sqlite-new-version-274", revision_diff["diff"])
        self.assertTrue(revision_diff["complete"])

        pieces: list[str] = []
        cursor = None
        while True:
            content_page = self.library.read_page(
                tail["document_id"],
                tail["revision_id"],
                cursor=cursor,
                page_bytes=17,
            )
            pieces.append(content_page["content"])
            if content_page["complete"]:
                break
            cursor = content_page["next_cursor"]
        self.assertEqual("".join(pieces), exact["content"])
        export = self.library.export([(tail["document_id"], tail["revision_id"])])
        self.assertEqual(export["document_count"], 1)
        self.assertEqual(export["manifest"][0]["content_sha256"], tail["content_sha256"])
        self.assertEqual(export["manifest"][0]["path"], "fixtures/document-274.md")
        self.assertFalse(export["attachments_included"])
        self.assertIn("attachments", export["attachment_note"])

        legacy = next(item for item in documents if item["path"] == "docs/history/milestones/12.65.0_legacy_prompt.md")
        self.assertEqual(legacy["milestone_id"], "12.65.0")
        self.assertEqual(legacy["authority_classification"], "historical_evidence")
        self.assertEqual(legacy["revision_status"], "historical")

    def test_repository_filters_and_authority_labels_remain_distinct(self) -> None:
        legacy = self.library.search(
            query="legacy-tail-marker",
            filters={
                "repository_key": "alpha-git",
                "milestone_id": "12.65.0",
                "document_class": "PROMPT",
                "authority": "historical_evidence",
            },
            page_size=5,
        )
        self.assertEqual(legacy["total_count"], 1)
        self.assertEqual(legacy["documents"][0]["milestone_id"], "12.65.0")

        current = self.library.search(
            filters={
                "milestone_id": "M010.006",
                "document_class": "PROMPT",
                "authority": "current_milestone_document",
            },
            page_size=5,
        )
        self.assertEqual(current["total_count"], 1)
        self.assertEqual(current["documents"][0]["approval_state"], "unknown")

        orphan_closeout = self.library.search(
            filters={
                "milestone_id": "M009.001",
                "document_class": "CLOSEOUT",
                "authority": "historical_evidence",
            },
            page_size=5,
        )
        self.assertEqual(orphan_closeout["total_count"], 1)
        self.assertTrue(any("Orphan or unmatched milestone closeout" in warning for warning in orphan_closeout["warnings"]))

        sqlite = self.library.search(
            query="sqlite-marker-273",
            filters={"repository_id": "sqlite-fixture-id", "source": "sqlite_fixture"},
            page_size=5,
        )
        self.assertEqual(sqlite["total_count"], 1)
        self.assertEqual(sqlite["documents"][0]["authority_classification"], "disposable_fixture")
        self.assertEqual(sqlite["documents"][0]["revision_status"], "draft")
        wrong_repository = self.library.search(
            query="sqlite-marker-273",
            filters={"repository_id": "another-repository"},
        )
        self.assertEqual(wrong_repository["total_count"], 0)
        self.assertTrue(wrong_repository["total_count_complete"])

    def test_sqlite_approval_state_is_fixture_labeled_not_managed_authority(self) -> None:
        with tempfile.TemporaryDirectory(prefix="repoctl_approved_revision_", dir="/tmp") as directory:
            root = Path(directory)
            identity = StoreIdentity(
                repository_id="approved-fixture-id",
                repository_key="approved-fixture",
                repository_root=str(root / "repository"),
                milestone_id="M999.002",
                prompt_path="fixtures/approved_prompt.md",
                selection_id="approved-fixture-selection",
                selection_revision=1,
            )
            store = InformationStore.initialize_disposable(
                root / "approved-fixture.sqlite3",
                identity,
                allow_unsupported_runtime_for_fixture=True,
            )
            revision = store.create_immutable_revision(
                DocumentRevisionInput(
                    document_id="approved-prompt",
                    revision_id="approved-prompt-revision",
                    logical_key="fixtures/approved_prompt.md",
                    logical_type="PROMPT",
                    source_path="fixtures/approved_prompt.md",
                    content="# Approved only inside this fixture.\n",
                )
            )
            store.select_approved_prompt_revision(
                request_id="approve-fixture-prompt",
                expected_store_revision=0,
                document_id=revision["document_id"],
                revision_id=revision["revision_id"],
                content_sha256=revision["content_sha256"],
                prompt_approved_event=_closeout_history()[0],
                trusted_fixture_po_confirmation=True,
            )
            record = SQLiteRevisionProvider(store).enumerate_records().records[0]
            self.assertEqual(record.revision_status, "approved")
            self.assertEqual(record.approval_state, "fixture_approved")
            self.assertEqual(record.authority_classification, "disposable_fixture")

    def test_conflicting_source_identities_and_traversal_are_rejected(self) -> None:
        original = self.git_provider.enumerate_records().records[0]

        class DuplicateProvider:
            provider_id = original.provider_id

            def enumerate_records(self) -> ProviderSnapshot:
                return ProviderSnapshot(
                    provider_id=self.provider_id,
                    records=(original, original),
                    fingerprint="fixture-duplicate",
                    bytes_scanned=0,
                )

        with self.assertRaises(DuplicateSourceIdentityError):
            DocumentLibrary([DuplicateProvider()]).search(page_size=1)

        conflicting_record = replace(
            original,
            source_identity=f"{original.source_identity}:conflict",
            content="conflicting content",
            content_sha256=hashlib.sha256(b"conflicting content").hexdigest(),
        )

        class ConflictingProvider:
            provider_id = original.provider_id

            def enumerate_records(self) -> ProviderSnapshot:
                return ProviderSnapshot(
                    provider_id=self.provider_id,
                    records=(original, conflicting_record),
                    fingerprint="fixture-conflicting-revision",
                    bytes_scanned=0,
                )

        with self.assertRaises(DuplicateSourceIdentityError):
            DocumentLibrary([ConflictingProvider()]).search(page_size=1)

        unsafe_repository = RepositoryEntry(
            key="unsafe-git",
            name="Unsafe fixture",
            path=self.repository_root,
            repository_id="unsafe-git-id",
            milestone_root="docs/milestones",
            documents={"context": "../outside.md"},
        )
        unsafe_library = DocumentLibrary(
            [GitFileProvider([GitRepositoryContext(unsafe_repository)])]
        )
        with self.assertRaises(SourceEnumerationError):
            unsafe_library.search(page_size=1)

    def test_agent_adapters_and_fixture_ui_preserve_exact_revision_access(self) -> None:
        item = self.library.search(query="sqlite-marker-274", page_size=1)["documents"][0]
        architect = ArchitectDocumentAdapter(self.library).read_page(
            item["document_id"],
            item["revision_id"],
            page_bytes=12,
        )
        coder = CoderDocumentAdapter(self.library).read_page(
            item["document_id"],
            item["revision_id"],
            page_bytes=12,
        )
        self.assertEqual(architect["adapter_role"], "architect")
        self.assertEqual(coder["adapter_role"], "coder")
        self.assertEqual(architect["data"], coder["data"])
        self.assertFalse(architect["data"]["complete"])
        with self.assertRaises(InvalidCursorError):
            self.library.read_page(
                item["document_id"],
                item["revision_id"],
                cursor=architect["data"]["next_cursor"],
                page_bytes=13,
            )

        app = Flask(__name__)
        app.register_blueprint(create_document_library_blueprint(self.library), url_prefix="/library")
        client = app.test_client()
        search_response = client.get("/library/?q=sqlite-marker-274")
        self.assertEqual(search_response.status_code, 200)
        self.assertIn(b"Document Library", search_response.data)

        path = (
            f"/library/documents/{item['document_id']}/"
            f"{item['revision_id']}"
        )
        detail_response = client.get(path)
        self.assertEqual(detail_response.status_code, 200)
        self.assertIn(b"sqlite-marker-274", detail_response.data)
        self.assertIn(b"disposable_fixture", detail_response.data)

        query = urlencode(
            {
                "left": f"{item['document_id']}|{item['revision_id']}",
                "right": f"{item['document_id']}|{item['revision_id']}",
            }
        )
        compare_response = client.get(f"/library/compare?{query}")
        self.assertEqual(compare_response.status_code, 200)
        self.assertIn(b"Compare exact revisions", compare_response.data)
        export_query = urlencode(
            {"selection": f"{item['document_id']}|{item['revision_id']}"}
        )
        export_response = client.get(f"/library/export?{export_query}")
        self.assertEqual(export_response.status_code, 200)
        self.assertIn("attachment;", export_response.headers["Content-Disposition"])
        self.assertEqual(export_response.get_json()["manifest"][0]["revision_id"], item["revision_id"])
        self.assertEqual(client.get("/library/compare").status_code, 400)

    def test_cursors_reject_cross_query_foreign_library_and_changed_sources(self) -> None:
        first = self.library.search(query="wide-tail-token", page_size=1)
        with self.assertRaises(InvalidCursorError):
            self.library.search(query="different-query", page_size=1, cursor=first["next_cursor"])
        other_library = DocumentLibrary(
            [self.git_provider, self.sqlite_provider],
            cursor_secret=b"a-different-document-library-cursor",
        )
        with self.assertRaises(InvalidCursorError):
            other_library.search(query="wide-tail-token", page_size=1, cursor=first["next_cursor"])

        archive_file = self.repository_root / "docs/history/archive/archive-000.md"
        original = archive_file.read_bytes()
        try:
            archive_file.write_bytes(original + b"\nchanged after the first page\n")
            with self.assertRaises(StaleCursorError):
                self.library.search(
                    query="wide-tail-token",
                    page_size=1,
                    cursor=first["next_cursor"],
                )
        finally:
            archive_file.write_bytes(original)

    def test_retrieval_efficiency_characterization_matches_legacy_and_structured_results(self) -> None:
        query = "legacy-tail-marker"
        baseline_started = time.perf_counter()
        candidates, discovery_limited, skipped = architect_contract._discover_historical_candidates(
            self.repository,
            selected_milestone_id=None,
        )
        baseline_search = architect_contract._search_historical_candidates(
            self.repository,
            candidates,
            selected_milestone_id=None,
            query=query,
            milestone_id=None,
            artifact_type=None,
            category="archive",
            limit=10,
            cursor=None,
            cursor_secret="document-library-characterization",
            scan_limited=discovery_limited,
            skipped_count=skipped,
        )
        self.assertEqual(len(baseline_search["results"]), 1)
        baseline_item = baseline_search["results"][0]
        source_candidate = next(item for item in candidates if item["path"] == baseline_item["path"])
        baseline_document = architect_contract._read_repository_document(
            self.repository,
            baseline_item["path"],
            allow_historical=True,
            max_bytes=architect_contract.MAX_REPOSITORY_DOCUMENT_FILE_BYTES,
            expected_snapshot=source_candidate["_snapshot"],
        )
        baseline_elapsed_ms = (time.perf_counter() - baseline_started) * 1000

        structured_started = time.perf_counter()
        structured_search = self.library.search(query=query, page_size=10)
        self.assertEqual(len(structured_search["documents"]), 1)
        structured_item = structured_search["documents"][0]
        structured_document = self.library.get(
            structured_item["document_id"],
            structured_item["revision_id"],
        )
        structured_elapsed_ms = (time.perf_counter() - structured_started) * 1000

        self.assertEqual(structured_item["path"], baseline_item["path"])
        self.assertEqual(structured_document["content"], baseline_document["content"])
        self.assertTrue(baseline_search["total_count_complete"])
        self.assertTrue(structured_search["total_count_complete"])
        self.assertEqual(baseline_search["has_more"], False)
        self.assertEqual(structured_search["has_more"], False)
        content_bytes = len(structured_document["content"].encode("utf-8"))
        self.assertEqual(content_bytes, baseline_document["bytes_returned"])
        historical_metrics = {
            "baseline": {
                "elapsed_ms": round(baseline_elapsed_ms, 3),
                "candidate_count": baseline_search["candidates_considered"],
                "files_scanned": baseline_search["files_scanned"],
                "search_pages": 1,
                "calls_including_exact_read": 2,
                "evidence_bytes": content_bytes,
                "estimated_tokens": estimate_tokens(baseline_document["content"]),
                "complete": baseline_search["total_count_complete"],
            },
            "document_library": {
                "elapsed_ms": round(structured_elapsed_ms, 3),
                "candidate_count": structured_search["candidate_count"],
                "searched_count": structured_search["searched_count"],
                "provider_bytes_read_for_search": structured_search["provider_bytes_read"],
                "search_pages": 1,
                "calls_including_exact_read": 2,
                "evidence_bytes": content_bytes,
                "estimated_tokens": estimate_tokens(structured_document["content"]),
                "complete": structured_search["total_count_complete"],
            },
            "same_content": structured_document["content"] == baseline_document["content"],
        }

        role_started = time.perf_counter()
        baseline_role = architect_contract._read_repository_document(
            self.repository,
            "docs/context/project_context.md",
            max_bytes=architect_contract.MAX_REPOSITORY_DOCUMENT_FILE_BYTES,
        )
        baseline_role_elapsed_ms = (time.perf_counter() - role_started) * 1000
        role_library_started = time.perf_counter()
        role_search = self.library.search(
            query="current governing fixture",
            filters={
                "repository_key": "alpha-git",
                "document_class": "PROJECT_CONTEXT",
            },
            page_size=5,
        )
        role_item = role_search["documents"][0]
        role_document = self.library.get(role_item["document_id"], role_item["revision_id"])
        role_library_elapsed_ms = (time.perf_counter() - role_library_started) * 1000
        role_bytes = len(role_document["content"].encode("utf-8"))
        self.assertEqual(role_search["total_count"], 1)
        self.assertEqual(role_document["content"], baseline_role["content"])
        self.assertEqual(
            hashlib.sha256(role_document["content"].encode("utf-8")).hexdigest(),
            role_document["content_sha256"],
        )
        role_metrics = {
            "baseline": {
                "elapsed_ms": round(baseline_role_elapsed_ms, 3),
                "candidate_count": 1,
                "files_scanned": 1,
                "search_pages": 0,
                "calls_including_exact_read": 1,
                "evidence_bytes": role_bytes,
                "estimated_tokens": estimate_tokens(baseline_role["content"]),
                "complete": True,
            },
            "document_library": {
                "elapsed_ms": round(role_library_elapsed_ms, 3),
                "candidate_count": role_search["candidate_count"],
                "searched_count": role_search["searched_count"],
                "provider_bytes_read_for_search": role_search["provider_bytes_read"],
                "search_pages": 1,
                "calls_including_exact_read": 2,
                "evidence_bytes": role_bytes,
                "estimated_tokens": estimate_tokens(role_document["content"]),
                "complete": role_search["total_count_complete"],
            },
            "same_content": role_document["content"] == baseline_role["content"],
        }
        known_record = next(
            item
            for item in self.sqlite_provider.enumerate_records().records
            if item.revision_id.endswith("sqlite-revision-274-v2")
        )
        known_started = time.perf_counter()
        known_document = self.library.get(known_record.document_id, known_record.revision_id)
        known_elapsed_ms = (time.perf_counter() - known_started) * 1000
        self.assertIn("sqlite-new-version-274", known_document["content"])
        known_metrics = {
            "elapsed_ms": round(known_elapsed_ms, 3),
            "candidate_count": 1,
            "tool_calls": 1,
            "evidence_bytes": len(known_document["content"].encode("utf-8")),
            "estimated_tokens": estimate_tokens(known_document["content"]),
            "exact_revision": known_document["revision_id"] == known_record.revision_id,
        }

        filtered_started = time.perf_counter()
        filtered = self.library.search(
            query="legacy-tail-marker",
            filters={
                "repository_key": "alpha-git",
                "milestone_id": "12.65.0",
                "document_class": "PROMPT",
            },
            page_size=5,
        )
        filtered_document = self.library.get(
            filtered["documents"][0]["document_id"],
            filtered["documents"][0]["revision_id"],
        )
        filtered_elapsed_ms = (time.perf_counter() - filtered_started) * 1000
        self.assertEqual(filtered["total_count"], 1)
        self.assertEqual(filtered["documents"][0]["milestone_id"], "12.65.0")
        filtered_metrics = {
            "elapsed_ms": round(filtered_elapsed_ms, 3),
            "candidate_count": filtered["candidate_count"],
            "searched_count": filtered["searched_count"],
            "tool_calls": 2,
            "evidence_bytes": len(filtered_document["content"].encode("utf-8")),
            "estimated_tokens": estimate_tokens(filtered_document["content"]),
            "complete": filtered["total_count_complete"],
        }

        broad_started = time.perf_counter()
        broad_page = self.library.search(query="wide-tail-token", page_size=100)
        broad_documents = list(broad_page["documents"])
        broad_pages = 1
        while broad_page["has_more"]:
            broad_page = self.library.search(
                query="wide-tail-token",
                page_size=100,
                cursor=broad_page["next_cursor"],
            )
            broad_documents.extend(broad_page["documents"])
            broad_pages += 1
        broad_elapsed_ms = (time.perf_counter() - broad_started) * 1000
        broad_identities = {
            (item["document_id"], item["revision_id"])
            for item in broad_documents
        }
        self.assertEqual(len(broad_documents), 556)
        self.assertEqual(len(broad_identities), 556)
        self.assertTrue(broad_page["total_count_complete"])
        broad_response = json.dumps(broad_documents, ensure_ascii=False, sort_keys=True)
        broad_metrics = {
            "elapsed_ms": round(broad_elapsed_ms, 3),
            "candidate_count": broad_page["candidate_count"],
            "match_count": len(broad_documents),
            "tool_calls_or_pages": broad_pages,
            "serialized_result_bytes": len(broad_response.encode("utf-8")),
            "estimated_result_tokens": estimate_tokens(broad_response),
            "complete": broad_page["total_count_complete"],
        }

        open_export_started = time.perf_counter()
        exact_document = self.library.get(known_record.document_id, known_record.revision_id)
        content_pages = []
        cursor = None
        page_count = 0
        while True:
            content_page = self.library.read_page(
                exact_document["document_id"],
                exact_document["revision_id"],
                cursor=cursor,
                page_bytes=64,
            )
            content_pages.append(content_page["content"])
            page_count += 1
            if content_page["complete"]:
                break
            cursor = content_page["next_cursor"]
        exported = self.library.export(
            [(exact_document["document_id"], exact_document["revision_id"])]
        )
        open_export_elapsed_ms = (time.perf_counter() - open_export_started) * 1000
        self.assertEqual("".join(content_pages), exact_document["content"])
        self.assertEqual(exported["manifest"][0]["content_sha256"], exact_document["content_sha256"])
        open_export_metrics = {
            "elapsed_ms": round(open_export_elapsed_ms, 3),
            "candidate_count": 1,
            "tool_calls": 2 + page_count,
            "content_pages": page_count,
            "evidence_bytes": len(exact_document["content"].encode("utf-8")),
            "estimated_tokens": estimate_tokens(exact_document["content"]),
            "open_exact_and_export_hash_match": True,
        }
        metrics = {
            "method": "UTF-8 bytes / 4, rounded up; estimate_tokens",
            "tasks": {
                "historical_archive_tail": historical_metrics,
                "current_role_document": role_metrics,
                "known_revision_id": known_metrics,
                "filtered_legacy_history": filtered_metrics,
                "broad_complete_search": broad_metrics,
                "full_content_open_and_export": open_export_metrics,
            },
        }
        print(f"document-library-retrieval-metrics={json.dumps(metrics, sort_keys=True)}")

    def test_unsafe_archive_symlink_fails_instead_of_claiming_completeness(self) -> None:
        archive_directory = self.repository_root / "docs/history/archive"
        link = archive_directory / "unsafe-link.md"
        link.symlink_to(archive_directory / "archive-000.md")
        try:
            with self.assertRaises(SourceEnumerationError):
                self.library.search(query="wide-tail-token", page_size=1)
        finally:
            link.unlink()


if __name__ == "__main__":
    unittest.main()
