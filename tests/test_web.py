from __future__ import annotations

import subprocess
import tempfile
import unittest
import re
import os
from pathlib import Path
from unittest import mock

from repoctl.analysis.manager import AnalysisError, analyze_comparison
from repoctl.analysis.provider import ModelIdentity
from repoctl.compare.manager import compare_snapshots
from repoctl.scanner.core import run_scan_with_artifacts
from repoctl.snapshot.manager import create_snapshot
from repoctl.web.app import create_web_app
from repoctl.workflow.commit_execution import execute_prepared_commit
from repoctl.workflow.commit_plan import prepare_commit
from repoctl.workflow.stage_execution import execute_prepared_stage
from repoctl.workflow.stage_plan import prepare_stage


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
            self.assertIn("Prepare Stage", resp.get_data(as_text=True))

            _git(repo, "add", "module_c.py")
            staged_resp = client.get("/workflow")
            staged_text = staged_resp.get_data(as_text=True)
            self.assertEqual(staged_resp.status_code, 200)
            self.assertIn("staged_only", staged_text)
            self.assertNotIn('action="/workflow/stage/prepare"', staged_text)
            self.assertNotIn('<button type="submit" class="primary">Prepare Stage</button>', staged_text)
            self.assertIn("Matching Snapshot required", staged_text)
            self.assertIn('action="/workflow/snapshot/create"', staged_text)

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
            self.assertNotIn('<button type="submit" class="primary">Prepare Commit</button>', clean_text)

            (repo / "module_c.py").write_text("def helper_two():\n    return 2\n", encoding="utf-8")
            resp_unstaged = client.get("/workflow")
            unstaged_text = resp_unstaged.get_data(as_text=True)
            self.assertIn("unstaged_only", unstaged_text)
            self.assertNotIn('action="/workflow/commit/prepare"', unstaged_text)
            self.assertNotIn('<button type="submit" class="primary">Prepare Commit</button>', unstaged_text)

            _git(repo, "add", "module_c.py")
            resp_staged = client.get("/workflow")
            staged_text = resp_staged.get_data(as_text=True)
            self.assertIn("staged_only", staged_text)
            self.assertIn("Matching Snapshot required", staged_text)
            self.assertNotIn('action="/workflow/commit/prepare"', staged_text)

    def test_workflow_can_create_matching_snapshot_explicitly(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            state_root = root / "state"
            (repo / "module_c.py").write_text("def helper_two():\n    return 2\n", encoding="utf-8")
            _git(repo, "add", "module_c.py")
            app = create_web_app(repository_path=str(repo), state_root=state_root)
            client = app.test_client()
            client.get("/workflow")
            csrf = _extract_csrf_token(client)

            before = client.get("/workflow")
            self.assertIn("Matching Snapshot required", before.get_data(as_text=True))
            created = client.post(
                "/workflow/snapshot/create",
                data={"csrf_token": csrf},
                follow_redirects=True,
            )
            created_text = created.get_data(as_text=True)
            self.assertEqual(created.status_code, 200)
            self.assertIn("Matching Snapshot available", created_text)
            self.assertIn('action="/workflow/commit/prepare"', created_text)

    def test_workflow_stage_review_and_approve_flow(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            state_root = root / "state"
            (repo / "module_c.py").write_text("def helper_two():\n    return 2\n", encoding="utf-8")

            app = create_web_app(repository_path=str(repo), state_root=state_root)
            client = app.test_client()
            client.get("/workflow")
            csrf = _extract_csrf_token(client)

            prepare_resp = client.post("/workflow/stage/prepare", data={"csrf_token": csrf}, follow_redirects=False)
            self.assertEqual(prepare_resp.status_code, 302)
            self.assertIn("/workflow/stage/plan?plan_id=", prepare_resp.headers["Location"])
            plan_id = prepare_resp.headers["Location"].split("plan_id=")[1]

            review_resp = client.get(f"/workflow/stage/plan?plan_id={plan_id}")
            review_text = review_resp.get_data(as_text=True)
            self.assertEqual(review_resp.status_code, 200)
            self.assertIn("Action:", review_text)
            self.assertIn("Stage", review_text)
            self.assertIn("Exact Stage Plan ID", review_text)
            self.assertIn(plan_id, review_text)
            self.assertIn("Approve Stage", review_text)

            approve_resp = client.post(
                "/workflow/stage/approve",
                data={"csrf_token": csrf, "plan_id": plan_id},
                follow_redirects=True,
            )
            self.assertEqual(approve_resp.status_code, 200)
            self.assertIn("Stage execution succeeded", approve_resp.get_data(as_text=True))
            self.assertIn(plan_id, approve_resp.get_data(as_text=True))

    def test_workflow_commit_review_and_approve_flow(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_repo(root)
            state_root = root / "state"
            app = create_web_app(repository_path=str(repo), state_root=state_root)
            client = app.test_client()
            client.get("/workflow")
            csrf = _extract_csrf_token(client)

            (repo / "module_c.py").write_text("def helper_two():\n    return 2\n", encoding="utf-8")
            prepare_stage_resp = client.post("/workflow/stage/prepare", data={"csrf_token": csrf}, follow_redirects=False)
            self.assertEqual(prepare_stage_resp.status_code, 302)
            stage_plan_id = prepare_stage_resp.headers["Location"].split("plan_id=")[1]
            stage_approve_resp = client.post(
                "/workflow/stage/approve",
                data={"csrf_token": csrf, "plan_id": stage_plan_id},
                follow_redirects=True,
            )
            self.assertEqual(stage_approve_resp.status_code, 200)
            create_snapshot(run_scan_with_artifacts(str(repo), state_root=state_root), state_root=state_root)

            prepare_resp = client.post(
                "/workflow/commit/prepare",
                data={"csrf_token": csrf, "commit_message": "commit via browser flow"},
                follow_redirects=False,
            )
            self.assertEqual(prepare_resp.status_code, 302)
            self.assertIn("/workflow/commit/plan?plan_id=", prepare_resp.headers["Location"])
            commit_plan_id = prepare_resp.headers["Location"].split("plan_id=")[1]

            review_resp = client.get(f"/workflow/commit/plan?plan_id={commit_plan_id}")
            review_text = review_resp.get_data(as_text=True)
            self.assertEqual(review_resp.status_code, 200)
            self.assertIn("Action:", review_text)
            self.assertIn("Commit", review_text)
            self.assertIn("Exact Commit Plan ID", review_text)
            self.assertIn(commit_plan_id, review_text)
            self.assertIn("Approve Commit", review_text)

            approve_resp = client.post(
                "/workflow/commit/approve",
                data={"csrf_token": csrf, "plan_id": commit_plan_id},
                follow_redirects=True,
            )
            self.assertEqual(approve_resp.status_code, 200)
            self.assertIn("Commit execution succeeded", approve_resp.get_data(as_text=True))
            self.assertIn(commit_plan_id, approve_resp.get_data(as_text=True))

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
