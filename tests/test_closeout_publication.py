from __future__ import annotations

import hashlib
import json
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask

from repoctl.information_store import (
    DocumentRevisionInput,
    IdempotencyConflictError,
    IdentityMismatchError,
    InformationStore,
    RevisionConflictError,
    StoreIdentity,
    WorkflowDocumentLink,
)
from repoctl.web.closeout_publication import create_fixture_closeout_publication_blueprint
from repoctl.workflow.closeout_publication import (
    CloseoutPublicationError,
    CloseoutPublicationService,
    FixturePublicationContext,
    NotConnectedGitReviewObserver,
    PublicationCollisionError,
    PublicationDriftError,
    PublicationGitUnavailableError,
    PublicationMaterializationError,
    PublicationNotAcceptedError,
    ReadOnlyGitReviewObserver,
)
from repoctl.workflow import git_actions
from tests.test_sqlite_workflow_poc import _closeout_history, _event


_PROMPT_PATH = "docs/milestones/M999.001_fixture_prompt.md"
_CLOSEOUT_PATH = "docs/milestones/M999.001_fixture_closeout.md"
_HEAD = "a" * 40


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class FixtureGitObserver:
    status = "CONNECTED"

    def __init__(self) -> None:
        self.branch = "fixture-branch"
        self.head = _HEAD
        self.other_worktree = "clean"
        self.other_index = "clean"
        self.is_staged = False
        self.staged_blob_sha256: str | None = None
        self.head_blob_sha256: str | None = None
        self.commit_blob_sha256: str | None = None
        self.verified_commit_id: str | None = None

    def inspect(
        self,
        worktree_root: Path,
        target_path: str,
        *,
        commit_id: str | None = None,
    ) -> dict:
        target = worktree_root / target_path
        try:
            metadata = target.lstat()
        except FileNotFoundError:
            worktree = {"state": "absent"}
            target_hash = None
        else:
            if not stat.S_ISREG(metadata.st_mode):
                worktree = {"state": "symlink"}
                target_hash = None
            else:
                content = target.read_bytes()
                target_hash = _sha256(content)
                worktree = {
                    "state": "present",
                    "sha256": target_hash,
                    "size": len(content),
                    "mode": stat.S_IMODE(metadata.st_mode),
                }
        status = (
            {"kind": "ordinary", "xy": "A.", "path": target_path}
            if self.is_staged
            else None
        )
        observation = {
            "connected": True,
            "repository_root": str(worktree_root),
            "branch": self.branch,
            "head": self.head,
            "index_fingerprint": _sha256(f"{self.other_index}:{self.staged_blob_sha256}".encode()),
            "worktree_fingerprint": _sha256(
                json.dumps([self.other_worktree, target_hash], sort_keys=True).encode()
            ),
            "worktree_fingerprint_without_target": _sha256(self.other_worktree.encode()),
            "target": {
                "path": target_path,
                "worktree": worktree,
                "status": status,
                "staged": self.is_staged,
                "index_records_sha256": _sha256(str(self.staged_blob_sha256).encode()),
                "staged_blob_sha256": self.staged_blob_sha256,
                "head_blob_sha256": self.head_blob_sha256,
            },
            "verified_commit_id": None,
            "commit_blob_sha256": None,
        }
        if commit_id is not None and commit_id == self.head and self.commit_blob_sha256 is not None:
            observation["verified_commit_id"] = commit_id
            observation["commit_blob_sha256"] = self.commit_blob_sha256
        return observation


class CloseoutPublicationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="repoctl_closeout_publication_", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.registered_root = self.root / "registered-repository"
        self.registered_root.mkdir()
        self.worktree_root = self.root / "disposable-worktree"
        (self.worktree_root / "docs/milestones").mkdir(parents=True)
        self.identity = StoreIdentity(
            repository_id="fixture-repository-id",
            repository_key="fixture-repository",
            repository_root=str(self.registered_root),
            milestone_id="M999.001",
            prompt_path=_PROMPT_PATH,
            selection_id="fixture-selection",
            selection_revision=1,
        )
        self.database_path = self.root / "information-store.sqlite3"
        self.store = InformationStore.initialize_disposable(
            self.database_path,
            self.identity,
            allow_unsupported_runtime_for_fixture=True,
        )
        self.observer = FixtureGitObserver()
        self._create_accepted_source()
        self.service = self._service()

    @property
    def target(self) -> Path:
        return self.worktree_root / _CLOSEOUT_PATH

    def _service(
        self,
        *,
        context: FixturePublicationContext | None = None,
        observer=None,
    ) -> CloseoutPublicationService:
        return CloseoutPublicationService(
            self.store,
            context
            or FixturePublicationContext(
                repository_id=self.identity.repository_id,
                repository_key=self.identity.repository_key,
                repository_root=self.identity.repository_root,
                milestone_id=self.identity.milestone_id,
                milestone_root="docs/milestones",
                worktree_root=self.worktree_root,
                test_only=True,
            ),
            observer or self.observer,
        )

    def _create_accepted_source(self) -> None:
        prompt = self.store.create_immutable_revision(
            DocumentRevisionInput(
                document_id="fixture-prompt",
                revision_id="fixture-prompt-revision",
                logical_key=_PROMPT_PATH,
                logical_type="PROMPT",
                source_path=_PROMPT_PATH,
                content="# Fixture prompt\n",
            )
        )
        self.closeout_content = "# Accepted closeout\n\nExact fixture bytes.\n"
        self.closeout_hash = _sha256(self.closeout_content.encode())
        events = _closeout_history()
        self.workflow_events = events
        self.store.select_approved_prompt_revision(
            request_id="fixture-prompt-approval",
            expected_store_revision=0,
            document_id=prompt["document_id"],
            revision_id=prompt["revision_id"],
            content_sha256=prompt["content_sha256"],
            prompt_approved_event=events[0],
            trusted_fixture_po_confirmation=True,
        )
        self.store.append_workflow_batch(
            request_id="fixture-implementation-start",
            expected_repository_id=self.identity.repository_id,
            expected_milestone_id=self.identity.milestone_id,
            expected_selection_id=self.identity.selection_id,
            expected_selection_revision=self.identity.selection_revision,
            expected_prompt_revision_id=prompt["revision_id"],
            expected_store_revision=1,
            events=[events[1]],
        )
        submitted = {**events[10], "evidence_path": _CLOSEOUT_PATH}
        closeout = DocumentRevisionInput(
            document_id="fixture-closeout",
            revision_id="fixture-closeout-revision",
            logical_key=_CLOSEOUT_PATH,
            logical_type="CLOSEOUT",
            source_path=_CLOSEOUT_PATH,
            content=self.closeout_content,
        )
        closeout_result = self.store.submit_closeout_revision(
            request_id="fixture-closeout-submit",
            expected_prompt_revision_id=prompt["revision_id"],
            expected_store_revision=2,
            revision=closeout,
            closeout_submitted_event=submitted,
        )
        self.closeout = {
            "document_id": closeout.document_id,
            "revision_id": closeout.revision_id,
            "content_sha256": self.closeout_hash,
            **closeout_result,
        }
        acceptance_link = WorkflowDocumentLink(
            event_id=events[11]["event_id"],
            document_id=self.closeout["document_id"],
            revision_id=self.closeout["revision_id"],
            content_sha256=self.closeout["content_sha256"],
            link_role="closeout",
            evidence="Exact accepted fixture closeout revision.",
        )
        self.store.append_workflow_batch(
            request_id="fixture-closeout-acceptance",
            expected_repository_id=self.identity.repository_id,
            expected_milestone_id=self.identity.milestone_id,
            expected_selection_id=self.identity.selection_id,
            expected_selection_revision=self.identity.selection_revision,
            expected_prompt_revision_id=prompt["revision_id"],
            expected_store_revision=3,
            events=events[11:13],
            links=[acceptance_link],
            trusted_fixture_po_confirmation=True,
        )

    def _preview(self, request_id: str = "publication-request-1") -> dict:
        return self.service.preview(
            request_id=request_id,
            document_id=self.closeout["document_id"],
            revision_id=self.closeout["revision_id"],
            expected_sha256=self.closeout_hash,
        )

    def test_exact_accepted_preview_and_confirmation_materialize_unstaged_idempotently(self) -> None:
        preview = self._preview()
        self.assertEqual(preview["status"], "preview_ready")
        self.assertEqual(preview["source"]["content_sha256"], self.closeout_hash)
        self.assertEqual(preview["target_path"], _CLOSEOUT_PATH)
        self.assertIn("+Exact fixture bytes.", preview["proposed_diff"])
        self.assertFalse(self.target.exists())

        with self.assertRaises(PublicationNotAcceptedError):
            self.service.materialize(
                preview["publication_id"],
                preview["preview_fingerprint"],
                fixture_po_confirmation=False,
            )
        self.assertFalse(self.target.exists())

        with self.assertRaises(PublicationDriftError):
            self.service.materialize(
                preview["publication_id"],
                "0" * 64,
                fixture_po_confirmation=True,
            )
        self.assertFalse(self.target.exists())

        result = self.service.materialize(
            preview["publication_id"],
            preview["preview_fingerprint"],
            fixture_po_confirmation=True,
        )
        self.assertEqual(result["status"], "materialized_unstaged")
        self.assertEqual(self.target.read_bytes(), self.closeout_content.encode())
        self.assertIsNone(self.observer.staged_blob_sha256)
        modified_at = self.target.stat().st_mtime_ns
        repeated_preview = self._preview()
        repeated_materialization = self.service.materialize(
            preview["publication_id"],
            preview["preview_fingerprint"],
            fixture_po_confirmation=True,
        )
        self.assertEqual(repeated_preview["publication_id"], preview["publication_id"])
        self.assertEqual(repeated_materialization["status"], "materialized_unstaged")
        self.assertEqual(self.target.stat().st_mtime_ns, modified_at)
        persisted = self.store.get_document_publication(publication_id=preview["publication_id"])
        self.assertEqual(persisted["content_sha256"], self.closeout_hash)
        self.assertEqual(persisted["target_path"], _CLOSEOUT_PATH)
        self.assertEqual(persisted["status"], "materialized_unstaged")
        self.assertIsNotNone(persisted["confirmed_at"])

    def test_identical_existing_target_is_a_no_write_noop(self) -> None:
        self.target.write_bytes(self.closeout_content.encode())
        original_mtime = self.target.stat().st_mtime_ns
        preview = self._preview()
        self.assertEqual(preview["proposed_diff"], "")
        result = self.service.materialize(
            preview["publication_id"],
            preview["preview_fingerprint"],
            fixture_po_confirmation=True,
        )
        self.assertEqual(result["status"], "materialized_unstaged")
        self.assertEqual(self.target.stat().st_mtime_ns, original_mtime)

    def test_schema_extension_persists_publication_identity_and_fingerprints(self) -> None:
        preview = self._preview()
        connection = sqlite3.connect(self.database_path)
        try:
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(document_publication)")
            }
            self.assertTrue(
                {
                    "request_id",
                    "request_hash",
                    "target_path",
                    "target_branch",
                    "target_head",
                    "worktree_fingerprint",
                    "index_fingerprint",
                    "related_commit_id",
                }
                <= columns
            )
            self.assertEqual(
                connection.execute(
                    "SELECT document_id, revision_id, content_sha256, target_path, status "
                    "FROM document_publication WHERE publication_id = ?",
                    (preview["publication_id"],),
                ).fetchone(),
                (
                    self.closeout["document_id"],
                    self.closeout["revision_id"],
                    self.closeout_hash,
                    _CLOSEOUT_PATH,
                    "preview_ready",
                ),
            )
        finally:
            connection.close()

    def test_unaccepted_and_reopened_closeouts_are_rejected_without_target_writes(self) -> None:
        for state in ("submitted", "reopened", "legacy"):
            with self.subTest(state=state):
                fixture = _PublicationFixture(state)
                self.addCleanup(fixture.cleanup)
                with self.assertRaises(PublicationNotAcceptedError):
                    fixture.service.preview(
                        request_id="rejected-request",
                        document_id=fixture.closeout["document_id"],
                        revision_id=fixture.closeout["revision_id"],
                        expected_sha256=fixture.closeout_hash,
                    )
                self.assertFalse(fixture.target.exists())
                self.assertIsNone(fixture.store.get_document_publication(request_id="rejected-request"))

    def test_reopening_after_preview_invalidates_confirmation_without_writing(self) -> None:
        preview = self._preview()
        reopen = _event(
            self.workflow_events,
            event_type="milestone_reopened",
            summary="Reopen fixture milestone after preview",
            actor_role="product_owner",
            po_approved=True,
            event_fields={"reopen_target_phase": "IN_PROGRESS"},
        )
        self.store.append_workflow_batch(
            request_id="fixture-reopen-after-preview",
            expected_repository_id=self.identity.repository_id,
            expected_milestone_id=self.identity.milestone_id,
            expected_selection_id=self.identity.selection_id,
            expected_selection_revision=self.identity.selection_revision,
            expected_prompt_revision_id="fixture-prompt-revision",
            expected_store_revision=5,
            events=[reopen],
            trusted_fixture_po_confirmation=True,
        )
        result = self.service.materialize(
            preview["publication_id"],
            preview["preview_fingerprint"],
            fixture_po_confirmation=True,
        )
        self.assertEqual(result["status"], "drifted")
        self.assertFalse(self.target.exists())

    def test_changed_request_identity_collision_and_symlink_are_fail_closed(self) -> None:
        preview = self._preview()
        with self.assertRaises(IdempotencyConflictError):
            self.service.preview(
                request_id="publication-request-1",
                document_id="different-document",
                revision_id="different-revision",
                expected_sha256=self.closeout_hash,
            )
        self.assertFalse(self.target.exists())

        self.target.write_bytes(b"not the accepted closeout\n")
        with self.assertRaises(PublicationCollisionError):
            self.service.preview(
                request_id="collision-request",
                document_id=self.closeout["document_id"],
                revision_id=self.closeout["revision_id"],
                expected_sha256=self.closeout_hash,
            )
        self.assertEqual(self.target.read_bytes(), b"not the accepted closeout\n")
        self.target.unlink()
        outside = self.root / "outside.md"
        outside.write_text("outside\n", encoding="utf-8")
        self.target.symlink_to(outside)
        with self.assertRaises(PublicationCollisionError):
            self.service.preview(
                request_id="symlink-request",
                document_id=self.closeout["document_id"],
                revision_id=self.closeout["revision_id"],
                expected_sha256=self.closeout_hash,
            )
        self.assertEqual(outside.read_text(encoding="utf-8"), "outside\n")
        self.assertEqual(preview["status"], "preview_ready")

    def test_context_mismatch_and_symlinked_milestone_root_are_rejected(self) -> None:
        bad_context = FixturePublicationContext(
            repository_id="different-repository",
            repository_key=self.identity.repository_key,
            repository_root=self.identity.repository_root,
            milestone_id=self.identity.milestone_id,
            milestone_root="docs/milestones",
            worktree_root=self.worktree_root,
            test_only=True,
        )
        with self.assertRaises(IdentityMismatchError):
            self._service(context=bad_context)
        with self.assertRaises(IdentityMismatchError):
            self._service(
                context=FixturePublicationContext(
                    repository_id=self.identity.repository_id,
                    repository_key=self.identity.repository_key,
                    repository_root=self.identity.repository_root,
                    milestone_id="M999.002",
                    milestone_root="docs/milestones",
                    worktree_root=self.worktree_root,
                    test_only=True,
                )
            )
        with self.assertRaises(IdentityMismatchError):
            self._service(
                context=FixturePublicationContext(
                    repository_id=self.identity.repository_id,
                    repository_key=self.identity.repository_key,
                    repository_root=str(self.root / "different-registered-root"),
                    milestone_id=self.identity.milestone_id,
                    milestone_root="docs/milestones",
                    worktree_root=self.worktree_root,
                    test_only=True,
                )
            )
        with self.assertRaises(CloseoutPublicationError):
            self._service(
                context=FixturePublicationContext(
                    repository_id=self.identity.repository_id,
                    repository_key=self.identity.repository_key,
                    repository_root=self.identity.repository_root,
                    milestone_id=self.identity.milestone_id,
                    milestone_root="docs/../outside",
                    worktree_root=self.worktree_root,
                    test_only=True,
                )
            )

        other_root = self.root / "symlinked-worktree"
        other_root.mkdir()
        (other_root / "outside").mkdir()
        (other_root / "docs").symlink_to(other_root / "outside", target_is_directory=True)
        with self.assertRaises(CloseoutPublicationError):
            self._service(
                context=FixturePublicationContext(
                    repository_id=self.identity.repository_id,
                    repository_key=self.identity.repository_key,
                    repository_root=self.identity.repository_root,
                    milestone_id=self.identity.milestone_id,
                    milestone_root="docs/milestones",
                    worktree_root=other_root,
                    test_only=True,
                )
            )

    def test_branch_head_and_index_drift_refuse_materialization(self) -> None:
        for change in ("branch", "head", "index"):
            with self.subTest(change=change):
                fixture = _PublicationFixture("accepted")
                self.addCleanup(fixture.cleanup)
                preview = fixture.service.preview(
                    request_id=f"drift-{change}",
                    document_id=fixture.closeout["document_id"],
                    revision_id=fixture.closeout["revision_id"],
                    expected_sha256=fixture.closeout_hash,
                )
                if change == "branch":
                    fixture.observer.branch = "different-branch"
                elif change == "head":
                    fixture.observer.head = "b" * 40
                else:
                    fixture.observer.other_index = "changed"
                result = fixture.service.materialize(
                    preview["publication_id"],
                    preview["preview_fingerprint"],
                    fixture_po_confirmation=True,
                )
                self.assertEqual(result["status"], "drifted")
                self.assertFalse(fixture.target.exists())

    def test_wrong_hash_and_non_closeout_source_are_rejected_without_writes(self) -> None:
        with self.assertRaisesRegex(RevisionConflictError, "hash"):
            self.service.preview(
                request_id="wrong-hash",
                document_id=self.closeout["document_id"],
                revision_id=self.closeout["revision_id"],
                expected_sha256="0" * 64,
            )
        other = self.store.create_immutable_revision(
            DocumentRevisionInput(
                document_id="other-document",
                revision_id="other-revision",
                logical_key="docs/milestones/M999.001_other.md",
                logical_type="OTHER",
                source_path="docs/milestones/M999.001_other.md",
                content="# Not a closeout\n",
            )
        )
        with self.assertRaises(PublicationNotAcceptedError):
            self.service.preview(
                request_id="wrong-type",
                document_id=other["document_id"],
                revision_id=other["revision_id"],
                expected_sha256=other["content_sha256"],
            )
        self.assertFalse(self.target.exists())

    def test_failed_materialization_can_be_reconciled_without_git_mutation(self) -> None:
        preview = self._preview()
        with patch.object(
            self.service,
            "_write_new_file",
            side_effect=PublicationMaterializationError("injected fixture write failure"),
        ):
            with self.assertRaises(PublicationMaterializationError):
                self.service.materialize(
                    preview["publication_id"],
                    preview["preview_fingerprint"],
                    fixture_po_confirmation=True,
                )
        failed = self.store.get_document_publication(publication_id=preview["publication_id"])
        self.assertEqual(failed["status"], "failed")
        self.assertIsNotNone(failed["confirmed_at"])
        self.assertFalse(self.target.exists())

        self.target.write_bytes(self.closeout_content.encode())
        reconciled = self.service.reconcile(preview["publication_id"])
        self.assertEqual(reconciled["status"], "materialized_unstaged")
        self.assertEqual(self.target.read_bytes(), self.closeout_content.encode())
        self.assertIsNone(self.observer.staged_blob_sha256)

    def test_staged_and_committed_statuses_require_read_only_observation(self) -> None:
        preview = self._preview()
        materialized = self.service.materialize(
            preview["publication_id"],
            preview["preview_fingerprint"],
            fixture_po_confirmation=True,
        )
        self.observer.staged_blob_sha256 = self.closeout_hash
        unstaged = self.service.reconcile(preview["publication_id"])
        self.assertEqual(unstaged["status"], "materialized_unstaged")

        self.observer.is_staged = True
        self.observer.staged_blob_sha256 = self.closeout_hash
        staged = self.service.reconcile(preview["publication_id"])
        self.assertEqual(staged["status"], "staged")
        self.assertEqual(staged["related_commit_id"], None)

        self.observer.head = "b" * 40
        self.observer.head_blob_sha256 = self.closeout_hash
        self.observer.commit_blob_sha256 = self.closeout_hash
        self.observer.is_staged = False
        self.observer.staged_blob_sha256 = None
        committed = self.service.reconcile(preview["publication_id"], commit_id=self.observer.head)
        self.assertEqual(committed["status"], "committed")
        self.assertEqual(committed["related_commit_id"], self.observer.head)
        self.assertEqual(materialized["status"], "materialized_unstaged")

    def test_fixture_web_flow_requires_explicit_gate_and_has_no_git_mutation_routes(self) -> None:
        app = Flask(__name__)
        app.register_blueprint(
            create_fixture_closeout_publication_blueprint(
                self.service,
                fixture_po_confirmation=lambda _record: False,
            ),
            url_prefix="/fixture",
        )
        client = app.test_client()
        initial = client.get("/fixture/")
        self.assertEqual(initial.status_code, 200)
        self.assertIn(b"accepted_not_published", initial.data)
        routes = {rule.rule for rule in app.url_map.iter_rules()}
        self.assertIn("/fixture/preview", routes)
        self.assertFalse(any("stage" in route or "commit" in route for route in routes))
        preview = client.post(
            "/fixture/preview",
            data={
                "request_id": "web-publication",
                "document_id": self.closeout["document_id"],
                "revision_id": self.closeout["revision_id"],
                "content_sha256": self.closeout_hash,
            },
        )
        self.assertEqual(preview.status_code, 200)
        publication = self.store.get_document_publication(request_id="web-publication")
        denied = client.post(
            f"/fixture/{publication['publication_id']}/publish",
            data={
                "confirm_publish": "yes",
                "preview_fingerprint": publication["preview_fingerprint"],
            },
        )
        self.assertEqual(denied.status_code, 403)
        self.assertFalse(self.target.exists())

    def test_not_connected_git_review_is_explicit_and_never_materializes(self) -> None:
        unavailable = self._service(observer=NotConnectedGitReviewObserver())
        self.assertEqual(unavailable.integration_status, "NOT_CONNECTED")
        with self.assertRaisesRegex(PublicationGitUnavailableError, "NOT_CONNECTED"):
            unavailable.preview(
                request_id="not-connected",
                document_id=self.closeout["document_id"],
                revision_id=self.closeout["revision_id"],
                expected_sha256=self.closeout_hash,
            )
        self.assertFalse(self.target.exists())


