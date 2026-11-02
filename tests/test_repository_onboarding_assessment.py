from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from repoctl.procedure_governance import (
    inspect_procedure,
    load_bundle,
    preview_reconciliation,
)
from repoctl.scanner.util import make_repository_id
from repoctl.web.architect_contract import (
    ArchitectContractError,
    _discover_historical_candidates,
    _document_inventory,
)
from repoctl.web.repository_registry import (
    DEFAULT_DOCUMENT_PATHS,
    RepositoryEntry,
    RepositoryRegistry,
    RepositoryRegistryError,
)
from repoctl.workflow.milestones import discover_milestones


def _write_registry(
    registry_path: Path,
    root: Path,
    documents: dict[str, str],
    *,
    key: str = "fixture",
) -> None:
    lines = [
        f"default = {json.dumps(key)}",
        "",
        f"[repositories.{key}]",
        f"name = {json.dumps(key.title())}",
        f"path = {json.dumps(str(root))}",
        'milestone_root = "docs/milestones"',
        "",
        f"[repositories.{key}.documents]",
    ]
    lines.extend(f"{role} = {json.dumps(path)}" for role, path in sorted(documents.items()))
    registry_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _registered_entry(registry_path: Path, root: Path) -> RepositoryEntry:
    with patch(
        "repoctl.web.repository_registry.validate_git_worktree",
        return_value=root.resolve(),
    ):
        registry = RepositoryRegistry.from_toml(registry_path)
    return registry.active()


