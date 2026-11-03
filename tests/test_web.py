from __future__ import annotations

import subprocess
import multiprocessing
import tempfile
import unittest
import re
import os
import json
import shutil
from pathlib import Path
from unittest import mock

from repoctl.analysis.manager import AnalysisError, analyze_comparison
from repoctl.analysis.provider import ModelIdentity
from repoctl.compare.manager import compare_snapshots
from repoctl.procedure_governance import (
    BUNDLE_ROOT,
    load_bundle,
    materialize_preview,
    preview_reconciliation,
)
from repoctl import procedure_transactions
from repoctl.procedure_transactions import (
    apply_confirmed,
    inspect_transaction,
    procedure_plan_digest,
    review_apply,
)
from repoctl.scanner.core import run_scan_with_artifacts
from repoctl.scanner.util import make_repository_id
from repoctl.snapshot.manager import create_snapshot
from repoctl.web.app import create_web_app
from repoctl.web.repository_registry import RepositoryEntry, RepositoryRegistry
from repoctl.workflow.architect_dispatches import submit_dispatch
from repoctl.workflow.coder_interface import submit_coder_context_verification
from repoctl.workflow.commit_execution import execute_prepared_commit
from repoctl.workflow.commit_plan import prepare_commit
from repoctl.workflow.diff import GitReviewError
from repoctl.workflow.stage_execution import execute_prepared_stage
from repoctl.workflow.stage_plan import prepare_stage
from repoctl.workflow.lifecycle import load_milestone_workflow, record_milestone_event
from repoctl.workflow.milestones import discover_milestones, save_milestone_selection
from tests.procedure_fixtures import copy_five_artifact_v130_bundle


def _git(repo: Path, *args: str) -> bytes:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", errors="replace"))
    return proc.stdout


def _git_text(repo: Path, *args: str) -> str:
    return _git(repo, *args).decode("utf-8", errors="strict").strip()


def _git_status_porcelain(repo: Path) -> bytes:
    return _git(repo, "status", "--porcelain=v2", "-z", "--untracked-files=all")


