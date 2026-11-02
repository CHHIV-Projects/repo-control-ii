from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import tomllib
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
from tests.procedure_fixtures import copy_five_artifact_v130_bundle


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        check=True,
        text=True,
    )
    return result.stdout.strip()


def _expected_six_artifacts() -> set[tuple[str, str, str]]:
    return {
        (".github/copilot-instructions.md", "bootstrap.md", "bootstrap"),
        ("AGENTS.md", "bootstrap.md", "bootstrap"),
        (".repo-control/workflow_core.md", "workflow_core.md", "copy"),
        (
            ".agents/skills/repo-control-implementation/SKILL.md",
            "skills/repo-control-implementation/SKILL.md",
            "copy",
        ),
        (
            ".agents/skills/repo-control-blocker-bug-escalation/SKILL.md",
            "skills/repo-control-blocker-bug-escalation/SKILL.md",
            "copy",
        ),
        (
            ".agents/skills/repo-control-validation-closeout/SKILL.md",
            "skills/repo-control-validation-closeout/SKILL.md",
            "copy",
        ),
    }


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
        copy_five_artifact_v130_bundle(BUNDLE_ROOT, self.bundle_root)
        self.authorized_sources = sorted(load_bundle(self.bundle_root).source_hashes)
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

    def _codex_artifact_marker(self) -> str:
        candidate_source = self.workflow_core.read_bytes()
        self.workflow_core.write_bytes((BUNDLE_ROOT / "workflow_core.md").read_bytes())
        try:
            marker = render_release_preparation_marker(
                "1.4.0",
                self.authorized_sources,
                self.bundle_root,
                add_codex_entrypoint=True,
                repository_key=self.entry.key,
                repository_id=self.entry.repository_id,
                repository_root=self.entry.path,
                milestone_id=self.candidate["milestone_id"],
            )
        finally:
            self.workflow_core.write_bytes(candidate_source)
        self.prompt_path.write_text("# Approved release task\n\n" + marker + "\n", encoding="utf-8")
        return marker

    def test_marker_generation_binds_valid_base_without_manual_hash_entry(self) -> None:
        marker = render_release_preparation_marker(
            "1.5.0",
            ["workflow_core.md"],
            BUNDLE_ROOT,
        )
        self.assertIn("base_manifest_sha256", marker)
        self.assertIn("workflow_core.md", marker)
        self.assertIn("repoctl-procedure-release-preparation:v1", marker)
        self.assertNotIn("authorized_artifact_additions", marker)
        with self.assertRaisesRegex(ProcedureReleaseError, "higher numeric"):
            render_release_preparation_marker("1.4.0", ["workflow_core.md"], BUNDLE_ROOT)
        with self.assertRaisesRegex(ProcedureReleaseError, "listed in the current canonical bundle"):
            render_release_preparation_marker("1.5.0", ["unknown.md"], BUNDLE_ROOT)

    def test_codex_marker_binds_exact_artifact_addition_and_active_identity(self) -> None:
        marker = self._codex_artifact_marker()
        self.assertIn("repoctl-procedure-release-preparation:v2", marker)
        self.assertIn('"target": "AGENTS.md"', marker)

        status = inspect_procedure_release_preparation(
            self.repo,
            self.candidate,
            self.workflow,
            self.bundle_root,
            repository_key=self.entry.key,
            repository_id=self.entry.repository_id,
        )
        self.assertTrue(status["authorized"])
        self.assertEqual(status["authorized_artifact_additions"], [{
            "source": "bootstrap.md",
            "target": "AGENTS.md",
            "renderer": "bootstrap",
        }])
        base_artifacts = tomllib.loads(
            (self.bundle_root / "manifest.toml").read_text(encoding="utf-8")
        )["artifacts"]
        self.assertEqual(len(base_artifacts), 5)

        mismatched_repository = inspect_procedure_release_preparation(
            self.repo,
            self.candidate,
            self.workflow,
            self.bundle_root,
            repository_key="another-repository",
            repository_id=self.entry.repository_id,
        )
        self.assertFalse(mismatched_repository["authorized"])
        self.assertEqual(mismatched_repository["status"], "NOT_AUTHORIZED")

        mismatched_milestone = inspect_procedure_release_preparation(
            self.repo,
            {**self.candidate, "milestone_id": "M009.998"},
            self.workflow,
            self.bundle_root,
            repository_key=self.entry.key,
            repository_id=self.entry.repository_id,
        )
        self.assertFalse(mismatched_milestone["authorized"])
        self.assertEqual(mismatched_milestone["status"], "NOT_AUTHORIZED")

        self.prompt_path.write_text(
            "# Approved release task\n\n"
            + marker.replace('"target": "AGENTS.md"', '"target": "user-owned.md"')
            + "\n",
            encoding="utf-8",
        )
        unauthorized_addition = inspect_procedure_release_preparation(
            self.repo,
            self.candidate,
            self.workflow,
            self.bundle_root,
            repository_key=self.entry.key,
            repository_id=self.entry.repository_id,
        )
        self.assertFalse(unauthorized_addition["authorized"])
        self.assertEqual(unauthorized_addition["status"], "INTEGRITY_UNAVAILABLE")
        self.assertIn("marker is invalid", unauthorized_addition["reason"])

    def test_six_artifact_transition_previews_and_publishes_only_isolated_bundle(self) -> None:
        published_manifest = (BUNDLE_ROOT / "manifest.toml").read_bytes()
        published_bundle = load_bundle(BUNDLE_ROOT)
        published_source_bytes = {
            path: (BUNDLE_ROOT / path).read_bytes()
            for path in published_bundle.source_hashes
        }
        self._codex_artifact_marker()

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
            self.assertEqual(
                preview["release_preparation"]["authorized_artifact_additions"],
                [{
                    "source": "bootstrap.md",
                    "target": "AGENTS.md",
                    "renderer": "bootstrap",
                }],
            )
            change_paths = {item["path"] for item in preview["preview"]["changes"]}
            self.assertTrue(
                {target for target, _, _ in _expected_six_artifacts()}.issubset(change_paths)
            )
            base_artifacts = tomllib.loads(
                (self.bundle_root / "manifest.toml").read_text(encoding="utf-8")
            )["artifacts"]
            self.assertEqual(len(base_artifacts), 5)

            published = publish_authorized_procedure_release(
                self.entry,
                self.candidate,
                self.workflow,
                self.bundle_root,
            )

        self.assertEqual(published["status"], "PUBLISHED")
        self.assertEqual(published["release"], "1.4.0")
        candidate_bundle = load_bundle(self.bundle_root)
        self.assertEqual(len(candidate_bundle.artifacts), 6)
        self.assertEqual(
            {
                (item.target, item.source, item.renderer)
                for item in candidate_bundle.artifacts
            },
            _expected_six_artifacts(),
        )
        published_status = inspect_procedure_release_preparation(
            self.repo,
            self.candidate,
            self.workflow,
            self.bundle_root,
            repository_key=self.entry.key,
            repository_id=self.entry.repository_id,
        )
        self.assertEqual(published_status["status"], "RELEASE_PUBLISHED")
        self.assertEqual(published_status["integrity_status"], "CURRENT")
        self.assertEqual((BUNDLE_ROOT / "manifest.toml").read_bytes(), published_manifest)
        self.assertEqual(
            {
                path: (BUNDLE_ROOT / path).read_bytes()
                for path in published_bundle.source_hashes
            },
            published_source_bytes,
        )

    def test_publication_revalidates_authorization_after_preview(self) -> None:
        self._codex_artifact_marker()
        original_manifest = (self.bundle_root / "manifest.toml").read_bytes()
        original_preview = preview_authorized_procedure_release

        def mutate_authorization_after_preview(*args, **kwargs):
            preview = original_preview(*args, **kwargs)
            self.prompt_path.write_text(
                self.prompt_path.read_text(encoding="utf-8").replace('"candidate_release": "1.4.0"', '"candidate_release": "1.5.0"'),
                encoding="utf-8",
            )
            return preview

        with (
            mock.patch(
                "repoctl.workflow.coder_interface.CENTRAL_REPOSITORY_REGISTRY_PATH",
                self.registry_path,
            ),
            mock.patch(
                "repoctl.procedure_release.preview_authorized_procedure_release",
                side_effect=mutate_authorization_after_preview,
            ),
        ):
            with self.assertRaisesRegex(ProcedureReleaseError, "authorization or candidate changed"):
                publish_authorized_procedure_release(
                    self.entry,
                    self.candidate,
                    self.workflow,
                    self.bundle_root,
                )
        self.assertEqual((self.bundle_root / "manifest.toml").read_bytes(), original_manifest)

    def test_candidate_source_change_after_preview_is_not_published(self) -> None:
        self._codex_artifact_marker()
        original_manifest = (self.bundle_root / "manifest.toml").read_bytes()
        original_preview = preview_authorized_procedure_release

        def mutate_source_after_preview(*args, **kwargs):
            preview = original_preview(*args, **kwargs)
            bootstrap = self.bundle_root / "bootstrap.md"
            bootstrap.write_bytes(bootstrap.read_bytes() + b"\nlate candidate edit\n")
            return preview

        with (
            mock.patch(
                "repoctl.workflow.coder_interface.CENTRAL_REPOSITORY_REGISTRY_PATH",
                self.registry_path,
            ),
            mock.patch(
                "repoctl.procedure_release.preview_authorized_procedure_release",
                side_effect=mutate_source_after_preview,
            ),
        ):
            with self.assertRaisesRegex(ProcedureReleaseError, "Candidate sources changed"):
                publish_authorized_procedure_release(
                    self.entry,
                    self.candidate,
                    self.workflow,
                    self.bundle_root,
                )
        self.assertEqual((self.bundle_root / "manifest.toml").read_bytes(), original_manifest)

    def test_stale_base_manifest_digest_fails_closed_before_preview(self) -> None:
        self._codex_artifact_marker()
        manifest_path = self.bundle_root / "manifest.toml"
        manifest_path.write_bytes(manifest_path.read_bytes() + b"\n")
        stale_manifest = manifest_path.read_bytes()

        with self.assertRaisesRegex(ProcedureReleaseError, "not authorized"):
            preview_authorized_procedure_release(
                self.entry,
                self.candidate,
                self.workflow,
                self.bundle_root,
            )
        self.assertEqual(manifest_path.read_bytes(), stale_manifest)

    def test_interrupted_manifest_replace_preserves_published_base(self) -> None:
        original_manifest = (self.bundle_root / "manifest.toml").read_bytes()
        with (
            mock.patch(
                "repoctl.workflow.coder_interface.CENTRAL_REPOSITORY_REGISTRY_PATH",
                self.registry_path,
            ),
            mock.patch("repoctl.procedure_release.os.replace", side_effect=OSError("simulated interruption")),
        ):
            with self.assertRaisesRegex(OSError, "simulated interruption"):
                publish_authorized_procedure_release(
                    self.entry,
                    self.candidate,
                    self.workflow,
                    self.bundle_root,
                )
        self.assertEqual((self.bundle_root / "manifest.toml").read_bytes(), original_manifest)
        manifest = tomllib.loads(original_manifest.decode("utf-8"))
        self.assertEqual(manifest["release"], "1.3.0")

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
        self.assertEqual(len(load_bundle(self.bundle_root).artifacts), 5)
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