class ReadOnlyGitPublicationObserverTests(unittest.TestCase):
    def test_commit_blob_is_verified_without_any_git_mutation(self) -> None:
        root = Path("/tmp/read-only-publication-fixture")
        target_path = "docs/milestones/M999.001_fixture_closeout.md"
        commit_id = "c" * 40
        content = b"# Accepted closeout\n"
        tree_record = f"100644 blob {'d' * 40}\t{target_path}\0".encode()
        state = {
            "head": _HEAD,
            "branch": {"name": "fixture-branch"},
            "working_tree": {"entries": []},
        }

        def run_read(_root, args, **kwargs):
            if args[0] == "ls-tree" and args[2] == "HEAD":
                return b"", b"", 0
            if args[0] == "ls-tree" and args[2] == commit_id:
                return tree_record, b"", 0
            if args[0] == "show" and args[1] == f"{commit_id}:{target_path}":
                return content, b"", 0
            raise AssertionError(f"Unexpected read-only Git command: {args}")

        with (
            patch.object(git_actions, "_state", return_value=state),
            patch.object(git_actions, "_index_records", return_value=b""),
            patch.object(git_actions, "_file_identity", return_value={"state": "absent"}),
            patch.object(git_actions, "_status_entry", return_value=None),
            patch.object(git_actions, "_resolve_commit", return_value=commit_id),
            patch.object(git_actions, "_run_read", side_effect=run_read),
            patch.object(git_actions, "_run_mutation") as mutation,
        ):
            result = ReadOnlyGitReviewObserver().inspect(root, target_path, commit_id=commit_id)
        self.assertEqual(result["verified_commit_id"], commit_id)
        self.assertEqual(result["commit_blob_sha256"], _sha256(content))
        mutation.assert_not_called()


