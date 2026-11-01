from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from repoctl.procedure_governance import BUNDLE_ROOT, load_bundle
from repoctl.procedure_release import (
    ProcedureReleaseError,
    inspect_procedure_release_preparation,
    preview_authorized_procedure_release,
    publish_authorized_procedure_release,
    render_release_preparation_marker,
)
from repoctl.scanner.util import make_repository_id
from repoctl.web.repository_registry import RepositoryEntry


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        check=True,
        text=True,
    )
    return result.stdout.strip()


class ProcedureReleaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "managed"
        self.repo.mkdir()
        _git(self.repo, "init", "-q")
        _git(self.repo, "config", "user.email", "test@example.com")
        _git(self.repo, "config", "user.name", "Test User")
        (self.repo / "tracked.txt").write_text("baseline\n", encoding="utf-8")
        _git(self.repo, "add", "tracked.txt")
        _git(self.repo, "commit", "-qm", "initial")
        self.bundle_root = self.repo / "shared" / "procedure"
        shutil.copytree(BUNDLE_ROOT, self.bundle_root)
        overlay_path = self.repo / "docs" / "context" / "coding_agent_rules.md"
        overlay_path.parent.mkdir(parents=True)
        overlay_path.write_text(
            "# Overlay\n\n<!-- repo-control-project-overlay:v1 -->\n",
            encoding="utf-8",
        )
        self.entry = RepositoryEntry(
            key="managed",
            name="Managed",
            path=self.repo.resolve(),
            repository_id=make_repository_id(self.repo),
            milestone_root="docs/milestones",
            documents={"coding_agent_rules": "docs/context/coding_agent_rules.md"},
        )
        self.registry_path = self.root / "repositories.toml"
        self.registry_path.write_text(
            'default = "managed"\n'
            "[repositories.managed]\n"
            'name = "Managed"\n'
            f"path = {json.dumps(str(self.repo.resolve()))}\n"
            'milestone_root = "docs/milestones"\n'
            "[repositories.managed.documents]\n"
            'coding_agent_rules = "docs/context/coding_agent_rules.md"\n',
            encoding="utf-8",
        )
        self.prompt_path = self.repo / "docs" / "milestones" / "M009.999_release_prompt.md"
        self.prompt_path.parent.mkdir(parents=True)
        self.candidate = {
            "milestone_id": "M009.999",
            "prompt_path": "docs/milestones/M009.999_release_prompt.md",
        }
        self.workflow = {
            "phase": "IN_PROGRESS",
            "event_history": [{"event_type": "implementation_started"}],
        }
        manifest_digest = hashlib.sha256(
            (self.bundle_root / "manifest.toml").read_bytes()
        ).hexdigest()
        self.prompt_path.write_text(
            "# Approved release task\n\n"
            "<!-- repoctl-procedure-release-preparation:v1\n"
            + json.dumps(
                {
                    "schema_version": 1,
                    "base_manifest_sha256": manifest_digest,
                    "candidate_release": "1.4.0",
                    "authorized_sources": ["workflow_core.md"],
                },
                indent=2,
            )
            + "\n-->\n",
            encoding="utf-8",
        )
        self.workflow_core = self.bundle_root / "workflow_core.md"
        self.workflow_core.write_text(
            self.workflow_core.read_text(encoding="utf-8") + "\nrelease candidate content\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_marker_generation_binds_valid_base_without_manual_hash_entry(self) -> None:
        marker = render_release_preparation_marker(
            "1.4.0",
            ["workflow_core.md"],
            BUNDLE_ROOT,
        )
        self.assertIn("base_manifest_sha256", marker)
        self.assertIn("workflow_core.md", marker)
        with self.assertRaisesRegex(ProcedureReleaseError, "higher numeric"):
            render_release_preparation_marker("1.3.0", ["workflow_core.md"], BUNDLE_ROOT)
        with self.assertRaisesRegex(ProcedureReleaseError, "listed in the current canonical bundle"):
            render_release_preparation_marker("1.4.0", ["unknown.md"], BUNDLE_ROOT)

    def test_explicit_authorization_previews_and_publishes_only_valid_candidate(self) -> None:
        status = inspect_procedure_release_preparation(
            self.repo,
            self.candidate,
            self.workflow,
            self.bundle_root,
        )
        self.assertEqual(status["status"], "RELEASE_IN_PREPARATION")
        self.assertTrue(status["authorized"])
        self.assertEqual(status["changed_sources"], ["workflow_core.md"])

        git_before = _git(self.repo, "status", "--porcelain")
        with mock.patch(
            "repoctl.workflow.coder_interface.CENTRAL_REPOSITORY_REGISTRY_PATH",
            self.registry_path,
        ):
            preview = preview_authorized_procedure_release(
                self.entry,
                self.candidate,
                self.workflow,
                self.bundle_root,
            )
            self.assertTrue(preview["preview_read_only"])
            self.assertEqual(preview["release_preparation"]["candidate_release"], "1.4.0")
            published = publish_authorized_procedure_release(
                self.entry,
                self.candidate,
                self.workflow,
                self.bundle_root,
            )

        self.assertEqual(published["status"], "PUBLISHED")
        self.assertEqual(published["release"], "1.4.0")
        self.assertEqual(load_bundle(self.bundle_root).release, "1.4.0")
        after_publish = inspect_procedure_release_preparation(
            self.repo,
            self.candidate,
            self.workflow,
            self.bundle_root,
        )
        self.assertEqual(after_publish["status"], "RELEASE_PUBLISHED")
        self.assertEqual(after_publish["integrity_status"], "CURRENT")
        self.assertEqual(git_before, _git(self.repo, "status", "--porcelain"))

    def test_incomplete_allowlisted_candidate_remains_scoped_and_unpublishable(self) -> None:
        self.workflow_core.unlink()
        status = inspect_procedure_release_preparation(
            self.repo,
            self.candidate,
            self.workflow,
            self.bundle_root,
        )
        self.assertEqual(status["status"], "RELEASE_IN_PREPARATION")
        self.assertTrue(status["authorized"])
        self.assertEqual(status["candidate_validation_status"], "INVALID")
        with mock.patch(
            "repoctl.workflow.coder_interface.CENTRAL_REPOSITORY_REGISTRY_PATH",
            self.registry_path,
        ):
            with self.assertRaisesRegex(
                ProcedureReleaseError,
                "Cannot inspect candidate canonical source",
            ):
                preview_authorized_procedure_release(
                    self.entry,
                    self.candidate,
                    self.workflow,
                    self.bundle_root,
                )

    def test_unapproved_source_change_and_unauthorized_lifecycle_fail_closed(self) -> None:
        (self.bundle_root / "bootstrap.md").write_text(
            (self.bundle_root / "bootstrap.md").read_text(encoding="utf-8") + "\nunauthorized\n",
            encoding="utf-8",
        )
        status = inspect_procedure_release_preparation(
            self.repo,
            self.candidate,
            self.workflow,
            self.bundle_root,
        )
        self.assertEqual(status["status"], "INTEGRITY_UNAVAILABLE")
        self.assertFalse(status["authorized"])
        self.assertIn("outside the active milestone allowlist", status["reason"])

        waiting = {**self.workflow, "phase": "WAITING"}
        unauthorized = inspect_procedure_release_preparation(
            self.repo,
            self.candidate,
            waiting,
            self.bundle_root,
        )
        self.assertEqual(unauthorized["status"], "NOT_AUTHORIZED")
        self.assertFalse(unauthorized["authorized"])

    def test_unmarked_source_drift_is_integrity_unavailable_not_release_preparation(self) -> None:
        self.prompt_path.write_text("# Ordinary milestone\n", encoding="utf-8")
        status = inspect_procedure_release_preparation(
            self.repo,
            self.candidate,
            self.workflow,
            self.bundle_root,
        )
        self.assertEqual(status["status"], "INTEGRITY_UNAVAILABLE")
        self.assertFalse(status["authorized"])