def _init_repo(root: Path, name: str = "web-repo") -> Path:
    repo = root / name
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")

    (repo / "app.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    (repo / "module_a.py").write_text("from module_b import helper\n\n\ndef run():\n    return helper()\n", encoding="utf-8")
    (repo / "module_b.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    (repo / "test_module_a.py").write_text("from module_b import helper\n\n\ndef test_run():\n    helper()\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")
    return repo


def _extract_csrf_token(client) -> str:
    with client.session_transaction() as sess:
        return sess["csrf_token"]


def _crash_fixture_apply(entry: RepositoryEntry, state_root: str, token: str) -> None:
    original = procedure_transactions._write_target
    calls = 0

    def write_then_exit(*args, **kwargs):
        nonlocal calls
        original(*args, **kwargs)
        calls += 1
        if calls == 1:
            os._exit(77)

    with mock.patch("repoctl.procedure_transactions._write_target", side_effect=write_then_exit):
        apply_confirmed(
            entry,
            token,
            state_root,
            active_key=entry.key,
            registry_generation=0,
        )


class _FakeProvider:
    def __init__(self, response_payload: dict) -> None:
        self._response_payload = response_payload
        self._identity = ModelIdentity(provider="ollama", model_name="gpt-oss:20b", model_digest="a" * 64)

    def resolve_model_identity(self) -> ModelIdentity:
        return self._identity

    def generate_analysis(self, **kwargs):
        return self._response_payload


class WebTests(unittest.TestCase):
    def test_init_rejects_non_git_repository(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with self.assertRaises(Exception):
                create_web_app(repository_path=str(root), state_root=root / "state")

    def test_loopback_policy_and_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            app = create_web_app(repository_path=str(repo), state_root=root / "state")
            self.assertEqual(app.config["BOUND_HOST"], "127.0.0.1")
            self.assertEqual(app.config["BOUND_PORT"], 8765)

            with self.assertRaises(ValueError):
                create_web_app(repository_path=str(repo), host="0.0.0.0", state_root=root / "state")

    def test_registry_selection_is_server_controlled_and_invalidates_prepared_actions(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            first = _init_repo(root, "first-repo")
            second = _init_repo(root, "second-repo")
            (first / "app.py").write_text("def run():\n    return 2\n", encoding="utf-8")
            _git(first, "add", "app.py")
            registry_path = root / "repositories.toml"
            registry_path.write_text(
                "\n".join(
                    [
                        'default = "first"',
                        "",
                        "[repositories.first]",
                        'name = "First Repository"',
                        f"path = {json.dumps(str(first))}",
                        "",
                        "[repositories.second]",
                        'name = "Second Repository"',
                        f"path = {json.dumps(str(second))}",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            app = create_web_app(
                repository_registry_path=registry_path,
                state_root=root / "state",
            )
            client = app.test_client()
            landing = client.get("/")
            self.assertIn("First Repository", landing.get_data(as_text=True))
            self.assertIn("Second Repository", landing.get_data(as_text=True))
            self.assertIn(str(first), landing.get_data(as_text=True))

            csrf = _extract_csrf_token(client)
            prepared = client.post(
                "/git-review/action/prepare",
                data={
                    "csrf_token": csrf,
                    "action": "commit",
                    "commit_message": "Prepared in first repository",
                },
            )
            self.assertEqual(prepared.status_code, 200)
            token_match = re.search(r'name="token" value="([^"]+)"', prepared.get_data(as_text=True))
            self.assertIsNotNone(token_match)
            prepared_token = token_match.group(1)

            first_head = _git_text(first, "rev-parse", "HEAD")
            first_status = _git_status_porcelain(first)
            second_head = _git_text(second, "rev-parse", "HEAD")
            second_status = _git_status_porcelain(second)

            switched = client.post(
                "/repositories/select",
                data={"csrf_token": csrf, "repository_key": "second"},
            )
            self.assertEqual(switched.status_code, 302)
            second_page = client.get("/")
            self.assertIn("Active Repository", second_page.get_data(as_text=True))
            self.assertIn("Second Repository", second_page.get_data(as_text=True))
            health = client.get("/healthz").get_json()
            self.assertEqual(health["service"], "repo-control")
            self.assertEqual(health["active_repository"], "second")
            self.assertIsInstance(health["active_repository_generation"], int)
            self.assertFalse(health["central_registry_stale"])
            self.assertEqual(health["runtime_capabilities"]["capability_schema_version"], 1)
            self.assertTrue(health["runtime_capabilities"]["instance_id"])
            self.assertIn(
                "context_verification_recorded",
                health["runtime_capabilities"]["workflow"]["supported_event_types"],
            )
            self.assertEqual(first_head, _git_text(first, "rev-parse", "HEAD"))
            self.assertEqual(first_status, _git_status_porcelain(first))
            self.assertEqual(second_head, _git_text(second, "rev-parse", "HEAD"))
            self.assertEqual(second_status, _git_status_porcelain(second))

            switched_back = client.post(
                "/repositories/select",
                data={"csrf_token": csrf, "repository_key": "first"},
            )
            self.assertEqual(switched_back.status_code, 302)
            stale_confirmation = client.post(
                "/git-review/action/confirm",
                data={
                    "csrf_token": csrf,
                    "action": "commit",
                    "token": prepared_token,
                },
            )
            self.assertEqual(stale_confirmation.status_code, 409)
            self.assertEqual(first_head, _git_text(first, "rev-parse", "HEAD"))
            self.assertEqual(first_status, _git_status_porcelain(first))

            rejected = client.post(
                "/repositories/select",
                data={"csrf_token": csrf, "repository_key": str(second)},
            )
            self.assertEqual(rejected.status_code, 400)
            self.assertEqual(first_head, _git_text(first, "rev-parse", "HEAD"))
            self.assertEqual(first_status, _git_status_porcelain(first))

    def test_procedure_updates_are_allowlisted_read_only_and_do_not_switch_selection(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repos = {
                "first": _init_repo(root, "first-repo"),
                "second": _init_repo(root, "second-repo"),
                "hash_mismatch": _init_repo(root, "hash-mismatch-repo"),
                "overlay_invalid": _init_repo(root, "overlay-invalid-repo"),
                "incompatible": _init_repo(root, "incompatible-repo"),
                "missing": _init_repo(root, "missing-repo"),
            }
            for repo in repos.values():
                overlay = repo / "docs/context/coding_agent_rules.md"
                overlay.parent.mkdir(parents=True)
                overlay.write_text(
                    "# Project overlay\n\n"
                    "<!-- repo-control-project-overlay:v1 -->\n\n"
                    "## Repository-specific rules\n\n- Keep fixture data local.\n",
                    encoding="utf-8",
                )
                (repo / "docs/milestones").mkdir(parents=True)

            registry_path = root / "repositories.toml"
            registry_lines = ['default = "first"']
            for key, repo in repos.items():
                registry_lines.extend(
                    [
                        "",
                        f"[repositories.{key}]",
                        f"name = {json.dumps(key.replace('_', ' ').title() + ' Repository')}",
                        f"path = {json.dumps(str(repo))}",
                        'milestone_root = "docs/milestones"',
                        "",
                        f"[repositories.{key}.documents]",
                        'coding_agent_rules = "docs/context/coding_agent_rules.md"',
                    ]
                )
            registry_path.write_text("\n".join(registry_lines) + "\n", encoding="utf-8")
            registry = RepositoryRegistry.from_toml(registry_path)
            bundle = load_bundle()
            entries = {entry.key: entry for entry in registry.entries}
            with mock.patch(
                "repoctl.workflow.coder_interface.CENTRAL_REPOSITORY_REGISTRY_PATH",
                registry_path,
            ):
                for key in ("first", "second", "hash_mismatch", "overlay_invalid", "incompatible"):
                    plan = preview_reconciliation(entries[key], bundle)
                    materialize_preview(entries[key], plan, bundle)
            second = repos["second"]
            lock_path = second / bundle.lock_path
            lock_path.write_text(
                lock_path.read_text(encoding="utf-8").replace(
                    f'release = "{bundle.release}"',
                    'release = "1.0.2"',
                ),
                encoding="utf-8",
            )
            (repos["hash_mismatch"] / ".github/copilot-instructions.md").write_text(
                "unmanaged fixture edit\n",
                encoding="utf-8",
            )
            (repos["overlay_invalid"] / "docs/context/coding_agent_rules.md").write_text(
                "# Invalid overlay\n",
                encoding="utf-8",
            )
            (repos["incompatible"] / bundle.lock_path).write_text("invalid TOML =", encoding="utf-8")

            with mock.patch(
                "repoctl.workflow.coder_interface.CENTRAL_REPOSITORY_REGISTRY_PATH",
                registry_path,
            ):
                app = create_web_app(
                    repository_registry_path=registry_path,
                    state_root=root / "state",
                )
                client = app.test_client()
                second_plan = preview_reconciliation(entries["second"], bundle)
                second_generated = {
                    change["path"]: (second / change["path"]).read_bytes()
                    for change in second_plan.as_dict()["changes"]
                }
                second_files_before = {
                    path: (second / path).read_bytes()
                    for path in (
                        bundle.lock_path,
                        "docs/context/coding_agent_rules.md",
                    )
                }
                second_git_before = _git_status_porcelain(second)

                response = client.get("/procedure-updates?repository=second")
                html = response.get_data(as_text=True)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                guide_response = client.get("/guides/repo-control-user-guide")
                self.assertEqual(guide_response.status_code, 200)
                guide_response.close()
                self.assertIn("Second Repository", html)
                self.assertIn("CURRENT", html)
                self.assertIn("STALE_VERSION", html)
                self.assertIn("MISSING", html)
                self.assertIn("HASH_MISMATCH", html)
                self.assertIn("OVERLAY_INVALID", html)
                self.assertIn("INCOMPATIBLE", html)
                self.assertIn("UPDATE", html)
                self.assertIn(bundle.manifest_sha256, html)
                self.assertIn("1.0.2", html)
                self.assertIn("1.4.0", html)
                self.assertIn("coding_agent_rules.md", html)
                self.assertIn("Review Apply", html)
                self.assertNotIn("Confirm Apply", html)
                self.assertEqual(
                    client.get("/healthz").get_json()["active_repository"],
                    "first",
                )
                self.assertEqual(second_git_before, _git_status_porcelain(second))
                self.assertEqual(
                    second_files_before,
                    {path: (second / path).read_bytes() for path in second_files_before},
                )
                self.assertEqual(
                    second_generated,
                    {path: (second / path).read_bytes() for path in second_generated},
                )

                with (
                    mock.patch(
                        "repoctl.workflow.coder_interface.resolve_coder_release_preparation_context",
                        return_value=(entries["first"], {}, {}),
                    ),
                    mock.patch(
                        "repoctl.procedure_release.inspect_procedure_release_preparation",
                        return_value={
                            "status": "RELEASE_IN_PREPARATION",
                            "authorized": True,
                            "candidate_release": "1.4.0",
                            "milestone_id": "M009.010",
                            "authorized_sources": ["workflow_core.md"],
                        },
                    ),
                ):
                    blocked_page = client.get("/procedure-updates?repository=second")
                    blocked_html = blocked_page.get_data(as_text=True)
                    self.assertIn("Apply is disabled; safe transaction recovery remains available.", blocked_html)
                    self.assertNotIn("Review Apply", blocked_html)
                    self.assertNotIn("Review Recovery", blocked_html)
                    csrf = _extract_csrf_token(client)
                    self.assertEqual(
                        client.post(
                            "/procedure-updates/apply-review",
                            data={
                                "csrf_token": csrf,
                                "repository_key": "second",
                                "active_repository_key": "first",
                                "repository_generation": "0",
                                "plan_sha256": "a" * 64,
                            },
                        ).status_code,
                        409,
                    )
                    self.assertEqual(
                        client.post(
                            "/procedure-updates/recovery-review",
                            data={
                                "csrf_token": csrf,
                                "repository_key": "second",
                                "active_repository_key": "first",
                                "repository_generation": "0",
                                "operation_id": "0" * 32,
                            },
                        ).status_code,
                        302,
                    )

                from repoctl.procedure_governance import ProcedureGovernanceError

                with mock.patch(
                    "repoctl.procedure_governance.load_bundle",
                    side_effect=ProcedureGovernanceError(
                        "Canonical source hash mismatch: workflow_core.md"
                    ),
                ):
                    unavailable = client.get("/procedure-updates?repository=second")
                    unavailable_html = unavailable.get_data(as_text=True)
                    self.assertEqual(unavailable.status_code, 200)
                    self.assertIn("Canonical bundle integrity unavailable", unavailable_html)
                    self.assertIn("Second Repository", unavailable_html)
                    self.assertIn("Comparison unavailable", unavailable_html)
                    self.assertNotIn("No repositories are configured for this service.", unavailable_html)
                    csrf = _extract_csrf_token(client)
                    self.assertEqual(
                        client.post(
                            "/procedure-updates/apply-review",
                            data={
                                "csrf_token": csrf,
                                "repository_key": "second",
                                "active_repository_key": "first",
                                "repository_generation": "0",
                                "plan_sha256": "a" * 64,
                            },
                        ).status_code,
                        409,
                    )
                    self.assertEqual(
                        client.post(
                            "/procedure-updates/recovery-review",
                            data={
                                "csrf_token": csrf,
                                "repository_key": "second",
                                "active_repository_key": "first",
                                "repository_generation": "0",
                                "operation_id": "0" * 32,
                            },
                        ).status_code,
                        302,
                    )

                invalid = client.get("/procedure-updates?repository=/tmp/arbitrary")
                self.assertEqual(invalid.status_code, 400)
                self.assertEqual(
                    client.post("/procedure-updates?repository=second").status_code,
                    405,
                )

    def test_procedure_apply_requires_two_post_confirmations_and_keeps_git_separate(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root, "apply-repo")
            state_root = root / "state"
            (repo / "docs/milestones").mkdir(parents=True)
            overlay = repo / "docs/context/coding_agent_rules.md"
            overlay.parent.mkdir(parents=True)
            overlay.write_text(
                "# Project overlay\n\n"
                "<!-- repo-control-project-overlay:v1 -->\n\n"
                "## Repository-specific rules\n\n- Keep fixture data local.\n",
                encoding="utf-8",
            )
            _git(repo, "add", "docs/context/coding_agent_rules.md")
            _git(repo, "commit", "-m", "add project overlay")
            original_overlay = overlay.read_bytes()
            registry_path = root / "repositories.toml"
            registry_path.write_text(
                "\n".join(
                    [
                        'default = "fixture"',
                        "",
                        "[repositories.fixture]",
                        'name = "Fixture Repository"',
                        f'path = "{repo}"',
                        'milestone_root = "docs/milestones"',
                        "",
                        "[repositories.fixture.documents]",
                        'coding_agent_rules = "docs/context/coding_agent_rules.md"',
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            with mock.patch(
                "repoctl.workflow.coder_interface.CENTRAL_REPOSITORY_REGISTRY_PATH",
                registry_path,
            ):
                app = create_web_app(repository_registry_path=registry_path, state_root=state_root)
                app.testing = True
                client = app.test_client()
                initial = client.get("/procedure-updates?repository=fixture")
                self.assertEqual(initial.status_code, 200)
                self.assertIn(b"Review Apply", initial.data)
                self.assertNotIn(b"Confirm Apply", initial.data)
                self.assertFalse((repo / ".github/copilot-instructions.md").exists())

                csrf = _extract_csrf_token(client)
                plan_digest = re.search(rb'name="plan_sha256" value="([0-9a-f]{64})"', initial.data)
                self.assertIsNotNone(plan_digest)
                review = client.post(
                    "/procedure-updates/apply-review",
                    data={
                        "csrf_token": csrf,
                        "repository_key": "fixture",
                        "active_repository_key": "fixture",
                        "repository_generation": "0",
                        "plan_sha256": plan_digest.group(1).decode(),
                    },
                )
                self.assertEqual(review.status_code, 302)
                confirmation_page = client.get("/procedure-updates?repository=fixture")
                self.assertIn(b"Confirm Apply", confirmation_page.data)
                self.assertIn(str(repo).encode(), confirmation_page.data)
                token = re.search(
                    rb'name="confirmation_token" value="([^"]+)"',
                    confirmation_page.data,
                )
                self.assertIsNotNone(token)
                self.assertFalse((repo / ".github/copilot-instructions.md").exists())
                malformed = client.post(
                    "/procedure-updates/apply",
                    data={
                        "csrf_token": csrf,
                        "repository_key": "fixture",
                        "active_repository_key": "fixture",
                        "repository_generation": "0",
                        "confirmation_token": "non-ascii-☃",
                    },
                )
                self.assertEqual(malformed.status_code, 400)
                self.assertFalse((repo / ".github/copilot-instructions.md").exists())

                applied = client.post(
                    "/procedure-updates/apply",
                    data={
                        "csrf_token": csrf,
                        "repository_key": "fixture",
                        "active_repository_key": "fixture",
                        "repository_generation": "0",
                        "confirmation_token": token.group(1).decode(),
                    },
                )
                self.assertEqual(applied.status_code, 302)
                self.assertTrue((repo / ".github/copilot-instructions.md").is_file())
                self.assertEqual(overlay.read_bytes(), original_overlay)
                self.assertEqual(_git(repo, "diff", "--cached", "--name-only"), b"")
                final_page = client.get("/procedure-updates?repository=fixture")
                self.assertIn(b"CURRENT", final_page.data)
                self.assertIn(b"VERIFIED", final_page.data)

                replay = client.post(
                    "/procedure-updates/apply",
                    data={
                        "csrf_token": csrf,
                        "repository_key": "fixture",
                        "active_repository_key": "fixture",
                        "repository_generation": "0",
                        "confirmation_token": token.group(1).decode(),
                    },
                )
                self.assertEqual(replay.status_code, 400)
                self.assertEqual(_git(repo, "diff", "--cached", "--name-only"), b"")

    def test_future_six_artifact_uses_the_po_confirmed_web_apply_path(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root, "future-six-repo")
            state_root = root / "state"
            (repo / "docs/milestones").mkdir(parents=True)
            overlay = repo / "docs/context/coding_agent_rules.md"
            overlay.parent.mkdir(parents=True)
            overlay.write_text(
                "# Project overlay\n\n"
                "<!-- repo-control-project-overlay:v1 -->\n\n"
                "## Repository-specific rules\n\n- Keep fixture data local.\n",
                encoding="utf-8",
            )
            _git(repo, "add", "docs/context/coding_agent_rules.md")
            _git(repo, "commit", "-m", "add project overlay")

            bundle_root = root / "future-procedure"
            copy_five_artifact_v130_bundle(BUNDLE_ROOT, bundle_root)
            manifest_path = bundle_root / "manifest.toml"
            manifest = manifest_path.read_text(encoding="utf-8").replace(
                'release = "1.3.0"',
                'release = "1.4.0"',
                1,
            )
            manifest += (
                '\n[[artifacts]]\nsource = "bootstrap.md"\n'
                'target = "AGENTS.md"\nrenderer = "bootstrap"\n'
            )
            manifest_path.write_text(manifest, encoding="utf-8")
            future_bundle = load_bundle(bundle_root)

            registry_path = root / "repositories.toml"
            registry_path.write_text(
                "\n".join(
                    [
                        'default = "fixture"',
                        "",
                        "[repositories.fixture]",
                        'name = "Fixture Repository"',
                        f"path = {json.dumps(str(repo))}",
                        'milestone_root = "docs/milestones"',
                        "",
                        "[repositories.fixture.documents]",
                        'coding_agent_rules = "docs/context/coding_agent_rules.md"',
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            with (
                mock.patch(
                    "repoctl.workflow.coder_interface.CENTRAL_REPOSITORY_REGISTRY_PATH",
                    registry_path,
                ),
                mock.patch("repoctl.procedure_governance.load_bundle", return_value=future_bundle),
                mock.patch("repoctl.procedure_transactions.load_bundle", return_value=future_bundle),
            ):
                app = create_web_app(repository_registry_path=registry_path, state_root=state_root)
                app.testing = True
                client = app.test_client()
                initial = client.get("/procedure-updates?repository=fixture")
                self.assertEqual(initial.status_code, 200)
                self.assertIn(b"AGENTS.md", initial.data)
                self.assertIn(b"Review Apply", initial.data)
                self.assertFalse((repo / "AGENTS.md").exists())

                csrf = _extract_csrf_token(client)
                plan_digest = re.search(rb'name="plan_sha256" value="([0-9a-f]{64})"', initial.data)
                self.assertIsNotNone(plan_digest)
                review = client.post(
                    "/procedure-updates/apply-review",
                    data={
                        "csrf_token": csrf,
                        "repository_key": "fixture",
                        "active_repository_key": "fixture",
                        "repository_generation": "0",
                        "plan_sha256": plan_digest.group(1).decode(),
                    },
                )
                self.assertEqual(review.status_code, 302)
                confirmation = client.get("/procedure-updates?repository=fixture")
                self.assertIn(b"Confirm Apply", confirmation.data)
                self.assertIn(b"AGENTS.md", confirmation.data)
                self.assertFalse((repo / "AGENTS.md").exists())
                token = re.search(rb'name="confirmation_token" value="([^"]+)"', confirmation.data)
                self.assertIsNotNone(token)

                applied = client.post(
                    "/procedure-updates/apply",
                    data={
                        "csrf_token": csrf,
                        "repository_key": "fixture",
                        "active_repository_key": "fixture",
                        "repository_generation": "0",
                        "confirmation_token": token.group(1).decode(),
                    },
                )

                self.assertEqual(applied.status_code, 302)
                self.assertEqual(
                    (repo / "AGENTS.md").read_bytes(),
                    (repo / ".github/copilot-instructions.md").read_bytes(),
                )
                self.assertEqual(inspect_transaction(
                    RepositoryEntry(
                        key="fixture",
                        name="Fixture Repository",
                        path=repo,
                        repository_id=make_repository_id(repo),
                        milestone_root="docs/milestones",
                        documents={"coding_agent_rules": "docs/context/coding_agent_rules.md"},
                    ),
                    state_root,
                )["state"], "VERIFIED")
                final = client.get("/procedure-updates?repository=fixture")
                self.assertIn(b"CURRENT", final.data)
                self.assertIn(b"VERIFIED", final.data)

    def test_first_time_overlay_initialization_is_a_separate_po_confirmed_operation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root, "first-time-repo")
            state_root = root / "state"
            docs = {
                "context": "docs/context/project_context_v8.md",
                "architecture": "docs/context/project_architecture_v8.md",
                "workflow": "docs/context/project_workflow_v8.md",
                "coding_agent_rules": "docs/context/coding_agent_rules_v8.md",
                "parking_lot": "docs/context/canonical_parking_lot_v8.md",
            }
            for role, relative in docs.items():
                path = repo / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                if role != "coding_agent_rules":
                    path.write_text(f"# Existing {role}\n\nFixture authority.\n", encoding="utf-8")
            original = b"# Existing project rules\n\n" + b"Preserve this rule exactly.\n" * 1500
            overlay = repo / docs["coding_agent_rules"]
            overlay.write_bytes(original)
            milestone_root = repo / "docs/milestones"
            milestone_root.mkdir(parents=True, exist_ok=True)
            (milestone_root / "M001.001_onboarding_prompt.md").write_text("# Onboarding\n", encoding="utf-8")
            registry_path = root / "repositories.toml"
            registry_lines = [
                'default = "fixture"',
                "",
                "[repositories.fixture]",
                'name = "First-Time Repository"',
                f"path = {json.dumps(str(repo))}",
                'milestone_root = "docs/milestones"',
                "",
                "[repositories.fixture.documents]",
            ]
            registry_lines.extend(f"{role} = {json.dumps(path)}" for role, path in docs.items())
            registry_path.write_text("\n".join(registry_lines) + "\n", encoding="utf-8")

            candidate = discover_milestones(repo)["candidates"][0]
            save_milestone_selection(repo, state_root, candidate["milestone_id"])
            record_milestone_event(
                repo,
                state_root,
                "docs/milestones",
                candidate,
                event_type="prompt_approved",
                summary="Fixture onboarding approved",
                actor_role="product_owner",
                po_approved=True,
            )
            record_milestone_event(
                repo,
                state_root,
                "docs/milestones",
                candidate,
                event_type="implementation_started",
                summary="Fixture onboarding started",
                actor_role="coder",
            )
            waiting = record_milestone_event(
                repo,
                state_root,
                "docs/milestones",
                candidate,
                event_type="question_submitted",
                summary="Fixture onboarding blocked",
                actor_role="coder",
                waiting_category="clarification",
                waiting_reason="Awaiting project-owner clarification",
            )
            self.assertEqual(waiting["phase"], "WAITING")

            with mock.patch(
                "repoctl.workflow.coder_interface.CENTRAL_REPOSITORY_REGISTRY_PATH",
                registry_path,
            ):
                app = create_web_app(repository_registry_path=registry_path, state_root=state_root)
                app.testing = True
                client = app.test_client()
                initial = client.get("/procedure-updates?repository=fixture")
                self.assertEqual(initial.status_code, 200)
                self.assertIn(b"OVERLAY_INVALID", initial.data)
                self.assertIn(b"Complete proposed additive diff", initial.data)
                self.assertIn(b"Preserves original bytes", initial.data)
                self.assertIn(b"Resulting overlay contract", initial.data)
                self.assertIn(b"valid", initial.data)
                self.assertIn(b"Review Overlay Initialization", initial.data)
                self.assertNotIn(b"Confirm Overlay Initialization", initial.data)
                self.assertEqual(overlay.read_bytes(), original)
                csrf = _extract_csrf_token(client)
                digest = re.search(rb'name="plan_sha256" value="([0-9a-f]{64})"', initial.data)
                self.assertIsNotNone(digest)
                wrong_key = client.post(
                    "/procedure-updates/overlay-review",
                    data={
                        "csrf_token": csrf,
                        "repository_key": "unknown",
                        "active_repository_key": "fixture",
                        "repository_generation": "0",
                        "plan_sha256": digest.group(1).decode(),
                    },
                )
                self.assertEqual(wrong_key.status_code, 400)
                wrong_selection = client.post(
                    "/procedure-updates/overlay-review",
                    data={
                        "csrf_token": csrf,
                        "repository_key": "fixture",
                        "active_repository_key": "another-repository",
                        "repository_generation": "0",
                        "plan_sha256": digest.group(1).decode(),
                    },
                )
                self.assertEqual(wrong_selection.status_code, 409)
                stale_generation = client.post(
                    "/procedure-updates/overlay-review",
                    data={
                        "csrf_token": csrf,
                        "repository_key": "fixture",
                        "active_repository_key": "fixture",
                        "repository_generation": "1",
                        "plan_sha256": digest.group(1).decode(),
                    },
                )
                self.assertEqual(stale_generation.status_code, 409)
                self.assertEqual(overlay.read_bytes(), original)
                reviewed = client.post(
                    "/procedure-updates/overlay-review",
                    data={
                        "csrf_token": csrf,
                        "repository_key": "fixture",
                        "active_repository_key": "fixture",
                        "repository_generation": "0",
                        "plan_sha256": digest.group(1).decode(),
                    },
                )
                self.assertEqual(reviewed.status_code, 302)
                confirm_page = client.get("/procedure-updates?repository=fixture")
                self.assertIn(b"Confirm Overlay Initialization", confirm_page.data)
                self.assertIn(b"Durable before/after images", confirm_page.data)
                self.assertEqual(overlay.read_bytes(), original)
                token = re.search(
                    rb'name="confirmation_token" value="([^"]+)"',
                    confirm_page.data,
                )
                self.assertIsNotNone(token)
                initialized = client.post(
                    "/procedure-updates/overlay-initialize",
                    data={
                        "csrf_token": csrf,
                        "repository_key": "fixture",
                        "active_repository_key": "fixture",
                        "repository_generation": "0",
                        "confirmation_token": token.group(1).decode(),
                    },
                )
                self.assertEqual(initialized.status_code, 302)
                candidate_overlay = overlay.read_bytes()
                self.assertTrue(candidate_overlay.endswith(original))
                self.assertFalse((repo / ".github/copilot-instructions.md").exists())
                current_page = client.get("/procedure-updates?repository=fixture")
                self.assertIn(b"Review Apply", current_page.data)
                self.assertIn(b"Coder verification remains non-VERIFIED", current_page.data)
                self.assertEqual(load_milestone_workflow(repo, state_root, "docs/milestones", candidate)["phase"], "WAITING")

                apply_digest = re.search(
                    rb'name="plan_sha256" value="([0-9a-f]{64})"',
                    current_page.data,
                )
                self.assertIsNotNone(apply_digest)
                apply_reviewed = client.post(
                    "/procedure-updates/apply-review",
                    data={
                        "csrf_token": csrf,
                        "repository_key": "fixture",
                        "active_repository_key": "fixture",
                        "repository_generation": "0",
                        "plan_sha256": apply_digest.group(1).decode(),
                    },
                )
                self.assertEqual(apply_reviewed.status_code, 302)
                apply_confirmation_page = client.get("/procedure-updates?repository=fixture")
                self.assertIn(b"Confirm Apply", apply_confirmation_page.data)
                apply_token = re.search(
                    rb'name="confirmation_token" value="([^"]+)"',
                    apply_confirmation_page.data,
                )
                self.assertIsNotNone(apply_token)
                applied = client.post(
                    "/procedure-updates/apply",
                    data={
                        "csrf_token": csrf,
                        "repository_key": "fixture",
                        "active_repository_key": "fixture",
                        "repository_generation": "0",
                        "confirmation_token": apply_token.group(1).decode(),
                    },
                )
                self.assertEqual(applied.status_code, 302)
                self.assertTrue((repo / ".github/copilot-instructions.md").is_file())
                self.assertEqual(overlay.read_bytes(), candidate_overlay)
                final_page = client.get("/procedure-updates?repository=fixture")
                self.assertIn(b"CURRENT", final_page.data)
                with mock.patch(
                    "repoctl.workflow.coder_interface.require_event_runtime_compatibility",
                    return_value={
                        "status": "COMPATIBLE",
                        "runtime_instance_id": "b" * 32,
                        "producer_contract_id": "repoctl-workflow-event-writer:v1",
                        "event_type": "context_verification_recorded",
                        "required_event_capabilities": {
                            "context_verification_record_schema_version": 2,
                        },
                        "ledger_schema_version": 1,
                        "event_schema_version": 1,
                        "context_verification_record_schema_version": 2,
                    },
                ):
                    verification = submit_coder_context_verification(
                        "NEW",
                        git_boundary_acknowledged=True,
                        expected_repository_key="fixture",
                        expected_repository_id=make_repository_id(repo),
                        expected_repository_root=repo,
                        expected_milestone_id=candidate["milestone_id"],
                        repository_path=repo,
                        state_root=state_root,
                        milestone_root="docs/milestones",
                    )
                self.assertEqual(verification["procedure_status"], "CURRENT")
                self.assertEqual(verification["result"], "BLOCKED")
                self.assertEqual(verification["phase"], "WAITING")
                self.assertEqual(_git(repo, "diff", "--cached", "--name-only"), b"")

    def test_procedure_recovery_requires_its_own_review_and_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root, "recover-repo")
            state_root = root / "state"
            (repo / "docs/milestones").mkdir(parents=True)
            overlay = repo / "docs/context/coding_agent_rules.md"
            overlay.parent.mkdir(parents=True)
            overlay.write_text(
                "# Project overlay\n\n"
                "<!-- repo-control-project-overlay:v1 -->\n\n"
                "## Repository-specific rules\n\n- Keep fixture data local.\n",
                encoding="utf-8",
            )
            _git(repo, "add", "docs/context/coding_agent_rules.md")
            _git(repo, "commit", "-m", "add project overlay")
            registry_path = root / "repositories.toml"
            registry_path.write_text(
                "\n".join(
                    [
                        'default = "fixture"',
                        "",
                        "[repositories.fixture]",
                        'name = "Fixture Repository"',
                        f'path = "{repo}"',
                        'milestone_root = "docs/milestones"',
                        "",
                        "[repositories.fixture.documents]",
                        'coding_agent_rules = "docs/context/coding_agent_rules.md"',
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            entry = RepositoryRegistry.from_toml(registry_path).entries[0]
            with mock.patch(
                "repoctl.workflow.coder_interface.CENTRAL_REPOSITORY_REGISTRY_PATH",
                registry_path,
            ):
                bundle = load_bundle()
                plan = preview_reconciliation(entry, bundle)
                confirmation = review_apply(
                    entry,
                    state_root,
                    active_key=entry.key,
                    registry_generation=0,
                    reviewed_plan_sha256=procedure_plan_digest(entry, bundle, plan),
                    bundle=bundle,
                )
                process = multiprocessing.get_context("fork").Process(
                    target=_crash_fixture_apply,
                    args=(entry, str(state_root), confirmation["confirmation"]["token"]),
                )
                process.start()
                process.join(15)
            self.assertEqual(process.exitcode, 77)

            with mock.patch(
                "repoctl.workflow.coder_interface.CENTRAL_REPOSITORY_REGISTRY_PATH",
                registry_path,
            ):
                app = create_web_app(repository_registry_path=registry_path, state_root=state_root)
                app.testing = True
                client = app.test_client()
                pending = inspect_transaction(entry, state_root)
                self.assertEqual(pending["state"], "APPLYING")
                from repoctl.procedure_governance import ProcedureGovernanceError

                with mock.patch(
                    "repoctl.procedure_governance.load_bundle",
                    side_effect=ProcedureGovernanceError("Canonical source hash mismatch"),
                ):
                    page = client.get("/procedure-updates?repository=fixture")
                    self.assertIn(b"Canonical bundle integrity unavailable", page.data)
                    self.assertIn(b"Review Recovery", page.data)
                    self.assertNotIn(b"Confirm Recover", page.data)
                    csrf = _extract_csrf_token(client)
                    review = client.post(
                        "/procedure-updates/recovery-review",
                        data={
                            "csrf_token": csrf,
                            "repository_key": "fixture",
                            "active_repository_key": "fixture",
                            "repository_generation": "0",
                            "operation_id": pending["operation_id"],
                        },
                    )
                    self.assertEqual(review.status_code, 302)
                    confirmation_page = client.get("/procedure-updates?repository=fixture")
                    self.assertIn(b"Confirm Recover", confirmation_page.data)
                    token = re.search(
                        rb'name="confirmation_token" value="([^"]+)"',
                        confirmation_page.data,
                    )
                    self.assertIsNotNone(token)
                    recovered = client.post(
                        "/procedure-updates/recover",
                        data={
                            "csrf_token": csrf,
                            "repository_key": "fixture",
                            "active_repository_key": "fixture",
                            "repository_generation": "0",
                            "operation_id": pending["operation_id"],
                            "confirmation_token": token.group(1).decode(),
                        },
                    )
                    self.assertEqual(recovered.status_code, 302)
                self.assertFalse((repo / ".github/copilot-instructions.md").exists())
                self.assertEqual(inspect_transaction(entry, state_root)["state"], "RECOVERED")
                with mock.patch(
                    "repoctl.procedure_governance.load_bundle",
                    side_effect=ProcedureGovernanceError("Canonical source hash mismatch"),
                ):
                    final_page = client.get("/procedure-updates?repository=fixture")
                self.assertIn(b"RECOVERED", final_page.data)

    def test_dashboard_renders_clean_state_and_upstream_caveat(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            app = create_web_app(repository_path=str(repo), state_root=root / "state")
            client = app.test_client()

            resp = client.get("/")
            text = resp.get_data(as_text=True)
            self.assertEqual(resp.status_code, 200)
            self.assertIn("Repository Dashboard", text)
            self.assertIn("clean", text)
            self.assertIn("initial", text)
            self.assertIn("No remote refresh was performed.", text)

    def test_dashboard_renders_unstaged_state(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            (repo / "app.py").write_text("def run():\n    return 2\n", encoding="utf-8")

            app = create_web_app(repository_path=str(repo), state_root=root / "state")
            client = app.test_client()
            resp = client.get("/")
            self.assertEqual(resp.status_code, 200)
            self.assertIn("unstaged_only", resp.get_data(as_text=True))

    def test_context_post_requires_csrf(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            app = create_web_app(repository_path=str(repo), state_root=root / "state")
            client = app.test_client()

            before_head = _git_text(repo, "rev-parse", "HEAD")
            before_status = _git_status_porcelain(repo)

            resp = client.post("/context/generate", data={"query": "source readiness"})
            self.assertEqual(resp.status_code, 400)
            self.assertIn("Invalid request token.", resp.get_data(as_text=True))
            self.assertEqual(before_head, _git_text(repo, "rev-parse", "HEAD"))
            self.assertEqual(before_status, _git_status_porcelain(repo))

    def test_context_escapes_html_and_keeps_target_git_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            app = create_web_app(repository_path=str(repo), state_root=root / "state")
            client = app.test_client()

            before_head = _git_text(repo, "rev-parse", "HEAD")
            before_status = _git_status_porcelain(repo)

            client.get("/context")
            csrf = _extract_csrf_token(client)
            resp = client.post(
                "/context/generate",
                data={"csrf_token": csrf, "query": "<script>alert(1)</script>"},
                follow_redirects=True,
            )
            text = resp.get_data(as_text=True)
            self.assertEqual(resp.status_code, 200)
            self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", text)
            self.assertNotIn("<script>alert(1)</script>", text)
            self.assertEqual(before_head, _git_text(repo, "rev-parse", "HEAD"))
            self.assertEqual(before_status, _git_status_porcelain(repo))

    def test_context_shows_current_then_stale_when_worktree_changes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            app = create_web_app(repository_path=str(repo), state_root=root / "state")
            client = app.test_client()

            client.get("/context")
            csrf = _extract_csrf_token(client)
            generated = client.post(
                "/context/generate",
                data={"csrf_token": csrf, "query": "helper"},
                follow_redirects=True,
            )
            self.assertEqual(generated.status_code, 200)
            self.assertIn("CURRENT", generated.get_data(as_text=True))

            repo_id = app.config["REPO_ID"]
            contexts_root = (root / "state" / repo_id / "contexts")
            context_ids = sorted([child.name for child in contexts_root.iterdir() if child.is_dir()])
            context_id = context_ids[-1]

            (repo / "app.py").write_text("def run():\n    return 5\n", encoding="utf-8")
            stale = client.get(f"/context?context_id={context_id}")
            stale_text = stale.get_data(as_text=True)
            self.assertEqual(stale.status_code, 200)
            self.assertIn("STALE", stale_text)
            self.assertIn("Working-tree state differs", stale_text)

    def test_context_and_snapshot_routes_preserve_architect_dispatch_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            state_root = root / "state"
            repository_id = make_repository_id(repo)
            dispatch = submit_dispatch(
                repo,
                state_root,
                repository_id,
                dispatch_type="INSTRUCTION",
                milestone_id="M008.003.001",
                body="Survive browser-triggered scans.",
            )
            ledger = state_root / repository_id / "architect_dispatches.json"
            original_bytes = ledger.read_bytes()
            app = create_web_app(repository_path=str(repo), state_root=state_root)
            client = app.test_client()
            client.get("/context")
            csrf = _extract_csrf_token(client)

            generated = client.post(
                "/context/generate",
                data={"csrf_token": csrf, "query": "scanner dispatch persistence"},
            )
            self.assertEqual(generated.status_code, 302)
            self.assertEqual(ledger.read_bytes(), original_bytes)
            self.assertIn(dispatch["dispatch_id"], original_bytes.decode("utf-8"))

            snapshot = client.post("/snapshots/create", data={"csrf_token": csrf})
            self.assertEqual(snapshot.status_code, 302)
            self.assertEqual(ledger.read_bytes(), original_bytes)

    def test_context_groups_related_test_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            app = create_web_app(repository_path=str(repo), state_root=root / "state")
            client = app.test_client()

            client.get("/context")
            csrf = _extract_csrf_token(client)
            resp = client.post(
                "/context/generate",
                data={"csrf_token": csrf, "query": "helper"},
                follow_redirects=True,
            )
            text = resp.get_data(as_text=True)
            self.assertEqual(resp.status_code, 200)
            self.assertIn("Related Tests groups deterministic test-reference evidence", text)
            self.assertIn("Evidence references:", text)
            self.assertIn("Details (", text)

    def test_context_selector_retains_non_first_selection_after_view(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            app = create_web_app(repository_path=str(repo), state_root=root / "state")
            client = app.test_client()

            client.get("/context")
            csrf = _extract_csrf_token(client)
            first = client.post(
                "/context/generate",
                data={"csrf_token": csrf, "query": "helper"},
                follow_redirects=True,
            )
            self.assertEqual(first.status_code, 200)

            second = client.post(
                "/context/generate",
                data={"csrf_token": csrf, "query": "module_b"},
                follow_redirects=True,
            )
            self.assertEqual(second.status_code, 200)

            repo_id = app.config["REPO_ID"]
            contexts_root = root / "state" / repo_id / "contexts"
            context_ids = sorted([child.name for child in contexts_root.iterdir() if child.is_dir()])
            self.assertGreaterEqual(len(context_ids), 2)
            selected_context_id = context_ids[1]

            viewed = client.get(f"/context?context_id={selected_context_id}")
            self.assertEqual(viewed.status_code, 200)
            text = viewed.get_data(as_text=True)
            expected_pattern = rf'<option value="{re.escape(selected_context_id)}" selected>{re.escape(selected_context_id)}</option>'
            self.assertRegex(text, expected_pattern)

    def test_snapshot_create_requires_csrf(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            state_root = root / "state"
            app = create_web_app(repository_path=str(repo), state_root=state_root)
            client = app.test_client()

            snapshots_root = state_root / app.config["REPO_ID"] / "snapshots"
            resp = client.post("/snapshots/create", data={})
            self.assertEqual(resp.status_code, 400)
            self.assertFalse(snapshots_root.exists())

    def test_snapshot_reuse_identity_on_repeat(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            state_root = root / "state"
            app = create_web_app(repository_path=str(repo), state_root=state_root)
            client = app.test_client()

            client.get("/snapshots")
            csrf = _extract_csrf_token(client)
            resp1 = client.post("/snapshots/create", data={"csrf_token": csrf}, follow_redirects=True)
            self.assertEqual(resp1.status_code, 200)
            resp2 = client.post("/snapshots/create", data={"csrf_token": csrf}, follow_redirects=True)
            self.assertEqual(resp2.status_code, 200)

            repo_id = app.config["REPO_ID"]
            snapshots_root = state_root / repo_id / "snapshots"
            snapshot_ids = sorted([child.name for child in snapshots_root.iterdir() if child.is_dir()])
            self.assertEqual(len(snapshot_ids), 1)

    def test_snapshots_post_create_feedback_and_selected_row(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            state_root = root / "state"
            app = create_web_app(repository_path=str(repo), state_root=state_root)
            client = app.test_client()

            client.get("/snapshots")
            csrf = _extract_csrf_token(client)
            resp = client.post("/snapshots/create", data={"csrf_token": csrf}, follow_redirects=True)
            text = resp.get_data(as_text=True)
            self.assertEqual(resp.status_code, 200)
            self.assertIn("Snapshot captured", text)

            repo_id = app.config["REPO_ID"]
            snapshots_root = state_root / repo_id / "snapshots"
            snapshot_ids = sorted([child.name for child in snapshots_root.iterdir() if child.is_dir()])
            self.assertEqual(len(snapshot_ids), 1)
            snapshot_id = snapshot_ids[0]
            self.assertIn(f"Snapshot ID: {snapshot_id}", text)
            self.assertIn(f'<tr class="row-selected">', text)

    def test_snapshots_page_orders_newest_first_by_filesystem_mtime(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            state_root = root / "state"
            app = create_web_app(repository_path=str(repo), state_root=state_root)
            client = app.test_client()

            client.get("/snapshots")
            csrf = _extract_csrf_token(client)

            first = client.post("/snapshots/create", data={"csrf_token": csrf}, follow_redirects=True)
            self.assertEqual(first.status_code, 200)

            (repo / "module_b.py").write_text("def helper():\n    return 2\n", encoding="utf-8")
            second = client.post("/snapshots/create", data={"csrf_token": csrf}, follow_redirects=True)
            self.assertEqual(second.status_code, 200)

            repo_id = app.config["REPO_ID"]
            snapshots_root = state_root / repo_id / "snapshots"
            ids = [child.name for child in snapshots_root.iterdir() if child.is_dir()]
            self.assertEqual(len(ids), 2)

            for idx, snapshot_id in enumerate(sorted(ids)):
                dir_path = snapshots_root / snapshot_id
                os.utime(dir_path, (1000 + idx, 1000 + idx))

            newest_id = sorted(ids)[-1]
            oldest_id = sorted(ids)[0]

            resp = client.get("/snapshots")
            self.assertEqual(resp.status_code, 200)
            text = resp.get_data(as_text=True)
            newest_pos = text.find(f"Snapshot ID: {newest_id}")
            oldest_pos = text.find(f"Snapshot ID: {oldest_id}")
            self.assertNotEqual(newest_pos, -1)
            self.assertNotEqual(oldest_pos, -1)
            self.assertLess(newest_pos, oldest_pos)

    def test_comparison_known_snapshot_and_unknown_snapshot_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            state_root = root / "state"
            app = create_web_app(repository_path=str(repo), state_root=state_root)
            client = app.test_client()

            before = create_snapshot(run_scan_with_artifacts(str(repo), state_root=state_root), state_root=state_root)
            (repo / "module_b.py").write_text("def helper():\n    return 2\n", encoding="utf-8")
            _git(repo, "add", "module_b.py")
            _git(repo, "commit", "-m", "change")
            after = create_snapshot(run_scan_with_artifacts(str(repo), state_root=state_root), state_root=state_root)

            client.get("/comparisons")
            csrf = _extract_csrf_token(client)
            good = client.post(
                "/comparisons/create",
                data={
                    "csrf_token": csrf,
                    "before_snapshot_id": before["snapshot_id"],
                    "after_snapshot_id": after["snapshot_id"],
                },
                follow_redirects=True,
            )
            self.assertEqual(good.status_code, 200)
            good_text = good.get_data(as_text=True)
            self.assertIn("Viewing Saved Comparison", good_text)
            self.assertIn("Before Preview", good_text)
            self.assertIn("After Preview", good_text)
            self.assertIn("Captured artifact time (UTC)", good_text)
            self.assertIn(f"Snapshot ID: {before['snapshot_id']}", good_text)
            self.assertIn(f"Snapshot ID: {after['snapshot_id']}", good_text)
            self.assertIn(
                f'<option value="{before["snapshot_id"]}" selected>',
                good_text,
            )
            self.assertIn(
                f'<option value="{after["snapshot_id"]}" selected>',
                good_text,
            )
            self.assertIn("files: added=", good_text)
            self.assertIn("symbols: added=", good_text)

            bad = client.post(
                "/comparisons/create",
                data={
                    "csrf_token": csrf,
                    "before_snapshot_id": "snap--unknown",
                    "after_snapshot_id": after["snapshot_id"],
                },
            )
            self.assertEqual(bad.status_code, 400)
            self.assertIn("known snapshot IDs", bad.get_data(as_text=True))

    def test_known_comparisons_order_newest_first_by_filesystem_mtime(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            state_root = root / "state"
            app = create_web_app(repository_path=str(repo), state_root=state_root)
            client = app.test_client()

            snap_a = create_snapshot(run_scan_with_artifacts(str(repo), state_root=state_root), state_root=state_root)

            (repo / "module_b.py").write_text("def helper():\n    return 2\n", encoding="utf-8")
            _git(repo, "add", "module_b.py")
            _git(repo, "commit", "-m", "change one")
            snap_b = create_snapshot(run_scan_with_artifacts(str(repo), state_root=state_root), state_root=state_root)

            (repo / "module_a.py").write_text("from module_b import helper\n\n\ndef run():\n    return helper() + 1\n", encoding="utf-8")
            _git(repo, "add", "module_a.py")
            _git(repo, "commit", "-m", "change two")
            snap_c = create_snapshot(run_scan_with_artifacts(str(repo), state_root=state_root), state_root=state_root)

            cmp_one = compare_snapshots(snap_a["snapshot_id"], snap_b["snapshot_id"], str(repo), state_root=state_root)
            cmp_two = compare_snapshots(snap_b["snapshot_id"], snap_c["snapshot_id"], str(repo), state_root=state_root)

            repo_id = app.config["REPO_ID"]
            comparisons_root = state_root / repo_id / "comparisons"
            os.utime(comparisons_root / cmp_one["comparison_id"], (1000, 1000))
            os.utime(comparisons_root / cmp_two["comparison_id"], (1001, 1001))

            resp = client.get("/comparisons")
            self.assertEqual(resp.status_code, 200)
            text = resp.get_data(as_text=True)
            newer_pos = text.find(f"Comparison ID: {cmp_two['comparison_id']}")
            older_pos = text.find(f"Comparison ID: {cmp_one['comparison_id']}")
            self.assertNotEqual(newer_pos, -1)
            self.assertNotEqual(older_pos, -1)
            self.assertLess(newer_pos, older_pos)

    def test_comparison_preview_counts_match_snapshot_entry_classification(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            state_root = root / "state"
            app = create_web_app(repository_path=str(repo), state_root=state_root)
            client = app.test_client()

            clean_snapshot = create_snapshot(run_scan_with_artifacts(str(repo), state_root=state_root), state_root=state_root)

            (repo / "app.py").write_text("def run():\n    return 99\n", encoding="utf-8")
            dirty_snapshot = create_snapshot(run_scan_with_artifacts(str(repo), state_root=state_root), state_root=state_root)

            resp = client.get(
                f"/comparisons?before_snapshot_id={clean_snapshot['snapshot_id']}&after_snapshot_id={dirty_snapshot['snapshot_id']}"
            )
            self.assertEqual(resp.status_code, 200)
            text = resp.get_data(as_text=True)
            self.assertIn("Working tree counts (snapshot entry classification): staged=0, unstaged=1, untracked=0, conflicts=0", text)

    def test_analysis_render_and_provider_failure_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            state_root = root / "state"
            app = create_web_app(repository_path=str(repo), state_root=state_root)
            client = app.test_client()

            before = create_snapshot(run_scan_with_artifacts(str(repo), state_root=state_root), state_root=state_root)
            (repo / "module_b.py").write_text("from module_c import new_func\n\ndef helper():\n    return new_func()\n", encoding="utf-8")
            (repo / "module_c.py").write_text("def new_func():\n    return 3\n", encoding="utf-8")
            _git(repo, "add", "module_b.py", "module_c.py")
            _git(repo, "commit", "-m", "delta")
            after = create_snapshot(run_scan_with_artifacts(str(repo), state_root=state_root), state_root=state_root)
            cmp_result = compare_snapshots(before["snapshot_id"], after["snapshot_id"], str(repo), state_root=state_root)

            provider = _FakeProvider(
                {
                    "summary": "Deterministic structural evidence reviewed.",
                    "summary_evidence_ids": ["A001"],
                    "review_signals": [
                        {
                            "category": "cross_category",
                            "review_priority": "low",
                            "observation": "Structural changes present.",
                            "interpretation": "Inspect source and tests.",
                            "evidence_ids": ["A001"],
                        }
                    ],
                    "questions_for_human_review": [
                        {
                            "review_priority": "low",
                            "question": "Was this change intentional?",
                            "evidence_ids": ["A001"],
                        }
                    ],
                }
            )
            analysis_result = analyze_comparison(cmp_result["comparison_id"], str(repo), state_root=state_root, provider=provider)

            rendered = client.get(
                f"/analysis?comparison_id={cmp_result['comparison_id']}&analysis_id={analysis_result['analysis_id']}"
            )
            text = rendered.get_data(as_text=True)
            self.assertEqual(rendered.status_code, 200)
            self.assertIn("A001", text)
            self.assertIn("advisory", text.casefold())
            self.assertNotIn("message.thinking", text)

            client.get("/analysis")
            csrf = _extract_csrf_token(client)
            with mock.patch("repoctl.web.app.analyze_comparison", side_effect=AnalysisError("provider error [invalid_structured_content_json]: invalid")):
                failed = client.post(
                    "/analysis/run",
                    data={"csrf_token": csrf, "comparison_id": cmp_result["comparison_id"]},
                )
            self.assertEqual(failed.status_code, 400)
            self.assertIn("No TARGET GIT MUTATION".lower(), failed.get_data(as_text=True).lower())

    def test_workflow_page_has_visibility_and_no_mutation_controls(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            state_root = root / "state"
            app = create_web_app(repository_path=str(repo), state_root=state_root)
            client = app.test_client()

            resp = client.get("/workflow")
            text = resp.get_data(as_text=True)
            self.assertEqual(resp.status_code, 200)
            self.assertIn("Workflow Visibility", text)
            self.assertNotIn("Approve Stage", text)
            self.assertNotIn("Approve Commit", text)

    def test_workflow_page_refreshes_current_state_and_hides_stage_for_staged_state(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            state_root = root / "state"
            app = create_web_app(repository_path=str(repo), state_root=state_root)
            client = app.test_client()

            (repo / "module_c.py").write_text("def helper_two():\n    return 2\n", encoding="utf-8")
            resp = client.get("/workflow")
            self.assertEqual(resp.status_code, 200)
            self.assertIn("unstaged_only", resp.get_data(as_text=True))
            self.assertIn("READ ONLY", resp.get_data(as_text=True))
            self.assertNotIn("Prepare Stage", resp.get_data(as_text=True))

            _git(repo, "add", "module_c.py")
            staged_resp = client.get("/workflow")
            staged_text = staged_resp.get_data(as_text=True)
            self.assertEqual(staged_resp.status_code, 200)
            self.assertIn("staged_only", staged_text)
            self.assertNotIn('action="/workflow/stage/prepare"', staged_text)
            self.assertNotIn("Create Matching Snapshot", staged_text)
            self.assertNotIn("Prepare Commit", staged_text)

    def test_workflow_page_commit_eligibility_follows_canonical_state(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            state_root = root / "state"
            app = create_web_app(repository_path=str(repo), state_root=state_root)
            client = app.test_client()

            resp_clean = client.get("/workflow")
            clean_text = resp_clean.get_data(as_text=True)
            self.assertIn("clean", clean_text)
            self.assertNotIn('action="/workflow/commit/prepare"', clean_text)
            self.assertNotIn("Prepare Commit", clean_text)

            (repo / "module_c.py").write_text("def helper_two():\n    return 2\n", encoding="utf-8")
            resp_unstaged = client.get("/workflow")
            unstaged_text = resp_unstaged.get_data(as_text=True)
            self.assertIn("unstaged_only", unstaged_text)
            self.assertNotIn('action="/workflow/commit/prepare"', unstaged_text)
            self.assertNotIn("Prepare Commit", unstaged_text)

            _git(repo, "add", "module_c.py")
            resp_staged = client.get("/workflow")
            staged_text = resp_staged.get_data(as_text=True)
            self.assertIn("staged_only", staged_text)
            self.assertNotIn('action="/workflow/commit/prepare"', staged_text)
            self.assertNotIn("Prepare Commit", staged_text)

    def test_legacy_workflow_snapshot_and_mutation_posts_are_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            state_root = root / "state"
            app = create_web_app(repository_path=str(repo), state_root=state_root)
            client = app.test_client()
            client.get("/workflow")
            csrf = _extract_csrf_token(client)
            for path in (
                "/workflow/snapshot/create",
                "/workflow/stage/prepare",
                "/workflow/stage/approve",
                "/workflow/commit/prepare",
                "/workflow/commit/approve",
            ):
                response = client.post(path, data={"csrf_token": csrf})
                self.assertEqual(response.status_code, 410, path)
                self.assertIn("disabled", response.get_data(as_text=True))

    def test_legacy_workflow_stage_routes_cannot_mutate(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            state_root = root / "state"
            app = create_web_app(repository_path=str(repo), state_root=state_root)
            client = app.test_client()
            client.get("/workflow")
            csrf = _extract_csrf_token(client)
            before = (_git_text(repo, "rev-parse", "HEAD"), _git(repo, "status", "--porcelain=v2", "-z"))
            prepared = client.post("/workflow/stage/prepare", data={"csrf_token": csrf})
            approved = client.post("/workflow/stage/approve", data={"csrf_token": csrf, "plan_id": "old-plan"})
            self.assertEqual(prepared.status_code, 410)
            self.assertEqual(approved.status_code, 410)
            self.assertEqual(before, (_git_text(repo, "rev-parse", "HEAD"), _git(repo, "status", "--porcelain=v2", "-z")))

    def test_legacy_workflow_commit_routes_cannot_mutate(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            state_root = root / "state"
            app = create_web_app(repository_path=str(repo), state_root=state_root)
            client = app.test_client()
            client.get("/workflow")
            csrf = _extract_csrf_token(client)
            before = (_git_text(repo, "rev-parse", "HEAD"), _git(repo, "status", "--porcelain=v2", "-z"))
            prepared = client.post(
                "/workflow/commit/prepare",
                data={"csrf_token": csrf, "commit_message": "legacy"},
            )
            approved = client.post("/workflow/commit/approve", data={"csrf_token": csrf, "plan_id": "old-plan"})
            self.assertEqual(prepared.status_code, 410)
            self.assertEqual(approved.status_code, 410)
            self.assertEqual(before, (_git_text(repo, "rev-parse", "HEAD"), _git(repo, "status", "--porcelain=v2", "-z")))

    def test_workflow_history_interleaves_stage_and_commit_with_scope_details(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            state_root = root / "state"

            (repo / "app.py").write_text("def run():\n    return 11\n", encoding="utf-8")
            stage_one = prepare_stage(str(repo), include_all=True, state_root=state_root)
            execute_prepared_stage(str(repo), stage_one["plan_id"], approve=True, state_root=state_root)
            create_snapshot(run_scan_with_artifacts(str(repo), state_root=state_root), state_root=state_root)
            commit_one = prepare_commit(str(repo), "cycle one commit", state_root=state_root)
            execute_prepared_commit(str(repo), commit_one["plan_id"], approve=True, state_root=state_root)

            (repo / "module_b.py").write_text("def helper():\n    return 7\n", encoding="utf-8")
            stage_two = prepare_stage(str(repo), include_all=True, state_root=state_root)
            execute_prepared_stage(str(repo), stage_two["plan_id"], approve=True, state_root=state_root)
            create_snapshot(run_scan_with_artifacts(str(repo), state_root=state_root), state_root=state_root)
            commit_two = prepare_commit(str(repo), "cycle two commit", state_root=state_root)
            execute_prepared_commit(str(repo), commit_two["plan_id"], approve=True, state_root=state_root)

            app = create_web_app(repository_path=str(repo), state_root=state_root)
            client = app.test_client()
            resp = client.get("/workflow")
            text = resp.get_data(as_text=True)
            self.assertEqual(resp.status_code, 200)

            self.assertIn("Workflow history - newest to oldest.", text)
            self.assertIn("Stage vs Commit help", text)
            self.assertIn("View scope", text)
            self.assertIn("Compatibility indicates whether this historical plan matches the repository's current state", text)
            self.assertIn("cycle one commit", text)

            stage_one_pos = text.find(stage_one["plan_id"])
            commit_one_pos = text.find(commit_one["plan_id"])
            stage_two_pos = text.find(stage_two["plan_id"])
            commit_two_pos = text.find(commit_two["plan_id"])
            self.assertNotEqual(stage_one_pos, -1)
            self.assertNotEqual(commit_one_pos, -1)
            self.assertNotEqual(stage_two_pos, -1)
            self.assertNotEqual(commit_two_pos, -1)
            self.assertLess(commit_two_pos, stage_two_pos)
            self.assertLess(stage_two_pos, commit_one_pos)
            self.assertLess(commit_one_pos, stage_one_pos)

    def test_forbidden_git_mutation_services_not_invoked_by_web_actions(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            state_root = root / "state"
            app = create_web_app(repository_path=str(repo), state_root=state_root)
            client = app.test_client()

            with mock.patch("repoctl.web.app.compare_snapshots") as cmp_mock, mock.patch("repoctl.web.app.create_snapshot") as snap_mock:
                client.get("/")
                client.get("/context")
                client.get("/workflow")
                self.assertFalse(cmp_mock.called)
                self.assertFalse(snap_mock.called)

    def test_git_review_is_read_only_and_renders_distinct_change_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            tracked = repo / "app.py"
            tracked.write_text("def run():\n    return 2\n", encoding="utf-8")
            _git(repo, "add", "app.py")
            tracked.write_text("def run():\n    return 3\n", encoding="utf-8")
            unusual = repo / "<script>.txt"
            unusual.write_text("<script>alert(1)</script>\n", encoding="utf-8")

            state_root = root / "state"
            app = create_web_app(repository_path=str(repo), state_root=state_root)
            client = app.test_client()

            def snapshot_tree(path: Path) -> dict[str, bytes | str]:
                result: dict[str, bytes | str] = {}
                for item in sorted(path.rglob("*")):
                    relative = str(item.relative_to(path))
                    if item.is_symlink():
                        result[relative] = f"symlink:{os.readlink(item)}"
                    elif item.is_file():
                        result[relative] = item.read_bytes()
                return result

            repository_before = snapshot_tree(repo)
            state_before = snapshot_tree(state_root)

            with (
                mock.patch("repoctl.web.app.generate_milestone_status") as status_mock,
                mock.patch("repoctl.web.app.run_scan_with_artifacts") as scan_mock,
                mock.patch("repoctl.web.app.create_snapshot") as snapshot_mock,
                mock.patch("repoctl.workflow.git_actions.PreparedGitActions.confirm") as confirm_mock,
            ):
                listing = client.get("/git-review")
                listing_text = listing.get_data(as_text=True)
                self.assertEqual(listing.status_code, 200)
                self.assertIn("Git Review", listing_text)
                self.assertIn("REVIEW · CONFIRM TO ACT", listing_text)
                self.assertIn('class="main main-wide"', listing_text)
                self.assertIn("Staged + additional unstaged changes", listing_text)
                self.assertIn("New / untracked", listing_text)
                self.assertIn(">??</span>", listing_text)
                self.assertIn("Select all visible", listing_text)
                self.assertIn("Clear selection", listing_text)
                self.assertIn("Stage selected", listing_text)
                self.assertIn("Unstage selected", listing_text)
                self.assertIn("Restore unstaged", listing_text)
                self.assertIn("Restore to HEAD", listing_text)
                self.assertIn("&lt;script&gt;.txt", listing_text)
                self.assertNotIn("<script>alert(1)</script>", listing_text)
                nav = listing_text.split('<nav class="nav">', 1)[1].split("</nav>", 1)[0]
                for label in ("Dashboard", "Git Review"):
                    self.assertIn(f">{label}</a>", nav)
                self.assertNotIn(">Workflow</a>", nav)
                for legacy_label in ("Context", "Snapshots", "Comparisons", "AI Review"):
                    self.assertNotIn(f">{legacy_label}</a>", nav)
                route_paths = {rule.rule for rule in app.url_map.iter_rules()}
                self.assertTrue({"/context", "/snapshots", "/comparisons", "/analysis"} <= route_paths)

                match = re.search(r'name="path_token" value="([A-Za-z0-9_-]+)"[^>]*aria-label="Select app\.py"', listing_text)
                self.assertIsNotNone(match)
                detail = client.get(f"/git-review?file={match.group(1)}")
                self.assertEqual(detail.status_code, 200)
                self.assertIn("Select a changed file", listing_text)
                detail_text = detail.get_data(as_text=True)
                self.assertIn("Recovery previews", detail_text)
                self.assertIn("Staged — index vs HEAD", detail_text)
                self.assertIn("Unstaged — working tree vs index", detail_text)
                self.assertIn("Known from Git", detail_text)
                self.assertIn("Unknown", detail_text)
                self.assertIn("Scope evidence", detail_text)
                self.assertIn("Preview: Restore unstaged changes", detail_text)

                restore_head = client.get(f"/git-review?file={match.group(1)}&preview=restore-head")
                restore_head_text = restore_head.get_data(as_text=True)
                self.assertEqual(restore_head.status_code, 200)
                self.assertIn("Preview: Restore to HEAD", restore_head_text)
                self.assertIn("NO CHANGE HAS BEEN APPLIED", restore_head_text)
                self.assertIn("HEAD source", restore_head_text)
                self.assertIn("State/content replaced", restore_head_text)
                self.assertIn("Potential file-content loss", restore_head_text)
                self.assertIn("Would change:</strong> index, worktree", restore_head_text)
                self.assertIn("Worktree vs HEAD", restore_head_text)

                unstage_preview = client.get(f"/git-review?file={match.group(1)}&preview=unstage")
                self.assertEqual(unstage_preview.status_code, 200)
                self.assertIn("Would change:</strong> index", unstage_preview.get_data(as_text=True))

                invalid_preview = client.get(f"/git-review?file={match.group(1)}&preview=commit")
                self.assertEqual(invalid_preview.status_code, 400)

                untracked_match = re.search(
                    r'name="path_token" value="([A-Za-z0-9_-]+)"[^>]*aria-label="Select &lt;script&gt;\.txt"',
                    listing_text,
                )
                self.assertIsNotNone(untracked_match)
                preview = client.get(f"/git-review?file={untracked_match.group(1)}")
                preview_text = preview.get_data(as_text=True)
                self.assertEqual(preview.status_code, 200)
                self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", preview_text)
                self.assertNotIn("<script>alert(1)</script>", preview_text)
                self.assertNotIn("Preview: Restore to HEAD", preview_text)
                self.assertIn("Keep for later review", preview_text)

                status_mock.assert_not_called()
                scan_mock.assert_not_called()
                snapshot_mock.assert_not_called()
                confirm_mock.assert_not_called()

            self.assertEqual(repository_before, snapshot_tree(repo))
            self.assertEqual(state_before, snapshot_tree(state_root))

    def test_git_review_surfaces_git_errors_without_reporting_clean_state(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            app = create_web_app(repository_path=str(repo), state_root=root / "state")
            client = app.test_client()

            with mock.patch(
                "repoctl.web.app.inspect_git_review",
                side_effect=GitReviewError("status evidence exceeded limit"),
            ):
                response = client.get("/git-review")

            self.assertEqual(response.status_code, 503)
            text = response.get_data(as_text=True)
            self.assertIn("Git state unavailable", text)
            self.assertIn("status evidence exceeded limit", text)
            self.assertNotIn("The working tree is clean.", text)