def _write_document(root: Path, relative: str, content: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _tree_snapshot(root: Path) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for path in sorted((root, *root.rglob("*"))):
        relative = "." if path == root else path.relative_to(root).as_posix()
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            value = f"symlink:{os.readlink(path)}"
        elif stat.S_ISREG(info.st_mode):
            value = hashlib.sha256(path.read_bytes()).hexdigest()
        elif stat.S_ISDIR(info.st_mode):
            value = "directory"
        else:
            value = f"other:{info.st_mode}"
        snapshot[relative] = value
    return snapshot


def _bundle_snapshot(bundle) -> dict[str, str]:
    sources = {
        source: hashlib.sha256((bundle.root / source).read_bytes()).hexdigest()
        for source in sorted(bundle.source_hashes)
    }
    sources["manifest.toml"] = hashlib.sha256(
        (bundle.root / "manifest.toml").read_bytes()
    ).hexdigest()
    return sources


def _local_link_findings(root: Path) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    link_pattern = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
    for document in sorted((root / "docs").rglob("*.md")):
        if document.is_symlink() or not document.is_file():
            continue
        text = document.read_text(encoding="utf-8")
        for target in link_pattern.findall(text):
            target = target.strip().split("#", 1)[0].split("?", 1)[0]
            if not target or target.startswith(("http://", "https://", "mailto:")):
                continue
            resolved = (document.parent / target).resolve(strict=False)
            if not resolved.is_relative_to(root.resolve()) or not resolved.exists():
                findings.append(
                    {
                        "source": document.relative_to(root).as_posix(),
                        "target": target,
                    }
                )
    return findings


def _install_fixture_procedure(entry: RepositoryEntry, bundle) -> None:
    with patch(
        "repoctl.workflow.coder_interface.resolve_repository_entry",
        return_value=entry,
    ):
        plan = preview_reconciliation(entry, bundle)
    if not plan.can_materialize:
        raise AssertionError(f"Fixture procedure plan is blocked: {plan.blockers}")
    for relative, content in plan.files:
        target = entry.path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    lock = entry.path / bundle.lock_path
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_bytes(plan.lock_bytes)


def _read_only_assessment(
    entry: RepositoryEntry,
    bundle,
    *,
    selected_milestone_id: str,
    git_state_fixture: dict[str, object] | None,
) -> dict[str, object]:
    before = _tree_snapshot(entry.path)
    bundle_before = _bundle_snapshot(bundle)
    documents = _document_inventory(entry)
    milestones = discover_milestones(entry.path, entry.milestone_root)
    try:
        history_candidates, history_limited, history_skipped = _discover_historical_candidates(
            entry,
            selected_milestone_id,
        )
        history_error = None
    except ArchitectContractError as exc:
        history_candidates = []
        history_limited = False
        history_skipped = 0
        history_error = {"code": exc.code, "message": str(exc)}

    migration_plan = []
    for role, source in sorted(entry.documents.items()):
        target = DEFAULT_DOCUMENT_PATHS[role]
        if source == target:
            continue
        source_path = entry.path / source
        target_path = entry.path / target
        migration_plan.append(
            {
                "role": role,
                "source": source,
                "target": target,
                "source_sha256": (
                    hashlib.sha256(source_path.read_bytes()).hexdigest()
                    if source_path.is_file() and not source_path.is_symlink()
                    else None
                ),
                "target_exists": target_path.exists() or target_path.is_symlink(),
                "write_authorized": False,
                "semantic_review_required": True,
            }
        )
    legacy_root = entry.path / "docs" / "legacy_milestones"
    legacy_outside_history = (
        sorted(
            path.relative_to(entry.path).as_posix()
            for path in legacy_root.rglob("*")
            if path.is_file() and not path.is_symlink()
        )
        if legacy_root.is_dir() and not legacy_root.is_symlink()
        else []
    )
    archive_plan = [
        {
            "source": source,
            "target": f"docs/history/milestones/{Path(source).name}",
            "identifier_preserved_in_path": True,
            "write_authorized": False,
            "target_exists": (
                entry.path / "docs/history/milestones" / Path(source).name
            ).exists(),
        }
        for source in legacy_outside_history
    ]

    with patch(
        "repoctl.workflow.coder_interface.resolve_repository_entry",
        return_value=entry,
    ):
        procedure_status = inspect_procedure(entry, bundle)
        procedure_preview = preview_reconciliation(entry, bundle)

    blockers: list[str] = []
    for document in documents:
        if document["status"] != "present":
            blockers.append(f"document:{document['role']}:{document['status']}")
    for issue in milestones["issues"]:
        blockers.append(f"milestone:{issue}")
    active_ids = {
        item["milestone_id"]
        for item in milestones["candidates"]
        if item.get("milestone_id")
    }
    archive_ids = {
        item["milestone_id"]
        for item in history_candidates
        if item.get("category") == "archive" and item.get("milestone_id")
    }
    inconsistent_history_ids = sorted(active_ids & archive_ids)
    blockers.extend(
        f"history_classification:duplicate_current_and_historical_id:{milestone_id}"
        for milestone_id in inconsistent_history_ids
    )
    blockers.extend(
        f"history_outside_supported_root:{path}" for path in legacy_outside_history
    )
    if history_error:
        blockers.append(f"history:{history_error['code']}")
    for finding in _local_link_findings(entry.path):
        blockers.append(f"broken_link:{finding['source']}:{finding['target']}")
    for item in migration_plan:
        if item["target_exists"]:
            blockers.append(f"target_collision:{item['target']}")
    if procedure_preview.blockers:
        blockers.extend(f"procedure_target:{item}" for item in procedure_preview.blockers)
    if git_state_fixture is None:
        git_state = {
            "classification": "not_observed",
            "provenance": "Git inspection was not authorized for this correction.",
        }
        blockers.append("git_state:not_observed")
    else:
        git_state = dict(git_state_fixture)
        if git_state.get("classification") != "clean":
            blockers.append("git_state:reported_dirty_or_unknown")

    after = _tree_snapshot(entry.path)
    bundle_after = _bundle_snapshot(bundle)
    return {
        "repository": {
            "key": entry.key,
            "path": str(entry.path),
            "repository_id": entry.repository_id,
        },
        "document_inventory": documents,
        "milestone_discovery": milestones,
        "historical_candidates": history_candidates,
        "history_limited": history_limited,
        "history_skipped": history_skipped,
        "history_error": history_error,
        "migration_plan": migration_plan,
        "legacy_outside_history": legacy_outside_history,
        "archive_plan": archive_plan,
        "inconsistent_history_ids": inconsistent_history_ids,
        "procedure_status": procedure_status,
        "procedure_target_paths": sorted(artifact.target for artifact in bundle.artifacts),
        "procedure_lock_path": bundle.lock_path,
        "procedure_preview_blockers": list(procedure_preview.blockers),
        "local_link_findings": _local_link_findings(entry.path),
        "git_state": git_state,
        "rollback_plan": {
            "assessment_writes": False,
            "restore_action": "none; source bytes and fixture tree remain unchanged",
            "future_migration": "requires separate PO authorization and exact source/target review",
        },
        "blockers": blockers,
        "fail_closed": bool(blockers),
        "filesystem_unchanged": before == after,
        "canonical_bundle_unchanged": bundle_before == bundle_after,
    }


class RepositoryOnboardingAssessmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.bundle = load_bundle()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _canonical_documents(self, root: Path) -> None:
        for role, path in DEFAULT_DOCUMENT_PATHS.items():
            content = f"# {role}\n\nFixture-specific {role} rules.\n"
            if role == "coding_agent_rules":
                content = (
                    f"<!-- {self.bundle.overlay_marker} -->\n"
                    "## Repository-specific rules\n\nFixture overlay.\n"
                )
            _write_document(root, path, content)

    def _entry(self, name: str, root: Path, documents: dict[str, str]) -> RepositoryEntry:
        root.mkdir(parents=True, exist_ok=True)
        registry_path = root / ".fixture-repositories.toml"
        _write_registry(registry_path, root, documents, key=name)
        return _registered_entry(registry_path, root)

    def test_fixture_a_canonical_registration_and_procedure_are_read_only(self) -> None:
        root = self.root / "fixture-a"
        self._canonical_documents(root)
        _write_document(
            root,
            "docs/milestones/M001.001_onboarding_prompt.md",
            "# Current onboarding prompt\n",
        )
        entry = self._entry("fixture-a", root, dict(DEFAULT_DOCUMENT_PATHS))
        _install_fixture_procedure(entry, self.bundle)

        report = _read_only_assessment(
            entry,
            self.bundle,
            selected_milestone_id="M001.001",
            git_state_fixture={
                "classification": "clean",
                "provenance": "synthetic fixture metadata; not read from Git",
            },
        )

        self.assertEqual(
            {item["role"] for item in report["document_inventory"]},
            set(DEFAULT_DOCUMENT_PATHS),
        )
        self.assertTrue(all(item["status"] == "present" for item in report["document_inventory"]))
        self.assertEqual(
            [item["milestone_id"] for item in report["milestone_discovery"]["candidates"]],
            ["M001.001"],
        )
        self.assertEqual(report["procedure_status"]["status"], "CURRENT")
        self.assertEqual(report["procedure_status"]["current_release"], "1.4.0")
        self.assertTrue(report["procedure_status"]["lock_sha256"])
        self.assertTrue(report["procedure_status"]["overlay"]["valid"])
        self.assertEqual(
            set(report["procedure_target_paths"]),
            {artifact.target for artifact in self.bundle.artifacts},
        )
        self.assertEqual(report["procedure_lock_path"], self.bundle.lock_path)
        self.assertFalse(report["procedure_preview_blockers"])
        self.assertFalse(report["local_link_findings"])
        self.assertFalse(report["blockers"])
        self.assertFalse(report["fail_closed"])
        self.assertTrue(report["filesystem_unchanged"])
        self.assertTrue(report["canonical_bundle_unchanged"])
        self.assertFalse(report["rollback_plan"]["assessment_writes"])

    def test_fixture_b_preserves_v8_sources_and_legacy_history_in_a_reviewable_plan(self) -> None:
        root = self.root / "fixture-b"
        legacy_documents = {
            role: path.removesuffix(".md") + "_v8.md"
            for role, path in DEFAULT_DOCUMENT_PATHS.items()
        }
        for role, path in legacy_documents.items():
            content = (
                f"# {role}\n\n"
                f"UNIQUE_INVARIANT_{role.upper()}\n\n"
                f"[Related context](project_context_v8.md)\n"
            )
            if role == "coding_agent_rules":
                content = (
                    f"<!-- {self.bundle.overlay_marker} -->\n"
                    "## Repository-specific rules\n\n"
                    "PRESERVE_PROJECT_RULES.\n"
                )
            _write_document(root, path, content)
        _write_document(
            root,
            "docs/milestones/M001.001_onboarding_prompt.md",
            "# Current Repo Control milestone\n",
        )
        _write_document(
            root,
            "docs/history/milestones/12.64_deployment_prompt.md",
            "# Legacy 12.64 deployment evidence\n",
        )
        _write_document(
            root,
            "docs/history/milestones/12.65_housekeeping_closeout.md",
            "# Legacy 12.65 housekeeping evidence\n",
        )
        _write_document(
            root,
            "docs/legacy_milestones/12.66_application_prompt.md",
            "# Legacy 12.66 application evidence outside the supported history roots\n",
        )
        entry = self._entry("fixture-b", root, legacy_documents)
        _install_fixture_procedure(entry, self.bundle)

        report = _read_only_assessment(
            entry,
            self.bundle,
            selected_milestone_id="M001.001",
            git_state_fixture={
                "classification": "clean",
                "provenance": "synthetic fixture metadata; not read from Git",
            },
        )

        self.assertEqual(len(report["migration_plan"]), 5)
        self.assertEqual(
            {(item["source"], item["target"]) for item in report["migration_plan"]},
            {
                (source, DEFAULT_DOCUMENT_PATHS[role])
                for role, source in legacy_documents.items()
            },
        )
        self.assertTrue(all(item["source_sha256"] for item in report["migration_plan"]))
        self.assertTrue(all(not item["target_exists"] for item in report["migration_plan"]))
        self.assertTrue(all(not item["write_authorized"] for item in report["migration_plan"]))
        self.assertTrue(all(item["semantic_review_required"] for item in report["migration_plan"]))
        history_paths = {item["path"] for item in report["historical_candidates"]}
        self.assertIn("docs/history/milestones/12.64_deployment_prompt.md", history_paths)
        self.assertIn("docs/history/milestones/12.65_housekeeping_closeout.md", history_paths)
        self.assertNotIn(
            "docs/legacy_milestones/12.66_application_prompt.md",
            history_paths,
        )
        self.assertIn(
            "docs/legacy_milestones/12.66_application_prompt.md",
            report["legacy_outside_history"],
        )
        self.assertTrue(all("12.6" in path for path in history_paths))
        self.assertTrue(
            all(
                item["milestone_id"] is None
                for item in report["historical_candidates"]
                if "12.6" in item["path"]
            )
        )
        self.assertEqual(
            report["archive_plan"],
            [
                {
                    "source": "docs/legacy_milestones/12.66_application_prompt.md",
                    "target": "docs/history/milestones/12.66_application_prompt.md",
                    "identifier_preserved_in_path": True,
                    "write_authorized": False,
                    "target_exists": False,
                }
            ],
        )
        self.assertFalse(report["milestone_discovery"]["issues"])
        self.assertFalse(report["local_link_findings"])
        self.assertEqual(
            report["blockers"],
            ["history_outside_supported_root:docs/legacy_milestones/12.66_application_prompt.md"],
        )
        self.assertTrue(report["fail_closed"])
        self.assertTrue(report["filesystem_unchanged"])
        self.assertTrue(report["canonical_bundle_unchanged"])
        self.assertFalse(report["rollback_plan"]["assessment_writes"])

    def test_fixture_c_reports_exact_conflicts_and_fails_closed_without_changes(self) -> None:
        root = self.root / "fixture-c"
        documents = dict(DEFAULT_DOCUMENT_PATHS)
        documents["context"] = "docs/context/project_context_v8.md"
        self._canonical_documents(root)
        _write_document(
            root,
            documents["context"],
            "# Legacy context source\n\nPreserve original bytes.\n",
        )
        _write_document(
            root,
            DEFAULT_DOCUMENT_PATHS["context"],
            "# User-owned canonical target collision\n",
        )
        (root / DEFAULT_DOCUMENT_PATHS["parking_lot"]).unlink()
        _write_document(
            root,
            DEFAULT_DOCUMENT_PATHS["architecture"],
            "# Architecture\n\n[Missing reference](./does-not-exist.md)\n",
        )
        _write_document(
            root,
            "docs/milestones/arc_a/M001.001_first_prompt.md",
            "# First current prompt\n",
        )
        _write_document(
            root,
            "docs/milestones/arc_b/M001.001_duplicate_prompt.md",
            "# Conflicting current prompt\n",
        )
        _write_document(
            root,
            "docs/milestones/M001.002_current_prompt.md",
            "# Current milestone with archived duplicate\n",
        )
        _write_document(
            root,
            "docs/history/milestones/M001.002_old_prompt.md",
            "# Same ID classified as historical evidence\n",
        )
        entry = self._entry("fixture-c", root, documents)

        outside = self.root / "outside-history"
        outside.mkdir()
        (outside / "evidence.md").write_text("outside\n", encoding="utf-8")
        agents = root / "AGENTS.md"
        user_agents = b"User-owned Codex instructions\n"
        agents.write_bytes(user_agents)
        before = _tree_snapshot(root)
        outside_before = (outside / "evidence.md").read_bytes()

        report = _read_only_assessment(
            entry,
            self.bundle,
            selected_milestone_id="M001.002",
            git_state_fixture={
                "classification": "unstaged_only",
                "paths": ["docs/context/project_context.md"],
                "provenance": "synthetic fixture evidence; not read from Git",
            },
        )

        self.assertTrue(report["fail_closed"])
        self.assertTrue(report["filesystem_unchanged"])
        self.assertTrue(report["canonical_bundle_unchanged"])
        self.assertFalse(report["rollback_plan"]["assessment_writes"])
        self.assertEqual(before, _tree_snapshot(root))
        self.assertEqual((outside / "evidence.md").read_bytes(), outside_before)
        self.assertEqual(agents.read_bytes(), user_agents)
        context_plan = next(
            item for item in report["migration_plan"] if item["role"] == "context"
        )
        self.assertEqual(context_plan["source"], "docs/context/project_context_v8.md")
        self.assertEqual(context_plan["target"], "docs/context/project_context.md")
        self.assertTrue(context_plan["target_exists"])
        self.assertFalse(context_plan["write_authorized"])
        self.assertTrue(
            any(blocker == "document:parking_lot:missing" for blocker in report["blockers"])
        )
        self.assertTrue(
            any(blocker.startswith("target_collision:docs/context/project_context.md") for blocker in report["blockers"])
        )
        self.assertTrue(any("broken_link:" in blocker for blocker in report["blockers"]))
        self.assertTrue(any("Ambiguous milestone ID M001.001" in blocker for blocker in report["blockers"]))
        self.assertIsNone(report["history_error"])
        self.assertEqual(report["inconsistent_history_ids"], ["M001.002"])
        self.assertIn(
            "history_classification:duplicate_current_and_historical_id:M001.002",
            report["blockers"],
        )
        self.assertTrue(
            any("AGENTS.md" in blocker for blocker in report["procedure_preview_blockers"])
        )
        self.assertIn("git_state:reported_dirty_or_unknown", report["blockers"])

        unsafe_root = self.root / "unsafe-registry"
        unsafe_root.mkdir()
        unsafe_outside = self.root / "unsafe-docs"
        unsafe_outside.mkdir()
        (unsafe_outside / "project_context.md").write_text("outside\n", encoding="utf-8")
        (unsafe_root / "docs").mkdir()
        (unsafe_root / "docs" / "context").symlink_to(unsafe_outside, target_is_directory=True)
        unsafe_documents = dict(DEFAULT_DOCUMENT_PATHS)
        unsafe_registry = unsafe_root / ".fixture-repositories.toml"
        _write_registry(unsafe_registry, unsafe_root, unsafe_documents, key="unsafe")
        with patch(
            "repoctl.web.repository_registry.validate_git_worktree",
            return_value=unsafe_root.resolve(),
        ):
            with self.assertRaisesRegex(RepositoryRegistryError, "symlinked path components"):
                RepositoryRegistry.from_toml(unsafe_registry)

        traversal_documents = dict(DEFAULT_DOCUMENT_PATHS)
        traversal_documents["context"] = "../outside.md"
        traversal_registry = unsafe_root / ".traversal-repositories.toml"
        _write_registry(traversal_registry, unsafe_root, traversal_documents, key="traversal")
        with patch(
            "repoctl.web.repository_registry.validate_git_worktree",
            return_value=unsafe_root.resolve(),
        ):
            with self.assertRaisesRegex(RepositoryRegistryError, "safe repository-relative path"):
                RepositoryRegistry.from_toml(traversal_registry)

        unsafe_history_root = self.root / "unsafe-history"
        (unsafe_history_root / "docs").mkdir(parents=True)
        (unsafe_history_root / "docs" / "history").symlink_to(outside, target_is_directory=True)
        unsafe_history_entry = RepositoryEntry(
            key="unsafe-history",
            name="Unsafe history fixture",
            path=unsafe_history_root,
            repository_id=make_repository_id(unsafe_history_root),
            milestone_root="docs/milestones",
            documents=dict(DEFAULT_DOCUMENT_PATHS),
        )
        with self.assertRaises(ArchitectContractError) as error:
            _discover_historical_candidates(unsafe_history_entry, None)
        self.assertEqual(error.exception.code, "unsafe_historical_root")


if __name__ == "__main__":
    unittest.main()