class _PublicationFixture:
    def __init__(self, state: str) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="repoctl_publication_case_", dir="/tmp")
        self.root = Path(self.temporary.name).resolve()
        registered_root = self.root / "registered"
        registered_root.mkdir()
        self.worktree_root = self.root / "worktree"
        (self.worktree_root / "docs/milestones").mkdir(parents=True)
        self.target = self.worktree_root / _CLOSEOUT_PATH
        identity = StoreIdentity(
            repository_id="fixture-repository-id",
            repository_key="fixture-repository",
            repository_root=str(registered_root),
            milestone_id="M999.001",
            prompt_path=_PROMPT_PATH,
            selection_id="fixture-selection",
            selection_revision=1,
        )
        self.store = InformationStore.initialize_disposable(
            self.root / "store.sqlite3",
            identity,
            allow_unsupported_runtime_for_fixture=True,
        )
        content = "# Synthetic closeout\n"
        self.closeout_hash = _sha256(content.encode())
        if state == "legacy":
            self.closeout = self.store.create_immutable_revision(
                DocumentRevisionInput(
                    document_id="legacy-closeout",
                    revision_id="legacy-closeout-revision",
                    logical_key=_CLOSEOUT_PATH,
                    logical_type="CLOSEOUT",
                    source_path=_CLOSEOUT_PATH,
                    content=content,
                )
            )
        else:
            prompt = self.store.create_immutable_revision(
                DocumentRevisionInput(
                    document_id="case-prompt",
                    revision_id="case-prompt-revision",
                    logical_key=_PROMPT_PATH,
                    logical_type="PROMPT",
                    source_path=_PROMPT_PATH,
                    content="# Prompt\n",
                )
            )
            events = _closeout_history()
            self.store.select_approved_prompt_revision(
                request_id="case-prompt-approval",
                expected_store_revision=0,
                document_id=prompt["document_id"],
                revision_id=prompt["revision_id"],
                content_sha256=prompt["content_sha256"],
                prompt_approved_event=events[0],
                trusted_fixture_po_confirmation=True,
            )
            self.store.append_workflow_batch(
                request_id="case-start",
                expected_repository_id=identity.repository_id,
                expected_milestone_id=identity.milestone_id,
                expected_selection_id=identity.selection_id,
                expected_selection_revision=identity.selection_revision,
                expected_prompt_revision_id=prompt["revision_id"],
                expected_store_revision=1,
                events=[events[1]],
            )
            submitted = {**events[10], "evidence_path": _CLOSEOUT_PATH}
            self.closeout_content = content
            closeout_result = self.store.submit_closeout_revision(
                request_id="case-submit",
                expected_prompt_revision_id=prompt["revision_id"],
                expected_store_revision=2,
                revision=DocumentRevisionInput(
                    document_id="case-closeout",
                    revision_id="case-closeout-revision",
                    logical_key=_CLOSEOUT_PATH,
                    logical_type="CLOSEOUT",
                    source_path=_CLOSEOUT_PATH,
                    content=content,
                ),
                closeout_submitted_event=submitted,
            )
            self.closeout = {
                "document_id": "case-closeout",
                "revision_id": "case-closeout-revision",
                "content_sha256": self.closeout_hash,
                **closeout_result,
            }
            if state in {"accepted", "reopened"}:
                link = WorkflowDocumentLink(
                    event_id=events[11]["event_id"],
                    document_id=self.closeout["document_id"],
                    revision_id=self.closeout["revision_id"],
                    content_sha256=self.closeout["content_sha256"],
                    link_role="closeout",
                    evidence="Explicit fixture acceptance link.",
                )
                self.store.append_workflow_batch(
                    request_id="case-accept",
                    expected_repository_id=identity.repository_id,
                    expected_milestone_id=identity.milestone_id,
                    expected_selection_id=identity.selection_id,
                    expected_selection_revision=identity.selection_revision,
                    expected_prompt_revision_id=prompt["revision_id"],
                    expected_store_revision=3,
                    events=events[11:13],
                    links=[link],
                    trusted_fixture_po_confirmation=True,
                )
                if state == "reopened":
                    reopen = _event(
                        events,
                        event_type="milestone_reopened",
                        summary="Reopen fixture milestone",
                        actor_role="product_owner",
                        po_approved=True,
                        event_fields={"reopen_target_phase": "IN_PROGRESS"},
                    )
                    self.store.append_workflow_batch(
                        request_id="case-reopen",
                        expected_repository_id=identity.repository_id,
                        expected_milestone_id=identity.milestone_id,
                        expected_selection_id=identity.selection_id,
                        expected_selection_revision=identity.selection_revision,
                        expected_prompt_revision_id=prompt["revision_id"],
                        expected_store_revision=5,
                        events=[reopen],
                        trusted_fixture_po_confirmation=True,
                    )
        self.closeout_content = content
        self.observer = FixtureGitObserver()
        self.service = CloseoutPublicationService(
            self.store,
            FixturePublicationContext(
                repository_id=identity.repository_id,
                repository_key=identity.repository_key,
                repository_root=identity.repository_root,
                milestone_id=identity.milestone_id,
                milestone_root="docs/milestones",
                worktree_root=self.worktree_root,
                test_only=True,
            ),
            self.observer,
        )

    def cleanup(self) -> None:
        self.temporary.cleanup()
