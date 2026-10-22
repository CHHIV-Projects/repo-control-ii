from __future__ import annotations

import contextlib
import hashlib
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from repoctl.cli import main
from repoctl.procedure_governance import (
    ProcedureGovernanceError,
    inspect_procedure,
    load_bundle,
    materialize_preview,
    preview_reconciliation,
)
from repoctl.scanner.util import make_repository_id
from repoctl.web.repository_registry import RepositoryEntry


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
    return path.resolve()


class ProcedureGovernanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.repo = _git_repo(self.root / "repo")
        (self.repo / "docs/milestones").mkdir(parents=True)
        overlay = self.repo / "docs/context/coding_agent_rules.md"
        overlay.parent.mkdir(parents=True)
        overlay.write_text(
            "# Project overlay\n\n"
            "<!-- repo-control-project-overlay:v1 -->\n\n"
            "## Repository-specific rules\n\n"
            "- Keep fixture data local.\n",
            encoding="utf-8",
        )
        self.entry = RepositoryEntry(
            key="repo-control-ii",
            name="Repo Control II",
            path=self.repo,
            repository_id=make_repository_id(self.repo),
            milestone_root="docs/milestones",
            documents={"coding_agent_rules": "docs/context/coding_agent_rules.md"},
        )
        self.bundle = load_bundle()
        self.registry_path = self.root / "repositories.toml"
        self._write_registry([self.entry])
        self.registry_patcher = mock.patch(
            "repoctl.workflow.coder_interface.CENTRAL_REPOSITORY_REGISTRY_PATH",
            self.registry_path,
        )
        self.registry_patcher.start()

    def tearDown(self) -> None:
        self.registry_patcher.stop()
        self.temp_dir.cleanup()

    def _write_registry(self, entries: list[RepositoryEntry]) -> None:
        lines = [f"default = {json.dumps(entries[0].key)}"]
        for entry in entries:
            lines.extend(
                [
                    "",
                    f"[repositories.{entry.key}]",
                    f"name = {json.dumps(entry.name)}",
                    f"path = {json.dumps(str(entry.path))}",
                    f"milestone_root = {json.dumps(entry.milestone_root)}",
                    "",
                    f"[repositories.{entry.key}.documents]",
                ]
            )
            for role, path in entry.documents.items():
                lines.append(f"{role} = {json.dumps(path)}")
        self.registry_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _materialize(self) -> None:
        plan = preview_reconciliation(self.entry, self.bundle)
        self.assertTrue(plan.can_materialize, plan.blockers)
        materialize_preview(self.entry, plan)

    def test_manifest_sources_are_hashed_and_skills_match_accepted_copies(self) -> None:
        self.assertEqual(self.bundle.schema_version, 1)
        self.assertEqual(self.bundle.release, "1.1.0")
        for source, expected in self.bundle.source_hashes.items():
            data = (self.bundle.root / source).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), expected)
        for skill in (
            "repo-control-implementation",
            "repo-control-blocker-bug-escalation",
            "repo-control-validation-closeout",
        ):
            canonical = self.bundle.root / "skills" / skill / "SKILL.md"
            accepted = Path(__file__).parents[1] / ".agents/skills" / skill / "SKILL.md"
            self.assertEqual(canonical.read_bytes(), accepted.read_bytes())

    def test_manifest_missing_malformed_and_source_hash_mismatch_fail_clearly(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            copied = Path(td) / "bundle"
            shutil.copytree(self.bundle.root, copied)
            (copied / "manifest.toml").unlink()
            with self.assertRaisesRegex(ProcedureGovernanceError, "manifest is missing"):
                load_bundle(copied)

        with tempfile.TemporaryDirectory() as td:
            copied = Path(td) / "bundle"
            shutil.copytree(self.bundle.root, copied)
            (copied / "manifest.toml").write_text("not valid TOML =", encoding="utf-8")
            with self.assertRaisesRegex(ProcedureGovernanceError, "Cannot read"):
                load_bundle(copied)

        with tempfile.TemporaryDirectory() as td:
            copied = Path(td) / "bundle"
            shutil.copytree(self.bundle.root, copied)
            with (copied / "bootstrap.md").open("a", encoding="utf-8") as stream:
                stream.write("\nchanged\n")
            with self.assertRaisesRegex(ProcedureGovernanceError, "hash mismatch"):
                load_bundle(copied)

        with tempfile.TemporaryDirectory() as td:
            copied = Path(td) / "bundle"
            shutil.copytree(self.bundle.root, copied)
            manifest = copied / "manifest.toml"
            manifest.write_text(
                manifest.read_text(encoding="utf-8").replace(
                    'target = ".github/copilot-instructions.md"',
                    'target = "../outside.md"',
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ProcedureGovernanceError, "repository-relative"):
                load_bundle(copied)

    def test_rendering_is_deterministic_and_project_agnostic(self) -> None:
        first = preview_reconciliation(self.entry, self.bundle)
        second = preview_reconciliation(self.entry, self.bundle)
        self.assertEqual(first.files, second.files)
        self.assertEqual(first.lock_bytes, second.lock_bytes)
        rendered = dict(first.files)[".github/copilot-instructions.md"].decode()
        self.assertIn("`docs/context/coding_agent_rules.md`", rendered)
        self.assertIn(f"`{self.bundle.release}`", rendered)
        self.assertNotIn("/home/chuck/projects/repo-control-ii`", rendered)
        self.assertNotIn("Vocab", rendered)

    def test_initial_preview_is_exact_read_only_and_materializes_only_generated_files(self) -> None:
        overlay_path = self.repo / "docs/context/coding_agent_rules.md"
        before_overlay = overlay_path.read_bytes()
        plan = preview_reconciliation(self.entry, self.bundle)
        self.assertTrue(plan.can_materialize, plan.blockers)
        data = plan.as_dict()
        self.assertTrue(data["overlay_untouched"])
        self.assertEqual(data["removals"], [])
        self.assertEqual(sum(change["action"] == "create" for change in data["changes"]), 6)
        bootstrap = next(item for item in data["changes"] if item["path"] == ".github/copilot-instructions.md")
        self.assertIn("+This file is generated", bootstrap["diff"])
        self.assertFalse((self.repo / ".github/copilot-instructions.md").exists())
        self.assertEqual(overlay_path.read_bytes(), before_overlay)

        result = materialize_preview(self.entry, plan)
        self.assertEqual(result["status"], "CURRENT")
        self.assertEqual(overlay_path.read_bytes(), before_overlay)
        self.assertEqual(inspect_procedure(self.entry, self.bundle)["status"], "CURRENT")

    def test_exact_existing_skill_copies_are_adopted_without_rewriting(self) -> None:
        skill = "repo-control-implementation"
        target = self.repo / ".agents/skills" / skill / "SKILL.md"
        target.parent.mkdir(parents=True)
        canonical = self.bundle.root / "skills" / skill / "SKILL.md"
        target.write_bytes(canonical.read_bytes())
        original = target.read_bytes()

        plan = preview_reconciliation(self.entry, self.bundle)
        skill_change = next(item for item in plan.changes if item["path"] == f".agents/skills/{skill}/SKILL.md")
        self.assertEqual(skill_change["action"], "adopt")
        self.assertTrue(plan.can_materialize, plan.blockers)
        result = materialize_preview(self.entry, plan)
        self.assertEqual(result["status"], "CURRENT")
        self.assertEqual(target.read_bytes(), original)

    def test_missing_skill_and_edited_generated_file_are_detected_and_not_overwritten(self) -> None:
        self._materialize()
        skill = self.repo / ".agents/skills/repo-control-implementation/SKILL.md"
        skill.unlink()
        self.assertEqual(inspect_procedure(self.entry, self.bundle)["status"], "MISSING")
        plan = preview_reconciliation(self.entry, self.bundle)
        self.assertTrue(plan.can_materialize)
        materialize_preview(self.entry, plan)
        self.assertEqual(inspect_procedure(self.entry, self.bundle)["status"], "CURRENT")

        skill.write_text("unmanaged edit\n", encoding="utf-8")
        status = inspect_procedure(self.entry, self.bundle)
        self.assertEqual(status["status"], "HASH_MISMATCH")
        plan = preview_reconciliation(self.entry, self.bundle)
        self.assertFalse(plan.can_materialize)
        self.assertTrue(any("unmanaged edit" in item for item in plan.blockers))
        with self.assertRaisesRegex(ProcedureGovernanceError, "blocked"):
            materialize_preview(self.entry, plan)
        self.assertEqual(skill.read_text(encoding="utf-8"), "unmanaged edit\n")

    def test_stale_release_can_be_previewed_and_updated_but_stale_plan_is_refused(self) -> None:
        self._materialize()
        original_bundle = self.bundle
        major, minor, patch = (int(part) for part in self.bundle.release.split("."))
        next_bundle = replace(self.bundle, release=f"{major}.{minor}.{patch + 1}")
        status = inspect_procedure(self.entry, next_bundle)
        self.assertEqual(status["status"], "STALE_VERSION")
        plan = preview_reconciliation(self.entry, next_bundle)
        self.assertTrue(plan.can_materialize, plan.blockers)
        self.assertTrue(any(item["path"] == ".github/copilot-instructions.md" and item["action"] == "update" for item in plan.changes))
        (self.repo / "docs/context/coding_agent_rules.md").write_text(
            "# Changed overlay\n\n<!-- repo-control-project-overlay:v1 -->\n\n"
            "## Repository-specific rules\n\n- Updated constraint.\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ProcedureGovernanceError, "stale"):
            materialize_preview(self.entry, plan, next_bundle)

        fresh_plan = preview_reconciliation(self.entry, next_bundle)
        materialize_preview(self.entry, fresh_plan, next_bundle)
        self.assertEqual(inspect_procedure(self.entry, next_bundle)["status"], "CURRENT")
        self.assertEqual(original_bundle.release, self.bundle.release)

    def test_corrupt_lock_and_unversioned_bundle_changes_are_not_reconciled_silently(self) -> None:
        self._materialize()
        lock = self.repo / ".repo-control/shared-procedure.lock.toml"
        valid_lock = lock.read_bytes()
        lock.write_text("bad TOML =", encoding="utf-8")
        self.assertEqual(inspect_procedure(self.entry, self.bundle)["status"], "INCOMPATIBLE")
        plan = preview_reconciliation(self.entry, self.bundle)
        self.assertFalse(plan.can_materialize)

        lock.write_bytes(valid_lock)
        lock.write_text(
            lock.read_text(encoding="utf-8").replace(
                'overlay_path = "docs/context/coding_agent_rules.md"',
                'overlay_path = "docs/context/other_rules.md"',
            ),
            encoding="utf-8",
        )
        self.assertEqual(inspect_procedure(self.entry, self.bundle)["status"], "INCOMPATIBLE")
        self.assertFalse(preview_reconciliation(self.entry, self.bundle).can_materialize)

        lock.write_bytes(valid_lock)
        changed_bundle = replace(self.bundle, manifest_sha256="0" * 64)
        self.assertEqual(inspect_procedure(self.entry, changed_bundle)["status"], "HASH_MISMATCH")
        self.assertFalse(preview_reconciliation(self.entry, changed_bundle).can_materialize)

    def test_overlay_and_target_collisions_block_writes(self) -> None:
        overlay = self.repo / "docs/context/coding_agent_rules.md"
        overlay.write_text("# Unmarked overlay\n", encoding="utf-8")
        self.assertEqual(inspect_procedure(self.entry, self.bundle)["status"], "OVERLAY_INVALID")
        plan = preview_reconciliation(self.entry, self.bundle)
        self.assertFalse(plan.can_materialize)
        self.assertTrue(plan.as_dict()["overlay_untouched"])
        self.assertFalse((self.repo / ".github").exists())

        overlay.write_text(
            "# Project overlay\n\n<!-- repo-control-project-overlay:v1 -->\n\n"
            "## Repository-specific rules\n",
            encoding="utf-8",
        )
        target = self.repo / ".github/copilot-instructions.md"
        target.parent.mkdir()
        target.write_text("hand-written instructions\n", encoding="utf-8")
        self.assertEqual(inspect_procedure(self.entry, self.bundle)["status"], "INCOMPATIBLE")
        plan = preview_reconciliation(self.entry, self.bundle)
        self.assertFalse(plan.can_materialize)
        self.assertTrue(any(change["action"] == "blocked" for change in plan.changes))
        self.assertEqual(target.read_text(encoding="utf-8"), "hand-written instructions\n")
        target.unlink()
        target.mkdir()
        self.assertEqual(inspect_procedure(self.entry, self.bundle)["status"], "INCOMPATIBLE")
        self.assertFalse(preview_reconciliation(self.entry, self.bundle).can_materialize)
        target.rmdir()
        target.parent.rmdir()
        target.parent.write_text("parent collision\n", encoding="utf-8")
        status = inspect_procedure(self.entry, self.bundle)
        self.assertEqual(status["status"], "INCOMPATIBLE")
        self.assertTrue(any("parent path component is not a directory" in issue["detail"] for issue in status["issues"]))

    def test_symlinked_targets_and_unmanaged_repositories_are_refused(self) -> None:
        outside = self.root / "outside.md"
        outside.write_text("outside\n", encoding="utf-8")
        (self.repo / ".github").symlink_to(self.root, target_is_directory=True)
        self.assertEqual(inspect_procedure(self.entry, self.bundle)["status"], "INCOMPATIBLE")
        plan = preview_reconciliation(self.entry, self.bundle)
        self.assertFalse(plan.can_materialize)
        self.assertEqual(outside.read_text(encoding="utf-8"), "outside\n")

        unmanaged = _git_repo(self.root / "unmanaged")
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            result = main(["coder-procedure-status", "--repository", str(unmanaged)])
        self.assertEqual(result, 2)
        self.assertIn("not managed", stderr.getvalue())

    def test_cli_inspect_and_preview_use_central_registry_and_do_not_write(self) -> None:
        vocab = _git_repo(self.root / "vocab")
        (vocab / "docs/milestones").mkdir(parents=True)
        vocab_overlay = vocab / "docs/context/coding_agent_rules.md"
        vocab_overlay.parent.mkdir(parents=True)
        vocab_overlay.write_text(
            "# Vocab overlay\n\n<!-- repo-control-project-overlay:v1 -->\n\n"
            "## Repository-specific rules\n\n- Preserve learner data.\n",
            encoding="utf-8",
        )
        vocab_entry = RepositoryEntry(
            key="vocab-app",
            name="Vocab App",
            path=vocab,
            repository_id=make_repository_id(vocab),
            milestone_root="docs/milestones",
            documents={"coding_agent_rules": "docs/context/coding_agent_rules.md"},
        )
        self._write_registry([self.entry, vocab_entry])
        before = sorted(path.relative_to(self.repo).as_posix() for path in self.repo.rglob("*"))
        git_status_before = subprocess.run(
            ["git", "-C", str(self.repo), "status", "--short"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = main(["coder-procedure-preview", "--repository", str(self.repo), "--format", "json"])
        self.assertEqual(result, 0)
        preview = json.loads(out.getvalue())
        self.assertEqual(preview["repository"]["key"], "repo-control-ii")
        self.assertEqual(preview["status"], "MISSING")
        self.assertTrue(preview["overlay_untouched"])
        after = sorted(path.relative_to(self.repo).as_posix() for path in self.repo.rglob("*"))
        self.assertEqual(before, after)
        self.assertEqual(
            git_status_before,
            subprocess.run(
                ["git", "-C", str(self.repo), "status", "--short"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout,
        )

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = main(["coder-procedure-status", "--repository", str(self.repo), "--format", "json"])
        self.assertEqual(result, 0)
        status = json.loads(out.getvalue())
        self.assertEqual(status["status"], "MISSING")

        vocab_before = sorted(path.relative_to(vocab).as_posix() for path in vocab.rglob("*"))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = main(["coder-procedure-preview", "--repository", str(vocab), "--format", "json"])
        self.assertEqual(result, 0)
        vocab_preview = json.loads(out.getvalue())
        self.assertEqual(vocab_preview["repository"]["key"], "vocab-app")
        self.assertEqual(vocab_preview["repository"]["repository_id"], make_repository_id(vocab))
        self.assertEqual(vocab_preview["status"], "MISSING")
        self.assertTrue(vocab_preview["overlay_untouched"])
        self.assertEqual(
            vocab_before,
            sorted(path.relative_to(vocab).as_posix() for path in vocab.rglob("*")),
        )
        vocab_plan = preview_reconciliation(vocab_entry, self.bundle)
        with self.assertRaisesRegex(ProcedureGovernanceError, "different managed repository"):
            materialize_preview(self.entry, vocab_plan)
        self.assertEqual(
            vocab_before,
            sorted(path.relative_to(vocab).as_posix() for path in vocab.rglob("*")),
        )


if __name__ == "__main__":
    unittest.main()
